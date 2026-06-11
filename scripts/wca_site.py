#!/usr/bin/env python
"""Generate the World Cup Alpha static site data feed (``site/data.json``).

Reads the bet ledger and the cached matchday card, and writes the structured
JSON that the static trading-terminal front-end (``site/``) renders.  Unlike
the deterministic library in :mod:`wca.sitedata`, this CLI is permitted to read
the wall clock: it stamps the current UTC time and passes it through.

Usage
-----
    python scripts/wca_site.py [--db data/wca.db] \
        [--card data/card_latest.md] [--out site/data.json]
"""

from __future__ import annotations

import argparse
import datetime
import glob
import os
import sys

# Make ``src`` importable when run directly (python scripts/wca_site.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import linemove, sitedata  # noqa: E402


def _now_utc_str() -> str:
    """Return the current UTC time as an ISO-ish display string."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")


def _newest_snapshot_file(snapshots_dir: str) -> str:
    """Return the path of the newest raw h2h snapshot JSON, or ``""``.

    Filenames look like ``oddsapi_h2h_uk_20260611T134608Z.json``; their
    embedded UTC stamp sorts lexicographically, so the lexically-greatest name
    is the newest snapshot.
    """
    pattern = os.path.join(snapshots_dir, "oddsapi_h2h_uk_*.json")
    matches = glob.glob(pattern)
    if not matches:
        return ""
    return max(matches)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the World Cup Alpha static site data feed.",
    )
    parser.add_argument(
        "--db",
        default="data/wca.db",
        help="Path to the SQLite ledger (default: data/wca.db).",
    )
    parser.add_argument(
        "--card",
        default="data/card_latest.md",
        help="Path to the cached matchday card (default: data/card_latest.md).",
    )
    parser.add_argument(
        "--out",
        default="site/data.json",
        help="Destination JSON file (default: site/data.json).",
    )
    parser.add_argument(
        "--snapshots-dir",
        default="data/raw/snapshots",
        help="Directory of raw h2h snapshot JSON files used to derive event "
             "metadata for the line-movement export (default: "
             "data/raw/snapshots).",
    )
    parser.add_argument(
        "--linemove-out",
        default="site/linemove.json",
        help="Destination for the line-movement JSON "
             "(default: site/linemove.json).",
    )
    parser.add_argument(
        "--no-linemove",
        action="store_true",
        help="Skip the line-movement (linemove.json) export.",
    )
    args = parser.parse_args(argv)

    now_utc = _now_utc_str()
    out_path = sitedata.write_site_data(
        args.db, out_path=args.out, card_path=args.card, now_utc=now_utc,
    )

    if not args.no_linemove:
        snap_file = _newest_snapshot_file(args.snapshots_dir)
        event_meta = (
            linemove.event_meta_from_snapshot_file(snap_file) if snap_file else {}
        )
        lm_path = linemove.write_linemove(
            args.db,
            out_path=args.linemove_out,
            event_meta=event_meta,
            now_utc=now_utc,
        )
        print(lm_path)

    data = sitedata.build_site_data(args.db, card_path=args.card, now_utc=now_utc)
    totals = data["totals"]

    print(out_path)
    print(
        "totals: wagered=£{wagered:,.2f}  open=£{open_stake:,.2f}  "
        "settled_pl=£{settled_pl:,.2f}  n_bets={n_bets}  "
        "positions={positions}  fixtures={fixtures}".format(
            wagered=totals["wagered"],
            open_stake=totals["open_stake"],
            settled_pl=totals["settled_pl"],
            n_bets=totals["n_bets"],
            positions=len(data["positions"]),
            fixtures=len(data["predictions"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
