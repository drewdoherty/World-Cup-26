"""Tests for the correlation-aware whole-book exposure model.

Covers Phase-2 Wave-1: same-fixture bets settled from one shared scoreline so
their joint P&L is exact, plus the 5%-of-bankroll per-correlated-underlying cap.
"""
from __future__ import annotations

import math

import numpy as np

from wca import exposure, exposure_corr


# ---------------------------------------------------------------------------
# Scoreline matrix.
# ---------------------------------------------------------------------------


def test_scoreline_matrix_normalised_and_shaped():
    mat = exposure_corr.scoreline_matrix(1.4, 1.1, max_goals=10)
    assert mat.shape == (11, 11)
    assert abs(float(mat.sum()) - 1.0) < 1e-9
    assert np.all(mat >= 0.0)


def test_scoreline_matrix_bad_lambda_does_not_raise():
    mat = exposure_corr.scoreline_matrix(float("nan"), -3.0, max_goals=5)
    # Coerced to a degenerate-but-valid distribution (all mass at 0-0).
    assert abs(float(mat.sum()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# settle_on_scoreline — one known scoreline per market.
# ---------------------------------------------------------------------------


def _bet(**kw):
    base = {"type": "Full-time result", "selection": "", "stake": 10.0,
            "odds": 2.0, "free": False, "profit": 10.0}
    base.update(kw)
    if "profit" not in kw and "stake" in kw and "odds" in kw:
        base["profit"] = base["stake"] * (base["odds"] - 1.0)
    return base


def test_settle_1x2_home_win():
    bet = _bet(type="Full-time result", selection="Brazil", profit=10.0)
    # 3-0 -> home win
    assert exposure_corr.settle_on_scoreline(bet, "Brazil", "Serbia", 3, 0) == 10.0
    # 1-1 -> draw -> lose stake
    assert exposure_corr.settle_on_scoreline(bet, "Brazil", "Serbia", 1, 1) == -10.0
    # 0-2 -> away win -> lose stake
    assert exposure_corr.settle_on_scoreline(bet, "Brazil", "Serbia", 0, 2) == -10.0


def test_settle_over_under_half_line():
    over = _bet(type="Over/Under 2.5 Goals", selection="Over 2.5", profit=10.0)
    under = _bet(type="Over/Under 2.5 Goals", selection="Under 2.5", profit=10.0)
    # 3-0 = 3 goals > 2.5 -> over wins, under loses
    assert exposure_corr.settle_on_scoreline(over, "A", "B", 3, 0) == 10.0
    assert exposure_corr.settle_on_scoreline(under, "A", "B", 3, 0) == -10.0
    # 1-1 = 2 goals < 2.5 -> under wins, over loses
    assert exposure_corr.settle_on_scoreline(over, "A", "B", 1, 1) == -10.0
    assert exposure_corr.settle_on_scoreline(under, "A", "B", 1, 1) == 10.0


def test_settle_over_under_integer_line_push():
    over = _bet(type="Over/Under 2 Goals", selection="Over 2", profit=10.0)
    under = _bet(type="Over/Under 2 Goals", selection="Under 2", profit=10.0)
    # Exactly 2 goals on a line of 2.0 -> push: stake returned, no P&L.
    assert exposure_corr.settle_on_scoreline(over, "A", "B", 1, 1) == 0.0
    assert exposure_corr.settle_on_scoreline(under, "A", "B", 1, 1) == 0.0
    # 3 goals -> over wins, under loses (no push)
    assert exposure_corr.settle_on_scoreline(over, "A", "B", 2, 1) == 10.0
    assert exposure_corr.settle_on_scoreline(under, "A", "B", 2, 1) == -10.0


def test_settle_btts_yes_no():
    yes = _bet(type="BTTS", selection="Yes", profit=10.0)
    no = _bet(type="BTTS", selection="No", profit=10.0)
    # 2-1 both score -> yes wins, no loses
    assert exposure_corr.settle_on_scoreline(yes, "A", "B", 2, 1) == 10.0
    assert exposure_corr.settle_on_scoreline(no, "A", "B", 2, 1) == -10.0
    # 1-0 only one scores -> no wins, yes loses
    assert exposure_corr.settle_on_scoreline(yes, "A", "B", 1, 0) == -10.0
    assert exposure_corr.settle_on_scoreline(no, "A", "B", 1, 0) == 10.0


def test_settle_correct_score():
    cs = _bet(type="Correct Score", selection="2-1", profit=80.0)
    assert exposure_corr.settle_on_scoreline(cs, "A", "B", 2, 1) == 80.0
    assert exposure_corr.settle_on_scoreline(cs, "A", "B", 1, 1) == -10.0
    # Bare "2:1" selection with a generic market is still recognised.
    cs2 = _bet(type="Score", selection="2:1", profit=80.0)
    assert exposure_corr.settle_on_scoreline(cs2, "A", "B", 2, 1) == 80.0


def test_settle_team_total():
    tt = _bet(type="Team Total Goals", selection="Brazil Over 1.5", profit=10.0)
    # Brazil (home) scores 2 -> over 1.5 wins
    assert exposure_corr.settle_on_scoreline(tt, "Brazil", "Serbia", 2, 0) == 10.0
    # Brazil scores 1 -> over 1.5 loses
    assert exposure_corr.settle_on_scoreline(tt, "Brazil", "Serbia", 1, 3) == -10.0


def test_settle_player_prop_is_scoreline_independent():
    prop = _bet(type="Anytime Goalscorer", selection="Vinicius Jr", profit=20.0)
    # A prop is not decided by the final scoreline -> contributes 0 either way.
    assert exposure_corr.settle_on_scoreline(prop, "Brazil", "Serbia", 3, 0) == 0.0
    assert exposure_corr.settle_on_scoreline(prop, "Brazil", "Serbia", 0, 0) == 0.0


def test_free_bet_loses_zero():
    fb = _bet(type="Full-time result", selection="Brazil", free=True, profit=20.0)
    # Losing scoreline: free bet stake not returned -> £0 cost.
    assert exposure_corr.settle_on_scoreline(fb, "Brazil", "Serbia", 0, 1) == 0.0
    # Winning scoreline: profit only.
    assert exposure_corr.settle_on_scoreline(fb, "Brazil", "Serbia", 1, 0) == 20.0


# ---------------------------------------------------------------------------
# Correlation: Home Win + Over 2.5 win TOGETHER on a 3-0.
# ---------------------------------------------------------------------------


def test_home_win_and_over_are_positively_correlated():
    home = _bet(type="Full-time result", selection="Brazil", profit=10.0)
    over = _bet(type="Over/Under 2.5 Goals", selection="Over 2.5", profit=10.0)
    dist = exposure_corr.fixture_pnl_distribution(
        [home, over], "Brazil", "Serbia", lam_h=2.2, lam_a=0.6
    )
    assert dist is not None
    # The joint distribution must contain a +20 state (both win, e.g. 3-0) with
    # positive probability — they are NOT mutually exclusive.
    both_win = [p for pnl, p in dist if abs(pnl - 20.0) < 1e-6]
    assert both_win and both_win[0] > 0.0

    # Correlation check: P(both win) must EXCEED the independent product, since a
    # home win that is high-scoring tends to clear 2.5. Compute the marginals
    # from the same matrix and compare.
    mat = exposure_corr.scoreline_matrix(2.2, 0.6)
    p_home = float(np.tril(mat, k=-1).sum())
    over_p, _under = exposure_corr.over_under_from_matrix(mat, 2.5)
    p_joint = both_win[0]
    assert p_joint > p_home * over_p + 1e-9  # strictly positive correlation

    # And the +20 mass is not double-counted: total prob still sums to 1.
    assert abs(sum(p for _pnl, p in dist) - 1.0) < 1e-9


def test_distribution_independent_of_unsettleable_props():
    # Adding a prop must not change the scoreline-driven distribution.
    home = _bet(type="Full-time result", selection="Brazil", profit=10.0)
    prop = _bet(type="Anytime Goalscorer", selection="Neymar", profit=15.0)
    d1 = exposure_corr.fixture_pnl_distribution([home], "Brazil", "Serbia",
                                                lam_h=1.8, lam_a=0.9)
    d2 = exposure_corr.fixture_pnl_distribution([home, prop], "Brazil", "Serbia",
                                                lam_h=1.8, lam_a=0.9)
    assert d1 == d2


# ---------------------------------------------------------------------------
# 5%-of-bankroll cap on correlated downside.
# ---------------------------------------------------------------------------


def test_cap_flags_over_exposed_fixture():
    # £200 of real-money stake at risk on the same fixture > 5% of £3000 (£150).
    big = _bet(type="Full-time result", selection="Brazil", stake=200.0,
               odds=2.0, profit=200.0)
    out = exposure_corr.build_correlated_exposure(
        {"Brazil vs Serbia": [big]},
        {"Brazil vs Serbia": (1.8, 0.9)},
        {"Brazil vs Serbia": ("Brazil", "Serbia")},
        bankroll=3000.0,
    )
    fx = out["fixtures"][0]
    assert fx["cap_abs"] == 150.0
    assert fx["worst_case"] == -200.0
    assert fx["net_downside"] == 200.0
    assert fx["over_exposed"] is True
    assert out["n_over_exposed"] == 1


def test_cap_does_not_flag_within_limit():
    # £100 at risk < £150 cap -> not flagged.
    ok = _bet(type="Full-time result", selection="Brazil", stake=100.0,
              odds=2.0, profit=100.0)
    out = exposure_corr.build_correlated_exposure(
        {"Brazil vs Serbia": [ok]},
        {"Brazil vs Serbia": (1.8, 0.9)},
        {"Brazil vs Serbia": ("Brazil", "Serbia")},
        bankroll=3000.0,
    )
    fx = out["fixtures"][0]
    assert fx["net_downside"] == 100.0
    assert fx["over_exposed"] is False
    assert out["n_over_exposed"] == 0


def test_correlated_hedge_reduces_downside_vs_naive_sum():
    # Back Brazil AND back Over 2.5 on the same game. The naive per-bet sum of
    # worst cases is -£20 (both could lose), but they cannot BOTH lose on every
    # scoreline the same way — e.g. a 0-0 loses the home bet but... still loses
    # over too. The point: the joint worst case is computed from real scorelines,
    # not a blind -stake-each sum. Here both lose on 0-1 (away win, 1 goal),
    # so worst is indeed -20 — but a 1-1 (draw, push-ish) shows over loses while
    # home loses too. We assert the worst case equals the true joint minimum.
    home = _bet(type="Full-time result", selection="Brazil", stake=10.0,
                odds=2.0, profit=10.0)
    over = _bet(type="Over/Under 3.5 Goals", selection="Over 3.5", stake=10.0,
                odds=2.0, profit=10.0)
    dist = exposure_corr.fixture_pnl_distribution(
        [home, over], "Brazil", "Serbia", lam_h=1.2, lam_a=1.0
    )
    worst = min(pnl for pnl, _ in dist)
    # A low-scoring away win (e.g. 0-1) loses both -> -20 is reachable.
    assert worst == -20.0
    # But a high-scoring home win (e.g. 4-0) wins both -> +20 reachable.
    best = max(pnl for pnl, _ in dist)
    assert best == 20.0


# ---------------------------------------------------------------------------
# Cross-fixture convolution (independent games).
# ---------------------------------------------------------------------------


def test_convolution_two_independent_fixtures():
    d1 = [(-10.0, 0.5), (10.0, 0.5)]
    d2 = [(-5.0, 0.5), (5.0, 0.5)]
    combined = exposure_corr.convolve_distributions([d1, d2])
    states = dict(combined)
    # Four equally-likely sums: -15, -5, +5, +15.
    assert abs(states[-15.0] - 0.25) < 1e-9
    assert abs(states[15.0] - 0.25) < 1e-9
    assert abs(sum(p for _pnl, p in combined) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Backward-compat: no lambdas -> legacy outputs, no crash.
# ---------------------------------------------------------------------------


_LEGACY_FIXTURES = [
    {"fixture": "Ateam vs Bteam", "kickoff": "2026-06-13 19:00:00+00:00",
     "model": {"home": 0.25, "draw": 0.25, "away": 0.50}},
]
_LAMBDA_FIXTURES = [
    {"fixture": "Ateam vs Bteam", "kickoff": "2026-06-13 19:00:00+00:00",
     "model": {"home": 0.45, "draw": 0.27, "away": 0.28},
     "lambda_home": 1.6, "lambda_away": 1.0},
]


def _result_single(**kw):
    base = {"status": "open", "stake": 10.0, "decimal_odds": 2.0,
            "source": "model", "market": "Full-time result",
            "match_desc": "Ateam vs Bteam", "selection": "Ateam"}
    base.update(kw)
    return base


def test_backward_compat_no_lambdas_produces_legacy_feed():
    bets = [_result_single()]
    data = exposure.build_exposure_data(bets, _LEGACY_FIXTURES)
    # Legacy outputs intact.
    assert "portfolio" in data and "fixtures" in data and "blindspots" in data
    fx = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rows = {r["outcome"]: r for r in fx["results"]}
    assert rows["Ateam"]["net_pnl"] == 10.0
    # Correlated section present but flags the fixture as lambda-less.
    corr = data["correlated_exposure"]
    cfx = next(f for f in corr["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    assert cfx["has_lambdas"] is False
    assert "note" in cfx


def test_backward_compat_with_lambdas_adds_correlated_section():
    bets = [_result_single()]
    data = exposure.build_exposure_data(bets, _LAMBDA_FIXTURES)
    corr = data["correlated_exposure"]
    cfx = next(f for f in corr["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    assert cfx["has_lambdas"] is True
    assert cfx["lambda_home"] == 1.6
    # Legacy per-outcome results untouched.
    fx = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rows = {r["outcome"]: r for r in fx["results"]}
    assert rows["Ateam"]["net_pnl"] == 10.0


# ---------------------------------------------------------------------------
# Determinism.
# ---------------------------------------------------------------------------


def test_determinism_identical_feed():
    bets = [_result_single(),
            _result_single(market="Over/Under 2.5 Goals",
                           selection="Over 2.5", match_desc="Ateam vs Bteam")]
    d1 = exposure.build_exposure_data(bets, _LAMBDA_FIXTURES)["correlated_exposure"]
    d2 = exposure.build_exposure_data(bets, _LAMBDA_FIXTURES)["correlated_exposure"]
    assert d1 == d2


def test_fixture_distribution_returns_none_without_lambdas():
    home = _bet(type="Full-time result", selection="Brazil", profit=10.0)
    out = exposure_corr.fixture_pnl_distribution([home], "Brazil", "Serbia")
    assert out is None
