"""Tests for Module D — the repeatable-edge VERDICT battery (``wca.rigor``).

Every test is offline and deterministic.  Synthetic ledgers are written to a
temp DB (never the production ``wca.db``); model books are synthesised as
plain dicts/JSONL.  The battery's contract under test:

* Wilson intervals match known textbook values and handle k=0,k=n,n=0,n=1.
* A synthetic book with mean CLV=0.03 greens **only** once n_eff>=25 *and* an
  outcome-anchored gate (skill/calibration) also passes.
* A null book (CLV centred on 0, no skill) never greens — it stays grey/red.
* A best-price-only artifact (positive CLV, model_prob == market_prob, i.e.
  zero predictive skill) must **not** green: it tops out at PROMISING (amber).
* A futures-only book is permanently INSUFFICIENT_SAMPLE.
* The real-data builder runs end-to-end and emits the exact schema.
"""

from __future__ import annotations

import json
import os
import sqlite3
import math
from pathlib import Path

import numpy as np
import pytest

from wca.rigor import clv as C
from wca.rigor import skill as S
from wca.rigor import stability as ST
from wca.rigor import verdict as V
from wca.rigor.build import build_rigor

_REPO = Path(__file__).resolve().parents[1]
_GEN = "2026-06-25T00:00:00Z"


# ---------------------------------------------------------------------------
# Synthetic ledger / book builders.
# ---------------------------------------------------------------------------

_BETS_DDL = """
CREATE TABLE bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT, match_id TEXT, match_desc TEXT, market TEXT, selection TEXT,
    platform TEXT, decimal_odds REAL, stake REAL, model_prob REAL,
    market_prob_devig REAL, ev REAL, kelly_fraction REAL, status TEXT,
    settled_pl REAL, closing_odds REAL, clv REAL, notes TEXT,
    settled_ts TEXT, account TEXT, source TEXT
)
"""


def _write_bets_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute(_BETS_DDL)
    cols = ("ts_utc", "match_id", "match_desc", "market", "selection",
            "platform", "decimal_odds", "stake", "model_prob",
            "market_prob_devig", "status", "settled_pl", "closing_odds",
            "clv", "source")
    for r in rows:
        con.execute(
            "INSERT INTO bets (%s) VALUES (%s)"
            % (",".join(cols), ",".join("?" * len(cols))),
            tuple(r.get(c) for c in cols),
        )
    con.commit()
    con.close()


def _single_bet(i, clv, won, *, platform="bet365", stake=10.0, odds=2.0):
    return {
        "ts_utc": "2026-06-%02dT12:00:00Z" % (i % 28 + 1),
        "match_id": "FX_%03d" % i,           # one fixture per bet -> 1 cluster each
        "match_desc": "Home%03d vs Away%03d" % (i, i),
        "market": "h2h", "selection": "Home%03d" % i,
        "platform": platform, "decimal_odds": odds, "stake": stake,
        "model_prob": 0.55, "market_prob_devig": 0.5,
        "status": "won" if won else "lost",
        "settled_pl": stake * (odds - 1) if won else -stake,
        "closing_odds": odds / (1 + clv), "clv": clv, "source": "model",
    }


def _write_model_jsonl(path, fixtures):
    """fixtures: list of (home, away, model_triple, market_triple)."""
    with open(path, "w") as fh:
        for i, (home, away, model, market) in enumerate(fixtures):
            rec = {
                "fixture": "%s vs %s" % (home, away),
                "generated": "2026-06-13T00:%02d:00" % (i % 60),
                "kickoff": "2026-06-%02d 19:00:00+00:00" % (i % 28 + 1),
                "match_id": "m%03d" % i,
                "model": model, "market": market,
                "elo": model, "dc": model,
            }
            fh.write(json.dumps(rec) + "\n")


def _write_results(path, fixtures_outcomes):
    """fixtures_outcomes: list of (home, away, outcome)."""
    results = []
    for i, (home, away, outcome) in enumerate(fixtures_outcomes):
        results.append({
            "date": "2026-06-%02d" % (i % 28 + 1),
            "fixture": "%s vs %s" % (home, away),
            "kickoff_utc": "2026-06-%02dT19:00:00Z" % (i % 28 + 1),
            "score": "1-0" if outcome == "home" else ("0-1" if outcome == "away" else "1-1"),
            "outcome": outcome,
        })
    with open(path, "w") as fh:
        json.dump({"results": results}, fh)


