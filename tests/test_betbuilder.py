"""Tests for the same-game bet-builder engine (:mod:`wca.betbuilder`)."""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from wca.betbuilder import (
    EVS,
    BetBuilder,
    Leg,
    build_bet_builder,
    calibrate_lambdas,
    enumerate_legs,
    find_fixture,
    format_bet_builder,
    independent_poisson_matrix,
    joint_prob,
    matrix_from_feed_entry,
    parse_fixture,
)
from wca.models.scores import btts_from_matrix, implied_1x2, over_under_from_matrix


# A self-contained fixture entry shaped like one scores_data.json record.
# Brazil (away) heavy favourites, like the real Scotland vs Brazil match.
SBR_ENTRY = {
    "fixture": "Scotland vs Brazil",
    "over_under": {"line": 2.5, "over": 48.2, "under": 51.8},
    "btts": 37.9,
    "model_1x2": {"home": 0.098969, "draw": 0.184295, "away": 0.716736},
}


# --------------------------------------------------------------------------- #
# Parsing / lookup helpers.
# --------------------------------------------------------------------------- #


def test_parse_fixture_variants():
    assert parse_fixture("Scotland vs Brazil") == ("Scotland", "Brazil")
    assert parse_fixture("Scotland v Brazil") == ("Scotland", "Brazil")
    assert parse_fixture("Bosnia and Herzegovina vs Qatar") == (
        "Bosnia and Herzegovina",
        "Qatar",
    )


def test_parse_fixture_rejects_garbage():
    with pytest.raises(ValueError):
        parse_fixture("not a fixture")


def test_find_fixture_loose_and_order_insensitive():
    fixtures = [SBR_ENTRY, {"fixture": "Morocco vs Haiti"}]
    assert find_fixture(fixtures, "Scotland vs Brazil") is SBR_ENTRY
    assert find_fixture(fixtures, "scotland brazil") is SBR_ENTRY
    assert find_fixture(fixtures, "brazil scotland") is SBR_ENTRY  # order-insensitive
    assert find_fixture(fixtures, "Spain vs Italy") is None


# --------------------------------------------------------------------------- #
# Matrix reconstruction + calibration.
# --------------------------------------------------------------------------- #


def test_independent_poisson_matrix_normalised():
    m = independent_poisson_matrix(1.2, 1.6, max_goals=10)
    assert m.shape == (11, 11)
    assert m.min() >= 0.0
    assert abs(m.sum() - 1.0) < 1e-9


def test_matrix_reconciles_1x2_exactly():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    p = fm.one_x_two
    target = (0.098969, 0.184295, 0.716736)
    assert np.allclose(p, target, atol=1e-6)
    assert abs(sum(p) - 1.0) < 1e-9


def test_calibration_reproduces_ou_and_btts():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    over = over_under_from_matrix(fm.matrix, 2.5)[0]
    btts = btts_from_matrix(fm.matrix)
    # Calibrated within ~1 percentage point of the published aggregates.
    assert abs(over - 0.482) < 0.01
    assert abs(btts - 0.379) < 0.01
    # Brazil (away) much stronger -> away goal mean dominates.
    assert fm.lambda_away > fm.lambda_home


def test_calibrate_lambdas_underdetermined_falls_back():
    # No O/U or BTTS target: should still return a usable, finite pair.
    lam_h, lam_a = calibrate_lambdas((0.4, 0.25, 0.35))
    assert lam_h > 0 and lam_a > 0
    assert np.isfinite(lam_h) and np.isfinite(lam_a)


def test_calibrate_accepts_fraction_or_percent():
    # Targets given as percentages (48.2) and fractions (0.482) agree.
    a = calibrate_lambdas((0.1, 0.18, 0.72), ou_line=2.5, ou_over=48.2, btts_yes=37.9)
    b = calibrate_lambdas((0.1, 0.18, 0.72), ou_line=2.5, ou_over=0.482, btts_yes=0.379)
    assert np.allclose(a, b)


# --------------------------------------------------------------------------- #
# Leg catalog.
# --------------------------------------------------------------------------- #


def test_enumerate_legs_probabilities_match_matrix():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    legs = enumerate_legs(fm)
    assert legs
    for leg in legs:
        assert 0.0 <= leg.prob <= 1.0
        assert abs(float(fm.matrix[leg.mask].sum()) - leg.prob) < 1e-12


def test_match_result_legs_sum_to_one():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    legs = {lg.selection: lg for lg in enumerate_legs(fm) if lg.market == "Match Result"}
    total = sum(lg.prob for lg in legs.values())
    assert abs(total - 1.0) < 1e-9


def test_enumerate_legs_dedupes_identical_regions():
    # Draw No Bet: Brazil shares its region with Match Result: Brazil to win,
    # so only one survives — no two legs may have identical masks.
    fm = matrix_from_feed_entry(SBR_ENTRY)
    legs = enumerate_legs(fm)
    seen = set()
    for leg in legs:
        key = leg.mask.tobytes()
        assert key not in seen, "duplicate leg region for %s" % leg.label
        seen.add(key)


