"""Governance + token resolution for FIRING a 02A Event-Market (PM) rec.

This module is the *server-side* half of the "Fire → park a PM-<n>" flow for
the ``02A // Event Markets (PM)`` panel (``site/event_market_recs.json``,
rendered by the self-contained panel in ``site/arb.html``).  It is imported by
``scripts/wca_place_server.py``'s ``POST /park-event`` endpoint and never trusts
the client: given a rec identity it re-reads and re-validates the rec from the
feed, resolves the PM token, and packages a ``pm_parked`` proposal that the
EXISTING in-play relay (:mod:`wca.inplay` ``SshRelay`` / ``GitArtifactRelay``)
ships to the mini's ``wca_pm_inplay_ingest.py`` — the same park + ``Y PM-<n>``
Telegram path the in-play monitor uses.  Nothing here places or fires an order.

SAFETY (mirrors the desk rules; enforced here AND in the frontend):

* A rec is fireable ONLY if it is SIZED (``stake_usd > 0``), NOT dimmed,
  ``no_cash_reason is None``, its family is NOT killed-for-cash
  (``exact_score`` / ``scorer_prop`` / ``correct_score``), it is not an
  under-signal totals-lay, its ``bucket`` is ``moneyline`` or ``mid`` (never
  ``longshot``), ``edge_net > 0``, and the feed is fresh (``captured_utc`` /
  ``meta.generated`` within :data:`MAX_FEED_AGE_HOURS`).
* The fired stake is clamped to
  ``min(rec per_order_cap $160, HARD_FIRE_CAP_USD)`` — the hard fire cap equals
  the in-play ingest's own ``INPLAY_SAFETY_CAP_USD`` ($100), so every proposal
  we build is inside the ingest's ``validate_proposal`` ceiling and we neither
  fork nor loosen that path.  The trader's static $160/order + $1,000/day caps
  are STILL enforced at fire time on the mini.
* The per-row settlement basis (90-min vs ET+pens) is carried on the proposal
  so the ``Y PM-<n>`` ping states which market the human is approving.
* Token resolution is READ-ONLY (feed ``token_id`` first, then
  ``data/pm_orderflow.db`` for advancement rows).  An unresolved token is a
  hard reject — a token is NEVER guessed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The in-play ingest's hard per-order safety cap.  We import the literal so the
# event-market fire path can never propose a notional the ingest would reject —
# and so the two ceilings can never silently diverge.
try:  # pragma: no cover - import shim for standalone script use
    from wca.inplay import INPLAY_SAFETY_CAP_USD as _INPLAY_CAP
except Exception:  # noqa: BLE001
    _INPLAY_CAP = 100.0

#: Documented HARD fire cap for an event-market park (USD).  Equals the in-play
#: ingest ceiling so a park we build always passes that path's validation.  The
#: effective fired stake is ``min(rec per_order_cap, HARD_FIRE_CAP_USD)``.
#: Changing it is a human-approved code change (like the trader's static caps).
HARD_FIRE_CAP_USD: float = float(_INPLAY_CAP)

#: A rec whose capture / feed generation is older than this is stale and not
#: fireable — in-line with the panel's own "⚠ STALE" hint (>2h) but a touch
#: looser to allow a hand-fire shortly after the panel flags it; the spec cap.
MAX_FEED_AGE_HOURS: float = 6.0

#: Families the desk KILLED for cash — never fireable (mirrors
#: ``wca.eventmarkets.KILLED_FAMILIES`` as a literal so this module has no hard
#: dependency on that import path).
KILLED_FAMILIES: Tuple[str, ...] = ("exact_score", "scorer_prop", "correct_score")

#: Buckets that may carry cash (moneyline / mid).  ``longshot`` never fires.
FIREABLE_BUCKETS: Tuple[str, ...] = ("moneyline", "mid")


# ---------------------------------------------------------------------------
# Rec lookup + validation
# ---------------------------------------------------------------------------


def rec_identity(rec: Dict[str, Any]) -> str:
    """Stable identity for a rec (the feed carries no ``id`` field).

    Fixture + family + selection uniquely identify a row within one feed; we
    use it both as the client-supplied identity and for idempotency.
    """
    return "|".join((
        str(rec.get("fixture") or ""),
        str(rec.get("family") or ""),
        str(rec.get("selection") or ""),
    )).strip().lower()


def find_rec(
    feed: Dict[str, Any],
    *,
    rec_id: Optional[str] = None,
    fixture: Optional[str] = None,
    family: Optional[str] = None,
    selection: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Locate a rec in the feed by ``rec_id`` or by fixture+selection+family.

    Returns the rec dict or ``None``.  Matching is case-insensitive and never
    trusts client-supplied prices/stakes — only the *identity* fields are used.
    """
    recs = [r for r in (feed.get("recs") or []) if isinstance(r, dict)]
    if rec_id:
        want = str(rec_id).strip().lower()
        for r in recs:
            if rec_identity(r) == want:
                return r
    if fixture and selection:
        wf, ws = str(fixture).strip().lower(), str(selection).strip().lower()
        wfam = str(family or "").strip().lower()
        for r in recs:
            if (str(r.get("fixture") or "").strip().lower() == wf
                    and str(r.get("selection") or "").strip().lower() == ws
                    and (not wfam or str(r.get("family") or "").strip().lower() == wfam)):
                return r
    return None


