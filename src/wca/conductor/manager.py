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
from wca.conductor import health as _health_mod
from wca.conductor import runner
from wca.conductor.config import ConductorConfig
from wca.conductor.health import EngineHealth
from wca.conductor.models import Engine, TaskRecord, TaskStatus

_HEALTH_TTL = 300.0  # seconds an engine-health probe stays cached

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
        self._health: Dict[str, EngineHealth] = {}
        self._health_lock = threading.Lock()  # separate: probing runs subprocesses

    # -- engine health ----------------------------------------------------

    def engine_health(self, engine: str, force: bool = False) -> EngineHealth:
        """Return cached health for *engine*, re-probing past the TTL.

        Probing shells out, so it is NEVER done while holding ``self._lock``.
        """
        engine = Engine.coerce(engine).value
        if self.cfg.is_disabled(engine):
            # operator turned this engine off (e.g. Codex exhausted) — treat as
            # unavailable so routing falls back without a (failing) probe/run.
            return EngineHealth(engine, False, "%s disabled via config" % engine)
        with self._health_lock:
            cached = self._health.get(engine)
            if cached is not None and not force and (time.time() - cached.checked_at) < _HEALTH_TTL:
                return cached
        probed = _health_mod.probe_engine(self.cfg, engine)
        probed.checked_at = time.time()
        with self._health_lock:
            self._health[engine] = probed
        return probed

    def healthy(self, engine: str) -> bool:
        return self.engine_health(engine).ok

    def refresh_health(self) -> Dict[str, EngineHealth]:
        return {e.value: self.engine_health(e.value, force=True) for e in Engine}

    # -- submission -------------------------------------------------------

    def submit(self, engine: str, task: str, chat_id: str = "") -> TaskRecord:
        """Accept an explicit-engine task, rerouting around a dead engine.

        If the requested engine is logged-out/unavailable, reroute to the other
        engine when it is healthy; if neither is healthy, REJECT with the real
        reason (e.g. "claude not logged in — run `claude setup-token`").
        """
        engine = Engine.coerce(engine).value  # validate / normalise
        task = (task or "").strip()
        if not task:
            raise ValueError("empty task")

        route_reason = ""
        primary = self.engine_health(engine)  # probes outside the lock
        if not primary.ok:
            other = Engine.CODEX.value if engine == Engine.CLAUDE.value else Engine.CLAUDE.value
            secondary = self.engine_health(other)
            if secondary.ok:
                route_reason = "rerouted %s→%s (%s)" % (engine, other, primary.reason)
                engine = other
            else:
                with self._lock:
                    record = self._new_record_locked(engine, task, chat_id)
                record.status = TaskStatus.REJECTED.value
                record.error = primary.reason
                return record

        with self._lock:
            return self._submit_locked(engine, task, chat_id, route_reason=route_reason)

    def submit_auto(self, task: str, chat_id: str = "") -> TaskRecord:
        """Accept a task after picking the engine with the health-aware router."""
        task = (task or "").strip()
        if not task:
            raise ValueError("empty task")

        claude_ok = self.engine_health(Engine.CLAUDE.value).ok  # probe outside lock
        codex_ok = self.engine_health(Engine.CODEX.value).ok
        with self._lock:
            codex_cap_ok = self.cfg.codex_auto_limit > self._active_engine_locked(Engine.CODEX.value)
        # The codex_auto_limit is a *conservation* cap (send overflow to Claude
        # to spend scarce Codex sparingly) — NOT a hard availability gate. When
        # Claude is unavailable there is nothing to conserve, so ignore the cap
        # rather than mis-route extra /task jobs to a logged-out Claude.
        codex_available = codex_ok and (codex_cap_ok or not claude_ok)
        decision = choose_engine(
            task,
            codex_available=codex_available,
            claude_available=claude_ok,
        )
        if not claude_ok and not codex_ok:
            with self._lock:
                record = self._new_record_locked(decision.engine.value, task, chat_id,
                                                 route_reason=decision.reason)
            record.status = TaskStatus.REJECTED.value
            record.error = "no healthy engine: claude (%s), codex (%s)" % (
                self.engine_health(Engine.CLAUDE.value).reason,
                self.engine_health(Engine.CODEX.value).reason,
            )
            return record
        with self._lock:
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

    _RETRYABLE = {TaskStatus.FAILED.value, TaskStatus.REJECTED.value, TaskStatus.NO_CHANGES.value}

    def retry(self, task_id: int) -> Optional[TaskRecord]:
        """Re-dispatch a finished task as a NEW record (same engine + text).

        Returns None if unknown; returns the original (unchanged) if it's not in
        a retryable state so the caller can message why.
        """
        old = self.get(task_id)
        if old is None:
            return None
        if old.status not in self._RETRYABLE:
            return old
        return self.submit(old.engine, old.task, chat_id=old.chat_id)

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

    def usage_table(self) -> str:
        """Real-time Anthropic token spend by the conductor, per engine + limits.

        Account-wide usage/limits live in the Anthropic Console (a subscription
        OAuth token can't expose them programmatically); this shows what the
        conductor itself has spent — what you manage the swarm against.
        """
        records = self.records()
        lines = ["*Anthropic usage* — conductor spend (real-time)"]
        total = 0
        for e in Engine:
            ev = e.value
            mine = [r for r in records if r.engine == ev]
            toks = sum(r.tokens for r in mine)
            total += toks
            n_run = sum(1 for r in mine if r.status == TaskStatus.RUNNING.value)
            h = self.engine_health(ev)
            status = "✅" if h.ok else ("🚫 " + h.reason if self.cfg.is_disabled(ev) else "❌ " + h.reason)
            lines.append("• *%s* — %s tok · %d task(s) · %d running · %s" % (
                ev, format(toks, ","), len(mine), n_run, status))
        lines.append("\n*Total:* %s tokens · %d-way parallel" % (format(total, ","), self.cfg.max_parallel))
        if self.cfg.token_budget:
            lines.append("Budget: %s / %s" % (format(total, ","), format(self.cfg.token_budget, ",")))
        lines.append("\n_Account-wide usage & limits: console.anthropic.com/settings/usage. "
                     "Your subscription's rolling limit surfaces as a task error — you'll be notified._")
        return "\n".join(lines)

    def prs(self) -> str:
        """Tasks that produced a PR / branch link — for review from the phone."""
        recs = [r for r in self.records() if r.pr_url]
        if not recs:
            return "_No task PRs yet._ Dispatch one with `/claude <task>`."
        lines = ["*Conductor — task PRs*"]
        for r in recs:
            label = "PR" if r.status == TaskStatus.DONE.value else "branch"
            task = r.task if len(r.task) <= 50 else r.task[:47] + "..."
            lines.append("`#%d` %s · [%s](%s)\n   %s" % (r.id, r.status, label, r.pr_url, task))
        return "\n".join(lines)

    def task_detail(self, task_id: int) -> str:
        """Full detail of one task (for /log <id>)."""
        r = self.get(task_id)
        if r is None:
            return "No task `#%d`." % task_id
        lines = ["*Task #%d* — %s · %s" % (r.id, r.engine, r.status),
                 "_%s_" % (r.task if len(r.task) <= 200 else r.task[:197] + "...")]
        if r.route_reason:
            lines.append("route: %s" % r.route_reason)
        if r.branch:
            lines.append("branch: `%s`" % r.branch)
        if r.pr_url:
            lines.append(r.pr_url)
        if r.tokens:
            lines.append("tokens: %d" % r.tokens)
        if r.summary:
            lines.append("summary: %s" % (r.summary if len(r.summary) <= 350 else r.summary[:347] + "..."))
        if r.error:
            lines.append("⚠️ %s" % (r.error if len(r.error) <= 350 else r.error[:347] + "..."))
        return "\n".join(lines)

    def health_table(self, force: bool = True) -> str:
        """Markdown summary of each engine's live auth/availability."""
        lines = ["*Conductor — engine health*"]
        for e in Engine:
            h = self.engine_health(e.value, force=force)
            lines.append("%s *%s* — %s" % ("✅" if h.ok else "❌", e.value, h.reason))
        return "\n".join(lines)

    def model_usage_table(self) -> str:
        """Per-agent view of ongoing (running) and parked (queued) tasks.

        'Parked' = accepted but waiting for a worker slot (max-parallel cap).
        """
        records = self.records()
        active = {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.PUSHED.value}
        lines = ["*Model usage* — ongoing & parked tasks by agent"]
        for e in Engine:
            ev = e.value
            h = self.engine_health(ev)
            mine = [r for r in records if r.engine == ev]
            running = [r for r in mine if r.status == TaskStatus.RUNNING.value]
            parked = [r for r in mine if r.status == TaskStatus.QUEUED.value]
            pushed = [r for r in mine if r.status == TaskStatus.PUSHED.value]
            toks = sum(r.tokens for r in mine)
            badge = "✅" if h.ok else ("🚫" if self.cfg.is_disabled(ev) else "❌")
            lines.append("\n%s *%s* — %d running · %d parked · %d pushing · %d tok" % (
                badge, ev, len(running), len(parked), len(pushed), toks))
            if not h.ok:
                lines.append("   _%s_" % h.reason)
            shown = running + parked + pushed
            for r in shown:
                icon = self._ICON.get(r.status, "•")
                task = r.task if len(r.task) <= 44 else r.task[:41] + "..."
                row = "   %s `#%d` %s — %s" % (icon, r.id, r.status, task)
                if r.tokens:
                    row += " · %d tok" % r.tokens
                lines.append(row)
            if not shown:
                lines.append("   _idle_")
        done = sum(1 for r in records if r.status not in active)
        lines.append("\n_%d active across the fleet · %d finished · cap %d_" % (
            self.active_count(), done, self.cfg.max_parallel))
        return "\n".join(lines)

    def agents_spec_table(self) -> str:
        """Spec + architecture for each agent in the fleet."""
        c = self.cfg
        lines = ["*Agents* — fleet specification & architecture", ""]
        roles = {
            Engine.CLAUDE.value: "default route · background / high-context / research work",
            Engine.CODEX.value: "scarce route · small mechanical edits (auto-cap %d)" % c.codex_auto_limit,
        }
        for e in Engine:
            ev = e.value
            binary, args = c.cli_for(ev)
            h = self.engine_health(ev)
            if c.is_disabled(ev):
                state = "🚫 disabled via config"
            else:
                state = "✅ available" if h.ok else "❌ %s" % h.reason
            short_bin = binary if len(binary) <= 48 else "…" + binary[-46:]
            lines.append("*%s* — %s" % (ev, state))
            lines.append("   role: %s" % roles.get(ev, "—"))
            lines.append("   cli: `%s`" % short_bin)
            lines.append("   args: `%s`" % (" ".join(args) or "(none)"))
            lines.append("")
        lines.append("*Shared architecture*")
        lines.append("• one task → fresh git worktree on a new branch off `%s`" % c.base_branch)
        lines.append("• agent runs headless → commit → push → PR (PR-only; never commits `main`)")
        lines.append("• router probes auth and skips logged-out / disabled engines")
        lines.append("• sandbox env: `PM_DRY_RUN=1`, `WCA_DB_PATH=data/dev.db`, Polymarket key stripped")
        budget = (" · token budget %d" % c.token_budget) if c.token_budget else ""
        lines.append("• caps: max-parallel %d%s; worktree add/remove serialized" % (c.max_parallel, budget))
        return "\n".join(lines)

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
        """Return human-readable warnings about missing/unauthenticated CLIs.

        Reports real engine *auth* state (not just PATH presence), so a logged-
        out Claude shows up here instead of failing silently at dispatch.
        """
        warnings: List[str] = []
        claude = self.engine_health(Engine.CLAUDE.value)
        if not claude.ok:
            warnings.append("claude unavailable: %s" % claude.reason)
        codex = self.engine_health(Engine.CODEX.value)
        if not codex.ok:
            warnings.append("codex unavailable: %s" % codex.reason)
        if claude.ok or codex.ok:
            healthy = ", ".join(e for e, h in (("claude", claude), ("codex", codex)) if h.ok)
            warnings.append("healthy engine(s): %s" % healthy)
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
