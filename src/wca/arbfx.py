"""FX-adjusted pure-arbitrage across Polymarket (USD) ↔ Betfair (GBP).

MONITORING / ANALYTICS ONLY — no execution, no fund movement. Detects where
backing one side on Betfair (via The Odds API) and opposing it on Polymarket
locks in risk-free profit after both venues' fees and an FX-conversion haircut.

Math (two-outcome lock, back X on Betfair / oppose via PM YES):
* Betfair net decimal:  b_bf = 1 + (o_bf - 1) * (1 - commission)
* PM net decimal:       b_pm = 1 / (p + fee),  fee = 0.03 * p * (1 - p)
* Arb exists iff 1/b_bf + 1/b_pm < 1 (FX-independent existence test).
* Guaranteed return on total stake = 1 / (1/b_bf + 1/b_pm) - 1, then reduced by
  an FX-conversion haircut applied to the cross-currency leg.
* FX sets the stake split: with fx = USD per GBP, equalise converted payouts so
  S_gbp * b_bf == (S_usd * b_pm) / fx.

Assumptions (documented, surfaced in output): best back odds only (no lay/
depth/liquidity from the aggregator) → monitoring-grade; same settlement basis
enforced by the caller (1x2_90min); FX haircut models real conversion spread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from wca.arb import PM_TAKER_FEE_RATE, effective_back, pm_yes_to_decimal

DEFAULT_BETFAIR_COMMISSION = 0.06
DEFAULT_FX_HAIRCUT = 0.005  # 0.5% on the converted leg (you can't convert at mid)


@dataclass(frozen=True)
class ArbLeg:
    venue: str          # "betfair" | "polymarket"
    currency: str       # "GBP" | "USD"
    selection: str
    price: float        # decimal odds (betfair) or YES price (pm)
    net_decimal: float  # fee-adjusted decimal odds
    stake: float        # native-currency stake for a unit total outlay (GBP)


@dataclass(frozen=True)
class ArbOpp:
    fixture: str
    market: str                 # e.g. "h2h"
    betfair_outcome: str
    pm_outcome: str
    betfair_odds: float
    pm_price: float
    fx_usd_per_gbp: float
    fee_adj_edge: float         # 1 - (1/b_bf + 1/b_pm); >0 means arb
    guaranteed_pct: float       # after FX haircut, on total outlay
    legs: List[ArbLeg]
    confidence: str             # "high" | "medium" | "low"
    notes: str = ""
    meta: Dict = field(default_factory=dict)


def evaluate_pair(
    *,
    fixture: str,
    market: str,
    betfair_outcome: str,
    betfair_odds: float,
    pm_outcome: str,
    pm_price: float,
    fx_usd_per_gbp: float,
    betfair_commission: float = DEFAULT_BETFAIR_COMMISSION,
    fx_haircut: float = DEFAULT_FX_HAIRCUT,
    confidence: str = "medium",
    total_outlay_gbp: float = 100.0,
) -> Optional[ArbOpp]:
    """Return an :class:`ArbOpp` if the pair locks risk-free profit, else None.

    ``betfair_outcome`` and ``pm_outcome`` must be the SAME real-world result
    (PM YES pays when that result occurs); the caller pairs them so the two legs
    are mutually exhaustive of the locked event.
    """
    if not (betfair_odds and betfair_odds > 1.0):
        return None
    if not (0.0 < pm_price < 1.0):
        return None
    if not (fx_usd_per_gbp and fx_usd_per_gbp > 0.0):
        return None

    b_bf = effective_back(betfair_odds, "betfair", {"betfair": betfair_commission})
    b_pm = pm_yes_to_decimal(pm_price)
    if b_bf <= 1.0 or b_pm <= 1.0:
        return None

    inv = 1.0 / b_bf + 1.0 / b_pm
    fee_adj_edge = 1.0 - inv
    if fee_adj_edge <= 0.0:
        return None  # no arb after fees

    # Stake split: equalise converted payouts. Fractions of total outlay (GBP).
    f_bf = (1.0 / b_bf) / inv          # GBP stake fraction
    f_pm = (1.0 / b_pm) / inv          # GBP-equivalent fraction for the USD leg
    gross_pct = (1.0 / inv) - 1.0
    # Haircut applies to the cross-currency (PM/USD) leg's contribution.
    guaranteed_pct = gross_pct - fx_haircut * f_pm

    s_bf_gbp = f_bf * total_outlay_gbp
    s_pm_usd = f_pm * total_outlay_gbp * fx_usd_per_gbp  # convert GBP-equiv → USD

    legs = [
        ArbLeg("betfair", "GBP", betfair_outcome, betfair_odds, b_bf, round(s_bf_gbp, 2)),
        ArbLeg("polymarket", "USD", pm_outcome, pm_price, b_pm, round(s_pm_usd, 2)),
    ]
    return ArbOpp(
        fixture=fixture, market=market,
        betfair_outcome=betfair_outcome, pm_outcome=pm_outcome,
        betfair_odds=round(betfair_odds, 4), pm_price=round(pm_price, 4),
        fx_usd_per_gbp=round(fx_usd_per_gbp, 4),
        fee_adj_edge=round(fee_adj_edge, 5),
        guaranteed_pct=round(guaranteed_pct, 5),
        legs=legs, confidence=confidence,
        notes="monitoring-only; best-back odds, no liquidity/depth; FX haircut %.2f%%"
              % (fx_haircut * 100),
        meta={"total_outlay_gbp": total_outlay_gbp},
    )
