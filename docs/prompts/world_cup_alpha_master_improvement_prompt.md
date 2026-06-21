# World Cup Alpha — Master Improvement Prompt

Use this prompt with a frontier coding/reasoning model to improve the existing
`drewdoherty/World-Cup-26` repository. It is written for a model/agent that can
read the repo, edit code, run tests, browse official/API docs, and operate a
local terminal.

---

## Prompt

You are taking over an existing live-money quantitative betting project:

`World Cup Alpha`

Repository:

```text
/Users/andrewdoherty/Desktop/Coding/World Cup Alpha
https://github.com/drewdoherty/World-Cup-26
```

The objective is not recreational gambling. The objective is to build and
operate a disciplined measurement system for FIFA World Cup betting:

- estimate calibrated probabilities;
- identify market mispricing;
- compare traditional UK sportsbooks, exchanges, and prediction markets;
- size positions conservatively;
- track bets, CLV, calibration, exposure, promotions, and P&L honestly;
- determine whether any repeatable positive expected value exists after vig,
  fees, commissions, spreads, stake limits, and operational errors.

The repo is already a live system. Do **not** assume it is empty. Do **not**
rebuild from scratch unless an audit proves the existing implementation is
wrong. Your first job is to understand what actually exists.

## Non-negotiable rules

1. **Never read, print, commit, or expose `.env` secrets.**
   - `.env` is gitignored and contains real API keys and private keys.
   - If you need to check configuration, inspect `.env.example`, code paths, or
     ask for a redacted confirmation.

2. **No fabricated data.**
   - Every team, fixture, price, squad update, injury, referee, market rule,
     token id, or API capability must come from local data or a cited primary
     source.
   - If a feed is unavailable, say so and make the system degrade visibly.

3. **No silent live-money actions.**
   - Sportsbook bets are manual only.
   - Polymarket/Kalshi orders must remain behind an explicit human confirmation
     gate.
   - Keep dry-run defaults unless the user has explicitly armed live execution.
   - Any live prediction-market order must leave both a trading log row and a
     ledger row; if either is missing, alert and refuse blind retry.

4. **Settlement definitions must match exactly.**
   - 90-minute markets must never be paired with ET/pens-inclusive markets.
   - Match odds / 1X2 / correct score / BTTS / totals usually resolve on 90
     minutes plus stoppage.
   - Tournament winner, group advancement, reach-round markets, and to-qualify
     can include extra time and penalties. Verify per venue.

5. **Currencies must never be summed blindly.**
   - UK sportsbook/exchange values are GBP.
   - Polymarket/Kalshi values are USD.
   - Show per-currency totals unless an FX rate is explicitly supplied and
     recorded.

6. **Optimize for CLV and calibration, not headline picks.**
   - CLV is the primary KPI.
   - Calibration/Brier/log-loss is the model-quality KPI.
   - ROI is lagging and noisy at this sample size.

7. **Run tests and add regression coverage.**
   - Use focused tests for touched modules.
   - Run the full suite before declaring completion unless a real blocker
     prevents it.

8. **Respect the live repo.**
   - Inspect `git status` first.
   - Do not revert unrelated user/live generated changes.
   - Do not commit raw snapshot spam unless the repo policy says to.
   - Preserve Python 3.9 compatibility unless you intentionally update the
     project target and tests.

## Current known system, to be verified locally

Treat this as a starting map, not gospel. Verify against code and tests.

Core files likely to matter:

```text
README.md
TODO.md
docs/architecture/SYSTEM_MAP.md
docs/research/polymarket_v2_spec.md
docs/research/polymarket_depositwallet_spec.md
docs/recon/settlement_rules.md
docs/recon/polymarket.md
src/wca/card.py
src/wca/models/
src/wca/markets/
src/wca/data/theoddsapi.py
src/wca/data/polymarket.py
src/wca/pm/
src/wca/bot/app.py
src/wca/ledger/
src/wca/sitedata.py
src/wca/sync.py
src/wca/advancement.py
src/wca/arb.py
src/wca/news.py
scripts/wca_build_card.py
scripts/wca_snapshotd.py
scripts/wca_event_ev.py
scripts/wca_advancement.py
scripts/wca_pm_probe.py
scripts/wca_pm_propose.py
scripts/wca_pm_approve.py
site/
tests/
```

