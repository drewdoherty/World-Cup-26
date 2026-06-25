#!/usr/bin/env python3
"""CLI for the prediction ledger.

Subcommands
-----------
ensure    create the schema (tables / indexes / views) in --db
backfill  replay model_predictions_log.jsonl into the paper book, settle and
          (with --now) stamp CLV from wca.db read-only odds snapshots
settle    settle open predictions against wc2026_results.json + advancement
close     stamp 1X2 CLV from wca.db read-only odds snapshots (needs --now)
publish   project --db to site-analytics/data/predledger.json

The default ``--db`` is ``data/dev.db``.  Writing ``data/wca.db`` is refused
unless ``WCA_ALLOW_PROD_DB`` is set (the store guard).

Timestamps (``--now`` / publish ``generated``) are caller-supplied so library
code never reads the wall clock; the CLI fills them from ``datetime.now(UTC)``
when omitted, which is the only place a clock is read.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

# Allow running as a plain script (scripts/ is not a package).
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from wca.predledger import backfill as pl_backfill  # noqa: E402
from wca.predledger import close as pl_close  # noqa: E402
from wca.predledger import publish as pl_publish  # noqa: E402
from wca.predledger import settle as pl_settle  # noqa: E402
from wca.predledger import store  # noqa: E402

_DEFAULT_DB = "data/dev.db"
_RESULTS = "data/processed/wc2026_results.json"
_ADV = "data/advancement_played_results.json"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cmd_ensure(args: argparse.Namespace) -> int:
    store.ensure_schema(args.db)
    print(json.dumps({"ensured": args.db}))
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    now = args.now or _now_utc()
    summary = pl_backfill.run_backfill(
        db=args.db,
        now=now,
        do_settle=not args.no_settle,
        do_close=not args.no_close,
    )
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_settle(args: argparse.Namespace) -> int:
    store.ensure_schema(args.db)
    tally = pl_settle.settle_open(args.results, args.adv, args.db)
    print(json.dumps(tally, indent=2))
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    store.ensure_schema(args.db)
    now = args.now or _now_utc()
    stats = pl_close.stamp_closes(now, args.db)
    print(json.dumps(stats, indent=2))
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    store.ensure_schema(args.db)
    generated = args.generated or _now_utc()
    path = pl_publish.write_feed(generated, args.db, args.out)
    payload = pl_publish.build_feed(generated, args.db)
    print(json.dumps({"written": path, "meta": payload["meta"]}, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WCA prediction-ledger CLI")
    parser.add_argument("--db", default=_DEFAULT_DB, help="SQLite path (default dev.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ensure = sub.add_parser("ensure", help="create schema")
    p_ensure.set_defaults(func=_cmd_ensure)

    p_bf = sub.add_parser("backfill", help="replay log -> paper book + settle + close")
    p_bf.add_argument("--now", default=None, help="ISO-8601 UTC gate for close")
    p_bf.add_argument("--no-settle", action="store_true")
    p_bf.add_argument("--no-close", action="store_true")
    p_bf.set_defaults(func=_cmd_backfill)

    p_settle = sub.add_parser("settle", help="settle open predictions vs results")
    p_settle.add_argument("--results", default=_RESULTS)
    p_settle.add_argument("--adv", default=_ADV)
    p_settle.set_defaults(func=_cmd_settle)

    p_close = sub.add_parser("close", help="stamp 1X2 CLV from wca.db RO")
    p_close.add_argument("--now", default=None, help="ISO-8601 UTC kickoff gate")
    p_close.set_defaults(func=_cmd_close)

    p_pub = sub.add_parser("publish", help="write site-analytics/data/predledger.json")
    p_pub.add_argument("--out", default=pl_publish._FEED_PATH)
    p_pub.add_argument("--generated", default=None, help="ISO-8601 UTC feed stamp")
    p_pub.set_defaults(func=_cmd_publish)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
