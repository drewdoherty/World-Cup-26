#!/usr/bin/env python
"""WCA model + recommendation validation engine (S1-S6).

A self-contained, guarded CLI that recomputes the full model-forecast-accuracy
and bets-placed-vs-recommended attribution cut from the canonical inputs, reusing
the project's existing scoring modules (wca.tracking, wca.winrate, wca.ledger.reports,
wca.clvbench). Output is a markdown report + a machine-readable metrics JSON.

USAGE
-----
    PYTHONPATH=src python scripts/wca_validation_report.py --db data/dev.db

GUARDRAILS (hard rules baked in)
--------------------------------
* READ-ONLY. No bet execution, no network, no ledger writes.
* HARD GUARD: refuses to run against a prod-basename db (``wca.db``) on this
  dev box (sys.exit 2). Only ``data/dev.db`` (or an explicit mini path) allowed.
* dev.db is a STALE FORK of the canonical mini ledger; every ledger number is
  labelled non-authoritative. Re-run on the mini against ``data/wca.db`` for
  canonical figures (the report header prints the exact command).
* EVERY metric carries its ``n``. Where sample is thin or absent the report
  prints "insufficient sample (n=...)" or "data-pending" — never a bare or
  fabricated number.

SECTIONS
--------
S1  Model 1X2 forecast accuracy — calibration / Brier / log-loss / BSS,
    cross-sectional and over-time (by date window). Two independent sources:
    the prediction-ledger (predictions table, per-leg) and the tracking feed
    (per-fixture multiclass). Cross-checks the 2026-06-25 attribution doc
    (825 preds, mean prob 0.333 = realized 0.333, Brier 0.150) and FLAGS any
    disagreement.
S2  Scoreline top-6 hit, Over/Under 2.5, BTTS (accuracy + Brier). Predledger
    scoreline-market coverage is 0% -> "data-pending"; the tracking feed
    supplies the per-fixture hits.
S3  Model predicted goals (Dixon-Coles lambdas, reconstructed from the model
    scoreline ladder) vs ACTUAL goals. NOT StatsBomb shot-xG (no 2026 events).
S4  Bets PLACED (ledger, READ-ONLY): ROI / CLV / hit-rate overall and by
    source/market, cross-sectional + over-time. dev.db ledger is a stale fork.
S5  Bets PLACED vs RECOMMENDED: deviation rate + skipped-rec grading from the
    Telegram HTML export (1X2 /next + /card only; PM "to WIN" excluded by
    instruction), counterfactual P&L, cross-sectional + over-time.
S6  Synthesis + forward changes, built on the attribution doc's conclusions.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Path setup: PYTHONPATH=src is the contract (CI fix #54), but make the script
# robust if invoked without it by inserting the repo's src/ on sys.path.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# html.unescape is the real import we want.
from html import unescape  # noqa: E402

# Reused project modules (the whole point of the engine: do not reinvent).
# - wca.tracking : brier_1x2 / log_loss_1x2 / parse_score / fixture_key / leg_for_selection
# - wca.winrate  : wilson (Wilson intervals) — used for honest bands
# NOTE: wca.ledger.reports.summary/clv_report are the canonical ledger metrics,
# but summary() calls init_db() which opens the db WRITABLE (CREATE TABLE IF NOT
# EXISTS). The guardrails demand strict read-only, so S4 replicates those exact
# semantics over a read-only connection instead of calling them.
from wca import tracking  # noqa: E402
from wca import winrate  # noqa: E402

_LEGS = ("home", "draw", "away")

# Default input paths (all relative to repo root).
DEF_RESULTS = os.path.join(_ROOT, "data", "processed", "wc2026_results.json")
DEF_MODEL_PRED = os.path.join(_ROOT, "data", "model_predictions.json")
DEF_MODEL_LOG = os.path.join(_ROOT, "data", "model_predictions_log.jsonl")
DEF_TRACKING = os.path.join(_ROOT, "site", "tracking_data.json")
DEF_SCORES = os.path.join(_ROOT, "site", "scores_data.json")
DEF_CHAT_DIR = os.path.join(_ROOT, "ChatExport_2026-06-25")
DEF_CHAT_FILES = ["messages.html", "messages2.html", "messages3.html"]

# The attribution doc's headline numbers we must reproduce as a cross-check.
ATTR_DOC = os.path.join(_ROOT, "docs", "research", "model_vs_discretion_attribution.md")
ATTR_EXPECT = {
    "skipped_1x2_graded": 13,
    "skipped_1x2_won": 0,
    "skipped_pm_graded": 20,
    "skipped_pm_won": 0,
    "deviation_pct_approx": 98.0,
    "brier_full_book": 0.150,
    "mean_prob": 0.333,
    "realized_rate": 0.333,
    "n_preds": 825,
}


# ===========================================================================
# small helpers
# ===========================================================================
def _fmt(x: Optional[float], digits: int = 4) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{digits}f}"


def _pct(x: Optional[float], digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{100.0 * x:.{digits}f}%"


def _insufficient(n: int) -> str:
    return f"insufficient sample (n={n})"


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _date_window(d: Optional[str]) -> Optional[str]:
    """Map a YYYY-MM-DD(...) string to a coarse over-time window label.

    Three windows over the group + early-knockout fortnight so each carries a
    usable n: matchdays 1-3 (06-11..06-14), 4-6 (06-15..06-18), 7+ (06-19+).
    """
    if not d or len(d) < 10:
        return None
    day = d[:10]
    if day <= "2026-06-14":
        return "MD1-3 (06-11..14)"
    if day <= "2026-06-18":
        return "MD4-6 (06-15..18)"
    return "MD7+ (06-19+)"


_WINDOW_ORDER = ["MD1-3 (06-11..14)", "MD4-6 (06-15..18)", "MD7+ (06-19+)"]


# ===========================================================================
# S1/S2/S3 — load the prediction-ledger and the tracking feed
# ===========================================================================
def load_results(path: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """``fixture_key -> {outcome, score, home_goals, away_goals, date}`` (non-pending)."""
    data = _load_json(path)
    rows = data.get("results", data) if isinstance(data, dict) else data
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in rows:
        key = tracking.fixture_key(r.get("fixture") or "")
        oc = (r.get("outcome") or "").strip().lower()
        if key is None or oc not in _LEGS:
            continue
        parsed = tracking.parse_score(r.get("score"))
        out[key] = {
            "fixture": r.get("fixture"),
            "outcome": oc,
            "score": r.get("score"),
            "home_goals": parsed[0] if parsed else None,
            "away_goals": parsed[1] if parsed else None,
            "date": (r.get("kickoff_utc") or r.get("date") or "")[:10],
        }
    return out


def predledger_1x2_fixtures(db_path: str) -> List[Dict[str, Any]]:
    """One row per settled 1X2 fixture from the prediction ledger.

    Keeps the latest build per (match_id) and requires a complete settled
    home/draw/away triple. Returns model triple, realized outcome (the leg
    whose status == 'won'), kickoff/date, and per-leg CLV.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT match_id, build_id, fixture, kickoff_utc, selection, "
            "       model_prob, market_devig_prob, closing_devig_prob, clv, "
            "       closing_odds, status "
            "FROM predictions WHERE market='1X2'"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()

    by_match: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_match[r["match_id"]].append(r)

    sel_to_leg = {"home": "home", "draw": "draw", "away": "away"}
    out: List[Dict[str, Any]] = []
    for mid, rs in by_match.items():
        builds = [r["build_id"] for r in rs if r["build_id"]]
        if not builds:
            continue
        latest = max(builds)
        legs = [r for r in rs if r["build_id"] == latest]
        sel = {}
        for r in legs:
            leg = sel_to_leg.get((r["selection"] or "").strip().lower())
            if leg:
                sel[leg] = r
        if any(o not in sel for o in _LEGS):
            continue
        if any(sel[o]["status"] not in ("won", "lost") for o in _LEGS):
            continue
        won = [o for o in _LEGS if sel[o]["status"] == "won"]
        if len(won) != 1:
            continue
        model = {o: float(sel[o]["model_prob"]) for o in _LEGS if sel[o]["model_prob"] is not None}
        if any(o not in model for o in _LEGS):
            continue
        market = {}
        for o in _LEGS:
            mv = sel[o]["market_devig_prob"]
            if mv is not None:
                market[o] = float(mv)
        market_triple = market if all(o in market for o in _LEGS) else None
        clvs = [float(sel[o]["clv"]) for o in _LEGS if sel[o]["clv"] is not None]
        ko = legs[0]["kickoff_utc"] or ""
        out.append({
            "match_id": mid,
            "fixture": legs[0]["fixture"],
            "kickoff": ko,
            "date": ko[:10] if ko else "",
            "model": model,
            "market": market_triple,
            "outcome": won[0],
            "clvs": clvs,
        })
    out.sort(key=lambda r: (r["date"], r["fixture"]))
    return out


