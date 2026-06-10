"""Tests for the time-decayed Dixon-Coles model.

Synthetic data is generated from known Poisson parameters so the recovered
attack/defence ordering, decay behaviour, regularisation and the rho=0
reduction to independent Poisson can all be checked against ground truth.
"""

from __future__ import annotations

import json
import math
import warnings

import numpy as np
import pytest

from wca.models.dixon_coles import (
    DEFAULT_XI,
    DixonColesModel,
    ScorelinePrediction,
    dc_tau,
    decay_weights,
    half_life_from_xi,
    xi_from_half_life,
)


# ---------------------------------------------------------------------------
# Synthetic data generation.
# ---------------------------------------------------------------------------


def _make_team_params(n_teams, rng, spread=0.6):
    """Return mean-zero attack/defence dicts for ``n_teams`` synthetic teams."""
    teams = ["T%02d" % i for i in range(n_teams)]
    atk = rng.normal(0.0, spread, size=n_teams)
    dfc = rng.normal(0.0, spread, size=n_teams)
    atk -= atk.mean()
    dfc -= dfc.mean()
    return teams, dict(zip(teams, atk)), dict(zip(teams, dfc))


def _simulate_matches(
    teams,
    attack,
    defence,
    mu,
    gamma,
    n_matches,
    rng,
    rho=0.0,
    neutral_frac=0.0,
    days_ago=None,
):
    """Simulate matches from the (independent-Poisson) Dixon-Coles means.

    ``rho`` is ignored for the goal draw (we draw independent Poissons) so this
    reflects the rho=0 generative model; it is sufficient for parameter-recovery
    tests of attack/defence/mu/gamma.
    """
    homes, aways, hg, ag, neut = [], [], [], [], []
    for _ in range(n_matches):
        i, j = rng.choice(len(teams), size=2, replace=False)
        h, a = teams[i], teams[j]
        is_neut = rng.random() < neutral_frac
        g = 0.0 if is_neut else gamma
        lam_h = math.exp(mu + attack[h] - defence[a] + g)
        lam_a = math.exp(mu + attack[a] - defence[h])
        homes.append(h)
        aways.append(a)
        hg.append(int(rng.poisson(lam_h)))
        ag.append(int(rng.poisson(lam_a)))
        neut.append(is_neut)
    out = {
        "home_teams": homes,
        "away_teams": aways,
        "home_goals": np.array(hg),
        "away_goals": np.array(ag),
        "neutral": np.array(neut),
    }
    if days_ago is not None:
        out["days_ago"] = np.asarray(days_ago)
    return out


# ---------------------------------------------------------------------------
# Decay weight math.
# ---------------------------------------------------------------------------


def test_default_xi_two_year_half_life():
    # Default xi must correspond to a two-year half-life.
    assert DEFAULT_XI == pytest.approx(math.log(2.0) / 2.0)
    assert half_life_from_xi(DEFAULT_XI) == pytest.approx(2.0)
    assert xi_from_half_life(2.0) == pytest.approx(DEFAULT_XI)


def test_decay_weights_halflife():
    xi = xi_from_half_life(2.0)
    # A match two years old has half weight; today's match has full weight.
    w = decay_weights(np.array([0.0, 365.25 * 2, 365.25 * 4]), xi)
    assert w[0] == pytest.approx(1.0)
    assert w[1] == pytest.approx(0.5)
    assert w[2] == pytest.approx(0.25)


def test_decay_weights_zero_xi_uniform():
    w = decay_weights(np.array([0.0, 100.0, 5000.0]), 0.0)
    assert np.allclose(w, 1.0)


def test_xi_from_half_life_validation():
    with pytest.raises(ValueError):
        xi_from_half_life(0.0)
    with pytest.raises(ValueError):
        xi_from_half_life(-1.0)


# ---------------------------------------------------------------------------
# tau correction.
# ---------------------------------------------------------------------------


