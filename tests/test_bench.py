"""Tests for the benchmark harness (wca.bench)."""
import json
import os
import sqlite3

import pytest

from wca.bench import metrics as M
from wca.bench.marketmap import canonical_market
from wca.bench.report import build_report
from wca.bench.sources import (
    latest_per_fixture,
    load_predictions,
    lookup_result,
    norm_team,
)


# --------------------------- marketmap ---------------------------

@pytest.mark.parametrize("raw,fam", [
    ("Match Odds", "1x2"),
    ("Full-time result", "1x2"),
    ("h2h", "1x2"),
    ("MATCH", "1x2"),
    ("pm_moneyline", "1x2"),
    ("Correct Score", "correct_score"),
    ("Both Teams To Score", "btts"),
    ("Total Cards", "cards"),
    ("Total Corners", "corners"),
    ("Total Goals", "totals"),
    ("bet_builder_acca", "acca_betbuilder"),
    ("Acca 2UP (boosted)", "acca_betbuilder"),
    ("player_shots_on_target", "shots_on_target"),
    ("First Goal Scorer", "goalscorer"),
    ("Asian Handicap", "handicap"),
    ("outright_golden_boot", "outright"),
    ("Will Japan reach the Round of 16 at the 2026 FIFA World Cup?", "advancement"),
    ("", "other"),
    ("polymarket", "other"),
])
def test_canonical_market(raw, fam):
    assert canonical_market(raw) == fam


# --------------------------- metrics ---------------------------

def test_brier_and_logloss():
    probs = {"home": 0.5, "draw": 0.3, "away": 0.2}
    # outcome home: (0.5-1)^2 + 0.3^2 + 0.2^2
    assert M.brier_1x2(probs, "H") == pytest.approx(0.25 + 0.09 + 0.04)
    assert M.log_loss_1x2(probs, "H") == pytest.approx(-__import__("math").log(0.5))
    assert M.brier_1x2(probs, "X") is None


def test_wilson_interval():
    p, lo, hi = M.wilson(5, 10)
    assert p == 0.5
    assert 0.0 < lo < 0.5 < hi < 1.0
    assert M.wilson(0, 0) == (0.0, 0.0, 0.0)


def test_reliability_bins_and_ece():
    # perfectly calibrated: prob 0.0 never hits, prob 1.0 always hits
    pairs = [(0.05, 0)] * 20 + [(0.95, 1)] * 20
    bins = M.reliability_bins(pairs, n_bins=5)
    first = next(b for b in bins if b["count"] and b["bin_lo"] == 0.0)
    last = next(b for b in bins if b["count"] and b["bin_hi"] == 1.0)
    assert first["freq_pos"] == 0.0
    assert last["freq_pos"] == 1.0
    assert M.ece(pairs, n_bins=10) == pytest.approx(0.05, abs=1e-9)


def test_trimmed_mean_drops_outliers():
    xs = [0.0] * 9 + [100.0]
    assert M.trimmed_mean(xs, frac=0.1) == pytest.approx(0.0)
    assert M.mean(xs) == pytest.approx(10.0)


# --------------------------- sources ---------------------------

def test_norm_team_aliases():
    assert norm_team("USA") == "united states"
    assert norm_team("Bosnia & Herzegovina") == "bosnia and herzegovina"


def test_lookup_result_date_shift():
    results = {("england", "panama", "2026-06-30"): (3, 0, "H")}
    assert lookup_result(results, "England", "Panama", "2026-06-30")[2] == "H"
    # kickoff rolled to the next UTC day -> still found within +/-1
    assert lookup_result(results, "England", "Panama", "2026-06-29")[2] == "H"
    assert lookup_result(results, "England", "Panama", "2026-07-05") is None


# --------------------------- end-to-end ---------------------------

