"""Tests for the World Football Elo rating system and outcome model."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from wca.models.elo import (
    AWAY_WIN,
    DEFAULT_K_FACTORS,
    DRAW,
    HOME_WIN,
    EloOutcomeModel,
    EloRater,
    classify_tournament,
    expected_score,
    goal_margin_multiplier,
)


# ---------------------------------------------------------------------------
# Goal-margin multiplier.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gd,expected",
    [
        (0, 1.0),
        (1, 1.0),
        (-1, 1.0),
        (2, 1.5),
        (-2, 1.5),
        (3, 1.75),
        (4, 1.75 + 1 / 8),
        (5, 1.75 + 2 / 8),
        (8, 1.75 + 5 / 8),
    ],
)
def test_goal_margin_multiplier(gd, expected):
    assert goal_margin_multiplier(gd) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# K-factor / tournament classification.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,importance",
    [
        ("FIFA World Cup", "world_cup"),
        ("FIFA World Cup qualification", "qualifier"),
        ("UEFA Euro", "continental"),
        ("UEFA Euro qualification", "qualifier"),
        ("Copa America", "continental"),
        ("African Cup of Nations", "continental"),
        ("AFC Asian Cup", "continental"),
        ("CONCACAF Gold Cup", "continental"),
        ("UEFA Nations League", "nations_league"),
        ("Friendly", "friendly"),
        ("Some Unknown Cup Of Mystery", "friendly"),  # 'cup' alone is not continental
        ("Totally Random Match", "friendly"),
        ("", "friendly"),
        ("FIFA WORLD CUP", "world_cup"),  # case-insensitive
    ],
)
def test_classify_tournament(name, importance):
    result = classify_tournament(name)
    assert result == importance, (name, result)


def test_k_factor_mapping_defaults():
    rater = EloRater()
    assert rater.k_for("FIFA World Cup") == DEFAULT_K_FACTORS["world_cup"] == 60.0
    assert rater.k_for("FIFA World Cup qualification") == DEFAULT_K_FACTORS["qualifier"] == 40.0
    assert rater.k_for("UEFA Euro") == DEFAULT_K_FACTORS["continental"] == 50.0
    assert rater.k_for("UEFA Nations League") == DEFAULT_K_FACTORS["nations_league"] == 30.0
    assert rater.k_for("Friendly") == DEFAULT_K_FACTORS["friendly"] == 20.0


def test_k_factor_custom_override():
    rater = EloRater(k_factors={"friendly": 5.0, "world_cup": 99.0})
    assert rater.k_for("Friendly") == 5.0
    assert rater.k_for("FIFA World Cup") == 99.0


# ---------------------------------------------------------------------------
# Expected score.
# ---------------------------------------------------------------------------


def test_expected_score_symmetry():
    assert expected_score(0.0) == pytest.approx(0.5)
    assert expected_score(400.0) == pytest.approx(10.0 / 11.0)
    # Complementary: E(diff) + E(-diff) == 1
    for d in (-300.0, -50.0, 75.0, 250.0):
        assert expected_score(d) + expected_score(-d) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Conservation: winner gains what loser drops.
# ---------------------------------------------------------------------------


def test_winner_gain_equals_loser_drop():
    rater = EloRater(initial_rating=1500.0, home_advantage=100.0)
    pre_home = rater.get_rating("A")
    pre_away = rater.get_rating("B")
    new_home, new_away = rater.rate_match(
        "A", "B", 2, 0, tournament="Friendly", neutral=False
    )
    gain = new_home - pre_home
    drop = pre_away - new_away
    assert gain == pytest.approx(drop)
    # Total rating mass conserved.
    assert (new_home + new_away) == pytest.approx(pre_home + pre_away)


def test_away_upset_conservation():
    rater = EloRater()
    rater.ratings["Strong"] = 1800.0
    rater.ratings["Weak"] = 1400.0
    pre_s = rater.get_rating("Strong")
    pre_w = rater.get_rating("Weak")
    # Weak (away) beats Strong (home) -> Weak gains, Strong loses.
    new_home, new_away = rater.rate_match(
        "Strong", "Weak", 0, 1, tournament="FIFA World Cup", neutral=True
    )
    assert new_away > pre_w  # weak side gained
    assert new_home < pre_s  # strong side lost
    assert (new_away - pre_w) == pytest.approx(pre_s - new_home)


# ---------------------------------------------------------------------------
# Neutral-venue symmetry.
# ---------------------------------------------------------------------------


def test_neutral_venue_symmetry():
    # On a neutral venue with equal ratings and the same scoreline, swapping
    # home/away labels must produce mirror-image rating changes.
    r1 = EloRater(home_advantage=100.0)
    nh1, na1 = r1.rate_match("A", "B", 1, 0, tournament="Friendly", neutral=True)
    delta_winner = nh1 - 1500.0

    r2 = EloRater(home_advantage=100.0)
    nh2, na2 = r2.rate_match("B", "A", 0, 1, tournament="Friendly", neutral=True)
    delta_winner2 = na2 - 1500.0  # B is away here and won

    assert delta_winner == pytest.approx(delta_winner2)


def test_home_advantage_applied_only_off_neutral():
    # With equal base ratings, a home win at a non-neutral venue should earn
    # the home side LESS than the same win at a neutral venue, because part of
    # the result was "expected" thanks to home advantage.
    r_home = EloRater(home_advantage=100.0)
    nh_home, _ = r_home.rate_match("A", "B", 1, 0, neutral=False)
    gain_home = nh_home - 1500.0

    r_neutral = EloRater(home_advantage=100.0)
    nh_neut, _ = r_neutral.rate_match("A", "B", 1, 0, neutral=True)
    gain_neutral = nh_neut - 1500.0

    assert gain_neutral > gain_home


def test_host_advantage_on_neutral_venue():
    # On a neutral venue, a flagged host gets the home advantage; the opponent
    # in the away slot should benefit if it is the host.
    rater = EloRater(home_advantage=100.0, host_advantage=True)
    # Host is the away team here.
    e_home_no_host = rater.expected_home("A", "B", neutral=True, host=None)
    e_home_host_away = rater.expected_home("A", "B", neutral=True, host="B")
    # If B (away) is host, the home expected score must drop.
    assert e_home_host_away < e_home_no_host
    # If A (home) is host, the home expected score must rise.
    e_home_host_home = rater.expected_home("A", "B", neutral=True, host="A")
    assert e_home_host_home > e_home_no_host


# ---------------------------------------------------------------------------
# rate_matches batch processing.
# ---------------------------------------------------------------------------


def test_rate_matches_chronological_and_history():
    df = pd.DataFrame(
        {
            "date": ["2026-06-12", "2026-06-11", "2026-06-13"],
            "home_team": ["A", "A", "B"],
            "away_team": ["B", "C", "C"],
            "home_score": [2, 1, 0],
            "away_score": [0, 1, 0],
            "tournament": ["Friendly", "FIFA World Cup", "UEFA Euro"],
            "neutral": [False, True, False],
        }
    )
    rater = EloRater()
    out = rater.rate_matches(df)
    assert set(out["final_ratings"]) == {"A", "B", "C"}
    # History should be in chronological order (sorted by date).
    dates = [h["date"] for h in out["history"]]
    assert dates == ["2026-06-11", "2026-06-12", "2026-06-13"]
    # Total mass conserved across all matches (zero-sum updates).
    assert sum(out["final_ratings"].values()) == pytest.approx(3 * 1500.0)


def test_rate_matches_missing_columns_raises():
    df = pd.DataFrame({"date": ["2026-06-11"], "home_team": ["A"]})
    with pytest.raises(ValueError):
        EloRater().rate_matches(df)


def test_rate_matches_host_column():
    df = pd.DataFrame(
        {
            "date": ["2026-06-11"],
            "home_team": ["A"],
            "away_team": ["USA"],
            "home_score": [0],
            "away_score": [0],
            "tournament": ["FIFA World Cup"],
            "neutral": [True],
            "host": ["USA"],
        }
    )
    rater = EloRater(host_advantage=True)
    out = rater.rate_matches(df)
    # USA were the host on a neutral venue and drew despite being favoured by
    # host advantage -> USA should lose a little rating, A should gain.
    assert out["final_ratings"]["USA"] < 1500.0
    assert out["final_ratings"]["A"] > 1500.0


# ---------------------------------------------------------------------------
# Serialization round trips.
# ---------------------------------------------------------------------------


def test_elorater_serialization_roundtrip():
    rater = EloRater(initial_rating=1500.0, home_advantage=85.0, host_advantage=False)
    rater.rate_match("A", "B", 3, 1, tournament="Copa America", neutral=True)
    s = rater.to_json()
    # JSON-serialisable.
    json.loads(s)
    restored = EloRater.from_json(s)
    assert restored.home_advantage == 85.0
    assert restored.host_advantage is False
    assert restored.ratings == pytest.approx(rater.ratings)
    assert restored.k_factors == rater.k_factors


# ---------------------------------------------------------------------------
# Ordered-logit outcome model.
# ---------------------------------------------------------------------------


def _simulate_outcomes(diffs, beta, c_lo, c_hi, scale, rng):
    """Sample ordinal outcomes from the true ordered-logit model."""
    x = np.asarray(diffs) / scale
    s_lo = 1.0 / (1.0 + np.exp(-(c_lo - beta * x)))
    s_hi = 1.0 / (1.0 + np.exp(-(c_hi - beta * x)))
    p_away = s_lo
    p_draw = s_hi - s_lo
    p_home = 1.0 - s_hi
    outcomes = []
    for pa, pd_, ph in zip(p_away, p_draw, p_home):
        outcomes.append(rng.choice([AWAY_WIN, DRAW, HOME_WIN], p=[pa, pd_, ph]))
    return np.array(outcomes)


def test_ordered_logit_recovers_parameters():
    rng = np.random.default_rng(20260610)
    scale = 400.0
    true_beta, true_c_lo, true_c_hi = 2.5, -0.8, 0.8
    diffs = rng.uniform(-600, 600, size=8000)
    outcomes = _simulate_outcomes(diffs, true_beta, true_c_lo, true_c_hi, scale, rng)

    model = EloOutcomeModel(scale=scale)
    model.fit(diffs, outcomes)

    assert model.fitted
    # Recover parameters to within a reasonable tolerance for a finite sample.
    assert model.beta == pytest.approx(true_beta, abs=0.35)
    assert model.c_lo == pytest.approx(true_c_lo, abs=0.25)
    assert model.c_hi == pytest.approx(true_c_hi, abs=0.25)


def test_predict_proba_sums_to_one_and_ordering():
    model = EloOutcomeModel()
    model.beta, model.c_lo, model.c_hi = 2.0, -0.6, 0.6
    for diff in (-500.0, -100.0, 0.0, 100.0, 500.0):
        p_home, p_draw, p_away = model.predict_proba(diff)
        assert p_home + p_draw + p_away == pytest.approx(1.0)
        assert 0.0 <= p_home <= 1.0
        assert 0.0 <= p_draw <= 1.0
        assert 0.0 <= p_away <= 1.0


def test_predict_proba_monotone_in_diff():
    model = EloOutcomeModel()
    model.beta, model.c_lo, model.c_hi = 2.0, -0.6, 0.6
    diffs = [-600.0, -300.0, -100.0, 0.0, 100.0, 300.0, 600.0]
    p_home = []
    p_away = []
    for d in diffs:
        ph, pd_, pa = model.predict_proba(d)
        p_home.append(ph)
        p_away.append(pa)
    # p_home strictly increasing, p_away strictly decreasing in diff.
    assert all(b > a for a, b in zip(p_home, p_home[1:]))
    assert all(b < a for a, b in zip(p_away, p_away[1:]))
    # Symmetry at diff=0: p_home(0) == p_away(0).
    ph0, pd0, pa0 = model.predict_proba(0.0)
    assert ph0 == pytest.approx(pa0)


def test_predict_proba_batch_matches_scalar():
    model = EloOutcomeModel()
    model.beta, model.c_lo, model.c_hi = 1.8, -0.5, 0.7
    diffs = [-200.0, 0.0, 150.0]
    batch = model.predict_proba_batch(diffs)
    assert batch.shape == (3, 3)
    for i, d in enumerate(diffs):
        ph, pd_, pa = model.predict_proba(d)
        assert batch[i, 0] == pytest.approx(ph)
        assert batch[i, 1] == pytest.approx(pd_)
        assert batch[i, 2] == pytest.approx(pa)
        assert batch[i].sum() == pytest.approx(1.0)


def test_outcome_model_serialization_roundtrip():
    model = EloOutcomeModel(scale=350.0)
    model.beta, model.c_lo, model.c_hi, model.fitted = 2.1, -0.7, 0.9, True
    s = model.to_json()
    json.loads(s)
    restored = EloOutcomeModel.from_json(s)
    assert restored.scale == 350.0
    assert restored.beta == pytest.approx(2.1)
    assert restored.c_lo == pytest.approx(-0.7)
    assert restored.c_hi == pytest.approx(0.9)
    assert restored.fitted is True
    # Predictions identical after round trip.
    assert restored.predict_proba(120.0) == pytest.approx(model.predict_proba(120.0))
