"""Ledger snapshot job: a durable, point-in-time copy of the bet ledger.

Two artifacts per run, both NON-MUTATING with respect to the live database:

1. A gzip-compressed copy of the whole SQLite DB, taken via the SQLite online
   backup API from a READ-ONLY connection. We never open the live DB for write
   and never run recovery on it.
2. A parquet export of the ``bets`` table (point-in-time, for backtests),
   written through :class:`~wca.archive.store.ArchiveStore`.

This is what the scheduled mini job and the GitHub Action call.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from wca.archive.store import ArchiveStore, _date_of

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _backup_db(src_path: Path, dst_path: Path) -> None:
    """Take a consistent online-backup copy of the live DB into ``dst_path``.

    Never mutates the source's ledger rows. Prefers a read-only source
    connection; if the WAL ``-shm`` cannot be mapped read-only (a known SQLite
    limitation), falls back to a normal connection used *only* for the backup
    read. The backup API itself is read-only w.r.t. the source's content.
    """
    abs_src = src_path.resolve()
    attempts = (
        ("file:%s?mode=ro" % abs_src, {"uri": True}),
        (str(abs_src), {}),  # fallback: normal connection, backup-read only
    )
    last_err: Exception = RuntimeError("no backup attempt ran")
    for dsn, kwargs in attempts:
        src = None
        try:
            src = sqlite3.connect(dsn, **kwargs)
            dst = sqlite3.connect(str(dst_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
            return
        except sqlite3.OperationalError as exc:
            last_err = exc
            if dst_path.exists():
                dst_path.unlink()
        finally:
            if src is not None:
                src.close()
    raise last_err


def _read_bets(db_path: Path) -> List[Dict[str, Any]]:
    """Read the bets table from a (already-copied, private) DB file."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT * FROM bets ORDER BY id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.OperationalError:
        return []  # no bets table yet
    finally:
        conn.close()


def snapshot_ledger(
    db_path: str = "data/wca.db",
    store: Optional[ArchiveStore] = None,
    ts_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Snapshot the ledger DB (gz) + export the bets table to parquet.

    Returns a summary dict. Safe to call on a busy production DB.
    """
    if store is None:
        store = ArchiveStore.from_env()
    ts = ts_utc or _now_iso()
    date = _date_of(ts)
    stamp = ts.replace(":", "").replace("-", "")

    src = Path(db_path)
    result: Dict[str, Any] = {"snapshot_ts": ts, "db_path": str(src)}
    if not src.exists():
        logger.warning("ledger snapshot: DB not found at %s", src)
        result["db_gz"] = None
        result["n_bets"] = 0
        result["bets_parts"] = []
        return result

    # 1) Consistent online-backup copy of the live DB -> read bets from the
    #    copy (so the parquet export matches the gz exactly) -> gzip.
    db_dir = store.root / "ledger_db" / ("date=%s" % date)
    db_dir.mkdir(parents=True, exist_ok=True)
    raw_copy = db_dir / ("wca-%s.db" % stamp)
    gz_copy = db_dir / ("wca-%s.db.gz" % stamp)

    _backup_db(src, raw_copy)
    bets = _read_bets(raw_copy)

    with raw_copy.open("rb") as f_in, gzip.open(str(gz_copy), "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw_copy.unlink()  # keep only the compressed copy

    rel_gz = str(gz_copy.relative_to(store.root))
    if store.backend.has_cloud:
        store.backend.upload(rel_gz, str(gz_copy))

    # 2) Point-in-time bets parquet export.
    bets_parts = store.write_bets(bets, snapshot_ts=ts)

    result.update(
        {
            "db_gz": str(gz_copy),
            "db_gz_rel": rel_gz,
            "n_bets": len(bets),
            "bets_parts": bets_parts,
        }
    )
    logger.info(
        "ledger snapshot: %s (%d bets) -> %s + %d bets parts",
        ts, len(bets), rel_gz, len(bets_parts),
    )
    return result
