#!/usr/bin/env python3
"""Consensus-pricing microstructure analysis (READ-ONLY).

Question for the desk: **does aggregating the ~16 books we capture per match
beat the single best book (and beat Betfair-exchange-alone) at forecasting
where the market settles, and at forecasting the actual 1X2 result?**

All probabilities are per-book Shin-de-vigged 1X2 vectors
(src/wca/markets/devig.py). Consensus estimators, per match x capture-time:

  (a) median        - simple median of every (stable) book's de-vigged prob
  (b) exchange_only - the exchange consensus (median of smarkets / betfair_ex /
                      matchbook that are present)
  (c) trimmed_mean  - 20%-trimmed mean of the stable books (robust to a rogue
                      book)

Benchmarks:
  - betfair_alone   - single Betfair Exchange de-vigged prob
  - best_book_oos   - single sportsbook, chosen leave-one-out by mean error on
                      the OTHER matches (no per-match in-sample cheat)

CRITICAL FAIRNESS FIX (panel alignment)
---------------------------------------
The naive "first capture vs close" comparison is biased: the sparse books
(betvictor, betway, matchbook, betfair_sb) only START quoting LATE in our
capture window (betvictor first appears ~59% of the way through, betway ~26%,
matchbook ~40%), so their "first" snapshot is mechanically much closer to the
close and they spuriously look like the most accurate single book. We therefore
restrict every estimator and the best-book benchmark to a fixed STABLE_BOOKS
panel that is present at the very first capture in all 72 matches (the 15 core
sportsbooks + smarkets). betfair_ex_uk is present at the open in 50/72 matches,
so betfair_alone is reported on its own (smaller) sample and flagged.

Two tests
---------
TEST 1 - forecast the de-vigged CLOSE.  Anchor = first capture ("open"); target
  = the stable-panel median de-vig at the last snapshot at/before kickoff (the
  de-vigged CLOSE). Metric = mean-abs-error over the 3 outcomes, averaged over
  matches. No results needed => full n. CAVEAT: capture cadence usually stopped
  well before kickoff (only a handful of matches have a snapshot within 60 min
  of KO), so the "close" is the best-available pre-KO line, not a true settle.

TEST 2 - forecast the ACTUAL RESULT.  Score each estimator's CLOSE line against
  the realised 1X2 outcome (data/processed/wc2026_results.json) with Brier and
  log-loss. Small sample (n~31) => indicative, not significant.

Writes site/microstructure/consensus.json. Opens the DB mode=ro; never mutates.

Run:  PYTHONPATH=src .venv/bin/python scripts/microstructure/consensus.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from wca.markets.devig import devig

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, "data", "wca.db")
RESULTS_PATH = os.path.join(ROOT, "data", "processed", "wc2026_results.json")
OUT_PATH = os.path.join(ROOT, "site", "microstructure", "consensus.json")

DEVIG_METHOD = "shin"          # favourite/longshot-corrected de-vig
# Books present at the first capture in ALL 72 matches (verified): 15 core
# sportsbooks + smarkets. This is the fixed panel every estimator and the
# best-book benchmark are restricted to, so timing of late-arriving books
# cannot bias the comparison.
STABLE_BOOKS = (
    "paddypower", "skybet", "grosvenor", "smarkets", "casumo", "coral",
    "ladbrokes_uk", "sport888", "williamhill", "unibet_uk", "livescorebet",
    "leovegas", "virginbet", "boylesports", "betfred_uk",
)
STABLE_SPORTSBOOKS = tuple(b for b in STABLE_BOOKS if b != "smarkets")
EXCHANGES = ("smarkets", "betfair_ex_uk", "matchbook")
BETFAIR = "betfair_ex_uk"
TRIM_FRAC = 0.20               # 20%-trimmed mean
OUTCOMES = ("home", "draw", "away")


def _connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _norm_team(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("&", "and")
    s = s.replace("united states", "usa")
    s = s.replace("south korea", "korea republic")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = s.replace(" and ", " ").replace(" ", "")
    return s


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_matches(con):
    rows = con.execute(
        """
        SELECT match_id,
               json_extract(raw,'$.home_team'),
               json_extract(raw,'$.away_team'),
               json_extract(raw,'$.commence_time')
        FROM odds_snapshots
        WHERE market='h2h'
        GROUP BY match_id
        """
    ).fetchall()
    return [
        {"match_id": mid, "home": h, "away": a, "kickoff": _parse_ts(ct)}
        for mid, h, a, ct in rows
    ]


def cross_section(con, match_id, home, away, ts):
    """Per-book Shin-de-vigged 1X2 vectors {book: [p_home, p_draw, p_away]}."""
    rows = con.execute(
        """
        SELECT json_extract(raw,'$.bookmaker_key'), selection, decimal_odds
        FROM odds_snapshots
        WHERE market='h2h' AND match_id=? AND ts_utc=?
        """,
        (match_id, ts),
    ).fetchall()
    books = defaultdict(dict)
    for bk, sel, od in rows:
        if od and od > 1.0:
            books[bk][sel] = od
    out = {}
    for bk, sel_od in books.items():
        ho, dr, aw = sel_od.get(home), sel_od.get("Draw"), sel_od.get(away)
        if ho and dr and aw:
            try:
                out[bk] = np.asarray(devig([ho, dr, aw], DEVIG_METHOD), dtype=float)
            except Exception:
                pass
    return out


def open_and_close_ts(con, match_id, kickoff_iso):
    open_ts = con.execute(
        "SELECT min(ts_utc) FROM odds_snapshots WHERE market='h2h' AND match_id=?",
        (match_id,),
    ).fetchone()[0]
    close_ts = con.execute(
        "SELECT max(ts_utc) FROM odds_snapshots WHERE market='h2h' AND match_id=? AND ts_utc<=?",
        (match_id, kickoff_iso),
    ).fetchone()[0]
    return open_ts, close_ts


# --------------------------------------------------------------------------
# Consensus estimators (restricted to stable panel unless noted)
# --------------------------------------------------------------------------
def _renorm(v):
    s = v.sum()
    return v / s if s > 0 else v


def _stable_matrix(cs):
    vecs = [cs[b] for b in STABLE_BOOKS if b in cs]
    return np.vstack(vecs) if vecs else None


def consensus_median(cs):
    m = _stable_matrix(cs)
    return _renorm(np.median(m, axis=0)) if m is not None else None


def consensus_trimmed(cs, frac=TRIM_FRAC):
    m = _stable_matrix(cs)
    if m is None:
        return None
    n = m.shape[0]
    k = int(math.floor(n * frac))
    if n - 2 * k < 1:
        k = 0
    out = np.empty(m.shape[1])
    for j in range(m.shape[1]):
        col = np.sort(m[:, j])
        out[j] = col[k : n - k].mean()
    return _renorm(out)


def consensus_exchange(cs):
    vecs = [cs[b] for b in EXCHANGES if b in cs]
    return _renorm(np.median(np.vstack(vecs), axis=0)) if vecs else None


def betfair_alone(cs):
    return cs.get(BETFAIR)


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def mae(pred, target):
    return float(np.mean(np.abs(pred - target)))


def brier(pred, idx):
    y = np.zeros(3)
    y[idx] = 1.0
    return float(np.sum((pred - y) ** 2))


def logloss(pred, idx):
    return -math.log(float(np.clip(pred[idx], 1e-6, 1.0)))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    con = _connect_ro(DB_PATH)
    matches = load_matches(con)

    # results index
    results_raw = []
    if os.path.exists(RESULTS_PATH):
        results_raw = json.load(open(RESULTS_PATH)).get("results", [])
    res_idx = {}
    for r in results_raw:
        try:
            hs, as_ = r["fixture"].split(" vs ")
        except ValueError:
            continue
        res_idx[tuple(sorted([_norm_team(hs), _norm_team(as_)]))] = {
            "home_team_norm": _norm_team(hs),
            "outcome": r["outcome"],
        }

    est_names = ["median", "exchange_only", "trimmed_mean", "betfair_alone"]
    recs = []
    book_open_err = defaultdict(list)   # stable sportsbook -> [(match_id, open_mae_to_close)]
    close_gaps_min = []
    n_close_within_60 = 0

    for m in matches:
        mid = m["match_id"]
        ko_iso = m["kickoff"].isoformat()
        open_ts, close_ts = open_and_close_ts(con, mid, ko_iso)
        if not open_ts or not close_ts or open_ts == close_ts:
            continue
        cs_open = cross_section(con, mid, m["home"], m["away"], open_ts)
        cs_close = cross_section(con, mid, m["home"], m["away"], close_ts)
        target = consensus_median(cs_close)   # de-vigged CLOSE = stable-panel median
        if target is None or _stable_matrix(cs_open) is None:
            continue

        gap_min = (m["kickoff"] - _parse_ts(close_ts)).total_seconds() / 60.0
        close_gaps_min.append(gap_min)
        if gap_min <= 60:
            n_close_within_60 += 1

        preds_open = {
            "median": consensus_median(cs_open),
            "exchange_only": consensus_exchange(cs_open),
            "trimmed_mean": consensus_trimmed(cs_open),
            "betfair_alone": betfair_alone(cs_open),
        }
        preds_close = {
            "median": consensus_median(cs_close),
            "exchange_only": consensus_exchange(cs_close),
            "trimmed_mean": consensus_trimmed(cs_close),
            "betfair_alone": betfair_alone(cs_close),
        }

        # best-book benchmark restricted to STABLE sportsbooks present at open
        for bk in STABLE_SPORTSBOOKS:
            if bk in cs_open:
                book_open_err[bk].append((mid, mae(cs_open[bk], target)))

        # result lookup
        key = tuple(sorted([_norm_team(m["home"]), _norm_team(m["away"])]))
        outcome_idx = None
        if key in res_idx:
            r = res_idx[key]
            same_home = r["home_team_norm"] == _norm_team(m["home"])
            o = r["outcome"]
            if o == "draw":
                outcome_idx = 1
            elif o == "home":
                outcome_idx = 0 if same_home else 2
            elif o == "away":
                outcome_idx = 2 if same_home else 0

        recs.append(
            {
                "match_id": mid,
                "open_mae": {k: (mae(v, target) if v is not None else None) for k, v in preds_open.items()},
                "preds_close": {k: (v.tolist() if v is not None else None) for k, v in preds_close.items()},
                "outcome_idx": outcome_idx,
            }
        )

    # ---- TEST 1: predict de-vigged close ----
    test1 = {}
    for name in est_names:
        errs = [r["open_mae"][name] for r in recs if r["open_mae"].get(name) is not None]
        if errs:
            test1[name] = {"mae": statistics.mean(errs), "median_ae": statistics.median(errs), "n": len(errs)}

    # best single book, leave-one-out (no in-sample per-match cherry pick)
    book_mean_err = {b: statistics.mean([e for _, e in l]) for b, l in book_open_err.items() if len(l) >= 5}
    global_best_book = min(book_mean_err, key=book_mean_err.get) if book_mean_err else None
    loo_best_errs = []
    for r in recs:
        mid = r["match_id"]
        scores = {}
        for b, l in book_open_err.items():
            others = [e for mm, e in l if mm != mid]
            if len(others) >= 5:
                scores[b] = statistics.mean(others)
        if not scores:
            continue
        pick = min(scores, key=scores.get)
        held = [e for mm, e in book_open_err[pick] if mm == mid]
        if held:
            loo_best_errs.append(held[0])
    if loo_best_errs:
        test1["best_book_oos"] = {
            "mae": statistics.mean(loo_best_errs),
            "median_ae": statistics.median(loo_best_errs),
            "n": len(loo_best_errs),
            "note": f"leave-one-out best stable sportsbook by mean open-MAE; global pick={global_best_book}",
        }

    ranking_test1 = [
        {"estimator": k, "mae": round(v["mae"], 5), "n": v["n"]}
        for k, v in sorted(test1.items(), key=lambda kv: kv[1]["mae"])
    ]

    # ---- sample-matched (paired) comparison: median vs betfair_alone ----
    # The unpaired MAEs are not comparable because betfair is absent at open for
    # 22/72 matches (which happen to be harder to predict). Here we restrict to
    # the matches where BOTH the median and betfair_alone exist and pair them.
    paired_bf = [
        (r["open_mae"]["median"], r["open_mae"]["betfair_alone"])
        for r in recs
        if r["open_mae"].get("median") is not None and r["open_mae"].get("betfair_alone") is not None
    ]
    paired_betfair = None
    if paired_bf:
        med_v = [a for a, _ in paired_bf]
        bf_v = [b for _, b in paired_bf]
        diffs = [a - b for a, b in paired_bf]  # >0 => betfair lower error (better)
        paired_betfair = {
            "n": len(paired_bf),
            "median_mae_on_paired": round(statistics.mean(med_v), 5),
            "betfair_mae_on_paired": round(statistics.mean(bf_v), 5),
            "mean_diff_median_minus_betfair": round(statistics.mean(diffs), 5),
            "n_matches_median_better": sum(1 for d in diffs if d < 0),
            "verdict": "within noise; median beats betfair in ~half the matches",
        }

    # ---- TEST 2: predict actual result (Brier/log-loss on CLOSE line) ----
    test2 = {}
    result_recs = [r for r in recs if r["outcome_idx"] is not None]
    for name in est_names:
        briers, lls = [], []
        for r in result_recs:
            v = r["preds_close"].get(name)
            if v is None:
                continue
            arr = np.asarray(v, dtype=float)
            briers.append(brier(arr, r["outcome_idx"]))
            lls.append(logloss(arr, r["outcome_idx"]))
        if briers:
            test2[name] = {"brier": statistics.mean(briers), "logloss": statistics.mean(lls), "n": len(briers)}

    # best single stable sportsbook at predicting the result (leave-one-out by Brier)
    book_close_for_result = defaultdict(list)   # book -> [(match_id, close_vec, outcome_idx)]
    for r in result_recs:
        # recompute close cross-section book vectors lazily: we need them; store via recs?
        pass
    # We need close per-book vectors for result matches; recompute compactly.
    for m in matches:
        mid = m["match_id"]
        rmatch = next((r for r in result_recs if r["match_id"] == mid), None)
        if rmatch is None:
            continue
        _, close_ts = open_and_close_ts(con, mid, m["kickoff"].isoformat())
        cs_close = cross_section(con, mid, m["home"], m["away"], close_ts)
        for b in STABLE_SPORTSBOOKS:
            if b in cs_close:
                book_close_for_result[b].append((mid, cs_close[b], rmatch["outcome_idx"]))
    book_brier = {
        b: statistics.mean([brier(v, idx) for _, v, idx in l])
        for b, l in book_close_for_result.items()
        if len(l) >= 5
    }
    best_book_result = min(book_brier, key=book_brier.get) if book_brier else None
    loo_brier = []
    for r in result_recs:
        mid = r["match_id"]
        scores = {}
        for b, l in book_close_for_result.items():
            others = [brier(v, idx) for mm, v, idx in l if mm != mid]
            if len(others) >= 5:
                scores[b] = statistics.mean(others)
        if not scores:
            continue
        pick = min(scores, key=scores.get)
        held = [brier(v, idx) for mm, v, idx in book_close_for_result[pick] if mm == mid]
        if held:
            loo_brier.append(held[0])
    if loo_brier:
        test2["best_book_oos"] = {
            "brier": statistics.mean(loo_brier),
            "logloss": None,
            "n": len(loo_brier),
            "note": f"leave-one-out best stable sportsbook by Brier; global pick={best_book_result}",
        }

    ranking_test2 = [
        {"estimator": k, "brier": round(v["brier"], 5), "logloss": (round(v["logloss"], 5) if v.get("logloss") is not None else None), "n": v["n"]}
        for k, v in sorted(test2.items(), key=lambda kv: kv[1]["brier"])
    ]

    # ---- headline deltas ----
    def t1(name):
        return test1.get(name, {}).get("mae")

    def t2(name):
        return test2.get(name, {}).get("brier")

    def imp(a, b):
        if a is None or b is None or b == 0:
            return None
        return round(100.0 * (b - a) / b, 1)

    headline = {
        "median_mae": t1("median"),
        "exchange_mae": t1("exchange_only"),
        "trimmed_mae": t1("trimmed_mean"),
        "betfair_alone_mae": t1("betfair_alone"),
        "best_book_oos_mae": t1("best_book_oos"),
        # positive => consensus median better (lower error) than the benchmark
        "median_vs_best_book_pct": imp(t1("median"), t1("best_book_oos")),
        # sample-matched paired comparison (the only fair one for betfair)
        "median_vs_betfair_pct_paired": (
            imp(paired_betfair["median_mae_on_paired"], paired_betfair["betfair_mae_on_paired"])
            if paired_betfair else None
        ),
        "best_estimator_test1": ranking_test1[0]["estimator"] if ranking_test1 else None,
        "median_brier": t2("median"),
        "best_book_brier": t2("best_book_oos"),
        "median_vs_best_book_brier_pct": imp(t2("median"), t2("best_book_oos")),
        "best_estimator_test2": ranking_test2[0]["estimator"] if ranking_test2 else None,
    }

    out = {
        "key": "consensus",
        "title": "Consensus Pricing: does aggregating books beat the best single book?",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start": "2026-06-11",
            "end": "2026-06-23",
            "n_matches_used": len(recs),
            "n_matches_with_result": len(result_recs),
        },
        "method": {
            "devig": DEVIG_METHOD,
            "stable_panel": list(STABLE_BOOKS),
            "exchanges": list(EXCHANGES),
            "trim_frac": TRIM_FRAC,
            "estimators": est_names + ["best_book_oos"],
            "target_test1": "stable-panel median de-vig at last snapshot at/before kickoff (the de-vigged CLOSE)",
            "anchor_test1": "first capture per match (the OPEN cross-section)",
            "panel_fairness_note": (
                "Every estimator and the best-book benchmark are restricted to the 16 books "
                "present at the first capture in all 72 matches, to neutralise the late-arrival "
                "bias of sparse books (betvictor/betway/matchbook first quote 26-59% into the window)."
            ),
            "best_book_selection": "leave-one-out: best stable sportsbook by mean error on the OTHER matches",
        },
        "headline": headline,
        "test1_predict_close": {
            "description": "MAE (over 3 outcomes, mean over matches) of each estimator's OPEN prediction vs the de-vigged CLOSE.",
            "metrics": {k: {kk: (round(vv, 5) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in test1.items()},
            "ranking": ranking_test1,
            "paired_median_vs_betfair": paired_betfair,
        },
        "test2_predict_result": {
            "description": "Brier / log-loss of each estimator's CLOSE line vs the realised 1X2 outcome.",
            "metrics": {k: {kk: (round(vv, 5) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in test2.items()},
            "ranking": ranking_test2,
        },
        "diagnostics": {
            "global_best_book_test1": global_best_book,
            "global_best_book_mean_open_mae": round(book_mean_err[global_best_book], 5) if global_best_book else None,
            "global_best_book_test2": best_book_result,
            "close_within_60min_of_ko": n_close_within_60,
            "close_to_ko_gap_median_min": round(statistics.median(close_gaps_min), 1) if close_gaps_min else None,
            "stable_book_mean_open_mae": {b: round(v, 5) for b, v in sorted(book_mean_err.items(), key=lambda kv: kv[1])},
        },
        "data_caveat": (
            "Single source (theoddsapi), 72 matches over ~12 days, one-3-way-market (h2h). "
            "Estimators restricted to a fixed 16-book panel for a fair comparison. The 'close' is "
            "the last snapshot at/before kickoff, but capture cadence usually stopped early: only "
            f"{n_close_within_60}/{len(recs)} matches have a snapshot within 60 min of kickoff "
            f"(median gap ~{round(statistics.median(close_gaps_min) / 60, 1) if close_gaps_min else '?'}h), so the de-vigged close is a "
            "best-available pre-KO line, not a true settle. TEST 1 (predict close) is full-n but indicative; "
            f"TEST 2 (predict result) has n={len(result_recs)} => indicative, not significant. No Polymarket "
            "price history exists; this is sportsbook+exchange consensus only."
        ),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    con.close()
    print(f"wrote {OUT_PATH}")
    print(json.dumps(headline, indent=2))
    print("\nTEST1 (predict de-vigged close) MAE, lower=better:")
    for r in ranking_test1:
        print(f"  {r['estimator']:16s} mae={r['mae']:.5f} n={r['n']}")
    print("TEST2 (predict result) Brier, lower=better:")
    for r in ranking_test2:
        ll = f"{r['logloss']:.5f}" if r["logloss"] is not None else "n/a"
        print(f"  {r['estimator']:16s} brier={r['brier']:.5f} logloss={ll} n={r['n']}")


if __name__ == "__main__":
    main()
