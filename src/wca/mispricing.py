"""Unified mispricing, lock-in and venue-aware display for every command.

The desk's commands (``/next``, ``/card``, ``/accas``, ``/boost``, ``/pm``, the
EV/arb scanners) all answer three questions and should answer them the *same*
way:

1. **Where is the market mispriced vs our model fair value?** — :func:`assess`
   turns a model probability + a venue quote into a signed edge.
2. **Can the mispricing be locked in?** — :func:`lock_in` looks at the best
   price for every outcome of a market and, when the implied probabilities sum
   to **less than 100%**, returns the dutch/arb stakes that bank a guaranteed
   profit. This is the same maths whether the legs sit on different venues
   (cross-venue arb) or — rarely — on the same venue (two mispriced orders).
3. **How is it shown?** — :class:`Quote` carries the venue *kind* so display is
   consistent: **sportsbook/exchange → £ stake + decimal odds**; **Polymarket →
   $ stake + cent share price** (``58¢`` = 58% = 0.58 decimal-implied).

The coherence guard (:func:`coherence`) is the safety net behind requirement 1:
a set of best prices that sums under 100% is *either* a real lock-in *or* stale
/ non-simultaneous data — it must never be rendered as three independent +EV
value bets (the ``/next`` all-outcomes-+EV bug). Commands call :func:`coherence`
and surface a lock-in or a warning accordingly.

Pure and IO-free except :func:`lock_in`'s optional FX lookup (injectable).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .arb import _arb_from_net, pm_yes_to_decimal

# Venue kinds drive both fee handling and display units.
SPORTSBOOK = "sportsbook"
EXCHANGE = "exchange"
POLYMARKET = "polymarket"

# A book is "coherent" if its outcome probabilities sum to >= 1 - tol. The
# threshold is wide enough that a near-fair Polymarket book (~99.5%, just
# mid-price noise) is treated as normal, while a real gap (the 82.6% /next bug,
# or a true cross-venue arb) trips the lock-in/stale path.
COHERENCE_TOL = 0.02


# ---------------------------------------------------------------------------
# Quote: one price for one outcome at one venue, in canonical units.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    """A single venue price for one outcome, stored as an implied probability.

    ``prob`` is the *raw* implied probability (no de-vig) — ``1/decimal`` for a
    sportsbook, or the share price for Polymarket. ``kind`` selects display
    units and the lay/fee semantics.
    """

    venue: str          # display label, e.g. "Bet365", "Betfair", "Polymarket"
    kind: str           # SPORTSBOOK | EXCHANGE | POLYMARKET
    prob: float         # implied probability in (0, 1)

    @property
    def decimal(self) -> float:
        return 1.0 / self.prob if self.prob > 0 else float("inf")

    @property
    def cents(self) -> float:
        return self.prob * 100.0

    @property
    def is_pm(self) -> bool:
        return self.kind == POLYMARKET


def from_decimal(venue: str, decimal_odds: float, kind: str = SPORTSBOOK) -> Quote:
    """Quote from sportsbook/exchange decimal odds."""
    if decimal_odds <= 1.0:
        raise ValueError("decimal odds must exceed 1.0")
    return Quote(venue=venue, kind=kind, prob=1.0 / float(decimal_odds))


def from_pm_price(price: float, venue: str = "Polymarket") -> Quote:
    """Quote from a Polymarket share price (probability in (0, 1))."""
    if not (0.0 < float(price) < 1.0):
        raise ValueError("Polymarket price must be in (0, 1)")
    return Quote(venue=venue, kind=POLYMARKET, prob=float(price))


# ---------------------------------------------------------------------------
# Venue-aware display.
# ---------------------------------------------------------------------------


def fmt_price(q: Quote) -> str:
    """Price string in the venue's native units.

    Polymarket → ``"58¢"`` (cent share price). Sportsbook/exchange → ``"1.72"``
    (decimal odds).
    """
    if q.is_pm:
        return "%d¢" % round(q.cents)
    return "%.2f" % q.decimal


def fmt_size(kind: str, amount: float) -> str:
    """Stake/size string: ``$`` for Polymarket, ``£`` otherwise."""
    sym = "$" if kind == POLYMARKET else "£"
    return "%s%.2f" % (sym, amount)


def fmt_fair(model_prob: float, kind: str) -> str:
    """Model fair value in the venue's native units (decimal or cents)."""
    if model_prob <= 0:
        return "—"
    if kind == POLYMARKET:
        return "%d¢" % round(model_prob * 100.0)
    return "%.2f" % (1.0 / model_prob)


# ---------------------------------------------------------------------------
# Mispricing: model fair vs a venue quote.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mispricing:
    model_prob: float
    quote: Quote
    edge: float          # model_prob * decimal - 1  (signed)

    @property
    def is_value(self) -> bool:
        return self.edge > 0.0

    @property
    def fair_str(self) -> str:
        return fmt_fair(self.model_prob, self.quote.kind)

    def line(self) -> str:
        """One-line render: ``Canada 51.5% | fair 58¢ | Polymarket 43¢ +21.2%``."""
        return "%s | fair %s | %s %s %+.1f%%" % (
            "%.1f%%" % (self.model_prob * 100.0),
            self.fair_str,
            self.quote.venue,
            fmt_price(self.quote),
            self.edge * 100.0,
        )


