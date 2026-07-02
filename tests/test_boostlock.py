"""Boost-lock math: both branches must pay identically, to the penny."""
from __future__ import annotations

import pytest

from wca.boostlock import build_lock, equal_profit_lay, format_lock


def test_equal_profit_both_branches_50pct_boost():
    # £50 @ 2.0 with 50% winnings boost, lay 2.10 at 0% commission (Smarkets).
    S, locked = equal_profit_lay(50.0, 2.0, 0.5, 2.10, 0.0)
    assert S == pytest.approx(125.0 / 2.10)
    # win branch: 50*1*1.5 - S*1.10 ; lose branch: S - 50
    assert 75.0 - S * 1.10 == pytest.approx(locked)
    assert S - 50.0 == pytest.approx(locked)
    assert locked > 9.0  # ~£9.52 lock on £50


def test_commission_reduces_lock():
    _, no_comm = equal_profit_lay(50.0, 2.0, 0.5, 2.10, 0.0)
    _, with_comm = equal_profit_lay(50.0, 2.0, 0.5, 2.10, 0.02)
    assert with_comm < no_comm


def test_no_boost_near_zero_lock_at_fair_lay():
    # Without a boost, backing 2.0 and laying 2.0 at 0% commission locks ~0.
    _, locked = equal_profit_lay(50.0, 2.0, 0.0, 2.0, 0.0)
    assert locked == pytest.approx(0.0, abs=1e-9)


def test_degenerate_inputs_raise():
    for bad in [
        dict(back_stake=0, builder_odds=2, boost_frac=0.5, lay_odds=2.1),
        dict(back_stake=50, builder_odds=1.0, boost_frac=0.5, lay_odds=2.1),
        dict(back_stake=50, builder_odds=2, boost_frac=0.5, lay_odds=1.0),
        dict(back_stake=50, builder_odds=2, boost_frac=-0.1, lay_odds=2.1),
    ]:
        with pytest.raises(ValueError):
            equal_profit_lay(**bad)


def test_build_lock_equivalent_template():
    lock = build_lock("Switzerland vs Algeria", "Switzerland", "Algeria",
                      builder_odds=2.05, lay_odds=2.10, back_stake=50.0)
    assert lock.equivalent is True
    assert len(lock.legs) == 3
    assert "Switzerland to win" in lock.legs[0]
    assert lock.locked_profit > 0
    out = format_lock(lock)
    assert "SGM BOOST LOCK" in out and "LOCKED" in out
    assert "APPROXIMATE" not in out


def test_build_lock_flags_non_implied_leg_and_low_odds():
    lock = build_lock("Portugal vs Croatia", "Portugal", "Croatia",
                      builder_odds=1.80, lay_odds=1.85, back_stake=50.0,
                      extra_leg="Croatia under 3.5 team goals")
    assert lock.equivalent is False
    assert "NON-implied" in lock.notes and "BELOW the promo minimum" in lock.notes
    assert "APPROXIMATE" in format_lock(lock)