def test_dc_tau_only_low_scores():
    lh = np.full((4, 4), 1.3)
    la = np.full((4, 4), 1.1)
    xx = np.arange(4)[:, None]
    yy = np.arange(4)[None, :]
    rho = 0.05
    tau = dc_tau(np.broadcast_to(xx, (4, 4)), np.broadcast_to(yy, (4, 4)), lh, la, rho)
    # Anything outside the 2x2 low-score block is exactly 1.
    for x in range(4):
        for y in range(4):
            if x <= 1 and y <= 1:
                continue
            assert tau[x, y] == pytest.approx(1.0)
    # The four corrected cells match the Dixon-Coles formulae.
    assert tau[0, 0] == pytest.approx(1.0 - 1.3 * 1.1 * rho)
    assert tau[0, 1] == pytest.approx(1.0 + 1.3 * rho)
    assert tau[1, 0] == pytest.approx(1.0 + 1.1 * rho)
    assert tau[1, 1] == pytest.approx(1.0 - rho)


def test_dc_tau_zero_rho_is_identity():
    lh = np.full((3, 3), 1.5)
    la = np.full((3, 3), 1.2)
    xx = np.broadcast_to(np.arange(3)[:, None], (3, 3))
    yy = np.broadcast_to(np.arange(3)[None, :], (3, 3))
    tau = dc_tau(xx, yy, lh, la, 0.0)
    assert np.allclose(tau, 1.0)


# ---------------------------------------------------------------------------
# Fitting: parameter recovery.
# ---------------------------------------------------------------------------


def test_attack_ordering_recovered():
    rng = np.random.default_rng(42)
    teams, attack, defence = _make_team_params(12, rng, spread=0.7)
    data = _simulate_matches(teams, attack, defence, mu=0.2, gamma=0.3,
                             n_matches=6000, rng=rng)
    model = DixonColesModel(xi=0.0, reg_lambda=1e-4, min_matches=1)
    model.fit(
        data["home_teams"], data["away_teams"],
        data["home_goals"], data["away_goals"],
        neutral=data["neutral"],
    )
    # Rank correlation between true and fitted attack should be high.
    true_atk = np.array([attack[t] for t in teams])
    fit_atk = np.array([model.attack[t] for t in teams])
    # Spearman via rank ordering.
    true_rank = np.argsort(np.argsort(true_atk))
    fit_rank = np.argsort(np.argsort(fit_atk))
    corr = np.corrcoef(true_rank, fit_rank)[0, 1]
    assert corr > 0.85

    # The strongest-attack and weakest-attack teams should be identified.
    assert teams[int(np.argmax(fit_atk))] == teams[int(np.argmax(true_atk))]
    assert teams[int(np.argmin(fit_atk))] == teams[int(np.argmin(true_atk))]


def test_home_advantage_positive():
    rng = np.random.default_rng(7)
    teams, attack, defence = _make_team_params(10, rng, spread=0.4)
    data = _simulate_matches(teams, attack, defence, mu=0.1, gamma=0.35,
                             n_matches=5000, rng=rng)
    model = DixonColesModel(xi=0.0, reg_lambda=1e-3, min_matches=1)
    model.fit(
        data["home_teams"], data["away_teams"],
        data["home_goals"], data["away_goals"],
        neutral=data["neutral"],
    )
    # Recovered home advantage should be clearly positive and near truth.
    assert model.home_advantage > 0.15
    assert model.home_advantage == pytest.approx(0.35, abs=0.2)


def test_identifiability_mean_zero():
    rng = np.random.default_rng(11)
    teams, attack, defence = _make_team_params(8, rng)
    data = _simulate_matches(teams, attack, defence, mu=0.2, gamma=0.25,
                             n_matches=3000, rng=rng)
    model = DixonColesModel(xi=0.0, reg_lambda=1e-3, min_matches=1)
    model.fit(
        data["home_teams"], data["away_teams"],
        data["home_goals"], data["away_goals"],
        neutral=data["neutral"],
    )
    assert np.mean(list(model.attack.values())) == pytest.approx(0.0, abs=1e-8)
    assert np.mean(list(model.defence.values())) == pytest.approx(0.0, abs=1e-8)


# ---------------------------------------------------------------------------
# Score matrix sanity.
# ---------------------------------------------------------------------------


def test_score_matrix_sums_to_one():
    rng = np.random.default_rng(3)
    teams, attack, defence = _make_team_params(6, rng)
    data = _simulate_matches(teams, attack, defence, mu=0.2, gamma=0.3,
                             n_matches=2000, rng=rng)
    model = DixonColesModel(xi=0.0, reg_lambda=1e-3, min_matches=1)
    model.fit(
        data["home_teams"], data["away_teams"],
        data["home_goals"], data["away_goals"],
        neutral=data["neutral"],
    )
    pred = model.predict(teams[0], teams[1])
    assert pred.total_probability() == pytest.approx(1.0, abs=1e-9)
    o = pred.outcome_probs()
    assert o["home"] + o["draw"] + o["away"] == pytest.approx(1.0)


