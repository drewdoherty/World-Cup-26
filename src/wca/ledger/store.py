"""SQLite-backed bet ledger for the World Cup Alpha platform.

All state is stored in a single SQLite database file (default
``data/wca.db``). Every public function accepts an explicit ``db_path``
argument so tests can use temporary files without touching the project
database.

Tables
------
bets
    One row per placed bet, with settlement and closing-line columns
    populated lazily.
bankroll_events
    Deposits and withdrawals.
odds_snapshots
    Created by the odds-collection module; this module will create the
    table if it is missing but will not alter its schema.

Closing-line value (CLV)
------------------------
CLV is the single most important bet-quality signal.  We use the
*return-ratio* form:

    CLV% = (decimal_odds_taken / closing_odds) - 1

A positive value means the bettor secured better odds than the closing
price, i.e. they "beat the close".  The closing line is the last price
available just before the match kicks off and is a strong proxy for the
efficient market consensus.

Reference: Levitt (2004) "Why are gambling markets organised differently
from financial markets?", *The Economic Journal* 114(495):223-246; and
the practical treatment in Benter (1994) "Computer based horse race
handicapping and wagering systems", in *Efficiency of Racetrack Betting
Markets* (Hausch, Lo & Ziemba eds).
"""

from __future__ import annotations

import sqlite3
from typing import Optional


# ---------------------------------------------------------------------------
# Default path (relative to the repo root, used only when the caller does not
# pass an explicit db_path).
# ---------------------------------------------------------------------------

_DEFAULT_DB = "data/wca.db"

# ---------------------------------------------------------------------------
# Schema DDL.
# ---------------------------------------------------------------------------

_DDL_BETS = """
CREATE TABLE IF NOT EXISTS bets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc              TEXT    NOT NULL,
    match_id            TEXT    NOT NULL,
    match_desc          TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    selection           TEXT    NOT NULL,
    platform            TEXT    NOT NULL,
    decimal_odds        REAL    NOT NULL,
    stake               REAL    NOT NULL,
    model_prob          REAL,
    market_prob_devig   REAL,
    ev                  REAL,
    kelly_fraction      REAL,
    status              TEXT    NOT NULL DEFAULT 'open',
    settled_pl          REAL,
    closing_odds        REAL,
    clv                 REAL,
    notes               TEXT
)
"""

_DDL_BANKROLL_EVENTS = """
CREATE TABLE IF NOT EXISTS bankroll_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc  TEXT    NOT NULL,
    amount  REAL    NOT NULL,
    reason  TEXT
)
"""

# odds_snapshots is owned by the odds-collection module; we just ensure the
# table exists with the canonical schema so ledger queries can JOIN it if
# needed.
_DDL_ODDS_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    ts_utc      TEXT,
    source      TEXT,
    match_id    TEXT,
    market      TEXT,
    selection   TEXT,
    decimal_odds REAL,
    raw         TEXT
)
"""


# ---------------------------------------------------------------------------
# Connection helpers.
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    """Return a connection with foreign-keys and WAL-mode enabled."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = _DEFAULT_DB) -> None:
    """Create all tables if they do not yet exist.

    Safe to call on an existing database; it is a no-op if the tables are
    already present.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  The file and any intermediate directories
        must already exist (or will be created by SQLite automatically for
        the file itself).
    """
    with _connect(db_path) as conn:
        conn.execute(_DDL_BETS)
        conn.execute(_DDL_BANKROLL_EVENTS)
        conn.execute(_DDL_ODDS_SNAPSHOTS)


# ---------------------------------------------------------------------------
# Bet recording and lifecycle.
# ---------------------------------------------------------------------------


