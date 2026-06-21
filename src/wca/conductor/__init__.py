"""WCA dev-conductor — fan tasks out to headless coding agents.

A *separate* dev-only subsystem (NOT the betting bot): one Telegram interface
that dispatches a task to a headless coding agent (``claude -p`` or
``codex exec``), each in its own fresh git worktree + branch off ``main``,
then commits, pushes, and opens a PR. Several tasks run in parallel.

Guardrails baked in (see :mod:`wca.conductor.runner` / :mod:`wca.conductor.manager`):

* **PR-only** — every task gets a fresh branch in a throwaway worktree; the
  runner refuses to operate on the base branch and never pushes ``main``.
* **Bounded fan-out** — a hard ``max_parallel`` cap via a thread pool.
* **Token budget** — optional ceiling across all instances; over-budget
  submissions are rejected, not silently queued forever.
* **Dry-by-default env** — spawned agents inherit ``PM_DRY_RUN=1`` +
  ``WCA_DB_PATH=data/dev.db`` and have ``POLYMARKET_PRIVATE_KEY`` stripped, so
  agent-run code can never touch live money or the real ledger.
* **Honest reporting** — status is read from real agent output and PR results,
  never fabricated.
"""

from __future__ import annotations

from wca.conductor.config import ConductorConfig
from wca.conductor.models import AgentResult, Engine, PrResult, TaskRecord, TaskStatus

__all__ = [
    "ConductorConfig",
    "AgentResult",
    "Engine",
    "PrResult",
    "TaskRecord",
    "TaskStatus",
]