Known implemented or partially implemented areas:

- Elo + Dixon-Coles + Shin-devig market blend.
- Reconciled scoreline matrix.
- Quarter-Kelly staking with caps.
- Ledger with account/source dimensions.
- Telegram bot with screenshot ingest, confirmations, summaries, scores, PM
  parked orders, and admin gate.
- Static Vercel dashboard with open/closed positions, venue split, charts, and
  tracking page.
- Odds snapshots via The Odds API.
- Polymarket Gamma read client and raw CLOB signer/client.
- Polymarket V2 and DepositWallet/POLY_1271 work.
- Advancement simulator and Polymarket comparison.
- Derivative EV scanner for h2h/totals/BTTS/DNB/alternate totals.
- Arbitrage scanner with settlement-key guards.
- News ingestion for squad/referee/material betting news.
- Matched-betting/offer tracker.

Known major gaps or fragile areas:

- The Telegram Polymarket trading flow is too bolted-on and needs a clean,
  reliable one-shot overhaul.
- The model “under the hood” audit exists, but must be refreshed against the
  current source and made actionable.
- Betfair Exchange API integration is still planned/incomplete.
- Odds API and Polymarket scraping need broader market coverage and cleaner
  storage contracts.
- Advancement/knockout pricing needs market anchoring and stronger settlement
  mapping.
- Match-event markets are where edge may be largest: correct score, totals,
  BTTS, DNB, cards, corners, shots on target, fouls, scorers, player props, and
  correlated bet-builders/accas.
- News feed should log broad events but only ping material trade ideas.
- Promo/offer/punt/model source accounting must remain separate.
- The dashboard/tracking page must update after every ledger reconciliation.

## Required execution approach

Work in phases. Do not skip the audit.

### Phase 0 — Repo audit and discrepancy map

1. Inspect the repo and produce a concise live-state audit:
   - What files/modules exist?
   - Which daemons/scripts are operational?
   - Which claims in `README.md`, `TODO.md`, `docs/architecture/SYSTEM_MAP.md`,
     and current code disagree?
   - Which tests cover the dangerous paths?
   - Which user-facing flows are stale or broken?

2. Produce/update:
   - `docs/research/model_diagnostics.md`
   - `docs/research/venue_correlation.md`
   - `docs/architecture/SYSTEM_MAP.md` if stale
   - `TODO.md` with precise status, owner, and next action

3. The audit must include:
   - model inputs and outputs;
   - how every datapoint reaches card, bot, site, tracking, and ledger;
   - CLV capture path;
   - bankroll ladder;
   - account/source split;
   - promotion extraction;
   - prediction-market order lifecycle;
   - known failure modes.

Acceptance:

- No code behavior changed except docs/TODO unless a severe bug is found.
- Every claim cites a local file path or primary source.

### Phase 1 — Polymarket trading overhaul

Goal: make Polymarket the cleanest, safest, most inspectable flow in the repo.

Do not patch around the current design if it is structurally wrong. Refactor it.

Required design:

1. One canonical Polymarket domain layer:
   - Gamma market discovery and event search.
   - CLOB orderbook/token resolution.
   - Data API positions/activity reconciliation.
   - CLOB trader/order placement.
   - Relayer/deposit-wallet approval state.
   - Settlement-rule metadata.

2. One canonical proposal object:
   - stable id;
   - venue;
   - market id/event id;
   - token id;
   - question;
   - outcome;
   - side;
   - price;
   - shares;
   - notional USD;
   - model probability;
   - edge/EV;
   - liquidity/depth;
   - spread;
   - fee estimate;
   - settlement key;
   - source model/hedge/punt/offer;
   - expiration/kickoff cutoff;
   - current status.