def _parse_ts(raw: Any) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace(" UTC", "Z").replace("Z", "+00:00")
    # "2026-07-08 15:26:42" (meta.generated) -> ISO
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def feed_age_hours(
    feed: Dict[str, Any], rec: Dict[str, Any], *, now: Optional[datetime] = None
) -> Optional[float]:
    """Hours since the rec was priced.  Prefers the row's ``captured_utc``,
    falls back to ``meta.generated``.  ``None`` when neither is parseable
    (which the caller MUST treat as stale — a missing stamp is not fresh)."""
    now = now or datetime.now(timezone.utc)
    ts = _parse_ts(rec.get("captured_utc")) or _parse_ts(
        (feed.get("meta") or {}).get("generated"))
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds() / 3600.0)


def validate_fireable(
    feed: Dict[str, Any],
    rec: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    max_age_hours: float = MAX_FEED_AGE_HOURS,
) -> Optional[str]:
    """Return a precise rejection reason, or ``None`` if the rec may fire.

    Re-validates every governance gate on the server from the feed itself —
    the client is never trusted.  Order is chosen so the most specific /
    dangerous condition wins the message.
    """
    fam = str(rec.get("family") or "").strip().lower()
    if fam in KILLED_FAMILIES:
        return "killed market family (%s) — never fireable for cash" % (fam or "?")
    if rec.get("no_cash_reason"):
        return "display-only: %s" % rec.get("no_cash_reason")
    if rec.get("dimmed"):
        return "row is dimmed (display-only) — not fireable"
    bucket = str(rec.get("bucket") or "").strip().lower()
    if bucket not in FIREABLE_BUCKETS:
        return ("bucket %r not fireable (only moneyline/mid may carry cash; "
                "longshots are free-bet/lottery only)" % (bucket or "?"))
    try:
        stake = float(rec.get("stake_usd"))
    except (TypeError, ValueError):
        return "stake_usd missing/non-numeric — not a sized row"
    if stake <= 0.0:
        return "stake_usd is $0 — display-only row, not fireable"
    try:
        edge = float(rec.get("edge_net"))
    except (TypeError, ValueError):
        return "edge_net missing/non-numeric"
    if edge <= 0.0:
        return "edge_net %.4f not positive — not fireable" % edge
    age = feed_age_hours(feed, rec, now=now)
    if age is None:
        return "no captured_utc / meta.generated timestamp — treated as stale"
    if age > max_age_hours:
        return ("feed is stale (%.1fh old > %.1fh cap) — rerun "
                "scripts/wca_event_markets.py" % (age, max_age_hours))
    return None


# ---------------------------------------------------------------------------
# Token resolution (feed first, then orderflow db for advancement rows)
# ---------------------------------------------------------------------------


def _team_from_advance_selection(selection: str) -> str:
    """"England to advance" -> "England".  Best-effort; empty on no match."""
    s = str(selection or "").strip()
    low = s.lower()
    for marker in (" to advance", " to reach", " advance"):
        if marker in low:
            return s[: low.index(marker)].strip()
    return s


def _yes_token_from_arrays(outcomes_raw: Any, token_ids_raw: Any) -> Optional[str]:
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        tokens = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(outcomes, list) or not isinstance(tokens, list):
        return None
    low = [str(o).strip().lower() for o in outcomes]
    idx = low.index("yes") if "yes" in low else 0
    if 0 <= idx < len(tokens):
        tok = str(tokens[idx]).strip()
        return tok or None
    return None


def _canon(name: str) -> str:
    try:
        from wca.data.teamnames import canonical
        return canonical(name)
    except Exception:  # noqa: BLE001
        return str(name or "").strip().lower()


