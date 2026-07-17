"""Settlement-aware dominance bounds for HL advancement vs PM 90-minute 1X2.

This module is pure research math. It never fetches data, sizes a position,
or submits an order. The contracts are different but nested:

    team advances = team wins in 90 minutes OR
                    (90-minute draw AND team wins ET/penalties)

That identity creates two fully covered baskets when every listed leg is
directly purchasable. A positive margin remains a candidate until venue
fees, depth, timestamps, and the exact cancellation clauses are verified.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Optional

from wca.hl.xvenue import pm_taker_fee


@dataclass(frozen=True)
class DominanceCandidate:
    direction: str
    cost: float
    guaranteed_payout: float
    margin: float
    break_even_hl_settlement_fee: float
    status: str
    detail: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _price(value: float, name: str) -> float:
    p = float(value)
    if not 0.0 < p < 1.0:
        raise ValueError("%s must be strictly between 0 and 1" % name)
    return p


def advance_probability(
    p_win_90: float,
    p_draw_90: float,
    p_win_tie_after_draw: float,
) -> float:
    """Return P(advance) from disjoint 90-minute and drawn-tie branches."""
    win = _price(p_win_90, "p_win_90")
    draw = _price(p_draw_90, "p_draw_90")
    tie = _price(p_win_tie_after_draw, "p_win_tie_after_draw")
    if win + draw >= 1.0:
        raise ValueError("p_win_90 + p_draw_90 must be below 1")
    return win + draw * tie


def _candidate(direction: str, cost: float, settlement_fee: Optional[float],
               detail: str) -> DominanceCandidate:
    fee_known = settlement_fee is not None
    fee = 0.0 if settlement_fee is None else float(settlement_fee)
    if not 0.0 <= fee < 1.0:
        raise ValueError("hl_settlement_fee must be in [0, 1)")
    # Conservative floor: assume the guaranteed $1 branch is paid entirely
    # through HL and therefore bears the whole settlement fee.
    payout = 1.0 - fee
    margin = payout - cost
    if margin <= 0:
        status = "NO_ARB"
    elif not fee_known:
        status = "CANDIDATE_FEE_UNVERIFIED"
    else:
        status = "ARB_CANDIDATE"
    return DominanceCandidate(
        direction=direction,
        cost=round(cost, 8),
        guaranteed_payout=round(payout, 8),
        margin=round(margin, 8),
        break_even_hl_settlement_fee=round(max(0.0, 1.0 - cost), 8),
        status=status,
        detail=detail,
    )


def evaluate_advancement_vs_1x2(
    *,
    hl_advance_yes_ask: float,
    hl_advance_no_ask: float,
    pm_team_yes_ask: float,
    pm_team_no_ask: float,
    pm_draw_yes_ask: float,
    hl_trading_fee_per_share: float = 0.0,
    hl_settlement_fee: Optional[float] = None,
) -> Dict[str, object]:
    """Evaluate both directly-purchasable dominance baskets.

    Basket 1 buys ``HL advance YES + PM team-win NO``. It pays at least $1
    in every played-match state and $2 when the team advances after a draw.

    Basket 2 buys ``HL advance NO + PM team-win YES + PM draw YES``. It pays
    at least $1 in every played-match state and $2 when the opponent advances
    after a 90-minute draw.

    PM fees are charged on every PM taker leg. HL trading fees are supplied
    explicitly. An unknown HL settlement fee prevents an ``ARB_CANDIDATE``
    label even when the zero-fee margin is positive.
    """
    hy = _price(hl_advance_yes_ask, "hl_advance_yes_ask")
    hn = _price(hl_advance_no_ask, "hl_advance_no_ask")
    py = _price(pm_team_yes_ask, "pm_team_yes_ask")
    pn = _price(pm_team_no_ask, "pm_team_no_ask")
    pd = _price(pm_draw_yes_ask, "pm_draw_yes_ask")
    hlf = float(hl_trading_fee_per_share)
    if hlf < 0.0:
        raise ValueError("hl_trading_fee_per_share cannot be negative")

    first = _candidate(
        "BUY_HL_ADVANCE_YES__BUY_PM_TEAM_NO",
        hy + hlf + pn + pm_taker_fee(pn),
        hl_settlement_fee,
        "PM team-NO covers failure to advance; a drawn tie followed by team "
        "advancement pays both legs.",
    )
    second = _candidate(
        "BUY_HL_ADVANCE_NO__BUY_PM_TEAM_YES__BUY_PM_DRAW_YES",
        hn + hlf + py + pm_taker_fee(py) + pd + pm_taker_fee(pd),
        hl_settlement_fee,
        "PM team-YES covers a 90-minute win, PM draw-YES covers a drawn tie, "
        "and HL advance-NO covers opponent advancement.",
    )
    return {
        "basis": "HL ET+pens advancement versus PM 90-minute 1X2",
        "settlement_fee_verified": hl_settlement_fee is not None,
        "buy_superset": first.to_dict(),
        "buy_cover": second.to_dict(),
    }

