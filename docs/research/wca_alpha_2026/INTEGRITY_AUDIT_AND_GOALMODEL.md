# WCA 2026 — Integrity Audit & Goal-Model Redesign

**Date:** 2026-06-30. **Scope:** World Cup Alpha (`/Users/andrewdoherty/Desktop/Coding/World Cup Alpha`).
**Authoritative played source:** `data/raw/martj42_cleaned.csv` — verified **73 WC2026 played matches**, dates 2026-06-11 .. 2026-06-28, realized **mean total goals = 2.9589**, n_matches in fit = **49,480**.
**Method discipline:** every figure below was reproduced by reading the cited file:line or by running the model read-only. Verifier verdicts are integrated inline. Numbers are tagged **real / approx / UNVERIFIED**. Nothing is invented.

---

## PART 1 — INTEGRITY AUDIT

### Bottom line up front

- **Where fabricated/placeholder data IS misleading real-money decisions (HIGH):**
  1. **Scorer EV** is priced off 238/238 `npxg_share` values that are all `source=analyst_estimate` — a placeholder presented as real, feeding anytime/first/brace fair odds and Kelly stakes. (`data/players.json`)
  2. **The whole totals/BTTS/corners/scorers goal level is biased low ~−0.50 goals/match.** Production DC fits `mu` to the full 49,480-match global history with **no WC anchor** (the anchor symbol exists only in the prototype worktree, never in production), so the per-fixture goal level (~2.45) sits well below realized WC2026 (2.9589). (`src/wca/card.py:604-610`)
  3. **Risk dashboard tiles** (`P(profit)`, `P(win>50)`, `best_case`, `worst_case`) are hardcoded constants / incoherent currency mixes presented as live risk metrics. (`src/wca/exposure_dashboard.py:75-91`)
  4. **Acca sizing reads 7-day-stale odds** (`odds_snapshots` frozen 2026-06-23) with **no max-age guard** — `accas.py` would size off stale book prices. (`src/wca/accas.py:1302`)
  5. **Action Desk open-exposure header is stale** (shows 59 open bets / worst −1096.22 when the ledger has 8 open bets). (`site/bet_recs.json`)

- **Where fabricated/stale data is NOT currently mis-deciding money (documented placeholders with safe fallbacks, or feeds with no live consumer):**
  - `squads.json` (2 teams) — attribution-only for prop display, carries no probabilities. (LOW)
  - `TEAM_PRIORS` / `PLAYER_P90_PRIORS` — degrade-to fallbacks, source-tagged `prior`, "modest so fallback never manufactures a confident edge". (LOW/MED, honest provenance)
  - `advancement_played_results.json` staleness — settle.py leaves advancement **OPEN** when no definitive bracket result exists; "no fabricated settlement" — safe by design. (LOW)
  - `advancement_latest.json` (12 days old, actually Markdown) — **no live src consumer found**; only misleads if a human reads it as "latest". (LOW)
  - `completed_fixtures.json` — self-documented view-only, "never used for P&L settlement or tracking metrics". (LOW)
  - Kelly/devig modules — no hidden edge/commission; fees are an explicit table, devig is pure math. (verified clean)

- **Important distinction on `wc2026_results.json` staleness:** it does **not** feed direct EV/sizing (that path uses `martj42_cleaned.csv`). It feeds **settlement → win-rate / CLV / rigor scorecard**, which in turn **govern Kelly-ladder rung promotion**. So a 31-of-73 stale subset corrupts the *evidence used to authorize sizing*, not the per-bet EV math. Still HIGH because it silently freezes the governance signal with no staleness guard.

### Verifier verdicts (what reproduced / what was overstated)

- **All 13 audit findings reproduced** against cited file:line and live recomputes; classifications correct, **none overstated** — with **one numeric caveat**:
- **Finding 1 numeric caveat (overstated value, immaterial):** the audit cites `mu=0.2045 → baseline 2.4409`. That **0.2045** is the **cached** value in `data/dc_params_corrected.json` (confirmed exact). The **live production fit** is `mu=0.2051 → baseline 2.4552`. Difference ~0.001 in mu / ~0.014 in baseline — does not change the conclusion (baseline ~2.45 vs realized 2.9589, bias ~−0.50). Both values are shown below.
- Independent re-computes confirmed: `odds_snapshots` MAX ts = 2026-06-23T06:52:27 (1,262,293 rows, exact); `wc2026_results.json` = 31 rows (last Turkey vs Paraguay 0-1) vs 73 played; `players.json` 238/238 `analyst_estimate`; `exposure_dashboard.py:88/91` constants, best_case=40/worst_case=−96; `bet_recs.json` open_exposure n_open=59/worst −1096.22 vs 8 actual.
- **No fabricated numbers found in either the audit or the prototype.**

---

### Surface A — GOAL / lambda / xG / EV / SIZING (the live money path)

