#!/usr/bin/env python
"""WCA dev-conductor — a *separate* Telegram bot that fans tasks out to
headless Claude Code agents (``claude -p``), one git worktree +
branch + PR per task.

This is dev infrastructure, NOT the betting bot. It must run on a dev box with
``.env.dev`` (dry-run, dev DB, no live keys). Launch from the MAIN checkout so
worktrees land in ``.claude/worktrees`` where the cleanup script expects them::

    python scripts/wca_conductor.py --env .env.dev

Commands (admin-gated where they spend tokens / write branches):

    /task <task>     dispatch a task to Claude Code, headless
    /claude <task>   dispatch a task to Claude Code, headless (alias of /task)
    /status          show the per-task table (read from real results)
    /cancel <id>     cancel a task that hasn't started yet
    /help            usage + runtime warnings

Guardrails: PR-only (fresh branch per task, never ``main``), a hard
max-parallel cap, an optional token budget, and a dry-run/no-secrets env for
every spawned agent. See :mod:`wca.conductor`.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Report artifacts a finished task can send back (auto-attached + via /report).
# Generated data feeds are excluded — they are noise, not a "report".
_REPORT_EXTS = {".md", ".csv", ".txt", ".png", ".svg", ".pdf"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_REPORT_EXCLUDE_PREFIXES = ("site/", "data/", ".github/", "node_modules/")
_MAX_REPORT_FILES = 6
_TELEGRAM_PHOTO_MAX = 10 * 1024 * 1024   # Bot API sendPhoto ceiling
_TELEGRAM_DOC_MAX = 48 * 1024 * 1024     # Bot API sendDocument ceiling (~50 MB)
_DIFF_STAT_MAX = 1400                     # chars of --stat to inline in chat
_DIFF_PATCH_MAX = 256 * 1024             # bytes of full patch to attach as a file

# Make ``src`` importable when run from a worktree (editable install resolves to
# the main checkout otherwise — the known worktree PYTHONPATH quirk).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wca.bot.telegram import (  # noqa: E402
    TelegramClient,
    TelegramError,
    image_document_file_id,
    largest_photo_file_id,
)
from wca.conductor.config import ConductorConfig  # noqa: E402
from wca.conductor.manager import ConductorManager  # noqa: E402
from wca.conductor.models import Engine, TaskRecord, TaskStatus  # noqa: E402


def _load_dotenv(path: str = ".env.dev") -> None:
    """Tiny .env loader (matches scripts/wca_bot.py — no python-dotenv dep)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _authorized(chat_id: str, allowed: Set[str]) -> bool:
    # Empty allowlist => single-user/dev convenience: accept any chat.
    return not allowed or str(chat_id) in allowed


def _is_admin(user_id: str, admin: Optional[str]) -> bool:
    # With no admin set, everyone in an authorized chat may dispatch (dev box).
    if not admin:
        return True
    return str(user_id) == str(admin)


def _msg_text(message: Dict[str, object]) -> str:
    """The user's text for a message — caption for a photo, else plain text."""
    return str(message.get("text") or message.get("caption") or "").strip()


# Telegram delivers an "album" (multiple images in one send) as separate update
# objects sharing a media_group_id; only one carries the caption. Cap how many we
# pull per album (Telegram's own album limit is 10).
_MAX_ALBUM_IMAGES = 10


