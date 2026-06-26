"""Fan-out manager: bounded parallelism, token budget, status, preflight.

The bot talks only to this class. It owns a fixed-size thread pool (the hard
``max_parallel`` cap), the registry of :class:`TaskRecord`s, and the
token-budget accounting. Submissions over budget are *rejected* (returned with
``status = REJECTED``), never silently dropped.
"""

from __future__ import annotations

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

# Conversational-mode tuning. History is trimmed to the last N turns when
# building a prompt so context stays bounded (and the prompt stays cheap).
_CHAT_HISTORY_TURNS = 16

_CHAT_PREAMBLE = (
    "You are the WCA dev-conductor's conversational assistant, replying inside a "
    "Telegram chat. You can read the project repo (you are running in its root) to "
    "answer questions about the code, architecture, data, and ongoing work, and to "
    "discuss ideas. Keep replies concise and Telegram-friendly (short paragraphs, "
    "Markdown ok). You are READ-ONLY here: do NOT modify files, run destructive or "
    "repo-writing commands, place bets, or touch live data. If the user wants an "
    "actual code change, propose the approach and tell them to dispatch it with "
    "`/task <description>` (which spawns a worktree + PR). Default to answering and "
    "proposing."
)

Notify = Callable[[TaskRecord], None]


class ConductorManager:
    def __init__(self, cfg: ConductorConfig, notify: Optional[Notify] = None,
                 store: Optional[object] = None) -> None:
        self.cfg = cfg
        self.notify = notify
        self._store = store  # ConductorStore | None — durable task + chat state
        self._records: Dict[int, TaskRecord] = {}
        self._futures: Dict[int, Future] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._pool = ThreadPoolExecutor(
            max_workers=cfg.max_parallel, thread_name_prefix="conductor"
        )
        self._health: Dict[str, EngineHealth] = {}
        self._health_lock = threading.Lock()  # separate: probing runs subprocesses
        # Conversational history per chat (in-memory cache; mirrored to the store).
        self._chat_history: Dict[str, List[Dict[str, str]]] = {}
        self._chat_locks: Dict[str, threading.Lock] = {}
        self._interrupted: List[TaskRecord] = []  # in-flight tasks lost to a restart
        if self._store is not None:
            self._reattach()

    # -- durability: persistence + restart reattach -----------------------

    def _persist(self, record: TaskRecord) -> None:
        """Best-effort: write a record to the store. Never raises (a persistence
        glitch must never kill a task or a reply)."""
        if self._store is None:
            return
        try:
            self._store.upsert_task(record)
        except Exception as exc:  # noqa: BLE001
            print("[conductor] persist failed for #%s: %s" % (record.id, exc), flush=True)

    def _reattach(self) -> None:
        """Reload persisted state on startup; flag tasks lost mid-flight.

        Any task that was QUEUED/RUNNING/PUSHED when the process died can't be
        resumed (its worktree/agent are gone), so it is marked INTERRUPTED and
        surfaced to :meth:`take_interrupted` so the bot can tell the user instead
        of failing silently. The id counter is advanced past every loaded id so a
        new submission can never collide with a persisted one.
        """
        try:
            records = self._store.load_tasks()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            print("[conductor] reattach failed: %s" % exc, flush=True)
            return
        in_flight = {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.PUSHED.value}
        for rec in records:
            if rec.status in in_flight:
                rec.status = TaskStatus.INTERRUPTED.value
                if not rec.error:
                    rec.error = "interrupted by a conductor restart"
                self._interrupted.append(rec)
                self._persist(rec)
            self._records[rec.id] = rec
        self._counter = max([self._store.max_task_id(), *self._records.keys(), 0])  # type: ignore[union-attr]
        try:
            self._chat_history = {
                cid: [{"role": r, "content": c} for r, c in turns]
                for cid, turns in self._store.load_chat().items()  # type: ignore[union-attr]
            }
        except Exception as exc:  # noqa: BLE001
            print("[conductor] chat reattach failed: %s" % exc, flush=True)

    def take_interrupted(self) -> List[TaskRecord]:
        """Return (and clear) the tasks that a restart interrupted, for notifying."""
        with self._lock:
            out = list(self._interrupted)
            self._interrupted = []
        return out

    def _on_transition(self, record: TaskRecord) -> None:
        """Single notify seam handed to the runner: persist, then notify the bot.

        Persisting here captures every lifecycle transition the runner emits
        (RUNNING, PUSHED, and the terminal state in its ``finally``), so a
        restart at any point finds an up-to-date record on disk.
        """
        self._persist(record)
        if self.notify is not None:
            try:
                self.notify(record)
            except Exception:  # noqa: BLE001 - a broken notifier must not kill the task
                pass

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

    def submit(self, engine: str, task: str, chat_id: str = "",
               images: Optional[List[str]] = None) -> TaskRecord:
        """Accept an explicit-engine task (Claude-only).

        If Claude is logged-out/unavailable, REJECT with the real reason (e.g.
        "claude not logged in — run `claude setup-token`"). There is no other
        engine to reroute to since Codex was removed from the swarm.

        ``images`` are local paths to pasted screenshots the agent should read
        as visual context (see :func:`runner.stage_images`).
        """
        engine = Engine.coerce(engine).value  # validate / normalise (claude only)
        task = (task or "").strip()
        if not task:
            raise ValueError("empty task")

        primary = self.engine_health(engine)  # probes outside the lock
        if not primary.ok:
            with self._lock:
                record = self._new_record_locked(engine, task, chat_id, images=images)
            record.status = TaskStatus.REJECTED.value
            record.error = primary.reason
            self._persist(record)
            return record

        with self._lock:
            return self._submit_locked(engine, task, chat_id, route_reason="", images=images)

    def submit_auto(self, task: str, chat_id: str = "",
                    images: Optional[List[str]] = None) -> TaskRecord:
        """Accept a task and route it (Claude-only) with a health check."""
        task = (task or "").strip()
        if not task:
            raise ValueError("empty task")

        claude_ok = self.engine_health(Engine.CLAUDE.value).ok  # probe outside lock
        decision = choose_engine(task, claude_available=claude_ok)
        if not claude_ok:
            with self._lock:
                record = self._new_record_locked(decision.engine.value, task, chat_id,
                                                 route_reason=decision.reason, images=images)
            record.status = TaskStatus.REJECTED.value
            record.error = "no healthy engine: claude (%s)" % (
                self.engine_health(Engine.CLAUDE.value).reason,
            )
            self._persist(record)
            return record
        with self._lock:
            return self._submit_locked(
                decision.engine.value, task, chat_id, route_reason=decision.reason, images=images,
            )

    def _submit_locked(self, engine: str, task: str, chat_id: str,
                       route_reason: str = "", images: Optional[List[str]] = None) -> TaskRecord:
        record = self._new_record_locked(engine, task, chat_id, route_reason, images=images)
        # Idempotency: an identical task already in flight is almost always a
        # double-tap or a resubmit after a restart. Reject the new one as a
        # duplicate (don't dispatch a second worktree/agent) and point at the
        # original — the audit's #1 collision root cause.
        dup = self._active_duplicate_locked(record.dedupe_key, exclude_id=record.id)
        if dup is not None:
            record.status = TaskStatus.REJECTED.value
            record.duplicate_of = dup.id
            record.error = "duplicate of #%d (already %s)" % (dup.id, dup.status)
            self._persist(record)
            return record
        budget = self.cfg.token_budget
        if budget is not None and self._spent_locked() >= budget:
            record.status = TaskStatus.REJECTED.value
            record.error = "token budget %d exhausted (spent %d)" % (budget, self._spent_locked())
            self._persist(record)
            return record
        future = self._pool.submit(runner.run_task, self.cfg, record, self._on_transition)
        self._futures[record.id] = future
        return record

    def _active_duplicate_locked(self, dedupe_key: str, exclude_id: int) -> Optional[TaskRecord]:
        """Oldest ACTIVE (queued/running/pushed) record sharing *dedupe_key*."""
        if not dedupe_key:
            return None
        active = {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.PUSHED.value}
        for rid in sorted(self._records):
            if rid == exclude_id:
                continue
            r = self._records[rid]
            if r.status in active and r.dedupe_key == dedupe_key:
                return r
        return None

    def _new_record_locked(self, engine: str, task: str, chat_id: str,
                           route_reason: str = "", images: Optional[List[str]] = None) -> TaskRecord:
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
            images=list(images or []),
            shortid=shortid,
            branch=branch,
            status=TaskStatus.QUEUED.value,
            route_reason=route_reason,
            created_at=time.time(),
            dedupe_key=runner.slugify(task),
        )
        self._records[rid] = record
        self._persist(record)
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
            self._persist(record)
        return record

    _RETRYABLE = {
        TaskStatus.FAILED.value,
        TaskStatus.REJECTED.value,
        TaskStatus.NO_CHANGES.value,
        TaskStatus.INTERRUPTED.value,
    }

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
        return self.submit(old.engine, old.task, chat_id=old.chat_id, images=old.images)

    def merge_task(self, task_id: int) -> "tuple[bool, str]":
        """Squash-merge a task's PR via ``gh`` — but ONLY if it's open and green.

        Returns ``(ok, message)``. Refuses unless the task produced a real PR,
        that PR is OPEN, and every status check has passed (no checks configured
        counts as green). The admin gate is enforced by the bot before this runs;
        this method is the safety floor (state + green-only).
        """
        import json as _json

        r = self.get(task_id)
        if r is None:
            return False, "no task `#%d`" % task_id
        if r.status != TaskStatus.DONE.value or not (r.pr_url and "/pull/" in r.pr_url):
            return False, "`#%d` has no open PR to merge (status %s)" % (task_id, r.status)
        gh = self.cfg.resolve_bin(self.cfg.gh_bin)
        if gh is None:
            return False, "`gh` not available — can't merge (authenticate it on the host)"
        env = self.cfg.agent_env()
        try:
            view = subprocess.run(
                [gh, "pr", "view", r.branch or "",
                 "--json", "state,mergeStateStatus,statusCheckRollup"],
                capture_output=True, text=True, timeout=30, env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, "gh view failed: %s" % exc
        if view.returncode != 0:
            return False, "gh view failed: %s" % ((view.stderr or "").strip()[:200])
        try:
            info = _json.loads(view.stdout or "{}")
        except ValueError:
            info = {}
        if info.get("state") != "OPEN":
            return False, "`#%d` PR is %s, not OPEN" % (task_id, info.get("state") or "?")
        rollup = info.get("statusCheckRollup") or []
        not_green = [
            c for c in rollup
            if (c.get("conclusion") or c.get("state") or "").upper()
            not in ("SUCCESS", "NEUTRAL", "SKIPPED", "")
        ]
        if not_green:
            return False, "`#%d` not green — %d check(s) failing/pending; review on GitHub" % (
                task_id, len(not_green))
        try:
            res = subprocess.run(
                [gh, "pr", "merge", r.branch or "", "--squash", "--delete-branch"],
                capture_output=True, text=True, timeout=90, env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, "merge failed: %s" % exc
        if res.returncode != 0:
            return False, "merge failed: %s" % ((res.stderr or res.stdout or "").strip()[:200])
        return True, "merged `#%d` (squash) and deleted `%s`" % (task_id, r.branch)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
        if self._store is not None:
            try:
                self._store.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

    # -- conversational chat ----------------------------------------------

    def _chat_lock_for(self, chat_id: str) -> threading.Lock:
        with self._lock:
            lk = self._chat_locks.get(chat_id)
            if lk is None:
                lk = threading.Lock()
                self._chat_locks[chat_id] = lk
            return lk

    def _record_chat(self, chat_id: str, role: str, content: str) -> None:
        hist = self._chat_history.setdefault(chat_id, [])
        hist.append({"role": role, "content": content})
        # keep the in-memory cache bounded (the store keeps the full record)
        if len(hist) > _CHAT_HISTORY_TURNS * 2:
            del hist[: len(hist) - _CHAT_HISTORY_TURNS * 2]
        if self._store is not None:
            try:
                self._store.append_chat(chat_id, role, content, time.time())  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                print("[conductor] chat persist failed: %s" % exc, flush=True)

    def _build_chat_prompt(self, chat_id: str, user_text: str) -> str:
        hist = self._chat_history.get(chat_id, [])[-_CHAT_HISTORY_TURNS:]
        lines = [_CHAT_PREAMBLE, ""]
        if hist:
            lines.append("Conversation so far:")
            for turn in hist:
                who = "User" if turn.get("role") == "user" else "Assistant"
                lines.append("%s: %s" % (who, turn.get("content", "")))
            lines.append("")
        lines.append("User: %s" % user_text)
        lines.append("")
        lines.append("Reply as the assistant (do not modify the repo):")
        return "\n".join(lines)

    def chat(self, chat_id: str, user_text: str) -> str:
        """Generate one conversational reply for *user_text*, keeping per-chat
        context. Persists both the user message and the reply (so context
        survives a restart). Bounded by ``cfg.chat_timeout``; never raises.
        """
        chat_id = str(chat_id)
        user_text = (user_text or "").strip()
        if not user_text:
            return ""
        with self._chat_lock_for(chat_id):  # serialize a chat's own turns
            health = self.engine_health(Engine.CLAUDE.value)
            if not health.ok:
                return "⚠️ Chat unavailable — claude engine: %s" % health.reason
            # Build the prompt from prior turns, THEN record this user message, so
            # the current turn isn't duplicated in the transcript.
            prompt = self._build_chat_prompt(chat_id, user_text)
            self._record_chat(chat_id, "user", user_text)
            try:
                result = runner.run_chat(self.cfg, prompt, timeout=self.cfg.chat_timeout)
            except Exception as exc:  # noqa: BLE001 - never let a reply crash the bot
                return "⚠️ chat error: %s" % exc
            if not result.ok:
                return "⚠️ %s" % (result.error or "chat failed")
            reply = result.summary or "(no reply)"
            self._record_chat(chat_id, "assistant", reply)
            return reply

    # -- accounting -------------------------------------------------------

    def _spent_locked(self) -> int:
        return sum(r.tokens for r in self._records.values())

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
        if r.status == TaskStatus.RUNNING.value and r.activity:
            lines.append("↳ now: %s" % r.activity)
        if r.pr_url:
            lines.append(r.pr_url)
        if r.tokens:
            lines.append("tokens: %d" % r.tokens)
        if r.summary:
            lines.append("summary: %s" % (r.summary if len(r.summary) <= 350 else r.summary[:347] + "..."))
        if r.error:
            lines.append("⚠️ %s" % (r.error if len(r.error) <= 350 else r.error[:347] + "..."))
        return "\n".join(lines)

    def watch(self, task_id: Optional[int] = None) -> str:
        """Live activity: one task (`/watch <id>`) or every running task (`/watch`)."""
        if task_id is not None:
            r = self.get(task_id)
            if r is None:
                return "No task `#%d`." % task_id
            act = r.activity or ("(no activity yet)" if r.status == TaskStatus.RUNNING.value else r.status)
            return "🔭 *#%d* %s · %s\n↳ %s" % (r.id, r.engine, r.status, act)
        running = [r for r in self.records() if r.status == TaskStatus.RUNNING.value]
        if not running:
            return "_No tasks running._"
        lines = ["🔭 *Live activity* (%d running)" % len(running)]
        for r in running:
            lines.append("`#%d` %s\n   ↳ %s" % (r.id, r.engine, r.activity or "(starting…)"))
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
                if r.status == TaskStatus.RUNNING.value and r.activity:
                    row += "\n      ↳ %s" % r.activity  # live: what the agent is doing now
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
            Engine.CLAUDE.value: "sole route · all conductor work (Codex removed 2026-06)",
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
        TaskStatus.INTERRUPTED.value: "♻️",
    }

    def status_table(self) -> str:
        records = self.records()
        if not records:
            return "_No conductor tasks yet._ Send `/task <task>` or `/claude <task>`."

        lines = ["*Conductor — tasks*"]
        for r in records:
            icon = self._ICON.get(r.status, "•")
            task = r.task if len(r.task) <= 56 else r.task[:53] + "..."
            row = "%s `#%d` *%s* · %s" % (icon, r.id, r.engine, r.status)
            if r.tokens:
                row += " · %d tok" % r.tokens
            row += "\n   %s" % task
            if r.status == TaskStatus.RUNNING.value and r.activity:
                row += "\n   ↳ %s" % r.activity  # live agent activity
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
        else:
            warnings.append("healthy engine(s): claude")
        gh = self.cfg.resolve_bin(self.cfg.gh_bin)
        if gh is None:
            warnings.append("`gh` CLI not found — PRs use the REST API fallback or a compare link")
        else:
            try:
                auth = subprocess.run(
                    [gh, "auth", "status"],
                    capture_output=True, text=True, timeout=15,
                    env=self.cfg.agent_env(),
                )
                if auth.returncode != 0:
                    warnings.append("`gh` not authenticated (`gh auth login`) — PRs use the REST API fallback or a compare link")
            except (OSError, subprocess.SubprocessError):
                warnings.append("`gh auth status` check failed — PRs may fall back to compare links")
        return warnings

    # -- anti-collision ---------------------------------------------------

    def find_active_duplicate(self, task: str) -> Optional[TaskRecord]:
        """The oldest ACTIVE task whose slug matches *task*, or ``None``.

        Two dispatches that slugify identically are almost always the same
        feature requested twice; running both branches off ``main`` produced the
        divergent, conflicting implementations the swarm hit. The bot calls this
        before submit to warn the operator (see AGENTS.md anti-collision policy).
        """
        slug = runner.slugify(task)
        active = {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.PUSHED.value}
        with self._lock:
            for rid in sorted(self._records):
                r = self._records[rid]
                if r.status in active and runner.slugify(r.task) == slug:
                    return r
        return None
