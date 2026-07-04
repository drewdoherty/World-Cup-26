# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 01 · The Odds API — full v4 endpoint run
#
# **Purpose.** Exercise EVERY relevant Odds API v4 read endpoint under a
# hard credit budget, land the payloads in the raw layer, and build the
# bronze/silver odds tables the matching + convergence notebooks read.
#
# **Contents**: [registry](#registry) · [guard](#guard) ·
# [sport-key discovery](#sportkey) · [endpoint runs](#runs) ·
# [bronze/silver builds](#builds) · [call log & quota](#log) ·
# [findings](#findings)
#
# **What to change**: `MAX_CREDITS` (budget), `DRY_RUN` (estimate only),
# `RUN_HISTORICAL` (expensive 10× endpoints), `p.offline` (cache only).

# %%
import sys, pathlib
_here = pathlib.Path.cwd().resolve()
JB = next(q for q in [_here, *_here.parents] if (q / "lib" / "bootstrap.py").exists())
sys.path.insert(0, str(JB))

import pandas as pd
import polars as pl
import lib.bootstrap as bt
import lib.config as cfg
import lib.oddsapi as oa
import lib.storage as st
import lib.ids as ids
import lib.fairvalue as fv

manifest = bt.run_manifest("01_odds_api_full_run")

# %% [markdown]
# ### Parameters — the knobs for THIS notebook

# %%
p = cfg.load_params()
MAX_CREDITS = p.max_credits      # hard per-run budget (default 25)
DRY_RUN = False                  # True → estimate costs, no live calls
RUN_HISTORICAL = False           # 10×-cost endpoints; flip deliberately
CACHE_MAX_AGE_S = 1800           # reuse raw snapshots younger than 30 min
REGIONS, MARKETS = "uk", "h2h,totals"
print(f"budget {MAX_CREDITS} credits · dry_run={DRY_RUN} · offline={p.offline}")

# %% [markdown]
# <a id="registry"></a>
# ## 1 · Endpoint registry
# Complete v4 read surface with the DOCUMENTED credit-cost formula per
# endpoint (estimates — the guard trues up from response headers).

# %%
registry = pd.DataFrame([
    {"endpoint": e.key, "path": e.path, "cost (documented)": e.cost_note,
     "what it serves": e.doc}
    for e in oa.ENDPOINTS.values()])
registry

# %% [markdown]
# <a id="guard"></a>
# ## 2 · Quota guard
# Central budget: every call estimates first, spends only if affordable,
# logs everything (live / cache / dry-run / skip + reason).

# %%
guard = oa.QuotaGuard(max_credits=MAX_CREDITS, dry_run=DRY_RUN)

# %% [markdown]
# <a id="sportkey"></a>
# ## 3 · World Cup sport key — discovered, validated, never hardcoded

# %%
sport_key = None
try:
    disc = oa.discover_wc_sport_key(guard, offline=p.offline)
    sport_key = disc["sport_key"]
    print("validated sport key:", sport_key)
    pd.DataFrame(disc["candidates"])
except oa.SkippedCall as e:
    print("sport-key discovery skipped:", e)

# %% [markdown]
# <a id="runs"></a>
# ## 4 · Run every endpoint (skips recorded, never silent)
# Each block is one endpoint; failures/skips append to the call log with the
# reason. Historical endpoints run in DRY-RUN unless `RUN_HISTORICAL=True`
# so their cost is visible without spending 10× credits.

# %%
payloads = {}

def attempt(key, **params):
    try:
        payload, snap, meta = oa.fetch(key, guard, offline=p.offline,
                                       cache_max_age_s=CACHE_MAX_AGE_S, **params)
        payloads[key] = payload
        n = len(payload) if isinstance(payload, list) else 1
        print(f"{key:24s} OK  ({n} objects) → {snap}")
    except oa.SkippedCall as e:
        print(f"{key:24s} SKIP: {e}")

if sport_key:
    attempt("events", sport_key=sport_key)                       # free
    attempt("odds", sport_key=sport_key, regions=REGIONS,        # 2 credits
            markets=MARKETS, oddsFormat="decimal")
    attempt("scores", sport_key=sport_key, daysFrom=2)           # 2 credits
    attempt("participants", sport_key=sport_key)                 # 1 credit

