#!/usr/bin/env python3
"""One-shot, idempotent backfill of bet ``account`` / ``source`` dimensions.

Run once against ``data/wca.db`` to:

1. Migrate the ``bets`` table (add ``account`` / ``source`` columns if absent).
2. Apply the exact, hand-curated backfill mapping for the 16 legacy bets
   (account "1" for all of them; source per the per-id table below).
3. Insert the three account-2 Betfair Sportsbook bets that currently only exist
   in ``sb_offers`` (skipped if an identical selection+account row already
   exists, so re-running is safe).
4. Print a before/after table.

Idempotent: re-running re-applies the same UPDATEs (no-ops) and skips the
account-2 inserts if they are already present.

Usage::

    ./.venv/bin/python scripts/wca_backfill_accounts.py [db_path]

Defaults to ``data/wca.db``.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import sqlite3  # noqa: E402

from wca.ledger import store  # noqa: E402
from wca.venues import canon_platform  # noqa: E402


_DEFAULT_DB = os.path.join(_REPO_ROOT, "data", "wca.db")

# Exact source mapping by bet id. account is "1" for every legacy bet.
_SOURCE_BY_ID = {
    1: "model",
    2: "model",
    3: "offer",   # Virgin boost
    4: "offer",   # Paddy SuperBoost
    5: "offer",   # free GBP2 acca SNR
    6: "model",
    7: "model",
    8: "model",
    9: "model",
    10: "model",
    11: "offer",  # Golden Boot promo qualifier
    12: "model",  # EV scanner
    13: "punt",   # in-play call, explicitly non-model
    14: "model",  # advancement + news
    15: "punt",
    16: "model",  # CTX override: notes say "screenshot ingest; conf 0.97" => scanner/model-driven, not directional
}

# Account-2 Betfair Sportsbook bets (currently only in sb_offers).
_ACCOUNT2_BETS = [
    dict(
        ts_utc="2026-06-12T12:00:00", match_id="ACC2_CAN",
        match_desc="Canada (qualifier)", market="MATCH", selection="Canada",
        platform="betfair_sportsbook", decimal_odds=1.88, stake=10.0,
        notes="Account 2 dead qualifier - below min odds; KO 2026-06-12T19:00Z",
        account="2", source="offer",
    ),
    dict(
        ts_utc="2026-06-12T12:00:00", match_id="ACC2_USA",
        match_desc="USA (qualifier)", market="MATCH", selection="USA",
        platform="betfair_sportsbook", decimal_odds=2.20, stake=10.0,
        notes="Account 2 qualifier; laid GBP 9 @2.18 on exchange; "
              "KO 2026-06-13T01:00Z",
        account="2", source="offer",
    ),
    dict(
        ts_utc="2026-06-12T12:00:00", match_id="ACC2_TREBLE",
        match_desc="Treble: Netherlands + Brazil + Paraguay",
        market="ACCA", selection="Netherlands/Brazil/Paraguay all win",
        platform="betfair_sportsbook", decimal_odds=12.87, stake=2.0,
        notes="FREE GBP2 SNR treble (Netherlands 2.0 + Brazil 1.65 + Paraguay "
              "3.9); max-loss treats stake as 0; settles Jun 13-14",
        account="2", source="offer",
    ),
]


def _columns(conn: sqlite3.Connection) -> list:
    return [r[1] for r in conn.execute("PRAGMA table_info(bets)")]


def _snapshot(conn: sqlite3.Connection) -> list:
    cols = _columns(conn)
    has = lambda c: c in cols  # noqa: E731
    sel = "id, match_desc, selection, platform, stake, status"
    if has("account"):
        sel += ", account"
    if has("source"):
        sel += ", source"
    return list(conn.execute("SELECT %s FROM bets ORDER BY id" % sel))


def _print_table(title: str, rows: list, conn: sqlite3.Connection) -> None:
    cols = _columns(conn)
    has_acct = "account" in cols
    has_src = "source" in cols
    print("\n=== %s ===" % title)
    header = "%3s  %-34s  %-26s  %-18s  %6s  %-7s" % (
        "id", "match", "selection", "platform", "stake", "status")
    if has_acct:
        header += "  acct"
    if has_src:
        header += "  source"
    print(header)
    for r in rows:
        line = "%3s  %-34.34s  %-26.26s  %-18.18s  %6.2f  %-7s" % (
            r["id"], r["match_desc"], r["selection"], r["platform"],
            float(r["stake"]), r["status"])
        if has_acct:
            line += "  %-4s" % (r["account"] if "account" in r.keys() else "")
        if has_src:
            line += "  %-6s" % (r["source"] if "source" in r.keys() else "")
        print(line)


def backfill(db_path: str = _DEFAULT_DB) -> None:
    # Ensure schema + columns exist.
    store.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        store._ensure_account_source_columns(conn)
        conn.commit()

        before = _snapshot(conn)
        _print_table("BEFORE", before, conn)

        # Apply the per-id mapping (account "1" for all legacy bets). We must
        # NOT touch rows that are already tagged account "2" — those are the
        # account-2 Betfair inserts from a previous run, and their ids may
        # collide with the legacy-id mapping. Skipping them keeps the backfill
        # idempotent (a second run leaves account-2 rows intact).
        for r in before:
            bet_id = r["id"]
            if "account" in r.keys() and str(r["account"]) == "2":
                continue
            source = _SOURCE_BY_ID.get(bet_id)
            if source is None:
                continue
            conn.execute(
                "UPDATE bets SET account = ?, source = ? WHERE id = ?",
                ("1", source, bet_id),
            )
        conn.commit()
    finally:
        conn.close()

    # Insert account-2 Betfair bets via record_bet (skip duplicates).
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for bet in _ACCOUNT2_BETS:
            dup = conn.execute(
                "SELECT 1 FROM bets WHERE selection = ? AND account = ? "
                "AND platform = ? LIMIT 1",
                (bet["selection"], bet["account"], canon_platform(bet["platform"])),
            ).fetchone()
            if dup is not None:
                continue
            store.record_bet(db_path=db_path, **bet)
    finally:
        conn.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _print_table("AFTER", _snapshot(conn), conn)
    finally:
        conn.close()


def main(argv: list) -> int:
    db_path = argv[1] if len(argv) > 1 else _DEFAULT_DB
    backfill(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
