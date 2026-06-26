"""Tests for the dev-conductor (worktree-per-task headless agent runner).

Everything runs offline: the single ``runner._run`` subprocess seam is patched
so the full pipeline (worktree -> agent -> commit -> push -> PR) is exercised
without git, the network, or the agent CLIs.
"""

from __future__ import annotations

import os

import subprocess
from pathlib import Path

import pytest

from wca.conductor import runner
from wca.conductor import manager as mgr_mod
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

    def fake(cmd, cwd=None, env=None, timeout=None, on_event=None):
        state["calls"].append(list(cmd))
        if os.path.basename(cmd[0]) == "git":
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
        if os.path.basename(cmd[0]) == "gh":
            return _cp(cmd, rc=pr_rc, out=pr_url if pr_rc == 0 else "", err="" if pr_rc == 0 else "not logged in")
        # otherwise: the agent invocation. If streaming, emit a couple of
        # stream-json events so live-activity wiring is exercised.
        state["agent_env"] = env
        if on_event is not None:
            on_event({"type": "system", "subtype": "init"})
            on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/wca/x.py"}}]}})
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
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
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
    summary, tokens = runner._parse_agent_output("claude", "line one\nline two\n")
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
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.FAILED.value
    assert rec.error


def test_run_task_no_changes(cfg, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run(has_changes=False))
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.NO_CHANGES.value


def test_run_task_pr_fallback_to_compare_link(cfg, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run(pr_rc=1))
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)  # don't wait out the backoff
    rec = _record()
    runner.run_task(cfg, rec)
    # A committed+pushed branch whose PR step failed is now a DISTINCT, retryable
    # state (not a quiet PUSHED) so it can be surfaced + auto-retried.
    assert rec.status == TaskStatus.COMMITTED_PR_FAILED.value
    assert "compare/main..." in rec.pr_url  # compare link still offered
    assert rec.error  # carries the gh failure reason


def test_run_task_missing_cli_fails_cleanly(cfg, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: None)  # nothing installed
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.FAILED.value
    assert "not found" in rec.error


# -- manager --------------------------------------------------------------


def test_manager_submit_runs_to_done(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
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
    queued = mgr.submit("claude", "stuck in the queue")
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
    decision = choose_engine("run a background Telegram report sender")
    assert decision.engine is Engine.CLAUDE
    assert decision.reason == "Claude"


def test_manager_submit_auto_routes_and_records_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1))
    rec = mgr.submit_auto("build a background bot monitor")
    mgr._futures[rec.id].result(timeout=10)
    assert rec.engine == Engine.CLAUDE.value
    assert rec.route_reason.startswith("Claude")


