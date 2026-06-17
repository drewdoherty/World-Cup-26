# Klement structural-prior re-pricing report

**Date:** 2026-06-17
**Scope:** Recompute fair implied probabilities / odds for every previously-generated
outcome in `data/model_predictions_log.jsonl` (the operator's bookmarked model
outputs) **with the revised Klement structural prior ENABLED**
(`fit_models(structural_prior=True)`), and compare to the recorded Klement-OFF
values. Evidence only — no code, no config, and no deployed file is modified.

> **TL;DR.** On the 20 distinct logged fixtures, enabling the Klement prior moves
> the blended model by a **mean of 0.11pp and at most 0.25pp** per outcome.
> The prior is, as designed, **swamped by the likelihood**: every team in these
> fixtures has hundreds-to-thousands of internationals, so the Dixon-Coles
> likelihood dominates the gentle (scale = 0.15) structural shrinkage target.
> The prior's real bite only appears for genuinely data-poor minnows — confirmed
> here by a synthetic thin-data experiment where movement grows ~6-10x.

---

## 1. What "the revised Klement model we arrived at" is

The Klement work landed on `main` across four commits
(`7b1bf84` → `22d4005` → merge `5a39aae` → CLI flags `0e2eb33`/`13e3a68`; the
feature branch `origin/claude/klement-forecasting-model-7gffa1` is behind `main`,
not ahead of it). The **revised, merged model** is the one currently in
`src/wca/models/structural.py`, and it is what `structural_prior=True` activates.
Its parameters (the "arrived-at" values):

| Component | Value / form | Source |
|---|---|---|
| Talent-pool term | `log10(population_m) × football_culture` (population × culture interaction) | `_population_term` |
| Wealth term | `−(ln(gdp / 60000))²` — downward parabola in log-wealth, **inverted-U peaking at $60k/capita** | `_gdp_term`, `GDP_PEAK_USD = 60000` |
| Confederation offset | CONMEBOL +0.55, UEFA +0.40, CAF −0.05, CONCACAF −0.20, AFC −0.30, OFC −0.65 | `CONFEDERATION_OFFSET` |
| Term weights | `w_pop = 1.0`, `w_gdp = 0.6` | `_W_POP`, `_W_GDP` |
| Index normalisation | z-scored mean-zero, unit-variance across the fitted team set | `strength_index` |
| Prior magnitude | **scale = 0.15** DC log-goal units at z = ±1 (`DEFAULT_PRIOR_SCALE`) | `build_dc_priors` |
| Where it acts | ridge in `DixonColesModel` shrinks attack/defence toward `scale·z` instead of 0; low-data teams (< `min_matches = 5`) get a `5×` stronger pull (`low_data_reg_multiplier`) | `dixon_coles.py` |

This is a **prior, not a signal**: it changes only the Dixon-Coles attack/defence
fit. Elo and the de-vigged market consensus are untouched. In the deployed
50/25/25-style blend (`BlendWeights(elo=0.25, dc=0.25, market=0.50)`) the prior
therefore reaches the final model probability only through DC's **25% weight**.

## 2. Method

For each unique fixture in `data/model_predictions_log.jsonl` (deduped to the
latest logged generation per `match_id`):

1. Load results from the deployed dataset (`data/raw/martj42_cleaned.csv`).
2. Fit Dixon-Coles **twice on the same data** — `structural_prior=False`
   (baseline) and `=True` (Klement) — so the OFF→ON delta is a pure prior effect
   (any dataset-vintage difference vs the originally-logged value cancels).
3. Recompute the DC 1X2 for the fixture under both fits.
4. Re-blend each with the fixture's **recorded** Elo and market legs (which the
   prior cannot change), reproducing the exact 25/25/50 blend the card uses.
5. Compare blended `model` and DC legs; compute Brier / log-loss on settled
   fixtures.

A sanity check confirms the baseline refit reproduces the recorded `dc` leg to
0.00pp on fixtures logged against the current dataset; the few larger residuals
(Austria-Jordan, Brazil-Morocco, Ivory Coast-Ecuador) are pure
data-vintage effects — those rows were logged before WC2026 results were added
to the CSV — and they cancel in the OFF→ON delta.

## 3. Aggregate re-pricing impact

| Metric | Value |
|---|---|
| Fixtures repriced | 20 |
| Mean blended-model shift (total-variation distance) | **0.114 pp** |
| Max blended-model shift | **0.253 pp** (Ivory Coast vs Ecuador) |
| Mean DC-leg shift (before the 0.25 blend weight) | 0.456 pp |
| Mean longshot (min) leg, model OFF → ON | 16.398% → 16.383% |
| Mean favourite (max) leg, model OFF → ON | 60.460% → 60.469% |

