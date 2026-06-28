"""Tests for the Polymarket price-history store (wca.pmhistory)."""

from __future__ import annotations

import os
import sqlite3

from wca import pmhistory as ph


def _con():
    con = sqlite3.connect(":memory:")
    ph.ensure_schema(con)
    return con


def test_append_and_skip_invalid():
    con = _con()
    n = ph.append_snapshots(con, [
        {"kind": "advancement", "team": "Brazil", "stage": "R16", "pm_mid": 0.6, "model_prob": 0.7},
        {"kind": "advancement", "team": "Brazil", "stage": "R16", "pm_mid": 1.4},   # out of range
        {"kind": "advancement", "team": "Brazil", "stage": "R16", "pm_mid": "x"},    # non-numeric
    ], ts_utc="2026-06-26T05:00:00Z")
    assert n == 1
    assert con.execute("select count(*) from pm_snapshots").fetchone()[0] == 1


def test_trajectory_grouped_and_ordered():
    con = _con()
    ph.append_snapshots(con, [{"kind": "advancement", "team": "Iran", "stage": "R16", "pm_mid": 0.5, "model_prob": 0.66}], "2026-06-26T05:00:00Z")
    ph.append_snapshots(con, [{"kind": "advancement", "team": "Iran", "stage": "R16", "pm_mid": 0.58, "model_prob": 0.66}], "2026-06-27T05:00:00Z")
    traj = ph.trajectory(con, kind="advancement")
    assert len(traj) == 1
    snaps = list(traj.values())[0]
    assert [s["pm_mid"] for s in snaps] == [0.5, 0.58]          # time-ordered
    assert snaps[0]["ts_utc"] < snaps[1]["ts_utc"]


def test_convergence_inputs_entry_is_earliest_no_lookahead():
    con = _con()
    # three snapshots; entry must be the EARLIEST and its model_prob used
    for ts, mid in [("2026-06-26T05:00:00Z", 0.50), ("2026-06-26T17:00:00Z", 0.54), ("2026-06-27T05:00:00Z", 0.60)]:
        ph.append_snapshots(con, [{"kind": "advancement", "team": "Japan", "stage": "R16", "pm_mid": mid, "model_prob": 0.67}], ts)
    rows = ph.convergence_inputs(con, kind="advancement")
    assert len(rows) == 1
    r = rows[0]
    assert r["entry_pm"] == 0.50 and r["later_pm"] == 0.60   # earliest / latest
    assert r["model"] == 0.67 and r["n_snaps"] == 3
    assert r["span_hours"] == 24.0


def test_single_snapshot_market_omitted():
    con = _con()
    ph.append_snapshots(con, [{"kind": "outright", "team": "Spain", "stage": "win", "pm_mid": 0.1, "model_prob": 0.12}], "2026-06-26T05:00:00Z")
    assert ph.convergence_inputs(con) == []   # need >=2 snapshots


def test_jsonl_roundtrip_and_convergence(tmp_path):
    p = str(tmp_path / "pm_hist.jsonl")
    ph.append_jsonl(p, [{"kind": "advancement", "team": "Brazil", "stage": "R16", "market_slug": "Brazil:R16", "pm_mid": 0.50, "model_prob": 0.66}], "2026-06-26T05:00:00Z")
    ph.append_jsonl(p, [{"kind": "advancement", "team": "Brazil", "stage": "R16", "market_slug": "Brazil:R16", "pm_mid": 0.60, "model_prob": 0.66}], "2026-06-27T05:00:00Z")
    recs = ph.load_records(p)
    assert len(recs) == 2
    rows = ph.convergence_inputs_from_records(recs, kind="advancement")
    assert len(rows) == 1
    assert rows[0]["entry_pm"] == 0.50 and rows[0]["later_pm"] == 0.60 and rows[0]["model"] == 0.66


def test_snapshotter_rows_from_advancement():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "wca_pm_snapshot", os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_pm_snapshot.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    adv = {"teams": [
        {"team": "Argentina", "model": {"R16": 0.85, "win": 0.17},
         "pm": {"R16": {"pm": 0.92}, "win": {"pm": 0.18}, "QF": {"pm": None}}},
        {"team": "Nowhere", "model": {}, "pm": {}},
    ]}
    rows = mod.rows_from_advancement(adv)
    # 2 priced markets for Argentina (R16, win); QF omitted (pm None); Nowhere none
    assert len(rows) == 2
    r16 = [r for r in rows if r["stage"] == "R16"][0]
    assert r16["team"] == "Argentina" and r16["pm_mid"] == 0.92 and r16["model_prob"] == 0.85
