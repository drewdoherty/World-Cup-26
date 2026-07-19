"""Read-only Polymarket account snapshot for sizing and reconciliation.

The classic public Data API does not expose a cash wallet balance.  For the
deposit-wallet account we therefore publish an auditable marked-equity proxy:
resolved ``realizedPnl`` plus current marked value of open positions.  This is
deliberately named and surfaced as a proxy, never silently presented as cash.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEVELOPER_ADDRESS = "0x86b4C55A4DF1FBea0F325E842434E0a537CAa549"
DATA_API = "https://data-api.polymarket.com"
DEFAULT_TIMEOUT = 8


def _get(path: str, params: Dict[str, Any], *, session: Any = None,
         timeout: int = DEFAULT_TIMEOUT) -> Any:
    query = urllib.parse.urlencode(params)
    url = DATA_API + path + ("?" + query if query else "")
    if session is not None:
        resp = session.get(url, timeout=timeout)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        return resp.json()
    req = urllib.request.Request(url, headers={"User-Agent": "world-cup-alpha/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rows(path: str, address: str, *, session: Any = None) -> List[Dict[str, Any]]:
    data = _get(path, {"user": address, "limit": 500}, session=session)
    return data if isinstance(data, list) else []


def read_account(address: str = DEVELOPER_ADDRESS, *, session: Any = None,
                 now_utc: str = "") -> Dict[str, Any]:
    """Read current and resolved account data without keys or order access.

    ``balance_usd`` is the marked-equity proxy used for indicative sizing.  A
    failed read returns ``available=False`` and no balance, so callers cannot
    accidentally size from stale or fabricated capital.
    """
    address = str(address or DEVELOPER_ADDRESS).strip()
    try:
        open_rows = _rows("/positions", address, session=session)
        closed_rows = _rows("/closed-positions", address, session=session)
        open_value = sum(_num(row.get("currentValue")) for row in open_rows)
        closed_pnl = sum(_num(row.get("realizedPnl")) for row in closed_rows)
        equity = max(0.0, open_value + closed_pnl)
        return {
            "available": True,
            "address": address,
            "balance_usd": round(equity, 2),
            "open_value_usd": round(open_value, 2),
            "resolved_pnl_usd": round(closed_pnl, 2),
            "open_count": len(open_rows),
            "closed_count": len(closed_rows),
            "source": "data-api:closed-realized-pnl+open-mark",
            "method": "marked equity proxy; not cash balance",
            "captured_utc": now_utc or datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:  # noqa: BLE001 - feed must degrade explicitly
        return {
            "available": False,
            "address": address,
            "balance_usd": None,
            "source": "data-api",
            "method": "unavailable",
            "error": type(exc).__name__,
            "captured_utc": now_utc or datetime.now(timezone.utc).isoformat(),
        }


def closed_positions(address: str = DEVELOPER_ADDRESS, *, session: Any = None,
                     balance: Optional[Dict[str, Any]] = None) -> Optional[List[Dict[str, Any]]]:
    """Project resolved Data API rows into the site closed-position shape."""
    try:
        rows = _rows("/closed-positions", address, session=session)
    except Exception:
        return None
    bal = balance or {}
    out: List[Dict[str, Any]] = []
    for row in rows:
        cost = _num(row.get("avgPrice")) * _num(row.get("totalBought"))
        pnl = _num(row.get("realizedPnl"))
        avg = _num(row.get("avgPrice"))
        out.append({
            "id": "pm-closed-" + str(row.get("asset") or "")[:12],
            "ts_utc": "",
            "settled_ts": datetime.fromtimestamp(_num(row.get("timestamp")), timezone.utc).isoformat() if _num(row.get("timestamp")) else "",
            "match": row.get("title"), "match_id": row.get("conditionId"),
            "market": row.get("title"), "selection": row.get("outcome"),
            "platform": "polymarket", "venue": "polymarket", "account": "1",
            "source": "polymarket-api", "currency": "USD",
            "decimal_odds": (1.0 / avg) if avg > 0 else None,
            "stake": round(cost, 2) if cost else None,
            "model_prob": None, "market_prob_devig": None, "ev": None,
            "kelly_fraction": None, "status": "won" if pnl > 0 else "lost",
            "pl": round(pnl, 2), "closing_odds": None, "clv": None,
            "notes": "resolved PM position from developer proxy Data API",
            "pm_balance_usd": bal.get("balance_usd"),
            "pm_quarter_kelly_usd": ((bal.get("balance_usd") or 0.0) * 0.25
                                      if bal.get("available") else None),
            "data_source": "polymarket-data-api",
        })
    return out