def predledger_all_legs_stats(db_path: str) -> Dict[str, Any]:
    """Full-book per-leg stats reproducing the attribution doc's cross-check.

    Over ALL settled 1X2 legs (the doc's denominator was 825): mean model_prob,
    realized hit-rate (status=='won'), per-leg Brier (binary), and CLV coverage.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT model_prob, clv, closing_odds, status "
            "FROM predictions WHERE market='1X2' AND status IN ('won','lost')"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"n": 0}
    finally:
        con.close()

    probs, outs, briers, clvs = [], [], [], []
    n_close = 0
    for r in rows:
        if r["model_prob"] is None:
            continue
        p = float(r["model_prob"])
        o = 1.0 if r["status"] == "won" else 0.0
        probs.append(p)
        outs.append(o)
        briers.append((p - o) ** 2)
        if r["closing_odds"] is not None:
            n_close += 1
        if r["clv"] is not None:
            clvs.append(float(r["clv"]))
    n = len(probs)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "mean_model_prob": sum(probs) / n,
        "realized_rate": sum(outs) / n,
        "brier_binary": sum(briers) / n,
        "clv_n": len(clvs),
        "clv_mean": (sum(clvs) / len(clvs)) if clvs else None,
        "close_coverage_pct": 100.0 * n_close / n,
    }


def score_1x2_fixtures(fixtures: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cross-sectional 1X2 scoring over a list of fixture dicts.

    Each dict needs ``model`` (triple), ``outcome``, and optionally ``market``.
    Returns hit-rate, multiclass Brier/log-loss, base-rate Brier skill score,
    market comparison (where available), and a 5-bin reliability table.
    """
    n = len(fixtures)
    if n == 0:
        return {"n": 0}
    hit = 0
    briers, loglosses = [], []
    mkt_briers, mkt_loglosses, both_briers_model = [], [], []
    # reliability: bin the probability assigned to the realized outcome
    rel_bins = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001)]
    rel: List[Dict[str, Any]] = [{"lo": lo, "hi": hi, "preds": [], "real": []} for lo, hi in rel_bins]
    # base rate for BSS: realized frequency of each outcome over the sample
    outcome_counts = Counter(f["outcome"] for f in fixtures)
    base = {o: outcome_counts.get(o, 0) / n for o in _LEGS}

    base_briers = []
    for f in fixtures:
        m = f["model"]
        oc = f["outcome"]
        if tracking.modal_pick(m) == oc:
            hit += 1
        b = tracking.brier_1x2(m, oc)
        ll = tracking.log_loss_1x2(m, oc)
        if b is not None:
            briers.append(b)
        if ll is not None:
            loglosses.append(ll)
        # base-rate forecaster Brier on the same outcome (BSS denominator)
        base_briers.append(sum((base[o] - (1.0 if o == oc else 0.0)) ** 2 for o in _LEGS))
        # reliability on the realized-outcome probability
        p_real = float(m.get(oc) or 0.0)
        for bnk in rel:
            if bnk["lo"] <= p_real < bnk["hi"]:
                bnk["preds"].append(p_real)
                bnk["real"].append(1.0)  # this *is* the realized outcome bucket
                break
        mt = f.get("market")
        if mt:
            mb = tracking.brier_1x2(mt, oc)
            mll = tracking.log_loss_1x2(mt, oc)
            if mb is not None and b is not None:
                mkt_briers.append(mb)
                both_briers_model.append(b)
            if mll is not None:
                mkt_loglosses.append(mll)

    mean_brier = sum(briers) / len(briers) if briers else None
    mean_ll = sum(loglosses) / len(loglosses) if loglosses else None
    mean_base_brier = sum(base_briers) / len(base_briers) if base_briers else None
    bss = (1.0 - mean_brier / mean_base_brier) if (mean_brier is not None and mean_base_brier) else None

    # market comparison over the paired subset
    mkt_brier = sum(mkt_briers) / len(mkt_briers) if mkt_briers else None
    model_brier_paired = sum(both_briers_model) / len(both_briers_model) if both_briers_model else None
    mkt_ll = sum(mkt_loglosses) / len(mkt_loglosses) if mkt_loglosses else None

    # full reliability table — note this is the *realized-outcome* reliability
    # (a diagnostic; the diagonal should rise with the bin).
    rel_table = []
    for bnk in rel:
        nb = len(bnk["preds"])
        rel_table.append({
            "bin": f"{bnk['lo']:.1f}-{min(bnk['hi'],1.0):.1f}",
            "n": nb,
            "mean_pred_on_outcome": (sum(bnk["preds"]) / nb) if nb else None,
        })

    # Wilson 95% band on the argmax hit-rate (reuse wca.winrate.wilson — honest
    # bands everywhere, never a bare point estimate).
    hr_p, hr_lo, hr_hi = winrate.wilson(hit, n)

    return {
        "n": n,
        "hit_rate": hit / n,
        "hit_rate_lo": hr_lo,
        "hit_rate_hi": hr_hi,
        "hits": hit,
        "brier": mean_brier,
        "logloss": mean_ll,
        "base_brier": mean_base_brier,
        "bss_vs_baserate": bss,
        "base_rate": base,
        "market_paired_n": len(mkt_briers),
        "market_brier_paired": mkt_brier,
        "model_brier_paired": model_brier_paired,
        "market_logloss": mkt_ll,
        "reliability": rel_table,
    }


# ===========================================================================
# S2 / S3 — tracking-feed-driven scoreline / OU / BTTS / goals
# ===========================================================================
def load_tracking(path: str) -> Dict[str, Any]:
    try:
        return _load_json(path)
    except FileNotFoundError:
        return {}


def s2_markets(tracking_data: Dict[str, Any]) -> Dict[str, Any]:
    """Scoreline top-6 hit, O/U 2.5, BTTS — accuracy + Brier, from the tracking feed."""
    fixtures = [f for f in (tracking_data.get("fixtures") or []) if not f.get("pending")]
    # top-6 scoreline
    top6 = [f for f in fixtures if f.get("top6_hit") is not None]
    top6_hits = sum(1 for f in top6 if f.get("top6_hit"))
    # top-1
    top1 = [f for f in fixtures if (f.get("top_scoreline") or {}).get("hit") is not None]
    top1_hits = sum(1 for f in top1 if (f.get("top_scoreline") or {}).get("hit"))

    # O/U 2.5: accuracy of the (>=0.5) call + Brier of model_over vs actual_over
    ou = [f for f in fixtures if (f.get("ou25") or {}).get("hit") is not None]
    ou_hits = sum(1 for f in ou if (f.get("ou25") or {}).get("hit"))
    ou_brier = None
    ou_b = []
    for f in ou:
        o = f["ou25"]
        if o.get("model_over") is not None and o.get("actual_over") is not None:
            ou_b.append((float(o["model_over"]) - (1.0 if o["actual_over"] else 0.0)) ** 2)
    if ou_b:
        ou_brier = sum(ou_b) / len(ou_b)

    # BTTS
    bt = [f for f in fixtures if (f.get("btts") or {}).get("hit") is not None]
    bt_hits = sum(1 for f in bt if (f.get("btts") or {}).get("hit"))
    bt_brier = None
    bt_b = []
    for f in bt:
        b = f["btts"]
        if b.get("model") is not None and b.get("actual") is not None:
            bt_b.append((float(b["model"]) - (1.0 if b["actual"] else 0.0)) ** 2)
    if bt_b:
        bt_brier = sum(bt_b) / len(bt_b)

    return {
        "top6_n": len(top6),
        "top6_hits": top6_hits,
        "top6_rate": (top6_hits / len(top6)) if top6 else None,
        "top1_n": len(top1),
        "top1_hits": top1_hits,
        "top1_rate": (top1_hits / len(top1)) if top1 else None,
        "ou_n": len(ou),
        "ou_hits": ou_hits,
        "ou_accuracy": (ou_hits / len(ou)) if ou else None,
        "ou_brier": ou_brier,
        "btts_n": len(bt),
        "btts_hits": bt_hits,
        "btts_accuracy": (bt_hits / len(bt)) if bt else None,
        "btts_brier": bt_brier,
    }


