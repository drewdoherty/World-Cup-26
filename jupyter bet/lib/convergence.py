"""Pre-match convergence: exact time-window marks + continuous curves,
with a hard look-ahead guard.

DATA HONESTY: every emitted row carries ``basis`` ∈
  observed       a real snapshot/trade existed inside the mark's tolerance
  reconstructed  interpolated between two real observations that bracket the
                 mark (method noted in ``basis_note``)
  unavailable    nothing usable — row kept with null value so coverage gaps
                 are visible, never papered over.

LOOK-AHEAD: builders take the mark timestamp as the hard cutoff; any input
row with ts > mark is excluded before computation. Closing values only
appear in columns explicitly suffixed ``_expost`` and only when
Params.allow_expost is True (benchmark cells).
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Sequence, Tuple

import polars as pl

import lib.bootstrap  # noqa: F401


def mark_times(kickoff_utc: dt.datetime,
               hours: Sequence[int]) -> Dict[int, dt.datetime]:
    """{hours_before: absolute UTC mark}. 0h == kickoff."""
    if kickoff_utc.tzinfo is None:
        raise ValueError("kickoff must be tz-aware")
    return {h: kickoff_utc - dt.timedelta(hours=h) for h in hours}


def value_at_mark(series: pl.DataFrame, mark: dt.datetime, *,
                  ts_col: str = "ts_utc", val_col: str = "price",
                  tolerance_min: int = 30,
                  allow_interpolation: bool = True) -> Dict[str, object]:
    """Value of a time series AT a mark, using only rows with ts ≤ mark
    plus (for interpolation only) the first row after it.

    Returns {value, basis, basis_note, obs_ts}. Never raises on gaps."""
    if series.is_empty():
        return {"value": None, "basis": "unavailable",
                "basis_note": "no observations at all", "obs_ts": None}
    s = series.sort(ts_col)
    before = s.filter(pl.col(ts_col) <= mark)
    tol = dt.timedelta(minutes=tolerance_min)

    if not before.is_empty():
        last = before.tail(1)
        ts_last = last[ts_col][0]
        if mark - ts_last <= tol:
            return {"value": float(last[val_col][0]), "basis": "observed",
                    "basis_note": f"snapshot {int((mark - ts_last).total_seconds() // 60)}min before mark",
                    "obs_ts": ts_last}
    if allow_interpolation and not before.is_empty():
        after = s.filter(pl.col(ts_col) > mark)
        if not after.is_empty():
            t0, v0 = before.tail(1)[ts_col][0], float(before.tail(1)[val_col][0])
            t1, v1 = after.head(1)[ts_col][0], float(after.head(1)[val_col][0])
            span = (t1 - t0).total_seconds()
            if 0 < span <= 24 * 3600:  # never bridge > 24h gaps
                w = (mark - t0).total_seconds() / span
                return {"value": v0 + w * (v1 - v0), "basis": "reconstructed",
                        "basis_note": f"linear between obs {t0:%m-%d %H:%M} and {t1:%m-%d %H:%M} UTC",
                        "obs_ts": None}
    return {"value": None, "basis": "unavailable",
            "basis_note": "no observation within tolerance and no bracketing pair ≤24h apart",
            "obs_ts": None}


def guard_lookahead(df: pl.LazyFrame, asof: dt.datetime, *,
                    ts_col: str = "ts_utc") -> pl.LazyFrame:
    """Drop every row after `asof`. ALL historical decision paths route
    through this before touching prices. Unit-tested."""
    return df.filter(pl.col(ts_col) <= asof)


def trade_flow_metrics(trades: pl.DataFrame, *, bucket: str = "1h",
                       ts_col: str = "ts_utc") -> pl.DataFrame:
    """Per-bucket volume, VWAP, trade count, velocity (Δvol) and acceleration
    (Δvelocity) from a trades frame (price/size/usd columns)."""
    if trades.is_empty():
        return pl.DataFrame(schema={ts_col: pl.Datetime("us", "UTC"),
                                    "usd_vol": pl.Float64, "n_trades": pl.UInt32,
                                    "vwap": pl.Float64, "velocity": pl.Float64,
                                    "acceleration": pl.Float64,
                                    "cum_usd_vol": pl.Float64})
    out = (trades.sort(ts_col)
           .group_by_dynamic(ts_col, every=bucket)
           .agg(pl.col("usd").sum().alias("usd_vol"),
                pl.len().alias("n_trades"),
                ((pl.col("price") * pl.col("size")).sum()
                 / pl.col("size").sum()).alias("vwap"))
           .with_columns(pl.col("usd_vol").diff().alias("velocity"))
           .with_columns(pl.col("velocity").diff().alias("acceleration"))
           .with_columns(pl.col("usd_vol").cum_sum().alias("cum_usd_vol")))
    return out


def realized_vol(prices: pl.DataFrame, *, ts_col: str = "ts_utc",
                 val_col: str = "price", bucket: str = "1h") -> pl.DataFrame:
    """Per-bucket std of price changes + max jump (suspension/news detector)."""
    if prices.is_empty():
        return pl.DataFrame(schema={ts_col: pl.Datetime("us", "UTC"),
                                    "vol": pl.Float64, "max_jump": pl.Float64})
    return (prices.sort(ts_col)
            .with_columns(pl.col(val_col).diff().alias("dp"))
            .group_by_dynamic(ts_col, every=bucket)
            .agg(pl.col("dp").std().alias("vol"),
                 pl.col("dp").abs().max().alias("max_jump")))


def convergence_table(marks: Dict[int, dt.datetime],
                      price_series: pl.DataFrame,
                      fair_series: Optional[pl.DataFrame] = None, *,
                      tolerance_min: int = 30,
                      closing_expost: Optional[float] = None,
                      allow_expost: bool = False) -> pl.DataFrame:
    """One row per mark: market price, fair value, edge, basis labels; the
    closing column only exists when allow_expost=True and is suffixed
    ``_expost`` (benchmark only, never an input)."""
    rows: List[Dict[str, object]] = []
    for h, mark in sorted(marks.items(), reverse=True):
        px = value_at_mark(price_series, mark, tolerance_min=tolerance_min)
        row: Dict[str, object] = {
            "hours_out": h, "mark_utc": mark,
            "market_price": px["value"], "price_basis": px["basis"],
            "price_note": px["basis_note"],
        }
        if fair_series is not None:
            fv = value_at_mark(fair_series, mark, tolerance_min=tolerance_min)
            row["fair_value"] = fv["value"]
            row["fair_basis"] = fv["basis"]
            row["edge"] = (fv["value"] - px["value"]
                           if None not in (fv["value"], px["value"]) else None)
        if allow_expost:
            row["closing_price_expost"] = closing_expost
            row["convergence_error_expost"] = (
                abs(px["value"] - closing_expost)
                if None not in (px["value"], closing_expost) else None)
        rows.append(row)
    return pl.DataFrame(rows)
