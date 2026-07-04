# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 05 · In-play analytics
#
# **Purpose.** Inside the live match window: PM price paths, trade flow,
# volume velocity, jumps/volatility, staleness/suspension signatures, and
# what size was ACTUALLY executable — from the real trade tape.
#
# **Honesty constraints (enforced by `lib.inplay`):**
# * No stoppage-aware clock exists here → all elapsed columns are
#   `wallclock_min` since *scheduled* kickoff and are labelled as such.
# * Per-minute score state needs an event feed we don't capture → columns
#   that would require it are absent, not inferred. Final scores from the
#   settled results feed give end-state context only.
# * Historical books were never captured → in-play spread/depth are
#   unavailable historically; the live cell fills them only for matches
#   in-play right now.

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
import lib.inplay as ip
import lib.ids as ids

UTC = dt.timezone.utc
manifest = bt.run_manifest("05_in_play_analytics")
p = cfg.load_params()

# %% [markdown]
# ## 1 · Pick the liveliest played matches (by in-window $ volume)

# %%
ev = st.load_dataset("silver", "pm_match_events")
matches = ev.filter(pl.col("kickoff_utc").is_not_null())
results = {(" vs ".join([r["fixture"].split(" vs ")[0],
                         r["fixture"].split(" vs ")[1]])): r
           for r in json.loads(pathlib.Path(bt.RESULTS_JSON).read_text())["results"]
           if " vs " in r.get("fixture", "")}

lf = pm.orderflow_trades()
vol_rows = []
for m in matches.to_dicts():
    ko = m["kickoff_utc"].replace(tzinfo=UTC) if m["kickoff_utc"].tzinfo is None else m["kickoff_utc"]
    if ko > dt.datetime.now(UTC):
        continue
    cids = [c for c in (m["cid_home"], m["cid_away"], m["cid_draw"]) if c]
    lo, hi = int(ko.timestamp()), int(ko.timestamp()) + ip.LIVE_WINDOW_MIN * 60
    v = (lf.filter(pl.col("condition_id").is_in(cids)
                   & (pl.col("ts") >= lo) & (pl.col("ts") <= hi))
         .select(pl.col("usd").sum().alias("usd"), pl.len().alias("n"))
         .collect())
    vol_rows.append({**m, "kickoff_utc": ko,
                     "inplay_usd": float(v["usd"][0] or 0),
                     "inplay_trades": int(v["n"][0])})
played = (pl.DataFrame(vol_rows).sort("inplay_usd", descending=True))
print(f"{played.height} played matches with tape; top by in-window $ volume:")
played.head(8).select("home", "away", "kickoff_utc", "inplay_usd",
                      "inplay_trades").to_pandas()

# %% [markdown]
# ## 2 · Deep-dive: the busiest in-play market
# End-state score attached from the settled results feed (context only).

# %%
top = played.head(1).to_dicts()[0]
fixture_name = f"{top['home']} vs {top['away']}"
res = results.get(fixture_name) or next(
    (r for k, r in results.items()
     if ids.slug(k.split(" vs ")[0]) == ids.slug(top["home"])
     and r.get("kickoff_utc", "").startswith(str(top["kickoff_utc"].date()))), None)
print(f"deep dive: {fixture_name}  KO {top['kickoff_utc']} UTC")
print(f"final score (settled results feed): {res['score'] if res else 'not in feed'}"
      f" ({res['outcome']} at 90min)" if res else "")

cids = {"home": top["cid_home"], "away": top["cid_away"], "draw": top["cid_draw"]}
win = ip.live_window_trades(
    lf.filter(pl.col("outcome_index") == 0), top["kickoff_utc"]
    ).filter(pl.col("condition_id").is_in([c for c in cids.values() if c]))
cid_to_oc = {v: k for k, v in cids.items() if v}
win = win.with_columns(pl.col("condition_id").replace_strict(cid_to_oc)
                       .alias("outcome"))
print(f"in-window trades (YES side): {win.height}")

# %%
metrics = ip.inplay_metrics(win, bucket_min=5)
st.save_dataset(metrics, "gold", "inplay_metrics_sample",
                inputs=["bronze/pm_trades"], notebook="05",
                note=f"{fixture_name}; wallclock minutes, NOT match clock")
metrics.head(10).to_pandas()

# %%
import matplotlib.pyplot as plt
import lib.plotting as plot
fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(11, 7))
for oc, grp in win.group_by("outcome"):
    g = grp.sort("ts")
    label = oc[0] if isinstance(oc, tuple) else oc
    ax1.plot(g["wallclock_min"], g["price"], lw=0.9, label=f"{label} (YES)")
