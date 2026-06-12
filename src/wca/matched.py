"""Matched-betting calculators for risk-free bookmaker promo extraction.

This module is **pure math, no IO**.  It implements the standard matched-betting
formulas for locking guaranteed value out of new-customer bookmaker offers by
*backing* a selection at the bookmaker and *laying* the same selection on a
betting exchange (Smarkets, Betfair, Matchbook, ...), so the net outcome is the
same regardless of the sporting result.

Why this exists (and why it is kept separate)
----------------------------------------------
The value extracted here is **risk-free promo money**, not model edge.  It must
never be mixed into the closing-line-value (CLV) experiment recorded in
``wca.ledger`` — see :mod:`wca.offers` for the separate tracker.  This file only
does arithmetic; it has no knowledge of the database.

The exchange concepts
---------------------
* **Back stake** — what you place at the bookmaker on a selection winning.
* **Lay stake** — what you place on the exchange *against* the same selection.
* **Liability** — the most the exchange can take from you if the lay loses
  (i.e. the selection wins): ``lay_stake * (lay_odds - 1)``.
* **Commission** — exchanges charge a commission as a fraction of *net*
  exchange winnings, and only on the **lay-win** side (when your lay bet wins).
  Smarkets is 2% (0% with a COMMFREE promo), Betfair 6% (basic plan 2%),
  Matchbook 2%.

Formulas (standard, exact)
--------------------------
Qualifying (normal, stake-returned) bet::

    lay_stake = back_stake * back_odds / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1)
    if back wins:  profit = back_stake*(back_odds-1) - lay_stake*(lay_odds-1)
    if lay  wins:  profit = lay_stake*(1 - commission) - back_stake

The two profits are ~equal — the (usually small, negative) *qualifying loss*.

Stake-not-returned (SNR) free bet — the bookmaker keeps your free-bet stake, so
you only win the profit portion ``free_stake*(back_odds-1)``::

    lay_stake = free_stake*(back_odds-1) / (lay_odds - commission)
    locked_profit = free_stake*(back_odds-1) - lay_stake*(lay_odds-1)
                  == lay_stake*(1 - commission)        (lay-win side)
    retention_pct = locked_profit / free_stake

Stake-returned (SR) free bet — rare; the free-bet stake *is* returned with
winnings, so it behaves like a qualifying bet but with zero personal back stake
at risk (the bookmaker funds the back side)::

    lay_stake = free_stake * back_odds / (lay_odds - commission)
    locked_profit = free_stake*back_odds - lay_stake*(lay_odds-1)
                  == lay_stake*(1 - commission)        (lay-win side)
    retention_pct = locked_profit / free_stake
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


# ---------------------------------------------------------------------------
# Lay-commission lookup.
# ---------------------------------------------------------------------------

_VENUE_COMMISSION: Dict[str, float] = {
    "smarkets": 0.02,
    "smarkets_commfree": 0.0,
    "betfair": 0.06,
    "betfair_basic": 0.02,
    "matchbook": 0.02,
}


def best_lay_commission(venue: str) -> float:
    """Return the lay commission (a fraction) for a named exchange venue.

    Recognised venues (case-insensitive):

    ======================  ============
    venue                   commission
    ======================  ============
    ``smarkets``            0.02
    ``smarkets_commfree``   0.00
    ``betfair``             0.06
    ``betfair_basic``       0.02
    ``matchbook``           0.02
    ======================  ============

    Raises
    ------
    KeyError
        If ``venue`` is not one of the recognised names.
    """
    key = venue.strip().lower()
    if key not in _VENUE_COMMISSION:
        raise KeyError(
            "unknown venue %r; choose from %s"
            % (venue, ", ".join(sorted(_VENUE_COMMISSION)))
        )
    return _VENUE_COMMISSION[key]


# ---------------------------------------------------------------------------
# Result container.
# ---------------------------------------------------------------------------


@dataclass
class MatchResult:
    """The outcome of a single matched-bet calculation.

    Money fields are held at full floating-point precision; use
    :meth:`as_dict` to obtain a dict with money rounded to 2 decimal places for
    display/serialisation.

    Attributes
    ----------
    lay_stake:
        Amount to lay on the exchange.
    liability:
        Exchange liability ``lay_stake * (lay_odds - 1)`` — the most the
        exchange can take if the lay loses (the backed selection wins).
    profit_if_back_wins:
        Net P&L across both books if the backed selection wins.
    profit_if_lay_wins:
        Net P&L across both books if the backed selection loses.
    worst_case:
        ``min(profit_if_back_wins, profit_if_lay_wins)`` — the guaranteed
        floor.  For a qualifying bet this is the (usually negative) qualifying
        loss; for a free bet the two profits are equal and this is the locked
        profit.
    rating:
        ``worst_case / reference_stake``.  For a qualifying bet the reference
        is the back stake, so ``rating`` is the qualifying loss as a fraction
        of stake (small negative).  For a free bet it is ``retention_pct``.
    back_stake:
        The back stake used in the calculation (the personal stake at the
        bookmaker; ``0`` cash-at-risk for SNR free bets where the free-bet
        token funds the back side).
    locked_profit:
        For free bets, the guaranteed amount won regardless of result.  For
        qualifying bets this mirrors ``worst_case`` (the qualifying loss).
    retention_pct:
        For free bets, ``locked_profit / free_stake`` — the fraction of the
        free-bet face value converted to cash.  ``None`` for qualifying bets.
    """

    lay_stake: float
    liability: float
    profit_if_back_wins: float
    profit_if_lay_wins: float
    worst_case: float
    rating: float
    back_stake: float = 0.0
    locked_profit: float = 0.0
    retention_pct: "float | None" = None

    def as_dict(self) -> Dict[str, object]:
        """Return a dict with all money/ratio fields rounded for display.

        Money fields are rounded to 2dp; ``rating`` and ``retention_pct`` are
        rounded to 4dp (they are fractions).  Internal attributes keep full
        precision — only this view is rounded.
        """
        d: Dict[str, object] = {
            "lay_stake": round(self.lay_stake, 2),
            "liability": round(self.liability, 2),
            "profit_if_back_wins": round(self.profit_if_back_wins, 2),
            "profit_if_lay_wins": round(self.profit_if_lay_wins, 2),
            "worst_case": round(self.worst_case, 2),
            "rating": round(self.rating, 4),
            "back_stake": round(self.back_stake, 2),
            "locked_profit": round(self.locked_profit, 2),
            "retention_pct": (
                round(self.retention_pct, 4)
                if self.retention_pct is not None
                else None
            ),
        }
        return d


# ---------------------------------------------------------------------------
# Input guards.
# ---------------------------------------------------------------------------


def _validate(back_odds: float, lay_odds: float, commission: float) -> None:
    if not (back_odds > 1.0):
        raise ValueError("back_odds must be > 1.0, got %r" % back_odds)
    if not (lay_odds > 1.0):
        raise ValueError("lay_odds must be > 1.0, got %r" % lay_odds)
    if not (0.0 <= commission < 1.0):
        raise ValueError("commission must be in [0, 1), got %r" % commission)


# ---------------------------------------------------------------------------
# Calculators.
# ---------------------------------------------------------------------------


def qualifying_bet(
    back_odds: float,
    lay_odds: float,
    back_stake: float,
    commission: float = 0.0,
) -> MatchResult:
    """Calculate the lay for a *qualifying* (normal, stake-returned) bet.

    A qualifying bet is a real-money back bet placed to unlock a promo (e.g.
    "bet 10 get 30 in free bets").  Backing at the bookie and laying on the
    exchange locks the net result to a small *qualifying loss* regardless of
    outcome.

    Parameters
    ----------
    back_odds, lay_odds:
        Decimal odds at the bookmaker (back) and exchange (lay); both > 1.
    back_stake:
        Real-money stake placed at the bookmaker.
    commission:
        Exchange commission as a fraction in [0, 1); charged on net exchange
        winnings (lay-win side only).

    Returns
    -------
    MatchResult
        ``rating`` is ``worst_case / back_stake`` — the qualifying loss as a
        fraction of stake (a small negative number for typical close prices).
    """
    _validate(back_odds, lay_odds, commission)
    if back_stake <= 0:
        raise ValueError("back_stake must be > 0, got %r" % back_stake)

    lay_stake = back_stake * back_odds / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1.0)

    profit_if_back_wins = back_stake * (back_odds - 1.0) - lay_stake * (lay_odds - 1.0)
    profit_if_lay_wins = lay_stake * (1.0 - commission) - back_stake

    worst_case = min(profit_if_back_wins, profit_if_lay_wins)
    rating = worst_case / back_stake

    return MatchResult(
        lay_stake=lay_stake,
        liability=liability,
        profit_if_back_wins=profit_if_back_wins,
        profit_if_lay_wins=profit_if_lay_wins,
        worst_case=worst_case,
        rating=rating,
        back_stake=back_stake,
        locked_profit=worst_case,
        retention_pct=None,
    )


def free_bet_snr(
    back_odds: float,
    lay_odds: float,
    free_stake: float,
    commission: float = 0.0,
) -> MatchResult:
    """Calculate the lay for a stake-not-returned (SNR) free bet.

    With an SNR free bet the bookmaker keeps the free-bet token's face value —
    if the back wins you receive only the *profit* portion
    ``free_stake*(back_odds-1)``.  There is no personal cash on the back side,
    so the only money at risk is the exchange liability, which is covered by
    the back-win profit.  The result is a guaranteed locked profit either way.

    Parameters
    ----------
    back_odds, lay_odds:
        Decimal odds at the bookmaker (back) and exchange (lay); both > 1.
    free_stake:
        Face value of the free-bet token.
    commission:
        Exchange commission as a fraction in [0, 1).

    Returns
    -------
    MatchResult
        ``profit_if_back_wins`` equals ``profit_if_lay_wins`` to ~1e-9;
        ``locked_profit`` is that guaranteed amount and
        ``retention_pct = locked_profit / free_stake`` (typically ~0.7-0.85).
    """
    _validate(back_odds, lay_odds, commission)
    if free_stake <= 0:
        raise ValueError("free_stake must be > 0, got %r" % free_stake)

    lay_stake = free_stake * (back_odds - 1.0) / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1.0)

    # Back wins: collect the free-bet profit (stake not returned) less liability.
    profit_if_back_wins = free_stake * (back_odds - 1.0) - lay_stake * (lay_odds - 1.0)
    # Lay wins: keep the lay stake net of commission; nothing risked on the back.
    profit_if_lay_wins = lay_stake * (1.0 - commission)

    worst_case = min(profit_if_back_wins, profit_if_lay_wins)
    locked_profit = worst_case
    retention_pct = locked_profit / free_stake
    # rating mirrors retention for free bets.
    rating = retention_pct

    return MatchResult(
        lay_stake=lay_stake,
        liability=liability,
        profit_if_back_wins=profit_if_back_wins,
        profit_if_lay_wins=profit_if_lay_wins,
        worst_case=worst_case,
        rating=rating,
        back_stake=0.0,
        locked_profit=locked_profit,
        retention_pct=retention_pct,
    )


def free_bet_sr(
    back_odds: float,
    lay_odds: float,
    free_stake: float,
    commission: float = 0.0,
) -> MatchResult:
    """Calculate the lay for a stake-*returned* (SR) free bet (rare).

    With an SR free bet the token's face value is returned alongside winnings
    if the back wins, so you collect ``free_stake*back_odds``.  Because the
    bookmaker funds the back side, no personal cash is at risk; the lay
    liability is covered by the back-win return.  Retention is higher than the
    SNR case (the returned stake adds roughly ``free_stake`` of value).

    Parameters
    ----------
    back_odds, lay_odds:
        Decimal odds at the bookmaker (back) and exchange (lay); both > 1.
    free_stake:
        Face value of the free-bet token.
    commission:
        Exchange commission as a fraction in [0, 1).

    Returns
    -------
    MatchResult
        ``profit_if_back_wins`` equals ``profit_if_lay_wins`` to ~1e-9;
        ``retention_pct = locked_profit / free_stake`` (typically > 1.0).
    """
    _validate(back_odds, lay_odds, commission)
    if free_stake <= 0:
        raise ValueError("free_stake must be > 0, got %r" % free_stake)

    lay_stake = free_stake * back_odds / (lay_odds - commission)
    liability = lay_stake * (lay_odds - 1.0)

    # Back wins: collect full return (stake returned) less liability.
    profit_if_back_wins = free_stake * back_odds - lay_stake * (lay_odds - 1.0)
    # Lay wins: keep the lay stake net of commission.
    profit_if_lay_wins = lay_stake * (1.0 - commission)

    worst_case = min(profit_if_back_wins, profit_if_lay_wins)
    locked_profit = worst_case
    retention_pct = locked_profit / free_stake
    rating = retention_pct

    return MatchResult(
        lay_stake=lay_stake,
        liability=liability,
        profit_if_back_wins=profit_if_back_wins,
        profit_if_lay_wins=profit_if_lay_wins,
        worst_case=worst_case,
        rating=rating,
        back_stake=0.0,
        locked_profit=locked_profit,
        retention_pct=retention_pct,
    )