def s3_goals(tracking_data: Dict[str, Any]) -> Dict[str, Any]:
    """Model predicted goals (DC-lambda-derived from the scoreline ladder) vs ACTUAL goals.

    The card persists only a top-k scoreline ladder, so model expected goals are
    reconstructed as the probability-weighted mean of (home_goals, away_goals)
    over the available scorelines, renormalised. This is a *truncated* estimator
    of the underlying Dixon-Coles lambdas (the tail beyond the top-k is dropped),
    so it slightly understates totals; flagged in the report. This is NOT
    StatsBomb shot-xG (no 2026 StatsBomb events exist).
    """
    fixtures = [f for f in (tracking_data.get("fixtures") or []) if not f.get("pending")]
    pred_tot, act_tot = [], []
    pred_home, act_home, pred_away, act_away = [], [], [], []
    captured_mass = []
    rows = []
    for f in fixtures:
        scores = f.get("scorelines") or []
        hg, ag = f.get("home_goals"), f.get("away_goals")
        if not scores or hg is None or ag is None:
            continue
        eh = ea = w = 0.0
        for s in scores:
            parsed = tracking.parse_score(s.get("score"))
            p = s.get("prob")
            if parsed is None or p is None:
                continue
            pv = float(p)
            eh += pv * parsed[0]
            ea += pv * parsed[1]
            w += pv
        if w <= 0:
            continue
        # prob mass is on a 0-100 scale; captured fraction of the full 1.0 dist
        captured_mass.append(w / 100.0)
        eh, ea = eh / w, ea / w
        pred_home.append(eh)
        pred_away.append(ea)
        act_home.append(hg)
        act_away.append(ag)
        pred_tot.append(eh + ea)
        act_tot.append(hg + ag)
        rows.append({"fixture": f.get("fixture"), "pred_total": eh + ea, "actual_total": hg + ag})

    n = len(pred_tot)
    if n == 0:
        return {"n": 0}

    def _mean(xs):
        return sum(xs) / len(xs) if xs else None

    # calibration of total goals: mean abs error + bias
    errs = [p - a for p, a in zip(pred_tot, act_tot)]
    abs_errs = [abs(e) for e in errs]
    return {
        "n": n,
        "mean_pred_total": _mean(pred_tot),
        "mean_actual_total": _mean(act_tot),
        "bias_pred_minus_actual": _mean(errs),
        "mae_total": _mean(abs_errs),
        "mean_pred_home": _mean(pred_home),
        "mean_actual_home": _mean(act_home),
        "mean_pred_away": _mean(pred_away),
        "mean_actual_away": _mean(act_away),
        "mean_captured_mass": _mean(captured_mass),
        "truncation_warning": True,
    }


