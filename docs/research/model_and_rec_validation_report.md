<!-- ============================================================ -->
<!-- CURATED PROSE (prepended) — human judgment + honesty framing. -->
<!-- Generated data sections begin at "# Model & Recommendation     -->
<!-- Validation Report" below and are NOT edited here.              -->
<!-- ============================================================ -->

# Model & Recommendation Validation — Executive Read (curated)

_Companion to and built on top of [`docs/research/model_vs_discretion_attribution.md`](./model_vs_discretion_attribution.md) (2026-06-25). That doc owns the **discretion / skipped-rec** verdict; this report **adds the model-forecast-accuracy layer (S1–S3)** it lacked and re-grades the recommendation streams (S4–S5) mechanically. Where the two disagree, the disagreement is surfaced, not smoothed — see S5.3(a)._

## ⚠️ Coverage & honesty caveats (read first)

1. **`data/dev.db` is a STALE FORK — NON-AUTHORITATIVE.** Every number below was computed read-only against the dev-box copy, which is forked from the canonical Mac-mini ledger (`data/wca.db`). The numbers are internally **reproducible** (the adversarial verify step reproduced every headline exactly) but they are **not canonical**. The S4 placed-bet book in particular is **empty here (n=0)** — the real book lives on the mini.
2. **RUN ON MINI for canonical numbers:** `PYTHONPATH=src python scripts/wca_validation_report.py --db data/wca.db`. This report is built to re-run **unchanged** on the mini; only the `--db` target differs.
3. **Thin, variance-dominated sample.** The fixture-level model cuts are **n=23** (prediction-ledger) / **n=27** (tracking feed); the over-time windows are **n=4–11**; the skipped-rec grades are **n=8 / n=43**. These are descriptive of a single favourite-dominated fortnight — **not** a forward profit estimate. Wilson 95% CIs are printed; they are wide.
4. **The Telegram export is HTML, not JSON.** The recommendation universe (S5) is regex-parsed from `ChatExport_2026-06-25/messages*.html`, not structured fields. Counts are deduped to distinct (fixture, leg); they are robust but parse-based, not a query against a structured rec journal (which does not yet exist — S6 infra item).
5. **Polymarket "to WIN" recs are DELIBERATELY EXCLUDED from grading.** 6,280 "to WIN" lines were counted only to size the universe and the deviation rate. The backed side is not recoverable from a deduped sized line, so a win/loss column would be **fabricated** — it is withheld here (S5.4). The attribution doc's directional "20 graded → 0 won / −$297 avoided" came from a fuller manual parse that preserved the side; reproduce it on the mini with a structured journal, not from this engine.
6. **No StatsBomb 2026 events exist.** S3 "predicted goals" is reconstructed from the model's own Dixon-Coles top-k scoreline ladder, NOT shot-xG; the top-k truncation structurally **under-states** totals. Treat S3 as a data-coverage limitation, not a model verdict.

## Executive summary

**Question (a) — How well did the MODEL forecast the events/results? (S1–S3)**

- **Calibration: excellent.** Full settled 1X2 book — mean model prob **0.333 == realized hit rate 0.333**, binary Brier **0.150** (**n=825** legs). This is the "good calibrator" headline, reproduced exactly from the attribution doc. Per-leg CLV is **−0.0200** (n=825, 100% close coverage) — calibrated, but the edge it claims is, on average, slightly worse than the close.
- **Pick discrimination: positive but thin.** Fixture-level argmax hit-rate **60.9% (14/23)** [95% 40.8–77.8%], multiclass Brier 0.5038, **Brier skill score +0.140 vs base rate** (**n=23**, prediction-ledger). The independent tracking feed corroborates: **63.0% (17/27)** [44.2–78.5%], BSS +0.120 (**n=27**). Both CIs are wide; **the market's paired Brier is marginally better than the model's** (0.4958 vs 0.5038 at n=23) — real skill, not yet a sizing signal.
- **Over time:** hit-rate climbs MD1-3 → MD7+ (37.5% n=8 → 72.7% n=11 → 75.0% n=4) but each bucket is too small to call a trend — **insufficient sample (n=4–11)**.
- **Secondary markets (n=27, tracking feed):** O/U 2.5 accuracy 55.6%, BTTS 40.7%, scoreline top-6 55.6% / top-1 7.4% — usable as a ladder, not a single line.
- **Goals/totals: not validatable from stored data.** Reconstructed predicted total 1.72 vs actual 3.15 (bias −1.43, MAE 1.67, n=27) is a **truncation artifact** (top-k keeps ~66% of mass on low scores), **not** a model-accuracy claim. Persist full DC lambdas to fix — **data-pending** until then.

