"""Project-wide Polymarket bankroll & sizing rule — the SINGLE SOURCE OF TRUTH.

GLOBAL RULE (applies everywhere Polymarket is sized, not just the test book):

* Polymarket accounting is done in **USD** (``$``).
* The staking bankroll is **£3,000 ± realised P&L**, converted to USD at the
  project FX rate **$1.33 = £1**.
* Position size is **¼-Kelly** of that USD bankroll.

Because £3,000 → $3,990 and realised P&L is already booked in USD, the bankroll
in USD is simply ``3000·1.33 + realised_pnl_usd`` (the £-first and $-first
readings are algebraically identical). Import from here — never re-hardcode the
base, the FX rate, or the Kelly fraction anywhere else.
"""

from __future__ import annotations

import os

# --- The rule (env-overridable so ops can retune without a code change). ------
GBP_PM_BANKROLL_BASE = float(os.environ.get("WCA_PM_BANKROLL_GBP", "3000"))  # £
GBP_USD = float(os.environ.get("WCA_GBP_USD", "1.33"))                        # $ per £
PM_KELLY_FRACTION = float(os.environ.get("WCA_PM_KELLY", "0.25"))            # ¼-Kelly
PM_CCY = "$"


def gbp_to_usd(gbp: float) -> float:
    """Convert £ to $ at the project FX rate ($1.33 = £1)."""
    return float(gbp) * GBP_USD


def pm_bankroll_usd(realized_pnl_usd: float = 0.0) -> float:
    """Live Polymarket bankroll in USD = £3,000 ± realised P&L, at $1.33 = £1.

    ``realized_pnl_usd`` is the strategy's realised P&L to date, already in USD.
    """
    return gbp_to_usd(GBP_PM_BANKROLL_BASE) + float(realized_pnl_usd or 0.0)
