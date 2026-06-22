#!/usr/bin/env python3
"""Backfill: canonicalise existing ``bets.platform`` values in the ledger.

DRY-RUN by default — prints the platform-name merges it *would* make. Only
writes when ``--apply`` is passed. Idempotent: re-running after an apply is a
no-op (canonical values map to themselves).

Usage::

    python scripts/wca_canon_venues.py                 # dry-run on data/wca.db
    python scripts/wca_canon_venues.py --apply
    python scripts/wca_canon_venues.py --db /tmp/x.db --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Allow running as a plain script (no PYTHONPATH=src needed).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wca.venues import canon_platform  # noqa: E402


def plan_merges(db_path: str) -> dict[str, dict[str, int]]:
    """Return ``{canonical: {raw: row_count}}`` for rows whose platform changes."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT platform AS p, COUNT(*) AS n FROM bets GROUP BY platform"
        ).fetchall()
    finally:
        conn.close()

    merges: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        raw = r["p"]
        canon = canon_platform(raw)
        if canon != (raw or ""):  # only report actual changes
            raw_disp = "" if raw is None else raw
            merges[canon][raw_disp] = merges[canon].get(raw_disp, 0) + r["n"]
    return dict(merges)


def apply_merges(db_path: str) -> int:
    """Rewrite every ``bets.platform`` to its canonical form. Returns rows changed."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT platform FROM bets").fetchall()
        changed = 0
        for (raw,) in rows:
            canon = canon_platform(raw)
            if canon != (raw or ""):
                if raw is None:
                    cur = conn.execute(
                        "UPDATE bets SET platform = ? WHERE platform IS NULL", (canon,)
                    )
                else:
                    cur = conn.execute(
                        "UPDATE bets SET platform = ? WHERE platform = ?", (canon, raw)
                    )
                changed += cur.rowcount
        conn.commit()
        return changed
    finally:
        conn.close()


def _format_summary(merges: dict[str, dict[str, int]]) -> str:
    if not merges:
        return "No venue-name merges needed — ledger already canonical."
    lines = []
    for canon in sorted(merges):
        parts = [
            f"{raw!r} ({n} row{'s' if n != 1 else ''})"
            for raw, n in sorted(merges[canon].items())
        ]
        lines.append(f"{canon} ← " + ", ".join(parts))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args(argv)

    merges = plan_merges(args.db)
    summary = _format_summary(merges)
    total = sum(n for d in merges.values() for n in d.values())

    if args.apply:
        changed = apply_merges(args.db)
        print(summary)
        print(f"\nAPPLIED: {changed} row(s) rewritten in {args.db}.")
    else:
        print(summary)
        if total:
            print(f"\nDRY-RUN: {total} row(s) would be rewritten. Pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
