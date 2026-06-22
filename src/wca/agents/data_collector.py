"""Agent 1 — Data Collector.

Fetches all raw data for a fixture: bookmaker odds (TheOddsAPI), Polymarket
prediction-market prices, and high-signal news/injury items from the RSS
scanner.  No modelling is performed here.

Input:  Fixture
Output: DataPackage
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from wca.agents.contracts import DataPackage, Fixture, PlayerAvailability

logger = logging.getLogger(__name__)


def run(
    fixture: Fixture,
    db_path: str = "data/wca.db",
    regions: str = "uk",
    pm_query: Optional[str] = None,
) -> DataPackage:
    """Collect all raw data for *fixture* and return a :class:`DataPackage`.

    Parameters
    ----------
    fixture:
        The target fixture, including TheOddsAPI ``event_id``.
    db_path:
        SQLite ledger path — used to read the news store and odds snapshots.
    regions:
        Comma-separated TheOddsAPI region string (e.g. ``"uk,eu"``).
    pm_query:
        Optional Polymarket search query.  Defaults to ``"<home> <away> FIFA"``.
    """
    pkg = DataPackage(fixture=fixture)

    # --- Bookmaker odds via TheOddsAPI -----------------------------------
    pkg.bookmaker_odds = _fetch_bookmaker_odds(fixture, regions)

    # --- Polymarket ---------------------------------------------------
    pkg.prediction_market_odds = _fetch_polymarket(
        fixture, query=pm_query or "%s %s FIFA" % (fixture.home, fixture.away)
    )

    # --- News / injury items from SQLite store (populated by wca_newsd) --
    pkg.news_items, pkg.injuries, pkg.suspensions = _fetch_news_and_injuries(
        fixture, db_path
    )

    return pkg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_bookmaker_odds(
    fixture: Fixture, regions: str
) -> List[Dict[str, Any]]:
    """Pull live 1X2 odds from TheOddsAPI and flatten to row dicts."""
    try:
        from wca.data import theoddsapi

        odds_df, quota = theoddsapi.get_odds(
            "soccer_fifa_world_cup",
            regions=regions,
            markets="h2h",
        )
        logger.debug("TheOddsAPI quota remaining: %s", quota.remaining)
    except Exception as exc:
        logger.warning("TheOddsAPI fetch failed: %s", exc)
        return []

    if odds_df is None or odds_df.empty:
        return []

    # Filter to the target fixture by event_id or team name match.
    rows = odds_df.to_dict(orient="records")
    target_rows: List[Dict[str, Any]] = []
    for row in rows:
        if str(row.get("event_id", "")) == fixture.event_id:
            target_rows.append(row)

    if not target_rows:
        # Fallback: fuzzy match by team names (case-insensitive)
        home_l = fixture.home.lower()
        away_l = fixture.away.lower()
        for row in rows:
            ht = str(row.get("home_team", "")).lower()
            at = str(row.get("away_team", "")).lower()
            if home_l in ht or ht in home_l:
                if away_l in at or at in away_l:
                    target_rows.append(row)

    return target_rows


def _fetch_polymarket(
    fixture: Fixture, query: str
) -> List[Dict[str, Any]]:
    """Search Polymarket for prediction-market prices for this match."""
    try:
        from wca.data.polymarket import search_events

        events = search_events(query, limit=10)
        if not events:
            return []

        result: List[Dict[str, Any]] = []
        for event in events[:3]:       # cap at top 3 events
            for market in (event.get("markets") or [])[:4]:
                price_map = market.get("priceMap") or {}
                for outcome, price in price_map.items():
                    result.append(
                        {
                            "source": "polymarket",
                            "market": market.get("question", event.get("title", "")),
                            "market_id": market.get("id", ""),
                            "selection": outcome,
                            "probability": float(price) if price else None,
                        }
                    )
        return result
    except Exception as exc:
        logger.warning("Polymarket fetch failed: %s", exc)
        return []


def _fetch_news_and_injuries(
    fixture: Fixture,
    db_path: str,
) -> tuple[List[Dict[str, Any]], List[PlayerAvailability], List[PlayerAvailability]]:
    """Read recent news items from the wca_newsd SQLite store.

    Returns ``(news_items, injuries, suspensions)``.
    """
    news: List[Dict[str, Any]] = []
    injuries: List[PlayerAvailability] = []
    suspensions: List[PlayerAvailability] = []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        teams = {fixture.home.lower(), fixture.away.lower()}

        rows = conn.execute(
            "SELECT title, summary, pub_date, url, score FROM news_items "
            "ORDER BY pub_date DESC LIMIT 200"
        ).fetchall()
        conn.close()

        for r in rows:
            item = dict(r)
            text = ((item.get("title") or "") + " " + (item.get("summary") or "")).lower()
            # Keep only items mentioning one of the two teams.
            if not any(t in text for t in teams):
                continue
            news.append(item)
            # Heuristic injury / suspension extraction.
            if any(kw in text for kw in ("injur", "ruled out", "doubt", "unavail", "miss")):
                team = fixture.home if fixture.home.lower() in text else fixture.away
                injuries.append(
                    PlayerAvailability(
                        name=_extract_player_name(item.get("title", "")),
                        team=team,
                        status="doubtful",
                        reason=item.get("title", "")[:120],
                        source=item.get("url", ""),
                    )
                )
            elif any(kw in text for kw in ("suspend", "ban", "card", "accumulate")):
                team = fixture.home if fixture.home.lower() in text else fixture.away
                suspensions.append(
                    PlayerAvailability(
                        name=_extract_player_name(item.get("title", "")),
                        team=team,
                        status="out",
                        reason=item.get("title", "")[:120],
                        source=item.get("url", ""),
                    )
                )
    except Exception as exc:
        logger.warning("News DB read failed (%s): %s", db_path, exc)

    return news, injuries, suspensions


def _extract_player_name(title: str) -> str:
    """Best-effort player name from a news headline (first two Title Case words)."""
    words = [w for w in title.split() if w and w[0].isupper()]
    return " ".join(words[:2]) if words else "Unknown"
