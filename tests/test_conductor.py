"""Tests for the dev-conductor (worktree-per-task headless agent runner).

Everything runs offline: the single ``runner._run`` subprocess seam is patched
so the full pipeline (worktree -> agent -> commit -> push -> PR) is exercised
without git, the network, or the agent CLIs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wca.conductor import runner
from wca.conductor.config import ConductorConfig
from wca.conductor.dispatcher import choose_engine
from wca.conductor.manager import ConductorManager
from wca.conductor.models import Engine, TaskRecord, TaskStatus


# -- fakes ----------------------------------------------------------------


def _cp(cmd, rc=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


def make_fake_run(
    *,
    has_changes=True,
    agent_stdout='{"result":"made changes","usage":{"input_tokens":100,"output_tokens":50}}',
    agent_rc=0,
    pr_rc=0,
    pr_url="https://github.com/drewdoherty/World-Cup-26/pull/42",
    remote="git@github.com:drewdoherty/World-Cup-26.git",
):
    """Build a fake ``_run`` that classifies a command and returns canned output.

    Records every call (and the agent's env) on ``fake.calls`` / ``fake.agent_env``.
    """
    state = {"calls": [], "agent_env": None}

    def fake(cmd, cwd=None, env=None, timeout=None):
        state["calls"].append(list(cmd))
        if cmd[0] == "git":
            sub = cmd[3] if len(cmd) > 3 else ""
            if sub == "worktree":
                return _cp(cmd)
            if sub == "status":
                return _cp(cmd, out=" M file.py\n" if has_changes else "")
            if sub == "commit":
                return _cp(cmd)
            if sub == "push":
                return _cp(cmd)
            if sub == "remote":
                return _cp(cmd, out=remote)
            return _cp(cmd)
        if cmd[0] == "gh":
            return _cp(cmd, rc=pr_rc, out=pr_url if pr_rc == 0 else "", err="" if pr_rc == 0 else "not logged in")
        # otherwise: the agent invocation
        state["agent_env"] = env
        return _cp(cmd, rc=agent_rc, out=agent_stdout, err="" if agent_rc == 0 else "boom")

    fake.state = state
    return fake


@pytest.fixture
def cfg(tmp_path):
    return ConductorConfig(repo_root=tmp_path)


@pytest.fixture
def patched(monkeypatch):
    """Patch the runner's subprocess seam and make all binaries 'present'."""
    fake = make_fake_run()
    monkeypatch.setattr(runner, "_run", fake)
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    return fake


def _record(rid=1, engine="claude", task="fix the thing", branch="conductor/claude-fix-the-thing-abc123"):
    return TaskRecord(id=rid, engine=engine, task=task, shortid="abc123", branch=branch)


# -- naming / parsing -----------------------------------------------------


def test_slugify_basic():
    assert runner.slugify("Fix the BUG #3!") == "fix-the-bug-3"
    assert runner.slugify("   ") == "task"
    assert len(runner.slugify("a" * 100)) <= 32


def test_parse_claude_json():
    summary, tokens = runner._parse_agent_output("claude", '{"result":"done","usage":{"input_tokens":10,"output_tokens":5}}')
    assert summary == "done"
    assert tokens == 15


def test_parse_claude_jsonl_takes_last_object():
    out = '{"type":"system"}\n{"result":"final","usage":{"input_tokens":1,"output_tokens":2}}'
    summary, tokens = runner._parse_agent_output("claude", out)
    assert summary == "final"
    assert tokens == 3


def test_parse_codex_fallback_to_last_lines():
    summary, tokens = runner._parse_agent_output("codex", "line one\nline two\n")
    assert "line two" in summary
    assert tokens == 0


def test_parse_handles_garbage():
    summary, tokens = runner._parse_agent_output("claude", "not json at all")
    assert summary == "not json at all"
    assert tokens == 0


# -- safe environment -----------------------------------------------------


def test_agent_env_strips_secret_and_forces_dry_run(cfg, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xLIVE_SECRET")
    monkeypatch.setenv("PM_DRY_RUN", "0")
    env = cfg.agent_env()
    assert "POLYMARKET_PRIVATE_KEY" not in env
    assert env["PM_DRY_RUN"] == "1"
    assert env["WCA_DB_PATH"] == "data/dev.db"


# -- worktree / git guards ------------------------------------------------


def test_create_worktree_refuses_base_branch(cfg, patched):
    with pytest.raises(ValueError):
        runner.create_worktree(cfg, cfg.base_branch, "leaf")


def test_commit_refuses_base_branch(cfg, patched):
    with pytest.raises(ValueError):
        runner.commit_and_push(cfg, Path("/x"), cfg.base_branch, "claude", "t")


def test_commit_and_push_no_diff_returns_false(cfg, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run(has_changes=False))
    assert runner.commit_and_push(cfg, Path("/x"), "conductor/foo", "claude", "t") is False


def test_compare_url_from_ssh_remote(cfg, patched):
    url = runner._compare_url(cfg, Path("/x"), "conductor/foo")
    assert url == "https://github.com/drewdoherty/World-Cup-26/compare/main...conductor/foo?expand=1"


# -- full pipeline --------------------------------------------------------


def test_run_task_happy_path_opens_pr(cfg, patched):
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.DONE.value
    assert rec.pr_url == "https://github.com/drewdoherty/World-Cup-26/pull/42"
    assert rec.tokens == 150
    assert rec.finished_at is not None
    # the agent ran under the dry-run, no-secrets env
    assert patched.state["agent_env"]["PM_DRY_RUN"] == "1"
    assert "POLYMARKET_PRIVATE_KEY" not in patched.state["agent_env"]


def test_run_task_never_pushes_base_branch(cfg, patched):
    rec = _record()
    runner.run_task(cfg, rec)
    pushes = [c for c in patched.state["calls"] if c[0] == "git" and "push" in c]
    assert pushes, "expected a push"
    for c in pushes:
        assert cfg.base_branch not in c, "must never push the base branch"


def test_run_task_agent_failure_marks_failed(cfg, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run(agent_rc=1))
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.FAILED.value
    assert rec.error


def test_run_task_no_changes(cfg, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run(has_changes=False))
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.NO_CHANGES.value


def test_run_task_pr_fallback_to_compare_link(cfg, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run(pr_rc=1))
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.PUSHED.value
    assert "compare/main..." in rec.pr_url
    assert rec.error  # carries the gh failure reason


def test_run_task_missing_cli_fails_cleanly(cfg, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(runner.shutil, "which", lambda b: None)  # nothing on PATH
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.FAILED.value
    assert "not found" in rec.error


# -- manager --------------------------------------------------------------


def test_manager_submit_runs_to_done(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1))
    rec = mgr.submit("claude", "do work", chat_id="99")
    mgr._futures[rec.id].result(timeout=10)
    assert mgr.get(rec.id).status == TaskStatus.DONE.value


def test_manager_rejects_over_budget(tmp_path, monkeypatch):
    def fake_run_task(cfg, record, notify=None):
        record.tokens = 1000
        record.status = TaskStatus.DONE.value
        record.finished_at = 1.0
        return record

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1, token_budget=500))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    first = mgr.submit("claude", "task one")
    mgr._futures[first.id].result(timeout=10)
    second = mgr.submit("claude", "task two")
    assert second.status == TaskStatus.REJECTED.value
    assert "budget" in second.error


