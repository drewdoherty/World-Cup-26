"""Tests for the socio-economic structural prior (wca.models.structural)."""

from __future__ import annotations

import math
import statistics

import numpy as np
import pytest

from wca.advancement import WC2026_GROUPS
from wca.models import structural as S
from wca.models.dixon_coles import DixonColesModel


# ---------------------------------------------------------------------------
# Data table.
# ---------------------------------------------------------------------------


def test_factor_table_covers_every_2026_team():
    factors = S.load_country_factors()
    teams = {t for g in WC2026_GROUPS.values() for t in g}
    missing = teams - set(factors)
    assert not missing, "structural table missing 2026 teams: %s" % sorted(missing)


def test_factor_table_values_are_sane():
    for f in S.load_country_factors().values():
        assert f.population_m > 0
        assert f.gdp_per_capita_usd > 0
        assert 0.0 <= f.football_culture <= 1.0
        assert f.home_altitude_m >= 0.0
        assert f.confederation in S.CONFEDERATION_OFFSET


# ---------------------------------------------------------------------------
# Strength index.
# ---------------------------------------------------------------------------


def test_strength_index_is_mean_zero_unit_variance():
    strength = S.strength_index(S.load_country_factors())
    vals = list(strength.values())
    assert math.isclose(statistics.mean(vals), 0.0, abs_tol=1e-9)
    assert math.isclose(statistics.pstdev(vals), 1.0, rel_tol=1e-6)


def test_strength_index_empty():
    assert S.strength_index({}) == {}


def test_gdp_term_is_inverted_u_peaking_at_the_peak():
    # The GDP term must be maximised at GDP_PEAK_USD and fall off either side.
    def term(gdp):
        f = S.CountryFactors("x", "UEFA", 10.0, gdp, 0.8, 0.0)
        return S._gdp_term(f)

    peak = term(S.GDP_PEAK_USD)
    assert peak == pytest.approx(0.0)
    assert term(S.GDP_PEAK_USD / 4) < peak  # too poor
    assert term(S.GDP_PEAK_USD * 4) < peak  # past diminishing returns


def test_population_term_requires_football_culture():
    # Two equally populous nations: the one with no football culture scores 0.
    big_culture = S.CountryFactors("a", "UEFA", 100.0, 40000, 1.0, 0.0)
    big_noculture = S.CountryFactors("b", "UEFA", 100.0, 40000, 0.0, 0.0)
    assert S._population_term(big_culture) > 0
    assert S._population_term(big_noculture) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Dixon-Coles priors.
# ---------------------------------------------------------------------------


def test_build_dc_priors_are_mean_zero_and_scaled():
    strength = S.strength_index(S.load_country_factors())
    atk, dfc = S.build_dc_priors(strength, scale=0.2)
    assert atk == dfc  # same sign and magnitude by construction
    assert math.isclose(statistics.mean(atk.values()), 0.0, abs_tol=1e-9)
    # Scale controls magnitude: doubling scale doubles every prior.
    atk2, _ = S.build_dc_priors(strength, scale=0.4)
    for t in atk:
        assert atk2[t] == pytest.approx(2 * atk[t])


def test_dc_priors_from_factors_roundtrips_team_set():
    atk, dfc = S.dc_priors_from_factors()
    assert set(atk) == set(S.load_country_factors())


# ---------------------------------------------------------------------------
# Structural prior wired into Dixon-Coles.
# ---------------------------------------------------------------------------


def _toy_matches(seed=0, n=200):
    rng = np.random.default_rng(seed)
    teams = ["A", "B", "C", "D"]
    H, Aw, HG, AG = [], [], [], []
    for _ in range(n):
        i, j = rng.choice(4, 2, replace=False)
        H.append(teams[i])
        Aw.append(teams[j])
        HG.append(int(rng.poisson(1.4)))
        AG.append(int(rng.poisson(1.1)))
    return teams, H, Aw, HG, AG


def test_dc_default_off_is_identical_to_classic():
    teams, H, Aw, HG, AG = _toy_matches()
    m1 = DixonColesModel(reg_lambda=0.05).fit(H, Aw, HG, AG)
    m2 = DixonColesModel(reg_lambda=0.05, attack_prior=None, defence_prior=None).fit(
        H, Aw, HG, AG
    )
    for t in teams:
        assert m1.attack[t] == pytest.approx(m2.attack[t], abs=1e-9)
        assert m1.defence[t] == pytest.approx(m2.defence[t], abs=1e-9)


def test_dc_structural_prior_pulls_toward_target():
    teams, H, Aw, HG, AG = _toy_matches()
    base = DixonColesModel(reg_lambda=0.5).fit(H, Aw, HG, AG)
    prior = {"A": 1.0, "B": -1.0, "C": 0.0, "D": 0.0}
    pulled = DixonColesModel(
        reg_lambda=0.5, attack_prior=prior, defence_prior=prior
    ).fit(H, Aw, HG, AG)
    # A's prior is strongly positive -> attack should rise relative to baseline;
    # B's prior is strongly negative -> attack should fall.
    assert pulled.attack["A"] > base.attack["A"]
    assert pulled.attack["B"] < base.attack["B"]


def test_dc_prior_centered_mean_zero_and_serialised():
    teams, H, Aw, HG, AG = _toy_matches()
    prior = {"A": 2.0, "B": 0.0, "C": 0.0, "D": 0.0}  # non-mean-zero on purpose
    m = DixonColesModel(reg_lambda=0.3, attack_prior=prior, defence_prior=prior).fit(
        H, Aw, HG, AG
    )
    centered = [m._attack_prior_c[t] for t in teams]
    assert math.isclose(sum(centered), 0.0, abs_tol=1e-9)
    # Round-trips through to_dict / from_dict.
    m2 = DixonColesModel.from_dict(m.to_dict())
    assert m2.attack_prior == m.attack_prior
    for t in teams:
        assert m2._attack_prior_c[t] == pytest.approx(m._attack_prior_c[t])


# ---------------------------------------------------------------------------
# Divergence flag (P3).
# ---------------------------------------------------------------------------


def test_structural_outright_probs_sum_to_one():
    strength = S.strength_index(S.load_country_factors())
    probs = S.structural_outright_probs(strength)
    assert math.isclose(sum(probs.values()), 1.0, rel_tol=1e-9)
    assert all(p > 0 for p in probs.values())


def test_outright_divergence_flags_and_sorts():
    strength = {"Strong": 3.0, "Mid": 0.0, "Weak": -3.0}
    # Model thinks they are all equal -> structural disagrees on the extremes.
    model = {"Strong": 1 / 3, "Mid": 1 / 3, "Weak": 1 / 3}
    div = S.outright_divergence(strength, model, min_log_ratio=0.2)
    assert div, "expected divergences"
    # Sorted by absolute log-ratio, descending.
    mags = [abs(d.log_ratio) for d in div]
    assert mags == sorted(mags, reverse=True)
    # The threshold filters small disagreements.
    none = S.outright_divergence(strength, model, min_log_ratio=10.0)
    assert none == []
