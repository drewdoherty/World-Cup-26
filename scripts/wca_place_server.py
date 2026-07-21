#!/usr/bin/env python
"""Localhost-only bridge: the bet-recs "PLACE" button -> the mini's fire script.

Runs ON THE DEV BOX (the MacBook).  It is a tiny stdlib ``http.server`` bound to
``127.0.0.1`` only (no external deps) whose single job is to take a POST from the
localhost bet-recs page and forward it to the canonical fire path on the mini
over SSH.

  POST /place        {"rec_id": "...", "nonce": "..."}    (JSON body)
      Header         X-WCA-Place-Token: <must equal env WCA_PLACE_TOKEN>
  POST /park-event   {"rec_id" | fixture+selection+family, "nonce"}
      Fire a SIZED 02A Event-Market (PM) rec: re-validate from
      site/event_market_recs.json, resolve the PM token, and PARK a PM-<n>
      via the in-play relay (SSH->mini ingest, else git-artifact fallback).
      NEVER places; the human's "Y PM-<n>" in @gamble1_bot is the only fire.
  GET  /health       -> {"ok": true, ...}

Guardrails (all enforced BEFORE any SSH):
  * bind 127.0.0.1 only; a non-loopback client is rejected 403.
  * shared-secret header must match ``WCA_PLACE_TOKEN`` (403 on mismatch/unset).
  * the rec is re-read from ``site/bet_recs.json`` and re-validated (must be an
    actionable, non-stale ADD) here too — defence in depth; the mini validates
    again.
  * DRY-RUN by default: the server forwards its OWN process ``PM_DRY_RUN`` value
    (default "1") into the remote command. It NEVER hardcodes PM_DRY_RUN=0.

If the mini is unreachable the server returns a clean JSON error (never a
half-state / never a partial success).

SAFETY: to go live the human must start THIS server with ``PM_DRY_RUN=0`` in its
environment (and the mini must have the live key). By default the whole path is
dry-run.

Usage
-----
    # dry-run (default)
    WCA_PLACE_TOKEN=some-shared-secret python scripts/wca_place_server.py
    # go live (human, explicit):
    #   PM_DRY_RUN=0 WCA_PLACE_TOKEN=... python scripts/wca_place_server.py
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Make ``wca`` importable when this script is launched directly (dev box).
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# ---------------------------------------------------------------------------
# Config (env-overridable; all have safe defaults)
# ---------------------------------------------------------------------------

BIND_HOST = "127.0.0.1"
BIND_PORT = int(os.environ.get("WCA_PLACE_PORT", "8010"))

MINI_HOST = os.environ.get("WCA_MINI_HOST", "andrewdoherty@Drews-Mac-mini.local")
MINI_REPO = os.environ.get("WCA_MINI_REPO", "World-Cup-26")
MINI_PY = os.environ.get("WCA_MINI_PY", ".venv/bin/python")
MINI_DB = os.environ.get("WCA_MINI_DB", "data/wca.db")
MINI_BET_RECS = os.environ.get("WCA_MINI_BET_RECS", "site/bet_recs.json")

# Local copy of bet_recs.json for the dev-side pre-validation (defence in depth).
LOCAL_BET_RECS = os.environ.get(
    "WCA_LOCAL_BET_RECS",
    os.path.join(_REPO_ROOT, "site", "bet_recs.json"),
)

# Local copy of the 02A event-market feed for /park-event re-validation.
LOCAL_EVENT_RECS = os.environ.get(
    "WCA_LOCAL_EVENT_RECS",
    os.path.join(_REPO_ROOT, "site", "event_market_recs.json"),
)

# Read-only orderflow DB used to resolve null-token advancement rows.
LOCAL_ORDERFLOW_DB = os.environ.get(
    "WCA_LOCAL_ORDERFLOW_DB",
    os.path.join(_REPO_ROOT, "data", "pm_orderflow.db"),
)

# Hard USD cap forwarded to the mini fire (the mini clamps to its own backstop).
MAX_USD = os.environ.get("WCA_PLACE_MAX_USD", "160")

SSH_TIMEOUT_SECS = int(os.environ.get("WCA_PLACE_SSH_TIMEOUT", "60"))


def _pm_dry_run_value() -> str:
    """The PM_DRY_RUN string to forward to the mini — default '1' (dry-run).

    Anything other than an explicit disable stays '1'. This mirrors
    ``wca.bot.app._pm_dry_run`` semantics so the two ends agree.
    """
    raw = os.environ.get("PM_DRY_RUN", "1").strip().lower()
    return "0" if raw in {"0", "false", "no"} else "1"


# ---------------------------------------------------------------------------
# Rec pre-validation (mirror of the mini's checks; never trusts the client)
# ---------------------------------------------------------------------------

def _load_and_validate(rec_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (rec, None) if the rec is a fireable ADD, else (None, reason)."""
    try:
        with open(LOCAL_BET_RECS, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return None, "could not read local bet_recs (%s)" % exc
    for r in (data.get("advancement_futures") or []):
        if str(r.get("id")) == str(rec_id):
            if str(r.get("action_label")) != "ADD":
                return None, "rec is not an ADD (action_label=%r)" % r.get("action_label")
            if r.get("stale"):
                return None, "rec is stale (%s)" % (r.get("stale_reason") or "stale")
            if str(r.get("venue")) != "polymarket":
                return None, "rec venue is not polymarket"
            # Mirror of wca_pm_fire._validate_rec's side guard (2026-07-14):
            # the fire path buys the YES token only — refuse a NO-side rec
            # here too rather than round-tripping to the mini to be refused.
            if str(r.get("side") or "YES").strip().upper() != "YES":
                return None, ("rec is NO-side — the fire path buys YES "
                              "tokens only; trade NO positions via the "
                              "bot's parked-order path")
            return r, None
    return None, "rec id %r not found in bet_recs" % rec_id


# ---------------------------------------------------------------------------
# Remote fire over SSH
# ---------------------------------------------------------------------------

def _fire_on_mini(rec_id: str, nonce: str) -> Dict[str, Any]:
    """SSH to the mini and run wca_pm_fire.py; return its parsed JSON.

    Forwards THIS process's PM_DRY_RUN (default '1') — never hardcodes 0. On any
    transport failure returns a clean JSON error dict (never a half-state).
    """
    dry = _pm_dry_run_value()
    # Build the remote command. rec_id/nonce are shell-quoted; they originate
    # from our own validated bet_recs id and a server-checked nonce, but we quote
    # regardless so nothing can break out of the remote shell word.
    remote = (
        "cd %s && PM_DRY_RUN=%s %s scripts/wca_pm_fire.py "
        "--rec-id %s --max-usd %s --db %s --bet-recs %s --nonce %s"
        % (
            shlex.quote(MINI_REPO),
            dry,
            shlex.quote(MINI_PY),
            shlex.quote(rec_id),
            shlex.quote(str(MAX_USD)),
            shlex.quote(MINI_DB),
            shlex.quote(MINI_BET_RECS),
            shlex.quote(nonce),
        )
    )
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        MINI_HOST,
        remote,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "dry_run": dry == "1",
            "message": "mini unreachable — SSH timed out after %ds (no order placed)"
            % SSH_TIMEOUT_SECS,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "dry_run": dry == "1",
            "message": "mini unreachable — SSH failed (%s); no order placed" % exc,
        }

    # The fire script prints exactly one JSON line on stdout. Parse the LAST
    # non-empty line so any incidental remote chatter doesn't corrupt the parse.
    out = (proc.stdout or "").strip()
    last = ""
    for line in out.splitlines():
        if line.strip():
            last = line.strip()
    if not last:
        return {
            "ok": False, "dry_run": dry == "1",
            "message": "mini returned no JSON (rc=%s); stderr=%s"
            % (proc.returncode, (proc.stderr or "").strip()[:400]),
        }
    try:
        return json.loads(last)
    except Exception:  # noqa: BLE001
        return {
            "ok": False, "dry_run": dry == "1",
            "message": "mini returned unparseable output: %s" % last[:400],
        }


