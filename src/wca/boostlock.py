"""Boosted same-game-builder lock: construct + hedge for a guaranteed profit.

The extraction (user, 2026-07-02): a sportsbook "X% winnings boost" on a
same-game bet builder (min legs / min combined odds per the promo terms) can
be locked risk-free WHEN the builder's legs are chosen so the joint outcome is
EQUIVALENT to one exchange-layable market:

* anchor leg  — Team FT win (90');
* implied legs — picked so the anchor winning implies them, e.g.
  "Team over 0.5 team goals" (any win has >=1 goal) and
  "Team double chance" (win implies win-or-draw).

Then builder wins <=> Team wins in 90', and laying Team on the exchange is an
EXACT hedge (no SGM correlation residue). If the book's SGM engine prunes a
fully-implied leg or prices the combo under the promo's minimum odds, the
caller must swap in the least-distorting real leg (e.g. opponent under 3.5
team goals) and accept the quantified residual scenario — this module flags
that case, it never hides it.

Money math (decimal odds ``o``, boost fraction ``b``, lay odds ``L``,
exchange commission ``c`` on net winnings, back stake ``B``)::

    builder win : +B(o-1)(1+b) - S(L-1)
    builder lose: -B + S(1-c)

Equal-profit lay stake  S = B((o-1)(1+b) + 1) / (L - c)
Locked profit (both ways) = S(1-c) - B

Free-bet variant (stake not returned, SNR): replace the win branch with
``B(o-1)(1+b) - S(L-1)`` (identical — stake was never returned in winnings
boosts either; boosts pay on WINNINGS, the stake IS returned) — for an SNR
free bet use ``B(o-1)`` winnings and no stake loss on the lose branch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class BoostLock:
    """A fully-specified boosted-builder lock."""

    fixture: str
    anchor: str                 # the layable outcome ("Switzerland FT win")
    legs: List[str]             # builder legs (anchor + implied legs)
    builder_odds: float         # book's quoted combined decimal odds
    boost_frac: float           # 0.5 = 50% winnings boost
    back_stake: float
    lay_odds: float
    lay_commission: float
    lay_stake: float
    locked_profit: float
    profit_pct_of_stake: float
    equivalent: bool            # True when legs are jointly implied by anchor
    notes: str = ""


def equal_profit_lay(back_stake: float, builder_odds: float, boost_frac: float,
                     lay_odds: float, lay_commission: float = 0.0) -> tuple:
    """(lay_stake, locked_profit) for the equal-profit hedge. Raises ValueError
    on degenerate inputs (odds <= 1, negative stake/boost, commission >= lay)."""
    B, o, b, L, c = (float(back_stake), float(builder_odds), float(boost_frac),
                     float(lay_odds), float(lay_commission))
    if B <= 0 or o <= 1.0 or L <= 1.0 or b < 0 or not (0 <= c < 1) or L <= c:
        raise ValueError("degenerate boost-lock inputs")
    win_return = B * (o - 1.0) * (1.0 + b)
    lay_stake = (win_return + B) / (L - c)
    locked = lay_stake * (1.0 - c) - B
    # Invariant: both branches pay the same to the penny.
    win_branch = win_return - lay_stake * (L - 1.0)
    assert abs(win_branch - locked) < 1e-9
    return lay_stake, locked


def implied_leg_template(team: str, opponent: str) -> List[str]:
    """The standard 3-leg equivalent builder for a 90' win anchor."""
    return [
        "%s to win (full time)" % team,
        "%s over 0.5 team goals" % team,
        "%s double chance (win or draw)" % team,
    ]


def build_lock(fixture: str, team: str, opponent: str, builder_odds: float,
               lay_odds: float, back_stake: float, boost_frac: float = 0.5,
               lay_commission: float = 0.0, min_combined_odds: float = 2.0,
               extra_leg: Optional[str] = None) -> BoostLock:
    """Assemble a :class:`BoostLock` for a win-anchored equivalent builder.

    ``extra_leg`` marks a NON-implied leg the book forced in (combo priced
    under the promo minimum, or the SGM engine pruned an implied leg) —
    the lock is then approximate and ``equivalent=False``.
    """
    legs = implied_leg_template(team, opponent)
    notes = ""
    equivalent = True
    if extra_leg:
        legs.append(extra_leg)
        equivalent = False
        notes = ("NON-implied leg added (%s): hedge is approximate — the "
                 "builder can lose while the lay also loses if that leg fails "
                 "on a %s win. Quantify before staking." % (extra_leg, team))
    if builder_odds < min_combined_odds:
        notes = (notes + " " if notes else "") + (
            "Quoted combo %.2f is BELOW the promo minimum %.2f — add the "
            "least-distorting real leg and re-quote."
            % (builder_odds, min_combined_odds)
        )
    lay_stake, locked = equal_profit_lay(
        back_stake, builder_odds, boost_frac, lay_odds, lay_commission
    )
    return BoostLock(
        fixture=fixture, anchor="%s FT win (90')" % team, legs=legs,
        builder_odds=float(builder_odds), boost_frac=float(boost_frac),
        back_stake=float(back_stake), lay_odds=float(lay_odds),
        lay_commission=float(lay_commission),
        lay_stake=round(lay_stake, 2), locked_profit=round(locked, 2),
        profit_pct_of_stake=round(100.0 * locked / float(back_stake), 2),
        equivalent=equivalent, notes=notes.strip(),
    )


def format_lock(lock: BoostLock) -> str:
    """Telegram/terminal-ready plan."""
    lines = [
        "*SGM BOOST LOCK — %s*" % lock.fixture,
        "Builder (%d legs, combined %.2f, %s%% winnings boost):"
        % (len(lock.legs), lock.builder_odds, ("%g" % (lock.boost_frac * 100))),
    ]
    for i, leg in enumerate(lock.legs, 1):
        lines.append("  %d. %s" % (i, leg))
    lines.append("BACK: $%.2f builder @ %.2f (equiv. anchor: %s)"
                 % (lock.back_stake, lock.builder_odds, lock.anchor))
    lines.append("LAY : $%.2f %s @ %.2f (comm %.0f%%)"
                 % (lock.lay_stake, lock.anchor, lock.lay_odds,
                    lock.lay_commission * 100))
    lines.append("LOCKED: $%.2f both ways (%.1f%% of stake)%s"
                 % (lock.locked_profit, lock.profit_pct_of_stake,
                    "" if lock.equivalent else "  ⚠ APPROXIMATE"))
    if lock.notes:
        lines.append("_%s_" % lock.notes)
    return "\n".join(lines)
