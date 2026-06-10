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
