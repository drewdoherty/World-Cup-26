"""Polymarket CLOB price-history client.

The Gamma ``/events`` snapshots give point-in-time prices; the CLOB
``prices-history`` endpoint gives the *full* per-token time series back to
market inception (futures markets reach weeks before kickoff), at a requested
fidelity. This is the authoritative trajectory source — far denser and deeper
than our own capture.

Reference: https://docs.polymarket.com  (CLOB ``/prices-history``)
Endpoint:  https://clob.polymarket.com/prices-history

Network-isolated here so callers/tests can pass the raw points elsewhere. Every
call is best-effort: on any error it returns ``[]`` rather than raising.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests

_BASE = "https://clob.polymarket.com/prices-history"
_TIMEOUT = 25
_HEADERS = {"User-Agent": "WorldCupAlpha/0.1 (research)", "Accept": "application/json"}

logger = logging.getLogger(__name__)

try:
    from wca.archive import tee as _archive_tee
except Exception:  # pragma: no cover
    _archive_tee = None

_BOOK_URL = "https://clob.polymarket.com/book"


def top_of_book(token_id: str, *, timeout: float = 15.0) -> Optional[dict]:
    """Live top-of-book for a CLOB ``token_id``: best bid/ask, sizes, mid, spread.

    A YES position EXITS at the bid (entry was the ask), so a transactable exit
    price needs the real book, not the mid-only price-history. Returns ``None`` on
    any error / empty book; ``{bid, bid_size, ask, ask_size, mid, spread}``."""
    if not token_id:
        return None
    try:
        resp = requests.get(_BOOK_URL, params={"token_id": str(token_id)},
                            headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        book = resp.json() or {}
    except Exception as exc:
        logger.debug("clob book fetch failed for %s: %s", token_id, exc)
        return None

    def _levels(side):
        out = []
        for lv in (book.get(side) or []):
            try:
                out.append((float(lv["price"]), float(lv["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    bids = sorted(_levels("bids"), reverse=True)
    asks = sorted(_levels("asks"))
    if not bids and not asks:
        return None
    bid = bids[0] if bids else (None, None)
    ask = asks[0] if asks else (None, None)
    mid = ((bid[0] + ask[0]) / 2.0) if (bid[0] is not None and ask[0] is not None) else (
        bid[0] if bid[0] is not None else ask[0])
    spread = (ask[0] - bid[0]) if (bid[0] is not None and ask[0] is not None) else None
    return {"bid": bid[0], "bid_size": bid[1], "ask": ask[0], "ask_size": ask[1],
            "mid": mid, "spread": spread}


#: Map a chart period to CLOB (interval, fidelity_minutes).
PERIOD_CLOB = {
    "24h-30m": ("1d", 30),
    "week-1h": ("1w", 60),
    "full-1h": ("max", 60),
}


def price_history(token_id: str, *, interval: str = "max", fidelity: int = 60,
                  start_ts: Optional[int] = None, end_ts: Optional[int] = None,
                  ) -> List[Tuple[datetime, float]]:
    """Return ``[(utc_datetime, price), ...]`` for a CLOB ``token_id``.

    ``interval`` is one of ``1m/1h/6h/1d/1w/max`` (ignored if ``start_ts``/
    ``end_ts`` are given); ``fidelity`` is the bar width in minutes. Prices are
    YES mids in [0,1]. Returns ``[]`` on any error or empty history.
    """
    if not token_id:
        return []
    params = {"market": str(token_id), "fidelity": int(fidelity)}
    if start_ts is not None and end_ts is not None:
        params["startTs"], params["endTs"] = int(start_ts), int(end_ts)
    else:
        params["interval"] = interval
    try:
        resp = requests.get(_BASE, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("clob prices-history failed for %s: %s", token_id, exc)
        return []
    if _archive_tee is not None:
        try:
            _archive_tee.raw("polymarket", "prices_history", data, kind="prices-history")
        except Exception:
            pass
    hist = (data or {}).get("history") or (data or {}).get("data") or []
    out: List[Tuple[datetime, float]] = []
    for pt in hist:
        try:
            t = int(pt["t"])
            p = float(pt["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 <= p <= 1.0:
            out.append((datetime.fromtimestamp(t, tz=timezone.utc), p))
    out.sort(key=lambda r: r[0])
    return out
