# Proposal — Redo "Under the Hood" for the honest 2026-06-21 state

**Status:** draft · **Date:** 2026-06-21 · **Owner:** desk

## 1. Summary

The current Under the Hood page (`site/architecture.html` +
`site/architecture.js`) is a strong static system map, but it now reads too
confidently. It presents several pieces as deployed or proven when the project
state is more mixed: some components are shipped, some are designed but not wired
into `build_card`, and the real-money edge is not yet validated.

Redo the section as an **honest model-room page**: keep the five-stage pipeline,
money flow, IMPROVE map and kill-rule, but rewrite them around evidence status,
data provenance and known gaps. The page should explain what exists today
without implying that the betting edge has been proven.

## 2. Ground Truth To Reflect

- **Blend prior:** deployed sportsbook card prior should be shown as
  `0.25 Elo / 0.25 Dixon-Coles / 0.50 market`.
- **Blend fit:** WC2022 holdout fit was approximately
  `0.00 Elo / 0.32 Dixon-Coles / 0.68 market` on `n=64`; the project kept the
  more conservative prior rather than chasing the tiny sample.
- **Dixon-Coles:** deployed intent is an 8-year half-life. Call out that this is
  a conservative international-football decay choice, not a discovered edge.
- **Polymarket advancement sim:** current advancement prices are **unanchored**:
  the sim has no market term, so apparent Polymarket edges are exploratory and
  not proven.
- **CLV-gated Kelly ladder:** designed, but **not wired into `build_card`**.
  Current card sizing remains rung 0; observed CLV is flat-to-negative so there
  is no basis for promotion.
- **Closing odds:** closing-odds capture is only about **42%**, so CLV reporting
  is incomplete and can be biased by missing closes.
- **Backtest:** no validated end-to-end betting backtest exists yet. Do not
  frame expected ROI or edge as established.
- **Execution:** a new **dev-conductor** bot fans tasks out to headless agents.
  This is engineering throughput infrastructure, not model validation.

## 3. Proposed Page Structure

### A. Opening: one honest mental model

Replace the current heroic prose with a compact statement:

> WCA combines historical team models with market prices, line-shops candidate
> bets, records execution manually, and uses CLV as the leading validation
> signal. The system is not yet proven to beat the market.

Then show the current pipeline:

`clean data -> Elo/DC + market anchor -> edge candidates -> human execution -> ledger/CLV -> validation backlog`

### B. Latest Findings & Gaps honesty box

Add a first-screen box before the stage cards:

| Area | Latest honest state |
|------|---------------------|
| Blend | Prior is `0.25/0.25/0.50`; WC2022 fit preferred market-heavy `0.00/0.32/0.68`, but sample is only 64 matches. |
| DC | 8-year half-life in use; still needs tournament-specific validation. |
| Sportsbook edge | Not validated. CLV is flat-to-negative so far. |
| CLV data | Closing odds captured for ~42% of bets; missing closes limit conclusions. |
| Kelly ladder | Designed but not wired into `build_card`; card remains rung 0. |
| Polymarket advancement | Unanchored sim with no market term; edges are research leads only. |
| Backtest | No validated end-to-end backtest yet. |
| Dev-conductor | Ships task fanout to headless agents; improves build velocity only. |

### C. Keep the 5-stage pipeline, but add status labels

Retain the current five stages because the mental model is useful:

1. **Ingestion** — shipped / partial / planned by feed.
2. **Models** — shipped, but explicitly split model estimate vs market anchor.
3. **Decision** — shipped line-shopping and filters; Kelly ladder shown as
   designed-but-not-wired.
4. **Execution** — human placement, bot-assisted ledger, dev-conductor for task
   fanout.
5. **Feedback** — ledger and CLV reporting, with missing-close coverage shown.

Each card should have a small status chip:

- `LIVE` — runs in the current workflow.
- `SHIPPED, NOT WIRED` — implemented or designed but not used by `build_card`.
- `RESEARCH` — exploratory and not approved for edge claims.
- `PLANNED` — not built.

This avoids burying the most important distinction in prose.

## 4. Content Changes By Existing Section

### Five-stage pipeline

