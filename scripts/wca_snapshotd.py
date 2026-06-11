"""World Cup Alpha odds-snapshot daemon.

Continuously polls The Odds API for FIFA World Cup head-to-head prices,
persists each pull two ways:

  1. a raw JSON snapshot at
     ``data/raw/snapshots/oddsapi_h2h_uk_<UTCSTAMP>.json`` (for audit/replay),
  2. flattened rows appended to the ``odds_snapshots`` SQLite table at
     ``data/wca.db`` via :mod:`wca.data.snapshot` (the verified schema the
     ledger agent reads).

Between pulls it asks :func:`wca.pollsched.next_poll_delay` how long to wait,
feeding it the kickoff times scraped from the just-pulled fixtures and the
live API quota.  This makes the cadence adaptive: fast while matches are live
or about to start, slow (and quota-aware) otherwise.

Usage::

    python scripts/wca_snapshotd.py            # loop forever
    python scripts/wca_snapshotd.py --once      # single iteration (cron/test)
    python scripts/wca_snapshotd.py --db x.db --env .env

SIGTERM and Ctrl-C exit cleanly.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# Make ``src`` importable when run as a plain script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pandas as pd  # noqa: E402

from wca.data import theoddsapi  # noqa: E402
from wca.data.snapshot import SnapshotRow, snapshot_all  # noqa: E402
from wca.pollsched import PollPolicy, next_poll_delay  # noqa: E402

logger = logging.getLogger("wca.snapshotd")

_SPORT_KEY = "soccer_fifa_world_cup"
_REGIONS = "uk"
_MARKETS = "h2h"
_SOURCE = "theoddsapi"

# Flag flipped by the signal handler so the loop can break cleanly.
_STOP = {"requested": False}


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so we don't add a python-dotenv dependency."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _utc_stamp() -> str:
    """Compact filesystem-safe UTC timestamp, e.g. 20260611T142233Z."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_raw_json(events_df: pd.DataFrame, repo_root: Path) -> Path:
    """Dump the pulled DataFrame to a raw JSON snapshot file."""
    snap_dir = repo_root / "data" / "raw" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    out_path = snap_dir / ("oddsapi_h2h_uk_" + _utc_stamp() + ".json")
    # ``to_json`` handles the datetime columns; orient=records keeps it flat.
    out_path.write_text(events_df.to_json(orient="records", date_format="iso"))
    return out_path


def _kickoffs_from_df(df: pd.DataFrame) -> List[str]:
    """Extract unique ISO kickoff strings from the pulled DataFrame."""
    if df.empty or "commence_time" not in df.columns:
        return []
    out: List[str] = []
    seen = set()
    for value in df["commence_time"].tolist():
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        # commence_time is a pandas Timestamp after parsing; normalise to ISO.
        if hasattr(value, "isoformat"):
            iso = value.isoformat()
        else:
            iso = str(value)
        if iso not in seen:
            seen.add(iso)
            out.append(iso)
    return out


def _rows_from_df(df: pd.DataFrame, ts_utc: str) -> List[SnapshotRow]:
    """Build SnapshotRow objects for every h2h outcome in the DataFrame."""
    rows: List[SnapshotRow] = []
    if df.empty:
        return rows
    for record in df.to_dict(orient="records"):
        if record.get("market") != "h2h":
            continue
        odds = record.get("decimal_odds")
        try:
            odds = float(odds) if odds is not None and not pd.isna(odds) else None
        except (TypeError, ValueError):
            odds = None
        rows.append(
            SnapshotRow(
                source=_SOURCE,
                match_id=str(record.get("event_id")),
                market="h2h",
                selection=str(record.get("outcome_name")),
                decimal_odds=odds,
                raw=_jsonable(record),
                ts_utc=ts_utc,
            )
        )
    return rows