def record_bet(
    ts_utc: str,
    match_id: str,
    match_desc: str,
    market: str,
    selection: str,
    platform: str,
    decimal_odds: float,
    stake: float,
    model_prob: Optional[float] = None,
    market_prob_devig: Optional[float] = None,
    ev: Optional[float] = None,
    kelly_fraction: Optional[float] = None,
    notes: Optional[str] = None,
    account: str = "1",
    source: str = "model",
    db_path: str = _DEFAULT_DB,
) -> int:
    """Insert a new open bet into the ledger and return its row ID.

    ``account`` separates physical betting accounts (e.g. "1" = own, "2" = a
    second account) so analytics can split a single venue across them.
    ``source`` tags WHY the bet was placed — "model" (from the card/scanners),
    "offer" (free-bet / promo extraction), or "punt" (a directional bet made on
    judgement, not the model). Keeps the CLV experiment separable from promo
    and discretionary activity.

    Parameters
    ----------
    ts_utc:
        ISO-8601 timestamp of bet placement (UTC), e.g. ``"2026-06-11T14:00:00"``.
    match_id:
        Unique identifier for the match, e.g. ``"GRP_A_01"``.
    match_desc:
        Human-readable match description, e.g. ``"Mexico vs Canada"``.
    market:
        Bet market type, e.g. ``"1X2"`` or ``"BTTS"``.
    selection:
        The specific outcome backed, e.g. ``"Home"`` or ``"Over 2.5"``.
    platform:
        Bookmaker or exchange name, e.g. ``"Bet365"``.
    decimal_odds:
        Decimal (European) odds at which the bet was placed.
    stake:
        Currency amount staked.
    model_prob:
        Model-derived win probability for this selection (optional).
    market_prob_devig:
        De-vigged market-implied probability for this selection (optional).
    ev:
        Expected value of the bet in currency (optional).
    kelly_fraction:
        Kelly fraction used to size this bet (optional).
    notes:
        Free-text notes (optional).
    db_path:
        Path to the SQLite database file.

    Returns
    -------
    int
        The auto-assigned row ``id`` of the newly inserted bet.
    """
    init_db(db_path)
    sql = """
        INSERT INTO bets
            (ts_utc, match_id, match_desc, market, selection, platform,
             decimal_odds, stake, model_prob, market_prob_devig, ev,
             kelly_fraction, status, notes, account, source)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
    """
    with _connect(db_path) as conn:
        _ensure_account_source_columns(conn)
        cur = conn.execute(
            sql,
            (
                ts_utc,
                match_id,
                match_desc,
                market,
                selection,
                platform,
                float(decimal_odds),
                float(stake),
                float(model_prob) if model_prob is not None else None,
                float(market_prob_devig) if market_prob_devig is not None else None,
                float(ev) if ev is not None else None,
                float(kelly_fraction) if kelly_fraction is not None else None,
                notes,
                str(account or "1"),
                str(source or "model"),
            ),
        )
        return cur.lastrowid


def _ensure_account_source_columns(conn) -> None:
    """Add account/source columns to pre-existing databases (idempotent)."""
    for col, ddl in (("account", "TEXT DEFAULT '1'"), ("source", "TEXT DEFAULT 'model'")):
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN %s %s" % (col, ddl))
        except Exception:
            pass


def _ensure_settled_ts_column(conn) -> None:
    """Add the settled_ts column to pre-existing databases (idempotent)."""
    try:
        conn.execute("ALTER TABLE bets ADD COLUMN settled_ts TEXT")
    except Exception:
        pass  # already present


def settle_bet(
    bet_id: int,
    result: str,
    db_path: str = _DEFAULT_DB,
    settled_ts_utc: Optional[str] = None,
) -> None:
    """Mark a bet as won or lost and compute the profit/loss.

    Parameters
    ----------
    bet_id:
        The ``id`` of the bet row to settle.
    result:
        ``"won"`` or ``"lost"``; case-insensitive.
    db_path:
        Path to the SQLite database file.
    settled_ts_utc:
        ISO timestamp of settlement; defaults to the current UTC time. Stored
        so realized-P&L curves can be plotted over settlement time.

    Raises
    ------
    ValueError
        If ``result`` is not ``"won"`` or ``"lost"``, or if the bet is not
        currently open.
    KeyError
        If no bet with ``bet_id`` exists.
    """
    result_lower = result.strip().lower()
    if result_lower not in ("won", "lost"):
        raise ValueError("result must be 'won' or 'lost', got %r" % result)

    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, stake, decimal_odds FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()
        if row is None:
            raise KeyError("no bet with id=%d" % bet_id)
        if row["status"] != "open":
            raise ValueError(
                "bet %d has status %r; only open bets can be settled" % (bet_id, row["status"])
            )

        stake_val = float(row["stake"])
        odds_val = float(row["decimal_odds"])
        # P&L convention: profit = net return (winnings minus stake already
        # counted; total return = odds * stake, net profit = (odds - 1) * stake,
        # net loss = -stake).
        if result_lower == "won":
            pl = (odds_val - 1.0) * stake_val
        else:
            pl = -stake_val

        _ensure_settled_ts_column(conn)
        if settled_ts_utc is None:
            import datetime as _dt

            settled_ts_utc = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "UPDATE bets SET status = ?, settled_pl = ?, settled_ts = ? WHERE id = ?",
            (result_lower, pl, settled_ts_utc, bet_id),
        )


