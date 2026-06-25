"""Betfair Exchange team spellings must canonicalize to the results dataset.

Betfair's MATCH_ODDS event names use their own national-team spellings; any that
fail to map to the martj42 results spelling silently drop the fixture from the
card (the 20→fewer collapse), so each must resolve through ``canonical``.
"""
from __future__ import annotations

import csv
import pathlib

import pytest

from wca.data.teamnames import canonical

# Betfair spelling (as seen in Exchange event names) -> results.csv spelling.
_BETFAIR_TO_RESULTS = {
    "Ivory Coast": "Ivory Coast",
    "Curacao": "Curaçao",
    "Cape Verde": "Cape Verde",
    "Bosnia": "Bosnia and Herzegovina",
    "USA": "United States",
    "IR Iran": "Iran",
    "Korea Republic": "South Korea",
    "Czechia": "Czech Republic",
    "Congo DR": "DR Congo",
}


@pytest.mark.parametrize("betfair,expected", sorted(_BETFAIR_TO_RESULTS.items()))
def test_betfair_names_canonicalize(betfair, expected):
    assert canonical(betfair) == expected


def test_targets_exist_in_results_dataset():
    """The canonical targets must actually appear in martj42_cleaned.csv."""
    csv_path = pathlib.Path("data/raw/martj42_cleaned.csv")
    if not csv_path.exists():  # dataset not present in this checkout — skip.
        pytest.skip("results dataset not available")
    teams = set()
    with csv_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            teams.add(row["home_team"])
            teams.add(row["away_team"])
    for expected in set(_BETFAIR_TO_RESULTS.values()):
        assert expected in teams, "%r not in results dataset" % expected
