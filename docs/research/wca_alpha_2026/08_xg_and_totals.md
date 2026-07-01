# 08 — xG / Total-Goals: Reproduced Bias, Locus, Fix Spec, Dependency Map

**Track A.** Read-only research. Mode = IMPLEMENT-prep: this document is the input
to a backward-compatible code change. No `src/` was modified. Repro script:
`docs/research/wca_alpha_2026/scripts/repro_xg_total_bias.py` (loads the fitted
`data/dc_params_corrected.json`, predicts model totals for played WC2026 fixtures,
compares to realized totals, paired test). Never claims profitability.

---

## 1. The user evidence — and why it understated the problem

The existing validation work (`docs/research/model_and_rec_validation_report.md:18,28,131-144,251`;
`docs/research/sections/goal_models.md:345`) reports a predicted-vs-actual total-goals
**bias of −1.43 (MAE 1.67, n=27)** but explicitly dismisses it as a *truncation artifact*:
the per-match log stored only the **top-k (6) scoreline ladder**, which keeps ~55–66% of
the scoreline mass concentrated on low scores, so the reconstructed total is mechanically
biased down. The report's verdict: "not a model-accuracy claim … data-pending until the
full Dixon-Coles lambdas are persisted."

That verdict is half right and half wrong:

- **Right:** the −1.43 figure is inflated by top-k truncation.
- **Wrong:** the conclusion that there is *no* model-level bias. When you reproduce the
  total from the **full fitted lambdas** (not the truncated ladder), a smaller but
  **structural and statistically significant** under-forecast remains.

---

## 2. Independent reproduction (full lambdas, n=31)

`repro_xg_total_bias.py` loads the canonical fitted model
`data/dc_params_corrected.json` (the same object `DixonColesModel.from_dict` consumes
across `scripts/*` — `mu=0.20438`, `home_advantage=0.27199`, `rho=−0.05019`,
`xi=0.08664`), and for every played WC2026 fixture in
`data/processed/wc2026_results.json` computes the **production match-event anchor**:
all WC2026 fixtures are neutral, so `card.py:1336` calls `dc.predict(h,a,neutral=True)`
⇒ `log λ_h = mu + atk_h − dfc_a`, `log λ_a = mu + atk_a − dfc_h` (γ=0). Expected total
goals = `λ_h + λ_a`.

**Result (n=31 played matches, all team names matched, 0 skipped):**

| metric | model | realized | bias (model−real) |
|---|---|---|---|
| mean total goals (λ-sum) | **2.336** | **3.000** | **−0.664** |
| mean total goals (τ-matrix, max_goals=10) | 2.334 | 3.000 | −0.666 |

- **Paired t-test:** t(30) = −2.055, **p = 0.0487** (significant at 5%).
- 95% CI on bias: **[−1.298, −0.031]** (excludes 0).
- MAE 1.454, sd(diff) 1.800.
- **Wilcoxon** signed-rank p = 0.152 (not significant): the sample has a heavy right tail
  — three 6-goal blowouts (Sweden-Tunisia, England-Croatia, Canada-Qatar each 6) and an
  8-goal Germany-Curaçao — so the *mean* under-forecast is real but the *median* match is
  closer. The bias is a **level** miss amplified by a fat upper tail the Poisson level
  also under-weights.
- **τ-matrix ≈ λ-sum to 0.002** ⇒ the bias is **NOT** the top-k truncation artifact the
  prior report blamed. Truncation at max_goals=10 is mass-neutral here.

**Over 2.5 calibration:** mean model P(Over 2.5) = 0.405 vs realized rate 0.452 (14/31);
gap +0.047 (binomial p = 0.59 at n=31 — underpowered but directionally consistent: the
model is too low on Overs).

**Conclusion:** the user's "xG too low" claim **reproduces** on full lambdas. Bias ≈
**−0.66 goals/match**, significant by paired t and by CI, not a truncation artifact.

---

## 3. Why the model is too low — root cause

The total-goals **level anchor is the fitted intercept `mu`** (`dixon_coles.py:629,
682-683`). `exp(mu)=1.227` per team ⇒ baseline neutral total `2·exp(mu)=2.45`. Over the
actual 48-team WC2026 slate (compressed, competitive strengths) the slate-mean model total
is **2.336** — essentially the baseline, because the Jensen uplift from attack/defence
dispersion is small for closely-matched sides.