def assess(model_prob: float, quote: Quote) -> Mispricing:
    """Edge of taking ``quote`` given the model probability ``model_prob``."""
    edge = model_prob * quote.decimal - 1.0
    return Mispricing(model_prob=float(model_prob), quote=quote, edge=edge)


# ---------------------------------------------------------------------------
# Coherence + lock-in across the outcomes of one market.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Coherence:
    implied_sum: float           # sum of best-price implied probs across outcomes
    venues: Tuple[str, ...]      # the venue behind each best price

    @property
    def is_lockin(self) -> bool:
        return self.implied_sum < 1.0 - COHERENCE_TOL

    @property
    def is_normal(self) -> bool:
        return self.implied_sum >= 1.0 - COHERENCE_TOL

    @property
    def single_venue(self) -> bool:
        return len(set(self.venues)) == 1


def coherence(best_quotes: Sequence[Quote]) -> Coherence:
    """Coherence of the best price per outcome of an exhaustive market.

    ``best_quotes`` must be one quote per mutually-exclusive, exhaustive outcome
    (e.g. home/draw/away). ``implied_sum`` under 100% means a lock-in is
    available *if the prices are simultaneously real* — the caller decides
    whether to present it as an opportunity or a stale-data warning.
    """
    s = sum(q.prob for q in best_quotes)
    return Coherence(implied_sum=s, venues=tuple(q.venue for q in best_quotes))


@dataclass(frozen=True)
class LockInLeg:
    quote: Quote
    stake: float          # in the leg's own currency (£ books/exchange, $ PM)

    def line(self) -> str:
        return "back %s @ %s — %s" % (
            self.quote.venue, fmt_price(self.quote), fmt_size(self.quote.kind, self.stake),
        )


@dataclass(frozen=True)
class LockIn:
    legs: Tuple[LockInLeg, ...]
    profit_pct: float           # guaranteed return on total staked
    total_gbp: float            # total outlay expressed in GBP


def lock_in(
    best_quotes: Sequence[Quote],
    bankroll_gbp: float,
    *,
    usd_per_gbp: float = 1.33,
    fee_for: Optional[Callable[[Quote], float]] = None,
) -> Optional[LockIn]:
    """Dutch/arb stakes that lock in profit across an outcome's best prices.

    Sizes each leg so every outcome returns the same payout, scaled to spend
    ``bankroll_gbp`` total (converting Polymarket legs to USD at ``usd_per_gbp``).
    Returns ``None`` when no risk-free edge exists (implied sum ≥ 100% net of
    fees). Net odds use Polymarket's taker fee and, by default, no book fee
    (pass ``fee_for`` to apply exchange commission).
    """
    if not best_quotes:
        return None
    net = []
    for q in best_quotes:
        if q.is_pm:
            net.append(pm_yes_to_decimal(q.prob))
        else:
            d = q.decimal
            c = fee_for(q) if fee_for else 0.0
            net.append(1.0 + (d - 1.0) * (1.0 - c))
    res = _arb_from_net(net)
    if res is None:
        return None
    fracs = res["stake_fractions"]
    legs: List[LockInLeg] = []
    for q, f in zip(best_quotes, fracs):
        stake_gbp = f * bankroll_gbp
        stake = stake_gbp * usd_per_gbp if q.is_pm else stake_gbp
        legs.append(LockInLeg(quote=q, stake=stake))
    return LockIn(legs=tuple(legs), profit_pct=res["profit_pct"], total_gbp=bankroll_gbp)


def coherence_note(
    best_quotes: Sequence[Quote],
    bankroll_gbp: float = 100.0,
    *,
    usd_per_gbp: float = 1.33,
) -> str:
    """Human-readable coherence verdict for a market's best prices.

    Returns a lock-in instruction when the book sums under 100%, a normal-vig
    note otherwise. Same-venue sub-100% books are flagged as *verify live*
    because they are usually stale/non-simultaneous rather than a true arb.
    """
    cov = coherence(best_quotes)
    if cov.is_normal:
        return "best prices sum %.1f%% — no lock-in; take the value leg(s)" % (
            cov.implied_sum * 100.0
        )
    head = "⚠️ best prices imply %.1f%% (<100%%)" % (cov.implied_sum * 100.0)
    # Single-venue sub-100% is almost always stale/non-simultaneous data (you
    # cannot take all outcome mids at once on one book) — warn, don't instruct.
    if cov.single_venue:
        return head + " — all on %s: STALE/non-simultaneous, verify live before trusting any edge" % (
            best_quotes[0].venue
        )
    # Cross-venue sub-100% is a real lock-in: show fee-aware stakes.
    li = lock_in(best_quotes, bankroll_gbp, usd_per_gbp=usd_per_gbp)
    if li is None:
        return head + " — cross-venue, but gap closes after fees: no risk-free lock-in"
    lines = [head + " — cross-venue LOCK-IN, guaranteed +%.1f%% on %s:" % (
        li.profit_pct * 100.0, fmt_size(SPORTSBOOK, bankroll_gbp))]
    for leg in li.legs:
        lines.append("   • " + leg.line())
    return "\n".join(lines)
