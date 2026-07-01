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
    assert B.PM_CCY == "$"


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
