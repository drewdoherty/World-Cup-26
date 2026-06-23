#!/usr/bin/env python3
"""Generate the enhanced Scores & Markets feed (``site/scores_markets.json``).

Powers the overhauled "Scorelines · Exposure · Best Price" panel: model markets
for every upcoming game of the current stage (group) plus a projected next stage
(Round of 32), organised so the site can pivot the view by **stage** or by
**team**.

Scorelines (1X2 / top scoreline / O-U 2.5 / BTTS / xG) are computed fresh from
the Elo + Dixon-Coles fit. Advancement probabilities, group standings and the
projected R32 qualifiers are **reused** from ``site/advancement_data.json`` (the
same numbers the advancement panel shows) so the two panels never disagree and
we avoid re-running the Monte-Carlo sim on every refresh.

Run hourly alongside the other site feeds (see .github/workflows/hourly-odds.yml).

    python scripts/wca_scores_markets_data.py [--out site/scores_markets.json]
        [--advancement site/advancement_data.json]
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from wca.card import fit_models, dc_probs, elo_probs  # noqa: E402
from wca.data.cleaning import resolve_results_path  # noqa: E402
from wca.advancement import WC2026_GROUPS  # noqa: E402

GROUP_LETTERS = list("ABCDEFGHIJKL")
STAGES = [
    ("group", "Group Stage"),
    ("r32", "Round of 32"),
    ("r16", "Round of 16"),
    ("qf", "Quarter-finals"),
    ("sf", "Semi-finals"),
    ("final", "Final"),
]


def _market(models, home: str, away: str) -> Dict[str, Any]:
    """Model FT markets for one fixture (neutral WC venue)."""
    mat, lam_h, lam_a = models.dc.score_matrix(home, away, neutral=True, warn=False)
    gx, gy = np.indices(mat.shape)
    dc = [
        float(np.tril(mat, -1).sum()),  # home win
        float(np.trace(mat)),            # draw
        float(np.triu(mat, 1).sum()),    # away win
    ]
    elo = elo_probs(models, home, away, True)
    blend = [round((dc[i] + elo[i]) / 2.0, 4) for i in range(3)]
    flat = [((x, y), float(mat[x, y])) for x in range(mat.shape[0]) for y in range(mat.shape[1])]
    (tx, ty), tp = max(flat, key=lambda z: z[1])
    return {
        "x1x2": blend,
        "top": f"{tx}-{ty}",
        "topp": round(tp, 3),
        "over25": round(float(mat[(gx + gy) >= 3].sum()), 3),
        "btts": round(float(mat[(gx >= 1) & (gy >= 1)].sum()), 3),
        "eg": [round(lam_h, 2), round(lam_a, 2)],
    }


def _load_advancement(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"teams": [], "groups": {}}
    with open(path) as fh:
        return json.load(fh)


def build(results_path: str, advancement_path: str) -> Dict[str, Any]:
    adv = _load_advancement(advancement_path)
    team_adv = {t["team"]: t.get("model", {}) for t in adv.get("teams", [])}
    standings = adv.get("groups", {})
    team_group = {t: g for g, ts in WC2026_GROUPS.items() for t in ts}

    models = fit_models(pd.read_csv(results_path), half_life_years=8.0)

    df = pd.read_csv(results_path)
    df["_d"] = pd.to_datetime(df["date"], errors="coerce")
    wc = df[(df["tournament"] == "FIFA World Cup") & (df["_d"].dt.year == 2026)]

    # --- group stage: every group game (played + upcoming) with model markets ---
    # Played games also carry their full-time score ("ft": [home, away]) so the
    # site's By-Group view can show results next to the pre-match model bar.
    group_games: List[Dict[str, Any]] = []
    for _, r in wc.sort_values("_d").iterrows():
        h, a = r["home_team"], r["away_team"]
        g = team_group.get(h)
        if not g or g != team_group.get(a):
            continue  # not a same-group fixture (knockout placeholder etc.)
        m = _market(models, h, a)  # pre-match model markets (neutral venue)
        m.update({"home": h, "away": a, "date": str(r["_d"].date()), "group": g})
        if pd.notna(r["home_score"]) and pd.notna(r["away_score"]):
            m["ft"] = [int(r["home_score"]), int(r["away_score"])]
            m["status"] = "FT"
        else:
            m["status"] = "upcoming"
        group_games.append(m)

    # --- next stage: projected R32 qualifiers (top-2 per group standings) ---
    r32_projected = []
    for g in GROUP_LETTERS:
        rows = standings.get(g) or []
        top2 = [
            {"team": x["team"], "pos": x["pos"], "pts": x.get("pts"),
             "p_adv": round(team_adv.get(x["team"], {}).get("R32", 0.0), 3)}
            for x in rows[:2]
        ]
        if top2:
            r32_projected.append({"group": g, "teams": top2})

    n_upcoming = sum(1 for gg in group_games if gg.get("status") != "FT")

    # --- by-team: upcoming games + advancement line per team ---
    by_team: Dict[str, Any] = {}
    for team, g in sorted(team_group.items()):
        games = [
            gg for gg in group_games
            if (gg["home"] == team or gg["away"] == team) and gg.get("status") != "FT"
        ]
        by_team[team] = {
            "group": g,
            "adv": {k: round(v, 3) for k, v in team_adv.get(team, {}).items()},
            "games": games,
        }

    # stage availability: group is current, r32 next (projected), rest locked
    stages = []
    for key, label in STAGES:
        if key == "group":
            status, count = "current", n_upcoming
        elif key == "r32":
            status, count = "next", len(r32_projected)
        else:
            status, count = "locked", 0
        stages.append({"key": key, "label": label, "status": status, "count": count})

    return {
        "meta": {
            "generated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "n_games": len(group_games),
            "source": "Elo+DC scorelines · advancement reused from advancement_data.json",
        },
        "stages": stages,
        "group_games": group_games,
        "r32_projected": r32_projected,
        "by_team": by_team,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build site/scores_markets.json")
    p.add_argument("--out", default="site/scores_markets.json")
    p.add_argument("--advancement", default="site/advancement_data.json")
    p.add_argument("--results", default=None, help="override results path")
    args = p.parse_args(argv)

    results_path = args.results or resolve_results_path()
    data = build(results_path, args.advancement)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(
        f"[scores_markets] wrote {args.out}: {data['meta']['n_games']} group games, "
        f"{len(data['r32_projected'])} projected R32 groups, {len(data['by_team'])} teams"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
