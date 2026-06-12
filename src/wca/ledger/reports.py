"""Reporting and analytics for the World Cup Alpha bet ledger.

All functions take a ``db_path`` argument and return either a
:class:`pandas.DataFrame` or a plain :class:`dict`.  They are deliberately
stateless: call them whenever you want a fresh snapshot.

Metrics implemented
-------------------
Bankroll curve
    Cumulative P&L over time, starting from the sum of all bankroll deposits.

Open exposure
    Unsettled bets and their total stake-at-risk.

CLV report
    Per-bet closing-line value and aggregate statistics.  The *closing-line
    value* (CLV) is the primary quality KPI for this project: consistently
    positive CLV indicates that bets are placed at prices better than the
    efficient market consensus at kick-off.

Calibration report
    Bets binned by model probability; observed win rate per bin; Brier score
    for *both* the model probability and the de-vigged market probability.
    This lets us compare model accuracy against the bookmaker's own (margined)
    implied forecast.

    Brier score (Brier, 1950): ``BS = (1/N) sum_i (p_i - o_i)^2`` where
    ``o_i in {0, 1}`` is the outcome.  Lower is better; a perfect forecaster
    scores 0.  A random forecast of 0.5 on binary outcomes scores 0.25.

References
----------
- Brier, G. W. (1950). "Verification of forecasts expressed in terms of
  probability". *Monthly Weather Review* 78(1):1-3.
- Good, I. J. (1952). "Rational decisions". *Journal of the Royal Statistical
  Society B* 14(1):107-114.  (Log-loss / cross-entropy scoring rule.)
"""

from __future__ import annotations

from typing import Dict, Any

import numpy as np
import pandas as pd

from wca.ledger.store import all_bets, all_bankroll_events


