#!/usr/bin/env python
"""Generate tracking buckets for the Tracking tab — Polymarket metrics aligned to match number.

Pulls real PM market history for configurable team+metric pairs (buckets),
maps dates to cumulative match counts, and embeds the data in `site/tracking_buckets.json`.

Buckets are user-selectable filters in the UI; each has its own {teams, metric, y_label}.

    PYTHONPATH=src python scripts/wca_tracking.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import ast
import pandas as pd
from wca.data.polymarket import find_world_cup_markets


DB = os.environ.get("WCA_DB", "data/wca.db")
OUT = os.environ.get("WCA_TRACKING_OUT", "site/tracking_buckets.json")


def get_json(url: str) -> dict:
    """Fetch JSON with User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def build_match_timeline() -> dict:
    """Return {date: cumulative_match_count} from cleaned results."""
    df = pd.read_csv("data/raw/martj42_cleaned.csv")
    wc = df[
        (df.tournament == "FIFA World Cup") & (df.date.astype(str).str.startswith("2026"))
    ].dropna(subset=["home_score", "away_score"])
    dates = sorted(wc.date.astype(str).unique())
    return {d: int((wc.date.astype(str) <= d).sum()) for d in dates}


def fetch_market_history(question_kw: list[str]) -> list[tuple[str, float]]:
    """Fetch PM market history for a question matching all keywords.

    Returns: [(date, probability), ...] where probability is the YES outcome price.
    """
    evs = find_world_cup_markets()
    for e in evs:
        for m in (e.get("markets") or []):
            q = (m.get("question", "") or "").lower()
            if all(kw in q for kw in question_kw) and m.get("clobTokenIds"):
                # YES outcome is always the first token
                tid = ast.literal_eval(m["clobTokenIds"])[0]
                pts = (
                    get_json(
                        f"https://clob.polymarket.com/prices-history?market={tid}&interval=max&fidelity=720"
                    ).get("history", [])
                )
                series = []
                seen = set()
                for p in pts:
                    d = time.strftime("%Y-%m-%d", time.gmtime(p["t"]))
                    if d >= "2026-06-10" and d not in seen:
                        seen.add(d)
                        series.append((d, round(p["p"] * 100, 1)))
                return series
    return []


def build_buckets(timeline: dict) -> dict:
    """Build all tracking buckets with real PM data."""
    buckets = {
        "top_5_favourites": {
            "label": "Top 5 favourites",
            "metric": "win World Cup",
            "y_label": "P(win)",
            "teams": [
                ("France", ["france", "win the", "world cup"]),
                ("Spain", ["spain", "win the", "world cup"]),
                ("England", ["england", "win the", "world cup"]),
                ("Argentina", ["argentina", "win the", "world cup"]),
                ("Brazil", ["brazil", "win the", "world cup"]),
            ],
        },
        "advancement_r16": {
            "label": "Advancement to R16",
            "metric": "reach Round of 16",
            "y_label": "P(reach R16)",
            "teams": [
                ("Iran", ["iran", "round of 16"]),
                ("Australia", ["australia", "round of 16"]),
                ("Colombia", ["colombia", "round of 16"]),
                ("Japan", ["japan", "round of 16"]),
                ("Uruguay", ["uruguay", "round of 16"]),
            ],
        },
        "biggest_movers": {
            "label": "Biggest movers",
            "metric": "advance to knockout",
            "y_label": "P(reach knockout)",
            "teams": [
                ("Canada", ["canada", "advance to the knockout"]),
                ("Switzerland", ["switzerland", "advance to the knockout"]),
                ("Qatar", ["qatar", "advance to the knockout"]),
                ("Czech Republic", ["czechia" if False else "czech", "advance to the knockout"]),
                ("South Korea", ["south korea", "advance to the knockout"]),
            ],
        },
        "hosts": {
            "label": "Host nations",
            "metric": "advance to knockout",
            "y_label": "P(reach knockout)",
            "teams": [
                ("USA", ["united states", "advance to the knockout"]),
                ("Mexico", ["mexico", "advance to the knockout"]),
                ("Canada", ["canada", "advance to the knockout"]),
            ],
        },
    }

    data = {}
    for bucket_key, config in buckets.items():
        teams_data = {}
        for team_name, kws in config["teams"]:
            series = fetch_market_history(kws)
            if series:
                teams_data[team_name] = [
                    {"date": d, "m": mn, "p": p}
                    for d, p in series
                    if (mn := timeline.get(d, 0 if d < "2026-06-11" else -1)) >= 0
                ]
        if teams_data:
            data[bucket_key] = {
                "label": config["label"],
                "metric": config["metric"],
                "y_label": config["y_label"],
                "teams": teams_data,
            }
    return data


def main():
    print("building match timeline from cleaned results...")
    timeline = build_match_timeline()
    print(f"  {len(timeline)} dates, {max(timeline.values())} cumulative matches")

    print("pulling real Polymarket histories for tracking buckets...")
    data = build_buckets(timeline)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))

    print(f"wrote {OUT}")
    for bkey, bdata in data.items():
        teams_ct = len(bdata["teams"])
        pts_ct = sum(len(v) for v in bdata["teams"].values())
        print(f"  {bkey}: {teams_ct} teams, {pts_ct} total points | {bdata['label']}")


if __name__ == "__main__":
    main()
