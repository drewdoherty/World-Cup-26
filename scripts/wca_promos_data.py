#!/usr/bin/env python
"""Generate the promo-operations site feed (``site/promos_data.json``).

Reads the :mod:`wca.promos` catalog from the SQLite db and writes the structured
JSON the static front-end renders. Unlike the deterministic library in
:mod:`wca.promosdata`, this CLI is permitted to read the wall clock: it stamps
the current UTC time and passes it through (mirrors :mod:`scripts.wca_site`).

The scores feed (``site/scores_data.json``) is loaded tolerantly if present and
forwarded to the builder for parity / future use; a missing or garbled feed is
not fatal.

Usage
-----
    python scripts/wca_promos_data.py [--db data/wca.db] \
        [--scores site/scores_data.json] [--out site/promos_data.json]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

# Make ``src`` importable when run directly (mirror wca_site.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import promos, promosdata  # noqa: E402


def _now_utc_str() -> str:
    """Return the current UTC time as an ISO-ish display string (mirror wca_site)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")


def _load_scores(path: str):
    """Load the scores feed JSON tolerantly; return ``{}`` on any problem."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the World Cup Alpha promo-operations site feed.",
    )
    parser.add_argument(
        "--db", default="data/wca.db",
        help="Path to the SQLite db holding the promo catalog (default: data/wca.db).",
    )
    parser.add_argument(
        "--scores", default="site/scores_data.json",
        help="Path to the scores feed (loaded tolerantly, forwarded to the "
             "builder for parity; default: site/scores_data.json).",
    )
    parser.add_argument(
        "--out", default="site/promos_data.json",
        help="Destination JSON file (default: site/promos_data.json).",
    )
    args = parser.parse_args(argv)

    now_utc = _now_utc_str()
    scores_feed = _load_scores(args.scores)

    conn = promos._connect(args.db)
    try:
        promos.init_db(conn)
        data = promosdata.build_promos_data(conn, scores_feed, now_utc)
    finally:
        conn.close()

    parent = os.path.dirname(os.path.abspath(args.out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    n_signup = len(data["signup_offers"])
    n_ongoing = sum(len(s["ongoing"]) for s in data["sites"])
    n_boosts = sum(len(s["boosts"]) for s in data["sites"])
    n_watch = len(data["watchlist"])
    n_ok = sum(1 for h in data["scrape_health"] if h["status"] == "ok")
    print(args.out)
    print(
        "promos: sites=%d signup=%d ongoing=%d boosts=%d watchlist=%d "
        "boost_evals=%d | scrape ok=%d/%d"
        % (len(data["sites"]), n_signup, n_ongoing, n_boosts, n_watch,
           len(data["boost_evals"]), n_ok, len(data["scrape_health"]))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
