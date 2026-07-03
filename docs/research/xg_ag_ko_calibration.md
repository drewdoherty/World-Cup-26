# Is the +goal-level anchor (2.81) overshooting in the knockouts?

**Study date:** 2026-07-03 (R32 finishing, R16 starting).
**Question:** the DC level anchor (`DEFAULT_DC_LEVEL_TARGET = 2.81`, `card.py:84`, applied via `apply_wc_level_anchor` as `mu += log(target/slate_total)`) was calibrated on group-stage under-prediction (~0.59 goals/game). Do knockout 90-minute goals fall enough that the anchor now has the opposite sign of error?

**Headline answer: NO — the overshoot hypothesis is not supported at decision grade, and half the evidence points the other way.** Actual 90-minute goals did NOT fall in prior-WC knockouts (they rose, +14% pooled, ns). This WC's R32 actuals (n=10) sit *within noise of the anchored level* (implied correction factor 0.94, 95% CI [0.58, 1.30]). Only xG-based and model-normalised estimates hint at a mild ~9% tightening — two independent estimates land at 0.909 and 0.908 — but neither is significant. Leave the anchor; monitor with an n-weighted trigger (details in §4/§6).

All numbers below were computed in this session; scripts and raw outputs sit next to this file (`sb_ko_falloff.py/.json`, `r32_bias.py/.json`, `refit_anchor.py/.json`, `final_calcs.py/.json`, `mini_last_prekickoff.json`).

---

## 0. Data sources and conventions

| Item | Source | Freshness / notes |
|---|---|---|
| Prior-WC events | StatsBomb open-data, comp 43, seasons 3 (2018) & 106 (2022), full local cache `data/raw/statsbomb/` (128/128 event files) | 90-min quantities = events with `period <= 2`; goals = Shot-outcome-Goal + `Own Goal For`; xG = sum `shot.statsbomb_xg` (shootout period 5 auto-excluded) |
| Deployed lambdas | mini `~/World-Cup-26/data/model_predictions_log.jsonl` (1,099 rows, read-only) | per fixture: LAST entry with `generated <= kickoff`. λ persisted from ~2026-06-26 onward (34 fixtures have λ) |
| Actual 2026 scores | repo `data/processed/wc2026_results.json` (82 results, through Jul 1 kickoffs), byte-identical to the mini copy; derived from `martj42_cleaned.csv` which is **90-minute-only** by construction (`scripts/wca_build_wc2026_results.py` docstring; knockout-shootouts memory) | Verified 90-min convention: Germany–Paraguay and Netherlands–Morocco recorded `1-1 draw` while Paraguay/Morocco appear in R16 pairings in the log (advanced via ET/pens) |
| Missing actuals | Spain–Austria, Portugal–Croatia (Jul 2), Switzerland–Algeria (Jul 3 03:00) played but scores not yet in ANY of our sources (martj42 row still `NA`, tracking/scores feeds have no result, ledger bets still `open`); 3 R32 games not yet kicked off (Australia–Egypt, Argentina–Cape Verde, Colombia–Ghana) | **R32 evaluation n = 10 of 16.** Stale-results memory (`wca-settle-stale-results-source`) applies |
| Refit | dev box, repo `data/raw/martj42_cleaned.csv` (fresh Jul 2 14:41), training cut at `date < 2026-06-28` → 49,479 matches | avoids training on the R32 evaluation games |

---

## 1. Prior-WC evidence (StatsBomb 2018 + 2022, 90-minute only)

### Goals/game and xG/game, group vs knockout

| WC | Group n | Group g/g | Group xG/g | KO n | KO g/g | KO xG/g | falloff (goals) | falloff (xG) |
|---|---|---|---|---|---|---|---|---|
| 2018 | 48 | 2.542 | 2.744 | 16 | 2.750 | 2.306 | **1.082** | **0.840** |
| 2022 | 48 | 2.500 | 2.370 | 16 | 3.000 | 2.344 | **1.200** | **0.989** |
| Pooled | 96 | 2.521 | 2.557 | 32 | 2.875 | 2.325 | **1.140** | **0.909** |

- Pooled goals falloff ratio 1.140, Poisson 95% CI **[0.90, 1.45]** (92 KO goals / 32 vs 242 group goals / 96). Welch t on per-match totals: t = −1.00 (KO *higher*, ns).
- Pooled xG falloff 0.909; Welch t = 1.31, df ≈ 64, p ≈ 0.20 (ns).
- Excluding the 3rd-place games changes nothing (goals ratio 1.150, xG ratio 0.911, n=30).

