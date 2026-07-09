#!/usr/bin/env python3
"""Mini-side ingest for in-play PM proposals — park + DM the fireable PM-<n>.

Runs on the MAC MINI (where ``pm_parked`` + the bot live). Two modes:

* **File scan** (default; the ``pminplayingest`` launchd job every 60 s, and a
  best-effort hook at the top of ``wca_pm_propose.py`` as a backstop): read the
  git-committed ``data/pm_inplay_proposals.json`` (written by the MacBook
  monitor's git artifact relay, landed here by autopull), park every proposal
  not yet ingested via :func:`wca.bot.app.push_parked_order` (the same path the
  propose CLI uses), and DM the admin the fireable ``Y PM-<n>`` prompt.

* **One-shot** (``--park-b64`` / ``--park-json``): the SSH relay path — park a
  single proposal passed on the command line and print its ``PM-<n>`` on
  stdout so the MacBook can include it in the immediate ping.

Safety
------
* This script NEVER places an order: parking only. Execution stays behind the
  human ``Y PM-<n>`` reply + ``PM_DRY_RUN`` in the bot, with the trader's
  static caps ($160/order, $1,000/day) enforced there.
* Idempotent: ingested uids are recorded in ``data/.pm_inplay_ingested.json``
  (mini-local, gitignored) — re-runs and autopull races never double-park.
* Staleness gate: in-play quotes die in minutes. Proposals older than
  ``--max-age-mins`` (default 45) are marked skipped (never parked) and the
  skip is DM'd so the human knows an edge was missed, not silently dropped.
* Note: the propose CLI's batch reset (``_reset_parked_for_new_batch``) clears
  ``pm_parked`` when a NEW pmpropose batch parks successfully — fire in-play
  ``PM-<n>``s promptly; a wiped park can be re-ingested only manually (the uid
  is already recorded).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

PROPOSALS_PATH = _REPO / "data" / "pm_inplay_proposals.json"
STATE_PATH = _REPO / "data" / ".pm_inplay_ingested.json"
DEFAULT_MAX_AGE_MINS = 45.0

#: Hard in-play notional ceiling re-checked at ingest (mirrors
#: ``wca.inplay.INPLAY_SAFETY_CAP_USD`` — duplicated as a literal so the mini
#: enforces it even if the artifact was hand-edited in transit).
INPLAY_SAFETY_CAP_USD = 100.0


def _load_dotenv(path: str = ".env") -> None:
    p = _REPO / path
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_state(path: Path = STATE_PATH) -> Dict[str, str]:
    """uid -> disposition ('parked PM-n' | 'skipped: ...')."""
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: Dict[str, str], path: Path = STATE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    except OSError:
        pass


def proposal_age_mins(proposal: Dict[str, Any], now: Optional[datetime] = None) -> float:
    """Minutes since the proposal was created (inf when unparseable —
    unparseable timestamps must fail the staleness gate, not pass it)."""
    ts = str(proposal.get("created_utc") or "")
    try:
        created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return float("inf")
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return ((now or _now()) - created).total_seconds() / 60.0


def validate_proposal(p: Dict[str, Any]) -> Optional[str]:
    """Gate-shape + safety check. Returns a rejection reason or None."""
    for key in ("uid", "token_id", "price", "size", "side"):
        if not p.get(key):
            return "missing %s" % key
    try:
        price = float(p["price"])
        size = float(p["size"])
    except (TypeError, ValueError):
        return "non-numeric price/size"
    if not (0.0 < price < 1.0) or size <= 0:
        return "price/size out of range"
    if str(p["side"]).upper() != "BUY":
        return "in-play relay only parks BUYs"
    if price * size > INPLAY_SAFETY_CAP_USD + 1e-6:
        return "notional %.2f exceeds in-play cap %.0f" % (price * size, INPLAY_SAFETY_CAP_USD)
    return None


def park_one(proposal: Dict[str, Any]) -> Tuple[str, str]:
    """Park via the bot's own queue. Returns ``(pm_token, confirmation_text)``."""
    from wca.bot.app import push_parked_order

    text = push_parked_order(dict(proposal))
    token = ""
    for word in text.split():
        if word.startswith("PM-"):
            token = word.strip("`.,")
            break
    return token, text


def _dm_admin(text: str) -> bool:
    admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
    if not admin or "PYTEST_CURRENT_TEST" in os.environ:
        return False
    try:
        from wca.bot.telegram import TelegramClient

        TelegramClient().send_message(admin, text)
        return True
    except Exception as exc:  # noqa: BLE001 — ingest never dies on a DM failure
        print("telegram DM failed: %s" % exc, file=sys.stderr)
        return False