# Distinct team names so canonicalisation keeps fixtures separate.
_TEAMS = [
    "Argentina", "Brazil", "France", "Spain", "Germany", "England", "Portugal",
    "Netherlands", "Italy", "Croatia", "Belgium", "Uruguay", "Colombia",
    "Mexico", "Japan", "Senegal", "Morocco", "Denmark", "Switzerland",
    "Poland", "Serbia", "Ghana", "Ecuador", "Tunisia", "Wales", "Iran",
    "Canada", "Qatar", "Cameroon", "Australia", "Nigeria", "Egypt",
    "Sweden", "Norway", "Austria", "Scotland", "Greece", "Turkey", "Chile",
    "Peru", "Paraguay", "Bolivia", "Panama", "Jamaica", "Algeria", "Mali",
    "Ukraine", "Czechia", "Slovakia", "Romania", "Hungary", "Finland",
    "Ireland", "Iceland", "Albania", "Kosovo", "Georgia", "Armenia",
    "Slovenia", "Bulgaria",
]


def _skill_fixtures(n_fixtures, *, skilled, seed=7):
    """Build (jsonl_fixtures, results) for a model that is/ isn't skilled.

    When ``skilled`` the model nudges toward the realized outcome (genuine
    information); otherwise model == market (zero skill, best-price artifact).
    """
    rng = np.random.default_rng(seed)
    jf, ro = [], []
    legs = ("home", "draw", "away")
    for f in range(n_fixtures):
        home, away = _TEAMS[(2 * f) % len(_TEAMS)], _TEAMS[(2 * f + 1) % len(_TEAMS)]
        # draw a true triple, sample the realized leg from it.
        raw = rng.uniform(0.2, 0.6, 3)
        true = raw / raw.sum()
        outcome_idx = int(rng.choice(3, p=true))
        outcome = legs[outcome_idx]
        market = {legs[j]: float(true[j]) for j in range(3)}
        # market is noisy around truth.
        mkt = np.array([true[j] + rng.normal(0, 0.04) for j in range(3)])
        mkt = np.clip(mkt, 0.02, 0.96); mkt = mkt / mkt.sum()
        market = {legs[j]: float(mkt[j]) for j in range(3)}
        if skilled:
            mdl = np.array(true)
            mdl[outcome_idx] += 0.12  # genuine pull toward the realized leg
            mdl = np.clip(mdl, 0.02, 0.96); mdl = mdl / mdl.sum()
            model = {legs[j]: float(mdl[j]) for j in range(3)}
        else:
            model = dict(market)  # zero skill: identical to market.
        jf.append((home, away, model, market))
        ro.append((home, away, outcome))
    return jf, ro


# ---------------------------------------------------------------------------
# Wilson interval correctness.
# ---------------------------------------------------------------------------


def test_wilson_known_values():
    # k=5, n=10 -> textbook Wilson 95% [0.2366, 0.7634].
    p, lo, hi = C.wilson(5, 10)
    assert p == 0.5
    assert lo == pytest.approx(0.2366, abs=1e-3)
    assert hi == pytest.approx(0.7634, abs=1e-3)


def test_wilson_edges():
    # n=0: no information, widest band, centre NaN.
    p, lo, hi = C.wilson(0, 0)
    assert math.isnan(p) and lo == 0.0 and hi == 1.0
    # k=0: lower bound pinned at 0 but interval non-degenerate.
    _, lo0, hi0 = C.wilson(0, 10)
    assert lo0 == 0.0 and hi0 > 0.0
    # k=n: upper bound pinned at 1 but interval non-degenerate.
    _, lon, hin = C.wilson(10, 10)
    assert hin == 1.0 and lon < 1.0
    # n=1: defined centre, full-width band.
    _, lo1, hi1 = C.wilson(1, 1)
    assert 0.0 <= lo1 < hi1 <= 1.0
    # wilson_lower None at n=0.
    assert C.wilson_lower(0, 0) is None


# ---------------------------------------------------------------------------
# n_eff cluster deflation.
# ---------------------------------------------------------------------------


def test_n_eff_single_cluster_is_one():
    # All observations in one cluster (e.g. a futures market) -> n_eff <= 1.
    vals = [0.03] * 20
    clusters = ["only"] * 20
    assert C.n_eff_clusters(vals, clusters) == 1.0


def test_n_eff_independent_clusters_near_n():
    # One observation per cluster -> n_eff close to n.
    rng = np.random.default_rng(0)
    vals = list(rng.normal(0.03, 0.01, 40))
    clusters = [f"c{i}" for i in range(40)]
    ne = C.n_eff_clusters(vals, clusters)
    assert ne > 25  # not deflated below the floor for independent draws.
    assert ne <= 40


# ---------------------------------------------------------------------------
# Gate G2 boundary grows with sample (not a fixed 1.65).
# ---------------------------------------------------------------------------


def test_sequential_threshold_above_fixed_and_growing():
    z_small = C.sequential_z_threshold(25)
    z_big = C.sequential_z_threshold(4000)
    assert z_small > 1.96       # always-valid boundary starts above the naive z.
    assert z_big > z_small      # and creeps upward with more looks.