- Update the blend card to `0.25 Elo / 0.25 DC / 0.50 market`.
- Include the WC2022 fitted result as evidence, not deployment:
  `fit ~= 0.00 / 0.32 / 0.68, n=64; prior kept`.
- Mark Polymarket advancement as `RESEARCH` and explicitly say **no market
  anchoring yet**.
- Mark the CLV ladder as `SHIPPED, NOT WIRED` or `DESIGNED, NOT WIRED` until
  `build_card` actually consumes it.
- Add a feedback card for closing-odds coverage: `~42% captured`.
- Add a validation card: `no validated end-to-end backtest`.

### IMPROVE map

Split the current improvement list into three lanes:

| Lane | Meaning |
|------|---------|
| Shipped live | Currently affects generated cards or published site data. |
| Shipped, not wired | Code/design exists but does not affect `build_card` or the decision loop. |
| Planned / research | Not production decision logic. |

Recommended top items:

- Wire CLV ladder into `build_card`, including rung state, demotion, and a clear
  audit line on each generated card.
- Raise closing-odds capture from ~42% to near-complete before making CLV claims.
- Build a validated backtest harness with frozen historical odds and no
  lookahead.
- Add a market term to the Polymarket advancement sim before presenting
  advancement edges as actionable.
- Add monitoring for model-vs-market calibration, CLV by venue, and missing
  closes.

### Money flow

Keep the pool -> stake -> settle -> CLV loop, but make the current state clear:

- Sportsbook card uses rung 0 sizing today.
- Ladder promotion is blocked until wiring plus sufficient positive CLV.
- Polymarket advancement bets should be labelled research/exploratory unless
  they come from a market-anchored model.
- Free/promotional bets should remain separated from cash-risk accounting.

### Kill-rule

Rewrite the kill-rule to be operational and measurable:

> Real-money model betting remains at rung 0 and should pause or shrink if
> settled-with-close CLV is negative after a sufficient sample. Because closing
> odds are only captured for ~42% of bets today, the first priority is improving
> close coverage before treating the kill-rule as statistically meaningful.

Do not imply the system has already demonstrated enough CLV evidence to scale.

## 5. Data-Sources-By-Pool Diagram

Add a compact diagram that shows which signals feed each pool:

```text
Historical results
  -> cleaning overlay
  -> Elo + Dixon-Coles
  -> sportsbook 1X2 blend
       + The Odds API market anchor
       + line shopping
       -> human bet card -> ledger -> CLV

Polymarket Gamma prices
  -> site display / price context
  -> advancement research
       + unanchored tournament sim today
       -> exploratory only until market term is added

Promos / offers
  -> offer parser / manual review
  -> isolated ledger tagging
  -> excluded or separated from model-edge validation

Dev-conductor
  -> fans engineering tasks to headless agents
  -> no direct model signal
```

The key UX goal is to prevent readers from assuming every pool uses the same
model confidence or validation standard.

## 6. Copy Rules For The Redo

- Use "candidate edge", "research lead", or "unvalidated signal" unless CLV or
  backtest evidence exists.
- Do not state or imply positive ROI.
- Do not call Polymarket advancement edges proven until the sim includes market
  anchoring and validation.
- Do not describe the CLV ladder as active in card sizing until `build_card`
  actually consumes it.
- Always show sample size beside fitted weights and CLV claims.
- Separate "model shipped" from "decision logic wired".

## 7. Suggested Implementation Shape

No site-code change is proposed in this document, but the eventual redo should
be small and maintainable:

- Keep `site/architecture.html` mostly intact.
- Replace the hardcoded prose/data blocks in `site/architecture.js` with a new
  honest content model.
- Add explicit status chips to cards and improvement items.
- Add one first-screen honesty box and one data-sources-by-pool diagram.
- Prefer generating the factual values from build artifacts later; for the first
  pass, hardcode fewer numbers and include dates/sample sizes.

## 8. Recommendation

Ship the rewrite before adding more visual polish. The most valuable change is
credibility: the page should make clear that WCA has useful infrastructure and a
market-anchored modelling framework, but it has **not yet proven a durable
betting edge**.
