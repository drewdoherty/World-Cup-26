# Asian Handicap, Corners & Cards — Historical Reliability Verdicts

**Date:** 2026-07-03 (R32 finishing / R16 starting).
**Question:** should real money go on AH, corners, cards?
**Short answer:** No real money on any of the three today. AH is the only market whose model
side passes a reliability test — it is PAPER-FIRST pending price capture + CLV plumbing.
Corners and cards fail on model evidence *and* infrastructure: FREE-BET-ONLY and NO
respectively.

---

## 0. Method (no leakage, real 90-minute actuals)

- **Models:** `wca.card.fit_models` (Elo + Dixon-Coles, half-life 8y, default args) fit on
  `data/raw/results.csv` filtered to **date ≤ 2018-06-13** (n_train = 41,639 played matches)
  and **date ≤ 2022-11-19** (n_train = 45,700), `reference_date` = cutoff. Scripts + fitted
  params: `scratchpad/research/fit_pretournament.py`, `dc_WC2018.json`, `dc_WC2022.json`.
- **Level anchor analogue:** production anchors DC `mu` so the WC-slate mean total equals the
  "FIFA World Cup since 2010" training mean (2.81, `card.py:84`). Replicated per cutoff from
  results.csv (tournament contains "FIFA World Cup", finals+qualifiers, 2010→cutoff):
  target **2.8166** for 2018 (n=1,783 matches), **2.8080** for 2022 (n=2,713). Applied via
  `recalibrate_level(target, neutral=True, fixtures=<evaluated 64-match slate>)`
  (Δmu = +0.2621 / +0.2329). Both **raw** and **anchored** variants scored.
- **Actuals:** results.csv knockout scores are **ET-inclusive, not 90-min** (verified:
  Croatia–England 2018 stored 2-1, was 1-1 at 90; Argentina–France 2022 stored 3-3, was 2-2
  at 90). So all actuals were rebuilt from the repo's cached StatsBomb open-data events
  (`data/raw/statsbomb/`, 128 event files) with **period ≤ 2 only**, via
  `wca.data.statsbomb.match_props` (`build_90min_actuals.py` → `wc_90min_actuals.csv`).
  Cross-check: all 118 non-ET matches agree exactly with results.csv scores (0 mismatches);
  10/128 matches went ET; all 128 matched to results.csv fixture rows (home/away order +
  neutral flag preserved; 2018 Russia games non-neutral).
- Eval scripts: `eval_margin.py`, `eval_corners_cards.py` (same scratchpad dir); raw
  per-match outputs in `margin_eval_rows.csv`.

**Known contamination found on the way (affects shipped constants):** `props.py` fallback
constants were fit on StatsBomb WC18+22 **without a period filter**. Recomputed (n=128):

| quantity | shipped constant | all-periods (what it was fit on) | true 90-min |
|---|---|---|---|
| total corners mean | 8.97 | 8.969 | **8.680** (var 9.558) |
| total cards mean | 3.41 | 3.414 | **3.289** (var 4.160) |
| `base_goals` (CornersModel) | 3.07 | 3.070 — **includes penalty-shootout kicks counted as goals** (period-5 Shot events with outcome Goal) | **2.609** |
| corners NB k | 157.5 | (MoM on all-periods) | **85.8** (MoM, 90-min) |
| cards NB k | 6.9 | (MoM on all-periods) | **12.4** (MoM, 90-min) |

The 3.07 → 2.609 gap is mostly ~52 shootout kicks across 9 shootouts counted as goals plus ET
goals. Any 90-min O/U priced off these constants inherits the bias.

---

## 1. HANDICAP — DC margin (supremacy) distribution

