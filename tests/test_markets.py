"""Tests for de-vigging (wca.markets.devig) and Kelly staking (wca.markets.kelly).

References for the numeric expectations:

* Multiplicative / power de-vig: Clarke, Kovalchik & Ingram (2017),
  "Adjusting bookmaker's odds to allow for overround".
* Shin (1993) method, Štrumbelj (2014) per-outcome closed form: the defining
  property tested here is that Shin pulls probability away from longshots
  relative to the multiplicative method.
* Kelly criterion: Kelly (1956); f* = (p*(b+1) - 1)/b with b = odds - 1.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from wca.markets import devig
from wca.markets import kelly


# ===========================================================================
# devig: implied probabilities and overround.
# ===========================================================================


def test_implied_probs_and_overround():
    odds = [2.0, 4.0, 4.0]  # fair 1X2 book
    pi = devig.implied_probs(odds)
    assert pi == pytest.approx([0.5, 0.25, 0.25])
    assert devig.booksum(odds) == pytest.approx(1.0)
    assert devig.overround(odds) == pytest.approx(0.0)
    assert devig.margin(odds) == pytest.approx(0.0)


def test_overround_margined_book():
    odds = [1.50, 4.50, 7.00]
    expected_book = 1 / 1.50 + 1 / 4.50 + 1 / 7.00
    assert devig.booksum(odds) == pytest.approx(expected_book)
    assert devig.overround(odds) == pytest.approx(expected_book - 1.0)
    assert devig.overround(odds) > 0.0


@pytest.mark.parametrize("bad", [[2.0], [], [1.0, 2.0], [0.5, 3.0], [2.0, float("inf")]])
def test_implied_probs_rejects_bad_odds(bad):
    with pytest.raises(ValueError):
        devig.implied_probs(bad)


# ===========================================================================
# devig: fair book is a fixed point of all three methods.
# ===========================================================================


@pytest.mark.parametrize(
    "fair_odds_book",
    [
        [2.0, 2.0],               # fair coin
        [2.0, 4.0, 4.0],          # fair 1X2
        [1.25, 5.0],              # fair, lopsided 2-way
        [4.0, 4.0, 4.0, 4.0],     # fair 4-way
        [1.0 / 0.55, 1.0 / 0.30, 1.0 / 0.15],  # arbitrary fair 3-way
    ],
)
@pytest.mark.parametrize("method", devig.METHODS)
def test_fair_book_unchanged(fair_odds_book, method):
    pi = devig.implied_probs(fair_odds_book)
    assert devig.booksum(fair_odds_book) == pytest.approx(1.0)
    out = devig.devig(fair_odds_book, method=method)
    assert out == pytest.approx(pi, abs=1e-9)
    assert out.sum() == pytest.approx(1.0)


# ===========================================================================
# devig: all methods normalize to one and stay in (0, 1).
# ===========================================================================


@pytest.mark.parametrize(
    "odds",
    [
        [1.50, 4.50, 7.00],
        [1.90, 2.10],
        [1.30, 4.00],
        [2.50, 3.40, 2.90],
        [1.33, 5.50, 9.00, 21.0],   # 4-way with a big longshot
    ],
)
@pytest.mark.parametrize("method", devig.METHODS)
def test_methods_sum_to_one(odds, method):
    p = devig.devig(odds, method=method)
    assert p.shape == (len(odds),)
    assert p.sum() == pytest.approx(1.0, abs=1e-9)
    assert np.all(p > 0.0)
    assert np.all(p < 1.0)


def test_devig_unknown_method_raises():
    with pytest.raises(ValueError):
        devig.devig([2.0, 2.0], method="nope")


# ===========================================================================
# devig: Shin's defining favourite/longshot property.
# ===========================================================================


def test_shin_lowers_longshot_vs_multiplicative_3way():
    # Margined 3-way book: a short favourite, a mid, and a longshot.
    odds = [1.50, 4.50, 7.00]
    mult = devig.multiplicative(odds)
    sh = devig.shin(odds)

    # The longshot (largest odds, last outcome) gets LOWER probability under
    # Shin than under multiplicative -- the key favourite/longshot correction.
    assert sh[-1] < mult[-1]
    # And correspondingly the favourite gets a higher probability.
    assert sh[0] > mult[0]
    # Both still sum to one.
    assert sh.sum() == pytest.approx(1.0)
    assert mult.sum() == pytest.approx(1.0)


def test_shin_z_in_unit_interval_and_positive_for_margined_book():
    odds = [1.50, 4.50, 7.00]
    z = devig.shin_z(odds)
    assert 0.0 < z < 1.0
    # Fair book -> z == 0.
    assert devig.shin_z([2.0, 4.0, 4.0]) == pytest.approx(0.0)


def test_shin_longshot_property_two_way():
    # In a margined 2-way book the longer-priced side is the longshot.
    odds = [1.30, 4.00]  # booksum ~ 1.019 (genuine overround)
    assert devig.overround(odds) > 0.0
    mult = devig.multiplicative(odds)
    sh = devig.shin(odds)
    # Outcome index 1 is the longshot (odds 4.00).
    assert sh[1] < mult[1]
    assert sh[0] > mult[0]


# ===========================================================================
# devig: power method reproduces a known relation and brackets correctly.
# ===========================================================================


def test_power_method_solves_exponent_relation():
    # By construction the power-method probabilities are pi_i ** k with sum 1.
    odds = [1.50, 4.50, 7.00]
    pi = devig.implied_probs(odds)
    p = devig.power(odds)
    # Recover k from any outcome and check it reproduces every outcome.
    k = math.log(p[0]) / math.log(pi[0])
    assert k > 1.0  # margined book deflates raw probabilities
    recon = pi ** k
    recon = recon / recon.sum()
    assert recon == pytest.approx(p, abs=1e-9)
    assert (pi ** k).sum() == pytest.approx(1.0, abs=1e-9)


def test_power_like_shin_lowers_longshot_vs_multiplicative():
    # The power method also applies a (milder) favourite/longshot correction.
    odds = [1.50, 4.50, 7.00]
    mult = devig.multiplicative(odds)
    pw = devig.power(odds)
    assert pw[-1] < mult[-1]
    assert pw[0] > mult[0]


# ===========================================================================
# devig: fair_odds inverts probabilities; compare table.
# ===========================================================================


def test_fair_odds_round_trip():
    probs = [0.5, 0.25, 0.25]
    odds = devig.fair_odds(probs)
    assert odds == pytest.approx([2.0, 4.0, 4.0])
    # Round trip back to probabilities.
    assert devig.implied_probs(odds) == pytest.approx(probs)


def test_fair_odds_rejects_nonpositive():
    with pytest.raises(ValueError):
        devig.fair_odds([0.5, 0.0, 0.5])


def test_compare_methods_table():
    odds = [1.50, 4.50, 7.00]
    table = devig.compare_methods(odds, labels=["Home", "Draw", "Away"])
    assert list(table.columns) == ["Home", "Draw", "Away", "sum"]
    assert set(table.index) == {"implied", "multiplicative", "power", "shin"}
    # Every de-vigged row sums to one.
    for method in devig.METHODS:
        assert table.loc[method, ["Home", "Draw", "Away"]].sum() == pytest.approx(1.0)
        assert table.loc[method, "sum"] == pytest.approx(1.0)
    # The implied row sums to the booksum (> 1).
    assert table.loc["implied", "sum"] == pytest.approx(devig.booksum(odds))
    # Default labels when none supplied.
    default = devig.compare_methods(odds)
    assert list(default.columns) == ["outcome_0", "outcome_1", "outcome_2", "sum"]


def test_compare_methods_label_length_mismatch():
    with pytest.raises(ValueError):
        devig.compare_methods([2.0, 2.0], labels=["only_one"])


# ===========================================================================
# kelly: full-Kelly fraction.
# ===========================================================================


def test_kelly_fraction_formula_even_money():
    # Even-money (odds 2.0): f* = 2p - 1. f*=0.05 at p=0.525.
    assert kelly.kelly_fraction(0.525, 2.0) == pytest.approx(0.05)
    assert kelly.kelly_fraction(0.6, 2.0) == pytest.approx(0.2)


def test_kelly_fraction_general_formula():
    # f* = (p*(b+1) - 1)/b with b = odds - 1.
    p, odds = 0.5, 2.2
    b = odds - 1.0
    expected = (p * (b + 1.0) - 1.0) / b
    assert kelly.kelly_fraction(p, odds) == pytest.approx(expected)
    # Equivalent (p*o - 1)/(o - 1) form.
    assert kelly.kelly_fraction(p, odds) == pytest.approx((p * odds - 1.0) / (odds - 1.0))


def test_kelly_fraction_zero_on_nonpositive_edge():
    # Negative edge: p below break-even 1/odds.
    assert kelly.kelly_fraction(0.4, 2.0) == 0.0
    # Exactly fair (zero edge): f* == 0.
    assert kelly.kelly_fraction(0.5, 2.0) == 0.0
    # Deep underdog mispriced low.
    assert kelly.kelly_fraction(0.05, 3.0) == 0.0


def test_kelly_fraction_validation():
    with pytest.raises(ValueError):
        kelly.kelly_fraction(1.5, 2.0)
    with pytest.raises(ValueError):
        kelly.kelly_fraction(0.5, 1.0)


# ===========================================================================
# kelly: edge and EV.
# ===========================================================================


def test_edge_and_ev():
    assert kelly.edge(0.6, 2.0) == pytest.approx(0.2)
    assert kelly.edge(0.5, 2.0) == pytest.approx(0.0)
    assert kelly.edge(0.4, 2.0) == pytest.approx(-0.2)
    # EV in currency = edge * stake.
    assert kelly.ev(0.6, 2.0, 100.0) == pytest.approx(20.0)
    assert kelly.ev(0.4, 2.0, 100.0) == pytest.approx(-20.0)
    # EV decomposes as win-profit minus loss-of-stake.
    p, odds, s = 0.55, 1.91, 50.0
    manual = p * (odds - 1.0) * s - (1.0 - p) * s
    assert kelly.ev(p, odds, s) == pytest.approx(manual)


# ===========================================================================
# kelly: fractional stake with cap.
# ===========================================================================


def test_stake_quarter_kelly_currency():
    # Quarter Kelly on a positive edge.
    bankroll = 1000.0
    p, odds = 0.6, 2.0
    f_full = kelly.kelly_fraction(p, odds)  # 0.2
    expected = f_full * 0.25 * bankroll      # 0.05 * 1000 = 50
    assert kelly.stake(p, odds, bankroll) == pytest.approx(expected)
    assert kelly.stake(p, odds, bankroll) == pytest.approx(50.0)


def test_stake_hard_cap_binds():
    # A huge edge would want a big stake; the cap clips it.
    bankroll = 1000.0
    # p=0.9 at odds 2.0 -> f* = 0.8; quarter Kelly = 0.2 of bankroll, but cap=0.05.
    s = kelly.stake(0.9, 2.0, bankroll, fraction=0.25, cap=0.05)
    assert s == pytest.approx(0.05 * bankroll)  # 50, the cap
    # With a looser cap the quarter-Kelly stake comes through.
    s2 = kelly.stake(0.9, 2.0, bankroll, fraction=0.25, cap=0.5)
    assert s2 == pytest.approx(0.25 * 0.8 * bankroll)  # 160


def test_stake_zero_on_negative_edge_or_empty_bankroll():
    assert kelly.stake(0.4, 2.0, 1000.0) == 0.0
    assert kelly.stake(0.6, 2.0, 0.0) == 0.0
    assert kelly.stake(0.6, 2.0, -100.0) == 0.0


def test_stake_validation():
    with pytest.raises(ValueError):
        kelly.stake(0.6, 2.0, 1000.0, fraction=1.5)
    with pytest.raises(ValueError):
        kelly.stake(0.6, 2.0, 1000.0, cap=2.0)


# ===========================================================================
# kelly: simultaneous-exposure scaling.
# ===========================================================================


def test_exposure_scale_no_op_within_cap():
    stakes = [20.0, 10.0, 5.0]  # total 35
    out = kelly.simultaneous_exposure_scale(stakes, max_total_fraction=0.10, bankroll=1000.0)
    # Budget is 100; total 35 is within budget -> unchanged.
    assert out == pytest.approx(stakes)


def test_exposure_scale_respects_cap_and_preserves_ratios():
    stakes = [60.0, 30.0, 30.0]  # total 120
    bankroll = 1000.0
    max_frac = 0.10  # budget 100
    out = kelly.simultaneous_exposure_scale(stakes, max_total_fraction=max_frac, bankroll=bankroll)
    # Total exactly at the cap.
    assert out.sum() == pytest.approx(max_frac * bankroll)
    assert out.sum() == pytest.approx(100.0)
    # Ratios preserved: each stake scaled by the same factor.
    scale = (max_frac * bankroll) / sum(stakes)
    assert out == pytest.approx(np.asarray(stakes) * scale)
    # Pairwise ratios identical to the originals.
    assert out[0] / out[1] == pytest.approx(stakes[0] / stakes[1])
    assert out[1] / out[2] == pytest.approx(stakes[1] / stakes[2])


def test_exposure_scale_edge_cases():
    # All-zero stakes stay zero.
    out = kelly.simultaneous_exposure_scale([0.0, 0.0], max_total_fraction=0.1, bankroll=1000.0)
    assert out == pytest.approx([0.0, 0.0])
    # Non-positive bankroll -> zeros.
    out2 = kelly.simultaneous_exposure_scale([10.0, 5.0], max_total_fraction=0.1, bankroll=0.0)
    assert out2 == pytest.approx([0.0, 0.0])


def test_exposure_scale_validation():
    with pytest.raises(ValueError):
        kelly.simultaneous_exposure_scale([-1.0, 2.0], max_total_fraction=0.1, bankroll=1000.0)
    with pytest.raises(ValueError):
        kelly.simultaneous_exposure_scale([1.0, 2.0], max_total_fraction=1.5, bankroll=1000.0)
