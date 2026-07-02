"""Read-only reconciliation between the canonical ledger and the Notion mirror.

The Notion "WCA Bet Ledger" is a manual import that drifts from the canonical
ledger (the mini's ``data/wca.db``). This module DIFFS the two by bet id and
reports what is missing / orphaned / mismatched — it writes NOTHING (neither the
ledger nor Notion). A later sync step can act on the report once reviewed.

Notion bulk-read via the MCP is plan-gated, so the live read uses the Notion REST
API with an integration token (``NOTION_TOKEN``); the diff core is pure and
testable independent of the network.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Dict, List, Optional, Sequence

NOTION_DB_ID = "3c4cfc10-b961-49c3-baed-fdb73084df76"  # WCA Bet Ledger database


def read_ledger(db_path: str) -> List[Dict[str, object]]:
    """Canonical ledger rows: {id, status, pl, stake, platform, match, selection}."""
    out: List[Dict[str, object]] = []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute(
            "SELECT id, status, settled_pl, stake, platform, match_desc, selection FROM bets"):
            out.append({"id": int(r["id"]), "status": (r["status"] or "").lower(),
                        "pl": r["settled_pl"], "stake": r["stake"], "platform": r["platform"],
                        "match": r["match_desc"], "selection": r["selection"]})
    finally:
        con.close()
    return out


def _prop(props, name, kind):
    p = props.get(name) or {}
    if kind == "number":
        return p.get("number")
    if kind == "select":
        return ((p.get("select") or {}) or {}).get("name")
    if kind == "title":
        t = p.get("title") or []
        return "".join(x.get("plain_text", "") for x in t)
    if kind == "rich_text":
        t = p.get("rich_text") or []
        return "".join(x.get("plain_text", "") for x in t)
    return None


def read_notion(db_id: str = NOTION_DB_ID, token: Optional[str] = None,
                *, timeout: float = 30.0) -> List[Dict[str, object]]:
    """All Notion ledger rows via the REST API (paginated). [] if no token/error."""
    tok = token or os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    if not tok:
        return []
    import requests

    url = "https://api.notion.com/v1/databases/%s/query" % db_id
    headers = {"Authorization": "Bearer %s" % tok, "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    out: List[Dict[str, object]] = []
    cursor = None
    try:
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            resp = requests.post(url, json=body, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            for pg in data.get("results", []):
                props = pg.get("properties") or {}
                bid = _prop(props, "ID", "number")
                if bid is None:
                    continue
                out.append({"id": int(bid), "status": (_prop(props, "Status", "select") or "").lower(),
                            "pl": _prop(props, "P/L", "number"), "stake": _prop(props, "Stake", "number"),
                            "platform": _prop(props, "Platform", "select")})
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    except Exception:
        return out  # partial is better than nothing; caller sees the count
    return out


def _num_eq(a, b, tol=0.01) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def diff_ledger_notion(ledger: Sequence[Dict[str, object]],
                       notion: Sequence[Dict[str, object]]) -> Dict[str, object]:
    """Compare by bet id. Returns missing_in_notion / orphan_in_notion / mismatched.

    A 'mismatch' is a bet present in both whose status or settled P&L differs.
    """
    lmap = {int(r["id"]): r for r in ledger}
    nmap = {int(r["id"]): r for r in notion}
    missing = sorted(set(lmap) - set(nmap))
    orphan = sorted(set(nmap) - set(lmap))
    mismatched = []
    for bid in sorted(set(lmap) & set(nmap)):
        lr, nr = lmap[bid], nmap[bid]
        diffs = {}
        if (lr.get("status") or "") != (nr.get("status") or ""):
            diffs["status"] = (lr.get("status"), nr.get("status"))
        if not _num_eq(lr.get("pl"), nr.get("pl")):
            diffs["pl"] = (lr.get("pl"), nr.get("pl"))
        if diffs:
            mismatched.append({"id": bid, "diffs": diffs,
                               "match": lr.get("match"), "selection": lr.get("selection")})
    return {
        "ledger_n": len(lmap), "notion_n": len(nmap),
        "missing_in_notion": [{"id": b, **{k: lmap[b].get(k) for k in ("status", "match", "selection", "platform")}}
                              for b in missing],
        "orphan_in_notion": [{"id": b, "status": nmap[b].get("status")} for b in orphan],
        "mismatched": mismatched,
        "in_sync": not (missing or orphan or mismatched),
    }
