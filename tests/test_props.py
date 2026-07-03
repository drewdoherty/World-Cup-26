"""Tests for prop-market models (corners, cards, anytime scorer)."""

import math

import pytest
from scipy.stats import nbinom

from wca.models.props import (
    AnytimeScorerModel,
    CardsModel,
    CornersModel,
    FoulsModel,
    ShotsOnTargetModel,
)


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
    # 90-min-only refit (2026-07-03): 3.29 cards/match (2nd yellow = 1 red);
    # the legacy 3.41 was ET-contaminated.
    assert m.mean_total() == pytest.approx(3.29)
    assert m.mean_total(1.2, 1.1, 1.15) == pytest.approx(3.29 * 1.2 * 1.1 * 1.15)


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


# ---------------------------------------------------------------------------
# A3-A5 additive, fallback-guarded extensions
# ---------------------------------------------------------------------------

# A small prop_priors-shaped dict (load_priors() output shape).
_PRIORS = {
    "GLOBAL": {
        "corners": {"mean": 4.484, "dispersion_k": 157.5, "n_matches": 0,
                    "shrinkage_weight": 0.0},
        "sot": {"mean": 4.16, "dispersion_k": 11.0, "n_matches": 0,
                "shrinkage_weight": 0.0},
        "fouls": {"mean": 14.262, "dispersion_k": 20.4, "n_matches": 0,
                  "shrinkage_weight": 0.0},
    },
    "Brazil": {
        "corners": {"mean": 6.0, "dispersion_k": 9.5, "n_matches": 7,
                    "shrinkage_weight": 0.6},
        "sot": {"mean": 6.5, "dispersion_k": 7.0, "n_matches": 7,
                "shrinkage_weight": 0.6},
        "fouls": {"mean": 11.0, "dispersion_k": 20.4, "n_matches": 7,
                  "shrinkage_weight": 0.6},
    },
    "Iran": {
        "corners": {"mean": 3.0, "dispersion_k": 9.5, "n_matches": 5,
                    "shrinkage_weight": 0.5},
        "fouls": {"mean": 18.0, "dispersion_k": 20.4, "n_matches": 5,
                  "shrinkage_weight": 0.5},
    },
}


# --- Fallback-identity: priors absent / names omitted -> bit-identical -------


def test_corners_fallback_identity_no_priors():
    """team_priors=None reproduces the legacy CornersModel exactly."""
    base = CornersModel()
    same = CornersModel(team_priors=None)
    for lh, la in [(1.0, 1.0), (1.8, 0.9), (2.1, 1.3), (0.3, 2.4)]:
        assert same.mean_total(lh, la) == base.mean_total(lh, la)
        for line in (8.5, 9.5, 10.5):
            assert same.prob_over(line, lh, la) == base.prob_over(line, lh, la)
            assert same.prob_team_over(line, lh, la) == base.prob_team_over(line, lh, la)


def test_corners_empty_priors_equiv_default():
    """CornersModel(team_priors={}) ≡ CornersModel() on a λ grid."""
    base = CornersModel()
    empty = CornersModel(team_priors={})
    for lh, la in [(1.0, 1.0), (1.8, 0.9), (2.1, 1.3)]:
        assert empty.mean_total(lh, la, "X", "Y") == base.mean_total(lh, la)
        assert empty.prob_team_over(9.5, lh, la, "X", "Y") == base.prob_team_over(9.5, lh, la)


def test_corners_names_omitted_legacy_path_with_priors():
    """Even with priors injected, omitting names keeps the legacy 8.97 path."""
    legacy = CornersModel()
    withp = CornersModel(team_priors=_PRIORS)
    for lh, la in [(1.0, 1.0), (1.8, 0.9)]:
        assert withp.mean_total(lh, la) == legacy.mean_total(lh, la)
        assert withp.team_mean(lh, la) == legacy.team_mean(lh, la)


def test_corners_team_dispersion_default_is_total_k():
    """team_dispersion=None -> team O/U uses the total k (legacy)."""
    base = CornersModel()
    assert base._team_k() == base.dispersion
    widened = CornersModel(team_dispersion=9.5)
    # Smaller k widens the tail -> higher P(over) far above the mean.
    p_total_k = base.prob_team_over(7.5, 1.4, 1.1)
    p_team_k = widened.prob_team_over(7.5, 1.4, 1.1)
    assert p_team_k > p_total_k


