#!/usr/bin/env python3
"""Synthetic / cross-market consistency analysis (key=synthetic_pricing).

READ-ONLY. Reads odds_snapshots from data/wca.db, never mutates the DB.
Writes site/microstructure/synthetic_pricing.json — a small feed the website
renders.

Question
--------
The 1X2 (h2h), totals (Over/Under) and BTTS (both-teams-to-score) markets on a
single match are NOT three independent things: they are three projections of the
*same* joint distribution over scorelines. A single goal-supply pair
(lambda_home, lambda_away) feeding a Dixon-Coles scoreline grid simultaneously
implies a 1X2 split, an Over/Under split at any line, and a BTTS split. If the
quoted markets cannot all be reproduced by ANY single goal-supply, they are
internally inconsistent — and the gap is, in principle, a tradable synthetic
mispricing.

Method
------
For each match, at the closing snapshot (latest capture at/under kickoff) we:

1. Build consensus de-vigged probabilities across all books quoting each market
   (Shin de-vig for the 3-way h2h; multiplicative for the 2-way totals & btts,
   which is exact on a 2-way book up to the favourite/longshot choice — we use
   multiplicative because the books are tight 2-ways).
2. INVERT h2h + totals to a Dixon-Coles (lambda_home, lambda_away) using the
   repo DixonColes scoreline grid with rho fixed to the fitted tournament value
   (data/dc_params_corrected.json). The h2h home/away log-ratio pins the lambda
   ratio; the Over(line) probability pins the total goal supply. We solve by
   bounded least squares over (log lambda_home, log lambda_away).
3. From that SAME (lambda_home, lambda_away, rho) grid we read off the model's
   implied BTTS — fully out-of-sample to the BTTS quote.
4. The cross-market inconsistency is  btts_market - btts_model  (probability
   points). A large positive gap = the BTTS market prices "both score" RICHER
   than the h2h+totals goal-supply can justify; negative = cheaper.

We also report the inversion fit residual on h2h+totals (how well a single
goal-supply could reproduce those two markets at all) so the BTTS gap is only
trusted where the h2h/totals fit is tight.

Caveats (honest)
----------------
* n = 21 matches with BTTS at the closing snapshot (3 of the 24 tri-market
  matches have no BTTS book at closing). n < 30 => indicative, NOT significant.
* Single odds source (theoddsapi); ~20 UK books; 12-day window 2026-06-11..23.
* rho is fixed to the fitted tournament value, not re-fit per match (per-match
  re-fit is unidentified from 2 markets). BTTS is mildly sensitive to rho; we
  report a rho-sensitivity band.
* No correct-score / Asian-handicap data exists in the DB, so the full joint
  cross-check (e.g. CS-implied vs totals-implied) is FRAMEWORK ONLY here.
* Consensus closing prices already removed vig; a real harvest also needs a
  single book to be off-consensus (we report dispersion as a proxy).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson

# Repo modules (PYTHONPATH=src)
from wca.markets import devig as dv

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(REPO, "data", "wca.db")
DC_PARAMS = os.path.join(REPO, "data", "dc_params_corrected.json")
OUT = os.path.join(REPO, "site", "microstructure", "synthetic_pricing.json")

MAX_GOALS = 10


# --------------------------------------------------------------------------- #
# Dixon-Coles scoreline grid (mirrors wca.models.dixon_coles.score_matrix but
# parameterised directly by lambdas + rho, since we are inverting market prices,
# not team strengths).
# --------------------------------------------------------------------------- #
def dc_matrix(lam_h: float, lam_a: float, rho: float, max_goals: int = MAX_GOALS) -> np.ndarray:
    goals = np.arange(max_goals + 1)
    ph = poisson.pmf(goals, lam_h)
    pa = poisson.pmf(goals, lam_a)
    mat = np.outer(ph, pa)
    # tau correction on the 4 low-score cells (Dixon-Coles 1997 eq 4.4)
    tau = np.ones_like(mat)
    tau[0, 0] = 1.0 - lam_h * lam_a * rho
    tau[0, 1] = 1.0 + lam_h * rho
    tau[1, 0] = 1.0 + lam_a * rho
    tau[1, 1] = 1.0 - rho
    mat = np.clip(mat * tau, 0.0, None)
    s = mat.sum()
    return mat / s if s > 0 else mat


def probs_from_matrix(mat: np.ndarray, line: float) -> Dict[str, float]:
    n = mat.shape[0]
    idx = np.add.outer(np.arange(n), np.arange(n))
    p_home = float(np.tril(mat, -1).sum())
    p_draw = float(np.trace(mat))
    p_away = float(np.triu(mat, 1).sum())
    p_over = float(mat[idx > line].sum())
    p_under = float(mat[idx < line].sum())
    p_h0 = float(mat[0, :].sum())
    p_a0 = float(mat[:, 0].sum())
    p_btts_yes = float(min(max(1.0 - p_h0 - p_a0 + float(mat[0, 0]), 0.0), 1.0))
    return {
        "home": p_home, "draw": p_draw, "away": p_away,
        "over": p_over, "under": p_under,
        "btts_yes": p_btts_yes, "btts_no": 1.0 - p_btts_yes,
    }


def invert_h2h_totals(
    p_home: float, p_away: float, p_over: float, line: float, rho: float
) -> Tuple[float, float, float]:
    """Find (lam_h, lam_a) whose DC grid best matches market p_home, p_away and
    p_over(line). Returns (lam_h, lam_a, rmse) on the 3 matched probabilities."""
    targets = np.array([p_home, p_away, p_over])

    def resid(theta):
        lam_h, lam_a = math.exp(theta[0]), math.exp(theta[1])
        m = dc_matrix(lam_h, lam_a, rho)
        pr = probs_from_matrix(m, line)
        pred = np.array([pr["home"], pr["away"], pr["over"]])
        return float(np.sum((pred - targets) ** 2))

    # init from rough independent-Poisson intuition
    best = None
    for lh0 in (0.8, 1.2, 1.6):
        for la0 in (0.8, 1.2, 1.6):
            r = minimize(resid, x0=[math.log(lh0), math.log(la0)],
                         method="L-BFGS-B",
                         bounds=[(math.log(0.05), math.log(6.0))] * 2,
                         options={"maxiter": 500, "ftol": 1e-12})
            if best is None or r.fun < best.fun:
                best = r
    lam_h, lam_a = math.exp(best.x[0]), math.exp(best.x[1])
    rmse = math.sqrt(best.fun / 3.0)
    return lam_h, lam_a, rmse


# --------------------------------------------------------------------------- #
# Consensus de-vigged market probabilities from the closing snapshot.
# --------------------------------------------------------------------------- #
def consensus_h2h(cur, mid, ts, home, away) -> Optional[Dict[str, float]]:
    rows = cur.execute(
        "SELECT json_extract(raw,'$.bookmaker_key') bk, selection, decimal_odds "
        "FROM odds_snapshots WHERE match_id=? AND market='h2h' AND ts_utc=?",
        (mid, ts)).fetchall()
    by_book: Dict[str, Dict[str, float]] = {}
    for bk, sel, od in rows:
        by_book.setdefault(bk, {})[sel] = od
    fair = []
    for bk, sel in by_book.items():
        if home in sel and away in sel and "Draw" in sel:
            odds = [sel[home], sel["Draw"], sel[away]]
            try:
                p = dv.shin(odds)  # Shin: 3-way favourite/longshot-corrected
            except Exception:
                continue
            fair.append(p)
    if not fair:
        return None
    p = np.mean(np.array(fair), axis=0)
    p = p / p.sum()
    return {"home": float(p[0]), "draw": float(p[1]), "away": float(p[2]), "n_books": len(fair)}


def consensus_two_way(cur, mid, ts, market, yes_label, no_label) -> Optional[Dict[str, float]]:
    rows = cur.execute(
        "SELECT json_extract(raw,'$.bookmaker_key') bk, json_extract(raw,'$.outcome_name') nm, "
        "decimal_odds, json_extract(raw,'$.outcome_point') pt "
        "FROM odds_snapshots WHERE match_id=? AND market=? AND ts_utc=?",
        (mid, market, ts)).fetchall()
    # group by (book, point)
    by: Dict[Tuple[str, object], Dict[str, float]] = {}
    for bk, nm, od, pt in rows:
        by.setdefault((bk, pt), {})[nm] = od
    # choose the line (point) covered by the most books
    line_books: Dict[object, int] = {}
    for (bk, pt), d in by.items():
        if yes_label in d and no_label in d:
            line_books[pt] = line_books.get(pt, 0) + 1
    if not line_books:
        return None
    main_pt = max(line_books, key=line_books.get)
    fair = []
    for (bk, pt), d in by.items():
        if pt != main_pt:
            continue
        if yes_label in d and no_label in d:
            try:
                p = dv.multiplicative([d[yes_label], d[no_label]])
            except Exception:
                continue
            fair.append(p)
    if not fair:
        return None
    arr = np.array(fair)
    p = arr.mean(axis=0)
    p = p / p.sum()
    return {
        "yes": float(p[0]), "no": float(p[1]),
        "yes_std": float(arr[:, 0].std()),
        "line": (float(main_pt) if main_pt is not None else None),
        "n_books": len(fair),
    }


def main() -> None:
    if not os.path.exists(DB):
        print(f"DB not found: {DB}", file=sys.stderr)
        sys.exit(1)
    rho = 0.0
    rho_src = "independent-poisson (rho=0) fallback"
    try:
        with open(DC_PARAMS) as f:
            rho = float(json.load(f).get("rho", 0.0))
        rho_src = f"fitted tournament DC rho={rho:.4f} (data/dc_params_corrected.json)"
    except Exception:
        pass

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()

    tri = [r[0] for r in cur.execute(
        "SELECT match_id FROM odds_snapshots WHERE market='h2h' "
        "INTERSECT SELECT match_id FROM odds_snapshots WHERE market='totals' "
        "INTERSECT SELECT match_id FROM odds_snapshots WHERE market='btts'")]

    results: List[Dict] = []
    window_kos: List[str] = []
    for mid in tri:
        ko = cur.execute(
            "SELECT MAX(json_extract(raw,'$.commence_time')) FROM odds_snapshots WHERE match_id=?",
            (mid,)).fetchone()[0]
        ts = cur.execute(
            "SELECT MAX(ts_utc) FROM odds_snapshots WHERE match_id=? AND ts_utc<=?",
            (mid, ko)).fetchone()[0]
        home = cur.execute(
            "SELECT json_extract(raw,'$.home_team') FROM odds_snapshots WHERE match_id=? LIMIT 1",
            (mid,)).fetchone()[0]
        away = cur.execute(
            "SELECT json_extract(raw,'$.away_team') FROM odds_snapshots WHERE match_id=? LIMIT 1",
            (mid,)).fetchone()[0]
        window_kos.append(ko)

        h2h = consensus_h2h(cur, mid, ts, home, away)
        tot = consensus_two_way(cur, mid, ts, "totals", "Over", "Under")
        btts = consensus_two_way(cur, mid, ts, "btts", "Yes", "No")
        if h2h is None or tot is None or btts is None:
            continue  # need all three to test consistency

        line = tot["line"]
        lam_h, lam_a, fit_rmse = invert_h2h_totals(
            h2h["home"], h2h["away"], tot["yes"], line, rho)
        m = dc_matrix(lam_h, lam_a, rho)
        model = probs_from_matrix(m, line)

        # rho sensitivity: re-read BTTS at rho +/- 0.05 holding lambdas fixed
        btts_rho_lo = probs_from_matrix(dc_matrix(lam_h, lam_a, rho - 0.05), line)["btts_yes"]
        btts_rho_hi = probs_from_matrix(dc_matrix(lam_h, lam_a, rho + 0.05), line)["btts_yes"]
        rho_band = abs(btts_rho_hi - btts_rho_lo)

        btts_gap = btts["yes"] - model["btts_yes"]  # market - model, prob points
        # also the residual draw / over consistency for context
        draw_gap = h2h["draw"] - model["draw"]
        over_gap = tot["yes"] - model["over"]

        results.append({
            "match_id": mid,
            "match": f"{home} v {away}",
            "kickoff": ko,
            "snapshot": ts,
            "totals_line": line,
            "lambda_home": round(lam_h, 3),
            "lambda_away": round(lam_a, 3),
            "lambda_total": round(lam_h + lam_a, 3),
            "fit_rmse_h2h_totals": round(fit_rmse, 4),
            "h2h_market": {k: round(h2h[k], 4) for k in ("home", "draw", "away")},
            "totals_market_over": round(tot["yes"], 4),
            "btts_market_yes": round(btts["yes"], 4),
            "btts_model_yes": round(model["btts_yes"], 4),
            "btts_gap_pts": round(100 * btts_gap, 2),       # market - model
            "draw_resid_pts": round(100 * draw_gap, 2),
            "over_resid_pts": round(100 * over_gap, 2),
            "btts_rho_band_pts": round(100 * rho_band, 2),
            "btts_market_book_std_pts": round(100 * btts["yes_std"], 2),
            "n_books_h2h": h2h["n_books"],
            "n_books_totals": tot["n_books"],
            "n_books_btts": btts["n_books"],
        })

    con.close()

    n = len(results)
    gaps = np.array([r["btts_gap_pts"] for r in results]) if results else np.array([])
    abs_gaps = np.abs(gaps)
    fit_rmses = np.array([r["fit_rmse_h2h_totals"] for r in results]) if results else np.array([])

    # ranking by absolute inconsistency
    ranked = sorted(results, key=lambda r: -abs(r["btts_gap_pts"]))

    summary = {
        "n_matches": n,
        "mean_abs_btts_gap_pts": round(float(abs_gaps.mean()), 2) if n else None,
        "median_abs_btts_gap_pts": round(float(np.median(abs_gaps)), 2) if n else None,
        "max_abs_btts_gap_pts": round(float(abs_gaps.max()), 2) if n else None,
        "mean_signed_btts_gap_pts": round(float(gaps.mean()), 2) if n else None,
        "share_gap_gt_2pts": round(float((abs_gaps > 2.0).mean()), 3) if n else None,
        "share_gap_gt_rho_band": (
            round(float(np.mean([abs(r["btts_gap_pts"]) > r["btts_rho_band_pts"]
                                 for r in results])), 3) if n else None),
        "mean_inversion_fit_rmse_pts": round(float(fit_rmses.mean() * 100), 3) if n else None,
        "median_btts_book_dispersion_pts": (
            round(float(np.median([r["btts_market_book_std_pts"] for r in results])), 2)
            if n else None),
    }

    feed = {
        "key": "synthetic_pricing",
        "title": "Synthetic / cross-market consistency (h2h x totals x BTTS)",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "first_kickoff": min(window_kos) if window_kos else None,
            "last_kickoff": max(window_kos) if window_kos else None,
            "snapshot_basis": "latest capture at/under kickoff (closing consensus)",
        },
        "method": (
            "Invert consensus de-vigged h2h (Shin) + totals (multiplicative) to a "
            "Dixon-Coles (lambda_home, lambda_away) goal supply with rho fixed to the "
            "fitted tournament value, then read the SAME grid's BTTS out-of-sample and "
            "compare to the quoted BTTS. Gap = btts_market - btts_model (prob points)."
        ),
        "rho_used": round(rho, 4),
        "rho_source": rho_src,
        "data_caveat": (
            f"n={n} matches (BTTS present at closing for 21 of 24 tri-market matches). "
            "Single source theoddsapi, ~20 UK books, window 2026-06-11..23. n<30 => "
            "INDICATIVE, not statistically significant. rho fixed, not per-match. No "
            "correct-score / Asian-handicap data exists, so any CS-vs-totals joint "
            "cross-check is FRAMEWORK ONLY, not measured here."
        ),
        "summary": summary,
        "ranking_by_abs_gap": [
            {
                "match": r["match"],
                "btts_gap_pts": r["btts_gap_pts"],
                "btts_market_yes": r["btts_market_yes"],
                "btts_model_yes": r["btts_model_yes"],
                "fit_rmse_h2h_totals_pts": round(r["fit_rmse_h2h_totals"] * 100, 2),
                "rho_band_pts": r["btts_rho_band_pts"],
                "totals_line": r["totals_line"],
                "lambda_total": r["lambda_total"],
            }
            for r in ranked
        ],
        "per_match": results,
        "framework_only_note": (
            "Correct-score and Asian-handicap markets are NOT in data/wca.db. The full "
            "joint-consistency programme (CS grid vs totals vs h2h; AH vs h2h) is "
            "specified but cannot be measured with current capture. Add CS/AH capture "
            "to extend this from a BTTS-vs-(h2h,totals) check to a full grid check."
        ),
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(feed, f, indent=2)
    print(f"WROTE {OUT}")
    print(f"n={n}  mean|gap|={summary['mean_abs_btts_gap_pts']}pts  "
          f"max|gap|={summary['max_abs_btts_gap_pts']}pts  "
          f"mean_fit_rmse={summary['mean_inversion_fit_rmse_pts']}pts")
    for r in ranked[:5]:
        print(f"  {r['match']:32s} gap={r['btts_gap_pts']:+.2f}pts "
              f"(mkt={r['btts_market_yes']:.3f} model={r['btts_model_yes']:.3f}) "
              f"fit_rmse={r['fit_rmse_h2h_totals']*100:.2f}pts rho_band={r['btts_rho_band_pts']:.2f}")


if __name__ == "__main__":
    main()
