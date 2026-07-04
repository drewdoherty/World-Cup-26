# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 02 · Polymarket — full World Cup data run
#
# **Purpose.** Build the canonical PM dataset family for every WC-related
# market we hold data on: metadata, outcomes/tokens, prices, order books,
# trades, volume/liquidity — as tidy tables at event / market / outcome /
# snapshot / book-level / trade grain.
#
# **Sources** (real, no fabrication):
# * `data/pm_orderflow.db` — production capture: ~2.09M trades + ~1.4k
#   markets (slugs, questions, outcomes, token ids, volume, liquidity).
# * Live Gamma / CLOB / Data-API — attempted per cell; each failure is
#   recorded with its reason (the PM route needs the VPN on this MacBook).
#
# **Contents**: [markets](#markets) · [classification](#classes) ·
# [match events](#events) · [trades](#trades) · [live layers](#live) ·
# [coverage](#coverage) · [findings](#findings)

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
import lib.pmdata as pm
import lib.matching as mt
import lib.ids as ids

manifest = bt.run_manifest("02_polymarket_full_run")
p = cfg.load_params()
N_LIVE_BOOKS = 12      # order books to snapshot live (open markets only)
N_HISTORY_TOKENS = 4   # tokens to pull dense CLOB price history for
print(f"offline={p.offline}")

# %% [markdown]
# <a id="markets"></a>
# ## 1 · Market universe (bronze)
# Straight from the production capture DB — read-only.

# %%
mk = pm.orderflow_markets()
st.save_dataset(mk, "bronze", "pm_markets",
                inputs=["repo:data/pm_orderflow.db#pm_markets"], notebook="02")
prof = st.profile_frame(mk, "pm_markets", unique_keys=["condition_id"])
print(f"{prof.attrs['rows']} markets, duplicate condition_ids: {prof.attrs['duplicate_keys']}")
prof

# %% [markdown]
# <a id="classes"></a>
# ## 2 · Classification
# `category` is PRODUCTION's own classification (carried in the DB);
# `lib.matching.classify_pm_market` maps it to our canonical vocabulary
# with text fallbacks for live-Gamma rows that lack it.

# %%
classes = pl.DataFrame({
    "class": [mt.classify_pm_market(r) for r in mk.to_dicts()],
    "condition_id": mk["condition_id"], "volume": mk["volume"],
    "closed": mk["closed"]})
(classes.group_by("class")
 .agg(pl.len().alias("n_markets"),
      pl.col("volume").sum().round(0).alias("total_usd_volume"),
      (1 - pl.col("closed").mean()).round(3).alias("open_frac"))
 .sort("total_usd_volume", descending=True)
 .to_pandas())

# %% [markdown]
# <a id="events"></a>
# ## 3 · Canonical match events + outcome (token) table — silver
# PM match markets come as one Yes/No market per outcome
# (`fifwc-<a>-<b>-<date>-{a,b,draw}`); `pm_match_events` reassembles the
# 1X2 with slug order = home first. Settlement: **90 minutes** (the PM
# match markets settle on regulation, matching sportsbook 1X2 — advancement
# markets, ET+pens, are a different table and can never be confused).

# %%
ev = mt.pm_match_events(mk)
st.save_dataset(ev, "silver", "pm_match_events",
                inputs=["bronze/pm_markets"], notebook="02")
n_groups = (classes.filter(pl.col("class") == "match_1x2").height) // 3
print(f"assembled {ev.height} matches (from ~{n_groups} slug groups; "
      f"unassembled groups lack a home/away leg and are counted, not hidden)")
ev.tail(8).to_pandas()

# %%
# outcome/token grain: one row per (market, outcome), preserving source ids
out_rows = []
for r in mk.to_dicts():
    outcomes = json.loads(r["outcomes"]) if r["outcomes"] else []
    tokens = json.loads(r["token_ids"]) if r["token_ids"] else []
    for i, o in enumerate(outcomes):
        out_rows.append({
            "condition_id": r["condition_id"], "outcome_index": i,
            "outcome": o, "token_id": tokens[i] if i < len(tokens) else None,
            "market_class": mt.classify_pm_market(r),
            "question": r["question"], "closed": bool(r["closed"]),
            "resolved_outcome_index": r["resolved_outcome_index"],
            "volume_usd": r["volume"], "liquidity_usd": r["liquidity"]})
