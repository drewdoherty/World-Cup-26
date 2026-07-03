"""CLI-level tests for the PM 1X2 snapshot freshness gate (network-free).

Regression guard for the 2026-07-02 postmortem: the capture pipeline (#109)
shipped with a CLI nobody scheduled, so the freshness wrapper here is what
turns a silent stall into a debounced admin alert once the job IS scheduled.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_pm_1x2_snapshot as cli  # noqa: E402

SCHEMA = (
    "CREATE TABLE odds_snapshots (ts_utc TEXT, source TEXT, match_id TEXT, "
    "market TEXT, selection TEXT, decimal_odds REAL, raw TEXT)"
)


def _empty_db():
    con = sqlite3.connect(":memory:")
    con.execute(SCHEMA)
    return con


def test_check_freshness_never_captured_fires_without_notify_flag(tmp_path):
    con = _empty_db()
    state = str(tmp_path / "alert_state.json")
    out = cli.check_freshness(
        con, stale_hours=4.0, alert_state_path=state, notify=False,
        now_iso="2026-07-02T12:00:00Z",
    )
    assert out["age_secs"] is None
    assert out["alert_fired"] is False  # notify=False suppresses the send, not the decision


def test_check_freshness_sends_and_writes_state_when_notify_true(tmp_path, monkeypatch):
    con = _empty_db()
    state = str(tmp_path / "alert_state.json")
    sent = []
    monkeypatch.setattr(cli, "_notify_stale", lambda age, hrs: sent.append((age, hrs)))
    out = cli.check_freshness(
        con, stale_hours=4.0, alert_state_path=state, notify=True,
        now_iso="2026-07-02T12:00:00Z",
    )
    assert out["alert_fired"] is True
    assert sent == [(None, 4.0)]
    assert json.loads(Path(state).read_text())["age_secs"] is None


def test_check_freshness_debounces_across_two_calls(tmp_path, monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute(SCHEMA)
    con.execute(
        "INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)",
        ("2026-07-02T06:00:00Z", "polymarket", "M1", "h2h", "Brazil", 1.9, "{}"),
    )
    con.commit()
    state = str(tmp_path / "alert_state.json")
    sent = []
    monkeypatch.setattr(cli, "_notify_stale", lambda age, hrs: sent.append(age))

    # 6h stale (threshold 4h): first call fires.
    r1 = cli.check_freshness(con, stale_hours=4.0, alert_state_path=state,
                             notify=True, now_iso="2026-07-02T12:00:00Z")
    assert r1["alert_fired"] is True and len(sent) == 1

    # Still ~6.5h stale a moment later: same window, must NOT re-fire.
    r2 = cli.check_freshness(con, stale_hours=4.0, alert_state_path=state,
                             notify=True, now_iso="2026-07-02T12:30:00Z")
    assert r2["alert_fired"] is False and len(sent) == 1

    # 10.5h stale (grown by another full 4h threshold past the 6h first-alert): re-fires.
    r3 = cli.check_freshness(con, stale_hours=4.0, alert_state_path=state,
                             notify=True, now_iso="2026-07-02T16:30:00Z")
    assert r3["alert_fired"] is True and len(sent) == 2


def test_check_freshness_fresh_row_never_fires(tmp_path, monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute(SCHEMA)
    con.execute(
        "INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)",
        ("2026-07-02T11:00:00Z", "polymarket", "M1", "h2h", "Brazil", 1.9, "{}"),
    )
    con.commit()
    sent = []
    monkeypatch.setattr(cli, "_notify_stale", lambda age, hrs: sent.append(age))
    out = cli.check_freshness(
        con, stale_hours=4.0, alert_state_path=str(tmp_path / "s.json"),
        notify=True, now_iso="2026-07-02T12:00:00Z",
    )
    assert out["alert_fired"] is False and sent == []
