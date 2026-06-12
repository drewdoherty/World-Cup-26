#!/usr/bin/env python
"""Build the StatsBomb props dataset (WC2018 + WC2022) and print a summary.

Usage
-----
    .venv/bin/python scripts/wca_props_data.py
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wca.data import statsbomb  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cache_dir = str(ROOT / "data" / "raw" / "statsbomb")
    out_dir = str(ROOT / "data" / "processed")

    matches_df, players_df = statsbomb.build_props_dataset(
        cache_dir=cache_dir, out_dir=out_dir)

    corners = matches_df["corners_home"] + matches_df["corners_away"]
    cards = (matches_df["yellows_home"] + matches_df["yellows_away"]
             + matches_df["reds_home"] + matches_df["reds_away"])

    print("")
    print("=== WCA props dataset summary ===")
    print("matches: %d" % len(matches_df))
    for label in sorted(matches_df["season"].unique()):
        n = (matches_df["season"] == label).sum()
        print("  %s: %d matches" % (label, n))
    print("corners/match: mean=%.3f var=%.3f" % (corners.mean(), corners.var()))
    print("cards/match (Y+R): mean=%.3f var=%.3f" % (cards.mean(), cards.var()))
    print("players: %d" % len(players_df))

    print("\nTop 10 players by npxg_sum (both WCs):")
    top = players_df.nlargest(10, "npxg_sum")
    for _, r in top.iterrows():
        print("  %-30s %-15s npxg=%.2f shots=%d goals=%d matches=%d"
              % (r["player"][:30], r["team"][:15], r["npxg_sum"],
                 r["shots"], r["goals"], r["matches"]))

    # Per-WC top 5 by npxG.
    for season_id, label in sorted(statsbomb.WC_SEASONS.items()):
        matches = statsbomb.fetch_matches(
            statsbomb.WC_COMPETITION_ID, season_id, cache_dir=cache_dir)
        ev = {m["match_id"]: statsbomb.fetch_events(
            m["match_id"], cache_dir=cache_dir) for m in matches}
        pdf = statsbomb.player_shares(ev)
        print("\nTop 5 npxg, %s:" % label)
        for _, r in pdf.nlargest(5, "npxg_sum").iterrows():
            print("  %-30s %-15s npxg=%.2f goals=%d"
                  % (r["player"][:30], r["team"][:15], r["npxg_sum"], r["goals"]))

    print("\nWrote %s/props_matches.csv and props_players.csv" % out_dir)


if __name__ == "__main__":
    main()
