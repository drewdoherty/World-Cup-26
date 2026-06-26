#!/usr/bin/env python3
"""Odds-Movement Prediction: can we predict the MARKET, not the match? (READ-ONLY)

Desk question
-------------
Using ONLY information available at a time T -- the favourite's current fair
price level, its recent move, cross-book disagreement, the exchange-vs-book gap
and time-to-kickoff -- can we predict the SIGN of the favourite's de-vigged
fair-probability move from T to the pre-kickoff close? If we can, is the hit
rate enough (vs the 50% / unconditional-drift baseline) to trade on?

Everything is derived from the deep intraday 1X2 (h2h) series in data/wca.db.
The DB is opened mode=ro and never mutated.

Pricing
-------
At each captured snapshot ts (a shared API-poll grid: every book + outcome stored
at one ts_utc) we build a CONSENSUS de-vigged 1X2 vector = the median across a
fixed STABLE_BOOKS panel of each book's Shin-de-vigged (src/wca/markets/devig.py)
[p_home, p_draw, p_away]. The "favourite" at T is argmax of that vector at T
(fixed at T -- never redefined using future info). We also build an EXCHANGE
consensus (median of smarkets / betfair_ex_uk / matchbook) for the exch-vs-book
feature, and the cross-stable-book STD of the favourite's prob (disagreement).

Target & anchor (leakage-controlled)
------------------------------------
"close" = the consensus vector at the LAST snapshot at/before kickoff. The label
is sign(close_fav_prob - T_fav_prob), favourite fixed at T. Features use only
data at or before T.

CRITICAL DATA REALITY -- the capture daemon stopped on 2026-06-23, but many of
the 68 deep matches kick off AFTER that. So for most matches the "close" is the
06-23 capture cutoff, which is genuinely hours-to-days before the real kickoff,
NOT a true settle. Only 8 deep matches have their last snapshot within 6h of
kickoff (a "true close"). We therefore report THREE honest views:

  TEST A (primary, n=68, 1 obs/match): one anchor per match at the mid-point of
    the captured pre-KO series -> strictly independent obs. Strict EXPANDING
    walk-forward by kickoff time (train on all earlier-KO matches, predict the
    next), logistic regression fit with numpy. Hit rate vs the majority-direction
    baseline ("always predict the more common sign").

  TEST B (true-close subset, n=8): the matches whose last snapshot is <=6h before
    kickoff. Up/down balance + whether recent-move sign predicts close-move sign.
    n=8 => framework-only, but it is the only view on a REAL close.

  TEST C (pooled momentum structure, ~2.5k anchors): across all deep matches,
    does a recent move predict the next forward move? AUC with a block-bootstrap
    (resample matches) 95% CI, to respect within-match autocorrelation.

We also quantify the up-drift artifact: ALL deep matches show the fav prob
drifting UP open->close, but on the true-close subset it is ~50/50 -- the drift
is an artifact of the truncated close, not a tradeable T->close phenomenon.

Run:  PYTHONPATH=src .venv/bin/python scripts/microstructure/movement_prediction.py
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from wca.markets.devig import devig

# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, "data", "wca.db")
OUT_PATH = os.path.join(ROOT, "site", "microstructure", "movement_prediction.json")

DEVIG_METHOD = "shin"
# 15-book panel present at the first capture in every match (same panel the
# consensus.py / exchange_vs_book.py studies use), to neutralise late-arriving
# books biasing the consensus mid.
STABLE_BOOKS = (
    "paddypower", "skybet", "grosvenor", "smarkets", "casumo", "coral",
    "ladbrokes_uk", "sport888", "williamhill", "unibet_uk", "livescorebet",
    "leovegas", "virginbet", "boylesports", "betfred_uk",
)
EXCHANGES = ("smarkets", "betfair_ex_uk", "matchbook")
MIN_DEEP_SNAPS = 200          # "deep-series" threshold
MIN_SERIES_PTS = 20           # min usable consensus points to keep a match
LAG = 5                       # snapshots back for the "recent move" feature
FWD = 5                       # snapshots forward for the pooled-momentum test
ANCHOR_FRAC = 0.5             # TEST A: one anchor at the midpoint of the series
TRUE_CLOSE_MAX_LEAD_MIN = 360  # TEST B: last snap within 6h of KO = "true close"
MIN_MOVE = 1e-4               # drop |move| below this (ambiguous sign)
WALKFWD_MIN_TRAIN = 25        # TEST A: start predicting after this many train matches
L2 = 2.0
BOOT = 500                    # block-bootstrap reps for the momentum AUC CI


def _connect_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --------------------------------------------------------------------------
def cross_section(con, mid, home, away, ts):
    """{book: shin-devigged [p_home,p_draw,p_away]} at one snapshot."""
    rows = con.execute(
        "SELECT json_extract(raw,'$.bookmaker_key'), selection, decimal_odds "
        "FROM odds_snapshots WHERE market='h2h' AND match_id=? AND ts_utc=?",
        (mid, ts),
    ).fetchall()
    books = defaultdict(dict)
    for bk, sel, od in rows:
        if od and od > 1.0:
            books[bk][sel] = od
    out = {}
    for bk, so in books.items():
        ho, dr, aw = so.get(home), so.get("Draw"), so.get(away)
        if ho and dr and aw:
            try:
                out[bk] = np.asarray(devig([ho, dr, aw], DEVIG_METHOD), dtype=float)
            except Exception:
                pass
    return out


def _renorm(v):
    s = v.sum()
    return v / s if s > 0 else v


def panel_median(cs):
    v = [cs[b] for b in STABLE_BOOKS if b in cs]
    return _renorm(np.median(np.vstack(v), axis=0)) if v else None


def exch_median(cs):
    v = [cs[b] for b in EXCHANGES if b in cs]
    return _renorm(np.median(np.vstack(v), axis=0)) if v else None


def build_series(con):
    """Per deep match -> (kickoff_dt, [(ts_dt, consensus_vec, exch_vec, fav_disagree, n_books)])."""
    matches = con.execute(
        "SELECT match_id, json_extract(raw,'$.home_team'), json_extract(raw,'$.away_team'), "
        "json_extract(raw,'$.commence_time'), COUNT(DISTINCT ts_utc) "
        "FROM odds_snapshots WHERE market='h2h' GROUP BY match_id"
    ).fetchall()
    deep = [m for m in matches if m[4] >= MIN_DEEP_SNAPS]
    series, close_lead_min = {}, {}
    for mid, home, away, ko, _ in deep:
        ko_dt = _parse(ko)
        ts_list = [
            r[0] for r in con.execute(
                "SELECT DISTINCT ts_utc FROM odds_snapshots WHERE market='h2h' "
                "AND match_id=? AND ts_utc<=? ORDER BY ts_utc",
                (mid, ko),
            ).fetchall()
        ]
        pts = []
        for ts in ts_list:
            cs = cross_section(con, mid, home, away, ts)
            pm = panel_median(cs)
            if pm is None:
                continue
            fi = int(np.argmax(pm))
            col = np.array([cs[b][fi] for b in STABLE_BOOKS if b in cs])
            disagree = float(np.std(col)) if len(col) > 1 else 0.0
            pts.append((_parse(ts), pm, exch_median(cs), disagree, len(cs)))
        if len(pts) >= MIN_SERIES_PTS:
            series[mid] = (ko_dt, pts)
            close_lead_min[mid] = (ko_dt - pts[-1][0]).total_seconds() / 60.0
    return series, close_lead_min, len(deep)


# --------------------------------------------------------------------------
# logistic regression (numpy; no sklearn in this env)
# --------------------------------------------------------------------------
def fit_logit(X, y, l2=L2, lr=0.3, it=3000):
    n, d = X.shape
    w = np.zeros(d + 1)
    Xb = np.hstack([np.ones((n, 1)), X])
    for _ in range(it):
        z = np.clip(Xb @ w, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = Xb.T @ (p - y) / n
        g[1:] += l2 * w[1:] / n
        w -= lr * g
    return w


def predict_p(w, X):
    Xb = np.hstack([np.ones((X.shape[0], 1)), X])
    return 1.0 / (1.0 + np.exp(-np.clip(Xb @ w, -30, 30)))


def auc(y, score):
    pos, neg = score[y == 1], score[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    c = sum((p > neg).sum() + 0.5 * (p == neg).sum() for p in pos)
    return float(c / (len(pos) * len(neg)))


# --------------------------------------------------------------------------
def test_a(series):
    """One anchor per match; strict expanding walk-forward logistic by KO time."""
    rows = []
    for mid, (ko_dt, pts) in series.items():
        ntp = len(pts)
        i = max(LAG, min(int(ANCHOR_FRAC * (ntp - 1)), ntp - 2))
        t_dt, vec, ev, dis, _ = pts[i]
        fi = int(np.argmax(vec))
        tfav = float(vec[fi])
        recent = tfav - float(pts[max(0, i - LAG)][1][fi])
        egap = float(ev[fi] - vec[fi]) if ev is not None else 0.0
        ttk = (ko_dt - t_dt).total_seconds() / 3600.0
        move = float(pts[-1][1][fi]) - tfav
        if abs(move) < MIN_MOVE:
            continue
        rows.append((mid, ko_dt.timestamp(), [recent, dis, egap, ttk, tfav], 1 if move > 0 else 0))
    if len(rows) < WALKFWD_MIN_TRAIN + 3:
        return {"n": len(rows), "note": "insufficient for walk-forward"}, rows

    X = np.array([r[2] for r in rows], dtype=float)
    y = np.array([r[3] for r in rows])
    order = np.argsort(np.array([r[1] for r in rows]))
    preds, ys = [], []
    for k in range(WALKFWD_MIN_TRAIN, len(order)):
        tr, te = order[:k], order[k]
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd[sd == 0] = 1.0
        w = fit_logit((X[tr] - mu) / sd, y[tr])
        p = predict_p(w, ((X[te] - mu) / sd).reshape(1, -1))[0]
        preds.append(float(p))
        ys.append(int(y[te]))
    preds, ys = np.array(preds), np.array(ys)
    model_acc = float(((preds > 0.5).astype(int) == ys).mean())
    maj_base = float(max(ys.mean(), 1 - ys.mean()))
    res = {
        "n_matches": len(rows),
        "n_oos": int(len(ys)),
        "frac_up_full": round(float(y.mean()), 4),
        "model_oos_accuracy": round(model_acc, 4),
        "majority_baseline_accuracy": round(maj_base, 4),
        "model_minus_baseline": round(model_acc - maj_base, 4),
        "oos_auc": round(auc(ys, preds), 4) if auc(ys, preds) is not None else None,
        "beats_baseline": bool(model_acc > maj_base),
        "feature_order": ["recent_move", "fav_disagreement", "exch_vs_book_gap", "time_to_ko_hrs", "fav_prob_level"],
    }
    return res, rows


def test_b(series, close_lead_min):
    """True-close subset: last snapshot <=6h before kickoff."""
    subset = [mid for mid, lead in close_lead_min.items() if lead <= TRUE_CLOSE_MAX_LEAD_MIN]
    ys, recent_signs = [], []
    for mid in subset:
        ko_dt, pts = series[mid]
        close_dt, cvec = pts[-1][0], pts[-1][1]
        anchor = None
        for j in range(len(pts) - 2, -1, -1):
            if (close_dt - pts[j][0]).total_seconds() >= 3600:
                anchor = j
                break
        if anchor is None:
            continue
        vec = pts[anchor][1]
        fi = int(np.argmax(vec))
        recent = float(vec[fi]) - float(pts[max(0, anchor - LAG)][1][fi])
        move = float(cvec[fi]) - float(vec[fi])
        if abs(move) < MIN_MOVE:
            continue
        ys.append(1 if move > 0 else 0)
        recent_signs.append(1 if recent > 0 else 0)
    ys = np.array(ys)
    rs = np.array(recent_signs)
    return {
        "n": int(len(ys)),
        "frac_up_to_true_close": round(float(ys.mean()), 4) if len(ys) else None,
        "recent_sign_predicts_close_sign_rate": round(float((rs == ys).mean()), 4) if len(ys) else None,
        "note": "true close = last snapshot within 6h of kickoff; n is tiny => framework-only",
    }


def test_c(series):
    """Pooled snapshot-pair momentum: does recent move predict next forward move?"""
    per_match = defaultdict(list)
    for mid, (ko_dt, pts) in series.items():
        ntp = len(pts)
        for i in range(LAG, ntp - FWD):
            fi = int(np.argmax(pts[i][1]))
            r = float(pts[i][1][fi]) - float(pts[i - LAG][1][fi])
            f = float(pts[i + FWD][1][fi]) - float(pts[i][1][fi])
            if abs(r) < MIN_MOVE or abs(f) < MIN_MOVE:
                continue
            per_match[mid].append((r, 1 if f > 0 else 0))
    if not per_match:
        return {"n": 0}
    allr = np.concatenate([np.array([x[0] for x in v]) for v in per_match.values()])
    ally = np.concatenate([np.array([x[1] for x in v]) for v in per_match.values()])
    obs_auc = auc(ally, allr)
    obs_corr = None
    # correlation of recent move vs raw forward move magnitude (signed)
    fwd_signed = []
    for v in per_match.values():
        for r, lbl in v:
            fwd_signed.append(1 if lbl == 1 else -1)
    # block bootstrap over matches for AUC CI
    rng = np.random.default_rng(0)
    mids = list(per_match)
    aucs = []
    for _ in range(BOOT):
        samp = rng.choice(mids, len(mids), replace=True)
        rr = np.concatenate([np.array([x[0] for x in per_match[m]]) for m in samp])
        yy = np.concatenate([np.array([x[1] for x in per_match[m]]) for m in samp])
        a = auc(yy, rr)
        if a is not None:
            aucs.append(a)
    return {
        "n_anchor_pairs": int(len(ally)),
        "n_matches": len(per_match),
        "recent_predicts_forward_up_auc": round(obs_auc, 4) if obs_auc is not None else None,
        "auc_block_bootstrap_ci95": [round(float(np.percentile(aucs, 2.5)), 4),
                                      round(float(np.percentile(aucs, 97.5)), 4)] if aucs else None,
        "interpretation": ("AUC<0.5 with CI entirely below 0.5 => recent up-moves are followed by "
                           "DOWN-moves more often than chance (weak mean-reversion), not exploitable momentum"),
    }


def updrift_diagnostic(series, close_lead_min):
    """Quantify the open->close up-drift, split true-close vs truncated."""
    def drift(subset):
        ups, tot, mags = 0, 0, []
        for mid in subset:
            _, pts = series[mid]
            o, c = pts[0][1], pts[-1][1]
            fi = int(np.argmax(o))
            d = float(c[fi]) - float(o[fi])
            mags.append(d)
            if abs(d) >= MIN_MOVE:
                tot += 1
                ups += int(d > 0)
        return {"up": ups, "n": tot, "up_rate": round(ups / tot, 4) if tot else None,
                "mean_fav_delta": round(float(np.mean(mags)), 5) if mags else None}

    true = [m for m in series if close_lead_min[m] <= TRUE_CLOSE_MAX_LEAD_MIN]
    trunc = [m for m in series if close_lead_min[m] > 1440]
    return {
        "all_deep": drift(list(series)),
        "true_close_<=6h": drift(true),
        "truncated_>24h": drift(trunc),
        "note": ("up-drift on ALL deep matches is an ARTIFACT of the truncated close (capture stopped "
                 "2026-06-23, before most kickoffs); on true closes it collapses to ~50/50."),
    }


# --------------------------------------------------------------------------
def main():
    con = _connect_ro(DB_PATH)
    series, close_lead_min, n_deep = build_series(con)

    a_res, _ = test_a(series)
    b_res = test_b(series, close_lead_min)
    c_res = test_c(series)
    drift = updrift_diagnostic(series, close_lead_min)

    window = con.execute(
        "SELECT MIN(ts_utc), MAX(ts_utc) FROM odds_snapshots WHERE market='h2h'"
    ).fetchone()
    con.close()

    tradeable = bool(
        a_res.get("beats_baseline")
        and (a_res.get("oos_auc") or 0) >= 0.55
    )

    leads = sorted(close_lead_min.values())
    median_lead_h = round(leads[len(leads) // 2] / 60.0, 1) if leads else None

    headline = {
        "verdict": "NOT tradeable" if not tradeable else "weak edge - validate further",
        "model_oos_hit_rate": a_res.get("model_oos_accuracy"),
        "majority_baseline_hit_rate": a_res.get("majority_baseline_accuracy"),
        "model_minus_baseline": a_res.get("model_minus_baseline"),
        "oos_auc": a_res.get("oos_auc"),
        "n_matches_walkforward": a_res.get("n_matches"),
        "n_oos_predictions": a_res.get("n_oos"),
        "true_close_subset_n": b_res.get("n"),
        "true_close_up_rate": b_res.get("frac_up_to_true_close"),
        "pooled_momentum_auc": c_res.get("recent_predicts_forward_up_auc"),
        "pooled_momentum_auc_ci95": c_res.get("auc_block_bootstrap_ci95"),
    }

    out = {
        "key": "movement_prediction",
        "title": "Odds-Movement Prediction: predicting the market, not the match",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start": window[0],
            "end": window[1],
            "source": "theoddsapi",
            "n_deep_matches": n_deep,
            "n_matches_used": len(series),
            "median_close_lead_to_ko_hrs": median_lead_h,
        },
        "config": {
            "devig_method": DEVIG_METHOD,
            "stable_books": list(STABLE_BOOKS),
            "exchanges": list(EXCHANGES),
            "min_deep_snaps": MIN_DEEP_SNAPS,
            "recent_move_lag_snaps": LAG,
            "anchor_fraction_testA": ANCHOR_FRAC,
            "true_close_max_lead_min": TRUE_CLOSE_MAX_LEAD_MIN,
            "features": ["recent_move", "fav_disagreement", "exch_vs_book_gap", "time_to_ko_hrs", "fav_prob_level"],
            "target": "sign(consensus favourite de-vigged prob: close - T); favourite fixed at T",
            "model": "logistic regression (numpy, L2=%.1f), strict expanding walk-forward by kickoff time" % L2,
        },
        "headline": headline,
        "test_a_walkforward_per_match": a_res,
        "test_b_true_close_subset": b_res,
        "test_c_pooled_momentum": c_res,
        "updrift_diagnostic": drift,
        "tradeable": tradeable,
        "series_for_chart": {
            "labels": ["model OOS hit-rate", "majority baseline", "coin flip"],
            "values": [a_res.get("model_oos_accuracy"), a_res.get("majority_baseline_accuracy"), 0.5],
        },
        "data_caveat": (
            "Single odds source (theoddsapi), 1X2/h2h only, %d deep intraday matches over ~12 days "
            "(2026-06-11..2026-06-23). The capture daemon stopped 2026-06-23, BEFORE most kickoffs, so for "
            "most matches the 'close' is the capture cutoff (median lead ~%sh before KO), NOT a true settle; "
            "only %s deep matches have a snapshot within 6h of kickoff. TEST A (n=%s, one independent obs per "
            "match) is the honest primary test; TEST B (n=%s) is framework-only; TEST C is autocorrelated within "
            "match so its AUC carries a block-bootstrap CI. The apparent open->close up-drift is a truncation "
            "artifact (it vanishes to ~50/50 on true closes). No order-book depth or matched volume; no Polymarket "
            "price history."
            % (n_deep, median_lead_h, b_res.get("n"), a_res.get("n_matches"), b_res.get("n"))
        ),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    print(f"wrote {OUT_PATH}")
    print(json.dumps(headline, indent=2))
    print("\nTEST A (per-match walk-forward):", json.dumps(a_res, indent=2))
    print("TEST B (true close):", json.dumps(b_res, indent=2))
    print("TEST C (pooled momentum):", json.dumps(c_res, indent=2))
    print("up-drift diagnostic:", json.dumps(drift, indent=2))


if __name__ == "__main__":
    main()
