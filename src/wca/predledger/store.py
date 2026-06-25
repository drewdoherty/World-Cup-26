"""SQLite-backed prediction ledger for the World Cup Alpha platform.

Tracks model predictions (paper and bet-linked) separately from the placed-bet
ledger in ``wca.ledger.store``.  Predictions can exist without a corresponding
bet (paper tracking) or be linked to a bet after placement.

Tables
------
predictions
    One row per (match_id|stage, market, selection, line) tuple.
    ``match_id`` is NULL for futures/outright markets, which are keyed on
    ``stage`` instead.  ``line = -1`` is the sentinel for no-line markets
    (1X2, BTTS, etc.).  ``placed = 1`` once a bet is linked via
    :func:`link_bet`.
accas
    Accumulator header — one row per unique ordered set of prediction legs.
acca_legs
    Junction between accas and predictions (one row per leg).
schema_meta
    Key/value metadata (schema version, created timestamp).

Closing-line value (CLV)
------------------------
Same return-ratio convention as ``wca.ledger.store``:

    CLV% = (offered_odds / closing_odds) - 1

Positive = beat the close.  ``clv`` stays NULL when ``offered_odds`` is NULL
(the prediction was recorded without a market price).

Dev-box guard
-------------
Writing to a file whose basename is ``wca.db`` raises :class:`PermissionError`
unless the environment variable ``WCA_ALLOW_PROD_DB=1`` is set.  This prevents
accidental mutation of the canonical production database from a dev machine.
Read-only helpers (``get_prediction``, ``model_book``, etc.) are not guarded.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import sqlite3
from typing import Any, Dict, List, Optional

from wca.ledger.store import _connect as _base_connect


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

_DEFAULT_DB = "data/wca.db"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = _base_connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _pred_id(
    match_id: Optional[str],
    stage: str,
    market: str,
    selection: str,
    line: float,
) -> str:
    """Deterministic SHA-256 hex digest (first 32 chars) for a prediction row."""
    key = "|".join([match_id or "", stage, market, selection, "%.6f" % line])
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _acca_id(prediction_ids: List[str]) -> str:
    """Deterministic SHA-256 hex digest for an ordered set of prediction legs."""
    key = "|".join(sorted(prediction_ids))
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _check_prod_guard(db_path: str) -> None:
    """Raise PermissionError when writing to wca.db without the env-var override."""
    if os.path.basename(db_path) == "wca.db" and not os.environ.get("WCA_ALLOW_PROD_DB"):
        raise PermissionError(
            "refusing to write to %r on dev-box; set WCA_ALLOW_PROD_DB=1 to override"
            % db_path
        )


# ---------------------------------------------------------------------------
# Schema DDL.
# ---------------------------------------------------------------------------

_DDL_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id   TEXT    PRIMARY KEY,
    match_id        TEXT,
    stage           TEXT    NOT NULL DEFAULT '',
    market          TEXT    NOT NULL,
    selection       TEXT    NOT NULL,
    line            REAL    NOT NULL DEFAULT -1,
    n_outcomes      INTEGER NOT NULL DEFAULT 2,
    model_prob      REAL,
    model_odds      REAL,
    offered_odds    REAL,
    devig_method    TEXT,
    bet_id          INTEGER REFERENCES bets(id),
    placed          INTEGER NOT NULL DEFAULT 0,
    close_ts_utc    TEXT,
    closing_odds    REAL,
    clv             REAL,
    outcome         TEXT,
    result_ts_utc   TEXT,
    ts_utc          TEXT    NOT NULL,
    notes           TEXT
)
"""

_DDL_ACCAS = """
CREATE TABLE IF NOT EXISTS accas (
    acca_id         TEXT    PRIMARY KEY,
    ts_utc          TEXT    NOT NULL,
    n_legs          INTEGER NOT NULL,
    combined_odds   REAL,
    placed          INTEGER NOT NULL DEFAULT 0
)
"""

_DDL_ACCA_LEGS = """
CREATE TABLE IF NOT EXISTS acca_legs (
    acca_id         TEXT    NOT NULL REFERENCES accas(acca_id),
    leg_index       INTEGER NOT NULL,
    prediction_id   TEXT    NOT NULL REFERENCES predictions(prediction_id),
    PRIMARY KEY (acca_id, leg_index)
)
"""