ax1.set_ylabel("trade price")
ax1.set_title(f"{fixture_name} — in-play tape "
              f"(final {res['score'] if res else '?'})")
ax1.legend(fontsize=8)
mv = metrics.group_by("bucket_min").agg(pl.col("usd_vol").sum()).sort("bucket_min")
ax2.bar(mv["bucket_min"].to_list(), mv["usd_vol"].to_list(), width=4)
ax2.set_xlabel("WALL-CLOCK minutes since scheduled kickoff (NOT match clock)")
ax2.set_ylabel("$ vol / 5min")
plot.save_fig(fig, "05_inplay_tape",
              note=f"n={win.height} real prints · pm_orderflow.db · {manifest['run_utc']}")
plt.show()

# %% [markdown]
# ## 3 · Jumps, volatility, staleness/suspension signatures
# Gaps > 120s between prints flag likely suspensions/illiquidity (goals,
# VAR). Jump sizes bound how fast an in-play quote goes stale.

# %%
import lib.convergence as cv
vol = cv.realized_vol(win.rename({"ts_utc": "ts_utc"}), bucket="5m")
stale = ip.staleness_flags(win, stale_after_s=120)
print(f"stale/suspension gaps >120s: {stale.height}")
stale.head(10).to_pandas()

# %%
jump_summary = (win.sort("ts")
    .with_columns(pl.col("price").diff().over("outcome").abs().alias("jump"))
    .group_by("outcome")
    .agg(pl.len().alias("n_prints"),
         pl.col("jump").max().round(3).alias("max_jump"),
         pl.col("jump").quantile(0.99).round(3).alias("p99_jump"),
         pl.col("jump").mean().round(4).alias("mean_abs_move")))
jump_summary.to_pandas()

# %% [markdown]
# ## 4 · Executable size + hypothetical entry/exit trace
# Sized from PRINTED volume around the marks — we never assume depth beyond
# what actually traded. Labelled hypothetical.

# %%
trace = ip.entry_exit_trace(win, "home", entry_min=10.0, exit_min=60.0,
                            usd=100.0, max_slippage=p.max_slippage)
pd.Series(trace).to_frame("value")

# %% [markdown]
# ## 5 · Live matches right now (books, spread, depth)
# Fills the live-only gap when a match is actually in-play at run time.

# %%
now = dt.datetime.now(UTC)
live_now = [m for m in matches.to_dicts()
            if m["kickoff_utc"] and 0 <= (now - (m["kickoff_utc"].replace(tzinfo=UTC)
            if m["kickoff_utc"].tzinfo is None else m["kickoff_utc"])).total_seconds()
            <= ip.LIVE_WINDOW_MIN * 60]
if not live_now:
    print(f"no WC match in-play at {manifest['run_utc']} — live book cell "
          "idle (honest skip)")
elif p.offline:
    print("offline mode — live books skipped")
else:
    for m in live_now:
        for oc, cid in (("home", m["cid_home"]), ("away", m["cid_away"]),
                        ("draw", m["cid_draw"])):
            row = (st.load_dataset("silver", "pm_outcomes")
                   .filter((pl.col("condition_id") == cid)
                           & (pl.col("outcome_index") == 0)))
            if row.height and row["token_id"][0]:
                try:
                    book = pm.clob_book(row["token_id"][0])
                    if book:
                        bm = pm.book_metrics(book)
                        print(f"{m['home']} vs {m['away']} [{oc}]: "
                              f"bid {bm['best_bid']} ask {bm['best_ask']} "
                              f"spread {bm['spread']} depth±5c "
                              f"${bm['depth_bid_5c_usd']:.0f}/${bm['depth_ask_5c_usd']:.0f}")
                except pm.PMUnavailable as e:
                    print(f"PM unreachable: {e}")
                    break

# %% [markdown]
# ## 6 · Findings, caveats, next steps
#
# * The tape is rich enough in-window to characterise in-play behaviour:
#   volume clusters at kickoff/goals, price jumps mark score events, and
#   >120s print-gaps flag suspension-like states.
# * Printed volume around any mark bounds honest executable size — the
#   entry/exit trace refuses sizes the tape can't support.
# * **Caveats:** wall-clock ≠ match clock (stoppage unknown); prints embed
#   spread; no historical books.
# * **Next:** live-book snapshots during matches (collector or §5) convert
#   the remaining `unavailable` columns to `observed` going forward.
