"""Tests for the unified mispricing/lock-in/display module (wca.mispricing)."""
from __future__ import annotations

import pytest

from wca.mispricing import (
    EXCHANGE,
    POLYMARKET,
    SPORTSBOOK,
    assess,
    coherence,
    coherence_note,
    fmt_fair,
    fmt_price,
    fmt_size,
    from_decimal,
    from_pm_price,
    lock_in,
)


def test_quote_conversions():
    q = from_decimal("Bet365", 2.0)
    assert q.prob == pytest.approx(0.5)
    assert q.decimal == pytest.approx(2.0)
    assert q.cents == pytest.approx(50.0)
    p = from_pm_price(0.58)
    assert p.is_pm and p.cents == pytest.approx(58.0)
    assert p.decimal == pytest.approx(1.0 / 0.58)


def test_display_units_by_venue():
    assert fmt_price(from_decimal("Bet365", 1.72)) == "1.72"
    assert fmt_price(from_pm_price(0.58)) == "58¢"
    assert fmt_size(SPORTSBOOK, 10) == "£10.00"
    assert fmt_size(POLYMARKET, 58.5) == "$58.50"
    assert fmt_fair(0.58, POLYMARKET) == "58¢"
    assert fmt_fair(0.5, SPORTSBOOK) == "2.00"


def test_assess_edge_sign():
    # model 20% at decimal 6.0 -> edge +0.2
    m = assess(0.20, from_decimal("PP", 6.0))
    assert m.edge == pytest.approx(0.2)
    assert m.is_value
    # model 20% at decimal 4.0 -> edge -0.2
    assert assess(0.20, from_decimal("PP", 4.0)).edge == pytest.approx(-0.2)


def test_coherence_normal_book_has_vig():
    # 1X2 summing over 100% = normal vig, not a lock-in.
    qs = [from_decimal("B", 2.1), from_decimal("B", 3.6), from_decimal("B", 3.6)]
    cov = coherence(qs)
    assert cov.implied_sum > 1.0
    assert cov.is_normal and not cov.is_lockin


def test_coherence_sub_100_is_lockin():
    # The /next SA-Canada bug: PM best prices imply 82.6%.
    qs = [from_pm_price(1 / 6.90), from_pm_price(1 / 3.92), from_pm_price(1 / 2.35)]
    cov = coherence(qs)
    assert cov.implied_sum == pytest.approx(0.826, abs=0.01)
    assert cov.is_lockin
    assert cov.single_venue  # all Polymarket -> flagged as verify-live


def test_coherence_note_flags_single_venue_stale():
    qs = [from_pm_price(1 / 6.90), from_pm_price(1 / 3.92), from_pm_price(1 / 2.35)]
    note = coherence_note(qs)
    assert "<100%" in note
    assert "STALE" in note or "verify live" in note.lower()


def test_lock_in_cross_venue_stakes_equalise_payout():
    # Construct a genuine cross-venue arb: two-way market summing < 100%.
    qs = [from_decimal("Bet365", 2.10), from_decimal("Smarkets", 2.10, EXCHANGE)]
    # implied sum = 0.476+0.476 = 0.952 < 1 -> arb
    li = lock_in(qs, 100.0)
    assert li is not None
    assert li.profit_pct > 0
    # equal payout: stake_i * decimal_i equal across legs
    payouts = [leg.stake * leg.quote.decimal for leg in li.legs]
    assert payouts[0] == pytest.approx(payouts[1], rel=1e-6)


def test_lock_in_none_when_book_has_vig():
    qs = [from_decimal("B", 1.90), from_decimal("B", 1.90)]  # sums to 105%
    assert lock_in(qs, 100.0) is None


def test_lock_in_pm_leg_sized_in_usd():
    # A book leg (GBP) + a PM leg (USD): PM stake should be FX-scaled.
    qs = [from_decimal("Bet365", 2.5), from_pm_price(0.30)]  # 0.40+0.30=0.70 < 1
    li = lock_in(qs, 100.0, usd_per_gbp=1.30)
    li_nofx = lock_in(qs, 100.0, usd_per_gbp=1.00)
    assert li is not None and li_nofx is not None
    pm_leg = [l for l in li.legs if l.quote.is_pm][0]
    pm_leg_nofx = [l for l in li_nofx.legs if l.quote.is_pm][0]
    book_leg = [l for l in li.legs if not l.quote.is_pm][0]
    book_leg_nofx = [l for l in li_nofx.legs if not l.quote.is_pm][0]
    # PM leg is FX-scaled (USD); book leg is not.
    assert pm_leg.stake == pytest.approx(pm_leg_nofx.stake * 1.30)
    assert book_leg.stake == pytest.approx(book_leg_nofx.stake)