def test_manager_cancel_queued_task(tmp_path, monkeypatch):
    import threading

    gate = threading.Event()

    def blocking_run_task(cfg, record, notify=None):
        gate.wait(timeout=5)
        record.status = TaskStatus.DONE.value
        record.finished_at = 1.0
        return record

    monkeypatch.setattr(runner, "run_task", blocking_run_task)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    running = mgr.submit("claude", "occupies the worker")
    queued = mgr.submit("codex", "stuck in the queue")
    cancelled = mgr.cancel(queued.id)
    assert cancelled.status == TaskStatus.REJECTED.value
    assert cancelled.error == "cancelled before start"
    gate.set()
    mgr._futures[running.id].result(timeout=10)


def test_manager_unknown_engine_rejected(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    with pytest.raises(ValueError):
        mgr.submit("gpt5", "nope")


def test_dispatcher_sends_background_work_to_claude():
    decision = choose_engine("run a background Telegram report sender", codex_available=True)
    assert decision.engine is Engine.CLAUDE
    assert "background" in decision.reason


def test_dispatcher_spends_codex_only_on_mechanical_tasks():
    decision = choose_engine("fix typo in README wording", codex_available=True)
    assert decision.engine is Engine.CODEX


def test_dispatcher_overflows_codex_to_claude_when_cap_reached():
    decision = choose_engine("fix typo in README wording", codex_available=False)
    assert decision.engine is Engine.CLAUDE
    assert "unavailable" in decision.reason


def test_manager_submit_auto_routes_and_records_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1))
    rec = mgr.submit_auto("build a background bot monitor")
    mgr._futures[rec.id].result(timeout=10)
    assert rec.engine == Engine.CLAUDE.value
    assert rec.route_reason.startswith("Claude:")


