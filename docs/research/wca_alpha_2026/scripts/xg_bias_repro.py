"""Reproduce the xG-too-low bias: model-implied total goals vs realized.

READ-ONLY. Imports wca primitives, reads data/ read-only. Writes nothing to src
or any .db. Refits Dixon-Coles on the pre-tournament history (reference_date =
2026-06-10, so NO WC2026 result leaks into the fit) and compares the model's
implied total goals (lambda_home + lambda_away) against the realized total goals
for every played WC2026 fixture.

Run:
  cd "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha"
  PYTHONPATH=src .venv/bin/python docs/research/wca_alpha_2026/scripts/xg_bias_repro.py
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha"
sys.path.insert(0, os.path.join(ROOT, "src"))

from wca.card import fit_models  # noqa: E402
from wca.models.scores import over_under_from_matrix as totals_over_under  # noqa: E402

REF_DATE = "2026-06-10"  # fit cutoff: strictly before the first WC2026 match (06-11)


def load_results() -> pd.DataFrame:
    df = pd.read_csv(os.path.join(ROOT, "data/raw/results.csv"))
    return df


def realized_fixtures():
    """List of (home, away, neutral, total_goals, home_goals, away_goals) from
    the realized WC2026 results, matched back to results.csv for the neutral flag
    and canonical team names."""
    res = json.load(open(os.path.join(ROOT, "data/processed/wc2026_results.json")))["results"]
    df = load_results()
    # Index WC2026 rows (date >= 2026-06-11) by (home, away) for neutral + names.
    wc = df[df["date"] >= "2026-06-11"].copy()
    out = []
    for r in res:
        sc = r.get("score", "")
        if not sc or "-" not in sc:
            continue
        try:
            hg, ag = sc.split("-")
            hg, ag = int(hg), int(ag)
        except Exception:
            continue
        fx = r["fixture"]
        if " vs " not in fx:
            continue
        home, away = fx.split(" vs ", 1)
        out.append({
            "fixture": fx, "home": home, "away": away,
            "home_goals": hg, "away_goals": ag, "total": hg + ag,
        })
    return out


def main():
    df = load_results()
    models = fit_models(df, reference_date=REF_DATE)
    dc = models.dc
    print(f"DC fit: mu={dc.mu:.4f} -> exp(mu)={math.exp(dc.mu):.3f}  "
          f"home_adv(gamma)={dc.home_advantage:.4f}  n_matches={models.n_matches}")
    print(f"Implied baseline total goals (2*exp(mu), neutral, avg teams) "
          f"~ {2*math.exp(dc.mu):.3f}\n")

    rows = realized_fixtures()
    recs = []
    skipped = []
    for fx in rows:
        try:
            # WC2026 = neutral venue tournament.
            lam_h, lam_a = dc.expected_lambdas(fx["home"], fx["away"], neutral=True, warn=False)
        except Exception as e:
            skipped.append((fx["fixture"], str(e)))
            continue
        pred_total = lam_h + lam_a
        recs.append({**fx, "lam_h": lam_h, "lam_a": lam_a, "pred_total": pred_total})

    n = len(recs)
    pred = np.array([r["pred_total"] for r in recs], dtype=float)
    actual = np.array([r["total"] for r in recs], dtype=float)
    diff = pred - actual  # model minus realized; negative => model UNDER-predicts

    mean_pred = pred.mean()
    mean_act = actual.mean()
    bias = diff.mean()
    sd = diff.std(ddof=1)
    se = sd / math.sqrt(n)
    t = bias / se
    # two-sided p via normal approx (n is small-ish; report t and df)
    from scipy import stats
    p_two = 2 * stats.t.sf(abs(t), df=n - 1)
    # one-sided: H1 = model under-predicts (bias < 0)
    p_one = stats.t.cdf(t, df=n - 1)

    print(f"n played fixtures used         : {n}  (skipped {len(skipped)})")
    print(f"mean model-implied total goals : {mean_pred:.3f}")
    print(f"mean realized total goals      : {mean_act:.3f}")
    print(f"mean bias (model - realized)   : {bias:.3f}  goals/match")
    print(f"sd of per-match diff           : {sd:.3f}")
    print(f"paired t-stat (H0: bias=0)     : t={t:.3f}, df={n-1}")
    print(f"  two-sided p                  : {p_two:.4f}")
    print(f"  one-sided p (model UNDER)    : {p_one:.4f}")

    # Wilcoxon signed-rank as a non-parametric robustness check.
    try:
        w_stat, w_p = stats.wilcoxon(pred, actual, alternative="less")
        print(f"Wilcoxon signed-rank (model<actual one-sided): stat={w_stat:.1f}, p={w_p:.4f}")
    except Exception as e:
        print("Wilcoxon failed:", e)

    # Over/Under 2.5 directional check using the RECONCILED matrix vs realized.
    # Build per-fixture P(Over 2.5) using the blended 1X2 from the prediction log
    # where available; here we use the DC-only 1X2 (matrix-implied) for a clean
    # model-internal read of how often the model's Over 2.5 prob underweights the
    # realized Over rate.
    over_probs = []
    over_real = []
    for r in recs:
        mat, lh, la = dc.score_matrix(r["home"], r["away"], neutral=True, warn=False)
        p_over, p_under = totals_over_under(mat, 2.5)
        over_probs.append(p_over)
        over_real.append(1.0 if r["total"] >= 3 else 0.0)
    over_probs = np.array(over_probs)
    over_real = np.array(over_real)
    print(f"\nO/U 2.5 (DC matrix, neutral):")
    print(f"  mean model P(Over2.5)        : {over_probs.mean():.3f}")
    print(f"  realized Over2.5 rate        : {over_real.mean():.3f}")
    print(f"  calibration gap (real-pred)  : {over_real.mean()-over_probs.mean():+.3f}")

    print("\nPer-fixture detail (model_total | realized_total | diff):")
    for r in sorted(recs, key=lambda x: x["pred_total"] - x["total"]):
        print(f"  {r['fixture']:<40} {r['pred_total']:.2f} | {r['total']} | "
              f"{r['pred_total']-r['total']:+.2f}")

    if skipped:
        print("\nSkipped (unseen team / fit error):")
        for fxname, err in skipped:
            print(f"  {fxname}: {err}")

    # Persist a small data artifact for the writeup.
    out = {
        "ref_date": REF_DATE,
        "dc_mu": dc.mu, "exp_mu": math.exp(dc.mu),
        "home_adv": dc.home_advantage,
        "n": n, "mean_pred_total": mean_pred, "mean_actual_total": mean_act,
        "bias": bias, "sd_diff": sd, "t_stat": t, "p_two": p_two, "p_one": p_one,
        "mean_p_over25": float(over_probs.mean()), "real_over25": float(over_real.mean()),
        "fixtures": recs,
    }
    outdir = os.path.join(ROOT, "docs/research/wca_alpha_2026/data")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "xg_bias_repro.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {outdir}/xg_bias_repro.json")


if __name__ == "__main__":
    main()