# ---------------------------------------------------------------------------
# G4 skill: real skill passes, best-price artifact does not.
# ---------------------------------------------------------------------------


def test_g4_skilled_passes_artifact_fails():
    # Genuine skill.
    jf, ro = _skill_fixtures(40, skilled=True)
    mps, kps, outs, fids = _legs_from(jf, ro)
    g4 = S.skill_vs_market(mps, kps, outs, fids)
    assert g4["pass"] is True and g4["logloss_diff"] > 0

    # Best-price artifact: model == market -> exactly zero differential.
    jf2, ro2 = _skill_fixtures(40, skilled=False)
    mps2, kps2, outs2, fids2 = _legs_from(jf2, ro2)
    g4b = S.skill_vs_market(mps2, kps2, outs2, fids2)
    assert g4b["pass"] is False
    assert g4b["logloss_diff"] == pytest.approx(0.0, abs=1e-9)


def _legs_from(jsonl_fixtures, results):
    """Flatten synthetic fixtures into the per-leg arrays G4 consumes."""
    legs = ("home", "draw", "away")
    outcome_by_fx = {(h, a): o for (h, a, o) in results}
    mps, kps, outs, fids = [], [], [], []
    for (h, a, model, market) in jsonl_fixtures:
        o = outcome_by_fx[(h, a)]
        for leg in legs:
            mps.append(model[leg]); kps.append(market[leg])
            outs.append(1.0 if leg == o else 0.0); fids.append("%s|%s" % (h, a))
    return mps, kps, outs, fids


# ---------------------------------------------------------------------------
# End-to-end: synthetic GREEN only past the floor + an anchored gate.
# ---------------------------------------------------------------------------


def _run(tmp_path, *, clv_rows, jsonl_fixtures, results, dev_db=None):
    db = tmp_path / "ledger.db"
    _write_bets_db(db, clv_rows)
    jl = tmp_path / "model.jsonl"
    rj = tmp_path / "results.json"
    _write_model_jsonl(jl, jsonl_fixtures)
    _write_results(rj, results)
    return build_rigor(
        wca_db=str(db), jsonl_path=str(jl), results_path=str(rj),
        dev_db=dev_db, generated=_GEN,
    )


def test_synthetic_green_requires_floor_and_anchor(tmp_path):
    rng = np.random.default_rng(3)
    # 40 single bets, mean CLV 0.03, low variance, ~57% win (matches CLV edge).
    clv_rows = []
    for i in range(40):
        clv = float(round(0.03 + rng.normal(0, 0.008), 5))
        clv_rows.append(_single_bet(i, clv, won=(rng.random() < 0.57)))
    # Genuinely skilled model book (40 fixtures) -> G4 passes.
    jf, ro = _skill_fixtures(40, skilled=True)
    out = _run(tmp_path, clv_rows=clv_rows, jsonl_fixtures=jf, results=ro)

    assert out["meta"]["n_eff"] >= 25                # past the CLV floor.
    gates = {g["id"]: g["pass"] for g in out["gates"]}
    assert gates["G1"] and gates["G2"] and gates["G3"]   # CLV gates pass.
    assert gates["G4"] is True                            # outcome-anchored.
    assert gates["G6"] is True                            # stable.
    assert out["verdict"]["level"] == "EDGE_LIKELY"
    assert out["verdict"]["color"] == "green"


def test_below_floor_stays_grey(tmp_path):
    # Same +0.03 edge but only 12 bets -> n_eff < 25 -> never green.
    rng = np.random.default_rng(3)
    clv_rows = [_single_bet(i, float(round(0.03 + rng.normal(0, 0.008), 5)),
                            won=(rng.random() < 0.6)) for i in range(12)]
    jf, ro = _skill_fixtures(12, skilled=True)
    out = _run(tmp_path, clv_rows=clv_rows, jsonl_fixtures=jf, results=ro)
    assert out["meta"]["n_eff"] < 25
    assert out["verdict"]["level"] == "INSUFFICIENT_SAMPLE"
    assert out["verdict"]["color"] == "grey"


def test_best_price_artifact_does_not_green(tmp_path):
    # Positive CLV (price selection) BUT model == market (zero skill).
    rng = np.random.default_rng(5)
    clv_rows = [_single_bet(i, float(round(0.03 + rng.normal(0, 0.008), 5)),
                            won=(rng.random() < 0.55)) for i in range(40)]
    jf, ro = _skill_fixtures(40, skilled=False)  # model == market.
    out = _run(tmp_path, clv_rows=clv_rows, jsonl_fixtures=jf, results=ro)
    assert out["meta"]["n_eff"] >= 25
    gates = {g["id"]: g["pass"] for g in out["gates"]}
    # CLV gates may pass, but no skill gate does -> not green.
    assert gates["G4"] is not True
    assert out["verdict"]["color"] != "green"
    assert out["verdict"]["level"] in ("PROMISING", "INCONCLUSIVE")


