#!/usr/bin/env python3
"""Polymarket orderflow ingest CLI — sweep WC26 taker fills into sqlite.

Run with::

    PYTHONPATH=src .venv/bin/python scripts/pm_orderflow_ingest.py

Thin wrapper around :mod:`wca.pm.orderflow`: discovers every in-scope 2026
World Cup team-level market on Polymarket (advancement rungs, winner, group
winners, match 1X2, other team futures), upserts them into
``data/pm_orderflow.db`` and pages the data-api ``/trades`` history for each.
Idempotent — reruns only fetch until they hit already-stored fills. The
data-api history window is capped (offset 3000), so run this regularly or the
older flow is gone for good; markets that hit the cap are logged truncated and
additionally swept with the large-trade CASH filters. ``--backfill-leaderboards
site/microstructure/orderflow.json`` instead sweeps the complete per-user fill
history of every rendered leaderboard wallet (the per-user filter gets its own
offset window), so featured PnL is not computed on truncated positions.

Never touches ``data/wca.db``.
"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default="data/pm_orderflow.db",
        help="sqlite path (default: data/pm_orderflow.db)",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="only discover/upsert markets; skip trade ingestion",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        metavar="N",
        help="ingest at most N markets (testing)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help="parallel fetch workers (default 8)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip markets already in pm_ingest_log (continue a killed backfill)",
    )
    parser.add_argument(
        "--backfill-leaderboards",
        metavar="ORDERFLOW_JSON",
        default=None,
        help="skip the market sweep; instead backfill the full per-user fill "
        "history of every wallet on the leaderboards of the given "
        "orderflow.json (fixes partial PnL on truncated markets)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.backfill_leaderboards:
        import json

        from wca.pm.orderflow import backfill_wallets

        with open(args.backfill_leaderboards, "r", encoding="utf-8") as fh:
            feed = json.load(fh)
        wallets: list = []
        for board in (feed.get("leaderboards") or {}).values():
            for row in board or []:
                w = row.get("wallet")
                if w and w not in wallets:
                    wallets.append(w)
        print("backfilling %d leaderboard wallets ..." % len(wallets))
        summary = backfill_wallets(args.db, wallets)
        print("wallets backfilled:  %s" % summary["wallets"])
        print("trades fetched:      %s" % summary["fetched"])
        print("trades new:          %s" % summary["new"])
        if summary["still_truncated"]:
            print("still capped (>3500 fills in one market):")
            for w in summary["still_truncated"]:
                print("  - %s" % w)
        return 0

    from wca.pm.orderflow import run

    summary = run(
        args.db,
        discover_only=args.discover_only,
        max_markets=args.max_markets,
        workers=args.workers,
        resume=args.resume,
    )
    print("markets discovered: %s" % summary.get("markets_discovered"))
    if not args.discover_only:
        print("markets ingested:   %s" % summary.get("markets_ingested"))
        print("trades fetched:     %s" % summary.get("trades_fetched"))
        print("trades new:         %s" % summary.get("trades_new"))
        truncated = summary.get("truncated") or []
        print("truncated markets:  %d" % len(truncated))
        for slug in truncated:
            print("  - %s" % slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
