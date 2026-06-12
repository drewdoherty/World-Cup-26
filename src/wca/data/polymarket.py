"""Polymarket Gamma API read-only client.

Reference: https://docs.polymarket.com/#gamma-api
Endpoint base: https://gamma-api.polymarket.com
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import requests

_BASE_URL = "https://gamma-api.polymarket.com"
_TIMEOUT = 15
_HEADERS = {
    "User-Agent": "WorldCupAlpha/0.1 (research; contact via GitHub)",
    "Accept": "application/json",
}

logger = logging.getLogger(__name__)


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Internal helper: GET *path* with query *params*.

    Returns parsed JSON. Raises ``requests.HTTPError`` on 4xx/5xx.
    """
    url = _BASE_URL.rstrip("/") + "/" + path.lstrip("/")
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _parse_market_prices(market: Dict[str, Any]) -> Dict[str, Any]:
    """Decode the JSON-string-encoded ``outcomes`` and ``outcomePrices`` fields.

    Polymarket stores these as JSON-encoded strings inside the JSON response,
    e.g. ``'["Yes","No"]'`` and ``'["0.62","0.38"]'``.  This helper decodes
    them in-place (returns a *copy* of the market dict).
    """
    m = dict(market)
    for field in ("outcomes", "outcomePrices"):
        raw = m.get(field)
        if isinstance(raw, str):
            try:
                m[field] = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Could not decode field %s: %r", field, raw)
    # Build a convenient outcome -> price mapping
    outcomes: List[str] = m.get("outcomes") or []
    prices_raw: List[Any] = m.get("outcomePrices") or []
    prices: List[float] = []
    for p in prices_raw:
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            prices.append(float("nan"))
    if outcomes and prices:
        m["priceMap"] = dict(zip(outcomes, prices))
    return m


def search_events(
    query: str,
    closed: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search Polymarket events by keyword.

    Parameters
    ----------
    query:
        Free-text search term sent to the Gamma API ``/events`` endpoint.
    closed:
        If *False* (default) restrict to active (non-closed) events.
    limit:
        Maximum number of results to return (Gamma API default is 20).

    Returns
    -------
    list of event dicts, each with a ``markets`` list whose prices are decoded.
    """
    params: Dict[str, Any] = {
        "q": query,
        "limit": limit,
        "closed": "true" if closed else "false",
    }
    data = _get("/events", params=params)
    # Gamma returns a list directly or a dict with a data key
    if isinstance(data, list):
        events = data
    else:
        events = data.get("data", data.get("events", []))
    result = []
    for event in events:
        ev = dict(event)
        ev["markets"] = [_parse_market_prices(m) for m in (ev.get("markets") or [])]
        result.append(ev)
    return result


def get_event(event_id: str) -> Dict[str, Any]:
    """Fetch a single Polymarket event by *event_id*, including its markets.

    Parameters
    ----------
    event_id:
        The Polymarket event ID (string or int coerced to string).

    Returns
    -------
    Event dict with decoded market prices.
    """
    data = _get(f"/events/{event_id}")
    # Depending on endpoint the response may be the event directly or wrapped
    if isinstance(data, dict):
        event = data.get("data", data)
    else:
        event = data
    event = dict(event)
    event["markets"] = [_parse_market_prices(m) for m in (event.get("markets") or [])]
    return event


def _parse_json_array(raw: Any) -> Optional[List[Any]]:
    """Decode a JSON-string-encoded array (``'["a","b"]'``), tolerantly.

    Polymarket encodes ``clobTokenIds``/``outcomes``/``outcomePrices`` as
    JSON strings inside the JSON response. Returns the decoded list, the value
    unchanged if it is already a list, or ``None`` if it cannot be decoded.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, list) else None
    return None


