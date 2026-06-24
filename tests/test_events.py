"""Tests for wca.models.events — the event-distribution pipeline."""
import math

import pytest

from wca.models.events import (
    BASE_FOULS_PER_MATCH,
    GOAL_TIMING,
    SUB_TIMING,
    TIMING_BUCKETS,
    card_risk,
    corner_count_dist,
    goal_timing_pdf,
    match_event_distributions,
    substitution_timing,
)


# ---------------------------------------------------------------------------
# Provenance constants
# ---------------------------------------------------------------------------

def test_timing_shapes_normalise():
    # Both empirical shapes should very nearly sum to 1.
    assert math.isclose(sum(GOAL_TIMING.values()), 1.0, abs_tol=1e-3)
    assert math.isclose(sum(SUB_TIMING.values()), 1.0, abs_tol=1e-3)
    assert set(GOAL_TIMING) == set(TIMING_BUCKETS)


# ---------------------------------------------------------------------------
# Goal timing
# ---------------------------------------------------------------------------

def test_goal_timing_expected_sums_to_lambda():
    gt = goal_timing_pdf(2.6)
    assert math.isclose(sum(gt.expected_goals.values()), 2.6, rel_tol=1e-9)
    # P(any goal) consistent with Poisson(lambda_total).
    assert math.isclose(gt.p_any_goal, 1 - math.exp(-2.6))


def test_goal_timing_first_split_sums_to_p_any():
    gt = goal_timing_pdf(2.6)
    assert math.isclose(sum(gt.p_first.values()), gt.p_any_goal, rel_tol=1e-9)


def test_goal_timing_before_is_monotone():
    gt = goal_timing_pdf(2.6)
    before = [gt.p_goal_before(b) for b in TIMING_BUCKETS]
    assert before == sorted(before)  # non-decreasing
    assert before[0] == 0.0  # nothing before the first bucket


def test_goal_timing_zero_lambda():
    gt = goal_timing_pdf(0.0)
    assert gt.p_any_goal == 0.0
    assert all(v == 0.0 for v in gt.p_first.values())


def test_goal_timing_rejects_negative():
    with pytest.raises(ValueError):
        goal_timing_pdf(-0.1)


# ---------------------------------------------------------------------------
# Corners
# ---------------------------------------------------------------------------

def test_corner_pmf_sums_to_one():
    cd = corner_count_dist(1.6, 1.2, max_count=40)
    assert math.isclose(sum(cd.pmf.values()), 1.0, abs_tol=1e-3)
    # Team means partition the total mean.
    assert math.isclose(cd.mean_home + cd.mean_away, cd.mean_total, rel_tol=1e-9)


def test_corner_prob_over_matches_model():
    cd = corner_count_dist(1.6, 1.2)
    p = cd.prob_over(9.5)
    assert 0.0 < p < 1.0
    over, under = cd.fair_over_under(9.5)
    assert math.isclose(1.0 / over + 1.0 / under, 1.0, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def test_card_aggression_scales_with_fouls():
    base = card_risk(1.4, 1.2)
    assert math.isclose(base.aggression_home, 1.0)  # no fouls -> neutral
    # A team fouling at exactly half the base rate -> aggression 1.0.
    neutral = card_risk(1.4, 1.2, fouls_home=BASE_FOULS_PER_MATCH / 2)
    assert math.isclose(neutral.aggression_home, 1.0)
    # Dirtier team -> higher aggression and a higher mean.
    dirty = card_risk(1.4, 1.2, fouls_home=18.0, fouls_away=18.0)
    assert dirty.aggression_home > 1.0
    assert dirty.mean_total > base.mean_total


def test_card_red_probability():
    # No red info -> tournament base; explicit rates override.
    base = card_risk(1.4, 1.2)
    assert 0.0 < base.p_red < 0.5
    hi = card_risk(1.4, 1.2, reds_home=0.3, reds_away=0.3)
    assert hi.p_red > base.p_red


def test_card_pmf_sums_to_one():
    cr = card_risk(1.4, 1.2, fouls_home=14.0, fouls_away=14.0, max_count=40)
    assert math.isclose(sum(cr.pmf.values()), 1.0, abs_tol=1e-3)


# ---------------------------------------------------------------------------
# Substitutions
# ---------------------------------------------------------------------------

def test_sub_timing_expected_sums():
    st = substitution_timing(10.0)
    assert math.isclose(sum(st.expected_subs.values()), 10.0, rel_tol=1e-9)
    # Most subs come in the 61-90 window empirically.
    late = st.weights["61-75"] + st.weights["76-90"]
    assert late > 0.6


def test_match_event_distributions_bundle():
    med = match_event_distributions(1.6, 1.2, fouls_home=14.0, fouls_away=16.0)
    assert med.goal_timing.lambda_total == pytest.approx(2.8)
    assert med.corners.mean_total > 0
    assert med.cards.mean_total > 0
    assert med.subs.total_subs > 0