# %%
# Per-event endpoints: nearest upcoming event from /events (if any)
event_id_src = None
if payloads.get("events"):
    evs = sorted(payloads["events"], key=lambda e: e.get("commence_time") or "")
    upcoming = [e for e in evs if (e.get("commence_time") or "") >= manifest["run_utc"]]
    if upcoming:
        event_id_src = upcoming[0]["id"]
        print("nearest upcoming event:", upcoming[0].get("home_team"), "vs",
              upcoming[0].get("away_team"), upcoming[0].get("commence_time"))
if sport_key and event_id_src:
    attempt("event_markets", sport_key=sport_key, event_id=event_id_src,
            regions=REGIONS)                                     # 1 credit
    attempt("event_odds", sport_key=sport_key, event_id=event_id_src,
            regions=REGIONS, markets="btts,draw_no_bet",         # 2 credits
            oddsFormat="decimal")
    # player props are only served per-event; many books don't post them for
    # every WC match — a 422/empty response is recorded, not hidden
    attempt("event_odds", sport_key=sport_key, event_id=event_id_src,
            regions=REGIONS, markets="player_goal_scorer_anytime",
            oddsFormat="decimal")
else:
    guard.log(endpoint="event_markets", mode="skip",
              est_credits=1, reason="no upcoming event id available this run")
    guard.log(endpoint="event_odds", mode="skip",
              est_credits=2, reason="no upcoming event id available this run")
    print("per-event endpoints skipped: no upcoming event id")

# %%
# Historical endpoints (10× multiplier) — coded and runnable; DRY-RUN by
# default so the cost is demonstrated without spending.
hist_guard = guard if RUN_HISTORICAL else oa.QuotaGuard(
    max_credits=MAX_CREDITS, dry_run=True)
hist_date = "2026-06-20T12:00:00Z"   # inside our sportsbook-snapshot window
if sport_key:
    for key, params in [
            ("historical_events", {"date": hist_date}),
            ("historical_odds", {"date": hist_date, "regions": REGIONS,
                                 "markets": "h2h"}),
            ("historical_event_odds", {"date": hist_date, "regions": REGIONS,
                                       "markets": "h2h",
                                       "event_id": event_id_src or "unknown"})]:
        try:
            payload, snap, _ = oa.fetch(key, hist_guard, offline=p.offline,
                                        sport_key=sport_key, **params)
            payloads[key] = payload
            print(f"{key:24s} OK → {snap}")
        except oa.SkippedCall as e:
            print(f"{key:24s} SKIP: {e}")
    if not RUN_HISTORICAL:
        for c in hist_guard.calls:
            guard.calls.append({**c, "reason": (c.get("reason") or "")
                                + " [RUN_HISTORICAL=False]"})

# %% [markdown]
# <a id="builds"></a>
# ## 5 · Bronze + silver builds
# Bronze uses PRODUCTION parsing (`wca.data.theoddsapi._parse_events`) so
# rows here are shaped exactly like production's. Silver adds canonical IDs
# and per-book de-vigged probabilities.

# %%
bronze_odds = pl.DataFrame()
if payloads.get("odds"):
    pdf = oa.parse_odds_payload(payloads["odds"])
    bronze_odds = pl.from_pandas(pdf)
    st.save_dataset(bronze_odds, "bronze", "oddsapi_odds",
                    inputs=[c.get("snapshot", "") for c in guard.calls
                            if c.get("endpoint") == "odds"],
                    notebook="01", note=f"regions={REGIONS} markets={MARKETS}")
    prof = st.profile_frame(bronze_odds, "bronze oddsapi_odds")
    print(f"bronze oddsapi_odds: {prof.attrs['rows']} rows")
elif st.dataset_path("bronze", "oddsapi_odds").exists():
    bronze_odds = st.load_dataset("bronze", "oddsapi_odds")
    print(f"live pull unavailable — reusing prior bronze ({bronze_odds.height} rows)")
else:
    print("no odds data this run and no prior bronze — downstream notebooks "
          "will use their PM/model fallbacks")