def test_derived_markets_consistency():
    model = DixonColesModel(xi=0.0)
    # Hand-set a simple two-team fit.
    model.teams = ["A", "B"]
    model._team_index = {"A": 0, "B": 1}
    model.attack = {"A": 0.2, "B": -0.2}
    model.defence = {"A": 0.1, "B": -0.1}
    model.mu = 0.2
    model.home_advantage = 0.3
    model.rho = 0.0
    model.fitted = True

    pred = model.predict("A", "B")
    o = pred.outcome_probs()
    assert sum(o.values()) == pytest.approx(1.0)

    ou = pred.over_under(2.5)
    assert ou["over"] + ou["under"] + ou["push"] == pytest.approx(1.0)
    assert ou["push"] == pytest.approx(0.0)  # half-line never pushes

    # Integer line can push.
    ou2 = pred.over_under(2.0)
    assert ou2["push"] > 0.0
    assert ou2["over"] + ou2["under"] + ou2["push"] == pytest.approx(1.0)

    btts = pred.both_teams_to_score()
    assert btts["yes"] + btts["no"] == pytest.approx(1.0)

    tcs = pred.top_correct_scores(5)
    assert len(tcs) == 5
    # Sorted descending and probabilities present.
    probs = [p for _, p in tcs]
    assert probs == sorted(probs, reverse=True)
    assert max(probs) <= 1.0

    eh, ea = pred.expected_goals()
    assert eh > 0 and ea > 0
    # Home favoured here -> more expected goals than away.
    assert eh > ea


def test_expected_goals_match_poisson_when_rho_zero():
    model = DixonColesModel(xi=0.0)
    model.teams = ["A", "B"]
    model._team_index = {"A": 0, "B": 1}
    model.attack = {"A": 0.3, "B": -0.3}
    model.defence = {"A": 0.0, "B": 0.0}
    model.mu = 0.3
    model.home_advantage = 0.2
    model.rho = 0.0
    model.fitted = True
    pred = model.predict("A", "B", max_goals=20)
    eh, ea = pred.expected_goals()
    # With rho=0 and a wide truncation the matrix means equal the Poisson means.
    assert eh == pytest.approx(pred.lambda_home, abs=1e-4)
    assert ea == pytest.approx(pred.lambda_away, abs=1e-4)


# ---------------------------------------------------------------------------
# rho = 0 reduces to independent Poisson.
# ---------------------------------------------------------------------------


def test_rho_zero_is_independent_poisson():
    from scipy.stats import poisson

    model = DixonColesModel(xi=0.0)
    model.teams = ["A", "B"]
    model._team_index = {"A": 0, "B": 1}
    model.attack = {"A": 0.1, "B": -0.1}
    model.defence = {"A": 0.05, "B": -0.05}
    model.mu = 0.25
    model.home_advantage = 0.3
    model.rho = 0.0
    model.fitted = True

    mat, lam_h, lam_a = model.score_matrix("A", "B", max_goals=15)
    goals = np.arange(16)
    indep = np.outer(poisson.pmf(goals, lam_h), poisson.pmf(goals, lam_a))
    indep = indep / indep.sum()
    assert np.allclose(mat, indep, atol=1e-9)


def test_nonzero_rho_changes_low_scores_only():
    model = DixonColesModel(xi=0.0)
    model.teams = ["A", "B"]
    model._team_index = {"A": 0, "B": 1}
    model.attack = {"A": 0.0, "B": 0.0}
    model.defence = {"A": 0.0, "B": 0.0}
    model.mu = 0.3
    model.home_advantage = 0.2
    model.rho = 0.0
    model.fitted = True
    mat0, _, _ = model.score_matrix("A", "B", max_goals=10)
    model.rho = 0.1
    mat1, _, _ = model.score_matrix("A", "B", max_goals=10)
    # The 2x2 low-score block changes; high scores barely move (renormalisation
    # only). The biggest absolute change lives in the low-score block.
    diff = np.abs(mat1 - mat0)
    block_change = diff[:2, :2].sum()
    outside_change = diff.sum() - block_change
    assert block_change > outside_change