Live path: `scripts/wca_build_card.py` → `wca.card.fit_models(load_results(...))` → `martj42_cleaned.csv` → DixonColes MLE → per-fixture lambdas → 1X2 blend (0.10 Elo / 0.30 DC / 0.60 market) + totals/BTTS/corners/cards/scorers EV → half-Kelly sizing (GBP £1,500 + PM $1,995 pools, fraction 0.50).

| What | file:line | Actual value | Class | Money risk | Feeds |
|---|---|---|---|---|---|
| DC goal-level anchor `mu` fit to full 49,480-match history (global long-run mean), NOT WC-anchored — the naive-average-as-forecast error. Per-fixture baseline understates WC goals. | `src/wca/card.py:604-610` (`DixonColesModel` fit on `_played(results)`) | **real**: live fit `mu=0.2051` → exp(mu)·2 = **2.4552** baseline (verifier). Cached `dc_params_corrected.json` `mu=0.20438…` → 2.4409. Realized WC2026 mean = **2.9589** → bias **≈ −0.50 goals/match** | naive-average-misleading | **HIGH** | DC `lambda_home`/`lambda_away` → totals (O/U 2.5), BTTS, team_total_goals, corners (scales off lambda), AnytimeScorer intensity → all EV vs offered odds + Kelly sizing |
| `DEFAULT_DC_LEVEL_TARGET` / WC mu uplift **does NOT exist in production** — anchor (`mu 0.205→0.385`) is prototype/worktree only. So the −0.50 bias is **unmitigated live for totals**. | grep `DEFAULT_DC_LEVEL_TARGET` / `dc_level_target` over `src/wca/card.py` returns **nothing**; symbol exists only at `wt-contrast/src/wca/card.py:78,603,694` | **real (verified absent)**: production `card.py:604-610` fits `mu` freely, no anchor/rescale | naive-average-misleading | **HIGH** | Absence of any goal-level anchor → understated `mu` feeds totals/BTTS/corners/scorers EV live with no correction. (1X2 partly shielded by 60% market weight; totals/BTTS are NOT) |
| `data/players.json` `npxg_share` ALL `source=analyst_estimate` (no empirical xG). Directly sets scorer probabilities and fair odds. | `data/players.json` (`_note`: "Analyst-estimated…pending a live xG / penalty-taker feed"); consumed `src/wca/nextmatch.py:402`, `src/wca/accas.py:490` via `ScorerPricer.price_player` | **real**: **238/238** records `analyst_estimate` (+1 provenance-tag note record = 239 entries). e.g. Messi 0.28, Lautaro 0.22 (pen taker), Alvarez 0.15 | placeholder-presented-as-real | **HIGH** | `npxg_share` → `ScorerPricer.intensity` → p_anytime/p_first/p_two_plus → `model_fair_anytime`, `double_delight_ev` → EV vs offered scorer odds (nextmatch, accas legs) |
| `BlendWeights` default 0.10/0.30/0.60 (Elo/DC/market). Documented backtest: blend does NOT beat de-vigged market with confidence (Δ −0.0031 nats, P(beat)=60.2%). | `src/wca/card.py:116-118` | **real**: elo=0.10, dc=0.30, market=0.60 | ok-verified (honest, documented choice) | MEDIUM | Blended 1X2 → edge/EV gate + Kelly stake for every match-result bet. 60% market weight mitigates DC goal bias for 1X2 **but NOT** for totals/BTTS (raw DC lambdas) |
| `pen_xg` hard-coded constant for designated penalty taker, used uniformly for every team. | `src/wca/models/props.py:218`; `src/wca/models/scorers.py:72` | **real**: `pen_xg=0.18` | placeholder-presented-as-real | MEDIUM | +0.18 to pen-taker scorer intensity → anytime/first scorer EV; no team penalty-rate variation |
| `BASE_TEAM_LAMBDA` hard-coded 1.35, scales team shots/SoT & player-SoT context. Note: 2.7/team base < realized 2.99, compounding the goal-level understatement. | `src/wca/models/betbuilder.py:62` | **real**: `BASE_TEAM_LAMBDA = 1.35` | placeholder-presented-as-real | MEDIUM | `_scaled_team_mean` denom for team_total_shots/sot and player_SoT context multiplier → those prop EVs |
| `TEAM_PRIORS` shots/sot/fouls means+dispersions are order-of-magnitude WC values pending refit. | `src/wca/models/betbuilder.py:66-70` | **real**: shots (12.0,18.0), sot (4.2,9.0), fouls (11.5,22.0) | documented-placeholder-with-fallback | MEDIUM | team_total_shots/sot/fouls EV when no `players.db` rate (RateStore degrades to these). Source tagged `prior` — honest provenance, but EV is published off them |
| `PLAYER_P90_PRIORS` generic per-90 fallback rates, applied with no opponent/defence adjustment in player-prop path. | `src/wca/models/betbuilder.py:75-80` | **real**: sot=0.7, fouls=1.2, yellows=0.18; dispersion sot=4.0, fouls=6.0 | documented-placeholder-with-fallback | MEDIUM | player_shots_on_target / player_fouls / player_to_be_booked EV when rate is None. Comment: "deliberately modest so fallback never manufactures a confident edge", source `prior` |
| `CardsModel` base rate hard-coded; `aggression` default 1.0 (no team foul-rate adjustment in default path). | `src/wca/models/props.py:169`; used `card.py:1358`; `betbuilder.py:504-510` | **real**: base_cards=3.41, dispersion=6.9, aggression=1.0, stakes_mult=1.0 | placeholder-presented-as-real | LOW | match_total_cards / team cards EV; flat tournament base unless a caller injects priors (none in default path) |
| `CornersModel` base rates hard-coded WC18+22 StatsBomb constants (not refit); `base_goals=3.07` > realized 2.99 → corner xG scaling mildly miscentered. | `src/wca/models/props.py:91-92` | **real**: base_corners=8.97, base_goals=3.07, dispersion=157.5, elasticity=0.30 | placeholder-presented-as-real | LOW | `CornersModel.mean_total/team_mean` → corners O/U and team corners EV (calibration documented from 128 WC matches) |
| `data/squads.json` populated for only 2 of 48 teams. Attribution-only. | `data/squads.json` (keys: United States, Australia; plus `_note`, `_schema`) | **real**: 2 teams; `_note` → unlisted players fall to "unknown team" bucket | documented-placeholder-with-fallback | LOW | Attribution-only for goalscorer prop **display**; carries no goal figures; does not set probabilities/EV |
| Kelly/devig contain no hidden edge or commission; fees explicit, devig pure math. | `src/wca/markets/kelly.py:54-129`; `src/wca/markets/devig.py:104-258`; fees `betbuilder.py:87-93` | **real**: fees betfair=0.02, smarkets=0, polymarket=0, sportsbook=0; rungs 0.25/0.35/0.50, max_odds_unvalidated=10.0; DUAL_POOL_KELLY_FRACTION=0.50 | ok-verified | LOW | Fee table → net_odds in ev_vs_offer/edge; Kelly fraction → stake. No fabricated values; 2% Betfair commission is a real exchange fee |

