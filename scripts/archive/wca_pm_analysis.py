#!/usr/bin/env python3
"""Query and analyze Polymarket price-history snapshots.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_pm_analysis.py --team Brazil
    PYTHONPATH=src python3 scripts/wca_pm_analysis.py --team Brazil --stage QF
    PYTHONPATH=src python3 scripts/wca_pm_analysis.py --market-stats
    PYTHONPATH=src python3 scripts/wca_pm_analysis.py --convergence Brazil QF
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import defaultdict

def format_pct(val: float) -> str:
    if val is None:
        return "N/A"
    return f"{val:.1%}"

def query_market(con: sqlite3.Connection, team: str, stage: str, limit: int = 10) -> None:
    """Show price history for a specific market."""
    cursor = con.cursor()
    cursor.execute("""
        SELECT ts_utc, pm_mid, model_prob
        FROM pm_snapshots
        WHERE kind = 'advancement' AND team = ? AND stage = ?
        ORDER BY ts_utc DESC
        LIMIT ?
    """, (team, stage, limit))
    
    rows = cursor.fetchall()
    if not rows:
        print(f"No data for {team} {stage}")
        return
    
    print(f"\n{team} → {stage}: Price History (last {len(rows)} snapshots)")
    print("=" * 80)
    print(f"{'Timestamp':<25} {'PM Price':<12} {'Model Prob':<12} {'Edge':<10}")
    print("-" * 80)
    
    for ts, pm_mid, model_prob in rows:
        edge = ((model_prob or 0) - pm_mid) * 100 if pm_mid and model_prob else None
        edge_str = f"{edge:+.2f}%" if edge is not None else "N/A"
        print(f"{ts:<25} {format_pct(pm_mid):<12} {format_pct(model_prob):<12} {edge_str:<10}")

def convergence_stats(con: sqlite3.Connection, team: str, stage: str) -> None:
    """Analyze price convergence toward model."""
    cursor = con.cursor()
    cursor.execute("""
        SELECT ts_utc, pm_mid, model_prob
        FROM pm_snapshots
        WHERE kind = 'advancement' AND team = ? AND stage = ?
        ORDER BY ts_utc ASC
    """, (team, stage))
    
    rows = cursor.fetchall()
    if len(rows) < 2:
        print(f"Insufficient data for {team} {stage} (need ≥2 snapshots)")
        return
    
    first_pm, last_pm = rows[0][1], rows[-1][1]
    first_model, last_model = rows[0][2], rows[-1][2]
    first_time, last_time = rows[0][0], rows[-1][0]
    
    if first_model is None or last_model is None:
        print(f"Missing model data for {team} {stage}")
        return
    
    # Did PM converge toward model?
    pm_dist_start = abs(first_pm - first_model)
    pm_dist_end = abs(last_pm - last_model)
    converged = pm_dist_end < pm_dist_start
    convergence_rate = ((pm_dist_start - pm_dist_end) / pm_dist_start * 100) if pm_dist_start > 0 else 0
    
    print(f"\n{team} → {stage}: Convergence Analysis")
    print("=" * 80)
    print(f"Period: {first_time} to {last_time} ({len(rows)} snapshots)")
    print(f"Model Probability: {format_pct(first_model)} → {format_pct(last_model)}")
    print(f"PM Price:         {format_pct(first_pm)} → {format_pct(last_pm)}")
    print(f"\nDistance to model (entry):  {pm_dist_start:.4f}")
    print(f"Distance to model (latest): {pm_dist_end:.4f}")
    print(f"Convergence:        {'✓ YES' if converged else '✗ NO'} ({convergence_rate:+.1f}%)")
    
    # Direction
    if last_pm > first_pm:
        direction = "↑ RISING (bullish)"
    elif last_pm < first_pm:
        direction = "↓ FALLING (bearish)"
    else:
        direction = "→ FLAT"
    
    model_direction = "↑" if last_model > first_model else ("↓" if last_model < first_model else "→")
    print(f"PM trend:           {direction}")
    print(f"Model trend:        {model_direction}")

def market_stats(con: sqlite3.Connection) -> None:
    """Show latest snapshot statistics."""
    cursor = con.cursor()
    
    # Get latest timestamp
    cursor.execute("SELECT MAX(ts_utc) FROM pm_snapshots")
    latest_ts = cursor.fetchone()[0]
    
    if not latest_ts:
        print("No snapshot data available")
        return
    
    # Markets by team and edge
    cursor.execute("""
        SELECT team, stage, pm_mid, model_prob
        FROM pm_snapshots
        WHERE ts_utc = ? AND kind = 'advancement'
        ORDER BY ABS(model_prob - pm_mid) DESC
    """, (latest_ts,))
    
    rows = cursor.fetchall()
    
    print(f"\nLatest PM Snapshot: {latest_ts}")
    print("=" * 100)
    print(f"Total markets: {len(rows)}")
    print("\nTop 10 by Edge (model edge vs PM):")
    print(f"{'Team':<20} {'Stage':<8} {'PM':<10} {'Model':<10} {'Edge':<10}")
    print("-" * 100)
    
    for team, stage, pm_mid, model_prob in rows[:10]:
        edge = ((model_prob or 0) - pm_mid) * 100 if pm_mid and model_prob else 0
        print(f"{team:<20} {stage:<8} {format_pct(pm_mid):<10} {format_pct(model_prob):<10} {edge:+.2f}%")

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/wca.db", help="sqlite database")
    ap.add_argument("--team", help="team name (e.g. Brazil)")
    ap.add_argument("--stage", help="tournament stage (R32/R16/QF/SF/Final/win)")
    ap.add_argument("--convergence", action="store_true", help="show convergence analysis")
    ap.add_argument("--market-stats", action="store_true", help="show latest snapshot stats")
    ap.add_argument("--limit", type=int, default=10, help="number of snapshots to show")
    args = ap.parse_args()
    
    con = sqlite3.connect(args.db)
    
    if args.market_stats:
        market_stats(con)
    elif args.team:
        if args.convergence:
            if not args.stage:
                ap.error("--convergence requires --stage")
            convergence_stats(con, args.team, args.stage)
        else:
            query_market(con, args.team, args.stage or "QF", args.limit)
    else:
        ap.print_help()
    
    con.close()

if __name__ == "__main__":
    main()
