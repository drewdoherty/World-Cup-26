#!/usr/bin/env python
"""Tiered Market-Intelligence collector (one-shot / daemon-friendly).

WHAT THIS PHASE DOES
--------------------
1. Loads the tiered polling config (``data/intel_polling.yml`` or defaults).
2. Discovers upcoming fixtures from the existing ``odds_snapshots`` store
   (READ-ONLY) — fixture_id + kickoff parsed from the captured OddsAPI payloads.
3. Asks the PURE planner (:mod:`wca.intel.poller`) which fixtures are *due* now,
   at what cadence, and which markets — applying the budget governor.
4. For each due fixture, reads the latest matching rows from ``odds_snapshots``
   (read-only), normalises them via the source adapters, and writes them with
   :func:`wca.intel.store.append_snapshots` (change-gated) into a target DB.
5. Prints a summary: fixtures considered/due, rows written, credits (if known).

HONEST SCOPE / LIMITS
---------------------
Live OddsAPI *fetching* on the planner's cadence is wired in a LATER phase. This
phase persists from the EXISTING capture store, proving the planner -> normalise
-> change-gated write path end-to-end against real data. The planner's cadence
is therefore advisory here (we still read & write whatever the store already
holds for due fixtures); ``last_polled_at`` is derived from the target DB so
re-runs exercise the change-gate. ``--remaining-credits`` lets you exercise the
budget governor without a live quota call.

SAFETY: the dev box is dry-run only. The target ``--db`` defaults to a SAFE dev
path (``data/dev.db``) and the writer NEVER targets ``data/wca.db`` unless you
pass ``--db .../data/wca.db`` explicitly. The odds source DB is always opened
read-only.

Usage
-----
    PYTHONPATH=src python scripts/wca_intel_collect.py \
        [--odds-db data/wca.db] [--db data/dev.db] [--config data/intel_polling.yml] \
        [--lookback-hours 12] [--horizon-hours 72] [--remaining-credits 1200] \
        [--generated 2026-06-28T07:00:00Z]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.intel import poller  # noqa: E402
from wca.intel.sources import OddsApiSource  # noqa: E402
from wca.intel.store import append_snapshots  # noqa: E402

_DEF_ODDS_DB = os.path.join(_ROOT, "data", "wca.db")
_DEF_TARGET_DB = os.path.join(_ROOT, "data", "dev.db")   # SAFE default (never wca.db)
_DEF_CONFIG = os.path.join(_ROOT, "data", "intel_polling.yml")

#: OddsAPI -> our canonical market_type (mirrors normalise._ODDSAPI_MARKET) for
#: filtering which captured markets a due fixture wants.
_ODDSAPI_MARKET = {"h2h": "moneyline", "totals": "ou", "btts": "btts", "h2h_lay": "moneyline_lay"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:%s?mode=ro&immutable=1" % os.path.abspath(db_path)
    return sqlite3.connect(uri, uri=True)


def _load_rows(con: sqlite3.Connection, since_iso: str) -> List[dict]:
    """Recent odds_snapshots rows as from_oddsapi_rows-shaped dicts."""
    cur = con.execute(
        "SELECT ts_utc, source, match_id, market, selection, decimal_odds, raw "
        "FROM odds_snapshots WHERE ts_utc >= ? ORDER BY ts_utc",
        (since_iso,),
    )
    return [{"ts_utc": ts, "source": src, "match_id": mid, "market": mkt,
             "selection": sel, "decimal_odds": dec, "raw": raw}
            for ts, src, mid, mkt, sel, dec, raw in cur]


def _fixture_meta(rows: List[dict]) -> Dict[str, Dict[str, object]]:
    """ko_utc per fixture_id from the raw OddsAPI payloads (latest wins)."""
    meta: Dict[str, Dict[str, object]] = {}
    for r in rows:
        raw = r.get("raw")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                continue
        fid = r.get("match_id")
        if fid and raw.get("commence_time"):
            meta[fid] = {"ko_utc": raw.get("commence_time")}
    return meta


def _last_polled_from_target(db_path: str) -> Dict[str, datetime]:
    """Most-recent fetched_at/ts per fixture already in the TARGET store, so
    re-runs respect cadence & exercise the change-gate. Empty if DB absent."""
    out: Dict[str, datetime] = {}
    if not os.path.exists(db_path):
        return out
    try:
        con = _connect_ro(db_path)
    except sqlite3.OperationalError:
        return out
    try:
        try:
            cur = con.execute(
                "SELECT fixture_id, MAX(COALESCE(fetched_at, ts_utc)) "
                "FROM market_snapshots GROUP BY fixture_id"
            )
        except sqlite3.OperationalError:
            return out          # table not created yet
        for fid, ts in cur:
            dt = _parse(ts)
            if fid and dt:
                out[fid] = dt
    finally:
        con.close()
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--odds-db", default=_DEF_ODDS_DB,
                    help="Source odds store (opened READ-ONLY). Default: data/wca.db")
    ap.add_argument("--db", default=_DEF_TARGET_DB,
                    help="Target market-intel DB to write into. Default: data/dev.db "
                         "(SAFE; never data/wca.db unless you pass it explicitly).")
    ap.add_argument("--config", default=_DEF_CONFIG, help="Polling config YAML.")
    ap.add_argument("--lookback-hours", type=float, default=12.0,
                    help="Only consider captures newer than this.")
    ap.add_argument("--horizon-hours", type=float, default=72.0,
                    help="Only include fixtures kicking off within this window.")
    ap.add_argument("--remaining-credits", type=float, default=None,
                    help="OddsAPI credit balance to feed the budget governor "
                         "(omit = unknown, no degradation). Live quota wired later.")
    ap.add_argument("--generated", default=None, help="Override 'now' (ISO8601 UTC).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Plan & report only; do not write any snapshots.")
    args = ap.parse_args(argv)

    now = _parse(args.generated) or _now()
    since = _iso_z(now - timedelta(hours=args.lookback_hours))
    cfg = poller.load_polling_config(args.config)

    # 1) Discover fixtures + recent rows from the read-only odds store.
    rows: List[dict] = []
    if os.path.exists(args.odds_db):
        con = _connect_ro(args.odds_db)
        try:
            rows = _load_rows(con, since)
        finally:
            con.close()
    meta = _fixture_meta(rows)

    hi = now + timedelta(hours=args.horizon_hours)
    fixtures = []
    for fid, m in meta.items():
        ko = _parse(m.get("ko_utc"))
        if ko is None or (now - timedelta(hours=3) <= ko <= hi):
            fixtures.append(poller.Fixture(fixture_id=fid, ko_utc=m.get("ko_utc")))

    # 2) Ask the planner what is due.
    last_polled = _last_polled_from_target(args.db)
    src = OddsApiSource()
    plans = poller.plan_polls(
        fixtures, now=now, last_polled_at=last_polled, config=cfg,
        remaining_credits=args.remaining_credits,
        available_markets=src.supported_markets,
    )
    due = {p.fixture_id: p for p in plans if p.due}

    # 3) For due fixtures, persist the latest matching captured rows (change-gated).
    written = 0
    if due and not args.dry_run:
        target = sqlite3.connect(args.db)
        try:
            for fid, plan in due.items():
                wanted = set(plan.markets)
                sub = [r for r in rows
                       if r.get("match_id") == fid
                       and _ODDSAPI_MARKET.get(r.get("market", ""), r.get("market")) in wanted]
                if not sub:
                    continue
                snaps = src.to_snapshots(sub)
                for s in snaps:
                    if s.fetched_at is None:
                        s.fetched_at = _iso_z(now)
                written += append_snapshots(target, snaps)
        finally:
            target.close()

    # 4) Summary.
    n_due = len(due)
    print("intel_collect: now=%s  fixtures=%d  due=%d  rows_written=%d  target=%s%s"
          % (_iso_z(now), len(fixtures), n_due, written, args.db,
             "  (dry-run)" if args.dry_run else ""))
    if args.remaining_credits is not None:
        print("  credits_remaining=%.0f  cost/full-fetch≈%d credits"
              % (args.remaining_credits,
                 src.cost_estimate(len(src.supported_markets), cfg.budget.regions)))
    for p in plans:
        flag = "DUE " if p.due else "skip"
        print("  [%s] %s  ko=%smin  markets=%s  (%s)"
              % (flag, p.fixture_id, p.mins_to_ko,
                 ",".join(p.markets) if p.markets else "-", p.reason))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
