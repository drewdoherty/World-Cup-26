#!/usr/bin/env python3
"""CLI for the World Cup Alpha prediction ledger.

Commands
--------
settle  Settle open predictions against result files.

Usage
-----
    python scripts/wca_predledger.py settle \\
        [--results data/processed/wc2026_results.json] \\
        [--advancement-results data/advancement_played_results.json] \\
        [--db data/dev.db]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Ensure the src package root is importable when run directly.
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_DB = os.environ.get("WCA_DB", "data/dev.db")
_DEFAULT_RESULTS = "data/processed/wc2026_results.json"
_DEFAULT_ADV_RESULTS = "data/advancement_played_results.json"


# ---------------------------------------------------------------------------
# Settle subcommand
# ---------------------------------------------------------------------------


def cmd_settle(args: argparse.Namespace) -> int:
    from wca.predledger.settle import settle_open
    from wca.predledger.store import ensure_schema

    results_path = args.results
    adv_path = args.advancement_results
    db = args.db

    if not os.path.exists(results_path):
        logger.error("Results file not found: %s", results_path)
        return 1
    if not os.path.exists(adv_path):
        logger.error("Advancement results file not found: %s", adv_path)
        return 1

    with open(results_path) as f:
        data = json.load(f)
    results = data.get("results") if isinstance(data, dict) else data

    with open(adv_path) as f:
        adv_results = json.load(f)

    ensure_schema(db)
    n = settle_open(results, adv_results, db)
    logger.info("Settled %d predictions in %s", n, db)
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wca_predledger",
        description="World Cup Alpha prediction ledger CLI",
    )
    p.add_argument("--db", default=_DEFAULT_DB, help="SQLite database path")
    sub = p.add_subparsers(dest="command", required=True)

    settle_p = sub.add_parser("settle", help="Settle open predictions against result files")
    settle_p.add_argument(
        "--results",
        default=_DEFAULT_RESULTS,
        help=f"Match results JSON (default: {_DEFAULT_RESULTS})",
    )
    settle_p.add_argument(
        "--advancement-results",
        default=_DEFAULT_ADV_RESULTS,
        dest="advancement_results",
        help=f"Advancement results JSON (default: {_DEFAULT_ADV_RESULTS})",
    )
    settle_p.set_defaults(func=cmd_settle)

    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