st.profile_frame(bronze_odds, "bronze") if bronze_odds.height else None

# %%
silver_quotes = pl.DataFrame()
if bronze_odds.height:
    import datetime as dt
    rows = []
    for key, grp in bronze_odds.group_by(
            ["event_id", "bookmaker_key", "market"], maintain_order=True):
        g = grp.to_dicts()
        odds = [r["decimal_odds"] for r in g]
        if not odds or any(o is None or o <= 1.0 for o in odds):
            continue
        try:
            devigged = fv.devig(odds, p.devig_method)
        except Exception:
            continue
        ko = g[0]["commence_time"]
        mkt = g[0]["market"]
        ev = ids.event_id(g[0]["home_team"], g[0]["away_team"], ko)
        mk = ids.market_id(ev, "1x2" if mkt == "h2h" else mkt,
                           line=g[0].get("outcome_point"),
                           settlement=ids.S_90MIN)
        for r, q in zip(g, devigged):
            rows.append({
                "event_id": ev, "market_id": mk,
                "outcome_id": ids.outcome_id(mk, r["outcome_name"]),
                "source": "theoddsapi", "source_event_id": r["event_id"],
                "bookmaker": r["bookmaker_key"], "market_src": mkt,
                "outcome": r["outcome_name"],
                "decimal_odds": r["decimal_odds"], "line": r.get("outcome_point"),
                "implied_prob": 1 / r["decimal_odds"], "devig_prob": float(q),
                "devig_method": p.devig_method,
                "kickoff_utc": ko, "retrieved_utc": r["retrieved_at"],
            })
    silver_quotes = pl.DataFrame(rows)
    st.save_dataset(silver_quotes, "silver", "sportsbook_quotes",
                    inputs=["bronze/oddsapi_odds"], notebook="01")
    print(f"silver sportsbook_quotes: {silver_quotes.height} rows "
          f"({silver_quotes['event_id'].n_unique()} events, "
          f"{silver_quotes['bookmaker'].n_unique()} books)")
    display(silver_quotes.head(6).to_pandas())
else:
    print("silver sportsbook_quotes not (re)built this run")

# %%
# Events + scores + participants bronze (source-shaped, thin)
for key, name in [("events", "oddsapi_events"), ("scores", "oddsapi_scores"),
                  ("participants", "oddsapi_participants")]:
    if payloads.get(key):
        df = pl.DataFrame([{k: (str(v) if isinstance(v, (dict, list)) else v)
                            for k, v in row.items()} for row in payloads[key]],
                          infer_schema_length=None)
        st.save_dataset(df, "bronze", name, notebook="01",
                        inputs=[c.get("snapshot", "") for c in guard.calls
                                if c.get("endpoint") == key])
        print(f"bronze {name}: {df.height} rows")

# %% [markdown]
# <a id="log"></a>
# ## 6 · Full call log + quota accounting
# `est_credits` are documented-formula ESTIMATES; `quota_remaining_hdr` is
# the provider's own header — the ground truth.

# %%
call_log = guard.to_frame()
import lib.plotting as plot
plot.save_table(call_log, "01_oddsapi_call_log")
print(f"estimated credits spent this run: {guard.spent_estimated} / {MAX_CREDITS}")
print(f"provider-reported remaining: {guard.remaining_reported}")
call_log

# %% [markdown]
# <a id="findings"></a>
# ## 7 · Findings, caveats, next steps
#
# * Every v4 read endpoint has a coded, guarded path above; the call log
#   records exactly what ran, what was served from cache, and what was
#   skipped with the reason (budget, dry-run, offline, no key, no event).
# * Featured markets come from `/odds` in one cheap call; btts/DNB/props
#   are per-event only — cost scales with events × markets, which is why
#   the guard exists.
# * Historical endpoints cost 10× — they stay in dry-run until
#   `RUN_HISTORICAL=True` is set deliberately.
# * **Caveat:** quota estimates are documented formulas; observed usage
#   (headers) is what the guard trusts for the running total.
# * **Next:** notebook 02 builds the Polymarket side; notebook 03 matches
#   the two universes.
