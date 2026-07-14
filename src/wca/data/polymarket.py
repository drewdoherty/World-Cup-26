"""Polymarket Gamma API read-only client.

Reference: https://docs.polymarket.com/#gamma-api
Endpoint base: https://gamma-api.polymarket.com
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

_BASE_URL = "https://gamma-api.polymarket.com"
_TIMEOUT = 15
_HEADERS = {
    "User-Agent": "WorldCupAlpha/0.1 (research; contact via GitHub)",
    "Accept": "application/json",
}

logger = logging.getLogger(__name__)

# Optional archival TEE: additive, never changes betting behavior (guarded).
try:
    from wca.archive import tee as _archive_tee
except Exception:  # pragma: no cover - archive is optional
    _archive_tee = None


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Internal helper: GET *path* with query *params*.

    Returns parsed JSON. Raises ``requests.HTTPError`` on 4xx/5xx.
    """
    url = _BASE_URL.rstrip("/") + "/" + path.lstrip("/")
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if _archive_tee is not None:
        _archive_tee.raw("polymarket", path.strip("/").split("/")[0] or "gamma", data, kind=path)
    return data


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
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bb, ba = market.get("bestBid"), market.get("bestAsk")
    try:
        if bb is not None and ba is not None:
            bb_f, ba_f = float(bb), float(ba)
            if bb_f > 0.0 and ba_f > 0.0:
                price = (bb_f + ba_f) / 2.0
                best_bid, best_ask = bb_f, ba_f
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
        # Additive, telemetry-only (2026-07-08 gate-fill-telemetry review):
        # the true book bid/ask behind ``price`` when the mid path was used,
        # so a caller can detect a tick-snap rounding the mid onto the ask
        # (or bid) on a 1-tick-wide book. None when the outcomePrices
        # fallback was used (no live book).
        "best_bid": best_bid,
        "best_ask": best_ask,
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


