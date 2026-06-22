"""Agent 7 — Bet Sizing.

Applies fractional Kelly to the approved edge opportunity, subject to the
ledger-resolved bankroll and the daily-exposure cap.

Input:  EdgeReport + AdversarialReview + bankroll params
Output: BetSizing (or None if review is not approved)
"""

from __future__ import annotations

import logging
from typing import Optional

from wca.agents.contracts import AdversarialReview, BetSizing, EdgeReport

logger = logging.getLogger(__name__)

# Default fractional-Kelly multiplier (25 % of full Kelly).
_KELLY_FRACTION = 0.25
# Hard per-bet cap as a fraction of bankroll.
_PER_BET_CAP = 0.05
# Daily-exposure cap across the full slate.
_DAILY_EXPOSURE_CAP = 0.05


def run(
    edges: EdgeReport,
    review: AdversarialReview,
    bankroll: float = 1500.0,
    kelly_fraction: float = _KELLY_FRACTION,
    per_bet_cap: float = _PER_BET_CAP,
    daily_exposure_used: float = 0.0,
    currency: str = "GBP",
) -> Optional[BetSizing]:
    """Return bet-sizing recommendation, or None if the pick is blocked/has no edge.

    Parameters
    ----------
    edges:
        Edge report from Agent 5.
    review:
        Adversarial review from Agent 6.  If ``approved`` is False the function
        returns ``None``.
    bankroll:
        Current deployable bankroll (should come from the ledger).
    kelly_fraction:
        Fractional-Kelly multiplier; default 0.25 (quarter Kelly).
    per_bet_cap:
        Hard per-bet ceiling as a fraction of bankroll.
    daily_exposure_used:
        Fraction of bankroll already committed to today's bets (for exposure cap).
    currency:
        Currency label for display (e.g. "GBP" or "USD").
    """
    if not review.approved or edges.top_pick is None:
        return None

    top = edges.top_pick

    try:
        from wca.markets.kelly import kelly_fraction as full_kelly
    except ImportError as exc:
        logger.error("Cannot import kelly module: %s", exc)
        return None

    # Full-Kelly fraction.
    full_k = full_kelly(p=top.model_probability, decimal_odds=top.odds)
    if full_k <= 0:
        logger.info("Kelly fraction is non-positive — no bet recommended.")
        return None

    # Apply fractional Kelly and the per-bet cap.
    frac_k = full_k * kelly_fraction
    capped = min(frac_k, per_bet_cap)

    # Respect the remaining daily-exposure budget.
    exposure_budget = max(0.0, _DAILY_EXPOSURE_CAP - daily_exposure_used)
    stake_pct = min(capped, exposure_budget)

    if stake_pct <= 0:
        logger.info("Daily exposure cap exhausted — no stake.")
        return None

    stake_amount = round(bankroll * stake_pct, 2)

    logger.info(
        "Sizing: %.2f%% of %s%.0f = %s%.2f (full Kelly %.2f%%, capped %.2f%%)",
        stake_pct * 100, currency, bankroll, currency, stake_amount,
        full_k * 100, capped * 100,
    )

    return BetSizing(
        opportunity=top,
        stake_pct=round(stake_pct, 6),
        stake_amount=stake_amount,
        bankroll_ref=bankroll,
        portfolio_impact={
            "daily_exposure_pct": round((daily_exposure_used + stake_pct) * 100, 2),
            "full_kelly_pct": round(full_k * 100, 4),
            "capped_kelly_pct": round(capped * 100, 4),
        },
    )
