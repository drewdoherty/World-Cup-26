"""Durable on-disk state for the dev-conductor.

The conductor used to keep every task record in process memory only
(``ConductorManager._records``). A KeepAlive/sleep restart therefore lost all
in-flight tasks silently — the operator's #1 complaint. This module persists
each :class:`TaskRecord` to a small SQLite database on every state transition
so the registry survives restarts, and stores per-chat conversation history so
the conversational mode keeps context across restarts too.

Dependency-free (stdlib ``sqlite3`` + ``json``). The connection is opened with
``check_same_thread=False`` and every access is guarded by a lock, because the
conductor writes from its worker-pool threads as well as the poll thread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from wca.conductor.models import TaskRecord

# Columns persisted for a task, in TaskRecord order. ``dedupe_key`` /
# ``duplicate_of`` back the idempotency guard (see ConductorManager).
_TASK_COLUMNS = (
    "id", "engine", "task", "chat_id", "images", "shortid", "branch",
    "worktree_path", "status", "summary", "error", "route_reason", "tokens",
    "returncode", "pr_url", "activity", "activity_at", "created_at",
    "started_at", "finished_at", "dedupe_key", "duplicate_of",
)


class ConductorStore:
    """SQLite-backed task + chat-history store. Thread-safe."""

    def __init__(self, path: "str | Path") -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id            INTEGER PRIMARY KEY,
                    engine        TEXT,
                    task          TEXT,
                    chat_id       TEXT,
                    images        TEXT,
                    shortid       TEXT,
                    branch        TEXT,
                    worktree_path TEXT,
                    status        TEXT,
                    summary       TEXT,
                    error         TEXT,
                    route_reason  TEXT,
                    tokens        INTEGER,
                    returncode    INTEGER,
                    pr_url        TEXT,
                    activity      TEXT,
                    activity_at   REAL,
                    created_at    REAL,
                    started_at    REAL,
                    finished_at   REAL,
                    dedupe_key    TEXT,
                    duplicate_of  INTEGER
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role    TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts      REAL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_chat
                    ON chat_messages(chat_id, id);
                """
            )
            self._conn.commit()

    # -- tasks ------------------------------------------------------------

    def upsert_task(self, record: TaskRecord) -> None:
        """Persist one record (INSERT OR REPLACE on the primary key)."""
        row = (
            record.id, record.engine, record.task, record.chat_id,
            json.dumps(list(record.images or [])), record.shortid, record.branch,
            record.worktree_path, record.status, record.summary, record.error,
            record.route_reason, int(record.tokens or 0), record.returncode,
            record.pr_url, record.activity, float(record.activity_at or 0.0),
            float(record.created_at or 0.0), record.started_at, record.finished_at,
            record.dedupe_key, record.duplicate_of,
        )
        placeholders = ", ".join("?" for _ in _TASK_COLUMNS)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks (%s) VALUES (%s)"
                % (", ".join(_TASK_COLUMNS), placeholders),
                row,
            )
            self._conn.commit()

    def load_tasks(self) -> List[TaskRecord]:
        """Every persisted task, ordered by id."""
        with self._lock:
            cur = self._conn.execute("SELECT * FROM tasks ORDER BY id")
            rows = cur.fetchall()
        return [_row_to_record(r) for r in rows]

    def max_task_id(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT MAX(id) FROM tasks")
            val = cur.fetchone()[0]
        return int(val) if val is not None else 0

    # -- chat history -----------------------------------------------------

    def append_chat(self, chat_id: str, role: str, content: str, ts: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                (str(chat_id), role, content, float(ts or 0.0)),
            )
            self._conn.commit()

    def load_chat(self, limit_per_chat: int = 40) -> Dict[str, List[Tuple[str, str]]]:
        """Most recent ``limit_per_chat`` (role, content) turns per chat, oldest-first."""
        out: Dict[str, List[Tuple[str, str]]] = {}
        with self._lock:
            chats = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT chat_id FROM chat_messages").fetchall()]
            for chat_id in chats:
                cur = self._conn.execute(
                    "SELECT role, content FROM chat_messages WHERE chat_id = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (chat_id, limit_per_chat),
                )
                rows = cur.fetchall()
            # reverse to oldest-first for prompt building
                out[str(chat_id)] = [(r["role"], r["content"]) for r in reversed(rows)]
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    try:
        images = json.loads(row["images"]) if row["images"] else []
    except (ValueError, TypeError):
        images = []
    return TaskRecord(
        id=int(row["id"]),
        engine=row["engine"] or "claude",
        task=row["task"] or "",
        chat_id=row["chat_id"] or "",
        images=list(images),
        shortid=row["shortid"] or "",
        branch=row["branch"],
        worktree_path=row["worktree_path"],
        status=row["status"] or "queued",
        summary=row["summary"] or "",
        error=row["error"] or "",
        route_reason=row["route_reason"] or "",
        tokens=int(row["tokens"] or 0),
        returncode=row["returncode"],
        pr_url=row["pr_url"],
        activity=row["activity"] or "",
        activity_at=float(row["activity_at"] or 0.0),
        created_at=float(row["created_at"] or 0.0),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        dedupe_key=row["dedupe_key"] or "",
        duplicate_of=row["duplicate_of"],
    )