def test_null_book_not_green(tmp_path):
    # CLV centred on 0, no skill -> grey or red, never green.
    rng = np.random.default_rng(9)
    clv_rows = [_single_bet(i, float(round(rng.normal(0, 0.02), 5)),
                            won=(rng.random() < 0.5)) for i in range(40)]
    jf, ro = _skill_fixtures(40, skilled=False)
    out = _run(tmp_path, clv_rows=clv_rows, jsonl_fixtures=jf, results=ro)
    assert out["verdict"]["color"] in ("grey", "amber", "red")
    assert out["verdict"]["level"] != "EDGE_LIKELY"


def test_futures_only_permanent_insufficient(tmp_path):
    # A book of only futures (outrights) -> permanently insufficient.
    rows = []
    for i in range(30):
        rows.append({
            "ts_utc": "2026-06-01T12:00:00Z",
            "match_id": "WC2026_GOLDEN_BOOT",
            "match_desc": "FIFA World Cup 2026 Golden Boot",
            "market": "outright_golden_boot",
            "selection": "Player %d" % i,
            "platform": "betfair_sportsbook", "decimal_odds": 8.0,
            "stake": 5.0, "model_prob": None, "market_prob_devig": None,
            "status": "lost", "settled_pl": -5.0,
            "closing_odds": None, "clv": None, "source": "offer",
        })
    out = _run(tmp_path, clv_rows=rows, jsonl_fixtures=[], results=[])
    assert out["verdict"]["level"] == "INSUFFICIENT_SAMPLE"
    assert out["verdict"]["color"] == "grey"
    assert "futures" in out["verdict"]["reason"].lower()


# ---------------------------------------------------------------------------
# Schema + real-data run.
# ---------------------------------------------------------------------------


def test_schema_keys_complete(tmp_path):
    rng = np.random.default_rng(1)
    clv_rows = [_single_bet(i, float(round(0.01 + rng.normal(0, 0.02), 5)),
                            won=(rng.random() < 0.5)) for i in range(20)]
    jf, ro = _skill_fixtures(20, skilled=False)
    out = _run(tmp_path, clv_rows=clv_rows, jsonl_fixtures=jf, results=ro)

    assert set(out.keys()) == {
        "meta", "verdict", "gates", "clv_block", "skill_block",
        "profit_block", "stability_block", "segments", "samples_to_sig",
    }
    assert set(out["meta"]) == {"generated", "n", "n_eff", "stage"}
    assert set(out["verdict"]) == {"level", "label", "reason", "color"}
    assert [g["id"] for g in out["gates"]] == ["G0", "G1", "G2", "G3", "G4",
                                               "G5", "G6", "G7"]
    for g in out["gates"]:
        assert set(g.keys()) == {"id", "name", "stat", "value", "threshold",
                                 "pass", "note"}
    assert set(out["clv_block"]) == {"mean", "lower", "beat_rate",
                                     "placebo_null", "n_eff"}
    assert set(out["skill_block"]) == {"logloss_diff", "brier_skill",
                                       "calibration_slope"}
    assert set(out["profit_block"]) == {"roi", "roi_lo", "sharpe", "n"}
    assert set(out["stability_block"]) == {"break_detected", "oos_is_ratio"}
    assert set(out["samples_to_sig"]) == {"roi_n", "clv_n", "current_n_eff"}
    assert out["samples_to_sig"]["roi_n"] == 3860
    assert out["samples_to_sig"]["clv_n"] == 25
    # Must be JSON-serialisable (no NaN / numpy scalars leak through).
    json.dumps(out, allow_nan=False)


@pytest.mark.skipif(
    not (_REPO / "data" / "wca.db").exists(),
    reason="production ledger not present",
)
def test_real_data_runs_and_is_honest():
    out = build_rigor(
        wca_db=str(_REPO / "data" / "wca.db"),
        jsonl_path=str(_REPO / "data" / "model_predictions_log.jsonl"),
        results_path=str(_REPO / "data" / "processed" / "wc2026_results.json"),
        dev_db=str(_REPO / "data" / "dev.db"),
        generated=_GEN,
    )
    json.dumps(out, allow_nan=False)
    # At the current N the honest verdict cannot be a confident green.
    assert out["verdict"]["level"] in (
        "INSUFFICIENT_SAMPLE", "NO_EDGE", "INCONCLUSIVE", "PROMISING",
    )
    assert out["meta"]["n"] > 0
    assert out["meta"]["n_eff"] < 25  # binding power floor not yet met.