outcomes_df = pl.DataFrame(out_rows)
st.save_dataset(outcomes_df, "silver", "pm_outcomes",
                inputs=["bronze/pm_markets"], notebook="02")
st.profile_frame(outcomes_df, "pm_outcomes",
                 unique_keys=["condition_id", "outcome_index"])

# %% [markdown]
# <a id="trades"></a>
# ## 4 · Trades (bronze, 2M+ rows — Polars lazy)
# The full capture, tidied to trade grain with UTC timestamps. This is REAL
# executed flow, so prices/volumes here are executable history, not quotes.
# Beware the production caveat: the data-api caps history at offset 3000,
# so this DB (incrementally captured since May) is the ONLY full history.

# %%
lf = pm.orderflow_trades()          # LazyFrame — filter before collecting
trades_summary = (lf
    .group_by("condition_id")
    .agg(pl.len().alias("n_trades"), pl.col("usd").sum().alias("usd_vol"),
         pl.col("ts").min().alias("first_ts"), pl.col("ts").max().alias("last_ts"))
    .collect())
print(f"{int(trades_summary['n_trades'].sum()):,} trades across "
      f"{trades_summary.height} markets")
top = (trades_summary.join(mk.select("condition_id", "question"), on="condition_id")
       .sort("usd_vol", descending=True).head(10))
top.select("question", "n_trades", "usd_vol").to_pandas()

# %%
# Persist the tidy trade table once (bronze). ~2M rows → zstd parquet.
trades_path = st.dataset_path("bronze", "pm_trades")
if not trades_path.exists():
    tidy = lf.collect()
    st.save_dataset(tidy, "bronze", "pm_trades",
                    inputs=["repo:data/pm_orderflow.db#pm_trades"], notebook="02",
                    note="full production orderflow capture, trade grain")
    print(f"bronze pm_trades written: {tidy.height:,} rows, "
          f"{trades_path.stat().st_size/1e6:.0f} MB")
    del tidy
else:
    print(f"bronze pm_trades already built "
          f"({trades_path.stat().st_size/1e6:.0f} MB) — idempotent skip")
st.save_dataset(trades_summary, "silver", "pm_trade_summary",
                inputs=["bronze/pm_trades"], notebook="02")

# %% [markdown]
# <a id="live"></a>
# ## 5 · Live layers — Gamma metadata, CLOB books, price history, holders
# Each attempted independently; failures recorded (VPN route required).
# Books give bid/ask/mid/microprice/spread + $ depth (full level data kept
# in raw); price history is the dense CLOB source (production-verified
# back to May 30).

# %%
gamma_status = "skipped (offline)"
if not p.offline:
    try:
        gamma_events, snaps = pm.gamma_wc_events()
        gamma_status = f"OK — {len(gamma_events)} WC events → {snaps[-1]}"
        gm = pl.DataFrame([{k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                            for k, v in e.items()} for e in gamma_events],
                          infer_schema_length=None)
        st.save_dataset(gm, "bronze", "pm_gamma_events", notebook="02",
                        inputs=list(snaps))
    except pm.PMUnavailable as e:
        gamma_status = f"UNREACHABLE: {e}"
    except Exception as e:  # noqa: BLE001 — record, don't crash the run
        gamma_status = f"ERROR: {type(e).__name__}: {e}"
print("gamma:", gamma_status)

# %%
# Live order books for the most active OPEN markets
quotes_rows, book_status = [], "skipped (offline)"
if not p.offline:
    open_active = (trades_summary
        .join(mk.filter(pl.col("closed") == 0).select("condition_id", "question"),
              on="condition_id")
        .sort("last_ts", descending=True).head(N_LIVE_BOOKS))
    ok = fail = 0
    for r in open_active.to_dicts():
        toks = (outcomes_df.filter(pl.col("condition_id") == r["condition_id"])
                .to_dicts())
        for t in toks:
            if not t["token_id"]:
                continue
            try:
                book = pm.clob_book(t["token_id"])
            except pm.PMUnavailable:
                book = None
            if not book:
                fail += 1
                continue
            m = pm.book_metrics(book)
            quotes_rows.append({
                "condition_id": r["condition_id"], "question": r["question"],
                "outcome": t["outcome"], "token_id": t["token_id"],
                "snapshot_utc": bt.utcnow_iso(), **m})
            ok += 1
    book_status = f"{ok} books captured, {fail} failed/empty"