def test_status_table_empty_and_populated(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
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
    with pytest.raises(ValueError):
        Engine.coerce("codex")  # removed from the swarm 2026-06
    with pytest.raises(ValueError):
        Engine.coerce("bard")


# -- error surfacing from claude stdout (regression: "agent exited 1") ------


def test_run_agent_surfaces_stdout_error_not_logged_in(cfg, monkeypatch):
    # claude --output-format json writes the real reason to STDOUT, not stderr.
    out = '{"type":"result","is_error":true,"result":"Not logged in · Please run /login"}'
    monkeypatch.setattr(runner, "_run", make_fake_run(agent_rc=1, agent_stdout=out))
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    res = runner.run_agent(cfg, "claude", "do x", Path("/tmp/wt"))
    assert res.returncode != 0
    assert "Not logged in" in res.error
    assert "agent exited" not in res.error


def test_run_agent_treats_is_error_as_failure_on_exit_0(cfg, monkeypatch):
    out = '{"is_error":true,"result":"boom"}'
    monkeypatch.setattr(runner, "_run", make_fake_run(agent_rc=0, agent_stdout=out))
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    res = runner.run_agent(cfg, "claude", "do x", Path("/tmp/wt"))
    assert res.returncode != 0
    assert res.error == "boom"


def test_run_task_cleans_up_worktree_on_failure(cfg, monkeypatch):
    fake = make_fake_run(agent_rc=1, agent_stdout='{"is_error":true,"result":"nope"}')
    monkeypatch.setattr(runner, "_run", fake)
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    rec = _record()
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.FAILED.value
    assert "nope" in rec.error
    removes = [c for c in fake.state["calls"] if "worktree" in c and "remove" in c]
    assert removes, "a failed run must reclaim its worktree"


# -- codex sandbox (regression: read-only -> agent can't edit -> NO_CHANGES) -


# -- engine health + availability-aware routing ----------------------------

from wca.conductor import health  # noqa: E402
from wca.conductor.dispatcher import choose_engine  # noqa: E402
from wca.conductor.health import EngineHealth  # noqa: E402


def test_probe_claude_detects_not_logged_in(cfg, monkeypatch):
    out = '{"is_error":true,"result":"Not logged in · Please run /login"}'
    monkeypatch.setattr(health.runner, "_run", make_fake_run(agent_rc=1, agent_stdout=out))
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    h = health.probe_claude(cfg)
    assert h.ok is False and "logged in" in h.reason.lower()


def test_probe_claude_ok(cfg, monkeypatch):
    monkeypatch.setattr(health.runner, "_run", make_fake_run(agent_stdout='{"result":"ok"}'))
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    assert health.probe_claude(cfg).ok is True


def test_choose_engine_no_healthy_engine_returns_preferred():
    d = choose_engine("refactor the model", claude_available=False)
    assert d.engine is Engine.CLAUDE and "no healthy engine" in d.reason


def _stub_health(mgr, claude_ok, codex_ok):
    def fake(engine, force=False):
        e = Engine.coerce(engine).value
        ok = claude_ok if e == "claude" else codex_ok
        return EngineHealth(e, ok, "ok" if ok else "%s logged out" % e)
    mgr.engine_health = fake  # type: ignore[assignment]


def test_explicit_claude_rejected_when_both_down(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr, claude_ok=False, codex_ok=False)
    rec = mgr.submit("claude", "do work")
    assert rec.status == TaskStatus.REJECTED.value and "logged out" in rec.error


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


# -- disabled engines (e.g. Codex exhausted -> Claude-only cluster) ---------


def test_from_env_reads_disabled_engines(tmp_path, monkeypatch):
    monkeypatch.setenv("WCA_CONDUCTOR_DISABLED_ENGINES", "codex")
    cfg = ConductorConfig.from_env(tmp_path)
    assert cfg.disabled_engines == ["codex"]


# -- /model usage + /agents views ------------------------------------------


def test_model_usage_table_groups_ongoing_and_parked_by_agent(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr, claude_ok=True, codex_ok=False)
    mgr._records[1] = TaskRecord(id=1, engine="claude", task="build the thing", status=TaskStatus.RUNNING.value, tokens=120)
    mgr._records[2] = TaskRecord(id=2, engine="claude", task="waiting in line", status=TaskStatus.QUEUED.value)
    out = mgr.model_usage_table()
    assert "Model usage" in out
    assert "1 running" in out and "1 parked" in out
    assert "#1" in out and "build the thing" in out
    assert "claude" in out  # the sole engine


def test_agents_spec_table_shows_specs_and_architecture(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, disabled_engines=["codex"]))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    out = mgr.agents_spec_table()
    assert "Agents" in out and "Shared architecture" in out
    assert "claude" in out
    assert "sole route" in out        # claude-only role text
    assert "PR-only" in out or "never commits" in out


# -- /usage, /prs, /log, /retry, swarm cap ---------------------------------


def test_max_parallel_defaults_to_sequential_revert(tmp_path):
    # Reverted from an 8-way swarm: parallel runs raced the shared .git
    # worktree registry/index and produced collisions. Sequential by default.
    assert ConductorConfig(repo_root=tmp_path).max_parallel == 1


def test_usage_table_sums_per_engine_spend(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, disabled_engines=["codex"]))
    _stub_health(mgr, claude_ok=True, codex_ok=False)
    mgr._records[1] = TaskRecord(id=1, engine="claude", task="t1", status=TaskStatus.DONE.value, tokens=1500)
    mgr._records[2] = TaskRecord(id=2, engine="claude", task="t2", status=TaskStatus.RUNNING.value, tokens=300)
    out = mgr.usage_table()
    assert "Anthropic usage" in out and "claude" in out
    assert "1,800" in out  # total spend, comma-grouped


