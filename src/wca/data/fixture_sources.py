"""Result feeds for cross-verifying the martj42 dataset.

Two *independent*, keyless, CI-friendly sources are queried per date and the
results reconciled (:mod:`wca.data.reconcile`). A correction is only ever
auto-staged when BOTH sources agree, so a single flaky feed can never push a
bad score into the model.

Sources
-------
ESPN hidden scoreboard JSON
    ``site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates=YYYYMMDD``
    No key. We sweep the international leagues (FIFA WC, qualifiers by
    confederation, friendlies).

TheSportsDB
    ``www.thesportsdb.com/api/v1/json/{key}/eventsday.php?d=YYYY-MM-DD&s=Soccer``
    Free tier (public key ``3``). Covers internationals/friendlies.

Both adapters are *defensive*: any network/parse failure yields an empty list
and is logged, never raised — a verification sweep must degrade gracefully.
Team names are returned in canonical (martj42) spelling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

from wca.data.teamnames import canonical

logger = logging.getLogger(__name__)

_TIMEOUT = 20
_HEADERS = {"User-Agent": "WorldCupAlpha/0.1 (data-integrity; research)"}

# ESPN soccer "leagues" that carry international results. The scoreboard
# endpoint is per-league, so we sweep the relevant ones.
ESPN_LEAGUES = [
    "fifa.world",            # World Cup (finals)
    "fifa.worldq.afc",
    "fifa.worldq.caf",
    "fifa.worldq.concacaf",
    "fifa.worldq.conmebol",
    "fifa.worldq.ofc",
    "fifa.worldq.uefa",
    "fifa.friendly",         # international friendlies
]


@dataclass(frozen=True)
class FixtureResult:
    """A single finished match as reported by one source."""
    date: str            # ISO YYYY-MM-DD
    home_team: str       # canonical spelling
    away_team: str
    home_score: int
    away_score: int
    source: str
    tournament: Optional[str] = None

    @property
    def key(self) -> tuple:
        return (self.date, self.home_team, self.away_team)


def _get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # pragma: no cover - network guard
        logger.warning("fixture source GET failed (%s): %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# ESPN
# ---------------------------------------------------------------------------

def espn_results(date_iso: str, leagues: Optional[List[str]] = None) -> List[FixtureResult]:
    """Finished results from ESPN for a single date (``YYYY-MM-DD``)."""
    leagues = leagues or ESPN_LEAGUES
    yyyymmdd = date_iso.replace("-", "")
    out: List[FixtureResult] = []
    for league in leagues:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
        data = _get_json(url, params={"dates": yyyymmdd})
        if not data:
            continue
        for ev in data.get("events", []):
            fr = _parse_espn_event(ev, date_iso, league)
            if fr is not None:
                out.append(fr)
    return out


def _parse_espn_event(ev: dict, date_iso: str, league: str) -> Optional[FixtureResult]:
    try:
        comp = ev["competitions"][0]
        status = (comp.get("status") or ev.get("status") or {}).get("type", {})
        if not status.get("completed", False):
            return None  # not finished -> ignore
        home = away = None
        hs = as_ = None
        for c in comp["competitors"]:
            name = canonical(c["team"]["displayName"])
            score = int(c["score"])
            if c["homeAway"] == "home":
                home, hs = name, score
            else:
                away, as_ = name, score
        if home is None or away is None:
            return None
        tourn = (ev.get("league") or {}).get("name") or league
        return FixtureResult(date_iso, home, away, hs, as_, "espn", tourn)
    except (KeyError, ValueError, TypeError) as exc:  # pragma: no cover
        logger.debug("ESPN event parse skipped: %s", exc)
        return None


# ---------------------------------------------------------------------------
# TheSportsDB
# ---------------------------------------------------------------------------

def thesportsdb_results(date_iso: str, api_key: str = "3") -> List[FixtureResult]:
    """Finished international results from TheSportsDB for a single date."""
    url = f"https://www.thesportsdb.com/api/v1/json/{api_key}/eventsday.php"
    data = _get_json(url, params={"d": date_iso, "s": "Soccer"})
    if not data:
        return []
    out: List[FixtureResult] = []
    for ev in (data.get("events") or []):
        fr = _parse_tsdb_event(ev, date_iso)
        if fr is not None:
            out.append(fr)
    return out


# Leagues within TheSportsDB that represent national-team football. We filter to
# these to avoid club fixtures polluting the verification set.
_TSDB_INTL_HINTS = ("World Cup", "Qualif", "Friendl", "Nations League",
                    "Euro", "Copa America", "Africa Cup", "Asian Cup", "Gold Cup")


def _parse_tsdb_event(ev: dict, date_iso: str) -> Optional[FixtureResult]:
    try:
        league = ev.get("strLeague") or ""
        if not any(h.lower() in league.lower() for h in _TSDB_INTL_HINTS):
            return None
        hs_raw, as_raw = ev.get("intHomeScore"), ev.get("intAwayScore")
        if hs_raw in (None, "") or as_raw in (None, ""):
            return None  # not yet played / no score
        home = canonical(ev["strHomeTeam"])
        away = canonical(ev["strAwayTeam"])
        return FixtureResult(
            date_iso, home, away, int(hs_raw), int(as_raw), "thesportsdb", league
        )
    except (KeyError, ValueError, TypeError) as exc:  # pragma: no cover
        logger.debug("TheSportsDB event parse skipped: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

SOURCES = {
    "espn": espn_results,
    "thesportsdb": thesportsdb_results,
}


def gather(date_iso: str) -> Dict[str, List[FixtureResult]]:
    """Return ``{source_name: [FixtureResult, ...]}`` for one date."""
    return {name: fn(date_iso) for name, fn in SOURCES.items()}
