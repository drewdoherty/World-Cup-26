# Multi-Venue Shadow Book Methodology

## Purpose

The shadow book is an isolated forecasting laboratory. It is not a live-trade
queue and it does not optimize the live system's closing-line-value objective.
Its job is to increase the number and diversity of falsifiable forecasts across
World Cup match-event and tournament markets, then use settled outcomes to
improve calibration and market selection.

## Experimental unit

One row is one timestamped venue/market/outcome observation. The row preserves:

- canonical market key and settlement basis;
- venue, instrument, displayed price, bid/ask/spread/depth where available;
- raw forecast, calibrated forecast, and forecast provenance;
- action or abstention, reason, side, simulated fill price, and stake;
- eventual binary or half-void outcome and simulated P&L.

Abstentions are data. Omitting them would make coverage and selection-policy
evaluation impossible and would introduce survivorship bias.

## Forecast sources

1. `production_model`: the forest's model probability. The paper decision uses
   a fee-adjusted quarter-Kelly rule after family/venue calibration.
2. `market_prior_exploration`: used only when there is no production model.
   The market probability is the forecast baseline and the $1 position is a
   controlled coverage experiment, not an alpha claim.
3. `cross_venue_pm` / `cross_venue_hl`: one venue's midpoint is a relative-value
   forecast for the other venue. These are explicitly tracked separately from
   the football model.

## Selection policy

- Model-backed entry: best of YES or NO after the Polymarket sports taker fee,
  minimum 1 percentage point edge, quarter Kelly, $40 position cap, $160 model
  exposure per fixture.
- Market-only exploration: deterministic YES/NO assignment, $1 per market,
  maximum $50 new exploration per fixture per cycle, 2-98 cent price range.
- Existing market/side positions are not duplicated. Later cycles walk forward
  through previously untraded families until coverage is exhausted.
- Cross venue: only fee-surviving, executable pairs with matched settlement and
  a fresh snapshot. Stale data and divergent settlement tails fail closed and
  remain visible as abstentions.

## Learning

Calibration is prequential. For each venue x family x forecast-source stratum,
settled observations in the same 10-point probability bin update a Beta-style
reliability estimate. A 20-observation prior shrinks the empirical frequency to
the raw forecast. Only outcomes available before the next decision can affect
that next decision.

This first implementation intentionally uses transparent reliability-bin
calibration. Hierarchical logistic calibration should replace it only after the
book has enough settled events to identify family and player effects without
overfitting.

## Evaluation hierarchy

1. Forecasting: Brier score, log loss (next schema revision), calibration gap,
   and sharpness by venue/family/source.
2. Selection: coverage, enter/abstain rates, reason distribution, counterfactual
   performance of abstained rows (next schema revision), and family sample size.
3. Trading simulation: P&L, drawdown, exposure, fill realism, fee drag, and
   cross-venue executable profit.

P&L is useful but is not treated as proof of forecast quality on a two-match
sample. Market-only exploration is never combined with model-alpha results.

## Non-negotiable invariants

- The database is `data/shadow_book.db`, separate from the live ledger.
- No code path signs or submits an order.
- Market settlement bases must match before a cross-venue pair is tradable.
- Nested contracts use the generic identity in `wca.hl.dominance`: an HL
  ET+pens advancement contract is the superset of PM team-win-in-90, with the
  missing branch equal to `Draw90 AND wins ET/pens`. The engine uses directly
  purchasable YES/NO legs rather than assuming short inventory.
- A positive zero-fee dominance margin is only
  `CANDIDATE_FEE_UNVERIFIED` until the HL settlement fee is observed or
  authoritatively specified. It is never presented as guaranteed arbitrage
  while that fee remains unknown.
- Stale venue snapshots cannot create positions.
- Forecast provenance and abstentions cannot be dropped.
- Calibration cannot use the outcome of the observation being calibrated.
- A market-only exploration fill cannot be relabeled as a model recommendation.

## Commands

```bash
python scripts/wca_shadow_book.py cycle
python scripts/wca_shadow_book.py report
python scripts/wca_shadow_book.py settle --settlements settlements.json
```

The site feed is `site/shadow_book.json`; the audit surface is
`site/shadow-book.html`.
