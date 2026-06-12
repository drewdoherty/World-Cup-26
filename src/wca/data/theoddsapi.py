"""Client for The Odds API v4 (https://the-odds-api.com).

Reference: https://the-odds-api.com/liveapi/guides/v4/
Auth: ODDS_API_KEY environment variable.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

_BASE_URL = "https://api.the-odds-api.com/v4"
_TIMEOUT = 20
_HEADERS = {
    "User-Agent": "WorldCupAlpha/0.1 (research; contact via GitHub)",
    "Accept": "application/json",
}

logger = logging.getLogger(__name__)


class QuotaInfo:
    """Holds API quota information surfaced from response headers."""

    def __init__(self, remaining: Optional[int], used: Optional[int]) -> None:
        self.remaining = remaining
        self.used = used

    def __repr__(self) -> str:  # pragma: no cover
        return f"QuotaInfo(remaining={self.remaining}, used={self.used})"


def _get_api_key() -> str:
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "ODDS_API_KEY environment variable is not set. "
            "Get a free key at https://the-odds-api.com."
        )
    return key


def _extract_quota(headers: Any) -> QuotaInfo:
    """Parse x-requests-remaining / x-requests-used from response headers."""
    def _int_or_none(v: Optional[str]) -> Optional[int]:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    remaining = _int_or_none(headers.get("x-requests-remaining"))
    used = _int_or_none(headers.get("x-requests-used"))
    return QuotaInfo(remaining=remaining, used=used)


def list_sports(
    all_sports: bool = False,
) -> Tuple[List[Dict[str, Any]], QuotaInfo]:
    """List available sports.

    Parameters
    ----------
    all_sports:
        If *True*, include sports that are currently out of season.

    Returns
    -------
    Tuple of (list of sport dicts, QuotaInfo).
    """
    params: Dict[str, Any] = {
        "apiKey": _get_api_key(),
        "all": "true" if all_sports else "false",
    }
    resp = requests.get(
        f"{_BASE_URL}/sports",
        params=params,
        headers=_HEADERS,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    quota = _extract_quota(resp.headers)
    return resp.json(), quota


def get_odds(
    sport_key: str,
    regions: str = "uk",
    markets: str = "h2h,totals",
    odds_format: str = "decimal",
    event_ids: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, QuotaInfo]:
    """Fetch odds for a sport and parse into a flat DataFrame.

    Parameters
    ----------
    sport_key:
        The Odds API sport key, e.g. ``"soccer_fifa_world_cup"``.
    regions:
        Comma-separated bookmaker regions, e.g. ``"uk"`` or ``"uk,eu"``.
    markets:
        Comma-separated market types, e.g. ``"h2h,totals"``.
    odds_format:
        ``"decimal"`` (default) or ``"american"``.
    event_ids:
        Optional list of specific event IDs to fetch.

    Returns
    -------
    Tuple of (DataFrame, QuotaInfo).

    DataFrame columns
    -----------------
    event_id, commence_time, home_team, away_team,
    bookmaker_key, bookmaker_title,
    market, outcome_name, outcome_point, decimal_odds, retrieved_at
    """
    params: Dict[str, Any] = {
        "apiKey": _get_api_key(),
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    if event_ids:
        params["eventIds"] = ",".join(event_ids)

    resp = requests.get(
        f"{_BASE_URL}/sports/{sport_key}/odds",
        params=params,
        headers=_HEADERS,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    quota = _extract_quota(resp.headers)
    events = resp.json()
    rows = _parse_events(events)
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(
            columns=[
                "event_id",
                "commence_time",
                "home_team",
                "away_team",
                "bookmaker_key",
                "bookmaker_title",
                "market",
                "outcome_name",
                "outcome_point",
                "decimal_odds",
                "retrieved_at",
            ]
        )
    else:
        df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True, errors="coerce")
        df["retrieved_at"] = pd.to_datetime(df["retrieved_at"], utc=True, errors="coerce")
    return df, quota


def get_event_odds(
    sport_key: str,
    event_id: str,
    regions: str = "uk",
    markets: str = "btts",
    odds_format: str = "decimal",
) -> Tuple[pd.DataFrame, QuotaInfo]:
    """Fetch odds for ONE event via the per-event endpoint.

    Some markets (btts, player props) return 422 from the bulk ``/odds``
    endpoint and are only served per-event. Returns the same flat DataFrame
    shape as :func:`get_odds`.
    """
    params: Dict[str, Any] = {
        "apiKey": _get_api_key(),
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    resp = requests.get(
        f"{_BASE_URL}/sports/{sport_key}/events/{event_id}/odds",
        params=params,
        headers=_HEADERS,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    quota = _extract_quota(resp.headers)
    rows = _parse_events([resp.json()])
    df = pd.DataFrame(rows)
    if not df.empty:
        df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True, errors="coerce")
        df["retrieved_at"] = pd.to_datetime(df["retrieved_at"], utc=True, errors="coerce")
    return df, quota


def _parse_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten the nested Odds API response into a list of row dicts."""
    rows: List[Dict[str, Any]] = []
    for event in events:
        base = {
            "event_id": event.get("id"),
            "commence_time": event.get("commence_time"),
            "home_team": event.get("home_team"),
            "away_team": event.get("away_team"),
        }
        for bookie in event.get("bookmakers") or []:
            bookie_meta = {
                "bookmaker_key": bookie.get("key"),
                "bookmaker_title": bookie.get("title"),
                "retrieved_at": bookie.get("last_update"),
            }
            for mkt in bookie.get("markets") or []:
                market_key = mkt.get("key")
                for outcome in mkt.get("outcomes") or []:
                    rows.append(
                        {
                            **base,
                            **bookie_meta,
                            "market": market_key,
                            "outcome_name": outcome.get("name"),
                            "outcome_point": outcome.get("point"),
                            "decimal_odds": outcome.get("price"),
                        }
                    )
    return rows
