<!-- Generated 2026-06-26 by the wca-phase2-research-program multi-agent workflow (20 agents: 5 grounding readers, 7 research streams, 7 adversarial verifiers, 1 synthesis). Every edge claim was adversarially checked for leakage / overfit / data-availability / sample-size; killed candidates were dropped. Companion to docs/research/model_vs_discretion_attribution.md and model_and_rec_validation_report.md. Findings are RESEARCH (ranked evidence), not implemented code. -->

# World Cup Alpha — Phase-2 Research Synthesis: Where Marginal Engineering Effort Buys the Most Bankroll Growth

## Executive Summary

**Lead with the binding constraints, because they govern every recommendation below:**

1. **No 2026 StatsBomb event feed.** There is no live per-player shot/SoT/save/foul/lineup pipeline for the 2026 squads. Every "rich" prop feature (player shots, GK saves, fouls, possession, scorer npxG shares) is **un-buildable live** and is dropped. `squads.json` has only ~3 populated teams, so even goalscorer side-attribution is broken.

2. **n is fatally thin (~25–50 settled-with-CLV).** Every P&L and ROI figure in the source research is below decision-grade (pre-registration target is ~200 for CLV, ~2,500–4,000 for ROI). Per-market subsets are n=1 to n=19. **CLV and calibration are the only honestly-measurable edges. P&L is not.** No candidate in this report carries a *confirmed* historical bankroll edge. Several quoted ledger figures were not even reproducible from the messy free-text market labels (e.g. 1X2-cluster reproduces as n=17 not n=19; PM moneyline as n=4/+16.8% not n=5/+14.5%). Direction holds; precise numbers do not.

3. **The model is a good 1X2 calibrator and a bad longshot selector.** Value-flag picks (model>market) lose; per-leg CLV on longshots ≈ −0.02; the user's own correct-score punts ran −73.9%. This single fact **kills** anytime-scorer, correct-score, and SGM/BYB markets — building a naive edge column there would actively *surface losing bets*.

4. **Odds capture is the real bottleneck, not the model.** Across all 457 multi-snapshots the only captured markets are **h2h (1X2), totals (O/U), and h2h_lay**. No spreads (AH), no captured BTTS/corners/cards/prop prices anywhere. The only historical price backtest set (WC2022 closing) is **h2h-only, n=64**. Exchange liquidity is narrower than assumed: Betfair Exchange fetches MATCH_ODDS (1X2) only; among exchanges only Matchbook (thin) carries totals. Odds sources are effectively **Betfair-creds-gated 1X2 + Polymarket floor**.

5. **The biggest available "alpha" is correlation honesty, not new markets.** Totals O/U and BTTS are mechanically derived from the *same* DC goal-supply lambdas that drive blended 1X2. They are correlated re-expressions of the *one* edge we have, not independent alpha. Any whole-book sizing that treats them as diversified will over-concentrate risk on a single fixture.

**Consequence for the roadmap:** the first wave is deliberately the high-certainty, low-effort, leakage-free wins that need no new data — a correlated-exposure model over the existing scoreline distribution, a "track-everything" calibration dashboard, a walk-forward CLV harness on cached snapshots, multiple-choice forecast selection, and reviving the already-built (currently dormant) news/injury daemon. Speculative, data-hungry work (paid 2026 event feed, Asian Handicap ingestion, new prop models) is **gated** on the walk-forward harness first proving signal.

---

## Section 1 — Ranked NEW PREDICTIVE FEATURES (with 2026-availability labels)