### Surface B — DATA FRESHNESS (settlement, odds, snapshots)

Authoritative `martj42_cleaned.csv` = 73 played through 2026-06-28; audit pipeline last ran 2026-06-29T17:49:52Z. Reference "now" = 2026-06-30.

| What | file:line | Actual value | Class | Money risk | Feeds |
|---|---|---|---|---|---|
| `wc2026_results.json` holds only **31 of 73** played matches; last date 2026-06-20 (Turkey vs Paraguay 0-1). Presented as "Manually-maintained 2026 World Cup results". No staleness/last-date guard. | `data/processed/wc2026_results.json` (len=31, max 2026-06-20); `src/wca/predledger/settle.py:48-67,148-187`; `backfill.py:29,145-147`; `winrate.py:7`; `rigor/build.py:7` | **real**: rows=31, range 2026-06-11..06-20; lag = **42 unsettled played matches / 8 days** | stale-presented-as-current | **HIGH** | Does NOT feed direct EV/sizing (that uses martj42). Feeds settle/backfill → CLV-to-date → **GATES Kelly-ladder rung promotion** + win-rate/rigor. `settle_open()` silently counts 42 missing matches as "unsettled" (settle.py:166-168), no error raised → paper book & hit-rate computed on stale subset |
| rigor scorecard + win-rate reconstructed from the same 31-match file → Brier / hit-rate measured on 31, not 73. No coverage guard. | `src/wca/rigor/build.py:192-199`; `src/wca/winrate.py:6-9` | **real**: results loaded = 31; gap of 42 never enter scorecard | stale-presented-as-current | **HIGH** | Model-confidence / blend-weight decisions read off the scorecard & win-rate (informs whether to trust/size the model) |
| Production `wca.db` `odds_snapshots` (OddsAPI h2h prices) stopped 2026-06-23; ~7 days stale. `accas.py` sizes off `MAX(ts_utc)` with **no max-age guard**. | `data/wca.db` odds_snapshots (ro): MAX(ts_utc)=2026-06-23T06:52:27.484258+00:00, **1,262,293 rows**; consumed `src/wca/accas.py:1302`, `closecapture.py:219,257,266` | **real (exact)**: MAX ts 2026-06-23 vs now 06-30 → ~7 days | stale-presented-as-current | **HIGH** | `accas.py:94,107-112` prices acca legs off `MAX(ts_utc)` book odds, `edge=model_prob·eff_odds−1` + stake, **no age guard** → would size off 7-day-old odds. closecapture stamps CLV from these (sparse/old → CLV mostly NULL) |
| `card.py` blend-weight docstring hardcodes "24 played matches" (dated 2026-06-18) as rationale for deployed 0.10/0.30/0.60 weights + ~14pp draw under-prediction. Not re-validated vs 73 played. | `src/wca/card.py:101-118` | **real**: docstring asserts diagnostics on 24 matches; deployed weights 0.10/0.30/0.60; authoritative count now 73 | stale-presented-as-current | MEDIUM | Weights feed `build_card` edge/sizing (build_card:1071+). Calibration narrative frozen at 24-match snapshot (per baseline, OLD per-fixture total carried bias −0.518 vs realized 2.9589) |
| `pm_price_history.jsonl` last row 2026-06-29 18:32 UTC; ~1.5 days behind. Bot staleness banners gate on cached card "generated" stamp, NOT on underlying feed freshness. | `data/pm_price_history.jsonl` (2057 rows, 2026-06-28 11:23 .. 06-29 18:32); `src/wca/bot/app.py:60` | **real**: max ts 2026-06-29 18:32; n=2057 (06-28: 1127, 06-29: 930) | stale-presented-as-current | MEDIUM | bot reads PM price series; banner (`_stale_banner`/`CARD_MAX_AGE_HOURS`, app.py:95-103,627,768,806) checks card age, not results/odds freshness feeding the card |
| `advancement_latest.json` is actually Markdown (not JSON), generated 2026-06-18 10:31 UTC; 20k-sim edges 12 days old. | `data/advancement_latest.json:1-5` ("_Generated 2026-06-18 10:31:26 UTC._", "20000 sims, seed 42") | **real**: gen 2026-06-18; **no src/ consumer found** (grep empty) | stale-presented-as-current | LOW | No live src consumer; only misleads a human reading it as "latest" (12-day-stale edges). No automated EV/sizing path consumes it |
| `completed_fixtures.json` stale (mtime 2026-06-21), lists 4 fixtures while 73 played. | `data/processed/completed_fixtures.json:3-8`; `_comment`: "never used for P&L settlement or tracking metrics" | **real**: 4 fixtures (Czechia v S.Africa, Switzerland v Bosnia, England v Ghana, Netherlands v Sweden) | documented-placeholder-with-fallback | LOW | View-only — hides dead pre-match markets; explicitly not used for settlement/P&L |
| `advancement_played_results.json` 24 group-stage results, mtime 2026-06-18; used for advancement settlement. | `data/advancement_played_results.json` (24 records); `src/wca/predledger/settle.py:70-84,160-163` | **real**: count=24 | documented-placeholder-with-fallback | LOW | settle.py deliberately leaves advancement predictions **OPEN** where no definitive bracket result exists (settle.py:160-163,20-21 "no fabricated settlement") → staleness cannot mis-settle; safe by design |
| Production `wca.db` `pm_snapshots` = 155 rows, all at one timestamp 2026-06-29 07:15 UTC (no series). | `data/wca.db` pm_snapshots (ro): COUNT=155, MIN=MAX='2026-06-29 07:15 UTC' | **real**: count=155, single timestamp | stale-presented-as-current | LOW | Single-timestamp snapshot; no PM time series for line-move/CLV. Not a primary live EV input (card uses live PM mid) |
| `card_latest.md` generated 2026-06-29T17:49:52 (~1 day old); 5 indicative picks. Card age IS guarded. | `data/card_latest.md:1` | **real**: gen 2026-06-29T17:49:52; picks all $0.00 / INDICATIVE (single-source Polymarket) | ok-verified | LOW | Bot applies staleness banner via `CARD_MAX_AGE_HOURS` on "generated" stamp (app.py:643,687); all picks $0 indicative / not staked |
| `model_predictions.json` / `…_log.jsonl` fresh (gen 2026-06-30T11:53:56), correct upcoming knockouts. | `data/model_predictions.json:368-370`; `…_log.jsonl` mtime 2026-06-30 14:56 | **real**: gen 2026-06-30T11:53:56; fixtures = upcoming R32/R16 (Ivory Coast v Norway 06-30 .. Canada v Morocco 07-04) | ok-verified | LOW | Prediction emitter — verified current. The stale settlement gap is in `wc2026_results.json`, not here |

