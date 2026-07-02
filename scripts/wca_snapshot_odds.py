"""CLI: ingest a one-shot odds snapshot for line movement tracking.

Usage::

    python scripts/wca_snapshot_odds.py [--db PATH] [--regions STR]
                                        [--markets STR] [--snapshots-dir DIR]

Pulls live odds from TheOddsAPI, dumps the raw frame to
``data/raw/snapshots/oddsapi_<markets>_<regions>_<UTCSTAMP>.json`` (the
git-tracked audit trail that ``linemove.robust_event_meta`` reads), and
appends flattened rows to the ``odds_snapshots`` table via the canonical
schema helpers in :mod:`wca.data.snapshot`.

This is the single-shot sibling of the long-running ``wca_snapshotd.py``
daemon and follows its conventions. Unlike the daemon it fails LOUDLY —
a cron/CI wrapper should see a nonzero exit, not a silent no-op.

Requires ODDS_API_KEY in the environment (or .env file at repo root).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

# Make src/ importable when run straight from a checkout.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _utc_stamp(now: datetime) -> str:
    """Compact filesystem-safe UTC timestamp, e.g. 20260611T142233Z."""
    return now.strftime("%Y%m%dT%H%M%SZ")


def _raw_snapshot_name(markets: str, regions: str, now: datetime) -> str:
    """``oddsapi_h2h_uk_<STAMP>.json`` / ``oddsapi_multi_uk_<STAMP>.json``.

    Matches the existing files in data/raw/snapshots/: a single market keeps
    its name, several collapse to "multi" (the daemon's convention).
    """
    market_slug = "multi" if "," in markets else markets.strip()
    region_slug = regions.replace(",", "-").strip()
    return "oddsapi_%s_%s_%s.json" % (market_slug, region_slug, _utc_stamp(now))


def capture_history(
    odds_df,
    jsonl_path: str,
    now: Optional[datetime] = None,
) -> int:
    """Append the pulled frame to the durable, DB-LESS JSONL history.

    This is the closing-line backbone (mirrors :mod:`wca.pmhistory`): it needs
    only a writable filesystem — never SQLite — so the history persists even
    when the ledger DB is absent (CI runner) or the mini is down. Callers run
    this FIRST and independently of any DB touch. Returns rows written.
    """
    from wca.data import oddshistory

    now = now or datetime.now(timezone.utc)
    ts_utc = now.isoformat()
    rows = oddshistory.rows_from_odds_frame(odds_df)
    return oddshistory.append_jsonl(jsonl_path, rows, ts_utc)


def ingest_snapshot(
    odds_df,
    db_path: str,
    snapshots_dir: Optional[str],
    markets: str,
    regions: str,
    now: Optional[datetime] = None,
    jsonl_path: Optional[str] = "data/odds_price_history.jsonl",
) -> Tuple[int, Optional[Path], int]:
    """Persist one pulled odds frame: durable JSONL + raw JSON + SQLite rows.

    Returns ``(rows_inserted, raw_json_path, jsonl_rows)``.  Split from
    :func:`main` so tests can round-trip a synthetic frame without network
    access.

    Capture order is deliberate: the **DB-less** JSONL history is written FIRST
    and independently, so the closing line persists on disk even if the raw dump
    or the SQLite write fails on a runner with no ledger DB.
    """
    from wca.data.snapshot import rows_from_odds_frame, snapshot_all

    now = now or datetime.now(timezone.utc)
    ts_utc = now.isoformat()

    # 1) Durable, DB-less history FIRST so it survives any later failure.
    jsonl_rows = capture_history(odds_df, jsonl_path, now=now) if jsonl_path else 0

    # 2) Raw audit dump (git-tracked replay trail).
    raw_path: Optional[Path] = None
    if snapshots_dir:
        snap_dir = Path(snapshots_dir)
        snap_dir.mkdir(parents=True, exist_ok=True)
        raw_path = snap_dir / _raw_snapshot_name(markets, regions, now)
        raw_path.write_text(odds_df.to_json(orient="records", date_format="iso"))

    # 3) Canonical SQLite ledger (fragile on a DB-less runner; runs last).
    rows = rows_from_odds_frame(odds_df, ts_utc)
    inserted = snapshot_all(db_path, sources={"theoddsapi": lambda: rows})
    return inserted, raw_path, jsonl_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a one-shot odds snapshot for line movement tracking."
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument(
        "--regions",
        default="uk",
        help="Comma-separated Odds API regions (default: uk)",
    )
    parser.add_argument(
        "--markets",
        default="h2h",
        help="Comma-separated markets to snapshot (default: h2h)",
    )
    parser.add_argument(
        "--snapshots-dir",
        default="data/raw/snapshots",
        help="Directory for the raw JSON audit dump (default: "
             "data/raw/snapshots; pass an empty string to skip)",
    )
    parser.add_argument(
        "--jsonl",
        default="data/odds_price_history.jsonl",
        help="Durable DB-less odds history JSONL (default: "
             "data/odds_price_history.jsonl; pass an empty string to skip)",
    )
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    args = parser.parse_args()

    _load_dotenv(args.env)

    try:
        from wca.data import theoddsapi
    except ImportError as exc:
        print("ERROR: could not import wca modules: %s" % exc, file=sys.stderr)
        sys.exit(1)

    try:
        odds_df, quota = theoddsapi.get_odds(
            "soccer_fifa_world_cup",
            regions=args.regions,
            markets=args.markets,
        )
    except Exception as exc:
        print("ERROR: odds pull failed: %s" % exc, file=sys.stderr)
        sys.exit(1)

    if odds_df.empty:
        # Legitimate only outside the tournament window; still worth a loud
        # line in the cron log.
        print("No odds returned (empty frame) — nothing ingested.")
        sys.exit(0)

    now = datetime.now(timezone.utc)

    # Durable, DB-less history FIRST and on its own — this is the closing-line
    # backbone, and it must persist even if the SQLite/raw step later fails on a
    # runner with no ledger DB. If THIS fails, fail loud: it is the path the CI
    # commit depends on.
    jsonl_rows = 0
    if args.jsonl:
        try:
            jsonl_rows = capture_history(odds_df, args.jsonl, now=now)
            print("Captured %d odds rows to durable history %s"
                  % (jsonl_rows, args.jsonl))
        except Exception as exc:
            print("ERROR: durable JSONL capture failed: %s" % exc, file=sys.stderr)
            sys.exit(1)

    # SQLite + raw dump second. A failure here no longer loses the closing line
    # (already on disk above), so report it but do not discard the run.
    inserted = 0
    raw_path = None
    db_ok = True
    try:
        inserted, raw_path, _ = ingest_snapshot(
            odds_df,
            db_path=args.db,
            snapshots_dir=args.snapshots_dir or None,
            markets=args.markets,
            regions=args.regions,
            now=now,
            jsonl_path=None,  # already captured above
        )
    except Exception as exc:
        db_ok = False
        print("WARN: SQLite/raw ingest failed (durable JSONL already written): %s"
              % exc, file=sys.stderr)

    print(
        "Ingested %d odds rows into %s (raw: %s, jsonl: %d, quota remaining: %s)"
        % (
            inserted,
            args.db,
            raw_path if raw_path else "skipped",
            jsonl_rows,
            quota.remaining if quota else "unknown",
        )
    )
    # The run is a success as long as the durable history captured rows. The
    # SQLite side is allowed to be down on a DB-less runner.
    if args.jsonl and jsonl_rows == 0:
        print(
            "ERROR: pulled a non-empty frame but captured 0 durable rows — "
            "schema/column mismatch?",
            file=sys.stderr,
        )
        sys.exit(1)
    if db_ok and not args.jsonl and inserted == 0:
        print(
            "ERROR: pulled a non-empty frame but inserted 0 rows — "
            "schema/column mismatch?",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
