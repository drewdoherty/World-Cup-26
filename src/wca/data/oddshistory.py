"""Durable, DB-less odds price-history store — the closing-line backbone.

Match 1X2 / totals markets close at kickoff, so a *true closing line* (the last
pre-KO venue price) is the dependency under nearly every microstructure / CLV
edge. Capture was silently dead for ~12 days because both durable paths needed
something fragile to be up: the ``odds_snapshots`` table lives in the gitignored
``data/wca.db`` (only the mini ``snapshotd`` daemon writes it), and the cloud
raw-dump step crashed under ``continue-on-error`` before anything accrued.

This module mirrors :mod:`wca.pmhistory`: an append-only, network-free, **DB-less**
JSONL store (``data/odds_price_history.jsonl``) that needs no SQLite to WRITE, so
a CI runner with no ledger DB can still capture and commit the history. A separate
IDEMPOTENT :func:`ingest` then folds the JSONL into ``odds_snapshots`` wherever the
DB lives (mini / dev), deduping on ``(ts_utc, fixture, market, book, selection)``.

One JSONL record per fixture x market x book x selection x ts_utc — exactly the
grain a closing-line lookup needs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Default committed history file (relative to repo root).
DEFAULT_JSONL_PATH = "data/odds_price_history.jsonl"

#: Identity of a single captured price across pulls.  Dedup is on this tuple
#: plus ``ts_utc`` so re-ingesting the same JSONL is a no-op.
_KEY_FIELDS = ("ts_utc", "fixture", "market", "book", "selection")


def _selection_key(outcome_name: Any, outcome_point: Any) -> str:
    """Fold the line into the selection, mirroring the snapshot daemon.

    "Over" alone is ambiguous across 2.5 / 3.5 lines; "Over 2.5" is a closing
    line a bet can be matched against.
    """
    name = "" if outcome_name is None else str(outcome_name)
    if _is_na(outcome_point):
        return name
    try:
        return "%s %g" % (name, float(outcome_point))
    except (TypeError, ValueError):
        return name


def _is_na(value: Any) -> bool:
    """True for None or a pandas/NumPy NaN, without importing pandas eagerly."""
    if value is None:
        return True
    try:
        return value != value  # NaN is the only value not equal to itself
    except Exception:
        return False


def rows_from_odds_frame(df: Any) -> List[Dict[str, object]]:
    """Flatten a ``theoddsapi.get_odds`` DataFrame into history records.

    Returns dicts with the JSONL grain (``fixture``, ``market``, ``book``,
    ``selection``, ``decimal_odds``) plus useful context (teams, commence_time)
    so a closing line can be located without re-joining. ``ts_utc`` is stamped
    at write time by :func:`append_jsonl`. Rows missing a fixture id are dropped.
    Pure / network-free so it is fully unit-testable.
    """
    out: List[Dict[str, object]] = []
    if df is None or getattr(df, "empty", False):
        return out
    for record in df.to_dict(orient="records"):
        event_id = record.get("event_id")
        if _is_na(event_id):
            continue
        odds = record.get("decimal_odds")
        try:
            odds = float(odds) if not _is_na(odds) else None
        except (TypeError, ValueError):
            odds = None
        commence = record.get("commence_time")
        if hasattr(commence, "isoformat"):
            commence = commence.isoformat()
        elif _is_na(commence):
            commence = None
        out.append({
            "fixture": str(event_id),
            "market": (None if _is_na(record.get("market")) else str(record.get("market"))),
            "book": (None if _is_na(record.get("bookmaker_key")) else str(record.get("bookmaker_key"))),
            "selection": _selection_key(record.get("outcome_name"), record.get("outcome_point")),
            "decimal_odds": odds,
            "home_team": (None if _is_na(record.get("home_team")) else str(record.get("home_team"))),
            "away_team": (None if _is_na(record.get("away_team")) else str(record.get("away_team"))),
            "commence_time": commence,
        })
    return out


def append_jsonl(path: str, rows: Sequence[Dict[str, object]], ts_utc: str) -> int:
    """Append captured odds to a JSONL history file (one record per line).

    DB-LESS by design: needs only a writable filesystem, never SQLite, so it
    survives a runner with no ledger DB and the mini being down. Each row needs
    at least ``fixture``; ``ts_utc`` is stamped here. Returns rows written.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    n = 0
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fixture = r.get("fixture")
            if fixture is None or str(fixture) == "":
                continue
            odds = r.get("decimal_odds")
            try:
                odds = float(odds) if odds is not None else None
            except (TypeError, ValueError):
                odds = None
            rec = {
                "ts_utc": ts_utc,
                "fixture": str(fixture),
                "market": r.get("market"),
                "book": r.get("book"),
                "selection": r.get("selection"),
                "decimal_odds": odds,
                "home_team": r.get("home_team"),
                "away_team": r.get("away_team"),
                "commence_time": r.get("commence_time"),
            }
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            n += 1
    return n


