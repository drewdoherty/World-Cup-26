"""propcorr: independence recovery, uplift signs, marginal consistency."""
from __future__ import annotations

import numpy as np
import pytest

from wca.models import propcorr


def _matrix(lam_h=1.6, lam_a=1.1, n=8):
    from math import exp, factorial
    ph = np.array([exp(-lam_h) * lam_h ** k / factorial(k) for k in range(n)])
    pa = np.array([exp(-lam_a) * lam_a ** k / factorial(k) for k in range(n)])
    m = np.outer(ph, pa)
    return m / m.sum()


def test_beta_zero_recovers_independence():
    m = _matrix()
    for result in propcorr.RESULTS:
        combo = propcorr.result_prop_combo(
            m, True, result, rate_p90=1.2, expected_minutes=90.0,
            threshold=1, beta=0.0)
        assert combo["joint_prob"] == pytest.approx(combo["naive_product"], abs=1e-12)
        assert combo["corr_uplift"] == pytest.approx(1.0, abs=1e-9)


def test_uplift_positive_for_win_negative_for_loss():
    m = _matrix()
    win = propcorr.result_prop_combo(m, True, "win", 1.2, 90.0, 1)
    loss = propcorr.result_prop_combo(m, True, "loss", 1.2, 90.0, 1)
    assert win["corr_uplift"] > 1.0
    assert loss["corr_uplift"] < 1.0


def test_joints_sum_to_marginal():
    m = _matrix()
    parts = [propcorr.joint_result_prop_prob(m, True, r, 1.2, 90.0, 1)
             for r in propcorr.RESULTS]
    marginal = propcorr.prop_marginal_prob(m, True, 1.2, 90.0, 1)
    assert sum(parts) == pytest.approx(marginal, abs=1e-12)


def test_away_orientation_flips_axis():
    m = _matrix(lam_h=2.2, lam_a=0.8)
    # the AWAY team is weak: its win-joint must be smaller than the home team's
    home_win = propcorr.joint_result_prop_prob(m, True, "win", 1.0, 90.0, 1)
    away_win = propcorr.joint_result_prop_prob(m, False, "win", 1.0, 90.0, 1)
    assert away_win < home_win


def test_threshold_monotone_and_minutes_scale():
    m = _matrix()
    p1 = propcorr.joint_result_prop_prob(m, True, "win", 1.2, 90.0, 1)
    p2 = propcorr.joint_result_prop_prob(m, True, "win", 1.2, 90.0, 2)
    p1_sub = propcorr.joint_result_prop_prob(m, True, "win", 1.2, 60.0, 1)
    assert p2 < p1
    assert p1_sub < p1


def test_advancement_bounds_and_settlement_flag():
    m = _matrix()
    only_win = propcorr.advancement_prop_combo(m, True, 1.2, 90.0, 1,
                                               p_win_given_level=0.0)
    with_pens = propcorr.advancement_prop_combo(m, True, 1.2, 90.0, 1,
                                                p_win_given_level=0.5)
    win = propcorr.result_prop_combo(m, True, "win", 1.2, 90.0, 1)
    assert only_win["joint_prob"] == pytest.approx(win["joint_prob"], abs=1e-12)
    assert with_pens["joint_prob"] > only_win["joint_prob"]
    assert "NOT modelled" in with_pens["settlement"]