### Surface C — LEDGER / SITE / BET-RECS / ARB

| What | file:line | Actual value | Class | Money risk | Feeds |
|---|---|---|---|---|---|
| `p_profit` is a hardcoded constant (0.6/0.4/0.5 by EV sign), published as live "P(profit)". | `src/wca/exposure_dashboard.py:88`; shipped `site/exposure_dashboard.json` p_profit=0.6; rendered `site/app.js:1174` | **real**: 0.6 (never derived from bet distribution) | placeholder-presented-as-real | **HIGH** | Risk & Blind Spots "P(profit)"; also `site/bet_recs.json` meta.open_exposure.p_profit (Action Desk) |
| `p_win_50` hardcoded constant (0.3 if best_case>50 else 0.1), presented as P(win > £50). | `src/wca/exposure_dashboard.py:91`; shipped p_win_50=0.1; `site/app.js:1176` | **real**: 0.1 | placeholder-presented-as-real | **HIGH** | Risk dashboard "P(win>50)" tile |
| `worst_case` mixes GBP + USD into one number, assumes incoherent joint scenario (every non-fav loses AND the one fav never wins); labeled GBP. | `src/wca/exposure_dashboard.py:80-84`; `site/app.js:1173` money(…, 'GBP'); shipped worst_case=−96.0 | **real**: −96.0 = −(£85 sportsbook + $11 polymarket) summed, shown as −£96 | naive-average-misleading | **HIGH** | Risk dashboard "worst case"; bet_recs meta.open_exposure.worst_case |
| `best_case` counts only bets with model_prob>0.5 (fallback 0.5 excludes 7 of 8 open bets w/ null prob); the single included bet is USD shown as GBP. | `src/wca/exposure_dashboard.py:75-79`; `site/app.js:1172` money(…, 'GBP'); shipped best_case=40.0 | **real**: 40.0 = bet id 14 only, 60·(1.6667−1)=40.0 **USD** displayed as £40 | naive-average-misleading | **HIGH** | Risk dashboard "best case" tile |
| Shipped `site/bet_recs.json` embeds STALE open-exposure (59 open, worst −1096.22) vs current ledger (8 open, naive sum −96). | `site/bet_recs.json` meta.open_exposure {n_open:59, worst_case:−1096.22, best_case:0, p_profit:0.6}; gen 2026-06-29 17:54; `data/wca.db` open=8 | **real**: n_open=59, worst −1096.22 (frozen @ commit 5456b22); ledger now 8 open | stale-presented-as-current | **HIGH** | Action Desk open-exposure context header |
| Portfolio EV headline = a single bet's EV (only bet 14 has non-null ev), presented as total open-book EV. | `src/wca/exposure_dashboard.py:68,95`; shipped ev=0.14 | **real**: 0.14 (7 of 8 open bets ev=NULL; only bet 14 contributes 0.1416895, in USD) | naive-average-misleading | MEDIUM | Risk dashboard EV; bet_recs meta.open_exposure.ev |
| `bet_recs` sportsbook_pool reports source='ledger' bankroll £2000 but n_settled=0 / clv_to_date=null while ledger has 62 settled — looks like fallback default, not a real CLV-ladder read. | `site/bet_recs.json` meta.sportsbook_pool {bankroll:2000.0,n_settled:0,source:'ledger'}; `scripts/wca_betrecs.py:51` DEFAULT_BANKROLL_GBP=2000.0; ledger settled=62 | **real**: bankroll=2000.0, n_settled=0 (DB has 62 settled: 17 won/45 lost/7 void) | stale-presented-as-current | MEDIUM | Kelly sizing base for all match_singles stakes (max_stake = bankroll·0.05 = 100) |
| FX fallback constant disagrees across modules (1.27 vs 1.33); both documented fallbacks; FX feeds combined-display only. | `scripts/wca_betrecs.py:60` FX_FALLBACK_GBP_USD=1.27 vs `src/wca/fx.py:16`=1.33, `accas.py:230/605`=1.33; `site/arb_data.json` fx=1.33 source='fallback' | **real**: 1.27 vs 1.33 (disclosed fallbacks) | documented-placeholder-with-fallback | LOW | Combined-currency display only; not per-leg sizing |
| `blind_spots` / `worst_result_states` are documented placeholders (empty lists; analysis deferred); real engine in exposure.py. | `src/wca/exposure_dashboard.py:102-103`; shipped blind_spots=[], worst_result_states=[] | **real**: [] (explicitly marked placeholder) | documented-placeholder-with-fallback | LOW | Risk dashboard blind-spot section (real correlated engine output is in exposure_data.json) |
| Shipped `site/arb_data.json` stale (gen 2026-06-23, fx fallback) but honest: arbs=[], cum_pct=0, explicit HYPOTHETICAL banner; no synthetic quotes. | `site/arb_data.json` meta.generated='2026-06-23 10:48:14 UTC'; `src/wca/arbdata.py:162-172` | **real**: arbs=[], hypothetical.cum_pct=0 | ok-verified | LOW | Arb tab + bet_recs guaranteed_arbs (currently empty) |
| The 8 open bets in `data/wca.db` are real ledger rows (mix GBP sportsbook + USD polymarket, plausible). | `data/wca.db` bets WHERE status='open' → ids 11,14,57,88,94,99,100,101; total stake 156.0 | **real**: 8 open bets (e.g. id14 Japan R16 polymarket 1.6667 @60; id101 Acca 2UP virginbet 3.37 @50) | ok-verified | LOW | Positions/exposure across the site |
| `sitedata` derives all totals/positions/P&L/CLV from ledger + card; live PM with silent ledger fallback; no hardcoded P&L. | `src/wca/sitedata.py:471-778`; live_pm_positions `:303-373` | **real**: no hardcoded financial constants; VENUE_CURRENCY/CURRENCY_SYMBOL are static lookups only | ok-verified | LOW | Main terminal data.json (totals/positions/pnl) |

