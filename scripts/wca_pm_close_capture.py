#!/usr/bin/env python
"""Capture Polymarket advancement closes for CLV stamping (MacBook-side).

WHY this script exists (network topology)
------------------------------------------
Polymarket's CLOB is reachable ONLY from this MacBook (VPN); the Mac mini
(production, canonical ``data/wca.db`` ledger) is PM-blind. CLV stamping
therefore has to be a two-machine relay:

    MacBook (this script)                 -> git ->      Mac mini
    reads pm_orderflow.db + CLOB,                 reads data/pm_closes.json,
    writes data/pm_closes.json                    stamps closing_odds/clv
                                                   onto the ledger
    (scripts/wca_pm_close_capture.py)             (scripts/wca_pm_stamp_clv.py)

This mirrors the existing ``scripts/wca_orderflow_refresh.sh`` pattern: a
MacBook job produces a small JSON artifact and commits it; nothing here ever
touches ``data/wca.db`` (that file does not even exist on this machine in the
common case, and the code makes no attempt to open one).

What it does
------------
For every advancement/futures PM market found in ``data/pm_orderflow.db``
(read-only; written by ``scripts/pm_orderflow_ingest.py --discover-only``)
whose underlying team has a deciding-match kickoff in
``data/processed/wc2026_results.json``, capture the CLOB close (top-of-book
mid, falling back to the last price-history point) at or after that kickoff,
and append it to ``data/pm_closes.json`` — the artifact the mini-side stamper
(``scripts/wca_pm_stamp_clv.py``) consumes. Appends are idempotent: one row
per ``(token_id, close_ts_utc)``; a rerun with nothing new to add is a no-op
(the file's mtime does not change). See :data:`_TEAM_CATEGORIES` for the
in-scope market categories (excludes ``match_1x2``, the single-match
moneyline — that market already has its own dedicated close-capture path,
``wca.closecapture``, driven off sportsbook ``odds_snapshots`` consensus
rather than the CLOB).

``data/pm_orderflow.db`` needs at least one market-discovery sweep before this
script has anything to read: ``PYTHONPATH=src python
scripts/pm_orderflow_ingest.py --discover-only`` (fast, no trade-history
paging; safe to rerun).

Two modes
---------
* **Live** (default): captures the CURRENT top-of-book for markets whose
  deciding match has already kicked off, timestamping the close as "now".
  Intended to run once per matchday (cron/launchd on the MacBook — see
  ``docs/HANDOFF_2026-07-03.md`` for why this can't run on the mini).
* ``--backfill``: reconstructs closes for PAST deciding-match kickoffs from
  :func:`wca.data.pm_clob_history.price_history` (the dense per-token series
  back to market inception) — the point in each token's history at or just
  after its team's last recorded kickoff.

Usage (manual, per matchday — NO launchd job ships with this change)
----------------------------------------------------------------------
This PR does not add a ``deploy/macbook/*.plist`` — unlike
``com.wca.positions``/``com.wca.feedpull`` (see ``deploy/macbook/README.md``
for that pattern), scheduling a recurring MacBook job needs a human decision
(cadence, VPN-up assumption) and is a follow-up. Until then, run by hand after
each matchday's fixtures have kicked off::

    # 1) Refresh market discovery (fast, no trade history; safe to rerun):
    PYTHONPATH=src python scripts/pm_orderflow_ingest.py --discover-only

    # 2) Capture today's newly-decided closes:
    PYTHONPATH=src python scripts/wca_pm_close_capture.py

    # 3) Commit + push the artifact so the mini's autopull delivers it:
    git add data/pm_closes.json && git commit -m "data: PM advancement closes" && git push

    # Reconstruct closes for everything played so far (safe to rerun):
    PYTHONPATH=src python scripts/wca_pm_close_capture.py --backfill

    # Point at explicit paths (e.g. testing against a copy):
    PYTHONPATH=src python scripts/wca_pm_close_capture.py --backfill \\
        --orderflow-db data/pm_orderflow.db --artifact data/pm_closes.json

Then, on the mini (after ``git pull``/autopull delivers the updated
artifact — no MacBook access needed, no network call made)::

    PYTHONPATH=src python scripts/wca_pm_stamp_clv.py

No secrets are read or printed; the CLOB endpoints hit here
(``clob.polymarket.com``) are public read-only market data, same as
``wca.data.pm_clob_history`` already uses elsewhere in the repo.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmclose  # noqa: E402
from wca.data import pm_clob_history as clob  # noqa: E402

_DEFAULT_ORDERFLOW_DB = "data/pm_orderflow.db"
_DEFAULT_RESULTS = "data/processed/wc2026_results.json"
_DEFAULT_ARTIFACT = "data/pm_closes.json"

# pm_markets.category values that price a team-level ADVANCEMENT/FUTURES
# outcome — the project's PRIMARY market (CLAUDE.md). Deliberately EXCLUDES
# "match_1x2" ("Will <Team> win on <date>?", a single-match moneyline — a
# team plays several of these, all with an unparseable/identical stage, so
# there is no safe team+stage join for them here; that market type already
# has its own dedicated close-capture path, wca.closecapture, driven off
# odds_snapshots rather than the CLOB). "other_future" is a grab-bag category
# (props, non-team markets like "will another team win the group") skipped
# entirely.
_TEAM_CATEGORIES = frozenset(
    {
        "advancement_r32",
        "advancement_r16",
        "advancement_qf",
        "advancement_sf",
        "advancement_final",
        "group_winner",
        "winner",
    }
)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_team_markets(
    orderflow_db: str,
) -> List[Dict[str, Any]]:
    """Read team-keyed PM markets (condition_id, question, team, token_ids)."""
    if not os.path.exists(orderflow_db):
        return []
    con = sqlite3.connect(f"file:{orderflow_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT condition_id, category, question, team, token_ids, closed "
            "FROM pm_markets WHERE team IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()

    out = []
    for condition_id, category, question, team, token_ids_raw, closed in rows:
        if category not in _TEAM_CATEGORIES:
            continue
        try:
            token_ids = json.loads(token_ids_raw) if token_ids_raw else []
        except (ValueError, TypeError):
            token_ids = []
        if not token_ids:
            continue
        out.append(
            {
                "condition_id": condition_id,
                "category": category,
                "question": question,
                "team": team,
                "token_ids": token_ids,
                "closed": bool(closed),
            }
        )
    return out


def _yes_token(market: Dict[str, Any]) -> Optional[str]:
    """First token id — pm_markets stores ``["Yes", "No"]`` outcome order, so
    ``token_ids[0]`` is the YES share whose mid is a direct win probability."""
    token_ids = market.get("token_ids") or []
    return str(token_ids[0]) if token_ids else None


def capture_live(
    orderflow_db: str,
    results_path: str,
    now_utc: Optional[str] = None,
    *,
    top_of_book_fn=None,
    price_history_fn=None,
) -> List[Dict[str, Any]]:
    """Capture the CURRENT close for markets whose deciding match has kicked off.

    Prefers the live top-of-book mid; when a market has no resting book (an
    illiquid/near-resolved advancement market often doesn't), falls back to
    the LAST point in :func:`wca.data.pm_clob_history.price_history` — the
    most recent trade/mark, same fallback the ``--backfill`` mode uses.
    ``top_of_book_fn``/``price_history_fn`` are injectable for tests.
    """
    top_of_book_fn = top_of_book_fn or clob.top_of_book
    price_history_fn = price_history_fn or clob.price_history
    now = now_utc or _now_utc_iso()
    now_bare = now.replace("Z", "").split("+")[0]

    markets = _load_team_markets(orderflow_db)
    last_kickoff = pmclose.load_team_last_kickoff(results_path)
    captured_utc = _now_utc_iso()

    out: List[Dict[str, Any]] = []
    for market in markets:
        team_c = pmclose.canon(market["team"])
        kickoff = last_kickoff.get(team_c)
        if not kickoff:
            continue
        kickoff_bare = kickoff.replace("Z", "").split("+")[0]
        if kickoff_bare > now_bare:
            continue  # deciding match hasn't kicked off yet
        token_id = _yes_token(market)
        if not token_id:
            continue
        book = top_of_book_fn(token_id)
        if book and book.get("mid") is not None:
            out.append(
                {
                    "condition_id": market["condition_id"],
                    "token_id": token_id,
                    "question": market["question"],
                    "close_ts_utc": kickoff,
                    "mid": book.get("mid"),
                    "best_bid": book.get("bid"),
                    "best_ask": book.get("ask"),
                    "source": "top_of_book",
                    "captured_utc": captured_utc,
                }
            )
            continue
        # No resting book — fall back to the last known trade/mark.
        history = price_history_fn(token_id, interval="max", fidelity=60)
        if not history:
            continue
        _, last_price = history[-1]
        out.append(
            {
                "condition_id": market["condition_id"],
                "token_id": token_id,
                "question": market["question"],
                "close_ts_utc": kickoff,
                "mid": last_price,
                "best_bid": None,
                "best_ask": None,
                "source": "price_history_last_trade",
                "captured_utc": captured_utc,
            }
        )
    return out


def capture_backfill(
    orderflow_db: str,
    results_path: str,
    *,
    price_history_fn=None,
) -> List[Dict[str, Any]]:
    """Reconstruct closes for PAST deciding-match kickoffs from price history.

    For each team market, finds the first price-history point at or after
    the team's last recorded kickoff (falls back to the LAST point in the
    whole series — a market that closed/resolved before more history was
    recorded — when nothing is at/after kickoff). ``price_history_fn`` is
    injectable for tests (defaults to
    :func:`wca.data.pm_clob_history.price_history`).
    """
    price_history_fn = price_history_fn or clob.price_history
    markets = _load_team_markets(orderflow_db)
    last_kickoff = pmclose.load_team_last_kickoff(results_path)
    captured_utc = _now_utc_iso()

    out: List[Dict[str, Any]] = []
    for market in markets:
        team_c = pmclose.canon(market["team"])
        kickoff = last_kickoff.get(team_c)
        if not kickoff:
            continue
        token_id = _yes_token(market)
        if not token_id:
            continue
        history = price_history_fn(token_id, interval="max", fidelity=60)
        if not history:
            continue
        kickoff_dt = _parse_iso(kickoff)
        chosen = None
        for ts, price in history:
            if kickoff_dt is None or ts >= kickoff_dt:
                chosen = (ts, price)
                break
        if chosen is None:
            chosen = history[-1]  # market closed before the kickoff we have
        ts, price = chosen
        out.append(
            {
                "condition_id": market["condition_id"],
                "token_id": token_id,
                "question": market["question"],
                "close_ts_utc": kickoff,
                "mid": price,
                "best_bid": None,
                "best_ask": None,
                "source": "price_history_backfill",
                "captured_utc": captured_utc,
            }
        )
    return out


def _parse_iso(ts: str):
    try:
        cleaned = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture Polymarket advancement closes into data/pm_closes.json "
        "(MacBook-side; run near/after each matchday's kickoffs)."
    )
    parser.add_argument(
        "--orderflow-db", default=_DEFAULT_ORDERFLOW_DB,
        help="Read-only pm_orderflow.db path (default: data/pm_orderflow.db).",
    )
    parser.add_argument(
        "--results", default=_DEFAULT_RESULTS,
        help="Results JSON for deciding-match kickoffs "
        "(default: data/processed/wc2026_results.json).",
    )
    parser.add_argument(
        "--artifact", default=_DEFAULT_ARTIFACT,
        help="Output artifact path (default: data/pm_closes.json).",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Reconstruct closes for PAST deciding-match kickoffs from "
        "CLOB price-history instead of capturing the live top-of-book.",
    )
    parser.add_argument(
        "--now", default=None,
        help="Override 'now' for --live mode (ISO-8601 UTC); default current time.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be captured/appended without writing the artifact.",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.orderflow_db):
        print(
            "ERROR: pm_orderflow.db not found at %s (run pm_orderflow_ingest.py "
            "first, or pass --orderflow-db)" % args.orderflow_db,
            file=sys.stderr,
        )
        return 1

    if args.backfill:
        rows = capture_backfill(args.orderflow_db, args.results)
    else:
        rows = capture_live(args.orderflow_db, args.results, now_utc=args.now)

    if not rows:
        print("no closes captured (0 candidate markets had a kicked-off deciding match)")
        return 0

    if args.dry_run:
        for row in rows:
            print(
                "would capture: %s | %s | close %s | mid=%.4f (%s)"
                % (
                    row["condition_id"][:12],
                    row["question"],
                    row["close_ts_utc"],
                    row["mid"],
                    row["source"],
                )
            )
        print("dry-run: %d candidate close(s), artifact not written" % len(rows))
        return 0

    _, n_added = pmclose.append_closes(rows, args.artifact)
    print(
        "captured %d candidate close(s); %d new row(s) appended to %s (%d total now)"
        % (len(rows), n_added, args.artifact, len(pmclose.load_closes(args.artifact)))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
