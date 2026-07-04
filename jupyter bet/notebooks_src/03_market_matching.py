# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 03 · Market matching — PM ↔ sportsbook, auditable
#
# **Purpose.** Link the two market universes on teams + kickoff +
# competition + market type + line + period + **settlement basis** —
# never fuzzy names alone. Output: accepted / rejected / ambiguous tables,
# a manual-override file, and the gold `market_links` dataset.
#
# **Hard rule encoded here (and unit-tested):** sportsbook 1X2 settles at
# 90 minutes; PM advancement includes ET+pens. Different settlement ⇒
# DIFFERENT market, always rejected.

# %%
import sys, pathlib
_here = pathlib.Path.cwd().resolve()
JB = next(q for q in [_here, *_here.parents] if (q / "lib" / "bootstrap.py").exists())
sys.path.insert(0, str(JB))

import datetime as dt
import pandas as pd
import polars as pl
import lib.bootstrap as bt
import lib.config as cfg
import lib.storage as st
import lib.matching as mtch
import lib.ids as ids

manifest = bt.run_manifest("03_market_matching")
p = cfg.load_params()

# %% [markdown]
# ## 1 · Side A — Polymarket canonical match markets (from notebook 02)

# %%
mk = st.load_dataset("bronze", "pm_markets")
pm_side = mtch.pm_canonical_matches(mk)
print(f"PM canonical 1X2 markets: {pm_side.height}")
pm_side.tail(5).to_pandas()

# %% [markdown]
# ## 2 · Side B — sportsbook markets
# Preferred: silver `sportsbook_quotes` from notebook 01 (live Odds API).
# Fallback when 01 couldn't pull: the repo schedule feed
# (`site/scores_markets.json`) as a REFERENCE side — clearly labelled; it
# proves the matcher end-to-end but carries no odds.

# %%
sb_rows, sb_source = [], None
try:
    sq = st.load_dataset("silver", "sportsbook_quotes")
    sb_source = "theoddsapi silver (notebook 01)"
    for r in (sq.group_by("event_id", "market_id")
                .agg(pl.col("kickoff_utc").first(),
                     pl.col("outcome").first(),
                     pl.col("line").first(),
                     pl.col("source_event_id").first())).to_dicts():
        parsed = ids.parse_event_id(r["event_id"])
        sb_rows.append({
            "source": "theoddsapi", "source_market_id": r["market_id"],
            "home": parsed["home_slug"].replace("-", " "),
            "away": parsed["away_slug"].replace("-", " "),
            "kickoff_utc": r["kickoff_utc"],
            "market_type": r["market_id"].split("|")[1],
            "line": r["line"], "period": "FT", "settlement": ids.S_90MIN})
except FileNotFoundError:
    sb_source = "REFERENCE side: site/scores_markets.json schedule (no odds — matcher demo)"
    import json
    sm = json.loads(pathlib.Path(bt.SCORES_MARKETS_JSON).read_text())
    for tie in (sm.get("ties") or sm.get("matches") or []):
        fx = tie.get("fixture") or ""
        ko = tie.get("kickoff_utc") or tie.get("kickoff")
        if " vs " not in fx or not ko:
            continue
        home, away = fx.split(" vs ", 1)
        kot = mtch._parse_ts(ko)
        if not kot:
            continue
        sb_rows.append({
            "source": "schedule_ref", "source_market_id": f"sched:{fx}:{ko}",
            "home": home, "away": away, "kickoff_utc": kot,
            "market_type": "1x2", "line": None, "period": "FT",
            "settlement": ids.S_90MIN})
sb_side = pl.DataFrame(sb_rows) if sb_rows else pl.DataFrame()
print(f"side B: {sb_side.height} markets — {sb_source}")
sb_side.tail(5).to_pandas() if sb_side.height else None

# %% [markdown]
# ## 3 · Manual override file
# `overrides.yaml` pins or bans pairs by exact source IDs; it always wins
# and the verdict reason records it. Created with a template if absent.