| # | Feature | 2026 availability | Evidence | Why it ranks here |
|---|---------|-------------------|----------|-------------------|
| 1 | **News/injury/withdrawal signal** (already-built daemon: `src/wca/news.py`, `scripts/wca_newsd.py`) | **Available now** (free Google News RSS + BBC/Guardian/ESPN/Sky; Telegram creds set) | Daemon in `deploy/macmini/install.sh` L38 but `pgrep newsd` empty (dormant). Its one ~20h burst (Jun 11–12) ingested 5,824 items, pushed 69 alerts, correctly surfaced Endo/Havertz/Timber/Werner/Gilmour at score 17–18. | Structural, not hindsight-fit. Slow injury news reprices over *hours* — matches our latency. Near-zero build cost (restart a supervised daemon). |
| 2 | **Market-anchor prior for KNOCKOUT advancement fixtures** | **Partial** (PM advancement prices fetchable now; Betfair "To Qualify" creds-gated) | `src/wca/advancement.py` L243 `mkt = None if knockout else market_lookup.get((a,b))` — knockout fixtures fall to pure Elo+DC with no market term. This is the un-anchored model-error surface behind the −6.9% ≥20%-edge longshot cohort. | A genuine feature gap, but loss-avoidance (compresses spurious longshot edges), not new alpha. Live advancement n=0 (both bets still open). |
| 3 | **Calibrated goal-supply lambdas as a portfolio input** (already computed) | **Available now** | Reconciled DC matrix → `over_under_from_matrix` (scores.py:270), `btts_from_matrix` (:284), `top_scorelines_from_matrix` (:294). | Not a *new* signal — the value is using the full scoreline distribution for correlation-aware sizing (see Section 5 wave 1), not re-betting it as independent markets. |
| 4 | Per-team corner/card tendency + referee aggression priors | **Needs data we lack** | `props.py` CardsModel uses aggression=1.0 default (no referee feed); CornersModel is base-rate + weak xG nudge (corners–goals r=0.02, corners–xG r=0.15). | Orthogonal to the goal-supply edge that transfers; no referee/lineup feed. Research-only. |
| 5 | Per-player npxG / shot / SoT / save rates for 2026 | **Needs data we lack (no 2026 event feed)** | `props_players.csv` = 1096 rows, WC2018+2022 only; `players.json` overrides 238 players; backtest concluded "worse calibration → shrink first". | Un-buildable live; adverse-selected (longshot zone). Deferred and gated. |

---

## Section 2 — Ranked BETTING MARKETS (calibration-transfer rationale; bet vs track-only)

**BET (data captured + calibration transfers + some exchange/PM liquidity):**

