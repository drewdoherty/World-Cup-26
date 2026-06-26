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

        resp = requests.post(
            _SESSIONS_URL,
            json={"username": user, "password": pwd},
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
