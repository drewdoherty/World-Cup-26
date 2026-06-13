"""Tests for :mod:`wca.boosts` — pure boost pricing against the model feed.

No network, no clock, no files (except a tolerance probe of
:func:`load_scores_feed` on a missing path). A single hand-built ``scores_feed``
with one fixture exercises every market branch and — critically — the
percentage-vs-probability unit handling (``model_1x2`` is [0,1], but
``over_under``/``btts``/``scores[].prob`` are 0–100).
"""

from __future__ import annotations

import math

import pytest

from wca.boosts import (
    MIN_EDGE,
    Boost,
    BoostEval,
    evaluate_boost,
    load_scores_feed,
)


# A deliberately simple, hand-checkable fixture.
#   model_1x2: home 0.50, draw 0.25, away 0.25  (already probabilities)
#   over_under line 2.5: over 60.0%, under 40.0%
#   btts: 55.0%
#   scores grid: enough mass (>80%) to re-aggregate a non-primary line.
FIXTURE = {
    "fixture": "Brazil vs Morocco",
    "kickoff": "2026-06-20T19:00:00+00:00",
    "model_1x2": {"home": 0.50, "draw": 0.25, "away": 0.25},
    "over_under": {"line": 2.5, "over": 60.0, "under": 40.0},
    "btts": 55.0,
    "scores": [
        {"score": "1-0", "prob": 30.0, "fair": 3.33},   # 1 goal  -> under 2.5
        {"score": "1-1", "prob": 25.0, "fair": 4.00},   # 2 goals -> under 2.5
        {"score": "2-1", "prob": 20.0, "fair": 5.00},   # 3 goals -> over 2.5
        {"score": "2-2", "prob": 15.0, "fair": 6.67},   # 4 goals -> over 2.5
        {"score": "3-1", "prob": 12.0, "fair": 8.33},   # 4 goals -> over 2.5
    ],
}


@pytest.fixture
def feed():
    return {"meta": {"generated": "test"}, "fixtures": [FIXTURE]}


def _boost(**kw):
    base = dict(
        site="bet365",
        fixture="Brazil vs Morocco",
        market="Match Result",
        selection="Brazil",
        boosted_odds=2.50,
    )
    base.update(kw)
    return Boost(**base)


# ---------------------------------------------------------------------------
# 1X2 above / below fair.
# ---------------------------------------------------------------------------


def test_1x2_above_fair_is_plus_ev(feed):
    # Brazil model prob 0.50 -> fair 2.00; boosted to 2.50 -> edge = 0.25.
    ev = evaluate_boost(_boost(boosted_odds=2.50), feed)
    assert ev.priceable is True
    assert ev.model_prob == pytest.approx(0.50)
    assert ev.fair_odds == pytest.approx(2.00)
    assert ev.edge == pytest.approx(2.50 * 0.50 - 1.0)
    assert ev.edge > 0
    assert ev.is_plus_ev is True


def test_1x2_below_fair_not_plus_ev(feed):
    # Same selection at 1.80 < fair 2.00 -> edge = 1.80*0.50 - 1 = -0.10.
    ev = evaluate_boost(_boost(boosted_odds=1.80), feed)
    assert ev.priceable is True
    assert ev.edge == pytest.approx(-0.10)
    assert ev.is_plus_ev is False


def test_1x2_resolves_away_team_and_draw(feed):
    away = evaluate_boost(_boost(selection="Morocco", boosted_odds=5.0), feed)
    assert away.priceable is True
    assert away.model_prob == pytest.approx(0.25)  # away side
    assert away.is_plus_ev is True  # 5.0 * 0.25 - 1 = 0.25

    draw = evaluate_boost(
        _boost(market="Match Result", selection="Draw", boosted_odds=4.5), feed
    )
    assert draw.priceable is True
    assert draw.model_prob == pytest.approx(0.25)  # draw side
    assert draw.fair_odds == pytest.approx(4.0)
    assert draw.is_plus_ev is True  # 4.5 * 0.25 - 1 = 0.125 > 0

    # At exactly fair (edge == 0) the boost is NOT flagged +EV (MIN_EDGE = 0,
    # the verdict is strictly `edge > MIN_EDGE`).
    at_fair = evaluate_boost(
        _boost(market="Match Result", selection="Draw", boosted_odds=4.0), feed
    )
    assert at_fair.priceable is True
    assert at_fair.edge == pytest.approx(0.0)
    assert at_fair.is_plus_ev is False


def test_reversed_fixture_order_still_matches(feed):
    # Boost written "Morocco vs Brazil" must still find the feed fixture and
    # resolve Brazil to the home side.
    ev = evaluate_boost(
        _boost(fixture="Morocco vs Brazil", selection="Brazil", boosted_odds=2.5),
        feed,
    )
    assert ev.priceable is True
    assert ev.model_prob == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# In-play / unsupported / missing-fixture -> unpriceable.
# ---------------------------------------------------------------------------


