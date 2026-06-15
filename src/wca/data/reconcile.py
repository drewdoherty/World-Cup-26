"""Reconcile martj42 against two independent feeds.

Policy (deliberately conservative — this feeds a real-money model):

* A score is only **auto-staged** as a correction when BOTH sources report the
  same result for the same unordered team pair on the same date, AND that result
  differs from (or is absent in) martj42.
* Anything else — sources disagree, only one source has the match, martj42 and a
  lone source differ — goes to a **review** list and is NEVER applied
  automatically. A human resolves those (as we did for the 7-item report).

Orientation is handled explicitly: ESPN / TheSportsDB may list home/away in a
different order than martj42 (common at neutral-site fixtures). We match on the
unordered team set and re-express any staged correction in *martj42's* existing
orientation so the overlay key stays stable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from wca.data.fixture_sources import FixtureResult

logger = logging.getLogger(__name__)


@dataclass
class Reconciliation:
    """Outcome of reconciling one date."""
    staged: List[Dict[str, Any]]   # corrections ready for the overlay
    review: List[Dict[str, Any]]   # discrepancies needing a human


def _team_score_map(fr: FixtureResult) -> Dict[str, int]:
    return {fr.home_team: fr.home_score, fr.away_team: fr.away_score}


def _agree(a: FixtureResult, b: FixtureResult) -> bool:
    """True iff two source results describe the same teams and same scores."""
    return _team_score_map(a) == _team_score_map(b)


def _index_by_pair(results: List[FixtureResult]) -> Dict[frozenset, FixtureResult]:
    """Index a source's results by unordered team pair (last write wins)."""
    out: Dict[frozenset, FixtureResult] = {}
    for fr in results:
        out[frozenset((fr.home_team, fr.away_team))] = fr
    return out


def reconcile_date(
    raw_df: pd.DataFrame,
    gathered: Dict[str, List[FixtureResult]],
    date_iso: str,
) -> Reconciliation:
    """Reconcile one date's worth of source results against martj42.

    *raw_df* is the martj42 frame read as strings (``dtype=str``). *gathered* is
    ``{source: [FixtureResult]}`` as returned by
    :func:`wca.data.fixture_sources.gather`, expected to hold exactly two sources.
    """
    names = list(gathered.keys())
    if len(names) != 2:
        raise ValueError("reconcile_date expects exactly two sources")
    src_a, src_b = names
    idx_a = _index_by_pair(gathered[src_a])
    idx_b = _index_by_pair(gathered[src_b])

    day = raw_df[raw_df["date"] == date_iso]
    # martj42 fixtures on this date, indexed by unordered pair -> row dict.
    mj_idx: Dict[frozenset, Dict[str, Any]] = {}
    for _, row in day.iterrows():
        mj_idx[frozenset((row["home_team"], row["away_team"]))] = row.to_dict()

    staged: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []

    for pair in set(idx_a) | set(idx_b):
        a = idx_a.get(pair)
        b = idx_b.get(pair)
        teams = tuple(pair)

        if a is None or b is None:
            # Only one source saw it -> not enough to act on.
            seen = a or b
            review.append({
                "date": date_iso, "teams": list(teams),
                "issue": "single_source",
                "source": seen.source,
                "score": f"{seen.home_team} {seen.home_score}-{seen.away_score} {seen.away_team}",
            })
            continue

        if not _agree(a, b):
            review.append({
                "date": date_iso, "teams": list(teams),
                "issue": "sources_disagree",
                src_a: f"{a.home_team} {a.home_score}-{a.away_score} {a.away_team}",
                src_b: f"{b.home_team} {b.home_score}-{b.away_score} {b.away_team}",
            })
            continue

        # Both sources agree. Compare to martj42.
        consensus = _team_score_map(a)  # team -> score
        mj = mj_idx.get(pair)
        source_tag = f"{src_a}+{src_b}"

        if mj is None:
            # Omitted fixture -> stage an INSERT in the sources' orientation.
            staged.append({
                "date": date_iso,
                "home_team": a.home_team, "away_team": a.away_team,
                "corrected_home_score": a.home_score,
                "corrected_away_score": a.away_score,
                "source": source_tag,
                "tournament": a.tournament or "Friendly",
                "_op": "insert",
            })
            continue

        # martj42 has the fixture. Re-express consensus in martj42 orientation.
        mh, ma = mj["home_team"], mj["away_team"]
        want_h, want_a = consensus[mh], consensus[ma]
        cur_h, cur_a = mj["home_score"], mj["away_score"]
        cur_missing = str(cur_h).strip() in ("", "NA", "nan") or \
            str(cur_a).strip() in ("", "NA", "nan")
        if cur_missing or int(cur_h) != want_h or int(cur_a) != want_a:
            staged.append({
                "date": date_iso,
                "home_team": mh, "away_team": ma,
                "corrected_home_score": want_h,
                "corrected_away_score": want_a,
                "source": source_tag,
                "_op": "fill" if cur_missing else "update",
            })

    return Reconciliation(staged=staged, review=review)
