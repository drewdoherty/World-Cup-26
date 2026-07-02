#!/usr/bin/env python3
"""READ-ONLY reproduction of the 'xG / total-goals too low' bias.

Loads the canonical fitted Dixon-Coles model (data/dc_params_corrected.json,
the same object DixonColesModel.from_dict consumes across the scripts/* tooling),
predicts the model-implied total goals (lambda_home + lambda_away) for every
played WC2026 fixture, and compares against the REALIZED total goals from
data/processed/wc2026_results.json. Reports a paired test (model_total minus
realized_total per match), bias size, significance and n.

It does NOT touch src/, writes nothing, opens no DB. Pure read + compute.

Why lambda_home + lambda_away is the right model anchor:
  All WC2026 fixtures are neutral-venue, so the card's match-event path
  (src/wca/card.py:1336-1338) calls models.dc.predict(home, away, neutral=True),
  i.e. log lam_h = mu + atk_h - dfc_a, log lam_a = mu + atk_a - dfc_h (gamma=0).
  The Poisson mean total goals is exactly lam_h + lam_a; the tau low-score
  correction is mass-neutral in expectation to <1e-3, so the lambda sum is the
  model's expected total goals. We also report the matrix-derived expected total
  (after tau + max_goals=10 truncation) to quantify any truncation gap.
"""
import json
import math
import os
import sys

import numpy as np
from scipy import stats
from scipy.stats import poisson

ROOT = "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha"
DC_JSON = os.path.join(ROOT, "data", "dc_params_corrected.json")
RESULTS = os.path.join(ROOT, "data", "processed", "wc2026_results.json")


def load_dc():
    with open(DC_JSON) as f:
        d = json.load(f)
    return d


def lambdas(dc, home, away, neutral=True):
    mu = float(dc["mu"])
    gamma = float(dc.get("home_advantage", dc.get("gamma", 0.0)))
    atk = dc["attack"]
    dfc = dc["defence"]
    # mean-zero baseline fallback for unseen teams (none expected here)
    atk_h = float(atk.get(home, 0.0))
    atk_a = float(atk.get(away, 0.0))
    dfc_h = float(dfc.get(home, 0.0))
    dfc_a = float(dfc.get(away, 0.0))
    g = 0.0 if neutral else gamma
    lam_h = math.exp(mu + atk_h - dfc_a + g)
    lam_a = math.exp(mu + atk_a - dfc_h)
    return lam_h, lam_a


def matrix_expected_total(dc, lam_h, lam_a, max_goals=10):
    """Tau-corrected, max_goals-truncated expected total (mirrors score_matrix)."""
    rho = float(dc.get("rho", 0.0))
    goals = np.arange(max_goals + 1)
    ph = poisson.pmf(goals, lam_h)
    pa = poisson.pmf(goals, lam_a)
    mat = np.outer(ph, pa)
    tau = np.ones_like(mat)
    tau[0, 0] = 1.0 - lam_h * lam_a * rho
    tau[0, 1] = 1.0 + lam_h * rho
    tau[1, 0] = 1.0 + lam_a * rho
    tau[1, 1] = 1.0 - rho
    mat = np.clip(mat * tau, 0.0, None)
    mat /= mat.sum()
    rows = np.arange(mat.shape[0])
    cols = np.arange(mat.shape[1])
    eh = float((rows * mat.sum(axis=1)).sum())
    ea = float((cols * mat.sum(axis=0)).sum())
    return eh + ea, mat


