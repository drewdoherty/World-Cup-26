# PM selection preferences — backtest (2026-07-02)

Question (user): before encoding into `/pm` ranking, did these hold for the WC
so far? **(1)** prefer +EV moneylines over longshots; **(2)** prefer
further-away fixtures over imminent ones (further away = more likely
mispriced).

Method: read-only over the mini's canonical data. Hypothetical flat-$1 bets
wherever `model_prob − market_prob ≥ 2%`, deduplicated, resolved against
played results. Three datasets (PM 1X2 snapshot history does not exist —
`odds_snapshots` is 100% theoddsapi; the #109 PM snapshotter had produced no
rows yet — so PM-specific evidence comes from the advancement price log and
the realized ledger, with the books cross-section as the large-n proxy).

## 1. Books-consensus proxy (1X2, vig-in consensus, n=12 deduped)

Model (`model_predictions_log.jsonl`, leakage-free: latest prediction before
each snapshot) vs the hourly average implied probability across all books;
vig left IN, so the 2% edge bar is conservative.

| model-prob bucket | n | hits | flat-$1 ROI |
|---|---|---|---|
| ≥50¢ (moneylines) | 2 | 2 | **+102.7%** |
| <25¢ (longshots) | 10 | 0 | **−100.0%** |

| lead-time bucket | n | hits | ROI |
|---|---|---|---|
| 12–48h | 5 | 1 | −59.8% |
| <12h | 7 | 1 | −70.8% |
| >48h | 0 | — | no sample (prediction log doesn't reach >48h pre-KO) |

## 2. PM advancement prices (`pm_price_history.jsonl`, "reach R16", resolved via the results-conditioned advancement feed; n=12 deduped team/day)

| model-prob bucket | n | hits | flat-$1 ROI |
|---|---|---|---|
| ≥50¢ | 3 | 2 | −8.0% |
| 25–50¢ | 7 | 2 | +81.8% |
| <25¢ | 2 | 0 | **−100.0%** |

Losers were exactly the eliminated longshot names (Ecuador, Sweden, Ivory
Coast, South Africa); winners Brazil/Paraguay.

## 3. Realized PM ledger (settled bets, real money)

| model-prob bucket | n | staked | P&L | ROI |
|---|---|---|---|---|
| ≥50¢ | 2 | $47.85 | +$9.88 | +20.6% |
| 25–50¢ | 1 | $8.00 | +$14.22 | +177.8% |
| <25¢ | 38 | $770.91 | +$91.50 | +11.9% |

## Verdicts

**Preference 1 — SUPPORTED (with an honest nuance).** In every *systematic*
test, sub-25¢ longshot "edges" were phantom: 0-for-12 combined here, on top of
the earlier attribution finding (skipped PM longshots 0-for-20; ≥20%-edge
advancement longshots −6.9%). The realized ledger's longshot bucket is net
positive (+11.9%) only because a few large advancement hits outweighed many
losers — a high-variance profile the likely-PnL rule exists to avoid. Ranking
+EV moneylines first is justified by hit-rate and variance, and by every
out-of-sample snapshot test.

**Preference 2 — DIRECTIONALLY CONSISTENT, NOT PROVEN.** 12–48h beat <12h
(−59.8% vs −70.8%) but both were negative (longshot-dominated) and there is
NO >48h sample: the prediction log simply doesn't exist far enough before
kickoff, and PM 1X2 price history was never captured. The hypothesis stays
encoded as a *preference* (ordering, not a gate). Re-test once the CLOB
capture daemon (Phase-1 increment 6) has accumulated far-out PM prices.

## Caveats

Tiny n throughout; vig-in proxy for the books test; advancement resolution via
the results-conditioned feed (results.csv itself lags days); flat-stake, no
fees on the books proxy (PM fee applied upstream in the advancement log's
edge). Nothing here is significant at conventional thresholds — it is the
right *direction* of evidence for a soft ordering rule, no more.

## What was shipped on the back of this

`scripts/wca_pm_propose.py::preference_sort_key` — proposals now order:
model ≥50¢ bucket, then 25–50¢, longshots last; further-out kickoff first
within a bucket; EV as tiebreak. `/pm` documents the rule inline.
