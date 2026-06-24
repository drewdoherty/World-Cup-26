"""Tests for wca.lineup — lineup-strength from players.db.

Builds a tiny real players.db (via build_players_db on synthetic frames) so the
rating path exercises the actual SQLite store, name-matching and injury logic.
"""
import json
import math

import numpy as np
import pandas as pd
import pytest

from wca.data import players_db
from wca.lineup import (
    LineupStrength,
    MatchLineups,
    get_lineup_strength,
    load_injuries,
    resolve_match,
    team_lineup_strength,
)


# ---------------------------------------------------------------------------
# Match / injuries resolution (pure)
# ---------------------------------------------------------------------------

def test_resolve_match_variants():
    assert resolve_match("Brazil vs Morocco") == ("Brazil", "Morocco")
    assert resolve_match(("USA", "Brazil")) == ("United States", "Brazil")
    assert resolve_match({"home": "USA", "away": "Brazil"}) == ("United States", "Brazil")
    with pytest.raises(ValueError):
        resolve_match("not a fixture string")


def test_load_injuries_dict_and_file(tmp_path):
    inj = load_injuries({"France": ["Kylian Mbappé"], "_note": "x"})
    assert "France" in inj
    assert "kylian mbappe" in inj["France"]
    p = tmp_path / "injuries.json"
    p.write_text(json.dumps({"Brazil": ["Neymar"]}))
    assert "neymar" in load_injuries(str(p))["Brazil"]
    assert load_injuries(None, path=str(tmp_path / "missing.json")) == {}


# ---------------------------------------------------------------------------
# Rating, against a real (tiny) players.db
# ---------------------------------------------------------------------------

def _players_frame():
    # minutes=90 so npxg_p90 == npxg_sum (easy exact assertions).
    rows = [
        ("Olivier Giroud", "France", 0.5),
        ("Kylian Mbappé Lottin", "France", 0.4),
        ("Antoine Griezmann", "France", 0.3),
        ("Neymar da Silva Santos Junior", "Brazil", 0.6),
    ]
    return pd.DataFrame([
        {"player": p, "team": t, "minutes": 90.0, "shots": 3, "sot": 2,
         "goals": 1, "xg_sum": x, "npxg_sum": x, "yellows": 0, "reds": 0,
         "matches": 1}
        for p, t, x in rows
    ])


def _matches_frame():
    return pd.DataFrame([{
        "match_id": 1, "home": "France", "away": "Brazil",
        "shots_home": 10, "sot_home": 4, "corners_home": 6, "fouls_home": 12,
        "yellows_home": 1, "reds_home": 0,
        "shots_away": 8, "sot_away": 3, "corners_away": 5, "fouls_away": 10,
        "yellows_away": 2, "reds_away": 0,
    }])


@pytest.fixture()
def db_path(tmp_path):
    squads = {
        "France": ["Olivier Giroud", "Kylian Mbappé", "Antoine Griezmann",
                   "Phantom Prospect"],
        "Brazil": ["Neymar Junior"],  # 2-token -> confident match to SB full name
        "Scotland": ["Lawrence Shankland"],  # no StatsBomb history
    }
    sp = tmp_path / "squads.json"
    op = tmp_path / "players.json"
    sp.write_text(json.dumps(squads))
    op.write_text(json.dumps({"_note": "x"}))
    dbp = tmp_path / "players.db"
    players_db.build_players_db(
        squads_path=str(sp), overrides_path=str(op), db_path=str(dbp),
        generated_utc="2026-06-24T00:00:00Z",
        matches_df=_matches_frame(), players_df=_players_frame())
    return str(dbp)


def test_team_rating_sums_available_npxg(db_path):
    ls = team_lineup_strength("France", db_path=db_path)
    assert isinstance(ls, LineupStrength)
    # Giroud .5 + Mbappe .4 + Griezmann .3 (Phantom has no stats).
    assert math.isclose(ls.rating, 1.2)
    assert ls.n_with_stats == 3
    assert ls.absences == []
    assert "no injury feed" in ls.lineup_name


def test_injury_removes_and_lists_absence(db_path):
    ls = team_lineup_strength("France", db_path=db_path,
                              injuries={"France": ["Kylian Mbappé"]})
    assert math.isclose(ls.rating, 0.8)  # Mbappe removed
    assert ls.absences == ["Kylian Mbappé"]
    assert ls.n_with_stats == 2
    assert "1 out" in ls.lineup_name


def test_no_history_team_is_data_pending(db_path):
    ls = team_lineup_strength("Scotland", db_path=db_path)
    assert ls.rating is None
    assert ls.source == "data-pending"
    assert "data-pending" in ls.lineup_name


def test_lineup_unpacks_as_spec_triple(db_path):
    name, rating, absences = get_lineup_strength(
        "France vs Brazil", team="France", db_path=db_path)
    assert isinstance(name, str)
    assert math.isclose(rating, 1.2)
    assert absences == []


def test_get_both_sides(db_path):
    ml = get_lineup_strength("France vs Brazil", db_path=db_path)
    assert isinstance(ml, MatchLineups)
    assert ml.home.team == "France"
    assert ml.away.team == "Brazil"
    assert math.isclose(ml.away.rating, 0.6)


def test_team_not_in_match_raises(db_path):
    with pytest.raises(ValueError):
        get_lineup_strength("France vs Brazil", team="Spain", db_path=db_path)


def test_top_n_caps_contributors(db_path):
    ls = team_lineup_strength("France", db_path=db_path, top_n=1)
    assert math.isclose(ls.rating, 0.5)  # only the best (Giroud)
    assert len(ls.contributors) == 1