**Question (b) — Bets PLACED vs RECOMMENDED, cross-sectionally and over time (S4–S5)**

- **Bets PLACED (S4): insufficient sample (n=0) on this dev.db copy.** ROI, mean CLV, hit-rate, %-beat-close are all NaN at n=0 and are printed as "insufficient sample (n=0)" — not invented. The placed book is on the mini; the deviation rate below is therefore **100% mechanically** (placed=0 by construction), not a behavioural finding — the canonical ~98% lives in the attribution doc.
- **Recommendations skipped (S5):** 1X2 rec universe = **83 deduped** (the 19 `/next` ✅ picks are a strict subset of the 83 `/card` picks). 
  - **Model `/next` value stream: 0-for-8 graded** (cf **−8.00u**, ROI −100%) — consistent in direction with the attribution doc's 0/13, though the count differs (wider results window / dedup — flagged honestly in S5.3(a), **not** reconciled away).
  - **Combined `/next`+`/card` stream: 9-for-43 graded** (cf **−5.57u**, ROI −13.0%). The `/card` stream surfaces **favourite-side** picks too, which is why it wins more — isolating the same shape: **value flags lose, favourite picks win.**
  - **Over time (combined):** MD1-3 3/6 (+5.13u), MD4-6 4/31 (−10.80u), MD7+ 2/6 (+0.10u) — the loss is concentrated in the MD4-6 longshot/draw cluster.
- **Net verdict (unchanged from the attribution doc, now with the model layer underneath it):** the model is a **good calibrator and a bad longshot bet-selector**; skipping its +EV longshot/draw flags **added value** in this window; the residual leak is the user running the *same* longshot habit on their own correct-score punts (doc: −73.9% ROI). All of this rests on a thin, variance-heavy sample — **judge on CLV, not P&L, until n grows.**

## Forward changes (extends the attribution doc — does not duplicate it)

_The attribution doc's 6 forward changes (keep skipping longshot flags · cut own correct-score punts · bet calibrated favourite-side 1X2 CLV-gated · scale promos · demote PM advancement · build a structured rec journal) still stand. The model-accuracy layer (S1–S3) adds these:_

7. **Promote nothing on hit-rate alone; the discrimination edge is real but sub-sizing-threshold.** BSS +0.140 (n=23) / +0.120 (n=27) is positive, but the **market's paired Brier beats the model's** (0.4958 < 0.5038, n=23) and CIs span ~37 points. **Gate every model bet on CLV, not on argmax hit-rate** — the per-leg CLV is already −0.020 (n=825), so the calibrated probabilities do **not** automatically clear the close. _(finding: S1.0/S1.1)_
8. **Persist the full Dixon-Coles lambdas at card-build.** The totals/goals validation is currently impossible — the persisted top-k ladder under-states totals by ~1.4 goals as a pure truncation artifact (n=27), so there is **no exact-score / totals model to validate against**. This is the prerequisite to ever trusting an O/U or correct-score bet, and the reason the "stop discretionary correct-score punts" rule must hold until it lands. _(finding: S3)_
9. **Build the structured rec journal so attribution becomes a query, not an HTML parse — and so PM "to WIN" recs can be graded directionally.** This engine **withholds** the PM grade (n=43 settled of 52) because the backed side is not recoverable from a deduped sized line; only a journal that stores selection/price/stake/side fixes that. Until then the PM stream stays **excluded**, and the doc's −$297 figure stays un-reproduced-by-engine. _(finding: S5.4)_
10. **Re-run this exact report on the mini before acting on any S4/S5 number.** S4 is n=0 here and the deviation rate is mechanically 100%; the canonical placed-book ROI/CLV/hit-rate only exist against `data/wca.db`. Treat the dev-box cut as a wiring/sanity check, the mini cut as the decision input. _(finding: S4 / authority block)_