---

## PART 2 — GOAL-MODEL REDESIGN

### Objective

Replace the misleading "average goals so far" intuition with an **opponent-difficulty-adjusted, two-timescale Dixon-Coles** goal-expectation for each remaining knockout tie. The naive average fails specifically in knockouts because matchups never repeat and a team's raw goals-for bakes in *who it already played*. Dixon-Coles solves this structurally: it fits attack/defence jointly by netting out the opponent's defence in the likelihood, so opponent-difficulty adjustment is **intrinsic, not a bolt-on** (`src/wca/dixon_coles.py:568-569`).

### Methodology spec

**Primitive:** two DC fits on the *same* 49,480-played history (`martj42_cleaned.csv`, verified n=49,480). Not goals-per-match averages.

1. **LONG-term DC** (component: longer-term DC). Deployed fit, `half_life_years=8.0` → `mu≈0.205` (exp 1.227), `rho≈−0.05`, `home_adv≈0.273` (verified by running `fit_models`, `card.py:521-612`). Effectively out-of-sample-dominated by 49,480 historical matches. Provides `attack^LONG`/`defence^LONG`, opponent-defence-netted by construction. **data_ready: now (verified).**

2. **TOURN-decayed DC** (component: tournament-decay). Second `DixonColesModel` at `half_life_years≈0.5` (`xi = xi_from_half_life(0.5)`, `dixon_coles.py:100-108`) on the same history. Short half-life up-weights WC2026 + recent internationals while the **same machinery still nets out opponent defence**. Verified buildable: fits, `mu=0.0892`, yields opponent-adjusted totals distinct from LONG (Brazil-Spain 2.487 vs 2.232; Mexico-S.Korea 1.847 vs 2.117). **data_ready: now (verified; half-life set OOS).** *Honest in-sample note:* a 0.5-yr half-life is heavily weighted toward the 73 WC2026 matches it is also characterising — intended (captures current form) but means TOURN is partly in-sample on tournament form.