# ---------------------------------------------------------------------------
# Time decay downweights old matches.
# ---------------------------------------------------------------------------


def test_decay_moves_toward_recent_era():
    rng = np.random.default_rng(2024)
    teams, _, _ = _make_team_params(8, rng)

    # Old era: team T00 is the strongest attacker.
    old_atk = {t: 0.0 for t in teams}
    old_atk[teams[0]] = 1.0
    old_def = {t: 0.0 for t in teams}

    # Recent era: team T00 is the WEAKEST attacker, T07 strongest.
    new_atk = {t: 0.0 for t in teams}
    new_atk[teams[0]] = -1.0
    new_atk[teams[7]] = 1.0
    new_def = {t: 0.0 for t in teams}

    old = _simulate_matches(teams, old_atk, old_def, mu=0.2, gamma=0.2,
                            n_matches=3000, rng=rng,
                            days_ago=rng.uniform(365.25 * 6, 365.25 * 8, 3000))
    new = _simulate_matches(teams, new_atk, new_def, mu=0.2, gamma=0.2,
                            n_matches=3000, rng=rng,
                            days_ago=rng.uniform(0, 365.25 * 1, 3000))

    home_teams = list(old["home_teams"]) + list(new["home_teams"])
    away_teams = list(old["away_teams"]) + list(new["away_teams"])
    hg = np.concatenate([old["home_goals"], new["home_goals"]])
    ag = np.concatenate([old["away_goals"], new["away_goals"]])
    days = np.concatenate([old["days_ago"], new["days_ago"]])

    # No decay: averages the two eras -> T00 attack roughly neutral.
    m_flat = DixonColesModel(xi=0.0, reg_lambda=1e-3, min_matches=1)
    m_flat.fit(home_teams, away_teams, hg, ag, days_ago=days)

    # Strong decay (half-life 1 year): recent era dominates -> T00 weak.
    m_decay = DixonColesModel(half_life_years=1.0, reg_lambda=1e-3, min_matches=1)
    m_decay.fit(home_teams, away_teams, hg, ag, days_ago=days)

    # Under decay, T00's attack should be substantially lower (more negative)
    # than under the flat fit, since it reflects the recent (weak) era.
    assert m_decay.attack[teams[0]] < m_flat.attack[teams[0]] - 0.2
    # And recent strong team should rank above T00 under decay.
    assert m_decay.attack[teams[7]] > m_decay.attack[teams[0]]


# ---------------------------------------------------------------------------
# Regularisation shrinks low-data teams.
# ---------------------------------------------------------------------------


def test_regularization_shrinks_low_data_teams():
    rng = np.random.default_rng(99)
    # Core teams with many matches.
    teams, attack, defence = _make_team_params(8, rng, spread=0.5)
    data = _simulate_matches(teams, attack, defence, mu=0.2, gamma=0.25,
                             n_matches=4000, rng=rng)
    homes = list(data["home_teams"])
    aways = list(data["away_teams"])
    hg = list(data["home_goals"])
    ag = list(data["away_goals"])

    # A minnow that played exactly two matches and got thrashed (extreme record
    # that, unregularised, would imply a huge negative attack).
    minnow = "MINNOW"
    for opp in (teams[0], teams[1]):
        homes.append(opp)
        aways.append(minnow)
        hg.append(7)
        ag.append(0)

    hg = np.array(hg)
    ag = np.array(ag)

    weak = DixonColesModel(reg_lambda=1e-4, min_matches=5,
                           low_data_reg_multiplier=1.0, xi=0.0)
    weak.fit(homes, aways, hg, ag)

    strong = DixonColesModel(reg_lambda=0.05, min_matches=5,
                             low_data_reg_multiplier=20.0, xi=0.0)
    strong.fit(homes, aways, hg, ag)

    assert strong.match_counts[minnow] == 2
    # Heavier regularisation pulls the minnow's attack toward the 0 baseline.
    assert abs(strong.attack[minnow]) < abs(weak.attack[minnow])
    # And toward zero specifically (not just smaller magnitude by chance).
    assert strong.attack[minnow] > weak.attack[minnow]


# ---------------------------------------------------------------------------
# Unseen-team fallback.
# ---------------------------------------------------------------------------


