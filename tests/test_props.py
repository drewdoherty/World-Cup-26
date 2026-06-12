"""Tests for prop-market models (corners, cards, anytime scorer)."""

import math

import pytest
from scipy.stats import nbinom

from wca.models.props import AnytimeScorerModel, CardsModel, CornersModel


# ---------------------------------------------------------------------------
# NB parameterisation
# ---------------------------------------------------------------------------


def _nb_moments(mu, k):
    p = k / (k + mu)
    dist = nbinom(k, p)
    return dist.mean(), dist.var()


@pytest.mark.parametrize("mu,k", [(9.6, 11.0), (3.8, 14.0), (1.2, 5.0)])
def test_nb_mean_variance_parameterisation(mu, k):
    mean, var = _nb_moments(mu, k)
    assert mean == pytest.approx(mu)
    assert var == pytest.approx(mu + mu ** 2 / k)


def test_corners_pmf_sums_to_one():
    m = CornersModel()
    total = sum(m.pmf(n, 1.3, 1.3) for n in range(200))
    assert total == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# CornersModel
# ---------------------------------------------------------------------------


def test_corners_mean_at_base_rates():
    m = CornersModel()
    # combined xG == base_goals -> mean == base_corners (WC18+22 fit: 8.97)
    half = m.base_goals / 2.0
    assert m.mean_total(half, half) == pytest.approx(m.base_corners)


def test_corners_mean_scales_with_goal_expectation():
    # Damped elasticity: mu = base_c * (1 + e * ((lh+la)/base_g - 1)) —
    # monotone in xG but NOT proportional (corners-xG corr is only ~0.15).
    m = CornersModel()
    expected = m.base_corners * (1.0 + m.elasticity * (4.0 / m.base_goals - 1.0))
    assert m.mean_total(2.0, 2.0) == pytest.approx(expected)
    assert m.mean_total(2.0, 1.0) > m.mean_total(1.0, 1.0)
    # damping: doubling xG moves the mean by far less than 2x
    assert m.mean_total(2.0, 2.0) < 1.5 * m.mean_total(1.0, 1.0)


def test_corners_over_under_sum_to_one():
    m = CornersModel()
    for line in (8.5, 9.5, 10.5):
        p_over = m.prob_over(line, 1.4, 1.1)
        # under = pmf mass at <= floor(line)
        p_under = sum(m.pmf(n, 1.4, 1.1) for n in range(int(math.floor(line)) + 1))
        assert p_over + p_under == pytest.approx(1.0, abs=1e-9)


def test_corners_monotone_in_line():
    m = CornersModel()
    probs = [m.prob_over(line, 1.4, 1.1) for line in (8.5, 9.5, 10.5, 11.5)]
    assert all(a > b for a, b in zip(probs, probs[1:]))


def test_corners_fair_odds_consistent():
    m = CornersModel()
    over, under = m.fair_odds_over_under(9.5, 1.4, 1.1)
    assert 1.0 / over + 1.0 / under == pytest.approx(1.0)
    assert over > 1.0 and under > 1.0


def test_corners_team_split_proportional():
    m = CornersModel()
    th = m.team_mean(1.8, 0.9)
    ta = m.team_mean(0.9, 1.8)
    assert th + ta == pytest.approx(m.mean_total(1.8, 0.9))
    assert th == pytest.approx(2.0 * ta)
    # stronger attack -> higher team-over prob
    assert m.prob_team_over(4.5, 1.8, 0.9) > m.prob_team_over(4.5, 0.9, 1.8)


def test_corners_invalid_args():
    with pytest.raises(ValueError):
        CornersModel(base_corners=-1.0)
    with pytest.raises(ValueError):
        CornersModel(dispersion=0.0)
    with pytest.raises(ValueError):
        CornersModel().mean_total(-0.1, 1.0)


# ---------------------------------------------------------------------------
# CardsModel
# ---------------------------------------------------------------------------


def test_cards_default_mean():
    m = CardsModel()
    # WC18+22 fit: 3.41 cards/match (2nd yellow = 1 red)
    assert m.mean_total() == pytest.approx(3.41)
    assert m.mean_total(1.2, 1.1, 1.15) == pytest.approx(3.41 * 1.2 * 1.1 * 1.15)


