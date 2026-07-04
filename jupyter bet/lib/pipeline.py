"""The decision pipeline, exposed stage-by-stage — PRODUCTION functions,
notebook-visible intermediates.

Production path (scripts/wca_betrecs.py, runs on the mini + CI):
    feeds (model preds, advancement, promos, scores/markets)
      → pool resolution  (_resolve_sportsbook_pool / _pm_pool — combined
                          £3,000 ± realised P&L bankroll, ¼-Kelly)
      → build_match_singles      (blend vs de-vigged consensus, edge gate,
                                  staleness gates, promo tags)
      → build_event_props        (calibrated corners/cards — withheld unless
                                  fresh real prices exist)
      → build_advancement_futures(PM advancement vs model sim, fee-adjusted,
                                  PM-blind + 6h staleness guards)
      → governance caps → actionable / withheld split → site feed.

:func:`run_stages` calls those SAME functions with the SAME inputs the
production feed builder reads, capturing each stage's inputs/outputs +
accept/reject reasons; :func:`decision_trace` explains one candidate
end-to-end; :func:`parity_check` proves the notebook path reproduces the
shipped ``site/bet_recs.json`` rec-for-rec (IDs, stakes, actions).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import polars as pl

import lib.bootstrap as bt
import wca_betrecs as br


def load_feeds() -> Dict[str, Any]:
    """The exact JSON feeds production reads (same default paths as
    wca_betrecs.main), with ages (staleness inputs)."""
    feeds: Dict[str, Any] = {}
    for name, path in [
        ("model_predictions", bt.REPO_ROOT / "data" / "model_predictions.json"),
        ("advancement", bt.ADVANCEMENT_JSON),
        ("promos", bt.PROMOS_JSON),
        ("scores_markets", bt.SCORES_MARKETS_JSON),
        ("prop_calibration", bt.REPO_ROOT / "data" / "prop_calibration.json"),
        ("arb", bt.REPO_ROOT / "site" / "arb_data.json"),
        ("exposure", bt.REPO_ROOT / "site" / "exposure_dashboard.json"),
        ("bet_recs_shipped", bt.BET_RECS_JSON),
    ]:
        data, age = br._load_json(str(path), default={})
        feeds[name] = {"data": data, "age_secs": age, "path": str(path)}
    return feeds


def resolve_pools(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Stage 0: bankroll pools exactly as production resolves them.
    On the dev box the ledger is a stale copy — flagged in the output."""
    db = db_path or str(bt.DEV_WCA_DB)
    sb = br._resolve_sportsbook_pool(db)
    pnl = br._pm_realised_pnl_usd(db)
    pm = br._pm_pool(br.pm_rule.pm_bankroll_usd(pnl or 0.0),
                     source="ledger" if pnl is not None else "base")
    return {"sportsbook": sb, "pm": pm, "pm_realised_pnl_usd": pnl,
            "ledger_db": db,
            "ledger_caveat": "dev-box wca.db is a STALE copy; canonical "
                             "ledger lives on the mini only"}


def run_stages(feeds: Dict[str, Any], pools: Dict[str, Any]) -> Dict[str, Any]:
    """Run the three production builders exactly as main() wires them."""
    preds = (feeds["model_predictions"]["data"] or {}).get("fixtures") or []
    model_age = feeds["model_predictions"]["age_secs"]
    exposure_raw = feeds["exposure"]["data"] or {}
    blind_spots = [str(b) for b in (exposure_raw.get("blind_spots") or [])
                   if isinstance(b, str)]
    exposure = br._open_exposure(pools["ledger_db"], exposure_raw)
    open_fixtures: set = set()   # mirrors production main() (ledger TODO)
    singles_act, singles_wh = br.build_match_singles(
        preds, pools["sportsbook"], open_fixtures, blind_spots,
        feeds["promos"]["data"] or {}, model_age)
    props_act, props_wh = br.build_event_props(
        feeds["prop_calibration"]["data"] or {}, preds, pools["sportsbook"],
        feeds["prop_calibration"]["age_secs"], model_age)
    adv_act, adv_wh = br.build_advancement_futures(
        feeds["advancement"]["data"] or {}, pools["pm"],
        feeds["advancement"]["age_secs"],
        feeds["scores_markets"]["data"] or {})
    return {
        "exposure": exposure,
        "singles": {"actionable": singles_act, "withheld": singles_wh},
        "props": {"actionable": props_act, "withheld": props_wh},
        "advancement": {"actionable": adv_act, "withheld": adv_wh},
    }