# --- Corners team-prior path activates with names + priors ------------------


def test_corners_team_prior_vs_fallback():
    m = CornersModel(team_priors=_PRIORS)
    # Brazil (6.0 for) vs Iran (3.0 for): EB means sum, xG-nudged.
    mu = m.mean_total(1.4, 1.0, "Brazil", "Iran")
    rel = (1.4 + 1.0) / m.base_goals - 1.0
    assert mu == pytest.approx((6.0 + 3.0) * (1.0 + m.elasticity * rel))
    # Unknown team -> GLOBAL/league fallback (4.484), never raises.
    mu_unknown = m.mean_total(1.4, 1.0, "Atlantis", "Iran")
    assert mu_unknown == pytest.approx((4.484 + 3.0) * (1.0 + m.elasticity * rel))


def test_corners_team_mean_prior_path():
    m = CornersModel(team_priors=_PRIORS)
    rel = (1.4 + 1.0) / m.base_goals - 1.0
    assert m.team_mean(1.4, 1.0, "Brazil", "Iran") == pytest.approx(
        6.0 * (1.0 + m.elasticity * rel))


def test_corners_validation_new_args():
    with pytest.raises(ValueError):
        CornersModel(team_dispersion=0.0)
    with pytest.raises(ValueError):
        CornersModel(league_team_mean=-1.0)


# --- Cards fallback identity + opt-in --------------------------------------


def test_cards_fallback_identity():
    base = CardsModel()
    same = CardsModel(ref_factor=1.0)
    assert same.mean_total() == base.mean_total() == pytest.approx(3.29)
    for line in (2.5, 3.5, 4.5):
        assert same.prob_over(line) == base.prob_over(line)
    # New ref_factor kwarg defaults to no-op.
    assert base.mean_total(ref_factor=None) == pytest.approx(3.29)


def test_cards_aggression_from_fouls():
    m = CardsModel()
    # foul rate at league mean -> aggression 1.0 (no-op).
    assert m.aggression_from_fouls(m.league_foul_mean) == pytest.approx(1.0)
    # missing / non-positive -> fallback 1.0.
    assert m.aggression_from_fouls(None) == 1.0
    assert m.aggression_from_fouls(0.0) == 1.0
    # higher fouls -> aggression > 1 (sub-linear via beta).
    high = m.aggression_from_fouls(2 * m.league_foul_mean)
    assert 1.0 < high < 2.0
    assert high == pytest.approx(2.0 ** m.foul_beta)


def test_cards_ref_factor_scales_mean():
    m = CardsModel()
    assert m.mean_total(ref_factor=1.2) == pytest.approx(3.29 * 1.2)
    assert m.prob_over(3.5, ref_factor=1.3) > m.prob_over(3.5)


def test_cards_validation_new_args():
    with pytest.raises(ValueError):
        CardsModel(ref_factor=-0.1)
    with pytest.raises(ValueError):
        CardsModel(league_foul_mean=0.0)
    with pytest.raises(ValueError):
        CardsModel().mean_total(ref_factor=-0.5)


# --- ShotsOnTargetModel (A3, NEW) ------------------------------------------


def test_sot_team_mean_constant_path():
    m = ShotsOnTargetModel()
    # at base_lambda the mean == base_shots * on_target_ratio
    assert m.team_mean(m.base_lambda) == pytest.approx(
        m.base_shots * m.on_target_ratio)


def test_sot_team_mean_monotone_in_lambda():
    m = ShotsOnTargetModel()
    assert m.team_mean(2.0) > m.team_mean(1.35) > m.team_mean(0.7)


def test_sot_probs_bounded_and_monotone():
    m = ShotsOnTargetModel()
    for line in (2.5, 3.5, 4.5):
        p = m.prob_team_over(line, 1.4)
        assert 0.0 <= p <= 1.0
    probs = [m.prob_team_over(line, 1.4) for line in (2.5, 3.5, 4.5, 5.5)]
    assert all(a > b for a, b in zip(probs, probs[1:]))


def test_sot_nb_approaches_poisson():
    from scipy.stats import poisson

    m = ShotsOnTargetModel(dispersion=1e7)
    mu = m.team_mean(1.35)
    assert m.prob_team_over(3.5, 1.35) == pytest.approx(poisson(mu).sf(3), rel=1e-4)


