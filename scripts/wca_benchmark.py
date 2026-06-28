#!/usr/bin/env python3
"""Run the model/card benchmark and write a JSON + Markdown report.

Usage:
    python scripts/wca_benchmark.py [--db data/wca.db] [--archive data/archive]
        [--jsonl data/model_predictions_log.jsonl]
        [--results data/raw/martj42_cleaned.csv]
        [--out-md reports/benchmark_latest.md] [--out-json reports/benchmark_latest.json]

Reads the #71 parquet archive when present, else the legacy sources. Read-only:
never mutates the ledger or archive. Point --db at a *copy* of the ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wca.bench.report import build_report, render_markdown  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/wca.db")
    ap.add_argument("--archive", default="data/archive")
    ap.add_argument("--jsonl", default="data/model_predictions_log.jsonl")
    ap.add_argument("--results", default="data/raw/martj42_cleaned.csv")
    ap.add_argument("--out-md", default="reports/benchmark_latest.md")
    ap.add_argument("--out-json", default="reports/benchmark_latest.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = build_report(db_path=args.db, archive_dir=args.archive,
                          jsonl_path=args.jsonl, results_csv=args.results,
                          generated_at=generated)
    md = render_markdown(report)

    for path in (args.out_md, args.out_json):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    with open(args.out_md, "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    if not args.quiet:
        print(md)
        print(f"\n[wrote {args.out_md} and {args.out_json}]")


if __name__ == "__main__":
    main()
