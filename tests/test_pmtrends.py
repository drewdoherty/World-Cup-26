"""Tests for the Polymarket trajectory line-chart engine (wca.pmtrends)."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from wca import pmmovers, pmtrends


def _rec(ts, pm, team="Brazil", stage="R16", kind="advancement"):
    return {"ts_utc": ts, "kind": kind, "team": team, "stage": stage,
            "pm_mid": pm, "market_slug": "%s:%s" % (team, stage)}


def _dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- trajectories


def test_trajectories_group_by_team_sorted_and_filtered():
    recs = pmmovers.clean_records([
        _rec("2026-06-29 12:00 UTC", 0.6, team="Japan", stage="R16"),
        _rec("2026-06-28 12:00 UTC", 0.5, team="Japan", stage="R16"),
        _rec("2026-06-28 12:00 UTC", 0.2, team="Japan", stage="QF"),   # other stage
        _rec("2026-06-28 12:00 UTC", 0.4, team="Spain", stage="R16"),
    ])
    tr = pmtrends.trajectories(recs, stage="R16", category="advancement")
    assert set(tr) == {"Japan", "Spain"}
    assert [p[1] for p in tr["Japan"]] == [0.5, 0.6]          # time-sorted, R16 only


def test_window_filter_clips_to_hours():
    recs = pmmovers.clean_records([
        _rec("2026-06-28 12:00 UTC", 0.5),
        _rec("2026-06-29 12:00 UTC", 0.6),
    ])
    tr = pmtrends.trajectories(recs, stage="R16")
    anchor = pmmovers.anchor_time(recs)
    win = pmtrends._window_filter(tr, anchor=anchor, hours=6.0)
    assert [p[1] for p in win["Brazil"]] == [0.6]            # only the recent point
    full = pmtrends._window_filter(tr, anchor=anchor, hours=None)
    assert len(full["Brazil"]) == 2


# --------------------------------------------------------------------------- selection


def test_select_teams_orders_by_kickoff_then_price():
    recs = pmmovers.clean_records([
        _rec("2026-06-28 12:00 UTC", 0.30, team="Norway"),
        _rec("2026-06-29 12:00 UTC", 0.35, team="Norway"),
        _rec("2026-06-28 12:00 UTC", 0.60, team="England"),
        _rec("2026-06-29 12:00 UTC", 0.62, team="England"),
    ])
    tr = pmtrends.trajectories(recs, stage="R16")
    kickoffs = {"Norway": _dt("2026-06-30T17:00:00"), "England": _dt("2026-07-01T16:00:00")}
    order = pmtrends.select_teams(tr, kickoffs=kickoffs, top_n=5)
    assert order == ["Norway", "England"]                    # sooner kickoff first


def test_select_teams_drops_resolved_when_require_live():
    recs = pmmovers.clean_records([
        _rec("2026-06-28 12:00 UTC", 1.0, team="Canada"),    # resolved to 100¢
        _rec("2026-06-29 12:00 UTC", 1.0, team="Canada"),
        _rec("2026-06-28 12:00 UTC", 0.4, team="Spain"),
        _rec("2026-06-29 12:00 UTC", 0.45, team="Spain"),
    ])
    tr = pmtrends.trajectories(recs, stage="R16")
    live = pmtrends.select_teams(tr, top_n=5, require_live=True)
    assert "Canada" not in live and "Spain" in live
    allt = pmtrends.select_teams(tr, top_n=5, require_live=False)
    assert "Canada" in allt


# --------------------------------------------------------------------------- context loaders


def test_load_kickoffs_parses_fixtures(tmp_path):
    p = tmp_path / "scores.json"
    p.write_text(json.dumps({"fixtures": [
        {"fixture": "Ivory Coast vs Norway", "kickoff": "2026-06-30T17:00:00+00:00"},
        {"fixture": "England vs DR Congo", "kickoff": "2026-07-01T16:00:00+00:00"},
        {"fixture": "TBD", "kickoff": None},
    ]}))
    ko = pmtrends.load_kickoffs(str(p))
    assert ko["Ivory Coast"] == _dt("2026-06-30T17:00:00")
    assert ko["England"] == _dt("2026-07-01T16:00:00")
    assert "TBD" not in ko


def test_exposure_teams_matches_open_bets(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE bets (status TEXT, match_desc TEXT, selection TEXT)")
    con.executemany(
        "INSERT INTO bets(status, match_desc, selection) VALUES (?,?,?)",
        [("open", "Belgium vs Iran", "Belgium win"),
         ("open", "Ecuador-Germany / Japan-Sweden", "Germany + Japan"),
         ("lost", "Spain vs Austria", "Spain win")])  # settled -> ignored
    con.commit(); con.close()
    known = ["Belgium", "Germany", "Japan", "Sweden", "Spain", "Brazil"]
    expo = pmtrends.exposure_teams(str(db), known)
    assert expo == {"Belgium", "Germany", "Japan", "Sweden"}  # Spain (settled) & Brazil excluded


# --------------------------------------------------------------------------- figures


def test_resample_series_one_last_point_per_bin():
    from datetime import timedelta
    base = _dt("2026-06-30T12:00:00")
    pts = [(base + timedelta(minutes=m), 0.50 + m / 1000.0) for m in (0, 10, 25, 40, 70)]
    r = pmtrends.resample_series(pts, bin_minutes=30)
    # bins: [12:00,12:30)->0,10,25 (keep 25); [12:30,13:00)->40; [13:00,13:30)->70
    assert len(r) == 3
    assert r[0][0].minute == 25                     # last observation in bin wins
    assert pmtrends.resample_series(pts, bin_minutes=None) == pts


def test_build_market_figures_emits_png_per_period():
    recs = pmmovers.clean_records([
        _rec("2026-06-28 12:00 UTC", 0.50, team="Norway"),
        _rec("2026-06-29 12:00 UTC", 0.55, team="Norway"),
        _rec("2026-06-28 12:00 UTC", 0.40, team="Spain"),
        _rec("2026-06-29 12:00 UTC", 0.45, team="Spain"),
    ])
    figs = pmtrends.build_market_figures(
        recs, stage="R16", periods=[("Full history", None)], top_n=5)
    assert len(figs) == 1
    f = figs[0]
    assert f["market"] == "Reach Round of 16"
    assert f["png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert set(f["teams"]) == {"Norway", "Spain"}