| # | Market | Calibration transfer | Status |
|---|--------|----------------------|--------|
| 1 | **1X2 / Match Odds** (harden, don't expand) | The model's genuine strength; the only market with full captured price history (h2h=2547 rows/snap), historical closing backtest (WC2022 n=64), and real exchange depth (Betfair MATCH_ODDS + Smarkets + Matchbook). | **BET.** Gains come from *subtraction* — stop staking value-flag longshots. Defend-not-grow. CLV evidence below decision-grade. |
| 2 | **Totals O/U 2.5** (line-shop vs soft books) | IS the goal-supply core — `over_under_from_matrix` reconciled to the same blended 1X2. | **BET (cautiously).** Prices already captured (416 rows/snap, 370 on 2.5 line; soft books + thin Matchbook). Card displays O/U but never edge-prices it. **Correlated with 1X2 — size as one exposure, do not treat P&L as a second edge.** |

**TRACK-ONLY / model can't beat (or no data, or adversely-selected):**

| Market | Why track-only |
|--------|----------------|
| **Asian Handicap** | Best *theoretical* transfer (derives analytically from the matrix, no new params, tightest exchange margins) **but no captured AH prices and no AH backtest**. Needs an OddsAPI `spreads` pull + Betfair ASIAN_HANDICAP wiring + a ~20-line `ah_from_matrix()`. High-value-IF-data; gated behind the harness. |
| **BTTS** | `btts_from_matrix` exists but no captured prices; near-pure redundant re-expression of 1X2/totals. Strictly dominated by Totals. |
| **Anytime/First Goalscorer** | Documented worst zone (longshot disease); squad attribution broken (3 teams); npxG shares missing for most 2026 players. Building an edge column surfaces losing bets. **Kill until squads filled + shrinkage validated.** |
| **Correct Score / scorecast / HT-FT** | Canonical longshot; ledger correct/exact-score net negative; user's own punts −73.9%. **Do not build for +EV.** |
| **Corners / Cards** | Goal calibration does not transfer (orthogonal drivers); no prices, no referee/tendency feeds. |
| **Shots/SoT/saves/fouls/possession/throw-ins** | No model, no prices, no 2026 rates — entire new subsystem. |
| **SGM / BYB / correlated accas** | No correlation/copula pricer; naive independence multiplication is exactly what the book exploits. |
| **PM exact-score** | Settled n=9, ROI ≈ −19% (thin but prior-consistent). **Retire via min-price/min-edge gate**, not an n=9 −EV proof. |
| **PM moneyline** | Only PM bucket in the black (n=4, +16.8% — noise). Keep small. |
| **Kalshi / Manifold** | Kalshi: binary-only, eligibility unverified, weakest-edge surface, L-effort → defer to read-only probe. Manifold: play-money, no real-bankroll path → kill. |

---

## Section 3 — Ranked IMPLEMENTATION TASKS (the build list)

1. **Revive + supervise the news/injury daemon** (`scripts/wca_newsd.py` on the mini). Effort S. Restart dormant daemon, keep alive, treat alerts as re-price prompts that still pass the +EV gate.
2. **Correlated-exposure model over the existing scoreline distribution.** Effort S–M. Make `src/wca/exposure.py` treat 1X2 + O/U (+ BTTS if ever added) on the same fixture as one correlated bucket against the hard cash floor. Pure leverage of data we already have; prevents over-concentration. Highest-certainty leakage-free win.
3. **Analytics "track-everything" + calibration dashboard.** Effort S–M. Persist model 1X2/O/U probs at card build, log realized outcomes, surface reliability/calibration curves and CLV-by-bucket. The only honestly-measurable edge surface at this n.
4. **Walk-forward CLV harness on cached snapshots.** Effort M. Replay the 533 h2h + 416 totals/snap historical snapshots forward-only (lagged prices, no look-ahead) to measure CLV vs later/closing capture. This is the gate that every speculative item below must pass.
5. **Multiple-choice forecast selection** (pick-best-outcome framing) wired into the recommendation filter. Effort S–M. Reduces the value-flag longshot leakage by selecting among outcomes rather than flagging every model>market deviation.
6. **Totals O/U 2.5 edge column + min-edge gate + quarter-Kelly**, mirroring the 1X2 path in `card.build_card`. Effort S. Ship *after* the correlation model so it can't double-stake.
7. **Harden 1X2 CLV-gated staking** — wire the bets-by-bucket gate to the recommendation filter; drop value-flag longshots. Effort S. **Walk-forward only — do not tune the gate on the n=25 realized ledger (in-sample hindsight).**
8. **Market-anchor knockout advancement fixtures** (`advancement.py`). Effort M. Anchor to a *different* venue (Betfair To Qualify) or the upstream 1X2 chain — never the PM price you are trying to beat. Gated on PM advancement liquidity.
9. **(Gated) Asian Handicap**: add OddsAPI `spreads` pull + Betfair ASIAN_HANDICAP wiring + `ah_from_matrix()`. Effort M. Only after the harness shows the goal model earns CLV on captured markets.
10. **(Deferred) Kalshi read-only price probe** after eligibility verification. Effort L for full integration — defer.

---

## Section 4 — Expected-Improvement Analysis (honest; no invented P&L)

| Enhancement | Mechanism of edge | Expected improvement (honest) | Effort | Confidence |
|-------------|-------------------|-------------------------------|--------|------------|
| Revive news daemon | Catch slow injury/withdrawal repricing over hours before market fully adjusts | Qualitative: demonstrably surfaced ~6 real withdrawals in one burst. **Realized P&L = 0 (fired into dormant period).** No quantified edge — re-price prompt, not auto-bet. | S | Med (signal recall strong; P&L unproven) |
| Correlated-exposure model | Stop double-staking 1X2+O/U on one fixture; enforce true whole-book hard floor | Risk reduction (variance/draw-down), not new return. Prevents over-stating diversification. No P&L claim. | S–M | High (deterministic, no fit) |
| Calibration dashboard | Make CLV/calibration — the only measurable edge at this n — visible and decision-usable | Measurement, not edge. Enables every downstream gate. | S–M | High |
| Walk-forward CLV harness | Forward-only replay on cached snapshots to measure CLV without look-ahead | Provides the *only* honest validation channel. No edge itself; unlocks/blocks all speculative work. | M | High (process), N/A (P&L) |
| Multiple-choice forecast selection | Select best outcome rather than flag every model>market deviation → fewer adverse longshots | Loss avoidance via reduced value-flag leakage (value-flags lose, ≈−0.02/leg). Magnitude unmeasured (n thin). | S–M | Med |
| Totals O/U 2.5 edge column | Soft-book O/U 2.5 stickier/laxer than 1X2; line-shop the calibrated goal model | "Capture soft-book mispricing," **not** "beat the closing total" (no historical totals closes exist). CLV unmeasured (n=1 settled). | S | Low (no totals backtest) |
| Harden 1X2 CLV-gating | Prune value-flag longshots the model selects badly | Defends bankroll via subtraction. 1X2-cluster n=17 avgCLV −0.0079 (noise). No new alpha. | S | Med-Low (in-sample overfit risk on n=25) |
| Knockout advancement anchor | Compress un-anchored Elo+DC knockout edges toward market prior | Loss avoidance on the −6.9% ≥20%-edge cohort (figure from memory, not reproduced; live n=0). | M | Low-Med |
| Asian Handicap (gated) | Tightest exchange margins; analytic transfer from same matrix | Best *theoretical* CLV shot, but zero captured AH data → no quantification possible. | M | N/A (data-pending) |
| Kalshi (deferred) | Independent venue / cross-market anchor | Unproven; binary-only surface is our weakest edge. | L | Low |

---

## Section 5 — Incremental Roadmap (prioritised by long-term bankroll growth PER UNIT of engineering effort)

**Wave 1 — High-certainty, low-effort, leakage-free wins (no new data):**
- Correlated-exposure model from the existing DC scoreline distribution (`exposure.py`) — treat 1X2 + O/U on a fixture as one bucket against the hard floor.
- Analytics "track-everything" + calibration dashboard — make CLV/calibration the decision surface.
- Walk-forward CLV harness on cached snapshots — the gate for everything speculative.
- Multiple-choice forecast selection — cut value-flag longshot leakage.
- Revive + supervise the news/injury daemon (already built, near-zero cost).

*Rationale: these either reuse data we already have or restart code we already wrote; none can leak (forward-only / deterministic), and they create the measurement + risk-control scaffolding the rest depends on.*

**Wave 2 — Small-effort edge expressions gated on Wave 1 scaffolding:**
- Totals O/U 2.5 edge column + min-edge gate + quarter-Kelly (ships *after* the correlation model so it can't double-stake).
- Harden 1X2 CLV-gated staking by subtraction (walk-forward only, never fit to the realised ledger).
- Retire/gate PM exact-score via min-price/min-edge floor; keep PM moneyline small.

*Rationale: real bankroll touch-points, but each is correlated with the existing edge or operates on thin n — only safe once correlation sizing + the harness exist.*

**Wave 3 — Loss-avoidance feature work, partial data:**
- Market-anchor knockout advancement fixtures (anchor to a different venue / the 1X2 chain).

*Rationale: M-effort, live n=0, depends on PM advancement liquidity — clear value but unmeasured.*

**Wave 4 — Speculative / data-hungry, hard-gated on the harness proving signal:**
- Asian Handicap ingestion + `ah_from_matrix()` (needs new OddsAPI spreads pull + Betfair wiring).
- Kalshi read-only probe after eligibility verification.
- Paid 2026 event feed + new prop/scorer models (only if/when squads are populated and shrinkage validated).

*Rationale: every item here needs data we lack and addresses markets where our calibration advantage is weakest or absent. None proceeds until the walk-forward harness demonstrates the goal model actually earns CLV on the markets we already capture.*