def test_prs_lists_only_linked_records(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    mgr._records[1] = TaskRecord(id=1, engine="claude", task="has pr",
                                 status=TaskStatus.DONE.value, pr_url="https://x/pull/1")
    mgr._records[2] = TaskRecord(id=2, engine="claude", task="no pr", status=TaskStatus.FAILED.value)
    out = mgr.prs()
    assert "#1" in out and "pull/1" in out and "#2" not in out


def test_task_detail_and_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    mgr._records[1] = TaskRecord(id=1, engine="claude", task="failed thing",
                                 status=TaskStatus.FAILED.value, error="boom")
    detail = mgr.task_detail(1)
    assert "failed thing" in detail and "boom" in detail
    mgr._counter = 100  # so the retry's new id doesn't collide with the seeded one
    new = mgr.retry(1)
    assert new.id != 1 and new.task == "failed thing"
    # a non-retryable (DONE) task returns itself unchanged
    mgr._records[5] = TaskRecord(id=5, engine="claude", task="done", status=TaskStatus.DONE.value)
    assert mgr.retry(5).id == 5


# -- live per-agent activity (streaming) -----------------------------------


def test_activity_from_event_tool_use():
    ev = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}}]}}
    act = runner._activity_from_event(ev)
    assert act.startswith("🔧 Edit") and "src/a.py" in act


