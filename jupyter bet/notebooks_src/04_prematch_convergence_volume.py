# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 04 · Pre-match convergence & volume
#
# **Purpose.** For PM World Cup match markets: how do price, volume and
# model edge behave at exactly **48h / 24h / 0h** before kickoff (plus
# optional 72/12/6/3/1h marks), and how do prices converge toward the
# close? Every value is labelled `observed` / `reconstructed` /
# `unavailable` — no silent interpolation, no look-ahead.
#
# **Fair-value method** is configurable (`Params.fair_value_method`); this
# notebook uses the **model** blend from the production prediction log by
# default (each mark uses the latest model row generated BEFORE the mark).
# Closing price appears ONLY in the explicitly-labelled ex-post benchmark
# section.

# %%
import sys, pathlib
_here = pathlib.Path.cwd().resolve()
JB = next(q for q in [_here, *_here.parents] if (q / "lib" / "bootstrap.py").exists())
sys.path.insert(0, str(JB))

import datetime as dt
import json
import pandas as pd
import polars as pl
import lib.bootstrap as bt
import lib.config as cfg
import lib.storage as st
import lib.pmdata as pm
import lib.convergence as cv
import lib.ids as ids

UTC = dt.timezone.utc
manifest = bt.run_manifest("04_prematch_convergence_volume")
p = cfg.load_params()
MARKS = tuple(p.window_hours) + tuple(p.extra_window_hours)
print(f"marks (hours before KO): {sorted(MARKS, reverse=True)}, "
      f"tolerance ±{p.window_tolerance_min}min")

# %% [markdown]
# ## 1 · Inputs — PM matches, trades, model log

# %%
ev = st.load_dataset("silver", "pm_match_events")
matches = ev.filter(pl.col("kickoff_utc").is_not_null())
print(f"{matches.height} PM matches with kickoff")

