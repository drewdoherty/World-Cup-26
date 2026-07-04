"""Parameter stress testing over gold candidate datasets.

A *candidate* row is a would-be bet with everything needed to re-decide it
under different Params: fair_p, market price/quote fields (spread, depth,
staleness), match_confidence, kickoff, plus — where the market has settled —
the REAL outcome label (from data/processed/wc2026_results.json; never
simulated). Sweeps re-run the SAME accept logic per Params point:

    edge_raw  = fair_p − mid
    fill      = mid + slip·half-spread ;  edge_net = fair_p − fill − fee(fill)
    gates     : edge_raw ≥ min_edge_raw, edge_net ≥ min_edge_net,
                spread ≤ max_spread, depth ≥ min_depth_usd,
                staleness ≤ staleness_max_s, confidence ≥ min_match_confidence
    stake     = production fractional-Kelly (capped)

Reported per sweep point: n_candidates, n_accepted, per-gate rejection
counts, total EV, exposure; where labels exist — realized P&L, ROI, hit
rate, max drawdown, Brier, log-loss (with n stated). Nothing is reported
from unsettled rows.
"""
from __future__ import annotations

import dataclasses
import itertools
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

import polars as pl

import lib.bootstrap  # noqa: F401
from lib.config import Params
from lib import fairvalue as fv

GATES = ("edge_raw", "edge_net", "spread", "depth", "staleness", "confidence")

CANDIDATE_COLS = {
    "candidate_id": pl.Utf8, "event_id": pl.Utf8, "market_type": pl.Utf8,
    "outcome": pl.Utf8, "fair_p": pl.Float64, "mid": pl.Float64,
    "spread": pl.Float64, "depth_usd": pl.Float64, "staleness_s": pl.Float64,
    "match_confidence": pl.Float64, "settled": pl.Boolean,
    "won": pl.Boolean,  # only meaningful when settled — REAL results only
}


def decide_one(c: Dict[str, Any], p: Params, bankroll: float) -> Dict[str, Any]:
    """Re-decide one candidate under Params p. Returns gates hit + stake."""
    mid = c["mid"]
    fill = mid + p.slippage_frac_of_spread * (c["spread"] / 2 if c["spread"] else 0)
    fee = fv.pm_fee(fill, p.pm_taker_fee_coeff * (1 if p.pm_fee_rate else 0)) \
        if p.pm_fee_rate else fv.pm_fee(fill, 0.0)
    edge_raw = c["fair_p"] - mid
    edge_net = c["fair_p"] - fill - fee
    fails = []
    if edge_raw < p.min_edge_raw:
        fails.append("edge_raw")
    if edge_net < p.min_edge_net:
        fails.append("edge_net")
    if c.get("spread") is not None and c["spread"] > p.max_spread:
        fails.append("spread")
    if c.get("depth_usd") is not None and c["depth_usd"] < p.min_depth_usd:
        fails.append("depth")
    if c.get("staleness_s") is not None and c["staleness_s"] > p.staleness_max_s:
        fails.append("staleness")
    if c.get("match_confidence") is not None and \
            c["match_confidence"] < p.min_match_confidence:
        fails.append("confidence")
    stake = 0.0
    if not fails and 0 < fill < 1:
        stake = fv.kelly_stake(c["fair_p"], fill, bankroll,
                               fraction=p.kelly_fraction,
                               cap_frac=min(p.stake_cap_usd / bankroll, 1.0)
                               if bankroll > 0 else 0.0)
        stake = min(stake, p.stake_cap_usd)
    return {"accepted": not fails, "fails": fails, "fill": fill,
            "edge_raw": edge_raw, "edge_net": edge_net, "stake": stake}


def evaluate(candidates: pl.DataFrame, p: Params,
             bankroll: float = 3990.0) -> Dict[str, Any]:
    """Run the gate/size logic over all candidates; aggregate honestly."""
    n = candidates.height
    gate_fails = {g: 0 for g in GATES}
    accepted: List[Dict[str, Any]] = []
    pnl_series: List[float] = []
    briers: List[float] = []
    loglosses: List[float] = []
    total_ev = 0.0
    exposure = 0.0
    for c in candidates.to_dicts():
        d = decide_one(c, p, bankroll)
        for g in d["fails"]:
            gate_fails[g] += 1
        if not d["accepted"]:
            continue
        accepted.append({**c, **d})
        exposure += d["stake"]
        total_ev += d["stake"] * fv.ev_per_dollar(c["fair_p"], d["fill"])
        if c.get("settled"):
            won = bool(c.get("won"))
            payout = d["stake"] / d["fill"] if won else 0.0
            pnl_series.append(payout - d["stake"])
            briers.append((c["fair_p"] - (1.0 if won else 0.0)) ** 2)
            q = min(max(c["fair_p"], 1e-9), 1 - 1e-9)
            loglosses.append(-(math.log(q) if won else math.log(1 - q)))
    settled_n = len(pnl_series)
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in pnl_series:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    turnover = sum(a["stake"] for a in accepted if a.get("settled"))
    return {
        "n_candidates": n, "n_accepted": len(accepted),
        **{f"reject_{g}": v for g, v in gate_fails.items()},
        "total_stake": round(exposure, 2), "total_ev": round(total_ev, 2),
        "settled_n": settled_n,
        "realized_pnl": round(sum(pnl_series), 2) if settled_n else None,
        "turnover_settled": round(turnover, 2) if settled_n else None,
        "roi": round(sum(pnl_series) / turnover, 4) if turnover > 0 else None,
        "hit_rate": round(sum(1 for x in pnl_series if x > 0) / settled_n, 3)
        if settled_n else None,
        "max_drawdown": round(mdd, 2) if settled_n else None,
        "brier": round(sum(briers) / settled_n, 4) if settled_n else None,
        "log_loss": round(sum(loglosses) / settled_n, 4) if settled_n else None,
    }


def sweep_one(candidates: pl.DataFrame, base: Params, param: str,
              values: Iterable[Any], bankroll: float = 3990.0) -> pl.DataFrame:
    """One-at-a-time sweep of a single parameter."""
    rows = []
    for v in values:
        p = dataclasses.replace(base, **{param: v})
        rows.append({"param": param, "value": float(v),
                     **evaluate(candidates, p, bankroll)})
    return pl.DataFrame(rows)


def sweep_grid(candidates: pl.DataFrame, base: Params,
               grid: Dict[str, Sequence[Any]],
               bankroll: float = 3990.0) -> pl.DataFrame:
    """Full cartesian grid over 2+ parameters (keep it small — n_points
    printed by the notebook before running)."""
    keys = sorted(grid)
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        p = dataclasses.replace(base, **dict(zip(keys, combo)))
        rows.append({**{k: float(v) for k, v in zip(keys, combo)},
                     **evaluate(candidates, p, bankroll)})
    return pl.DataFrame(rows)
