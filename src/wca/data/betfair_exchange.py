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

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BETTING_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"
_CERT_LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"
_INTERACTIVE_LOGIN_URL = "https://identitysso.betfair.com/api/login"
_TIMEOUT = 20
_SOCCER_EVENT_TYPE_ID = "1"  # Betfair event-type id for Association Football.

# Where a freshly-minted session token is cached ACROSS processes so an hourly
# build does not re-login each run (Betfair's interactive login is rate-limited
# / throttled). Path is overridable; file is gitignored and chmod 0600. The
# token itself is NEVER logged.
_SESSION_CACHE_PATH = os.environ.get(
    "BETFAIR_SESSION_CACHE", "data/.betfair_session.json"
)
# Cached session token is considered fresh for this long (Betfair tokens live
# far longer, but we re-mint well inside the window to stay safe).
_SESSION_CACHE_TTL_SECONDS = int(
    os.environ.get("BETFAIR_SESSION_TTL_SECONDS", str(3 * 60 * 60))  # ~3 hours
)

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


def _read_cached_token() -> Optional[str]:
    """Return a non-expired session token cached to disk, or ``None``.

    Freshness is mtime-based: a cache file older than ``_SESSION_CACHE_TTL_SECONDS``
    is treated as expired so a new token is minted. Any read/parse error degrades
    to ``None`` (re-mint). The token is NEVER logged.
    """
    try:
        path = _SESSION_CACHE_PATH
        if not os.path.exists(path):
            return None
        age = time.time() - os.path.getmtime(path)
        if age > _SESSION_CACHE_TTL_SECONDS:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        tok = str(data.get("session_token") or "").strip()
        return tok or None
    except Exception as exc:  # noqa: BLE001 — cache is best-effort.
        logger.warning("Betfair session cache read failed (re-minting): %s", exc)
        return None


def _write_cached_token(token: str) -> None:
    """Persist a freshly-minted token to the gitignored cache file (chmod 0600).

    Best-effort: any failure is logged WITHOUT the token and otherwise ignored.
    """
    if not token:
        return
    try:
        path = _SESSION_CACHE_PATH
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"session_token": token, "minted_at": _now_iso()}, fh)
        try:
            os.chmod(path, 0o600)  # owner-only — never world-readable.
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 — cache write is best-effort.
        logger.warning("Betfair session cache write failed (continuing): %s", exc)


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_session_token() -> Optional[str]:
    """Return a usable session token, or ``None`` if creds are unavailable.

    Resolution order (first that works wins; any failure returns ``None``,
    never raises) — the disk cache exists to STOP the interactive-login throttle:

    1. an explicit ``BETFAIR_SESSION_TOKEN`` (manual / short-lived);
    2. a cached token minted earlier in this process;
    3. a non-expired token cached to disk by an earlier process (mtime TTL);
    4. **cert login** when ``BETFAIR_CERT_PATH``/``BETFAIR_CERT_KEY_PATH`` are
       set alongside username/password (the 24/7 non-interactive path);
    5. **interactive login** with just ``BETFAIR_USERNAME``/``BETFAIR_PASSWORD``
       (no cert) — Betfair's rate-limited identity endpoint, fine for an hourly
       build but cert login is preferred for high-frequency use.

    A token freshly minted via (4) or (5) is written back to the disk cache so
    the NEXT process reuses it (steps 1-3) instead of hitting the login endpoint
    again. The token is NEVER logged.
    """
    global _CACHED_TOKEN
    token = os.environ.get("BETFAIR_SESSION_TOKEN", "").strip()
    if token:
        return token
    if _CACHED_TOKEN:
        return _CACHED_TOKEN
    disk = _read_cached_token()
    if disk:
        _CACHED_TOKEN = disk  # keep it for the rest of this process too.
        return disk

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
                _write_cached_token(_CACHED_TOKEN)
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
            _write_cached_token(_CACHED_TOKEN)
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
    market_id = o.get("marketId")
    selection_id = o.get("selectionId")
    market_desc = item.get("marketDesc") or market_id
    sel_desc = item.get("runnerDesc") or str(selection_id or "")
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
        # Raw Betfair IDs kept so resolve_order_names can backfill names later.
        "market_id": str(market_id) if market_id is not None else None,
        "selection_id": selection_id,
    }


