"""Schema, writers and read helpers for the prediction ledger.

Tables
------
predictions
    One row per (build, fixture, market, selection, line, stage).  The primary
    key ``prediction_id`` is a deterministic sha1 of those identity columns, so
    re-emitting the same prediction is an idempotent upsert (one row, never a
    duplicate).
acca_legs
    Maps an accumulator id to its component prediction rows.

Views
-----
v_model_book
    ``predictions LEFT JOIN bets`` with a ``book`` tag of ``paper`` (no bet
    linked) or ``realized`` (a real bet placed).  Lets one query compare the
    paper model book against the money the model actually moved.
v_realized_book
    The realized subset of ``v_model_book`` (rows with a linked bet).

Closing-line value (CLV)
------------------------
For the prediction ledger CLV is computed *fair-vs-fair*::

    clv = model_fair_odds / closing_odds - 1

``model_fair_odds`` is ``1 / model_prob`` (the price the model would have made)
and ``closing_odds`` is the de-vigged fair consensus close — both vig-free, so
the ratio is an honest measure of whether the model's price beat the market's
final fair price.  CLV is ``NULL`` (never ``0``) when no close exists.

Safety
------
``data/wca.db`` is the production ledger and is read-only on this box.  Every
writer routes through :func:`_connect_write`, which refuses any database whose
basename is ``wca.db`` unless ``WCA_ALLOW_PROD_DB`` is set in the environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from wca.ledger.store import _connect as _ledger_connect

# Default write target on this dev box.
_DEFAULT_DB = "data/dev.db"

# Markets that price a match-winner outcome and therefore carry a market
# de-vig / edge / closing-line (everything else gets NULL market columns).
_X12_MARKETS = frozenset({"1x2", "h2h", "match odds", "match winner"})

_LEGS = ("home", "draw", "away")


# ---------------------------------------------------------------------------
# Production-DB write guard.
# ---------------------------------------------------------------------------


def _is_prod_db(db_path: str) -> bool:
    """True when *db_path* points at the production ledger (basename wca.db)."""
    return os.path.basename(str(db_path)).lower() == "wca.db"


def _guard_db(db_path: str) -> None:
    """Raise unless writing *db_path* is permitted on this box.

    The production ledger ``data/wca.db`` (723 MB, real money) must never be
    mutated from the dev box.  Set ``WCA_ALLOW_PROD_DB`` to override (the prod
    box does); the test-suite asserts the refusal without the override.
    """
    if _is_prod_db(db_path) and not os.environ.get("WCA_ALLOW_PROD_DB"):
        raise PermissionError(
            "refusing to write production ledger %r from this box; set "
            "WCA_ALLOW_PROD_DB=1 to override" % db_path
        )


def _connect_write(db_path: str) -> sqlite3.Connection:
    """Guarded write connection (reuses ledger PRAGMA setup + busy_timeout)."""
    _guard_db(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _ledger_connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _connect_read(db_path: str) -> sqlite3.Connection:
    """Read connection (no guard — reads are always safe)."""
    conn = _ledger_connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# Deterministic identity hashing.
# ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    """Stable string form for an identity component (``None`` -> '')."""
    if value is None:
        return ""
    if isinstance(value, float):
        # -1.0 and -1 must hash identically; render canonically.
        if value == int(value):
            return str(int(value))
        return repr(value)
    return str(value)


def prediction_id(
    build_id: Any,
    match_id: Any,
    stage: Any,
    market: Any,
    selection: Any,
    line: Any,
) -> str:
    """sha1(build_id|match_id|stage|market|selection|line)[:16] (hex)."""
    raw = "|".join(
        _norm(x) for x in (build_id, match_id, stage, market, selection, line)
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def acca_id(build_id: Any, pred_ids: Sequence[str]) -> str:
    """Deterministic id for an accumulator from its (sorted) leg ids."""
    raw = _norm(build_id) + "||" + "|".join(sorted(str(p) for p in pred_ids))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Schema.
# ---------------------------------------------------------------------------

_DDL_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id       TEXT PRIMARY KEY,
    build_id            TEXT,
    ts_utc              TEXT,
    match_id            TEXT,
    fixture             TEXT,
    kickoff_utc         TEXT,
    market              TEXT,
    selection           TEXT,
    line                REAL DEFAULT -1,
    stage               TEXT DEFAULT '',
    n_outcomes          INTEGER,
    model_prob          REAL,
    model_fair_odds     REAL,
    elo_prob            REAL,
    dc_prob             REAL,
    market_devig_prob   REAL,
    market_best_odds    REAL,
    market_book         TEXT,
    devig_method        TEXT,
    edge                REAL,
    ev_per_unit         REAL,
    bet_id              INTEGER,
    placed              INTEGER DEFAULT 0,
    closing_devig_prob  REAL,
    closing_odds        REAL,
    clv                 REAL,
    close_ts            TEXT,
    close_lag_seconds   INTEGER,
    n_books_at_close    INTEGER,
    close_is_prematch   INTEGER,
    status              TEXT DEFAULT 'open',
    settled_ts          TEXT,
    settle_source       TEXT,
    model_source        TEXT,
    notes               TEXT
)
"""

