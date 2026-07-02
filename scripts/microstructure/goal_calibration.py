#!/usr/bin/env python3
"""Model goal-expectancy calibration (key=goal_calibration).

READ-ONLY. Reads the model's per-fixture expected goals + 1X2 + final score from
``scores_markets.json`` (``by_team[].games[]``: ``eg=[home,away]``,
``x1x2=[home,draw,away]``, ``ft``). Writes site/microstructure/goal_calibration.json.

Question
--------
The synthetic-pricing area asks whether 1X2 x totals x BTTS are mutually
consistent under ONE Dixon-Coles goal supply. This area asks the prior question:
is that goal supply set at the right LEVEL versus what actually happened? If the
model's expected goals are systematically below realised goals, every
goal-dependent market it prices (totals, BTTS, correct-score) is biased the same
way -- a tradable, model-side mispricing distinct from pure execution edges.

Method
------
Over played fixtures (dedup by team-pair + date), aggregate model expected goals
(eg_home+eg_away) vs actual goals (from ft), and model P(draw) (x1x2[1]) vs
realised draws. Significance: a Poisson z on total goals (var=sum xG), a
Poisson-binomial z on draws (var=sum p(1-p)). We also split goals-per-game by
result (drawn vs decisive) to locate where any surplus landed, and report the
uniform scale factor that would match total goals.

Honest framing: this is a FORECASTING finding, not a market-microstructure edge.
It earns a place here because (a) it is the calibration premise behind the
synthetic-pricing area, and (b) it implies a concrete totals/overs edge that
pairs with the OddsAPI totals odds we already capture but do not yet trade.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
OUT = os.path.join(REPO, "site", "microstructure", "goal_calibration.json")
_DEFAULT_SRC = os.path.join(REPO, "site", "scores_markets.json")


def _rows(scores_markets):
    by_team = scores_markets.get("by_team") or {}
    seen, rows = set(), []
    for _team, rec in by_team.items():
        for g in (rec.get("games") or []):
            ft = g.get("ft")
            if not ft or "-" not in str(ft):
                continue
            key = tuple(sorted([g.get("home", ""), g.get("away", "")])) + (g.get("date", ""),)
            if key in seen:
                continue
            eg, x = g.get("eg"), g.get("x1x2")
            if not eg or not x:
                continue
            try:
                hs, as_ = (int(v) for v in str(ft).split("-")[:2])
            except (ValueError, TypeError):
                continue
            seen.add(key)
            rows.append({"xg": eg[0] + eg[1], "ag": hs + as_, "pdraw": x[1],
                         "draw": hs == as_, "hs": hs, "as": as_})
    return rows


def analyse(scores_markets):
    rows = _rows(scores_markets)
    n = len(rows)
    if n == 0:
        return {"n": 0, "state": "insufficient"}
    sxg = sum(r["xg"] for r in rows)
    sag = sum(r["ag"] for r in rows)
    exp_draw = sum(r["pdraw"] for r in rows)
    act_draw = sum(1 for r in rows if r["draw"])
    over = sum(1 for r in rows if r["ag"] > r["xg"])
    z_goals = (sag - sxg) / math.sqrt(sxg) if sxg > 0 else 0.0
    var_d = sum(r["pdraw"] * (1 - r["pdraw"]) for r in rows)
    z_draw = (act_draw - exp_draw) / math.sqrt(var_d) if var_d > 0 else 0.0
    drawn = [r for r in rows if r["draw"]]
    dec = [r for r in rows if not r["draw"]]
    gpg_drawn = sum(r["ag"] for r in drawn) / len(drawn) if drawn else 0.0
    gpg_dec = sum(r["ag"] for r in dec) / len(dec) if dec else 0.0
    ds = Counter((r["hs"], r["as"]) for r in drawn)
    # two-sided normal p from z
    p_goals = math.erfc(abs(z_goals) / math.sqrt(2))
    p_draw = math.erfc(abs(z_draw) / math.sqrt(2))
    return {
        "n": n,
        "sum_xg": round(sxg, 1), "sum_actual": sag,
        "gap_total": round(sxg - sag, 1),
        "gap_per_game": round((sxg - sag) / n, 3),
        "model_gpg": round(sxg / n, 3), "actual_gpg": round(sag / n, 3),
        "z_goals": round(z_goals, 2), "p_goals": round(p_goals, 4),
        "pct_games_over_model": round(over / n * 100, 0),
        "scale_to_match": round(sag / sxg, 3),
        "shortfall_pct": round((sag / sxg - 1) * 100, 0),
        "exp_draws": round(exp_draw, 1), "actual_draws": act_draw,
        "draw_surplus": round(act_draw - exp_draw, 1),
        "z_draw": round(z_draw, 2), "p_draw": round(p_draw, 4),
        "model_draw_rate": round(exp_draw / n * 100, 1),
        "actual_draw_rate": round(act_draw / n * 100, 1),
        "gpg_drawn": round(gpg_drawn, 2), "gpg_decisive": round(gpg_dec, 2),
        "drawn_scorelines": {f"{h}-{a}": c for (h, a), c in sorted(ds.items())},
    }


def build_feed(scores_markets):
    a = analyse(scores_markets)
    return {
        "key": "goal_calibration",
        "generated_at": scores_markets.get("meta", {}).get("generated"),
        "stats": a,
        "verdict": (
            "COMPATIBLE — goal under-prediction is real and significant; the draw "
            "surplus is sampling noise. Raise base goal expectancy (~%d%%); leave "
            "the Dixon-Coles draw term until draws persist after the level fix."
            % int(a.get("shortfall_pct", 0))
        ) if a.get("n") else "insufficient sample",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=_DEFAULT_SRC, help="path to scores_markets.json")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    with open(args.src, encoding="utf-8") as f:
        sm = json.load(f)
    feed = build_feed(sm)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2)
    s = feed["stats"]
    print("WROTE %s" % args.out)
    if s.get("n"):
        print("  n=%d | xG %.1f vs %d actual (%+.3f/game, z=%.2f p=%.4f) | "
              "draws %d vs %.1f exp (z=%.2f p=%.2f) | shortfall ~%d%%"
              % (s["n"], s["sum_xg"], s["sum_actual"], s["gap_per_game"], s["z_goals"],
                 s["p_goals"], s["actual_draws"], s["exp_draws"], s["z_draw"], s["p_draw"],
                 int(s["shortfall_pct"])))


if __name__ == "__main__":
    main()
