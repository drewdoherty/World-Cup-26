# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 06 · The decision pipeline — production, cell by cell
#
# **Purpose.** Walk the REAL production decision path
# (`scripts/wca_betrecs.py` — the code that builds the shipped
# recommendation feed) stage by stage, showing per stage: input schema,
# the transform, parameters, the intermediate frame, rows accepted vs
# rejected with reasons, and the output schema. Then trace individual
# candidates end-to-end and prove parity against the shipped feed.
#
# Stages:
# feeds → pool resolution (combined £3,000±PnL bankroll, ¼-Kelly) →
# match singles (blend vs de-vigged consensus) → event props →
# advancement futures (PM, fee-adjusted) → governance caps →
# actionable/withheld split.
#
# **This notebook only READS.** It never fires orders, never writes any
# feed, never touches `pm_parked`.

# %%
import sys, pathlib
_here = pathlib.Path.cwd().resolve()
JB = next(q for q in [_here, *_here.parents] if (q / "lib" / "bootstrap.py").exists())
sys.path.insert(0, str(JB))

import json
import pandas as pd
import polars as pl
import lib.bootstrap as bt
import lib.config as cfg
import lib.storage as st
import lib.pipeline as pp

manifest = bt.run_manifest("06_decision_pipeline")
p = cfg.load_params()

# %% [markdown]
# ## Stage 0a · Input feeds (exactly what production reads) + freshness
# Staleness gates downstream depend on these ages — production withholds on
# stale feeds rather than recommending from them.

# %%
feeds = pp.load_feeds()
pd.DataFrame([{
    "feed": k, "path": v["path"].replace(str(bt.REPO_ROOT) + "/", ""),
    "age_h": round(v["age_secs"] / 3600, 1) if v["age_secs"] is not None else None,
    "items": len((v["data"] or {}).get("predictions")
                 or (v["data"] or {}).get("markets")
                 or (v["data"] or {}).get("recommendations")
                 or (v["data"] or {}).get("promotions") or []),
} for k, v in feeds.items()])

# %% [markdown]
# ## Stage 0b · Bankroll pools — production resolution
# ONE combined bankroll (£3,000 ± total realised P&L, GBP+PM at $1.33/£),
# ¼-Kelly, with static fail-closed caps. The dev-box ledger is STALE — the
# caveat rides along in the output rather than being hidden.

# %%
pools = pp.resolve_pools()
pd.Series({
    "sportsbook pool £": pools["sportsbook"]["bankroll"],
    "sportsbook kelly": pools["sportsbook"]["kelly_fraction"],
    "sportsbook max stake £": pools["sportsbook"]["max_stake"],
    "pm pool $": pools["pm"]["bankroll"],
    "pm kelly": pools["pm"]["kelly_fraction"],
    "pm max stake $": pools["pm"]["max_stake"],
    "pm realised pnl $ (stale dev ledger)": pools["pm_realised_pnl_usd"],
    "caveat": pools["ledger_caveat"],
}).to_frame("value")

# %% [markdown]
# ## Stages 1–3 · Run the production builders
# `build_match_singles` (model blend vs de-vigged consensus, 2pp edge gate,
# staleness + exposure guards, promo tags) · `build_event_props`
# (calibrated corners/cards — withheld unless real fresh prices exist) ·
# `build_advancement_futures` (PM advancement vs sim, fee-adjusted edges,
# ¼-Kelly, PM-blind + 6h staleness guards).

# %%
stages = pp.run_stages(feeds, pools)
funnel = pp.funnel(stages)
funnel.to_pandas()

# %% [markdown]
# ### Intermediate frames — schema, rows, nulls (transparency contract)

# %%
frames = pp.stage_frames(stages)
for name, df in frames.items():
    if df.height:
        prof = st.profile_frame(df, name)
        print(f"— {name}: {prof.attrs['rows']} rows × {df.width} cols")
for name in ("singles_actionable", "advancement_actionable"):
    if frames[name].height:
        display(frames[name].head(6).to_pandas())

# %% [markdown]
# ### Why rows were rejected — the withheld tables (full reasons)

# %%
for name in ("singles_withheld", "props_withheld", "advancement_withheld"):
    df = frames[name]
    if df.height:
        cols = [c for c in ("fixture", "selection", "market", "team", "stage",
                            "reason", "action") if c in df.columns]
        print(f"— {name} ({df.height} rows)")
        display(df.select(cols).head(8).to_pandas())

# %% [markdown]
# ## Decision traces — one candidate, every number, in order

# %%
trace_targets = []
for family in ("advancement", "singles"):
    if stages[family]["actionable"]:
        trace_targets.append((f"{family} (actionable)",
                              stages[family]["actionable"][0]))
    if stages[family]["withheld"]:
        trace_targets.append((f"{family} (withheld)",
                              stages[family]["withheld"][0]))
for label, cand in trace_targets[:3]:
    print(f"═══ {label} ═══")
    display(pp.decision_trace(cand).to_pandas())

# %% [markdown]
# ## Parity — notebook path vs the SHIPPED feed
# Same builders + same inputs ⇒ identical recommendations. Diffs appear
# only when the shipped file was built from different feed snapshots
# (its `meta.generated` vs our feed ages above) — that's drift visibility,
# not error.

# %%
shipped = feeds["bet_recs_shipped"]["data"] or {}
parity = pp.parity_check(stages, shipped)
shipped_meta = (shipped.get("meta") or {})
print(f"shipped feed generated: {shipped_meta.get('generated')}")
pd.Series({k: v for k, v in parity.items() if k != "field_diffs"}).to_frame("value")

# %%
if parity["field_diffs"]:
    display(pd.DataFrame(parity["field_diffs"]))
else:
    print("no field diffs on shared IDs — full parity on stake/action/edge/price")

# %% [markdown]
# ## Persist the decision snapshot (gold)

# %%
import lib.plotting as plot
for name, df in frames.items():
    if df.height:
        st.save_dataset(df, "gold", f"decisions_{name}", notebook="06",
                        inputs=[v["path"] for v in feeds.values()])
plot.save_table(funnel.to_pandas(), "06_decision_funnel")
print("gold decision datasets + funnel table saved")

# %% [markdown]
# ## Findings, caveats, next steps
#
# * The production path is fully replayable in-notebook: every gate
#   (edge ≥ 2pp, staleness, PM-blind, exposure, governance caps) is visible
#   with the exact rows it removed and why.
# * Parity against the shipped feed is ID-exact when feed snapshots match;
#   the diff table quantifies drift when they don't.
# * **Caveats:** dev-ledger staleness affects pool sizes (labelled);
#   fresh-feed reruns belong on the mini/CI, not here.
# * **Next:** notebook 07 stress-tests these same decisions under parameter
#   sweeps; notebook 08 assembles the value-area view.
