"""Prediction-ledger schema bootstrap and low-level DB helpers.

Tables
------
predictions
    One row per priced selection per card build (paper + realized book).
acca_legs
    Materialises paper accas as sets of prediction rows.

Views
-----
v_model_book   -- all predictions left-joined to bets
v_realized_book -- placed-only subset

CLV arithmetic
--------------
    clv = model_fair_odds / closing_odds - 1

NULL (not 0) when no close exists; mirrors store.py:428 exactly.

Dev-box guard
-------------
Any write whose --db basename is ``wca.db`` on the dev box raises unless the
environment variable WCA_ALLOW_PROD_DB is set.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

from wca.ledger.store import _connect as _ledger_connect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dev-box guard
# ---------------------------------------------------------------------------

def _is_dev_box() -> bool:
    import platform
    # The dev box is an Andrew's MacBook; the mini (prod) has a different hostname.
    # hostname check is the most reliable distinguisher; WCA_ALLOW_PROD_DB overrides.
    return "macbook" in platform.node().lower()


def _guard_prod_write(db_path: str) -> None:
    if os.path.basename(db_path) == "wca.db" and _is_dev_box():
        if not os.environ.get("WCA_ALLOW_PROD_DB"):
            raise PermissionError(
                f"Refusing to write wca.db on the dev box "
                f"(set WCA_ALLOW_PROD_DB=1 to override): {db_path}"
            )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    """Open predledger connection with WAL + FK + busy-timeout.

    Sets PRAGMA busy_timeout=5000 per-connection here (not in shared
    wca.ledger.store._connect) so the live bot's connection is unaffected.
    """
    conn = _ledger_connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id      TEXT PRIMARY KEY,
    build_id           TEXT NOT NULL,
    ts_utc             TEXT NOT NULL,

    match_id           TEXT,
    fixture            TEXT,
    kickoff_utc        TEXT,
    market             TEXT NOT NULL,
    selection          TEXT NOT NULL,
    line               REAL NOT NULL DEFAULT -1,
    stage              TEXT NOT NULL DEFAULT '',
    n_outcomes         INTEGER NOT NULL,

    model_prob         REAL NOT NULL,
    model_fair_odds    REAL NOT NULL,
    elo_prob           REAL,
    dc_prob            REAL,

    market_devig_prob  REAL,
    market_best_odds   REAL,
    market_book        TEXT,
    devig_method       TEXT,
    edge               REAL,
    ev_per_unit        REAL,

    bet_id             INTEGER,
    placed             INTEGER NOT NULL DEFAULT 0,

    closing_devig_prob  REAL,
    closing_odds        REAL,
    clv                 REAL,
    close_ts            TEXT,
    close_lag_seconds   INTEGER,
    n_books_at_close    INTEGER,
    close_is_prematch   INTEGER,

    status             TEXT NOT NULL DEFAULT 'open',
    settled_ts         TEXT,
    settle_source      TEXT,

    model_source       TEXT,
    notes              TEXT,
    FOREIGN KEY (bet_id) REFERENCES bets(id)
)
"""

_DDL_PREDICTIONS_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_pred_natural ON predictions(build_id, match_id, market, selection, line, stage)",
    "CREATE INDEX IF NOT EXISTS idx_pred_build  ON predictions(build_id)",
    "CREATE INDEX IF NOT EXISTS idx_pred_match  ON predictions(match_id)",
    "CREATE INDEX IF NOT EXISTS idx_pred_market ON predictions(market, selection)",
    "CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status)",
]

_DDL_ACCA_LEGS = """
CREATE TABLE IF NOT EXISTS acca_legs (
    acca_id       TEXT NOT NULL,
    prediction_id TEXT NOT NULL,
    build_id      TEXT NOT NULL,
    bet_id        INTEGER,
    PRIMARY KEY (acca_id, prediction_id),
    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id),
    FOREIGN KEY (bet_id) REFERENCES bets(id)
)
"""

_DDL_ACCA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_acca_id  ON acca_legs(acca_id)",
    "CREATE INDEX IF NOT EXISTS idx_acca_bet ON acca_legs(bet_id)",
]

_DDL_VIEWS = [
    """
    CREATE VIEW IF NOT EXISTS v_model_book AS
    SELECT p.*,
           b.stake,
           b.decimal_odds AS bet_odds,
           b.settled_pl,
           b.clv AS bet_clv,
           CASE WHEN p.bet_id IS NULL THEN 'paper' ELSE 'realized' END AS book
    FROM predictions p LEFT JOIN bets b ON b.id = p.bet_id
    """,
    """
    CREATE VIEW IF NOT EXISTS v_realized_book AS
    SELECT * FROM v_model_book WHERE book = 'realized'
    """,
]

_DDL_META = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""


def ensure_schema(db_path: str) -> None:
    """Create predledger tables, indexes, and views if they don't exist.

    Additive only — never touches bets or odds_snapshots.
    """
    _guard_prod_write(db_path)
    with _connect(db_path) as conn:
        conn.execute(_DDL_PREDICTIONS)
        for idx in _DDL_PREDICTIONS_INDEXES:
            conn.execute(idx)
        conn.execute(_DDL_ACCA_LEGS)
        for idx in _DDL_ACCA_INDEXES:
            conn.execute(idx)
        for view in _DDL_VIEWS:
            conn.execute(view)
        conn.execute(_DDL_META)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('predledger_version','1')"
        )


# ---------------------------------------------------------------------------
# Settle helper
# ---------------------------------------------------------------------------


def settle_prediction(
    conn: sqlite3.Connection,
    prediction_id: str,
    status: str,
    settled_ts: str,
    settle_source: str,
) -> bool:
    """Update a single prediction row status. Only transitions from 'open'.

    Returns True if a row was updated (was open), False otherwise.
    """
    cur = conn.execute(
        "UPDATE predictions SET status=?, settled_ts=?, settle_source=? "
        "WHERE prediction_id=? AND status='open'",
        (status, settled_ts, settle_source, prediction_id),
    )
    return cur.rowcount > 0
