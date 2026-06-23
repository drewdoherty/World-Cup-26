"""The per-task pipeline: worktree -> headless agent -> commit -> push -> PR.

Every external command goes through :func:`_run` (a thin ``subprocess.run``
wrapper) so tests can patch one seam and exercise the whole flow offline.

Guardrails enforced here:

* :func:`create_worktree` and :func:`commit_and_push` refuse the base branch,
  so a task can never land on ``main``.
* The agent runs under :meth:`ConductorConfig.agent_env` (dry-run, no secrets).
* :func:`open_pr` degrades to a compare link when ``gh`` is missing/unauth'd
  rather than failing the task.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from wca.conductor.config import ConductorConfig
from wca.conductor.models import AgentResult, Engine, PrResult, TaskRecord, TaskStatus

# Notifier called at each lifecycle transition (manager wires this to Telegram).
Notify = Callable[[TaskRecord], None]

# `git worktree add`/`remove` mutate the shared .git/worktrees registry + index,
# which is NOT safe under concurrency: parallel tasks racing here fail with
# "worktree add failed". Serialize just these fast git ops (the slow agent run
# stays parallel).
_WORKTREE_LOCK = threading.Lock()

# Pasted screenshots are copied here *inside the worktree* so the headless agent
# can Read them by a cwd-relative path (no extra --add-dir / permission prompt).
# Two independent guards keep them out of the PR: the directory is deleted before
# commit (see run_task), AND `.conductor_inbox/` is added to the repo's shared
# info/exclude (see _ensure_inbox_excluded) so `git add -A` can never stage it,
# regardless of which base branch the worktree was forked from.
_INBOX_DIR = ".conductor_inbox"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_REMOTE_RE = re.compile(r"(?:git@[^:]+:|https?://[^/]+/)(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$")


def _run(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: Optional[float] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> subprocess.CompletedProcess:
    """Run *cmd*, capturing text output. The single seam tests patch.

    With ``on_event`` it STREAMS: stdout is read line-by-line and every parsed
    JSON object (e.g. a claude ``--output-format stream-json`` event) is handed
    to ``on_event`` live, so callers can surface per-agent activity in flight.
    Without it, it blocks like ``subprocess.run`` (git/gh).
    """
    if on_event is None:
        return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, capture_output=True, text=True)

    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    killer = threading.Timer(timeout, proc.kill) if timeout else None
    if killer:
        killer.start()
    out: List[str] = []
    try:
        for line in proc.stdout or []:
            out.append(line)
            s = line.strip()
            if not s.startswith("{"):
                continue
            try:
                ev = json.loads(s)
            except ValueError:
                continue
            try:
                on_event(ev)
            except Exception:  # noqa: BLE001 - a bad handler must not kill the run
                pass
        proc.wait()
    finally:
        if killer:
            killer.cancel()
    err = proc.stderr.read() if proc.stderr else ""
    return subprocess.CompletedProcess(cmd, proc.returncode or 0, "".join(out), err)


def _activity_from_event(ev: dict) -> Optional[str]:
    """Turn one stream-json event into a short 'what the agent is doing' line."""
    t = ev.get("type")
    if t == "system":
        return "🟢 starting…"
    if t == "assistant":
        blocks = ((ev.get("message") or {}).get("content")) or []
        for b in reversed(blocks):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                inp = b.get("input") or {}
                hint = (inp.get("file_path") or inp.get("path") or inp.get("command")
                        or inp.get("pattern") or inp.get("description") or "")
                hint = str(hint).splitlines()[0] if hint else ""
                return "🔧 %s%s" % (b.get("name", "tool"), (" " + hint[:48]) if hint else "")
            if b.get("type") == "text":
                txt = " ".join((b.get("text") or "").split())
                if txt:
                    return "💬 %s" % txt[:64]
    return None


# -- naming ---------------------------------------------------------------


def slugify(task: str, max_len: int = 32) -> str:
    """Branch-safe slug from a free-text task. Always non-empty."""
    slug = _SLUG_RE.sub("-", task.strip().lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "task"


def _worktree_leaf(cfg: ConductorConfig, record: TaskRecord) -> str:
    return "%s-%03d-%s" % (cfg.branch_prefix, record.id, record.shortid or "x")


# -- worktree -------------------------------------------------------------


def _ensure_inbox_excluded(cfg: ConductorConfig) -> None:
    """Add ``.conductor_inbox/`` to the repo's shared ``info/exclude``.

    The branch-tracked ``.gitignore`` entry only exists once this change is on
    the base branch; worktrees forked from an older base wouldn't ignore the
    inbox. The per-repo ``info/exclude`` (shared by all linked worktrees) makes
    the exclusion base-branch-independent, so even a mid-run ``git add -A`` by
    the agent can never stage a pasted screenshot. Best-effort and idempotent.
    """
    try:
        res = _run([cfg.git_bin, "-C", str(cfg.repo_root), "rev-parse", "--git-common-dir"])
        if res.returncode != 0 or not res.stdout.strip():
            return
        common = Path(res.stdout.strip())
        if not common.is_absolute():
            common = cfg.repo_root / common
        info = common / "info"
        info.mkdir(parents=True, exist_ok=True)
        excl = info / "exclude"
        existing = excl.read_text() if excl.exists() else ""
        if ("%s/" % _INBOX_DIR) in existing:
            return
        with excl.open("a") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("%s/\n" % _INBOX_DIR)
    except OSError:
        pass


def create_worktree(cfg: ConductorConfig, branch: str, leaf: str) -> Path:
    """Create a fresh worktree on a NEW *branch* off ``base_branch``."""
    if branch == cfg.base_branch:
        raise ValueError("refusing to use base branch %r as a task branch" % branch)
    cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.worktrees_dir / leaf
    with _WORKTREE_LOCK:  # serialize the shared-.git race
        _ensure_inbox_excluded(cfg)  # screenshots can never be staged, any base
        res = _run([
            cfg.git_bin, "-C", str(cfg.repo_root),
            "worktree", "add", "-b", branch, str(path), cfg.base_branch,
        ])
    if res.returncode != 0:
        raise RuntimeError("worktree add failed: %s" % (res.stderr.strip() or res.stdout.strip()))
    return path


# -- agent ----------------------------------------------------------------


def _parse_agent_output(engine: str, stdout: str) -> Tuple[str, int]:
    """Extract a (summary, tokens) pair from raw agent stdout.

    Defensive: handles claude ``--output-format json`` (single object or
    JSONL), and falls back to the last non-empty line for codex / unparseable
    output. Never raises.
    """
    text = stdout or ""
    if Engine.coerce(engine) is Engine.CLAUDE:
        obj = _last_json_object(text)
        if obj is not None:
            summary = str(obj.get("result") or obj.get("subtype") or "").strip()
            usage = obj.get("usage") or {}
            tokens = 0
            if isinstance(usage, dict):
                tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
            if not tokens:
                tokens = int(obj.get("total_tokens", 0) or 0)
            if summary:
                return summary[:1000], tokens
    # codex / fallback: last few non-empty lines.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    summary = " / ".join(lines[-3:]) if lines else ""
    return summary[:1000], 0


def _last_json_object(text: str) -> Optional[dict]:
    """Best-effort: parse *text* as a JSON object, or the last JSON line."""
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


def stage_images(worktree: Path, images: List[str]) -> List[str]:
    """Copy pasted screenshots into the worktree's inbox for the agent to read.

    Returns worktree-relative paths (``.conductor_inbox/NN.ext``) for the files
    that were actually staged; missing/unreadable sources are skipped. The inbox
    is removed before commit (:func:`run_task`) so screenshots never reach a PR.
    """
    rels: List[str] = []
    inbox = worktree / _INBOX_DIR
    for i, src in enumerate(images or [], start=1):
        sp = Path(src)
        if not sp.is_file():
            continue
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / ("%02d%s" % (i, sp.suffix.lower() or ".png"))
        try:
            shutil.copyfile(sp, dest)
        except OSError:
            continue
        rels.append("%s/%s" % (_INBOX_DIR, dest.name))
    return rels


def augment_task_with_images(task: str, rel_paths: List[str]) -> str:
    """Append a 'read these screenshots first' note pointing at staged images."""
    if not rel_paths:
        return task
    n = len(rel_paths)
    bullets = "\n".join("- %s" % p for p in rel_paths)
    note = (
        "\n\n---\n"
        "The user attached %d screenshot%s for visual context (a bug, UI state, "
        "log, or error). Read %s with the Read tool BEFORE you start and let what "
        "you see guide the work:\n%s"
    ) % (n, "" if n == 1 else "s", "it" if n == 1 else "them", bullets)
    return task + note


def run_agent(cfg: ConductorConfig, engine: str, task: str, cwd: Path,
              on_activity: Optional[Callable[[str], None]] = None) -> AgentResult:
    """Invoke the headless agent for *engine* inside *cwd*.

    ``on_activity`` (claude only) receives a short live activity string per
    stream-json event (tool calls, edits, messages) so callers can show what the
    agent is doing in flight.
    """
    binary, extra = cfg.cli_for(engine)
    # Only treat a bare name as missing; an absolute path may exist off-PATH.
    if "/" not in binary and shutil.which(binary) is None:
        return AgentResult(127, error="%s CLI not found on PATH (%s)" % (engine, binary))

    on_event = None
    if Engine.coerce(engine) is Engine.CLAUDE:
        # `--` ends option parsing so a prompt that starts with '-' (e.g. a task
        # pasted as "- send a message ...") is taken as the positional prompt,
        # not mis-read as an unknown CLI flag. Flags must precede it.
        cmd = [binary, "-p", *extra, "--", task]
        if on_activity is not None:
            def on_event(ev: dict) -> None:  # noqa: E306 - streamed activity hook
                act = _activity_from_event(ev)
                if act:
                    on_activity(act)
    else:
        # codex: flags before the positional prompt (e.g. -s workspace-write),
        # then `--` so a leading-dash prompt is parsed as the prompt, not a flag.
        cmd = [binary, "exec", *extra, "--", task]

    try:
        res = _run(cmd, cwd=str(cwd), env=cfg.agent_env(), timeout=cfg.agent_timeout, on_event=on_event)
    except subprocess.TimeoutExpired:
        return AgentResult(124, error="agent timed out after %.0fs" % cfg.agent_timeout)

    summary, tokens = _parse_agent_output(engine, res.stdout)
    # claude --output-format json reports its real failure on STDOUT, not
    # stderr — e.g. {"is_error":true,"result":"Not logged in · Please run
    # /login"} with exit 1, or even is_error with exit 0. Read it so the bot
    # surfaces the actual reason instead of a bare "agent exited 1".
    obj = _last_json_object(res.stdout) if Engine.coerce(engine) is Engine.CLAUDE else None
    is_error = bool(obj.get("is_error")) if obj else False
    if res.returncode != 0 or is_error:
        detail = ""
        if obj:
            detail = str(obj.get("result") or obj.get("error") or "").strip()
        if not detail:
            detail = (res.stderr or "").strip()[-500:] or (res.stdout or "").strip()[-500:] \
                or "agent exited %d" % res.returncode
        return AgentResult(res.returncode or 1, summary, tokens, res.stdout, res.stderr, detail)
    return AgentResult(res.returncode, summary, tokens, res.stdout, res.stderr, "")


# -- git: commit / push ---------------------------------------------------


def _commit_message(engine: str, task: str) -> str:
    subject = task.strip().splitlines()[0] if task.strip() else "conductor task"
    if len(subject) > 68:
        subject = subject[:65] + "..."
    return (
        "%s: %s\n\n"
        "Task dispatched via the WCA dev-conductor (%s headless).\n\n"
        "Full task:\n%s\n"
    ) % (engine, subject, engine, task.strip())


def commit_and_push(cfg: ConductorConfig, cwd: Path, branch: str, engine: str, task: str) -> bool:
    """Stage/commit/push the worktree. Returns False if there was no diff."""
    if branch == cfg.base_branch:
        raise ValueError("refusing to push base branch %r" % branch)

    _run([cfg.git_bin, "-C", str(cwd), "add", "-A"])
    status = _run([cfg.git_bin, "-C", str(cwd), "status", "--porcelain"])
    if not status.stdout.strip():
        return False

    commit = _run([cfg.git_bin, "-C", str(cwd), "commit", "-m", _commit_message(engine, task)])
    if commit.returncode != 0:
        raise RuntimeError("commit failed: %s" % (commit.stderr.strip() or commit.stdout.strip()))

    if not cfg.push:
        return True

    push = _run([cfg.git_bin, "-C", str(cwd), "push", "-u", "origin", branch])
    if push.returncode != 0:
        raise RuntimeError("push failed: %s" % (push.stderr.strip() or push.stdout.strip()))
    return True


# -- git: pull request ----------------------------------------------------


def _remote_slug(cfg: ConductorConfig, cwd: Path) -> Optional[str]:
    res = _run([cfg.git_bin, "-C", str(cwd), "remote", "get-url", "origin"])
    if res.returncode != 0:
        return None
    m = _REMOTE_RE.search(res.stdout.strip())
    return m.group("slug") if m else None


def _compare_url(cfg: ConductorConfig, cwd: Path, branch: str) -> Optional[str]:
    slug = _remote_slug(cfg, cwd)
    if not slug:
        return None
    return "https://github.com/%s/compare/%s...%s?expand=1" % (slug, cfg.base_branch, branch)


def open_pr(cfg: ConductorConfig, cwd: Path, branch: str, title: str, body: str) -> PrResult:
    """Open a PR via ``gh``; fall back to a compare link on any failure."""
    if not cfg.create_pr:
        return PrResult(False, compare_url=_compare_url(cfg, cwd, branch), error="PR creation disabled")

    if shutil.which(cfg.gh_bin) is None:
        return PrResult(False, compare_url=_compare_url(cfg, cwd, branch), error="gh CLI not found")

    res = _run([
        cfg.gh_bin, "pr", "create",
        "--base", cfg.base_branch, "--head", branch,
        "--title", title, "--body", body,
    ], cwd=str(cwd))
    if res.returncode == 0:
        url = (res.stdout or "").strip().splitlines()
        return PrResult(True, url=url[-1].strip() if url else None)
    return PrResult(
        False,
        compare_url=_compare_url(cfg, cwd, branch),
        error=(res.stderr or "").strip()[-300:] or "gh pr create failed",
    )


def _pr_title(record: TaskRecord) -> str:
    subject = record.task.strip().splitlines()[0] if record.task.strip() else "conductor task"
    if len(subject) > 70:
        subject = subject[:67] + "..."
    return "[%s] %s" % (record.engine, subject)


def _pr_body(record: TaskRecord) -> str:
    parts = [
        "**Dispatched by the WCA dev-conductor** (`%s` headless).\n" % record.engine,
        "**Task**\n```\n%s\n```\n" % record.task.strip(),
    ]
    if record.summary:
        parts.append("**Agent summary**\n%s\n" % record.summary)
    if record.tokens:
        parts.append("_Tokens: %d_\n" % record.tokens)
    parts.append("\n🤖 Generated with [Claude Code](https://claude.com/claude-code)")
    return "\n".join(parts)


# -- orchestration --------------------------------------------------------


def remove_worktree(cfg: ConductorConfig, path: Optional[str]) -> None:
    """Best-effort removal of a task worktree (reclaims failed / no-op runs)."""
    if not path:
        return
    with _WORKTREE_LOCK:  # same shared-.git registry as create_worktree
        _run([cfg.git_bin, "-C", str(cfg.repo_root), "worktree", "remove", "--force", str(path)])


def run_task(cfg: ConductorConfig, record: TaskRecord, notify: Optional[Notify] = None) -> TaskRecord:
    """Run one task end to end, mutating *record* in place at each step.

    Always returns the record (never raises): failures land in
    ``record.status = FAILED`` with a message in ``record.error``.
    """
    def _emit() -> None:
        if notify is not None:
            try:
                notify(record)
            except Exception:  # a broken notifier must never kill the task
                pass

    record.started_at = time.time()
    record.status = TaskStatus.RUNNING.value
    _emit()

    try:
        worktree = create_worktree(cfg, record.branch or "", _worktree_leaf(cfg, record))
        record.worktree_path = str(worktree)

        # Copy any pasted screenshots into the worktree and point the prompt at
        # them. The original record.task (used for the commit/PR) stays clean.
        prompt = record.task
        if record.images:
            rels = stage_images(worktree, record.images)
            prompt = augment_task_with_images(record.task, rels)

        def _set_activity(act: str) -> None:
            record.activity = act
            record.activity_at = time.time()

        agent = run_agent(cfg, record.engine, prompt, worktree, on_activity=_set_activity)

        # Screenshots are debug INPUT, never part of the change — drop the inbox
        # before staging so `git add -A` can't sweep them into the PR.
        if record.images:
            shutil.rmtree(worktree / _INBOX_DIR, ignore_errors=True)

        record.tokens = agent.tokens
        record.returncode = agent.returncode
        record.summary = agent.summary
        if not agent.ok:
            record.status = TaskStatus.FAILED.value
            record.error = agent.error
            return record

        changed = commit_and_push(cfg, worktree, record.branch or "", record.engine, record.task)
        if not changed:
            record.status = TaskStatus.NO_CHANGES.value
            return record
        record.status = TaskStatus.PUSHED.value
        _emit()

        pr = open_pr(cfg, worktree, record.branch or "", _pr_title(record), _pr_body(record))
        record.pr_url = pr.link
        if pr.created:
            record.status = TaskStatus.DONE.value
        else:
            # Branch is pushed and inspectable; PR just needs a manual click.
            record.status = TaskStatus.PUSHED.value
            record.error = pr.error
        return record

    except Exception as exc:  # noqa: BLE001 - report, don't propagate
        record.status = TaskStatus.FAILED.value
        record.error = str(exc)
        return record
    finally:
        record.finished_at = time.time()
        # Reclaim the worktree when nothing was pushed (failed / no-op runs) so
        # repeated failures don't pile up throwaway worktrees.
        if record.status in (TaskStatus.FAILED.value, TaskStatus.NO_CHANGES.value):
            remove_worktree(cfg, record.worktree_path)
        _emit()
