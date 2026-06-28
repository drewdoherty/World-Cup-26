"""Tests for the outright-edge feed builder (scripts/wca_outright_edge_data.py)."""

from __future__ import annotations

import importlib.util
import json
import os


def _load():
    spec = importlib.util.spec_from_file_location(
        "wca_outright_edge_data",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_outright_edge_data.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(team, stage, ts, pm, model):
    return {"kind": "advancement", "team": team, "stage": stage,
            "market_slug": "%s:%s" % (team, stage), "ts_utc": ts, "pm_mid": pm, "model_prob": model}


def test_collecting_with_single_capture():
    mod = _load()
    recs = [_rec("Brazil", "R16", "2026-06-26T05:00:00Z", 0.5, 0.66)]
    feed = mod.build_feed(recs, generated="2026-06-28T00:00:00Z")
    assert feed["convergence"]["state"] == "COLLECTING"
    assert feed["meta"]["n_captures"] == 1
    assert feed["calibration"]["state"] == "insufficient"


def test_convergence_live_with_two_captures():
    mod = _load()
    recs = []
    for i in range(35):
        recs.append(_rec("T%d" % i, "R16", "2026-06-26T05:00:00Z", 0.50, 0.66))
        recs.append(_rec("T%d" % i, "R16", "2026-06-27T05:00:00Z", 0.58, 0.66))
    feed = mod.build_feed(recs, generated="2026-06-28T00:00:00Z")
    assert feed["convergence"]["n_signal"] == 35
    assert feed["convergence"]["convergence_rate"] == 1.0
    assert feed["convergence"]["state"] == "live"


def test_feed_deterministic():
    mod = _load()
    recs = [_rec("Iran", "R16", "2026-06-26T05:00:00Z", 0.5, 0.66),
            _rec("Iran", "R16", "2026-06-27T05:00:00Z", 0.55, 0.66)]
    a = json.dumps(mod.build_feed(recs, generated="2026-06-28T00:00:00Z"), sort_keys=True)
    b = json.dumps(mod.build_feed(recs, generated="2026-06-28T00:00:00Z"), sort_keys=True)
    assert a == b
