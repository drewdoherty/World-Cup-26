# Modeling & stats status — 48h review (2026-06-27)

Scope: where the last ~48h of statistical/model work stands, what is *measured*
vs *asserted*, and what is genuinely improved vs still open. Every claim is
tied to data; small samples are flagged. Numbers come from the new benchmark
harness (`src/wca/bench`, run on a copy of the dev-box ledger — which forks from
the canonical mini ledger, so treat absolutes as illustrative).

## 1. Headline

The model is a **good 1X2 calibrator** and a **bad bet-selector at the edges**.
This 48h's work did **not** find new alpha — it built the *measurement* that
proves the alpha claims were variance, and reproduced that result independently.
The dependable edge remains **execution/cost** (venue routing), not prediction.

## 2. Model state by component

### 2.1 Team model (Elo + Dixon-Coles 1X2) — calibrated, no edge over market
- Argmax hit-rate **66.7%** (26/39 fixtures, Wilson 95% CI 51.0–79.4%).
- Brier **0.482 (model)** vs **0.479 (build-time market)** → skill vs market
  **−0.7%**. The model tracks the market; it does not beat it on this sample.
- ECE (legs, 10-bin) **0.083**; reliability table is monotone and close to the
  diagonal (e.g. 0.4–0.6 bin: mean pred 0.506 → realized 0.600, n=15).
- Verdict: **calibration is real and stable**; discrimination over the market
  is not yet demonstrable at n=39. This matches the prior validation report
  (`docs/research/model_and_rec_validation_report.md`, Brier 0.150 binary,
  per-leg CLV −0.020 on n=825).

### 2.2 CLV — the edge claims fail the closing-line test
Walk-forward CLV (model fair prob vs consensus closing fair prob,
`clv = p_close/p_model − 1`), 51 fixtures / 153 legs:
- Overall CLV **−1.2%**, beat-close **43.8%** (< 50% = no edge).
- **+EV-flagged legs (build-time edge ≥ 2%): CLV −11.4%, beat-close 14.3%
  (n=42).** When the model thinks it has an edge, the line moves *against* it.
- Monotone and damning by edge bucket:

  | build-time edge | n | CLV mean | beat-close |
  |---|---:|---:|---:|
  | [−1.00,−0.02) | 34 | **+4.9%** | 82.4% |
  | [−0.02,0.00) | 33 | **+6.6%** | 69.7% |
  | [0.00,+0.02) | 44 | −2.1% | 22.7% |
  | [+0.02,+0.05) | 36 | **−9.6%** | 13.9% |
  | [+0.05,+0.10) | 6 | **−21.9%** | 16.7% |

  This independently reproduces the attribution finding that ≥2% edge flags are
  CLV-negative. Note the partly-mechanical component (positive edge means
  p_model > p_market, so a market that doesn't move yields negative CLV by
  construction) — but the *direction is the point*: the model's confidence does
  not predict favourable line movement, i.e. no anticipatory edge.

### 2.3 Realized ledger — variance, plus the one real leak
- Overall ROI **+39.9%** (£172.32 on £431.76, 62 decided) — this is the
  "+39.9% is variance" headline; concentrated in 1X2 (+65.6%) and a couple of
  lucky books (bet365 +305%, n=2).
- **Correct-score punts: −73.9% ROI (£−28.37 / £38.37, 11 bets, 9% win).** This
  is the consistent, model-independent leak — the user's own longshot habit —
  and it is the clearest action item.
- By family: goalscorer +113.8% (n=3, noise), acca/betbuilder +27.0% (n=12),
  shots_on_target +33% (n=2). All sub-sample noise except the correct-score sink.

### 2.4 Player-level model (Phase 2) — built, not adopted, not merged
- `players.db` + scorer/props/events live on the Phase-2 branch (not on `main`).
  Anytime/first/brace goalscorer pricing is calibrated (`models/scorers.py`,
  `models/props.py`).
- Backtest verdict carried forward: **better discrimination, worse calibration
  → do not adopt raw player probs; shrink first.** No 2026 event feed exists, so
  player rates are WC2018+2022 only; props run off the `data/players.json`
  override store. **Open**: validate shrinkage out-of-sample before any sizing.

## 3. Genuinely improved (this PR) vs still open

**Improved / new (measured):**
- A repeatable **benchmark harness** that scores generated predictions and the
  placed ledger against actual outcomes — calibration, hit-rate, walk-forward
  CLV, ROI — broken down by market / venue / edge bucket. It runs today off
  legacy sources and auto-upgrades to the #71 parquet archive as it fills.
- The edge→CLV monotonic decay is now a **reproducible number**, not an anecdote.
- **Bet-builder market models** (team totals, player SoT/fouls/bookings) added
  on top of the existing prop engines.

**Still open:**
- **No true closing line at the instant of kickoff** for many fixtures (capture
  cadence); the harness uses the latest snapshot ≤ kickoff as a proxy. #1 data
  fix remains tightening close capture (see microstructure recon).
- **Sample is thin** (n≈39 settled fixtures with results; 25 bets with CLV).
  Every model-quality number has wide CIs; nothing here is sizing-grade yet.
- **Player props need shrinkage validation** and a 2026 rate source.
- **No sportsbook odds feed** for SoT/cards/corners/fouls → bet-builder markets
  are model-fair-odds only; EV can be computed only where a price is supplied.

## 4. What the data says to do next
1. Stop the correct-score bleed (measured −73.9%); it is not model-driven.
2. Do not stake the model's ≥2% "edge" flags as edges — they are CLV-negative.
   Treat the model as a calibrator/odds-sanity tool, route to best price.
3. Drive any model change through the benchmark with train/test discipline
   (see `docs/research/improvement_plan.md`); prioritise by out-of-sample CLV,
   not in-sample fit.
