"""Polymarket exposure review: trim / keep / add proposals under a stated rule.

This encodes the desk rule for managing an existing Polymarket book:

    Take +EV trades but **not all longshots**. Look at close-to-moneyline (and
    moneyline) markets first, and prefer selections whose fair value is **far**
    from the market price (large mispricing) over those that have converged.

Each open position is scored against a model probability (e.g. the advancement
simulator's fair probability for the same market) and classified:

* ``TRIM``  — exit or cut the position because the edge is gone (model now agrees
  with, or disfavours, the price) **or** it is a longshot the rule deprioritises.
* ``KEEP``  — a healthy, near-moneyline edge worth holding.
* ``ADD``   — a near-moneyline position with a large remaining edge: scale up.
* ``REVIEW``— no model probability available; flag for manual pricing.

The module is pure and IO-free (load positions/model probs elsewhere and pass
them in) so the rule is unit-testable. :func:`format_proposals` renders a
Telegram-ready message and :func:`ping_proposals` sends it (or dry-prints when
no bot token is configured — it never silently fails to a live send).

It scores only the markets the platform actually models (1X2/advancement-style
binary markets here). It deliberately makes **no** claim about shots-on-target,
cards or assists, which the platform does not price.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Sequence

# Rule thresholds (decimal/probability units). Tunable, with rationale:
#   * a longshot is anything the market prices below ~15% (decimal > ~6.7): the
#     rule says "not all longshots", so these are trimmed unless exceptional.
#   * "close to moneyline" is an implied probability in [0.35, 0.65].
#   * an edge below ~5% is treated as converged (mispricing realised) -> trim.
#   * an edge at or above ~12% on a near-moneyline market is an ADD candidate.
DEFAULT_LONGSHOT_MAX_IMPLIED = 0.15
DEFAULT_MONEYLINE_BAND = (0.35, 0.65)
DEFAULT_MIN_EDGE = 0.05
DEFAULT_ADD_EDGE = 0.12


class Action(str, Enum):
    TRIM = "TRIM"
    KEEP = "KEEP"
    ADD = "ADD"
    REVIEW = "REVIEW"


@dataclass
class Position:
    """An open Polymarket position to be reviewed."""

    market: str
    selection: str
    stake: float
    decimal_odds: float
    model_prob: Optional[float] = None
    currency: str = "USD"

    @property
    def implied_prob(self) -> float:
        return 1.0 / self.decimal_odds if self.decimal_odds > 0 else 0.0

    @property
    def edge(self) -> Optional[float]:
        """Model edge ``model_prob * decimal_odds - 1`` (None if no model prob)."""
        if self.model_prob is None or self.decimal_odds <= 0:
            return None
        return self.model_prob * self.decimal_odds - 1.0

    @property
    def moneyline_distance(self) -> float:
        """Distance of the market price from an even-money (0.5) line."""
        return abs(self.implied_prob - 0.5)


@dataclass
class Proposal:
    """A recommended action for one position."""

    position: Position
    action: Action
    reason: str
    suggested_stake: float  # target stake after the action (0 = full exit)

    @property
    def stake_change(self) -> float:
        return self.suggested_stake - self.position.stake


def classify(
    pos: Position,
    *,
    longshot_max_implied: float = DEFAULT_LONGSHOT_MAX_IMPLIED,
    moneyline_band: Sequence[float] = DEFAULT_MONEYLINE_BAND,
    min_edge: float = DEFAULT_MIN_EDGE,
    add_edge: float = DEFAULT_ADD_EDGE,
    trim_fraction: float = 0.5,
) -> Proposal:
    """Classify one position under the desk rule.

    Order of checks (first match wins):

    1. No model probability -> ``REVIEW``.
    2. Edge <= 0 (model disfavours the price) -> ``TRIM`` to 0 (full exit).
    3. Longshot (implied < ``longshot_max_implied``) -> ``TRIM`` to 0 even when
       nominally +EV: the rule explicitly avoids loading up on longshots.
    4. Edge < ``min_edge`` (converged toward fair) -> ``TRIM`` by
       ``trim_fraction`` (the mispricing is mostly realised; bank some).
    5. Near-moneyline AND edge >= ``add_edge`` -> ``ADD`` (the rule's sweet spot:
       close to moneyline, large remaining mispricing).
    6. Otherwise -> ``KEEP``.
    """
    lo, hi = float(moneyline_band[0]), float(moneyline_band[1])
    edge = pos.edge

    if edge is None:
        return Proposal(pos, Action.REVIEW, "no model price — value manually", pos.stake)

    if edge <= 0.0:
        return Proposal(
            pos, Action.TRIM,
            "model edge %+.1f%% <= 0: model no longer favours this — exit" % (edge * 100),
            0.0,
        )

    if pos.implied_prob < float(longshot_max_implied):
        return Proposal(
            pos, Action.TRIM,
            "longshot (%.0f%% implied) — rule deprioritises longshots; exit despite %+.1f%% edge"
            % (pos.implied_prob * 100, edge * 100),
            0.0,
        )

    if edge < float(min_edge):
        return Proposal(
            pos, Action.TRIM,
            "edge %+.1f%% has converged toward fair — bank %.0f%%"
            % (edge * 100, trim_fraction * 100),
            pos.stake * (1.0 - float(trim_fraction)),
        )

    near_moneyline = lo <= pos.implied_prob <= hi
    if near_moneyline and edge >= float(add_edge):
        return Proposal(
            pos, Action.ADD,
            "near-moneyline (%.0f%% implied) with %+.1f%% edge — large mispricing, scale up"
            % (pos.implied_prob * 100, edge * 100),
            pos.stake,  # caller decides the add size; flagged as a candidate
        )

    return Proposal(
        pos, Action.KEEP,
        "healthy edge %+.1f%% (%.0f%% implied) — hold" % (edge * 100, pos.implied_prob * 100),
        pos.stake,
    )


def propose(
    positions: Sequence[Position], **kwargs
) -> List[Proposal]:
    """Classify every position; ranked TRIM/ADD first, then by |edge| descending.

    Sorting surfaces the actionable items (the rule's "further away rather than
    closer" — largest mispricing first) at the top of the message.
    """
    proposals = [classify(p, **kwargs) for p in positions]
    order = {Action.TRIM: 0, Action.ADD: 1, Action.KEEP: 2, Action.REVIEW: 3}

    def sort_key(pr: Proposal):
        e = pr.position.edge
        return (order[pr.action], -(abs(e) if e is not None else -1.0))

    return sorted(proposals, key=sort_key)


def format_proposals(
    proposals: Sequence[Proposal], *, title: str = "Polymarket exposure review"
) -> str:
    """Render proposals as a Telegram-ready Markdown message."""
    if not proposals:
        return "*%s*\nNo open Polymarket positions to review." % title

    icon = {
        Action.TRIM: "✂️", Action.ADD: "➕", Action.KEEP: "✅", Action.REVIEW: "❓",
    }
    lines = ["*%s*" % title, "_Rule: +EV, not longshots; near-moneyline first, "
             "biggest mispricing first._", ""]
    total_trim = 0.0
    for pr in proposals:
        p = pr.position
        mkt = p.market if len(p.market) <= 48 else p.market[:47] + "…"
        head = "%s *%s* — %s `%s`" % (icon[pr.action], pr.action.value, mkt, p.selection)
        detail = "    %s @ %.2f | stake %.0f → %.0f | %s" % (
            p.currency, p.decimal_odds, p.stake, pr.suggested_stake, pr.reason,
        )
        lines.append(head)
        lines.append(detail)
        if pr.action == Action.TRIM:
            total_trim += -pr.stake_change
    if total_trim > 0:
        lines.append("")
        lines.append("_Total proposed trim: ~%.0f staked released._" % total_trim)
    lines.append("")
    lines.append("⚠️ _Proposals priced off the model snapshot — verify live "
                 "Polymarket prices before executing._")
    return "\n".join(lines)


def ping_proposals(
    text: str,
    *,
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    dry_run: bool = True,
) -> bool:
    """Send the proposals to the Telegram bot, or dry-print.

    Returns ``True`` if a live send was made. With ``dry_run=True`` (the default)
    or a missing token/chat_id it prints the message and returns ``False`` — it
    never silently no-ops a requested live send without saying so.
    """
    import os

    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if dry_run or not token or not chat_id:
        why = "dry-run" if dry_run else "no TELEGRAM_BOT_TOKEN/CHAT_ID"
        print("[ping_proposals: %s — not sent]\n%s" % (why, text))
        return False

    from wca.bot.telegram import TelegramClient

    client = TelegramClient(token=token)
    client.send_message(chat_id, text)
    return True
