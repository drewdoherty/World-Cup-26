"""Tests for the Polymarket top-movers engine (wca.pmmovers)."""

from __future__ import annotations

from wca import pmmovers as pm


def _rec(ts, pm_mid, *, kind="advancement", team="Brazil", stage="QF", slug=None, **extra):
    r = {"ts_utc": ts, "kind": kind, "team": team, "stage": stage, "pm_mid": pm_mid,
         "market_slug": slug if slug is not None else "%s:%s" % (team, stage)}
    r.update(extra)
    return r


# --------------------------------------------------------------------------- categorize


def test_categorize_by_stage():
    assert pm.categorize(_rec("t", 0.5, stage="QF")) == "advancement"
    assert pm.categorize(_rec("t", 0.5, stage="Final")) == "advancement"
    assert pm.categorize(_rec("t", 0.2, stage="win")) == "futures"
    assert pm.categorize(_rec("t", 0.2, stage="group_winner")) == "futures"


def test_categorize_by_kind_overrides_stage():
    assert pm.categorize({"kind": "prop", "stage": "QF", "pm_mid": 0.3}) == "prop"
    assert pm.categorize({"kind": "outright", "pm_mid": 0.3}) == "futures"


def test_categorize_by_title_fallback():
    # The full-universe feed carries no stage — text must drive the bucket.
    assert pm.categorize({"kind": "", "title": "Lionel Messi - Player Props", "pm_mid": 0.3}) == "prop"
    assert pm.categorize({"kind": "", "question": "Exact Score: Brazil 2 - 1 Spain?", "pm_mid": 0.1}) == "prop"
    assert pm.categorize({"kind": "", "title": "Golden Boot Winner", "pm_mid": 0.1}) == "futures"


def test_categorize_match_and_elim_are_advancement():
    # Archive backfill kinds/stages map to advancement.
    assert pm.categorize({"kind": "match", "pm_mid": 0.5}) == "advancement"
    assert pm.categorize({"kind": "x", "stage": "win_match", "pm_mid": 0.5}) == "advancement"
    assert pm.categorize({"kind": "x", "stage": "elim:Final", "pm_mid": 0.5}) == "advancement"


def test_categorize_unknown_is_none():
    assert pm.categorize({"kind": "halftime", "pm_mid": 0.5}) is None


# --------------------------------------------------------------------------- clean_records


def test_clean_records_drops_invalid_and_annotates():
    recs = pm.clean_records([
        _rec("2026-06-28 11:23 UTC", 0.5),
        _rec("2026-06-28 13:23 UTC", 1.5),      # out of range
        _rec("bad-ts", 0.5),                     # unparseable ts
        {"kind": "halftime", "ts_utc": "2026-06-28 11:23 UTC", "pm_mid": 0.5},  # uncategorised
    ])
    assert len(recs) == 1
    assert recs[0]["_cat"] == "advancement"
    assert recs[0]["_dt"] is not None


def test_clean_records_time_sorted():
    recs = pm.clean_records([
        _rec("2026-06-29 07:15 UTC", 0.6),
        _rec("2026-06-28 11:23 UTC", 0.5),
    ])
    assert [r["pm_mid"] for r in recs] == [0.5, 0.6]


# --------------------------------------------------------------------------- movers


def _series():
    # One market over 3 snapshots: 0.50 -> 0.55 (4h) -> 0.62 (latest, +24h overall)
    return pm.clean_records([
        _rec("2026-06-28 12:00 UTC", 0.50),
        _rec("2026-06-29 08:00 UTC", 0.55),
        _rec("2026-06-29 12:00 UTC", 0.62),
    ])


def test_compute_movers_delta_and_window():
    recs = _series()
    windows = [("4h", 4.0), ("all", None)]
    out = pm.compute_movers(recs, category="advancement", windows=windows)
    # 4h window: nearest <= (12:00 - 4h = 08:00) is the 0.55 snap -> +7pp
    assert round(out["4h"][0]["delta_pp"], 1) == 7.0
    # all window: from the earliest 0.50 -> +12pp
    assert round(out["all"][0]["delta_pp"], 1) == 12.0


def test_compute_movers_ranks_by_abs_move():
    recs = pm.clean_records([
        _rec("2026-06-28 12:00 UTC", 0.50, team="Brazil", stage="QF"),
        _rec("2026-06-29 12:00 UTC", 0.20, team="Brazil", stage="QF"),   # -30pp
        _rec("2026-06-28 12:00 UTC", 0.40, team="Spain", stage="QF"),
        _rec("2026-06-29 12:00 UTC", 0.45, team="Spain", stage="QF"),    # +5pp
    ])
    out = pm.compute_movers(recs, category="advancement", windows=[("all", None)])
    movers = out["all"]
    assert movers[0]["team"] == "Brazil"           # bigger |move| first
    assert abs(movers[0]["delta_pp"]) > abs(movers[1]["delta_pp"])


def test_single_snapshot_market_has_no_mover():
    recs = pm.clean_records([_rec("2026-06-28 12:00 UTC", 0.5)])
    out = pm.compute_movers(recs, category="advancement", windows=[("all", None)])
    assert out["all"] == []


def test_window_matrix_orders_for_barh():
    recs = pm.clean_records([
        _rec("2026-06-28 12:00 UTC", 0.50, team="Brazil", stage="QF"),
        _rec("2026-06-29 12:00 UTC", 0.20, team="Brazil", stage="QF"),
        _rec("2026-06-28 12:00 UTC", 0.40, team="Spain", stage="QF"),
        _rec("2026-06-29 12:00 UTC", 0.45, team="Spain", stage="QF"),
    ])
    rows = pm.window_matrix(recs, category="advancement", windows=[("all", None)], top_n=8)
    # ascending max_abs so the biggest mover plots at the top of a barh axis
    assert rows[-1]["max_abs"] >= rows[0]["max_abs"]


# --------------------------------------------------------------------------- summaries / charts


def test_text_summary_marks_empty_category_collecting():
    recs = _series()  # advancement only
    txt = pm.text_summary(recs)
    assert "Player / Exact-Score Props" in txt
    assert "COLLECTING" in txt
    assert "Tournament Futures" in txt


def test_build_charts_returns_three_in_order():
    recs = _series()
    charts = pm.build_charts(recs)
    assert [c["category"] for c in charts] == ["prop", "futures", "advancement"]
    # advancement has data -> a chart; prop has none -> a placeholder, both PNG bytes
    adv = next(c for c in charts if c["category"] == "advancement")
    prop = next(c for c in charts if c["category"] == "prop")
    assert isinstance(adv["png"], (bytes, bytearray)) and adv["png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert isinstance(prop["png"], (bytes, bytearray)) and prop["png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert prop["n_markets"] == 0 and adv["n_markets"] >= 1
    assert "COLLECTING" in prop["caption"]


def test_default_windows_scale_to_span():
    recs = _series()  # ~24h span
    wins = pm.default_windows(recs)
    labels = [l for l, _ in wins]
    assert labels[-1] == "all"
    assert len(wins) == 3