def test_activity_from_event_text_system_result():
    txt = runner._activity_from_event({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Looking at the code"}]}})
    assert txt.startswith("💬")
    assert runner._activity_from_event({"type": "system", "subtype": "init"}) is not None
    assert runner._activity_from_event({"type": "result"}) is None


def test_run_task_records_live_activity(cfg, patched):
    # the patched fake emits a tool_use event during the streamed agent run
    rec = _record()
    runner.run_task(cfg, rec)
    assert "Edit" in rec.activity and rec.activity_at > 0


def test_watch_shows_running_activity(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    mgr._records[1] = TaskRecord(id=1, engine="claude", task="t",
                                 status=TaskStatus.RUNNING.value, activity="🔧 Edit src/x.py")
    assert "Edit src/x.py" in mgr.watch() and "#1" in mgr.watch()
    assert "Edit src/x.py" in mgr.watch(1)
    assert "No task" in mgr.watch(999)


def test_run_uses_streaming_only_with_on_event(cfg, monkeypatch):
    # claude_args now request stream-json; the runner streams only when given a hook
    bin_, args = cfg.cli_for("claude")
    assert "stream-json" in args and "--verbose" in args


# -- collision-proofing: parallel swarm stays race-free --------------------


def test_swarm_runs_many_tasks_in_parallel_without_collision(tmp_path, monkeypatch):
    # The worktree-add race was the historical collision; _WORKTREE_LOCK + a
    # per-worktree index make cap>1 safe. Stress 12 tasks through a 4-wide pool
    # and assert no id/branch collision and every task completes.
    monkeypatch.setattr(runner, "_run", make_fake_run())
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=4))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    recs = [mgr.submit("claude", "task %d" % i) for i in range(12)]
    for r in recs:
        mgr._futures[r.id].result(timeout=20)
    final = [mgr.get(r.id) for r in recs]
    assert all(f.status == TaskStatus.DONE.value for f in final)
    assert len({f.branch for f in final}) == 12          # unique branches
    assert sorted(f.id for f in final) == list(range(1, 13))  # counter never raced


# -- /merge: gated, green-only PR merge ------------------------------------


def _done_pr_record(mgr, rid=1):
    rec = TaskRecord(id=rid, engine="claude", task="t", status=TaskStatus.DONE.value,
                     branch="conductor/x", pr_url="https://github.com/o/r/pull/9")
    mgr._records[rid] = rec
    return rec


def test_merge_refuses_without_open_pr(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    mgr._records[1] = TaskRecord(id=1, engine="claude", task="t",
                                 status=TaskStatus.PUSHED.value, pr_url=None)
    ok, msg = mgr.merge_task(1)
    assert ok is False and "no open PR" in msg


def test_merge_refuses_when_not_green(tmp_path, monkeypatch):
    import subprocess as sp
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _done_pr_record(mgr)
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/gh")

    def fake_run(cmd, **kw):
        if "view" in cmd:
            return sp.CompletedProcess(cmd, 0, '{"state":"OPEN","statusCheckRollup":[{"conclusion":"FAILURE"}]}', "")
        return sp.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(mgr_mod.subprocess, "run", fake_run)
    ok, msg = mgr.merge_task(1)
    assert ok is False and "not green" in msg


def test_merge_squash_merges_when_green(tmp_path, monkeypatch):
    import subprocess as sp
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _done_pr_record(mgr)
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/gh")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "view" in cmd:
            return sp.CompletedProcess(cmd, 0, '{"state":"OPEN","statusCheckRollup":[{"conclusion":"SUCCESS"}]}', "")
        return sp.CompletedProcess(cmd, 0, "Merged", "")
    monkeypatch.setattr(mgr_mod.subprocess, "run", fake_run)
    ok, msg = mgr.merge_task(1)
    assert ok is True and "merged" in msg.lower()
    assert any("merge" in c and "--squash" in c and "--delete-branch" in c for c in calls)


def test_merge_refuses_without_gh(tmp_path, monkeypatch):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _done_pr_record(mgr)
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: None)
    ok, msg = mgr.merge_task(1)
    assert ok is False and "gh" in msg.lower()


# -- sequential default (revert: parallel raced the shared .git registry) --


def test_max_parallel_defaults_to_sequential(tmp_path):
    assert ConductorConfig(repo_root=tmp_path).max_parallel == 1


def test_from_env_max_parallel_defaults_to_one(tmp_path, monkeypatch):
    monkeypatch.delenv("WCA_CONDUCTOR_MAX_PARALLEL", raising=False)
    assert ConductorConfig.from_env(tmp_path).max_parallel == 1


def test_env_can_still_opt_into_a_swarm(tmp_path, monkeypatch):
    monkeypatch.setenv("WCA_CONDUCTOR_MAX_PARALLEL", "6")
    assert ConductorConfig.from_env(tmp_path).max_parallel == 6


# -- leading-dash prompt safety (regression: task starting with '-') -------


def test_claude_prompt_after_double_dash_survives_leading_dash(cfg, monkeypatch):
    fake = make_fake_run()
    monkeypatch.setattr(runner, "_run", fake)
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    runner.run_agent(cfg, "claude", "- send a message when done", Path("/tmp/wt"))
    cmd = [c for c in fake.state["calls"] if c and os.path.basename(c[0]) not in ("git", "gh")][0]
    assert "--" in cmd, "option parsing must be terminated before the prompt"
    assert cmd.index("--") < cmd.index("- send a message when done")
    assert cmd[-1] == "- send a message when done"  # prompt is the final operand


# -- pasted-screenshot end-to-end (image paste) ----------------------------


def test_stage_images_copies_and_returns_relative_paths(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    a = tmp_path / "a.png"
    a.write_bytes(b"PNGA")
    b = tmp_path / "b.jpg"
    b.write_bytes(b"JPGB")
    rels = runner.stage_images(wt, [str(a), str(b), str(tmp_path / "missing.png")])
    assert rels == [".conductor_inbox/01.png", ".conductor_inbox/02.jpg"]
    assert (wt / ".conductor_inbox" / "01.png").read_bytes() == b"PNGA"


def test_augment_task_with_images_mentions_paths():
    out = runner.augment_task_with_images("fix the bug", [".conductor_inbox/01.png"])
    assert "fix the bug" in out
    assert ".conductor_inbox/01.png" in out
    assert "Read" in out
    # no images -> unchanged
    assert runner.augment_task_with_images("x", []) == "x"


def test_run_task_stages_images_into_prompt_and_cleans_up(cfg, monkeypatch, tmp_path):
    fake = make_fake_run()
    monkeypatch.setattr(runner, "_run", fake)
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    rec = _record()
    rec.images = [str(img)]
    runner.run_task(cfg, rec)
    assert rec.status == TaskStatus.DONE.value
    # the agent's prompt pointed at the staged screenshot...
    cmd = [c for c in fake.state["calls"] if c and os.path.basename(c[0]) not in ("git", "gh")][0]
    assert ".conductor_inbox/01.png" in cmd[-1]
    # ...but the inbox is gone before commit, so it never reaches the PR.
    assert not (Path(rec.worktree_path) / ".conductor_inbox").exists()


def test_run_task_without_images_leaves_prompt_unchanged(cfg, monkeypatch):
    fake = make_fake_run()
    monkeypatch.setattr(runner, "_run", fake)
    monkeypatch.setattr(ConductorConfig, "resolve_bin", lambda self, b: "/usr/bin/%s" % b)
    rec = _record(task="just do the thing")
    runner.run_task(cfg, rec)
    cmd = [c for c in fake.state["calls"] if c and os.path.basename(c[0]) not in ("git", "gh")][0]
    assert cmd[-1] == "just do the thing"  # no screenshot note appended


def test_submit_threads_images_onto_record(tmp_path, monkeypatch):
    captured = {}

    def fake_run_task(cfg, record, notify=None):
        captured["images"] = list(record.images)
        record.status = TaskStatus.DONE.value
        record.finished_at = 1.0
        return record

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    rec = mgr.submit("claude", "debug this", images=["/tmp/shot.png"])
    mgr._futures[rec.id].result(timeout=10)
    assert rec.images == ["/tmp/shot.png"]
    assert captured["images"] == ["/tmp/shot.png"]


def test_submit_auto_threads_images_onto_record(tmp_path, monkeypatch):
    captured = {}

    def fake_run_task(cfg, record, notify=None):
        captured["images"] = list(record.images)
        record.status = TaskStatus.DONE.value
        record.finished_at = 1.0
        return record

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    rec = mgr.submit_auto("look at this screenshot", images=["/tmp/x.png"])
    mgr._futures[rec.id].result(timeout=10)
    assert captured["images"] == ["/tmp/x.png"]


def test_ensure_inbox_excluded_writes_git_info_exclude(tmp_path):
    # real git repo so rev-parse --git-common-dir resolves
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    cfg = ConductorConfig(repo_root=tmp_path)
    runner._ensure_inbox_excluded(cfg)
    excl = (tmp_path / ".git" / "info" / "exclude")
    assert excl.exists()
    assert ".conductor_inbox/" in excl.read_text()
    # idempotent: a second call doesn't duplicate the entry
    runner._ensure_inbox_excluded(cfg)
    assert excl.read_text().count(".conductor_inbox/") == 1


def test_retry_preserves_images(tmp_path, monkeypatch):
    def fail_run_task(cfg, record, notify=None):
        record.status = TaskStatus.FAILED.value
        record.error = "boom"
        record.finished_at = 1.0
        return record

    monkeypatch.setattr(runner, "run_task", fail_run_task)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=1))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    rec = mgr.submit("claude", "debug this", images=["/tmp/a.png"])
    mgr._futures[rec.id].result(timeout=10)
    retried = mgr.retry(rec.id)
    assert retried.id != rec.id
    assert retried.images == ["/tmp/a.png"]
    mgr._futures[retried.id].result(timeout=10)


# -- Claude-only routing + anti-collision (Codex removed 2026-06) ----------


def test_choose_engine_is_claude_only():
    for t in ("fix typo in readme", "refactor the model", "rename a var"):
        d = choose_engine(t)
        assert d.engine is Engine.CLAUDE


def test_engine_has_no_codex_member():
    assert [e.value for e in Engine] == ["claude"]


def test_find_active_duplicate_flags_same_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=2))
    _stub_health(mgr, claude_ok=True, codex_ok=True)
    first = mgr.submit("claude", "design and implement /accas function")
    dup = mgr.find_active_duplicate("design and implement /accas function")
    assert dup is not None and dup.id == first.id
    assert mgr.find_active_duplicate("a totally different task") is None
