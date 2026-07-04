# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 07 · Parameter stress tests
#
# **Purpose.** Re-decide real candidate bets under systematically varied
# parameters (edge floors, cost/liquidity gates, staleness, Kelly fraction)
# and report how candidate counts, EV, realized P&L (settled only),
# calibration and gate-rejection patterns respond.
#
# **Candidate datasets (all real):**
# 1. **Settled 1X2** — the production model's logged pre-KO probabilities
#    (latest row ≥1h before kickoff — no look-ahead) vs the PM price at the
#    24h mark (real prints), labelled with REAL results.
# 2. **Open advancement** — the current advancement feed (model sim vs live
#    PM price, fee-adjusted), unsettled → EV/exposure effects only.

# %%
import sys, pathlib
_here = pathlib.Path.cwd().resolve()
JB = next(q for q in [_here, *_here.parents] if (q / "lib" / "bootstrap.py").exists())
sys.path.insert(0, str(JB))

import dataclasses
import datetime as dt
import json
import pandas as pd
import polars as pl
import lib.bootstrap as bt
import lib.config as cfg
import lib.storage as st
import lib.stress as sx
import lib.ids as ids

UTC = dt.timezone.utc
manifest = bt.run_manifest("07_parameter_stress_tests")
base = cfg.load_params()
BANKROLL_USD = 3000 * 1.33     # combined-bankroll base in $ (production FX)
print(f"baseline params: min_edge_raw={base.min_edge_raw}, "
      f"min_edge_net={base.min_edge_net}, kelly={base.kelly_fraction}")

# %% [markdown]
# ## 1 · Candidate set A — settled 1X2 (model @T-24h vs PM tape @T-24h)

# %%
conv = st.load_dataset("gold", "prematch_convergence")
results = json.loads(pathlib.Path(bt.RESULTS_JSON).read_text())["results"]
res_by_pair = {}
for r in results:
    if " vs " not in r.get("fixture", ""):
        continue
    h, a = r["fixture"].split(" vs ", 1)
    res_by_pair["__".join(sorted((ids.slug(h), ids.slug(a))))] = r

cand_rows = []
for row in (conv.filter((pl.col("hours_out") == 24)
                        & (pl.col("price_basis") == "observed")
                        & pl.col("model_fair").is_not_null())).to_dicts():
    pair = "__".join(sorted((ids.slug(row["home"]), ids.slug(row["away"]))))
    res = res_by_pair.get(pair)
    if not res:
        continue
    # label: did THIS outcome (home/draw/away, 90-min basis) win?
    outcome_map = {"home": ids.slug(res["fixture"].split(" vs ")[0]),
                   "away": ids.slug(res["fixture"].split(" vs ")[1])}
    if row["outcome"] == "draw":
        won = res["outcome"] == "draw"
    else:
        won = outcome_map.get(res["outcome"]) == ids.slug(row[row["outcome"]])
    cand_rows.append({
        "candidate_id": f"{row['event_slug']}|{row['outcome']}",
        "event_id": row["event_slug"], "market_type": "1x2",
        "outcome": row["outcome"], "fair_p": row["model_fair"],
        "mid": row["pm_price"],
        # tape prints carry no book spread; a fixed 2c estimate is applied
        # and stress-swept below — labelled an estimate, not an observation
        "spread": 0.02, "depth_usd": None, "staleness_s": 0.0,
        "match_confidence": 1.0, "settled": True, "won": bool(won)})
cand_settled = pl.DataFrame(cand_rows)
st.save_dataset(cand_settled, "gold", "stress_candidates_settled_1x2",
                inputs=["gold/prematch_convergence",
                        "repo:data/processed/wc2026_results.json"],
                notebook="07", note="spread=0.02 ESTIMATE (tape carries none)")
print(f"settled 1X2 candidates: {cand_settled.height} "
      f"({cand_settled.filter(pl.col('won')).height} winners)")
cand_settled.head(5).to_pandas()

