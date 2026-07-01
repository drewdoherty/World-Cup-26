#!/usr/bin/env python
"""Localhost-only bridge: the bet-recs "PLACE" button -> the mini's fire script.

Runs ON THE DEV BOX (the MacBook).  It is a tiny stdlib ``http.server`` bound to
``127.0.0.1`` only (no external deps) whose single job is to take a POST from the
localhost bet-recs page and forward it to the canonical fire path on the mini
over SSH.

  POST /place   {"rec_id": "...", "nonce": "..."}    (JSON body)
      Header    X-WCA-Place-Token: <must equal env WCA_PLACE_TOKEN>
  GET  /health  -> {"ok": true, ...}

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
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "site", "bet_recs.json"),
)

# Hard USD cap forwarded to the mini fire (the mini clamps to its own backstop).
MAX_USD = os.environ.get("WCA_PLACE_MAX_USD", "100")

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
                "token_configured": bool(os.environ.get("WCA_PLACE_TOKEN")),
            })
            return
        self._send_json(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/place":
            self._send_json(404, {"ok": False, "message": "not found"})
            return
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
