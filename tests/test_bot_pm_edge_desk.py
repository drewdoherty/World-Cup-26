"""/pm EDGE DESK (SHADOW) section: happy path, missing feed, stale feed.

The section is read-only decision support riding at the bottom of ``/pm``:
it must be impossible to read as executable (no PM-<n> tokens, no Y-able
ids, every row labelled ``shadow``), and a missing/stale/unstamped feed must
yield one honest line — never an exception, never silently-shown stale rows.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from wca.bot import app


def _stamp(hours_ago):
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed(hours_ago=1.0, freshness=True, rows=None):
    return {
        "meta": {
            "generated_at": _stamp(hours_ago),
            "n_by_verdict": {"SHADOW_ADD": 2, "WATCH": 7,
                             "WITHHOLD": 2, "DO_NOT_TRADE": 1},
        },
        "freshness": {"pass": freshness},
        "rows": rows if rows is not None else [
            {"team": "Morocco", "stage": "SF", "verdict": "SHADOW_ADD",
             "bucket": "moneyline", "model_prob": 0.55, "pm_price": 0.44,
             "edge_adj": 0.1026},
            {"team": "Brazil", "stage": "group_winner",
             "verdict": "SHADOW_ADD", "bucket": "mid", "model_prob": 0.48,
             "pm_price": 0.41, "edge_adj": 0.0511},
            {"team": "Coldland", "stage": "SF", "verdict": "DO_NOT_TRADE",
             "bucket": "moneyline", "model_prob": 0.52, "pm_price": 0.56,
             "edge_adj": -0.011},
        ],
    }


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_pm_edge_desk_happy_path(tmp_path):
    p = tmp_path / "advancement_edge_desk.json"
    _write(p, _feed())
    out = app.handle_pm(str(tmp_path / "db.db"), edge_desk_path=str(p))
    assert "EDGE DESK (SHADOW)" in out
    # SHADOW_ADD rows in FEED order: PM ¢ convention, bucket tag, team/stage,
    # fee-adjusted edge with the +EV/−EV marker (ruling 2026-07-08),
    # settlement basis — labelled shadow, never an order.
    assert ("shadow [MONEYLINE] Morocco SF — model 55¢ / PM 44¢, "
            "edge +10.3¢ ✅+EV — incl. ET+pens") in out
    assert ("shadow [MID] Brazil group_winner — model 48¢ / PM 41¢, "
            "edge +5.1¢ ✅+EV — settles on group stage") in out
    # Non-SHADOW verdicts are counted, not listed.
    assert "watch 7 · withhold 2 · do-not-trade 1" in out
    assert "Coldland" not in out
    # CLV-blocker / freshness caveat line.
    assert "live money BLOCKED" in out
    assert "freshness PASS" in out


def test_pm_edge_desk_is_never_executable(tmp_path):
    p = tmp_path / "edge.json"
    _write(p, _feed())
    section = "\n".join(app._edge_desk_section(str(p)))
    assert "PM-" not in section            # no parked-order tokens
    assert "Y PM" not in section           # nothing Y-able
    assert "shadow" in section
    assert "read-only" in section


def test_pm_edge_desk_missing_feed_one_honest_line(tmp_path):
    out = app.handle_pm(str(tmp_path / "db.db"),
                        edge_desk_path=str(tmp_path / "nope.json"))
    assert "EDGE DESK (SHADOW)" in out
    assert "feed missing" in out
    assert "Morocco" not in out


def test_pm_edge_desk_stale_feed_hidden(tmp_path):
    p = tmp_path / "edge.json"
    _write(p, _feed(hours_ago=7.0))
    out = app.handle_pm(str(tmp_path / "db.db"), edge_desk_path=str(p))
    assert "feed stale" in out and "not shown" in out
    assert "Morocco" not in out            # stale rows never rendered


def test_pm_edge_desk_fresh_boundary(tmp_path):
    p = tmp_path / "edge.json"
    _write(p, _feed(hours_ago=5.9))
    assert "Morocco" in "\n".join(app._edge_desk_section(str(p)))
    _write(p, _feed(hours_ago=6.1))
    assert "Morocco" not in "\n".join(app._edge_desk_section(str(p)))


def test_pm_edge_desk_future_or_unparseable_stamp_fails_closed(tmp_path):
    p = tmp_path / "edge.json"
    _write(p, _feed(hours_ago=-2.0))       # 2h in the future
    section = "\n".join(app._edge_desk_section(str(p)))
    assert "future-dated" in section and "Morocco" not in section
    feed = _feed()
    feed["meta"]["generated_at"] = "not a timestamp"
    _write(p, feed)
    section = "\n".join(app._edge_desk_section(str(p)))
    assert "age unknown" in section and "Morocco" not in section


def test_pm_edge_desk_empty_shadow_rows_honest(tmp_path):
    p = tmp_path / "edge.json"
    _write(p, _feed(rows=[{"team": "Coldland", "stage": "SF",
                           "verdict": "DO_NOT_TRADE", "bucket": "moneyline",
                           "model_prob": 0.52, "pm_price": 0.56,
                           "edge_adj": -0.011}]))
    section = "\n".join(app._edge_desk_section(str(p)))
    assert "no SHADOW_ADD rows" in section
    assert "watch 7" in section            # counts still shown from meta
