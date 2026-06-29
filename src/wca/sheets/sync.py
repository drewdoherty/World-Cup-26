"""Bi-directional sync between wca.db bets and the Google Sheet.

Push direction (wca.db → sheet):
  - New bet in wca.db → append row to Open Bets tab
  - Bet settled in wca.db → remove from Open Bets, append to Closed Bets
  - CLV computed → update Closed Bets row

Pull direction (sheet → wca.db):
  - notes column edited → written back to wca.db
  - closing_odds edited manually → written back + CLV recomputed
  - Manual settlement override via sheet status column → reflected in wca.db

Full sync (run periodically, e.g. every 10 min via analytics-live):
  - Reconcile ALL rows; push missing bets; pull any manual edits.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = "data/wca.db"


# ---- helpers ----------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _clv(decimal_odds: Optional[float], closing_odds: Optional[float]) -> Optional[float]:
    """Return (decimal_odds / closing_odds - 1) or None."""
    if decimal_odds and closing_odds and closing_odds > 0:
        return round(decimal_odds / closing_odds - 1, 6)
    return None


def _bet_to_open_row(b: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "bet_id":        str(b["id"]),
        "placed_utc":    b.get("ts_utc", ""),
        "match_desc":    b.get("match_desc", ""),
        "market":        b.get("market", ""),
        "selection":     b.get("selection", ""),
        "platform":      b.get("platform", ""),
        "account":       b.get("account", "1"),
        "source":        b.get("source", "model"),
        "odds":          b.get("decimal_odds", ""),
        "stake":         b.get("stake", ""),
        "model_prob":    b.get("model_prob", ""),
        "mkt_prob_devig": b.get("market_prob_devig", ""),
        "ev":            b.get("ev", ""),
        "kelly_frac":    b.get("kelly_fraction", ""),
        "notes":         b.get("notes", ""),
        "token_id":      b.get("token_id", ""),
        "status":        "open",
    }


def _bet_to_closed_row(b: Dict[str, Any]) -> Dict[str, Any]:
    row = _bet_to_open_row(b)
    row["status"] = b.get("status", "")
    row.update({
        "result":       b.get("status", ""),
        "settled_pl":   b.get("settled_pl", ""),
        "closing_odds": b.get("closing_odds", ""),
        "clv_pct":      _clv(b.get("decimal_odds"), b.get("closing_odds")),
        "settled_utc":  b.get("settled_ts", ""),
    })
    return row


def _fetch_all_bets(db_path: str) -> List[Dict[str, Any]]:
    con = _connect(db_path)
    try:
        rows = con.execute("SELECT * FROM bets ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---- public API -------------------------------------------------------------

def full_sync(db_path: str = _DEFAULT_DB, dry_run: bool = False) -> Dict[str, int]:
    """Reconcile wca.db against the sheet. Idempotent — safe to run any time.

    Returns counts: {pushed_open, pushed_closed, pulled_notes, pulled_closes}.
    """
    # Import here so the module is importable even when gspread is absent
    from wca.sheets import client

    counts = {"pushed_open": 0, "pushed_closed": 0, "pulled_notes": 0, "pulled_closes": 0}

    sh = client.connect()
    ws_open   = client.get_or_create_worksheet(sh, client.TAB_OPEN,   client.OPEN_HEADERS)
    ws_closed = client.get_or_create_worksheet(sh, client.TAB_CLOSED, client.CLOSED_HEADERS)

    bets = _fetch_all_bets(db_path)
    open_bets   = [b for b in bets if b["status"] == "open"]
    closed_bets = [b for b in bets if b["status"] != "open"]

    # -- Pull: read existing sheet state first so we can detect manual edits --
    sheet_open   = {str(r["bet_id"]): r for r in client.all_rows_as_dicts(ws_open)   if r.get("bet_id")}
    sheet_closed = {str(r["bet_id"]): r for r in client.all_rows_as_dicts(ws_closed) if r.get("bet_id")}

    # Pull manual edits back to wca.db before overwriting
    counts["pulled_notes"]  += _pull_notes(bets, sheet_open, sheet_closed, db_path, dry_run)
    counts["pulled_closes"] += _pull_closes(bets, sheet_closed, db_path, dry_run)

    # -- Push: ensure every bet is in the right tab --
    for b in open_bets:
        bid = str(b["id"])
        # If erroneously in Closed tab (shouldn't happen), remove it
        if bid in sheet_closed:
            logger.warning("bet %s found in Closed tab but is open; removing", bid)
            if not dry_run:
                client.delete_row_by_key(ws_closed, "bet_id", bid)

        row = _bet_to_open_row(b)
        # Preserve manual notes from the sheet
        if bid in sheet_open and sheet_open[bid].get("notes"):
            row["notes"] = sheet_open[bid]["notes"]

        if not dry_run:
            client.upsert_row(ws_open, client.OPEN_HEADERS, row)
        counts["pushed_open"] += 1

    for b in closed_bets:
        bid = str(b["id"])
        # Remove from Open tab if present (bet was settled since last sync)
        if bid in sheet_open:
            logger.info("moving bet %s from Open → Closed tab", bid)
            if not dry_run:
                client.delete_row_by_key(ws_open, "bet_id", bid)

        row = _bet_to_closed_row(b)
        if bid in sheet_closed and sheet_closed[bid].get("notes"):
            row["notes"] = sheet_closed[bid]["notes"]

        if not dry_run:
            client.upsert_row(ws_closed, client.CLOSED_HEADERS, row)
        counts["pushed_closed"] += 1

    logger.info("sync complete: %s", counts)
    return counts


def push_single_bet(bet_id: int, db_path: str = _DEFAULT_DB) -> None:
    """Push one freshly recorded bet to the Open Bets tab.

    Called immediately after record_bet() so the row appears in the
    sheet without waiting for the next full sync.
    """
    from wca.sheets import client

    con = _connect(db_path)
    try:
        row = con.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    finally:
        con.close()

    if row is None:
        logger.warning("push_single_bet: bet %d not found", bet_id)
        return

    b = dict(row)
    sh = client.connect()
    ws = client.get_or_create_worksheet(sh, client.TAB_OPEN, client.OPEN_HEADERS)
    client.upsert_row(ws, client.OPEN_HEADERS, _bet_to_open_row(b))
    logger.info("pushed bet %d to Open Bets sheet", bet_id)


def settle_in_sheet(bet_id: int, db_path: str = _DEFAULT_DB) -> None:
    """Move a just-settled bet from Open → Closed in the sheet.

    Called immediately after settle_bet() so the move is instant.
    """
    from wca.sheets import client

    con = _connect(db_path)
    try:
        row = con.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    finally:
        con.close()

    if row is None:
        logger.warning("settle_in_sheet: bet %d not found", bet_id)
        return

    b = dict(row)
    sh = client.connect()
    ws_open   = client.get_or_create_worksheet(sh, client.TAB_OPEN,   client.OPEN_HEADERS)
    ws_closed = client.get_or_create_worksheet(sh, client.TAB_CLOSED, client.CLOSED_HEADERS)

    client.delete_row_by_key(ws_open, "bet_id", str(bet_id))
    client.upsert_row(ws_closed, client.CLOSED_HEADERS, _bet_to_closed_row(b))
    logger.info("moved bet %d to Closed Bets sheet (status=%s)", bet_id, b["status"])


# ---- pull helpers -----------------------------------------------------------

def _pull_notes(
    bets: List[Dict[str, Any]],
    sheet_open: Dict[str, Any],
    sheet_closed: Dict[str, Any],
    db_path: str,
    dry_run: bool,
) -> int:
    """Write back edited notes from the sheet to wca.db."""
    updated = 0
    bets_by_id = {str(b["id"]): b for b in bets}

    for bid, sheet_row in {**sheet_open, **sheet_closed}.items():
        db_bet = bets_by_id.get(str(bid))
        if db_bet is None:
            continue
        sheet_notes = sheet_row.get("notes") or ""
        db_notes    = db_bet.get("notes") or ""
        if str(sheet_notes).strip() != str(db_notes).strip():
            logger.info("pulling notes edit for bet %s", bid)
            if not dry_run:
                _write_notes(int(bid), str(sheet_notes), db_path)
            updated += 1
    return updated


def _pull_closes(
    bets: List[Dict[str, Any]],
    sheet_closed: Dict[str, Any],
    db_path: str,
    dry_run: bool,
) -> int:
    """Pull manually-entered closing_odds from sheet → wca.db + recompute CLV."""
    updated = 0
    bets_by_id = {str(b["id"]): b for b in bets}

    for bid, sheet_row in sheet_closed.items():
        db_bet = bets_by_id.get(str(bid))
        if db_bet is None:
            continue
        sheet_close = sheet_row.get("closing_odds")
        db_close    = db_bet.get("closing_odds")
        if not sheet_close:
            continue
        try:
            sheet_close_f = float(str(sheet_close).replace(",", "."))
        except ValueError:
            continue
        if db_close and abs(float(db_close) - sheet_close_f) < 1e-6:
            continue  # already in sync

        decimal_odds = db_bet.get("decimal_odds")
        clv = _clv(float(decimal_odds) if decimal_odds else None, sheet_close_f)
        logger.info("pulling closing_odds %.4f for bet %s (CLV %.4f)", sheet_close_f, bid, clv or 0)
        if not dry_run:
            _write_closing(int(bid), sheet_close_f, clv, db_path)
        updated += 1
    return updated


def _write_notes(bet_id: int, notes: str, db_path: str) -> None:
    con = _connect(db_path)
    try:
        con.execute("UPDATE bets SET notes = ? WHERE id = ?", (notes, bet_id))
        con.commit()
    finally:
        con.close()


def _write_closing(bet_id: int, closing_odds: float, clv: Optional[float], db_path: str) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            "UPDATE bets SET closing_odds = ?, clv = ? WHERE id = ?",
            (closing_odds, clv, bet_id),
        )
        con.commit()
    finally:
        con.close()