def load_records(path: str) -> List[Dict[str, object]]:
    """Load all odds records from a JSONL history file (missing file -> [])."""
    if not os.path.exists(path):
        return []
    out: List[Dict[str, object]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _dedup_key(rec: Dict[str, object]) -> Tuple[object, ...]:
    return tuple(rec.get(k) for k in _KEY_FIELDS)


# ---------------------------------------------------------------------------
# Idempotent ingest: JSONL -> odds_snapshots (wherever the DB lives)
# ---------------------------------------------------------------------------

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

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_odds_snapshots_match_market_ts
    ON odds_snapshots (match_id, market, ts_utc);
"""

_INSERT_SQL = """
INSERT INTO odds_snapshots
    (ts_utc, source, match_id, market, selection, decimal_odds, raw)
VALUES (?, ?, ?, ?, ?, ?, ?);
"""

#: Source tag for rows that arrived via the durable JSONL path.
JSONL_SOURCE = "theoddsapi"


def _existing_keys(con: sqlite3.Connection) -> set:
    """The (ts_utc, fixture, market, book, selection) tuples already present.

    ``book`` is recovered from the JSON ``raw`` blob (the canonical schema has
    no book column), so re-ingesting an already-written record is a true no-op.
    """
    keys = set()
    cur = con.execute(
        "SELECT ts_utc, match_id, market, selection, raw FROM odds_snapshots"
    )
    for ts_utc, match_id, market, selection, raw in cur:
        book = None
        if raw:
            try:
                book = json.loads(raw).get("book")
            except (ValueError, AttributeError):
                book = None
        keys.add((ts_utc, match_id, market, book, selection))
    return keys


def ingest(
    jsonl_path: str = DEFAULT_JSONL_PATH,
    db_path: str = "data/wca.db",
    *,
    records: Optional[Iterable[Dict[str, object]]] = None,
) -> int:
    """Fold the JSONL history into ``odds_snapshots``, idempotently.

    Dedups on ``(ts_utc, fixture, market, book, selection)`` against rows
    already in the table, so re-ingesting the same JSONL inserts nothing. Pass
    ``records`` to ingest in-memory rows instead of reading ``jsonl_path``.
    Returns the number of NEW rows inserted. Requires a writable DB (this is the
    ingestion side; WRITING the JSONL never needs a DB).
    """
    recs = list(records) if records is not None else load_records(jsonl_path)

    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    inserted = 0
    with sqlite3.connect(db_path) as con:
        con.execute(_CREATE_TABLE_SQL)
        con.execute(_CREATE_INDEX_SQL)
        con.commit()
        seen = _existing_keys(con)
        to_insert = []
        for rec in recs:
            fixture = rec.get("fixture")
            if fixture is None or str(fixture) == "":
                continue
            key = (
                rec.get("ts_utc"),
                str(fixture),
                rec.get("market"),
                rec.get("book"),
                rec.get("selection"),
            )
            if key in seen:
                continue
            seen.add(key)
            odds = rec.get("decimal_odds")
            try:
                odds = float(odds) if odds is not None else None
            except (TypeError, ValueError):
                odds = None
            to_insert.append((
                rec.get("ts_utc"),
                JSONL_SOURCE,
                str(fixture),
                "" if rec.get("market") is None else str(rec.get("market")),
                "" if rec.get("selection") is None else str(rec.get("selection")),
                odds,
                json.dumps(rec, sort_keys=True),
            ))
        if to_insert:
            con.executemany(_INSERT_SQL, to_insert)
            con.commit()
            inserted = len(to_insert)
    return inserted