The distribution barely moves: the favourite-vs-longshot spread changes by
hundredths of a point. In **fair-odds** terms, a 0.25pp shift on a ~45% leg is a
decimal-odds change from 2.22 to 2.23 — well inside any book's bid/ask and far
inside the de-vig uncertainty. **No logged outcome is materially re-priced.**

## 4. Per-outcome / per-fixture detail (top movers first)

Movement ranked by blended-model total-variation distance. `n_h` / `n_a` are the
(xi-weighted effective) match counts the Dixon-Coles fit has for each team —
note they are all in the **hundreds to thousands**, i.e. far above the
`min_matches = 5` low-data threshold, which is why the prior is inert.

| Fixture | n_h | n_a | model TVD | DC TVD | biggest leg shift |
|---|---:|---:|---:|---:|---|
| Ivory Coast vs Ecuador | 637 | 592 | 0.253pp | 1.011pp | away +0.253pp |
| Uzbekistan vs Colombia | 354 | 638 | 0.242pp | 0.968pp | away +0.242pp |
| Haiti vs Scotland | 511 | 852 | 0.214pp | 0.855pp | away −0.214pp |
| Ghana vs Panama | 671 | 539 | 0.177pp | 0.709pp | home −0.177pp |
| United States vs Paraguay | 791 | 784 | 0.154pp | 0.615pp | home −0.154pp |
| Canada vs Qatar | 470 | 636 | 0.152pp | 0.610pp | home +0.152pp |
| Australia vs Turkey | 582 | 643 | 0.138pp | 0.553pp | away −0.138pp |
| Mexico vs South Korea | 1004 | 1008 | 0.122pp | 0.489pp | home +0.122pp |
| Argentina vs Algeria | 1070 | 618 | 0.121pp | 0.483pp | home +0.121pp |
| Brazil vs Morocco | 1060 | 618 | 0.117pp | 0.467pp | home +0.117pp |
| Czech Republic vs South Africa | 361 | 481 | 0.103pp | 0.411pp | home −0.103pp |
| Iraq vs Norway | 656 | 873 | 0.101pp | 0.404pp | away −0.101pp |
| Qatar vs Switzerland | 636 | 885 | 0.096pp | 0.383pp | away −0.096pp |
| Portugal vs DR Congo | 695 | 524 | 0.065pp | 0.261pp | home −0.065pp |
| England vs Croatia | 1090 | 396 | 0.062pp | 0.247pp | home −0.062pp |
| Netherlands vs Japan | 880 | 791 | 0.056pp | 0.223pp | away −0.056pp |
| Switzerland vs Bosnia and Herzegovina | 885 | 284 | 0.042pp | 0.170pp | home −0.042pp |
| Austria vs Jordan | 862 | 487 | 0.033pp | 0.133pp | home −0.033pp |
| France vs Senegal | 936 | 639 | 0.021pp | 0.083pp | home +0.021pp |
| Germany vs Curaçao | 1032 | 386 | 0.011pp | 0.044pp | home +0.011pp |

### Direction of the nudge

The signed shifts are consistent with the structural index. The structural
strength z-scores (and the resulting attack/defence priors, scale 0.15) rank as:

* **Top (positive prior, +atk/+dfc):** England +1.14, Germany +1.13, France +1.10,
  Spain +1.02, Netherlands +0.93 — large population × high football culture ×
  near-peak GDP, UEFA bonus.
* **Bottom (negative prior, −atk/−dfc):** DR Congo −3.51, Haiti −2.18, Senegal
  −2.01, Uzbekistan −1.58, Ghana −1.53, Ivory Coast −1.27, Cape Verde −1.19 —
  dragged down by the inverted-U wealth penalty (very low GDP/capita) and the
  CAF/AFC confederation offsets.

So enabling the prior **shaves a hair off the structurally weak side and adds it
to the structurally strong side** — e.g. Ivory Coast (z −1.27) vs Ecuador
(CONMEBOL): away (Ecuador) +0.25pp; Uzbekistan (z −1.58) vs Colombia: away
(Colombia) +0.24pp; Haiti (z −2.18) vs Scotland: Haiti's away win prob −0.21pp.
Every move is in the "structure likes the bigger/wealthier/CONMEBOL-UEFA side"
direction, just at sub-decimal-of-a-point magnitude.