def stage_frames(stages: Dict[str, Any]) -> Dict[str, pl.DataFrame]:
    """Polars views of each stage output for profiling/inspection."""
    out: Dict[str, pl.DataFrame] = {}
    for family in ("singles", "props", "advancement"):
        for status in ("actionable", "withheld"):
            rows = stages[family][status]
            out[f"{family}_{status}"] = (
                pl.DataFrame([_flat(r) for r in rows], infer_schema_length=None)
                if rows else pl.DataFrame())
    return out


def _flat(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in rec.items()}


def funnel(stages: Dict[str, Any]) -> pl.DataFrame:
    """Accepted/rejected counts per stage with the reject-reason breakdown."""
    rows: List[Dict[str, Any]] = []
    for family in ("singles", "props", "advancement"):
        act = stages[family]["actionable"]
        wh = stages[family]["withheld"]
        reasons: Dict[str, int] = {}
        for r in wh:
            key = (r.get("reason") or r.get("action") or "unspecified")[:80]
            reasons[key] = reasons.get(key, 0) + 1
        rows.append({"stage": family, "actionable": len(act),
                     "withheld": len(wh),
                     "top_reject_reasons": json.dumps(dict(sorted(
                         reasons.items(), key=lambda kv: -kv[1])[:5]))})
    return pl.DataFrame(rows)


def decision_trace(candidate: Dict[str, Any]) -> pl.DataFrame:
    """One candidate's full story: every field production attached at each
    step, in evaluation order, with the formula that produced it."""
    order = [
        ("fixture/selection", ["fixture", "selection", "market", "stage", "team"]),
        ("model input", ["model_prob", "model", "p_advance", "fair"]),
        ("market input", ["market_prob", "price", "decimal_odds", "pm_price", "mkt"]),
        ("edge", ["edge", "edge_pp", "net_edge"]),
        ("fee", ["fee", "fee_adj"]),
        ("EV", ["ev", "net_ev", "ev_per_dollar"]),
        ("stake (¼-Kelly, capped)", ["stake", "stake_usd", "stake_gbp", "kelly"]),
        ("governance", ["action", "reason", "cut_reason", "withheld_reason",
                        "staleness", "age_secs"]),
    ]
    rows = []
    for step, keys in order:
        for k in keys:
            if k in candidate and candidate[k] not in (None, ""):
                rows.append({"step": step, "field": k,
                             "value": str(candidate[k])})
    for k, v in candidate.items():
        if not any(k in ks for _, ks in order) and v not in (None, ""):
            rows.append({"step": "other", "field": k, "value": str(v)[:120]})
    return pl.DataFrame(rows)


def parity_check(stages: Dict[str, Any],
                 shipped: Dict[str, Any]) -> Dict[str, Any]:
    """Compare notebook-built recs against the shipped site/bet_recs.json.
    Exact-match on IDs; per-field diff on stake/action for shared IDs.
    NOTE: parity holds only when the notebook runs against the same feed
    snapshots the shipped file was built from — a fresher feed on either
    side shows up here as an honest diff, not an error."""
    ours: Dict[str, Dict[str, Any]] = {}
    for family in ("singles", "props", "advancement"):
        for r in stages[family]["actionable"]:
            ours[str(r.get("id"))] = r
    theirs = {str(r.get("id")): r
              for key in ("match_singles", "event_props", "advancement_futures")
              for r in (shipped.get(key) or [])}
    shared = sorted(set(ours) & set(theirs))
    diffs = []
    for rid in shared:
        for f in ("stake", "action", "edge", "price"):
            a, b = ours[rid].get(f), theirs[rid].get(f)
            if a != b:
                diffs.append({"id": rid, "field": f, "notebook": a, "shipped": b})
    return {"n_notebook": len(ours), "n_shipped": len(theirs),
            "n_shared_ids": len(shared),
            "only_notebook": sorted(set(ours) - set(theirs))[:20],
            "only_shipped": sorted(set(theirs) - set(ours))[:20],
            "field_diffs": diffs}
