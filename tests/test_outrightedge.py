"""Tests for the outright/advancement edge metrics (wca.outrightedge)."""

from __future__ import annotations

import random

import pytest

from wca import outrightedge as oe


# --------------------------------------------------------------------------- #
# Convergence (leading)
# --------------------------------------------------------------------------- #


def test_convergence_rewards_market_moving_toward_model():
    # model above entry, market drifts up toward it -> converged
    rows = [{"entry_pm": 0.50, "later_pm": 0.58, "model": 0.66} for _ in range(40)]
    out = oe.convergence(rows)
    assert out["convergence_rate"] == 1.0
    assert 0.0 < out["capture_fraction"] <= 1.0
    assert out["n_signal"] == 40 and out["sufficient"] is True


def test_convergence_penalises_market_moving_away():
    rows = [{"entry_pm": 0.50, "later_pm": 0.42, "model": 0.66} for _ in range(40)]
    out = oe.convergence(rows)
    assert out["convergence_rate"] == 0.0
    assert out["capture_fraction"] < 0


def test_convergence_skips_no_signal_markets():
    # model == entry -> no edge to converge to, excluded
    rows = [{"entry_pm": 0.50, "later_pm": 0.55, "model": 0.505}]
    out = oe.convergence(rows, min_signal=0.01)
    assert out["n_signal"] == 0 and out["convergence_rate"] is None


# --------------------------------------------------------------------------- #
# Calibration (lagging)
# --------------------------------------------------------------------------- #


def test_calibration_discriminating_model():
    rng = random.Random(7)
    probs, outs, clusters = [], [], []
    for i in range(200):
        truth = rng.random()
        p = min(.97, max(.03, truth + rng.gauss(0, 0.08)))
        probs.append(p); outs.append(1 if rng.random() < truth else 0); clusters.append(i % 40)
    out = oe.calibration(probs, outs, cluster_ids=clusters)
    assert out["auc"] > 0.6                 # discriminates
    assert out["brier_skill"] > 0           # beats the base rate
    assert out["n"] == 200 and out["n_eff"] <= 40   # cluster-deflated


def test_calibration_empty_safe():
    out = oe.calibration([], [])
    assert out["n"] == 0 and out["auc"] is None and out["sufficient"] is False


# --------------------------------------------------------------------------- #
# Paired skill (lagging)
# --------------------------------------------------------------------------- #


def test_paired_skill_positive_when_model_sharper():
    rng = random.Random(11)
    mp, kp, y = [], [], []
    for _ in range(150):
        truth = rng.random()
        model = min(.97, max(.03, truth + rng.gauss(0, 0.06)))   # sharp
        market = min(.97, max(.03, truth + rng.gauss(0, 0.14)))  # noisier
        mp.append(model); kp.append(market); y.append(1 if rng.random() < truth else 0)
    out = oe.paired_skill(mp, kp, y)
    assert out["brier_skill"] > 0 and out["logloss_diff"] > 0


def test_paired_skill_negative_when_market_sharper():
    rng = random.Random(13)
    mp, kp, y = [], [], []
    for _ in range(150):
        truth = rng.random()
        model = min(.97, max(.03, truth + rng.gauss(0, 0.16)))   # noisy
        market = min(.97, max(.03, truth + rng.gauss(0, 0.05)))  # sharp
        mp.append(model); kp.append(market); y.append(1 if rng.random() < truth else 0)
    out = oe.paired_skill(mp, kp, y)
    assert out["brier_skill"] < 0


# --------------------------------------------------------------------------- #
# Information coefficient
# --------------------------------------------------------------------------- #


def test_ic_positive_when_edge_predicts_outcome():
    rng = random.Random(17)
    edges, outs = [], []
    for _ in range(120):
        truth = rng.random()
        edge = (truth - 0.5) + rng.gauss(0, 0.1)   # edge tracks truth
        edges.append(edge); outs.append(1 if rng.random() < truth else 0)
    out = oe.information_coefficient(edges, outs)
    assert out["ic"] is not None and out["ic"] > 0.1


def test_ic_constant_outcome_none():
    out = oe.information_coefficient([0.1, 0.2, 0.3], [1, 1, 1])
    assert out["ic"] is None and out["sufficient"] is False


def test_determinism():
    rows = [{"entry_pm": 0.5, "later_pm": 0.55, "model": 0.6} for _ in range(35)]
    assert oe.convergence(rows) == oe.convergence(rows)