3. **Per-team / per-parameter blend (shrinkage), fit OOS, never hand-set:**
   `attack_A* = w_A·attack_A^TOURN + (1−w_A)·attack_A^LONG`; `defence_A* = w_A·defence_A^TOURN + (1−w_A)·defence_A^LONG` (same for B), with credibility weight
   **`w_t = n_t / (n_t + k)`**, `n_t` = decay-weighted recent match count (`DixonColesModel.match_counts`, `dixon_coles.py:514`), `k` fit OOS. Few recent matches → low `w_t` (trust LONG); deep into WC2026 → higher `w_t`. James-Stein / credibility form. Reuses the existing convex-blend pattern (`BlendWeights/normalised`, `card.py:89-124`). **data_ready: buildable now; `k` fit OOS.** *Caveat:* ~73 WC2026 matches ≈ 1 cluster → `k` weakly identified from 2026 alone.

4. **Opponent-difficulty adjustment (intrinsic):** primary channel is the blended `defence_B*` entering `lambda_A` directly — exactly "evaluated against the specific next opponent". No naive opponent-goals-conceded average anywhere.

5. **Next-opponent ELO context (a difficulty CHECK, not a replacement):** opponent B's ELO (`elo.py EloRater.get_rating/_rating_diff:228-290`) (a) cross-checks `defence_B*`, and (b) feeds the deployed convex ELO+DC 1X2 blend (`advancement.py:236-254`; weights 0.10/0.30/0.60, `card.py:116-118`). Used to harden `w_t` when recent opponents were anomalously weak/strong vs their ELO, so a soft-schedule run-up does not leak into `attack*`. **data_ready: now (ELO already fit/deployed).**

6. **Squad-quality adjustment — HONEST DATA LIMIT (data-gated):** intended additive log-rate nudge `delta_squad` on `attack*`/`defence*` (units of the DC structural prior, scale ~0.15 at 1sd, `structural.py:76`). **No calibrated squad-strength feed exists.** Verified: `data/squads.json` = 2 real teams only (United States, Australia; other keys `_note`/`_schema`), exists only to split player props by team (`nextmatch.py:204-236`); `data/players.json` = 44 teams but all 238 records `source=analyst_estimate`, field `npxg_share` is a within-team xG *share* for prop pricing (`nextmatch.py:371`), **not** a team aggregate. **Deployable form = the structural socio-economic prior already wired into DC** (`dc_priors_from_factors`, `structural.py:205-212`; `country_factors.csv` covers all 48 entered teams — verified): a documented coarse PRIOR with safe fallback (LOW risk). **Fabricating per-team squad strengths from `analyst_estimate` npxg shares and feeding EV/sizing = the HIGH-risk error this directive forbids — explicitly NOT done.** Real squad ratings are a future feed (replace `analyst_estimate` as live xG lands, per `players.json _note`).

### Per-fixture lambda formula

With blended params and `mu^blend = w̄·mu^TOURN + (1−w̄)·mu^LONG`:

```
log lambda_A = mu^blend + attack_A* − defence_B*  (+ gamma_home if the fixture is not neutral)
log lambda_B = mu^blend + attack_B* − defence_A*
Expected total = lambda_A + lambda_B
O/U(2.5), BTTS, team totals via existing score_matrix / over_under  (dixon_coles.py:686-723, 195-211)
```

Opponent B's blended **defence** enters `lambda_A` directly — opponent difficulty is carried here, with ELO as the orthogonal confirm. Identity verified at `dixon_coles.py:682-684` (`expected_lambdas`).

