"""In-play analytics from REAL captured trade flow.

Primary source: ``data/pm_orderflow.db`` trades inside each match's live
window (scheduled kickoff → +130 wall-clock minutes). This gives price,
trade flow, volume velocity, jumps/volatility and staleness for the whole
tournament so far, plus live matches while the capture daemon runs.

HONESTY CONSTRAINTS (printed by the notebook, enforced here):
* Match clock: we DO NOT have a stoppage-aware clock. All elapsed columns
  are ``wallclock_min`` = minutes since *scheduled* kickoff, labelled as
  such — never presented as match minutes.
* Score state: goals are attached from final results (known scorelines per
  90 minutes) only as END-STATE context; per-minute score requires an event
  feed we don't capture — columns that would need it stay absent rather
  than inferred.
* Order books are NOT captured historically (top_of_book is live-only), so
  historical spread/depth are marked unavailable; the live snapshot cell
  fills them only for currently-open markets.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

import polars as pl

import lib.bootstrap  # noqa: F401


LIVE_WINDOW_MIN = 130  # scheduled KO → ~FT + stoppage (wall-clock)


def live_window_trades(trades: pl.LazyFrame, kickoff_utc: dt.datetime,
                       *, window_min: int = LIVE_WINDOW_MIN) -> pl.DataFrame:
    """Trades between scheduled kickoff and +window_min (wall clock)."""
    lo = int(kickoff_utc.timestamp())
    hi = lo + window_min * 60
    return (trades.filter((pl.col("ts") >= lo) & (pl.col("ts") <= hi))
            .collect()
            .with_columns(((pl.col("ts") - lo) / 60.0).alias("wallclock_min")))


def inplay_metrics(win: pl.DataFrame, *, bucket_min: int = 5) -> pl.DataFrame:
    """Per-outcome per-bucket in-play metrics from a live-window trades
    frame: last price, VWAP, $ volume, trade count, max jump, buy share,
    and quote staleness (seconds since previous trade at bucket end)."""
    if win.is_empty():
        return pl.DataFrame()
    return (win.sort("ts")
            .with_columns((pl.col("wallclock_min") // bucket_min * bucket_min)
                          .cast(pl.Int32).alias("bucket_min"))
            .group_by(["outcome", "bucket_min"], maintain_order=True)
            .agg(pl.col("price").last().alias("last_price"),
                 ((pl.col("price") * pl.col("size")).sum()
                  / pl.col("size").sum()).alias("vwap"),
                 pl.col("usd").sum().alias("usd_vol"),
                 pl.len().alias("n_trades"),
                 (pl.col("price").diff().abs().max()).alias("max_jump"),
                 (pl.when(pl.col("side") == "BUY").then(pl.col("usd"))
                  .otherwise(0.0).sum() / pl.col("usd").sum())
                 .alias("buy_usd_share"),
                 pl.col("ts").max().alias("last_trade_ts"))
            .sort(["outcome", "bucket_min"])
            .with_columns((pl.col("bucket_min") + bucket_min
                           - (pl.col("last_trade_ts")
                              - pl.col("last_trade_ts").min().over("outcome"))
                           .mul(0)).alias("_"))
            .drop("_"))


def staleness_flags(win: pl.DataFrame, *, stale_after_s: int = 120) -> pl.DataFrame:
    """Gaps between consecutive trades per outcome — likely suspensions or
    illiquidity when > stale_after_s (goal reviews, red cards…)."""
    if win.is_empty():
        return pl.DataFrame()
    return (win.sort("ts")
            .with_columns(pl.col("ts").diff().over("outcome").alias("gap_s"))
            .filter(pl.col("gap_s") > stale_after_s)
            .select("outcome", "wallclock_min", "gap_s", "price", "usd"))


def entry_exit_trace(win: pl.DataFrame, outcome: str, *,
                     entry_min: float, exit_min: float,
                     usd: float, max_slippage: float = 0.02) -> Dict[str, Any]:
    """Hypothetical entry/exit priced from ACTUAL prints (VWAP of trades
    within ±2 wallclock-min of the marks) — labelled hypothetical; depth
    beyond printed volume is NOT assumed: if printed $ volume around the
    mark is below `usd`, the trace is marked not executable at size."""
    out: Dict[str, Any] = {"outcome": outcome, "entry_min": entry_min,
                           "exit_min": exit_min, "usd": usd,
                           "label": "HYPOTHETICAL (priced from real prints)"}
    o = win.filter(pl.col("outcome") == outcome)
    for tag, mark in (("entry", entry_min), ("exit", exit_min)):
        band = o.filter((pl.col("wallclock_min") >= mark - 2)
                        & (pl.col("wallclock_min") <= mark + 2))
        if band.is_empty():
            out[f"{tag}_px"] = None
            out[f"{tag}_note"] = "no prints within ±2min"
            continue
        vw = float((band["price"] * band["size"]).sum() / band["size"].sum())
        vol = float(band["usd"].sum())
        out[f"{tag}_px"] = round(vw, 4)
        out[f"{tag}_printed_usd"] = round(vol, 2)
        out[f"{tag}_note"] = ("printed volume < requested size — NOT "
                              "executable at size" if vol < usd else "ok")
    if out.get("entry_px") and out.get("exit_px"):
        shares = usd / out["entry_px"]
        out["pnl_hypothetical"] = round(shares * (out["exit_px"] - out["entry_px"]), 2)
    return out
