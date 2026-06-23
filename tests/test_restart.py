"""Tests for the /restart command (auth, mechanism, fallbacks).

The actual process-killing calls (``sys.exit`` / ``os.execv``) are mocked so
the test process is never killed.
"""

from __future__ import annotations

import pytest

from wca.bot import app


# -- auth -----------------------------------------------------------------


def test_restart_rejects_non_admin():
    reply, should = app.handle_restart("/restart", is_admin=False)
    assert should is False
    assert reply == app.RESTART_DENIED_MSG


def test_restart_admin_allowed():
    reply, should = app.handle_restart("/restart", is_admin=True)
    assert should is True
    assert "Restarting" in reply


def test_is_restart_command_matches_variants():
    assert app._is_restart_command("/restart")
    assert app._is_restart_command("/restart pull")
    assert app._is_restart_command("/restart@gamble_1bot")
    assert not app._is_restart_command("/restartx")
    assert not app._is_restart_command("/card")
    assert not app._is_restart_command("")


# -- mechanism: supervised -> clean exit ----------------------------------


def test_perform_restart_supervised_exits_cleanly(monkeypatch):
    monkeypatch.setattr(app, "_supervised", lambda: True)
    calls = {}
    monkeypatch.setattr(app.sys, "exit", lambda code=0: calls.setdefault("exit", code))
    # os.execv must NOT be called in the supervised path.
    monkeypatch.setattr(app.os, "execv", lambda *a: calls.setdefault("execv", a))
    app.perform_restart()
    assert calls == {"exit": 0}


# -- mechanism: unsupervised -> self re-exec ------------------------------


def test_perform_restart_unsupervised_reexecs(monkeypatch):
    monkeypatch.setattr(app, "_supervised", lambda: False)
    seen = {}
    monkeypatch.setattr(app.os, "execv", lambda path, argv: seen.update(path=path, argv=argv))
    monkeypatch.setattr(app.sys, "exit", lambda code=0: seen.setdefault("exit", code))
    app.perform_restart()
    assert seen.get("path") == app.sys.executable
    assert seen.get("argv")[0] == app.sys.executable
    assert "exit" not in seen  # supervised path must not run


# -- supervision detection ------------------------------------------------


def test_supervised_env_override(monkeypatch):
    monkeypatch.setenv("WCA_RESTART_MODE", "exec")
    assert app._supervised() is False
    monkeypatch.setenv("WCA_RESTART_MODE", "supervised")
    assert app._supervised() is True


def test_supervised_heuristic_ppid(monkeypatch):
    monkeypatch.delenv("WCA_RESTART_MODE", raising=False)
    monkeypatch.setattr(app.os, "getppid", lambda: 1)
    assert app._supervised() is True
    monkeypatch.setattr(app.os, "getppid", lambda: 4242)
    assert app._supervised() is False


# -- /restart pull --------------------------------------------------------


def test_restart_pull_failure_blocks_restart(monkeypatch):
    monkeypatch.setattr(app, "_git_pull", lambda repo_root=".": (False, "fatal: boom"))
    reply, should = app.handle_restart("/restart pull", is_admin=True)
    assert should is False
    assert "NOT restarting" in reply
    assert "boom" in reply


def test_restart_pull_success_then_restart(monkeypatch):
    monkeypatch.setattr(app, "_git_pull", lambda repo_root=".": (True, "Already up to date."))
    monkeypatch.setattr(app, "_supervised", lambda: True)
    reply, should = app.handle_restart("/restart pull", is_admin=True)
    assert should is True
    assert "Pulled latest" in reply and "Restarting" in reply
