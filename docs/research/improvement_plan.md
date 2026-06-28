# Robust improvement plan — benchmark-driven, out-of-sample first (2026-06-27)

Principle: **no model change ships unless it improves a measured out-of-sample
metric.** In-sample fit is never evidence. The benchmark harness (`wca.bench`)
is the gate; everything below is prioritised by *measured* edge, not intuition.

## Why this ordering

The 48h benchmark says the bottleneck is **measurement + selection discipline +
sample size**, not the point model:
- 1X2 calibration is already good (ECE 0.083); Brier skill vs market −0.7%.
- The model's ≥2% "edge" flags are **CLV-negative (−11.4%, beat-close 14.3%)**.
- The only consistent realized leak is **correct-score punts (−73.9% ROI)**.

So the highest-value moves are *not* a fancier model. They are: stop the leak,
stop staking false edges, grow the measured sample, and only then tune.

## Wave 0 — stop the bleeding (no model change, do now)
1. **Retire discretionary correct-score / longshot punts.** Measured −73.9%
   (n=11) and the model is a bad longshot selector (0-for-8 graded flags). This
   is pure loss reduction, consistent with the user's "likely-PnL, no minnows"
   rule.
2. **Demote the ≥2% edge flag from a stake signal to a sanity flag.** Route to
   best price; do not size off it. (Re-evaluate only if Wave 2 shows it predicts
   CLV on a larger sample.)

## Wave 1 — make the measurement trustworthy (data, not model)
3. **Tighten closing-line capture** to the kickoff instant for every fixture.
   Today the harness proxies "closing" with the latest snapshot ≤ kickoff; CLV
   is only as honest as that capture. This is the #1 data fix (microstructure
   recon agrees).
4. **Run the benchmark on a schedule** against the canonical mini ledger (not the
   forked dev copy) and the #71 parquet archive as it fills. Persist
   `reports/benchmark_latest.json` over time so metrics get *trend lines* and CIs
   tighten. Target: n≥150 settled fixtures before any metric is called
   sizing-grade.

## Wave 2 — disciplined model work (only via the harness)
Each item is a **train/test experiment**, scored on held-out CLV + calibration,
with an explicit overfitting guard.

5. **Calibration shrinkage layer.** Fit an isotonic / Platt map on a *training*
   split of settled fixtures, evaluate ECE + log-loss on a *held-out* split and
   on walk-forward CLV. Adopt only if held-out ECE drops without hurting CLV.
   This is also the gate for the Phase-2 player props ("worse calibration →
   shrink first").
6. **Re-test the blend weights** (Elo/DC/market) on the *new* settled WC2026
   sample using the existing LOTO backtests as the prior. Keep status-quo
   (0.25/0.25/0.50) unless held-out log-loss improves beyond bootstrap noise.
7. **Player-prop shrinkage validation.** Shrink WC18+22 player shares toward the
   team/positional mean; score anytime-scorer Brier/log-loss on held-out
   fixtures. Adopt a shrinkage α only if it beats both raw and uniform OOS.

## Overfitting guards (apply to every Wave-2 item)
- **Pre-register** the metric and the train/test split before looking at results.
- **Walk-forward only**: train strictly before each test fixture's kickoff; no
  look-ahead in odds or results.
- **Report CIs** (Wilson / bootstrap) and a **placebo/label-shuffle null**; a
  change must beat the null, not just the point estimate.
- **Prefer fewer parameters**; reject a change whose OOS gain is within noise of
  in-sample gain (classic overfit signature).
- **Decision rule**: ship iff held-out CLV is non-negative *and* held-out
  calibration (ECE / log-loss) improves, both outside their CIs.

## Explicitly deprioritised (measured low/negative value)
- Timing/early-line prediction edges (all came back NULL in recon).
- New exotic markets before a sportsbook odds feed exists (can't be EV'd).
- Sizing off model edge flags (CLV-negative until proven otherwise).

## Definition of done for the loop
A change is "done" when: (a) it was run as a pre-registered walk-forward
experiment in `wca.bench`, (b) it improved held-out CLV and calibration outside
CIs, (c) the benchmark report reflects the new numbers with sample sizes, and
(d) it did not increase exposure to the known correct-score/longshot failure
mode.
