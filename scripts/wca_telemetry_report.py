#!/usr/bin/env python
"""Gate + fill telemetry report (read-only, observation only).

Prints two independent sections:

1. WITHHELD BREAKDOWN — counts candidates dropped by ``scripts/wca_betrecs.py``
   grouped by the machine-greppable ``reason_code`` field added by the
   2026-07-08 gate-fill-telemetry work (see ``site/bet_recs.json``'s
   ``withheld`` list). Rows without a ``reason_code`` (pre-telemetry data, or
   any gate that still hasn't been instrumented) are called out separately
   rather than silently folded into a bucket.
2. FILL-RATE STATS — reconstructs order lifecycle from
   ``data/pm_fill_log.jsonl`` (written by ``wca.pm.filltelemetry``): how many
   orders were placed, how many have a matching ``fill_observed`` row, a
   breakdown of fill status, and how often the ROUND_HALF_UP mid-rounding
   tick-snap crossed onto the touch (``mid_rounding`` rows).

READ-ONLY. No network, no ledger writes, no bet execution. Safe to run
anywhere, anytime, including with no data at all ("no data yet" is a valid,
clean output — never a stack trace).

USAGE
-----
    PYTHONPATH=src python scripts/wca_telemetry_report.py
    PYTHONPATH=src python scripts/wca_telemetry_report.py \
        --bet-recs site/bet_recs.json --fill-log data/pm_fill_log.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

# Make `wca.*` importable whether or not PYTHONPATH=src was set by the caller.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.pm import filltelemetry  # noqa: E402

DEFAULT_BET_RECS_PATH = os.path.join(_REPO_ROOT, "site", "bet_recs.json")
DEFAULT_FILL_LOG_PATH = os.path.join(_REPO_ROOT, filltelemetry.DEFAULT_LOG_PATH)


# ---------------------------------------------------------------------------
# Section 1: withheld-row breakdown by reason_code
# ---------------------------------------------------------------------------

def load_withheld_rows(bet_recs_path: str) -> Optional[List[Dict[str, Any]]]:
    """Return the ``withheld`` list from a bet_recs.json, or None if the file
    is absent/unreadable (caller prints "no data yet", never raises)."""
    if not os.path.exists(bet_recs_path):
        return None
    try:
        with open(bet_recs_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    withheld = data.get("withheld")
    if not isinstance(withheld, list):
        return None
    return withheld


def summarize_withheld(withheld: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_code: Counter = Counter()
    no_code = 0
    for row in withheld:
        code = row.get("reason_code")
        if code:
            by_code[code] += 1
        else:
            no_code += 1
    return {
        "total": len(withheld),
        "by_reason_code": dict(by_code.most_common()),
        "missing_reason_code": no_code,
    }


def render_withheld_section(bet_recs_path: str) -> str:
    lines = ["=== WITHHELD ROWS BY reason_code (%s) ===" % bet_recs_path]
    withheld = load_withheld_rows(bet_recs_path)
    if withheld is None:
        lines.append("no data yet (file missing, unreadable, or has no 'withheld' list)")
        return "\n".join(lines)
    if not withheld:
        lines.append("no data yet (0 withheld rows)")
        return "\n".join(lines)

    summary = summarize_withheld(withheld)
    lines.append("total withheld candidates: %d" % summary["total"])
    lines.append("")
    if summary["by_reason_code"]:
        width = max(len(code) for code in summary["by_reason_code"])
        for code, count in summary["by_reason_code"].items():
            pct = 100.0 * count / summary["total"] if summary["total"] else 0.0
            lines.append("  %-*s  %5d  (%5.1f%%)" % (width, code, count, pct))
    if summary["missing_reason_code"]:
        lines.append("")
        lines.append(
            "  WARNING: %d withheld row(s) have NO reason_code (uninstrumented "
            "gate or stale pre-telemetry data)" % summary["missing_reason_code"]
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 2: PM fill-rate stats from data/pm_fill_log.jsonl
# ---------------------------------------------------------------------------

def summarize_fill_log(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    placed = [r for r in rows if r.get("kind") == "placed"]
    fills = [r for r in rows if r.get("kind") == "fill_observed"]
    mid_rounding = [r for r in rows if r.get("kind") == "mid_rounding"]

    live_placed = [r for r in placed if not r.get("dry_run")]
    dry_placed = [r for r in placed if r.get("dry_run")]

    # Match fill_observed rows back to placed rows by order_id (None order_id
    # -- e.g. dry-run placements with no server id -- can never be matched;
    # counted separately rather than silently mis-attributed).
    placed_ids = {r.get("order_id") for r in placed if r.get("order_id")}
    fill_ids = {r.get("order_id") for r in fills if r.get("order_id")}
    matched = placed_ids & fill_ids
    unmatched_placed = [
        r for r in live_placed
        if r.get("order_id") and r.get("order_id") not in fill_ids
    ]

    status_counts: Counter = Counter(r.get("status", "unknown") for r in fills)

    crossed = [r for r in mid_rounding if r.get("crossed_to_ask") or r.get("crossed_to_bid")]

    return {
        "placed_total": len(placed),
        "placed_live": len(live_placed),
        "placed_dry_run": len(dry_placed),
        "fill_observed_total": len(fills),
        "fill_status_counts": dict(status_counts.most_common()),
        "live_orders_with_matched_fill_row": len(matched),
        "live_orders_without_fill_row": len(unmatched_placed),
        "mid_rounding_total": len(mid_rounding),
        "mid_rounding_crossed": len(crossed),
    }


def render_fill_log_section(fill_log_path: str) -> str:
    lines = ["", "=== PM FILL LIFECYCLE (%s) ===" % fill_log_path]
    if not os.path.exists(fill_log_path):
        lines.append("no data yet (fill log not created — no orders placed since this "
                      "feature shipped)")
        return "\n".join(lines)

    rows = filltelemetry.read_rows(fill_log_path)
    if not rows:
        lines.append("no data yet (fill log present but empty)")
        return "\n".join(lines)

    s = summarize_fill_log(rows)
    lines.append("orders placed:        %d  (live=%d, dry_run=%d)" %
                 (s["placed_total"], s["placed_live"], s["placed_dry_run"]))
    lines.append("fill_observed rows:   %d" % s["fill_observed_total"])
    if s["fill_status_counts"]:
        lines.append("  by status:")
        for status, count in s["fill_status_counts"].items():
            lines.append("    %-14s %5d" % (status, count))
    lines.append("")
    if s["placed_live"] == 0:
        lines.append("live fill-rate: no data yet (no live orders placed)")
    else:
        lines.append(
            "live orders with a matched fill_observed row: %d / %d"
            % (s["live_orders_with_matched_fill_row"], s["placed_live"])
        )
        lines.append(
            "live orders with NO fill_observed row (unfilled/unconfirmed "
            "resting maker orders — the invisible-EV-leak this report exists "
            "to surface): %d" % s["live_orders_without_fill_row"]
        )
    lines.append("")
    if s["mid_rounding_total"] == 0:
        lines.append("mid-rounding tick-snap: no data yet (no proposals built since this "
                      "feature shipped)")
    else:
        pct = 100.0 * s["mid_rounding_crossed"] / s["mid_rounding_total"]
        lines.append(
            "ROUND_HALF_UP crossed onto touch (1-tick book): %d / %d (%.1f%%)"
            % (s["mid_rounding_crossed"], s["mid_rounding_total"], pct)
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bet-recs", default=DEFAULT_BET_RECS_PATH,
                         help="path to bet_recs.json (default: site/bet_recs.json)")
    parser.add_argument("--fill-log", default=DEFAULT_FILL_LOG_PATH,
                         help="path to pm_fill_log.jsonl (default: data/pm_fill_log.jsonl)")
    args = parser.parse_args(argv)

    print(render_withheld_section(args.bet_recs))
    print(render_fill_log_section(args.fill_log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