_DDL_ACCA_LEGS = """
CREATE TABLE IF NOT EXISTS acca_legs (
    acca_id        TEXT,
    prediction_id  TEXT,
    build_id       TEXT,
    bet_id         INTEGER,
    PRIMARY KEY (acca_id, prediction_id)
)
"""

# bets is owned by the money ledger; ensure it exists so the LEFT JOIN views
# resolve even on a fresh dev.db that has no money ledger yet.
_DDL_BETS_STUB = """
CREATE TABLE IF NOT EXISTS bets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc              TEXT,
    match_id            TEXT,
    match_desc          TEXT,
    market              TEXT,
    selection           TEXT,
    platform            TEXT,
    decimal_odds        REAL,
    stake               REAL,
    model_prob          REAL,
    market_prob_devig   REAL,
    ev                  REAL,
    kelly_fraction      REAL,
    status              TEXT DEFAULT 'open',
    settled_pl          REAL,
    closing_odds        REAL,
    clv                 REAL,
    notes               TEXT,
    account             TEXT,
    source              TEXT,
    settled_ts          TEXT
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_pred_build ON predictions(build_id)",
    "CREATE INDEX IF NOT EXISTS ix_pred_match ON predictions(match_id)",
    "CREATE INDEX IF NOT EXISTS ix_pred_market ON predictions(market)",
    "CREATE INDEX IF NOT EXISTS ix_pred_status ON predictions(status)",
    "CREATE INDEX IF NOT EXISTS ix_pred_bet ON predictions(bet_id)",
    "CREATE INDEX IF NOT EXISTS ix_acca_acca ON acca_legs(acca_id)",
    "CREATE INDEX IF NOT EXISTS ix_acca_pred ON acca_legs(prediction_id)",
)

# A prediction is "realized" when it carries a linked bet_id (placed=1).
_DDL_V_MODEL_BOOK = """
CREATE VIEW IF NOT EXISTS v_model_book AS
SELECT
    p.*,
    b.id            AS b_bet_id,
    b.stake         AS b_stake,
    b.decimal_odds  AS b_decimal_odds,
    b.status        AS b_status,
    b.settled_pl    AS b_settled_pl,
    b.clv           AS b_clv,
    CASE WHEN p.bet_id IS NULL THEN 'paper' ELSE 'realized' END AS book