# --------------------------------------------------------------------------- #
# Joint pricing / correlation.
# --------------------------------------------------------------------------- #


def test_joint_prob_captures_correlation():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    legs = {lg.label: lg for lg in enumerate_legs(fm)}
    win = legs["Match Result: Brazil to win"]
    btts_no = legs["Both Teams To Score: No"]
    jp = joint_prob(fm.matrix, [win, btts_no])
    indep = win.prob * btts_no.prob
    # Brazil winning while Scotland fails to score are positively correlated:
    # the joint exceeds the independent product.
    assert jp > indep
    assert 0.0 < jp <= min(win.prob, btts_no.prob)


def test_mutually_exclusive_legs_have_zero_joint():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    legs = {lg.label: lg for lg in enumerate_legs(fm)}
    # Scotland to score 1+ AND Brazil to win to nil (Scotland 0) is impossible.
    scot_scores = legs["Scotland Total Goals: Over 0.5"]
    brazil_wtn = legs["Brazil Win to Nil: Yes"]
    assert joint_prob(fm.matrix, [scot_scores, brazil_wtn]) == 0.0


def test_betbuilder_naive_and_correlation_ratio():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    legs = {lg.label: lg for lg in enumerate_legs(fm)}
    combo = [legs["Match Result: Brazil to win"], legs["Both Teams To Score: No"]]
    bb = BetBuilder(legs=combo, joint_prob=joint_prob(fm.matrix, combo))
    # Positive correlation -> fair price shorter than the naive multiply.
    assert bb.fair_odds < bb.naive_odds
    assert bb.correlation_ratio > 1.0
    assert abs(bb.naive_odds - (1 / combo[0].prob) * (1 / combo[1].prob)) < 1e-9


# --------------------------------------------------------------------------- #
# Builder search.
# --------------------------------------------------------------------------- #


def test_build_respects_min_odds_floor():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    builders = build_bet_builder(fm, min_odds=EVS)
    assert builders
    for b in builders:
        assert b.fair_odds >= EVS - 1e-9


def test_build_results_sorted_most_likely_first():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    builders = build_bet_builder(fm, min_odds=EVS, top_n=5)
    probs = [b.joint_prob for b in builders]
    assert probs == sorted(probs, reverse=True)
    # Most likely qualifying builder sits just above the floor.
    assert builders[0].fair_odds >= EVS - 1e-9


def test_build_one_leg_per_family():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    for b in build_bet_builder(fm, min_odds=EVS, max_legs=4):
        families = [lg.family for lg in b.legs]
        assert len(families) == len(set(families))


def test_build_no_redundant_legs():
    # Dropping any leg must change the joint probability (minimality).
    fm = matrix_from_feed_entry(SBR_ENTRY)
    for b in build_bet_builder(fm, min_odds=EVS, max_legs=4):
        for i in range(len(b.legs)):
            rest = [b.legs[j] for j in range(len(b.legs)) if j != i]
            assert abs(joint_prob(fm.matrix, rest) - b.joint_prob) > 1e-12


def test_build_respects_leg_count_bounds():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    builders = build_bet_builder(fm, min_odds=EVS, min_legs=3, max_legs=3, top_n=10)
    assert builders
    for b in builders:
        assert len(b.legs) == 3


def test_build_must_include_anchor():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    builders = build_bet_builder(fm, min_odds=EVS, must_include=["Brazil to win"])
    assert builders
    for b in builders:
        assert any("brazil to win" in lg.label.lower() for lg in b.legs)


def test_build_high_floor_can_return_empty():
    # An astronomically high floor yields no builder from these markets.
    fm = matrix_from_feed_entry(SBR_ENTRY)
    builders = build_bet_builder(fm, min_odds=1e9, max_legs=4)
    assert builders == []


# --------------------------------------------------------------------------- #
# Formatting + end-to-end on the real feed.
# --------------------------------------------------------------------------- #


def test_format_contains_key_fields():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    builders = build_bet_builder(fm, min_odds=EVS)
    text = format_bet_builder(fm, builders, min_odds=EVS)
    assert "Scotland vs Brazil" in text
    assert "Builder:" in text
    assert "Naive multiply" in text


def test_format_empty_builders():
    fm = matrix_from_feed_entry(SBR_ENTRY)
    text = format_bet_builder(fm, [], min_odds=EVS)
    assert "No same-game builder" in text


def test_end_to_end_real_feed_if_present():
    path = "site/scores_data.json"
    if not os.path.exists(path):
        pytest.skip("scores feed not present")
    with open(path, encoding="utf-8") as fh:
        fixtures = json.load(fh).get("fixtures", [])
    entry = find_fixture(fixtures, "Scotland vs Brazil")
    if entry is None:
        pytest.skip("Scotland vs Brazil not in feed")
    fm = matrix_from_feed_entry(entry)
    builders = build_bet_builder(fm, min_odds=EVS)
    assert builders
    assert builders[0].fair_odds >= EVS - 1e-9
