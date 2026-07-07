"""Tests for the SHADOW-ONLY advancement edge desk (scripts/wca_edge_desk.py).

All tests are offline: the four input feeds come from JSON fixtures in
tests/fixtures/edge_desk/ (happy path) plus in-memory mutations for the edge
cases (missing PM price, projected tie, truncated-market caveat, stale feed,
negative edge + hot orderflow, longshot cash rule).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "edge_desk")
# Injected clock ~1h after the fixture stamps → every freshness check passes.
FRESH_NOW = "2026-07-07T10:00:00Z"
# Injected clock 25h later → every source (orderflow max 24h) is stale.
STALE_NOW = "2026-07-08T10:00:00Z"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wca_edge_desk",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_edge_desk.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


def _fixture(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def _feeds():
    return {
        "advancement": _fixture("advancement_data"),
        "bet_recs": _fixture("bet_recs"),
        "pm_ideas": _fixture("pm_ideas"),
        "orderflow": _fixture("orderflow"),
    }


def _build(feeds=None, generated=FRESH_NOW):
    f = feeds or _feeds()
    return MOD.build_feed(f["advancement"], f["bet_recs"], f["pm_ideas"],
                          f["orderflow"], generated=generated)


def _row(feed, team, stage):
    matches = [r for r in feed["rows"] if r["team"] == team and r["stage"] == stage]
    assert len(matches) == 1, "expected exactly one %s/%s row" % (team, stage)
    return matches[0]


# ---------------------------------------------------------------- meta / shape

def test_meta_house_conventions():
    feed = _build()
    meta = feed["meta"]
    assert meta["schema_version"] == 1
    assert meta["generated_at"] == FRESH_NOW
    assert meta["shadow_only"] is True
    assert isinstance(meta["caveats"], list) and meta["caveats"]
    # sources: {path: generated stamp of that feed}
    assert meta["sources"]["site/advancement_data.json"] == "2026-07-07 09:00:00 UTC"
    assert meta["sources"]["site/bet_recs.json"] == "2026-07-07 09:05:00 UTC"
    assert meta["sources"]["site/pm_ideas.json"] == "2026-07-07T08:50:00Z"
    assert meta["sources"]["site/microstructure/orderflow.json"] == "2026-07-07T08:45:00+00:00"
    assert meta["n_rows"] == len(feed["rows"]) == 14
    assert meta["n_candidates"] == 2


def test_shadow_only_verdict_enum_never_trade():
    feed = _build()
    assert set(feed["meta"]["verdict_enum"]) == {"SHADOW_CANDIDATE", "DO_NOT_TRADE"}
    for row in feed["rows"]:
        assert row["verdict"] in feed["meta"]["verdict_enum"]
        assert row["verdict"] != "TRADE"
        # The CLV/history blocker is stamped on every single row.
        assert row["gates"]["clv_history"]["pass"] is False
        assert "CLV" in row["gates"]["clv_history"]["reason"]
    assert feed["clv_history_blocker"]["blocked"] is True


def test_settlement_basis_flagged_everywhere():
    feed = _build()
    assert any("ET+pens" in c or "extra time" in c for c in feed["meta"]["caveats"])
    for row in feed["rows"]:
        assert "ET+pens" in row["market_label"]


def test_decided_legs_excluded_from_universe():
    feed = _build()
    teams = {r["team"] for r in feed["rows"]}
    assert "Eliminatia" not in teams          # all stages decided (0.0 / 1.0)
    assert not [r for r in feed["rows"] if r["team"] == "Morocco" and r["stage"] == "QF"]


# ------------------------------------------------------------------ happy path

def test_happy_path_shadow_candidate_with_traceable_numbers():
    feed = _build()
    row = _row(feed, "Morocco", "SF")
    # Every number matches its named source-feed field verbatim.
    assert row["model_prob"] == 0.55
    assert row["pm_price"] == 0.44
    assert row["edge_adj"] == 0.1026
    assert "advancement_data.teams[Morocco].pm[SF]" in row["pm_price_source"]
    assert row["side"] == "YES" and row["position_prob"] == 0.55
    assert row["bucket"] == "moneyline"
    assert row["bet_rec"]["id"] == "morocco_sf_pm"
    assert row["bet_rec"]["stake"] == 106.51
    for gate in ("freshness", "price_present", "edge_positive", "min_prob_cash"):
        assert row["gates"][gate]["pass"] is True, gate
    assert row["verdict"] == "SHADOW_CANDIDATE"


def test_pm_price_falls_back_to_bet_recs_with_source_label():
    feed = _build()
    row = _row(feed, "Brazil", "QF")           # advancement_data has pm: {}
    assert row["pm_price"] == 0.71
    assert row["edge_adj"] == 0.0573
    assert "bet_recs.advancement_futures[brazil_qf_pm]" in row["pm_price_source"]
    assert row["side"] == "YES"
    assert row["verdict"] == "SHADOW_CANDIDATE"


def test_related_pm_ideas_joined_with_n():
    feed = _build()
    row = _row(feed, "Morocco", "SF")
    assert row["related_pm_ideas"]["n"] == 1
    assert row["related_pm_ideas"]["ideas"][0]["match"] == "Morocco vs France"
    none_row = _row(feed, "Switzerland", "SF")
    assert none_row["related_pm_ideas"]["n"] == 0


# ------------------------------------------------------- edge case: no PM price

def test_missing_pm_price_is_null_with_reason_and_gate_fail():
    feed = _build()
    row = _row(feed, "Brazil", "SF")           # no PM quote anywhere
    assert row["pm_price"] is None
    assert row["edge_adj"] is None
    assert row["side"] is None
    assert "no PM quote" in row["pm_price_reason"]
    assert row["gates"]["price_present"]["pass"] is False
    assert row["verdict"] == "DO_NOT_TRADE"


# ------------------------------------------------------ edge case: projected tie

def test_projected_tie_flagged_from_group_table():
    feed = _build()
    row = _row(feed, "Morocco", "SF")          # level on pts+gd+gf with Tieland
    assert row["group_context"]["projected_tie"] is True
    assert row["group_context"]["tied_with"] == ["Tieland"]
    assert "ambiguous" in row["group_context"]["reason"]
    assert any("projected group-position tie" in c for c in feed["meta"]["caveats"])


def test_no_tie_flag_when_group_positions_clear():
    feed = _build()
    row = _row(feed, "Switzerland", "SF")
    assert row["group_context"]["projected_tie"] is False
    assert row["group_context"]["tied_with"] == []


def test_unknown_group_gives_null_tie_with_reason():
    feeds = _feeds()
    for t in feeds["advancement"]["teams"]:
        if t["team"] == "Colombia":
            t["group"] = "Z"                   # no group-Z table exists
    feed = _build(feeds)
    row = _row(feed, "Colombia", "QF")
    assert row["group_context"]["projected_tie"] is None
    assert "not in advancement_data.groups" in row["group_context"]["reason"]


# --------------------------------------------- edge case: truncated-market caveat

def test_truncated_market_caveat_with_n():
    feed = _build()
    caveat = [c for c in feed["meta"]["caveats"] if "truncated" in c]
    assert caveat and "n=3" in caveat[0]       # 3 truncated markets in fixture


def test_no_truncation_no_caveat():
    feeds = _feeds()
    feeds["orderflow"]["window"]["truncated_markets"] = []
    feed = _build(feeds)
    assert not [c for c in feed["meta"]["caveats"] if "truncated" in c]


# ------------------------------------------------- edge case: stale feed / gates

def test_stale_feeds_fail_freshness_gate_and_all_rows_do_not_trade():
    feed = _build(generated=STALE_NOW)
    assert feed["freshness"]["pass"] is False
    assert all(not c["pass"] for c in feed["freshness"]["checks"])
    assert any("stale" in (c["reason"] or "") for c in feed["freshness"]["checks"])
    assert any("freshness gate FAILED" in c for c in feed["meta"]["caveats"])
    for row in feed["rows"]:
        assert row["gates"]["freshness"]["pass"] is False
        assert row["verdict"] == "DO_NOT_TRADE"
    assert feed["meta"]["n_candidates"] == 0


def test_single_stale_source_fails_whole_freshness_gate():
    feeds = _feeds()
    feeds["advancement"]["meta"]["generated"] = "2026-07-06 09:00:00 UTC"  # 25h old
    feed = _build(feeds)
    assert feed["freshness"]["pass"] is False
    by_src = {c["source"]: c for c in feed["freshness"]["checks"]}
    assert by_src["site/advancement_data.json"]["pass"] is False
    assert by_src["site/bet_recs.json"]["pass"] is True


def test_missing_source_fails_closed_with_reason():
    feeds = _feeds()
    feeds["orderflow"] = None
    feed = MOD.build_feed(feeds["advancement"], feeds["bet_recs"],
                          feeds["pm_ideas"], None, generated=FRESH_NOW,
                          load_errors={"orderflow": "file not found: x"})
    assert feed["freshness"]["pass"] is False
    assert feed["meta"]["sources"]["site/microstructure/orderflow.json"] is None
    assert any("source unavailable" in c for c in feed["meta"]["caveats"])
    row = _row(feed, "Morocco", "SF")
    assert row["orderflow"]["buy_pressure"] is None
    assert row["orderflow"]["hot"] is None
    assert "unavailable" in row["orderflow"]["reason"]


def test_unparseable_stamp_fails_closed():
    feeds = _feeds()
    feeds["bet_recs"]["meta"]["generated"] = "not a timestamp"
    feed = _build(feeds)
    by_src = {c["source"]: c for c in feed["freshness"]["checks"]}
    assert by_src["site/bet_recs.json"]["pass"] is False
    assert "no parseable" in by_src["site/bet_recs.json"]["reason"]


# --------------------------------- edge case: negative edge + hot orderflow

def test_negative_edge_with_hot_orderflow_is_still_do_not_trade():
    feed = _build()
    row = _row(feed, "Colombia", "QF")
    # advancement_qf taker flow is HOT in the fixture (buy_pressure 0.95)...
    assert row["orderflow"]["hot"] is True
    assert row["orderflow"]["buy_pressure"] == 0.95
    assert row["orderflow"]["n_trades"] == 31000          # n stated
    # ...but the fee-adjusted edge is negative, so the verdict cannot budge.
    assert row["edge_adj"] == -0.0054
    assert row["gates"]["edge_positive"]["pass"] is False
    assert "regardless of orderflow" in row["gates"]["edge_positive"]["reason"]
    assert row["verdict"] == "DO_NOT_TRADE"


def test_orderflow_note_says_context_only():
    feed = _build()
    for row in feed["rows"]:
        assert "NEVER overrides" in row["orderflow"]["note"]


# ----------------------------------------------- edge case: longshot cash rule

def test_longshot_positive_edge_blocked_by_likely_pnl_rule():
    feed = _build()
    row = _row(feed, "Switzerland", "SF")      # model 14%, +edge
    assert row["edge_adj"] == 0.0371 and row["edge_adj"] > 0
    assert row["bucket"] == "longshot"
    assert row["gates"]["min_prob_cash"]["pass"] is False
    assert "likely-PnL" in row["gates"]["min_prob_cash"]["reason"]
    assert row["verdict"] == "DO_NOT_TRADE"


def test_no_side_edge_buckets_on_position_probability():
    feed = _build()
    row = _row(feed, "Colombia", "QF")         # model .5982 < pm .605 → NO side
    assert row["side"] == "NO"
    assert row["position_prob"] == 0.4018
    assert "derived" in row["position_prob_source"]
    assert row["bucket"] == "mid"


# ---------------------------------------------------------- ordering / determinism

def test_ordering_follows_selection_rule_buckets_then_edge():
    feed = _build()
    ranks = [{"moneyline": 0, "mid": 1, "longshot": 2}[r["bucket"]]
             for r in feed["rows"]]
    assert ranks == sorted(ranks)
    # Within a bucket: edge_adj descending, null-edge rows last.
    by_bucket = {}
    for r in feed["rows"]:
        by_bucket.setdefault(r["bucket"], []).append(r["edge_adj"])
    for edges in by_bucket.values():
        numbered = [e for e in edges if e is not None]
        assert numbered == sorted(numbered, reverse=True)
        if None in edges:
            assert edges.index(None) >= len(numbered)


def test_bucket_convention_matches_wca_pm_propose():
    assert MOD.prob_bucket(0.55) == "moneyline"
    assert MOD.prob_bucket(0.30) == "mid"
    assert MOD.prob_bucket(0.10) == "longshot"


def test_deterministic_output():
    a = json.dumps(_build(), sort_keys=True)
    b = json.dumps(_build(), sort_keys=True)
    assert a == b


def test_mutation_isolation_between_builds():
    feeds = _feeds()
    before = copy.deepcopy(feeds)
    _build(feeds)
    assert feeds == before                     # build_feed is pure


# ----------------------------------------------------------------- CLI / files

def test_cli_generates_deterministic_file(tmp_path):
    out = tmp_path / "edge_desk.json"
    argv = ["--advancement", os.path.join(FIXTURES, "advancement_data.json"),
            "--bet-recs", os.path.join(FIXTURES, "bet_recs.json"),
            "--pm-ideas", os.path.join(FIXTURES, "pm_ideas.json"),
            "--orderflow", os.path.join(FIXTURES, "orderflow.json"),
            "--out", str(out), "--generated", FRESH_NOW]
    assert MOD.main(argv) == 0
    first = out.read_text()
    payload = json.loads(first)
    assert payload["meta"]["generated_at"] == FRESH_NOW
    assert payload["meta"]["n_rows"] == 14
    assert MOD.main(argv) == 0
    assert out.read_text() == first            # byte-identical rerun


def test_cli_missing_input_file_still_emits_honest_feed(tmp_path):
    out = tmp_path / "edge_desk.json"
    argv = ["--advancement", os.path.join(FIXTURES, "advancement_data.json"),
            "--bet-recs", os.path.join(FIXTURES, "bet_recs.json"),
            "--pm-ideas", os.path.join(FIXTURES, "pm_ideas.json"),
            "--orderflow", str(tmp_path / "nope.json"),
            "--out", str(out), "--generated", FRESH_NOW]
    assert MOD.main(argv) == 0
    payload = json.loads(out.read_text())
    assert payload["freshness"]["pass"] is False
    assert any("source unavailable" in c for c in payload["meta"]["caveats"])


def test_committed_feeds_smoke():
    """The generator must run standalone offline from the committed site feeds."""
    root = os.path.join(os.path.dirname(__file__), "..")
    paths = {name: os.path.join(root, rel)
             for name, rel in MOD.DEFAULT_PATHS.items()}
    if not all(os.path.exists(p) for p in paths.values()):
        import pytest
        pytest.skip("committed site feeds not present in this checkout")
    feed = MOD.generate(paths, generated=FRESH_NOW)
    assert feed["meta"]["schema_version"] == 1
    assert feed["meta"]["shadow_only"] is True
    assert feed["clv_history_blocker"]["blocked"] is True
    for row in feed["rows"]:
        assert row["verdict"] in ("SHADOW_CANDIDATE", "DO_NOT_TRADE")
