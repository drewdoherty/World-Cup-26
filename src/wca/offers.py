"""SQLite tracker for matched-betting bookmaker sign-up offers.

This tracks **risk-free promo extraction** — new-customer bookmaker offers that
are converted to guaranteed cash via matched betting (back at the bookie, lay on
an exchange; see :mod:`wca.matched`).  It is deliberately a *separate ledger*
from the model bets.

ISOLATION (read this)
---------------------
The value tracked here is **promo extraction, not model edge**.  It MUST stay
apart from the closing-line-value (CLV) experiment:

* This module writes only to its own table, ``sb_offers``.  It never touches
  ``bets``, ``bankroll_events`` or any other table owned by
  :mod:`wca.ledger.store`.
* The figures returned by :func:`offers_summary` are *not* part of
  ``wca.ledger.reports.summary`` and never enter the CLV / calibration
  numbers.  Mixing risk-free promo profit into the model-edge ledger would
  pollute the experiment the whole platform exists to run.

A regression test asserts that recording offers does not change the ledger's
bet counts (``tests/test_offers.py::TestIsolation``).

Schema — ``sb_offers``
----------------------
======================  ====================================================
column                  meaning
======================  ====================================================
``id``                  integer primary key
``ts_utc``              ISO-8601 UTC timestamp the row was created
``account_holder``      whose account the offer is on (e.g. "me", "mum")
``bookmaker``           bookmaker name
``offer_desc``          free-text offer description
``offer_type``          ``qualifying`` / ``free_snr`` / ``free_sr`` / other
``qualifying_stake``    real-money stake used to qualify (REAL, nullable)
``qualifying_loss``     cost of qualifying, stored as a POSITIVE number (REAL)
``free_bet_value``      face value of the free bet unlocked (REAL, nullable)
``lay_venue``           exchange used to lay (e.g. "smarkets")
``extracted_value``     locked cash extracted from the free bet (REAL)
``status``              ``claimed`` / ``qualified`` / ``extracted`` / ``expired``
``notes``               free-text notes
======================  ====================================================
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any, Dict, List, Optional


_DEFAULT_DB = "data/wca.db"

# Lifecycle states an offer can be in.
_VALID_STATUS = ("claimed", "qualified", "extracted", "expired")

_DDL_SB_OFFERS = """
CREATE TABLE IF NOT EXISTS sb_offers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc            TEXT    NOT NULL,
    account_holder    TEXT    NOT NULL,
    bookmaker         TEXT    NOT NULL,
    offer_desc        TEXT,
    offer_type        TEXT,
    qualifying_stake  REAL,
    qualifying_loss   REAL,
    free_bet_value    REAL,
    lay_venue         TEXT,
    extracted_value   REAL,
    status            TEXT    NOT NULL DEFAULT 'claimed',
    notes             TEXT
)
"""

# Columns a caller is allowed to update via update_offer().
_UPDATABLE = {
    "account_holder",
    "bookmaker",
    "offer_desc",
    "offer_type",
    "qualifying_stake",
    "qualifying_loss",
    "free_bet_value",
    "lay_venue",
    "extracted_value",
    "status",
    "notes",
}


# ---------------------------------------------------------------------------
# Connection / schema helpers.
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str = _DEFAULT_DB) -> None:
    """Create the ``sb_offers`` table if it does not exist.

    Safe to call repeatedly.  This NEVER creates or alters the ``bets`` or
    ``bankroll_events`` tables — it only owns ``sb_offers``.
    """
    with _connect(db_path) as conn:
        conn.execute(_DDL_SB_OFFERS)


def _now_utc() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# CRUD.
# ---------------------------------------------------------------------------


def record_offer(
    account_holder: str,
    bookmaker: str,
    offer_desc: Optional[str] = None,
    offer_type: Optional[str] = None,
    qualifying_stake: Optional[float] = None,
    qualifying_loss: Optional[float] = None,
    free_bet_value: Optional[float] = None,
    lay_venue: Optional[str] = None,
    extracted_value: Optional[float] = None,
    status: str = "claimed",
    notes: Optional[str] = None,
    ts_utc: Optional[str] = None,
    db_path: str = _DEFAULT_DB,
) -> int:
    """Insert a new offer row and return its ``id``.

    Parameters
    ----------
    account_holder:
        Whose account the offer is on, e.g. ``"me"`` or ``"mum"``.
    bookmaker:
        Bookmaker name.
    offer_desc, offer_type:
        Free-text description and a short type tag (``qualifying`` /
        ``free_snr`` / ``free_sr`` / ...).
    qualifying_stake:
        Real-money stake used to qualify (optional).
    qualifying_loss:
        Cost of qualifying.  Store as a POSITIVE number (e.g. ``0.54`` for a
        54p loss); :func:`offers_summary` sums these as costs.
    free_bet_value:
        Face value of the free bet unlocked (optional).
    lay_venue:
        Exchange used to lay (e.g. ``"smarkets"``).
    extracted_value:
        Locked cash extracted from the free bet (optional).
    status:
        One of ``claimed`` / ``qualified`` / ``extracted`` / ``expired``.
    notes:
        Free-text notes (optional).
    ts_utc:
        Creation timestamp; defaults to now (UTC).
    db_path:
        Path to the SQLite database file.

    Raises
    ------
    ValueError
        If ``status`` is not a recognised lifecycle state.
    """
    if status not in _VALID_STATUS:
        raise ValueError(
            "status must be one of %s, got %r" % (", ".join(_VALID_STATUS), status)
        )
    init_db(db_path)
    ts = ts_utc or _now_utc()
    sql = """
        INSERT INTO sb_offers
            (ts_utc, account_holder, bookmaker, offer_desc, offer_type,
             qualifying_stake, qualifying_loss, free_bet_value, lay_venue,
             extracted_value, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            sql,
            (
                ts,
                account_holder,
                bookmaker,
                offer_desc,
                offer_type,
                _f(qualifying_stake),
                _f(qualifying_loss),
                _f(free_bet_value),
                lay_venue,
                _f(extracted_value),
                status,
                notes,
            ),
        )
        return cur.lastrowid