# model 1X2 log (production, append-only): latest row per fixture BEFORE a
# given mark = the model's information set at that time (no look-ahead).
model_rows = []
for line in open(bt.MODEL_PRED_LOG):
    r = json.loads(line)
    if not r.get("fixture") or not r.get("model"):
        continue
    home, away = (r["fixture"].split(" vs ", 1) + [""])[:2]
    gen = r.get("generated")
    try:
        gen_ts = dt.datetime.strptime(gen, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        continue
    model_rows.append({
        "pair": "__".join(sorted((ids.slug(home), ids.slug(away)))),
        "home_slug": ids.slug(home), "generated_utc": gen_ts,
        "p_home": r["model"]["home"], "p_draw": r["model"]["draw"],
        "p_away": r["model"]["away"]})
model_log = pl.DataFrame(model_rows)
print(f"model log: {model_log.height} rows, "
      f"{model_log['pair'].n_unique()} fixtures")

# %% [markdown]
# ## 2 · Build per-outcome convergence tables
# Price series = REAL trade prints for each outcome token (the tape). At
# each mark we take the last print within tolerance (`observed`), or a
# bracketing linear interpolation ≤24h (`reconstructed`), else
# `unavailable`. Model fair value obeys the same cutoff on `generated`.

# %%
def model_probs_asof(pair_key, home_slug, mark):
    m = (model_log
         .filter((pl.col("pair") == pair_key)
                 & (pl.col("generated_utc") <= mark))
         .sort("generated_utc"))
    if m.is_empty():
        return None
    r = m.tail(1).to_dicts()[0]
    if r["home_slug"] == home_slug:            # align orientation by slug
        return {"home": r["p_home"], "draw": r["p_draw"], "away": r["p_away"]}
    return {"home": r["p_away"], "draw": r["p_draw"], "away": r["p_home"]}

conv_rows = []
lf = pm.orderflow_trades()
for m in matches.to_dicts():
    ko = m["kickoff_utc"].replace(tzinfo=UTC) if m["kickoff_utc"].tzinfo is None else m["kickoff_utc"]
    pair_key = "__".join(sorted((ids.slug(m["home"]), ids.slug(m["away"]))))
    marks = cv.mark_times(ko, MARKS)
    cids = {"home": m["cid_home"], "away": m["cid_away"], "draw": m["cid_draw"]}
    tr_all = lf.filter(pl.col("condition_id").is_in([c for c in cids.values() if c])).collect()
    for oc, cid in cids.items():
        if not cid:
            continue
        # YES-side prints only (outcome_index 0 == Yes for these markets)
        s = (tr_all.filter((pl.col("condition_id") == cid)
                           & (pl.col("outcome_index") == 0))
             .select("ts_utc", "price", "usd"))
        for h, mark in marks.items():
            px = cv.value_at_mark(s, mark, tolerance_min=p.window_tolerance_min)
            probs = model_probs_asof(pair_key, ids.slug(m["home"]), mark)
            fair = probs[oc] if probs else None
            pre = s.filter(pl.col("ts_utc") <= mark)
            conv_rows.append({
                "event_slug": m["event_slug"], "home": m["home"],
                "away": m["away"], "outcome": oc, "kickoff_utc": ko,
                "hours_out": h, "mark_utc": mark,
                "pm_price": px["value"], "price_basis": px["basis"],
                "model_fair": fair,
                "fair_basis": "observed" if fair is not None else "unavailable",
                "edge": (fair - px["value"]
                         if None not in (fair, px["value"]) else None),
                "cum_usd_vol": float(pre["usd"].sum()) if pre.height else 0.0,
                "n_trades_pre": pre.height,
            })
conv = pl.DataFrame(conv_rows)
st.save_dataset(conv, "gold", "prematch_convergence",
                inputs=["silver/pm_match_events", "bronze/pm_trades",
                        "repo:data/model_predictions_log.jsonl"],
                notebook="04")
print(f"convergence rows: {conv.height} "
      f"({conv['event_slug'].n_unique()} matches × outcomes × marks)")
conv.head(8).to_pandas()

# %% [markdown]
# ## 3 · Coverage honesty — basis counts per mark

# %%
cov = (conv.group_by("hours_out", "price_basis").agg(pl.len().alias("n"))
       .pivot(values="n", index="hours_out", on="price_basis")
       .sort("hours_out", descending=True))
cov.to_pandas()

# %% [markdown]
# ## 4 · Model-edge convergence (does |model − market| shrink toward KO?)
# Only `observed` prices with an available model value; n stated per mark.

# %%
obs = conv.filter((pl.col("price_basis") == "observed")
                  & pl.col("edge").is_not_null())
agg = (obs.group_by("hours_out")
       .agg(pl.len().alias("n"),
            pl.col("edge").abs().mean().round(4).alias("mean_abs_edge"),
            pl.col("edge").mean().round(4).alias("mean_signed_edge"),
            pl.col("cum_usd_vol").median().round(0).alias("median_cum_vol"))
       .sort("hours_out", descending=True))
low_n = agg.filter(pl.col("n") < p.min_sample)
if low_n.height:
    print(f"NOTE: marks with n < {p.min_sample} are shown but not conclusions:",
          low_n["hours_out"].to_list())
agg.to_pandas()

# %%
import matplotlib.pyplot as plt
import lib.plotting as plot
apd = agg.to_pandas().sort_values("hours_out", ascending=False)
fig, ax1 = plt.subplots(figsize=(9, 5))
ax1.plot(apd["hours_out"], apd["mean_abs_edge"], "o-", label="mean |model − PM|")
ax1.set_xlabel("hours before kickoff")
ax1.set_ylabel("mean |edge| (prob units)")
ax1.invert_xaxis()
ax2 = ax1.twinx()
ax2.bar(apd["hours_out"], apd["median_cum_vol"], alpha=0.25, width=3,
        label="median cum $ volume")
ax2.set_ylabel("median cumulative $ volume")
ax1.set_title("Model-market convergence vs volume by hours-out")
fig.legend(loc="upper right")
plot.save_fig(fig, "04_convergence_by_hours_out",
              note=f"n per mark: {dict(zip(apd.hours_out, apd.n))} · trades tape, model log · {manifest['run_utc']}")
plt.show()

# %% [markdown]
# ## 5 · Volume velocity / acceleration + VWAP (per match)

# %%
sample_matches = (conv.filter(pl.col("hours_out") == 0)
                  .group_by("event_slug").agg(pl.col("cum_usd_vol").sum())
                  .sort("cum_usd_vol", descending=True).head(3)["event_slug"].to_list())
fig, axes = plt.subplots(len(sample_matches), 1, figsize=(11, 3.2 * len(sample_matches)),
                         sharex=False)
for ax, slug in zip(axes if len(sample_matches) > 1 else [axes], sample_matches):
    row = matches.filter(pl.col("event_slug") == slug).to_dicts()[0]
    cids = [c for c in (row["cid_home"], row["cid_away"], row["cid_draw"]) if c]
    tr = pm.orderflow_trades(condition_ids=cids).collect()
    ko = row["kickoff_utc"].replace(tzinfo=UTC) if row["kickoff_utc"].tzinfo is None else row["kickoff_utc"]
    pre = tr.filter(pl.col("ts_utc") <= ko)     # pre-match only — no look-ahead
    flow = cv.trade_flow_metrics(pre, bucket="1h")
    ax.plot(flow["ts_utc"], flow["vwap"], lw=1.2, label="hourly VWAP (all outcomes)")
    axv = ax.twinx()
    axv.fill_between(flow["ts_utc"].to_list(), flow["velocity"].fill_null(0).to_list(),
                     alpha=0.2, step="mid", label="volume velocity Δ$/h")
    ax.set_title(f"{row['home']} vs {row['away']} — pre-match tape")
    ax.legend(loc="upper left", fontsize=8)
plot.ts_axis(ax)
plot.save_fig(fig, "04_volume_velocity_vwap",
              note="pre-KO trades only · pm_orderflow.db")
plt.show()

# %% [markdown]
# ## 6 · Ex-post benchmark — convergence error vs the close
# `closing` = last real print BEFORE kickoff. This section is the ONLY
# place closing values appear, and only because `allow_expost` is set
# locally here; the decision datasets above never saw it.

# %%
allow_expost = True   # benchmark cell — explicitly ex-post, never an input
closing = (conv.filter((pl.col("hours_out") == 0)
                       & (pl.col("price_basis") == "observed"))
           .select("event_slug", "outcome",
                   pl.col("pm_price").alias("closing_price_expost")))
bench = (conv.filter(pl.col("hours_out") > 0)
         .join(closing, on=["event_slug", "outcome"], how="inner")
         .with_columns((pl.col("pm_price") - pl.col("closing_price_expost"))
                       .abs().alias("conv_error_expost")))
bench_agg = (bench.filter(pl.col("pm_price").is_not_null())
             .group_by("hours_out")
             .agg(pl.len().alias("n"),
                  pl.col("conv_error_expost").mean().round(4).alias("mean_abs_error"),
                  pl.col("conv_error_expost").median().round(4).alias("median_abs_error"))
             .sort("hours_out", descending=True))
bench_agg.to_pandas()

# %%
bpd = bench_agg.to_pandas().sort_values("hours_out", ascending=False)
fig, ax = plt.subplots(figsize=(9, 4.5))
ax.plot(bpd["hours_out"], bpd["mean_abs_error"], "o-", label="mean |price − close|")
ax.plot(bpd["hours_out"], bpd["median_abs_error"], "s--", label="median")
ax.invert_xaxis()
ax.set_xlabel("hours before kickoff")
ax.set_ylabel("abs error vs close (EX-POST benchmark)")
ax.set_title("Convergence toward the close — ex-post benchmark only")
ax.legend()
plot.save_fig(fig, "04_convergence_error_expost",
              note=f"closing = last pre-KO print; ex-post only · n={dict(zip(bpd.hours_out, bpd.n))}")
plt.show()

# %% [markdown]
# ## 7 · Missing live inputs + the snapshot collector
# The tape can't give **spread/depth** history (books were never captured).
# For future matches, run the collector while the market is open:
# ```
# ../.venv/bin/python tools/pm_snapshot_collector.py --minutes 180 --every 60
# ```
# It appends top-of-book (bid/ask/mid/microprice/spread/depth) for every
# open match token into silver `pm_quote_snapshots`, which this notebook
# picks up automatically on the next run (cell below).

# %%
try:
    snaps = st.load_dataset("silver", "pm_quote_snapshots")
    print(f"quote snapshots available: {snaps.height} rows, "
          f"{snaps['token_id'].n_unique()} tokens — spread/depth marks will "
          f"populate for the covered matches")
except FileNotFoundError:
    print("no quote snapshots captured yet — spread/depth at marks remain "
          "UNAVAILABLE (honest gap; run the collector before the next match)")

# %% [markdown]
# ## 8 · Findings, caveats, next steps
#
# * Coverage: the basis table (§3) shows how observable each mark really is
#   — far-out marks lean on sparse tape; 0h marks are dense.
# * The convergence and benchmark tables state n everywhere; marks below
#   `min_sample` are flagged, not spun.
# * Volume accelerates into kickoff (velocity plots); VWAP drift near KO is
#   visible on the sampled heavy markets.
# * **Caveats:** trade prints ≠ quotes (execution prices embed spread);
#   spread/depth history is unavailable until the collector runs; model
#   fair value is the production blend, so this measures THE MODEL's edge,
#   not an oracle's.
# * **Next:** notebook 05 (in-play) reuses the same tape inside the match
#   window; notebook 07 stress-tests decisions built at these marks.
