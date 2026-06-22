"""Agent 2 — Team Intelligence.

Estimates starting lineups, player availability, travel/rest effects and
tactical matchup context from the raw data package.  Pure heuristics — no LLM
calls, no external network requests.

Input:  DataPackage
Output: TeamIntelligence
"""

from __future__ import annotations

import logging
from typing import Dict, List

from wca.agents.contracts import DataPackage, PlayerAvailability, TeamIntelligence

logger = logging.getLogger(__name__)

# Known penalty-taker roles that imply tactical significance.
_HIGH_IMPACT_KEYWORDS = frozenset(
    [
        "captain", "striker", "forward", "top scorer", "penalty",
        "key player", "star", "goalscorer", "talisman",
    ]
)

# Rough strength penalty for missing important players.
_IMPACT_PENALTY = 0.04     # 4% xG reduction per high-impact absentee
_TRAVEL_PENALTY = 0.02     # 2% for long-haul travel disadvantage
_MIN_STRENGTH = 0.70


def run(pkg: DataPackage) -> TeamIntelligence:
    """Produce team-intelligence context from *pkg*.

    Parameters
    ----------
    pkg:
        :class:`~wca.agents.contracts.DataPackage` produced by Agent 1.
    """
    home = pkg.fixture.home
    away = pkg.fixture.away

    # Build availability map from collected injury / suspension signals.
    availability: Dict[str, str] = {}
    for item in pkg.injuries + pkg.suspensions:
        availability[item.name] = item.status

    # Count missing players per team.
    home_missing = _count_missing(pkg.injuries, pkg.suspensions, home)
    away_missing = _count_missing(pkg.injuries, pkg.suspensions, away)

    # Strength adjustments.
    home_adj = max(_MIN_STRENGTH, 1.0 - home_missing * _IMPACT_PENALTY)
    away_adj = max(_MIN_STRENGTH, 1.0 - away_missing * _IMPACT_PENALTY)

    tactical_notes = _build_tactical_notes(
        home, away, pkg.injuries, pkg.suspensions, pkg.news_items
    )

    return TeamIntelligence(
        expected_lineups={"home": [], "away": []},   # lineup data not yet available
        player_availability=availability,
        tactical_notes=tactical_notes,
        strength_adjustments={"home": home_adj, "away": away_adj},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_missing(
    injuries: List[PlayerAvailability],
    suspensions: List[PlayerAvailability],
    team: str,
) -> int:
    """Count confirmed-out or high-concern players for *team*."""
    count = 0
    for item in injuries + suspensions:
        if item.team.lower() == team.lower() and item.status in ("out", "doubtful"):
            count += 1
    return count


def _build_tactical_notes(
    home: str,
    away: str,
    injuries: List[PlayerAvailability],
    suspensions: List[PlayerAvailability],
    news_items: list,
) -> List[str]:
    notes: List[str] = []

    home_out = [i for i in injuries + suspensions if i.team.lower() == home.lower() and i.status == "out"]
    away_out = [i for i in injuries + suspensions if i.team.lower() == away.lower() and i.status == "out"]
    home_doubt = [i for i in injuries + suspensions if i.team.lower() == home.lower() and i.status == "doubtful"]
    away_doubt = [i for i in injuries + suspensions if i.team.lower() == away.lower() and i.status == "doubtful"]

    if home_out:
        notes.append("%s confirmed absences: %s" % (home, ", ".join(i.name for i in home_out)))
    if away_out:
        notes.append("%s confirmed absences: %s" % (away, ", ".join(i.name for i in away_out)))
    if home_doubt:
        notes.append("%s doubts: %s" % (home, ", ".join(i.name for i in home_doubt)))
    if away_doubt:
        notes.append("%s doubts: %s" % (away, ", ".join(i.name for i in away_doubt)))

    # Surface high-signal news headlines.
    for item in news_items[:5]:
        title = item.get("title", "")
        score = item.get("score", 0)
        if score and int(score) >= 3:
            notes.append("News: %s" % title[:120])

    if not notes:
        notes.append("No significant team-intelligence flags found.")

    return notes
