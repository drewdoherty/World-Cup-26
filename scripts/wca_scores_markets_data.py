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
from typing import Any, Dict, List, Optional

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


# Knockout round windows for the fixed 2026 schedule (venues/dates are set; only
# the teams change as results land). A KO fixture is bucketed to its round purely
# by its calendar date, so the feed stays correct as later rounds appear in the
# results spine — no per-round bracket resolution needed.
_KO_WINDOWS = [
    ("r32", datetime.date(2026, 7, 3)),
    ("r16", datetime.date(2026, 7, 8)),
    ("qf", datetime.date(2026, 7, 12)),
    ("sf", datetime.date(2026, 7, 16)),
    # 2026-07-18 is the 3rd-place play-off — intentionally not a headline stage.
    ("final", datetime.date(2026, 7, 31)),
]
_KO_LABEL = {"r32": "Round of 32", "r16": "Round of 16", "qf": "Quarter-finals",
             "sf": "Semi-finals", "final": "Final"}


def _ko_round(d: datetime.date) -> Optional[str]:
    """Map a knockout fixture's date to its round key, or None (e.g. 3rd-place)."""
    for key, cutoff in _KO_WINDOWS:
        if d <= cutoff:
            if key == "final" and d < datetime.date(2026, 7, 19):
                return None  # 3rd-place play-off window — skip
            return key
    return None


def build(results_path: str, advancement_path: str) -> Dict[str, Any]:
    adv = _load_advancement(advancement_path)
    team_adv = {t["team"]: t.get("model", {}) for t in adv.get("teams", [])}
    standings = adv.get("groups", {})
    team_group = {t: g for g, ts in WC2026_GROUPS.items() for t in ts}

    models = fit_models(pd.read_csv(results_path), half_life_years=8.0)

    df = pd.read_csv(results_path)
    df["_d"] = pd.to_datetime(df["date"], errors="coerce")
    wc = df[(df["tournament"] == "FIFA World Cup") & (df["_d"].dt.year == 2026)]

    # --- current stage: ALL group games (completed + upcoming) with model markets ---
    group_games: List[Dict[str, Any]] = []
    n_upcoming = 0
    for _, r in wc.sort_values("_d").iterrows():
        h, a = r["home_team"], r["away_team"]
        g = team_group.get(h)
        if not g or g != team_group.get(a):
            continue  # not a same-group fixture (knockout placeholder etc.)
        m = _market(models, h, a)
        ft: Optional[str] = None
        if pd.notna(r.get("home_score")) and pd.notna(r.get("away_score")):
            ft = f"{int(r['home_score'])}-{int(r['away_score'])}"
        else:
            n_upcoming += 1
        m.update({"home": h, "away": a, "date": str(r["_d"].date()), "group": g, "ft": ft})
        group_games.append(m)

    # --- knockout stages: concrete ties from the results spine (FT-accurate) ---
    # A cross-group WC2026 fixture is a knockout tie; bucket it to its round by
    # date and price the SAME model markets as the group rows. Rounds populate as
    # results land (R16 teams are only known once R32 completes), so this stays
    # accurate to the latest FT with no timed job.
    ko_games: Dict[str, List[Dict[str, Any]]] = {k: [] for k, _ in _KO_WINDOWS}
    ko_upcoming: Dict[str, int] = {k: 0 for k, _ in _KO_WINDOWS}
    for _, r in wc.sort_values("_d").iterrows():
        h, a = r["home_team"], r["away_team"]
        gh, ga = team_group.get(h), team_group.get(a)
        if gh and ga and gh == ga:
            continue  # same-group => already handled as a group game
        if pd.isna(r["_d"]):
            continue
        rnd = _ko_round(r["_d"].date())
        if rnd is None:
            continue
        m = _market(models, h, a)
        ft = None
        if pd.notna(r.get("home_score")) and pd.notna(r.get("away_score")):
            ft = f"{int(r['home_score'])}-{int(r['away_score'])}"
        else:
            ko_upcoming[rnd] += 1
        m.update({"home": h, "away": a, "date": str(r["_d"].date()),
                  "group": None, "round": _KO_LABEL[rnd], "ft": ft})
        ko_games[rnd].append(m)

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

    # --- by-team: all of a team's games (group + knockout) + advancement line ---
    all_games = group_games + [g for gs in ko_games.values() for g in gs]
    by_team: Dict[str, Any] = {}
    for team, g in sorted(team_group.items()):
        games = [gg for gg in all_games if gg["home"] == team or gg["away"] == team]
        by_team[team] = {
            "group": g,
            "adv": {k: round(v, 3) for k, v in team_adv.get(team, {}).items()},
            "games": games,
        }

    # stage availability: 'current' = the earliest stage still holding an unplayed
    # game; earlier stages are 'done' (clickable, all FT-in), later ones 'locked'.
    stage_upcoming = {"group": n_upcoming, "r32": ko_upcoming["r32"], "r16": ko_upcoming["r16"],
                      "qf": ko_upcoming["qf"], "sf": ko_upcoming["sf"], "final": ko_upcoming["final"]}
    stage_has = {"group": bool(group_games), "r32": bool(ko_games["r32"]), "r16": bool(ko_games["r16"]),
                 "qf": bool(ko_games["qf"]), "sf": bool(ko_games["sf"]), "final": bool(ko_games["final"])}
    current_key = next((k for k, _ in STAGES if stage_has[k] and stage_upcoming[k] > 0), None)
    stages = []
    for key, label in STAGES:
        if not stage_has[key]:
            status, count = "locked", 0
        elif key == current_key:
            status, count = "current", stage_upcoming[key]  # badge: "● N left"
        elif stage_upcoming[key] == 0:
            status, count = "done", 0
        else:
            status, count = "next", stage_upcoming[key]
        stages.append({"key": key, "label": label, "status": status, "count": count})

    return {
        "meta": {
            "generated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "n_games": len(group_games),
            "n_upcoming": n_upcoming,
            "n_ko_games": sum(len(v) for v in ko_games.values()),
            "source": "Elo+DC scorelines · advancement reused from advancement_data.json",
        },
        "stages": stages,
        "group_games": group_games,
        "r32_projected": r32_projected,
        "r32_games": ko_games["r32"],
        "r16_games": ko_games["r16"],
        "qf_games": ko_games["qf"],
        "sf_games": ko_games["sf"],
        "final_games": ko_games["final"],
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
