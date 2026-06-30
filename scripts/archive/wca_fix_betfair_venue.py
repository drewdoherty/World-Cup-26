#!/usr/bin/env python
"""One-shot migration: rename bare 'Betfair' platform rows → 'Betfair Sportsbook'.

Hard rule (venues.py, 2026-06-24): bare 'Betfair' is sportsbook, not exchange.
Exchange bets must carry 'Betfair Exchange UK' / betfair_ex_uk explicitly.

Run DRY-RUN first (default), then --apply on the mini to commit the change.

Usage:
    python scripts/wca_fix_betfair_venue.py              # dry-run
    python scripts/wca_fix_betfair_venue.py --apply      # write to DB
    python scripts/wca_fix_betfair_venue.py --db data/wca.db --apply

Also ensures any 'Betfair' bets without account='2' are set to account='1'.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

_DEFAULT_DB = "data/wca.db"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=_DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="Write changes (default: dry-run only)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Rows to migrate: platform='Betfair' (bare — not 'Betfair Sportsbook' or
    # 'Betfair Exchange UK' which are already correctly labelled).
    rows = conn.execute(
        "SELECT id, platform, account, status, match_desc FROM bets "
        "WHERE platform = 'Betfair' ORDER BY id"
    ).fetchall()

    if not rows:
        print("No bare 'Betfair' rows found — nothing to do.")
        conn.close()
        return

    print("%s — %d row(s) to rename 'Betfair' → 'Betfair Sportsbook':"
          % ("APPLY" if args.apply else "DRY-RUN", len(rows)))
    for r in rows:
        acct_fix = " [account: '%s' → '1']" % r["account"] if r["account"] != "1" else ""
        print("  #%-4d  %-12s  %s%s" % (
            r["id"], r["status"], r["match_desc"][:50], acct_fix))

    if not args.apply:
        print("\nRe-run with --apply to commit. The mini DB (data/wca.db) is canonical.")
        conn.close()
        return

    # Apply: rename + fix account to '1' for non-A2 rows.
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))

    # Rename platform
    conn.execute(
        "UPDATE bets SET platform = 'Betfair Sportsbook' "
        "WHERE id IN (%s) AND platform = 'Betfair'" % placeholders,
        ids,
    )
    # Set account='1' for any that aren't A2 (safety: don't overwrite explicit A2)
    conn.execute(
        "UPDATE bets SET account = '1' "
        "WHERE id IN (%s) AND account != '2'" % placeholders,
        ids,
    )
    conn.commit()
    print("\nDone. %d rows updated." % len(ids))
    conn.close()


if __name__ == "__main__":
    main()