def _make_db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE bets (id INTEGER PRIMARY KEY, ts_utc TEXT,
        match_id TEXT, match_desc TEXT, market TEXT, selection TEXT,
        platform TEXT, decimal_odds REAL, stake REAL, model_prob REAL,
        market_prob_devig REAL, ev REAL, kelly_fraction REAL, status TEXT,
        settled_pl REAL, closing_odds REAL, clv REAL, notes TEXT)""")
    con.executemany(
        "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
        "platform, decimal_odds, stake, model_prob, status, settled_pl, clv) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-06-13T00:00:00", "m1", "A vs B", "Match Odds", "home",
             "betfair", 2.0, 10.0, 0.55, "won", 10.0, 0.05),
            ("2026-06-13T00:00:00", "m1", "A vs B", "Correct Score", "2-1",
             "smarkets", 9.0, 5.0, 0.12, "lost", -5.0, None),
        ],
    )
    con.execute("""CREATE TABLE odds_snapshots (ts_utc TEXT, source TEXT,
        match_id TEXT, market TEXT, selection TEXT, decimal_odds REAL, raw TEXT)""")
    raw = json.dumps({"home_team": "Aland", "away_team": "Bland",
                      "commence_time": "2026-06-13T19:00:00+00:00"})
    rows = []
    for ts, oh, od, oa in [
        ("2026-06-13T10:00:00+00:00", 2.10, 3.4, 3.6),   # earlier
        ("2026-06-13T18:00:00+00:00", 2.00, 3.5, 3.8),   # closing (<= kickoff)
        ("2026-06-13T20:00:00+00:00", 1.50, 4.0, 6.0),   # after kickoff (ignored)
    ]:
        rows += [
            (ts, "book", "m1", "h2h", "Aland", oh, raw),
            (ts, "book", "m1", "h2h", "Draw", od, raw),
            (ts, "book", "m1", "h2h", "Bland", oa, raw),
        ]
    con.executemany("INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def _make_jsonl(path):
    recs = [
        {"fixture": "Aland vs Bland", "generated": "2026-06-13T09:00:00",
         "kickoff": "2026-06-13 19:00:00+00:00", "match_id": "m1",
         "model": {"home": 0.50, "draw": 0.27, "away": 0.23},
         "market": {"home": 0.45, "draw": 0.28, "away": 0.27}},
    ]
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _make_results(path):
    with open(path, "w") as fh:
        fh.write("date,home_team,away_team,home_score,away_score,tournament,city,country,neutral\n")
        fh.write("2026-06-13,Aland,Bland,2,0,FIFA World Cup,X,Y,TRUE\n")
        fh.write("2026-06-14,Other,Team,NA,NA,FIFA World Cup,X,Y,TRUE\n")


def test_end_to_end_report(tmp_path):
    db = str(tmp_path / "wca.db")
    jsonl = str(tmp_path / "preds.jsonl")
    results = str(tmp_path / "results.csv")
    _make_db(db)
    _make_jsonl(jsonl)
    _make_results(results)

    rep = build_report(db_path=db, archive_dir=str(tmp_path / "noarchive"),
                       jsonl_path=jsonl, results_csv=results,
                       generated_at="2026-06-27T00:00:00Z")

    # calibration: 1 fixture joined, model argmax = home = realized -> hit
    cal = rep["calibration_1x2"]
    assert cal["n"] == 1
    assert cal["hits"] == 1

    # CLV: closing snapshot is the 18:00 one (<=19:00 kickoff), not the 20:00.
    clv = rep["walk_forward_clv"]
    assert clv["n_fixtures_matched"] == 1
    # devig of (2.00, 3.5, 3.8): home implied .5 normalised
    raw_sum = 1/2.0 + 1/3.5 + 1/3.8
    p_close_home = (1/2.0) / raw_sum
    home_clv = p_close_home / 0.50 - 1.0
    legs = clv["clv_mean"]
    assert legs is not None
    # home leg CLV present and matches hand calc within tolerance via bucket means
    assert clv["n_legs"] == 3

    # ledger: correct_score should be the loss-making family
    led = rep["ledger"]
    assert led["n_total"] == 2
    fams = led["by_market"]
    assert "correct_score" in fams and fams["correct_score"]["pl"] == -5.0
    assert "1x2" in fams and fams["1x2"]["pl"] == 10.0


def test_load_predictions_and_latest_dedup(tmp_path):
    jsonl = str(tmp_path / "p.jsonl")
    with open(jsonl, "w") as fh:
        fh.write(json.dumps({"fixture": "A vs B", "generated": "2026-06-13T01:00:00",
                             "kickoff": "2026-06-13 19:00:00+00:00", "match_id": "x1",
                             "model": {"home": .4, "draw": .3, "away": .3}}) + "\n")
        fh.write(json.dumps({"fixture": "A vs B", "generated": "2026-06-13T05:00:00",
                             "kickoff": "2026-06-13 19:00:00+00:00", "match_id": "x2",
                             "model": {"home": .5, "draw": .25, "away": .25}}) + "\n")
    preds = load_predictions(archive_dir=str(tmp_path / "none"), jsonl_path=jsonl)
    assert len(preds) == 2
    latest = latest_per_fixture(preds)
    assert len(latest) == 1
    # keeps the most recent build (p_home 0.5)
    assert float(latest.iloc[0]["p_home"]) == 0.5
