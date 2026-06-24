"""Idempotency / claim / cooldown state for the cash-out daemon.

A kill is a LEVEL, not an edge: once "0-0" is dead the score stays past the
threshold for the rest of the match, so a naive watcher would re-propose the
same SELL on every poll tick. This SQLite table records, per outcome token, both
the VAR-cooldown clock (when the kill was first observed) and the claim that
prevents double-selling — and it survives a daemon restart, so a mid-match crash
can neither reset the cooldown nor double-sell.

Phase lifecycle per ``asset`` (token id):

    (absent) ─observe()─▶ 'observed' ─claim()─▶ 'claimed' ─┬─ mark_sold()  ─▶ 'sold'
                  ▲                                        └─ set_phase()  ─▶ 'settle_failed'
                  └──────────────  clear()  / set_phase('observed')  ◀──────┘

* ``observed``      — kill seen; cooldown ticking. NOT "handled" (still actionable).
* ``claimed``       — we are placing the SELL right now (transient).
* ``sold``          — booked. Terminal; dedup blocks any re-sell.
* ``settle_failed`` — live order went out but booking/fill was unconfirmed.
                      "handled" so the watcher never auto-retries the order; needs
                      manual reconciliation.

``observe`` stamps the first-seen wall-clock epoch ONCE (so the cooldown is
computed from the original sighting even across a restart). ``claim`` atomically
upgrades ``observed``→``claimed`` (or inserts fresh); a racing caller loses.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Optional

_DDL = """
CREATE TABLE IF NOT EXISTS pm_cashout_state (
    asset            TEXT PRIMARY KEY,
    match_id         TEXT,
    phase            TEXT NOT NULL,
    detail           TEXT,
    first_kill_epoch REAL,
    ts_utc           TEXT NOT NULL,
    updated_utc      TEXT
)
"""

_HANDLED = ("claimed", "sold", "settle_failed")


def _now() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_DDL)
    return conn


def init(db_path: str) -> None:
    with _conn(db_path):
        pass


def get_phase(asset: str, db_path: str) -> Optional[str]:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT phase FROM pm_cashout_state WHERE asset=?", (str(asset),)
        ).fetchone()
    return row[0] if row else None


def observe(asset: str, match_id: str, epoch: float, *, detail: str = "",
            db_path: str) -> float:
    """Record (once) that *asset*'s kill was first seen at wall-clock *epoch*.

    Returns the stored first-seen epoch — the original one if already present, so
    the VAR cooldown is measured from the first sighting even across a restart.
    """
    now = _now()
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO pm_cashout_state "
            "(asset, match_id, phase, detail, first_kill_epoch, ts_utc, updated_utc) "
            "VALUES (?,?, 'observed', ?, ?, ?, ?)",
            (str(asset), str(match_id or ""), detail, float(epoch), now, now),
        )
        row = conn.execute(
            "SELECT first_kill_epoch FROM pm_cashout_state WHERE asset=?", (str(asset),)
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else float(epoch)


def claim(asset: str, match_id: str, *, detail: str = "", db_path: str) -> bool:
    """Atomically take *asset* for selling. ``True`` iff WE won the claim.

    Wins by inserting a fresh ``claimed`` row, or by upgrading an existing
    ``observed`` row to ``claimed`` (only one caller's UPDATE can match). Returns
    ``False`` if it is already claimed / sold / settle_failed (dedup).
    """
    now = _now()
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pm_cashout_state "
            "(asset, match_id, phase, detail, first_kill_epoch, ts_utc, updated_utc) "
            "VALUES (?,?, 'claimed', ?, NULL, ?, ?)",
            (str(asset), str(match_id or ""), detail, now, now),
        )
        if cur.rowcount == 1:
            return True
        up = conn.execute(
            "UPDATE pm_cashout_state SET phase='claimed', detail=?, updated_utc=? "
            "WHERE asset=? AND phase='observed'",
            (detail, now, str(asset)),
        )
        return up.rowcount == 1


def set_phase(asset: str, phase: str, *, detail: str = "", db_path: str) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE pm_cashout_state SET phase=?, detail=?, updated_utc=? WHERE asset=?",
            (phase, detail, _now(), str(asset)),
        )


def mark_sold(asset: str, *, detail: str = "", db_path: str) -> None:
    set_phase(asset, "sold", detail=detail, db_path=db_path)


def revert_to_observed(asset: str, *, detail: str = "", db_path: str) -> None:
    """Drop a transient ``claimed`` back to ``observed`` (kept cooldown epoch) so
    a no-fill / dry-arm can be retried later without restarting the cooldown."""
    set_phase(asset, "observed", detail=detail, db_path=db_path)


def clear(asset: str, db_path: str) -> None:
    """Forget *asset* entirely (e.g. a VAR-reversed goal)."""
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM pm_cashout_state WHERE asset=?", (str(asset),))


def is_handled(asset: str, db_path: str) -> bool:
    """True if *asset* is claimed/sold/settle_failed — don't start a new sell.

    ``settle_failed`` is included: the order already went out, so it must be
    reconciled manually, never auto-retried. ``observed`` is NOT handled (the
    cooldown is still running and the position is still actionable).
    """
    return get_phase(asset, db_path) in _HANDLED
