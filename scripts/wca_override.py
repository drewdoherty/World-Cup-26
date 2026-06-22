#!/usr/bin/env python3
"""Manually overwrite fields on a single bet in the ledger (source of truth).

Use this when the automated grader can't settle/fix a bet on its own — free
bets, bet-builders, accumulators, player props, or metadata gaps (blank match,
"Unknown" venue, wrong account/source). It sets the fields you pass AND stamps
``manual_override`` with a reason, so ``wca_ledger_audit.py`` and the auto-graders
leave the row alone afterwards (the manual correction is never clobbered).

Dry-run by default; pass --apply to write. Before writing it snapshots the whole
``bets`` table to data/backups/bets_snapshot_<ts>.csv (tiny — the heavy
odds_snapshots history is left out), so any field is restorable.

Examples
--------
  # correct metadata + settle a free bet to exactly £0 (free bet lost -> 0)
  python scripts/wca_override.py --bet-id 102 --match-desc "USA vs Australia" \
      --platform "Betfair Exchange UK" --source hedge --account 2 \
      --status lost --settled-pl 0 \
      --reason "free bet, lost per Betfair settled screenshot" --apply

  # settle a bet-builder LOST from a verified result (no CLV on builders)
  python scripts/wca_override.py --bet-id 110 --status lost --settled-pl -10 \
      --reason "Belgium 0-0 Iran (verified): win leg failed" --apply

  # just flag/lock a bet (no value change), or clear the flag
  python scripts/wca_override.py --bet-id 57 --reason "hold: awaiting Ghana R32" --apply
  python scripts/wca_override.py --bet-id 57 --clear-override --apply
"""
import argparse
import csv
import datetime
import os
import sqlite3
import sys

_DEFAULT_DB = os.environ.get("WCA_DB", "data/wca.db")

# cli flag -> (column, python type)
_FIELDS = {
    "match_id": str, "match_desc": str, "market": str, "selection": str,
    "platform": str, "source": str, "account": str, "status": str,
    "notes": str, "settled_ts": str,
    "decimal_odds": float, "stake": float, "model_prob": float,
    "ev": float, "settled_pl": float, "closing_odds": float, "clv": float,
}
_VALID_STATUS = {"open", "won", "lost", "void"}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot_bets(con, db_path: str) -> str:
    """Dump the whole (small) bets table to a timestamped CSV before writing."""
    bdir = os.path.join(os.path.dirname(db_path) or ".", "backups")
    os.makedirs(bdir, exist_ok=True)
    path = os.path.join(bdir, "bets_snapshot_%s.csv"
                        % datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    rows = con.execute("SELECT * FROM bets ORDER BY id").fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        if rows:
            w.writerow(rows[0].keys())
            for r in rows:
                w.writerow(list(r))
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Manually overwrite one bet (source of truth).")
    ap.add_argument("--bet-id", type=int, required=True)
    for name, typ in _FIELDS.items():
        ap.add_argument("--" + name.replace("_", "-"), dest=name, type=typ, default=None)
    ap.add_argument("--reason", default=None,
                    help="manual_override note (why). Defaults to a generic stamp when fields change.")
    ap.add_argument("--clear-override", action="store_true",
                    help="remove the manual_override flag instead of setting it")
    ap.add_argument("--db", default=_DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    sets = {col: getattr(args, col) for col in _FIELDS if getattr(args, col) is not None}
    if "status" in sets and sets["status"].lower() not in _VALID_STATUS:
        print("ERROR: --status must be one of %s" % sorted(_VALID_STATUS))
        sys.exit(2)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM bets WHERE id=?", (args.bet_id,)).fetchone()
    if row is None:
        print("ERROR: no bet with id %d in %s" % (args.bet_id, args.db))
        sys.exit(1)
    before = dict(row)

    if args.clear_override:
        sets["manual_override"] = None
    elif args.reason is not None:
        sets["manual_override"] = args.reason
    elif sets:
        sets["manual_override"] = "manual override %s" % _now()

    if not sets:
        print("Nothing to change (pass field flags, --reason, or --clear-override).")
        sys.exit(0)

    print("bet #%d  %s  [%s]" % (args.bet_id, before.get("match_desc") or "(no match)", before.get("status")))
    for col in sets:
        print("  %-16s %r  ->  %r" % (col, before.get(col), sets[col]))

    if not args.apply:
        print("\nDRY-RUN — re-run with --apply to write (a bets-table snapshot is taken first).")
        con.close()
        return

    backup = _snapshot_bets(con, args.db)
    assignments = ", ".join("%s=?" % c for c in sets)
    con.execute("UPDATE bets SET %s WHERE id=?" % assignments,
                (*[sets[c] for c in sets], args.bet_id))
    con.commit()
    after = dict(con.execute("SELECT * FROM bets WHERE id=?", (args.bet_id,)).fetchone())
    con.close()
    print("\nAPPLIED.  bets snapshot -> %s" % backup)
    print("  now: status=%s  settled_pl=%s  manual_override=%r"
          % (after.get("status"), after.get("settled_pl"), after.get("manual_override")))


if __name__ == "__main__":
    main()
