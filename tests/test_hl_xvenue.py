"""Tests for the HL<->PM cross-venue math + SHADOW-only feed (wca.hl.xvenue
+ scripts/wca_hl_xvenue.py).

Fully offline and deterministic: every book is a raw-response fixture
captured live 2026-07-09 ~18:14 UTC (tests/fixtures/hl_xvenue/) or a
hand-built ladder. The three real-pair regression cases pin the numbers that
were independently re-derived during the recon verification pass:

* champion:Norway — the ONE real fee-surviving cross: dir2 (buy PM Yes 6.0c
  + buy HL No 93.6c) = +0.00231/share, executable 249,600 shares /
  $249,268.57 cost / $331.43 profit, first band 87,086.63 sh @ margin
  0.002308, legs captured 66.7s apart. Status XV_ARB_CANDIDATE.
* qf:Belgium — +0.00029/share, 44.29 shares, $0.0127 profit, but dir2 on a
  QF pair carries the cancellation-toxic tail (collect 0.5 on ~1.0 cost) so
  it is GATED to XV_MISMATCHED_SETTLEMENT, never a candidate.
* champion:France — no fee-surviving direction (-0.00933 / -0.00481):
  XV_WATCH.

Fee math is checked against hand-computed cases and against
wca.advancement.pm_taker_fee (parity is load-bearing: same venue, same fee).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil

import pytest

from wca.hl import client as hl_client
from wca.hl import xvenue

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "hl_xvenue")


def _raw(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _hl(name):
    return hl_client.parse_l2_book(_raw(name))


def _pm(name):
    return xvenue.parse_pm_book(_raw(name))


def _cfg(pair_id):
    return {p["pair_id"]: p for p in xvenue.pair_configs()}[pair_id]


def _norway_row():
    return xvenue.evaluate_pair(
        _cfg("champion:Norway"),
        _hl("l2book_202_side0.json"), _hl("l2book_202_side1.json"),
        _pm("book_win_wc_Norway_Yes.json"), _pm("book_win_wc_Norway_No.json"),
    )


# ---------------------------------------------------------------------------
# Fee math (hand-computed)
# ---------------------------------------------------------------------------

def test_pm_taker_fee_hand_cases():
    # fee = 0.03 * p * (1-p): maximal at 50c, tiny at the extremes — which is
    # exactly why the only fee-surviving cross sits at 6c/93.6c.
    assert xvenue.pm_taker_fee(0.06) == pytest.approx(0.001692, abs=1e-12)
    assert xvenue.pm_taker_fee(0.5) == pytest.approx(0.0075, abs=1e-12)
    assert xvenue.pm_taker_fee(0.936) == pytest.approx(0.00179712, abs=1e-12)
    assert xvenue.pm_taker_fee(0.0) == 0.0
    assert xvenue.pm_taker_fee(1.0) == 0.0
    # out-of-range prices clamp instead of going negative
    assert xvenue.pm_taker_fee(-0.5) == 0.0
    assert xvenue.pm_taker_fee(1.5) == 0.0


def test_pm_fee_parity_with_advancement():
    # Same venue, same fee: the local constant must never drift from the
    # production one in wca.advancement.
    from wca.advancement import PM_TAKER_FEE_COEF as adv_coef
    from wca.advancement import pm_taker_fee as adv_fee

    assert xvenue.PM_TAKER_FEE_COEF == adv_coef
    for p in (0.01, 0.06, 0.25, 0.5, 0.736, 0.936, 0.99):
        assert xvenue.pm_taker_fee(p) == pytest.approx(adv_fee(p), abs=1e-15)


def test_edge_at_best_hand_case():
    # The Norway hit: 1 - (0.936 + 0.060 + fee(0.060)) = +0.002308/share.
    edge = xvenue.edge_at_best(0.936, 0.060, 0.060)
    assert edge == pytest.approx(0.002308, abs=1e-9)
    assert xvenue.edge_at_best(None, 0.5, 0.5) is None


def test_hl_fee_constants_are_explicit():
    # HL trading fee is zero TODAY (docs + fills); the settlement fee is
    # UNVERIFIED and must stay visibly modelled, not silently absorbed.
    assert xvenue.HL_TRADING_FEE_PER_SHARE == 0.0
    assert xvenue.HL_SETTLEMENT_FEE_ASSUMED == 0.0
    assert xvenue.HL_SETTLEMENT_FEE_VERIFIED is False


# ---------------------------------------------------------------------------
# Two-leg walk (hand-computed ladders)
# ---------------------------------------------------------------------------

def test_walk_two_leg_hand_case():
    # L1: 0.5 + 0.4 + fee(0.4)=0.0072 -> 0.9072, fills 50 (PM level caps)
    # L2: 0.5 + 0.45 + fee(0.45)=0.007425 -> 0.957425, fills 50 (HL exhausts)
    res = xvenue.walk_two_leg([(0.5, 100.0)], [(0.4, 50.0), (0.45, 100.0)])
    assert res["shares"] == 100.0
    assert res["cost_usd"] == pytest.approx(93.23, abs=0.005)
    assert res["pm_fees_usd"] == pytest.approx(0.73, abs=0.005)
    assert res["profit_usd"] == pytest.approx(6.76875, abs=1e-3)
    assert len(res["levels"]) == 2
    assert res["levels"][0]["margin_per_share"] == pytest.approx(0.0928, abs=1e-6)
    assert res["levels"][1]["margin_per_share"] == pytest.approx(0.042575, abs=1e-6)


def test_walk_two_leg_stops_at_cost_one():
    # Second PM level pushes pair cost over $1 -> only the first band fills.
    res = xvenue.walk_two_leg([(0.6, 100.0)], [(0.39, 50.0), (0.5, 100.0)])
    assert res["shares"] == 50.0
    assert len(res["levels"]) == 1
    res2 = xvenue.walk_two_leg([(0.6, 100.0)], [(0.41, 50.0)])
    assert res2["shares"] == 0.0 and res2["profit_usd"] == 0.0
    assert xvenue.walk_two_leg([], [(0.4, 50.0)])["shares"] == 0.0


def test_walk_two_leg_norway_regression():
    # Real books, hand-verified in the recon verification pass: PM asks
    # 0.060/0.061/0.062 (87,086.63 / 86,657.93 / 77,857.45 sh) vs the single
    # HL 0.936 level (249,600 sh) -> HL side caps the fill at 249,600.
    hl_no = _hl("l2book_202_side1.json")
    pm_yes = _pm("book_win_wc_Norway_Yes.json")
    res = xvenue.walk_two_leg(hl_no["asks"], pm_yes["asks"])
    assert res["shares"] == 249600.0
    assert res["cost_usd"] == 249268.57
    assert res["profit_usd"] == pytest.approx(331.43, abs=0.01)
    assert res["ret_on_cost_pct"] == pytest.approx(0.133, abs=0.001)
    assert res["levels"][0]["shares"] == 87086.63
    assert res["levels"][0]["margin_per_share"] == pytest.approx(0.002308, abs=1e-6)
    # At WCA scale ($2k pair cost) that margin is ~$4.63 — the research doc's
    # "no arb worth firing today" figure falls straight out of this band.
    per_share_cost = 1.0 - res["levels"][0]["margin_per_share"]
    assert 2000.0 / per_share_cost * res["levels"][0]["margin_per_share"] == pytest.approx(4.63, abs=0.01)


# ---------------------------------------------------------------------------
# Pair evaluation: statuses + settlement gating
# ---------------------------------------------------------------------------

def test_norway_champion_is_arb_candidate():
    row = _norway_row()
    assert row["status"] == xvenue.STATUS_ARB_CANDIDATE
    d2 = row["directions"]["dir2_buy_pm_yes_buy_hl_no"]
    assert d2["edge_per_share_at_best"] == pytest.approx(0.00231, abs=1e-6)
    assert d2["executable"]["shares"] == 249600.0
    assert d2["executable"]["profit_usd"] == pytest.approx(331.43, abs=0.01)
    assert d2["leg_skew_seconds"] == pytest.approx(66.7, abs=0.05)
    assert d2["settlement_tail"]["gated"] is False
    # dir1 is negative here AND would be gated (co-champion tail) anyway.
    d1 = row["directions"]["dir1_buy_hl_yes_buy_pm_no"]
    assert d1["edge_per_share_at_best"] == pytest.approx(-0.00727, abs=1e-6)
    assert d1["settlement_tail"]["gated"] is True
    # Monitor-only wording, never an instruction.
    assert "unproven" in row["status_reason"]


def test_belgium_qf_gated_to_mismatched_settlement():
    row = xvenue.evaluate_pair(
        _cfg("qf:Belgium"),
        _hl("l2book_779_side1.json"), _hl("l2book_779_side0.json"),
        _pm("book_reach_sf_Belgium_Yes.json"), _pm("book_reach_sf_Belgium_No.json"),
    )
    # Positive fee-adjusted edge exists (+0.00029/share, 44.29 sh, $0.0127)
    # but dir2 on a QF pair collects only 0.5 in the cancellation tail:
    # NEVER a candidate.
    assert row["status"] == xvenue.STATUS_MISMATCHED_SETTLEMENT
    d2 = row["directions"]["dir2_buy_pm_yes_buy_hl_no"]
    assert d2["edge_per_share_at_best"] == pytest.approx(0.00029, abs=1e-6)
    assert d2["executable"]["shares"] == 44.29
    assert d2["executable"]["profit_usd"] == pytest.approx(0.0127, abs=1e-4)
    assert d2["settlement_tail"]["gated"] is True
    assert "cancellation_toxic_0_5" in row["status_reason"]


def test_france_champion_is_watch():
    row = xvenue.evaluate_pair(
        _cfg("champion:France"),
        _hl("l2book_189_side0.json"), _hl("l2book_189_side1.json"),
        _pm("book_win_wc_France_Yes.json"), _pm("book_win_wc_France_No.json"),
    )
    assert row["status"] == xvenue.STATUS_WATCH
    assert row["directions"]["dir1_buy_hl_yes_buy_pm_no"]["edge_per_share_at_best"] == pytest.approx(-0.00933, abs=1e-6)
    assert row["directions"]["dir2_buy_pm_yes_buy_hl_no"]["edge_per_share_at_best"] == pytest.approx(-0.00481, abs=1e-6)


def test_gating_depends_on_pair_kind_both_ways():
    # Synthetic books where dir1 (buy HL Yes + buy PM No) is clearly
    # positive: 0.10 + 0.85 + fee(0.85)=0.003825 -> edge +0.046175/share.
    hl_yes = {"time_ms": 1000, "bids": [(0.09, 100.0)], "asks": [(0.10, 100.0)]}
    hl_no = {"time_ms": 1000, "bids": [(0.89, 100.0)], "asks": [(0.91, 100.0)]}
    pm_yes = {"timestamp_ms": 2000, "bids": [(0.12, 100.0)], "asks": [(0.13, 100.0)]}
    pm_no = {"timestamp_ms": 2000, "bids": [(0.84, 100.0)], "asks": [(0.85, 100.0)]}
    # champion pair: dir1 carries the co-champion tail -> GATED.
    row_c = xvenue.evaluate_pair(_cfg("champion:Norway"), hl_yes, hl_no, pm_yes, pm_no)
    assert row_c["status"] == xvenue.STATUS_MISMATCHED_SETTLEMENT
    assert "co_champion" in row_c["status_reason"]
    # qf pair, same books: dir1's cancellation tail is a WINDFALL -> open.
    row_q = xvenue.evaluate_pair(_cfg("qf:Norway"), hl_yes, hl_no, pm_yes, pm_no)
    assert row_q["status"] == xvenue.STATUS_ARB_CANDIDATE
    assert row_q["directions"]["dir1_buy_hl_yes_buy_pm_no"]["settlement_tail"]["gated"] is False


def test_missing_book_fails_closed_to_no_data():
    row = xvenue.evaluate_pair(
        _cfg("champion:Norway"),
        _hl("l2book_202_side0.json"), None,
        _pm("book_win_wc_Norway_Yes.json"), _pm("book_win_wc_Norway_No.json"),
    )
    assert row["status"] == xvenue.STATUS_NO_DATA
    assert "hl_no" in row["status_reason"]
    assert row["directions"] is None


def test_pair_universe_is_the_16_settlement_matched_pairs():
    cfgs = xvenue.pair_configs()
    assert len(cfgs) == 16
    assert sum(1 for c in cfgs if c["kind"] == "champion") == 8
    assert sum(1 for c in cfgs if c["kind"] == "qf") == 8
    # Both legs carry a settlement basis on every pair, and the bases are
    # never the 90-minute 1X2 contract (structural exclusion: PM 1X2 is
    # 3-way/90-min; HL QF is 2-way ET+pens with a 0.5-void tail).
    for c in cfgs:
        assert c["hl_settlement_basis"]
        assert c["pm_settlement_basis"]
        assert "1X2" not in c["pm_settlement_basis"]
        if c["kind"] == "qf":
            assert "ET+pens" in c["pm_settlement_basis"]
            assert "extra time" in c["hl_settlement_basis"]
    # QF side indices follow outcomeMeta sideSpecs order.
    qf_sides = {(c["hl_outcome_id"], c["team"]): c["hl_yes_side"] for c in cfgs if c["kind"] == "qf"}
    assert qf_sides[(761, "France")] == 0 and qf_sides[(761, "Morocco")] == 1
    assert qf_sides[(778, "Norway")] == 0 and qf_sides[(778, "England")] == 1
    assert qf_sides[(779, "Spain")] == 0 and qf_sides[(779, "Belgium")] == 1
    assert qf_sides[(788, "Argentina")] == 0 and qf_sides[(788, "Switzerland")] == 1


def test_pinned_tokens_match_captured_books():
    # The pinned PM token ids must equal the asset_id fields of the captured
    # CLOB books (identity of the fixture data itself).
    assert _pm("book_win_wc_Norway_Yes.json")["asset_id"] == xvenue.PM_WIN_WC["Norway"][1]
    assert _pm("book_win_wc_Norway_No.json")["asset_id"] == xvenue.PM_WIN_WC["Norway"][2]
    assert _pm("book_reach_sf_Belgium_Yes.json")["asset_id"] == xvenue.PM_REACH_SF["Belgium"][1]
    assert _pm("book_reach_sf_Belgium_No.json")["asset_id"] == xvenue.PM_REACH_SF["Belgium"][2]


# ---------------------------------------------------------------------------
# Feed assembly
# ---------------------------------------------------------------------------

def test_build_feed_schema_and_monitor_only():
    row = _norway_row()
    feed = xvenue.build_feed(
        [row], generated_at="2026-07-09T18:16:00Z", n_snapshots=1,
        sources={"mode": "test"},
    )
    assert feed["schema_version"] == 1
    assert feed["generated_at"] == "2026-07-09T18:16:00Z"
    assert feed["monitor_only"] is True
    assert feed["n_snapshots"] == 1
    assert feed["fee_model"]["hl_settlement_fee_verified"] is False
    assert feed["summary"]["by_status"][xvenue.STATUS_ARB_CANDIDATE] == 1
    # settlement basis rides on BOTH legs of every pair row
    for pair in feed["pairs"]:
        assert pair["hl"]["settlement_basis"]
        assert pair["pm"]["settlement_basis"]
    # n=... caveat is mandatory and first
    assert feed["caveats"][0].startswith("n=1 ")
    # Monitor-only: no execution vocabulary anywhere in the serialized feed.
    blob = json.dumps(feed).lower()
    for banned in ("place", "fire", "execute order", "stake"):
        assert banned not in blob
    # statuses restricted to the enum
    assert set(p["status"] for p in feed["pairs"]) <= set(xvenue.ALLOWED_STATUSES)


def test_build_feed_rejects_unknown_status():
    row = _norway_row()
    row["status"] = "PLACE"  # would be an execution label — must be impossible
    with pytest.raises(ValueError):
        xvenue.build_feed([row], generated_at="2026-07-09T18:16:00Z",
                          n_snapshots=1, sources={})


# ---------------------------------------------------------------------------
# Script integration (offline replay; importlib-loaded like other script tests)
# ---------------------------------------------------------------------------

def _load_script():
    spec = importlib.util.spec_from_file_location(
        "wca_hl_xvenue",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_hl_xvenue.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPT = _load_script()


def _norway_replay_dir(tmp_path):
    d = tmp_path / "books"
    d.mkdir()
    for name in (
        "l2book_202_side0.json", "l2book_202_side1.json",
        "book_win_wc_Norway_Yes.json", "book_win_wc_Norway_No.json",
    ):
        shutil.copy(os.path.join(FIXTURES, name), str(d / name))
    return str(d)


def test_script_offline_replay_end_to_end(tmp_path):
    books = _norway_replay_dir(tmp_path)
    out = str(tmp_path / "hl_xvenue.json")
    hist = str(tmp_path / "hist.jsonl")
    rc = SCRIPT.main([
        "--offline-dir", books, "--out", out, "--history", hist,
        "--generated", "2026-07-09T18:16:00Z", "--skip-gamma-verify",
    ])
    assert rc == 0
    feed = json.load(open(out))
    assert feed["schema_version"] == 1
    assert feed["n_snapshots"] == 1
    by_id = {p["pair_id"]: p for p in feed["pairs"]}
    assert len(by_id) == 16
    assert by_id["champion:Norway"]["status"] == xvenue.STATUS_ARB_CANDIDATE
    # every pair with missing dump files failed CLOSED
    others = [p for pid, p in by_id.items() if pid != "champion:Norway"]
    assert all(p["status"] == xvenue.STATUS_NO_DATA for p in others)
    # history-driven n: a second run must say n=2
    rc = SCRIPT.main([
        "--offline-dir", books, "--out", out, "--history", hist,
        "--generated", "2026-07-09T18:17:00Z", "--skip-gamma-verify",
    ])
    assert rc == 0
    assert json.load(open(out))["n_snapshots"] == 2


def test_script_offline_replay_is_deterministic(tmp_path):
    books = _norway_replay_dir(tmp_path)
    feeds = []
    for i in (1, 2):
        out = str(tmp_path / ("out%d.json" % i))
        SCRIPT.main([
            "--offline-dir", books, "--out", out,
            "--history", str(tmp_path / ("h%d.jsonl" % i)),
            "--generated", "2026-07-09T18:16:00Z", "--skip-gamma-verify",
        ])
        feed = json.load(open(out))
        # the history filename is the only run-specific source field
        feed["sources"].pop("history_file", None)
        feeds.append(feed)
    assert feeds[0] == feeds[1]


def test_script_offline_asset_id_mismatch_fails_closed(tmp_path):
    books = _norway_replay_dir(tmp_path)
    # Corrupt the Yes book so it is some OTHER token's book.
    path = os.path.join(books, "book_win_wc_Norway_Yes.json")
    raw = json.load(open(path))
    raw["asset_id"] = "999"
    json.dump(raw, open(path, "w"))
    out = str(tmp_path / "out.json")
    SCRIPT.main([
        "--offline-dir", books, "--out", out, "--history", str(tmp_path / "h.jsonl"),
        "--generated", "2026-07-09T18:16:00Z", "--skip-gamma-verify",
    ])
    feed = json.load(open(out))
    by_id = {p["pair_id"]: p for p in feed["pairs"]}
    assert by_id["champion:Norway"]["status"] == xvenue.STATUS_NO_DATA


def test_script_generated_stamp_hard_error(tmp_path):
    with pytest.raises(SystemExit):
        SCRIPT.main([
            "--offline-dir", str(tmp_path), "--out", str(tmp_path / "o.json"),
            "--history", str(tmp_path / "h.jsonl"), "--generated", "yesterday-ish",
        ])


def test_verify_pm_mapping():
    good = [{
        "slug": "world-cup-winner",
        "markets": [{
            "id": "558951", "groupItemTitle": "Norway",
            "clobTokenIds": json.dumps([xvenue.PM_WIN_WC["Norway"][1],
                                        xvenue.PM_WIN_WC["Norway"][2]]),
        }],
    }]
    expected = {"Norway": {
        "market_id": xvenue.PM_WIN_WC["Norway"][0],
        "token_yes": xvenue.PM_WIN_WC["Norway"][1],
        "token_no": xvenue.PM_WIN_WC["Norway"][2],
    }}
    assert SCRIPT.verify_pm_mapping(good, expected) == {}
    # token drift -> problem (fail-closed upstream)
    bad = json.loads(json.dumps(good))
    bad[0]["markets"][0]["clobTokenIds"] = json.dumps(["1", "2"])
    assert "Norway" in SCRIPT.verify_pm_mapping(bad, expected)
    # missing team -> problem
    assert "Norway" in SCRIPT.verify_pm_mapping(
        [{"markets": []}], expected)
    # empty payload -> every team flagged
    assert SCRIPT.verify_pm_mapping([], expected) == {"Norway": "gamma event payload empty"}
