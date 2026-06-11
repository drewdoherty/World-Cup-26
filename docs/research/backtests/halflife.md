# Dixon-Coles time-decay half-life backtest

Walk-forward, out-of-sample evaluation of the Dixon-Coles decay half-life on three recent tournament holdouts. **Evidence only** -- `card.py` is not modified by this study. The deployed card uses `half_life_years = 8` (`wca.card.fit_models`); the module default is 2.0.

## Method

For each holdout block, a fresh Dixon-Coles model is fit on every *played* international strictly before the block's first match, with the decay `reference_date` pinned to that start date. Each holdout match's 1X2 is predicted respecting the `neutral` flag (hosts appear as non-neutral home rows, so they receive home advantage automatically). Scores are multiclass natural-log log-loss and 3-class Brier vs the realised outcome. Elo (ratings + ordered logit) carries no decay and is fit once per block; the blend is a fixed 50/50 Elo+DC mix per half-life. Aggregates are match-count-weighted means across blocks.

Holdout blocks:

| Block | Matches | Train cutoff |
|---|---:|---|
| WC2018 | 64 | < 2018-06-01 |
| WC2022 | 64 | < 2022-11-01 |
| Euro2024+Copa2024 | 83 | < 2024-06-01 |

## Aggregate (Dixon-Coles only)

Match-count-weighted mean across all holdouts.

| half-life (yr) | log-loss | Brier |
|---:|---:|---:|
| 1 | 0.9932 | 0.5847 |
| 2 | 0.9811 | 0.5783 |
| 4 | 0.9773 | 0.5772 | **(best)**
| 8 | 0.9789 | 0.5795 | *(deployed)*
| 16 | 0.9853 | 0.5847 |

## Aggregate (50/50 Elo + DC blend)

Match-count-weighted mean across all holdouts.

| half-life (yr) | log-loss | Brier |
|---:|---:|---:|
| 1 | 0.9888 | 0.5847 |
| 2 | 0.9845 | 0.5823 |
| 4 | 0.9824 | 0.5813 |
| 8 | 0.9817 | 0.5814 | **(best)** *(deployed)*
| 16 | 0.9833 | 0.5828 |

## Per-holdout log-loss (Dixon-Coles only)

| half-life (yr) | WC2018 | WC2022 | Euro2024+Copa2024 |
|---:|---:|---:|---:|
| 1 | 0.9648 | 1.0656 | 0.9593 |
| 2 | 0.9556 | 1.0404 | 0.9551 |
| 4 | 0.9580 | 1.0255 | 0.9549 |
| 8 | 0.9608 | 1.0113 | 0.9679 |
| 16 | 0.9622 | 1.0055 | 0.9876 |

Per-holdout best half-life (DC log-loss): WC2018: 2; WC2022: 16; Euro2024+Copa2024: 4.

## Per-holdout log-loss (50/50 blend)

| half-life (yr) | WC2018 | WC2022 | Euro2024+Copa2024 |
|---:|---:|---:|---:|
| 1 | 0.9711 | 1.0435 | 0.9602 |
| 2 | 0.9696 | 1.0331 | 0.9586 |
| 4 | 0.9707 | 1.0255 | 0.9582 |
| 8 | 0.9701 | 1.0172 | 0.9633 |
| 16 | 0.9687 | 1.0128 | 0.9719 |

Per-holdout best half-life (blend log-loss): WC2018: 16; WC2022: 16; Euro2024+Copa2024: 4.

## Recommendation

- **Best half-life (DC-only, pooled log-loss):** 4 (log-loss 0.9773).
- **Best half-life (50/50 blend, pooled log-loss):** 8 (log-loss 0.9817).
- **Margin vs deployed 8.0 (DC-only):** +0.0016 log-loss (deployed 0.9789 -> best 0.9773; positive means best is better).
- **Margin vs deployed 8.0 (blend):** +0.0000 log-loss (deployed 0.9817 -> best 0.9817).
- **Per-holdout consistency (DC-only):** 2 of 3 blocks favour half-life 4 over 8.0. Per-block (deployed-best) log-loss deltas: WC2018 +0.0028, WC2022 -0.0143, Euro2024+Copa2024 +0.0130.

**Verdict.** The pooled margin over 8.0 is small (0.0016 log-loss) and/or inconsistent across holdouts (2/3 blocks favour it), so the difference is **not decision-grade**: keep 8.0 unless a larger study confirms 4. 

_Caveat: three holdouts is a small sample (~211 matches total); log-loss differences of a few thousandths are within tournament-level noise. Treat this as directional, not definitive._