**The user's premise — actual goals fall in KO rounds at 90 minutes — is contradicted by the last two World Cups.** Shot *quality* (xG) dipped ~9% (ns); conversion outperformance (KO goals/xG = 1.24 vs group 0.99) offset it fully. Caveat: the raw group→KO ratio conflates the stage effect with team composition (stronger attacking sides survive); §3's model-normalised estimate addresses this for 2026.

### Within-KO stage gradient (pooled 2018+2022, small n!)

| Stage | n | goals/g | xG/g | level after 90 |
|---|---|---|---|---|
| R16 | 16 | **3.25** | 2.442 | 5 |
| QF | 8 | 2.125 | 2.023 | 3 |
| SF | 4 | 2.000 | 2.323 | 1 |
| 3rd place | 2 | 2.50 | 2.248 | 0 |
| Final | 2 | 5.00 | 2.679 | 1 |

R16 historically scores MORE than group (3.25 vs 2.52); the dip is at QF+SF (pooled 2.083 g/g, ratio 0.83 vs group, n=12 — directional only). Any per-round anchor built from these cells would be fit on n = 2–16.

### The ET/pens complication

- **10/32 (31.25%) of prior-WC KO games were level after 90** (2018: 5/16; 2022: 5/16), vs group-stage draw shares of 9/48 and 10/48 (19.8% pooled).
- This WC so far: group draws 20/72 = 27.8%; R32 level-at-90 2/10 = 20%.
- Deployed model mean draw prob on the 10 R32 games: model 0.254, DC-leg 0.260, market devig 0.253. Prior-WC base (31%) says KO 90-min draws are, if anything, *under*-priced by both model and market — this matters for 1X2 (settles at 90) while advancement markets are unaffected (sim handles ET/pens with real-pens anchoring).

---

## 2. This WC, R32: deployed lambdas vs actual 90-min goals (n=10)

Last pre-kickoff log entry per fixture; actuals from `wc2026_results.json`:

| Fixture (kickoff UTC) | deployed λh+λa | actual | score |
|---|---|---|---|
| South Africa–Canada (6/28 19:00) | 1.785 | 1 | 0-1 |
| Brazil–Japan (6/29 17:00) | 2.544 | 3 | 2-1 |
| Germany–Paraguay (6/29 20:30) | 2.563 | 2 | 1-1 |
| Netherlands–Morocco (6/30 01:00) | 2.013 | 2 | 1-1 |
| Ivory Coast–Norway (6/30 17:00) | 2.246 | 3 | 1-2 |
| France–Sweden (6/30 21:00) | 2.411 | 3 | 3-0 |
| Mexico–Ecuador (7/1 01:00) | 2.345 | 2 | 2-0 |
| England–DR Congo (7/1 16:00) | 2.853 | 3 | 2-1 |
| Belgium–Senegal (7/1 20:00) | 2.774 | 5 | 3-2 |
| United States–Bosnia (7/2 00:00) | 3.213 | 2 | 2-0 |

- Mean forecast 2.475 vs mean actual 2.60 → **bias −0.125/game (model LOW, not high)**. Paired sign test: 5 forecasts high, 5 low, p = 1.0. Total-goals Poisson deviance 3.01 on 10 fixtures (0.30/game — vs 1.89/game on the group sample below).
- Contrast, same log, final group round (n=12, kickoffs 6/26–6/28): forecast 2.309 vs actual 3.167, **bias −0.858/game**, deviance 22.7 — the group-stage under-prediction that motivated the anchor was real and large right up to the end of the groups.

### Critical discovery: the anchor went live MID-R32

Comparing deployed λ to my clean refits (§3): the first 7 R32 fixtures match the RAW (un-anchored) fit almost exactly (deployed/raw ratios 0.994–1.013), the last 3 match the ANCHORED fit (ratios 1.167–1.175 ≈ e^Δmu = 1.178). Confirmed in the log: England–DR Congo λ-total jumps 2.447 → 2.876 between the 2026-07-01 01:34 and 06:47 builds; anchor commit `df9dded` is dated 2026-06-30 14:32 +0300.

