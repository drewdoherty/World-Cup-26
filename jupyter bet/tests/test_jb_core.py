"""Core lib tests: ids, storage roundtrip, config, odds-api guard.

Fixture data in these tests is synthetic BY DESIGN (unit tests exercise
formulas); nothing here feeds any report or notebook output.
"""
from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest

import lib.bootstrap as bt
import lib.config as cfg
import lib.ids as ids
import lib.oddsapi as oa
import lib.storage as st


# ---------------------------------------------------------------- ids ----

def test_event_id_canonicalises_aliases():
    ko = dt.datetime(2026, 7, 5, 17, 0, tzinfo=dt.timezone.utc)
    a = ids.event_id("Korea Republic", "Czech Republic", ko)
    b = ids.event_id("South Korea", "Czechia", ko)
    assert a == b == "wc2026:south-korea__czech-republic__2026-07-05T17Z"


def test_event_id_hour_floor_absorbs_feed_skew():
    ko1 = dt.datetime(2026, 7, 5, 17, 0, tzinfo=dt.timezone.utc)
    ko2 = dt.datetime(2026, 7, 5, 17, 45, tzinfo=dt.timezone.utc)
    assert ids.event_id("Spain", "Austria", ko1) == ids.event_id("Spain", "Austria", ko2)


def test_event_id_requires_tz():
    with pytest.raises(ValueError):
        ids.event_id("Spain", "Austria", dt.datetime(2026, 7, 5, 17, 0))


def test_market_id_distinguishes_settlement():
    ko = dt.datetime(2026, 7, 5, 17, 0, tzinfo=dt.timezone.utc)
    e = ids.event_id("Spain", "Austria", ko)
    m90 = ids.market_id(e, "1x2", settlement=ids.S_90MIN)
    metp = ids.market_id(e, "advance", settlement=ids.S_ETPENS)
    assert m90 != metp and "90min" in m90 and "et-pens" in metp


def test_parse_event_id_roundtrip():
    ko = dt.datetime(2026, 7, 5, 17, 0, tzinfo=dt.timezone.utc)
    parsed = ids.parse_event_id(ids.event_id("Mexico", "South Africa", ko))
    assert parsed["home_slug"] == "mexico"
    assert parsed["kickoff_hour_utc"].startswith("2026-07-05T17")


# ------------------------------------------------------------ storage ----

def test_raw_roundtrip_and_meta(tmp_storage):
    snap = st.write_raw("testsrc", "/things", {"a": [1, 2]},
                        params={"x": 1, "apiKey": "SECRET"}, status=200,
                        headers={"x-requests-remaining": "97"})
    assert st.read_raw(snap) == {"a": [1, 2]}
    meta = st.raw_meta(snap)
    assert meta["params"]["apiKey"] == "***", "secrets must be redacted"
    assert meta["status"] == 200
    assert meta["headers"]["x-requests-remaining"] == "97"
    assert st.latest_raw("testsrc", "/things", {"x": 1, "apiKey": "other"}) == snap


def test_latest_raw_none_when_never_pulled(tmp_storage):
    assert st.latest_raw("testsrc", "/never") is None


def test_dataset_lineage_catalog(tmp_storage):
    df = pl.DataFrame({"k": [1, 2], "v": ["a", "b"]})
    st.save_dataset(df, "silver", "unit_ds", inputs=["raw/x"], notebook="t")
    back = st.load_dataset("silver", "unit_ds")
    assert back.equals(df)
    cat = st.catalog()
    row = cat.filter(pl.col("dataset") == "unit_ds").to_dicts()[0]
    assert row["rows"] == 2 and row["layer"] == "silver"
    assert json.loads(row["inputs"]) == ["raw/x"]


def test_load_missing_dataset_says_which_notebook(tmp_storage):
    with pytest.raises(FileNotFoundError, match="notebook"):
        st.load_dataset("gold", "not_built")


def test_profile_frame_nulls_and_dupes():
    df = pl.DataFrame({"a": [1, 1, None], "b": ["x", "x", "y"]})
    prof = st.profile_frame(df, "t", unique_keys=["a", "b"])
    assert prof.attrs["rows"] == 3
    assert prof.attrs["duplicate_keys"] == 1
    assert prof.loc[prof.column == "a", "null_rate"].iloc[0] == pytest.approx(
        1 / 3, abs=1e-3)  # profile rounds to 4dp


# ------------------------------------------------------------- config ----

def test_params_yaml_roundtrip(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("min_edge_net: 0.03\nwindow_hours: [48, 24, 0]\n")
    p = cfg.load_params(y)
    assert p.min_edge_net == 0.03
    assert p.window_hours == (48, 24, 0)


def test_params_unknown_key_raises(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("no_such_param: 1\n")
    with pytest.raises(KeyError, match="no_such_param"):
        cfg.load_params(y)


def test_example_yaml_stays_in_sync(tmp_path):
    out = cfg.write_example_yaml(tmp_path / "ex.yaml")
    p = cfg.load_params(out)          # loading the example must reproduce defaults
    assert p == cfg.Params()


# ------------------------------------------------------------ odds api ----

def test_cost_formulas_match_documented_rules():
    assert oa.ENDPOINTS["sports"].cost({}) == 0
    assert oa.ENDPOINTS["events"].cost({}) == 0
    assert oa.ENDPOINTS["odds"].cost({"regions": "uk,eu", "markets": "h2h,totals"}) == 4
    assert oa.ENDPOINTS["event_odds"].cost({"regions": "uk", "markets": "btts"}) == 1
    assert oa.ENDPOINTS["scores"].cost({}) == 1
    assert oa.ENDPOINTS["scores"].cost({"daysFrom": 2}) == 2
    assert oa.ENDPOINTS["historical_odds"].cost(
        {"regions": "uk", "markets": "h2h"}) == 10
    assert oa.ENDPOINTS["historical_event_odds"].cost(
        {"regions": "uk,us", "markets": "h2h,totals,btts"}) == 60


def test_guard_blocks_over_budget(tmp_storage, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key-not-real")
    guard = oa.QuotaGuard(max_credits=0)
    with pytest.raises(oa.SkippedCall, match="budget"):
        oa.fetch("odds", guard, sport_key="soccer_x", regions="uk", markets="h2h")
    assert guard.calls[-1]["mode"] == "skip"


def test_guard_dry_run_estimates_without_network(tmp_storage, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key-not-real")
    guard = oa.QuotaGuard(max_credits=100, dry_run=True)
    with pytest.raises(oa.SkippedCall, match="dry-run"):
        oa.fetch("historical_odds", guard, sport_key="soccer_x",
                 regions="uk", markets="h2h", date="2026-06-20T00:00:00Z")
    assert guard.calls[-1]["est_credits"] == 10
    assert guard.spent_estimated == 0


def test_offline_without_cache_is_explicit(tmp_storage):
    guard = oa.QuotaGuard(max_credits=100)
    with pytest.raises(oa.SkippedCall, match="offline"):
        oa.fetch("events", guard, offline=True, sport_key="soccer_x")
    assert "no cached snapshot" in guard.calls[-1]["reason"]


def test_offline_serves_cached_snapshot(tmp_storage):
    st.write_raw("theoddsapi", "/sports/soccer_x/events", [{"id": "e1"}],
                 params={"sport_key": "soccer_x"}, status=200)
    guard = oa.QuotaGuard(max_credits=100)
    payload, snap, meta = oa.fetch("events", guard, offline=True,
                                   sport_key="soccer_x")
    assert payload == [{"id": "e1"}]
    assert guard.calls[-1]["mode"] == "cache"
