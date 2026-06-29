"""Bi-directional sync between wca.db bets and the Notion bet database.

Push direction (wca.db → Notion):
  - New bet → create page in database
  - Bet settled → update status + settled fields

Pull direction (Notion → wca.db):
  - notes edited → written back to wca.db
  - closing_odds entered manually → written back + CLV recomputed

Full sync (run periodically via analytics-live):
  - Reconcile ALL rows; push missing / stale pages; pull manual edits.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = "data/wca.db"


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _clv(decimal_odds: Optional[float], closing_odds: Optional[float]) -> Optional[float]:
    if decimal_odds and closing_odds and closing_odds > 0:
        return round(decimal_odds / closing_odds - 1, 6)
    return None


def _fetch_all_bets(db_path: str) -> List[Dict[str, Any]]:
    con = _connect(db_path)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM bets ORDER BY id").fetchall()]
    finally:
        con.close()


# ---- public API -------------------------------------------------------------

def full_sync(db_path: str = _DEFAULT_DB, dry_run: bool = False) -> Dict[str, int]:
    """Reconcile wca.db against Notion. Idempotent — safe to run any time.

    Returns counts: {pushed, updated, pulled_notes, pulled_closes}.
    """
    from wca.notion import client as nc

    counts = {"pushed": 0, "updated": 0, "pulled_notes": 0, "pulled_closes": 0}
    notion = nc.connect()

    bets = _fetch_all_bets(db_path)

    # Build index of existing Notion pages keyed by bet_id
    pages = nc.query_all(notion)
    notion_by_id: Dict[int, Dict] = {}
    for p in pages:
        num = p.get("properties", {}).get(nc.PROP_BET_ID, {}).get("number")
        if num is not None:
            notion_by_id[int(num)] = p

    for b in bets:
        bid = b["id"]
        existing = notion_by_id.get(bid)
        props = nc.bet_to_props(b)

        if existing is None:
            # New bet — create page
            if not dry_run:
                nc.create_page(notion, props)
            counts["pushed"] += 1
        else:
            # Pull manual edits BEFORE overwriting non-editable fields
            editable = nc.page_to_editable(existing)

            notes_changed = str(editable.get("notes") or "").strip() != str(b.get("notes") or "").strip()
            close_notion  = editable.get("closing_odds")
            close_db      = b.get("closing_odds")

            if notes_changed and editable.get("notes"):
                logger.info("pulling notes edit for bet %d", bid)
                if not dry_run:
                    _write_notes(bid, editable["notes"], db_path)
                    # Update local bet dict so push reflects it
                    b["notes"] = editable["notes"]
                counts["pulled_notes"] += 1

            if close_notion and (not close_db or abs(float(close_db) - float(close_notion)) > 1e-6):
                clv = _clv(b.get("decimal_odds"), close_notion)
                logger.info("pulling closing_odds %.4f for bet %d (clv %.4f)", close_notion, bid, clv or 0)
                if not dry_run:
                    _write_closing(bid, float(close_notion), clv, db_path)
                    b["closing_odds"] = close_notion
                    b["clv"] = clv
                counts["pulled_closes"] += 1

            # Update page to reflect canonical wca.db state
            if not dry_run:
                nc.update_page(notion, existing["id"], nc.bet_to_props(b))
            counts["updated"] += 1

    logger.info("notion sync complete: %s", counts)
    return counts


def push_single_bet(bet_id: int, db_path: str = _DEFAULT_DB) -> None:
    """Push one freshly recorded bet to Notion.

    Called immediately after record_bet() so the page appears without waiting
    for the next full sync.
    """
    from wca.notion import client as nc

    con = _connect(db_path)
    try:
        row = con.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    finally:
        con.close()

    if row is None:
        logger.warning("push_single_bet: bet %d not found", bet_id)
        return

    b = dict(row)
    notion = nc.connect()

    # Upsert: update if already exists (e.g. re-push after a failed sync)
    existing = nc.get_page_by_bet_id(notion, bet_id)
    if existing:
        nc.update_page(notion, existing["id"], nc.bet_to_props(b))
    else:
        nc.create_page(notion, nc.bet_to_props(b))

    logger.info("pushed bet %d to Notion (status=%s)", bet_id, b.get("status"))


def settle_in_notion(bet_id: int, db_path: str = _DEFAULT_DB) -> None:
    """Update a just-settled bet's Notion page with final status + P&L.

    Called immediately after settle_bet() so the page reflects settlement
    without waiting for the next full sync.
    """
    from wca.notion import client as nc

    con = _connect(db_path)
    try:
        row = con.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    finally:
        con.close()

    if row is None:
        logger.warning("settle_in_notion: bet %d not found", bet_id)
        return

    b = dict(row)
    notion = nc.connect()

    existing = nc.get_page_by_bet_id(notion, bet_id)
    if existing:
        nc.update_page(notion, existing["id"], nc.bet_to_props(b))
    else:
        # Shouldn't happen but handle gracefully
        nc.create_page(notion, nc.bet_to_props(b))

    logger.info("updated Notion bet %d to status=%s pl=%s", bet_id, b.get("status"), b.get("settled_pl"))


# ---- pull helpers -----------------------------------------------------------

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
