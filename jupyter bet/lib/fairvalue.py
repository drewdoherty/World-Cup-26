"""Fair value, costs, EV and sizing — thin wrappers over PRODUCTION math.

Nothing here re-derives a formula that production already owns:
* de-vigging          → ``wca.markets.devig`` (shin / multiplicative / power)
* PM taker fee        → same ``coeff·p·(1−p)`` shape as ``wca_betrecs._pm_fee``
                        / ``pm/trader`` (coefficient is a Params knob)
* EV per unit stake   → ``wca_betrecs._net_ev`` (p·price − 1)
* fractional Kelly    → ``wca_betrecs._kelly_stake`` (cap-clamped)
* £↔$                → ``wca.markets.bankroll`` (fixed 1.33)

Fair-value METHODS registry (Params.fair_value_method):
  pm_mid       midpoint of best bid/ask               (needs a book/quote)
  microprice   size-weighted touch price               (needs book with sizes)
  last_trade   most recent trade price                 (needs trades)
  vwap_1h      volume-weighted price, trailing 60 min  (needs trades)
  book_devig   de-vigged sportsbook consensus          (needs bookmaker odds)
  model        blended model probability               (needs model feed)

`closing` is deliberately NOT in the registry: closing price is an ex-post
benchmark only (look-ahead rule) — convergence code accesses it through an
explicitly-labelled column, never through fair_value().
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import polars as pl

import lib.bootstrap  # noqa: F401
from wca.markets import devig as _devig
from wca_betrecs import _kelly_stake, _net_ev  # production helpers

FAIR_VALUE_METHODS = ("pm_mid", "microprice", "last_trade", "vwap_1h",
                      "book_devig", "model")


# --------------------------------------------------------------------------
# Odds ↔ probability + de-vig (production dispatch)
# --------------------------------------------------------------------------

def implied_probs(decimal_odds: Sequence[float]) -> np.ndarray:
    return _devig.implied_probs(decimal_odds)


def overround(decimal_odds: Sequence[float]) -> float:
    return _devig.overround(decimal_odds)


def devig(decimal_odds: Sequence[float], method: str = "shin") -> np.ndarray:
    fn = {"shin": _devig.shin, "multiplicative": _devig.multiplicative,
          "power": _devig.power}.get(method)
    if fn is None:
        raise ValueError(f"devig_method must be shin|multiplicative|power, got {method}")
    return fn(decimal_odds)


# --------------------------------------------------------------------------
# Fair value dispatch
# --------------------------------------------------------------------------

def fair_value(method: str, *,
               quote: Optional[dict] = None,
               trades: Optional[pl.DataFrame] = None,
               asof_ts: Optional[int] = None,
               book_odds: Optional[Sequence[float]] = None,
               book_index: Optional[int] = None,
               devig_method: str = "shin",
               model_prob: Optional[float] = None) -> Optional[float]:
    """Fair probability for ONE outcome under `method`; None if inputs for
    that method are unavailable (caller records the gap — no silent fallback).

    quote  dict with best_bid/best_ask/bid_sz_top/ask_sz_top (book_metrics()).
    trades Polars frame with ts (epoch s) + price + size, SAME outcome token.
    asof_ts  epoch-seconds cutoff — trades AFTER this are excluded (look-ahead
             guard; required whenever trades are used historically).
    """
    if method not in FAIR_VALUE_METHODS:
        raise ValueError(f"unknown fair value method {method}")
    if method == "pm_mid":
        if quote and quote.get("best_bid") is not None and quote.get("best_ask") is not None:
            return (quote["best_bid"] + quote["best_ask"]) / 2
        return None
    if method == "microprice":
        if quote and None not in (quote.get("best_bid"), quote.get("best_ask"),
                                  quote.get("bid_sz_top"), quote.get("ask_sz_top")):
            den = quote["bid_sz_top"] + quote["ask_sz_top"]
            if den > 0:
                return (quote["best_bid"] * quote["ask_sz_top"]
                        + quote["best_ask"] * quote["bid_sz_top"]) / den
        return None
    if method in ("last_trade", "vwap_1h"):
        if trades is None or trades.is_empty():
            return None
        t = trades
        if asof_ts is not None:
            t = t.filter(pl.col("ts") <= asof_ts)
        if t.is_empty():
            return None
        if method == "last_trade":
            return float(t.sort("ts").tail(1)["price"][0])
        hi = asof_ts if asof_ts is not None else int(t["ts"].max())
        w = t.filter(pl.col("ts") > hi - 3600)
        if w.is_empty() or float(w["size"].sum()) <= 0:
            return None
        return float((w["price"] * w["size"]).sum() / w["size"].sum())
    if method == "book_devig":
        if not book_odds or book_index is None:
            return None
        return float(devig(book_odds, devig_method)[book_index])
    if method == "model":
        return model_prob
    return None


# --------------------------------------------------------------------------
# Costs and executable EV (PM buy side)
# --------------------------------------------------------------------------

def pm_fee(p: float, coeff: float = 0.0) -> float:
    """PM taker fee at probability p (coeff·p·(1−p)); most WC markets 0."""
    return coeff * p * (1.0 - p)


def expected_fill(quote: dict, *, slippage_frac_of_spread: float = 0.5) -> Optional[float]:
    """Expected buy fill: mid + fraction of the half-spread toward the ask."""
    bb, ba = quote.get("best_bid"), quote.get("best_ask")
    if bb is None or ba is None:
        return None
    mid = (bb + ba) / 2
    return mid + slippage_frac_of_spread * (ba - mid)


def edge_net(fair_p: float, fill_price: float, *, fee_coeff: float = 0.0) -> float:
    """Net edge in probability units: fair − (fill + fee at fill)."""
    return fair_p - (fill_price + pm_fee(fill_price, fee_coeff))


def ev_per_dollar(fair_p: float, fill_price: float, *, fee_coeff: float = 0.0) -> float:
    """EV per $1 staked buying at fill_price with fair probability fair_p.
    Equivalent to production _net_ev(p, 1/price) with a fee-loaded price."""
    eff = fill_price + pm_fee(fill_price, fee_coeff)
    if eff <= 0 or eff >= 1:
        return 0.0
    return _net_ev(fair_p, 1.0 / eff)


def kelly_stake(fair_p: float, fill_price: float, bankroll: float, *,
                fraction: float = 0.25, cap_frac: float = 0.10,
                fee_coeff: float = 0.0) -> float:
    """Fractional-Kelly $ stake via PRODUCTION _kelly_stake (decimal-odds
    form: price_dec = 1/effective price)."""
    eff = fill_price + pm_fee(fill_price, fee_coeff)
    if eff <= 0 or eff >= 1:
        return 0.0
    return _kelly_stake(fair_p, 1.0 / eff, bankroll,
                        fraction=fraction, cap=cap_frac)


def walk_book(levels: List[dict], usd: float) -> Optional[Dict[str, float]]:
    """Walk ask levels [{price,size},…] spending `usd`; returns avg fill px,
    shares, and worst level touched — or None if depth is insufficient."""
    spent = 0.0
    shares = 0.0
    worst = None
    for lvl in sorted(levels, key=lambda l: float(l["price"])):
        px, sz = float(lvl["price"]), float(lvl["size"])
        take_usd = min(usd - spent, px * sz)
        if take_usd <= 0:
            break
        shares += take_usd / px
        spent += take_usd
        worst = px
    if spent + 1e-9 < usd:
        return None
    return {"avg_px": spent / shares, "shares": shares, "worst_px": worst}