Margin buckets from the DC scoreline matrix (`matrix[x,y]`, margin = x−y, matrix
renormalised): **H2+ / H1 / D / A1 / A2+**. Scored on 128 90-min WC results (64 + 64).
Naive baseline = unconditional margin-bucket frequencies from prior WC finals, 32-team era
(1998→cutoff): n=320 (pre-2018), n=384 (pre-2022) — baseline itself ET-inclusive for KO
games (conservative in the model's favour by a small amount).

### Skill vs naive baseline (multiclass, 5 buckets)

| scope | n | model Brier (anch) | base Brier | model LL (anch) | base LL | paired ΔLL (anch−base) |
|---|---|---|---|---|---|---|
| WC2018 | 64 | 0.7577 | 0.8047 | 1.4871 | 1.6191 | −0.1320 (SE 0.0588) |
| WC2022 | 64 | 0.7277 | 0.7987 | 1.4455 | 1.6058 | −0.1603 (SE 0.0833) |
| **pooled** | **128** | **0.7427** | **0.8017** | **1.4663** | **1.6125** | **−0.1462 (SE 0.0508, t≈2.9)** |

Raw (unanchored) is essentially the same (pooled ΔLL −0.1486, SE 0.0436, t≈3.4). The
level anchor is roughly a wash for *margins* (helps 2022, slightly hurts 2018) — it matters
for totals, not supremacy, as designed.

### Calibration at the AH-relevant quantities (anchored, pooled n=128)

Bucket calibration (mean predicted vs observed frequency):

| bucket | n_obs | obs freq | model mean |
|---|---|---|---|
| H2+ | 27 | 0.211 | 0.232 |
| H1 | 25 | 0.195 | 0.186 |
| D | 29 | 0.227 | 0.233 |
| A1 | 29 | 0.227 | 0.169 |
| A2+ | 18 | 0.141 | 0.181 |

Favourite-centric (fav = higher DC 90-min win prob):

| | n_obs | obs | model |
|---|---|---|---|
| fav by 2+ (≙ AH −1.5) | 40 | 0.312 | 0.335 |
| fav by 1 | 33 | 0.258 | 0.227 |
| draw | 29 | 0.227 | 0.233 |
| dog by 1 | 21 | 0.164 | 0.127 |
| dog by 2+ | 5 | 0.039 | 0.078 |

AH proxies, tercile calibration (n=128, terciles of ~43):
- **fav −0.5** (fav wins at 90): overall pred 0.562 vs obs 0.570; terciles pred/obs
  0.429/0.488, 0.555/0.535, 0.705/0.690 — monotone, gaps ≤6pp (binomial SE per tercile ≈7.6pp).
- **fav −1.5** (fav by 2+): overall pred 0.335 vs obs 0.312 (binomial SE 4.1pp); terciles
  0.212/0.233, 0.318/0.279, 0.478/0.429 — monotone, mild over-confidence at the top.

Draws: obs 0.227 vs anchored 0.233 (raw over-predicts at 0.278 — the anchor fixes the draw
mass on this sample). Note this does NOT contradict the live WC2026 group-stage finding of
~14pp draw *under*-prediction — different sample; both observations carry ~n≤128 noise.

### Reliability reading — honest limits

- The margin distribution **clearly beats the unconditional WC distribution** (~0.15 nats,
  ~3 SE, consistent across both held-out tournaments). Calibration at −0.5/−1.5 proxies is
  within sampling noise.
- Weak spots: **"dog by exactly 1" under-predicted** (0.127 vs 0.164 obs, n=21) and **"dog by
  2+" over-predicted** (0.078 vs 0.039, n=5) — exactly the cells that price +1.0/+1.5 dog
  lines; small n, but the direction is consistent with DC tail behaviour.
- **Beating naive ≠ beating the market.** AH margins at sharp books are ~2–5%; per-bucket
  calibration noise here is ±4–8pp on n=128. This test certifies the model as a *sane AH
  calibrator*, not as +EV vs closing AH lines. That question is empirically unanswerable
  today because **we have zero AH price history** (§4) — the same reason the 1X2 card anchors
  60% weight on the devigged market and treats the model as a divergence detector.
- Training contamination: results.csv KO scores in the *training* corpus are ET-inclusive
  (all international KOs). Small (few hundred rows in 40k+), direction: slightly fattens
  tails. Not fixed here; noted.

**Handicap verdict: PAPER-FIRST.** Model side passes; the real-money bar (CLV-measurable
edge vs captured prices) cannot even be tested yet.

---

## 2. CORNERS — `CornersModel` vs StatsBomb 90-min corners

Production path (`card.py build_event_references`, DISPLAY-ONLY today): shipped constants
(base 8.97, k=157.5, elasticity 0.30, base_goals 3.07) driven by **anchored** pre-tournament
DC lambdas. Note the shipped constants are **in-sample for this test** (fit on these same 128
matches, all-periods); the honest-OOS variant refits base/k/base_goals on the *other*
tournament's 90-min data (LOTO). Baseline: the other tournament's empirical over-rate.

| variant | line | mean p_over | obs over | Brier | ΔBrier vs base (SE) | side hit-rate |
|---|---|---|---|---|---|---|
| WC2018 shipped | 8.5 | 0.512 | 0.516 | 0.2417 | −0.0102 (0.0072) | 0.641 |
| WC2018 shipped | 9.5 | 0.385 | 0.375 | 0.2264 | −0.0167 (0.0140) | 0.625 |
| WC2018 LOTO | 8.5 | 0.494 | 0.516 | 0.2425 | −0.0095 (0.0055) | 0.578 |
| WC2018 LOTO | 9.5 | 0.386 | 0.375 | 0.2268 | −0.0164 (0.0140) | 0.625 |
| WC2022 shipped | 8.5 | 0.508 | 0.469 | 0.2493 | −0.0019 (0.0052) | 0.562 |
| WC2022 shipped | 9.5 | 0.381 | 0.281 | 0.2096 | −0.0014 (0.0046) | 0.703 |
| WC2022 LOTO | 8.5 | 0.549 | 0.469 | 0.2543 | **+0.0030** (0.0073) | 0.516 |
| WC2022 LOTO | 9.5 | 0.418 | 0.281 | 0.2183 | **+0.0074** (0.0076) | 0.719 |

(n=64 per row; "side hit-rate" = picking the ≥0.5 side, includes lucky base-rate calls.)

- **Pooled skill vs base-rate baseline (n=128): ΔBrier −0.0061 (SE 0.0044, t=−1.37) at 8.5;
  −0.0090 (SE 0.0074, t=−1.23) at 9.5.** Not significant, and partly in-sample.
- **Fixture-level discrimination:** MAE(μ, actual) 2.09–2.66 corners. corr(μ, actual):
  pooled **r=0.112, p=0.21** (n=128); WC2018 r=0.282 (p=0.020), WC2022 r=0.005 (p=0.965) —
  unstable, sign-consistent with the model's own docstring (xG↔corners r≈0.15). The
  elasticity tilt flips sign between tournaments (helps 2018, hurts 2022 vs flat).
- Dispersion: shipped k=157.5 vs 90-min MoM k=85.8 — tails slightly too thin; level base
  8.97 vs true 90-min 8.68 (§0 contamination), partially masked in production because
  anchored lambdas make the elasticity term slightly negative (μ ≈ 8.75–8.78).

**Corners verdict: the model is a base-rate guesser with no demonstrated fixture-level
skill.** Corners markets are won on team-style covariates (crossing volume, wing play, shot
mix) and in-match state — none modelled. Plus zero price capture and no settlement data
source for 2026 (StatsBomb open-data does not cover WC2026). **FREE-BET-ONLY** (stake-back
promos where price precision is irrelevant), never cash.

---

## 3. CARDS — `CardsModel` vs StatsBomb 90-min cards (2nd yellow = 1 red)

The honest statement first: **fixture-level cards modelling does not exist in production.**
`CardsModel` prices `mu = 3.41 × aggression_h × aggression_a × stakes × ref_factor`, but at
card-build time every multiplier is 1.0 (`card.py:1487-1490` — "no team aggression priors
available"); `data/processed/prop_priors.csv` is absent (gitignored, never built on this
checkout), so the FoulsModel→aggression and referee paths are dead code paths. What ships is
a **flat constant 3.41 with NB k=6.9** — there is nothing fixture-specific to validate.
Evaluating the constant anyway (n=64 per tournament, lines 3.5/4.5; LOTO = refit on other
tournament):

| variant | line | mean p_over | obs over | ΔBrier vs base (SE) |
|---|---|---|---|---|
| WC2018 shipped flat | 3.5 | 0.425 | 0.406 | +0.0001 (0.0004) |
| WC2018 shipped flat | 4.5 | 0.275 | 0.188 | +0.0037 (0.0024) |
| WC2022 shipped flat | 3.5 | 0.425 | 0.422 | −0.0002 (0.0023) |
| WC2022 shipped flat | 4.5 | 0.275 | 0.250 | −0.0033 (0.0095) |

- Indistinguishable from the naive baseline by construction (both are base rates); MAE
  1.44–1.75 cards; corr undefined (constant μ).
- Shipped k=6.9 vs 90-min MoM k=12.4 — **tails materially too fat** (fit on ET-inclusive
  counts): it overprices high lines (4.5 over priced 0.275 vs 0.188 observed in 2018).
- Tournament regime shift is real (90-min means 3.17 in 2018 vs 3.41 in 2022; and WC2026 has
  new referee directives) — a flat historical constant cannot track it.
- Settlement risk: our convention (2nd yellow = ONE red; total = Y+R) does not match many
  books ("2nd yellow counts as 2 cards" / booking-points variants) — a mis-settlement trap
  with no automation behind it.
- The biggest known cards driver — the **referee** — is not in any data pipeline.

**Cards verdict: NO.** Too thin to evaluate beyond the base rate, and the base rate itself is
mis-dispersed for 90-min settlement.

---

## 4. MARKET REALITY CHECK (binding constraint)

Verified on the production mini (read-only, `~/World-Cup-26/data/wca.db`):

- `odds_snapshots` (3.34M rows): **h2h 2,693,131 · totals 439,020 · btts 165,650 ·
  draw_no_bet 37,344. Zero AH/spreads, zero corners, zero cards rows.**
  `theoddsapi.get_odds` pulls `markets="h2h,totals"` (+`btts` via per-event endpoint) —
  `src/wca/data/theoddsapi.py:99,178`.
- `closecapture.py` stamps closing odds/CLV for **1X2 markets only** (`is_1x2_market` gate,
  `closecapture.py:64`).
- Ledger history in these markets: **3 bets ever** (ids 20, 21, 88 — Total Corners U8.5,
  Total Cards U3.5, a corners+cards+BTTS builder), all discretionary, **all lost, −£12.0**,
  `closing_odds`/`clv` all NULL. n=3 is anecdote, but it is 0/3 with no CLV measurement —
  the same discretionary-punt leak pattern already documented for correct-score.
- No settlement automation: auto-settler settles off goals in results feeds; corner/card/
  margin outcomes are not ingested anywhere for 2026 (StatsBomb open-data ends at 2022).

Project rules: CLV is the primary KPI; a market with no captured price series has **no devig
anchor, no CLV measurement, no scalable feedback loop** — model reliability alone does not
clear the real-money bar. This gates all three markets regardless of §§1–3.

---

## 5. VERDICTS

| market | verdict | evidence line | minimum infrastructure before promotion |
|---|---|---|---|
| **Asian Handicap** | **PAPER-FIRST** | DC margin dist beats naive WC history by 0.146 nats (SE 0.051, n=128, both WCs held out); fav −0.5/−1.5 calibrated within noise (0.562/0.570, 0.335/0.312); dog-by-1 cell under-predicted (0.127 vs 0.164, n=21) | (1) add `spreads` (+alternates if plan allows) to the TheOddsAPI pull — config-level change (verify soccer AH coverage on our plan; Betfair Exchange client exists but needs credentials); (2) 2-way devig + AH edge calc off the existing scoreline matrix incl. quarter-line push logic; (3) extend closecapture/CLV stamping beyond `is_1x2_market`; (4) 90-min margin settlement (must NOT use ET-inclusive results.csv KO scores); (5) ≥50 CLV-stamped paper bets with positive mean CLV in the test book before any cash |
| **Corners** | **FREE-BET-ONLY** | no fixture-level skill: pooled corr(μ,actual)=0.112 (p=0.21, n=128), ΔBrier vs base-rate ≤1.4 SE and partly in-sample, sign flips between WCs; shipped constants ET-contaminated (8.97 vs true 8.68) | not worth building for cash: would need price capture + a real feature model (style covariates) + a 2026 corners results feed + settlement. For free-bet/boost extraction only, price sanity-check with 90-min LOTO constants (base 8.68, k≈86) |
| **Cards** | **NO** | production model is a flat constant (aggression paths unfed — prop_priors.csv absent); k=6.9 vs 90-min k=12.4 (tails too fat; 4.5-over priced 0.275 vs 0.188 obs in 2018); no referee data; settlement-convention mismatch; ledger 0/3 −£12 with no CLV | would need referee assignments + team foul priors + price capture + settlement feed — none exists; do not build during this tournament |

**Bankroll rule cross-check:** typical AH/O-U probabilities (30–65%) satisfy the no-<25%-
longshot rule, so the likely-PnL rule is not the binder — CLV-measurability is.

---

## Appendix — reproduction

All artifacts in `/private/tmp/claude-501/-Users-andrewdoherty-Desktop-Coding-World-Cup-Alpha/52dc63f7-8f46-4632-9a80-fbfc2b51ecd9/scratchpad/research/`:
`fit_pretournament.py` (fits; log `fit.log`: 163s/188s), `dc_WC2018.json`, `dc_WC2022.json`,
`build_90min_actuals.py` → `wc_90min_actuals.csv` (128 rows), `eval_margin.py` →
`margin_eval_rows.csv`, `eval_corners_cards.py`. Run from repo root with
`PYTHONPATH=src .venv/bin/python <script>`.