_DDL_SCHEMA_META = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key     TEXT    PRIMARY KEY,
    value   TEXT    NOT NULL
)
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_predictions_match ON predictions(match_id)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_market ON predictions(market, selection)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_stage ON predictions(stage)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_placed ON predictions(placed)",
    "CREATE INDEX IF NOT EXISTS idx_acca_legs_pred ON acca_legs(prediction_id)",
]

_DDL_V_MODEL_BOOK = """
CREATE VIEW IF NOT EXISTS v_model_book AS
SELECT
    p.prediction_id,
    p.match_id,
    p.stage,
    p.market,
    p.selection,
    p.line,
    p.n_outcomes,
    p.model_prob,
    p.model_odds,
    p.offered_odds,
    p.devig_method,
    p.placed,
    p.ts_utc
FROM predictions p
"""

_DDL_V_REALIZED_BOOK = """
CREATE VIEW IF NOT EXISTS v_realized_book AS
SELECT
    p.prediction_id,
    p.match_id,
    p.stage,
    p.market,
    p.selection,
    p.line,
    p.n_outcomes,
    p.model_prob,
    p.model_odds,
    p.offered_odds,
    p.closing_odds,
    p.clv,
    p.outcome,
    p.placed,
    p.bet_id,
    CASE WHEN p.placed = 1 THEN 'realized' ELSE 'paper' END AS book_type,
    p.result_ts_utc,
    p.close_ts_utc
FROM predictions p
"""


# ---------------------------------------------------------------------------
# Schema management.
# ---------------------------------------------------------------------------


def _init_schema(db_path: str) -> None:
    """Create all predledger tables, indexes, and views if absent (no prod guard).

    Purely additive — uses ``CREATE TABLE/INDEX/VIEW IF NOT EXISTS`` only.
    Does **not** touch the ``bets`` or ``odds_snapshots`` tables.
    Called by both :func:`ensure_schema` (public, guarded) and write functions.
    """
    with _connect(db_path) as conn:
        conn.execute(_DDL_PREDICTIONS)
        conn.execute(_DDL_ACCAS)
        conn.execute(_DDL_ACCA_LEGS)
        conn.execute(_DDL_SCHEMA_META)
        for ddl in _DDL_INDEXES:
            conn.execute(ddl)
        conn.execute(_DDL_V_MODEL_BOOK)
        conn.execute(_DDL_V_REALIZED_BOOK)
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('predledger_version', '1')"
            " ON CONFLICT(key) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('created_utc', ?)"
            " ON CONFLICT(key) DO NOTHING",
            (_dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),),
        )


def ensure_schema(db_path: str = _DEFAULT_DB) -> None:
    """Create all predledger tables, indexes, and views if they do not exist.

    Guarded by the dev-box prod check: raises :class:`PermissionError` when
    called with a path whose basename is ``wca.db`` and ``WCA_ALLOW_PROD_DB``
    is not set in the environment.

    Safe to call on every startup; subsequent calls are no-ops for existing
    tables and views.
    """
    _check_prod_guard(db_path)
    _init_schema(db_path)


# ---------------------------------------------------------------------------
# Writers.
# ---------------------------------------------------------------------------


