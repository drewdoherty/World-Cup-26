"""Automatic engine routing for the dev-conductor.

The policy is intentionally Claude-first. Andrew has much more Claude usage
than Codex, and Codex token accounting is less reliable in v0, so automatic
dispatch spends Codex only on obviously small mechanical edits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from wca.conductor.models import Engine


@dataclass(frozen=True)
class RouteDecision:
    engine: Engine
    reason: str


_CLAUDE_FIRST = (
    "background",
    "daemon",
    "long-running",
    "monitor",
    "poll",
    "autopull",
    "telegram",
    "bot",
    "conductor",
    "report",
    "ledger",
    "bankroll",
    "stake",
    "money",
    "polymarket",
    "kalshi",
    "model",
    "kelly",
    "clv",
    "risk",
    "strategy",
    "debug",
    "investigate",
    "research",
    "architecture",
    "refactor",
)

_CODEX_MECHANICAL = (
    "typo",
    "spelling",
    "format",
    "lint",
    "rename",
    "copy edit",
    "copy-edit",
    "wording",
    "comment",
    "one-line",
    "single-line",
    "small mechanical",
    "mechanical",
    "change label",
)


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)


def choose_engine(
    task: str,
    codex_available: bool = True,
    claude_available: bool = True,
) -> RouteDecision:
    """Pick the best engine for an automatic ``/task`` submission.

    Explicit ``/claude`` and ``/codex`` remain available as overrides. This
    function handles only automatic routing: it prefers Claude (to conserve
    scarce Codex) unless the task is a cheap mechanical edit — but it will
    **route around an unavailable engine**. ``claude_available`` /
    ``codex_available`` reflect real engine health (logged-out / capped), so a
    logged-out Claude transparently falls back to a healthy Codex.
    """
    text = " ".join((task or "").lower().split())

    # Preferred engine purely on task shape (mechanical edits are Codex-cheap).
    if _contains_any(text, _CODEX_MECHANICAL):
        preferred, why = Engine.CODEX, "Codex: small mechanical edit"
    elif _contains_any(text, _CLAUDE_FIRST):
        preferred, why = Engine.CLAUDE, "Claude: background/high-context WCA work"
    else:
        preferred, why = Engine.CLAUDE, "Claude: default auto route to conserve Codex"

    avail = {Engine.CLAUDE: claude_available, Engine.CODEX: codex_available}
    other = Engine.CODEX if preferred is Engine.CLAUDE else Engine.CLAUDE

    if avail[preferred]:
        return RouteDecision(preferred, why)
    if avail[other]:
        return RouteDecision(
            other, "%s: %s unavailable" % (other.value.capitalize(), preferred.value)
        )
    # Neither available — return preferred so the caller surfaces a clear
    # failure (the run reports the real "not logged in" reason).
    return RouteDecision(preferred, "%s (no healthy engine)" % why)