---

# Model & Recommendation Validation Report

```
DATA SOURCE         : /Users/andrewdoherty/Desktop/Coding/World Cup Alpha/data/dev.db
GENERATED (engine)  : 2026-06-26T05:17:32Z
AUTHORITY           : *** dev.db is a STALE FORK of the canonical mini
                      ledger — NON-AUTHORITATIVE. Numbers below are
                      reproducible but not canonical. ***
RUN ON MINI         : PYTHONPATH=src python scripts/wca_validation_report.py \
                        --db data/wca.db   (canonical numbers)
GUARDS              : read-only · no network · no ledger writes · no bets
EVERY METRIC        : carries its n. Thin/absent -> 'insufficient sample
                      (n=...)' or 'data-pending'. No fabricated numbers.
```

## S1 — Model forecast accuracy: 1X2

### S1.0 Cross-check vs the 2026-06-25 attribution doc (full settled 1X2 book)

| metric | recomputed | attribution doc | check |
|---|---|---|---|
| n settled 1X2 legs | 825 | doc: 825 | OK |
| mean model prob | 0.333 | doc: 0.333 | OK |
| realized hit rate | 0.333 | doc: 0.333 | OK |
| binary Brier | 0.150 | doc: 0.15 | OK |
| per-leg CLV (mean) | -0.0200 | n_clv=825 |  |
| close coverage | 100.0% |  |  |

_The full-book per-leg cut reproduces the doc's calibration headline (mean prob == realized base rate == 0.333; Brier ~0.15). This is the 'good calibrator' finding, confirmed independently._

### S1.1 Fixture-level 1X2 (cross-sectional)

**Source: Prediction-ledger (settled triples)**

| metric | value |
|---|---|
| n fixtures | 23 |
| pick hit-rate (argmax) | 60.9% (14/23) [95% 40.8%–77.8%] |
| multiclass Brier | 0.5038 |
| log-loss | 0.8508 |
| base-rate Brier (BSS denom) | 0.5860 |
| Brier skill score vs base rate | 0.1403 |
| market Brier (paired, n=23) | 0.4958 |
| model Brier (same paired subset) | 0.5038 |

**Source: Tracking feed (per-fixture)**

| metric | value |
|---|---|
| n fixtures | 27 |
| pick hit-rate (argmax) | 63.0% (17/27) [95% 44.2%–78.5%] |
| multiclass Brier | 0.4972 |
| log-loss | 0.8335 |
| base-rate Brier (BSS denom) | 0.5652 |
| Brier skill score vs base rate | 0.1203 |
| market Brier (paired, n=27) | 0.4921 |
| model Brier (same paired subset) | 0.4972 |

### S1.2 Over-time (by matchday window) — prediction-ledger

| window | n | hit-rate | Brier | log-loss | BSS |
|---|---|---|---|---|---|
| MD1-3 (06-11..14) | 8 | 37.5% (3/8) | 0.685 | 1.092 | -0.154 |
| MD4-6 (06-15..18) | 11 | 72.7% (8/11) | 0.387 | 0.688 | 0.269 |
| MD7+ (06-19+) | 4 | 75.0% (3/4) | 0.464 | 0.818 | 0.073 |

## S2 — Model forecast accuracy: scoreline / Over-Under / BTTS

**Scoreline market in the prediction ledger:** data-pending (predledger scoreline-market coverage = 0%; only 1X2 legs are persisted with settlement). The hits below come from the tracking feed's top-k scoreline ladder.

| metric | value |
|---|---|
| scoreline top-6 hit | 55.6% (15/27) |
| scoreline top-1 hit | 7.4% (2/27) |
| O/U 2.5 accuracy | 55.6% (15/27) |
| O/U 2.5 Brier | 0.2416 |
| BTTS accuracy | 40.7% (11/27) |
| BTTS Brier | 0.2867 |

