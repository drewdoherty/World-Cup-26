"""The project-wide Polymarket bankroll/sizing rule (single source of truth)."""

from __future__ import annotations

import importlib

import pytest


def _fresh(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import wca.markets.bankroll as B
    return importlib.reload(B)


def test_defaults_are_the_global_rule():
    import wca.markets.bankroll as B
    B = importlib.reload(B)
    assert B.GBP_PM_BANKROLL_BASE == 3000.0
    assert B.GBP_USD == 1.33
    assert B.PM_KELLY_FRACTION == 0.25
    assert B.PM_MAX_STAKE_FRAC == 0.04     # per-bet cap
    assert B.PM_BOOK_CAP_FRAC == 0.75      # whole-book cap
    assert B.PM_CCY == "$"


def test_size_placement_caps_at_4pct():
    import wca.markets.bankroll as B
    B = importlib.reload(B)
    # Pedri 94/82: f*=0.667, ¼-Kelly=16.7% -> capped to 4%.
    s = B.size_placement(0.94, 0.82, 3990.0)
    assert s["capped"] is True
    assert s["frac"] == pytest.approx(0.04)
    assert s["stake"] == pytest.approx(0.04 * 3990.0)   # ~$160
    # France 62/55: ¼-Kelly=3.9% < 4% -> uncapped.
    f = B.size_placement(0.62, 0.55, 3990.0)
    assert f["capped"] is False and f["frac"] == pytest.approx(0.25 * 0.07 / 0.45)


def test_book_scale_fits_75pct_cap():
    import wca.markets.bankroll as B
    B = importlib.reload(B)
    bank = 4000.0                       # cap = 0.75*4000 = 3000
    assert B.book_scale(1000.0, 1000.0, bank) == pytest.approx(1.0)     # 2000 <= 3000 fits
    assert B.book_scale(1000.0, 2500.0, bank) == pytest.approx(0.5)     # avail 500 / new 1000
    assert B.book_scale(1000.0, 3000.0, bank) == pytest.approx(0.0)     # book already full
    assert B.book_scale(0.0, 3500.0, bank) == pytest.approx(1.0)        # no new stake


def test_gbp_to_usd_and_bankroll():
    import wca.markets.bankroll as B
    B = importlib.reload(B)
    assert B.gbp_to_usd(3000.0) == pytest.approx(3990.0)
    assert B.pm_bankroll_usd(0.0) == pytest.approx(3990.0)      # £3,000 @ $1.33
    assert B.pm_bankroll_usd(500.0) == pytest.approx(4490.0)    # + realised P&L
    assert B.pm_bankroll_usd(-490.0) == pytest.approx(3500.0)   # - realised P&L


def test_env_overrides(monkeypatch):
    B = _fresh(monkeypatch, WCA_PM_BANKROLL_GBP="4000", WCA_GBP_USD="1.30", WCA_PM_KELLY="0.5")
    assert B.pm_bankroll_usd(0.0) == pytest.approx(5200.0)
    assert B.PM_KELLY_FRACTION == 0.5
    # restore module defaults for the rest of the suite
    for k in ("WCA_PM_BANKROLL_GBP", "WCA_GBP_USD", "WCA_PM_KELLY"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(B)
