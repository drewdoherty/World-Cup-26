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


# ---------------------------------------------------------------------------
# Generic N-venue locks (PM USD, Betfair GBP, Smarkets GBP). Monitoring-only.
# ---------------------------------------------------------------------------

SMARKETS_COMMISSION = 0.02  # 2% on net winnings (verified)
_EXCHANGE_COMMISSION = {"betfair": DEFAULT_BETFAIR_COMMISSION, "smarkets": SMARKETS_COMMISSION}


def exchange_back_net(odds: float, venue: str) -> float:
    """Net decimal for backing an outcome on a GBP exchange."""
    c = _EXCHANGE_COMMISSION.get(venue, 0.0)
    return 1.0 + (odds - 1.0) * (1.0 - c) if odds and odds > 1.0 else 0.0


def exchange_lay_net(lay_odds: float, venue: str) -> float:
    """Net decimal of the ¬outcome payoff from LAYING at *lay_odds* (native depth)."""
    c = _EXCHANGE_COMMISSION.get(venue, 0.0)
    return 1.0 + (1.0 - c) / (lay_odds - 1.0) if lay_odds and lay_odds > 1.0 else 0.0


def pm_no_net(yes_price: float) -> float:
    """Net decimal of PM NO (¬outcome) implied from the YES price (symmetric fee)."""
    if not (0.0 < yes_price < 1.0):
        return 0.0
    cost = (1.0 - yes_price) + PM_TAKER_FEE_RATE * yes_price * (1.0 - yes_price)
    return 1.0 / cost if cost > 0 else 0.0


@dataclass(frozen=True)
class LockLeg:
    venue: str
    currency: str
    side: str        # "win" (backs outcome) | "lose" (opposes it)
    net: float
    desc: str
    stake: float = 0.0


@dataclass(frozen=True)
class LockResult:
    fixture: str
    market: str
    outcome: str
    venue_pair: str
    fee_adj_edge: float
    guaranteed_pct: float
    confidence: str
    legs: List[LockLeg]
    notes: str = ""


def evaluate_lock(win, lose, *, fx_usd_per_gbp, fx_haircut=DEFAULT_FX_HAIRCUT,
                  total_outlay_gbp=100.0):
    """win/lose are dicts {venue,currency,net,desc,confidence}. Return LockResult|None."""
    wn, ln = win["net"], lose["net"]
    if not (wn > 1.0 and ln > 1.0 and fx_usd_per_gbp and fx_usd_per_gbp > 0.0):
        return None
    inv = 1.0 / wn + 1.0 / ln
    edge = 1.0 - inv
    if edge <= 0.0:
        return None
    f_w, f_l = (1.0 / wn) / inv, (1.0 / ln) / inv
    gross = (1.0 / inv) - 1.0
    cross = win["currency"] != lose["currency"]
    usd_frac = (f_w if win["currency"] == "USD" else 0.0) + (f_l if lose["currency"] == "USD" else 0.0)
    guaranteed = gross - (fx_haircut * usd_frac if cross else 0.0)

    def stake(frac, cur):
        g = frac * total_outlay_gbp
        return round(g * fx_usd_per_gbp, 2) if cur == "USD" else round(g, 2)

    legs = [
        LockLeg(win["venue"], win["currency"], "win", round(wn, 4), win["desc"], stake(f_w, win["currency"])),
        LockLeg(lose["venue"], lose["currency"], "lose", round(ln, 4), lose["desc"], stake(f_l, lose["currency"])),
    ]
    conf = _min_conf(win.get("confidence", "monitoring-grade"), lose.get("confidence", "monitoring-grade"), cross)
    return LockResult(
        fixture=win.get("fixture", ""), market=win.get("market", ""),
        outcome=win.get("outcome", ""),
        venue_pair="%s↔%s" % (win["venue"], lose["venue"]),
        fee_adj_edge=round(edge, 5), guaranteed_pct=round(guaranteed, 5),
        confidence=conf, legs=legs,
        notes="monitoring-only" + ("; FX haircut %.2f%%" % (fx_haircut * 100) if cross else "; same-currency (no FX)"),
    )


_CONF_RANK = {"execution-grade": 2, "monitoring-grade": 1, "low": 0}


def _min_conf(a, b, cross):
    rank = min(_CONF_RANK.get(a, 1), _CONF_RANK.get(b, 1))
    label = {2: "execution-grade", 1: "monitoring-grade", 0: "low"}[rank]
    # any cross-currency (FX) leg caps at monitoring-grade (FX/slippage risk)
    if cross and rank > 1:
        label = "monitoring-grade"
    return label


def best_lock(*, fixture, market, outcome, win_legs, lose_legs, fx_usd_per_gbp,
              fx_haircut=DEFAULT_FX_HAIRCUT):
    """Best cross-venue lock for one outcome: back it on venue A, oppose on B (A≠B)."""
    best = None
    for w in win_legs:
        for l in lose_legs:
            if w["venue"] == l["venue"]:
                continue
            w2 = {**w, "fixture": fixture, "market": market, "outcome": outcome}
            res = evaluate_lock(w2, l, fx_usd_per_gbp=fx_usd_per_gbp, fx_haircut=fx_haircut)
            if res and (best is None or res.guaranteed_pct > best.guaranteed_pct):
                best = res
    return best