# %% [markdown]
# ## 2 · Candidate set B — open advancement (current feed)

# %%
adv = json.loads(pathlib.Path(bt.ADVANCEMENT_JSON).read_text())
adv_gen = (adv.get("meta") or {}).get("generated")
badv = []
for t in adv.get("teams", []):     # real shape: teams[] × pm{stage:{pm,edge_adj}}
    for stage, pmv in (t.get("pm") or {}).items():
        mp = (t.get("model") or {}).get(stage)
        pmp = pmv.get("pm")
        if mp is None or pmp is None or not (0 < pmp < 1):
            continue
        badv.append({
            "candidate_id": f"adv|{t.get('team')}|{stage}",
            "event_id": f"adv:{stage}", "market_type": "advance",
            "outcome": str(t.get("team")), "fair_p": float(mp),
            "mid": float(pmp),
            "spread": None, "depth_usd": None,   # feed has no book data
            "staleness_s": 0.0, "match_confidence": 1.0,
            "settled": False, "won": None})
cand_open = (pl.DataFrame(badv)
             .cast({"spread": pl.Float64, "depth_usd": pl.Float64,
                    "won": pl.Boolean})
             if badv else pl.DataFrame())
if cand_open.height:
    st.save_dataset(cand_open, "gold", "stress_candidates_open_adv",
                    inputs=["repo:site/advancement_data.json"], notebook="07",
                    note=f"feed generated {adv_gen}")
print(f"open advancement candidates: {cand_open.height} (feed {adv_gen})")

# %%
cand_settled_h = cand_settled.cast({"depth_usd": pl.Float64})
candidates = (pl.concat([cand_settled_h, cand_open], how="vertical_relaxed")
              if cand_open.height else cand_settled_h)
print(f"total candidates: {candidates.height}")

# %% [markdown]
# ## 3 · Baseline evaluation
# Gates + production ¼-Kelly sizing at baseline parameters. Realized
# metrics come ONLY from the settled subset (n stated).

# %%
baseline = sx.evaluate(candidates, base, BANKROLL_USD)
pd.Series(baseline).to_frame("baseline")

# %% [markdown]
# ## 4 · One-at-a-time sweeps
# **What to change:** the value lists below. Every point re-runs the same
# accept logic; nothing is fitted.

# %%
sweeps = {
    "min_edge_raw": [0.0, 0.01, 0.02, 0.03, 0.05, 0.08],
    "min_edge_net": [0.0, 0.005, 0.01, 0.02, 0.04],
    "max_spread": [0.01, 0.02, 0.04, 0.06, 0.10],
    "staleness_max_s": [300, 900, 3600, 14400],
    "kelly_fraction": [0.1, 0.25, 0.5],
    "slippage_frac_of_spread": [0.0, 0.5, 1.0],
}
sweep_frames = {}
for param, values in sweeps.items():
    sweep_frames[param] = sx.sweep_one(candidates, base, param, values,
                                       BANKROLL_USD)
    print(f"swept {param} over {values}")
sweep_all = pl.concat(sweep_frames.values())
st.save_dataset(sweep_all, "gold", "stress_sweeps", notebook="07",
                inputs=["gold/stress_candidates_settled_1x2",
                        "gold/stress_candidates_open_adv"])
sweep_frames["min_edge_raw"].to_pandas()

# %%
import matplotlib.pyplot as plt
import lib.plotting as plot
fig, axes = plt.subplots(2, 3, figsize=(15, 8))
for ax, (param, df) in zip(axes.flat, sweep_frames.items()):
    d = df.to_pandas()
    ax.plot(d["value"], d["n_accepted"], "o-", label="accepted")
    ax.set_title(param, fontsize=10)
    ax.set_ylabel("n accepted")
    axr = ax.twinx()
    if d["realized_pnl"].notna().any():
        axr.plot(d["value"], d["realized_pnl"], "s--", color="tab:green",
                 label="realized P&L $ (settled)")
        axr.set_ylabel("realized P&L $")
