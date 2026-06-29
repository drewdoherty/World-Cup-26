"""Polymarket share prices -> h2h odds frame (no credentials required).

The Polymarket Gamma API is a public read-only endpoint, so this source keeps
working when both Betfair (no creds) and The Odds API (revoked key) are down —
it is the always-on floor that stops the card build from going stale.

Each live single-match World Cup event on Polymarket (title ``"<Home> vs.
<Away>"``) carries a per-team "Will <Team> win on <date>?" market plus a
"… end in a draw?" market. The YES *share price* of each is that outcome's
implied probability; we invert it to a decimal odd (``1 / price``) and emit the
same flat frame shape as :func:`theoddsapi.get_odds`, tagged
``bookmaker_key="polymarket"``.

These are mid-of-book implied probabilities, NOT a sharp bookmaker line: there
is no overround removed and liquidity varies. Good enough to keep /card,
/next and /scores live and model-comparable while a real odds feed is restored.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from wca.data import polymarket

logger = logging.getLogger(__name__)

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


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_COLUMNS))


def _mid_price(market: Dict[str, Any]) -> Optional[float]:
    """YES implied probability: mid(bestBid, bestAsk), else YES outcomePrice."""
    res = polymarket._yes_token_and_price(market)
    if res is None:
        return None
    price = res.get("price")
    if price is None or not (0.0 < float(price) < 1.0):
        return None
    return float(price)


def _split_title(title: str) -> Optional[Tuple[str, str]]:
    """Parse a "<Home> vs. <Away>" event title into (home, away)."""
    head = (title or "").split(" - ")[0]
    parts = [p.strip() for p in head.replace(" vs. ", " vs ").split(" vs ")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


# Market-type suffixes Polymarket appends to a fixture's ANCILLARY events
# (separate from the full-match winner): "<Home> vs. <Away> - Halftime Result",
# "... - Second Half Result", "... - Exact Score", etc. Each ancillary event
# reuses the team name as its market ``groupItemTitle``, so without this filter
# they get admitted as full-match h2h rows. The "leading at halftime" price is
# LONGER than the full-match win price, so it wins ``card.best_price`` and gets
# quoted as the match-winner price — the 2026-06-29 phantom-edge bug. The bare
# full-match event has NO " - <type>" suffix in either its title or slug.
_AUX_SLUG_MARKERS: Tuple[str, ...] = (
    "halftime-result",
    "second-half-result",
    "exact-score",
    "more-markets",
    "total-corners",
    "total-goals",
    "player-props",
    "first-to-score",
    "both-teams-to-score",
    "double-chance",
)


def _is_full_match_event(event: Dict[str, Any]) -> bool:
    """True only for the bare per-fixture full-match (match-winner) event.

    Polymarket lists several ancillary events per fixture (Halftime Result,
    Second Half Result, Exact Score, ...) whose markets reuse the team name as
    ``groupItemTitle``; admitting them as full-match h2h rows contaminates
    best-price and consensus. The full-match event's title is a bare
    ``"<Home> vs. <Away>"`` (no " - " suffix) and its slug carries no
    market-type marker. We reject on EITHER signal so a rename of one can't
    leak the other back in.
    """
    title = event.get("title") or ""
    if " - " in title:  # any "<fixture> - <market type>" ancillary event
        return False
    slug = (event.get("slug") or "").lower()
    if any(marker in slug for marker in _AUX_SLUG_MARKERS):
        return False
    return True


def rows_from_events(
    events: List[Dict[str, Any]],
    *,
    retrieved_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build flat h2h row dicts from Polymarket single-match events.

    Pure (no network) so it is unit-testable against sample event dicts. Skips
    events that are not a single fixture (group/outright/no-draw markets).
    """
    rows: List[Dict[str, Any]] = []
    for event in events or []:
        # Only the bare full-match event — never the Halftime/Second-Half/Exact
        # Score ancillary events, whose team-named markets would otherwise be
        # mis-admitted as the match-winner line (2026-06-29 phantom-edge bug).
        if not _is_full_match_event(event):
            continue
        teams = _split_title(event.get("title") or "")
        if teams is None:
            continue
        home, away = teams
        markets = event.get("markets") or []

        # Locate the per-team win markets and the draw market for this fixture.
        team_market: Dict[str, Dict[str, Any]] = {}
        draw_market: Optional[Dict[str, Any]] = None
        for m in markets:
            git = (m.get("groupItemTitle") or "").strip()
            question = (m.get("question") or "").lower()
            if git.lower().startswith("draw") or "end in a draw" in question:
                draw_market = m
            elif git:
                team_market[git] = m

        if home not in team_market or away not in team_market:
            # Not a clean two-team win market set — skip rather than guess.
            continue

        commence = event.get("endDate") or event.get("startDate")
        outcomes = [
            (home, _mid_price(team_market[home])),
            (away, _mid_price(team_market[away])),
        ]
        if draw_market is not None:
            outcomes.append(("Draw", _mid_price(draw_market)))

        for outcome_name, prob in outcomes:
            if prob is None:
                continue
            rows.append({
                "event_id": event.get("id") or event.get("slug"),
                "commence_time": commence,
                "home_team": home,
                "away_team": away,
                "bookmaker_key": "polymarket",
                "bookmaker_title": "Polymarket",
                "market": "h2h",
                "outcome_name": outcome_name,
                "outcome_description": None,
                "outcome_point": None,
                "decimal_odds": round(1.0 / prob, 4),
                "retrieved_at": retrieved_at,
            })
    return rows


def get_odds(
    sport_key: str = "soccer_fifa_world_cup",
    *,
    regions: str = "uk",
    markets: str = "h2h",
    odds_format: str = "decimal",
    event_ids: Optional[List[str]] = None,
    events: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[pd.DataFrame, None]:
    """Fetch live World Cup single-match share prices as an h2h odds frame.

    Returns ``(DataFrame, None)``. Degrades to an empty frame (never raises) if
    Polymarket is unreachable. ``events`` may be injected to avoid network I/O.
    """
    try:
        if events is None:
            events = polymarket.find_world_cup_markets(include_closed=False)
        rows = rows_from_events(events)
    except Exception as exc:  # noqa: BLE001 — never crash the build.
        logger.warning("Polymarket odds fetch failed: %s", exc)
        return _empty_frame(), None

    df = pd.DataFrame(rows, columns=list(_COLUMNS))
    if not df.empty:
        df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True, errors="coerce")
        df["retrieved_at"] = pd.to_datetime(df["retrieved_at"], utc=True, errors="coerce")
    return df, None