def _bets_df(db_path: str) -> pd.DataFrame:
    """Load all bets as a DataFrame with proper dtypes."""
    rows = all_bets(db_path)
    if not rows:
        return pd.DataFrame(
            columns=[
                "id", "ts_utc", "match_id", "match_desc", "market",
                "selection", "platform", "decimal_odds", "stake",
                "model_prob", "market_prob_devig", "ev", "kelly_fraction",
                "status", "settled_pl", "closing_odds", "clv", "notes",
            ]
        )
    df = pd.DataFrame([dict(r) for r in rows])
    for col in ("decimal_odds", "stake", "model_prob", "market_prob_devig",
                "ev", "kelly_fraction", "settled_pl", "closing_odds", "clv"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def bankroll_curve(db_path: str) -> pd.DataFrame:
    """Compute the bankroll curve from deposits and settled bet P&L.

    The curve is built chronologically:

    1. All bankroll-event deposits/withdrawals contribute their ``amount``.
    2. All settled (won/lost) bets contribute their ``settled_pl`` at
       ``ts_utc``.
    3. Events are sorted by timestamp and the ``bankroll`` column is the
       running sum.

    Returns
    -------
    pandas.DataFrame
        Columns: ``ts_utc``, ``event_type`` (``"deposit"``/``"withdrawal"``/
        ``"won"``/``"lost"``), ``delta``, ``bankroll``.
    """
    events = all_bankroll_events(db_path)
    bets = all_bets(db_path)

    records = []
    for e in events:
        amt = float(e["amount"])
        records.append(
            {
                "ts_utc": e["ts_utc"],
                "event_type": "deposit" if amt >= 0 else "withdrawal",
                "delta": amt,
            }
        )

    for b in bets:
        if b["status"] in ("won", "lost") and b["settled_pl"] is not None:
            records.append(
                {
                    "ts_utc": b["ts_utc"],
                    "event_type": b["status"],
                    "delta": float(b["settled_pl"]),
                }
            )

    if not records:
        return pd.DataFrame(columns=["ts_utc", "event_type", "delta", "bankroll"])

    df = pd.DataFrame(records).sort_values("ts_utc", kind="stable").reset_index(drop=True)
    df["bankroll"] = df["delta"].cumsum()
    return df


def open_exposure(db_path: str) -> pd.DataFrame:
    """Return all open (unsettled) bets and a summary of total stake at risk.

    Returns
    -------
    pandas.DataFrame
        All open bets, sorted by ``ts_utc``.  An extra summary row with
        ``id == None`` and ``match_id == "TOTAL"`` is appended showing the
        summed stake.
    """
    df = _bets_df(db_path)
    open_df = df[df["status"] == "open"].copy()
    if open_df.empty:
        return open_df

    open_df = open_df.sort_values("ts_utc").reset_index(drop=True)

    total_stake = float(open_df["stake"].sum())
    total_ev = float(open_df["ev"].sum()) if open_df["ev"].notna().any() else None

    # Build summary row with dtypes matching open_df to avoid FutureWarning in
    # pandas 2.x about concatenation with empty/NA-only columns.
    summary_data: Dict[str, Any] = {col: [None] for col in open_df.columns}
    summary_data["match_id"] = ["TOTAL"]
    summary_data["match_desc"] = [""]
    summary_data["market"] = [""]
    summary_data["selection"] = [""]
    summary_data["platform"] = [""]
    summary_data["stake"] = [total_stake]
    summary_data["ev"] = [total_ev]
    summary_data["status"] = ["open"]
    summary = pd.DataFrame(summary_data)
    return pd.concat([open_df, summary], ignore_index=True)


def clv_report(db_path: str) -> Dict[str, Any]:
    """Per-bet CLV table and aggregate CLV statistics.

    Only bets with a non-null ``closing_odds`` are included.

    Returns
    -------
    dict with keys:
        ``"per_bet"``
            DataFrame with columns ``id``, ``match_desc``, ``selection``,
            ``decimal_odds``, ``closing_odds``, ``clv``, ``status``.
        ``"avg_clv"``
            Mean CLV across all bets with closing odds recorded (float).
        ``"pct_beat_close"``
            Fraction of bets with CLV > 0 (float in [0, 1]).
        ``"n_bets"``
            Number of bets with closing odds recorded (int).
    """
    df = _bets_df(db_path)
    clv_df = df[df["closing_odds"].notna()].copy()

    if clv_df.empty:
        return {
            "per_bet": pd.DataFrame(
                columns=["id", "match_desc", "selection", "decimal_odds",
                         "closing_odds", "clv", "status"]
            ),
            "avg_clv": float("nan"),
            "pct_beat_close": float("nan"),
            "n_bets": 0,
        }

    per_bet = clv_df[
        ["id", "match_desc", "selection", "decimal_odds", "closing_odds", "clv", "status"]
    ].copy().reset_index(drop=True)

    avg_clv = float(clv_df["clv"].mean())
    pct_beat = float((clv_df["clv"] > 0).mean())

    return {
        "per_bet": per_bet,
        "avg_clv": avg_clv,
        "pct_beat_close": pct_beat,
        "n_bets": len(clv_df),
    }


def staking_stats(db_path: str) -> Dict[str, Any]:
    """Evidence inputs for the pre-registered Kelly ladder (KellyPolicy).

    Counts only *settled* bets (won/lost) that also have closing odds
    recorded — the ladder promotes on demonstrated CLV, and CLV requires a
    closing line. Bets are ordered by id (insertion order) for the rolling
    window.

    Returns
    -------
    dict with keys:
        ``"n_settled"``     settled bets with closing odds (int)
        ``"clv_to_date"``   mean CLV over those bets, or ``None`` if zero bets
        ``"rolling50_clv"`` mean CLV over the most recent 50, or ``None`` if
                            fewer than 50 exist
    """
    df = _bets_df(db_path)
    eligible = df[
        df["status"].str.lower().isin(["won", "lost"]) & df["clv"].notna()
    ].sort_values("id")

    n = len(eligible)
    if n == 0:
        return {"n_settled": 0, "clv_to_date": None, "rolling50_clv": None}
    clv_to_date = float(eligible["clv"].mean())
    rolling50 = float(eligible["clv"].tail(50).mean()) if n >= 50 else None
    return {"n_settled": n, "clv_to_date": clv_to_date, "rolling50_clv": rolling50}


def calibration_report(db_path: str, n_bins: int = 5) -> Dict[str, Any]:
    """Calibration analysis comparing model probability to market probability.

    Only settled bets (won/lost) with a non-null ``model_prob`` are included.
    Bets are binned by ``model_prob`` into ``n_bins`` equal-width bins in
    [0, 1].  For each bin the observed win rate is computed.

    Two Brier scores are computed over the *full settled sample* (not per-bin):

    * **model Brier score**: ``BS_model = mean((model_prob - outcome)^2)``
    * **market Brier score**: ``BS_market = mean((market_prob_devig - outcome)^2)``
      (only for bets where ``market_prob_devig`` is not null)

    A lower Brier score indicates better calibration.  If
    ``BS_model < BS_market`` the model is better-calibrated than the
    bookmaker's own de-vigged odds.

    Parameters
    ----------
    db_path:
        Path to the SQLite database.
    n_bins:
        Number of equal-width probability bins in [0, 1].

    Returns
    -------
    dict with keys:
        ``"calibration_bins"``
            DataFrame with columns ``bin_low``, ``bin_high``, ``n_bets``,
            ``observed_win_rate``, ``mean_model_prob``.
        ``"brier_model"``
            Brier score for ``model_prob`` vs outcomes (float or NaN).
        ``"brier_market"``
            Brier score for ``market_prob_devig`` vs outcomes (float or NaN).
        ``"n_settled"``
            Number of settled bets used (int).
    """
    df = _bets_df(db_path)
    settled = df[df["status"].isin(("won", "lost")) & df["model_prob"].notna()].copy()

    if settled.empty:
        empty_bins = pd.DataFrame(
            columns=["bin_low", "bin_high", "n_bets", "observed_win_rate", "mean_model_prob"]
        )
        return {
            "calibration_bins": empty_bins,
            "brier_model": float("nan"),
            "brier_market": float("nan"),
            "n_settled": 0,
        }

    # Binary outcome: 1 for won, 0 for lost.
    settled["outcome"] = (settled["status"] == "won").astype(float)

    # Brier score: model.
    model_p = settled["model_prob"].to_numpy(dtype=float)
    outcomes = settled["outcome"].to_numpy(dtype=float)
    brier_model = float(np.mean((model_p - outcomes) ** 2))

    # Brier score: market (only bets that have market_prob_devig).
    mkt_mask = settled["market_prob_devig"].notna()
    if mkt_mask.sum() >= 1:
        mkt_p = settled.loc[mkt_mask, "market_prob_devig"].to_numpy(dtype=float)
        mkt_o = settled.loc[mkt_mask, "outcome"].to_numpy(dtype=float)
        brier_market = float(np.mean((mkt_p - mkt_o) ** 2))
    else:
        brier_market = float("nan")

    # Calibration bins.
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_records = []
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        # Include upper edge only in the last bin to capture p == 1.0.
        if i < n_bins - 1:
            mask = (settled["model_prob"] >= lo) & (settled["model_prob"] < hi)
        else:
            mask = (settled["model_prob"] >= lo) & (settled["model_prob"] <= hi)
        subset = settled[mask]
        n = int(len(subset))
        obs_wr = float(subset["outcome"].mean()) if n > 0 else float("nan")
        mean_mp = float(subset["model_prob"].mean()) if n > 0 else float("nan")
        bin_records.append(
            {
                "bin_low": round(float(lo), 4),
                "bin_high": round(float(hi), 4),
                "n_bets": n,
                "observed_win_rate": obs_wr,
                "mean_model_prob": mean_mp,
            }
        )

    return {
        "calibration_bins": pd.DataFrame(bin_records),
        "brier_model": brier_model,
        "brier_market": brier_market,
        "n_settled": len(settled),
    }


def summary(db_path: str) -> Dict[str, Any]:
    """High-level summary dict suitable for printing to the terminal.

    Returns
    -------
    dict
        Keys include ``total_bets``, ``open_bets``, ``won_bets``,
        ``lost_bets``, ``void_bets``, ``total_staked``, ``total_pl``,
        ``roi``, ``avg_clv``, ``pct_beat_close``, ``brier_model``,
        ``brier_market``, ``total_deposited``, ``current_bankroll``.
    """
    df = _bets_df(db_path)

    total_bets = len(df)
    open_bets = int((df["status"] == "open").sum())
    won_bets = int((df["status"] == "won").sum())
    lost_bets = int((df["status"] == "lost").sum())
    void_bets = int((df["status"] == "void").sum())

    settled = df[df["status"].isin(("won", "lost"))]
    total_staked = float(settled["stake"].sum()) if not settled.empty else 0.0
    total_pl = float(settled["settled_pl"].sum()) if not settled.empty else 0.0
    roi = (total_pl / total_staked) if total_staked > 0 else float("nan")

    # Money currently at risk: stakes of open (unsettled) bets. total_staked
    # deliberately stays settled-only so ROI is realised-return over realised
    # stakes; the bot surfaces both.
    open_df = df[df["status"] == "open"]
    open_staked = float(open_df["stake"].sum()) if not open_df.empty else 0.0

    clv_data = clv_report(db_path)
    avg_clv = clv_data["avg_clv"]
    pct_beat = clv_data["pct_beat_close"]

    cal = calibration_report(db_path)
    brier_model = cal["brier_model"]
    brier_market = cal["brier_market"]

    # Bankroll: sum of deposits + total settled P&L.
    events = all_bankroll_events(db_path)
    total_deposited = float(sum(float(e["amount"]) for e in events))
    current_bankroll = total_deposited + total_pl

    # Per-source breakdown: n (all bets), staked (all bets), settled_pl
    # (settled won/lost only). The three canonical sources are always present
    # so downstream consumers can index them unconditionally. This is purely
    # additive and does not touch the CLV / calibration keys above.
    by_source: Dict[str, Any] = {
        s: {"n": 0, "staked": 0.0, "settled_pl": 0.0}
        for s in ("model", "offer", "punt")
    }
    if "source" in df.columns:
        src_series = df["source"].fillna("model").astype(str)
    else:
        src_series = pd.Series(["model"] * len(df), index=df.index)
    for src, grp in df.groupby(src_series):
        blk = by_source.setdefault(
            str(src), {"n": 0, "staked": 0.0, "settled_pl": 0.0}
        )
        blk["n"] = int(len(grp))
        blk["staked"] = float(grp["stake"].sum()) if not grp.empty else 0.0
        settled_grp = grp[grp["status"].isin(("won", "lost"))]
        blk["settled_pl"] = (
            float(settled_grp["settled_pl"].sum()) if not settled_grp.empty else 0.0
        )

    return {
        "by_source": by_source,
        "total_bets": total_bets,
        "open_bets": open_bets,
        "won_bets": won_bets,
        "lost_bets": lost_bets,
        "void_bets": void_bets,
        "total_staked": total_staked,
        "open_staked": open_staked,
        "total_pl": total_pl,
        "roi": roi,
        "avg_clv": avg_clv,
        "pct_beat_close": pct_beat,
        "brier_model": brier_model,
        "brier_market": brier_market,
        "total_deposited": total_deposited,
        "current_bankroll": current_bankroll,
    }