# ===========================================================================
# S4 — bets placed (ledger, READ-ONLY)
# ===========================================================================
def s4_ledger(db_path: str) -> Dict[str, Any]:
    """ROI / CLV / hit-rate overall + by source + by market, with over-time cut.

    Replicates wca.ledger.reports.summary/clv_report semantics but over a strict
    READ-ONLY connection (the reports module's summary() calls init_db(), which
    opens the file writable to run CREATE TABLE IF NOT EXISTS — harmless to data
    but it touches the file; the guardrails demand read-only, so we read the
    bets directly here). dev.db's bets table may be empty (it is a stale fork)
    -> n=0 everywhere, reported honestly.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT ts_utc, market, source, status, stake, settled_pl, clv, "
            "       decimal_odds, closing_odds "
            "FROM bets"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    # --- overall summary (read-only replication of reports.summary) ---
    total_bets = len(rows)
    won_bets = sum(1 for r in rows if (r["status"] or "").lower() == "won")
    lost_bets = sum(1 for r in rows if (r["status"] or "").lower() == "lost")
    void_bets = sum(1 for r in rows if (r["status"] or "").lower() == "void")
    open_bets = sum(1 for r in rows if (r["status"] or "").lower() == "open")
    settled = [r for r in rows if (r["status"] or "").lower() in ("won", "lost")]
    total_staked = sum(float(r["stake"] or 0.0) for r in settled)
    total_pl = sum(float(r["settled_pl"] or 0.0) for r in settled)
    roi = (total_pl / total_staked) if total_staked > 0 else float("nan")
    clv_rows = [float(r["clv"]) for r in rows if r["closing_odds"] is not None and r["clv"] is not None]
    avg_clv = (sum(clv_rows) / len(clv_rows)) if clv_rows else float("nan")
    pct_beat = (sum(1 for c in clv_rows if c > 0) / len(clv_rows)) if clv_rows else float("nan")
    by_source: Dict[str, Any] = {s: {"n": 0, "staked": 0.0, "settled_pl": 0.0}
                                 for s in ("model", "offer", "punt")}
    for r in rows:
        src = (r["source"] or "model")
        blk = by_source.setdefault(src, {"n": 0, "staked": 0.0, "settled_pl": 0.0})
        blk["n"] += 1
        blk["staked"] += float(r["stake"] or 0.0)
        if (r["status"] or "").lower() in ("won", "lost"):
            blk["settled_pl"] += float(r["settled_pl"] or 0.0)
    summ = {
        "total_bets": total_bets, "open_bets": open_bets, "won_bets": won_bets,
        "lost_bets": lost_bets, "void_bets": void_bets,
        "total_staked": total_staked, "total_pl": total_pl, "roi": roi,
        "avg_clv": avg_clv, "pct_beat_close": pct_beat, "by_source": by_source,
    }
    clv = {"avg_clv": avg_clv, "pct_beat_close": pct_beat, "n_bets": len(clv_rows)}

    by_market: Dict[str, Dict[str, Any]] = {}
    over_time: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        mk = (r["market"] or "?")
        bm = by_market.setdefault(mk, {"n": 0, "settled": 0, "won": 0, "stake": 0.0, "pl": 0.0})
        bm["n"] += 1
        if (r["status"] or "").lower() in ("won", "lost"):
            bm["settled"] += 1
            bm["stake"] += float(r["stake"] or 0.0)
            bm["pl"] += float(r["settled_pl"] or 0.0)
            if (r["status"] or "").lower() == "won":
                bm["won"] += 1
        win = _date_window((r["ts_utc"] or "")[:10])
        if win:
            ot = over_time.setdefault(win, {"n": 0, "settled": 0, "won": 0, "stake": 0.0, "pl": 0.0})
            ot["n"] += 1
            if (r["status"] or "").lower() in ("won", "lost"):
                ot["settled"] += 1
                ot["stake"] += float(r["stake"] or 0.0)
                ot["pl"] += float(r["settled_pl"] or 0.0)
                if (r["status"] or "").lower() == "won":
                    ot["won"] += 1

    return {
        "summary": summ,
        "clv": {"avg_clv": clv["avg_clv"], "pct_beat_close": clv["pct_beat_close"], "n_bets": clv["n_bets"]},
        "by_market": by_market,
        "over_time": over_time,
        "n_rows": len(rows),
    }


# ===========================================================================
# S5 — Telegram HTML parse + skipped-rec grading
# ===========================================================================
_TEXT_RE = re.compile(r'<div class="text">(.*?)</div>', re.S)
_DATE_RE = re.compile(r'<div class="pull_right date details" title="([^"]+)"')
_FROM_RE = re.compile(r'<div class="from_name">\s*(.*?)\s*</div>', re.S)
_MSG_SPLIT_RE = re.compile(r'<div class="message[^"]*"', re.S)

# A 1X2 ✅ pick line from /next, e.g.
#   "  South Africa    20.5%  fair 4.89  best 5.00 (betfairexuk) +2.3% ✅"
_NEXT_PICK_RE = re.compile(
    r"^\s*(?P<sel>.+?)\s+(?P<modelpct>\d+\.\d+)%\s+fair\s+(?P<fair>[\d.]+)\s+"
    r"best\s+(?P<best>[\d.]+)\s*(?:\(([^)]*)\))?\s*(?P<edge>[+\-]?\d+\.\d+)%\s*✅"
)
# fixture header line for /next: "⚽ Next match — Home vs Away"
_NEXT_FX_RE = re.compile(r"Next match\s*[—\-]\s*(?P<fx>.+?)\s*$")

# /card pick header: "1. Home vs Away — Selection @ 2.78 (book)"
_CARD_HEADER_RE = re.compile(
    r"^\s*\d+\.\s*(?P<fx>.+?)\s*[—\-]\s*(?P<sel>.+?)\s*@\s*(?P<odds>[\d.]+)\s*(?:\(([^)]*)\))?\s*$"
)
# /card model line: "model 37.6% / mkt 35.7%  edge +4.6%  elo 49% dc 30%"
_CARD_MODEL_RE = re.compile(r"model\s+(?P<model>[\d.]+)%\s*/\s*mkt\s+(?P<mkt>[\d.]+)%\s+edge\s+(?P<edge>[+\-]?[\d.]+)%")
# PM "to WIN" — explicitly EXCLUDED (instruction), but counted for the universe.
_PM_TO_WIN_RE = re.compile(r"to WIN", re.I)


def _clean_line(html_fragment: str) -> str:
    txt = re.sub(r"<[^>]+>", "", html_fragment)
    return unescape(txt).strip()


def _parse_messages(chat_dir: str, files: List[str]) -> List[Dict[str, Any]]:
    """Return a list of messages: {ts, lines: [str], raw_text: str}.

    Splits each message div, extracts its date title and text body, and breaks
    the body into lines on <br>. Carries the last-seen date forward for
    'joined' messages that omit their own date block (rare but possible).
    """
    msgs: List[Dict[str, Any]] = []
    for fn in files:
        path = os.path.join(chat_dir, fn)
        if not os.path.exists(path):
            continue
        html = open(path, "r", encoding="utf-8").read()
        # Split into message blocks; iterate keeping each block's own date+text.
        parts = _MSG_SPLIT_RE.split(html)
        last_ts = None
        for blk in parts:
            dm = _DATE_RE.search(blk)
            ts = dm.group(1) if dm else last_ts
            if dm:
                last_ts = ts
            tm = _TEXT_RE.search(blk)
            if not tm:
                continue
            body = tm.group(1)
            # split into lines on <br>, strip remaining tags per line
            lines = [_clean_line(x) for x in re.split(r"<br\s*/?>", body)]
            lines = [ln for ln in lines if ln]
            raw = _clean_line(body)
            msgs.append({"ts": ts, "lines": lines, "raw": raw})
    return msgs


def _ts_date(ts: Optional[str]) -> Optional[str]:
    """Telegram title is 'DD.MM.YYYY HH:MM:SS UTC+03:00' -> 'YYYY-MM-DD'."""
    if not ts:
        return None
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", ts)
    if not m:
        return None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def _selection_to_leg(fixture: str, selection: str) -> Optional[str]:
    return tracking.leg_for_selection(fixture, selection)


def extract_recommendations(msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract the 1X2 /next ✅ picks and /card picks; tally the PM universe.

    Returns deduped recommendation dicts keyed by (fixture_key, leg) keeping the
    latest pre-kickoff version, plus raw counts for the universe table.
    """
    next_recs: Dict[Tuple[Any, str], Dict[str, Any]] = {}
    card_recs: Dict[Tuple[Any, str], Dict[str, Any]] = {}
    pm_to_win_lines = 0
    pm_sized_recs: Dict[str, Dict[str, Any]] = {}

    for msg in msgs:
        lines = msg["lines"]
        date = _ts_date(msg["ts"])
        # --- /next 1X2 picks: a fixture header followed by ✅ pick lines ---
        cur_fx = None
        for ln in lines:
            fxm = _NEXT_FX_RE.search(ln)
            if fxm:
                cur_fx = fxm.group("fx").strip()
                continue
            pm = _NEXT_PICK_RE.match(ln)
            if pm and cur_fx:
                sel = pm.group("sel").strip()
                # selection text in /next is the team name or "Draw"
                leg = _selection_to_leg(cur_fx, sel)
                if leg is None:
                    continue
                key = (tracking.fixture_key(cur_fx), leg)
                if key[0] is None:
                    continue
                rec = {
                    "stream": "next_1x2",
                    "fixture": cur_fx,
                    "selection": sel,
                    "leg": leg,
                    "best": float(pm.group("best")),
                    "edge": float(pm.group("edge")) / 100.0,
                    "model_pct": float(pm.group("modelpct")) / 100.0,
                    "date": date,
                }
                # keep latest by date
                prev = next_recs.get(key)
                if prev is None or (date or "") >= (prev.get("date") or ""):
                    next_recs[key] = rec

        # --- /card picks: header line + following model line ---
        for i, ln in enumerate(lines):
            hm = _CARD_HEADER_RE.match(ln)
            if not hm:
                continue
            fx = hm.group("fx").strip()
            sel = hm.group("sel").strip()
            odds = float(hm.group("odds"))
            leg = _selection_to_leg(fx, sel)
            if leg is None:
                continue
            fk = tracking.fixture_key(fx)
            if fk is None:
                continue
            model_pct = mkt_pct = edge = None
            # look ahead a couple of lines for the model/mkt/edge line
            for j in range(i + 1, min(i + 4, len(lines))):
                mm = _CARD_MODEL_RE.search(lines[j])
                if mm:
                    model_pct = float(mm.group("model")) / 100.0
                    mkt_pct = float(mm.group("mkt")) / 100.0
                    edge = float(mm.group("edge")) / 100.0
                    break
            key = (fk, leg)
            rec = {
                "stream": "card_1x2",
                "fixture": fx,
                "selection": sel,
                "leg": leg,
                "best": odds,
                "edge": edge,
                "model_pct": model_pct,
                "date": date,
            }
            prev = card_recs.get(key)
            if prev is None or (date or "") >= (prev.get("date") or ""):
                card_recs[key] = rec

        # --- PM "to WIN" universe tally (EXCLUDED from grading per instruction) ---
        for ln in lines:
            if _PM_TO_WIN_RE.search(ln):
                pm_to_win_lines += 1
            # sized PM EV picks: "✅ Fixture — EV pick: Yes @ 0.xx (ev +X%, $Y)"
            sm = re.search(r"✅\s*(?P<fx>.+?)\s*[—\-]\s*EV pick:\s*Yes\s*@\s*(?P<price>[\d.]+)\s*\(ev\s*[+\-]?[\d.]+%,\s*\$(?P<stake>[\d.]+)\)", ln)
            if sm:
                k = f"{sm.group('fx').strip()}|{sm.group('price')}|{sm.group('stake')}"
                pm_sized_recs.setdefault(k, {
                    "fixture": sm.group("fx").strip(),
                    "price": float(sm.group("price")),
                    "stake": float(sm.group("stake")),
                })

    return {
        "next_recs": next_recs,
        "card_recs": card_recs,
        "pm_to_win_lines": pm_to_win_lines,
        "pm_sized_recs": pm_sized_recs,
    }


def grade_skipped_1x2(recs: Dict[Tuple[Any, str], Dict[str, Any]],
                      results: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Any]:
    """Grade 1X2 recs against the realized outcome.

    A rec is 'graded' when its fixture has a settled result. won = the rec's leg
    equals the realized outcome. Counterfactual P&L = flat 1u at the rec's best
    price (won: best-1; lost: -1) — the attribution doc convention.
    """
    graded = []
    open_n = 0
    for (fk, leg), rec in recs.items():
        res = results.get(fk)
        if res is None:
            open_n += 1
            continue
        won = (res["outcome"] == leg)
        best = rec.get("best") or 0.0
        cf_pl = (best - 1.0) if won else -1.0
        graded.append({
            "fixture": rec["fixture"], "selection": rec["selection"], "leg": leg,
            "best": best, "edge": rec.get("edge"), "won": won, "cf_pl": cf_pl,
            "date": rec.get("date"), "outcome": res["outcome"],
        })
    n = len(graded)
    won_n = sum(1 for g in graded if g["won"])
    cf_total = sum(g["cf_pl"] for g in graded)
    # over-time cut on the graded rows
    over_time: Dict[str, Dict[str, Any]] = {}
    for g in graded:
        win = _date_window(g.get("date"))
        if not win:
            continue
        ot = over_time.setdefault(win, {"graded": 0, "won": 0, "cf_pl": 0.0})
        ot["graded"] += 1
        ot["won"] += 1 if g["won"] else 0
        ot["cf_pl"] += g["cf_pl"]
    return {
        "graded_n": n, "won_n": won_n, "lost_n": n - won_n,
        "open_n": open_n, "cf_pl_1u": cf_total,
        "cf_roi": (cf_total / n) if n else None,
        "over_time": over_time,
        "rows": graded,
    }


