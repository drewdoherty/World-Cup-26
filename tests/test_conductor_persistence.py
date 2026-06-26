"""Tests for the conductor's durable state + conversational chat mode.

Two features, exercised offline:

1. **Persistence + restart reattach** — a task that was in flight when the
   process died is reloaded as INTERRUPTED and surfaced to the user; the id
   counter never collides with a persisted id; an identical resubmit is deduped
   instead of double-dispatched.
2. **Conversational chat** — a plain (non-slash) message gets a reply (was
   silently dropped before), context is kept per chat and survives a restart,
   and /task still dispatches.
"""

from __future__ import annotations

import threading
import time

import pytest

from wca.conductor import runner
from wca.conductor.config import ConductorConfig
from wca.conductor.manager import ConductorManager
from wca.conductor.models import AgentResult, Engine, TaskRecord, TaskStatus
from wca.conductor.health import EngineHealth
from wca.conductor.store import ConductorStore

import scripts.wca_conductor as conductor_bot


# -- helpers --------------------------------------------------------------


def _stub_health(mgr, ok=True, reason="ok"):
    mgr.engine_health = lambda engine, force=False: EngineHealth(  # type: ignore[assignment]
        Engine.coerce(engine).value, ok, reason if ok else "claude logged out")


class FakeClient:
    """Minimal Telegram client: records every outbound message."""

    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((str(chat_id), text))
        return {}

    def set_my_commands(self, *a, **k):
        return {}


def _msg(text, chat_id=1, user_id=1):
    return {"text": text, "chat": {"id": chat_id}, "from": {"id": user_id}}


# -- persistence + reattach -----------------------------------------------


def test_record_persists_and_reloads(tmp_path):
    db = tmp_path / "state.db"
    store = ConductorStore(db)
    rec = TaskRecord(id=3, engine="claude", task="do a thing", chat_id="55",
                     status=TaskStatus.DONE.value, tokens=120, pr_url="https://x/pull/3",
                     dedupe_key="do-a-thing")
    store.upsert_task(rec)
    store.close()

    reopened = ConductorStore(db)
    loaded = reopened.load_tasks()
    assert len(loaded) == 1
    assert loaded[0].id == 3 and loaded[0].task == "do a thing"
    assert loaded[0].status == TaskStatus.DONE.value and loaded[0].tokens == 120
    assert loaded[0].pr_url == "https://x/pull/3"
    assert reopened.max_task_id() == 3


def test_restart_marks_in_flight_task_interrupted(tmp_path, monkeypatch):
    db = tmp_path / "state.db"

    # First process: a task that reaches RUNNING (persisted) then the process
    # "dies" mid-flight (run_task leaves it RUNNING).
    def fake_running(cfg, rec, notify=None):
        rec.status = TaskStatus.RUNNING.value
        if notify:
            notify(rec)  # _on_transition -> persists RUNNING
        return rec

    monkeypatch.setattr(runner, "run_task", fake_running)
    store1 = ConductorStore(db)
    mgr1 = ConductorManager(ConductorConfig(repo_root=tmp_path), store=store1)
    _stub_health(mgr1)
    rec = mgr1.submit("claude", "long running job", chat_id="77")
    mgr1._futures[rec.id].result(timeout=10)
    assert mgr1.get(rec.id).status == TaskStatus.RUNNING.value
    store1.close()  # simulate shutdown without finishing the task

    # Second process: reattach should flag the RUNNING task as INTERRUPTED and
    # keep the counter past it.
    store2 = ConductorStore(db)
    mgr2 = ConductorManager(ConductorConfig(repo_root=tmp_path), store=store2)
    reloaded = mgr2.get(rec.id)
    assert reloaded is not None
    assert reloaded.status == TaskStatus.INTERRUPTED.value
    assert mgr2._counter >= rec.id
    # the next submitted id can't collide with the persisted one
    next_id = mgr2._counter + 1
    assert next_id > rec.id
    # surfaced exactly once
    interrupted = mgr2.take_interrupted()
    assert [r.id for r in interrupted] == [rec.id]
    assert mgr2.take_interrupted() == []


def test_reattach_announces_interrupted_to_user(tmp_path):
    db = tmp_path / "state.db"
    store = ConductorStore(db)
    # seed a task that looks like it was running at death
    store.upsert_task(TaskRecord(id=9, engine="claude", task="big migration",
                                 chat_id="123", status=TaskStatus.RUNNING.value,
                                 dedupe_key="big-migration"))
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path), store=store)
    client = FakeClient()
    bot = conductor_bot.ConductorBot(client, mgr, set(), None)
    bot._announce_interrupted()
    assert any("/retry 9" in t and "interrupted" in t.lower() for _, t in client.sent)
    assert mgr.get(9).status == TaskStatus.INTERRUPTED.value


# -- idempotency / dedupe -------------------------------------------------