3. Persistent queue:
   - no process-memory-only proposals;
   - survives bot restarts;
   - stale proposals expire automatically;
   - `Y PM-<n>` cannot execute an old match or stale price;
   - before execution, re-fetch live price/orderbook and re-check edge,
     max slippage, liquidity, kickoff cutoff, daily cap, and dry-run/live mode.

4. Telegram UX:
   - one clean message for current PM trade ideas;
   - show exact human meaning of YES/NO;
   - show current exposure and whether the trade is add/hedge/reduce;
   - show price, fair price, edge, liquidity, max loss, max win, and cutoff;
   - use `Y PM-<n>` and `N PM-<n>`;
   - group chats are read-only; admin user only can execute.

5. Ledger/logging:
   - every dry-run and live attempt is logged;
   - every live order response is captured;
   - fills are reconciled from Polymarket activity/positions;
   - site and tracking data regenerate after execution/reconciliation;
   - incomplete logging triggers an admin alert and blocks blind retry.

6. Correct account class:
   - Verify from local docs/code and, if network is available, official
     Polymarket docs/source.
   - Existing notes indicate the trading proxy is a DepositWallet requiring
     CLOB sig type `POLY_1271` / `POLYMARKET_SIG_TYPE=3`, not the older
     Gnosis Safe sig type. Do not trust this blindly; prove it from source.
   - Do not assume `0x4023...` is the trading proxy unless verified; distinguish
     EOA, deposit address, proxy/deposit wallet, relayer, and funder.

7. Tests:
   - proposal expiry;
   - stale-price rejection;
   - admin gate;
   - group read-only behavior;
   - dry-run default;
   - live-mode logging guarantees;
   - token resolver for home/draw/away;
   - exact score / advancement token resolution where supported;
   - fee/slippage guard;
   - restart persistence.

Acceptance:

- A dry-run end-to-end path works:
  market discovery -> proposal -> Telegram message -> `Y PM-n` -> signed dry-run
  -> ledger/log row -> site/tracking refresh.
- A live path remains gated and does not fire in tests.
- No secrets printed.

### Phase 2 — Market-data ingestion upgrade

Goal: capture the widest useful live pricing surface without losing auditability.

Build or repair:

1. The Odds API ingestion:
   - all available World Cup markets relevant to the model:
     `h2h`, `totals`, `alternate_totals`, `btts`, `draw_no_bet`, correct score
     if available, player props if available, cards/corners/SOT/fouls if
     available;
   - raw JSON snapshots;
   - normalized rows with event id, market key, outcome name, point/line,
     bookmaker, price, timestamp, and settlement key;
   - quota-aware cadence;
   - no overwriting good data with empty/truncated files.

2. Betfair Exchange:
   - build a real API client only if credentials/docs support it;
   - use certificate login if required by current Betfair docs;
   - parse market catalogue and market book;
   - map Betfair markets to the same normalized schema;
   - include exchange commission and available size;
   - never treat delayed prices as live without labelling latency.

3. Polymarket:
   - Gamma events/markets scraper;
   - CLOB orderbook scraper;
   - token id resolver;
   - bid/ask/mid/spread/depth;
   - position/activity reconciliation;
   - advancement/outright/match-event market classification;
   - settlement key and fee metadata.

4. Additional sources:
   - official FIFA fixture/results pages where useful;
   - squad/injury/suspension/lineup/referee news from RSS/official sources;
   - weather/venue/rest/travel only when sourceable;
   - football-data.co.uk and StatsBomb for historical event priors;
   - no fake Twitter scraping; use official APIs or reputable feeds only.

Acceptance:

- One normalized market snapshot schema serves scanners, site charts, CLV,
  and arbitrage.