**That anchor is below the model's own training data:**

| training subset (`data/raw/results.csv`, martj42) | n | mean total goals |
|---|---|---|
| all-time | 49,477 | 2.939 |
| since 2010 | 15,889 | 2.728 |
| since 2022 | 4,640 | 2.722 |
| FIFA World Cup, since 2010 | 3,750 | **2.81** |

The fitted `mu` implies a WC slate total of **2.34**, i.e. **~0.4–0.5 goals/match below**
the recent international/World-Cup base rate of 2.7–2.81, and **0.66 below** the realized
WC2026 rate of 3.00. Corroborating evidence inside the codebase: `betbuilder.py:60-62`
hard-codes `BASE_TEAM_LAMBDA = 1.35` ("WC total ~2.7"), and `props.py:83` sets CornersModel
`base_goals = 3.07` (WC18+22 incl. ET) — both downstream priors already encode a level the
DC lambdas undershoot.

**Mechanism of the under-shoot:** (a) ridge shrinkage (`reg_lambda=0.01`, ×5 for low-data
teams) pulls attack/defence toward the global mean, compressing the dispersion that would
otherwise lift totals via `exp()` convexity; (b) `mu` is a single global intercept fit by
penalised MLE over a 49k-match corpus dominated by lower-scoring, defensive internationals,
so it is **not** calibrated to the elevated scoring environment of a finals tournament; (c)
the time-decay (`xi`/half-life 8y) does not re-center the level toward the recent uptrend.
The supremacy/1X2 direction is fine (the model picks the right favourite — its 1X2
log-loss backtests pass, `docs/research/backtests/halflife.md`); it is the **total level**
that is mis-anchored. A supremacy-bias check (`repro` script) shows the level miss is the
clean, structural one to fix; the supremacy gap (−0.70, p=0.031) is small-sample
favourite-overperformance, not a directional model error.

---

## 4. EXACT code locus of the too-low anchor

| layer | file:line | role |
|---|---|---|
| **Level anchor (root)** | `src/wca/models/dixon_coles.py:629` (`self.mu = float(mu)`), consumed at `:682-683` (`log_lh = self.mu + atk_h − dfc_a + gamma`) | the fitted intercept = log baseline goal rate. **This is the single number that sets the totals level.** |
| Serialized anchor | `data/dc_params_corrected.json` key `"mu"` = 0.20438 (and `attack`/`defence`/`rho`) — loaded by `DixonColesModel.from_dict` (`dixon_coles.py:768`) across `scripts/*` | the deployed value. |
| λ → matrix | `dixon_coles.py:686-723` (`score_matrix`), `:725-741` (`predict`) | turns lambdas into the score matrix. |
| Matrix → reconciled card | `src/wca/models/scores.py:129` (`reconcile_scoreline_matrix`), `:391` (`scoreline_card`) | **the backward-compat firewall** (see §5). |
| Match-event raw-λ path | `src/wca/card.py:1336-1338` | feeds **raw** λ into CornersModel (`:1354-1355`). |
| Corners coupling | `src/wca/models/props.py:91,106` (`base_goals=3.07`) | corner mean scales off `(λ_h+λ_a)/base_goals`. |

---

## 5. Fix spec — backward-compatible, 1X2-preserving

### 5.1 The key invariant that makes the fix safe

Raising both lambdas **does** shift the *raw* DC 1X2 (verified: scaling λ by 1.2845 moves
draw prob −0.04 to −0.05 and sharpens toward the favourite — a uniform scale is **NOT**
1X2-neutral on its own). **But the card never bets the raw DC 1X2.** Every published total /
BTTS / correct-score number flows through `reconcile_scoreline_matrix` (`scores.py:129`),
which rescales each of the three outcome regions (home/draw/away) to the **blended 1X2
target** exactly, preserving only the *within-region* scoreline shape. **Verified end-to-end:**

```
US vs Paraguay, blend 1X2 = (0.439, 0.295, 0.266):
  base    (λ-sum 1.99):  implied 1X2 = (0.439,0.295,0.266)  Over2.5=0.330  BTTS=0.399
  scaled  (λ-sum 2.56):  implied 1X2 = (0.439,0.295,0.266)  Over2.5=0.466  BTTS=0.532
```

