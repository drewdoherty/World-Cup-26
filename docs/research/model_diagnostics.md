# Model Diagnostics — World Cup Alpha

> **Status (2026-06-18):** Metric *primitives* (Brier, log-loss, calibration
> bins) are **LIVE in code**. This document is the missing *aggregate diagnostic
> write-up* (`TODO.md` P3). No backtest is re-run here; it records deployed
> params, what is and is not computed, and the diagnostics still to build.

## 1. Inputs and outputs

**Training data:** `data/raw/martj42_cleaned.csv` — ~49,474 rows, ~49,430 played
matches, 336 teams (loaded by `fit_models`, `src/wca/card.py:286`).

**Per-fixture output:** blended 1X2 (`elo*0.25 + dc*0.25 + market*0.50`,
`BlendWeights`, `src/wca/card.py:68-70`); edges `p·best_odds − 1` filtered at
`min_edge=0.02`; quarter-Kelly sized; and a correct-score matrix reconciled to
the *same* blended 1X2 via `reconcile_scoreline_matrix`
(`src/wca/models/scores.py:129-251`, minimum-KL per-region rescale).

## 2. Deployed parameters

**World-Football Elo** (`src/wca/models/elo.py`):
- initial rating 1500; `home_advantage=100` (:202-208); `K(world_cup)=60` (:69-75)
- goal-margin multiplier G (:145-167): margin 0/1→1.0, 2→1.5, 3→1.75, ≥4→1.75+(N−3)/8
- Outcome model: McCullagh (1980) ordered logit, scale=400 (`EloOutcomeModel`,
  :468-522). Fitted: **beta≈1.9995, c_lo≈−0.7326, c_hi≈0.4746**.

**Dixon-Coles** (`src/wca/models/dixon_coles.py`):
- **deployed `half_life_years=8.0`** (`fit_models`, `card.py:288`) → `xi≈0.0866`,
  overriding the library `DEFAULT_HALF_LIFE_YEARS=2.0` (:97).
- fitted **mu≈0.2044, gamma≈0.2734, rho≈−0.0489**; `reg_lambda=0.01`,
  `min_matches=5`, `low_data_mult=5`, `max_goals=10`.
- `structural_prior` default **OFF** in the card; when off, classic shrink-to-mean.

**Devig:** Shin (Štrumbelj 2014 closed form, `src/wca/markets/devig.py:179-255`)
per complete book, per-column **median** then renormalised (`market_consensus`,
`card.py:403-410`). Power/multiplicative implemented but unused by the card.

**Blend & ladder:** weights 0.25/0.25/0.50; the 8yr half-life and these weights
are deliberately kept after backtests (commit `a545de6`; the backtest fitted
`w_elo≈0.00` and preferred DC, so Elo is over-weighted relative to its measured
value). `LADDER_BANKROLLS=(1500.0,2500.0,5000.0)` (`card.py:112`).

## 3. Calibration status — what IS and is NOT computed

**Computed today (do not re-flag as "to build"):**
- **Brier (1X2)** model vs Shin-market: `brier_1x2`, `src/wca/tracking.py:386`;
  aggregated to `model_brier`/`market_brier`, :946-956.
- **Log-loss (1X2)** model vs market: `log_loss_1x2`, `src/wca/tracking.py:400`;
  per-bet `logloss_model`/`logloss_market`, :839-842.
- **Calibration bins:** `calibration_report` (Brier + 5 equal-width `model_prob`
  bins with observed win rate), `src/wca/ledger/reports.py:311-408`.

**NOT computed (genuine gaps):**
- Aggregate **per-tournament leave-one-tournament-out (LOTO) log-loss
  consistency** (WC2018 / WC2022 / Euro2024 / Copa2024). The `blend_fit` LOTO
  exists as a research artifact but is not surfaced as a standing diagnostic.
- **Per-team residual** diagnostics (which teams the blend systematically
  over/under-rates).
- **Reliability diagram** / ECE beyond the 5 equal-width bins.
- A scheduled job that recomputes and publishes these to the site.

## 4. Per-team / low-data residual gap

DC `predict()` for an unseen team silently falls back to the mean-zero (or
structural) prior; the card calls `predict(..., warn=False)`
(`dixon_coles.py:421-447`, from `card.py:386`), so an unrecognised team yields a
plausible-but-unanchored 1X2 with **no warning**. Mitigated by `canonical()` +
336-team coverage, but there is no hard guard, and no per-team residual table.

## 5. Known model-wiring caveats (must state)

- The venue-aware host path (`src/wca/models/venues.py:91-113`, co-host dilution
  + Azteca altitude) is **NOT wired into the card**: `card.py:539` calls
  `elo_probs(... host=host)` with **no `host_points`**, so a WC2026 host on a
  neutral venue receives the **full undiluted 100-pt** Elo bonus and zero
  altitude term. Only `advancement.py:208-225` uses the diluted path. Do **not**
  rely on host-nation 1X2 edges until this is plumbed into `_iter_fixture_blends`.
- Latent name-key risk: `_meta_lookup` keys the neutral/host table on **raw**
  results-df names while `build_card` looks up with `canonical()` (`card.py:494`
  vs :524-527). Harmless today (0/336 historical, 0/48 scheduled names differ),
  but a single non-canonical scheduled spelling silently falls back to
  `neutral=True`.

## 6. Tournament winner: model vs market (2026-06-18)

The pure-model advancement sim crowns Argentina (#1, 17.8%). Anchored to the live
Polymarket winner book it drops to #3–#4; **France** is the market favourite.
See `docs/research/argentina_market_anchored.md`. This is the strongest current
evidence that Elo over-reacts to single results and that the winner/stage outputs
need a market anchor.

## 7. Diagnostics to build (P3)

1. Per-tournament LOTO log-loss table (model vs Shin-market), persisted + charted.
2. Per-team residual / shrinkage report; add a hard "unanchored team" warning.
3. Reliability diagram + ECE; extend `calibration_report` to quantile bins.
4. A diagnostics refresh job (mirror `wca_snapshotd` `include_tracking=True`).
5. Once host dilution is wired, a host-nation calibration slice.
