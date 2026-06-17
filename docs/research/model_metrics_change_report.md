# Model-metrics-change report

**Date:** 2026-06-17
**Question (operator):** How do the model's *output* metrics — Brier, log-loss,
calibration, and the fair-probability distribution — change for
**(i) the Klement structural-prior enhancement** and
**(ii) the automated WC26 cleaned-data pipeline**
(`data/raw/martj42_cleaned.csv` vs the prior source `data/raw/results.csv`)?

> **TL;DR.** The two changes act on completely different scales.
> The **cleaned-data pipeline materially improves the model** (Brier
> −0.0087, log-loss −0.0139 on the 20 played WC2026 fixtures; mean output shift
> 0.80pp, max 3.99pp) because it fixes wrong / missing scores that feed the fit.
> The **Klement prior is metrics-neutral** on the same data-rich fixtures
> (Brier +0.0004, mean shift 0.11pp) — by design it only engages for data-poor
> minnows, of which the current live set has none. In short: **data quality is
> ~7x more impactful on model output than the structural prior** here.

All numbers are evidence only; no deployed file, config, or default is changed.

---

## (i) The Klement structural-prior enhancement

`fit_models(structural_prior=True)` shrinks the Dixon-Coles attack/defence
parameters toward a socio-economic strength target (population × football
culture, an inverted-U in GDP/capita peaking at $60k, confederation offset;
`src/wca/models/structural.py`, prior scale 0.15) instead of toward zero. It
reaches the deployed blend only through DC's 25% weight, and is **off by
default**. Full per-fixture re-pricing is in
[`klement_repricing_report.md`](klement_repricing_report.md); the metrics summary:

**Fair-probability distribution shift (20 logged fixtures, blended model):**

| Statistic | Klement OFF | Klement ON | Δ |
|---|---:|---:|---:|
| Mean per-outcome shift (total-variation distance) | — | — | **0.114 pp** |
| Max per-outcome shift | — | — | 0.253 pp |
| Mean longshot (min) leg | 16.398% | 16.383% | −0.015 pp |
| Mean favourite (max) leg | 60.460% | 60.469% | +0.009 pp |

**Calibration (16 settled fixtures, 3-way Brier / log-loss):**

| Variant | Brier | Log-loss |
|---|---:|---:|
| Blended model, Klement OFF | 0.53717 | 0.90526 |
| Blended model, Klement ON | 0.53761 | 0.90575 |
| Δ (ON − OFF) | **+0.00044** | **+0.00049** |
| DC-only, OFF | 0.52218 | 0.87153 |
| DC-only, ON | 0.52395 | 0.87352 |
| Δ (ON − OFF) | +0.00177 | +0.00199 |

**Reading.** On this data-rich field the prior is **calibration-neutral to a
hair negative** — it nudges already-well-estimated parameters off their MLE, so
both Brier and log-loss tick up by < 0.0005 on the blend. This is *not*
decision-grade and exactly reconfirms the structural-prior backtest
(`backtests/structural_prior.md`): inert on data-rich teams, untestable here on
its true target (data-poor minnows, of which the live set has none).

**Where it would change metrics.** A synthetic thin-data experiment (four minnows
truncated below the `min_matches = 5` low-data threshold) grows the per-match
fair-prob shift to **0.8-1.6pp** for minnow-vs-minnow fixtures — 6-10x the
live-set effect — confirming the prior's metric impact is real but confined to
the data-poor regime it targets. See the re-pricing report, §5.

## (ii) The automated WC26 cleaned-data pipeline

`data/raw/results.csv` is the immutable upstream martj42 mirror (re-fetched daily,
would clobber hand edits). `data/raw/martj42_cleaned.csv` is the file **every
consumer actually reads** — produced by a deterministic, idempotent correction
overlay (`src/wca/data/cleaning.py` applying `data/corrections.json`,
match-keyed on `(date, home_team, away_team)`, canonical spelling).

**What the overlay changed (current corrections set, n = 10):**

* **Score corrections** to wrong upstream values, e.g.
  Guatemala–Czech Republic `1-5 → 1-3`, Bermuda–Cape Verde `3-0 → 0-3`
  (a swapped scoreline), Germany–Curaçao confirmed `7-1`.
