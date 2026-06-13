"""Tests for the player-level goalscorer pricing layer (wca.models.scorers)."""
import json
import math

import pytest

from wca.models.props import AnytimeScorerModel
from wca.models.scorers import (
    PlayerParams,
    ScorerPricer,
    load_player_overrides,
    players_for_team,
)


# ---------------------------------------------------------------------------
# Intensity / consistency with the underlying AnytimeScorerModel
# ---------------------------------------------------------------------------

def test_intensity_matches_anytime_model():
    pricer = ScorerPricer(pen_xg=0.18)
    base = AnytimeScorerModel(pen_xg=0.18)
    lam_team, share = 1.7, 0.30
    # The scorer pricer's anytime prob must equal the base model's exactly.
    line = pricer.price(lam_team, 2.5, share)
    assert line.p_anytime == pytest.approx(base.prob_anytime(lam_team, share))
    assert line.p_first == pytest.approx(
        base.prob_first_scorer(lam_team, share, 2.5)
    )


def test_penalty_taker_increases_all_markets():
    pricer = ScorerPricer()
    no_pen = pricer.price(1.7, 2.5, 0.30, penalty_taker=False)
    pen = pricer.price(1.7, 2.5, 0.30, penalty_taker=True)
    assert pen.p_anytime > no_pen.p_anytime
    assert pen.p_first > no_pen.p_first
    assert pen.p_two_plus > no_pen.p_two_plus


def test_share_zero_nonpenalty_gives_zero_scoring():
    pricer = ScorerPricer()
    line = pricer.price(1.7, 2.5, 0.0, penalty_taker=False)
    assert line.p_anytime == pytest.approx(0.0)
    assert line.p_first == pytest.approx(0.0)
    assert math.isinf(line.fair_anytime)


# ---------------------------------------------------------------------------
# Goal-count tail masses are internally consistent
# ---------------------------------------------------------------------------

def test_tail_masses_monotone_and_poisson_consistent():
    pricer = ScorerPricer()
    line = pricer.price(2.0, 3.0, 0.35, penalty_taker=True)
    # P(>=1) >= P(>=2) >= P(>=3)
    assert line.p_anytime >= line.p_two_plus >= line.p_three_plus >= 0.0
    # Reconstruct from the intensity and check the >=2 mass.
    lam = line.intensity
    expected_two = 1.0 - math.exp(-lam) * (1.0 + lam)
    assert line.p_two_plus == pytest.approx(expected_two)


def test_expected_minutes_scales_intensity():
    pricer = ScorerPricer()
    full = pricer.price(1.6, 2.4, 0.3)
    half = pricer.price(1.6, 2.4, 0.3, expected_minutes=45.0)
    assert half.intensity == pytest.approx(full.intensity * 0.5)


def test_invalid_share_raises():
    pricer = ScorerPricer()
    with pytest.raises(ValueError):
        pricer.intensity(1.5, 1.5)


# ---------------------------------------------------------------------------
# Double Delight / Hat-Trick Heaven EV
# ---------------------------------------------------------------------------

def test_double_delight_multiplier_above_one():
    pricer = ScorerPricer()
    line = pricer.price(1.7, 2.48, 0.30, penalty_taker=True)
    dd = pricer.double_delight_ev(line, offered_first_odds=4.5)
    # The boost can only add value: effective multiplier strictly > 1.
    assert dd["effective_mult"] > 1.0
    # And the EV must exceed the no-boost EV.
    assert dd["ev_per_unit"] > dd["ev_no_boost"]


def test_double_delight_ev_matches_hand_calc():
    pricer = ScorerPricer()
    line = pricer.price(1.7, 2.48, 0.30, penalty_taker=True)
    odds = 5.0
    dd = pricer.double_delight_ev(line, odds)
    pa = line.p_anytime
    p2g1 = line.p_two_plus / pa
    p3g1 = line.p_three_plus / pa
    exact1, exact2, three = 1 - p2g1, p2g1 - p3g1, p3g1
    ev = line.p_first * (exact1 * odds + exact2 * 2 * odds + three * 3 * odds)
    assert dd["ev_per_unit"] == pytest.approx(ev)


def test_double_delight_rejects_bad_odds():
    pricer = ScorerPricer()
    line = pricer.price(1.7, 2.48, 0.30)
    with pytest.raises(ValueError):
        pricer.double_delight_ev(line, 1.0)


# ---------------------------------------------------------------------------
# Override store loading
# ---------------------------------------------------------------------------

def test_load_overrides_skips_meta_keys(tmp_path):
    p = tmp_path / "players.json"
    p.write_text(json.dumps({
        "_note": "ignore me",
        "_schema": {"x": "y"},
        "Scotland": [
            {"name": "Lawrence Shankland", "npxg_share": 0.30,
             "penalty_taker": True, "expected_minutes": 85, "source": "analyst_estimate"}
        ],
    }))
    store = load_player_overrides(str(p))
    assert set(store.keys()) == {"Scotland"}
    rec = store["Scotland"][0]
    assert isinstance(rec, PlayerParams)
    assert rec.name == "Lawrence Shankland"
    assert rec.penalty_taker is True
    assert rec.npxg_share == pytest.approx(0.30)


def test_players_for_team_missing_returns_empty(tmp_path):
    p = tmp_path / "players.json"
    p.write_text(json.dumps({"Scotland": []}))
    assert players_for_team("Haiti", str(p)) == []


def test_load_overrides_missing_file_returns_empty():
    assert load_player_overrides("data/does_not_exist_xyz.json") == {}


def test_shipped_store_has_shankland():
    """The repo's data/players.json should seed Shankland for Scotland."""
    store = load_player_overrides("data/players.json")
    assert "Scotland" in store
    names = [p.name for p in store["Scotland"]]
    assert "Lawrence Shankland" in names