- Every market row has a settlement key or an explicit unhedgeable reason.
- Tests cover malformed feeds and missing fields.

### Phase 3 — Model and strategy audit

Goal: know exactly what the model is good and bad at, then upgrade the highest
EV market classes first.

1. Audit current models:
   - Elo implementation and parameters;
   - Dixon-Coles implementation and time decay;
   - market-devig methods;
   - blend weights;
   - calibration;
   - per-team residuals;
   - host/venue/altitude handling;
   - lineup/injury sensitivity;
   - scoreline reconciliation.

2. Backtest:
   - previous World Cups;
   - Euros/Copa/AFCON if data supports;
   - international qualifiers/friendlies;
   - walk-forward validation;
   - no look-ahead bias;
   - compare against market baseline;
   - report Brier, log loss, calibration bins, ROI simulation, drawdown, CLV
     where closing lines exist.

3. Advancement and knockout:
   - condition on already-played results;
   - anchor group-stage and future-match probabilities to market where possible;
   - distinguish R32/R16/QF/SF/final/winner;
   - include ET/pens semantics;
   - compare against Polymarket and any sportsbook stage markets;
   - surface only edges that survive fee/spread/liquidity/staleness checks.

4. Match-event modelling priority:
   - correct score from reconciled score matrix;
   - totals and alternate totals;
   - BTTS;
   - DNB/double chance where model-native;
   - corners;
   - cards;
   - shots on target;
   - fouls;
   - goalscorers/player props;
   - correlated bet-builders/accas.

5. Event-model data:
   - derive historical priors from football-data.co.uk and StatsBomb where
     licensed and sourceable;
   - use empirical-Bayes shrinkage;
   - keep missing values as missing, not zero;
   - document every mapping decision.

Acceptance:

- Produce `docs/research/model_diagnostics.md`.
- Produce `docs/research/event_model_plan.md` or update existing docs.
- No model parameter is changed live without an audit note and test/backtest.

### Phase 4 — EV, arbitrage, promotions, and accas

Goal: maximize expected value while keeping the accounting honest.

Build/repair scanners that produce a single ranked opportunity feed:

1. Sources:
   - UK books;
   - exchanges;
   - Polymarket;
   - Kalshi if available;
   - promotional boosts/free bets/early payout;
   - user-entered punts or manual opportunities.

2. Market classes:
   - 1X2;
   - correct score;
   - totals/alternate totals;
   - BTTS;
   - DNB/double chance;
   - advancement/to qualify/outrights;
   - cards/corners/SOT/fouls;
   - goalscorers;
   - same-game bet builders;
   - accas/parlays.

3. Ranking:
   - model edge;
   - market-implied edge;
   - fee/commission/spread;
   - liquidity and stake limits;
   - settlement confidence;
   - correlation with existing exposure;
   - CLV plausibility;
   - time to kickoff;
   - promo extraction value.

4. Accas/parlays:
   - do not multiply same-game legs naively;
   - derive same-game joint probabilities from the score matrix where possible;
   - flag unmodelled legs explicitly;
   - do not mix incompatible settlement keys;
   - separate true +EV, promo extraction, and entertainment/punt.

5. Arbitrage:
   - calculate guaranteed return after commission/fees;
   - require same settlement key;
   - require liquidity/size;
   - show exact stake split;
   - refuse fake arbs.

Acceptance:

- One CLI produces a ranked, timestamped opportunity report.
- Telegram can request the same report.
- Website can display the same report.
- Every opportunity is tagged: `model`, `offer`, `arb`, `hedge`, or `punt`.

### Phase 5 — Ledger, bot, site, tracking polish

Goal: make the system operationally boring.

1. Ledger:
   - account split: account 1/user and account 2/mum;
   - source split: model/offer/punt/hedge/arb if supported;
   - venue normalization;
   - per-currency P&L;
   - promo extraction table;
   - settlement helpers;
   - CLV helpers.