* **Filled-in WC2026 results** that were `NA` in the raw mirror at fit time:
  France–Senegal, Iraq–Norway, Argentina–Algeria, Austria–Jordan, etc.
* A small number of de-duplicated / re-ordered rows.
* Sources: `espn+thesportsdb` (7), plus ESPN/VAVEL/FOX/FIFA cross-checks.

**Effect on model output — Elo+DC model (ex-market) on 20 played WC2026 fixtures,
fit on RAW vs fit on CLEANED:**

| Statistic | RAW source | CLEANED source | Δ |
|---|---:|---:|---:|
| 3-way Brier | 0.59670 | 0.58799 | **−0.00871** |
| Log-loss | 0.96823 | 0.95437 | **−0.01386** |
| Mean per-fixture output shift (TVD) | — | — | 0.796 pp |
| Max per-fixture output shift (TVD) | — | — | 3.99 pp |

> Metrics here are higher (worse) in absolute terms than the §(i) blended numbers
> because this isolates the **Elo+DC model with the market leg removed** — the
> market is the strongest leg of the deployed blend. The comparison is
> apples-to-apples within the row (same model, only the data source differs), so
> the **Δ** is the clean measure of the cleaning effect.

**Biggest movers (raw → cleaned):**

| Fixture | TVD | Why |
|---|---:|---|
| Austria vs Jordan | 3.99 pp | WC2026 score was `NA` in raw, filled in cleaned (re-fits both sides) |
| Iraq vs Norway | 3.75 pp | same — `NA → 1-4` |
| France vs Senegal | 2.46 pp | same — `NA → 3-1` |
| Argentina vs Algeria | 1.79 pp | same — `NA → 3-0` |
| Spain vs Cape Verde | 1.64 pp | corrected upstream score feeding Cape Verde's thin record |

**Reading.** Cleaning **improves** every headline metric: Brier −0.0087 and
log-loss −0.0139 are an order of magnitude larger than the Klement prior's
(neutral) effect and are in the *right* direction — they come from feeding the
fit correct, complete scores instead of wrong or missing ones. The largest
output shifts are precisely the fixtures whose scores went from `NA`/wrong in raw
to correct in cleaned. The mean 0.80pp shift (max 3.99pp) is **~7x** the Klement
prior's mean 0.11pp (max 0.25pp).

## Side-by-side

| Change | Mean output shift | Max output shift | Brier Δ | Log-loss Δ | Direction |
|---|---:|---:|---:|---:|---|
| **(i) Klement prior** (blend) | 0.11 pp | 0.25 pp | +0.0004 | +0.0005 | neutral / hair worse |
| **(ii) Cleaned-data pipeline** (Elo+DC) | 0.80 pp | 3.99 pp | **−0.0087** | **−0.0139** | clearly better |

## Conclusions

1. **Data quality dominates.** The automated cleaning pipeline is the materially
   beneficial change to model output — it fixes the inputs every model leg
   depends on, and the improvement is real and decision-grade in direction.
   Keep the cleaning pipeline running on every CI tick (it is idempotent and
   safe).
2. **The Klement prior is correctly defaulted off** for the liquid 1X2 card: it
   is metrics-neutral on data-rich fixtures and its only real bite is in the
   data-poor minnow regime, which liquid 1X2 markets do not exercise. Its value
   proposition remains the thin Polymarket outright / advancement markets, to be
   evaluated on live minnow data — not enabled wholesale.
3. The two changes are **orthogonal and stackable**: cleaning improves the
   likelihood for everyone; the prior (if enabled) only adjusts teams the
   likelihood can't yet pin down. They do not interfere.

---

### Reproduction

* Data: `data/raw/results.csv` (raw mirror), `data/raw/martj42_cleaned.csv`
  (cleaned, deployed), `data/corrections.json` (overlay),
  `data/model_predictions_log.jsonl` (recorded outputs).
* Code: `wca.card.fit_models(structural_prior=…)`, `dc_probs`, `elo_probs`,
  `BlendWeights()` (0.25/0.25/0.50); `wca.data.cleaning` overlay;
  `wca.models.structural` (scale 0.15, $60k GDP peak).
* Metrics: 3-way Brier `Σ(p−y)²` and log-loss `−ln p(outcome)`, match-count-equal
  weighting, on settled WC2026 fixtures.