def test_duplicate_resubmit_is_not_double_dispatched(tmp_path, monkeypatch):
    gate = threading.Event()

    def blocking(cfg, rec, notify=None):
        rec.status = TaskStatus.RUNNING.value
        if notify:
            notify(rec)
        gate.wait(timeout=5)
        rec.status = TaskStatus.DONE.value
        rec.finished_at = 1.0
        return rec

    monkeypatch.setattr(runner, "run_task", blocking)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=2))
    _stub_health(mgr)
    first = mgr.submit("claude", "implement the X feature")
    second = mgr.submit("claude", "implement the X feature")
    assert second.status == TaskStatus.REJECTED.value
    assert second.duplicate_of == first.id
    assert second.id not in mgr._futures  # never dispatched a second worktree
    gate.set()
    mgr._futures[first.id].result(timeout=10)


def test_distinct_tasks_are_not_deduped(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path, max_parallel=2))
    _stub_health(mgr)
    a = mgr.submit("claude", "feature A")
    b = mgr.submit("claude", "feature B")
    assert a.status != TaskStatus.REJECTED.value
    assert b.status != TaskStatus.REJECTED.value
    assert b.duplicate_of is None


# -- conversational chat --------------------------------------------------


def test_chat_returns_reply_and_threads_history(tmp_path, monkeypatch):
    captured = {}

    def fake_chat(cfg, prompt, timeout=None):
        captured["prompt"] = prompt
        return AgentResult(0, summary="The conductor fans tasks out to agents.")

    monkeypatch.setattr(runner, "run_chat", fake_chat)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr)
    reply = mgr.chat("42", "what does the conductor do?")
    assert reply == "The conductor fans tasks out to agents."
    # both turns recorded
    hist = mgr._chat_history["42"]
    assert hist[-2]["role"] == "user" and hist[-1]["role"] == "assistant"

    reply2 = mgr.chat("42", "and how do I dispatch?")
    assert reply2  # second turn works
    # the prior turn is threaded into the new prompt for context
    assert "what does the conductor do?" in captured["prompt"]


def test_chat_history_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_chat",
                        lambda cfg, prompt, timeout=None: AgentResult(0, summary="ack"))
    db = tmp_path / "state.db"
    store1 = ConductorStore(db)
    mgr1 = ConductorManager(ConductorConfig(repo_root=tmp_path), store=store1)
    _stub_health(mgr1)
    mgr1.chat("7", "remember this fact")
    store1.close()

    store2 = ConductorStore(db)
    mgr2 = ConductorManager(ConductorConfig(repo_root=tmp_path), store=store2)
    assert "7" in mgr2._chat_history
    assert any("remember this fact" in m["content"] for m in mgr2._chat_history["7"])


def test_chat_blocked_when_engine_down(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr, ok=False)
    reply = mgr.chat("1", "hello")
    assert "unavailable" in reply.lower()


def test_chat_surfaces_agent_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_chat",
                        lambda cfg, prompt, timeout=None: AgentResult(1, error="boom"))
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr)
    reply = mgr.chat("1", "hello")
    assert "boom" in reply


# -- bot routing ----------------------------------------------------------


def test_plain_text_routes_to_chat_not_silence(tmp_path, monkeypatch):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    client = FakeClient()
    bot = conductor_bot.ConductorBot(client, mgr, set(), None)
    captured = {}
    monkeypatch.setattr(bot, "_chat",
                        lambda text, chat_id, user_id: captured.update(text=text, chat=chat_id) or "ROUTED")
    reply = bot.handle(_msg("hey, what's the project about?"))
    assert reply == "ROUTED"  # not None / not silently dropped
    assert captured["text"] == "hey, what's the project about?"


def test_chat_worker_sends_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_chat",
                        lambda cfg, prompt, timeout=None: AgentResult(0, summary="pong"))
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr)
    client = FakeClient()
    bot = conductor_bot.ConductorBot(client, mgr, set(), None)
    bot._chat_worker("ping", "1")
    assert any(t == "pong" for _, t in client.sent)


def test_chat_admin_gated(tmp_path):
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    client = FakeClient()
    bot = conductor_bot.ConductorBot(client, mgr, set(), admin="999")  # only 999 is admin
    out = bot._chat("hi", "1", "7")  # non-admin user
    assert out is not None and "admin" in out.lower()


def test_task_command_still_dispatches(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_task", lambda cfg, rec, notify=None: rec)
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    _stub_health(mgr)
    client = FakeClient()
    bot = conductor_bot.ConductorBot(client, mgr, set(), None)
    reply = bot.handle(_msg("/task build a small report"))
    assert reply is not None and ("routed" in reply.lower() or "dispatched" in reply.lower())
    # a real task record was created (not routed to chat)
    assert any(r.task == "build a small report" for r in mgr.records())
