"""Construct a correlation-aware same-game *bet builder* for one fixture.

A bet builder (bet365's same-game multi) combines several selections from a
single match into one bet. Same-game legs are correlated, so the honest price is
the joint probability read off the reconciled Dixon-Coles score matrix — not the
product of the individual leg odds. This script reconstructs that matrix from the
published scores feed (``site/scores_data.json``) and returns the most likely
builder whose fair price clears a minimum-odds floor (default 2.0 = EVS / evens).

Usage:
    ./.venv/bin/python scripts/wca_betbuilder.py
    ./.venv/bin/python scripts/wca_betbuilder.py --fixture "Scotland vs Brazil" --min-odds 2.0
    ./.venv/bin/python scripts/wca_betbuilder.py --fixture "scotland brazil" --max-legs 3 --anchor "to win"

No API credits used — reads the local feed only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wca.betbuilder import (  # noqa: E402
    EVS,
    build_bet_builder,
    find_fixture,
    format_bet_builder,
    load_feed,
    matrix_from_feed_entry,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fixture",
        default="Scotland vs Brazil",
        help="Fixture to build for (loose match, e.g. 'scotland brazil').",
    )
    ap.add_argument(
        "--feed",
        default="site/scores_data.json",
        help="Path to the scores feed JSON (default: site/scores_data.json).",
    )
    ap.add_argument(
        "--min-odds",
        type=float,
        default=EVS,
        help="Minimum decimal odds for the builder (default 2.0 = EVS).",
    )
    ap.add_argument("--min-legs", type=int, default=2, help="Minimum legs (default 2).")
    ap.add_argument("--max-legs", type=int, default=4, help="Maximum legs (default 4).")
    ap.add_argument(
        "--anchor",
        action="append",
        default=None,
        metavar="SUBSTR",
        help="Require a leg whose label contains SUBSTR (repeatable), "
        "e.g. --anchor 'to win'.",
    )
    ap.add_argument("--top-n", type=int, default=5, help="Builders to show (default 5).")
    args = ap.parse_args()

    fixtures = load_feed(args.feed)
    if not fixtures:
        print("No fixtures found in %s" % args.feed, file=sys.stderr)
        return 1

    entry = find_fixture(fixtures, args.fixture)
    if entry is None:
        names = ", ".join(str(f.get("fixture", "?")) for f in fixtures)
        print(
            "Fixture %r not found. Available: %s" % (args.fixture, names),
            file=sys.stderr,
        )
        return 2

    fm = matrix_from_feed_entry(entry)
    builders = build_bet_builder(
        fm,
        min_odds=args.min_odds,
        min_legs=args.min_legs,
        max_legs=args.max_legs,
        must_include=args.anchor,
        top_n=args.top_n,
    )
    print(format_bet_builder(fm, builders, min_odds=args.min_odds))
    return 0 if builders else 3


if __name__ == "__main__":
    raise SystemExit(main())