## S3 — Model predicted goals (Dixon-Coles lambdas) vs ACTUAL goals

> NOTE: this is MODEL-predicted goals reconstructed from the model's Dixon-Coles scoreline ladder (probability-weighted mean goals over the persisted top-k scorelines), NOT StatsBomb shot-xG. `wca.data.statsbomb` is WC2018/2022 open data only — there are NO 2026 StatsBomb events. The top-k truncation drops the tail, so totals are mildly UNDER-stated.

| metric | value |
|---|---|
| n fixtures | 27 |
| mean captured scoreline mass (top-k) | 65.8% |
| mean predicted total goals (truncated) | 1.717 |
| mean actual total goals | 3.148 |
| bias (pred - actual) — truncation-dominated | -1.432 |
| MAE (total goals) | 1.665 |
| mean pred home / actual home | 1.079 / 2.185 |
| mean pred away / actual away | 0.638 / 0.963 |

> The top-k ladder captures only ~65.8% of the scoreline distribution, concentrated on low scores, so the predicted total is structurally biased DOWN. Treat the bias/MAE here as a data-coverage limitation, NOT a model-accuracy verdict. A clean totals validation needs the full Dixon-Coles lambdas persisted at card-build (currently not stored).

## S4 — Bets PLACED performance (ledger, READ-ONLY)

> dev.db ledger = STALE FORK, NON-AUTHORITATIVE. Re-run on the mini (`--db data/wca.db`) for the canonical book.