def upsert_predictions(
    rows: List[Dict[str, Any]],
    db_path: str = _DEFAULT_DB,
) -> List[str]:
    """Upsert prediction rows; returns list of ``prediction_id`` strings.

    Each dict in ``rows`` must contain ``market``, ``selection``, and
    ``ts_utc``.  Optional keys: ``match_id``, ``stage``, ``line``,
    ``n_outcomes``, ``model_prob``, ``offered_odds``, ``devig_method``,
    ``notes``.

    Two upserts of the same (match_id, stage, market, selection, line)
    tuple produce exactly one row — the second call updates the mutable
    fields but never creates a duplicate.  Immutable state columns
    (``placed``, ``outcome``, ``closing_odds``, ``clv``, ``bet_id``) are
    never overwritten by an upsert.
    """
    _check_prod_guard(db_path)
    _init_schema(db_path)

    ids: List[str] = []
    with _connect(db_path) as conn:
        for row in rows:
            match_id: Optional[str] = row.get("match_id")
            stage: str = row.get("stage") or ""
            market: str = row["market"]
            selection: str = row["selection"]
            line: float = float(row.get("line", -1))
            n_outcomes: int = int(row.get("n_outcomes", 2))
            model_prob: Optional[float] = row.get("model_prob")
            model_odds: Optional[float] = (
                row.get("model_odds")
                or ((1.0 / model_prob) if model_prob else None)
            )
            offered_odds: Optional[float] = row.get("offered_odds")
            devig_method: Optional[str] = row.get("devig_method")
            ts_utc: str = row["ts_utc"]
            notes: Optional[str] = row.get("notes")

            pid = _pred_id(match_id, stage, market, selection, line)

            conn.execute(
                """
                INSERT INTO predictions
                    (prediction_id, match_id, stage, market, selection, line,
                     n_outcomes, model_prob, model_odds, offered_odds,
                     devig_method, ts_utc, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prediction_id) DO UPDATE SET
                    n_outcomes    = excluded.n_outcomes,
                    model_prob    = excluded.model_prob,
                    model_odds    = excluded.model_odds,
                    offered_odds  = excluded.offered_odds,
                    devig_method  = excluded.devig_method,
                    ts_utc        = excluded.ts_utc,
                    notes         = excluded.notes
                """,
                (
                    pid, match_id, stage, market, selection, line,
                    n_outcomes, model_prob, model_odds, offered_odds,
                    devig_method, ts_utc, notes,
                ),
            )
            ids.append(pid)

    return ids


