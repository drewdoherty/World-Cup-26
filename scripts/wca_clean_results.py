#!/usr/bin/env python
"""Refresh and clean the martj42 results dataset.

Pipeline (idempotent, safe to run on every CI tick):

  1. Download the raw martj42 mirror (``data/raw/results.csv``) — pristine.
  2. Reconcile the last N days of fixtures against two independent feeds
     (ESPN + TheSportsDB). Where BOTH agree and martj42 is wrong/missing, stage
     a correction into ``data/corrections.json``; disagreements go to
     ``data/corrections_review.json`` for a human.
  3. Rebuild ``data/raw/martj42_cleaned.csv`` = raw + corrections overlay.
  4. Write an audit (``data/audit.json``).

The cleaned CSV is what ``/card`` and every model consumer reads
(via ``wca.data.cleaning.resolve_results_path``), so a stale or wrong score in
martj42 no longer silently biases the model or the bet suggestions.

Usage
-----
    python scripts/wca_clean_results.py [--days 21] [--no-network]
                                        [--no-verify] [--force-download]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pandas as pd  # noqa: E402

from wca.data import cleaning, fixture_sources, reconcile  # noqa: E402
from wca.data.results import download_results, download_shootouts  # noqa: E402

REVIEW_PATH = "data/corrections_review.json"
AUDIT_PATH = "data/audit.json"


def _daterange(days: int) -> list:
    today = datetime.datetime.utcnow().date()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(days + 1)]


def verify_window(days: int) -> tuple:
    """Reconcile the last *days* days. Returns (staged, review)."""
    raw_df = pd.read_csv(cleaning.RAW_DEST, dtype=str, keep_default_na=False)
    staged, review = [], []
    for d in _daterange(days):
        gathered = fixture_sources.gather(d)
        rec = reconcile.reconcile_date(raw_df, gathered, d)
        staged.extend(rec.staged)
        review.extend(rec.review)
    return staged, review


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Clean & refresh martj42 results.")
    ap.add_argument("--days", type=int, default=21,
                    help="reconcile this many days back (default 21)")
    ap.add_argument("--no-network", action="store_true",
                    help="skip the raw download (use existing mirror)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip 2-source verification; only re-apply curated corrections")
    ap.add_argument("--force-download", action="store_true")
    args = ap.parse_args(argv)

    # 1. Raw mirror -------------------------------------------------------
    if not args.no_network:
        try:
            download_results(force=args.force_download)
            # Penalty-shootout winners (separate martj42 file). A knockout tie
            # level after 90 min shows only its drawn score in results.csv, so
            # the projected bracket needs this to advance the real winner. A
            # shootouts failure must NEVER break the pipeline, so warn + carry on
            # with any existing mirror.
            try:
                download_shootouts(force=args.force_download)
            except Exception as exc:
                print(f"WARN: shootouts download failed ({exc}); "
                      f"using existing mirror if present", file=sys.stderr)
        except Exception as exc:
            print(f"WARN: raw download failed ({exc}); using existing mirror",
                  file=sys.stderr)

    # 2. Verify against two sources --------------------------------------
    corrections = cleaning.load_corrections()
    new_auto = 0
    review: list = []
    if not args.no_verify and not args.no_network:
        staged, review = verify_window(args.days)
        for s in staged:
            rec = {k: v for k, v in s.items() if not k.startswith("_")}
            corrections, changed = cleaning.merge_correction(corrections, rec)
            if changed:
                new_auto += 1
                print(f"  auto-staged [{s.get('_op')}]: {s['date']} "
                      f"{s['home_team']} vs {s['away_team']} -> "
                      f"{s['corrected_home_score']}-{s['corrected_away_score']} "
                      f"({s['source']})")
        if new_auto:
            cleaning.save_corrections(corrections)
        Path(REVIEW_PATH).write_text(
            json.dumps({"review": review}, indent=2, ensure_ascii=False) + "\n"
        )

    # 3. Rebuild cleaned CSV ---------------------------------------------
    summary = cleaning.build_cleaned()

    # 3b. Derive the processed results file from the freshly-cleaned dataset.
    # Keeps data/processed/wc2026_results.json (settle / backfill / win-rate /
    # rigor / card-anchor consumer) in lock-step with martj42_cleaned.csv so it
    # can no longer drift stale. Best-effort: a failure here must not abort the
    # clean run that already rebuilt the authoritative CSV.
    try:
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import wca_build_wc2026_results as build_results  # noqa: E402

        build_results.main([])
    except Exception as exc:  # noqa: BLE001 — never abort the clean on a derive error
        print(f"WARN: processed results derive failed ({exc})", file=sys.stderr)

    # 4. Audit ------------------------------------------------------------
    Path(AUDIT_PATH).write_text(
        json.dumps({
            "generated_utc": datetime.datetime.utcnow().isoformat() + "Z",
            "raw_rows": summary["raw_rows"],
            "cleaned_rows": summary["cleaned_rows"],
            "updates": summary["updates"],
            "inserts": summary["inserts"],
            "auto_staged_this_run": new_auto,
            "needs_review": len(review),
            "audit": summary["audit"],
        }, indent=2, ensure_ascii=False) + "\n"
    )

    print(f"\nDone: {summary['raw_rows']} raw -> {summary['cleaned_rows']} cleaned "
          f"({summary['updates']} updates, {summary['inserts']} inserts); "
          f"{new_auto} auto-staged, {len(review)} need review.")
    print(f"Cleaned dataset: {summary['out_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