2. Telegram:
   - `/summary` should be readable in seconds;
   - `/bets` should show open positions, grouped sensibly, no wall of mud;
   - `/scores` should rank scorelines clearly;
   - `/pm` should show PM readiness, dry-run/live mode, parked orders, open PM
     exposure, and warnings;
   - add a clear command for opportunities if absent.

3. Website:
   - terminal dashboard updates after every ledger/site refresh;
   - tracking page updates at the same time;
   - open and closed positions separated correctly;
   - account/source/promo columns visible;
   - venue colours consistent;
   - line movement chart ordered by kickoff/time and labelled in local timezone;
   - opportunity page for EV/arb/promo/PM ideas;
   - under-the-hood page reflects current code.

4. Automation:
   - snapshot daemon keeps running;
   - bot restarts safely after code changes;
   - publishing does not exceed Vercel deploy limits;
   - no test run can push fake site data.

Acceptance:

- Reconcile a settled match and verify:
  ledger -> site data -> tracking data -> bot summary all agree.
- Tests cover the reconciliation path.

## Model/agent recommendations

Use models based on risk, not convenience.

1. **Lead orchestrator / final integrator**
   - Recommended: GPT-5 Codex or Claude Opus 4.8.
   - Job: maintain the task graph, make architecture decisions, resolve
     conflicts, review all money-touching changes, run final tests, and write
     the final handoff.

2. **Quant/model audit agent**
   - Recommended: Claude Opus 4.8 or another top reasoning model.
   - Job: model diagnostics, backtests, calibration, advancement/knockout
     semantics, event-model assumptions.
   - Must be skeptical and must prefer "not enough evidence" over a false edge.

3. **Prediction-market/security agent**
   - Recommended: Claude Opus 4.8 / GPT-5 high-reasoning.
   - Job: Polymarket/Kalshi signing, relayer, order lifecycle, stale-price
     prevention, settlement rules, live-money guardrails.
   - This is the highest-risk coding area. Do not assign it to a cheap model.

4. **Data ingestion agent**
   - Recommended: Claude Sonnet 4.5/4.6 or GPT-5 Codex.
   - Job: The Odds API, Betfair, Polymarket scraping, normalized schemas,
     storage, quotas, malformed feed tests.

5. **Bot/site/product agent**
   - Recommended: Claude Sonnet 4.5/4.6.
   - Job: Telegram UX, dashboard/tracking UI, opportunity page, readable
     reports, auto-refresh, site publishing.

6. **Test/documentation agent**
   - Recommended: Claude Haiku 4.5 or a cheaper fast model, but only after
     senior agents define contracts.
   - Job: add regression tests, update docs, generate fixtures, check formatting.

7. **Adversarial reviewer**
   - Recommended: a different top-tier model from the implementer.
   - Job: hunt for fake arbs, stale price execution, data leakage, settlement
     mismatches, currency summing, hidden look-ahead bias, and live-money
     logging gaps.

## Required final response from the agent

When finished, report:

1. What was audited.
2. What was changed.
3. What was deliberately left unchanged and why.
4. Tests run and results.
5. Any live-money blockers.
6. Any markets/opportunities found, with source, timestamp, venue, price, fair
   price, edge, stake, liquidity, settlement key, and confidence.
7. Exact next actions for the user, only if human action is required.

Do not claim an edge unless it survives the model, market, fee, liquidity,
settlement, and staleness checks.

---

## Original project brief, condensed

Build a professional open-source World Cup betting research platform that
compares sportsbooks, exchanges, and prediction markets; estimates calibrated
probabilities; finds mispricing; sizes bets with fractional Kelly; tracks CLV,
ROI, calibration, drawdown, and risk of ruin; and documents whether positive
long-term ROI is realistically achievable. Begin with transparent Elo,
Dixon-Coles, and market-implied models before adding more complex ML. Assume
markets are efficient, be skeptical, and prioritize statistical validity over
prediction accuracy theatre.