def void_bet(bet_id: int, db_path: str = _DEFAULT_DB) -> None:
    """Void a bet (stake returned, no P&L impact).

    Parameters
    ----------
    bet_id:
        The ``id`` of the bet row to void.
    db_path:
        Path to the SQLite database file.

    Raises
    ------
    KeyError
        If no bet with ``bet_id`` exists.
    ValueError
        If the bet is already settled or voided.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()
        if row is None:
            raise KeyError("no bet with id=%d" % bet_id)
        if row["status"] != "open":
            raise ValueError(
                "bet %d has status %r; only open bets can be voided" % (bet_id, row["status"])
            )
        _ensure_settled_ts_column(conn)
        import datetime as _dt

        conn.execute(
            "UPDATE bets SET status = 'void', settled_pl = 0.0, settled_ts = ? WHERE id = ?",
            (_dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"), bet_id),
        )


def set_closing_odds(
    bet_id: int,
    closing_odds: float,
    db_path: str = _DEFAULT_DB,
) -> None:
    """Record closing odds for a bet and compute CLV.

    CLV formula (return-ratio form)
    --------------------------------
    CLV% = (decimal_odds_taken / closing_odds) - 1

    A positive CLV means the bettor obtained better odds than the closing
    line — they "beat the close".  A negative CLV means the line moved
    against them after placement.

    The closing odds should be the last price available immediately before
    kick-off (or market suspension), representing the sharpest consensus.

    Parameters
    ----------
    bet_id:
        The ``id`` of the bet row to update.
    closing_odds:
        Last-traded / closing decimal odds for this selection.
    db_path:
        Path to the SQLite database file.

    Raises
    ------
    KeyError
        If no bet with ``bet_id`` exists.
    ValueError
        If ``closing_odds`` is not strictly greater than 1.0.
    """
    c_odds = float(closing_odds)
    if c_odds <= 1.0:
        raise ValueError("closing_odds must be > 1.0, got %r" % c_odds)

    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT decimal_odds FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()
        if row is None:
            raise KeyError("no bet with id=%d" % bet_id)

        taken_odds = float(row["decimal_odds"])
        # CLV% = (odds_taken / closing_odds) - 1
        # Positive = beat the close; negative = line moved against us.
        clv = (taken_odds / c_odds) - 1.0

        conn.execute(
            "UPDATE bets SET closing_odds = ?, clv = ? WHERE id = ?",
            (c_odds, clv, bet_id),
        )


# ---------------------------------------------------------------------------
# Bankroll events.
# ---------------------------------------------------------------------------


def add_bankroll_event(
    ts_utc: str,
    amount: float,
    reason: Optional[str] = None,
    db_path: str = _DEFAULT_DB,
) -> int:
    """Record a deposit (positive amount) or withdrawal (negative amount).

    Parameters
    ----------
    ts_utc:
        ISO-8601 UTC timestamp.
    amount:
        Currency amount; positive for deposits, negative for withdrawals.
    reason:
        Optional free-text description.
    db_path:
        Path to the SQLite database file.

    Returns
    -------
    int
        Auto-assigned row ``id``.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO bankroll_events (ts_utc, amount, reason) VALUES (?, ?, ?)",
            (ts_utc, float(amount), reason),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Read helpers used by reports.py.
# ---------------------------------------------------------------------------


def get_bet(bet_id: int, db_path: str = _DEFAULT_DB) -> Optional[sqlite3.Row]:
    """Return the row for a single bet, or ``None`` if not found."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()


def all_bets(db_path: str = _DEFAULT_DB) -> list:
    """Return all bet rows as a list of :class:`sqlite3.Row` objects."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM bets ORDER BY id"
        ).fetchall()


def all_bankroll_events(db_path: str = _DEFAULT_DB) -> list:
    """Return all bankroll-event rows ordered by id."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM bankroll_events ORDER BY id"
        ).fetchall()
