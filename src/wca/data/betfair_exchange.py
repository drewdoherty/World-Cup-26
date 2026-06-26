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
_CERT_LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"
_INTERACTIVE_LOGIN_URL = "https://identitysso.betfair.com/api/login"
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
# In-process cache of a freshly-minted session token so a single build does not
# re-login per call (Betfair's interactive login endpoint is rate-limited).
_CACHED_TOKEN: Optional[str] = None


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_COLUMNS))


def _app_key() -> str:
    """Resolve the application key from the environment (never hardcoded).

    Order: an explicit ``BETFAIR_APP_KEY`` always wins; otherwise the LIVE key
    (real-time prices) is preferred, with the DELAYED key (~1-180s) as fallback.
    Set ``BETFAIR_APP_KEY_PREFER=delayed`` to flip the preference — needed while
    the LIVE subscription is inactive and only the delayed key authenticates.
    """
    legacy = os.environ.get("BETFAIR_APP_KEY", "").strip()
    live = os.environ.get("BETFAIR_APP_KEY_LIVE", "").strip()
    delayed = os.environ.get("BETFAIR_APP_KEY_DELAYED", "").strip()
    prefer = os.environ.get("BETFAIR_APP_KEY_PREFER", "live").strip().lower()
    ranked = [delayed, live] if prefer == "delayed" else [live, delayed]
    for val in [legacy, *ranked]:
        if val:
            return val
    return ""