⇒ After reconciliation the **1X2 is pinned to the blend in both cases** while Over 2.5 rises
0.330→0.466 and BTTS 0.399→0.532. The reconciliation **absorbs** the 1X2 drift; only the
totals/BTTS move. This is the mechanism that makes any level lift backward-compatible for
the entire reconciled-card surface (`wca_event_ev` Overs/BTTS, scoreline card, site/bot xG).

### 5.2 Recommended change — raise the DC level anchor (a `mu` shift / level recalibration)

Two equivalent implementations; **prefer (A)** for cleanliness, fall back to (B) if you
want zero change to the fitted file:

**(A) Recalibrate `mu` to the recent base rate during fit.** Add a level-calibration step in
`DixonColesModel.fit` (or a post-fit `recalibrate_level(target_total)` method) that shifts
`mu` by `Δ = log(target_total / model_slate_total)` so the **fitted-data mean total** matches
the recent-WC base rate. Candidate targets and the implied shift (from this slate):

| target total | scale | `Δmu` | new `mu` |
|---|---|---|---|
| 2.70 (betbuilder prior) | 1.156 | +0.145 | 0.349 |
| 2.81 (WC since 2010) | 1.203 | +0.185 | 0.389 |
| 3.00 (realized WC2026, in-sample) | 1.285 | +0.250 | 0.455 |

**Recommend `Δmu ≈ +0.185` (anchor to 2.81, the recent-WC training mean)** — it is the
defensible, *out-of-sample* choice (calibrate to history, not to the 31-match realized
sample, which would be over-fitting the very data we tested on). It closes ~70% of the
−0.66 gap and lands the slate total at ~2.81, inside the [−1.30,−0.03] CI on the right side.
Do **not** chase 3.00 (in-sample). This keeps the user steer satisfied: an independent model
calibrated to **history**, lifting the level toward the true scoring environment.

Implementation notes for safety:
- The shift is a **scalar add to `mu` only**; `attack`, `defence`, `rho`, `home_advantage`
  are untouched ⇒ the supremacy/log-ratio `log(λ_h/λ_a)` is **invariant** ⇒ the *raw* DC
  1X2 *difference* is preserved and the *reconciled* 1X2 is exact-by-construction.
- Apply it in `fit` behind a flag `level_target: Optional[float] = None` (default `None` =
  current behaviour) so the API and all existing fits are **bit-for-bit unchanged** unless
  opted in. Set the flag in `card.fit_models` (`card.py:605-610`) and regenerate
  `data/dc_params_corrected.json`.

**(B) Regenerate the params file only (no code change).** Run a one-off that loads
`dc_params_corrected.json`, sets `mu += 0.185`, re-writes the file. Zero `src/` change, but
the level lift is then undocumented in code — prefer (A) for traceability.

### 5.3 CornersModel coupling — leave `base_goals=3.07` as-is

`props.py:106` centres corner scaling at `base_goals=3.07`. Today raw λ-sum 2.34 makes
`rel=(2.34/3.07−1)=−0.24`, damping corners ~7% below base (elasticity 0.30). After the fix
(λ-sum ~2.81) `rel≈−0.08`, corners sit near base — **more correct**, since 3.07 is the WC18+22
*actual* total. **Do not change `base_goals`**; the fix moves the input λ into the regime the
corner anchor was calibrated for. `betbuilder.BASE_TEAM_LAMBDA=1.35` likewise becomes
consistent with the DC team-λ (~1.40). No edits needed to props/betbuilder.

---

## 6. Dependency map — every consumer of the λ/xG/score-matrix path

**A. Reconciled-card surface (auto-inherits the fix, 1X2 pinned — SAFE):**
- `src/wca/models/scores.py:391` `scoreline_card` → `over_under`, `btts`, `top_scorelines`,
  `implied_1x2` (all from the reconciled matrix).
- `src/wca/card.py:1748-1753` xG emit (reads reconciled matrix marginals) → **xG rises**.
- `src/wca/card.py:1765-1769` O/U 2.5 + BTTS card lines → **Over%/BTTS% rise**.
- `scripts/wca_event_ev.py:122-140` totals / alternate_totals / BTTS EV vs live odds →
  **primary tradeable impact** (see §7).
- `src/wca/sitedata.py:79-154` parses xG / O-U / BTTS out of the formatted card text →
  display-only, inherits new numbers (regex, no hard-coded values).