def _yes_token_and_price(
    market: Dict[str, Any], event: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Resolve the YES clobTokenId + best price for a binary Yes/No market.

    Price preference: the mid of ``bestBid``/``bestAsk`` when both are present,
    otherwise the YES ``outcomePrices`` entry. Returns ``None`` when no YES
    token id can be parsed or no usable price exists.

    When *event* is supplied its ``slug``/``title`` are returned as
    ``event_slug``/``event_title`` so downstream callers can prove World-Cup
    provenance (single-match questions like "Will X win on <date>?" carry no
    "world cup"/"fifa" keyword, but the event slug is ``fifwc-...``).
    """
    token_ids = _parse_json_array(market.get("clobTokenIds"))
    if not token_ids:
        return None
    outcomes = _parse_json_array(market.get("outcomes")) or []
    # The YES token is the one paired with the "Yes" outcome (index 0 by
    # Polymarket convention, but resolve by name when outcomes are present).
    yes_idx = 0
    for i, o in enumerate(outcomes):
        if str(o).strip().lower() == "yes":
            yes_idx = i
            break
    if yes_idx >= len(token_ids):
        return None
    token_id = str(token_ids[yes_idx])

    # Best price: mid of bestBid/bestAsk if both present, else outcomePrices.
    price: Optional[float] = None
    bb, ba = market.get("bestBid"), market.get("bestAsk")
    try:
        if bb is not None and ba is not None:
            bb_f, ba_f = float(bb), float(ba)
            if bb_f > 0.0 and ba_f > 0.0:
                price = (bb_f + ba_f) / 2.0
    except (TypeError, ValueError):
        price = None
    if price is None:
        prices = _parse_json_array(market.get("outcomePrices")) or []
        if yes_idx < len(prices):
            try:
                price = float(prices[yes_idx])
            except (TypeError, ValueError):
                price = None
    if price is None or not (0.0 < price < 1.0):
        return None

    return {
        "token_id": token_id,
        "price": price,
        "neg_risk": bool(market.get("negRisk", False)),
        "market_question": market.get("question") or "",
        "outcome": "Yes",
        "event_slug": (event or {}).get("slug") or "",
        "event_title": (event or {}).get("title") or "",
    }


def resolve_outcome_token(
    fixture_home: str,
    fixture_away: str,
    selection: str,
    *,
    events: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a card selection to its Polymarket YES token + best price.

    Matches the fixture ``(fixture_home, fixture_away)`` to a live single-match
    World Cup event, then picks the market for ``selection``:

    * a team name -> the ``"Will <Team> win on <date>?"`` market for that team;
    * ``"Draw"`` (case-insensitive) -> the ``"... end in a draw?"`` market.

    Team names are compared on their *canonical* spelling
    (:func:`wca.data.teamnames.canonical`) so the odds-feed / results spelling
    and Polymarket's spelling line up. ``events`` may be supplied to avoid a
    network call (tests, batch runs); otherwise the live World Cup events are
    fetched.

    Returns ``{token_id, price, neg_risk, market_question, outcome}`` for the
    YES outcome, or ``None`` when no event / market / token can be resolved.
    """
    from wca.data.teamnames import canonical

    if events is None:
        events = find_world_cup_markets(include_closed=False)

    home_c = canonical(fixture_home)
    away_c = canonical(fixture_away)
    sel = (selection or "").strip()
    is_draw = sel.lower() == "draw"
    sel_c = canonical(sel) if not is_draw else None

    # Find the event whose two teams (from per-team win markets) canonically
    # match the fixture pair, order-independent.
    for event in events:
        markets = event.get("markets") or []
        # Collect the per-team win markets and the draw market for this event.
        team_markets: Dict[str, Dict[str, Any]] = {}
        draw_market: Optional[Dict[str, Any]] = None
        for m in markets:
            git = (m.get("groupItemTitle") or "").strip()
            question = (m.get("question") or "")
            if git.lower().startswith("draw") or "end in a draw" in question.lower():
                draw_market = m
                continue
            if git:
                team_markets[canonical(git)] = m

        event_teams = set(team_markets.keys())
        if {home_c, away_c} != event_teams:
            continue

        if is_draw:
            if draw_market is None:
                return None
            return _yes_token_and_price(draw_market, event)

        market = team_markets.get(sel_c)
        if market is None:
            return None
        return _yes_token_and_price(market, event)

    return None


def find_world_cup_markets(include_closed: bool = False) -> List[Dict[str, Any]]:
    """Return all active Polymarket 2026 FIFA World Cup markets.

    The Gamma API ``q=`` search does not reliably filter by topic; the correct
    approach is to fetch all events with ``tag_slug='soccer'`` (paginated) and
    then filter locally for World Cup content.

    Parameters
    ----------
    include_closed:
        If *True*, also return markets that have already resolved.

    Returns
    -------
    List of event dicts, each containing decoded markets.  De-duplicated by
    event ``id``.
    """
    _WC_KEYWORDS = ("world cup", "fifa world cup", "wc 2026", "2026 wc")

    seen_ids: set = set()
    results: List[Dict[str, Any]] = []

    limit = 100
    offset = 0
    while True:
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "tag_slug": "soccer",
        }
        if not include_closed:
            params["closed"] = "false"

        data = _get("/events", params=params)
        if isinstance(data, list):
            page = data
        else:
            page = data.get("data", data.get("events", []))

        if not page:
            break

        for event in page:
            title = (event.get("title") or "").lower()
            description = (event.get("description") or "").lower()
            text = title + " " + description
            if any(kw in text for kw in _WC_KEYWORDS):
                ev_id = event.get("id") or event.get("slug") or str(event)
                if ev_id not in seen_ids:
                    seen_ids.add(ev_id)
                    ev = dict(event)
                    ev["markets"] = [
                        _parse_market_prices(m) for m in (ev.get("markets") or [])
                    ]
                    results.append(ev)

        offset += limit
        if len(page) < limit:
            break  # last page reached

    return results
