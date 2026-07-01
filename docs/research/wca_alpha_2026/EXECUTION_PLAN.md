# Execution plan — two directions (2026-06-30)

Synthesised from the 11 docs created 2026-06-29 (microstructure recon, phase-2
program, improvement plan, 48h status, model+rec validation, market-intelligence
design/deliverables, arb methodology, betbuilder, data-archival, pm-snapshot).

## The one verdict that governs everything
The model is a **good 1X2 calibrator, a bad longshot/value-flag selector**. The
**dependable edge is execution/cost + calibration discipline, not prediction**
(exchange ~0.7% overround vs sportsbook ~6.8%; FLB 18-52% on longshots; every active
timing/prediction edge tested came back NULL). **No model/edge change ships unless it
improves a pre-registered out-of-sample metric** (held-out CLV + calibration, walk-forward,
placebo null). Stop the correct-score bleed (-73.9%); the >=2% edge flag is a sanity
flag, not a stake signal (CLV -11.4% on flagged legs).

## Phase 0 — the cross-cutting unlock (do first; both directions depend on it)
**Restore true closing-line capture.** See `CLOSING_LINE_DIAGNOSIS.md`: capture has been
dead ~12 days (mini daemon since 06-23; cloud raw dumps since 06-18). Fix = restart mini
`snapshotd` + make odds capture cloud-durable (DB-less JSONL mirror like PM) + idempotent
ingest + fail-loud workflow. This unblocks the walk-forward CLV harness — the gate for
every speculative item below.

## Direction A — Polymarket betting & forecasting
PM/Gamma is FREE to poll; the asset is the price trajectory. Edge is measurement +
a few honest spots, NOT the efficient deep outright market (model "edges" there are
model error — confirmed live: model 18.8% vs PM 7% on Brazil-to-win).
1. **Grade the PM history** already accruing (`pm_price_history.jsonl`, twice-hourly cloud).
   Wire resolved outcomes so convergence/CLV on advancement become gradeable as ties
   settle. Canonical branches: `feat/pm-price-history-outright-edge`, `feat/pm-1x2-snapshotter`
   (makes PM a rate source for the benchmark). Needs >=40 resolved ties before any PM combo capital.
2. **Market-anchor knockout advancement** (`advancement.py` has no market term for knockouts
   -> spurious-longshot surface). Anchor to a DIFFERENT venue (Betfair To Qualify) or the
   1X2 chain, never the PM price you are trying to beat.
3. **Retire the losers, keep the calibrated core**: gate/retire PM exact-score (-19%); keep
   PM moneyline small (n=4, noise). `harden/xg-totals` (xG fix) sharpens totals/BTTS where PM offers them.

## Direction B — Microstructure-tab edges
The measured edge is structural (venue routing/cost), not predictive.
1. **Land the market-intelligence DB** (`feat/market-microstructure`: `intel` package,
   `market_snapshots`/`market_metrics`, "F // Market Intelligence" tab, `/arb` v1). The
   accumulating history IS the asset — study price formation, venue lead-lag, efficiency, CLV.
2. **Venue selector + line-shopping + negative-avoidance** — highest-certainty, zero-new-data
   win: always route to the lowest-margin venue; never bet into 14% / 36-52%-longshot lines.
   Reuse `venues.py`/`devig.py`/`arb.effective_back`.
3. **Whole-book correlated-exposure guard** (`feat/correlated-exposure-model`) — size
   1X2 + O/U(+BTTS) on a fixture as ONE bucket; they are correlated re-expressions of the
   same goal supply, not three edges.
4. **Gated on creds:** direct Betfair/Smarkets DEPTH capture -> unblocks the execution engine
   (SOR, liquidity sizing, slippage, real arb). Until then, hard-cap to tiny stakes.

## Sequencing
P0 closing-line capture -> walk-forward CLV harness green -> then (A2 advancement anchor +
B1 market-intel DB land) in parallel -> A1 PM grading + B2 venue selector -> gated depth/creds work.
Each step ships only if it clears its pre-registered OOS gate.
