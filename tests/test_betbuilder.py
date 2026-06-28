"""Tests for the bet-builder market models (wca.models.betbuilder)."""
import math

import pytest

from wca.models import betbuilder as bb
from wca.models.scorers import PlayerParams


def test_team_total_goals_is_poisson_and_fair_odds_consistent():
    lines = bb.team_total_goals("X", 1.5)
    by_line = {l.line: l for l in lines}
    # P(over 0.5) = 1 - e^-1.5
    assert by_line[0.5].p_over == pytest.approx(1 - math.exp(-1.5), rel=1e-9)
    for l in lines:
        assert 0.0 <= l.p_over <= 1.0
        assert l.p_under == pytest.approx(1.0 - l.p_over)
        if l.p_over > 0:
            assert l.fair_over == pytest.approx(1.0 / l.p_over)


def test_team_goal_over_prob_monotonic_in_lambda():
    weak = bb.team_total_goals("X", 0.8)[1].p_over   # line 1.5
    strong = bb.team_total_goals("X", 2.2)[1].p_over
    assert strong > weak


def test_team_shots_scale_with_attack_strength():
    base = bb.team_total_shots("X", bb.BASE_TEAM_LAMBDA)[0].mean
    assert base == pytest.approx(bb.TEAM_PRIORS["shots"][0], rel=1e-6)
    hotter = bb.team_total_shots("X", 2.0 * bb.BASE_TEAM_LAMBDA)[0].mean
    assert hotter > base


def test_player_to_be_booked_bounds_and_monotonic():
    low = bb.player_to_be_booked("p", "T", bb.PlayerRate("p", "T", yellows_p90=0.1))
    high = bb.player_to_be_booked("p", "T", bb.PlayerRate("p", "T", yellows_p90=0.5))
    assert 0.0 <= low.prob <= 1.0
    assert high.prob > low.prob
    assert high.fair == pytest.approx(1.0 / high.prob)


def test_player_sot_minutes_proration():
    full = bb.player_shots_on_target("p", "T", bb.PlayerRate("p", "T", sot_p90=1.5))
    sub = bb.player_shots_on_target("p", "T", bb.PlayerRate("p", "T", sot_p90=1.5,
                                                            expected_minutes=30))
    assert sub[0].mean < full[0].mean


def test_ev_vs_offer_fee_math():
    # 55% model prob at 2.0 on a 2% commission venue.
    ev = bb.ev_vs_offer(0.55, 2.0, "betfair")
    assert ev["net_odds"] == pytest.approx(1.0 + 1.0 * 0.98)
    assert ev["ev_per_unit"] == pytest.approx(0.55 * 1.98)
    # zero-fee venue: net == gross
    ev0 = bb.ev_vs_offer(0.5, 3.0, "smarkets")
    assert ev0["net_odds"] == pytest.approx(3.0)


def test_price_with_overround_adds_margin():
    fair = [0.5, 0.3, 0.2]
    disp = bb.price_with_overround(fair, margin=0.05)
    implied = sum(1.0 / o for o in disp)
    assert implied == pytest.approx(1.05, rel=1e-9)


def test_ratestore_falls_back_to_priors_without_db():
    store = bb.RateStore(None)
    tr = store.team("Nowhere")
    assert tr.source == "prior"
    assert tr.shots_pm is None
    pr = store.player("Nowhere", "Ghost")
    assert pr.source == "prior"


def test_ratestore_missing_db_path_is_graceful():
    store = bb.RateStore("/does/not/exist.db")
    assert store.loaded is False
    assert store.team("X").source == "prior"


def test_fixture_betbuilder_payload_shape():
    pay = bb.fixture_betbuilder(
        "England", "Brazil", 1.8, 1.4,
        scorers={"England": [PlayerParams("Kane", "England", 0.28, True)],
                 "Brazil": [PlayerParams("Vini", "Brazil", 0.22)]},
    )
    assert pay["fixture"] == "England vs Brazil"
    for key in ("team_totals", "match_cards", "player_to_score", "player_props"):
        assert key in pay and isinstance(pay[key], list)
    assert len(pay["player_to_score"]) == 2
    # each team contributes goals+shots+sot+fouls+corners lines
    markets = {row["market"] for row in pay["team_totals"]}
    assert {"team_total_goals", "team_total_shots", "team_total_sot",
            "team_total_fouls", "team_total_corners"} <= markets


def test_round_odds_handles_inf():
    assert bb._round_odds(float("inf")) is None
    assert bb._round_odds(2.0) == 2.0