### Prototype contrast table — naive vs long-DC vs blend (per remaining tie)

All numbers below are **REAL computed values** (code: `scratchpad/wt-contrast/proto.py`; CSV: `scratchpad/report/goalmodel_proto.csv`; run `PYTHONPATH=src .venv/bin/python` against `martj42_cleaned.csv`, `reference_date=2026-06-29`). Team A = home-listed side, B = away-listed; `lam_a`/`lam_b` are A's and B's expected goals **in that fixture**.

- **naive** = team's raw avg goals scored over its WC2026 played matches only (n=3 each, GF/games). Real value, but a raw average, **not** opponent-conditioned — the misleading baseline.
- **longDC** = REAL fit, `fit_models(half_life=8.0, ref=2026-06-29, dc_level_target=2.81)` → `expected_lambdas`, `mu=0.3848`, `rho=−0.0569`, n=49,480. Opponent-adjusted, WC-level-anchored. **Verifier reproduced exactly** (France/Sweden a=1.9107/b=0.9745; Ivory Coast/Norway a=1.2095/b=1.4684; Mexico/Ecuador a=1.5974/b=1.2129).
- **blend** = **approx** of the design's tournament-decay+long blend. Second DC at short half-life 0.5yr (`mu=0.0903`), blended attack/defence = 0.60·long + 0.40·short (`BLEND_W_SHORT=0.40`), re-centred mean-zero, grafted onto the long model's anchored `mu`/`rho`/`home_adv`. **Prototype, not deployable** (see caveats).
- **opp_elo_a / opp_elo_b** = REAL World Football Elo from the same long `fit_models` run. `opp_elo_a` = A's next opponent's rating (= B's Elo); `opp_elo_b` = A's Elo.

| Tie | naive A | naive B | longDC A | longDC B | blend A | blend B | opp_elo A | opp_elo B | A's-opp/B's-opp ELO |
|---|---|---|---|---|---|---|---|---|---|
| France vs Sweden | 3.3333 | 2.3333 | 1.9107 | 0.9745 | 2.6978 | 1.2019 | 1802.6 | 2179.7 | neutral |
| Ivory Coast vs Norway | 1.3333 | 2.6667 | 1.2095 | 1.4684 | 1.2672 | 1.5072 | 1969.1 | 1860.1 | neutral |
| Mexico vs Ecuador | 2.0000 | 0.6667 | 1.5974 | 1.2129 | 1.3404 | 0.9437 | 1990.8 | 2011.0 | **non-neutral** (US-hosted home) |
| England vs DR Congo | 2.0000 | 1.3333 | 2.3876 | 0.5362 | 1.8215 | 0.5449 | 1814.3 | 2101.8 | neutral |
| United States vs Bosnia & H. | 2.6667 | 1.6667 | 2.1344 | 1.1307 | 2.6616 | 1.2959 | 1688.1 | 1874.8 | **non-neutral** (US home) |
| Belgium vs Senegal | 2.0000 | 2.6667 | 1.8042 | 1.0240 | 2.0295 | 1.2156 | 1903.5 | 1974.4 | neutral |
| Portugal vs Croatia | 2.0000 | 1.6667 | 1.6569 | 1.1162 | 2.0304 | 1.1248 | 1960.9 | 2040.9 | neutral |
| Spain vs Austria | 1.6667 | 2.0000 | 2.2876 | 0.8560 | 2.2505 | 0.9238 | 1906.5 | 2198.9 | neutral |

**Mean tie total: naive = 4.00 goals · longDC = 2.91 · blend = 3.11.** Naive runs ~1.1 goals/tie too high because it ignores opponent strength entirely.

**How the naive average misleads (concrete):**
- **England vs DR Congo** — naive puts DR Congo at 1.33 GF, but longDC drops it to **0.54** because DR Congo (Elo 1814.3) faces England's strong defence (Elo 2101.8). Naive never sees the opponent.
- **Spain vs Austria** — naive puts Austria (2.00) *above* Spain (1.67); opponent-adjustment **inverts** it (Spain 2.29, Austria 0.86) given Spain Elo 2198.9 vs Austria 1906.5.
- **Belgium vs Senegal** — naive has Senegal (2.67) out-scoring Belgium (2.00); longDC reverses to 1.80 vs 1.02.
- Naive's per-team rate also bakes in *who each team already played* (a team that drew minnows looks lethal) — does not transfer to a new opponent. Exactly why it is invalid for knockouts.

**Prototype caveats (mark every number's status):**
- `BLEND_W_SHORT=0.40` and `SHORT_HL=0.5yr` are **chosen design knobs, NOT fitted/validated against holdout** — treat blend lambdas as **approx**, not deployable.
- The blend grafts short-HL attack/defence onto the long model's anchored `mu`, so its level (mean 3.11) drifts ~0.20 above the 2.81 WC anchor; a production blend should re-run `apply_wc_level_anchor` on the blended params to re-fix the level.
- Re-centring was done over the full blended team set — a mild approximation vs re-solving the constrained MLE.
- **Squad adjustment OMITTED, not faked** — no per-player squad-strength feed wired into `expected_lambdas` within budget; per the no-fabrication rule it is left out rather than approximated. Opponent-difficulty/defence adjustment IS present (intrinsic to DC + the Elo columns).
- naive n=3 per team (group stage only) — very small samples, another reason it is noisy.
- Production-vs-prototype reminder: the WC-level anchor (`dc_level_target=2.81`, `DEFAULT_DC_LEVEL_TARGET`) exists **only in the prototype worktree**, never in production `src/` (verified). The prototype's longDC `mu=0.3848` reflects that anchor; the **live** production fit is `mu≈0.205` (baseline ~2.45), so the −0.50 totals bias is unmitigated in production today.

### Buildable now vs data-gated

**Buildable now (verified):**
- (a) LONG DC fit hl=8, n=49,480 — runs.
- (b) TOURN short-hl DC fit hl≈0.5 — runs, gives opponent-adjusted totals distinct from LONG.
- (c) per-team/per-param convex blend with `w_t = n_t/(n_t+k)` reusing `DixonColesModel.match_counts` + existing `BlendWeights` pattern.
- (d) next-opponent lambda via `expected_lambdas` decomposition (`dixon_coles.py:682-684`) with opponent blended defence entering directly.
- (e) ELO difficulty context (already fit/deployed).
- (f) structural-prior squad proxy via `dc_priors_from_factors` (48/48 teams).

**Data-gated (NOT real squad strength — do NOT fabricate):**
- Real per-team squad ratings. `squads.json` = 2 real teams (attribution-only); `players.json` = 44 teams but all 238 `analyst_estimate` npxg *shares* (within-team prop split, not team aggregate). Squad adjustment therefore = structural prior only (LOW-risk documented fallback), never a real squad feed driving EV/sizing.
- Re-validated blend knobs (`H_short`, `k`) — data-gated on multi-tournament OOS (see below); WC2026 alone ≈ 1 cluster.

**Stale-data hazard to avoid (matches the audit):** authoritative played source = `martj42_cleaned.csv` (73 played, mean total 2.9589), NOT `wc2026_results.json` (stale 31-match subset). Note `tournament2026.py:335,318` simulator `mean_goals=2.7` affects only GD/goals tie-break realism, never who wins — it is NOT the totals forecast.

### Out-of-sample validation plan

Walk-forward, leakage-free, **never scored against naive averages.**

1. **Holdout blocks:** WC2018, WC2022, Euro2024 + Copa2024 — the same blocks as the half-life backtest (`card.py:541-548`). For each fixture refit **both** DC components using **only data strictly before that fixture's date** (per-fixture/per-matchday `reference_date`, no future leak), form blended `attack*`/`defence*`, compute `lambda_A+lambda_B`, score totals by **Poisson deviance / log-loss** and **O/U(2.5) Brier + CRPS**.
2. **Grid-search `H_short` (e.g. 0.25/0.5/1.0 yr via `xi_from_half_life`) and `k`** on these OOS blocks only; pick the pair minimizing pooled OOS loss; **report per-block deltas** (per-tournament optima diverge — WC2022 prefers long memory, `card.py:541-548`).
3. **Baselines to beat (model-based, never "avg goals so far"):** (a) LONG-only DC hl=8; (b) the WC-anchored mu override (cuts bias from **−0.518 to −0.042** on the 73 WC2026 matches — verified baseline); (c) market totals where available. The blend must beat LONG-only OOS by a **decision-grade margin** or it is not deployed.
4. **WC2026 confirmation only (low power):** same per-matchday walk-forward (totals for matchday N from fits through N−1), reporting realized-vs-expected mean total against the verified **2.9589** plus bias decomposition.
5. **Honest caveat:** ~1 WC cluster → WC2026 confirmation is low-power; the deploy decision rests on the multi-tournament OOS blocks. `k`/`H_short` reported with per-block sensitivity so a single-tournament fluke cannot drive the choice. Default if OOS inconclusive: modest `w_t` (large `k`, e.g. `k≈10` so ~10 recent matches reach `w_t=0.5`), leaning on LONG — the conservative anti-overfit choice consistent with the existing 0.10/0.30/0.60 stance.

---

### Provenance footer
- Prototype code: `scratchpad/wt-contrast/proto.py`; CSV: `scratchpad/report/goalmodel_proto.csv`.
- Key production paths: `src/wca/card.py:604-610`, `:116-118`; `data/processed/wc2026_results.json`; `src/wca/exposure_dashboard.py:75-91`; `src/wca/accas.py:1302`; `data/players.json`; `site/bet_recs.json`; `data/raw/martj42_cleaned.csv`; `src/wca/dixon_coles.py:682-684`; `src/wca/structural.py:205-212`.
- Every number tagged real / approx / UNVERIFIED. Finding 1's `mu=0.2045 / baseline 2.4409` is the **cached** value (`data/dc_params_corrected.json`); the **live** production fit is `mu=0.2051 / baseline 2.4552` (immaterial, conclusion unchanged). No fabricated numbers.