> **Caveat on the index's face validity.** Because the wealth term is a hard
> inverted-U and the CAF offset is mildly negative, several strong African and
> CONMEBOL-adjacent sides (Senegal, Ghana, Ivory Coast) sit near the *bottom* of
> the structural ranking despite real footballing pedigree. This is the
> documented behaviour of the coarse five-variable prior — it is meant to inform
> a low-data shrinkage target, **not** to rank teams. It is exactly why the prior
> must stay swamped for data-rich teams (it is), and why it should only ever be a
> gentle nudge for the genuine minnows.

## 5. Where the prior actually bites — synthetic thin-data confirmation

The logged set cannot exercise the prior's core use case because it contains no
data-poor team (the same limitation the structural-prior backtest hit:
`docs/research/backtests/structural_prior.md`). To demonstrate the mechanism, I
truncated four minnows (Curaçao, Cape Verde, Haiti, DR Congo) to their 3 most
recent matches (≈1 xi-weighted effective match each, **below** `min_matches = 5`,
so the `5×` low-data ridge engages) and re-fit both ways:

| Fixture (thinned) | DC 1X2 OFF → ON | TVD |
|---|---|---:|
| Cape Verde vs Haiti | H 33.0→34.1, D 60.5→58.9, A 6.5→7.0 | **1.60pp** |
| Haiti vs DR Congo | H 34.0→33.2, D 40.5→40.0, A 25.4→26.8 | **1.34pp** |
| DR Congo vs Curaçao | H 36.4→37.3, D 22.3→22.5, A 41.3→40.2 | 1.08pp |
| Curaçao vs Cape Verde | H 7.1→7.7, D 28.3→28.5, A 64.5→63.7 | 0.78pp |
| Germany vs DR Congo | H 97.0→96.8, D 2.5→2.6, A 0.6→0.6 | 0.18pp |
| England vs Curaçao | H 98.7→98.6, D 1.1→1.1, A 0.3→0.3 | 0.03pp |

Two clean findings:

1. **Minnow-vs-minnow** fixtures move **6-10x more** than anything in the live
   logged set (0.8-1.6pp vs ≤0.25pp). This is the regime the prior was built for.
2. **Minnow-vs-giant** fixtures still barely move, because the data-rich giant
   pins the scoreline. So the prior helps price the *minnow-vs-minnow*
   group/advancement combinatorics, not the favourite legs.

Even fully engaged the effect is modest by construction (scale 0.15 is a
deliberately gentle nudge, not a fact).

## 6. Calibration / EV implications

On the 16 logged fixtures that have since settled:

| Variant | 3-way Brier | Log-loss |
|---|---:|---:|
| Blended model, Klement OFF | 0.53717 | 0.90526 |
| Blended model, Klement ON | 0.53761 | 0.90575 |
| DC-only, Klement OFF | 0.52218 | 0.87153 |
| DC-only, Klement ON | 0.52395 | 0.87352 |

* The prior is **calibration-neutral to very slightly negative** here
  (+0.0004 Brier / +0.0005 log-loss on the blend) — entirely expected, and not
  decision-grade: these are all data-rich teams the prior should not touch, and
  the tiny degradation is just the prior nudging already-well-estimated
  parameters off the MLE by a hair. This **reconfirms the backtest verdict** that
  the prior is inert (and a touch harmful) on data-rich fixtures.
* **EV implication:** at ≤0.25pp on the blended fair probability, enabling the
  prior cannot flip any edge sign or move a Kelly stake by a meaningful amount on
  these markets. There is **no EV case to enable it for liquid 1X2**, consistent
  with the design note that the de-vigged market already subsumes structural
  information.
* The **only** place enabling it could matter is exactly where it is intended:
  thin Polymarket **outright / advancement** markets that price a 48-team field
  including genuine minnows with < 5 internationals. There the 0.8-1.6pp
  per-match shifts compound across a group's score combinatorics into a
  non-trivial advancement-probability change — but that case must be evaluated on
  live minnow data, not on this data-rich log.

## 7. Recommendation

Keep `structural_prior=False` (the default) for the liquid 1X2 card — the
re-pricing confirms it is inert-to-slightly-harmful on the logged, data-rich
fixtures. Reserve `structural_prior=True` for the minnow-heavy outright /
advancement pricing path, and gate any live use on the divergence flag plus a
human data-quality look, never as a stake trigger.

---

### Reproduction

* `data/raw/martj42_cleaned.csv` (deployed results), `data/model_predictions_log.jsonl`
  (recorded Klement-OFF outputs).
* `fit_models(results, structural_prior=False)` vs `=True`; `dc_probs`, `elo_probs`,
  `BlendWeights()` (0.25/0.25/0.50) in `src/wca/card.py`.
* Structural index / priors: `src/wca/models/structural.py`
  (`load_country_factors`, `strength_index`, `build_dc_priors`, scale 0.15).
