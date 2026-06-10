"""Orchestrate odds snapshots and persist them to SQLite.

Schema (table: odds_snapshots)
-------------------------------
ts_utc      TEXT    ISO-8601 UTC timestamp when the snapshot was taken
source      TEXT    identifier for the data source (e.g. "polymarket", "theoddsapi")
match_id    TEXT    source-specific match / event identifier
market      TEXT    market type (e.g. "h2h", "winner")
selection   TEXT    outcome label (e.g. "Brazil", "Over 2.5")
decimal_odds REAL   best decimal odds for this selection
raw         TEXT    full JSON of the raw response row for audit / replay

The ledger agent queries this table directly, so the schema MUST NOT change.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    ts_utc       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    match_id     TEXT    NOT NULL,
    market       TEXT    NOT NULL,
    selection    TEXT    NOT NULL,
    decimal_odds REAL,
    raw          TEXT
);
"""

_INSERT_SQL = """
INSERT INTO odds_snapshots
    (ts_utc, source, match_id, market, selection, decimal_odds, raw)
VALUES (?, ?, ?, ?, ?, ?, ?);
"""


class SnapshotRow:
    """A single row to be written to ``odds_snapshots``.

    Parameters
    ----------
    source:
        Short identifier for the data provider.
    match_id:
        The source's own event / match identifier.
    market:
        Market name / type.
    selection:
        Outcome label within the market.
    decimal_odds:
        Best decimal price for this selection.
    raw:
        The raw dict / object that was parsed to produce this row.
        Will be JSON-serialised automatically.
    ts_utc:
        Snapshot timestamp.  Defaults to *now* in UTC if not provided.
    """

    __slots__ = ("ts_utc", "source", "match_id", "market", "selection",
                 "decimal_odds", "raw")

    def __init__(
        self,
        source: str,
        match_id: str,
        market: str,
        selection: str,
        decimal_odds: Optional[float],
        raw: Any,
        ts_utc: Optional[str] = None,
    ) -> None:
        self.source = source
        self.match_id = match_id
        self.market = market
        self.selection = selection
        self.decimal_odds = decimal_odds
        self.raw = raw
        if ts_utc is None:
            self.ts_utc = datetime.now(timezone.utc).isoformat()
        else:
            self.ts_utc = ts_utc

    def to_tuple(self) -> tuple:
        """Return the row as a tuple suitable for the INSERT statement."""
        raw_str = self.raw if isinstance(self.raw, str) else json.dumps(self.raw)
        return (
            self.ts_utc,
            self.source,
            self.match_id,
            self.market,
            self.selection,
            self.decimal_odds,
            raw_str,
        )


# Type alias: a source callable returns a list of SnapshotRow objects.
SourceCallable = Callable[[], List[SnapshotRow]]


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()


def snapshot_all(
    db_path: Union[str, Path] = "data/wca.db",
    sources: Optional[Dict[str, SourceCallable]] = None,
) -> int:
    """Pull current prices from all configured *sources* and append to SQLite.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Created (with parents) if absent.
    sources:
        Mapping of source-name -> callable.  Each callable must return a list
        of :class:`SnapshotRow` objects.  If *None* or empty, this is a no-op
        (but still creates the table and returns 0).

    Returns
    -------
    Total number of rows inserted across all sources.
    """
    if sources is None:
        sources = {}

    db_path = Path(db_path)
    if not db_path.is_absolute():
        from pathlib import Path as _P
        import os
        db_path = _P(os.getcwd()) / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    with sqlite3.connect(str(db_path)) as conn:
        _ensure_table(conn)
        for name, source_fn in sources.items():
            try:
                rows: List[SnapshotRow] = source_fn()
            except Exception:
                logger.exception("Source '%s' raised an exception; skipping.", name)
                continue
            if not rows:
                logger.info("Source '%s' returned 0 rows.", name)
                continue
            tuples = [r.to_tuple() for r in rows]
            conn.executemany(_INSERT_SQL, tuples)
            conn.commit()
            logger.info("Source '%s' inserted %d rows.", name, len(tuples))
            total_rows += len(tuples)

    return total_rows


def read_snapshots(
    db_path: Union[str, Path] = "data/wca.db",
) -> "List[Dict[str, Any]]":
    """Read all rows from ``odds_snapshots`` as a list of dicts.

    Utility for inspection / debugging; not required by the schema contract.
    """
    db_path = Path(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM odds_snapshots ORDER BY ts_utc")
        return [dict(r) for r in cur.fetchall()]
