#!/usr/bin/env python
"""Generate the World Cup Alpha static HTML dashboard.

Reads the bet ledger and writes a single self-contained HTML page suitable for
GitHub Pages.  Unlike the library functions in :mod:`wca.dashboard`, this CLI is
permitted to read the wall clock: it stamps the current UTC time and passes it
through to the (otherwise deterministic) renderer.

Usage
-----
    python scripts/wca_dashboard.py [--db data/wca.db] [--out docs/dashboard/index.html]
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

# Make ``src`` importable when run directly (python scripts/wca_dashboard.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.dashboard import gather_stats, render_html, write_dashboard  # noqa: E402


def _now_utc_str() -> str:
    """Return the current UTC time as an ISO-ish display string."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the World Cup Alpha static HTML dashboard.",
    )
    parser.add_argument(
        "--db",
        default="data/wca.db",
        help="Path to the SQLite ledger (default: data/wca.db).",
    )
    parser.add_argument(
        "--out",
        default="docs/dashboard/index.html",
        help="Destination HTML file (default: docs/dashboard/index.html).",
    )
    args = parser.parse_args(argv)

    now_utc = _now_utc_str()
    out_path = write_dashboard(args.db, args.out, now_utc)

    # Re-gather once for the printed summary (cheap; keeps output truthful).
    stats = gather_stats(args.db)
    totals = stats["totals"]

    print(out_path)
    print(
        "totals: wagered=£{wagered:,.2f}  open=£{open_stake:,.2f}  "
        "settled_pl=£{settled_pl:,.2f}  n_bets={n_bets}".format(
            wagered=float(totals["wagered"]),
            open_stake=float(totals["open_stake"]),
            settled_pl=float(totals["settled_pl"]),
            n_bets=int(totals["n_bets"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