def test_manager_submit_auto_respects_codex_auto_limit_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    cfg = ConductorConfig(repo_root=tmp_path, max_parallel=1, codex_auto_limit=0)
    mgr = ConductorManager(cfg)
    rec = mgr.submit_auto("fix typo in README wording")
    mgr._futures[rec.id].result(timeout=10)
    assert rec.engine == Engine.CLAUDE.value
    assert "unavailable" in rec.route_reason


def test_status_table_empty_and_populated(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1))
    assert "No conductor tasks" in mgr.status_table()
    rec = mgr.submit("claude", "render me")
    mgr._futures[rec.id].result(timeout=10)
    table = mgr.status_table()
    assert "#%d" % rec.id in table
    assert "render me" in table


# -- config ---------------------------------------------------------------


def test_config_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WCA_CONDUCTOR_MAX_PARALLEL", "7")
    monkeypatch.setenv("WCA_CONDUCTOR_TOKEN_BUDGET", "12345")
    monkeypatch.setenv("CLAUDE_BIN", "/opt/claude")
    cfg = ConductorConfig.from_env(tmp_path)
    assert cfg.max_parallel == 7
    assert cfg.token_budget == 12345
    assert cfg.claude_bin == "/opt/claude"
    assert cfg.worktrees_dir == Path(tmp_path) / ".claude" / "worktrees"


def test_config_zero_budget_means_unlimited(tmp_path):
    cfg = ConductorConfig(repo_root=tmp_path, token_budget=0)
    assert cfg.token_budget is None


def test_engine_coerce():
    assert Engine.coerce("CLAUDE") is Engine.CLAUDE
    assert Engine.coerce("codex") is Engine.CODEX
    with pytest.raises(ValueError):
        Engine.coerce("bard")


# -- error surfacing from claude stdout (regression: "agent exited 1") ------


def test_run_agent_surfaces_stdout_error_not_logged_in(cfg, monkeypatch):
    # claude --output-format json writes the real reason to STDOUT, not stderr.
    out = '{"type":"result","is_error":true,"result":"Not logged in · Please run /login"}'
    monkeypatch.setattr(runner, "_run", make_fake_run(agent_rc=1, agent_stdout=out))
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    res = runner.run_agent(cfg, "claude", "do x", Path("/tmp/wt"))
    assert res.returncode != 0
    assert "Not logged in" in res.error
    assert "agent exited" not in res.error


def test_run_agent_treats_is_error_as_failure_on_exit_0(cfg, monkeypatch):
    out = '{"is_error":true,"result":"boom"}'
    monkeypatch.setattr(runner, "_run", make_fake_run(agent_rc=0, agent_stdout=out))
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    res = runner.run_agent(cfg, "claude", "do x", Path("/tmp/wt"))
    assert res.returncode != 0
    assert res.error == "boom"


def test_run_task_cleans_up_worktree_on_failure(cfg, monkeypatch):
    fake = make_fake_run(agent_rc=1, agent_stdout='{"is_error":true,"result":"nope"}')
    monkeypatch.setattr(runner, "_run", fake)
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.FAILED.value
    assert "nope" in rec.error
    removes = [c for c in fake.state["calls"] if "worktree" in c and "remove" in c]
    assert removes, "a failed run must reclaim its worktree"


# -- codex sandbox (regression: read-only -> agent can't edit -> NO_CHANGES) -


def test_codex_args_default_to_workspace_write(tmp_path):
    cfg = ConductorConfig(repo_root=tmp_path)
    binary, args = cfg.cli_for("codex")
    assert "--sandbox" in args and "workspace-write" in args


def test_codex_command_puts_sandbox_flag_before_prompt(cfg, monkeypatch):
    fake = make_fake_run()
    monkeypatch.setattr(runner, "_run", fake)
    monkeypatch.setattr(runner.shutil, "which", lambda b: "/usr/bin/%s" % b)
    runner.run_agent(cfg, "codex", "do the thing", Path("/tmp/wt"))
    agent_cmds = [c for c in fake.state["calls"] if c and c[0] not in ("git", "gh")]
    assert agent_cmds, "expected a codex invocation"
    cmd = agent_cmds[0]
    assert "exec" in cmd and "--sandbox" in cmd and "workspace-write" in cmd
    # flags must precede the positional prompt or codex won't parse them
    assert cmd.index("--sandbox") < cmd.index("do the thing")


