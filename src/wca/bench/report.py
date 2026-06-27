"""Benchmark report: score the generated card + commands vs actual outcomes.

Three independent scorings, each with explicit sample sizes and caveats:

1. **Model 1X2 calibration & discrimination** — latest pre-kickoff model
   prediction per fixture vs the realized result: reliability table, ECE,
   Brier (model vs build-time market), argmax hit-rate with Wilson CI.
2. **Walk-forward CLV** — each model leg's fair prob vs the consensus closing
   fair prob (``clv = p_close/p_model - 1``), bucketed by build-time edge.
   Reproduces the "≥2% edge flags are CLV-negative" attribution check.
3. **Realized ledger** — ROI / hit-rate / mean-CLV on placed bets, broken down
   by canonical market family, venue, and edge bucket.

All numbers are computed from a *copy* of the ledger DB; the dev-box ledger is
known to fork from the canonical mini ledger, so treat absolute figures as
illustrative of the harness, not as the production scorecard.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from wca.bench import metrics as M
from wca.bench.sources import (
    latest_per_fixture,
    load_bets,
    load_closing_lines,
    load_predictions,
    load_results,
    lookup_result,
    norm_team,
)

EDGE_BUCKETS = [(-1.0, -0.02), (-0.02, 0.0), (0.0, 0.02), (0.02, 0.05),
                (0.05, 0.10), (0.10, 1.0)]


# ---------------------------------------------------------------------------
# 1. Model 1X2 calibration & discrimination
# ---------------------------------------------------------------------------

def _calibration_section(preds: pd.DataFrame, results: Dict) -> Dict:
    rows = []
    for _, r in preds.iterrows():
        res = lookup_result(results, r["home"], r["away"], r["kickoff_date"])
        if res is None:
            continue
        _, _, outcome = res
        model = {"home": r["p_home"], "draw": r["p_draw"], "away": r["p_away"]}
        if any(model[k] is None or pd.isna(model[k]) for k in model):
            continue
        market = {"home": r["m_home"], "draw": r["m_draw"], "away": r["m_away"]}
        has_market = all(market[k] is not None and not pd.isna(market[k]) for k in market)
        argmax = max(model, key=model.get)
        win = {"H": "home", "D": "draw", "A": "away"}[outcome]
        rows.append({
            "fixture": r["fixture"], "outcome": outcome, "win": win,
            "model": model, "market": market if has_market else None,
            "brier_model": M.brier_1x2(model, outcome),
            "brier_market": M.brier_1x2(market, outcome) if has_market else None,
            "ll_model": M.log_loss_1x2(model, outcome),
            "hit": 1 if argmax == win else 0,
            "argmax": argmax,
        })
    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "no predicted fixtures joined to a realized result yet"}

    # reliability across all legs (3 per fixture): predicted prob vs leg-hit
    leg_pairs = []
    for row in rows:
        for k in ("home", "draw", "away"):
            leg_pairs.append((row["model"][k], 1 if row["win"] == k else 0))

    hits = sum(r["hit"] for r in rows)
    p_hat, lo, hi = M.wilson(hits, n)
    bm = M.mean([r["brier_model"] for r in rows])
    market_rows = [r for r in rows if r["brier_market"] is not None]
    bmk = M.mean([r["brier_market"] for r in market_rows]) if market_rows else None
    skill = (1.0 - bm / bmk) if (bmk and bmk > 0) else None

    return {
        "n": n,
        "hit_rate": p_hat, "hit_ci": [lo, hi], "hits": hits,
        "brier_model": bm,
        "brier_market": bmk,
        "brier_skill_vs_market": skill,
        "log_loss_model": M.mean([r["ll_model"] for r in rows]),
        "ece_legs": M.ece(leg_pairs, n_bins=10),
        "reliability_legs": M.reliability_bins(leg_pairs, n_bins=5),
        "n_legs": len(leg_pairs),
    }


# ---------------------------------------------------------------------------
# 2. Walk-forward CLV (model book vs consensus close)
# ---------------------------------------------------------------------------

def _clv_section(preds: pd.DataFrame, closing: Dict) -> Dict:
    by_match = closing["by_match"]
    team_index = closing["team_index"]
    leg_rows = []
    matched = 0
    for _, r in preds.iterrows():
        mid = r["match_id"]
        close = by_match.get(mid)
        if close is None:
            tkey = (norm_team(r["home"]), norm_team(r["away"]), r["kickoff_date"])
            alt = team_index.get(tkey)
            close = by_match.get(alt) if alt else None
        if close is None:
            continue
        matched += 1
        for k in ("home", "draw", "away"):
            p_model = r[f"p_{k}"]
            p_market = r[f"m_{k}"]
            p_close = close.get(k)
            if p_model is None or pd.isna(p_model) or p_model <= 0 or p_close is None:
                continue
            clv = p_close / p_model - 1.0
            edge = (p_model - p_market) if (p_market is not None and not pd.isna(p_market)) else None
            leg_rows.append({"leg": k, "clv": clv, "edge": edge})
    if not leg_rows:
        return {"n_fixtures": 0, "note": "no model legs joined to a closing line"}

    all_clv = [x["clv"] for x in leg_rows]
    out = {
        "n_fixtures_matched": matched,
        "n_legs": len(leg_rows),
        "clv_mean": M.mean(all_clv),
        "clv_median": M.median(all_clv),
        "clv_trimmed": M.trimmed_mean(all_clv),
        "beat_close_rate": M.wilson(sum(1 for c in all_clv if c > 0), len(all_clv))[0],
        "by_edge_bucket": {},
        "flagged_ge_2pct": {},
    }
    # by edge bucket
    for lo, hi in EDGE_BUCKETS:
        b = [x["clv"] for x in leg_rows if x["edge"] is not None and lo <= x["edge"] < hi]
        key = f"[{lo:+.2f},{hi:+.2f})"
        out["by_edge_bucket"][key] = {
            "n": len(b), "clv_mean": M.mean(b),
            "beat_rate": M.wilson(sum(1 for c in b if c > 0), len(b))[0] if b else None,
        }
    flagged = [x["clv"] for x in leg_rows if x["edge"] is not None and x["edge"] >= 0.02]
    k = sum(1 for c in flagged if c > 0)
    p, clo, chi = M.wilson(k, len(flagged)) if flagged else (None, None, None)
    out["flagged_ge_2pct"] = {
        "n": len(flagged), "clv_mean": M.mean(flagged),
        "beat_rate": p, "beat_ci": [clo, chi],
    }
    return out


# ---------------------------------------------------------------------------
# 3. Realized ledger (placed bets)
# ---------------------------------------------------------------------------

def _ledger_section(bets: pd.DataFrame) -> Dict:
    if bets.empty:
        return {"n": 0, "note": "no bets in ledger"}
    settled = bets[bets["status"].isin(["won", "lost", "void", "cashed"])].copy()
    decided = settled[settled["status"].isin(["won", "lost", "cashed"])].copy()

    def _grp(df: pd.DataFrame, col: str) -> Dict:
        out = {}
        for key, g in df.groupby(col):
            staked = g["stake"].fillna(0).sum()
            pl = g["settled_pl"].fillna(0).sum()
            n = len(g)
            wins = int((g["status"] == "won").sum())
            clvs = [c for c in g.get("clv", pd.Series(dtype=float)).tolist()
                    if c is not None and not pd.isna(c)]
            out[str(key)] = {
                "n": n, "staked": round(float(staked), 2),
                "pl": round(float(pl), 2), "roi": M.roi(staked, pl),
                "win_rate": (wins / n) if n else None,
                "mean_clv": M.mean(clvs), "n_clv": len(clvs),
            }
        return out

    overall_staked = decided["stake"].fillna(0).sum()
    overall_pl = decided["settled_pl"].fillna(0).sum()
    clvs = [c for c in settled.get("clv", pd.Series(dtype=float)).tolist()
            if c is not None and not pd.isna(c)]
    return {
        "n_total": len(bets),
        "n_settled": len(settled),
        "n_decided": len(decided),
        "overall": {
            "staked": round(float(overall_staked), 2),
            "pl": round(float(overall_pl), 2),
            "roi": M.roi(overall_staked, overall_pl),
            "mean_clv": M.mean(clvs), "n_clv": len(clvs),
        },
        "by_market": _grp(decided, "market_family"),
        "by_venue": _grp(decided, "venue"),
    }


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def build_report(db_path: str = "data/wca.db",
                 archive_dir: str = "data/archive",
                 jsonl_path: str = "data/model_predictions_log.jsonl",
                 results_csv: str = "data/raw/martj42_cleaned.csv",
                 generated_at: Optional[str] = None) -> Dict:
    preds_raw = load_predictions(archive_dir, jsonl_path)
    pred_source = preds_raw.attrs.get("source", "none") if not preds_raw.empty else "none"
    preds = latest_per_fixture(preds_raw)
    results = load_results(results_csv)
    closing = load_closing_lines(db_path)
    bets = load_bets(db_path, archive_dir)

    report = {
        "generated_at": generated_at,
        "sources": {
            "predictions": pred_source,
            "n_prediction_builds": int(len(preds_raw)),
            "n_fixtures_latest": int(len(preds)),
            "n_results_loaded": len(results),
            "n_closing_lines": len(closing["by_match"]),
            "bets_source": bets.attrs.get("source", "none") if not bets.empty else "none",
        },
        "calibration_1x2": _calibration_section(preds, results) if not preds.empty else {"n": 0},
        "walk_forward_clv": _clv_section(preds, closing) if not preds.empty else {"n_fixtures": 0},
        "ledger": _ledger_section(bets),
    }
    return report


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _fmt(x, pct=False, places=3):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    if pct:
        return f"{x * 100:+.1f}%"
    return f"{x:.{places}f}"


def render_markdown(report: Dict) -> str:
    s = report["sources"]
    cal = report["calibration_1x2"]
    clv = report["walk_forward_clv"]
    led = report["ledger"]
    L: List[str] = []
    L.append("# Benchmark report — generated card & commands vs actual outcomes")
    if report.get("generated_at"):
        L.append(f"_Generated: {report['generated_at']}_")
    L.append("")
    L.append("## Sources & coverage")
    L.append(f"- Predictions: **{s['predictions']}** "
             f"({s['n_prediction_builds']} builds → {s['n_fixtures_latest']} latest-per-fixture)")
    L.append(f"- Results loaded: **{s['n_results_loaded']}** | "
             f"Closing lines: **{s['n_closing_lines']}** | Bets: **{s['bets_source']}**")
    L.append("")

    # Calibration
    L.append("## 1. Model 1X2 calibration & discrimination")
    if cal.get("n", 0) == 0:
        L.append(f"_{cal.get('note', 'no data')}_")
    else:
        L.append(f"- Fixtures scored: **{cal['n']}** ({cal['n_legs']} legs)")
        L.append(f"- Argmax hit-rate: **{_fmt(cal['hit_rate'], pct=True)}** "
                 f"(95% CI {_fmt(cal['hit_ci'][0], pct=True)}…{_fmt(cal['hit_ci'][1], pct=True)}, "
                 f"{cal['hits']}/{cal['n']})")
        L.append(f"- Brier — model **{_fmt(cal['brier_model'])}** vs market "
                 f"**{_fmt(cal['brier_market'])}** | skill vs market "
                 f"**{_fmt(cal['brier_skill_vs_market'], pct=True)}**")
        L.append(f"- Log-loss (model): **{_fmt(cal['log_loss_model'])}** | "
                 f"ECE (legs, 10-bin): **{_fmt(cal['ece_legs'])}**")
        L.append("")
        L.append("| pred bin | n | mean pred | realized | 95% CI |")
        L.append("|---|---:|---:|---:|---|")
        for b in cal["reliability_legs"]:
            if b["count"] == 0:
                continue
            L.append(f"| [{b['bin_lo']:.1f},{b['bin_hi']:.1f}) | {b['count']} | "
                     f"{_fmt(b['mean_pred'])} | {_fmt(b['freq_pos'])} | "
                     f"{_fmt(b['ci_lo'])}…{_fmt(b['ci_hi'])} |")
    L.append("")

    # CLV
    L.append("## 2. Walk-forward CLV (model fair vs consensus close)")
    if clv.get("n_legs", 0) == 0:
        L.append(f"_{clv.get('note', 'no data')}_")
    else:
        L.append(f"- Fixtures matched to a close: **{clv['n_fixtures_matched']}** "
                 f"({clv['n_legs']} legs)")
        L.append(f"- CLV mean **{_fmt(clv['clv_mean'], pct=True)}** | "
                 f"median **{_fmt(clv['clv_median'], pct=True)}** | "
                 f"trimmed **{_fmt(clv['clv_trimmed'], pct=True)}** | "
                 f"beat-close **{_fmt(clv['beat_close_rate'], pct=True)}**")
        f = clv["flagged_ge_2pct"]
        L.append(f"- **+EV-flagged legs (edge ≥ 2%)**: n={f['n']}, "
                 f"CLV mean **{_fmt(f['clv_mean'], pct=True)}**, "
                 f"beat-close **{_fmt(f['beat_rate'], pct=True)}** "
                 f"(this is the headline edge-validity check)")
        L.append("")
        L.append("| build-time edge bucket | n | CLV mean | beat-close |")
        L.append("|---|---:|---:|---:|")
        for k, v in clv["by_edge_bucket"].items():
            if v["n"] == 0:
                continue
            L.append(f"| {k} | {v['n']} | {_fmt(v['clv_mean'], pct=True)} | "
                     f"{_fmt(v['beat_rate'], pct=True)} |")
    L.append("")

    # Ledger
    L.append("## 3. Realized ledger (placed bets)")
    if led.get("n_total", 0) == 0:
        L.append(f"_{led.get('note', 'no data')}_")
    else:
        o = led["overall"]
        L.append(f"- Bets: **{led['n_total']}** total, {led['n_settled']} settled, "
                 f"{led['n_decided']} decided")
        L.append(f"- Overall: staked £{o['staked']}, P&L £{o['pl']}, "
                 f"ROI **{_fmt(o['roi'], pct=True)}**, mean CLV "
                 f"**{_fmt(o['mean_clv'], pct=True)}** (n={o['n_clv']})")
        L.append("")
        L.append("| market family | n | staked | P&L | ROI | win% | mean CLV |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for k, v in sorted(led["by_market"].items(), key=lambda kv: kv[1]["pl"]):
            L.append(f"| {k} | {v['n']} | £{v['staked']} | £{v['pl']} | "
                     f"{_fmt(v['roi'], pct=True)} | {_fmt(v['win_rate'], pct=True)} | "
                     f"{_fmt(v['mean_clv'], pct=True)} |")
        L.append("")
        L.append("| venue | n | staked | P&L | ROI | win% | mean CLV |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for k, v in sorted(led["by_venue"].items(), key=lambda kv: kv[1]["pl"]):
            L.append(f"| {k} | {v['n']} | £{v['staked']} | £{v['pl']} | "
                     f"{_fmt(v['roi'], pct=True)} | {_fmt(v['win_rate'], pct=True)} | "
                     f"{_fmt(v['mean_clv'], pct=True)} |")
    L.append("")
    L.append("---")
    L.append("_Caveat: computed from a copy of the dev-box ledger, which forks "
             "from the canonical mini ledger; small samples — treat as harness "
             "validation, not the production scorecard._")
    return "\n".join(L)
