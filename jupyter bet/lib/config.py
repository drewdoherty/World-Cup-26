"""Typed research configuration — every tunable in one dataclass.

Notebooks build a :class:`Params` in a dedicated parameter cell, optionally
overlaying ``config.yaml`` (copy ``config.example.yaml``). Each field carries
a description + sensible range in :data:`PARAM_META`, and
``params_table(p)`` renders the whole config as a DataFrame so a run's
settings are always visible and saved alongside outputs.

Defaults deliberately mirror production where production has an opinion
(fees, Kelly fraction, staleness) and are conservative elsewhere.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import lib.bootstrap as bt


@dataclass
class Params:
    # ---- edge thresholds -------------------------------------------------
    min_edge_raw: float = 0.02        # model prob − implied prob, before costs
    min_edge_net: float = 0.01        # after fees + expected slippage
    # ---- costs -----------------------------------------------------------
    pm_fee_rate: float = 0.0          # PM base fee (0 for most WC markets)
    pm_taker_fee_coeff: float = 0.03  # fee = coeff·p·(1−p) where charged; 0.03 mirrors pm/trader
    exchange_commission: float = 0.02  # Smarkets 2% (wca.arbfx.SMARKETS_COMMISSION)
    slippage_frac_of_spread: float = 0.5  # expected fill = mid + this·half-spread
    # ---- liquidity / execution gates --------------------------------------
    max_spread: float = 0.06          # PM: ask−bid in probability units
    min_depth_usd: float = 50.0       # size executable within slippage cap
    max_slippage: float = 0.01        # price move tolerated when walking the book
    staleness_max_s: int = 3600       # quote older than this ⇒ stale, reject
    # ---- matching --------------------------------------------------------
    min_match_confidence: float = 0.9
    kickoff_tolerance_h: float = 3.0
    # ---- fair value -------------------------------------------------------
    fair_value_method: str = "pm_mid"   # pm_mid|microprice|last_trade|vwap_1h|book_devig|model
    devig_method: str = "shin"          # shin|multiplicative|power (wca.markets.devig)
    # ---- time windows ------------------------------------------------------
    window_hours: Tuple[int, ...] = (48, 24, 0)
    extra_window_hours: Tuple[int, ...] = (72, 12, 6, 3, 1)
    window_tolerance_min: int = 30    # snapshot must be within ±this of the mark
    # ---- statistics ---------------------------------------------------------
    min_sample: int = 30
    # ---- sizing / portfolio -------------------------------------------------
    kelly_fraction: float = 0.25      # production: wca.markets.bankroll.PM_KELLY_FRACTION
    stake_cap_usd: float = 160.0      # production per-order cap (pm/trader.py)
    exposure_cap_frac: float = 0.25   # max fraction of bankroll in one event
    correlation_haircut: float = 0.5  # stake multiplier for correlated same-event legs
    # ---- arbitrage / promos ---------------------------------------------------
    arb_min_profit_frac: float = 0.005  # locked ROI floor AFTER fees/rounding
    freebet_conversion: float = 0.70    # £ value of £1 free bet (matched extraction)
    # ---- data hygiene -----------------------------------------------------------
    allow_expost: bool = False        # closing-price columns usable? (benchmark cells only)
    offline: bool = False             # never touch the network when True
    max_credits: int = 25             # Odds API credit budget for one notebook run

    def to_frame(self):
        """Render as a pandas DataFrame (name, value, description, range)."""
        import pandas as pd
        rows = []
        for f in dataclasses.fields(self):
            meta = PARAM_META.get(f.name, {})
            rows.append({
                "param": f.name,
                "value": repr(getattr(self, f.name)),
                "description": meta.get("desc", ""),
                "sensible_range": meta.get("range", ""),
            })
        return pd.DataFrame(rows)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(dataclasses.asdict(self), indent=2, default=list))


PARAM_META: Dict[str, Dict[str, str]] = {
    "min_edge_raw": {"desc": "Model prob minus market implied prob, pre-cost", "range": "0.00–0.10"},
    "min_edge_net": {"desc": "Edge after fees and expected slippage", "range": "0.00–0.08"},
    "pm_fee_rate": {"desc": "Flat PM fee rate (0 for most WC markets)", "range": "0–0.02"},
    "pm_taker_fee_coeff": {"desc": "PM taker fee = coeff·p·(1−p) where charged", "range": "0–0.05"},
    "exchange_commission": {"desc": "GBP exchange commission on net winnings", "range": "0.00 (Smarkets promo)–0.06 (Betfair)"},
    "slippage_frac_of_spread": {"desc": "Expected fill = mid + this × half-spread", "range": "0 (fill at mid)–1 (fill at touch)"},
    "max_spread": {"desc": "Reject quotes with ask−bid above this (prob units)", "range": "0.02–0.15"},
    "min_depth_usd": {"desc": "Min $ executable within slippage cap", "range": "10–500"},
    "max_slippage": {"desc": "Max price move walking the book for our size", "range": "0.005–0.03"},
    "staleness_max_s": {"desc": "Quote older than this is stale → reject", "range": "300–14400"},
    "min_match_confidence": {"desc": "Cross-venue match score floor", "range": "0.7–1.0"},
    "kickoff_tolerance_h": {"desc": "Kickoff mismatch tolerated when matching", "range": "1–6"},
    "fair_value_method": {"desc": "pm_mid | microprice | last_trade | vwap_1h | book_devig | model", "range": "categorical"},
    "devig_method": {"desc": "shin | multiplicative | power (wca.markets.devig)", "range": "categorical"},
    "window_hours": {"desc": "Required convergence marks (h before kickoff)", "range": "(48,24,0) fixed by spec"},
    "extra_window_hours": {"desc": "Optional extra marks", "range": "subset of (72,12,6,3,1)"},
    "window_tolerance_min": {"desc": "Snapshot must sit within ± this of a mark", "range": "5–120"},
    "min_sample": {"desc": "Min n before a stat is reported as a finding", "range": "10–200"},
    "kelly_fraction": {"desc": "Fraction of full Kelly (production 0.25)", "range": "0.05–0.5"},
    "stake_cap_usd": {"desc": "Hard per-order cap (production $160)", "range": "10–160 (>160 needs human code change)"},
    "exposure_cap_frac": {"desc": "Max bankroll fraction on one event", "range": "0.05–0.5"},
    "correlation_haircut": {"desc": "Stake multiplier for correlated same-event legs", "range": "0–1"},
    "arb_min_profit_frac": {"desc": "Locked ROI floor after all costs", "range": "0.001–0.03"},
    "freebet_conversion": {"desc": "Cash value of £1 free bet when matched", "range": "0.5–0.8"},
    "allow_expost": {"desc": "Allow closing-price (ex-post) columns — benchmark cells only", "range": "False in any decision path"},
    "offline": {"desc": "True = read cached raw/parquet only, no network", "range": "bool"},
    "max_credits": {"desc": "Odds API credit budget per notebook run", "range": "0–500"},
}


def load_params(config_path: Optional[Path] = None, **overrides: Any) -> Params:
    """Params from defaults ← config.yaml (if present) ← keyword overrides."""
    cfg: Dict[str, Any] = {}
    path = config_path or (bt.JB_ROOT / "config.yaml")
    if path.exists():
        import yaml
        loaded = yaml.safe_load(path.read_text()) or {}
        known = {f.name for f in dataclasses.fields(Params)}
        unknown = set(loaded) - known
        if unknown:
            raise KeyError(f"config.yaml has unknown params: {sorted(unknown)}")
        cfg.update(loaded)
    cfg.update(overrides)
    for key in ("window_hours", "extra_window_hours"):
        if key in cfg and isinstance(cfg[key], list):
            cfg[key] = tuple(cfg[key])
    return Params(**cfg)


def write_example_yaml(path: Optional[Path] = None) -> Path:
    """Generate config.example.yaml from the dataclass so docs never drift."""
    out = path or (bt.JB_ROOT / "config.example.yaml")
    lines: List[str] = [
        "# jupyter bet — research parameters (copy to config.yaml and edit).",
        "# Every key maps 1:1 to lib.config.Params; unknown keys raise.",
        "",
    ]
    p = Params()
    for f in dataclasses.fields(Params):
        meta = PARAM_META.get(f.name, {})
        lines.append(f"# {meta.get('desc','')}  [sensible: {meta.get('range','')}]")
        val = getattr(p, f.name)
        if isinstance(val, tuple):
            lines.append(f"{f.name}: {list(val)}")
        elif isinstance(val, bool):
            lines.append(f"{f.name}: {str(val).lower()}")
        elif isinstance(val, str):
            lines.append(f"{f.name}: {val}")
        else:
            lines.append(f"{f.name}: {val}")
        lines.append("")
    out.write_text("\n".join(lines))
    return out
