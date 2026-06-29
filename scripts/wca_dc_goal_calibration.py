#!/usr/bin/env python3
"""Calibrate a Dixon-Coles goal-supply boost from played WC2026 fixtures.

Uses the model's already-stored per-game expected goals (eg=[lh,la]) and 1X2
(x1x2) from scores_markets.json + final scores (ft). Evaluates a grid of
multiplicative boosts b on lambda, scoring each by the SCORELINE log-loss
(DC tau likelihood at the observed score), the 1X2 log-loss/Brier, and O/U 2.5,
plus a walk-forward (expanding-window) check that estimates b from prior games
only. Read-only; prints a report.
"""
import json, math, sys
import numpy as np
from scipy.stats import poisson

SM = sys.argv[1] if len(sys.argv) > 1 else "site/scores_markets.json"
RHO = -0.05018674762581003
MAXG = 10


def rows():
    d = json.load(open(SM))
    seen, out = set(), []
    for _t, rec in (d.get("by_team") or {}).items():
        for g in (rec.get("games") or []):
            ft = g.get("ft"); eg = g.get("eg"); x = g.get("x1x2")
            if not ft or "-" not in str(ft) or not eg or not x:
                continue
            key = tuple(sorted([g.get("home",""), g.get("away","")])) + (g.get("date",""),)
            if key in seen:
                continue
            try:
                hs, as_ = (int(v) for v in str(ft).split("-")[:2])
            except (ValueError, TypeError):
                continue
            seen.add(key)
            out.append({"date": g.get("date",""), "lh": eg[0], "la": eg[1],
                        "hs": min(hs, MAXG), "as": min(as_, MAXG)})
    out.sort(key=lambda r: r["date"])
    return out


def score_matrix(lh, la, b):
    lh, la = lh * b, la * b
    goals = np.arange(MAXG + 1)
    mat = np.outer(poisson.pmf(goals, lh), poisson.pmf(goals, la))
    # DC tau on the 4 low cells
    tau = np.ones_like(mat)
    tau[0, 0] = 1 - lh * la * RHO
    tau[0, 1] = 1 + lh * RHO
    tau[1, 0] = 1 + la * RHO
    tau[1, 1] = 1 - RHO
    mat = np.clip(mat * tau, 0, None)
    return mat / mat.sum()


def metrics(rs, b):
    sll = oll = x_ll = 0.0; bri = 0.0; n = len(rs)
    se_xg = 0.0
    for r in rs:
        m = score_matrix(r["lh"], r["la"], b)
        p_score = max(m[r["hs"], r["as"]], 1e-12)
        sll += -math.log(p_score)
        # 1X2
        ph = np.tril(m, -1).sum(); pa = np.triu(m, 1).sum(); pd = np.trace(m)
        outcome = "h" if r["hs"] > r["as"] else ("a" if r["as"] > r["hs"] else "d")
        p_out = {"h": ph, "d": pd, "a": pa}[outcome]
        x_ll += -math.log(max(p_out, 1e-12))
        # Brier on the realised 1X2 vector
        y = {"h": (1,0,0), "d": (0,1,0), "a": (0,0,1)}[outcome]
        bri += sum((p - yy) ** 2 for p, yy in zip((ph, pd, pa), y))
        # O/U 2.5
        gi, gj = np.indices(m.shape)
        p_over = m[(gi + gj) >= 3].sum()
        over = (r["hs"] + r["as"]) >= 3
        p_ou = p_over if over else (1 - p_over)
        oll += -math.log(max(p_ou, 1e-12))
    return {"scoreline_ll": sll / n, "x12_ll": x_ll / n, "ou_ll": oll / n,
            "x12_brier": bri / n}


def main():
    rs = rows(); n = len(rs)
    print(f"n={n} played fixtures\n")
    print("FIXED-BOOST grid (in-sample mean log-loss; lower=better):")
    print(f"{'boost':>6} {'scoreline':>10} {'1X2':>8} {'O/U2.5':>8} {'1X2_Brier':>10}")
    grid = [1.00, 1.05, 1.10, 1.15, 1.20, 1.24, 1.30]
    best_b, best_sll = 1.0, 9e9
    for b in grid:
        m = metrics(rs, b)
        flag = ""
        if m["scoreline_ll"] < best_sll:
            best_sll, best_b = m["scoreline_ll"], b
        print(f"{b:>6.2f} {m['scoreline_ll']:>10.4f} {m['x12_ll']:>8.4f} {m['ou_ll']:>8.4f} {m['x12_brier']:>10.4f}")
    # finer MLE search
    bs = np.linspace(1.0, 1.4, 81)
    slls = [metrics(rs, b)["scoreline_ll"] for b in bs]
    b_mle = float(bs[int(np.argmin(slls))])
    # total-goals MLE (closed form ignoring tau)
    tot_act = sum(r["hs"] + r["as"] for r in rs)
    tot_exp = sum(r["lh"] + r["la"] for r in rs)
    print(f"\nscoreline-LL optimal boost = {b_mle:.3f}")
    print(f"total-goals MLE boost      = {tot_act/tot_exp:.3f}  ({tot_act} actual / {tot_exp:.1f} expected)")

    # walk-forward: estimate b from games before i (warmup 12), score game i
    print("\nWALK-FORWARD (expanding window, warmup=12): mean scoreline log-loss on held-out game i")
    warm = 12
    sll_wf = sll_base = 0.0; cnt = 0
    for i in range(warm, n):
        prev = rs[:i]
        ta = sum(r["hs"] + r["as"] for r in prev); te = sum(r["lh"] + r["la"] for r in prev)
        b_i = ta / te if te > 0 else 1.0
        m = score_matrix(rs[i]["lh"], rs[i]["la"], b_i)
        sll_wf += -math.log(max(m[rs[i]["hs"], rs[i]["as"]], 1e-12))
        m0 = score_matrix(rs[i]["lh"], rs[i]["la"], 1.0)
        sll_base += -math.log(max(m0[rs[i]["hs"], rs[i]["as"]], 1e-12))
        cnt += 1
    print(f"  adaptive boost : {sll_wf/cnt:.4f}")
    print(f"  no boost (1.0) : {sll_base/cnt:.4f}")
    print(f"  improvement    : {(sll_base-sll_wf)/cnt:+.4f} nats/game ({(1-sll_wf/sll_base)*100:+.1f}%)")

    # shrunk recommendation: halve the log-boost toward 1.0 for regime-change risk
    b_shrunk = math.exp(0.5 * math.log(b_mle))
    print(f"\nRECOMMENDED boost (0.5x log-shrink for knockout regime risk): {b_shrunk:.3f}")
    print(f"  (full MLE {b_mle:.3f} -> shrunk {b_shrunk:.3f}; applies multiplicatively to both lambdas)")


if __name__ == "__main__":
    main()