def test_unseen_team_falls_back_to_prior_with_warning():
    rng = np.random.default_rng(5)
    teams, attack, defence = _make_team_params(6, rng)
    data = _simulate_matches(teams, attack, defence, mu=0.2, gamma=0.3,
                             n_matches=1500, rng=rng)
    model = DixonColesModel(xi=0.0, reg_lambda=1e-3, min_matches=1)
    model.fit(
        data["home_teams"], data["away_teams"],
        data["home_goals"], data["away_goals"],
    )

    with pytest.warns(RuntimeWarning):
        pred = model.predict("ATLANTIS", teams[0])
    assert pred.total_probability() == pytest.approx(1.0)

    # The unseen team uses the mean-zero prior, so its lambda matches a
    # hypothetical mean-zero opponent.
    lam_h, lam_a = model.expected_lambdas("ATLANTIS", teams[0], warn=False)
    expected_lh = math.exp(model.mu + 0.0 - model.defence[teams[0]] + model.home_advantage)
    assert lam_h == pytest.approx(expected_lh)


def test_both_unseen_teams_neutral():
    model = DixonColesModel(xi=0.0)
    model.teams = ["A"]
    model._team_index = {"A": 0}
    model.attack = {"A": 0.0}
    model.defence = {"A": 0.0}
    model.mu = 0.3
    model.home_advantage = 0.25
    model.rho = 0.05
    model.fitted = True
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pred = model.predict("X", "Y", neutral=True)
    # Two identical prior teams on a neutral venue -> symmetric outcome probs.
    o = pred.outcome_probs()
    assert o["home"] == pytest.approx(o["away"], abs=1e-9)


# ---------------------------------------------------------------------------
# Serialization round trips.
# ---------------------------------------------------------------------------


def test_model_serialization_roundtrip():
    rng = np.random.default_rng(8)
    teams, attack, defence = _make_team_params(5, rng)
    data = _simulate_matches(teams, attack, defence, mu=0.2, gamma=0.3,
                             n_matches=1000, rng=rng)
    model = DixonColesModel(half_life_years=1.5, reg_lambda=0.02,
                            min_matches=3, low_data_reg_multiplier=4.0)
    model.fit(
        data["home_teams"], data["away_teams"],
        data["home_goals"], data["away_goals"],
        neutral=data["neutral"],
    )
    s = model.to_json()
    json.loads(s)  # valid JSON
    restored = DixonColesModel.from_json(s)
    assert restored.xi == pytest.approx(model.xi)
    assert restored.reg_lambda == pytest.approx(0.02)
    assert restored.min_matches == 3
    assert restored.attack == pytest.approx(model.attack)
    assert restored.defence == pytest.approx(model.defence)
    assert restored.mu == pytest.approx(model.mu)
    assert restored.home_advantage == pytest.approx(model.home_advantage)
    assert restored.rho == pytest.approx(model.rho)
    assert restored.fitted is True
    # Predictions identical after round trip.
    p1 = model.predict(teams[0], teams[1])
    p2 = restored.predict(teams[0], teams[1])
    assert np.allclose(p1.matrix, p2.matrix)


def test_scoreline_prediction_serialization_roundtrip():
    mat = np.full((4, 4), 1.0 / 16.0)
    pred = ScorelinePrediction(mat, "A", "B", 1.3, 1.1)
    d = pred.to_dict()
    json.loads(json.dumps(d))
    restored = ScorelinePrediction.from_dict(d)
    assert restored.home == "A"
    assert restored.away == "B"
    assert restored.lambda_home == pytest.approx(1.3)
    assert np.allclose(restored.matrix, mat)


# ---------------------------------------------------------------------------
# Performance / scale smoke test.
# ---------------------------------------------------------------------------


def test_fit_scales_to_many_matches():
    import time

    rng = np.random.default_rng(2026)
    teams, attack, defence = _make_team_params(200, rng, spread=0.5)
    data = _simulate_matches(teams, attack, defence, mu=0.2, gamma=0.25,
                             n_matches=10000, rng=rng,
                             days_ago=rng.uniform(0, 365.25 * 8, 10000))
    model = DixonColesModel(reg_lambda=0.01, min_matches=5)
    t0 = time.time()
    model.fit(
        data["home_teams"], data["away_teams"],
        data["home_goals"], data["away_goals"],
        days_ago=data["days_ago"],
    )
    elapsed = time.time() - t0
    assert model.fitted
    # Must complete in well under a minute.
    assert elapsed < 50.0
