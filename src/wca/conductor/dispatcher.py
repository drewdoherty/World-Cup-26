"""Automatic engine routing for the dev-conductor.

**Claude-only since 2026-06.** Codex was removed from the swarm (unreliable
token accounting + auth, and everything routed to Claude in practice anyway).
``choose_engine`` is kept as the single routing seam — it now always returns
Claude — so callers/tests keep their shape if another engine is added later.
"""

from __future__ import annotations

from dataclasses import dataclass

from wca.conductor.models import Engine


@dataclass(frozen=True)
class RouteDecision:
    engine: Engine
    reason: str


def choose_engine(task: str, claude_available: bool = True) -> RouteDecision:
    """Pick the engine for an automatic ``/task`` submission — always Claude.

    ``claude_available`` reflects real engine health; when Claude is unavailable
    we still return Claude so the caller surfaces the real "not logged in"
    reason rather than silently dropping the task.
    """
    reason = "Claude" if claude_available else "Claude (no healthy engine)"
    return RouteDecision(Engine.CLAUDE, reason)