def _jsonable(record: dict) -> dict:
    """Coerce a DataFrame record into a JSON-serialisable dict."""
    out = {}
    for key, value in record.items():
        if value is None:
            out[key] = None
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            try:
                if pd.isna(value):
                    out[key] = None
                    continue
            except (TypeError, ValueError):
                pass
            out[key] = value
    return out


# In-game site-sync cadence tracker (module-level so poll_once stays simple).
_SYNC_STATE: dict = {}


def poll_once(db_path: str, repo_root: Path, policy: PollPolicy) -> int:
    """Run a single poll: pull, persist, and return the next delay in seconds.

    Returns the number of seconds the daemon should sleep before the next
    iteration.  Never raises on odds-pull failure -- it logs and returns the
    idle interval so the caller keeps looping.
    """
    ts = _now_iso()
    try:
        df, quota = theoddsapi.get_odds(
            _SPORT_KEY, regions=_REGIONS, markets=_MARKETS
        )
    except Exception:  # noqa: BLE001 -- never let a pull crash the daemon
        logger.exception("odds pull failed; backing off for idle interval")
        return policy.idle_seconds

    # 1. raw JSON snapshot
    try:
        _save_raw_json(df, repo_root)
    except Exception:  # noqa: BLE001
        logger.exception("failed to write raw JSON snapshot (continuing)")

    # 2. SQLite append via the verified schema helper.
    rows = _rows_from_df(df, ts)
    try:
        n_written = snapshot_all(db_path, sources={_SOURCE: lambda: rows})
    except Exception:  # noqa: BLE001
        logger.exception("failed to append snapshot rows to SQLite (continuing)")
        n_written = 0

    kickoffs = _kickoffs_from_df(df)
    quota_remaining: Optional[int] = quota.remaining
    delay, reason = next_poll_delay(ts, kickoffs, quota_remaining, policy)

    logger.info(
        "%s polled, quota=%s, rows=%d, next in %ds (%s)",
        ts,
        quota_remaining,
        n_written,
        delay,
        reason,
    )

    # During live/pre-close phases, periodically regenerate + push the site's
    # line-history so the chart tracks the match (every 3rd fast poll ≈ 9 min,
    # well inside Vercel's deploy limits). Best-effort by design.
    if reason in ("in_game", "pre_close"):
        _SYNC_STATE["fast_polls"] = _SYNC_STATE.get("fast_polls", 0) + 1
        if _SYNC_STATE["fast_polls"] % 3 == 1:  # 1st, 4th, 7th... fast poll
            try:
                from wca import sync

                if sync.push_site(reason="in-game line history", db_path=db_path):
                    logger.info("site line-history pushed")
            except Exception:  # noqa: BLE001
                logger.exception("site sync failed (continuing)")
    else:
        _SYNC_STATE["fast_polls"] = 0
    return delay


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):  # noqa: ANN001
        logger.info("received signal %s; shutting down after current sleep", signum)
        _STOP["requested"] = True

    signal.signal(signal.SIGTERM, _handler)
    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):  # pragma: no cover - non-main thread
        pass


def _interruptible_sleep(seconds: int) -> None:
    """Sleep in short slices so a stop signal is honoured promptly."""
    remaining = float(seconds)
    while remaining > 0 and not _STOP["requested"]:
        slice_s = min(1.0, remaining)
        time.sleep(slice_s)
        remaining -= slice_s


def run(db_path: str, once: bool, repo_root: Path, policy: PollPolicy) -> None:
    _install_signal_handlers()
    try:
        while True:
            delay = poll_once(db_path, repo_root, policy)
            if once or _STOP["requested"]:
                break
            _interruptible_sleep(delay)
            if _STOP["requested"]:
                break
    except KeyboardInterrupt:  # pragma: no cover - defensive
        logger.info("interrupted; exiting cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="World Cup Alpha snapshot daemon")
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single poll iteration and exit (for cron / testing)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    _load_dotenv(args.env)
    run(db_path=args.db, once=args.once, repo_root=_REPO_ROOT, policy=PollPolicy())


if __name__ == "__main__":
    main()
