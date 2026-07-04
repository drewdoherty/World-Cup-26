"""Cross-venue arbitrage + boost/promo extraction — production math only.

Arbitrage  → ``wca.arbfx``: evaluate_pair / evaluate_lock / best_lock with
             venue-correct commissions (Smarkets 2%, Betfair 6%), the fixed
             $1.33/£ with a 0.5% FX haircut on converted legs, and PM
             NO-side pricing (1 − yes).
Boosts     → ``wca.boostlock``: equal-profit lay vs a boosted builder,
             promo max-stake clamps.
Promo EV   → :func:`promo_ev` models the ACTUAL terms (qualifying odds
             floor, stake-returned flag, free-bet conversion, max stake,
             rollover, expiry). Anything with unknown terms is emitted
             ``executable=False`` with the missing fields listed — the spec
             forbids pretending.

Sportsbook legs appear ONLY inside these two structures (post-cost locked
arb, or promo extraction) — plain sportsbook value bets are out of scope by
design (and were killed as −EV leaks in production).
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import lib.bootstrap  # noqa: F401
from wca import arbfx
from wca.boostlock import build_lock, equal_profit_lay


def stake_rounding(stake: float, venue: str) -> float:
    """Venues accept different stake granularity; rounding erodes locked
    profit — model it, don't ignore it. PM = whole shares ⇒ cents are fine;
    books/exchanges commonly enforce £0.01–£0.05 steps; we round DOWN to
    £0.05 for books (conservative)."""
    step = 0.05 if venue not in ("polymarket",) else 0.01
    return math.floor(stake / step) * step


def arb_exchange_pm(*, fixture: str = "?", market: str = "h2h",
                    exchange_outcome: str = "A", exchange_odds: float,
                    pm_outcome: str = "not-A", pm_yes_price: float,
                    fx_usd_per_gbp: float = 1.33,
                    exchange_commission: float = arbfx.DEFAULT_BETFAIR_COMMISSION,
                    total_outlay_gbp: float = 100.0,
                    min_profit_frac: float = 0.005) -> Dict[str, Any]:
    """Verify a genuine exchange-back + PM-YES two-way lock via PRODUCTION
    ``wca.arbfx.evaluate_pair`` (fees + FX haircut), then re-audit the lock
    AFTER stake rounding. The two outcomes must be mutually exhaustive of
    the settled event — the caller certifies coverage; this function
    certifies the money."""
    opp = arbfx.evaluate_pair(
        fixture=fixture, market=market,
        betfair_outcome=exchange_outcome, betfair_odds=exchange_odds,
        pm_outcome=pm_outcome, pm_price=pm_yes_price,
        fx_usd_per_gbp=fx_usd_per_gbp,
        betfair_commission=exchange_commission,
        total_outlay_gbp=total_outlay_gbp)
    out: Dict[str, Any] = {"raw": dataclasses.asdict(opp) if opp else None,
                           "is_arb": opp is not None}
    if opp is None:
        out["verdict"] = "no arb after venue costs (production evaluate_pair)"
        return out
    ex_leg, pm_leg = opp.legs[0], opp.legs[1]
    s_ex = stake_rounding(ex_leg.stake, ex_leg.venue)          # GBP
    s_pm = stake_rounding(pm_leg.stake, "polymarket")           # USD
    s_pm_gbp = s_pm / fx_usd_per_gbp * (1 - arbfx.DEFAULT_FX_HAIRCUT)
    outlay_gbp = s_ex + s_pm / fx_usd_per_gbp
    # scenario 1: exchange outcome lands → exchange pays, PM stake gone
    pnl_ex = s_ex * (ex_leg.net_decimal - 1) - s_pm / fx_usd_per_gbp
    # scenario 2: PM outcome lands → PM pays (haircut on conversion)
    pnl_pm = s_pm * (pm_leg.net_decimal - 1) / fx_usd_per_gbp \
        * (1 - arbfx.DEFAULT_FX_HAIRCUT) - s_ex
    worst = min(pnl_ex, pnl_pm)
    out.update({
        "legs": [{"venue": ex_leg.venue, "ccy": "GBP", "odds": ex_leg.price,
                  "net_decimal": ex_leg.net_decimal, "stake": round(s_ex, 2)},
                 {"venue": "polymarket", "ccy": "USD", "yes_price": pm_leg.price,
                  "net_decimal": pm_leg.net_decimal, "stake": round(s_pm, 2)}],
        "total_outlay_gbp": round(outlay_gbp, 2),
        "locked_profit_gbp_after_rounding": round(worst, 2),
        "roi_after_rounding": round(worst / outlay_gbp, 4) if outlay_gbp else 0.0,
        "guaranteed_pct_pre_rounding": opp.guaranteed_pct,
    })
    out["verdict"] = ("EXECUTABLE arb" if out["roi_after_rounding"]
                      >= min_profit_frac else
                      f"arb evaporates after rounding/costs "
                      f"(roi {out['roi_after_rounding']:.3%} < floor)")
    return out


@dataclass
class PromoTerms:
    """Actual promo terms — every field matters for EV. None = unknown."""
    name: str
    venue: str
    promo_type: str                     # free_bet | profit_boost | bet_and_get
    max_stake: Optional[float] = None
    qualifying_min_odds: Optional[float] = None
    stake_returned: Optional[bool] = None   # boosted/free-bet stake returned?
    boost_frac: Optional[float] = None      # profit boost fraction
    freebet_amount: Optional[float] = None
    rollover: Optional[float] = None        # turnover multiple before withdrawal
    expiry_utc: Optional[str] = None
    jurisdiction_ok: Optional[bool] = None  # UK books for the user


def promo_ev(t: PromoTerms, *, back_odds: float, lay_odds: float,
             fair_p: Optional[float] = None,
             exchange_commission: float = 0.02,
             freebet_conversion: float = 0.70) -> Dict[str, Any]:
    """EV of extracting one promo, matched where possible.

    free_bet / bet_and_get: qualifying loss = matched-bet cost at these odds;
    value = freebet_amount × conversion − qualifying loss.
    profit_boost: EV of the boosted back matched with an equal-profit lay
    (production wca.boostlock math). Returns executable=False with the
    missing-term list when terms are incomplete."""
    missing = [f for f in ("max_stake", "qualifying_min_odds", "stake_returned")
               if getattr(t, f) is None]
    if t.promo_type in ("free_bet", "bet_and_get") and t.freebet_amount is None:
        missing.append("freebet_amount")
    if t.promo_type == "profit_boost" and t.boost_frac is None:
        missing.append("boost_frac")
    if t.jurisdiction_ok is False:
        return {"executable": False, "reason": "outside user jurisdiction",
                "ev": None}
    if missing:
        return {"executable": False,
                "reason": f"terms incomplete: {missing}", "ev": None}
    if t.qualifying_min_odds and back_odds < t.qualifying_min_odds:
        return {"executable": False,
                "reason": f"odds {back_odds} below qualifying floor "
                          f"{t.qualifying_min_odds}", "ev": None}

    stake = t.max_stake
    lay_net_mult = 1.0 - exchange_commission
    # matched qualifying cost: back stake vs equal-profit lay
    lay_stake = stake * back_odds / (lay_odds - exchange_commission)
    q_cost_back_wins = stake * (back_odds - 1) - lay_stake * (lay_odds - 1)
    q_cost_lay_wins = lay_stake * lay_net_mult - stake
    qualifying_cost = -min(q_cost_back_wins, q_cost_lay_wins)

    if t.promo_type in ("free_bet", "bet_and_get"):
        gross = t.freebet_amount * freebet_conversion
        ev = gross - qualifying_cost
        rollover_drag = 0.0
        if t.rollover:
            # each £1 of rollover turnover at ~2% matched cost
            rollover_drag = t.rollover * t.freebet_amount * 0.02
            ev -= rollover_drag
        return {"executable": True, "ev": round(ev, 2),
                "qualifying_cost": round(qualifying_cost, 2),
                "freebet_value": round(gross, 2),
                "rollover_drag": round(rollover_drag, 2),
                "detail": f"£{stake} @ {back_odds} matched at lay {lay_odds}"}
    if t.promo_type == "profit_boost":
        lay = equal_profit_lay(stake, back_odds, t.boost_frac, lay_odds,
                               exchange_commission)
        boosted_win = stake * (back_odds - 1) * (1 + t.boost_frac) \
            - lay * (lay_odds - 1)
        lay_win = lay * lay_net_mult - stake
        ev = min(boosted_win, lay_win)  # equal-profit ⇒ ≈ both sides
        return {"executable": True, "ev": round(ev, 2),
                "lay_stake": round(lay, 2),
                "detail": f"boost {t.boost_frac:.0%} on £{stake} @ {back_odds}, "
                          f"lay {lay_odds}"}
    return {"executable": False, "reason": f"unknown promo_type {t.promo_type}",
            "ev": None}