def main():
    dc = load_dc()
    teams = set(dc["teams"])
    with open(RESULTS) as f:
        results = json.load(f)["results"]

    rows = []
    skipped = []
    for r in results:
        fx = r.get("fixture", "")
        score = str(r.get("score", ""))
        if " vs " not in fx or "-" not in score:
            skipped.append((fx, score, "no parse"))
            continue
        home, away = [s.strip() for s in fx.split(" vs ")]
        try:
            hg, ag = [int(x) for x in score.split("-")]
        except ValueError:
            skipped.append((fx, score, "bad score"))
            continue
        if home not in teams or away not in teams:
            skipped.append((fx, score, "unseen team"))
            continue
        lam_h, lam_a = lambdas(dc, home, away, neutral=True)
        lam_total = lam_h + lam_a
        mat_total, mat = matrix_expected_total(dc, lam_h, lam_a)
        real_total = hg + ag
        # over 2.5 model prob (from tau matrix) vs realized
        totals = np.add.outer(np.arange(mat.shape[0]), np.arange(mat.shape[1]))
        p_over25 = float(mat[totals > 2.5].sum())
        rows.append(dict(
            fixture=fx, lam_h=lam_h, lam_a=lam_a, lam_total=lam_total,
            mat_total=mat_total, real_total=real_total,
            p_over25=p_over25, over25_real=int(real_total > 2.5),
        ))

    n = len(rows)
    lam_tot = np.array([x["lam_total"] for x in rows])
    mat_tot = np.array([x["mat_total"] for x in rows])
    real_tot = np.array([x["real_total"] for x in rows], dtype=float)

    print("=" * 78)
    print("REPRODUCTION: model-implied vs realized TOTAL GOALS — WC2026 played matches")
    print("=" * 78)
    print(f"DC params: {DC_JSON}")
    print(f"  mu={dc['mu']:.5f}  exp(mu)={math.exp(dc['mu']):.4f}  "
          f"home_adv(gamma)={dc.get('home_advantage', dc.get('gamma')):.5f}  "
          f"rho={dc.get('rho'):.5f}  xi={dc.get('xi'):.5f}")
    print(f"Results: {RESULTS}")
    print(f"n played matched = {n}   skipped = {len(skipped)}")
    if skipped:
        for s in skipped:
            print("   SKIP", s)
    print()

    def report(name, model_tot):
        diff = model_tot - real_tot  # signed: model minus realized
        bias = float(diff.mean())
        sd = float(diff.std(ddof=1))
        se = sd / math.sqrt(n)
        # paired t-test of model_total vs realized_total
        t, p_t = stats.ttest_rel(model_tot, real_tot)
        # Wilcoxon signed-rank (non-parametric)
        try:
            w, p_w = stats.wilcoxon(model_tot - real_tot)
        except Exception:
            w, p_w = float("nan"), float("nan")
        mae = float(np.abs(diff).mean())
        ci_lo, ci_hi = bias - 1.96 * se, bias + 1.96 * se
        print(f"--- {name} ---")
        print(f"  mean model total   = {model_tot.mean():.3f}")
        print(f"  mean realized total= {real_tot.mean():.3f}")
        print(f"  BIAS (model-real)  = {bias:+.3f}  (negative => model TOO LOW)")
        print(f"  95% CI on bias     = [{ci_lo:+.3f}, {ci_hi:+.3f}]")
        print(f"  MAE                = {mae:.3f}   sd(diff)={sd:.3f}  se={se:.3f}")
        print(f"  paired t-test      = t({n-1})={t:+.3f}  p={p_t:.5f}")
        print(f"  Wilcoxon           = W={w:.1f}  p={p_w:.5f}")
        print()
        return bias, p_t, se

    bias_lam, p_lam, se_lam = report("lambda sum (Poisson mean total, production anchor)", lam_tot)
    bias_mat, p_mat, se_mat = report("tau matrix expected total (max_goals=10 truncated)", mat_tot)

    # Calibration of Over 2.5: mean model prob vs realized rate
    pover = np.array([x["p_over25"] for x in rows])
    over_real = np.array([x["over25_real"] for x in rows], dtype=float)
    print("--- Over 2.5 calibration ---")
    print(f"  mean model P(Over2.5) = {pover.mean():.3f}")
    print(f"  realized Over2.5 rate = {over_real.mean():.3f}  ({int(over_real.sum())}/{n})")
    print(f"  gap (real-model)      = {over_real.mean()-pover.mean():+.3f}")
    # binomial test that realized over-rate != model mean
    k = int(over_real.sum())
    bt = stats.binomtest(k, n, pover.mean())
    print(f"  binomial test (k={k}, n={n}, p0={pover.mean():.3f}) p={bt.pvalue:.5f}")
    print()

    # ---- Implied corrective anchor -------------------------------------
    # Model total = 2*exp(mu)*shape where shape ~ mean over matchups of
    # exp(atk_h-dfc_a)+exp(atk_a-dfc_h) all /2 ... empirically:
    scale_needed = real_tot.mean() / lam_tot.mean()
    print("--- Implied corrective anchor ---")
    print(f"  realized/model total ratio = {scale_needed:.4f}")
    print(f"  => to match realized mean, multiply every lambda by {scale_needed:.4f}")
    print(f"  => equivalent mu shift = +{math.log(scale_needed):.4f} "
          f"(new mu = {dc['mu'] + math.log(scale_needed):.4f})")
    print()

    # Per-match dump
    print("--- per-match (sorted by model-real diff) ---")
    for x in sorted(rows, key=lambda r: r["lam_total"] - r["real_total"]):
        print(f"  {x['fixture']:<38} model={x['lam_total']:.2f} "
              f"(lh={x['lam_h']:.2f},la={x['lam_a']:.2f}) "
              f"real={x['real_total']}  d={x['lam_total']-x['real_total']:+.2f}")


if __name__ == "__main__":
    main()
