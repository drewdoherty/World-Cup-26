"""Fan-out manager: bounded parallelism, token budget, status, preflight.

The bot talks only to this class. It owns a fixed-size thread pool (the hard
``max_parallel`` cap), the registry of :class:`TaskRecord`s, and the
token-budget accounting. Submissions over budget are *rejected* (returned with
``status = REJECTED``), never silently dropped.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from wca.conductor.dispatcher import choose_engine
from wca.conductor import runner
from wca.conductor.config import ConductorConfig
from wca.conductor.models import Engine, TaskRecord, TaskStatus

Notify = Callable[[TaskRecord], None]


class ConductorManager:
    def __init__(self, cfg: ConductorConfig, notify: Optional[Notify] = None) -> None:
        self.cfg = cfg
        self.notify = notify
        self._records: Dict[int, TaskRecord] = {}
        self._futures: Dict[int, Future] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._pool = ThreadPoolExecutor(
            max_workers=cfg.max_parallel, thread_name_prefix="conductor"
        )

    # -- submission -------------------------------------------------------

    def submit(self, engine: str, task: str, chat_id: str = "") -> TaskRecord:
        """Accept a task. Returns the record (possibly already REJECTED)."""
        engine = Engine.coerce(engine).value  # validate / normalise
        task = (task or "").strip()
        if not task:
            raise ValueError("empty task")

        with self._lock:
            return self._submit_locked(engine, task, chat_id)

    def submit_auto(self, task: str, chat_id: str = "") -> TaskRecord:
        """Accept a task after picking the engine with the dispatcher."""
        task = (task or "").strip()
        if not task:
            raise ValueError("empty task")

        with self._lock:
            codex_available = self.cfg.codex_auto_limit > self._active_engine_locked(Engine.CODEX.value)
            decision = choose_engine(task, codex_available=codex_available)
            return self._submit_locked(
                decision.engine.value, task, chat_id, route_reason=decision.reason,
            )

    def _submit_locked(self, engine: str, task: str, chat_id: str,
                       route_reason: str = "") -> TaskRecord:
        record = self._new_record_locked(engine, task, chat_id, route_reason)
        budget = self.cfg.token_budget
        if budget is not None and self._spent_locked() >= budget:
            record.status = TaskStatus.REJECTED.value
            record.error = "token budget %d exhausted (spent %d)" % (budget, self._spent_locked())
            return record
        future = self._pool.submit(runner.run_task, self.cfg, record, self.notify)
        self._futures[record.id] = future
        return record

    def _new_record_locked(self, engine: str, task: str, chat_id: str,
                           route_reason: str = "") -> TaskRecord:
        self._counter += 1
        rid = self._counter
        shortid = uuid.uuid4().hex[:6]
        branch = "%s/%s-%s-%s" % (
            self.cfg.branch_prefix, engine, runner.slugify(task), shortid,
        )
        record = TaskRecord(
            id=rid,
            engine=engine,
            task=task,
            chat_id=str(chat_id),
            shortid=shortid,
            branch=branch,
            status=TaskStatus.QUEUED.value,
            route_reason=route_reason,
            created_at=time.time(),
        )
        self._records[rid] = record
        return record

    # -- control ----------------------------------------------------------

    def cancel(self, task_id: int) -> Optional[TaskRecord]:
        """Best-effort cancel. Only QUEUED (not-yet-started) tasks can stop."""
        with self._lock:
            record = self._records.get(task_id)
            future = self._futures.get(task_id)
        if record is None:
            return None
        if future is not None and future.cancel():
            record.status = TaskStatus.REJECTED.value
            record.error = "cancelled before start"
        return record

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)

    # -- accounting -------------------------------------------------------

    def _spent_locked(self) -> int:
        return sum(r.tokens for r in self._records.values())

    def _active_engine_locked(self, engine: str) -> int:
        return sum(
            1 for r in self._records.values()
            if r.engine == engine
            and r.status in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.PUSHED.value}
        )

    def spent_tokens(self) -> int:
        with self._lock:
            return self._spent_locked()

    def records(self) -> List[TaskRecord]:
        with self._lock:
            return [self._records[k] for k in sorted(self._records)]

    def get(self, task_id: int) -> Optional[TaskRecord]:
        with self._lock:
            return self._records.get(task_id)

    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for r in self._records.values()
                if r.status in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.PUSHED.value}
            )

    # -- presentation -----------------------------------------------------

    _ICON = {
        TaskStatus.QUEUED.value: "⏳",
        TaskStatus.RUNNING.value: "⚙️",
        TaskStatus.PUSHED.value: "⬆️",
        TaskStatus.DONE.value: "✅",
        TaskStatus.NO_CHANGES.value: "∅",
        TaskStatus.FAILED.value: "❌",
        TaskStatus.REJECTED.value: "🚫",
    }

    def status_table(self) -> str:
        records = self.records()
        if not records:
            return "_No conductor tasks yet._ Send `/claude <task>` or `/codex <task>`."

        lines = ["*Conductor — tasks*"]
        for r in records:
            icon = self._ICON.get(r.status, "•")
            task = r.task if len(r.task) <= 56 else r.task[:53] + "..."
            row = "%s `#%d` *%s* · %s" % (icon, r.id, r.engine, r.status)
            if r.tokens:
                row += " · %d tok" % r.tokens
            row += "\n   %s" % task
            if r.pr_url:
                label = "PR" if r.status == TaskStatus.DONE.value else "diff"
                row += "\n   [%s](%s)" % (label, r.pr_url)
            if r.route_reason:
                row += "\n   route: %s" % r.route_reason
            if r.error and r.status in {TaskStatus.FAILED.value, TaskStatus.REJECTED.value, TaskStatus.PUSHED.value}:
                err = r.error if len(r.error) <= 90 else r.error[:87] + "..."
                row += "\n   ⚠️ %s" % err
            lines.append(row)

        spent = self.spent_tokens()
        foot = "_%d task(s)" % len(records)
        if self.cfg.token_budget:
            foot += " · %d/%d tok" % (spent, self.cfg.token_budget)
        elif spent:
            foot += " · %d tok" % spent
        foot += " · cap %d_" % self.cfg.max_parallel
        lines.append(foot)
        return "\n".join(lines)

    # -- preflight --------------------------------------------------------

    def preflight(self) -> List[str]:
        """Return human-readable warnings about missing runtime prerequisites."""
        warnings: List[str] = []
        if shutil.which(self.cfg.claude_bin) is None:
            warnings.append("`claude` CLI not on PATH (%s) — /claude tasks will fail" % self.cfg.claude_bin)
        if shutil.which(self.cfg.codex_bin) is None:
            warnings.append("`codex` CLI not on PATH (%s) — /codex tasks will fail" % self.cfg.codex_bin)
        if shutil.which(self.cfg.gh_bin) is None:
            warnings.append("`gh` CLI not found — PRs fall back to compare links")
        else:
            try:
                auth = subprocess.run(
                    [self.cfg.gh_bin, "auth", "status"],
                    capture_output=True, text=True, timeout=15,
                )
                if auth.returncode != 0:
                    warnings.append("`gh` not authenticated (`gh auth login`) — PRs fall back to compare links")
            except (OSError, subprocess.SubprocessError):
                warnings.append("`gh auth status` check failed — PRs may fall back to compare links")
        return warnings
