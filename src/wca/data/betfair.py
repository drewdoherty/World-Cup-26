"""Betfair-side price data — sourced from The Odds API (read-only).

There is no live Betfair Exchange API client in this project (a cert-login
client was planned in TODO.md but never built). The Betfair *prices* the arb
pipeline needs are supplied by The Odds API, whose ``uk`` region includes the
``betfair_ex_uk`` / ``betfair_ex_eu`` exchange feeds. This module is a thin
adapter over :mod:`wca.data.theoddsapi` that fetches and filters to those
exchange rows, preserving the exact DataFrame shape the rest of the system
expects.

SUBSTITUTION (documented): The Odds API gives **best back decimal odds only** —
no lay prices, no order-book depth/liquidity, ~1–180s delayed, h2h/totals
markets. Odds are decimal (currency-agnostic); the GBP side is implied by
Betfair-UK and realised only at stake sizing. This is weaker than a real
Betfair Exchange API would be, and is suitable for MONITORING, not execution.

EXECUTION: intentionally unimplemented — see :func:`betfair_execution_stub`.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import pandas as pd

from wca.data import theoddsapi

# Betfair Exchange bookmaker keys as they appear in The Odds API responses.
BETFAIR_EX_KEYS: Tuple[str, ...] = ("betfair_ex_uk", "betfair_ex_eu")
GBP = "GBP"


def betfair_odds(
    sport_key: str = "soccer_fifa_world_cup",
    *,
    markets: str = "h2h,totals",
    keys: Tuple[str, ...] = BETFAIR_EX_KEYS,
) -> Tuple[pd.DataFrame, "theoddsapi.QuotaInfo"]:
    """Fetch Betfair Exchange odds via The Odds API, GBP side.

    Requests the ``uk`` region (which carries ``betfair_ex_uk``) and filters the
    flat odds frame down to the Betfair Exchange rows. The returned frame has
    the same columns as :func:`theoddsapi.get_odds` plus a ``currency`` column
    (``"GBP"``), so downstream code (``arb.py``, the site feed) is unchanged.
    """
    df, quota = theoddsapi.get_odds(
        sport_key, regions="uk", markets=markets, odds_format="decimal"
    )
    return filter_betfair(df, keys=keys), quota


def filter_betfair(df: pd.DataFrame, *, keys: Tuple[str, ...] = BETFAIR_EX_KEYS) -> pd.DataFrame:
    """Filter a flat Odds-API frame to Betfair Exchange rows, tag currency GBP.

    Pure (no I/O) so it is unit-testable against a mocked frame. Returns an
    empty, correctly-shaped frame when no Betfair rows are present.
    """
    if df is None or df.empty or "bookmaker_key" not in df.columns:
        out = df.copy() if df is not None else pd.DataFrame()
        if "currency" not in out.columns:
            out["currency"] = pd.Series(dtype="object")
        return out
    out = df[df["bookmaker_key"].isin(keys)].copy()
    out["currency"] = GBP
    return out


# ---------------------------------------------------------------------------
# Execution boundary — intentionally NOT implemented.
# ---------------------------------------------------------------------------

def betfair_execution_stub(*_args, **_kwargs):
    """Explicit boundary: this stack cannot place/lay/cancel on Betfair.

    The Odds API is a read-only aggregator. No Betfair execution ever existed in
    this project, and none is added here. This stub exists so the boundary is
    visible in code rather than silently dropped.
    """
    raise NotImplementedError(
        "read-only: The Odds API cannot execute orders. Betfair bet placement is "
        "intentionally unimplemented (monitoring-only). Use Polymarket ClobTrader "
        "for the only supported execution path."
    )
