"""Smarkets-side price data (READ-ONLY).

Smarkets is a GBP betting exchange with a native REST API (api.smarkets.com)
that exposes real order-book depth (back AND lay), so — unlike the back-odds-
only Odds API/Betfair feed — a matched Smarkets opportunity can be
EXECUTION-GRADE (you can see the lay side and available volume).

Sourcing, in order:
  1. native Smarkets API if a session/token is configured (SMARKETS_API_TOKEN) —
     gives back+lay+depth → confidence "execution-grade";
  2. else The Odds API ``smarkets`` bookmaker feed (back odds only) → a clearly
     documented DOWNGRADE to "monitoring-grade".

READ-ONLY: no order placement. The boundary is explicit in
:func:`smarkets_execution_stub` (mirrors the Betfair stub).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from wca.data import theoddsapi

logger = logging.getLogger(__name__)

GBP = "GBP"
SMARKETS_API = "https://api.smarkets.com/v3"
ODDSAPI_SMARKETS_KEY = "smarkets"
_SESSIONS_URL = SMARKETS_API + "/sessions/"
_TIMEOUT = 20

# In-process cache of a session token minted from username/password so we do
# not re-login per call within a single sync run.
_CACHED_SESSION: Optional[str] = None


def have_native_session() -> bool:
    """True when a native Smarkets token is configured (back+lay+depth)."""
    return bool(os.environ.get("SMARKETS_API_TOKEN", "").strip())


def session_login() -> Optional[str]:
    """Return a Smarkets API session token, or ``None`` if unavailable.

    Resolution order (first that works wins; failures return ``None``, never
    raise):
      1. an explicit ``SMARKETS_API_TOKEN`` (long-lived token);
      2. a token cached earlier in this process;
      3. POST ``SMARKETS_USERNAME``/``SMARKETS_PASSWORD`` to
         ``/v3/sessions/`` to mint a fresh token.
    """
    global _CACHED_SESSION
    token = os.environ.get("SMARKETS_API_TOKEN", "").strip()
    if token:
        return token
    if _CACHED_SESSION:
        return _CACHED_SESSION
    user = os.environ.get("SMARKETS_USERNAME", "").strip()
    pwd = os.environ.get("SMARKETS_PASSWORD", "").strip()
    if not (user and pwd):
        return None
    try:
        import requests

        # Documented /v3/sessions/ create-session body (docs.smarkets.com).
        # A persistent 401 AFTER these fields is an account-level issue (API
        # access / 2FA / password), not a payload bug.
        resp = requests.post(
            _SESSIONS_URL,
            json={
                "username": user,
                "password": pwd,
                "remember": True,
                "reopen_account": False,
                "use_auth_v2": False,
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json() or {}
        # Smarkets returns the session token under "token" (alias "session").
        tok = body.get("token") or body.get("session")
        if tok:
            _CACHED_SESSION = tok
            return tok
        logger.warning("Smarkets session login: no token in response")
    except Exception as exc:  # noqa: BLE001 — login is best-effort.
        logger.warning("Smarkets session login failed: %s", exc)
    return None


def _auth_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": "Session-Token " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _normalise_smk_position(p: Dict[str, Any], account: str = "1") -> Optional[Dict[str, Any]]:
    """Map one Smarkets open contract/order into a normalised position.

    Smarkets quotes prices in percent (e.g. 25.0 -> implied 0.25 -> decimal 4.0).
    Pure (no I/O) so it is unit-testable against sample payloads. Returns
    ``None`` when there is no matched/open quantity.
    """
    qty = float(p.get("quantity") or p.get("matched_quantity") or 0.0)
    if qty <= 0:
        return None
    price_pct = p.get("avg_price") or p.get("price")
    decimal_odds = None
    try:
        if price_pct:
            implied = float(price_pct) / 100.0
            decimal_odds = (1.0 / implied) if implied > 0 else None
    except (TypeError, ValueError):
        decimal_odds = None
    return {
        "venue": "smarkets",
        "market": p.get("market_name") or str(p.get("market_id") or ""),
        "selection": p.get("contract_name") or p.get("name") or str(p.get("contract_id") or ""),
        "fixture_or_event": p.get("event_name") or "",
        "stake": qty,
        "size": qty,
        "avg_price": decimal_odds,
        "odds": decimal_odds,
        "current_value": None,
        "current_price": None,
        "external_id": str(p.get("id") or p.get("contract_id") or ""),
        "account": account,
        "side": (p.get("side") or "").lower(),
    }


def list_open_positions(*, account: str = "1") -> List[Dict[str, Any]]:
    """Return the account's open Smarkets positions, normalised.

    READ-ONLY. Logs in (session_login) then queries the account's open
    contracts. DEGRADES GRACEFULLY — returns ``[]`` and logs (never raises) on
    auth/network failure.

    NOTE: the exact Smarkets positions endpoint shape is INFERRED here
    (``GET /v3/positions/`` returning ``{"positions": [...]}`` with percent
    prices). If the live API differs, only ``_normalise_smk_position`` and the
    URL/JSON-key below need adjusting — the reconcile engine is unaffected.
    """
    token = session_login()
    if not token:
        logger.info("Smarkets positions disabled — no SMARKETS_API_TOKEN or "
                    "SMARKETS_USERNAME/PASSWORD configured")
        return []
    try:
        import requests

        resp = requests.get(
            SMARKETS_API + "/positions/",
            headers=_auth_headers(token),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json() or {}
        raw = body.get("positions") or body.get("data") or []
        out: List[Dict[str, Any]] = []
        for p in raw:
            norm = _normalise_smk_position(p, account=account)
            if norm is not None:
                out.append(norm)
        logger.info("Smarkets positions: %d open of %d", len(out), len(raw))
        return out
    except Exception as exc:  # noqa: BLE001 — never crash the sync.
        logger.warning("Smarkets list_open_positions failed (degrading to empty): %s", exc)
        return []


def _normalise_smk_settled(p: Dict[str, Any], account: str = "1") -> Optional[Dict[str, Any]]:
    """Map one Smarkets SETTLED position into the normalised settled shape.

    INFERRED SHAPE CAVEAT: the live Smarkets v3 settled-positions payload is not
    available in this environment, so the field names below are best-effort
    guesses (``realised_profit``/``settled_profit`` for P&L in GBP minor units,
    ``settled_at`` for the timestamp, ``settled``/``state`` to confirm the bet is
    actually settled). The conservatism guard is deliberate: we ONLY treat a
    position as an unambiguous settle when (a) it is flagged settled AND (b) a
    numeric realised P&L is present. Anything else returns ``None`` so the
    reconcile engine routes it to ``review`` rather than auto-settling. If the
    live API differs, ONLY this normaliser + the URL/JSON-keys in
    :func:`list_settled_positions` need adjusting — the reconcile engine is
    unaffected. Pure (no I/O) so it is unit-testable.

    Smarkets quotes monetary amounts in pennies (minor units); we convert P&L to
    major units (GBP) here.
    """
    state = str(p.get("state") or p.get("status") or "").strip().lower()
    is_settled = bool(p.get("settled")) or state in ("settled", "closed", "resolved")
    if not is_settled:
        return None
    raw_pnl = p.get("realised_profit")
    if raw_pnl is None:
        raw_pnl = p.get("settled_profit")
    if raw_pnl is None:
        raw_pnl = p.get("profit")
    try:
        # Smarkets monetary fields are integer pennies → convert to GBP.
        pnl = float(raw_pnl) / 100.0
    except (TypeError, ValueError):
        return None  # no usable realised P&L -> not an unambiguous settle.
    # Result is taken from an explicit outcome when present; otherwise inferred
    # from the sign of the realised P&L (a settle with +pnl won, <=0 lost).
    outcome = str(p.get("outcome") or p.get("result") or "").strip().lower()
    if outcome in ("won", "win", "winner"):
        result = "won"
    elif outcome in ("lost", "lose", "loser"):
        result = "lost"
    else:
        result = "won" if pnl > 0 else "lost"
    return {
        "venue": "smarkets",
        "market": p.get("market_name") or str(p.get("market_id") or ""),
        "selection": p.get("contract_name") or p.get("name") or str(p.get("contract_id") or ""),
        "fixture_or_event": p.get("event_name") or "",
        "stake": None,
        "size": None,
        "avg_price": None,
        "odds": None,
        "settled_pnl": pnl,
        "result": result,
        "settled_ts": p.get("settled_at") or p.get("settled_date"),
        "external_id": str(p.get("id") or p.get("contract_id") or ""),
        "account": account,
        "side": (p.get("side") or "").lower(),
    }


def list_settled_positions(*, since_hours: int = 24, account: str = "1") -> List[Dict[str, Any]]:
    """Return the account's SETTLED Smarkets positions over the last
    ``since_hours``, normalised. READ-ONLY; degrades to ``[]`` (never raises).

    INFERRED ENDPOINT CAVEAT: the exact Smarkets settled-positions endpoint is
    not exercised here. We query ``GET /v3/positions/?state=settled`` with a
    ``settled_since`` cutoff and read ``{"positions": [...]}`` (falling back to
    ``data``). The window is applied client-side from ``settled_at`` too, in case
    the server ignores ``settled_since``. If the live API differs, only the URL /
    JSON-keys here and :func:`_normalise_smk_settled` need adjusting.
    """
    token = session_login()
    if not token:
        logger.info("Smarkets settled positions disabled — no SMARKETS_API_TOKEN or "
                    "SMARKETS_USERNAME/PASSWORD configured")
        return []
    import datetime as _dt

    cutoff = _dt.datetime.utcnow() - _dt.timedelta(hours=max(1, int(since_hours)))
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        import requests

        resp = requests.get(
            SMARKETS_API + "/positions/",
            params={"state": "settled", "settled_since": cutoff_iso},
            headers=_auth_headers(token),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json() or {}
        raw = body.get("positions") or body.get("data") or []
        out: List[Dict[str, Any]] = []
        for p in raw:
            norm = _normalise_smk_settled(p, account=account)
            if norm is None:
                continue
            if not _within_window(norm.get("settled_ts"), cutoff):
                continue  # belt-and-braces client-side window filter.
            out.append(norm)
        logger.info("Smarkets settled: %d of %d (since %s)", len(out), len(raw), cutoff_iso)
        return out
    except Exception as exc:  # noqa: BLE001 — never crash the sync.
        logger.warning("Smarkets list_settled_positions failed (degrading to empty): %s", exc)
        return []


def _within_window(settled_ts: Any, cutoff: "Any") -> bool:
    """True when ``settled_ts`` is at/after ``cutoff`` (or unparseable → keep).

    Unparseable timestamps are kept (the server-side filter already scoped the
    query); only a parseable timestamp strictly older than the cutoff is dropped.
    """
    if not settled_ts:
        return True
    import datetime as _dt

    s = str(settled_ts).strip().replace("Z", "+00:00")
    for parse in (
        lambda v: _dt.datetime.fromisoformat(v),
        lambda v: _dt.datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S"),
    ):
        try:
            ts = parse(s)
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            return ts >= cutoff
        except (ValueError, TypeError):
            continue
    return True


def smarkets_odds(
    sport_key: str = "soccer_fifa_world_cup",
    *,
    markets: str = "h2h",
) -> Tuple[pd.DataFrame, str]:
    """Return (frame, grade). grade ∈ {"execution-grade","monitoring-grade"}.

    Native path is intentionally a documented stub here (no token/network in
    this env); falls back to The Odds API ``smarkets`` feed. Either way the frame
    matches the shared odds shape, tagged currency=GBP and a ``lay_odds`` column
    (populated only on the native path).
    """
    if have_native_session():
        # Native back+lay+depth would be fetched here. Left unimplemented in this
        # environment (no token); documented downgrade path is used instead.
        pass
    df, _ = theoddsapi.get_odds(sport_key, regions="uk", markets=markets, odds_format="decimal")
    return filter_smarkets(df), "monitoring-grade"


def filter_smarkets(df: pd.DataFrame) -> pd.DataFrame:
    """Filter a flat Odds-API frame to Smarkets rows; tag GBP. Back-only feed."""
    if df is None or df.empty or "bookmaker_key" not in df.columns:
        out = df.copy() if df is not None else pd.DataFrame()
        for c in ("currency", "lay_odds"):
            if c not in out.columns:
                out[c] = pd.Series(dtype="object")
        return out
    out = df[df["bookmaker_key"] == ODDSAPI_SMARKETS_KEY].copy()
    out["currency"] = GBP
    out["lay_odds"] = None  # back-only via the aggregator
    return out


def smarkets_execution_stub(*_args, **_kwargs):
    """Explicit boundary: this stack cannot place/cancel orders on Smarkets."""
    raise NotImplementedError(
        "read-only: Smarkets order placement is intentionally unimplemented "
        "(monitoring-only). Polymarket ClobTrader is the only execution path."
    )
