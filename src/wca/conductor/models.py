"""Data structures for the dev-conductor.

Kept dependency-free (stdlib ``dataclasses`` + ``enum``) so the models can be
imported and unit-tested without pulling in ``requests`` or the betting stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Engine(str, Enum):
    """Which headless coding agent runs the task."""

    CLAUDE = "claude"
    CODEX = "codex"

    @classmethod
    def coerce(cls, value: str) -> "Engine":
        v = (value or "").strip().lower()
        for member in cls:
            if member.value == v:
                return member
        raise ValueError("unknown engine %r (want one of %s)" % (value, ", ".join(m.value for m in cls)))


class TaskStatus(str, Enum):
    """Lifecycle of a single conductor task.

    Linear-ish: QUEUED -> RUNNING -> (PUSHED ->) DONE, with NO_CHANGES /
    FAILED / REJECTED as terminal off-ramps.
    """

    QUEUED = "queued"        # accepted, waiting for a worker slot
    RUNNING = "running"      # agent is working in its worktree
    PUSHED = "pushed"        # branch pushed; PR not (yet) opened
    DONE = "done"            # PR opened (pr_url is a real PR)
    NO_CHANGES = "no-changes"  # agent ran but produced no diff
    FAILED = "failed"        # agent / git / push error (see .error)
    REJECTED = "rejected"    # never ran: cap, budget, or cancel-before-start

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.DONE,
            TaskStatus.NO_CHANGES,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
        }


@dataclass
class AgentResult:
    """Outcome of a single headless agent invocation."""

    returncode: int
    summary: str = ""
    tokens: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class PrResult:
    """Outcome of attempting to open a pull request.

    ``created`` distinguishes a real PR (``url`` set) from a graceful fallback
    where we could only build a compare link (``compare_url``) because ``gh``
    was missing / unauthenticated.
    """

    created: bool
    url: Optional[str] = None
    compare_url: Optional[str] = None
    error: str = ""

    @property
    def link(self) -> Optional[str]:
        return self.url or self.compare_url


@dataclass
class TaskRecord:
    """Mutable state for one dispatched task. Updated in place as it runs."""

    id: int
    engine: str
    task: str
    chat_id: str = ""
    shortid: str = ""
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    status: str = TaskStatus.QUEUED.value
    summary: str = ""
    error: str = ""
    route_reason: str = ""
    tokens: int = 0
    returncode: Optional[int] = None
    pr_url: Optional[str] = None
    activity: str = ""          # live: what the agent is doing right now
    activity_at: float = 0.0
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def short(self) -> str:
        """One-line human summary for a Telegram status row."""
        head = "#%d %s · %s" % (self.id, self.engine, self.status)
        body = self.task if len(self.task) <= 60 else self.task[:57] + "..."
        return "%s — %s" % (head, body)