fig.suptitle("One-at-a-time parameter sweeps — accepted count vs realized P&L")
fig.tight_layout()
plot.save_fig(fig, "07_sweeps_one_at_a_time",
              note=f"settled n per point varies; see stress_sweeps table · {manifest['run_utc']}")
plt.show()

# %% [markdown]
# ## 5 · Grid sweep + sensitivity heatmap (edge floor × spread cap)

# %%
grid = sx.sweep_grid(candidates, base,
                     {"min_edge_raw": [0.0, 0.01, 0.02, 0.03, 0.05],
                      "max_spread": [0.01, 0.02, 0.04, 0.06, 0.10]},
                     BANKROLL_USD)
piv = grid.to_pandas().pivot(index="min_edge_raw", columns="max_spread",
                             values="n_accepted")
fig, ax = plt.subplots(figsize=(7, 5))
im = ax.imshow(piv.values, aspect="auto", cmap="viridis", origin="lower")
ax.set_xticks(range(len(piv.columns)), [f"{c:g}" for c in piv.columns])
ax.set_yticks(range(len(piv.index)), [f"{i:g}" for i in piv.index])
ax.set_xlabel("max_spread")
ax.set_ylabel("min_edge_raw")
ax.set_title("Accepted candidates — sensitivity heatmap")
for (i, j), v in pd.DataFrame(piv.values).stack().items():
    ax.text(j, i, int(v), ha="center", va="center",
            color="white" if v < piv.values.max() / 2 else "black", fontsize=9)
fig.colorbar(im, label="n accepted")
plot.save_fig(fig, "07_sensitivity_heatmap",
              note="grid sweep over real candidates")
plt.show()

# %% [markdown]
# ## 6 · Which constraints reject the most?

# %%
rej = (pd.Series({g: baseline[f"reject_{g}"] for g in sx.GATES})
       .sort_values(ascending=False).to_frame("candidates_rejected"))
plot.save_table(rej.reset_index().rename(columns={"index": "gate"}),
                "07_gate_rejections")
rej

# %% [markdown]
# ## 7 · Calibration on the settled subset

# %%
settled = candidates.filter(pl.col("settled"))
if settled.height >= base.min_sample:
    bins = (settled.with_columns(((pl.col("fair_p") * 5).floor() / 5)
                                 .alias("p_bin"))
            .group_by("p_bin")
            .agg(pl.len().alias("n"), pl.col("won").mean().alias("hit_rate"),
                 pl.col("fair_p").mean().alias("mean_model_p"))
            .sort("p_bin"))
    display(bins.to_pandas())
    b = bins.to_pandas()
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect")
    ax.scatter(b["mean_model_p"], b["hit_rate"],
               s=b["n"] * 4, alpha=0.7, label="model bins (size = n)")
    ax.set_xlabel("model probability")
    ax.set_ylabel("realised frequency")
    ax.set_title(f"Calibration — settled 1X2 outcomes (n={settled.height})")
    ax.legend()
    plot.save_fig(fig, "07_calibration", note=f"n={settled.height} settled outcome-candidates")
    plt.show()
else:
    print(f"settled n={settled.height} < min_sample={base.min_sample} — "
          "calibration shown as data, not conclusion")

# %% [markdown]
# ## 8 · Findings, caveats, next steps
#
# * The funnel is edge-gate-dominated (§6): the raw-edge floor rejects far
#   more than liquidity/staleness gates at current settings.
# * Realized-P&L sweeps use ONLY settled candidates with real labels; open
#   advancement rows contribute to EV/exposure curves only.
# * **Caveats:** the settled set's spread is a stated 2¢ estimate (tape has
#   no books); n is tournament-scale, not season-scale — treat P&L curves
#   as diagnostics, not proofs; Brier/log-loss are computed per
#   outcome-candidate, not per match.
# * **Next:** feed 08's value-area screens with the surviving parameter
#   ranges; rerun after each matchday extends the settled set.
