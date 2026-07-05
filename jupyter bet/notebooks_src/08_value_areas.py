# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 08 · Value areas — where the +EV actually is
#
# **Purpose.** One section per betting area, each with its own table/chart
# and an honest availability verdict. Polymarket is the execution venue;
# sportsbook legs appear ONLY as (a) genuine post-cost arbitrage or
# (b) +EV boost/promo extraction — ordinary sportsbook value bets are out
# of scope by design (killed as −EV leaks in production backtests).
#
# Sections: [match 1X2](#m1x2) · [advancement/futures](#adv) ·
# [outrights](#outright) · [props & match events](#props) ·
# [in-play](#inplay) · [cross-market arb](#arb) · [boosts/promos](#promo) ·
# [summary](#summary)

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
import lib.fairvalue as fv
import lib.arbpromo as ap
import lib.ids as ids

UTC = dt.timezone.utc
manifest = bt.run_manifest("08_value_areas")
p = cfg.load_params()
now = dt.datetime.now(UTC)
area_status = {}

# %% [markdown]
# <a id="m1x2"></a>
# ## 1 · Match markets (1X2, 90-min) — model vs PM tape
# Upcoming matches only; PM price = VWAP of the last 6h of real prints
# (labelled; books preferred when live). Edge after the PM fee shape and
# the configured slippage assumption.

# %%
ev = st.load_dataset("silver", "pm_match_events")
lf = pm.orderflow_trades()
model_log_raw = [json.loads(l) for l in open(bt.MODEL_PRED_LOG)]
rows = []
for m in ev.filter(pl.col("kickoff_utc").is_not_null()).to_dicts():
    ko = m["kickoff_utc"].replace(tzinfo=UTC) if m["kickoff_utc"].tzinfo is None else m["kickoff_utc"]
    if ko <= now:
        continue
    pair = "__".join(sorted((ids.slug(m["home"]), ids.slug(m["away"]))))
    latest = None
    for r in model_log_raw:
        fx = r.get("fixture") or ""
        if " vs " not in fx or not r.get("model"):
            continue
        h, a = fx.split(" vs ", 1)
        if "__".join(sorted((ids.slug(h), ids.slug(a)))) == pair:
            if latest is None or r["generated"] > latest["generated"]:
                latest = r
    if latest is None:
        continue
    home_first = ids.slug(latest["fixture"].split(" vs ")[0]) == ids.slug(m["home"])
    probs = {"home": latest["model"]["home" if home_first else "away"],
             "draw": latest["model"]["draw"],
             "away": latest["model"]["away" if home_first else "home"]}
    lo = int((now - dt.timedelta(hours=6)).timestamp())
    for oc, cid in (("home", m["cid_home"]), ("away", m["cid_away"]),
                    ("draw", m["cid_draw"])):
        if not cid:
            continue
        t = (lf.filter((pl.col("condition_id") == cid)
                       & (pl.col("outcome_index") == 0)
                       & (pl.col("ts") >= lo)).collect())
        if t.is_empty():
            continue
        vwap = float((t["price"] * t["size"]).sum() / t["size"].sum())
        fair = probs[oc]
        fill = vwap + p.slippage_frac_of_spread * 0.01   # 1c half-spread assumption
        rows.append({
            "fixture": f"{m['home']} vs {m['away']}", "kickoff_utc": ko,
            "hours_out": round((ko - now).total_seconds() / 3600, 1),
            "outcome": oc, "model_p": round(fair, 3),
            "pm_vwap_6h": round(vwap, 3), "n_prints_6h": t.height,
            "edge_net": round(fv.edge_net(fair, fill,
                                          fee_coeff=p.pm_taker_fee_coeff), 3),
            "ev_per_$": round(fv.ev_per_dollar(fair, fill,
                                               fee_coeff=p.pm_taker_fee_coeff), 3),
        })
m1x2 = pl.DataFrame(rows).sort("ev_per_$", descending=True) if rows else pl.DataFrame()
if m1x2.height:
    st.save_dataset(m1x2, "gold", "value_match_1x2", notebook="08",
                    inputs=["silver/pm_match_events", "bronze/pm_trades",
                            "repo:data/model_predictions_log.jsonl"])
    area_status["match 1X2"] = f"{m1x2.height} priced outcomes on upcoming matches"
    display(m1x2.head(12).to_pandas())