def upsert_acca(
    prediction_ids: List[str],
    ts_utc: str,
    combined_odds: Optional[float] = None,
    db_path: str = _DEFAULT_DB,
) -> str:
    """Upsert an accumulator; returns the ``acca_id`` hash.

    ``prediction_ids`` must reference existing predictions (FK enforced).
    The acca_id is the SHA-256 of the sorted prediction_id list, so the
    same set of legs always maps to the same acca_id regardless of insertion
    order.
    """
    _check_prod_guard(db_path)
    _init_schema(db_path)

    aid = _acca_id(prediction_ids)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO accas (acca_id, ts_utc, n_legs, combined_odds)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(acca_id) DO UPDATE SET
                ts_utc        = excluded.ts_utc,
                n_legs        = excluded.n_legs,
                combined_odds = excluded.combined_odds
            """,
            (aid, ts_utc, len(prediction_ids), combined_odds),
        )
        for idx, pid in enumerate(prediction_ids):
            conn.execute(
                """
                INSERT INTO acca_legs (acca_id, leg_index, prediction_id)
                VALUES (?, ?, ?)
                ON CONFLICT(acca_id, leg_index) DO UPDATE SET
                    prediction_id = excluded.prediction_id
                """,
                (aid, idx, pid),
            )

    return aid


def link_bet(
    prediction_id: str,
    bet_id: int,
    db_path: str = _DEFAULT_DB,
) -> None:
    """Link a prediction to a placed bet in the ledger; sets ``placed = 1``.

    Parameters
    ----------
    prediction_id:
        Hash returned by :func:`upsert_predictions`.
    bet_id:
        Row ``id`` from ``wca.ledger.store.record_bet``.

    Raises
    ------
    KeyError
        If no prediction with ``prediction_id`` exists.
    """
    _check_prod_guard(db_path)
    _init_schema(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT prediction_id FROM predictions WHERE prediction_id = ?",
            (prediction_id,),
        ).fetchone()
        if row is None:
            raise KeyError("no prediction with id=%r" % prediction_id)
        conn.execute(
            "UPDATE predictions SET bet_id = ?, placed = 1 WHERE prediction_id = ?",
            (bet_id, prediction_id),
        )


def settle_prediction(
    prediction_id: str,
    outcome: str,
    result_ts_utc: Optional[str] = None,
    db_path: str = _DEFAULT_DB,
) -> None:
    """Record the outcome of a prediction.

    Parameters
    ----------
    outcome:
        ``"won"``, ``"lost"``, or ``"void"`` (case-insensitive).
    result_ts_utc:
        Settlement timestamp; defaults to current UTC if omitted.

    Raises
    ------
    KeyError
        If no prediction with ``prediction_id`` exists.
    ValueError
        If ``outcome`` is not one of the three valid values.
    """
    outcome_lower = outcome.strip().lower()
    if outcome_lower not in ("won", "lost", "void"):
        raise ValueError(
            "outcome must be 'won', 'lost', or 'void', got %r" % outcome
        )

    _check_prod_guard(db_path)
    _init_schema(db_path)

    if result_ts_utc is None:
        result_ts_utc = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT prediction_id FROM predictions WHERE prediction_id = ?",
            (prediction_id,),
        ).fetchone()
        if row is None:
            raise KeyError("no prediction with id=%r" % prediction_id)
        conn.execute(
            "UPDATE predictions SET outcome = ?, result_ts_utc = ? WHERE prediction_id = ?",
            (outcome_lower, result_ts_utc, prediction_id),
        )


def set_prediction_close(
    prediction_id: str,
    closing_odds: float,
    close_ts_utc: Optional[str] = None,
    db_path: str = _DEFAULT_DB,
) -> None:
    """Record closing odds for a prediction and compute CLV.

    CLV formula (return-ratio, same convention as ``wca.ledger.store``)
    -------------------------------------------------------------------
    CLV% = (offered_odds / closing_odds) - 1

    Positive = beat the close.  ``clv`` is set to NULL when the prediction
    has no ``offered_odds`` (paper predictions recorded without a market price).

    Parameters
    ----------
    closing_odds:
        De-vigged fair consensus price at last capture before kick-off.
        Must be strictly greater than 1.0.

    Raises
    ------
    KeyError
        If no prediction with ``prediction_id`` exists.
    ValueError
        If ``closing_odds`` is not strictly greater than 1.0.
    """
    c_odds = float(closing_odds)
    if c_odds <= 1.0:
        raise ValueError("closing_odds must be > 1.0, got %r" % c_odds)

    _check_prod_guard(db_path)
    _init_schema(db_path)

    if close_ts_utc is None:
        close_ts_utc = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT offered_odds FROM predictions WHERE prediction_id = ?",
            (prediction_id,),
        ).fetchone()
        if row is None:
            raise KeyError("no prediction with id=%r" % prediction_id)

        offered = row["offered_odds"]
        clv: Optional[float] = (
            (float(offered) / c_odds) - 1.0 if offered is not None else None
        )

        conn.execute(
            "UPDATE predictions"
            " SET closing_odds = ?, clv = ?, close_ts_utc = ?"
            " WHERE prediction_id = ?",
            (c_odds, clv, close_ts_utc, prediction_id),
        )


# ---------------------------------------------------------------------------
# Query helpers.
# ---------------------------------------------------------------------------
# Callers must call ensure_schema (or any write function) on the db first.
# These helpers are not guarded so reads from wca.db are always allowed.


def get_prediction(
    prediction_id: str,
    db_path: str = _DEFAULT_DB,
) -> Optional[sqlite3.Row]:
    """Return the row for a single prediction, or ``None`` if not found."""
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM predictions WHERE prediction_id = ?",
            (prediction_id,),
        ).fetchone()


def predictions_for_match(
    match_id: str,
    db_path: str = _DEFAULT_DB,
) -> list:
    """Return all predictions for a given ``match_id``, ordered by ``ts_utc``."""
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM predictions WHERE match_id = ? ORDER BY ts_utc",
            (match_id,),
        ).fetchall()


def model_book(db_path: str = _DEFAULT_DB) -> list:
    """Return all rows from ``v_model_book`` ordered by ``ts_utc``."""
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM v_model_book ORDER BY ts_utc"
        ).fetchall()


def realized_book(db_path: str = _DEFAULT_DB) -> list:
    """Return all rows from ``v_realized_book`` ordered by ``ts_utc``."""
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM v_realized_book ORDER BY ts_utc"
        ).fetchall()