# ---------------------------------------------------------------------------
# /park-event — FIRE a sized 02A Event-Market rec (park a PM-<n>, never place)
#
# Re-reads + re-validates the rec from site/event_market_recs.json (never the
# client), resolves the PM token (feed first, then the read-only orderflow DB
# for advancement rows), packages a pm_parked proposal, and ships it via the
# EXISTING in-play relay (wca.inplay SshRelay -> mini ingest, else the git
# artifact fallback).  The human's "Y PM-<n>" in @gamble1_bot is the only fire.
# ---------------------------------------------------------------------------


def _load_event_feed() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        with open(LOCAL_EVENT_RECS, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except Exception as exc:  # noqa: BLE001
        return None, "could not read event_market_recs.json (%s)" % exc


def _park_event(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate, resolve, and relay-park one event-market rec.

    Returns a clean JSON dict (``ok`` carries the real outcome). Never raises;
    a mini-unreachable relay is a complete answer (git-artifact fallback), not
    a half-state.
    """
    from wca import eventfire

    dry = _pm_dry_run_value() == "1"
    nonce = str(body.get("nonce") or "").strip()
    if not nonce:
        return {"ok": False, "dry_run": dry, "message": "nonce required"}

    feed, err = _load_event_feed()
    if feed is None:
        return {"ok": False, "dry_run": dry, "message": err}

    rec = eventfire.find_rec(
        feed,
        rec_id=(str(body.get("rec_id")).strip() if body.get("rec_id") else None),
        fixture=body.get("fixture"),
        family=body.get("family"),
        selection=body.get("selection"),
    )
    if rec is None:
        return {"ok": False, "dry_run": dry,
                "message": "rec not found in event_market_recs.json"}

    reason = eventfire.validate_fireable(feed, rec)
    if reason is not None:
        return {"ok": False, "dry_run": dry, "message": "not fireable — %s" % reason}

    from pathlib import Path
    token_id, tok_reason = eventfire.resolve_token(
        rec, orderflow_db=Path(LOCAL_ORDERFLOW_DB))
    if token_id is None:
        return {"ok": False, "dry_run": dry, "message": tok_reason}

    proposal = eventfire.build_proposal(feed, rec, token_id, nonce=nonce)

    # Ship via the SAME in-play relay the monitor uses: SSH to the mini when
    # reachable (instant PM-<n>), else the git-artifact fallback (fireable
    # after the ~5-min autopull; the mini ingest DMs the PM-<n> then).
    from wca import inplay

    ssh = inplay.SshRelay()
    git = inplay.GitArtifactRelay(_REPO_ROOT)
    relay = inplay.select_relay(ssh, git)
    res = relay.park(proposal)

    settle = proposal["settlement_basis"]
    settle_lbl = "ET+pens" if settle in ("ET+pens", "advance") else "90 min"
    price_c = round(proposal["price"] * 100, 1)
    if not res.ok:
        return {
            "ok": False, "dry_run": dry, "relay": res.relay,
            "message": "park failed via %s: %s" % (res.relay, res.detail),
        }
    if res.pm_token:  # synchronous (ssh) — PM-<n> known now
        return {
            "ok": True, "dry_run": dry, "relay": res.relay,
            "pm_tag": res.pm_token, "settlement": settle_lbl,
            "message": ("%s — %s %s $%s @ %s¢ [settles %s]. Approve with Y %s"
                        % (res.pm_token, proposal["match_desc"],
                           proposal["outcome"], proposal["size_usd"],
                           price_c, settle_lbl, res.pm_token)),
        }
    # git-artifact fallback — parked to origin/main; fireable after mini sync.
    return {
        "ok": True, "dry_run": dry, "relay": res.relay, "pm_tag": None,
        "settlement": settle_lbl, "pending_sync": True,
        "message": ("parked to origin/main via git relay — %s. The mini's "
                    "ingest will DM the fireable PM-<n> after autopull (~≤6min)."
                    % res.detail),
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "wca-place/1.0"

    # Quieter logging — one concise line per request.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[wca-place] %s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Only the localhost site talks to us; keep CORS tight to loopback.
        self.send_header("Access-Control-Allow-Origin", "http://localhost")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-WCA-Place-Token")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _is_loopback(self) -> bool:
        host = (self.client_address[0] if self.client_address else "") or ""
        return host in ("127.0.0.1", "::1", "localhost")

    def do_OPTIONS(self) -> None:  # noqa: N802 — CORS preflight
        self._send_json(204, {})

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/health":
            self._send_json(200, {
                "ok": True,
                "service": "wca-place",
                "dry_run": _pm_dry_run_value() == "1",
                "mini_host": MINI_HOST,
                "max_usd": MAX_USD,
                "endpoints": ["/place", "/park-event"],
                "token_configured": bool(os.environ.get("WCA_PLACE_TOKEN")),
            })
            return
        self._send_json(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/place", "/park-event"):
            self._send_json(404, {"ok": False, "message": "not found"})
            return
        # Shared guardrails (BOTH endpoints): loopback-only, then token gate.
        if not self._is_loopback():
            self._send_json(403, {"ok": False, "message": "loopback only"})
            return
        expected = os.environ.get("WCA_PLACE_TOKEN")
        got = self.headers.get("X-WCA-Place-Token")
        if not expected or not got or got != expected:
            self._send_json(403, {"ok": False, "message": "bad or missing place token"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"ok": False, "message": "bad JSON body (%s)" % exc})
            return

        if path == "/park-event":
            self._handle_park_event(body)
            return

        # --- /place (UNCHANGED) ---------------------------------------------
        rec_id = str(body.get("rec_id") or "").strip()
        nonce = str(body.get("nonce") or "").strip()
        if not rec_id or not nonce:
            self._send_json(400, {"ok": False, "message": "rec_id and nonce required"})
            return

        rec, reason = _load_and_validate(rec_id)
        if rec is None:
            self._send_json(400, {"ok": False, "message": reason})
            return

        result = _fire_on_mini(rec_id, nonce)
        # Always 200 at the transport layer for a well-formed request; the JSON
        # ``ok`` field carries the real outcome so the UI never has to parse HTTP
        # status for business logic. A refusal is still a clean, complete answer.
        self._send_json(200, result)

    def _handle_park_event(self, body: Dict[str, Any]) -> None:
        """POST /park-event — park a fireable 02A event-market rec (never fires)."""
        nonce = str(body.get("nonce") or "").strip()
        has_id = bool(str(body.get("rec_id") or "").strip())
        has_pair = bool(str(body.get("fixture") or "").strip()
                        and str(body.get("selection") or "").strip())
        if not nonce or not (has_id or has_pair):
            self._send_json(400, {
                "ok": False,
                "message": "nonce and (rec_id OR fixture+selection) required"})
            return
        try:
            result = _park_event(body)
        except Exception as exc:  # noqa: BLE001 — never a half-state / 500
            result = {"ok": False, "dry_run": _pm_dry_run_value() == "1",
                      "message": "park-event failed cleanly: %s" % exc}
        self._send_json(200, result)


def main() -> int:
    if not os.environ.get("WCA_PLACE_TOKEN"):
        sys.stderr.write(
            "[wca-place] WARNING: WCA_PLACE_TOKEN is unset — every /place request "
            "will be rejected 403 until you export a shared secret.\n"
        )
    dry = _pm_dry_run_value()
    sys.stderr.write(
        "[wca-place] listening on http://%s:%d  (PM_DRY_RUN=%s -> %s, mini=%s)\n"
        % (BIND_HOST, BIND_PORT, dry, "DRY-RUN" if dry == "1" else "LIVE", MINI_HOST)
    )
    httpd = ThreadingHTTPServer((BIND_HOST, BIND_PORT), _Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