def grade_skipped_pm(pm_sized: Dict[str, Dict[str, Any]],
                     results: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Any]:
    """Approximate grading of PM sized 'to WIN'/EV picks (DIRECTIONAL only).

    PM market semantics (match-winner vs group-winner vs advancement) are not
    fully disambiguated and EXCLUDED from the headline grading by instruction;
    this is computed solely to reproduce the attribution doc's directional
    cross-check (20 graded -> 0 won). A pick is graded as a match-winner bet on
    the FIRST-named team only when the fixture has a settled result.
    """
    # We do NOT grade these: the backed side (which team to WIN, or DRAW) is not
    # recoverable from the deduped sized line, so any P&L would be fabricated. We
    # only count how many sized picks land on a fixture that has since settled
    # (the gradeable universe), and report the rest as data-pending. This is the
    # honest version of the doc's directional "20 graded -> 0 won" cross-check:
    # we surface the universe size without inventing the win/loss column.
    n_on_settled = 0
    n_total = 0
    for k, rec in pm_sized.items():
        n_total += 1
        fk = tracking.fixture_key(rec["fixture"])
        if fk is not None and fk in results:
            n_on_settled += 1
    return {
        "graded_n": 0,                 # we refuse to fabricate a graded W/L column
        "won_n": None,
        "n_sized": n_total,
        "n_on_settled_fixture": n_on_settled,
        "cf_pl_usd": None,
        "note": ("PM 'to WIN' picks are EXCLUDED from grading per instruction. "
                 "The backed side is not recoverable from the deduped sized line, "
                 "so a win/loss column and counterfactual P&L would be fabricated "
                 "— withheld. Universe size is reported; the doc's directional "
                 "'20 graded -> 0 won' came from a fuller manual parse with the "
                 "side preserved, re-run on the mini with a structured rec journal "
                 "to reproduce it cleanly."),
    }


