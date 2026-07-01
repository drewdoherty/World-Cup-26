#!/usr/bin/env python
"""Build the historical match-event prop priors (corners/SoT/fouls/cards).

Loads football-data.co.uk club CSVs (Tier 1: shots/SoT/corners/fouls/cards at
scale) + the cached StatsBomb internationals (Tier 2: WC2018+2022, the
international anchor with xG), unifies them, computes per-market baselines, the
international-vs-domestic adjustment, and empirical-Bayes per-team priors, then
writes ``data/processed/prop_priors.csv``.

``data/processed`` is gitignored, so the CSV is a LOCAL artifact; the *code*
and a tiny committed fixture under ``tests/`` are what ship.  The models fall
back to hard-coded defaults (see ``matchevents.load_priors``) when the CSV is
absent, so a fresh checkout never breaks.

Attribution: football-data.co.uk (Joseph Buchdahl); StatsBomb open data.

Usage
-----
    .venv/bin/python scripts/wca_matchevents_data.py \
        [--seasons 2122 2223 2324] [--fd-cache DIR] [--no-network] [--out PATH]

``--no-network`` skips the football-data download and builds priors from the
cached StatsBomb props only (still real data, smaller sample).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from wca.data import matchevents  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", nargs="+", default=["2122", "2223", "2324"],
                    help="football-data.co.uk mmz4281 season codes")
    ap.add_argument("--fd-cache", default=str(ROOT / "data" / "raw" / "football_data"),
                    help="cache dir for downloaded football-data CSVs")
    ap.add_argument("--statsbomb-csv",
                    default=str(ROOT / "data" / "processed" / "props_matches.csv"))
    ap.add_argument("--no-network", action="store_true",
                    help="skip football-data download; StatsBomb only")
    ap.add_argument("--eb-tau", type=float, default=matchevents.DEFAULT_EB_TAU)
    ap.add_argument("--out", default=str(ROOT / "data" / "processed" / "prop_priors.csv"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    fd_wide = None
    if not args.no_network:
        logging.info("fetching football-data.co.uk: seasons=%s codes=%d",
                     args.seasons, len(matchevents.FOOTBALL_DATA_CODES))
        fd_wide = matchevents.fetch_football_data(
            seasons=tuple(args.seasons), cache_dir=args.fd_cache)
        logging.info("football-data: %d matches", len(fd_wide) if fd_wide is not None else 0)

    sb_wide = None
    sb_path = Path(args.statsbomb_csv)
    if sb_path.exists():
        sb_wide = matchevents.statsbomb_wide(matches_csv=str(sb_path))
        logging.info("statsbomb: %d matches", len(sb_wide))
    else:
        logging.warning("no statsbomb props csv at %s", sb_path)

    rows = matchevents.load_matchevents(football_data=fd_wide, statsbomb=sb_wide)
    if len(rows) == 0:
        logging.error("no match-event rows loaded; aborting (need network or "
                      "a cached props_matches.csv)")
        return 1
    logging.info("unified team-rows: %d (%d matches)", len(rows),
                 rows["match_id"].nunique())

    print("\n=== per-team-row global baselines (real data) ===")
    print("%-9s %8s %8s %8s %7s" % ("market", "mean", "var", "k", "n"))
    for market in matchevents.PRIOR_MARKETS + ("shots",):
        gb = matchevents.global_baseline(rows, market)
        adj = matchevents.intl_domestic_adjustment(rows, market)
        print("%-9s %8.3f %8.3f %8.1f %7d   intl/dom=%.3f"
              % (market, gb["mean"], gb["var"], gb["dispersion_k"], gb["n"], adj))

    table = matchevents.write_prop_priors(rows, path=args.out, eb_tau=args.eb_tau)
    n_teams = (table["entity"] != "GLOBAL").sum()
    print("\nwrote %s" % args.out)
    print("  GLOBAL rows: %d, per-team rows: %d"
          % ((table["entity"] == "GLOBAL").sum(), n_teams))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