- `src/wca/bot/app.py`, `src/wca/predledger/*` → publish the same card text; inherit.

**B. Raw-λ surface (uses unreconciled `pred.lambda_home/away` — moves WITH the fix, intended):**
- `src/wca/card.py:1336-1338` → `CornersModel.mean_total/prob_over` (`props.py:102-115`) and
  `cards_mu` (cards model is λ-independent). Corners shift toward base rate — correct (§5.3).
- `src/wca/models/scorers.py:99-139` `total_lambda` first-scorer split, and `betbuilder.py:315`
  `team_total_goals(lambda_team)` → team-goal Poisson lines rise. Consistent with the level fix.
- `src/wca/nextmatch.py`, `src/wca/accas.py`, `src/wca/models/betbuilder.py`,
  `src/wca/exposure_corr.py`, `src/wca/exposure.py`, `src/wca/modelpreds.py`,
  `src/wca/bench/sources.py`, `src/wca/archive/{schemas,store}.py` — call `predict`/lambdas;
  all move consistently in the same direction.

**C. TESTS — what the fix touches (and why most are safe):**
- `tests/test_scores.py` (1X2 reconciliation, O/U, BTTS): **SAFE** — built on synthetic
  matrices (0.5/0.3/0.2 etc.), independent of fitted `mu`. The 1X2-invariance these assert is
  exactly what the fix preserves.
- `tests/test_dixon_coles.py:290-291` (matrix-EG ≈ λ relational), `:501` (`mu` round-trip
  serialization), `:519` (λ round-trip): **SAFE** — relational / serialization, no hard-coded
  production `mu`.
- `tests/test_props.py:42-51,106-108`: derive expectations from `m.base_goals`/`m.base_corners`
  **dynamically** ⇒ **SAFE** as long as CornersModel defaults are unchanged (they are, §5.3).
- `tests/test_card_events.py:117-161`: assert **relationships** (`r.corners_mu ==
  cm.mean_total(lam_h,lam_a)` recomputed both sides), bounds, and line constants — fit on
  small local fixtures, not the production params file ⇒ **SAFE**.
- **No test loads `dc_params_corrected.json` or asserts a specific predicted total/`mu`**
  (verified: `grep -rl dc_params_corrected tests/` → empty). ⇒ The level lift breaks **no
  existing assertion**.
- **New tests to ADD** with the fix: (i) `fit(level_target=2.81)` ⇒ fitted-data mean total
  ≈ 2.81; (ii) `level_target=None` ⇒ identical params to current (regression lock); (iii)
  reconciled `scoreline_card` 1X2 unchanged after a `mu` shift (lock the §5.1 invariant);
  (iv) `expected_lambdas` log-ratio invariant to the shift.

---

## 7. Downstream trading impact (long Overs / BTTS)

The fix **raises model P(Over)/P(BTTS)** without touching the blended 1X2 we already bet.
Concretely on a representative fixture the reconciled Over 2.5 went 0.330→0.466 and BTTS
0.399→0.532 (§5.1). Slate-wide, model P(Over 2.5) moves from 0.405 toward the realized
0.452. Net directional effect: **more positive-EV signals on Over totals and BTTS-Yes** in
`wca_event_ev` against the live `theoddsapi` totals/BTTS markets (ODDS_API_KEY live, ~96k
quota, 49 books) — the less-efficient match-event markets the model is meant to beat.

**Guardrails on the trade thesis (do not overclaim):** n=31 is small; the bias is
significant by paired t (p=0.049) but not Wilcoxon (p=0.15), driven partly by a fat upper
tail. Calibrate the lift to **history (2.81), not the realized 3.00**, to avoid fitting the
test sample. After deploying, the correct validation is **forward CLV** on Over/BTTS legs
priced off the corrected card vs closing lines — track in `predledger`. No profitability is
claimed here; the claim is only that the model's total-goals **level** was significantly and
structurally too low, and the fix removes that mis-anchoring without disturbing the 1X2 the
rest of the book depends on.

---

## 8. Reproduce

```
cd "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha"
.venv/bin/python docs/research/wca_alpha_2026/scripts/repro_xg_total_bias.py
```
Read-only: loads `data/dc_params_corrected.json` + `data/processed/wc2026_results.json`,
writes nothing, opens no DB.
