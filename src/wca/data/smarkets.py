"""Smarkets-side price data (READ-ONLY).

Smarkets is a GBP betting exchange with a native REST API (api.smarkets.com)
that exposes real order-book depth (back AND lay), so — unlike the back-odds-
only Odds API/Betfair feed — a matched Smarkets opportunity can be
EXECUTION-GRADE (you can see the lay side and available volume).

Sourcing, in order:
  1. native Smarkets API if a session/token is configured (SMARKETS_API_TOKEN) —
     gives back+lay+depth → confidence "execution-grade";
  2. else The Odds API ``smarkets`` bookmaker feed (back odds only) → a clearly
     documented DOWNGRADE to "monitoring-grade".

READ-ONLY: no order placement. The boundary is explicit in
:func:`smarkets_execution_stub` (mirrors the Betfair stub).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from wca.data import theoddsapi

GBP = "GBP"
SMARKETS_API = "https://api.smarkets.com/v3"
ODDSAPI_SMARKETS_KEY = "smarkets"


def have_native_session() -> bool:
    """True when a native Smarkets token is configured (back+lay+depth)."""
    return bool(os.environ.get("SMARKETS_API_TOKEN", "").strip())


def smarkets_odds(
    sport_key: str = "soccer_fifa_world_cup",
    *,
    markets: str = "h2h",
) -> Tuple[pd.DataFrame, str]:
    """Return (frame, grade). grade ∈ {"execution-grade","monitoring-grade"}.

    Native path is intentionally a documented stub here (no token/network in
    this env); falls back to The Odds API ``smarkets`` feed. Either way the frame
    matches the shared odds shape, tagged currency=GBP and a ``lay_odds`` column
    (populated only on the native path).
    """
    if have_native_session():
        # Native back+lay+depth would be fetched here. Left unimplemented in this
        # environment (no token); documented downgrade path is used instead.
        pass
    df, _ = theoddsapi.get_odds(sport_key, regions="uk", markets=markets, odds_format="decimal")
    return filter_smarkets(df), "monitoring-grade"


def filter_smarkets(df: pd.DataFrame) -> pd.DataFrame:
    """Filter a flat Odds-API frame to Smarkets rows; tag GBP. Back-only feed."""
    if df is None or df.empty or "bookmaker_key" not in df.columns:
        out = df.copy() if df is not None else pd.DataFrame()
        for c in ("currency", "lay_odds"):
            if c not in out.columns:
                out[c] = pd.Series(dtype="object")
        return out
    out = df[df["bookmaker_key"] == ODDSAPI_SMARKETS_KEY].copy()
    out["currency"] = GBP
    out["lay_odds"] = None  # back-only via the aggregator
    return out


def smarkets_execution_stub(*_args, **_kwargs):
    """Explicit boundary: this stack cannot place/cancel orders on Smarkets."""
    raise NotImplementedError(
        "read-only: Smarkets order placement is intentionally unimplemented "
        "(monitoring-only). Polymarket ClobTrader is the only execution path."
    )
