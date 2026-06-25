"""Live Betfair Exchange API client (real order-book odds).

This is the *real* Betfair Exchange integration the project always wanted but
never had — :mod:`wca.data.betfair` is only a thin filter over The Odds API and
therefore still depends on the (now-revoked) ``ODDS_API_KEY``.  This module
talks to the Betfair Betting API directly (JSON-RPC) and returns best-back
decimal odds in the same flat DataFrame shape as :func:`theoddsapi.get_odds`,
so it can drop straight into the odds-source orchestrator.

CREDENTIALS (this source is OFF until these are present in the environment):

    BETFAIR_APP_KEY        — your Betfair Application Key (developer portal)
    BETFAIR_SESSION_TOKEN  — a current session token (SSOID)

  …or, to mint a session token automatically via non-interactive cert login:

    BETFAIR_USERNAME       — Betfair account username
    BETFAIR_PASSWORD       — Betfair account password
    BETFAIR_CERT_PATH      — path to your client certificate (.crt/.pem)
    BETFAIR_CERT_KEY_PATH  — path to the matching private key (.key/.pem)
    BETFAIR_APP_KEY        — (still required)

When no usable credentials are configured the public entry points return an
empty, correctly-shaped frame and log once — they NEVER raise — so the
orchestrator degrades to the next source rather than crashing the build.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BETTING_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"
_LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"
_TIMEOUT = 20
_SOCCER_EVENT_TYPE_ID = "1"  # Betfair event-type id for Association Football.

# Column shape shared with theoddsapi.get_odds so downstream code is unchanged.
_COLUMNS: Tuple[str, ...] = (
    "event_id",
    "commence_time",
    "home_team",
    "away_team",
    "bookmaker_key",
    "bookmaker_title",
    "market",
    "outcome_name",
    "outcome_description",
    "outcome_point",
    "decimal_odds",
    "retrieved_at",
)

# Module-level flag so the "creds missing" warning is logged once, not per call.
_WARNED_NO_CREDS = False


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_COLUMNS))


def _app_key() -> str:
    """Application key: prefer the LIVE key, fall back to DELAYED, then legacy.

    LIVE gives real-time prices; DELAYED (~1-180s) is a valid fallback. Values
    are read from the environment only — never hardcoded.
    """
    for var in ("BETFAIR_APP_KEY_LIVE", "BETFAIR_APP_KEY_DELAYED", "BETFAIR_APP_KEY"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""


def _resolve_session_token() -> Optional[str]:
    """Return a usable session token, or ``None`` if creds are unavailable.

    Prefers an explicit ``BETFAIR_SESSION_TOKEN``; otherwise attempts a
    non-interactive cert login when username/password/cert paths are all set.
    Any failure returns ``None`` (never raises).
    """
    token = os.environ.get("BETFAIR_SESSION_TOKEN", "").strip()
    if token:
        return token

    user = os.environ.get("BETFAIR_USERNAME", "").strip()
    pwd = os.environ.get("BETFAIR_PASSWORD", "").strip()
    cert = os.environ.get("BETFAIR_CERT_PATH", "").strip()
    key = os.environ.get("BETFAIR_CERT_KEY_PATH", "").strip()
    if not (user and pwd and cert and key and _app_key()):
        return None
    try:
        resp = requests.post(
            _LOGIN_URL,
            data={"username": user, "password": pwd},
            cert=(cert, key),
            headers={"X-Application": _app_key(), "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("loginStatus") == "SUCCESS":
            return body.get("sessionToken")
        logger.warning("Betfair cert login failed: %s", body.get("loginStatus"))
    except Exception as exc:  # noqa: BLE001 — login is best-effort.
        logger.warning("Betfair cert login error: %s", exc)
    return None


def creds_available() -> bool:
    """True when an app key *and* a resolvable session token are configured."""
    return bool(_app_key()) and bool(_resolve_session_token())


def missing_creds() -> List[str]:
    """Return the list of env vars needed to turn this source on (for reports)."""
    missing: List[str] = []
    if not _app_key():
        missing.append("BETFAIR_APP_KEY")
    if not _resolve_session_token():
        missing.append("BETFAIR_SESSION_TOKEN (or BETFAIR_USERNAME/PASSWORD + "
                       "BETFAIR_CERT_PATH/BETFAIR_CERT_KEY_PATH)")
    return missing


def _rpc(method: str, params: Dict[str, Any], session_token: str) -> Any:
    """Call a Betting-API JSON-RPC method and return its ``result``."""
    payload = [{
        "jsonrpc": "2.0",
        "method": "SportsAPING/v1.0/%s" % method,
        "params": params,
        "id": 1,
    }]
    headers = {
        "X-Application": _app_key(),
        "X-Authentication": session_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    resp = requests.post(_BETTING_URL, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, list):
        body = body[0] if body else {}
    if "error" in body:
        raise RuntimeError("Betfair RPC error: %s" % body["error"])
    return body.get("result")


def parse_market_book(
    catalogue: List[Dict[str, Any]],
    books: List[Dict[str, Any]],
    *,
    retrieved_at: Optional[str] = None,
) -> pd.DataFrame:
    """Map MATCH_ODDS catalogue + market books into the flat odds frame.

    Pure (no I/O) so it is unit-testable against sample JSON-RPC payloads.
    ``catalogue`` rows carry ``event``/``runners`` metadata; ``books`` carry the
    live ``runners[].ex.availableToBack`` best-back prices keyed by selection id.
    """
    book_by_market: Dict[str, Dict[str, Any]] = {
        b.get("marketId"): b for b in (books or [])
    }
    rows: List[Dict[str, Any]] = []
    for cat in catalogue or []:
        market_id = cat.get("marketId")
        event = cat.get("event") or {}
        name = event.get("name") or ""
        # Betfair formats soccer event names as "Home v Away".
        home, away = "", ""
        if " v " in name:
            home, away = (p.strip() for p in name.split(" v ", 1))
        commence = cat.get("marketStartTime") or event.get("openDate")
        book = book_by_market.get(market_id, {})
        price_by_sel: Dict[Any, float] = {}
        for r in book.get("runners") or []:
            backs = ((r.get("ex") or {}).get("availableToBack")) or []
            if backs:
                price_by_sel[r.get("selectionId")] = backs[0].get("price")
        for runner in cat.get("runners") or []:
            sel_id = runner.get("selectionId")
            runner_name = (runner.get("runnerName") or "").strip()
            # Betfair draw runner is literally "The Draw".
            outcome_name = "Draw" if runner_name.lower() == "the draw" else runner_name
            price = price_by_sel.get(sel_id)
            if price is None:
                continue
            rows.append({
                "event_id": event.get("id"),
                "commence_time": commence,
                "home_team": home,
                "away_team": away,
                "bookmaker_key": "betfair_ex",
                "bookmaker_title": "Betfair Exchange",
                "market": "h2h",
                "outcome_name": outcome_name,
                "outcome_description": None,
                "outcome_point": None,
                "decimal_odds": price,
                "retrieved_at": retrieved_at,
            })
    df = pd.DataFrame(rows, columns=list(_COLUMNS))
    if not df.empty:
        df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True, errors="coerce")
        df["retrieved_at"] = pd.to_datetime(df["retrieved_at"], utc=True, errors="coerce")
    return df


def get_odds(
    sport_key: str = "soccer_fifa_world_cup",
    *,
    regions: str = "uk",
    markets: str = "h2h",
    odds_format: str = "decimal",
    event_ids: Optional[List[str]] = None,
    competition_keyword: str = "World Cup",
) -> Tuple[pd.DataFrame, None]:
    """Fetch live MATCH_ODDS best-back prices for World Cup soccer.

    Returns ``(DataFrame, None)`` — the second element mirrors theoddsapi's
    ``QuotaInfo`` slot (Betfair has no per-call request quota header). Degrades
    to an empty frame (never raises) when creds are missing or any call fails.
    """
    global _WARNED_NO_CREDS
    token = _resolve_session_token()
    if not _app_key() or not token:
        if not _WARNED_NO_CREDS:
            logger.warning(
                "Betfair Exchange disabled — missing creds: %s",
                ", ".join(missing_creds()),
            )
            _WARNED_NO_CREDS = True
        return _empty_frame(), None

    try:
        market_filter = {
            "eventTypeIds": [_SOCCER_EVENT_TYPE_ID],
            "textQuery": competition_keyword,
            "marketTypeCodes": ["MATCH_ODDS"],
        }
        catalogue = _rpc(
            "listMarketCatalogue",
            {
                "filter": market_filter,
                "maxResults": "200",
                "marketProjection": ["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
            },
            token,
        ) or []
        market_ids = [c.get("marketId") for c in catalogue if c.get("marketId")]
        if not market_ids:
            return _empty_frame(), None
        books = _rpc(
            "listMarketBook",
            {
                "marketIds": market_ids,
                "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
            },
            token,
        ) or []
        return parse_market_book(catalogue, books), None
    except Exception as exc:  # noqa: BLE001 — never crash the build.
        logger.warning("Betfair Exchange fetch failed: %s", exc)
        return _empty_frame(), None


def get_event_odds(
    sport_key: str,
    event_id: str,
    *,
    regions: str = "uk",
    markets: str = "btts",
    odds_format: str = "decimal",
) -> Tuple[pd.DataFrame, None]:
    """Per-event player-prop markets are not wired for Betfair yet.

    Returns an empty frame so the orchestrator falls through to the next source
    (scorer enrichment is handled separately via Polymarket downstream).
    """
    return _empty_frame(), None