def test_sot_player_thinning():
    m = ShotsOnTargetModel()
    team = m.team_mean(1.4)
    # full share, full minutes -> player mean == team mean
    assert m.player_mean(1.4, 1.0, 90.0) == pytest.approx(team)
    # half minutes halves it
    assert m.player_mean(1.4, 1.0, 45.0) == pytest.approx(team * 0.5)
    p = m.prob_player_over(0.5, 1.4, 0.3)
    assert 0.0 <= p <= 1.0


def test_sot_prior_path_uses_injected_mean():
    m = ShotsOnTargetModel(team_priors=_PRIORS)
    # Brazil sot prior 6.5 overrides the shots*ratio construction.
    assert m.team_mean(1.0, "Brazil") == pytest.approx(6.5)
    # unknown team -> GLOBAL sot 4.16
    assert m.team_mean(1.0, "Atlantis") == pytest.approx(4.16)
    # no team name but priors injected -> GLOBAL sot prior (4.16), not constant
    assert m.team_mean(m.base_lambda) == pytest.approx(4.16)
    # with NO priors injected, the constant shots*ratio path is used
    nop = ShotsOnTargetModel()
    assert nop.team_mean(nop.base_lambda) == pytest.approx(
        nop.base_shots * nop.on_target_ratio)


def test_sot_validation():
    with pytest.raises(ValueError):
        ShotsOnTargetModel(on_target_ratio=0.0)
    with pytest.raises(ValueError):
        ShotsOnTargetModel(on_target_ratio=1.5)
    with pytest.raises(ValueError):
        ShotsOnTargetModel(base_shots=0.0)
    with pytest.raises(ValueError):
        ShotsOnTargetModel(elasticity=1.5)
    with pytest.raises(ValueError):
        ShotsOnTargetModel().player_mean(1.4, 1.5)


# --- FoulsModel (A4, NEW) --------------------------------------------------


def test_fouls_fallback_to_league_mean():
    m = FoulsModel()
    # no priors -> every team gets the league mean
    assert m.team_mean() == pytest.approx(14.262)
    assert m.team_mean("Brazil") == pytest.approx(14.262)


def test_fouls_prior_path():
    m = FoulsModel(team_priors=_PRIORS)
    assert m.team_mean("Brazil") == pytest.approx(11.0)
    assert m.team_mean("Iran") == pytest.approx(18.0)
    # unknown -> GLOBAL fouls 14.262
    assert m.team_mean("Atlantis") == pytest.approx(14.262)


def test_fouls_probs_and_player_thinning():
    m = FoulsModel()
    probs = [m.prob_team_over(line) for line in (12.5, 14.5, 16.5)]
    assert all(0.0 <= p <= 1.0 for p in probs)
    assert all(a > b for a, b in zip(probs, probs[1:]))
    assert m.player_mean(1.0, expected_minutes=90.0) == pytest.approx(m.team_mean())
    assert m.player_mean(1.0, expected_minutes=45.0) == pytest.approx(m.team_mean() * 0.5)


def test_fouls_nb_approaches_poisson():
    from scipy.stats import poisson

    m = FoulsModel(dispersion=1e7)
    mu = m.team_mean()
    assert m.prob_team_over(14.5, ) == pytest.approx(poisson(mu).sf(14), rel=1e-4)


def test_fouls_validation():
    with pytest.raises(ValueError):
        FoulsModel(base_fouls=0.0)
    with pytest.raises(ValueError):
        FoulsModel(dispersion=0.0)
    with pytest.raises(ValueError):
        FoulsModel().player_mean(1.5)


# --- Fouls -> Cards integration (the r=0.508 coupling, opt-in) --------------


def test_fouls_feed_cards_aggression():
    fm = FoulsModel(team_priors=_PRIORS)
    cm = CardsModel()
    # Iran fouls more (18 > 14.262) -> aggression > 1; Brazil less -> < 1.
    agg_iran = cm.aggression_from_fouls(fm.team_mean("Iran"))
    agg_brazil = cm.aggression_from_fouls(fm.team_mean("Brazil"))
    assert agg_iran > 1.0 > agg_brazil
    # a high-foul matchup prices more cards than the base.
    mu_matchup = cm.mean_total(agg_iran, agg_iran)
    assert mu_matchup > cm.mean_total()
