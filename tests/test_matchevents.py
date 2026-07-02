"""Tests for the historical match-event pipeline (wca.data.matchevents).

No network is performed: the football-data path is exercised against a tiny
committed fixture (``tests/fixtures/football_data_sample.csv``, 6 real rows
from football-data.co.uk E0 2023/24), and the StatsBomb path against a small
synthetic events list.  ``data/processed/prop_priors.csv`` is gitignored, so
the load_priors fallback is exercised against a non-existent path.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wca.data import matchevents as me
from wca.data import statsbomb

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FD_SAMPLE = FIXTURES / "football_data_sample.csv"


# ---------------------------------------------------------------------------
# football-data column mapping
# ---------------------------------------------------------------------------

def _load_fd_wide():
    return me.parse_football_data_csv(FD_SAMPLE.read_text(encoding="latin-1"),
                                      season="2324")


def test_fd_column_mapping_values():
    """HS/AS->shots, HST/AST->sot, HC/AC->corners, HF/AF->fouls,
    HY/AY->yellows, HR/AR->reds — checked against the first real fixture row."""
    wide = _load_fd_wide()
    # Row 0: Burnley 0-3 Man City; HS=6 AS=17 HST=1 AST=8 HC=6 AC=5 HF=11 AF=8
    #        HY=0 AY=0 HR=1 AR=0
    r = wide.iloc[0]
    assert r["home"] == "Burnley" and r["away"] == "Man City"
    assert r["goals_home"] == 0 and r["goals_away"] == 3
    assert r["shots_home"] == 6 and r["shots_away"] == 17
    assert r["shots_on_target_home"] == 1 and r["shots_on_target_away"] == 8
    assert r["corners_home"] == 6 and r["corners_away"] == 5
    assert r["fouls_home"] == 11 and r["fouls_away"] == 8
    assert r["yellows_home"] == 0 and r["yellows_away"] == 0
    assert r["reds_home"] == 1 and r["reds_away"] == 0
    assert r["competition"] == "E0"


def test_fd_possession_and_xg_are_nan():
    """football-data carries neither possession nor xg -> NaN, not 0."""
    wide = _load_fd_wide()
    assert wide["possession_home"].isna().all()
    assert wide["xg_home"].isna().all()
    assert wide["xg_away"].isna().all()


def test_fd_missing_column_is_nan_not_zero():
    """A CSV lacking a stat column maps that field to NaN (never 0)."""
    csv = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC\n"
           "E0,11/08/2023,Burnley,Man City,0,3,6,5\n")
    wide = me.parse_football_data_csv(csv, season="2324")
    # corners present
    assert wide.iloc[0]["corners_home"] == 6
    # shots / sot / fouls / cards columns absent from the CSV -> all NaN
    for f in ("shots", "shots_on_target", "fouls", "yellows", "reds"):
        assert math.isnan(wide.iloc[0][f + "_home"])
        assert math.isnan(wide.iloc[0][f + "_away"])


def test_fd_date_parsing():
    wide = _load_fd_wide()
    assert wide.iloc[0]["date"] == pd.Timestamp("2023-08-11")


def test_fd_canonicalises_team_names():
    """USA -> United States via teamnames.canonical."""
    csv = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC\n"
           "X,01/01/2024,USA,Brazil,1,1,5,5\n")
    wide = me.parse_football_data_csv(csv, season="2324")
    assert wide.iloc[0]["home"] == "United States"


# ---------------------------------------------------------------------------
# Two team rows per match
# ---------------------------------------------------------------------------

def test_two_team_rows_per_match():
    wide = _load_fd_wide()
    rows = me.to_team_rows(wide)
    assert len(rows) == 2 * len(wide)
    # exactly one home + one away row per match_id
    by_match = rows.groupby("match_id")["is_home"].apply(list)
    for vals in by_match:
        assert sorted(vals) == [False, True]


def test_team_rows_team_opponent_swap():
    wide = _load_fd_wide()
    rows = me.to_team_rows(wide)
    mid = wide.iloc[0]["match_id"]
    sub = rows[rows["match_id"] == mid].set_index("is_home")
    home = sub.loc[True]
    away = sub.loc[False]
    assert home["team"] == away["opponent"]
    assert home["opponent"] == away["team"]
    # Burnley (home) shots = 6, Man City (away) shots = 17
    assert home["shots"] == 6 and away["shots"] == 17
    assert list(rows.columns) == list(me.UNIFIED_COLUMNS)


def test_team_rows_preserve_nan():
    """NaN per-side stats survive the explode (no zero-filling)."""
    csv = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC\n"
           "E0,11/08/2023,A,B,0,3,6,5\n")
    wide = me.parse_football_data_csv(csv, season="2324")
    rows = me.to_team_rows(wide)
    assert rows["shots"].isna().all()
    assert rows["fouls"].isna().all()


# ---------------------------------------------------------------------------
# StatsBomb SoT derivation + normalisation
# ---------------------------------------------------------------------------

def _synthetic_events():
    """Two-team event list: Home takes 3 shots (Goal, Saved, Blocked),
    Away takes 2 (Saved To Post, Off T).  SoT(home)=2, SoT(away)=1."""
    def shot(team, outcome):
        return {"type": {"name": "Shot"}, "team": {"name": team},
                "shot": {"outcome": {"name": outcome}, "statsbomb_xg": 0.1}}
    return [
        {"type": {"name": "Starting XI"}, "team": {"name": "Home"}},
        {"type": {"name": "Starting XI"}, "team": {"name": "Away"}},
        shot("Home", "Goal"),
        shot("Home", "Saved"),
        shot("Home", "Blocked"),
        shot("Away", "Saved To Post"),
        shot("Away", "Off T"),
    ]


def test_sot_derivation_matches_frozen_outcomes():
    props = statsbomb.match_props(_synthetic_events(),
                                  home_team="Home", away_team="Away")
    # frozen SOT_OUTCOMES = {Goal, Saved, Saved To Post}
    assert props["sot_home"] == 2  # Goal + Saved (Blocked excluded)
    assert props["sot_away"] == 1  # Saved To Post (Off T excluded)
    assert props["shots_home"] == 3 and props["shots_away"] == 2


def test_sot_outcomes_frozen_set():
    assert statsbomb.SOT_OUTCOMES == frozenset(
        {"Goal", "Saved", "Saved To Post"})


def test_statsbomb_wide_normalisation():
    sb = pd.DataFrame([{
        "match_id": 1, "season": "WC2022", "date": "2022-11-20",
        "home": "Qatar", "away": "Ecuador",
        "corners_home": 4, "corners_away": 6,
        "yellows_home": 1, "yellows_away": 2, "reds_home": 0, "reds_away": 0,
        "fouls_home": 10, "fouls_away": 12, "shots_home": 5, "shots_away": 9,
        "sot_home": 2, "sot_away": 4, "goals_home": 0, "goals_away": 2,
        "xg_home": 0.5, "xg_away": 1.8,
    }])
    wide = me.statsbomb_wide(matches_df=sb)
    r = wide.iloc[0]
    assert r["source"] == "statsbomb" and r["neutral"]
    assert r["shots_on_target_home"] == 2 and r["shots_on_target_away"] == 4
    assert r["xg_away"] == 1.8
    # possession absent from props csv -> NaN
    assert math.isnan(r["possession_home"])


def test_statsbomb_wide_missing_sot_is_nan():
    """An older props cache without sot columns leaves SoT NaN, not 0."""
    sb = pd.DataFrame([{
        "match_id": 1, "season": "WC2018", "date": "2018-06-14",
        "home": "Russia", "away": "Saudi Arabia",
        "corners_home": 6, "corners_away": 2,
        "yellows_home": 1, "yellows_away": 1, "reds_home": 0, "reds_away": 0,
        "goals_home": 5, "goals_away": 0,
    }])
    wide = me.statsbomb_wide(matches_df=sb)
    assert math.isnan(wide.iloc[0]["shots_on_target_home"])


# ---------------------------------------------------------------------------
# Baselines / dispersion / intl-domestic adjustment
# ---------------------------------------------------------------------------

def test_mom_dispersion_poisson_when_not_overdispersed():
    # var <= mean -> effectively Poisson (huge k)
    assert me._mom_dispersion(2.0, 2.0) >= 1e5
    # Var = mu + mu^2/k with mu=4, k=8 -> var = 4 + 2 = 6
    assert me._mom_dispersion(4.0, 6.0) == pytest.approx(8.0)


def test_global_baseline_drops_nan():
    rows = pd.DataFrame({
        "team": ["A", "B", "A", "B"],
        "source": ["football-data"] * 4,
        "corners": [5.0, 7.0, np.nan, 3.0],
        "yellows": [1, 2, 1, 0], "reds": [0, 0, 0, 0],
        "shots_on_target": [3, 4, 2, 1], "fouls": [10, 11, 9, 8],
        "shots": [9, 10, 8, 7],
    })
    gb = me.global_baseline(rows, "corners")
    assert gb["n"] == 3  # the NaN dropped
    assert gb["mean"] == pytest.approx((5 + 7 + 3) / 3)


def test_intl_domestic_adjustment_ratio():
    rows = pd.DataFrame({
        "team": ["A", "B", "C", "D"],
        "source": ["statsbomb", "statsbomb", "football-data", "football-data"],
        "corners": [6.0, 4.0, 5.0, 5.0],
        "yellows": [1, 1, 1, 1], "reds": [0, 0, 0, 0],
        "shots_on_target": [3, 3, 3, 3], "fouls": [10, 10, 10, 10],
        "shots": [9, 9, 9, 9],
    })
    # intl mean 5.0, dom mean 5.0 -> 1.0
    assert me.intl_domestic_adjustment(rows, "corners") == pytest.approx(1.0)


def test_intl_domestic_adjustment_defaults_one_when_one_side_empty():
    rows = pd.DataFrame({
        "team": ["A", "B"], "source": ["statsbomb", "statsbomb"],
        "corners": [6.0, 4.0], "yellows": [1, 1], "reds": [0, 0],
        "shots_on_target": [3, 3], "fouls": [10, 10], "shots": [9, 9],
    })
    assert me.intl_domestic_adjustment(rows, "corners") == 1.0


def test_cards_market_is_yellows_plus_reds():
    rows = pd.DataFrame({
        "team": ["A", "B"], "source": ["football-data"] * 2,
        "corners": [5.0, 5.0], "yellows": [2, 1], "reds": [1, 0],
        "shots_on_target": [3, 3], "fouls": [10, 10], "shots": [9, 9],
    })
    gb = me.global_baseline(rows, "cards")
    assert gb["mean"] == pytest.approx((2 + 1 + 1 + 0) / 2)  # = 2.0


# ---------------------------------------------------------------------------
# Empirical-Bayes shrinkage
# ---------------------------------------------------------------------------

def _eb_rows():
    # League corners mean = 4.0 across all rows; team H has its own high rate.
    return pd.DataFrame({
        "team": ["H", "H", "H", "H", "L", "L", "L", "L"],
        "source": ["football-data"] * 8,
        "corners": [10.0, 10.0, 10.0, 10.0, 0.0, 0.0, 0.0, 0.0],
        "yellows": [1] * 8, "reds": [0] * 8,
        "shots_on_target": [3] * 8, "fouls": [10] * 8, "shots": [9] * 8,
    })


def test_eb_shrinks_toward_league_mean():
    rows = _eb_rows()
    league = me.global_baseline(rows, "corners")["mean"]  # = 5.0
    eb = me.empirical_bayes_priors(rows, "corners", eb_tau=4.0)
    by_team = eb.set_index("entity")
    h = by_team.loc["H", "mean"]
    # H raw rate 10, n=4, tau=4 -> (4*10 + 4*5)/(8) = 7.5, between 5 and 10
    assert h == pytest.approx((4 * 10 + 4 * league) / (4 + 4))
    assert league < h < 10.0
    assert by_team.loc["H", "shrinkage_weight"] == pytest.approx(4 / 8)


def test_eb_more_data_means_less_shrinkage():
    """A team with more matches is pulled less toward the league mean."""
    base = _eb_rows()
    # add 4 more H rows at rate 10 (n=8) and compare shrunk mean to n=4
    extra = pd.DataFrame({
        "team": ["H"] * 4, "source": ["football-data"] * 4,
        "corners": [10.0] * 4, "yellows": [1] * 4, "reds": [0] * 4,
        "shots_on_target": [3] * 4, "fouls": [10] * 4, "shots": [9] * 4,
    })
    more = pd.concat([base, extra], ignore_index=True)
    eb4 = me.empirical_bayes_priors(base, "corners", eb_tau=4.0
                                    ).set_index("entity").loc["H", "mean"]
    eb8 = me.empirical_bayes_priors(more, "corners", eb_tau=4.0
                                    ).set_index("entity").loc["H", "mean"]
    # league mean shifts (more H rows), but H is closer to its raw 10 with n=8
    assert eb8 > eb4


def test_build_prop_priors_has_global_and_team_rows():
    rows = _eb_rows()
    table = me.build_prop_priors(rows)
    assert (table["entity"] == "GLOBAL").sum() == len(me.PRIOR_MARKETS)
    # team rows for each market
    teams = table[table["entity"] != "GLOBAL"]
    assert set(teams["entity"].unique()) == {"H", "L"}
    assert set(table.columns) >= {"entity", "market", "mean",
                                  "dispersion_k", "n_matches", "shrinkage_weight"}


# ---------------------------------------------------------------------------
# load_priors fallback (backward-compat contract)
# ---------------------------------------------------------------------------

def test_load_priors_missing_file_falls_back(tmp_path):
    priors = me.load_priors(str(tmp_path / "does_not_exist.csv"))
    assert "GLOBAL" in priors
    # falls back to the hard-coded league-team means
    assert priors["GLOBAL"]["corners"]["mean"] == pytest.approx(
        me.LEAGUE_TEAM_FALLBACK["corners"])
    assert priors["GLOBAL"]["cards"]["mean"] == pytest.approx(
        me.LEAGUE_TEAM_FALLBACK["cards"])


def test_load_priors_malformed_falls_back(tmp_path):
    bad = tmp_path / "prop_priors.csv"
    bad.write_text("not,the,right,columns\n1,2,3,4\n")
    priors = me.load_priors(str(bad))
    assert "GLOBAL" in priors
    assert priors["GLOBAL"]["corners"]["mean"] == pytest.approx(
        me.LEAGUE_TEAM_FALLBACK["corners"])


def test_load_priors_roundtrip(tmp_path):
    rows = _eb_rows()
    path = tmp_path / "prop_priors.csv"
    me.write_prop_priors(rows, path=str(path))
    priors = me.load_priors(str(path))
    assert "GLOBAL" in priors and "H" in priors
    assert priors["H"]["corners"]["mean"] > priors["GLOBAL"]["corners"]["mean"]


def test_team_prior_canonicalises_and_falls_back(tmp_path):
    rows = pd.DataFrame({
        "team": ["United States", "United States"],
        "source": ["football-data"] * 2,
        "corners": [8.0, 8.0], "yellows": [1, 1], "reds": [0, 0],
        "shots_on_target": [3, 3], "fouls": [10, 10], "shots": [9, 9],
    })
    path = tmp_path / "prop_priors.csv"
    me.write_prop_priors(rows, path=str(path))
    priors = me.load_priors(str(path))
    # "USA" canonicalises to "United States"
    assert me.team_prior(priors, "USA", "corners") == pytest.approx(
        me.team_prior(priors, "United States", "corners"))
    # unknown team -> GLOBAL mean
    assert me.team_prior(priors, "Atlantis", "corners") == pytest.approx(
        priors["GLOBAL"]["corners"]["mean"])
