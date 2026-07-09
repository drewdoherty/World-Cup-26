"""Observation-only Polymarket order/fill lifecycle telemetry.

Motivation (2026-07-08 adversarial review): PM maker orders rest GTC at (a
rounded) mid with NO fill-rate logging today — an unfilled maker order is a
100% EV leak that is currently invisible. This module adds a single
append-only log, ``data/pm_fill_log.jsonl``, one JSON object per line, so
placement and (when observed) fill outcome can be reconstructed and a
fill-rate can be computed by :mod:`scripts.wca_telemetry_report`.

This module NEVER changes execution behaviour. It does not gate, size, cap,
retry, or cancel anything — it only appends a record of what already
happened. Callers (``wca.pm.trader.ClobTrader.place_order`` and
``wca.bot.app``) call :func:`log_placed` / :func:`log_fill_observed` /
:func:`log_mid_rounding` immediately after their own (unmodified) logic runs;
a failure to write telemetry is swallowed (never raised) so a full disk or a
permissions issue can never block a live order or a cash-out.

Row shapes (``kind`` discriminates)
------------------------------------
* ``"placed"``       — order placed (dry-run or live): order id, market,
  side, price, size, notional, mid-at-placement (when known), order_type,
  dry_run, ts.
* ``"fill_observed"`` — a later read of fill status for a previously placed
  order: filled qty/status, ts. Emitted by the SELL cash-out reconciliation
  path (the only place a fill is currently confirmed) and available for a
  future BUY-side poller.
* ``"mid_rounding"``  — flags the ROUND_HALF_UP-crosses-to-ask case the
  review found: a proposal price was snapped from a true book mid to the
  0.01 tick grid, and rounding moved it at or past the best ask (i.e. the
  "resting at mid" order actually parks at the touch/ask, not mid). Log-only
  — :func:`wca.pm.propose.build_pm_proposals` rounding is untouched.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_LOG_PATH = "data/pm_fill_log.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _append(path: str, row: Dict[str, Any]) -> None:
    """Append one JSON line. Never raises — telemetry must not affect trading."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 - observation only, never raise
        pass


def log_placed(
    *,
    order_id: Optional[str],
    token_id: str,
    market: Optional[str],
    side: str,
    price: float,
    size: float,
    order_type: str,
    dry_run: bool,
    mid_at_placement: Optional[float] = None,
    de_risk: bool = False,
    path: str = DEFAULT_LOG_PATH,
    ts: Optional[str] = None,
) -> None:
    """Record a just-placed order (dry-run or live). Observation only.

    ``mid_at_placement`` is the book mid at the moment of placement (when the
    caller has it, e.g. from ``propose.py``'s resolved price) — this is what
    lets the report compute how far a resting GTC order sat from the touch,
    and hence estimate the opportunity cost of an unfilled maker order.
    """
    row = {
        "kind": "placed",
        "ts": ts or _utc_now_iso(),
        "order_id": order_id,
        "token_id": str(token_id),
        "market": market,
        "side": str(side).upper(),
        "price": float(price),
        "size": float(size),
        "notional": round(float(price) * float(size), 6),
        "order_type": str(order_type),
        "dry_run": bool(dry_run),
        "de_risk": bool(de_risk),
        "mid_at_placement": (float(mid_at_placement) if mid_at_placement is not None else None),
    }
    _append(path, row)


def log_fill_observed(
    *,
    order_id: Optional[str],
    token_id: str,
    side: str,
    filled_size: float,
    requested_size: Optional[float] = None,
    proceeds_or_cost: Optional[float] = None,
    status: str,
    path: str = DEFAULT_LOG_PATH,
    ts: Optional[str] = None,
) -> None:
    """Record an observed fill outcome for a previously-placed order.

    ``status`` is a free label such as ``"filled"``, ``"partial"``,
    ``"no_fill"``, or ``"unconfirmed"`` — mirroring the outcomes
    ``wca.bot.app.execute_cashout`` already distinguishes on the SELL side.
    This is the ONLY place fill confirmation happens today; the BUY entry
    path has no equivalent poller (see module docstring) so it never calls
    this — that gap is exactly what this telemetry makes measurable via
    ``scripts/wca_telemetry_report.py`` (placed rows with no matching
    fill_observed row for the same order_id).
    """
    row = {
        "kind": "fill_observed",
        "ts": ts or _utc_now_iso(),
        "order_id": order_id,
        "token_id": str(token_id),
        "side": str(side).upper(),
        "filled_size": float(filled_size),
        "requested_size": (float(requested_size) if requested_size is not None else None),
        "proceeds_or_cost": (float(proceeds_or_cost) if proceeds_or_cost is not None else None),
        "status": str(status),
    }
    _append(path, row)


def log_mid_rounding(
    *,
    token_id: str,
    raw_mid: float,
    rounded_price: float,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
    tick_size: str = "0.01",
    path: str = DEFAULT_LOG_PATH,
    ts: Optional[str] = None,
) -> None:
    """Flag a ROUND_HALF_UP tick-snap that crossed the true mid onto the ask
    (or bid) — the 1-tick-book case the 2026-07-08 review found.

    Frequency-only: does not change the rounding in
    :func:`wca.pm.propose.build_pm_proposals`. ``crossed_to_ask`` /
    ``crossed_to_bid`` are computed here so the report can count occurrences
    without re-deriving the book-crossing logic itself.
    """
    crossed_to_ask = best_ask is not None and rounded_price >= float(best_ask) - 1e-9 and raw_mid < float(best_ask) - 1e-9
    crossed_to_bid = best_bid is not None and rounded_price <= float(best_bid) + 1e-9 and raw_mid > float(best_bid) + 1e-9
    row = {
        "kind": "mid_rounding",
        "ts": ts or _utc_now_iso(),
        "token_id": str(token_id),
        "raw_mid": float(raw_mid),
        "rounded_price": float(rounded_price),
        "best_bid": (float(best_bid) if best_bid is not None else None),
        "best_ask": (float(best_ask) if best_ask is not None else None),
        "tick_size": str(tick_size),
        "crossed_to_ask": bool(crossed_to_ask),
        "crossed_to_bid": bool(crossed_to_bid),
    }
    _append(path, row)


def read_rows(path: str = DEFAULT_LOG_PATH) -> list:
    """Read all rows from the jsonl log. Returns ``[]`` if the file is absent
    or unreadable/corrupt (never raises — this is a reporting helper)."""
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        return []
    return rows