print("clob books:", book_status)
quotes = pl.DataFrame(quotes_rows) if quotes_rows else pl.DataFrame()
if quotes.height:
    st.save_dataset(quotes, "silver", "pm_quotes_live", notebook="02",
                    note="live top-of-book snapshot at run time")
    display(quotes.head(8).to_pandas())

# %%
# Dense CLOB price history for a few active tokens (trajectory source)
hist_status = "skipped (offline)"
hist_frames = []
if not p.offline and quotes.height:
    for t in quotes.head(N_HISTORY_TOKENS).to_dicts():
        try:
            h = pm.clob_price_history(t["token_id"])
            if h.height:
                hist_frames.append(h.with_columns(
                    pl.lit(t["question"]).alias("question"),
                    pl.lit(t["outcome"]).alias("outcome")))
        except pm.PMUnavailable as e:
            hist_status = f"UNREACHABLE: {e}"
            break
    else:
        hist_status = f"OK — {len(hist_frames)} token histories"
if hist_frames:
    hist = pl.concat(hist_frames)
    st.save_dataset(hist, "bronze", "pm_price_history", notebook="02")
print("clob price history:", hist_status)

# %%
# Holders / open interest (data-api; live only, sample)
holders_status = "skipped (offline)"
if not p.offline and quotes.height:
    cid = quotes["condition_id"][0]
    try:
        hl = pm.data_api_holders(cid)
        holders_status = f"OK — {len(hl)} holder rows for {cid[:10]}…"
    except pm.PMUnavailable as e:
        holders_status = f"UNREACHABLE: {e}"
print("holders:", holders_status)

# %% [markdown]
# ### Chart — real price + volume trajectory (from captured trades)

# %%
import matplotlib.pyplot as plt
import lib.plotting as plot
import lib.convergence as cv

sample = top.head(1).to_dicts()[0]
tr = pm.orderflow_trades(condition_ids=[sample["condition_id"]]).collect()
flow = cv.trade_flow_metrics(tr, bucket="1h")
fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(11, 6))
for outcome, grp in tr.group_by("outcome"):
    g = grp.sort("ts_utc")
    ax1.plot(g["ts_utc"], g["price"], lw=0.8, label=str(outcome[0]) if isinstance(outcome, tuple) else str(outcome))
ax1.set_ylabel("trade price")
ax1.set_title(f"{sample['question']} — {int(sample['n_trades']):,} real trades")
ax1.legend(loc="best", fontsize=8)
ax2.bar(flow["ts_utc"].to_list(), flow["usd_vol"].to_list(), width=0.04)
ax2.set_ylabel("$ volume / h")
plot.ts_axis(ax2)
plot.save_fig(fig, "02_sample_market_trajectory",
              note=f"source: pm_orderflow.db · n={tr.height} trades · built {manifest['run_utc']}")
plt.show()

# %% [markdown]
# <a id="coverage"></a>
# ## 6 · Coverage — observed vs unavailable (honesty table)

# %%
coverage = pd.DataFrame([
    {"data": "markets metadata", "status": f"observed — {mk.height} markets (capture DB)"},
    {"data": "outcomes/tokens", "status": f"observed — {outcomes_df.height} rows"},
    {"data": "trades (full history)", "status": f"observed — {int(trades_summary['n_trades'].sum()):,} trades"},
    {"data": "live gamma metadata", "status": gamma_status},
    {"data": "live order books (depth/spread)", "status": book_status},
    {"data": "dense price history (CLOB)", "status": hist_status},
    {"data": "holders / OI", "status": holders_status},
    {"data": "HISTORICAL order books", "status": "unavailable — books were never captured historically; "
     "historical spread/depth cannot be reconstructed and are not"},
])
plot.save_table(coverage, "02_pm_coverage")
coverage

# %% [markdown]
# <a id="findings"></a>
# ## 7 · Findings, caveats, next steps
#
# * The PM WC universe splits ~1,450 markets into: match 1X2 (282 outcome
#   markets → ~91–94 assembled matches, 90-min settlement), advancement
#   ladders (240, ET+pens), group/outright futures (~925).
# * Trade history is deep and real (2M+ prints) — good enough to price
#   convergence and in-play behaviour without any quote reconstruction.
# * Live books/gamma depend on the VPN route; the coverage table above
#   records exactly what this run could and couldn't see.
# * **Next:** notebook 03 matches this universe against the Odds API side;
#   04/05 consume `pm_trades` + `pm_match_events` for convergence/in-play.