def ingest_file(
    proposals_path: Path = PROPOSALS_PATH,
    state_path: Path = STATE_PATH,
    *,
    max_age_mins: float = DEFAULT_MAX_AGE_MINS,
    now: Optional[datetime] = None,
    park: Any = park_one,
    notify: Any = _dm_admin,
) -> Dict[str, List[str]]:
    """Scan the proposals artifact; park fresh un-ingested ones; DM outcomes.

    Returns ``{"parked": [uids], "skipped": [uids], "invalid": [uids]}``.
    Idempotent: every uid is recorded in the state file whatever its fate.
    """
    out: Dict[str, List[str]] = {"parked": [], "skipped": [], "invalid": []}
    try:
        doc = json.loads(proposals_path.read_text()) if proposals_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return out
    state = load_state(state_path)
    dirty = False

    for p in (doc.get("proposals") or []):
        if not isinstance(p, dict):
            continue
        uid = str(p.get("uid") or "")
        if not uid or uid in state:
            continue
        reason = validate_proposal(p)
        if reason is not None:
            state[uid] = "invalid: %s" % reason
            out["invalid"].append(uid)
            dirty = True
            continue
        age = proposal_age_mins(p, now=now)
        if age > max_age_mins:
            state[uid] = "skipped: stale (%.0f min old)" % age
            out["skipped"].append(uid)
            dirty = True
            notify(
                "⏭ *In-play proposal SKIPPED (stale)* — %s\n%s\n"
                "_%.0f min old (max %.0f); in-play quotes die fast — not parked._"
                % (p.get("match_desc", "?"), p.get("reason", ""), age, max_age_mins)
            )
            continue
        token, text = park(p)
        state[uid] = "parked %s" % (token or "?")
        out["parked"].append(uid)
        dirty = True
        basis = p.get("settlement_basis", "90-min")
        notify(
            "🚨 *In-play proposal (relay)* — %s\n_%s_\nsettles: %s basis\n\n%s"
            % (p.get("match_desc", "?"), p.get("reason", ""), basis, text)
        )

    if dirty:
        save_state(state, state_path)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Ingest in-play PM proposals on the mini (park + DM; never fires)")
    ap.add_argument("--park-json", default=None,
                    help="one-shot: park this single proposal (JSON string)")
    ap.add_argument("--park-b64", default=None,
                    help="one-shot: park this single proposal (base64 JSON — SSH-safe)")
    ap.add_argument("--proposals", default=str(PROPOSALS_PATH),
                    help="proposals artifact path (file-scan mode)")
    ap.add_argument("--state", default=str(STATE_PATH),
                    help="ingested-uid state file")
    ap.add_argument("--max-age-mins", type=float, default=DEFAULT_MAX_AGE_MINS,
                    help="skip proposals older than this (default 45)")
    ap.add_argument("--env", default=".env", help="dotenv file to load")
    args = ap.parse_args(argv)

    _load_dotenv(args.env)

    # --- one-shot (SSH relay) ---------------------------------------------
    raw = args.park_json
    if args.park_b64:
        try:
            raw = base64.b64decode(args.park_b64).decode("utf-8")
        except Exception:  # noqa: BLE001
            print("ERROR: undecodable --park-b64 payload", file=sys.stderr)
            return 1
    if raw is not None:
        try:
            proposal = json.loads(raw)
        except json.JSONDecodeError as exc:
            print("ERROR: bad proposal JSON: %s" % exc, file=sys.stderr)
            return 1
        reason = validate_proposal(proposal)
        if reason is not None:
            print("ERROR: proposal rejected: %s" % reason, file=sys.stderr)
            return 1
        age = proposal_age_mins(proposal)
        if age > args.max_age_mins:
            print("ERROR: proposal stale (%.0f min old > %.0f)" % (age, args.max_age_mins),
                  file=sys.stderr)
            return 1
        state = load_state(Path(args.state))
        uid = str(proposal.get("uid") or "")
        if uid in state:
            print("already ingested: %s (%s)" % (uid, state[uid]))
            # Re-print the recorded PM token so the SSH caller still gets it.
            print(state[uid].replace("parked ", ""))
            return 0
        token, text = park_one(proposal)
        state[uid] = "parked %s" % (token or "?")
        save_state(state, Path(args.state))
        _dm_admin(
            "🚨 *In-play proposal (ssh relay)* — %s\n_%s_\nsettles: %s basis\n\n%s"
            % (proposal.get("match_desc", "?"), proposal.get("reason", ""),
               proposal.get("settlement_basis", "90-min"), text)
        )
        print(token or "PM-?")
        return 0

    # --- file-scan mode -----------------------------------------------------
    res = ingest_file(Path(args.proposals), Path(args.state),
                      max_age_mins=args.max_age_mins)
    print("ingest: parked=%d skipped=%d invalid=%d"
          % (len(res["parked"]), len(res["skipped"]), len(res["invalid"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