def _f(v: Optional[float]) -> Optional[float]:
    return float(v) if v is not None else None


def update_offer(offer_id: int, db_path: str = _DEFAULT_DB, **fields: Any) -> None:
    """Update one or more columns of an existing offer.

    Pass updatable columns as keyword arguments, e.g.::

        update_offer(3, status="extracted", extracted_value=23.79)

    Numeric columns are coerced to ``float``.  ``status``, if supplied, is
    validated against the lifecycle states.

    Raises
    ------
    KeyError
        If no offer with ``offer_id`` exists.
    ValueError
        If an unknown column is supplied, no fields are given, or an invalid
        ``status`` is given.
    """
    if not fields:
        raise ValueError("no fields to update")
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError("unknown field(s): %s" % ", ".join(sorted(unknown)))
    if "status" in fields and fields["status"] not in _VALID_STATUS:
        raise ValueError(
            "status must be one of %s, got %r"
            % (", ".join(_VALID_STATUS), fields["status"])
        )

    numeric = {
        "qualifying_stake",
        "qualifying_loss",
        "free_bet_value",
        "extracted_value",
    }
    set_parts = []
    values: List[Any] = []
    for col, val in fields.items():
        set_parts.append("%s = ?" % col)
        values.append(_f(val) if col in numeric else val)
    values.append(offer_id)

    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM sb_offers WHERE id = ?", (offer_id,)
        ).fetchone()
        if row is None:
            raise KeyError("no offer with id=%d" % offer_id)
        conn.execute(
            "UPDATE sb_offers SET %s WHERE id = ?" % ", ".join(set_parts),
            tuple(values),
        )


def get_offer(offer_id: int, db_path: str = _DEFAULT_DB) -> Optional[sqlite3.Row]:
    """Return the row for a single offer, or ``None`` if not found."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM sb_offers WHERE id = ?", (offer_id,)
        ).fetchone()


def list_offers(db_path: str = _DEFAULT_DB) -> List[sqlite3.Row]:
    """Return all offer rows ordered by ``id``."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return conn.execute("SELECT * FROM sb_offers ORDER BY id").fetchall()


# ---------------------------------------------------------------------------
# Summary.
# ---------------------------------------------------------------------------


def offers_summary(db_path: str = _DEFAULT_DB) -> Dict[str, Any]:
    """Aggregate the promo-extraction ledger.

    NOTE: this is the matched-betting / promo view only.  These figures are
    intentionally NOT part of :func:`wca.ledger.reports.summary`, the CLV
    report, or calibration — they are risk-free promo extraction and are
    tracked apart from model edge.

    Returns
    -------
    dict with keys:
        ``n_offers``
            Total number of offer rows.
        ``by_status``
            ``{status: count}`` over all lifecycle states present.
        ``total_free_bet_value``
            Sum of ``free_bet_value`` over all rows (nulls treated as 0).
        ``total_extracted``
            Sum of ``extracted_value`` over all rows.
        ``total_qualifying_loss``
            Sum of ``qualifying_loss`` over all rows (the cost of qualifying;
            stored positive).
        ``net_locked``
            ``total_extracted - total_qualifying_loss`` — the net risk-free
            value locked in.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM sb_offers").fetchall()

    n_offers = len(rows)
    by_status: Dict[str, int] = {}
    total_free_bet = 0.0
    total_extracted = 0.0
    total_qual_loss = 0.0
    for r in rows:
        st = r["status"]
        by_status[st] = by_status.get(st, 0) + 1
        if r["free_bet_value"] is not None:
            total_free_bet += float(r["free_bet_value"])
        if r["extracted_value"] is not None:
            total_extracted += float(r["extracted_value"])
        if r["qualifying_loss"] is not None:
            total_qual_loss += float(r["qualifying_loss"])

    return {
        "n_offers": n_offers,
        "by_status": by_status,
        "total_free_bet_value": round(total_free_bet, 2),
        "total_extracted": round(total_extracted, 2),
        "total_qualifying_loss": round(total_qual_loss, 2),
        "net_locked": round(total_extracted - total_qual_loss, 2),
    }
