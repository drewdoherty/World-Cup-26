"""Notion API client — auth, database access, page CRUD.

Auth: Internal Integration Token from NOTION_TOKEN env var.
Database: single "WCA Bet Ledger" DB, ID from NOTION_BET_DB_ID env var.
Each bet is one page; status property ("open"/"won"/"lost"/"void"/"cashed")
distinguishes open from closed — no second database needed.

Never import this directly in tests — patch at the sync level.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

_ENV_TOKEN = "NOTION_TOKEN"
_ENV_DB_ID = "NOTION_BET_DB_ID"

# Property names as they appear in the Notion database.
# Keep in sync with wca_notion_setup.py which creates them.
PROP_BET_ID       = "bet_id"
PROP_PLACED_UTC   = "placed_utc"
PROP_MATCH_DESC   = "match_desc"
PROP_MARKET       = "market"
PROP_SELECTION    = "selection"
PROP_PLATFORM     = "platform"
PROP_ACCOUNT      = "account"
PROP_SOURCE       = "source"
PROP_ODDS         = "odds"
PROP_STAKE        = "stake"
PROP_MODEL_PROB   = "model_prob"
PROP_MKT_PROB     = "mkt_prob_devig"
PROP_EV           = "ev"
PROP_KELLY        = "kelly_frac"
PROP_NOTES        = "notes"
PROP_TOKEN_ID     = "token_id"
PROP_STATUS       = "status"
PROP_SETTLED_PL   = "settled_pl"
PROP_CLOSING_ODDS = "closing_odds"
PROP_CLV_PCT      = "clv_pct"
PROP_SETTLED_UTC  = "settled_utc"

# Editable from Notion — values pulled back to wca.db on sync
EDITABLE_PROPS = {PROP_NOTES, PROP_CLOSING_ODDS}

OPEN_STATUS = "open"
CLOSED_STATUSES = {"won", "lost", "void", "cashed"}


def _token() -> str:
    tok = os.environ.get(_ENV_TOKEN, "")
    if not tok:
        raise RuntimeError(
            "NOTION_TOKEN env var not set. "
            "See docs/notion_setup.md for instructions."
        )
    return tok


def _db_id() -> str:
    did = os.environ.get(_ENV_DB_ID, "")
    if not did:
        raise RuntimeError(
            "NOTION_BET_DB_ID env var not set. "
            "Run wca_notion_setup.py first to create the database."
        )
    return did


def connect():
    """Return a notion_client.Client authenticated with NOTION_TOKEN."""
    from notion_client import Client  # type: ignore
    return Client(auth=_token())


def query_all(client, db_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch all pages from the database, handling pagination."""
    db_id = db_id or _db_id()
    pages: List[Dict[str, Any]] = []
    cursor = None
    while True:
        kwargs: Dict[str, Any] = {"database_id": db_id}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = _retry(lambda k=kwargs: client.databases.query(**k))
        pages.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return pages


def get_page_by_bet_id(client, bet_id: int, db_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the Notion page for this bet_id, or None if not found."""
    db_id = db_id or _db_id()
    resp = _retry(lambda: client.databases.query(
        database_id=db_id,
        filter={"property": PROP_BET_ID, "number": {"equals": bet_id}},
    ))
    results = resp.get("results", [])
    return results[0] if results else None


def create_page(client, props: Dict[str, Any], db_id: Optional[str] = None) -> Dict[str, Any]:
    db_id = db_id or _db_id()
    return _retry(lambda: client.pages.create(
        parent={"database_id": db_id},
        properties=props,
    ))


def update_page(client, page_id: str, props: Dict[str, Any]) -> Dict[str, Any]:
    return _retry(lambda: client.pages.update(page_id=page_id, properties=props))


def archive_page(client, page_id: str) -> None:
    _retry(lambda: client.pages.update(page_id=page_id, archived=True))


# ---- property builders ------------------------------------------------------

def _text(val: Any) -> Dict:
    return {"rich_text": [{"text": {"content": str(val or "")}}]}


def _title(val: Any) -> Dict:
    return {"title": [{"text": {"content": str(val or "")}}]}


def _num(val: Any) -> Dict:
    try:
        return {"number": float(val)} if val not in (None, "", "None") else {"number": None}
    except (TypeError, ValueError):
        return {"number": None}


def _select(val: Any) -> Dict:
    return {"select": {"name": str(val)}} if val else {"select": None}


def _date(val: Any) -> Dict:
    if not val:
        return {"date": None}
    s = str(val).strip()
    if "T" not in s and " " in s:
        s = s.replace(" ", "T")
    if not s.endswith("Z") and "+" not in s:
        s += "Z"
    return {"date": {"start": s}}


def bet_to_props(b: Dict[str, Any]) -> Dict[str, Any]:
    """Map a wca.db bet row to Notion property payload."""
    return {
        PROP_BET_ID:       _num(b.get("id")),
        PROP_PLACED_UTC:   _date(b.get("ts_utc")),
        PROP_MATCH_DESC:   _title(b.get("match_desc", "")),
        PROP_MARKET:       _text(b.get("market", "")),
        PROP_SELECTION:    _text(b.get("selection", "")),
        PROP_PLATFORM:     _select(b.get("platform", "")),
        PROP_ACCOUNT:      _select(b.get("account", "1")),
        PROP_SOURCE:       _select(b.get("source", "model")),
        PROP_ODDS:         _num(b.get("decimal_odds")),
        PROP_STAKE:        _num(b.get("stake")),
        PROP_MODEL_PROB:   _num(b.get("model_prob")),
        PROP_MKT_PROB:     _num(b.get("market_prob_devig")),
        PROP_EV:           _num(b.get("ev")),
        PROP_KELLY:        _num(b.get("kelly_fraction")),
        PROP_NOTES:        _text(b.get("notes", "")),
        PROP_TOKEN_ID:     _text(b.get("token_id", "")),
        PROP_STATUS:       _select(b.get("status", "open")),
        PROP_SETTLED_PL:   _num(b.get("settled_pl")),
        PROP_CLOSING_ODDS: _num(b.get("closing_odds")),
        PROP_CLV_PCT:      _num(b.get("clv")),
        PROP_SETTLED_UTC:  _date(b.get("settled_ts")),
    }


def page_to_editable(page: Dict[str, Any]) -> Dict[str, Any]:
    """Extract editable field values from a Notion page (for pull-back to wca.db)."""
    props = page.get("properties", {})

    def _get_text(key: str) -> str:
        rt = props.get(key, {}).get("rich_text", [])
        return rt[0]["text"]["content"] if rt else ""

    def _get_num(key: str) -> Optional[float]:
        return props.get(key, {}).get("number")

    return {
        "notes":        _get_text(PROP_NOTES),
        "closing_odds": _get_num(PROP_CLOSING_ODDS),
    }


# ---- retry ------------------------------------------------------------------

def _retry(fn, retries: int = 3, delay: float = 2.0) -> Any:
    last_err = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            # Notion rate limit is HTTP 429; also retry transient 5xx
            last_err = exc
            msg = str(exc).lower()
            if "429" in msg or "500" in msg or "503" in msg:
                if attempt < retries - 1:
                    time.sleep(delay * (attempt + 1))
            else:
                raise
    raise last_err