def _paper_book_db() -> str:
    """Path to the isolated paper test-book DB (repo-root/data/test_book.db)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "test_book.db")


def _paper_book_report(db_path: Optional[str] = None) -> str:
    """Exposure + performance of the paper test book, for the /paper command."""
    import os as _os

    path = db_path or _paper_book_db()
    if not _os.path.exists(path):
        return ("🧪 *Test book* — not initialised yet.\n"
                "No paper book at `%s`. The trade cycle seeds it on first run." % path)
    try:
        from wca.testbook import store
    except Exception as exc:  # pragma: no cover
        return "🧪 *Test book* — module unavailable (%s)." % exc

    con = store.connect(path)
    rep = store.report(con)
    if rep.get("seed", 0) == 0 and rep.get("n_open", 0) == 0:
        return "🧪 *Test book* — seeded $0 / no positions yet."

    lines = [
        "🧪 *Test book* (paper, $%.0f seed)" % rep["seed"],
        "equity *$%.0f* (ROI %+.1f%%) · cash $%.0f · deployed $%.0f"
        % (rep["equity"], rep["roi_pct"], rep["realized_balance"], rep["deployed"]),
        "realised $%+.2f (%d settled) · unrealised $%+.2f (MTM)"
        % (rep["realized_pl"], rep["n_settled"], rep["unrealized_pl"]),
    ]

    # Exposure by market family (open stake at risk).
    by_basis: Dict[str, Dict[str, float]] = {}
    opens = store.open_bets(con)
    for b in opens:
        d = by_basis.setdefault(str(b["resolution_basis"]), {"n": 0, "stake": 0.0})
        d["n"] += 1
        d["stake"] += float(b["stake_usd"])
    if by_basis:
        lines.append("\n*Open exposure by market*")
        for basis, d in sorted(by_basis.items(), key=lambda kv: -kv[1]["stake"]):
            lines.append("  %-9s %2d  $%.0f" % (basis, d["n"], d["stake"]))

    # Settled P&L by basis (performance), where any have resolved.
    settled = {k: v for k, v in (rep.get("by_basis") or {}).items() if v.get("pl")}
    if settled:
        lines.append("\n*Settled P&L by market*")
        for basis, d in sorted(settled.items(), key=lambda kv: kv[1]["pl"]):
            lines.append("  %-9s n=%-3d $%+.2f" % (basis, d["n"], d["pl"]))

    if opens:
        lines.append("\n*Open positions* (top 12 by stake)")
        for b in sorted(opens, key=lambda x: -float(x["stake_usd"]))[:12]:
            lines.append("  #%-4d [%s] %-26s @%2.0f¢ $%.0f"
                         % (b["id"], b["resolution_basis"], str(b["selection"])[:26],
                            float(b["entry_price"]) * 100, float(b["stake_usd"])))
    lines.append("\n_paper-only · isolated from the real ledger · marks refresh each 10-min cycle_")
    return "\n".join(lines)


def _paper_decisions_report(db_path: Optional[str] = None) -> str:
    """Decision-quality (add/trim/close) of the paper book — process + outcome."""
    import os as _os

    path = db_path or _paper_book_db()
    if not _os.path.exists(path):
        return "🧪 *Paper decisions* — book not initialised yet."
    try:
        from wca.testbook import store, settle as S
    except Exception as exc:  # pragma: no cover
        return "🧪 *Paper decisions* — module unavailable (%s)." % exc
    con = store.connect(path)
    counts = {r[0]: r[1] for r in con.execute(
        "SELECT action, COUNT(*) FROM decision_events GROUP BY action")}
    total = sum(counts.values())
    if not total:
        return "🧪 *Paper decisions* — no decisions logged yet (loop seeds them)."
    lines = ["🧪 *Paper decision quality*",
             "%d decisions · %d add / %d trim / %d close"
             % (total, counts.get("add", 0), counts.get("trim", 0), counts.get("close", 0)),
             "\n*Process* (decision-time only · model-q)"]
    proc = S.process_rollup(con)
    for basis in sorted(proc):
        for qs, d in sorted(proc[basis].items()):
            lines.append("  %-9s %-12s GOG %+.3f · Δg %+.4f · capbind %.0f%% · n%d"
                         % (basis, qs, d["mean_gog"] or 0, d["mean_delta_g"] or 0,
                            100 * (d["cap_binding_rate"] or 0), d["n"]))
    calib = S.calibration_rollup(con)
    lines.append("\n*Outcome* (lagging · quarantined · validation-only)")
    if calib["by_basis"]:
        for basis, d in sorted(calib["by_basis"].items()):
            gap = d["ev_calibration_gap"]
            lines.append("  ev_gap %-9s %s [n%d%s]"
                         % (basis, ("%+.3f" % gap) if gap is not None else "n/a",
                            d["n"], " COLLECTING" if d["collecting"] else ""))
    else:
        lines.append("  ev_calibration_gap: COLLECTING")
    lines.append("  exit vs hold: $%+.2f over %d exits%s"
                 % (calib["exit_value_vs_hold"], calib["n_exits"],
                    " [INSUFFICIENT]" if calib["n_exits"] < 10 else ""))
    lines.append("\n_GOG>0 ⇒ over-Kelly there (shrink q); ev_gap>0 ⇒ model under-predicts (raise q)._")
    return "\n".join(lines)


def _help_text(manager: ConductorManager) -> str:
    lines = [
        "*WCA dev-conductor*",
        "Fan dev tasks out to headless agents — one worktree + branch + PR each.",
        "",
        "`/task <task>` — dispatch to Claude Code, headless",
        "`/claude <task>` — dispatch to Claude Code (alias of /task)",
        "`/menu` — interactive button menu",
        "`/model` — model usage: ongoing & parked tasks by agent",
        "`/agents` — agent specs & architecture",
        "`/status` — per-task table",
        "`/watch [id]` — LIVE: what each agent is doing right now",
        "`/usage` — Anthropic token spend & limits (real-time)",
        "`/prs` — open task PRs (review from your phone)",
        "`/paper` — paper test-book: exposure + performance ($2000 paper book)",
        "`/paperdecisions` — paper add/trim/close decision quality (process + calibration)",
        "`/report <id>` — send a task's report files (.md/.csv/.png…) back to you",
        "`/diff <id>` — summarised change + full .patch file",
        "`/merge <id>` — squash-merge a *green* task PR (admin)",
        "`/chart` — token-spend chart (PNG)",
        "`/log <id>` — full detail of one task",
        "`/health` — live engine auth/availability",
        "`/retry <id>` — re-run a failed task",
        "`/cancel <id>` — cancel a not-yet-started task",
        "",
        "💬 *Just type a message* (no slash) to chat with the assistant — it "
        "reads the repo to answer questions & discuss; it won't change code "
        "(use `/task` for that).",
        "📎 *Paste a screenshot* with a caption (or just a description) and the "
        "agent reads it as visual context to debug.",
        "📄 Report files a task writes are *sent back to you* automatically; "
        "`/report <id>` re-fetches them.",
        "",
        "_Sequential by default · PR-only · dry-run env · auto-notifies on start, finish & hiccups._",
    ]
    warnings = manager.preflight()
    if warnings:
        lines.append("")
        lines.append("⚠️ *Runtime checks:*")
        lines.extend("• " + w for w in warnings)
    return "\n".join(lines)


# Inline-keyboard menu (BotFather-style) + the registered slash-command list.
_MENU_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "📊 Model usage", "callback_data": "model_usage"},
         {"text": "🤖 Agents", "callback_data": "agents"}],
        [{"text": "📋 Status", "callback_data": "status"},
         {"text": "🔭 Live", "callback_data": "watch"}],
        [{"text": "💸 Usage", "callback_data": "usage"},
         {"text": "📊 Chart", "callback_data": "chart"}],
        [{"text": "🔀 PRs", "callback_data": "prs"},
         {"text": "❤️ Health", "callback_data": "health"}],
        [{"text": "🧪 Paper book", "callback_data": "paper"}],
    ]
}

_COMMANDS = [
    {"command": "task", "description": "auto-route a task to an agent"},
    {"command": "claude", "description": "dispatch a task to Claude"},
    {"command": "menu", "description": "interactive button menu"},
    {"command": "model", "description": "model usage — ongoing & parked tasks"},
    {"command": "agents", "description": "agent specs & architecture"},
    {"command": "status", "description": "per-task table"},
    {"command": "watch", "description": "live agent activity: /watch [id]"},
    {"command": "usage", "description": "Anthropic token spend & limits"},
    {"command": "prs", "description": "open task PRs"},
    {"command": "paper", "description": "paper test-book exposure + performance"},
    {"command": "paperdecisions", "description": "paper add/trim/close decision quality"},
    {"command": "report", "description": "send a task's report files: /report <id>"},
    {"command": "diff", "description": "summarised change + .patch: /diff <id>"},
    {"command": "merge", "description": "squash-merge a green PR: /merge <id>"},
    {"command": "chart", "description": "token-spend chart (PNG)"},
    {"command": "log", "description": "full detail of a task: /log <id>"},
    {"command": "health", "description": "engine auth/availability"},
    {"command": "retry", "description": "re-run a failed task: /retry <id>"},
    {"command": "cancel", "description": "cancel a queued task"},
    {"command": "help", "description": "usage + runtime checks"},
]


def _view(manager: ConductorManager, name: str) -> Optional[str]:
    """Render a named read-only view (shared by commands + menu callbacks)."""
    if name == "model_usage":
        return manager.model_usage_table()
    if name == "agents":
        return manager.agents_spec_table()
    if name == "status":
        return manager.status_table()
    if name == "watch":
        return manager.watch()
    if name == "health":
        return manager.health_table()
    if name == "usage":
        return manager.usage_table()
    if name == "prs":
        return manager.prs()
    if name == "paper":
        return _paper_book_report()
    return None


class ConductorBot:
    """Glue between Telegram long-poll and the :class:`ConductorManager`."""

    def __init__(self, client: TelegramClient, manager: ConductorManager,
                 allowed: Set[str], admin: Optional[str],
                 notify_chat: Optional[str] = None) -> None:
        self.client = client
        self.manager = manager
        self.allowed = allowed
        self.admin = admin
        self._send_lock = threading.Lock()      # requests.Session isn't thread-safe
        self._announced_start: Set[int] = set()
        self._announced_done: Set[int] = set()
        self._notify_chat = notify_chat          # proactive hiccup / health alerts
        self._stop = threading.Event()
        self._health_state: Dict[str, bool] = {}
        # Per-task lifecycle notifier (called from worker threads).
        manager.notify = self._on_update

    # -- outbound ---------------------------------------------------------

    def _send(self, chat_id: str, text: str, reply_markup: Optional[Dict] = None) -> None:
        with self._send_lock:
            try:
                self.client.send_message(chat_id, text, reply_markup=reply_markup)
            except TelegramError as exc:
                print("[conductor] send failed: %s" % exc, flush=True)

    def _on_update(self, record: TaskRecord) -> None:
        """Worker-thread callback: notify on START and on COMPLETION."""
        chat = record.chat_id or self._notify_chat
        if not chat:
            return
        if record.status == TaskStatus.RUNNING.value and record.id not in self._announced_start:
            self._announced_start.add(record.id)
            task = record.task if len(record.task) <= 60 else record.task[:57] + "..."
            self._send(chat, "⚙️ `#%d` started on *%s*\n_%s_" % (record.id, record.engine, task))
            return
        if record.finished_at is not None and record.id not in self._announced_done:
            self._announced_done.add(record.id)
            self._send(chat, self._completion_text(record))
            # Summarise the change + attach report files the task produced.
            if record.status in (TaskStatus.DONE.value, TaskStatus.PUSHED.value,
                                 TaskStatus.COMMITTED_PR_FAILED.value):
                try:
                    dig = self._diff_digest(record)
                    if dig and dig[0]:
                        self._send(chat, "🔧 `#%d` changes:\n```\n%s\n```"
                                   % (record.id, dig[0][:_DIFF_STAT_MAX]))
                except Exception as exc:  # noqa: BLE001
                    print("[conductor] diff summary failed: %s" % exc, flush=True)
                try:
                    self._send_report_files(chat, record)
                except Exception as exc:  # noqa: BLE001 - never let attach kill the notifier
                    print("[conductor] auto-attach failed: %s" % exc, flush=True)

    def _health_watch(self, interval: float = 300.0) -> None:
        """Background watcher: alert the notify chat when an engine's health flips."""
        while not self._stop.is_set():
            self._prune_uploads()  # age out stale pasted screenshots (keeps disk bounded)
            for e in Engine:
                ev = e.value
                try:
                    ok = self.manager.engine_health(ev, force=True).ok
                    reason = self.manager.engine_health(ev).reason
                except Exception:  # noqa: BLE001 - watcher must never die
                    continue
                prev = self._health_state.get(ev)
                if prev is not None and prev != ok and self._notify_chat:
                    badge = "🟢 recovered" if ok else "⚠️ unavailable"
                    self._send(self._notify_chat, "%s *%s* — %s" % (badge, ev, reason))
                self._health_state[ev] = ok
            self._stop.wait(interval)

    @staticmethod
    def _completion_text(r: TaskRecord) -> str:
        if r.status == TaskStatus.DONE.value:
            head = "✅ `#%d` %s done — PR opened" % (r.id, r.engine)
        elif r.status == TaskStatus.COMMITTED_PR_FAILED.value:
            # The work is committed + pushed; only the PR step failed. This MUST
            # be loud and explicit — never a silent "stranded on a local branch".
            head = "⚠️ `#%d` %s committed but the *PR step FAILED* — your work is SAFE on the branch" % (
                r.id, r.engine)
        elif r.status == TaskStatus.PUSHED.value:
            head = "⬆️ `#%d` %s pushed (open the PR manually)" % (r.id, r.engine)
        elif r.status == TaskStatus.NO_CHANGES.value:
            head = "∅ `#%d` %s ran but made no changes" % (r.id, r.engine)
        else:
            head = "❌ `#%d` %s failed" % (r.id, r.engine)
        lines = [head, "_%s_" % (r.task if len(r.task) <= 70 else r.task[:67] + "...")]
        if r.status == TaskStatus.COMMITTED_PR_FAILED.value and r.branch:
            lines.append("branch: `%s`" % r.branch)
        if r.pr_url:
            lines.append(r.pr_url)
        if r.tokens:
            lines.append("Tokens: %d" % r.tokens)
        if r.error and r.status in {TaskStatus.FAILED.value, TaskStatus.PUSHED.value,
                                    TaskStatus.COMMITTED_PR_FAILED.value}:
            lines.append("⚠️ %s" % (r.error if len(r.error) <= 200 else r.error[:197] + "..."))
        if r.status in {TaskStatus.FAILED.value, TaskStatus.NO_CHANGES.value}:
            lines.append("→ `/retry %d` · `/log %d`" % (r.id, r.id))
        if r.status == TaskStatus.COMMITTED_PR_FAILED.value:
            lines.append("→ retrying the PR automatically; `/retry %d` to retry now · `/log %d`"
                         % (r.id, r.id))
        return "\n".join(lines)

    # -- inbound ----------------------------------------------------------

    @staticmethod
    def _attach_note(images: Optional[List[str]]) -> str:
        n = len(images or [])
        if not n:
            return ""
        return "\n📎 %d screenshot%s attached for context." % (n, "" if n == 1 else "s")

    def _dispatch(self, engine: str, arg: str, chat_id: str, user_id: str,
                  images: Optional[List[str]] = None) -> str:
        if not _is_admin(user_id, self.admin):
            return "🚫 Not authorized to dispatch tasks."
        task = arg.strip()
        if not task:
            return "Usage: `/%s <task>`" % engine
        record = self.manager.submit(engine, task, chat_id=chat_id, images=images)
        if record.status == TaskStatus.REJECTED.value:
            return "🚫 `#%d` rejected: %s" % (record.id, record.error)
        return (
            "🚀 `#%d` dispatched to *%s* on `%s`.%s\n"
            "I'll post the PR link when it finishes. `/status` to track."
            % (record.id, engine, record.branch, self._attach_note(images))
        )

    def _dispatch_auto(self, arg: str, chat_id: str, user_id: str,
                       images: Optional[List[str]] = None) -> str:
        if not _is_admin(user_id, self.admin):
            return "🚫 Not authorized to dispatch tasks."
        task = arg.strip()
        if not task:
            return "Usage: `/task <task>`"
        record = self.manager.submit_auto(task, chat_id=chat_id, images=images)
        if record.status == TaskStatus.REJECTED.value:
            return "🚫 `#%d` rejected: %s" % (record.id, record.error)
        reason = "\nRoute: %s" % record.route_reason if record.route_reason else ""
        return (
            "🚀 `#%d` routed to *%s* on `%s`.%s%s\n"
            "I'll post the PR link when it finishes. `/status` to track."
            % (record.id, record.engine, record.branch, reason, self._attach_note(images))
        )

    # -- attachments ------------------------------------------------------

    def _uploads_dir(self) -> Path:
        return Path(self.manager.cfg.repo_root) / "data" / "conductor_uploads"

    def _save_one(self, message: Dict[str, object]) -> Optional[str]:
        """Download a single message's image to the uploads dir; None on failure."""
        stem = uuid.uuid4().hex[:8]
        try:
            return self.client.save_image(message, self._uploads_dir(), stem)
        except TelegramError as exc:
            print("[conductor] image download failed: %s" % exc, flush=True)
            return None

    def _save_message_images(self, message: Dict[str, object]) -> List[str]:
        """Download the screenshot attached to *message*. Empty on failure."""
        path = self._save_one(message)
        return [path] if path else []

    def _save_album_images(self, members: List[Dict[str, object]]) -> List[str]:
        """Download every image across an album's member messages (capped)."""
        paths: List[str] = []
        for m in members[:_MAX_ALBUM_IMAGES]:
            p = self._save_one(m)
            if p:
                paths.append(p)
        return paths

    def _prune_uploads(self, max_age_hours: float = 24.0) -> None:
        """Best-effort: delete pasted screenshots older than *max_age_hours*.

        The originals in ``data/conductor_uploads/`` outlive a task (so `/retry`
        can re-stage them), but on a long-running host they would otherwise grow
        unbounded. Age-based, never eager, so an in-flight retry isn't starved.
        """
        d = self._uploads_dir()
        if not d.exists():
            return
        cutoff = time.time() - max_age_hours * 3600.0
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for f in entries:
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    # -- task output: report files + charts -------------------------------

    def _task_report_files(self, record: TaskRecord) -> List[Tuple[str, bytes, bool]]:
        """Report-like files a task ADDED/MODIFIED, as ``(name, bytes, is_image)``.

        Read straight from the task's branch via git (so it works even after the
        worktree is reclaimed), filtered to report extensions, excluding generated
        data/site feeds, size-capped to Telegram's limits, and count-capped.
        """
        if not record.branch:
            return []
        cfg = self.manager.cfg
        git, repo, base = cfg.git_bin, str(cfg.repo_root), cfg.base_branch
        try:
            listing = subprocess.run(
                [git, "-C", repo, "diff", "--name-only", "--diff-filter=AM",
                 "%s...%s" % (base, record.branch)],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if listing.returncode != 0:
            return []
        out: List[Tuple[str, bytes, bool]] = []
        for path in listing.stdout.splitlines():
            path = path.strip()
            if not path or os.path.splitext(path)[1].lower() not in _REPORT_EXTS:
                continue
            if any(path.startswith(p) for p in _REPORT_EXCLUDE_PREFIXES):
                continue
            try:
                blob = subprocess.run(  # no text=True -> raw bytes (images!)
                    [git, "-C", repo, "show", "%s:%s" % (record.branch, path)],
                    capture_output=True, timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if blob.returncode != 0 or not blob.stdout:
                continue
            is_image = os.path.splitext(path)[1].lower() in _IMAGE_EXTS
            if len(blob.stdout) > (_TELEGRAM_PHOTO_MAX if is_image else _TELEGRAM_DOC_MAX):
                continue
            out.append((os.path.basename(path), blob.stdout, is_image))
            if len(out) >= _MAX_REPORT_FILES:
                break
        return out

    def _send_report_files(self, chat_id: str, record: TaskRecord) -> int:
        """Send a task's report files (images inline, the rest as documents)."""
        files = self._task_report_files(record)
        sent = 0
        for name, content, is_image in files:
            caption = "📄 %s · from #%d" % (name, record.id)
            try:
                with self._send_lock:
                    if is_image:
                        self.client.send_photo(chat_id, content, filename=name, caption=caption)
                    else:
                        self.client.send_document(chat_id, content, filename=name, caption=caption)
                sent += 1
            except TelegramError as exc:
                print("[conductor] report send failed (%s): %s" % (name, exc), flush=True)
        return sent

    def _diff_digest(self, record: TaskRecord) -> Optional[Tuple[str, bytes]]:
        """A task's change as ``(stat_summary, full_patch_bytes)`` vs base.

        ``git diff --stat base...branch`` gives the summarised view; the full
        unified diff rides along as a ``.patch`` the caller can attach. Returns
        None if the task has no branch or git fails.
        """
        if not record.branch:
            return None
        cfg = self.manager.cfg
        git, repo, base = cfg.git_bin, str(cfg.repo_root), cfg.base_branch
        spec = "%s...%s" % (base, record.branch)
        try:
            stat = subprocess.run([git, "-C", repo, "diff", "--stat", spec],
                                  capture_output=True, text=True, timeout=30)
            patch = subprocess.run([git, "-C", repo, "diff", spec],
                                   capture_output=True, timeout=30)  # bytes
        except (OSError, subprocess.SubprocessError):
            return None
        if stat.returncode != 0:
            return None
        summary = (stat.stdout or "").strip()
        patch_bytes = patch.stdout if patch.returncode == 0 else b""
        if not summary:
            return None
        return summary, patch_bytes

    def _send_diff(self, chat_id: str, record: TaskRecord) -> bool:
        """Send a task's summarised diff (stat inline + full .patch attached)."""
        dig = self._diff_digest(record)
        if dig is None:
            return False
        summary, patch = dig
        self._send(chat_id, "🔧 *#%d diff* · `%s`\n```\n%s\n```" % (
            record.id, record.branch, summary[:_DIFF_STAT_MAX]))
        if patch and len(patch) <= _DIFF_PATCH_MAX:
            try:
                with self._send_lock:
                    self.client.send_document(chat_id, patch, filename="task-%d.patch" % record.id,
                                              caption="full diff · #%d" % record.id)
            except TelegramError as exc:
                print("[conductor] diff attach failed: %s" % exc, flush=True)
        return True

    def _render_usage_chart(self) -> Optional[bytes]:
        """Render conductor token-spend-by-task as a PNG. None if matplotlib is
        unavailable (the chart command then falls back to the text table)."""
        try:
            import matplotlib
            matplotlib.use("Agg")  # headless: no display in the bot process
            import matplotlib.pyplot as plt
        except Exception:  # noqa: BLE001 - matplotlib is an optional extra
            return None
        recs = [r for r in self.manager.records() if r.tokens]
        if not recs:
            return None
        recs = sorted(recs, key=lambda r: r.tokens)[-15:]  # top 15 spenders
        labels = ["#%d %s" % (r.id, r.engine) for r in recs]
        vals = [r.tokens for r in recs]
        colors = ["#d97706" if r.engine == "claude" else "#2563eb" for r in recs]
        try:
            fig, ax = plt.subplots(figsize=(7.5, max(2.5, 0.42 * len(recs))))
            ax.barh(labels, vals, color=colors)
            ax.set_xlabel("tokens")
            ax.set_title("Conductor token spend by task")
            for i, v in enumerate(vals):
                ax.text(v, i, " %s" % format(v, ","), va="center", fontsize=8)
            fig.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120)
            plt.close(fig)
            return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            print("[conductor] chart render failed: %s" % exc, flush=True)
            try:
                plt.close("all")
            except Exception:
                pass
            return None

    def handle(self, message: Dict[str, object],
               images_override: Optional[List[str]] = None) -> Optional[str]:
        # A photo carries its command/description in `caption`, not `text`.
        text = _msg_text(message)
        has_image = bool(largest_photo_file_id(message) or image_document_file_id(message)) \
            or bool(images_override)
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = str(chat.get("id", ""))  # type: ignore[union-attr]
        user_id = str(user.get("id", ""))  # type: ignore[union-attr]

        if not text and not has_image:
            return None
        # A bare screenshot with no instruction isn't actionable — ask for one.
        if has_image and not text:
            return (
                "📎 Got your screenshot. Add a caption telling me what to do with it, e.g.\n"
                "`/claude debug why this layout breaks` — or just describe it and I'll auto-route."
            )

        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@", 1)[0]  # strip @botname in groups
        _DISPATCH = ("/task", "/claude")
        is_dispatch = (not cmd.startswith("/")) or cmd in _DISPATCH

        # Admin-gate dispatch BEFORE any download, so a non-admin can't trigger a
        # wasted getFile+CDN fetch (and an orphaned file) just by attaching a photo.
        if has_image and is_dispatch and not _is_admin(user_id, self.admin):
            return "🚫 Not authorized to dispatch tasks."

        # Resolve the screenshot(s): album members are pre-downloaded and passed
        # in via images_override; a lone photo is downloaded here, on demand.
        images: List[str] = []
        download_failed = False
        if has_image and is_dispatch:
            if images_override is not None:
                images = list(images_override)
            else:
                images = self._save_message_images(message)
            # has_image guaranteed a file_id, so an empty result means the
            # download actually failed — tell the user instead of dispatching blind.
            download_failed = not images
        warn = ("\n⚠️ couldn't download your screenshot — running without it; "
                "re-send to attach." if download_failed else "")

        # Image + a plain (non-slash) caption → auto-route the caption as a task,
        # so "snap a screenshot, describe the bug, send" just works.
        if has_image and not cmd.startswith("/"):
            return self._dispatch_auto(text, chat_id, user_id, images=images) + warn

        if cmd in ("/start", "/help"):
            return _help_text(self.manager)
        if cmd == "/menu":
            self._send(chat_id, "*WCA dev-conductor* — pick a view:", reply_markup=_MENU_KEYBOARD)
            return None
        if cmd == "/model":
            return self.manager.model_usage_table()
        if cmd == "/agents":
            return self.manager.agents_spec_table()
        if cmd == "/status":
            return self.manager.status_table()
        if cmd == "/watch":
            if arg.strip():
                try:
                    return self.manager.watch(int(arg.strip()))
                except ValueError:
                    return "Usage: `/watch [id]`"
            return self.manager.watch()
        if cmd == "/health":
            return self.manager.health_table()
        if cmd == "/usage":
            return self.manager.usage_table()
        if cmd == "/prs":
            return self.manager.prs()
        if cmd in ("/paper", "/testbook"):
            return _paper_book_report()
        if cmd in ("/paperdecisions", "/decisions"):
            return _paper_decisions_report()
        if cmd == "/log":
            try:
                return self.manager.task_detail(int(arg.strip()))
            except ValueError:
                return "Usage: `/log <id>`"
        if cmd == "/report":
            try:
                tid = int(arg.strip())
            except ValueError:
                return "Usage: `/report <id>` — send the report files a task produced"
            rec = self.manager.get(tid)
            if rec is None:
                return "No task `#%d`." % tid
            n = self._send_report_files(chat_id, rec)
            if n:
                return None  # the files (with captions) are the reply
            return "No report files (.md/.csv/.txt/.png/.svg/.pdf) in `#%d`." % tid
        if cmd == "/diff":
            try:
                tid = int(arg.strip())
            except ValueError:
                return "Usage: `/diff <id>` — summarised change + full .patch"
            rec = self.manager.get(tid)
            if rec is None:
                return "No task `#%d`." % tid
            if not self._send_diff(chat_id, rec):
                return "No diff for `#%d` (no branch / nothing changed)." % tid
            return None  # the digest + patch ARE the reply
        if cmd == "/merge":
            if not _is_admin(user_id, self.admin):
                return "🚫 Not authorized."
            try:
                tid = int(arg.strip())
            except ValueError:
                return "Usage: `/merge <id>` — squash-merge a GREEN task PR"
            ok, msg = self.manager.merge_task(tid)
            return ("✅ " if ok else "🚫 ") + msg
        if cmd == "/chart":
            png = self._render_usage_chart()
            if png is None:
                return ("📊 No chart available (need `matplotlib`, or no token "
                        "spend yet).\n\n" + self.manager.usage_table())
            try:
                with self._send_lock:
                    self.client.send_photo(chat_id, png, filename="conductor_usage.png",
                                           caption="📊 Conductor token spend by task")
            except TelegramError as exc:
                return "Chart send failed: %s" % exc
            return None
        if cmd == "/retry":
            if not _is_admin(user_id, self.admin):
                return "🚫 Not authorized."
            try:
                tid = int(arg.strip())
            except ValueError:
                return "Usage: `/retry <id>`"
            rec = self.manager.retry(tid)
            if rec is None:
                return "No task `#%d`." % tid
            if rec.id != tid:
                # re-dispatched as a fresh task (failed / no-change / interrupted)
                return "🔁 retrying as `#%d` on *%s* (`%s`)." % (rec.id, rec.engine, rec.branch)
            # same record returned → an in-place PR retry, or nothing to retry.
            if rec.status == TaskStatus.DONE.value:
                return "✅ `#%d` PR opened on retry.\n%s" % (tid, rec.pr_url or "")
            if rec.status == TaskStatus.COMMITTED_PR_FAILED.value:
                err = rec.error if len(rec.error or "") <= 200 else rec.error[:197] + "..."
                return ("⚠️ `#%d` PR retry still failing — your commit is SAFE on `%s`.\n⚠️ %s\n"
                        "Try `/retry %d` again once `gh` auth/network is healthy."
                        % (tid, rec.branch, err, tid))
            return "Can't retry `#%d` (status %s — only failed / no-change / PR-failed tasks)." % (tid, rec.status)
        if cmd == "/task":
            return self._dispatch_auto(arg, chat_id, user_id, images=images) + warn
        if cmd == "/claude":
            return self._dispatch(Engine.CLAUDE.value, arg, chat_id, user_id, images=images) + warn
        if cmd == "/cancel":
            if not _is_admin(user_id, self.admin):
                return "🚫 Not authorized."
            try:
                tid = int(arg.strip())
            except ValueError:
                return "Usage: `/cancel <id>`"
            record = self.manager.cancel(tid)
            if record is None:
                return "No task `#%s`." % arg.strip()
            if record.status == TaskStatus.REJECTED.value and record.error == "cancelled before start":
                return "🚫 `#%d` cancelled." % tid
            return "Can't cancel `#%d` (already %s)." % (tid, record.status)
        if cmd.startswith("/"):
            return "Unknown command. `/help` for usage."
        # Plain, non-slash text (and replies in a chat thread) → conversational
        # agent. This was previously dropped silently; now it's an interactive
        # assistant. /task and /claude remain the PR-spawning paths.
        return self._chat(text, chat_id, user_id)

    # -- conversational chat ----------------------------------------------

    def _chat(self, text: str, chat_id: str, user_id: str) -> Optional[str]:
        """Kick off a bounded conversational reply in the background.

        Returns None: the reply is sent from the worker thread so a slow agent
        turn never blocks the poll loop. Admin-gated (chat spends tokens).
        """
        if not _is_admin(user_id, self.admin):
            return "🚫 Chat is restricted to the admin."
        threading.Thread(
            target=self._chat_worker, args=(text, chat_id),
            name="conductor-chat", daemon=True,
        ).start()
        return None

    def _chat_worker(self, text: str, chat_id: str) -> None:
        try:
            reply = self.manager.chat(chat_id, text)
        except Exception as exc:  # noqa: BLE001 - never die on one reply
            reply = "⚠️ chat error: %s" % exc
        if reply:
            self._send(chat_id, reply)

    def handle_callback(self, cb: Dict[str, object]) -> None:
        """Render the view for a tapped inline-keyboard button."""
        data = str(cb.get("data") or "")
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))  # type: ignore[union-attr]
        with self._send_lock:
            try:
                self.client.answer_callback_query(str(cb.get("id")))  # dismiss spinner
            except TelegramError:
                pass
        if not _authorized(chat_id, self.allowed):
            return
        if data == "chart":  # sends a photo, not a text view
            png = self._render_usage_chart()
            if png is None:
                self._send(chat_id, "📊 No chart available yet.\n\n" + self.manager.usage_table())
                return
            try:
                with self._send_lock:
                    self.client.send_photo(chat_id, png, filename="conductor_usage.png",
                                           caption="📊 Conductor token spend by task")
            except TelegramError as exc:
                self._send(chat_id, "Chart send failed: %s" % exc)
            return
        text = _view(self.manager, data)
        if text:
            self._send(chat_id, text)

    def _handle_message(self, message: Dict[str, object], chat_id: str) -> None:
        """Route one (non-album) message and send any reply."""
        try:
            reply = self.handle(message)
        except Exception as exc:  # noqa: BLE001 - never die on one message
            reply = "Error: %s" % exc
        if reply:
            self._send(chat_id, reply)

    def _handle_album(self, members: List[Dict[str, object]]) -> None:
        """Route a Telegram album (multiple images, one shared caption) as ONE task.

        Telegram splits an album across several updates; the caption rides on
        exactly one member. We dispatch once via the captioned member with every
        image attached, and download only when it will actually be used (a
        caption is present AND the sender is an admin).
        """
        base = next((m for m in members if _msg_text(m)), members[0])
        chat_id = str((base.get("chat") or {}).get("id", ""))  # type: ignore[union-attr]
        user_id = str((base.get("from") or {}).get("id", ""))  # type: ignore[union-attr]
        images: List[str] = []
        if _msg_text(base) and _is_admin(user_id, self.admin):
            images = self._save_album_images(members)
        try:
            reply = self.handle(base, images_override=images)
        except Exception as exc:  # noqa: BLE001
            reply = "Error: %s" % exc
        if reply:
            self._send(chat_id, reply)

    def _announce_interrupted(self) -> None:
        """Message the user about each task a restart interrupted (re-run hint)."""
        try:
            interrupted = self.manager.take_interrupted()
        except Exception as exc:  # noqa: BLE001
            print("[conductor] take_interrupted failed: %s" % exc, flush=True)
            return
        for r in interrupted:
            chat = r.chat_id or self._notify_chat
            if not chat:
                continue
            task = r.task if len(r.task) <= 70 else r.task[:67] + "..."
            self._send(chat, "♻️ `#%d` was interrupted by a restart — re-run with "
                       "`/retry %d`?\n_%s_" % (r.id, r.id, task))

    def _retry_pending_prs(self) -> None:
        """On startup, recover every committed-but-PR-failed task: tell the user
        the work is safe, retry the PR now that auth may be healthy, and report
        the outcome. Runs in a background thread (gh can be slow)."""
        try:
            pending = self.manager.take_pr_retry_pending()
        except Exception as exc:  # noqa: BLE001
            print("[conductor] take_pr_retry_pending failed: %s" % exc, flush=True)
            return
        for r in pending:
            chat = r.chat_id or self._notify_chat
            task = r.task if len(r.task) <= 70 else r.task[:67] + "..."
            if chat:
                self._send(chat, "⚠️ `#%d` committed but its PR didn't complete — your work is "
                           "SAFE on `%s`. Retrying the PR now…\n_%s_" % (r.id, r.branch, task))
            try:
                updated = self.manager.retry_pr(r.id)
            except Exception as exc:  # noqa: BLE001 - never die on one retry
                print("[conductor] PR retry failed for #%d: %s" % (r.id, exc), flush=True)
                continue
            if chat and updated is not None:
                if updated.status == TaskStatus.DONE.value:
                    self._send(chat, "✅ `#%d` PR opened on retry.\n%s" % (r.id, updated.pr_url or ""))
                else:
                    err = updated.error if len((updated.error or "")) <= 200 else updated.error[:197] + "..."
                    self._send(chat, "⚠️ `#%d` PR still failing — commit SAFE on `%s`.\n⚠️ %s\n"
                               "`/retry %d` once `gh` auth/network is healthy."
                               % (r.id, updated.branch, err, r.id))

    # -- main loop --------------------------------------------------------

    def run(self, poll_timeout: int = 25) -> None:
        warnings = self.manager.preflight()
        print("[conductor] online (cap=%d, budget=%s)" % (
            self.manager.cfg.max_parallel, self.manager.cfg.token_budget or "∞"), flush=True)
        for w in warnings:
            print("[conductor] WARN: %s" % w.replace("`", ""), flush=True)
        try:
            self.client.set_my_commands(_COMMANDS)  # populate Telegram's '/' menu
        except TelegramError as exc:
            print("[conductor] set_my_commands failed: %s" % exc, flush=True)
        self._prune_uploads()  # clear stale pasted screenshots left over from a prior run
        # background watcher: alerts the notify chat when an engine flips up/down
        threading.Thread(target=self._health_watch, name="health-watch", daemon=True).start()
        if self._notify_chat:
            cap = self.manager.cfg.max_parallel
            mode = "sequential" if cap <= 1 else "%d-way swarm" % cap
            self._send(self._notify_chat, "🟢 *conductor online* — %s. /help" % mode)
        # Tell the user about any task that was in flight when we last died, so a
        # restart never loses work silently.
        self._announce_interrupted()
        # Recover any task that committed but whose PR step failed/didn't finish:
        # surface it + retry the PR now that auth may be healthy (background so a
        # slow gh never delays the poll loop).
        threading.Thread(target=self._retry_pending_prs, name="pr-retry", daemon=True).start()

        offset: Optional[int] = None
        import time as _time
        poll_fails = 0
        while True:
            try:
                updates = self.client.get_updates(offset=offset, poll_timeout=poll_timeout)
                poll_fails = 0
            except TelegramError as exc:
                poll_fails += 1
                print("[conductor] poll error: %s" % exc, flush=True)
                if poll_fails == 3 and self._notify_chat:  # alert once on a sustained outage
                    self._send(self._notify_chat, "⚠️ *conductor poll error* — %s" % str(exc)[:150])
                _time.sleep(5)
                continue
            albums: Dict[str, List[Dict[str, object]]] = {}
            for update in updates:
                offset = int(update["update_id"]) + 1
                callback = update.get("callback_query")
                if callback:
                    try:
                        self.handle_callback(callback)
                    except Exception as exc:  # noqa: BLE001 - never die on one update
                        print("[conductor] callback error: %s" % exc, flush=True)
                    continue
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if not _authorized(chat_id, self.allowed):
                    self._send(chat_id, "Unauthorized chat `%s`." % chat_id)
                    continue
                # Buffer album members (multiple images, one send) and dispatch the
                # whole group once after the batch — so extra members don't each
                # trigger a spurious "add a caption" reply.
                mgid = message.get("media_group_id")
                if mgid:
                    albums.setdefault(str(mgid), []).append(message)
                    continue
                self._handle_message(message, chat_id)
            for members in albums.values():
                try:
                    self._handle_album(members)
                except Exception as exc:  # noqa: BLE001 - never die on one album
                    print("[conductor] album error: %s" % exc, flush=True)