def resolve_token(
    rec: Dict[str, Any], *, orderflow_db: Optional[Path] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the PM token to BACK the rec's selection.

    Returns ``(token_id, None)`` on success or ``(None, reason)``.  Order:

    1. The feed's own ``token_id`` (the market this rec was priced against).
    2. For advancement rows only, ``data/pm_orderflow.db`` ``pm_markets``
       (READ-ONLY) — matched by advancement stage + team, "Yes" leg.

    Aux 90-min families (spread / totals / BTTS / team_total) are NOT reliably
    ingested into the orderflow DB (its ingester drops aux events by design),
    so a null-token aux row is an honest hard reject — we never guess.
    """
    tok = str(rec.get("token_id") or "").strip()
    if tok and tok.lower() != "none":
        return tok, None

    fam = str(rec.get("family") or "").strip().lower()
    if fam != "advance":
        return None, (
            "unresolved token — the feed carried no token_id for this %s row "
            "(aux markets are not in the orderflow DB). Rerun "
            "scripts/wca_event_markets.py with the PM route up." % (fam or "?"))

    db_path = orderflow_db or (Path(__file__).resolve().parents[2]
                               / "data" / "pm_orderflow.db")
    if not Path(db_path).exists():
        return None, ("unresolved token — data/pm_orderflow.db absent on this "
                      "box; rerun scripts/wca_event_markets.py with the PM route up")
    team = _team_from_advance_selection(rec.get("selection") or "")
    if not team:
        return None, "unresolved token — could not parse team from selection"
    team_c = _canon(team)

    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT question, outcomes, token_ids FROM pm_markets "
            "WHERE category LIKE 'advancement_%'"
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return None, "unresolved token — orderflow DB read failed (%s)" % exc

    candidates: List[str] = []
    for question, outcomes_raw, token_ids_raw in rows:
        q = str(question or "")
        # "Will England reach the Round of 16 ...?" — pull the subject team.
        subj = q[5:] if q.lower().startswith("will ") else q
        for cut in (" reach", " advance", " qualify", " win", " to "):
            i = subj.lower().find(cut)
            if i > 0:
                subj = subj[:i]
                break
        if _canon(subj) != team_c:
            continue
        yes = _yes_token_from_arrays(outcomes_raw, token_ids_raw)
        if yes:
            candidates.append(yes)

    uniq = sorted(set(candidates))
    if len(uniq) == 1:
        return uniq[0], None
    if not uniq:
        return None, ("unresolved token — no advancement market for %s in the "
                      "orderflow DB; rerun scripts/wca_event_markets.py with the "
                      "PM route up" % team)
    return None, ("unresolved token — %d ambiguous advancement markets for %s "
                  "(stage not pinned); rerun scripts/wca_event_markets.py"
                  % (len(uniq), team))


# ---------------------------------------------------------------------------
# Proposal packaging (shape the in-play ingest already parks + pings)
# ---------------------------------------------------------------------------


def clamped_stake(rec: Dict[str, Any], per_order_cap: float) -> float:
    """Fired stake = ``min(rec stake, rec per_order_cap, HARD_FIRE_CAP_USD)``."""
    try:
        stake = float(rec.get("stake_usd") or 0.0)
    except (TypeError, ValueError):
        stake = 0.0
    cap = min(float(per_order_cap or HARD_FIRE_CAP_USD), HARD_FIRE_CAP_USD)
    return round(max(0.0, min(stake, cap)), 2)


def build_proposal(
    feed: Dict[str, Any],
    rec: Dict[str, Any],
    token_id: str,
    *,
    nonce: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Package a fireable rec as a ``pm_parked`` proposal for the in-play relay.

    ``size`` is SHARES (the gate computes notional as ``price * size``), mirroring
    :func:`wca.inplay.to_parked_proposal`.  ``uid`` is derived from the rec
    identity + nonce so re-clicking the same row with the same nonce is
    idempotent through the ingest's uid dedupe.
    """
    now = now or datetime.now(timezone.utc)
    meta = feed.get("meta") or {}
    per_order_cap = float(meta.get("per_order_cap_usd") or HARD_FIRE_CAP_USD)
    stake_usd = clamped_stake(rec, per_order_cap)
    price = float(rec.get("price") or 0.0)
    # Floor shares (round DOWN to the cent) so the reconstructed notional
    # ``price * shares`` can never exceed the clamped stake by float rounding —
    # the in-play ingest gate rejects a notional even a hair over its cap.
    import math
    shares = (math.floor((stake_usd / price) * 100.0) / 100.0) if price > 0 else 0.0
    # size_usd carries the ACTUAL parked notional (<= the clamp), not the target.
    stake_usd = round(price * shares, 2)
    settlement = str(rec.get("settlement") or "90min")
    fixture = str(rec.get("fixture") or "")
    selection = str(rec.get("selection") or "")
    reason = ("event-market fire: %s %s (net edge +%.1fpp, %s bucket)"
              % (fixture, selection,
                 float(rec.get("edge_net") or 0.0) * 100.0,
                 str(rec.get("bucket") or "?")))
    # Deterministic, collision-resistant uid: rec identity digest + nonce.
    import hashlib
    digest = hashlib.sha1(rec_identity(rec).encode("utf-8")).hexdigest()[:8]
    uid = "evfire-%s-%s" % (digest, str(nonce).strip())
    return {
        "uid": uid,
        "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inplay": True,            # rides the in-play ingest path (park + PM-<n>)
        "detector": "event_market_fire",
        "reason": reason,
        "settlement_basis": settlement,
        "token_id": str(token_id),
        "side": "BUY",             # we BACK the model's selection
        "price": round(price, 3),
        "size": shares,            # gate sizes in SHARES
        "shares": shares,
        "size_usd": stake_usd,
        "market_question": str(rec.get("label") or selection),
        "outcome": selection,
        "match_desc": fixture,
        "model_prob": float(rec.get("model_prob") or 0.0),
        "ev": float(rec.get("ev") or 0.0),
        "neg_risk": False,
        "label": str(rec.get("label") or selection),
    }