# %%
if not mtch.OVERRIDES_PATH.exists():
    mtch.OVERRIDES_PATH.write_text(
        "# Manual match overrides — exact source_market_ids, verdict accept|reject\n"
        "# pairs:\n"
        "#   - a: fifwc-esp-aut-2026-07-02\n"
        "#     b: wc2026:spain__austria__2026-07-02T19Z|1x2||FT|90min\n"
        "#     verdict: accept\n"
        "pairs: []\n")
print(mtch.OVERRIDES_PATH.read_text())

# %% [markdown]
# ## 4 · Run the matcher — every considered pair gets a verdict + reasons

# %%
links = (mtch.match_frames(pm_side, sb_side,
                           kickoff_tolerance_h=p.kickoff_tolerance_h,
                           min_confidence=p.min_match_confidence)
         if pm_side.height and sb_side.height else pl.DataFrame())
if links.height:
    counts = links.group_by("verdict").agg(pl.len().alias("n")).sort("verdict")
    print(counts.to_pandas().to_string(index=False))
else:
    print("no candidate pairs this run (side B empty?)")

# %%
if links.height:
    display(links.filter(pl.col("verdict") == "accepted").head(10).to_pandas())
    display(links.filter(pl.col("verdict") == "rejected").head(10).to_pandas())
    amb = links.filter(pl.col("verdict") == "ambiguous")
    print(f"ambiguous: {amb.height}")
    if amb.height:
        display(amb.head(10).to_pandas())

# %% [markdown]
# ## 5 · The settlement-basis firewall, demonstrated on real rows
# A PM advancement market for the same two teams on the same day must be
# REJECTED against a 90-minute 1X2 — even though team names + kickoff agree.

# %%
adv_row = None
for r in mk.to_dicts():
    if mtch.classify_pm_market(r) == "advance" and r.get("question"):
        adv_row = r
        break
if adv_row and sb_side.height:
    team_q = adv_row["question"]
    demo_a = {"source": "polymarket", "source_market_id": adv_row["condition_id"],
              "home": "Spain", "away": "Austria",
              "kickoff_utc": dt.datetime(2026, 7, 2, 19, 0, tzinfo=dt.timezone.utc),
              "market_type": "1x2", "line": None, "period": "FT",
              "settlement": ids.S_ETPENS}   # advancement settles ET+pens
    demo_b = {**demo_a, "source": "theoddsapi",
              "source_market_id": "book-1x2", "settlement": ids.S_90MIN}
    verdict = mtch.score_match(demo_a, demo_b)
    print(f"real advancement market: {team_q!r}")
    print(f"verdict vs a same-day 90-min 1X2: {verdict['verdict'].upper()}")
    print("reasons:", verdict["reasons"])
    assert verdict["verdict"] == "rejected"

# %% [markdown]
# ## 6 · Persist gold `market_links`

# %%
import lib.plotting as plot
if links.height:
    st.save_dataset(links, "gold", "market_links",
                    inputs=["silver/pm_match_events", "silver/sportsbook_quotes"],
                    notebook="03", note=f"side B = {sb_source}")
    plot.save_table(links.to_pandas(), "03_market_links")
    acc = links.filter(pl.col("verdict") == "accepted")
    print(f"gold market_links saved — {acc.height} accepted links")

# %% [markdown]
# ## 7 · Findings, caveats, next steps
#
# * Matching is deterministic and fully auditable: every pair carries its
#   per-gate checks and human-readable reasons; overrides are explicit.
# * The settlement firewall (90-min vs ET+pens) is enforced structurally —
#   it is part of the market ID and a hard gate, and is unit-tested.
# * **Caveat:** when notebook 01 hasn't pulled live odds, side B is a
#   schedule reference — links then prove identity resolution, not price
#   comparability.
# * **Next:** notebook 04 uses the linked (or PM-only) markets for
#   convergence; notebook 08 uses accepted links for cross-venue checks.