def _resolve_session_token() -> Optional[str]:
    """Return a usable session token, or ``None`` if creds are unavailable.

    Resolution order (first that works wins; any failure returns ``None``,
    never raises):

    1. an explicit ``BETFAIR_SESSION_TOKEN`` (manual / short-lived);
    2. a cached token minted earlier in this process;
    3. **cert login** when ``BETFAIR_CERT_PATH``/``BETFAIR_CERT_KEY_PATH`` are
       set alongside username/password (the 24/7 non-interactive path);
    4. **interactive login** with just ``BETFAIR_USERNAME``/``BETFAIR_PASSWORD``
       (no cert) — Betfair's rate-limited identity endpoint, fine for an hourly
       build but cert login is preferred for high-frequency use.
    """
    global _CACHED_TOKEN
    token = os.environ.get("BETFAIR_SESSION_TOKEN", "").strip()
    if token:
        return token
    if _CACHED_TOKEN:
        return _CACHED_TOKEN

    user = os.environ.get("BETFAIR_USERNAME", "").strip()
    pwd = os.environ.get("BETFAIR_PASSWORD", "").strip()
    if not (user and pwd and _app_key()):
        return None
    cert = os.environ.get("BETFAIR_CERT_PATH", "").strip()
    key = os.environ.get("BETFAIR_CERT_KEY_PATH", "").strip()

    try:
        if cert and key:
            resp = requests.post(
                _CERT_LOGIN_URL,
                data={"username": user, "password": pwd},
                cert=(cert, key),
                headers={"X-Application": _app_key(), "Accept": "application/json"},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("loginStatus") == "SUCCESS":
                _CACHED_TOKEN = body.get("sessionToken")
                return _CACHED_TOKEN
            logger.warning("Betfair cert login failed: %s", body.get("loginStatus"))
            return None

        # No cert configured — interactive username/password login.
        resp = requests.post(
            _INTERACTIVE_LOGIN_URL,
            data={"username": user, "password": pwd},
            headers={
                "X-Application": _app_key(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "SUCCESS":
            _CACHED_TOKEN = body.get("token")
            return _CACHED_TOKEN
        logger.warning(
            "Betfair interactive login failed: status=%s error=%s",
            body.get("status"), body.get("error"),
        )
    except Exception as exc:  # noqa: BLE001 — login is best-effort.
        logger.warning("Betfair login error: %s", exc)
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
        missing.append("BETFAIR_SESSION_TOKEN, or BETFAIR_USERNAME+BETFAIR_PASSWORD "
                       "(optionally + BETFAIR_CERT_PATH/BETFAIR_CERT_KEY_PATH for "
                       "24/7 cert login)")
    return missing


class _InvalidAppKey(RuntimeError):
    """Raised when Betfair rejects the X-Application key (try the next one)."""


def _candidate_app_keys() -> List[str]:
    """Ordered, de-duplicated list of app keys to try for data calls.

    Honours ``BETFAIR_APP_KEY_PREFER`` (LIVE gives real-time, DELAYED ~1-180s).
    A LIVE key whose subscription is inactive returns INVALID_APP_KEY, so we
    fall through to the next candidate rather than failing the whole fetch.
    """
    legacy = os.environ.get("BETFAIR_APP_KEY", "").strip()
    live = os.environ.get("BETFAIR_APP_KEY_LIVE", "").strip()
    delayed = (os.environ.get("BETFAIR_APP_KEY_DELAYED", "").strip()
               or os.environ.get("BETFAIR_APP_KEY_DELAY", "").strip())
    prefer = os.environ.get("BETFAIR_APP_KEY_PREFER", "live").strip().lower()
    ranked = [delayed, live] if prefer == "delayed" else [live, delayed]
    out: List[str] = []
    for k in [legacy, *ranked]:
        if k and k not in out:
            out.append(k)
    return out


def _rpc(method: str, params: Dict[str, Any], session_token: str,
         app_key: Optional[str] = None) -> Any:
    """Call a Betting-API JSON-RPC method and return its ``result``."""
    payload = [{
        "jsonrpc": "2.0",
        "method": "SportsAPING/v1.0/%s" % method,
        "params": params,
        "id": 1,
    }]
    headers = {
        "X-Application": app_key or _app_key(),
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
        code = ((body["error"].get("data") or {}).get("APINGException") or {}).get("errorCode")
        if code == "INVALID_APP_KEY":
            raise _InvalidAppKey(code)
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
    candidates = _candidate_app_keys()
    if not candidates or not token:
        if not _WARNED_NO_CREDS:
            logger.warning(
                "Betfair Exchange disabled — missing creds: %s",
                ", ".join(missing_creds()),
            )
            _WARNED_NO_CREDS = True
        return _empty_frame(), None

    market_filter = {
        "eventTypeIds": [_SOCCER_EVENT_TYPE_ID],
        "textQuery": competition_keyword,
        "marketTypeCodes": ["MATCH_ODDS"],
    }
    # Try each app key in preference order; an inactive LIVE key raises
    # INVALID_APP_KEY, so fall through to DELAYED rather than failing the fetch.
    for i, app_key in enumerate(candidates):
        try:
            catalogue = _rpc(
                "listMarketCatalogue",
                {
                    "filter": market_filter,
                    "maxResults": "200",
                    "marketProjection": ["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
                },
                token, app_key=app_key,
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
                token, app_key=app_key,
            ) or []
            logger.info("Betfair Exchange: %d markets via app key #%d", len(catalogue), i + 1)
            return parse_market_book(catalogue, books), None
        except _InvalidAppKey:
            logger.info("Betfair app key #%d rejected (INVALID_APP_KEY); trying next", i + 1)
            continue
        except Exception as exc:  # noqa: BLE001 — never crash the build.
            logger.warning("Betfair Exchange fetch failed: %s", exc)
            return _empty_frame(), None
    logger.warning("Betfair Exchange: no app key authorized for data; degrading")
    return _empty_frame(), None


def _normalise_order(o: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one Betfair ``listCurrentOrders`` order into a normalised position.

    Only MATCHED size is treated as an open position (unmatched size is a
    resting limit order, not money at risk on an outcome). Returns ``None`` for
    orders with no matched size. Pure (no I/O) so it is unit-testable.
    """
    size_matched = float(o.get("sizeMatched") or 0.0)
    if size_matched <= 0:
        return None
    price = o.get("averagePriceMatched") or o.get("priceSize", {}).get("price")
    item = o.get("itemDescription") or {}
    market_desc = item.get("marketDesc") or o.get("marketId")
    sel_desc = item.get("runnerDesc") or str(o.get("selectionId") or "")
    event_desc = item.get("eventDesc") or ""
    side = (o.get("side") or "").upper()
    return {
        "venue": "Betfair",
        "market": market_desc,
        "selection": "Draw" if str(sel_desc).strip().lower() == "the draw" else sel_desc,
        "fixture_or_event": event_desc,
        "stake": size_matched,
        "size": size_matched,
        "avg_price": float(price) if price else None,
        "odds": float(price) if price else None,
        "current_value": None,
        "current_price": None,
        "external_id": str(o.get("betId") or ""),
        "account": "1",
        "side": side,
    }


def list_current_orders(*, account: str = "1") -> List[Dict[str, Any]]:
    """Return open (matched) Betfair Exchange positions, normalised.

    READ-ONLY: calls the Betting API ``listCurrentOrders``. DEGRADES GRACEFULLY
    — returns ``[]`` and logs (never raises) when creds are missing or any call
    fails (the mini currently gets connection errors reaching Betfair).
    """
    token = _resolve_session_token()
    candidates = _candidate_app_keys()
    if not candidates or not token:
        logger.info("Betfair positions disabled — missing creds: %s",
                    ", ".join(missing_creds()))
        return []
    for i, app_key in enumerate(candidates):
        try:
            result = _rpc(
                "listCurrentOrders",
                {"orderProjection": "ALL", "includeItemDescription": True},
                token, app_key=app_key,
            ) or {}
            orders = result.get("currentOrders") or []
            out: List[Dict[str, Any]] = []
            for o in orders:
                norm = _normalise_order(o)
                if norm is not None:
                    norm["account"] = account
                    out.append(norm)
            logger.info("Betfair positions: %d matched of %d orders", len(out), len(orders))
            return out
        except _InvalidAppKey:
            logger.info("Betfair app key #%d rejected for orders; trying next", i + 1)
            continue
        except Exception as exc:  # noqa: BLE001 — never crash the sync.
            logger.warning("Betfair listCurrentOrders failed (degrading to empty): %s", exc)
            return []
    logger.warning("Betfair positions: no app key authorized; degrading to empty")
    return []


def _normalise_cleared_order(o: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one Betfair ``listClearedOrders`` order into a normalised SETTLED
    position.

    Betfair reports the realised ``profit`` (already net of the back/lay side)
    and a ``betOutcome`` of ``WON``/``LOST`` per settled bet, plus a
    ``settledDate``. We only treat an *unambiguous* WON/LOST outcome as a
    settle — anything else (e.g. PLACED, an open/partial outcome, or a missing
    outcome) returns ``None`` so the reconcile engine leaves it for review.
    Pure (no I/O) so it is unit-testable against sample JSON-RPC payloads.
    """
    outcome = str(o.get("betOutcome") or "").strip().upper()
    if outcome not in ("WON", "LOST"):
        return None
    size_settled = float(o.get("sizeSettled") or o.get("sizeMatched") or 0.0)
    price = o.get("priceMatched") or o.get("priceRequested")
    item = o.get("itemDescription") or {}
    market_desc = item.get("marketDesc") or o.get("marketId")
    sel_desc = item.get("runnerDesc") or str(o.get("selectionId") or "")
    event_desc = item.get("eventDesc") or ""
    try:
        pnl = float(o.get("profit"))
    except (TypeError, ValueError):
        return None  # no realised P&L -> not an unambiguous settle.
    return {
        "venue": "Betfair",
        "market": market_desc,
        "selection": "Draw" if str(sel_desc).strip().lower() == "the draw" else sel_desc,
        "fixture_or_event": event_desc,
        "stake": size_settled,
        "size": size_settled,
        "avg_price": float(price) if price else None,
        "odds": float(price) if price else None,
        "settled_pnl": pnl,
        "result": "won" if outcome == "WON" else "lost",
        "settled_ts": o.get("settledDate"),
        "external_id": str(o.get("betId") or ""),
        "account": "1",
        "side": (o.get("side") or "").upper(),
    }


def list_cleared_orders(*, since_hours: int = 24, account: str = "1") -> List[Dict[str, Any]]:
    """Return SETTLED Betfair Exchange positions over the last ``since_hours``.

    READ-ONLY: calls the Betting API ``listClearedOrders`` with
    ``settledDateRange`` covering the lookback window and an unambiguous
    ``betStatus='SETTLED'``. Each row carries the venue's realised ``profit`` and
    WON/LOST outcome so the reconcile engine can settle the matched ledger bet
    with VENUE TRUTH (no recomputation). DEGRADES GRACEFULLY — returns ``[]`` and
    logs (never raises) when creds are missing or any call fails (the mini
    currently cannot reach Betfair; this runs on the MacBook over the VPN).
    """
    token = _resolve_session_token()
    candidates = _candidate_app_keys()
    if not candidates or not token:
        logger.info("Betfair cleared-orders disabled — missing creds: %s",
                    ", ".join(missing_creds()))
        return []
    import datetime as _dt

    since = (_dt.datetime.utcnow() - _dt.timedelta(hours=max(1, int(since_hours)))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    for i, app_key in enumerate(candidates):
        try:
            result = _rpc(
                "listClearedOrders",
                {
                    "betStatus": "SETTLED",
                    "settledDateRange": {"from": since},
                    "includeItemDescription": True,
                    "groupBy": "BET",
                    "recordCount": 1000,
                },
                token, app_key=app_key,
            ) or {}
            orders = result.get("clearedOrders") or []
            out: List[Dict[str, Any]] = []
            for o in orders:
                norm = _normalise_cleared_order(o)
                if norm is not None:
                    norm["account"] = account
                    out.append(norm)
            logger.info("Betfair cleared: %d settled of %d (since %s)",
                        len(out), len(orders), since)
            return out
        except _InvalidAppKey:
            logger.info("Betfair app key #%d rejected for cleared orders; trying next", i + 1)
            continue
        except Exception as exc:  # noqa: BLE001 — never crash the sync.
            logger.warning("Betfair listClearedOrders failed (degrading to empty): %s", exc)
            return []
    logger.warning("Betfair cleared-orders: no app key authorized; degrading to empty")
    return []


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
