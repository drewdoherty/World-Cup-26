"""Polymarket data access: Gamma (metadata), CLOB (books/price history),
Data-API (trades) — plus the repo's REAL captured datasets.

Offline backbone (no network, no fabrication):
* ``data/pm_orderflow.db``  — production trade-level capture: pm_trades
  (~2.09M real trades) + pm_markets (~1.4k WC markets with slugs, questions,
  outcomes, token ids, volume, liquidity). Read-only.
* ``site/advancement_data.json`` — latest advancement model-vs-PM feed.

Live layers (require the VPN route on this MacBook):
* Gamma  ``gamma-api.polymarket.com``  events/markets/tags metadata
* CLOB   ``clob.polymarket.com``       /book, /prices-history (reuses
  production ``wca.data.pm_clob_history``)
* Data   ``data-api.polymarket.com``   /trades, /holders (offset cap 3000 —
  production caveat; incremental capture lives in ``wca.pm.orderflow``)

Every live payload is written to the raw layer before use; every reader
returns Polars frames with source IDs preserved.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import polars as pl
import requests

import lib.bootstrap as bt
import lib.storage as st

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
SOURCE = "polymarket"


class PMUnavailable(Exception):
    """A PM endpoint could not be reached (VPN down / blocked / offline)."""


def _get_json(url: str, params: Dict[str, Any], *, retries: int = 3,
              timeout: float = 20.0) -> Tuple[Any, int, Dict[str, str]]:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "wca-research/1.0"})
            return (r.json() if r.content else None), r.status_code, dict(r.headers)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            time.sleep(1.5 * (2 ** attempt))
    raise PMUnavailable(f"{url}: {last}")


# ---------------------------------------------------------------------------
# Gamma — events/markets metadata (paginated), raw-captured
# ---------------------------------------------------------------------------

def gamma_wc_events(*, offline: bool = False, closed: Optional[bool] = None,
                    max_pages: int = 30) -> Tuple[List[dict], List[str]]:
    """All FIFA World Cup events from Gamma (slug prefix ``fifa-world-cup``
    tag search + free-text), paginated. Returns (events, snapshot_ids)."""
    endpoint = "gamma_events_wc"
    if offline:
        snap = st.latest_raw(SOURCE, endpoint)
        if not snap:
            raise PMUnavailable("offline and gamma never captured")
        return st.read_raw(snap), [snap]
    events: Dict[str, dict] = {}
    snaps: List[str] = []
    for q in ("world cup", "fifa"):
        offset = 0
        for _ in range(max_pages):
            params: Dict[str, Any] = {"q": q, "limit": 100, "offset": offset}
            if closed is not None:
                params["closed"] = str(closed).lower()
            data, status, _hdr = _get_json(f"{GAMMA}/events", params)
            page = data if isinstance(data, list) else (data or {}).get("data", [])
            if status >= 400 or not page:
                break
            for ev in page:
                if _is_wc_event(ev):
                    events[str(ev.get("id"))] = ev
            offset += len(page)
            if len(page) < 100:
                break
    merged = list(events.values())
    snaps.append(st.write_raw(SOURCE, endpoint, merged,
                              params={"queries": "world cup|fifa",
                                      "closed": closed},
                              status=200, url=f"{GAMMA}/events"))
    return merged, snaps


def _is_wc_event(ev: dict) -> bool:
    blob = " ".join(str(ev.get(k) or "") for k in ("slug", "title", "ticker")).lower()
    return ("fifa" in blob or "world cup" in blob) and "club" not in blob


# ---------------------------------------------------------------------------
# CLOB — order books + dense price history (reuses production module)
# ---------------------------------------------------------------------------

def clob_book(token_id: str, *, capture: bool = True) -> Optional[dict]:
    """Full order book for one token (bids/asks arrays), raw-captured."""
    data, status, _ = _get_json("https://clob.polymarket.com/book",
                                {"token_id": token_id})
    if status >= 400 or not isinstance(data, dict):
        return None
    if capture:
        st.write_raw(SOURCE, "clob_book", data,
                     params={"token_id": token_id}, status=status,
                     url="https://clob.polymarket.com/book")
    return data


def clob_price_history(token_id: str, *, interval: str = "max",
                       fidelity: int = 60, capture: bool = True) -> pl.DataFrame:
    """Dense per-token price history via production pm_clob_history."""
    from wca.data.pm_clob_history import price_history
    pts = price_history(token_id, interval=interval, fidelity=fidelity) or []
    if capture and pts:
        st.write_raw(SOURCE, "clob_prices_history", pts,
                     params={"token_id": token_id, "interval": interval,
                             "fidelity": fidelity}, status=200,
                     url="https://clob.polymarket.com/prices-history")
    schema = {"t": pl.Int64, "p": pl.Float64}
    rows = [{"t": int(x["t"]), "p": float(x["p"])} for x in pts
            if isinstance(x, dict) and "t" in x and "p" in x]
    df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    return df.with_columns(
        pl.from_epoch("t", time_unit="s").dt.replace_time_zone("UTC")
          .alias("ts_utc"),
        pl.lit(token_id).alias("token_id"))


def book_metrics(book: dict) -> Dict[str, Optional[float]]:
    """bid/ask/mid/spread/microprice + depth within 1¢/5¢ of touch, from a
    raw CLOB book payload. Pure function — unit-tested."""
    def _levels(side: str) -> List[Tuple[float, float]]:
        return sorted(((float(l["price"]), float(l["size"]))
                       for l in (book.get(side) or [])),
                      key=lambda x: x[0], reverse=(side == "bids"))
    bids, asks = _levels("bids"), _levels("asks")
    bb = bids[0][0] if bids else None
    ba = asks[0][0] if asks else None
    bbs = bids[0][1] if bids else None
    bas = asks[0][1] if asks else None
    mid = (bb + ba) / 2 if bb is not None and ba is not None else None
    micro = ((bb * bas + ba * bbs) / (bbs + bas)
             if None not in (bb, ba, bbs, bas) and (bbs + bas) > 0 else None)
    def _depth(levels, ref, width, price_x_size):
        if ref is None:
            return None
        tot = 0.0
        for p, s in levels:
            if abs(p - ref) <= width:
                tot += p * s if price_x_size else s
        return tot
    return {
        "best_bid": bb, "best_ask": ba, "mid": mid, "microprice": micro,
        "spread": (ba - bb) if None not in (bb, ba) else None,
        "bid_sz_top": bbs, "ask_sz_top": bas,
        "depth_bid_5c_usd": _depth(bids, bb, 0.05, True),
        "depth_ask_5c_usd": _depth(asks, ba, 0.05, True),
    }


# ---------------------------------------------------------------------------
# Orderflow DB (read-only) — the real offline trades/markets source
# ---------------------------------------------------------------------------

def _ro_conn(path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def orderflow_markets() -> pl.DataFrame:
    """pm_markets: 1 row per condition_id with slugs/outcomes/token_ids."""
    schema = {"condition_id": pl.Utf8, "event_slug": pl.Utf8,
              "market_slug": pl.Utf8, "question": pl.Utf8,
              "event_title": pl.Utf8, "category": pl.Utf8, "team": pl.Utf8,
              "outcomes": pl.Utf8, "token_ids": pl.Utf8, "closed": pl.Int64,
              "resolved_outcome_index": pl.Int64, "end_date": pl.Utf8,
              "game_start_time": pl.Utf8, "volume": pl.Float64,
              "liquidity": pl.Float64, "fetched_utc": pl.Utf8}
    with _ro_conn(bt.ORDERFLOW_DB) as conn:
        rows = conn.execute(
            "SELECT " + ", ".join(schema) + " FROM pm_markets").fetchall()
    return pl.DataFrame(rows, schema=schema, orient="row")


def orderflow_trades(*, condition_ids: Optional[Iterable[str]] = None,
                     since_ts: Optional[int] = None) -> pl.LazyFrame:
    """pm_trades as a LazyFrame (2M+ rows — filter before collecting).

    Loads via chunked SQLite reads into Polars; ts is epoch seconds UTC."""
    where, args = [], []
    if condition_ids is not None:
        ids = list(condition_ids)
        where.append(f"condition_id IN ({','.join('?' * len(ids))})")
        args.extend(ids)
    if since_ts is not None:
        where.append("ts >= ?")
        args.append(int(since_ts))
    sql = ("SELECT condition_id, asset, outcome, outcome_index, wallet, "
           "side, size, price, usd, ts, tx_hash FROM pm_trades")
    if where:
        sql += " WHERE " + " AND ".join(where)
    schema = {"condition_id": pl.Utf8, "asset": pl.Utf8, "outcome": pl.Utf8,
              "outcome_index": pl.Int64, "wallet": pl.Utf8, "side": pl.Utf8,
              "size": pl.Float64, "price": pl.Float64, "usd": pl.Float64,
              "ts": pl.Int64, "tx_hash": pl.Utf8}
    with _ro_conn(bt.ORDERFLOW_DB) as conn:
        cur = conn.execute(sql, args)
        frames = []
        while True:
            chunk = cur.fetchmany(200_000)
            if not chunk:
                break
            frames.append(pl.DataFrame(chunk, schema=schema, orient="row"))
    out = (pl.concat(frames) if frames else pl.DataFrame(schema=schema))
    return (out.with_columns(
        pl.from_epoch("ts", time_unit="s").dt.replace_time_zone("UTC")
          .alias("ts_utc")).lazy())


# ---------------------------------------------------------------------------
# Data-API trades/holders (live; offset-capped — production caveat)
# ---------------------------------------------------------------------------

def data_api_trades(condition_id: str, *, max_offset: int = 3000,
                    capture: bool = True) -> List[dict]:
    """Recent trades for one market. HARD CAP: the endpoint stops serving
    beyond offset 3000 (production-verified), so this is a RECENT window,
    not full history — full history lives in pm_orderflow.db."""
    out: List[dict] = []
    offset = 0
    while offset <= max_offset:
        data, status, _ = _get_json(f"{DATA_API}/trades",
                                    {"market": condition_id, "limit": 500,
                                     "offset": offset})
        if status >= 400 or not data:
            break
        out.extend(data)
        if len(data) < 500:
            break
        offset += 500
    if capture and out:
        st.write_raw(SOURCE, "dataapi_trades", out,
                     params={"market": condition_id}, status=200,
                     url=f"{DATA_API}/trades")
    return out


def data_api_holders(condition_id: str, *, capture: bool = True) -> List[dict]:
    data, status, _ = _get_json(f"{DATA_API}/holders",
                                {"market": condition_id, "limit": 100})
    holders = data if isinstance(data, list) else (data or {}).get("holders", [])
    if capture and holders:
        st.write_raw(SOURCE, "dataapi_holders", holders,
                     params={"market": condition_id}, status=status,
                     url=f"{DATA_API}/holders")
    return holders or []
