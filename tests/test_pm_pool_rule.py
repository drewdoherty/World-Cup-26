"""The PM pool must follow the global sizing rule (wca.markets.bankroll).

Regression guard for the 2026-07-02 fix: the pool was hardcoded at $1,310 in
scripts/wca_betrecs.py and src/wca/advancement.py, silently overriding the
project-wide rule (¼-Kelly of £3,000 ± realised P&L at $1.33/£, 4%/bet cap).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from wca.markets import bankroll  # noqa: E402
from wca import advancement  # noqa: E402
import wca_betrecs as br  # noqa: E402


def test_advancement_pool_matches_global_rule():
    assert advancement.PM_POOL_BANKROLL == pytest.approx(bankroll.pm_bankroll_usd())
    assert advancement.PM_KELLY_FRACTION == bankroll.PM_KELLY_FRACTION
    assert advancement.PM_PER_BET_CAP == bankroll.PM_MAX_STAKE_FRAC


def test_betrecs_pm_pool_matches_global_rule():
    assert br.DEFAULT_PM_BANKROLL_USD == pytest.approx(bankroll.pm_bankroll_usd())
    pool = br._pm_pool(bankroll.pm_bankroll_usd())
    assert pool["kelly_fraction"] == bankroll.PM_KELLY_FRACTION
    assert pool["per_bet_cap"] == bankroll.PM_MAX_STAKE_FRAC
    assert pool["max_stake"] == pytest.approx(
        round(bankroll.pm_bankroll_usd() * bankroll.PM_MAX_STAKE_FRAC, 2)
    )


def test_pm_realised_pnl_missing_db_is_none(tmp_path):
    assert br._pm_realised_pnl_usd(str(tmp_path / "nope.db")) is None
