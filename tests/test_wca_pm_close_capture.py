"""Tests for scripts/wca_pm_close_capture.py (MacBook-side PM close capture).

No network calls: ``top_of_book``/``price_history`` are injected as fakes.
Covers: live-mode capture (kicked-off vs future deciding matches), backfill
row shape, and the artifact idempotency wired through from wca.pmclose.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "wca_pm_close_capture.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("wca_pm_close_capture", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return _load_module()


def _make_orderflow_db(path, markets):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE pm_markets (condition_id TEXT PRIMARY KEY, event_slug TEXT, "
        "market_slug TEXT, question TEXT, event_title TEXT, category TEXT, team TEXT, "
        "outcomes TEXT, token_ids TEXT, closed INTEGER NOT NULL DEFAULT 0, "
        "resolved_outcome_index INTEGER, end_date TEXT, game_start_time TEXT, "
        "volume REAL, liquidity REAL, fetched_utc TEXT)"
    )
    for m in markets:
        con.execute(
            "INSERT INTO pm_markets (condition_id, question, category, team, "
            "token_ids, closed) VALUES (?, ?, ?, ?, ?, ?)",
            (
                m["condition_id"],
                m["question"],
                m["category"],
                m["team"],
                json.dumps(m["token_ids"]),
                int(m.get("closed", 0)),
            ),
        )
    con.commit()
    con.close()


def _write_results(path, rows):
    with open(path, "w") as fh:
        json.dump({"results": rows, "_comment": "test"}, fh)


@pytest.fixture()
def env(tmp_path, mod):
    orderflow_db = str(tmp_path / "pm_orderflow.db")
    _make_orderflow_db(
        orderflow_db,
        [
            {
                "condition_id": "0xghana",
                "question": "Will Ghana be eliminated in the Round of 32 at the 2026 FIFA World Cup?",
                "category": "advancement_r32",
                "team": "Ghana",
                "token_ids": ["tokGhanaYes", "tokGhanaNo"],
            },
            {
                "condition_id": "0xfuture",
                "question": "Will Iceland reach the Round of 16 at the 2026 FIFA World Cup?",
                "category": "advancement_r16",
                "team": "Iceland",
                "token_ids": ["tokIcelandYes", "tokIcelandNo"],
            },
        ],
    )
    results_path = str(tmp_path / "wc2026_results.json")
    _write_results(
        results_path,
        [
            {"date": "2026-06-20", "fixture": "Ghana vs Egypt", "score": "0-1",
             "outcome": "away", "kickoff_utc": "2026-06-20T18:00:00Z"},
            # Iceland has no played match -> no deciding kickoff -> skipped.
        ],
    )
    return {"orderflow_db": orderflow_db, "results": results_path}


# ---------------------------------------------------------------------------
# Live capture.
# ---------------------------------------------------------------------------


def test_capture_live_captures_kicked_off_team_only(env, mod):
    calls = []

    def fake_top_of_book(token_id, **kwargs):
        calls.append(token_id)
        return {"bid": 0.53, "ask": 0.57, "mid": 0.55, "spread": 0.04}

    rows = mod.capture_live(
        env["orderflow_db"], env["results"],
        now_utc="2026-06-21T00:00:00Z",
        top_of_book_fn=fake_top_of_book,
    )
    assert len(rows) == 1
    assert rows[0]["condition_id"] == "0xghana"
    assert rows[0]["token_id"] == "tokGhanaYes"
    assert rows[0]["close_ts_utc"] == "2026-06-20T18:00:00Z"
    assert rows[0]["mid"] == 0.55
    assert rows[0]["source"] == "top_of_book"
    assert calls == ["tokGhanaYes"]  # Iceland skipped (no deciding kickoff yet)


def test_capture_live_no_book_falls_back_to_price_history(env, mod):
    from datetime import datetime, timezone

    def fake_price_history(token_id, **kwargs):
        return [
            (datetime(2026, 6, 19, tzinfo=timezone.utc), 0.40),
            (datetime(2026, 6, 20, 19, 0, tzinfo=timezone.utc), 0.58),
        ]

    rows = mod.capture_live(
        env["orderflow_db"], env["results"],
        now_utc="2026-06-21T00:00:00Z",
        top_of_book_fn=lambda token_id, **kw: None,
        price_history_fn=fake_price_history,
    )
    assert len(rows) == 1
    assert rows[0]["mid"] == 0.58
    assert rows[0]["source"] == "price_history_last_trade"
    assert rows[0]["best_bid"] is None


def test_capture_live_no_book_no_history_skips_row(env, mod):
    rows = mod.capture_live(
        env["orderflow_db"], env["results"],
        now_utc="2026-06-21T00:00:00Z",
        top_of_book_fn=lambda token_id, **kw: None,
        price_history_fn=lambda *a, **k: [],
    )
    assert rows == []


def test_capture_live_missing_orderflow_db_returns_empty(tmp_path, mod):
    rows = mod.capture_live(
        str(tmp_path / "nope.db"), str(tmp_path / "nope.json"),
        top_of_book_fn=lambda *a, **k: {"mid": 0.5},
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Backfill.
# ---------------------------------------------------------------------------


def test_capture_backfill_row_shape(env, mod):
    from datetime import datetime, timezone

    def fake_price_history(token_id, **kwargs):
        return [
            (datetime(2026, 6, 15, tzinfo=timezone.utc), 0.40),
            (datetime(2026, 6, 20, 19, 0, tzinfo=timezone.utc), 0.60),  # after KO
            (datetime(2026, 6, 25, tzinfo=timezone.utc), 0.80),
        ]

    rows = mod.capture_backfill(
        env["orderflow_db"], env["results"], price_history_fn=fake_price_history
    )
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) >= {
        "condition_id", "token_id", "question", "close_ts_utc", "mid",
        "best_bid", "best_ask", "source", "captured_utc",
    }
    assert row["source"] == "price_history_backfill"
    assert row["mid"] == 0.60  # first point at/after the 18:00Z kickoff
    assert row["close_ts_utc"] == "2026-06-20T18:00:00Z"


def test_capture_backfill_falls_back_to_last_point_if_none_after_kickoff(env, mod):
    from datetime import datetime, timezone

    def fake_price_history(token_id, **kwargs):
        return [
            (datetime(2026, 6, 1, tzinfo=timezone.utc), 0.40),
            (datetime(2026, 6, 5, tzinfo=timezone.utc), 0.45),
        ]

    rows = mod.capture_backfill(
        env["orderflow_db"], env["results"], price_history_fn=fake_price_history
    )
    assert len(rows) == 1
    assert rows[0]["mid"] == 0.45


def test_capture_backfill_empty_history_skips_row(env, mod):
    rows = mod.capture_backfill(
        env["orderflow_db"], env["results"], price_history_fn=lambda *a, **k: []
    )
    assert rows == []


# ---------------------------------------------------------------------------
# End-to-end via main(): artifact idempotency.
# ---------------------------------------------------------------------------


def test_main_writes_artifact_and_rerun_is_noop(env, mod, monkeypatch, tmp_path, capsys):
    artifact = str(tmp_path / "pm_closes.json")
    monkeypatch.setattr(
        mod.clob, "top_of_book",
        lambda token_id, **kw: {"bid": 0.53, "ask": 0.57, "mid": 0.55, "spread": 0.04},
    )
    rc = mod.main([
        "--orderflow-db", env["orderflow_db"],
        "--results", env["results"],
        "--artifact", artifact,
        "--now", "2026-06-21T00:00:00Z",
    ])
    assert rc == 0
    assert os.path.exists(artifact)
    on_disk = json.load(open(artifact))
    assert len(on_disk) == 1
    mtime_1 = os.path.getmtime(artifact)

    import time
    time.sleep(0.01)
    rc2 = mod.main([
        "--orderflow-db", env["orderflow_db"],
        "--results", env["results"],
        "--artifact", artifact,
        "--now", "2026-06-21T00:00:00Z",
    ])
    assert rc2 == 0
    mtime_2 = os.path.getmtime(artifact)
    assert mtime_1 == mtime_2  # no-op rerun: file untouched
    out = capsys.readouterr().out
    assert "0 new row" in out


def test_main_missing_orderflow_db_errors(tmp_path, mod):
    rc = mod.main(["--orderflow-db", str(tmp_path / "nope.db")])
    assert rc == 1
