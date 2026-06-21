#!/usr/bin/env python
"""WCA dev-conductor — a *separate* Telegram bot that fans tasks out to
headless coding agents (``claude -p`` / ``codex exec``), one git worktree +
branch + PR per task.

This is dev infrastructure, NOT the betting bot. It must run on a dev box with
``.env.dev`` (dry-run, dev DB, no live keys). Launch from the MAIN checkout so
worktrees land in ``.claude/worktrees`` where the cleanup script expects them::

    python scripts/wca_conductor.py --env .env.dev

Commands (admin-gated where they spend tokens / write branches):

    /task <task>     auto-route: Claude-first; Codex only for small mechanical edits
    /claude <task>   dispatch a task to Claude Code, headless
    /codex  <task>   dispatch a task to Codex, headless
    /status          show the per-task table (read from real results)
    /cancel <id>     cancel a task that hasn't started yet
    /help            usage + runtime warnings

Guardrails: PR-only (fresh branch per task, never ``main``), a hard
max-parallel cap, an optional token budget, and a dry-run/no-secrets env for
every spawned agent. See :mod:`wca.conductor`.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

# Make ``src`` importable when run from a worktree (editable install resolves to
# the main checkout otherwise — the known worktree PYTHONPATH quirk).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wca.bot.telegram import TelegramClient, TelegramError  # noqa: E402
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


def _help_text(manager: ConductorManager) -> str:
    lines = [
        "*WCA dev-conductor*",
        "Fan dev tasks out to headless agents — one worktree + branch + PR each.",
        "",
        "`/task <task>` — auto-route (Claude-first; Codex for tiny mechanical edits)",
        "`/claude <task>` — dispatch to Claude Code",
        "`/codex <task>` — dispatch to Codex",
        "`/status` — per-task table",
        "`/cancel <id>` — cancel a not-yet-started task",
        "",
        "_PR-only · max-parallel cap · dry-run env (no live keys/ledger)._",
    ]
    warnings = manager.preflight()
    if warnings:
        lines.append("")
        lines.append("⚠️ *Runtime checks:*")
        lines.extend("• " + w for w in warnings)
    return "\n".join(lines)


class ConductorBot:
    """Glue between Telegram long-poll and the :class:`ConductorManager`."""

    def __init__(self, client: TelegramClient, manager: ConductorManager,
                 allowed: Set[str], admin: Optional[str]) -> None:
        self.client = client
        self.manager = manager
        self.allowed = allowed
        self.admin = admin
        self._send_lock = threading.Lock()      # requests.Session isn't thread-safe
        self._announced: Set[int] = set()
        # Completion notifier (called from worker threads).
        manager.notify = self._on_update

    # -- outbound ---------------------------------------------------------

    def _send(self, chat_id: str, text: str) -> None:
        with self._send_lock:
            try:
                self.client.send_message(chat_id, text)
            except TelegramError as exc:
                print("[conductor] send failed: %s" % exc, flush=True)

    def _on_update(self, record: TaskRecord) -> None:
        """Worker-thread callback: announce each task once, when it finishes."""
        if record.finished_at is None:
            return
        if record.id in self._announced:
            return
        self._announced.add(record.id)
        if not record.chat_id:
            return
        self._send(record.chat_id, self._completion_text(record))

    @staticmethod
    def _completion_text(r: TaskRecord) -> str:
        if r.status == TaskStatus.DONE.value:
            head = "✅ `#%d` %s done — PR opened" % (r.id, r.engine)
        elif r.status == TaskStatus.PUSHED.value:
            head = "⬆️ `#%d` %s pushed (open the PR manually)" % (r.id, r.engine)
        elif r.status == TaskStatus.NO_CHANGES.value:
            head = "∅ `#%d` %s ran but made no changes" % (r.id, r.engine)
        else:
            head = "❌ `#%d` %s failed" % (r.id, r.engine)
        lines = [head, "_%s_" % (r.task if len(r.task) <= 70 else r.task[:67] + "...")]
        if r.pr_url:
            lines.append(r.pr_url)
        if r.tokens:
            lines.append("Tokens: %d" % r.tokens)
        if r.error and r.status in {TaskStatus.FAILED.value, TaskStatus.PUSHED.value}:
            lines.append("⚠️ %s" % (r.error if len(r.error) <= 200 else r.error[:197] + "..."))
        return "\n".join(lines)

    # -- inbound ----------------------------------------------------------

    def _dispatch(self, engine: str, arg: str, chat_id: str, user_id: str) -> str:
        if not _is_admin(user_id, self.admin):
            return "🚫 Not authorized to dispatch tasks."
        task = arg.strip()
        if not task:
            return "Usage: `/%s <task>`" % engine
        record = self.manager.submit(engine, task, chat_id=chat_id)
        if record.status == TaskStatus.REJECTED.value:
            return "🚫 `#%d` rejected: %s" % (record.id, record.error)
        return (
            "🚀 `#%d` dispatched to *%s* on `%s`.\n"
            "I'll post the PR link when it finishes. `/status` to track."
            % (record.id, engine, record.branch)
        )

    def _dispatch_auto(self, arg: str, chat_id: str, user_id: str) -> str:
        if not _is_admin(user_id, self.admin):
            return "🚫 Not authorized to dispatch tasks."
        task = arg.strip()
        if not task:
            return "Usage: `/task <task>`"
        record = self.manager.submit_auto(task, chat_id=chat_id)
        if record.status == TaskStatus.REJECTED.value:
            return "🚫 `#%d` rejected: %s" % (record.id, record.error)
        reason = "\nRoute: %s" % record.route_reason if record.route_reason else ""
        return (
            "🚀 `#%d` routed to *%s* on `%s`.%s\n"
            "I'll post the PR link when it finishes. `/status` to track."
            % (record.id, record.engine, record.branch, reason)
        )

    def handle(self, message: Dict[str, object]) -> Optional[str]:
        text = str(message.get("text") or "").strip()
        if not text:
            return None
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@", 1)[0]  # strip @botname in groups
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = str(chat.get("id", ""))  # type: ignore[union-attr]
        user_id = str(user.get("id", ""))  # type: ignore[union-attr]

        if cmd in ("/start", "/help"):
            return _help_text(self.manager)
        if cmd == "/status":
            return self.manager.status_table()
        if cmd == "/task":
            return self._dispatch_auto(arg, chat_id, user_id)
        if cmd == "/claude":
            return self._dispatch(Engine.CLAUDE.value, arg, chat_id, user_id)
        if cmd == "/codex":
            return self._dispatch(Engine.CODEX.value, arg, chat_id, user_id)
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
        return None

    # -- main loop --------------------------------------------------------

    def run(self, poll_timeout: int = 25) -> None:
        warnings = self.manager.preflight()
        print("[conductor] online (cap=%d, budget=%s)" % (
            self.manager.cfg.max_parallel, self.manager.cfg.token_budget or "∞"), flush=True)
        for w in warnings:
            print("[conductor] WARN: %s" % w.replace("`", ""), flush=True)

        offset: Optional[int] = None
        import time as _time
        while True:
            try:
                updates = self.client.get_updates(offset=offset, poll_timeout=poll_timeout)
            except TelegramError as exc:
                print("[conductor] poll error: %s" % exc, flush=True)
                _time.sleep(5)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if not _authorized(chat_id, self.allowed):
                    self._send(chat_id, "Unauthorized chat `%s`." % chat_id)
                    continue
                try:
                    reply = self.handle(message)
                except Exception as exc:  # noqa: BLE001 - never die on one message
                    reply = "Error: %s" % exc
                if reply:
                    self._send(chat_id, reply)


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
    manager = ConductorManager(cfg)
    try:
        client = TelegramClient()
    except TelegramError as exc:
        print("[conductor] %s" % exc, flush=True)
        return 1

    allowed = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
    admin = os.environ.get("TELEGRAM_ADMIN_USER_ID") or None
    bot = ConductorBot(client, manager, allowed, admin)
    try:
        bot.run(poll_timeout=args.poll_timeout)
    except KeyboardInterrupt:
        print("\n[conductor] shutting down", flush=True)
    finally:
        manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