def test_cards_over_under_sum_to_one():
    m = CardsModel()
    p_over = m.prob_over(3.5)
    p_under = sum(m.pmf(n) for n in range(4))
    assert p_over + p_under == pytest.approx(1.0, abs=1e-9)


def test_cards_monotone_in_line_and_aggression():
    m = CardsModel()
    assert m.prob_over(2.5) > m.prob_over(3.5) > m.prob_over(4.5)
    assert m.prob_over(3.5, 1.3, 1.3) > m.prob_over(3.5)
    assert m.prob_over(3.5, stakes_mult=1.2) > m.prob_over(3.5)


def test_cards_fair_odds():
    over, under = CardsModel().fair_odds_over_under(3.5)
    assert 1.0 / over + 1.0 / under == pytest.approx(1.0)


def test_cards_large_dispersion_approaches_poisson():
    from scipy.stats import poisson

    m = CardsModel(dispersion=1e7)
    assert m.prob_over(3.5) == pytest.approx(poisson(m.base_cards).sf(3), rel=1e-4)


def test_cards_invalid_args():
    with pytest.raises(ValueError):
        CardsModel(base_cards=0.0)
    with pytest.raises(ValueError):
        CardsModel().prob_over(3.5, aggression_home=-0.5)


# ---------------------------------------------------------------------------
# AnytimeScorerModel
# ---------------------------------------------------------------------------


def test_scorer_probability_bounds_and_sanity():
    m = AnytimeScorerModel()
    p = m.prob_anytime(1.5, 0.3)
    assert 0.25 < p < 0.45
    assert 0.0 <= p <= 1.0
    # share applies to NON-penalty team xG: lam = (1.5 - 0.18) * 0.3
    assert p == pytest.approx(1.0 - math.exp(-(1.5 - 0.18) * 0.3))


def test_scorer_zero_share_is_penalty_only():
    m = AnytimeScorerModel(pen_xg=0.18)
    assert m.prob_anytime(1.5, 0.0) == 0.0
    p_pen = m.prob_anytime(1.5, 0.0, penalty_taker=True)
    assert p_pen == pytest.approx(1.0 - math.exp(-0.18))


def test_scorer_monotone_in_share_and_minutes():
    m = AnytimeScorerModel()
    assert m.prob_anytime(1.5, 0.4) > m.prob_anytime(1.5, 0.3)
    assert m.prob_anytime(1.5, 0.3, expected_minutes=90) > m.prob_anytime(
        1.5, 0.3, expected_minutes=60
    )


def test_scorer_penalty_taker_boost():
    m = AnytimeScorerModel()
    assert m.prob_anytime(1.5, 0.3, penalty_taker=True) > m.prob_anytime(1.5, 0.3)


def test_fair_odds_anytime():
    m = AnytimeScorerModel()
    p = m.prob_anytime(1.5, 0.3)
    assert m.fair_odds_anytime(1.5, 0.3) == pytest.approx(1.0 / p)
    assert m.fair_odds_anytime(1.5, 0.0) == float("inf")


def test_first_scorer_less_than_anytime():
    m = AnytimeScorerModel()
    lam_total = 2.6
    p_first = m.prob_first_scorer(1.5, 0.3, lam_total)
    p_any = m.prob_anytime(1.5, 0.3)
    assert 0.0 < p_first < p_any
    assert m.prob_first_scorer(1.5, 0.3, 0.0) == 0.0


def test_scorer_value_errors():
    m = AnytimeScorerModel()
    with pytest.raises(ValueError):
        m.prob_anytime(1.5, -0.1)
    with pytest.raises(ValueError):
        m.prob_anytime(1.5, 1.1)
    with pytest.raises(ValueError):
        m.prob_anytime(-1.0, 0.3)
    with pytest.raises(ValueError):
        m.prob_anytime(1.5, 0.3, expected_minutes=-5)
    with pytest.raises(ValueError):
        AnytimeScorerModel(pen_xg=-0.1)
