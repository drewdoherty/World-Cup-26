"""Market-snapshot store — the historical market-intelligence database.

Append-only, change-gated: a new row is written only when a price has moved
materially OR a max-staleness interval has elapsed (so "no move" is still
timestamped) — this keeps the history compact without losing signal. Pure /
network-free: collectors hand normalised rows here, so storage is unit-testable.

Two tables: ``market_snapshots`` (one row per venue×market×selection×time) and
``market_metrics`` (cross-venue derived metrics per market×time, written by the
derived-metrics builder — schema created here so downstream stays simple).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence

#: Columns of market_snapshots, in order.
SNAPSHOT_COLUMNS = (
    "ts_utc", "fetched_at", "fixture_id", "ko_utc", "mins_to_ko",
    "source", "venue", "venue_kind", "market_type", "selection", "line",
    "decimal_odds", "implied_raw", "implied_devig", "liquidity", "raw", "api_meta",
)


@dataclass
class MarketSnapshot:
    ts_utc: str
    source: str
    venue: str
    market_type: str
    selection: str
    decimal_odds: float
    implied_raw: float
    fixture_id: Optional[str] = None
    ko_utc: Optional[str] = None
    mins_to_ko: Optional[float] = None
    venue_kind: Optional[str] = None
    line: Optional[float] = None
    implied_devig: Optional[float] = None
    liquidity: Optional[float] = None
    fetched_at: Optional[str] = None
    raw: Optional[object] = None
    api_meta: Optional[object] = None

    def key(self) -> str:
        """Identity of the priced thing across time (for change-gating)."""
        return "|".join(str(x) for x in
                        (self.fixture_id, self.market_type, self.selection, self.line, self.venue))


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS market_snapshots ("
        " ts_utc TEXT NOT NULL, fetched_at TEXT, fixture_id TEXT, ko_utc TEXT, mins_to_ko REAL,"
        " source TEXT NOT NULL, venue TEXT NOT NULL, venue_kind TEXT,"
        " market_type TEXT NOT NULL, selection TEXT NOT NULL, line REAL,"
        " decimal_odds REAL, implied_raw REAL, implied_devig REAL, liquidity REAL,"
        " raw TEXT, api_meta TEXT)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_ms_key "
        "ON market_snapshots(fixture_id, market_type, selection, venue, ts_utc)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS market_metrics ("
        " ts_utc TEXT NOT NULL, fixture_id TEXT, market_type TEXT, selection TEXT, line REAL,"
        " best_odds REAL, worst_odds REAL, avg_odds REAL, median_odds REAL,"
        " implied_range REAL, spread REAL, pct_improvement REAL, stdev REAL,"
        " consensus_prob REAL, median_prob REAL, vig_adj_consensus REAL,"
        " model_prob REAL, ev_vs_model REAL, kelly_stake REAL, clv REAL,"
        " line_move REAL, rolling_vol REAL, largest_disagreement REAL, secs_since_move REAL,"
        " best_venue TEXT, worst_venue TEXT)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_mm_key ON market_metrics(fixture_id, market_type, selection, ts_utc)"
    )
    con.commit()


def _last_implied(con: sqlite3.Connection, s: MarketSnapshot):
    row = con.execute(
        "SELECT implied_raw, ts_utc FROM market_snapshots"
        " WHERE fixture_id IS ? AND market_type=? AND selection=? AND line IS ? AND venue=?"
        " ORDER BY ts_utc DESC LIMIT 1",
        (s.fixture_id, s.market_type, s.selection, s.line, s.venue),
    ).fetchone()
    return row  # (implied_raw, ts_utc) or None


def _parse_secs(a: str, b: str) -> Optional[float]:
    from datetime import datetime, timezone
    def p(t):
        try:
            d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    da, db = p(a), p(b)
    return (da - db).total_seconds() if (da and db) else None


def append_snapshots(con: sqlite3.Connection, rows: Sequence[MarketSnapshot], *,
                     eps: float = 0.003, max_staleness_s: float = 3600.0) -> int:
    """Append snapshots, skipping rows whose implied prob hasn't moved >= ``eps``
    since the last stored value for the same key — UNLESS ``max_staleness_s`` has
    elapsed (so a flat price is still re-stamped periodically). Returns # written.
    """
    ensure_schema(con)
    written = 0
    for s in rows:
        try:
            ir = float(s.implied_raw)
        except (TypeError, ValueError):
            continue
        prev = _last_implied(con, s)
        if prev is not None:
            last_ir, last_ts = prev
            moved = abs(ir - float(last_ir)) >= eps
            stale = True
            secs = _parse_secs(s.ts_utc, last_ts)
            if secs is not None:
                stale = secs >= max_staleness_s
            if not moved and not stale:
                continue
        raw = s.raw if (s.raw is None or isinstance(s.raw, str)) else json.dumps(s.raw, sort_keys=True)
        meta = s.api_meta if (s.api_meta is None or isinstance(s.api_meta, str)) else json.dumps(s.api_meta, sort_keys=True)
        con.execute(
            "INSERT INTO market_snapshots (%s) VALUES (%s)"
            % (", ".join(SNAPSHOT_COLUMNS), ", ".join(["?"] * len(SNAPSHOT_COLUMNS))),
            (s.ts_utc, s.fetched_at, s.fixture_id, s.ko_utc, s.mins_to_ko,
             s.source, s.venue, s.venue_kind, s.market_type, s.selection, s.line,
             s.decimal_odds, ir, s.implied_devig, s.liquidity, raw, meta),
        )
        written += 1
    con.commit()
    return written


def latest_per_selection(con: sqlite3.Connection, fixture_id: str, market_type: str,
                         line: Optional[float] = None) -> Dict[str, List[Dict[str, object]]]:
    """Most-recent snapshot per (selection, venue) for one market — the input the
    cross-venue spread/consensus metrics consume. Returns {selection: [rows]}.
    """
    cur = con.execute(
        "SELECT ts_utc, venue, venue_kind, selection, line, decimal_odds, implied_raw, implied_devig, liquidity"
        " FROM market_snapshots WHERE fixture_id=? AND market_type=?"
        + (" AND line IS ?" if line is None else " AND line=?")
        + " ORDER BY ts_utc",
        (fixture_id, market_type) + ((None,) if line is None else (line,)),
    )
    by_sel_venue: Dict[tuple, Dict[str, object]] = {}
    for ts, venue, kind, sel, ln, dec, ir, idv, liq in cur:
        by_sel_venue[(sel, venue)] = {
            "ts_utc": ts, "venue": venue, "venue_kind": kind, "selection": sel, "line": ln,
            "decimal_odds": dec, "implied_raw": ir, "implied_devig": idv, "liquidity": liq,
        }
    out: Dict[str, List[Dict[str, object]]] = {}
    for (sel, _venue), row in by_sel_venue.items():
        out.setdefault(sel, []).append(row)
    return out
