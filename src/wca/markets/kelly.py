"""Kelly-criterion staking for fixed-odds bets.

The Kelly criterion (Kelly, 1956, "A New Interpretation of Information Rate",
*Bell System Technical Journal* 35(4):917-926) maximises the expected
logarithmic growth rate of a bankroll. For a single binary bet at decimal odds
``o`` (net fractional odds ``b = o - 1``) with win probability ``p`` the
growth-optimal fraction of bankroll to stake is

.. math::

    f^* = \\frac{p (b + 1) - 1}{b} = \\frac{p\\,o - 1}{o - 1}.

``f^*`` is positive only when the bet has positive expected value
(``p > 1 / o``); otherwise the optimal stake is zero (you never bet into a
negative edge).

Full Kelly is the variance-maximal growth-optimal bet and is far too
aggressive in practice given parameter uncertainty, so this project uses
*fractional* Kelly (a fixed multiple ``fraction`` of ``f^*``, default a
*quarter* Kelly) together with a hard per-bet cap expressed as a fraction of
bankroll. Fractional Kelly trades a modest amount of long-run growth for a
large reduction in drawdown and is standard practice in quantitative betting;
see MacLean, Thorp & Ziemba (2011), *The Kelly Capital Growth Investment
Criterion*.

A portfolio of several bets placed on the same day exposes the bankroll to
their *combined* outcome. :func:`simultaneous_exposure_scale` caps the total
staked across a same-day slate at a fraction of bankroll, scaling the
individual stakes down proportionally (preserving their relative sizing) when
the slate would otherwise breach the cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np


def edge(p: float, decimal_odds: float) -> float:
    """Expected profit per unit staked: ``p * odds - 1`` (the *edge*).

    Positive when the modelled win probability ``p`` exceeds the odds-implied
    break-even probability ``1 / odds``. This is exactly the numerator of the
    expected-value-per-stake and the sign that gates a Kelly bet.
    """
    o = float(decimal_odds)
    if o <= 1.0:
        raise ValueError("decimal odds must be strictly greater than 1.0")
    return float(p) * o - 1.0


def kelly_fraction(p: float, decimal_odds: float) -> float:
    """Full-Kelly optimal stake as a fraction of bankroll.

    ``f* = (p * (b + 1) - 1) / b`` with ``b = odds - 1``. Returns ``0.0`` when
    the edge is non-positive (never stake into a zero or negative edge).

    Parameters
    ----------
    p:
        Modelled probability of the bet winning, in ``[0, 1]``.
    decimal_odds:
        Decimal odds of the bet (must exceed 1.0).
    """
    prob = float(p)
    if not (0.0 <= prob <= 1.0):
        raise ValueError("probability p must be in [0, 1]")
    o = float(decimal_odds)
    if o <= 1.0:
        raise ValueError("decimal odds must be strictly greater than 1.0")

    b = o - 1.0
    f = (prob * (b + 1.0) - 1.0) / b
    # Equivalent to (p * o - 1) / (o - 1); guard against negative-edge bets.
    if f <= 0.0:
        return 0.0
    return f


def stake(
    p: float,
    odds: float,
    bankroll: float,
    fraction: float = 0.25,
    cap: float = 0.05,
) -> float:
    """Fractional-Kelly stake in currency, hard-capped as a fraction of bankroll.

    Computes the full-Kelly fraction, scales it by ``fraction`` (quarter Kelly
    by default), then clips it to at most ``cap`` of the bankroll before
    converting to a currency amount.

    Parameters
    ----------
    p:
        Modelled win probability.
    odds:
        Decimal odds.
    bankroll:
        Current bankroll in currency.
    fraction:
        Kelly multiplier (e.g. ``0.25`` for quarter Kelly). Must be in
        ``[0, 1]``.
    cap:
        Maximum stake as a fraction of bankroll. Must be in ``[0, 1]``.

    Returns
    -------
    float
        Stake in the same currency units as ``bankroll``; ``0.0`` for a
        non-positive edge or non-positive bankroll.
    """
    if not (0.0 <= float(fraction) <= 1.0):
        raise ValueError("fraction must be in [0, 1]")
    if not (0.0 <= float(cap) <= 1.0):
        raise ValueError("cap must be in [0, 1]")
    bank = float(bankroll)
    if bank <= 0.0:
        return 0.0

    f_full = kelly_fraction(p, odds)
    if f_full <= 0.0:
        return 0.0

    f = f_full * float(fraction)
    f = min(f, float(cap))
    return f * bank


def ev(p: float, odds: float, stake: float) -> float:
    """Expected profit (not return) of a bet in currency.

    For a stake ``s`` at decimal odds ``o`` and win probability ``p``::

        EV = p * (o - 1) * s - (1 - p) * s = (p * o - 1) * s = edge * s

    i.e. the win pays the net profit ``(o - 1) * s`` and a loss forfeits the
    stake ``s``. Positive EV requires a positive edge.
    """
    s = float(stake)
    return edge(p, odds) * s


def simultaneous_exposure_scale(
    stakes: Sequence[float],
    max_total_fraction: float,
    bankroll: float,
) -> np.ndarray:
    """Scale same-day stakes so their total stays within an exposure cap.

    When several bets settle on the same day the bankroll is exposed to their
    combined stake. This scales every stake by a common factor so the total
    does not exceed ``max_total_fraction * bankroll``. If the slate is already
    within the cap the stakes are returned unchanged. Relative sizing between
    bets is always preserved.

    Parameters
    ----------
    stakes:
        Proposed currency stakes (each ``>= 0``).
    max_total_fraction:
        Maximum allowed total exposure as a fraction of bankroll, in
        ``[0, 1]``.
    bankroll:
        Current bankroll in currency.

    Returns
    -------
    numpy.ndarray
        Scaled stakes (a copy). Their sum is at most
        ``max_total_fraction * bankroll`` up to floating point.
    """
    s = np.asarray(stakes, dtype=float).ravel()
    if np.any(s < 0.0):
        raise ValueError("stakes must be non-negative")
    if not np.all(np.isfinite(s)):
        raise ValueError("stakes must be finite")
    if not (0.0 <= float(max_total_fraction) <= 1.0):
        raise ValueError("max_total_fraction must be in [0, 1]")
    bank = float(bankroll)
    if bank <= 0.0:
        return np.zeros_like(s)

    total = float(s.sum())
    if total <= 0.0:
        return s.copy()

    budget = float(max_total_fraction) * bank
    if total <= budget:
        return s.copy()

    scale = budget / total
    return s * scale


# ---------------------------------------------------------------------------
# Pre-registered staking policy: the CLV-gated Kelly ladder.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LadderRung:
    """One rung of the Kelly ladder.

    ``min_settled`` is the number of settled bets *with closing odds recorded*
    required before this rung can be earned; promotion additionally requires
    positive to-date CLV (the rung must be *earned* by evidence, not time).
    """

    min_settled: int
    fraction: float


@dataclass(frozen=True)
class KellyPolicy:
    """Pre-registered, CLV-gated fractional-Kelly ladder.

    Registered 2026-06-11, before the first bet was placed. The rationale is
    documented in ``docs/policy/staking_policy.md``; the short version:

    * The Kelly growth curve ``c (2 - c)`` is flat near the optimum while the
      drawdown curve ``x ** (2/c - 1)`` is exponentially steep, so with noisy
      edge estimates the rational multiplier sits well below 1. With edge
      noise ~1.7x the edge itself (honest for an unbacktested model),
      quarter Kelly is growth-optimal.
    * Bets are selected where model and market disagree most, so estimated
      edges are biased upward (winner's curse); the fraction is shrinkage.
    * The ladder is pre-registered so the multiplier can only rise on CLV
      evidence — never on a hot streak.

    Rules enforced by :meth:`evaluate`:

    * rung 0 (c=0.25) until 50 settled bets with closing odds;
    * rung 1 (c=0.35) once 50+ settled AND to-date CLV > 0;
    * rung 2 (c=0.50) once 100+ settled AND to-date CLV > 0 — the tournament
      ceiling, never exceeded;
    * demotion one rung (floored at rung 0) whenever rolling-50 CLV < 0.
      (Negative CLV at rung 0 is the kill rule's jurisdiction — pause, not
      resize.)
    * while on rung 0, recommendations above ``max_odds_unvalidated`` are
      filtered out entirely: longshots are where selection bias is worst and
      the blend weights are not yet fitted.

    Arbitrage bets are exempt from Kelly sizing altogether (sized to book
    limits) and never count toward the ladder's exposure arithmetic.
    """

    rungs: Tuple[LadderRung, ...] = (
        LadderRung(min_settled=0, fraction=0.25),
        LadderRung(min_settled=50, fraction=0.35),
        LadderRung(min_settled=100, fraction=0.50),
    )
    max_odds_unvalidated: float = 10.0

    def evaluate(
        self,
        n_settled: int,
        clv_to_date: Optional[float],
        rolling50_clv: Optional[float] = None,
    ) -> Tuple[float, int, str]:
        """Return ``(fraction, rung_index, reason)`` for the current evidence.

        Parameters
        ----------
        n_settled:
            Settled bets with closing odds recorded.
        clv_to_date:
            Mean CLV across all such bets (``None`` when there are none).
        rolling50_clv:
            Mean CLV over the most recent 50 such bets (``None`` if fewer
            than 50 exist).
        """
        if n_settled < 0:
            raise ValueError("n_settled must be non-negative")

        rung = 0
        for i, r in enumerate(self.rungs):
            if i == 0:
                continue
            if (
                n_settled >= r.min_settled
                and clv_to_date is not None
                and clv_to_date > 0.0
            ):
                rung = i

        reason = "rung %d earned: %d settled, CLV %s" % (
            rung,
            n_settled,
            ("%.4f" % clv_to_date) if clv_to_date is not None else "n/a",
        )
        if rolling50_clv is not None and rolling50_clv < 0.0 and rung > 0:
            rung -= 1
            reason += "; demoted one rung (rolling-50 CLV %.4f < 0)" % rolling50_clv

        return self.rungs[rung].fraction, rung, reason

    def odds_cap(self, rung: int) -> Optional[float]:
        """Max odds allowed at this rung; ``None`` means uncapped."""
        return self.max_odds_unvalidated if rung == 0 else None