# -- engine health + availability-aware routing ----------------------------

from wca.conductor import health  # noqa: E402
from wca.conductor.dispatcher import choose_engine  # noqa: E402
from wca.conductor.health import EngineHealth  # noqa: E402


def test_probe_claude_detects_not_logged_in(cfg, monkeypatch):
    out = '{"is_error":true,"result":"Not logged in · Please run /login"}'
    monkeypatch.setattr(health.runner, "_run", make_fake_run(agent_rc=1, agent_stdout=out))
    monkeypatch.setattr(health.shutil, "which", lambda b: "/usr/bin/%s" % b)
    h = health.probe_claude(cfg)
    assert h.ok is False and "logged in" in h.reason.lower()


def test_probe_claude_ok(cfg, monkeypatch):
    monkeypatch.setattr(health.runner, "_run", make_fake_run(agent_stdout='{"result":"ok"}'))
    monkeypatch.setattr(health.shutil, "which", lambda b: "/usr/bin/%s" % b)
    assert health.probe_claude(cfg).ok is True


def test_probe_codex_uses_auth_file(cfg, monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda b: "/usr/bin/%s" % b)
    monkeypatch.setattr(health.os.path, "exists", lambda p: True)
    monkeypatch.setattr(health.os.path, "getsize", lambda p: 4000)
    assert health.probe_codex(cfg).ok is True
    monkeypatch.setattr(health.os.path, "exists", lambda p: False)
    assert health.probe_codex(cfg).ok is False


def test_choose_engine_falls_back_when_claude_unavailable():
    # a Claude-first task with claude down -> Codex
    d = choose_engine("refactor the model", codex_available=True, claude_available=False)
    assert d.engine is Engine.CODEX


def test_choose_engine_no_healthy_engine_returns_preferred():
    d = choose_engine("refactor the model", codex_available=False, claude_available=False)
    assert d.engine is Engine.CLAUDE and "no healthy engine" in d.reason


def _stub_health(mgr, claude_ok, codex_ok):
    def fake(engine, force=False):
        e = Engine.coerce(engine).value
        ok = claude_ok if e == "claude" else codex_ok
        return EngineHealth(e, ok, "ok" if ok else "%s logged out" % e)
    mgr.engine_health = fake  # type: ignore[assignment]


def test_explicit_claude_reroutes_to_codex_when_logged_out(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr, claude_ok=False, codex_ok=True)
    rec = mgr.submit("claude", "do background work")
    assert rec.engine == "codex"
    assert "rerouted claude" in rec.route_reason
    assert rec.status != TaskStatus.REJECTED.value


def test_explicit_claude_rejected_when_both_down(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr, claude_ok=False, codex_ok=False)
    rec = mgr.submit("claude", "do work")
    assert rec.status == TaskStatus.REJECTED.value and "logged out" in rec.error


def test_submit_auto_routes_to_codex_when_claude_down(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr, claude_ok=False, codex_ok=True)
    rec = mgr.submit_auto("write a research report on the model")
    assert rec.engine == "codex"
    assert rec.status != TaskStatus.REJECTED.value


# -- worktree creation is serialized (regression: concurrent `git worktree add`) -


def test_create_worktree_holds_worktree_lock(cfg, monkeypatch):
    seen = {}

    def fake(cmd, cwd=None, env=None, timeout=None):
        if "worktree" in cmd and "add" in cmd:
            seen["locked"] = runner._WORKTREE_LOCK.locked()
        return _cp(cmd)

    monkeypatch.setattr(runner, "_run", fake)
    runner.create_worktree(cfg, "conductor/x", "leaf")
    assert seen.get("locked") is True, "git worktree add must run under _WORKTREE_LOCK"


def test_submit_auto_ignores_codex_cap_when_claude_down(tmp_path, monkeypatch):
    # Regression: with claude logged out and the Codex auto-cap "reached", a
    # /task must still go to Codex — not mis-route to a dead Claude.
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, codex_auto_limit=1))
    _stub_health(mgr, claude_ok=False, codex_ok=True)
    # simulate one Codex task already active (cap of 1 reached)
    mgr._records[99] = TaskRecord(id=99, engine="codex", task="busy",
                                  status=TaskStatus.RUNNING.value)
    rec = mgr.submit_auto("write a background research report")
    assert rec.engine == "codex"
    assert rec.status != TaskStatus.REJECTED.value
