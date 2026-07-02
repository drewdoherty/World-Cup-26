"""Recompute Shots-on-Target (SoT) empirics from the cached StatsBomb WC
2018+2022 event data, READ-ONLY, to ground the SoT model design.

Reuses wca.data.statsbomb primitives (match_props, player_shares, SOT_OUTCOMES)
so the on-target classification is identical to production. Writes a small JSON
summary under docs/research/wca_alpha_2026/data/ (NOT into production data/).

Run from repo root with the venv python:
    .venv/bin/python docs/research/wca_alpha_2026/scripts/sot_empirics.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from wca.data import statsbomb as sb

CACHE = sb.DEFAULT_CACHE_DIR
OUT = Path("docs/research/wca_alpha_2026/data/sot_empirics.json")


def main():
    match_rows = []
    events_by_match = {}
    for season_id, label in sorted(sb.WC_SEASONS.items()):
        matches = sb.fetch_matches(sb.WC_COMPETITION_ID, season_id, cache_dir=CACHE)
        for m in matches:
            mid = m["match_id"]
            try:
                events = sb.fetch_events(mid, cache_dir=CACHE)
            except Exception:
                continue
            events_by_match[mid] = events
            home = (m.get("home_team") or {}).get("home_team_name")
            away = (m.get("away_team") or {}).get("away_team_name")
            props = sb.match_props(events, home_team=home, away_team=away)
            row = {"match_id": mid, "season": label, "home": home, "away": away}
            row.update(props)
            match_rows.append(row)

    mdf = pd.DataFrame(match_rows)
    # team-side long frame
    sides = []
    for _, r in mdf.iterrows():
        for sfx in ("_home", "_away"):
            sides.append({
                "shots": r["shots" + sfx], "sot": r["sot" + sfx],
                "goals": r["goals" + sfx], "xg": r["xg" + sfx],
            })
    sdf = pd.DataFrame(sides)

    total_sot = mdf["sot_home"] + mdf["sot_away"]
    total_shots = mdf["shots_home"] + mdf["shots_away"]
    total_goals = mdf["goals_home"] + mdf["goals_away"]
    total_xg = mdf["xg_home"] + mdf["xg_away"]

    def moments(x):
        x = np.asarray(x, dtype=float)
        return {
            "n": int(x.size), "mean": float(x.mean()), "var": float(x.var(ddof=1)),
            "var_mean_ratio": float(x.var(ddof=1) / x.mean()) if x.mean() else None,
            "nb_k_mm": (float(x.mean() ** 2 / (x.var(ddof=1) - x.mean()))
                        if x.var(ddof=1) > x.mean() else None),
            "min": float(x.min()), "max": float(x.max()),
            "p10": float(np.percentile(x, 10)), "p50": float(np.percentile(x, 50)),
            "p90": float(np.percentile(x, 90)),
        }

    res = {
        "n_matches": int(len(mdf)),
        "match_total_sot": moments(total_sot),
        "match_total_shots": moments(total_shots),
        "team_sot": moments(sdf["sot"]),
        "team_shots": moments(sdf["shots"]),
        "conversions": {
            "sot_per_shot": float(sdf["sot"].sum() / sdf["shots"].sum()),
            "goals_per_sot": float(sdf["goals"].sum() / sdf["sot"].sum()),
            "goals_per_shot": float(sdf["goals"].sum() / sdf["shots"].sum()),
            "sot_per_xg": float(sdf["sot"].sum() / sdf["xg"].sum()),
            "goals_per_xg": float(sdf["goals"].sum() / sdf["xg"].sum()),
        },
        "correlations_team_side": {
            "sot_vs_xg": float(np.corrcoef(sdf["sot"], sdf["xg"])[0, 1]),
            "sot_vs_shots": float(np.corrcoef(sdf["sot"], sdf["shots"])[0, 1]),
            "sot_vs_goals": float(np.corrcoef(sdf["sot"], sdf["goals"])[0, 1]),
            "shots_vs_xg": float(np.corrcoef(sdf["shots"], sdf["xg"])[0, 1]),
        },
        "correlations_match_total": {
            "sot_vs_xg": float(np.corrcoef(total_sot, total_xg)[0, 1]),
            "sot_vs_goals": float(np.corrcoef(total_sot, total_goals)[0, 1]),
        },
    }

    # Linear regression team SoT ~ team xG (for the elasticity / slope estimate)
    x = np.asarray(sdf["xg"], dtype=float)
    y = np.asarray(sdf["sot"], dtype=float)
    A = np.vstack([x, np.ones_like(x)]).T
    slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
    # mean-relative elasticity: d(log sot)/d(log xg) at the means
    xbar, ybar = x.mean(), y.mean()
    res["team_sot_on_xg_regression"] = {
        "slope": float(slope), "intercept": float(intercept),
        "elasticity_at_mean": float(slope * xbar / ybar),
    }

    # Player-level SoT empirics (top shot-takers, per-90 distribution)
    pdf = sb.player_shares(events_by_match)
    pdf = pdf[pdf["minutes"].notna() & (pdf["minutes"] > 0)].copy()
    pdf["sot_p90"] = pdf["sot"] * 90.0 / pdf["minutes"]
    pdf["on_target_rate"] = np.where(pdf["shots"] > 0, pdf["sot"] / pdf["shots"], np.nan)
    sig = pdf[pdf["minutes"] >= 180].copy()  # non-thin
    res["player_sot"] = {
        "n_players_with_minutes": int(len(pdf)),
        "n_players_non_thin_180min": int(len(sig)),
        "sot_p90_mean_nonthin": float(sig["sot_p90"].mean()),
        "sot_p90_p50_nonthin": float(sig["sot_p90"].median()),
        "sot_p90_p90_nonthin": float(sig["sot_p90"].quantile(0.9)),
        "on_target_rate_mean": float(sig.loc[sig["shots"] >= 5, "on_target_rate"].mean()),
        "on_target_rate_pooled": float(pdf["sot"].sum() / pdf["shots"].sum()),
    }
    # top SoT/90 players (min 270 mins) for sanity
    top = sig[sig["minutes"] >= 270].nlargest(12, "sot_p90")[
        ["player", "team", "minutes", "shots", "sot", "sot_p90", "on_target_rate"]]
    res["top_sot_p90_players"] = json.loads(top.to_json(orient="records"))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(res, fh, indent=2)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