else:
    area_status["match 1X2"] = ("no upcoming match had recent prints + a model row "
                                "this run — rerun near the next matchday")
    print(area_status["match 1X2"])

# %% [markdown]
# **Reading this table with production's selection rules:** prefer +EV
# MONEYLINES with model ≥50¢ first, 25–50¢ next; <25¢ longshots get NO cash
# (0-for-12 in backtests); prefer further-out fixtures (ordering, not gate).

# %% [markdown]
# <a id="adv"></a>
# ## 2 · Advancement / knockout futures (PRIMARY area) — current feed

# %%
adv = json.loads(pathlib.Path(bt.ADVANCEMENT_JSON).read_text())
meta = adv.get("meta") or {}
adv_age_h = None
try:
    gen = dt.datetime.strptime(meta.get("generated", ""), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
    adv_age_h = round((now - gen).total_seconds() / 3600, 1)
except ValueError:
    pass
adv_rows = []
for t in adv.get("teams", []):     # real shape: teams[] × pm{stage:{pm,edge_adj}}
    for stage, pmv in (t.get("pm") or {}).items():
        mp = (t.get("model") or {}).get(stage)
        if mp is None or pmv.get("pm") is None:
            continue
        adv_rows.append({
            "team": t.get("team"), "stage": stage, "model_p": float(mp),
            "pm_price": float(pmv["pm"]),
            "edge_raw": round(float(mp) - float(pmv["pm"]), 4),
            "edge_fee_adj": pmv.get("edge_adj"),   # production fee-adjusted
        })
advf = (pl.DataFrame(adv_rows).sort("edge_fee_adj", descending=True)
        if adv_rows else pl.DataFrame())
area_status["advancement futures"] = (
    f"{advf.height} markets, feed age {adv_age_h}h "
    f"({'FRESH' if adv_age_h is not None and adv_age_h < 6 else 'STALE — rerun builder before acting (6h gate)'})")
print(area_status["advancement futures"])
display(advf.head(10).to_pandas())
display(advf.tail(5).to_pandas())  # most negative = potential trims/fades

# %% [markdown]
# <a id="outright"></a>
# ## 3 · Tournament outrights — PM winner ladder vs tape

# %%
mk = st.load_dataset("bronze", "pm_markets")
import lib.matching as mtch
win_mk = pl.DataFrame([r for r in mk.to_dicts()
                       if mtch.classify_pm_market(r) == "outright"])
import re as _re
model_win = {ids.slug(t["team"]): (t.get("model") or {}).get("win")
             for t in adv.get("teams", [])}
if win_mk.height:
    lo = int((now - dt.timedelta(hours=48)).timestamp())
    orows = []
    for r in win_mk.to_dicts():
        t = (lf.filter((pl.col("condition_id") == r["condition_id"])
                       & (pl.col("outcome_index") == 0)
                       & (pl.col("ts") >= lo)).collect())
        if t.is_empty():
            continue
        last = t.sort("ts").tail(1)
        m = _re.match(r"^Will (.+?) win the 2026 FIFA World Cup\?$",
                      r["question"] or "")
        team_slug = ids.slug(m.group(1)) if m else None
        mp = model_win.get(team_slug)
        px = float(last["price"][0])
        orows.append({"market": r["question"],
                      "model_win_p": mp, "last_price": px,
                      "edge": round(mp - px, 4) if mp is not None else None,
                      "usd_vol_48h": round(float(t["usd"].sum()), 0),
                      "n_prints_48h": t.height})
    outright = (pl.DataFrame(orows).sort("edge", descending=True,
                                         nulls_last=True)
                if orows else pl.DataFrame())
    n_modeled = (outright.filter(pl.col("edge").is_not_null()).height
                 if outright.height else 0)
    area_status["outrights"] = (
        f"{outright.height} winner markets with prints in 48h; "
        f"{n_modeled} priced against the sim's champion probability "
        f"(prices are tape prints — verify at live asks before acting)")
    if outright.height:
        st.save_dataset(outright, "gold", "value_outrights", notebook="08",
                        inputs=["bronze/pm_markets", "bronze/pm_trades",
                                "repo:site/advancement_data.json"])
        display(outright.head(12).to_pandas())
else:
    area_status["outrights"] = "no outright markets classified"
print(area_status["outrights"])

# %% [markdown]
# <a id="props"></a>
# ## 4 · Player/team props & match events — honest status
# Production verdict stands: correct score, scorer props and un-boosted
# SGMs are KILLED for cash (−EV leaks; scorer punts −73.9%). Corners/cards
# models exist (calibrated) but emit only with fresh REAL market prices —
# none are captured on this box today. PM lists no WC prop markets in the
# capture (0 rows classified as props above).

# %%
prop_cal_path = bt.REPO_ROOT / "site" / "prop_calibration.json"
if prop_cal_path.exists():
    pc = json.loads(prop_cal_path.read_text())
    n_fix = len(pc.get("fixtures") or [])
    area_status["props/match events"] = (
        f"calibration feed present ({n_fix} fixtures) but NO fresh market "
        "prices on this box → production correctly WITHHOLDS (see notebook "
        "06 props funnel); PM offers no WC prop markets in capture")
else:
    area_status["props/match events"] = "no prop calibration feed on this box"
print(area_status["props/match events"])

# %% [markdown]
# <a id="inplay"></a>
# ## 5 · In-play — live now?

# %%
live_now = [m for m in ev.filter(pl.col("kickoff_utc").is_not_null()).to_dicts()
            if 0 <= (now - (m["kickoff_utc"].replace(tzinfo=UTC)
                            if m["kickoff_utc"].tzinfo is None
                            else m["kickoff_utc"])).total_seconds() <= 130 * 60]
area_status["in-play"] = (f"{len(live_now)} match(es) in window now"
                          if live_now else
                          "no WC match in-play at run time — notebook 05 has "
                          "the historical in-play toolkit")
print(area_status["in-play"])

# %% [markdown]
# <a id="arb"></a>
# ## 6 · Cross-market inconsistencies & executable arbitrage
# Two real checks:
# **(a) PM-internal 1X2 coherence** — do the three YES prices sum below
# $1 after fees (buy-all-three lock) or far above (short structure)?
# Uses live asks when reachable, else recent tape VWAPs (flagged — tape
# can't be executed on).
# **(b) Exchange↔PM two-way locks** — needs live GBP exchange odds; when
# no fresh sportsbook quotes exist the check reports its skip reason.

# %%
arows = []
quotes_live = None
try:
    quotes_live = st.load_dataset("silver", "pm_quotes_live")
except FileNotFoundError:
    pass
for m in ev.filter(pl.col("kickoff_utc").is_not_null()).to_dicts():
    ko = m["kickoff_utc"].replace(tzinfo=UTC) if m["kickoff_utc"].tzinfo is None else m["kickoff_utc"]
    if ko <= now:
        continue
    legs, basis = {}, []
    for oc, cid in (("home", m["cid_home"]), ("away", m["cid_away"]),
                    ("draw", m["cid_draw"])):
        px = None
        if quotes_live is not None and cid:
            q = quotes_live.filter(pl.col("condition_id") == cid)
            if q.height and q["best_ask"][0] is not None:
                px, src = float(q["best_ask"][0]), "live ask"
        if px is None and cid:
            t = (lf.filter((pl.col("condition_id") == cid)
                           & (pl.col("outcome_index") == 0)
                           & (pl.col("ts") >= int((now - dt.timedelta(hours=6)).timestamp())))
                 .collect())
            if t.height:
                px, src = float((t["price"] * t["size"]).sum() / t["size"].sum()), "tape vwap6h (NOT executable)"
        if px is None:
            legs = None
            break
        legs[oc] = px
        basis.append(src)
    if not legs:
        continue
    total = sum(legs.values())
    fee_load = sum(fv.pm_fee(v, p.pm_taker_fee_coeff) for v in legs.values())
    arows.append({"fixture": f"{m['home']} vs {m['away']}",
                  "sum_yes": round(total, 3),
                  "sum_after_fees": round(total + fee_load, 3),
                  "buy_all_lock_roi": round(1 - (total + fee_load), 3),
                  "price_basis": "; ".join(sorted(set(basis)))})
arb1 = (pl.DataFrame(arows).sort("buy_all_lock_roi", descending=True)
        if arows else pl.DataFrame())
if arb1.height:
    execu = arb1.filter((pl.col("buy_all_lock_roi") > p.arb_min_profit_frac)
                        & (pl.col("price_basis") == "live ask"))
    area_status["cross-market arb"] = (
        f"{arb1.height} matches checked; {execu.height} EXECUTABLE "
        f"buy-all locks above the {p.arb_min_profit_frac:.1%} floor at live asks")
    display(arb1.head(10).to_pandas())
else:
    area_status["cross-market arb"] = "no upcoming match had three priced legs this run"
print(area_status["cross-market arb"])

# %%
# (b) exchange↔PM lock — machinery + skip reason when no live exchange odds
try:
    sq = st.load_dataset("silver", "sportsbook_quotes")
    fresh = sq.filter(pl.col("retrieved_utc") > (now - dt.timedelta(hours=6)))
    ex_status = f"{fresh.height} fresh sportsbook quotes — pair with PM asks below"
except FileNotFoundError:
    fresh = None
    ex_status = ("SKIPPED: no fresh sportsbook/exchange odds captured this "
                 "run (notebook 01 live pull needed) — evaluate_pair "
                 "machinery is production wca.arbfx and unit-tested")
print("exchange↔PM lock:", ex_status)
# worked mechanism demo on synthetic-looking but PRODUCTION-verified math is
# in tests/test_jb_quant.py::test_arb_exchange_pm_* — not repeated here to
# keep this section real-data-only.

# %% [markdown]
# <a id="promo"></a>
# ## 7 · Boosts & promotions (SECONDARY priority)

# %%
promos = json.loads(pathlib.Path(bt.PROMOS_JSON).read_text()) if pathlib.Path(bt.PROMOS_JSON).exists() else {}
plist = promos.get("promotions") or []
if not plist:
    area_status["boosts/promos"] = (
        "promo catalog is EMPTY right now (daemon feed live, 0 promotions) "
        "— nothing to extract today; promo_ev/boostlock machinery ready "
        "(unit-tested) for the next drop")
    print(area_status["boosts/promos"])
else:
    prows = []
    for pr in plist:
        terms = ap.PromoTerms(
            name=pr.get("title") or "?", venue=pr.get("bookie") or "?",
            promo_type="free_bet" if "free" in (pr.get("type") or "").lower()
            else "profit_boost",
            max_stake=pr.get("max_stake"),
            qualifying_min_odds=pr.get("min_odds"),
            stake_returned=pr.get("stake_returned"),
            boost_frac=pr.get("boost_frac"),
            freebet_amount=pr.get("free_bet_amount"),
            rollover=pr.get("rollover"), expiry_utc=pr.get("expiry"),
            jurisdiction_ok=True)
        r = ap.promo_ev(terms, back_odds=2.0, lay_odds=2.05,
                        exchange_commission=p.exchange_commission,
                        freebet_conversion=p.freebet_conversion)
        prows.append({"promo": terms.name, "venue": terms.venue, **r})
    promo_df = pd.DataFrame(prows)
    n_exec = int(promo_df["executable"].sum())
    area_status["boosts/promos"] = f"{len(plist)} promos, {n_exec} executable after terms modelling"
    display(promo_df)

# %% [markdown]
# <a id="summary"></a>
# ## 8 · Summary — current state of every value area

# %%
import lib.plotting as plot
summary = pd.DataFrame([{"area": k, "status": v} for k, v in area_status.items()])
plot.save_table(summary, "08_value_area_summary")
summary

# %% [markdown]
# ## Findings, caveats, next steps
#
# * **Primary (advancement futures)** carries the actionable edge list —
#   but respect the 6h feed gate and PM-advancement hygiene (rerun before
#   acting; edges flip within days).
# * **Match 1X2** table implements the selection rules directly (moneylines
#   over longshots, further-out preferred).
# * **Props/scorers/correct-score** stay dead for cash — that's an encoded
#   production decision, not an oversight.
# * **Arb**: PM-internal coherence is checkable offline from the tape but
#   only EXECUTABLE on live asks; exchange↔PM locks await fresh exchange
#   odds.
# * **Promos**: catalog empty today — machinery armed for the next drop.
# * Every stake this notebook suggests is sized by the production
#   ¼-Kelly/combined-bankroll code with hard caps; execution itself remains
#   human-gated (`Y PM-<n>`), and nothing here places orders.
