#!/usr/bin/env python
"""Generate the Market Intelligence feed (``market_intel.json``) for localhost:8001.

Read-only and offline: pulls recent venue quotes from ``odds_snapshots``
(``data/wca.db``, READ-ONLY), normalises them into ``MarketSnapshot``s
(:mod:`wca.intel.normalise`), keeps the latest quote per venue, and writes the
cross-venue derived-metrics feed atomically. All math lives in
:mod:`wca.intel.metrics` / :mod:`wca.intel.feed`; this CLI only gathers inputs
and the ``generated`` timestamp.

Honest scope: only the markets OddsAPI actually sells (moneyline/totals/BTTS/
AH-lay) are present; Betfair/Smarkets quotes are the OddsAPI relay (no live
liquidity); stale quotes are flagged in the feed, not hidden.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_market_intel.py \
        [--odds-db data/wca.db] [--out site-analytics/data/market_intel.json] \
        [--lookback-hours 12] [--horizon-hours 72] [--generated 2026-06-28T00:00:00Z]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.intel.feed import build_feed  # noqa: E402
from wca.intel.normalise import from_oddsapi_rows  # noqa: E402

_DEF_ODDS_DB = os.path.join(_ROOT, "data", "wca.db")
_DEF_OUT = os.path.join(_ROOT, "site-analytics", "data", "market_intel.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:%s?mode=ro&immutable=1" % os.path.abspath(db_path)
    return sqlite3.connect(uri, uri=True)


def _write_atomic(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".market_intel_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_rows(con: sqlite3.Connection, since_iso: str) -> List[dict]:
    """Recent odds_snapshots rows as from_oddsapi_rows-shaped dicts."""
    cur = con.execute(
        "SELECT ts_utc, source, match_id, market, selection, decimal_odds, raw "
        "FROM odds_snapshots WHERE ts_utc >= ? ORDER BY ts_utc",
        (since_iso,),
    )
    rows: List[dict] = []
    for ts_utc, source, match_id, market, selection, dec, raw in cur:
        rows.append({"ts_utc": ts_utc, "source": source, "match_id": match_id,
                     "market": market, "selection": selection,
                     "decimal_odds": dec, "raw": raw})
    return rows


def _fixture_meta(rows: List[dict]) -> Dict[str, Dict[str, object]]:
    """Home/away/ko per fixture_id, parsed from the raw OddsAPI payloads."""
    meta: Dict[str, Dict[str, object]] = {}
    for r in rows:
        raw = r.get("raw")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                continue
        fid = r.get("match_id")
        if fid and fid not in meta:
            meta[fid] = {"home": raw.get("home_team"), "away": raw.get("away_team"),
                         "ko_utc": raw.get("commence_time")}
    return meta


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the Market Intelligence feed.")
    ap.add_argument("--odds-db", default=_DEF_ODDS_DB)
    ap.add_argument("--out", default=_DEF_OUT)
    ap.add_argument("--lookback-hours", type=float, default=12.0,
                    help="Only consider captures newer than this.")
    ap.add_argument("--horizon-hours", type=float, default=72.0,
                    help="Only include fixtures kicking off within this window (and not >3h past).")
    ap.add_argument("--generated", default=None, help="Override generated timestamp (ISO8601 UTC).")
    args = ap.parse_args(argv)

    now = _parse(args.generated) or _now()
    since = _iso_z(now - timedelta(hours=args.lookback_hours))

    rows: List[dict] = []
    if os.path.exists(args.odds_db):
        con = _connect_ro(args.odds_db)
        try:
            rows = _load_rows(con, since)
        finally:
            con.close()

    meta = _fixture_meta(rows)
    # keep fixtures kicking off within [now-3h, now+horizon]
    lo, hi = now - timedelta(hours=3), now + timedelta(hours=args.horizon_hours)
    keep = set()
    for fid, m in meta.items():
        ko = _parse(m.get("ko_utc"))
        if ko is None or (lo <= ko <= hi):
            keep.add(fid)
    rows = [r for r in rows if r.get("match_id") in keep]

    snaps = from_oddsapi_rows(rows)
    feed = build_feed(snaps, now_utc=_iso_z(now),
                      fixture_meta={k: meta[k] for k in keep if k in meta})
    _write_atomic(args.out, feed)
    print("market_intel: %d fixtures, %d markets, %d snapshots -> %s"
          % (feed["meta"]["n_fixtures"], feed["meta"]["n_markets"], len(snaps), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