def test_inplay_flag_not_priced(feed):
    ev = evaluate_boost(_boost(is_inplay=True), feed)
    assert ev.priceable is False
    assert ev.is_plus_ev is False
    assert "live" in ev.reason.lower() or "in-play" in ev.reason.lower()


def test_inplay_text_not_priced(feed):
    ev = evaluate_boost(_boost(market="Match Result (In-Play)"), feed)
    assert ev.priceable is False
    assert "live" in ev.reason.lower() or "in-play" in ev.reason.lower()


def test_player_prop_not_priced(feed):
    ev = evaluate_boost(
        _boost(market="Anytime Goalscorer", selection="Vinicius Jr"), feed
    )
    assert ev.priceable is False
    assert "player prop" in ev.reason.lower()


def test_unknown_market_not_priced(feed):
    ev = evaluate_boost(
        _boost(market="Total Corners", selection="Over 9.5"), feed
    )
    assert ev.priceable is False


def test_fixture_not_in_feed(feed):
    ev = evaluate_boost(_boost(fixture="Argentina vs France"), feed)
    assert ev.priceable is False
    assert "fixture" in ev.reason.lower()


# ---------------------------------------------------------------------------
# BTTS + Over/Under unit handling (the load-bearing ÷100).
# ---------------------------------------------------------------------------


def test_btts_yes_maps_to_btts_over_100(feed):
    # btts 55.0 (percent) -> 0.55 probability. Boosted 2.0 -> edge 0.10.
    ev = evaluate_boost(
        _boost(market="Both Teams To Score", selection="Yes", boosted_odds=2.0),
        feed,
    )
    assert ev.priceable is True
    assert ev.model_prob == pytest.approx(0.55)
    assert ev.fair_odds == pytest.approx(1.0 / 0.55)
    assert ev.edge == pytest.approx(2.0 * 0.55 - 1.0)


def test_btts_no_is_complement(feed):
    ev = evaluate_boost(
        _boost(market="BTTS", selection="No", boosted_odds=2.5), feed
    )
    assert ev.priceable is True
    assert ev.model_prob == pytest.approx(1.0 - 0.55)  # 0.45


def test_over_under_primary_line_units(feed):
    # over 60.0 (percent) -> 0.60. Boosted 1.80 -> edge 0.08.
    ev = evaluate_boost(
        _boost(market="Over 2.5 Goals", selection="Over", boosted_odds=1.80),
        feed,
    )
    assert ev.priceable is True
    assert ev.model_prob == pytest.approx(0.60)
    assert ev.edge == pytest.approx(1.80 * 0.60 - 1.0)

    under = evaluate_boost(
        _boost(market="Under 2.5 Goals", selection="Under", boosted_odds=2.6),
        feed,
    )
    assert under.priceable is True
    assert under.model_prob == pytest.approx(0.40)


def test_over_under_nonprimary_line_from_grid(feed):
    # Line 1.5 not the primary 2.5 -> re-derive from the grid.
    #   over 1.5 = scores with >=2 goals: 1-1(25)+2-1(20)+2-2(15)+3-1(12) = 72%.
    ev = evaluate_boost(
        _boost(market="Over 1.5 Goals", selection="Over", boosted_odds=1.5),
        feed,
    )
    assert ev.priceable is True
    assert ev.model_prob == pytest.approx(0.72)


# ---------------------------------------------------------------------------
# Correct score.
# ---------------------------------------------------------------------------


def test_correct_score_in_grid(feed):
    # "2-1" -> 20.0% -> 0.20. Boosted 6.0 -> edge 0.20.
    ev = evaluate_boost(
        _boost(market="Correct Score", selection="2-1", boosted_odds=6.0), feed
    )
    assert ev.priceable is True
    assert ev.model_prob == pytest.approx(0.20)
    assert ev.is_plus_ev is True


def test_correct_score_outside_grid(feed):
    ev = evaluate_boost(
        _boost(market="Correct Score", selection="4-4", boosted_odds=50.0), feed
    )
    assert ev.priceable is False
    assert "grid" in ev.reason.lower()


# ---------------------------------------------------------------------------
# Feed loading tolerance + contract sanity.
# ---------------------------------------------------------------------------


def test_load_scores_feed_missing_file_returns_empty():
    assert load_scores_feed("/no/such/path/scores_data.json") == {}


def test_empty_feed_is_unpriceable():
    ev = evaluate_boost(_boost(), {})
    assert ev.priceable is False


def test_min_edge_is_zero():
    assert MIN_EDGE == 0.0


def test_boosteval_is_dataclass_shape(feed):
    ev = evaluate_boost(_boost(), feed)
    assert isinstance(ev, BoostEval)
    assert hasattr(ev, "model_prob")
    assert hasattr(ev, "fair_odds")
    assert hasattr(ev, "edge")
    assert hasattr(ev, "is_plus_ev")
    assert hasattr(ev, "priceable")
    assert hasattr(ev, "reason")
    # No NaNs leak into a priced verdict.
    assert ev.edge is None or not math.isnan(ev.edge)
