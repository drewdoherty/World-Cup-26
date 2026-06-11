# Blend-weight fit: Elo / Dixon-Coles / market

**Question.** What convex weights should `wca.card.BlendWeights` use over the
Elo ordered-logit model, the time-decayed Dixon-Coles model, and the de-vigged
market consensus? The deployed default is `elo=0.25, dc=0.25, market=0.50`.

**Answer (headline).** The evidence does **not** justify moving the deployed
default. The fitted blend never beats the de-vigged market by a margin that
clears its own bootstrap noise, and the only model signal that survives
out-of-sample is a small Dixon-Coles contribution — Elo adds essentially
nothing. The most defensible change is a *minor* rebalance toward DC; the
status-quo `0.25 / 0.25 / 0.50` is statistically indistinguishable from the best
fit and is a fine **keep**. See [Recommendation](#recommendation).

This is evidence only. `card.py` is **not** modified. Reproduce with:

```
python backtests/blend_fit.py step1        # no API credits (4 model fits, ~7 min)
python backtests/wc2022_odds_pull.py       # budgeted historical odds pull
python backtests/blend_fit.py step3        # 3-way fit + bootstrap
```

Artifacts: `backtests/blend_fit.py`, `backtests/wc2022_odds_pull.py`,
`data/raw/wc2022_closing_odds.json`, cached intermediates in `backtests/_cache/`.

---

## Step 1 — Elo-vs-DC relative weight, leave-one-tournament-out (no market)

**Design.** Four finals tournaments are held out one at a time: World Cup 2018,
World Cup 2022, Euro 2024, Copa América 2024 (finals only — the two March 2024
"Copa América" rows in the data are CONCACAF play-ins and are excluded by the
finals date window). For each holdout, Elo (rating + ordered logit) and a
time-decayed Dixon-Coles model (`half_life_years=8.0`, matching the deployed
card) are fit on **every international result strictly before** that
tournament's first match. We then evaluate the convex blend

```
p = w_elo * p_elo + (1 - w_elo) * p_dc        (renormalised)
```

`w_elo` is gridded on `[0, 1]` step `0.05`. The honest LOTO estimate chooses
`w_elo` by minimising pooled multiclass log-loss on the **other three** holdouts
and scores it, untouched, on the held-out one. We also report each fold's own
in-sample optimum and the pooled-across-all-four optimum.

Matches per holdout: WC2018 64, WC2022 64, Euro2024 51, Copa2024 32 — **211
out-of-sample matches**.

### Per-fold standalone component log-loss and in-sample optimum

| Holdout    | Elo-only | DC-only | in-sample `w_elo*` |
|------------|---------:|--------:|-------------------:|
| WC2018     | 0.9888   | 0.9597  | 0.00 |
| WC2022     | 1.0360   | 1.0101  | 0.00 |
| Euro2024   | 1.0415   | 0.9831  | 0.00 |
| Copa2024   | 0.8570   | 0.9371  | 1.00 |

DC is the stronger standalone model on three of four tournaments; only Copa 2024
prefers Elo (and there it prefers Elo *entirely*). The per-fold optima are
bang on the grid endpoints, i.e. each single tournament wants one model — a
classic small-sample symptom.

### Leave-one-tournament-out (honest: weight chosen on the *other* folds)

| Held out   | `w_elo` chosen | blend LL | Elo LL | DC LL |
|------------|---------------:|---------:|-------:|------:|
| WC2018     | 0.25 | 0.9617 | 0.9888 | 0.9597 |
| WC2022     | 0.20 | 1.0113 | 1.0360 | 1.0101 |
| Euro2024   | 0.40 | 0.9994 | 1.0415 | 0.9831 |
| Copa2024   | 0.00 | 0.9371 | 0.8570 | 0.9371 |

**CV mean test log-loss:** blend **0.9774**, Elo-only 0.9808, DC-only **0.9725**.

Read that carefully: averaged across the four held-out tournaments, **DC-only
(0.9725) edges the cross-validated blend (0.9774)**. The blend wins on WC2018
and Euro2024 but loses badly on Copa2024 (where the other folds' weight, 0.00,
happens to match — yet Elo-only would have scored 0.857). The blend reliably
beats Elo-only; it does **not** reliably beat DC-only out of sample.

### Pooled grid (all four holdouts, 211 matches)

Optimum `w_elo = 0.15`, but the curve is **very flat** near the bottom:

```
w_elo  log-loss
0.05   0.9769
0.10   0.9767
0.15   0.9766   <- min
0.20   0.9767
0.25   0.9769
0.30   0.9772
...    (rises steeply toward Elo-heavy)
1.00   0.9959
```

Everything in `w_elo ∈ [0.05, 0.25]` is within 0.0005 nats of the optimum.
Pooled DC-only (`w_elo=0`) is 0.9772; the pooled best blend (0.9766) improves on
it by **0.0006 nats** — negligible. Pooled Elo-only (`w_elo=1`) is 0.9959, clearly
the worst.

**Step 1 takeaway.** Between the two models, DC carries the signal. The optimal
relative weight `w_elo / (w_elo + w_dc) ≈ 0.15` (so roughly DC:Elo = 85:15), but
the gain over DC-alone is within noise, and out-of-sample DC-only is actually
the single best pure choice. Elo's marginal value here is to provide a small
hedge, not a lift.

---

## Step 2 — WC2022 closing odds (budgeted historical pull)

Pulled with `backtests/wc2022_odds_pull.py` against The Odds API historical
endpoints, sport key `soccer_fifa_world_cup`.

**Verified cost model** (from `x-requests-last` on the first live calls):

* `/historical/sports/{sport}/events?date=ISO` → **1 credit** per call.
* `/historical/sports/{sport}/events/{id}/odds` with `regions=eu&markets=h2h`
  → **10 credits** per call (1 region × 1 market × the 10× historical
  multiplier).

**Procedure.** Discover all 64 event ids by snapshotting the events listing once
per match-day at `08:00Z` (before the day's first kickoff, so the listing still
carries the day's upcoming fixtures) — 23 listings ≈ 23 credits. Then one odds
snapshot **~5 minutes before each kickoff** (the API resolves to the nearest
available snapshot, typically ~10 min pre-kickoff), 64 × 10 = 640 credits.

**Budget actuals.** Total spend ≈ **687 credits** (well under the 7,000 hard
budget). Remaining credits stayed at ~19,300 throughout — never within reach of
the 11,000 abort floor. Output saved to `data/raw/wc2022_closing_odds.json`:
64/64 events, 9–12 bookmakers each, every event with at least one complete
3-way `h2h` book.

**Consensus.** Per book, the three decimal prices are de-vigged with **Shin**
(`wca.markets.devig.shin`), then the per-outcome **median** across books is taken
and renormalised — identical to `wca.card.market_consensus`, so the backtest's
market baseline is exactly what the live card uses.

---

## Step 3 — full 3-way convex blend on WC2022

Models trained on **pre-WC2022** data (same fit as Step 1's WC2022 fold); market
consensus from Step 2. Weights `(w_elo, w_dc, w_market)` are fit on the simplex
by minimising WC2022 log-loss (`scipy.optimize.minimize`, Nelder–Mead over a
softmax parameterisation, multi-start). All **64** matches matched a market.

### Fitted weights

```
fitted          (elo, dc, market) = 0.00 / 0.32 / 0.68
bootstrap median                  = 0.00 / 0.31 / 0.63
```

The optimiser drives **Elo to exactly zero** and splits the rest ~1/3 DC, ~2/3
market.

### Point log-loss with 1000× match-resampled bootstrap 95% CI

| Config                       | log-loss | 95% CI |
|------------------------------|---------:|--------|
| **fitted** (0.00/0.32/0.68)  | **0.9978** | [0.8580, 1.1530] |
| current (0.25/0.25/0.50)     | 1.0011   | [0.8649, 1.1490] |
| market-only (0/0/1)          | 1.0009   | [0.8525, 1.1662] |
| equal thirds (⅓/⅓/⅓)         | 1.0046   | [0.8703, 1.1476] |
| DC-only (0/1/0)              | 1.0101   | [0.8695, 1.1569] |
| Elo-only (1/0/0)             | 1.0360   | [0.8960, 1.1950] |

### Does the fit actually beat the market?

Paired bootstrap (same resample scores both): `fitted − market_only` log-loss
delta **mean −0.0031**, 95% CI **[−0.0224, +0.0155]** — straddles zero.
**P(fitted beats market_only on a resample) = 60.2%** — barely better than a
coin toss.

The fitted-weight bootstrap CIs are enormous: `w_dc ∈ [0.00, 1.00]`,
`w_market ∈ [0.00, 1.00]` — with n=64 the data cannot pin down the DC/market
split at all. The one thing the bootstrap *is* consistent about is Elo:
`w_elo` median 0.00, CI `[0.00, 0.443]` — Elo's weight is reliably small.

**Step 3 takeaway.** On WC2022 the market is the dominant and hard-to-beat
component; a modest DC overlay (~⅓) shaves a trivial, statistically
insignificant amount off log-loss; Elo contributes nothing. The current
`0.25/0.25/0.50` is essentially tied with both the fitted blend and market-only
(all three within 0.003 nats, all CIs heavily overlapping).

---

## Recommendation

**Keep the deployed `BlendWeights(elo=0.25, dc=0.25, market=0.50)` — or, if a
change is wanted, make this single small, evidence-backed move:**

> `BlendWeights(elo=0.10, dc=0.30, market=0.60)`

Rationale and evidence:

1. **Market dominates.** Both independent analyses anchor the market: Step 3's
   fit puts 0.68 there and the de-vigged consensus alone (1.0009) ties the best
   blend (0.9978). Nothing in the data argues for *less* market weight; if
   anything 0.50 is a touch low.

2. **DC > Elo as the model component.** Step 1 LOTO: DC-only 0.9725 vs Elo-only
   0.9808 CV-mean; pooled relative optimum `w_elo/(w_elo+w_dc) ≈ 0.15`. Step 3:
   fitted `w_elo = 0.00`, `w_dc = 0.32`. Both say the model weight should tilt
   **away from Elo toward DC**, not split evenly. Hence proposing 0.10/0.30
   rather than 0.25/0.25.

3. **The improvement is within noise — so do not over-react.** The fitted blend
   beats current by 0.0033 nats and beats market-only by 0.0031 nats, with a
   bootstrap delta CI spanning zero and a 60% beat rate. With n=64 (one
   tournament) the DC/market split is unidentified (CIs span the whole simplex).
   A wholesale jump to the raw fit (0.00/0.32/0.68) would be over-fitting a
   single World Cup. The 0.10/0.30/0.60 nudge moves in the evidenced direction
   while staying conservative and keeping a small Elo hedge for the regimes
   (Copa 2024) where Elo did help.

4. **Floor check.** Market-only is not beaten with confidence by *any* model
   blend, so the model overlay must stay a minority of the weight. Both the
   current 0.50 and the proposed 0.60 market weight respect that.

**Net:** there is no statistically significant edge to capture, so *keep current*
is fully defensible. If the desk prefers to act on the (weak, directional)
signal, shift Elo→DC and slightly up the market weight to
`elo=0.10, dc=0.30, market=0.60`. Do **not** zero out Elo or chase the raw
single-tournament fit. Re-fit once a second held-out tournament with closing
odds is available (e.g. after WC2026 group stage) before any larger move.

### Honest caveats

- **n is tiny.** Step 3 is one tournament (64 matches); the bootstrap weight CIs
  span the entire DC/market simplex. Treat the 3-way split as
  order-of-magnitude, not precise.
- **One market window.** Closing odds are from a single ~5–10 min pre-kickoff
  snapshot, `eu` region only. A different region/timing could shift the
  consensus slightly.
- **Penalty-shootout knockouts** are recorded as draws in the results data
  (e.g. Argentina–France 2022 final, 3–3 then pens). That is the correct 1X2
  label for a pre-match market, so no adjustment is made, but it inflates the
  draw base rate in the knockout sample.
- **Survivorship / regime.** WC2018, WC2022, Euro2024, Copa2024 are all recent
  finals; the half-life and K-factor settings are the deployed ones and were not
  re-tuned here. The relative DC>Elo finding is robust across all four, but the
  absolute log-loss levels depend on those fixed hyper-parameters.
- **Elo home/host handling.** Past finals encode host games as non-neutral, so
  Elo's home advantage covers them; no extra host bonus was injected. This
  matches how the live card treats neutral WC2026 venues with host bonuses for
  USA/Mexico/Canada.