def build_name_maps(catalogue: List[Dict[str, Any]]):
    """Build (marketId -> event name) and ((marketId, selectionId) -> runner
    name) maps from a ``listMarketCatalogue`` result.

    Pure (no I/O) so it is unit-testable against a sample catalogue. The runner
    name for the Betfair draw runner ("The Draw") is normalised to "Draw" to
    match the ledger convention used elsewhere in this module.
    """
    event_by_market: Dict[str, str] = {}
    runner_by_market_sel: Dict[Tuple[str, Any], str] = {}
    for cat in catalogue or []:
        market_id = cat.get("marketId")
        if market_id is None:
            continue
        market_id = str(market_id)
        event = cat.get("event") or {}
        event_name = (event.get("name") or "").strip()
        if event_name:
            event_by_market[market_id] = event_name
        for runner in cat.get("runners") or []:
            sel_id = runner.get("selectionId")
            name = (runner.get("runnerName") or "").strip()
            if not name:
                continue
            if name.lower() == "the draw":
                name = "Draw"
            runner_by_market_sel[(market_id, sel_id)] = name
    return event_by_market, runner_by_market_sel


def resolve_order_names(
    positions: List[Dict[str, Any]],
    token: str,
    app_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Backfill ``fixture_or_event`` + ``selection`` on open positions whose names
    are still raw Betfair IDs.

    ``listCurrentOrders`` returns ``marketId`` + ``selectionId`` but no names, so
    a position can't match the ledger by name. Given the set of market IDs, this
    calls ``listMarketCatalogue`` (marketProjection EVENT + RUNNER_DESCRIPTION)
    and maps ``marketId -> event name`` and ``(marketId, selectionId) -> runner
    name``. Only positions whose ``selection`` still equals its raw selection ID
    (i.e. no ``itemDescription`` was returned) are rewritten; positions that
    already carry names are left untouched.

    Degrades GRACEFULLY: if the catalogue call fails or returns nothing, the raw
    IDs are left in place (never raises). Reuses :func:`_rpc`.
    """
    if not positions:
        return positions
    need = [
        p for p in positions
        if p.get("market_id") and _name_is_raw_id(p)
    ]
    if not need:
        return positions
    market_ids = sorted({p["market_id"] for p in need})
    try:
        catalogue = _rpc(
            "listMarketCatalogue",
            {
                "filter": {"marketIds": market_ids},
                "maxResults": str(max(1, len(market_ids))),
                "marketProjection": ["EVENT", "RUNNER_DESCRIPTION"],
            },
            token, app_key=app_key,
        ) or []
    except Exception as exc:  # noqa: BLE001 — leave IDs in place on failure.
        logger.warning("Betfair name resolution failed (leaving IDs): %s", exc)
        return positions
    event_by_market, runner_by_market_sel = build_name_maps(catalogue)
    for p in need:
        mid = p.get("market_id")
        sid = p.get("selection_id")
        event_name = event_by_market.get(mid)
        if event_name:
            p["fixture_or_event"] = event_name
            if not p.get("market") or str(p.get("market")) == mid:
                p["market"] = event_name
        runner_name = runner_by_market_sel.get((mid, sid))
        if runner_name:
            p["selection"] = runner_name
    return positions


def _name_is_raw_id(p: Dict[str, Any]) -> bool:
    """True when a position's ``selection`` is still the raw selection ID (i.e.
    ``listCurrentOrders`` returned no ``itemDescription`` to name it)."""
    sel = p.get("selection")
    sid = p.get("selection_id")
    return sid is not None and str(sel).strip() == str(sid).strip()


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
            # listCurrentOrders has no names; backfill event + runner names from
            # the market catalogue so positions can match the ledger. Degrades
            # to leaving raw IDs if the catalogue call fails.
            out = resolve_order_names(out, token, app_key=app_key)
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