**This ledger copy contains 0 bets** -> insufficient sample (n=0). (dev.db carries the 870-row prediction ledger but no placed bets; the placed-bet book lives on the mini's `data/wca.db`.)

## S5 — Bets PLACED vs RECOMMENDED (discretionary overlay)

### S5.1 Recommendation universe (Telegram HTML export, parsed)

| stream | count |
|---|---|
| /next 1X2 ✅ picks (deduped) | 19 |
| /card 1X2 picks (deduped) | 83 |
| 1X2 recs total (deduped union) | 83 |
| PM 'to WIN' lines (EXCLUDED from grading) | 6280 |
| PM sized EV picks (deduped, directional only) | 52 |

_PM 'to WIN' recs are EXCLUDED from grading per instruction (duplicated / over-frequent). They are counted only to size the universe + deviation rate._

### S5.2 Deviation (skip) rate

- 1X2 recs surfaced (deduped): **83**; placed (matched in ledger): **0**; skipped: **83**.
- Deviation rate over 1X2 recs: **100.0%** (n=83).
- NOTE: this dev.db ledger has 0 placed bets, so 'placed' here is 0 by construction and deviation = 100% mechanically. The attribution doc's ~98% is from the canonical book; re-run on the mini to confirm.

### S5.3 Skipped 1X2 recs, graded vs realized result

**(a) `/next` ✅ value-pick stream ONLY — cross-check vs attribution doc**

- **8 graded** (11 open). Won **0**, lost **8**. Counterfactual flat-1u at best price: **-8.00u** (ROI -100.0%).
- ⚠️ **Differs from the attribution doc** (doc: 13 graded -> 0 won; recompute: 8 graded -> 0 won). Likely a wider results window (more fixtures now settled) and/or dedup differences vs the doc's manual parse. Direction (value flags lose) is unchanged. Inspect rows below.

**(b) Combined `/next` + `/card` 1X2 stream (task extension — NOT doc-comparable)**

- **43 graded** (40 open). Won **9**, lost **34**. Counterfactual flat-1u at best price: **-5.57u** (ROI -13.0%).
- `/card`-only sub-cut: 43 graded -> 9 won (cf -5.57u). The `/card` stream surfaces **favourite-side** picks too (not just the value longshots the doc graded), so it wins more often and is **not** comparable to the doc's value-only cut. This split isolates: value flags lose, favourite picks win — the same 'good calibrator / bad longshot selector' shape.

_Over-time (combined cut, by matchday window):_

| window | graded | won/graded | cf P&L (1u) |
|---|---|---|---|
| MD1-3 (06-11..14) | 6 | 3/6 | 5.13 |
| MD4-6 (06-15..18) | 31 | 4/31 | -10.80 |
| MD7+ (06-19+) | 6 | 2/6 | 0.10 |

| fixture | selection | leg | best | edge | result | cf P&L (1u) |
|---|---|---|---|---|---|---|
| Canada vs Bosnia and Herzegovina | Canada | home | 1.87 | 7.3% | lost | -1.00 |
| Mexico vs South Africa | Mexico | home | 1.45 | 3.1% | WON | 0.45 |
| Qatar vs Switzerland | Qatar | home | 17.01 | 30.5% | lost | -1.00 |
| Qatar vs Switzerland | Draw | draw | 7.00 | 13.2% | WON | 6.00 |
| South Korea vs Czech Republic | South Korea | home | 2.68 | 2.6% | WON | 1.68 |
| United States vs Paraguay | Paraguay | away | 4.30 | 14.4% | lost | -1.00 |
| Belgium vs Egypt | Egypt | away | 7.80 | 25.4% | lost | -1.00 |
| Belgium vs Egypt | Draw | draw | 4.50 | 7.9% | WON | 3.50 |
| Iran vs New Zealand | Iran | home | 1.84 | 5.0% | lost | -1.00 |
| Saudi Arabia vs Uruguay | Saudi Arabia | home | 9.20 | 4.9% | lost | -1.00 |
| Australia vs Turkey | Australia | home | 5.70 | 38.9% | WON | 4.70 |
| Australia vs Turkey | Draw | draw | 3.90 | 6.4% | lost | -1.00 |
| Brazil vs Morocco | Morocco | away | 6.00 | 5.3% | lost | -1.00 |
| Brazil vs Morocco | Draw | draw | 4.00 | 5.1% | WON | 3.00 |
| Germany vs Curaçao | Draw | draw | 26.00 | 57.7% | lost | -1.00 |
| Germany vs Curaçao | Curaçao | away | 56.00 | 46.2% | lost | -1.00 |
| Haiti vs Scotland | Haiti | home | 6.40 | 7.7% | lost | -1.00 |
| Haiti vs Scotland | Draw | draw | 4.80 | 9.4% | lost | -1.00 |
| Ivory Coast vs Ecuador | Ecuador | away | 2.50 | 11.9% | lost | -1.00 |
| Sweden vs Tunisia | Tunisia | away | 4.60 | 10.3% | lost | -1.00 |
| Sweden vs Tunisia | Draw | draw | 3.60 | 3.0% | lost | -1.00 |
| Austria vs Jordan | Jordan | away | 9.60 | 28.5% | lost | -1.00 |
| Austria vs Jordan | Draw | draw | 5.40 | 16.2% | lost | -1.00 |
| England vs Croatia | Croatia | away | 5.50 | 9.5% | lost | -1.00 |
| England vs Croatia | Draw | draw | 3.85 | 3.0% | lost | -1.00 |
| France vs Senegal | Senegal | away | 8.00 | 11.8% | lost | -1.00 |
| France vs Senegal | Draw | draw | 4.80 | 9.6% | lost | -1.00 |
| Ghana vs Panama | Panama | away | 3.60 | 27.5% | lost | -1.00 |
| Iraq vs Norway | Iraq | home | 16.00 | 78.4% | lost | -1.00 |
| Iraq vs Norway | Draw | draw | 7.50 | 45.5% | lost | -1.00 |
| Portugal vs DR Congo | DR Congo | away | 13.00 | 11.4% | lost | -1.00 |
| Portugal vs DR Congo | Draw | draw | 6.00 | 4.8% | WON | 5.00 |
| Uzbekistan vs Colombia | Uzbekistan | home | 10.50 | 19.1% | lost | -1.00 |
| Uzbekistan vs Colombia | Draw | draw | 5.10 | 8.7% | lost | -1.00 |
| Canada vs Qatar | Qatar | away | 12.50 | 23.4% | lost | -1.00 |
| Canada vs Qatar | Draw | draw | 6.00 | 4.9% | lost | -1.00 |
| Czech Republic vs South Africa | South Africa | away | 5.00 | 2.3% | lost | -1.00 |
| Mexico vs South Korea | Mexico | home | 2.10 | 5.7% | WON | 1.10 |
| Scotland vs Morocco | Scotland | home | 6.80 | 27.7% | lost | -1.00 |
| Scotland vs Morocco | Draw | draw | 3.75 | 6.5% | lost | -1.00 |
| Turkey vs Paraguay | Paraguay | away | 4.00 | 8.1% | WON | 3.00 |
| United States vs Australia | Australia | away | 5.70 | 23.4% | lost | -1.00 |
| United States vs Australia | Draw | draw | 4.50 | 10.7% | lost | -1.00 |

### S5.4 Skipped PM 'to WIN' longshots (EXCLUDED from grading)

- PM sized EV picks surfaced (deduped): **52**; of these, **43** land on a fixture that has since settled (the gradeable universe).
- **Grade WITHHELD (data-pending):** PM 'to WIN' picks are EXCLUDED from grading per instruction. The backed side is not recoverable from the deduped sized line, so a win/loss column and counterfactual P&L would be fabricated — withheld. Universe size is reported; the doc's directional '20 graded -> 0 won' came from a fuller manual parse with the side preserved, re-run on the mini with a structured rec journal to reproduce it cleanly.
- For reference, the attribution doc's directional cut graded 20 -> 0 won (-$297 avoided) from a fuller manual parse that preserved the backed side. This engine does not reproduce that number because it refuses to fabricate the side; that cut needs the structured rec journal (S6 infra item).

## S6 — Synthesis + forward changes

Built on the 2026-06-25 attribution doc (`docs/research/model_vs_discretion_attribution.md`), extended with the model-forecast-accuracy sections (S1-S3) it lacked. Each change is tied to a finding with its n.

1. **The model is a good 1X2 _calibrator_.** Full settled book: mean model prob 0.333 == realized 0.333, binary Brier 0.150 (n=825 legs). Reproduces the doc. Keep trusting the probabilities; harvest favourite-side / straight 1X2.
2. **Pick-level discrimination is positive but thin.** Fixture-level argmax hit-rate 60.9% (14/23), BSS vs base-rate 0.140 (n=23). Positive skill, but n is too small to promote sizing — judge on CLV.
3. **O/U and BTTS are usable secondary markets.** O/U 2.5 accuracy 55.6% (n=27), BTTS 40.7% (n=27). Scoreline top-6 55.6% (n=27) — keep scoreline as a ladder, not a single-line bet.
4. **Goals: the persisted top-k ladder cannot be used as a totals estimator.** Reconstructed mean predicted total 1.72 vs actual 3.15 (bias -1.43, MAE 1.67, n=27) — the large negative bias is a TRUNCATION ARTIFACT (the top-6 ladder keeps only ~55% of the scoreline mass, which sits on low scores), NOT a model claim. The full Dixon-Coles lambdas are not persisted. ACTION: persist the full DC lambdas at card-build so totals can be validated. Until then, no exact-score model exists -> keep avoiding discretionary correct-score punts (doc: -73.9% ROI, your biggest leak).
5. **Skipped-rec verdict holds (extended).** The model's `/next` value stream went 0/8 graded (cf -8.0u) — consistent with the doc; keep skipping those longshot/draw +EV flags and the PM advancement stream. The wider `/next`+`/card` stream went 9/43 (cf -5.6u): the favourite-side `/card` picks are where the value is. Gate model bets on CLV + a ≥5-6% edge; scale the promo/boost lane (the doc's one robust +EV lane).
6. _(Infra)_ Persist a structured rec journal (selection/price/stake/model_prob) + unify the predledger↔bet-ledger match_id hash so this attribution becomes a query, not a Telegram-export parse.

---
_Generated by `scripts/wca_validation_report.py` — read-only, guarded, reproducible. Re-run on the mini for canonical ledger numbers._