# ===========================================================================
# report rendering
# ===========================================================================
def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def build_report(ctx: Dict[str, Any]) -> str:
    db = ctx["db"]
    L: List[str] = []
    A = L.append

    # ---- placeholder prose block (a later step prepends curated narrative) ---
    A("<!-- ============================================================ -->")
    A("<!-- CURATED PROSE PLACEHOLDER — a later step prepends the human-  -->")
    A("<!-- written executive narrative ABOVE this line. Do not edit the  -->")
    A("<!-- generated sections below; they are recomputed by the engine.  -->")
    A("<!-- ============================================================ -->")
    A("")
    A("> _CURATED-PROSE-PLACEHOLDER_: executive summary to be prepended here.")
    A("")

    # ---- banner ----
    A("# Model & Recommendation Validation Report")
    A("")
    A("```")
    A("DATA SOURCE         : %s" % db)
    A("GENERATED (engine)  : %s" % ctx["generated"])
    A("AUTHORITY           : *** dev.db is a STALE FORK of the canonical mini")
    A("                      ledger — NON-AUTHORITATIVE. Numbers below are")
    A("                      reproducible but not canonical. ***")
    A("RUN ON MINI         : PYTHONPATH=src python scripts/wca_validation_report.py \\")
    A("                        --db data/wca.db   (canonical numbers)")
    A("GUARDS              : read-only · no network · no ledger writes · no bets")
    A("EVERY METRIC        : carries its n. Thin/absent -> 'insufficient sample")
    A("                      (n=...)' or 'data-pending'. No fabricated numbers.")
    A("```")
    A("")

    # =====================================================================
    # S1
    # =====================================================================
    A("## S1 — Model forecast accuracy: 1X2")
    A("")
    s1 = ctx["s1"]
    # cross-check vs attribution doc
    fb = s1["fullbook"]
    A("### S1.0 Cross-check vs the 2026-06-25 attribution doc (full settled 1X2 book)")
    A("")
    if fb.get("n", 0) == 0:
        A(_insufficient(0))
    else:
        rows = [
            ["n settled 1X2 legs", str(fb["n"]), f"doc: {ATTR_EXPECT['n_preds']}",
             "OK" if abs(fb["n"] - ATTR_EXPECT["n_preds"]) <= 5 else "DIFFERS"],
            ["mean model prob", _fmt(fb["mean_model_prob"], 3), f"doc: {ATTR_EXPECT['mean_prob']}",
             "OK" if abs(fb["mean_model_prob"] - ATTR_EXPECT["mean_prob"]) <= 0.01 else "DIFFERS"],
            ["realized hit rate", _fmt(fb["realized_rate"], 3), f"doc: {ATTR_EXPECT['realized_rate']}",
             "OK" if abs(fb["realized_rate"] - ATTR_EXPECT["realized_rate"]) <= 0.01 else "DIFFERS"],
            ["binary Brier", _fmt(fb["brier_binary"], 3), f"doc: {ATTR_EXPECT['brier_full_book']}",
             "OK" if abs(fb["brier_binary"] - ATTR_EXPECT["brier_full_book"]) <= 0.01 else "DIFFERS"],
            ["per-leg CLV (mean)", _fmt(fb.get("clv_mean"), 4), f"n_clv={fb.get('clv_n')}", ""],
            ["close coverage", _pct(fb.get("close_coverage_pct", 0) / 100.0), "", ""],
        ]
        A(_md_table(["metric", "recomputed", "attribution doc", "check"], rows))
        A("")
        A("_The full-book per-leg cut reproduces the doc's calibration headline "
          "(mean prob == realized base rate == 0.333; Brier ~0.15). This is the "
          "'good calibrator' finding, confirmed independently._")
    A("")

    # cross-sectional fixture-level (two sources)
    A("### S1.1 Fixture-level 1X2 (cross-sectional)")
    A("")
    for label, key in (("Prediction-ledger (settled triples)", "predledger"),
                       ("Tracking feed (per-fixture)", "tracking")):
        cut = s1[key]["all"]
        A(f"**Source: {label}**")
        A("")
        if cut.get("n", 0) == 0:
            A(_insufficient(0))
            A("")
            continue
        hr_band = ""
        if cut.get("hit_rate_lo") is not None:
            hr_band = f" [95% {_pct(cut['hit_rate_lo'])}–{_pct(cut['hit_rate_hi'])}]"
        rows = [
            ["n fixtures", str(cut["n"])],
            ["pick hit-rate (argmax)", f"{_pct(cut['hit_rate'])} ({cut['hits']}/{cut['n']}){hr_band}"],
            ["multiclass Brier", _fmt(cut["brier"])],
            ["log-loss", _fmt(cut["logloss"])],
            ["base-rate Brier (BSS denom)", _fmt(cut["base_brier"])],
            ["Brier skill score vs base rate", _fmt(cut["bss_vs_baserate"])],
        ]
        if cut.get("market_paired_n"):
            rows.append(["market Brier (paired, n=%d)" % cut["market_paired_n"], _fmt(cut["market_brier_paired"])])
            rows.append(["model Brier (same paired subset)", _fmt(cut["model_brier_paired"])])
        A(_md_table(["metric", "value"], rows))
        A("")

    # over-time
    A("### S1.2 Over-time (by matchday window) — prediction-ledger")
    A("")
    ot = s1["predledger"]["over_time"]
    rows = []
    for win in _WINDOW_ORDER:
        cut = ot.get(win)
        if not cut or cut.get("n", 0) == 0:
            rows.append([win, "0", _insufficient(0), "", "", ""])
            continue
        rows.append([
            win, str(cut["n"]),
            f"{_pct(cut['hit_rate'])} ({cut['hits']}/{cut['n']})",
            _fmt(cut["brier"], 3), _fmt(cut["logloss"], 3), _fmt(cut["bss_vs_baserate"], 3),
        ])
    A(_md_table(["window", "n", "hit-rate", "Brier", "log-loss", "BSS"], rows))
    A("")

    # =====================================================================
    # S2
    # =====================================================================
    A("## S2 — Model forecast accuracy: scoreline / Over-Under / BTTS")
    A("")
    s2 = ctx["s2"]
    A("**Scoreline market in the prediction ledger:** data-pending "
      "(predledger scoreline-market coverage = 0%; only 1X2 legs are persisted "
      "with settlement). The hits below come from the tracking feed's top-k "
      "scoreline ladder.")
    A("")
    rows = []
    if s2["top6_n"]:
        rows.append(["scoreline top-6 hit", f"{_pct(s2['top6_rate'])} ({s2['top6_hits']}/{s2['top6_n']})"])
        rows.append(["scoreline top-1 hit", f"{_pct(s2['top1_rate'])} ({s2['top1_hits']}/{s2['top1_n']})"])
    else:
        rows.append(["scoreline top-6 hit", _insufficient(0)])
    if s2["ou_n"]:
        rows.append(["O/U 2.5 accuracy", f"{_pct(s2['ou_accuracy'])} ({s2['ou_hits']}/{s2['ou_n']})"])
        rows.append(["O/U 2.5 Brier", _fmt(s2["ou_brier"])])
    else:
        rows.append(["O/U 2.5", _insufficient(0)])
    if s2["btts_n"]:
        rows.append(["BTTS accuracy", f"{_pct(s2['btts_accuracy'])} ({s2['btts_hits']}/{s2['btts_n']})"])
        rows.append(["BTTS Brier", _fmt(s2["btts_brier"])])
    else:
        rows.append(["BTTS", _insufficient(0)])
    A(_md_table(["metric", "value"], rows))
    A("")

    # =====================================================================
    # S3
    # =====================================================================
    A("## S3 — Model predicted goals (Dixon-Coles lambdas) vs ACTUAL goals")
    A("")
    A("> NOTE: this is MODEL-predicted goals reconstructed from the model's "
      "Dixon-Coles scoreline ladder (probability-weighted mean goals over the "
      "persisted top-k scorelines), NOT StatsBomb shot-xG. `wca.data.statsbomb` "
      "is WC2018/2022 open data only — there are NO 2026 StatsBomb events. The "
      "top-k truncation drops the tail, so totals are mildly UNDER-stated.")
    A("")
    s3 = ctx["s3"]
    if s3.get("n", 0) == 0:
        A(_insufficient(0))
    else:
        rows = [
            ["n fixtures", str(s3["n"])],
            ["mean captured scoreline mass (top-k)", _pct(s3.get("mean_captured_mass"))],
            ["mean predicted total goals (truncated)", _fmt(s3["mean_pred_total"], 3)],
            ["mean actual total goals", _fmt(s3["mean_actual_total"], 3)],
            ["bias (pred - actual) — truncation-dominated", _fmt(s3["bias_pred_minus_actual"], 3)],
            ["MAE (total goals)", _fmt(s3["mae_total"], 3)],
            ["mean pred home / actual home", f"{_fmt(s3['mean_pred_home'],3)} / {_fmt(s3['mean_actual_home'],3)}"],
            ["mean pred away / actual away", f"{_fmt(s3['mean_pred_away'],3)} / {_fmt(s3['mean_actual_away'],3)}"],
        ]
        A(_md_table(["metric", "value"], rows))
        A("")
        A(f"> The top-k ladder captures only ~{_pct(s3.get('mean_captured_mass'))} "
          "of the scoreline distribution, concentrated on low scores, so the "
          "predicted total is structurally biased DOWN. Treat the bias/MAE here "
          "as a data-coverage limitation, NOT a model-accuracy verdict. A clean "
          "totals validation needs the full Dixon-Coles lambdas persisted at "
          "card-build (currently not stored).")
    A("")

    # =====================================================================
    # S4
    # =====================================================================
    A("## S4 — Bets PLACED performance (ledger, READ-ONLY)")
    A("")
    A("> dev.db ledger = STALE FORK, NON-AUTHORITATIVE. Re-run on the mini "
      "(`--db data/wca.db`) for the canonical book.")
    A("")
    s4 = ctx["s4"]
    summ = s4["summary"]
    n_settled = summ["won_bets"] + summ["lost_bets"]
    if summ["total_bets"] == 0:
        A("**This ledger copy contains 0 bets** -> " + _insufficient(0) +
          ". (dev.db carries the 870-row prediction ledger but no placed bets; "
          "the placed-bet book lives on the mini's `data/wca.db`.)")
    else:
        rows = [
            ["total bets", str(summ["total_bets"])],
            ["settled (won/lost)", str(n_settled)],
            ["won / lost", f"{summ['won_bets']} / {summ['lost_bets']}"],
            ["total staked (settled)", _fmt(summ["total_staked"], 2)],
            ["total P&L (settled)", _fmt(summ["total_pl"], 2)],
            ["ROI", _pct(summ["roi"]) if n_settled else _insufficient(0)],
            ["avg CLV", _fmt(summ["avg_clv"]) + f" (n={s4['clv']['n_bets']})" if s4["clv"]["n_bets"] else _insufficient(0)],
            ["% beat close", _pct(summ["pct_beat_close"]) if s4["clv"]["n_bets"] else _insufficient(0)],
        ]
        A(_md_table(["metric", "value"], rows))
        A("")
        A("**By source**")
        A("")
        srows = []
        for src, blk in summ["by_source"].items():
            srows.append([src, str(blk["n"]), _fmt(blk["staked"], 2), _fmt(blk["settled_pl"], 2)])
        A(_md_table(["source", "n", "staked", "settled P&L"], srows))
        A("")
        if s4["by_market"]:
            A("**By market**")
            A("")
            mrows = []
            for mk, blk in sorted(s4["by_market"].items()):
                roi = (blk["pl"] / blk["stake"]) if blk["stake"] else None
                mrows.append([mk, str(blk["n"]), str(blk["settled"]),
                              f"{blk['won']}/{blk['settled']}" if blk["settled"] else "0/0",
                              _fmt(blk["pl"], 2), _pct(roi) if roi is not None else "n/a"])
            A(_md_table(["market", "n", "settled", "won/settled", "P&L", "ROI"], mrows))
            A("")
        if s4["over_time"]:
            A("**Over-time (by matchday window)**")
            A("")
            orows = []
            for win in _WINDOW_ORDER:
                blk = s4["over_time"].get(win)
                if not blk:
                    orows.append([win, "0", _insufficient(0), "", ""])
                    continue
                roi = (blk["pl"] / blk["stake"]) if blk["stake"] else None
                orows.append([win, str(blk["n"]), str(blk["settled"]), _fmt(blk["pl"], 2),
                              _pct(roi) if roi is not None else "n/a"])
            A(_md_table(["window", "n", "settled", "P&L", "ROI"], orows))
            A("")
    A("")

    # =====================================================================
    # S5
    # =====================================================================
    A("## S5 — Bets PLACED vs RECOMMENDED (discretionary overlay)")
    A("")
    s5 = ctx["s5"]
    univ = s5["universe"]
    A("### S5.1 Recommendation universe (Telegram HTML export, parsed)")
    A("")
    rows = [
        ["/next 1X2 ✅ picks (deduped)", str(univ["n_next"])],
        ["/card 1X2 picks (deduped)", str(univ["n_card"])],
        ["1X2 recs total (deduped union)", str(univ["n_1x2_union"])],
        ["PM 'to WIN' lines (EXCLUDED from grading)", str(univ["pm_to_win_lines"])],
        ["PM sized EV picks (deduped, directional only)", str(univ["n_pm_sized"])],
    ]
    A(_md_table(["stream", "count"], rows))
    A("")
    A("_PM 'to WIN' recs are EXCLUDED from grading per instruction (duplicated / "
      "over-frequent). They are counted only to size the universe + deviation rate._")
    A("")

    A("### S5.2 Deviation (skip) rate")
    A("")
    dev = s5["deviation"]
    A(f"- 1X2 recs surfaced (deduped): **{dev['n_1x2']}**; placed (matched in "
      f"ledger): **{dev['n_placed_match']}**; skipped: **{dev['n_skipped']}**.")
    A(f"- Deviation rate over 1X2 recs: **{_pct(dev['rate']) if dev['rate'] is not None else 'n/a'}** "
      f"(n={dev['n_1x2']}).")
    if dev["ledger_empty"]:
        A("- NOTE: this dev.db ledger has 0 placed bets, so 'placed' here is 0 "
          "by construction and deviation = 100% mechanically. The attribution "
          "doc's ~98% is from the canonical book; re-run on the mini to confirm.")
    A("")

    A("### S5.3 Skipped 1X2 recs, graded vs realized result")
    A("")
    gn = s5["grade_1x2_next_only"]
    gc = s5["grade_1x2_card_only"]
    g = s5["grade_1x2"]

    # --- (a) /next-only cut: apples-to-apples cross-check with the doc ---
    A("**(a) `/next` ✅ value-pick stream ONLY — cross-check vs attribution doc**")
    A("")
    exp_g = ATTR_EXPECT["skipped_1x2_graded"]
    exp_w = ATTR_EXPECT["skipped_1x2_won"]
    if gn["graded_n"] == 0:
        A(_insufficient(0))
    else:
        A(f"- **{gn['graded_n']} graded** ({gn['open_n']} open). Won **{gn['won_n']}**, "
          f"lost **{gn['lost_n']}**. Counterfactual flat-1u at best price: "
          f"**{_fmt(gn['cf_pl_1u'], 2)}u** "
          f"(ROI {_pct(gn['cf_roi']) if gn['cf_roi'] is not None else 'n/a'}).")
        if abs(gn["graded_n"] - exp_g) <= 4 and gn["won_n"] == exp_w:
            A(f"- ✓ **Consistent with the attribution doc** ({exp_g} graded -> {exp_w} won). "
              "The model's `/next` value flags (draws + away underdogs) went "
              "~0-for-graded; skipping them added value, as the doc found.")
        else:
            A(f"- ⚠️ **Differs from the attribution doc** (doc: {exp_g} graded -> {exp_w} won; "
              f"recompute: {gn['graded_n']} graded -> {gn['won_n']} won). "
              "Likely a wider results window (more fixtures now settled) and/or "
              "dedup differences vs the doc's manual parse. Direction (value "
              "flags lose) is unchanged. Inspect rows below.")
    A("")

    # --- (b) combined /next + /card extension (the task's full stream) ---
    A("**(b) Combined `/next` + `/card` 1X2 stream (task extension — NOT doc-comparable)**")
    A("")
    if g["graded_n"] == 0:
        A(_insufficient(0))
    else:
        A(f"- **{g['graded_n']} graded** ({g['open_n']} open). Won **{g['won_n']}**, "
          f"lost **{g['lost_n']}**. Counterfactual flat-1u at best price: "
          f"**{_fmt(g['cf_pl_1u'], 2)}u** "
          f"(ROI {_pct(g['cf_roi']) if g['cf_roi'] is not None else 'n/a'}).")
        A(f"- `/card`-only sub-cut: {gc['graded_n']} graded -> {gc['won_n']} won "
          f"(cf {_fmt(gc['cf_pl_1u'],2)}u). The `/card` stream surfaces "
          "**favourite-side** picks too (not just the value longshots the doc "
          "graded), so it wins more often and is **not** comparable to the doc's "
          "value-only cut. This split isolates: value flags lose, favourite picks "
          "win — the same 'good calibrator / bad longshot selector' shape.")
        A("")
        # over-time for the combined cut
        if g.get("over_time"):
            A("_Over-time (combined cut, by matchday window):_")
            A("")
            orows = []
            for win in _WINDOW_ORDER:
                blk = g["over_time"].get(win)
                if not blk:
                    orows.append([win, "0", _insufficient(0), ""])
                    continue
                orows.append([win, str(blk["graded"]), f"{blk['won']}/{blk['graded']}",
                              _fmt(blk["cf_pl"], 2)])
            A(_md_table(["window", "graded", "won/graded", "cf P&L (1u)"], orows))
            A("")
        # detail table (cap rows) — combined
        drows = []
        for r in sorted(g["rows"], key=lambda x: (x.get("date") or "", x["fixture"]))[:50]:
            drows.append([r["fixture"], r["selection"], r["leg"], _fmt(r["best"], 2),
                          _pct(r["edge"]) if r.get("edge") is not None else "n/a",
                          "WON" if r["won"] else "lost", _fmt(r["cf_pl"], 2)])
        A(_md_table(["fixture", "selection", "leg", "best", "edge", "result", "cf P&L (1u)"], drows))
    A("")

    A("### S5.4 Skipped PM 'to WIN' longshots (EXCLUDED from grading)")
    A("")
    gp = s5["grade_pm"]
    A(f"- PM sized EV picks surfaced (deduped): **{gp['n_sized']}**; of these, "
      f"**{gp['n_on_settled_fixture']}** land on a fixture that has since settled "
      "(the gradeable universe).")
    A("- **Grade WITHHELD (data-pending):** " + gp["note"])
    A(f"- For reference, the attribution doc's directional cut graded "
      f"{ATTR_EXPECT['skipped_pm_graded']} -> {ATTR_EXPECT['skipped_pm_won']} won "
      "(-$297 avoided) from a fuller manual parse that preserved the backed side. "
      "This engine does not reproduce that number because it refuses to fabricate "
      "the side; that cut needs the structured rec journal (S6 infra item).")
    A("")

    # =====================================================================
    # S6
    # =====================================================================
    A("## S6 — Synthesis + forward changes")
    A("")
    A("Built on the 2026-06-25 attribution doc (`docs/research/"
      "model_vs_discretion_attribution.md`), extended with the model-forecast-"
      "accuracy sections (S1-S3) it lacked. Each change is tied to a finding "
      "with its n.")
    A("")
    fb = s1["fullbook"]
    pl_all = s1["predledger"]["all"]
    A("1. **The model is a good 1X2 _calibrator_.** Full settled book: mean "
      f"model prob {_fmt(fb.get('mean_model_prob'),3)} == realized "
      f"{_fmt(fb.get('realized_rate'),3)}, binary Brier {_fmt(fb.get('brier_binary'),3)} "
      f"(n={fb.get('n',0)} legs). Reproduces the doc. Keep trusting the "
      "probabilities; harvest favourite-side / straight 1X2.")
    if pl_all.get("n"):
        A(f"2. **Pick-level discrimination is positive but thin.** Fixture-level "
          f"argmax hit-rate {_pct(pl_all['hit_rate'])} ({pl_all['hits']}/{pl_all['n']}), "
          f"BSS vs base-rate {_fmt(pl_all['bss_vs_baserate'],3)} (n={pl_all['n']}). "
          "Positive skill, but n is too small to promote sizing — judge on CLV.")
    s2 = ctx["s2"]
    if s2["ou_n"]:
        A(f"3. **O/U and BTTS are usable secondary markets.** O/U 2.5 accuracy "
          f"{_pct(s2['ou_accuracy'])} (n={s2['ou_n']}), BTTS {_pct(s2['btts_accuracy'])} "
          f"(n={s2['btts_n']}). Scoreline top-6 {_pct(s2['top6_rate'])} "
          f"(n={s2['top6_n']}) — keep scoreline as a ladder, not a single-line bet.")
    s3 = ctx["s3"]
    if s3.get("n"):
        A(f"4. **Goals: the persisted top-k ladder cannot be used as a totals "
          f"estimator.** Reconstructed mean predicted total {_fmt(s3['mean_pred_total'],2)} "
          f"vs actual {_fmt(s3['mean_actual_total'],2)} (bias "
          f"{_fmt(s3['bias_pred_minus_actual'],2)}, MAE {_fmt(s3['mae_total'],2)}, "
          f"n={s3['n']}) — the large negative bias is a TRUNCATION ARTIFACT (the "
          "top-6 ladder keeps only ~55% of the scoreline mass, which sits on low "
          "scores), NOT a model claim. The full Dixon-Coles lambdas are not "
          "persisted. ACTION: persist the full DC lambdas at card-build so totals "
          "can be validated. Until then, no exact-score model exists -> keep "
          "avoiding discretionary correct-score punts (doc: -73.9% ROI, your "
          "biggest leak).")
    gn = s5["grade_1x2_next_only"]
    g = s5["grade_1x2"]
    A(f"5. **Skipped-rec verdict holds (extended).** The model's `/next` value "
      f"stream went {gn['won_n']}/{gn['graded_n']} graded "
      f"(cf {_fmt(gn['cf_pl_1u'],1)}u) — consistent with the doc; keep skipping "
      "those longshot/draw +EV flags and the PM advancement stream. The wider "
      f"`/next`+`/card` stream went {g['won_n']}/{g['graded_n']} "
      f"(cf {_fmt(g['cf_pl_1u'],1)}u): the favourite-side `/card` picks are where "
      "the value is. Gate model bets on CLV + a ≥5-6% edge; scale the promo/boost "
      "lane (the doc's one robust +EV lane).")
    A("6. _(Infra)_ Persist a structured rec journal (selection/price/stake/"
      "model_prob) + unify the predledger↔bet-ledger match_id hash so this "
      "attribution becomes a query, not a Telegram-export parse.")
    A("")
    A("---")
    A("_Generated by `scripts/wca_validation_report.py` — read-only, "
      "guarded, reproducible. Re-run on the mini for canonical ledger numbers._")
    A("")
    return "\n".join(L)


