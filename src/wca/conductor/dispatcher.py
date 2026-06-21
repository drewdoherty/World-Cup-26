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


def choose_engine(task: str, codex_available: bool = True) -> RouteDecision:
    """Pick the best engine for an automatic ``/task`` submission.

    Explicit ``/claude`` and ``/codex`` remain available as overrides. This
    function handles only automatic routing and therefore defaults to Claude
    unless the task looks cheap enough for scarce Codex usage.
    """
    text = " ".join((task or "").lower().split())
    if _contains_any(text, _CLAUDE_FIRST):
        return RouteDecision(
            Engine.CLAUDE,
            "Claude: background/high-context WCA work",
        )
    if _contains_any(text, _CODEX_MECHANICAL):
        if codex_available:
            return RouteDecision(Engine.CODEX, "Codex: small mechanical edit")
        return RouteDecision(Engine.CLAUDE, "Claude: Codex auto cap reached")
    return RouteDecision(Engine.CLAUDE, "Claude: default auto route to conserve Codex")