def _build_config(args: argparse.Namespace) -> ConductorConfig:
    overrides: Dict[str, object] = {}
    if args.max_parallel is not None:
        overrides["max_parallel"] = args.max_parallel
    if args.token_budget is not None:
        overrides["token_budget"] = args.token_budget
    if args.base_branch is not None:
        overrides["base_branch"] = args.base_branch
    if args.branch_prefix is not None:
        overrides["branch_prefix"] = args.branch_prefix
    if args.no_pr:
        overrides["create_pr"] = False
    if args.no_push:
        overrides["push"] = False
        overrides["create_pr"] = False  # can't PR what isn't pushed
    return ConductorConfig.from_env(_REPO_ROOT, **overrides)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="WCA dev-conductor Telegram bot")
    parser.add_argument("--env", default=".env.dev", help="dotenv file to load (default: .env.dev)")
    parser.add_argument("--max-parallel", type=int, default=None, help="max concurrent agents")
    parser.add_argument("--token-budget", type=int, default=None, help="total token ceiling (0=unlimited)")
    parser.add_argument("--base-branch", default=None, help="branch to fork tasks from (default: main)")
    parser.add_argument("--branch-prefix", default=None, help="task branch prefix (default: conductor)")
    parser.add_argument("--no-pr", action="store_true", help="push but don't open PRs (compare links only)")
    parser.add_argument("--no-push", action="store_true", help="local-only: no push, no PR (smoke test)")
    parser.add_argument("--poll-timeout", type=int, default=25, help="Telegram long-poll seconds")
    args = parser.parse_args(argv)

    _load_dotenv(args.env)

    # Refuse to run against the live ledger: this bot is dev-only.
    db_path = os.environ.get("WCA_DB_PATH", "")
    if db_path and Path(db_path).name == "wca.db":
        print("[conductor] REFUSING to start: WCA_DB_PATH points at the live "
              "ledger (%s). Use .env.dev (data/dev.db)." % db_path, flush=True)
        return 2

    cfg = _build_config(args)
    # Durable task + chat state survives KeepAlive/sleep restarts. Disable by
    # setting WCA_CONDUCTOR_STATE_DB="off"; override the path otherwise.
    store = None
    state_db = os.environ.get("WCA_CONDUCTOR_STATE_DB") or str(_REPO_ROOT / "data" / "conductor_state.db")
    if state_db.lower() not in ("off", "none", "0"):
        try:
            from wca.conductor.store import ConductorStore  # noqa: WPS433 - optional dep-free store
            store = ConductorStore(state_db)
            print("[conductor] state store: %s" % state_db, flush=True)
        except Exception as exc:  # noqa: BLE001 - run without persistence rather than fail to start
            print("[conductor] state store disabled (%s)" % exc, flush=True)
    manager = ConductorManager(cfg, store=store)
    try:
        client = TelegramClient()
    except TelegramError as exc:
        print("[conductor] %s" % exc, flush=True)
        return 1

    allowed = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
    admin = os.environ.get("TELEGRAM_ADMIN_USER_ID") or None
    # proactive notifications (start/finish/hiccups) go here; default = the chat id
    notify_chat = os.environ.get("WCA_CONDUCTOR_NOTIFY_CHAT") or (sorted(allowed)[0] if allowed else None)
    bot = ConductorBot(client, manager, allowed, admin, notify_chat=notify_chat)
    try:
        bot.run(poll_timeout=args.poll_timeout)
    except KeyboardInterrupt:
        print("\n[conductor] shutting down", flush=True)
    finally:
        manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
