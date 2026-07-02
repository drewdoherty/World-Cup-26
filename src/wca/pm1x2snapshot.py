"""Polymarket 1X2 snapshot — make Polymarket a first-class *venue* in the
Model-vs-Venue benchmark.

The benchmark (``venuesbench`` / ``venuesdata``) ranks every venue by the
distance between its de-vigged H/D/A and the model's fair 1X2. It reads venue
quotes from ``odds_snapshots`` (``market='h2h'``, keyed by ``bookmaker_key``
inside ``raw``). Until now Polymarket had **no** captured 1X2 price series there
— only dry-run orders existed — so the benchmark's Polymarket panel stayed
``COLLECTING``.

This module closes that gap. It takes the match-winner share prices already
produced by :func:`wca.data.polymarket_odds.get_odds` (which emit exactly the
``bookmaker_key='polymarket'`` / ``outcome_name`` / ``decimal_odds`` shape the
benchmark consumes), resolves each Polymarket fixture to the SAME ``match_id``
the bookmaker rows and the model ledger use (bridged by canonical team pair),
and appends them to ``odds_snapshots``. From then on ``venuesbench`` picks
Polymarket up automatically as a matched-time H/D/A venue — no benchmark change
needed.

Design notes
------------
* **Network-free core.** All functions here are pure / DB-only and unit-tested;
  the live Polymarket fetch lives in ``scripts/wca_pm_1x2_snapshot.py``.
* **No-lookahead safe.** Rows are stamped with the real capture time, so the
  benchmark's at-or-before matcher treats them like any other quote.
* **Honest, never faked.** A Polymarket fixture with no matching ``match_id``
  (no bookmaker/model coverage for that game) is returned for audit, never
  force-inserted. An incomplete H/D/A partition is dropped downstream by
  ``per_book_quotes_from_rows`` exactly like any incomplete book.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from wca.venuesdata import canon_team, pair_key

#: Polymarket charges a fee on net winnings; surfaced in ``raw`` for the
#: executable-price layer (mirrors how the exchange commission is handled).
PM_FEE = 0.02

PMRow = Dict[str, object]


def build_match_index(con: sqlite3.Connection) -> Dict[frozenset, str]:
    """Map each fixture's canonical team pair -> its ``odds_snapshots`` match_id.

    Built from existing ``h2h`` rows (any bookmaker), so a Polymarket fixture can
    be stamped with the SAME ``match_id`` the model ledger / books already use.
    The newest match_id wins on the (rare) chance a pair repeats.
    """
    idx: Dict[frozenset, str] = {}
    cur = con.execute(
        "SELECT match_id, ts_utc, "
        "json_extract(raw,'$.home_team'), json_extract(raw,'$.away_team') "
        "FROM odds_snapshots WHERE market='h2h'"
    )
    seen_ts: Dict[frozenset, str] = {}
    for match_id, ts, home, away in cur:
        if not match_id or not home or not away:
            continue
        key = pair_key(home, away)
        if key not in seen_ts or (ts or "") >= seen_ts[key]:
            idx[key] = match_id
            seen_ts[key] = ts or ""
    return idx


def _outcome_selection(outcome_name: str, home: str, away: str) -> Optional[str]:
    """Canonical h2h selection token the benchmark expects (home/away team name
    or 'Draw'). Returns None for an unrecognised outcome."""
    if outcome_name is None:
        return None
    o = canon_team(outcome_name)
    if o in ("draw", "tie", "the draw"):
        return "Draw"
    if o == canon_team(home):
        return home
    if o == canon_team(away):
        return away
    # Some PM titles already store the literal team string — accept it verbatim.
    return outcome_name


def pm_rows_to_snapshot_rows(
    pm_rows: Sequence[PMRow],
    match_index: Dict[frozenset, str],
    ts_utc: str,
) -> Tuple[List[Tuple], List[PMRow]]:
    """Convert Polymarket h2h rows into ``odds_snapshots`` insert tuples.

    Returns ``(insert_rows, unmatched)``. ``insert_rows`` are
    ``(ts_utc, source, match_id, market, selection, decimal_odds, raw)`` tuples;
    ``unmatched`` are the PM rows whose fixture had no ``match_id`` (audited,
    never inserted). Rows with a non-positive / missing price are skipped.
    """
    insert_rows: List[Tuple] = []
    unmatched: List[PMRow] = []
    for r in pm_rows:
        home = str(r.get("home_team") or "")
        away = str(r.get("away_team") or "")
        odds = r.get("decimal_odds")
        outcome = r.get("outcome_name")
        if not home or not away or odds is None:
            continue
        try:
            odds_f = float(odds)
        except (TypeError, ValueError):
            continue
        if odds_f <= 1.0:
            continue
        key = pair_key(home, away)
        match_id = match_index.get(key)
        if match_id is None:
            unmatched.append(r)
            continue
        selection = _outcome_selection(str(outcome), home, away)
        if selection is None:
            continue
        raw = {
            "bookmaker_key": "polymarket",
            "bookmaker_title": "Polymarket",
            "outcome_name": selection,
            "home_team": home,
            "away_team": away,
            "pm_implied": round(1.0 / odds_f, 6),
            "fee": PM_FEE,
            "event_id": r.get("event_id"),
        }
        insert_rows.append(
            (ts_utc, "polymarket", match_id, "h2h", selection, odds_f, json.dumps(raw))
        )
    return insert_rows, unmatched


def insert_snapshot_rows(con: sqlite3.Connection, rows: Sequence[Tuple]) -> int:
    """Append snapshot rows to ``odds_snapshots`` (append-only). Returns count."""
    if not rows:
        return 0
    con.executemany(
        "INSERT INTO odds_snapshots(ts_utc, source, match_id, market, selection, "
        "decimal_odds, raw) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    return len(rows)


def snapshot(
    con: sqlite3.Connection, pm_rows: Sequence[PMRow], ts_utc: str
) -> Dict[str, object]:
    """Full pipeline given already-fetched PM rows: resolve match_ids, insert,
    return a small honest summary (inserted / unmatched fixtures)."""
    index = build_match_index(con)
    insert_rows, unmatched = pm_rows_to_snapshot_rows(pm_rows, index, ts_utc)
    n = insert_snapshot_rows(con, insert_rows)
    unmatched_fixtures = sorted(
        {(str(r.get("home_team")), str(r.get("away_team"))) for r in unmatched}
    )
    return {
        "ts_utc": ts_utc,
        "inserted": n,
        "n_fixtures_indexed": len(index),
        "n_unmatched_legs": len(unmatched),
        "unmatched_fixtures": [" vs ".join(f) for f in unmatched_fixtures],
    }


# --------------------------------------------------------------------------- #
# Freshness / silent-stall detection.
#
# 2026-07-02 postmortem: PR #109 shipped the capture pipeline above and a CLI
# (``scripts/wca_pm_1x2_snapshot.py``) but never wired it into any scheduler
# (no ``deploy/`` entry, no CI workflow) — a full day passed with the docstring
# telling operators to "run on a schedule" while nothing did. The CLI also
# degrades silently by design ("no rows fetched... nothing written", exit 0),
# so even once scheduled a persistent zero-match/zero-fetch state would stay
# invisible. These two gaps only close together: scheduling makes the job run;
# this freshness check makes a run that keeps inserting nothing loud instead
# of quiet.
# --------------------------------------------------------------------------- #


def _parse_utc(ts: str) -> datetime:
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def seconds_since_last_snapshot(
    con: sqlite3.Connection, now_iso: Optional[str] = None
) -> Optional[float]:
    """Seconds since the most recent captured Polymarket ``h2h`` snapshot.

    Returns ``None`` if the source has NEVER written a row — distinct from
    merely stale. A ``None`` result means the job either isn't scheduled or
    has matched zero fixtures on every run; a large-but-finite result means it
    ran, matched before, and has since gone quiet (fetch outage, PM API
    change, or the tournament simply has no live h2h market right now).
    """
    row = con.execute(
        "SELECT MAX(ts_utc) FROM odds_snapshots WHERE source='polymarket'"
    ).fetchone()
    last = row[0] if row else None
    if not last:
        return None
    now_dt = _parse_utc(now_iso) if now_iso else datetime.now(timezone.utc)
    return max(0.0, (now_dt - _parse_utc(last)).total_seconds())


def should_alert_stale(
    age_secs: Optional[float],
    last_alert_age_secs: Optional[float],
    threshold_secs: float,
) -> bool:
    """Whether a freshness alert should fire now (debounced, never spammy).

    * ``age_secs is None`` (never captured a single row) -> always alert.
    * Below ``threshold_secs`` -> never alert.
    * Above threshold -> alert once, then only again once staleness has grown
      by another full threshold (4h, then 8h, then 12h... not every cycle).
    """
    if age_secs is None:
        return True
    if age_secs < threshold_secs:
        return False
    if last_alert_age_secs is None:
        return True
    return age_secs >= last_alert_age_secs + threshold_secs