So the "deployed" series is **mixed-regime**:
- Un-anchored 7 games: forecast 2.272 vs actual 2.286 → bias −0.013.
- Anchored 3 games: forecast 2.947 vs actual 3.333 → bias −0.387 (Belgium–Senegal's 5 dominates; n=3, meaningless alone).

### Goal-blend shadow (gb_lambda)

`gb_lambda_*` logging started **2026-07-02 12:23** — zero overlap with settled fixtures, so **no OOS comparison vs actuals is possible yet (n=0; too thin, said plainly).** On the 9 upcoming fixtures that have both, gb sits at mean **0.942× the anchored DC level** (range 0.79–1.15) — the shadow is already running ~6% cooler, coincidentally at the §3 KO-factor point estimate.

---

## 3. Before vs after the anchor: clean refit (dev box)

`fit_models` twice on 49,479 matches (`date < 2026-06-28`, i.e. R32 excluded from training), `dc_level_target=None` vs `2.81`; per-fixture λ via `models.dc.expected_lambdas` with neutral flags from the results csv (Mexico–Ecuador and US–Bosnia non-neutral hosts).

**Leakage note:** results-level leakage is removed by the training cut. Residual leakage: (i) the anchor slate file (`wc2026_results.json`) contains the R32 *pairings* (not scores) — pairing-composition only, second order; (ii) the 2.81 target is a pre-tournament constant (WC-since-2010 mean), so no target leakage. Acceptable for a level comparison because the anchor is a scalar mu shift — anchored λ = raw λ × e^Δmu for every fixture; only the level is under test.

| Fit | mu | mean R32 forecast (n=10) | mean actual | bias | Poisson deviance | loglik |
|---|---|---|---|---|---|---|
| raw MLE | 0.2057 | 2.348 | 2.60 | **−0.252** | 3.597 | −15.750 |
| anchored 2.81 | 0.3693 (Δmu 0.1637, ×1.178) | 2.765 | 2.60 | **+0.165** | 3.436 | −15.669 |

- The anchored fit is *slightly better* on deviance/loglik than raw even on the KO sample; the bias flips sign but with similar magnitude — the truth sits between, closer to the anchor.
- Implied correction factor on the anchored basis: **26 observed goals / 27.66 expected = 0.940, Poisson 95% CI [0.58, 1.30]** — comfortably includes 1.0.
- Factor grid (loglik on refit-anchored λ): f=0.85 → −15.747; **0.909 → −15.634; 0.94 → −15.619; 1.00 → −15.670**; 1.05 → −15.784; 1.14 → −16.135. The best factor (0.94) beats "no change" by **0.05 nats over 10 games** — pure noise.
- Slate composition: the raw fit already prices KO pairings tighter than group pairings (model mean λ-total 2.348 over the 10 KO pairings vs 2.448 over the 72 group pairings, ratio 0.959) — the multiplicative anchor preserves this, so "KO games are tighter *pairings*" is already in the model and is NOT the anchor's job.
- Stage effect isolated from composition (this WC): actual/model(raw) = 1.220 in groups (2.986/2.448, n=72) vs 1.107 in R32 (2.60/2.348, n=10) → **double ratio 0.908**. Independently, prior-WC xG falloff was **0.909**. Two independent estimates agree on a mild ~9% KO tightening — but the R32 CI ([0.58, 1.30]) and the xG test (p≈0.20) both say: not proven.
- Reality check on the target itself: WC2026 group realized 2.986 g/g (n=72) vs target 2.81 — the anchor was still ~0.18 LOW in groups (consistent with the deliberately out-of-sample choice of 2.81 over the realized 3.00).

---

## 4. Methods for stage-aware level calibration — comparison and ranking

Evaluation axes: expected bias reduction (**B**), variance/sample-efficiency (**V**), leakage risk (**L**), implementation cost (**C**), reversibility (**R**).

**(e) Leave as-is; the market leg of the blend absorbs 1X2; anchor stays 2.81 — RANK 1 (deploy: nothing).**
B: current evidence says remaining bias is within ±10% with sign unknown — expected gain of any change ≈ 0.
V: infinitely sample-efficient (no new parameters).
L: none. C: zero. R: n/a.
The anchor is 1X2-supremacy-invariant by construction, and the deployed 1X2 is blended with the market anyway; the only exposures that a wrong level actually hurts are totals/BTTS (see §5) and the draw mass feeding advancement sims — both currently within noise. Validated: on deployed R32 λ, "no change" loglik −15.459 beats a 0.909 haircut (−15.686) and beats reverting to raw (−16.078).

**(c) Bayesian shrinkage of the anchor toward the KO-empirical level, n-weighted — RANK 2 (deploy as MONITOR + trigger, not as an immediate mu change).**
Posterior on the KO factor f with Gamma prior centred 1.0 (prior weight = prior-WC KO exposure ~90 λ-units, honest since prior-WC 90-min goals showed NO falloff) updated by 2026 KO goals: currently ≈ (90 + 26)/(90 + 27.66) ≈ 0.986 — indistinguishable from 1.
B: converges to the right answer as KO games accrue (8 more by ~Jul 7, then 4, 2…).
V: optimal use of tiny n; explicitly prevents overreacting to a 10-game sample.
L: none if updated only on settled games. C: ~50 lines + a scheduled recompute. R: perfect (a logged scalar; flip back anytime).
Concrete trigger: recompute after each completed round; shift the KO-stage target only if the posterior 90% CI excludes 1.0, or |log f| > |log 0.9| with ≥16 settled KO games.

**(d) Market-totals-implied level per fixture (devig O/U ladder → λ) — RANK 3 (deploy as DIAGNOSTIC column only).**
B: best per-fixture accuracy available — the closing totals market embeds lineups, stakes, weather AND stage; it is the natural "actual" proxy between sparse results.
V: excellent (per-fixture, no history needed).
L: none from results; but **structural circularity: if the model's level is set from the totals market, model-vs-market totals edges are zero by construction** — it cannibalises the very market it would trade (the `wca-oddsapi-utilization` finding that totals O/U 2.5 devig is our top unused edge). Must REPLACE the anchor for the fixtures it covers, never stack (no double-count).
C: low-moderate — we already pay for and store the totals ladders (162k unused rows).
Right use: log per-fixture λ_market next to λ_deployed and gb_λ; feed advancement-sim sensitivity; NOT the betting level for totals.

**(a) Two-regime anchor (KO target = group target × prior-WC falloff) — RANK 4 (do not deploy).**
Fatal problem: *which* falloff? Actual-goals ratio says ×1.14 (target 3.20!), xG says ×0.909 (2.56), per-round R16 says ×1.29, QF+SF says ×0.83. The estimator flips sign across defensible metric choices on n=32/16/12 — a hard regime switch would encode metric-choice noise. B: unknown sign. V: poor. L: low. C: trivial (`dc_level_target` already parameterised). R: perfect. It becomes attractive only after (c)'s trigger fires and picks the sign.

**(b) Continuous stage covariate inside the DC fit — RANK 5.**
B: marginal — the stage effect would be estimated mostly from decades of old tournaments under time-decay, and stage labels don't exist for most of the 49k-corpus (friendlies/qualifiers).
V: worst — a global parameter fit on the thinnest slice of the likelihood.
L: low. C: highest (touches the MLE, retest everything, backtest suite invalidated mid-tournament). R: poor (refit needed to remove).
Wrong tool mid-tournament; a post-mortem research item at best (consistent with the Phase-2 verdict that the bottleneck is odds-capture/sample, not the model).

**Validation of the top actionable candidate** (done, §3): the best constant KO factor on R32 is 0.94, worth 0.05 nats over "leave alone" — i.e. the champion after validation IS (e)+(c)-as-monitor.

---

## 5. Betting-strategy implications for R16/QF/SF/F

Computed on the 9 upcoming fixtures with logged λ (DC score matrix with refit ρ = −0.0562), comparing deployed level (f=1.00) vs the mild-tightening candidates f=0.94 / f=0.909:

| Fixture | λ-tot (gb) | P(O2.5) f=1/0.94/0.91 | P(draw90) f=1/0.94/0.91 | BTTS f=1/0.94/0.91 |
|---|---|---|---|---|
| Australia–Egypt (R32) | 2.20 (1.81) | .378/.342/.324 | .306/.317/.324 | .453/.423/.407 |
| Argentina–Cape Verde (R32) | 3.06 (2.63) | .590/.549/.526 | .130/.142/.148 | .349/.328/.317 |
| Colombia–Ghana (R32) | 2.60 (2.49) | .482/.442/.421 | .218/.230/.237 | .426/.399/.385 |
| Canada–Morocco (R16) | 2.14 (1.80) | .360/.325/.307 | .293/.305/.312 | .406/.378/.364 |
| Paraguay–France (R16)* | — | — | — | — |
| Brazil–Norway (R16) | 3.31 (3.53) | .597/.555/.533 | .191/.203/.209 | .513/.485/.470 |
| Mexico–England (R16) | 2.78 (2.21) | .527/.486/.464 | .255/.265/.271 | .550/.519/.502 |
| Portugal–Spain (R16) | 2.67 (2.43) | .499/.459/.437 | .268/.278/.284 | .539/.508/.492 |
| US–Belgium (R16) | 3.33 (3.84) | .647/.606/.584 | .231/.240/.245 | .648/.617/.600 |

*Paraguay–France logged without λ in its latest entry at extraction time.

Concretely:

1. **Totals/BTTS (the only exposures the level directly moves):** a 0.91 haircut shifts P(Over 2.5) by −4 to −6pp and BTTS by −3pp — 2-3× the 2% `min_edge` gate. Since the evidence does NOT support the haircut, do not impose it; but treat model Over-leans within ~3pp of the gate as fragile from QF onward (the only stage where prior-WC data shows tightening: QF+SF 2.08 g/g, ratio 0.83, n=12). Remember totals+BTTS = same DC lambda = ONE exposure (Phase-2 rule) — an erroneous level change would move the whole family coherently, so sizing discipline matters more than the lean direction.
2. **1X2 draws (settle at 90):** KO 90-min-level base rate was 31% in 2018/22 (10/32) vs deployed model draw ~25-27% on tight pairings. The model may UNDER-price KO draws slightly — with Smarkets 0%-commission and the 3% margin rule, draw backs on tight pairings (Canada–Morocco .293 model vs market .274 logged; Australia–Egypt .306 vs .333) are the place where a mild level DOWN-shift would add, not remove, edge (lower λ ⇒ higher draw). Do not fade model draws in KO.
3. **Advancement/futures (PRIMARY per RE-AIM):** mechanics unchanged (sim handles ET/pens); the match-level input shift is small — f=0.94 moves P(draw at 90) ~+1pp, which splits ~50/50 through ET/pens, moving advancement probs by well under 1pp — smaller than PM spreads. No action needed; re-run `advancement_latest` before acting regardless (staleness memory).
4. **Attribution hygiene:** all R32 totals bets priced between Jun 28 and Jul 1 06:47 came from the UN-anchored model (λ ~18% lower) — tag them separately in CLV/attribution before concluding anything about the anchor's betting value. Ledger bets #263/#264 (O2.5 accas, Jul 1 07:55) were placed post-anchor; fine.
5. **CLV continuity:** any future level change alters logged model preds mid-KPI-window — if (c)'s trigger ever fires, stamp the change date in the prediction log (as the Jul 1 jump conveniently did).

---

## 6. Limitations

- R32 evaluation n=10 of 16; 3 played games lack ingested scores (results pipeline lag, known issue), 3 unplayed at study time. Every 2026 inference here has CI width ~±35%.
- Prior-WC falloff ratios conflate stage effect with team composition; the model-normalised 2026 estimate (0.908) corrects this but on n=10.
- gb shadow has zero settled OOS games (logging began 2026-07-02).
- The deployed-λ series mixes anchor regimes (7 raw / 3 anchored); clean conclusions use the §3 refit.
- Refit trains through Jun 27 while deployed fits refreshed daily; per-fixture replication vs deployed was verified to ≤1.3% for the raw-regime games.

## 7. Recommendations (ranked)

1. **Keep `DEFAULT_DC_LEVEL_TARGET = 2.81` unchanged for R16.** No decision-grade overshoot: R32 factor MLE 0.94, CI [0.58, 1.30]; best-vs-null Δloglik 0.05 nats; prior-WC 90-min goals *rose* in KOs.
2. **Stand up the (c) monitor:** after each completed KO round, recompute posterior KO factor = (90 + KO goals)/(90 + Σ anchored λ); act only if the 90% CI excludes 1.0 or |log f| > log(1/0.9) with ≥16 settled KO games. First checkpoint after R16 (~Jul 7, n≈18-24 if results ingestion is fixed).
3. **Fix results ingestion before R16** — Jul 2/3 scores are in no local source; the monitor and any settlement depend on it (stale-results memory).
4. **Log per-fixture market-implied λ (method d) as a diagnostic column** from the already-captured totals ladders; never feed it back into totals edges (circularity) — use for draw/advancement sanity and as the between-results "actual".
5. **From QF onward, demand extra margin (~+2pp) on Over-2.5/BTTS-Yes backs**; prior-WC QF+SF ran 0.83× group scoring (n=12, directional).
6. **Lean into, don't fade, KO 90-min draw pricing on tight pairings** (prior-WC 31% level-at-90 vs model ~25-27%); check Smarkets draw prices for Canada–Morocco and Australia–Egypt against the 3% margin rule.
7. **Let the gb shadow accrue settled KO games** before judging it; its level (0.94× DC) will effectively A/B-test the mild-tightening hypothesis for free.
