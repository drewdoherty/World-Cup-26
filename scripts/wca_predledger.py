"""Prediction-ledger CLI.

Usage examples
--------------
Ensure schema exists (safe to call repeatedly)::

    python scripts/wca_predledger.py schema --db data/dev.db

Show the model book (all predictions)::

    python scripts/wca_predledger.py show --db data/dev.db

Show settled predictions with realized/paper labels::

    python scripts/wca_predledger.py realized --db data/dev.db
"""

from __future__ import annotations

import argparse
import sys

from wca.predledger import store


def cmd_schema(args: argparse.Namespace) -> None:
    store.ensure_schema(args.db)
    print("Schema ensured: %s" % args.db)


def cmd_show(args: argparse.Namespace) -> None:
    rows = store.model_book(db_path=args.db)
    if not rows:
        print("No predictions found.")
        return
    print("%-8s  %-12s  %-6s  %-12s  %-8s  %s" % (
        "placed", "match_id", "market", "selection", "offered", "prediction_id"
    ))
    print("-" * 72)
    for r in rows:
        print("%-8s  %-12s  %-6s  %-12s  %-8s  %s" % (
            "yes" if r["placed"] else "paper",
            (r["match_id"] or r["stage"] or "")[:12],
            str(r["market"])[:6],
            str(r["selection"])[:12],
            ("%.3f" % r["offered_odds"]) if r["offered_odds"] else "-",
            r["prediction_id"],
        ))
    print("\n%d row(s)" % len(rows))


def cmd_realized(args: argparse.Namespace) -> None:
    rows = store.realized_book(db_path=args.db)
    settled = [r for r in rows if r["outcome"] is not None]
    if not settled:
        print("No settled predictions found.")
        return
    print("%-10s  %-6s  %-8s  %-8s  %s" % (
        "book_type", "outcome", "clv", "offered", "prediction_id"
    ))
    print("-" * 60)
    for r in settled:
        clv_str = ("%.4f" % r["clv"]) if r["clv"] is not None else "NULL"
        print("%-10s  %-6s  %-8s  %-8s  %s" % (
            r["book_type"],
            r["outcome"] or "-",
            clv_str,
            ("%.3f" % r["offered_odds"]) if r["offered_odds"] else "-",
            r["prediction_id"],
        ))
    print("\n%d settled row(s)" % len(settled))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Prediction-ledger CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite database path")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("schema", help="Ensure predledger schema exists in the database")
    sub.add_parser("show", help="Print the model book (all predictions)")
    sub.add_parser("realized", help="Print settled predictions with paper/realized labels")

    args = parser.parse_args(argv)

    if args.cmd == "schema":
        cmd_schema(args)
    elif args.cmd == "show":
        cmd_show(args)
    elif args.cmd == "realized":
        cmd_realized(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
