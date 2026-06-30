#!/usr/bin/env python
"""Empirical grounding for the corners / cards / fouls match-event models.

READ-ONLY. Reads data/processed/props_matches.csv (StatsBomb WC2018+2022,
128 matches, per-team corners/yellows/reds/fouls/shots/goals/xg) and reports:

  * base rates + NB dispersions (var/mean -> k)
  * match-level correlations (corner/card/foul vs xG, goals, shots)
  * foul -> card conversion and cards ~ a + b*fouls regression
  * knockout vs group multipliers
  * empirical-Bayes shrinkage constants for team priors
  * pairing (attack-for x opp-concede) vs xG-elasticity corner prediction

Run from repo root:
    .venv/bin/python docs/research/wca_alpha_2026/scripts/corners_cards_fouls_empirics.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "data" / "processed" / "props_matches.csv"


def long_form(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in df.iterrows():
        rows.append(dict(team=r.home, opp=r.away, cf=r.corners_home, ca=r.corners_away,
                         fouls=r.fouls_home, cards=r.yellows_home + r.reds_home))
        rows.append(dict(team=r.away, opp=r.home, cf=r.corners_away, ca=r.corners_home,
                         fouls=r.fouls_away, cards=r.yellows_away + r.reds_away))
    return pd.DataFrame(rows)


def nb_k(s: pd.Series) -> float:
    m, v = s.mean(), s.var()
    return m * m / (v - m) if v > m else float("inf")


def main() -> int:
    df = pd.read_csv(CSV)
    df["corners"] = df.corners_home + df.corners_away
    df["cards"] = df.yellows_home + df.yellows_away + df.reds_home + df.reds_away
    df["fouls"] = df.fouls_home + df.fouls_away
    df["goals"] = df.goals_home + df.goals_away
    df["xg"] = df.xg_home + df.xg_away
    df["shots"] = df.shots_home + df.shots_away

    print(f"N = {len(df)} matches  ({df.season.value_counts().to_dict()})")
    for c in ["corners", "cards", "fouls", "goals", "xg", "shots"]:
        s = df[c]
        print(f"  {c:8s} mean={s.mean():6.3f} var={s.var():6.3f} v/m={s.var()/s.mean():.3f} NB_k={nb_k(s):7.1f}")

    print("\nMatch-level correlations:")
    for a, b in [("corners", "xg"), ("corners", "goals"), ("corners", "shots"),
                 ("cards", "fouls"), ("cards", "xg"), ("fouls", "xg"), ("fouls", "corners")]:
        print(f"  corr({a},{b}) = {df[a].corr(df[b]):+.3f}")

    yel = (df.yellows_home + df.yellows_away).sum()
    red = (df.reds_home + df.reds_away).sum()
    fl = df.fouls.sum()
    print(f"\nFoul->card: yellows={yel} reds={red} fouls={fl} | cards/foul={(yel+red)/fl:.4f}")
    b, a = np.polyfit(df.fouls, df.cards, 1)
    print(f"cards ~ {a:.3f} + {b:.4f}*fouls")

    df["date"] = pd.to_datetime(df.date)
    df["ko"] = df.apply(lambda r: r.date >= (pd.Timestamp("2018-06-30") if r.season == "WC2018"
                                             else pd.Timestamp("2022-12-03")), axis=1)
    g, k = df[~df.ko], df[df.ko]
    print(f"\nKnockout mult: cards={k.cards.mean()/g.cards.mean():.3f} "
          f"fouls={k.fouls.mean()/g.fouls.mean():.3f} corners={k.corners.mean()/g.corners.mean():.3f}")

    L = long_form(df)
    print("\nEmpirical-Bayes shrinkage (within/between var -> pseudo-count K matches):")
    for c in ["cf", "fouls", "cards"]:
        tm = L.groupby("team")[c].mean()
        within = L.groupby("team")[c].var().mean()
        between = tm.var()
        n = L.groupby("team")[c].size().mean()
        K = within / max(between - within / n, 1e-6)
        print(f"  {c:6s} between={between:.3f} within={within:.3f} K~{K:.1f}")

    att = L.groupby("team").cf.mean()
    con = L.groupby("opp").cf.mean()
    mu = L.cf.mean()
    df["cpred_pair"] = df.apply(lambda r: max(att[r.home] + con[r.away] - mu, 0)
                                + max(att[r.away] + con[r.home] - mu, 0), axis=1)
    df["cpred_xg"] = 8.97 * (1 + 0.3 * (df.xg / 3.07 - 1))
    print(f"\nTotal-corner prediction (in-sample corr w/ actual):")
    print(f"  pairing (att_for x opp_concede) r={df.cpred_pair.corr(df.corners):.3f}")
    print(f"  xG-elasticity (current model)   r={df.cpred_xg.corr(df.corners):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