def _player_props_event(
    fixture_home: str,
    fixture_away: str,
    events: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Find the single-match "<Home> vs. <Away> - Player Props" event.

    Matched on the canonical fixture pair appearing in the event title (the
    Player-Props event has no per-team win markets to key off, so the title is
    the only fixture signal). Returns the event dict or ``None``.
    """
    from wca.data.teamnames import canonical

    home_c = canonical(fixture_home)
    away_c = canonical(fixture_away)
    for event in events:
        title = (event.get("title") or "")
        if "player prop" not in title.lower():
            continue
        # Title shape: "United States vs. Australia - Player Props".
        head = title.split(" - ")[0]
        parts = [p.strip() for p in head.replace(" vs. ", " vs ").split(" vs ")]
        if len(parts) != 2:
            continue
        teams_c = {canonical(parts[0]), canonical(parts[1])}
        if teams_c == {home_c, away_c}:
            return event
    return None


def resolve_player_anytime_token(
    fixture_home: str,
    fixture_away: str,
    player: str,
    *,
    events: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a player's Polymarket *anytime-goalscorer* YES token + price.

    Polymarket prices single-match scorer props as graded "<Player>: N+ goals"
    questions inside a "<Home> vs. <Away> - Player Props" event; the **1+ goals**
    market is the anytime-scorer equivalent (there is no per-player
    first-goalscorer market on Polymarket — that gap is reported by the caller).

    Player names are matched case-insensitively on a normalised spelling so the
    Odds API spelling ("Brendan Aaronson", "Sergino Dest") lines up with
    Polymarket's ("Brenden Aaronson", "Sergiño Dest"). Returns the usual
    ``{token_id, price, neg_risk, market_question, outcome, ...}`` dict, or
    ``None`` when no event / 1+ goals market / token resolves.
    """
    if events is None:
        events = find_world_cup_markets(include_closed=False)
    event = _player_props_event(fixture_home, fixture_away, events)
    if event is None:
        return None

    want = _norm_player(player)
    want_key = _player_key(player)
    fuzzy: Optional[Dict[str, Any]] = None
    for m in event.get("markets") or []:
        git = (m.get("groupItemTitle") or m.get("question") or "")
        name, _, suffix = git.partition(":")
        # Anytime == exactly "1+ goals" — NOT "1+ goals + assists" / "1+ shots".
        if suffix.strip().lower() != "1+ goals":
            continue
        if _norm_player(name) == want:
            return _yes_token_and_price(m, event)
        # Fall back to a last-name + first-initial key so the Odds API spelling
        # ("Brendan Aaronson", "Mohamed Toure", "Sergino Dest") matches
        # Polymarket's ("Brenden Aaronson", "Mo Touré", "Sergiño Dest").
        if want_key and _player_key(name) == want_key and fuzzy is None:
            fuzzy = _yes_token_and_price(m, event)
    return fuzzy


def _exact_score_event(
    fixture_home: str,
    fixture_away: str,
    events: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Find the "<Home> vs. <Away> - Exact Score" event.

    Returns ``(event, pm_home_team_name)`` — the second element is the team
    Polymarket lists first in the title, so the caller can re-orient scorelines
    to its own home/away convention. ``(None, None)`` when no event matches.
    """
    from wca.data.teamnames import canonical

    home_c, away_c = canonical(fixture_home), canonical(fixture_away)
    for event in events:
        title = (event.get("title") or "")
        if "exact score" not in title.lower():
            continue
        head = title.split(" - ")[0]
        parts = [p.strip() for p in head.replace(" vs. ", " vs ").split(" vs ")]
        if len(parts) != 2:
            continue
        if {canonical(parts[0]), canonical(parts[1])} == {home_c, away_c}:
            return event, parts[0]
    return None, None


def resolve_exact_scores(
    fixture_home: str,
    fixture_away: str,
    *,
    events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, float]:
    """Polymarket exact-score (correct-score) YES probabilities for one fixture.

    Polymarket prices each scoreline as its own binary "Exact Score: <Home> H - A
    <Away>?" market inside a "<Home> vs. <Away> - Exact Score" event; the YES
    price is the market's probability for that exact score. Returns a mapping of
    ``"H-A"`` (the requested home-away convention) -> probability in 0..1, or
    ``{}`` when no exact-score event resolves. Scorelines are re-oriented if
    Polymarket lists the two teams the other way round. The catch-all "Any Other
    Score" market (no digits) is skipped.
    """
    import re as _re
    from wca.data.teamnames import canonical

    if events is None:
        events = find_world_cup_markets(include_closed=False)
    event, pm_home = _exact_score_event(fixture_home, fixture_away, events)
    if event is None:
        return {}
    flip = canonical(pm_home or "") != canonical(fixture_home)
    out: Dict[str, float] = {}
    for m in event.get("markets") or []:
        label = (m.get("groupItemTitle") or m.get("question") or "")
        mm = _re.search(r"(\d+)\s*[-–]\s*(\d+)", label)
        if not mm:
            continue
        a, b = int(mm.group(1)), int(mm.group(2))
        score = "%d-%d" % ((b, a) if flip else (a, b))
        res = _yes_token_and_price(m, event)
        price = (res or {}).get("price")
        if price is not None and 0.0 < float(price) < 1.0:
            out[score] = float(price)
    return out


def _norm_player(name: str) -> str:
    """Normalise a player name for cross-source matching (accents, case, ws)."""
    import unicodedata

    n = unicodedata.normalize("NFKD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(n.lower().split())


def _player_key(name: str) -> str:
    """Loose match key: first-initial + last-name (accent/case-insensitive).

    Bridges short-form / variant first names across feeds ("Brendan"/"Brenden",
    "Mohamed"/"Mo", "Sergino"/"Sergiño") while keeping the (always-spelled-out)
    surname intact, so two different players are not collapsed. Empty when the
    name has no usable surname token.
    """
    parts = _norm_player(name).split()
    if len(parts) < 2:
        return ""
    return parts[0][:1] + "|" + parts[-1]


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

        try:
            data = _get("/events", params=params)
        except requests.HTTPError as exc:
            # Gamma's /events paginator has an undocumented offset ceiling
            # (observed: offset=2000 -> 200, offset=2100 -> 422, stable/
            # reproducible, 2026-07-13) — the same class of limit already
            # known on the data-api /trades endpoint (caps at offset 3000).
            # Past it, every subsequent page 422s too, so this is "no more
            # results", not a transient error: stop paginating and return
            # what's already collected rather than discarding it.
            status = exc.response.status_code if exc.response is not None else None
            if status == 422:
                logger.info(
                    "gamma /events offset ceiling hit at offset=%d (422) — "
                    "stopping pagination with %d event(s) collected",
                    offset, len(results))
                break
            raise
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