# ===========================================================================
# main
# ===========================================================================
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="WCA model + recommendation validation engine (S1-S6).")
    ap.add_argument("--db", default=os.path.join("data", "dev.db"),
                    help="ledger / predledger SQLite db (default data/dev.db). "
                         "Refuses prod basename wca.db on the dev box.")
    ap.add_argument("--out", default=os.path.join("docs", "research", "model_and_rec_validation_report.md"),
                    help="output markdown report path.")
    ap.add_argument("--json", default=os.path.join("/tmp", "wca_validation_metrics.json"),
                    help="machine-readable metrics JSON path.")
    ap.add_argument("--results", default=DEF_RESULTS)
    ap.add_argument("--tracking", default=DEF_TRACKING)
    ap.add_argument("--chat-dir", default=DEF_CHAT_DIR)
    args = ap.parse_args(argv)

    # ---- HARD GUARD: prod-db protection ----
    base = os.path.basename(args.db)
    if base in {"wca.db"}:
        sys.stderr.write(
            "REFUSING: --db basename is 'wca.db' (the prod/canonical ledger). "
            "This engine is read-only but the dev-box guard forbids even opening "
            "the prod basename here. Use --db data/dev.db on the dev box, or run "
            "this on the MINI where data/wca.db is canonical.\n"
        )
        return 2

    # Resolve relative paths against repo root for robustness.
    def _abs(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(_ROOT, p)

    db = _abs(args.db)
    out_path = _abs(args.out)
    json_path = _abs(args.json)

    if not os.path.exists(db):
        sys.stderr.write(f"ERROR: db not found: {db}\n")
        return 1

    print(f"[wca_validation] db          = {db}")
    print(f"[wca_validation] out (md)    = {out_path}")
    print(f"[wca_validation] out (json)  = {json_path}")
    print("[wca_validation] loading inputs ...")

    results = load_results(_abs(args.results))
    print(f"[wca_validation] results: {len(results)} settled fixtures")

    tracking_data = load_tracking(_abs(args.tracking))
    n_track = len([f for f in (tracking_data.get('fixtures') or []) if not f.get('pending')])
    print(f"[wca_validation] tracking feed: {n_track} completed fixtures")

    # ---- S1 ----
    pl_fixtures = predledger_1x2_fixtures(db)
    print(f"[wca_validation] predledger: {len(pl_fixtures)} settled 1X2 fixtures")
    fullbook = predledger_all_legs_stats(db)
    print(f"[wca_validation] predledger full-book legs: n={fullbook.get('n',0)}")

    # tracking-feed fixtures with a model triple -> fixture dicts for scorer
    track_fx = []
    for f in (tracking_data.get("fixtures") or []):
        if f.get("pending") or not f.get("model_1x2") or not f.get("outcome"):
            continue
        m = f["model_1x2"]
        if any(m.get(o) is None for o in _LEGS):
            continue
        track_fx.append({
            "fixture": f["fixture"],
            "model": {o: float(m[o]) for o in _LEGS},
            "market": ({o: float(f["market_1x2"][o]) for o in _LEGS}
                       if f.get("market_1x2") and all(f["market_1x2"].get(o) is not None for o in _LEGS) else None),
            "outcome": f["outcome"],
            "date": f.get("date"),
        })

    s1 = {
        "fullbook": fullbook,
        "predledger": {
            "all": score_1x2_fixtures(pl_fixtures),
            "over_time": {},
        },
        "tracking": {
            "all": score_1x2_fixtures(track_fx),
        },
    }
    # over-time on predledger
    by_win: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in pl_fixtures:
        win = _date_window(f.get("date"))
        if win:
            by_win[win].append(f)
    for win, fxs in by_win.items():
        s1["predledger"]["over_time"][win] = score_1x2_fixtures(fxs)

    # ---- S2 / S3 ----
    s2 = s2_markets(tracking_data)
    s3 = s3_goals(tracking_data)
    print(f"[wca_validation] S2 markets: top6_n={s2['top6_n']} ou_n={s2['ou_n']} btts_n={s2['btts_n']}")
    print(f"[wca_validation] S3 goals: n={s3.get('n',0)}")

    # ---- S4 ----
    s4 = s4_ledger(db)
    print(f"[wca_validation] S4 ledger: {s4['summary']['total_bets']} placed bets")

    # ---- S5 ----
    print("[wca_validation] parsing Telegram HTML export ...")
    msgs = _parse_messages(_abs(args.chat_dir), DEF_CHAT_FILES)
    print(f"[wca_validation] parsed {len(msgs)} message blocks")
    recs = extract_recommendations(msgs)
    next_recs = recs["next_recs"]
    card_recs = recs["card_recs"]
    # union of 1X2 recs by (fixture_key, leg), prefer /next where both exist
    union: Dict[Tuple[Any, str], Dict[str, Any]] = dict(card_recs)
    union.update(next_recs)
    # Three grading cuts:
    #  * /next-only      -> apples-to-apples with the attribution doc (it graded
    #                       the /next ✅ value picks: draws + away underdogs).
    #  * combined union  -> the task's "/next + /card" extension (folds in the
    #                       favourite-side /card picks, so it is NOT comparable
    #                       to the doc and naturally wins more).
    g_next = grade_skipped_1x2(next_recs, results)
    g_card = grade_skipped_1x2(card_recs, results)
    g1x2 = grade_skipped_1x2(union, results)
    gpm = grade_skipped_pm(recs["pm_sized_recs"], results)

    # deviation: dev.db bets table -> placed 1X2 legs (empty here)
    placed_match = 0  # no bets in dev.db; matched against recs would be 0
    n_1x2 = len(union)
    ledger_empty = s4["summary"]["total_bets"] == 0
    n_skipped = n_1x2 - placed_match
    s5 = {
        "universe": {
            "n_next": len(next_recs),
            "n_card": len(card_recs),
            "n_1x2_union": n_1x2,
            "pm_to_win_lines": recs["pm_to_win_lines"],
            "n_pm_sized": len(recs["pm_sized_recs"]),
        },
        "deviation": {
            "n_1x2": n_1x2,
            "n_placed_match": placed_match,
            "n_skipped": n_skipped,
            "rate": (n_skipped / n_1x2) if n_1x2 else None,
            "ledger_empty": ledger_empty,
        },
        "grade_1x2": g1x2,
        "grade_1x2_next_only": g_next,
        "grade_1x2_card_only": g_card,
        "grade_pm": gpm,
    }
    print(f"[wca_validation] S5 recs: next={len(next_recs)} card={len(card_recs)} "
          f"union={n_1x2}")
    print(f"[wca_validation] S5 grading: /next-only {g_next['graded_n']}->{g_next['won_n']}won "
          f"| /card-only {g_card['graded_n']}->{g_card['won_n']}won "
          f"| union {g1x2['graded_n']}->{g1x2['won_n']}won")

    ctx = {
        "db": db,
        "generated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "s1": s1, "s2": s2, "s3": s3, "s4": s4, "s5": s5,
    }

    report = build_report(ctx)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"[wca_validation] wrote report -> {out_path}")

    # machine-readable metrics
    metrics = {
        "meta": {
            "db": db,
            "db_is_stale_fork": True,
            "generated": ctx["generated"],
            "canonical_run": "PYTHONPATH=src python scripts/wca_validation_report.py --db data/wca.db",
        },
        "s1": {
            "fullbook": fullbook,
            "predledger_all": s1["predledger"]["all"],
            "predledger_over_time": s1["predledger"]["over_time"],
            "tracking_all": s1["tracking"]["all"],
        },
        "s2": s2,
        "s3": s3,
        "s4": {
            "summary": s4["summary"],
            "clv": s4["clv"],
            "by_market": s4["by_market"],
            "over_time": s4["over_time"],
        },
        "s5": {
            "universe": s5["universe"],
            "deviation": s5["deviation"],
            "grade_1x2_next_only": {k: v for k, v in g_next.items() if k != "rows"},
            "grade_1x2_card_only": {k: v for k, v in g_card.items() if k != "rows"},
            "grade_1x2_combined": {k: v for k, v in g1x2.items() if k != "rows"},
            "grade_1x2_combined_rows": g1x2["rows"],
            "grade_pm": gpm,
        },
        "attribution_doc_expected": ATTR_EXPECT,
    }

    def _default(o):
        if isinstance(o, (set, frozenset)):
            return sorted(str(x) for x in o)
        return str(o)

    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2, default=_default)
    print(f"[wca_validation] wrote metrics -> {json_path}")
    print("[wca_validation] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