FROM predictions p
LEFT JOIN bets b ON b.id = p.bet_id
"""

_DDL_V_REALIZED_BOOK = """
CREATE VIEW IF NOT EXISTS v_realized_book AS
SELECT * FROM v_model_book WHERE book = 'realized'
"""


def ensure_schema(db_path: str = _DEFAULT_DB) -> None:
    """Create tables, indexes and views (idempotent, safe to re-run).

    Ordering matters for in-place upgrades: ``CREATE TABLE IF NOT EXISTS`` is a
    no-op against an existing old-#41-schema ``predictions`` table, so the new
    columns (e.g. ``build_id``) only appear after ``_migrate_predictions_schema``
    runs. The indexes/views reference those new columns, so the migration MUST
    run *before* them — otherwise ``CREATE INDEX ix_pred_build`` raises
    ``no such column: build_id`` on an existing DB before the migration can fix
    it. On a fresh DB the migration is a harmless no-op (the CREATE TABLE already
    built the full column set).
    """
    # 1. Base tables (no-op if they already exist with an older column set).
    with _connect_write(db_path) as conn:
        conn.execute(_DDL_BETS_STUB)
        conn.execute(_DDL_PREDICTIONS)
        conn.execute(_DDL_ACCA_LEGS)
        conn.commit()
    # 2. Upgrade an existing predictions table to the current column set BEFORE
    #    any index/view references the new columns.
    _migrate_predictions_schema(db_path)
    # 3. Indexes + views (now guaranteed the referenced columns exist).
    with _connect_write(db_path) as conn:
        for ddl in _INDEXES:
            conn.execute(ddl)
        conn.execute(_DDL_V_MODEL_BOOK)
        conn.execute(_DDL_V_REALIZED_BOOK)
        conn.commit()


def _migrate_predictions_schema(db_path: str = _DEFAULT_DB) -> None:
    """Migrate predictions table from old #41 schema to new a02794e+ schema.

    The old schema (from PR #41) had fewer columns. This migration adds missing
    columns via ALTER TABLE, handling the case where some columns may already
    exist (idempotent). Columns are added with sensible defaults so existing
    rows continue to work.

    New columns added (if missing):
    - build_id (TEXT)
    - fixture (TEXT)
    - kickoff_utc (TEXT)
    - elo_prob (REAL)
    - dc_prob (REAL)
    - market_devig_prob (REAL)
    - market_best_odds (REAL)
    - market_book (TEXT)
    - edge (REAL)
    - ev_per_unit (REAL)
    - closing_devig_prob (REAL)
    - close_lag_seconds (INTEGER)
    - n_books_at_close (INTEGER)
    - close_is_prematch (INTEGER)
    - status (TEXT, default 'open')
    - settle_source (TEXT)
    - model_source (TEXT)
    - model_fair_odds (REAL)

    Column renames/replacements for old schema compatibility:
    - model_odds -> model_fair_odds (dropped, re-created)
    - offered_odds (dropped, never re-created)
    - close_ts_utc -> close_ts (dropped, re-created)
    - outcome (dropped, never re-created)
    - result_ts_utc -> settled_ts (dropped, re-created)
    """
    with _connect_write(db_path) as conn:
        # Check if the predictions table exists at all.
        pred_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
        ).fetchone()

        if pred_table is None:
            # No old table to migrate; schema creation already ran.
            return

        # Check which columns already exist in the current table.
        existing_cols_raw = conn.execute(
            "PRAGMA table_info(predictions)"
        ).fetchall()
        existing_cols = {row[1] for row in existing_cols_raw}  # row[1] is the column name.

        # Bring the table up to the FULL current column set without drift: build
        # the canonical schema under a throwaway name, read its columns, and
        # ALTER-add any the (older) live table is missing. This covers EVERY
        # column the new code + indexes/views reference — build_id, bet_id,
        # status, market, match_id, the model_* / market_* / close_* set — not a
        # hand-maintained subset that can fall out of sync with _DDL_PREDICTIONS
        # (the original cause of the `no such column: bet_id` crash).
        conn.executescript(
            "DROP TABLE IF EXISTS _pred_schema_probe;\n"
            + _DDL_PREDICTIONS.replace("predictions", "_pred_schema_probe", 1)
        )
        target_cols = conn.execute(
            "PRAGMA table_info(_pred_schema_probe)"
        ).fetchall()
        conn.execute("DROP TABLE _pred_schema_probe")

        for row in target_cols:
            name, col_type = row[1], (row[2] or "TEXT")
            if name not in existing_cols:
                # ALTER ADD COLUMN drops NOT NULL/PK/DEFAULT constraints (added
                # columns are nullable), which is exactly right for back-filling
                # existing rows.
                conn.execute(
                    f"ALTER TABLE predictions ADD COLUMN {name} {col_type}"
                )

        # `status` carries a default of 'open' on fresh inserts; back-fill any
        # rows that pre-date the column (added above as NULL).
        conn.execute("UPDATE predictions SET status='open' WHERE status IS NULL")
        conn.commit()


# ---------------------------------------------------------------------------
# Column projection for upsert.
# ---------------------------------------------------------------------------

# Columns a caller may set (prediction_id is derived, never passed raw).
_WRITABLE_COLS = (
    "build_id",
    "ts_utc",
    "match_id",
    "fixture",
    "kickoff_utc",
    "market",
    "selection",
    "line",
    "stage",
    "n_outcomes",
    "model_prob",
    "model_fair_odds",
    "elo_prob",
    "dc_prob",
    "market_devig_prob",
    "market_best_odds",
    "market_book",
    "devig_method",
    "edge",
    "ev_per_unit",
    "bet_id",
    "placed",
    "closing_devig_prob",
    "closing_odds",
    "clv",
    "close_ts",
    "close_lag_seconds",
    "n_books_at_close",
    "close_is_prematch",
    "status",
    "settled_ts",
    "settle_source",
    "model_source",
    "notes",
)

# Identity columns that participate in the prediction_id hash.
_IDENTITY = ("build_id", "match_id", "stage", "market", "selection", "line")


def _row_prediction_id(row: Dict[str, Any]) -> str:
    """Compute the deterministic id for a (possibly partial) row dict."""
    line = row.get("line")
    if line is None:
        line = -1
    return prediction_id(
        row.get("build_id"),
        row.get("match_id"),
        row.get("stage", "") or "",
        row.get("market"),
        row.get("selection"),
        line,
    )


def upsert_predictions(rows: Iterable[Dict[str, Any]], db_path: str = _DEFAULT_DB) -> List[str]:
    """Insert-or-replace prediction rows; return list of prediction_id strings.

    The ``prediction_id`` is always recomputed from the identity columns, so
    the same logical prediction upserted twice collapses to one row (verified
    by the test-suite).  ``line`` defaults to ``-1`` and ``stage`` to ``''`` to
    match the NULL-line/stage convention used for 1X2 / scoreline / O-U / BTTS.

    A re-upsert preserves columns the new row leaves unset *only* where the
    caller omits the key entirely — explicit ``None`` overwrites.  This lets a
    settle/close pass send a partial row (id + the few columns it stamps)
    without nuking earlier model columns.
    """
    ensure_schema(db_path)
    rows = list(rows)
    if not rows:
        return []
    pred_ids: List[str] = []
    with _connect_write(db_path) as conn:
        for row in rows:
            pid = _row_prediction_id(row)
            existing = conn.execute(
                "SELECT * FROM predictions WHERE prediction_id=?", (pid,)
            ).fetchone()
            merged: Dict[str, Any] = {}
            if existing is not None:
                merged = {k: existing[k] for k in existing.keys()}
            for col in _WRITABLE_COLS:
                if col in row:
                    merged[col] = row[col]
            # Identity columns always come from the row that defined the id.
            merged["prediction_id"] = pid
            if merged.get("line") is None:
                merged["line"] = -1
            if merged.get("stage") is None:
                merged["stage"] = ""
            cols = ["prediction_id"] + list(_WRITABLE_COLS)
            placeholders = ",".join("?" for _ in cols)
            values = [merged.get(c) for c in cols]
            conn.execute(
                "INSERT OR REPLACE INTO predictions (%s) VALUES (%s)"
                % (",".join(cols), placeholders),
                values,
            )
            pred_ids.append(pid)
        conn.commit()
    return pred_ids


def upsert_acca(
    acca_id_value: str,
    pred_ids: Sequence[str],
    db_path: str = _DEFAULT_DB,
    bet_id: Optional[int] = None,
) -> int:
    """Map an accumulator to its leg prediction ids; return legs written.

    Idempotent on ``(acca_id, prediction_id)``; re-running with the same legs
    updates ``bet_id`` in place rather than duplicating rows.  ``build_id`` is
    taken from the first linked prediction so accas group by build.
    """
    ensure_schema(db_path)
    pred_ids = [str(p) for p in pred_ids]
    if not pred_ids:
        return 0
    written = 0
    with _connect_write(db_path) as conn:
        build_row = conn.execute(
            "SELECT build_id FROM predictions WHERE prediction_id=?",
            (pred_ids[0],),
        ).fetchone()
        build_id = build_row["build_id"] if build_row else None
        for pid in pred_ids:
            conn.execute(
                "INSERT OR REPLACE INTO acca_legs "
                "(acca_id, prediction_id, build_id, bet_id) VALUES (?,?,?,?)",
                (acca_id_value, pid, build_id, bet_id),
            )
            written += 1
        conn.commit()
    return written


def link_bet(pred_id: str, bet_id: int, db_path: str = _DEFAULT_DB) -> None:
    """Attach a real bet to a prediction (sets ``placed=1``)."""
    ensure_schema(db_path)
    with _connect_write(db_path) as conn:
        conn.execute(
            "UPDATE predictions SET bet_id=?, placed=1 WHERE prediction_id=?",
            (int(bet_id), pred_id),
        )
        conn.execute(
            "UPDATE acca_legs SET bet_id=? WHERE prediction_id=?",
            (int(bet_id), pred_id),
        )
        conn.commit()


def settle_prediction(
    pred_id: str,
    status: str,
    source: str,
    db_path: str = _DEFAULT_DB,
    settled_ts: Optional[str] = None,
) -> None:
    """Stamp a prediction's settlement (``won`` / ``lost`` / ``push`` / ``void``)."""
    ensure_schema(db_path)
    with _connect_write(db_path) as conn:
        conn.execute(
            "UPDATE predictions SET status=?, settle_source=?, settled_ts=? "
            "WHERE prediction_id=?",
            (status, source, settled_ts, pred_id),
        )
        conn.commit()


def set_prediction_close(
    pred_id: str,
    closing_odds: Optional[float],
    model_fair_odds: Optional[float] = None,
    db_path: str = _DEFAULT_DB,
    *,
    closing_devig_prob: Optional[float] = None,
    close_ts: Optional[str] = None,
    close_lag_seconds: Optional[int] = None,
    n_books_at_close: Optional[int] = None,
    close_is_prematch: Optional[int] = None,
) -> Optional[float]:
    """Stamp a prediction's de-vigged close + fair-vs-fair CLV; return CLV.

    ``clv = model_fair_odds / closing_odds - 1`` (both vig-free).  When
    ``model_fair_odds`` is not supplied it is read from the row (falling back
    to ``1/model_prob``).  CLV is ``NULL`` whenever no usable close exists
    (``closing_odds`` falsy or ``<= 1``) — never silently ``0``.
    """
    ensure_schema(db_path)
    with _connect_write(db_path) as conn:
        row = conn.execute(
            "SELECT model_prob, model_fair_odds FROM predictions "
            "WHERE prediction_id=?",
            (pred_id,),
        ).fetchone()
        if row is None:
            return None
        if model_fair_odds is None:
            model_fair_odds = row["model_fair_odds"]
            if model_fair_odds is None and row["model_prob"]:
                try:
                    mp = float(row["model_prob"])
                    if mp > 0:
                        model_fair_odds = 1.0 / mp
                except (TypeError, ValueError):
                    model_fair_odds = None
        clv: Optional[float] = None
        c_odds = None
        if closing_odds is not None:
            try:
                c_odds = float(closing_odds)
            except (TypeError, ValueError):
                c_odds = None
        if c_odds is not None and c_odds > 1.0 and model_fair_odds:
            clv = float(model_fair_odds) / c_odds - 1.0
        conn.execute(
            "UPDATE predictions SET closing_odds=?, model_fair_odds=?, clv=?, "
            "closing_devig_prob=?, close_ts=?, close_lag_seconds=?, "
            "n_books_at_close=?, close_is_prematch=? WHERE prediction_id=?",
            (
                c_odds,
                model_fair_odds,
                clv,
                closing_devig_prob,
                close_ts,
                close_lag_seconds,
                n_books_at_close,
                close_is_prematch,
                pred_id,
            ),
        )
        conn.commit()
        return clv


# ---------------------------------------------------------------------------
# Read helpers.
# ---------------------------------------------------------------------------


def get_prediction(pred_id: str, db_path: str = _DEFAULT_DB) -> Optional[sqlite3.Row]:
    with _connect_read(db_path) as conn:
        return conn.execute(
            "SELECT * FROM predictions WHERE prediction_id=?", (pred_id,)
        ).fetchone()


def all_predictions(db_path: str = _DEFAULT_DB) -> List[sqlite3.Row]:
    with _connect_read(db_path) as conn:
        return conn.execute(
            "SELECT * FROM predictions ORDER BY kickoff_utc, fixture, market, "
            "selection"
        ).fetchall()


def open_predictions(
    db_path: str = _DEFAULT_DB, market: Optional[str] = None
) -> List[sqlite3.Row]:
    """Open (unsettled) predictions, optionally filtered to one market."""
    sql = "SELECT * FROM predictions WHERE status='open'"
    params: List[Any] = []
    if market is not None:
        sql += " AND market=?"
        params.append(market)
    with _connect_read(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def model_book(db_path: str = _DEFAULT_DB) -> List[sqlite3.Row]:
    with _connect_read(db_path) as conn:
        return conn.execute("SELECT * FROM v_model_book").fetchall()


def realized_book(db_path: str = _DEFAULT_DB) -> List[sqlite3.Row]:
    with _connect_read(db_path) as conn:
        return conn.execute("SELECT * FROM v_realized_book").fetchall()


def is_x12_market(market: Any) -> bool:
    return isinstance(market, str) and market.strip().casefold() in _X12_MARKETS
