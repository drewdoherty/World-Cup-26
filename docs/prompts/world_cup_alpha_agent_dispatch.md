# World Cup Alpha — Agent Dispatch Prompts

Use this alongside
`docs/prompts/world_cup_alpha_master_improvement_prompt.md`.

The master prompt is the shared constitution. The prompts below are narrower
copy-paste tasks for parallel agents. Each agent must first read the master
prompt and then stay inside its assigned lane unless it finds a genuine blocker.

## Recommended model assignment

| Workstream | Recommended model | Why |
| --- | --- | --- |
| Lead orchestration / merge review | GPT-5 Codex or Claude Opus 4.8 | Cross-file judgment, live-money risk, conflict resolution |
| Polymarket trading overhaul | Claude Opus 4.8 / GPT-5 high reasoning | Highest danger: signatures, stale orders, real funds |
| Quant/model audit + advancement | Claude Opus 4.8 | Needs skeptical probability reasoning and no false edge claims |
| Market-data ingestion | Claude Sonnet 4.5/4.6 or GPT-5 Codex | Large but bounded code/data contracts |
| Telegram/site UX | Claude Sonnet 4.5/4.6 | Product polish plus repo integration |
| Tests/docs after contracts | Claude Haiku 4.5 | Fast regression expansion once interfaces are fixed |
| Adversarial review | Different top-tier model from implementer | Catches fake arbs, settlement mismatch, and live-money holes |

## Execution order

Run these in parallel only where noted.

1. **Lead audit** starts first and owns merge decisions.
2. **Polymarket overhaul** starts immediately after the lead audit identifies the
   real current PM interfaces.
3. **Market-data ingestion** and **Quant/model audit** can run in parallel.
4. **Telegram/site UX** starts once the PM proposal contract and opportunity
   feed contract are stable.
5. **Adversarial review** runs after each risky branch, before merge.

## Shared guardrails for every agent

Paste these into every sub-agent prompt:

```text
Read `docs/prompts/world_cup_alpha_master_improvement_prompt.md` first and obey
it. Inspect `git status --short` before editing. Do not read, print, or commit
`.env`. Do not fabricate prices, fixtures, markets, token ids, squads, or
injuries. Do not silently place live bets or prediction-market orders. Preserve
Python 3.9 compatibility. Add focused tests for touched behavior. Do not revert
unrelated user/live changes. Cite local file paths and primary sources for all
claims. If this task touches site publishing, ensure both terminal dashboard and
tracking data refresh together.
```

---

## Prompt A — Lead Audit And Merge Plan

Recommended model: **GPT-5 Codex or Claude Opus 4.8**

```text
You are the lead architect for World Cup Alpha.

Repository:
/Users/andrewdoherty/Desktop/Coding/World Cup Alpha

Read:
- docs/prompts/world_cup_alpha_master_improvement_prompt.md
- README.md
- TODO.md
- docs/architecture/SYSTEM_MAP.md
- SETUP_AUDIT.md if present
- all current untracked PROMPT_*.md / checklist files

Task:
Produce a source-verified implementation audit and merge plan. Do not make
large behavior changes yet unless you find a severe live-money safety bug.

Audit:
1. Current modules, scripts, daemons, tests, and site pages.
2. Claims in README/TODO/docs that disagree with code.
3. Current state of:
   - model/card pipeline;
   - Polymarket trading;
   - Odds API and Betfair data;
   - advancement/knockout pricing;
   - event-market scanners;
   - news feed;
   - ledger/account/source/offers;
   - Telegram bot;
   - site/tracking publishing.
4. Dirty git tree triage:
   - user/live data;
   - generated files;
   - safe code/docs;
   - snapshot spam not to commit.
5. Prioritized task graph with file ownership to avoid parallel collisions.

Write/update:
- docs/research/system_audit_current.md
- TODO.md
- docs/prompts/world_cup_alpha_agent_dispatch.md if the dispatch needs updating

Acceptance:
- No `.env` read.
- Every claim cites source file(s).
- The plan explicitly names P0/P1/P2 priorities and which agent owns them.
- It flags any immediate live-money blocker.
- Run only lightweight tests unless you changed behavior.
```

---

## Prompt B — Polymarket Trading Overhaul

Recommended model: **Claude Opus 4.8 / GPT-5 high reasoning**

```text
You own the Polymarket trading overhaul. This is the highest-risk part of the
project. Read the master prompt first.

Focus files:
- src/wca/data/polymarket.py
- src/wca/pm/
- src/wca/bot/app.py
- scripts/wca_pm_probe.py
- scripts/wca_pm_propose.py
- scripts/wca_pm_approve.py
- scripts/wca_pm_watch.py
- docs/research/polymarket_v2_spec.md
- docs/research/polymarket_depositwallet_spec.md
- tests/test_pm_*.py

Do not touch unrelated model/site files unless needed for the PM contract.

Task:
Replace the bolted-on PM producer/gate flow with a clean, persistent, safe
order lifecycle.

Required outcomes:
1. One canonical proposal/order schema, persisted in SQLite:
   - stable id;
   - event/market/token ids;
   - question/outcome;
   - side;
   - limit price;
   - shares;
   - notional USD;
   - model prob;
   - fair price;
   - edge/EV;
   - fee estimate;
   - bid/ask/spread/depth;
   - settlement key;
   - source: model/hedge/punt/offer/arb;
   - expires_at/kickoff cutoff;
   - current state and error.

2. Persistent Telegram confirm queue:
   - proposal survives bot restart;
   - `Y PM-n` can only execute current, unexpired proposal;
   - old PM ids cannot execute stale matches;
   - group chats read-only;
   - admin user only can execute.

3. Execution preflight:
   - re-fetch orderbook;
   - reject if price moved beyond slippage;
   - reject if edge is no longer positive above threshold unless explicitly
     marked hedge;
   - reject if insufficient depth;
   - reject if after cutoff/kickoff;
   - check PM_DRY_RUN;
   - enforce per-order and daily caps;
   - check account class / sig type / funder.

4. Logging:
   - dry-run attempts logged;
   - live attempts logged;
   - live response captured;
   - fills reconciled from Polymarket activity;
   - ledger row created for live accepted orders;
   - site + tracking refresh after reconciliation;
   - if order may be live but logging fails, alert admin and block blind retry.

5. UX:
   - `/pm` shows readiness, dry-run/live, funder, sig type, parked orders, open
     PM exposure, last errors, and warnings;
   - proposal message clearly states what YES/NO means;
   - shows price, fair, edge, stake, max win/loss, spread/depth, cutoff.

6. Correct account class:
   - Verify whether the current proxy is DepositWallet/POLY_1271 from local
     docs/code and official docs/source if network is available.
   - Do not use old Gnosis Safe assumptions unless proven.

Tests:
- stale proposal cannot execute;
- price move rejects execution;
- dry-run default;
- live path is gated;
- persistence across restart;
- admin/group gate;
- proposal schema migration;
- token resolver for 1X2, draw, correct-score if supported, advancement if
  supported;
- logging completeness guard.

Acceptance:
One dry-run E2E test: proposal -> persisted queue -> Telegram confirmation text
-> Y PM-n -> signed dry-run result -> PM log row -> ledger row or dry-run audit
row -> site/tracking refresh hook. No live order in tests.
```

---

## Prompt C — Market Data Ingestion And Normalized Odds Schema

Recommended model: **Claude Sonnet 4.5/4.6 or GPT-5 Codex**

```text
You own market-data ingestion.

Read the master prompt first. Stay mostly in:
- src/wca/data/
- src/wca/linemove.py
- scripts/wca_snapshotd.py
- scripts/wca_snapshot_odds.py
- scripts/wca_event_ev.py
- scripts/wca_arb.py
- tests/test_snapshot_odds.py
- tests/test_linemove*.py
- tests/test_arb.py

Task:
Create or repair one normalized market snapshot layer that can ingest all useful
World Cup markets from The Odds API, Betfair Exchange, and Polymarket.

Required schema:
- source;
- venue/bookmaker;
- event id;
- fixture teams;
- kickoff;
- market key;
- outcome name;
- line/point;
- decimal odds or PM price;
- bid/ask/mid if available;
- available size/liquidity if available;
- retrieved timestamp;
- settlement key;
- currency;
- commission/fee metadata;
- raw source id fields.

Implement:
1. The Odds API:
   - h2h, totals, alternate_totals, btts, draw_no_bet;
   - correct score/player props/cards/corners/SOT/fouls where available;
   - raw JSON snapshots;
   - quota-aware pulls;
   - no good-file clobber on empty/truncated response.

2. Betfair:
   - inspect current repo docs first;
   - implement only against current official Betfair docs and available env
     contract;
   - parse market catalogue + market book;
   - include delayed/live status and commission;
   - normalize to the same schema.

3. Polymarket:
   - Gamma event discovery;
   - CLOB orderbook snapshots;
   - market classifier for 1X2, advancement, outright, score/event markets;
   - settlement key and liquidity/spread.

4. Downstream:
   - update line movement to use normalized data;
   - make event EV scanner consume normalized data where possible;
   - make arb scanner require settlement keys and liquidity.

Tests:
- malformed feeds;
- missing fields;
- settlement-key classification;
- 90-minute vs ET/pens refusal;
- quota/header parsing;
- Betfair mocked auth/catalogue/book;
- PM orderbook mocked parsing;
- no clobber of populated linemove with empty feed.

Acceptance:
One CLI can produce a timestamped normalized snapshot and print source counts by
market. Existing card/scanner behavior must not regress.
```

---

## Prompt D — Quant Model, Advancement, And Event-Market Audit

Recommended model: **Claude Opus 4.8**

```text
You own the quant/model audit and high-EV market roadmap.

Read the master prompt first. Focus:
- src/wca/card.py
- src/wca/models/
- src/wca/markets/
- src/wca/advancement.py
- src/wca/sim/tournament2026.py
- src/wca/boosts.py
- src/wca/accas.py
- scripts/wca_event_ev.py
- scripts/wca_advancement.py
- scripts/wca_price_scorers.py
- backtests/
- docs/research/backtests/
- tests/test_*model*, test_advancement, test_props, test_scorers

Task:
Audit the probability model and upgrade the decision framework for advancement,
knockout, and match-event betting. Do not change live staking parameters unless
the backtest/audit justifies it and you document the change.

Audit:
1. Elo parameters and calibration.
2. Dixon-Coles parameters, half-life, rho, shrinkage, low-data teams.
3. Market devig and blend weights.
4. Scoreline reconciliation.
5. Host/venue/altitude and lineup/squad weaknesses.
6. Calibration bins, Brier/log-loss, per-team residuals, holdout consistency.
7. Whether live 2026 results/news should update team strength or only future
   state.

Advancement:
1. Ensure already-played group games are fixed.
2. Anchor future group-stage probabilities to market where available.
3. Preserve ET/pens semantics for advancement/knockout markets.
4. Compare model vs Polymarket/books with fees/spread/liquidity/staleness.
5. Surface only actionable edges with confidence labels.

Match events:
1. Correct-score pricing from reconciled score grid.
2. Totals/alternate totals, BTTS, DNB/double chance.
3. Build or improve priors for cards, corners, SOT, fouls from real sources:
   football-data.co.uk and StatsBomb where allowed.
4. Goalscorer/player props: do not fake xG shares. Document data requirement if
   not currently sourceable.
5. Correlated same-game accas: use joint probability from score grid where
   possible; flag unmodelled legs.

Write/update:
- docs/research/model_diagnostics.md
- docs/research/event_model_plan.md
- docs/research/advancement_edges.md or a successor

Tests:
- no look-ahead backtest guards;
- scoreline-derived joint probabilities;
- advancement settlement semantics;
- market-anchored sim contract;
- event-prior missing values stay NaN.

Acceptance:
The final report must clearly separate: proven, plausible but unproven, and not
currently modelled. No fake confidence.
```

---

## Prompt E — Unified Opportunity Feed, EV, Arbs, Promos, Accas

Recommended model: **Claude Sonnet 4.5/4.6 or GPT-5 Codex**

```text
You own the opportunity feed.

Read the master prompt first. Focus:
- src/wca/arb.py
- src/wca/boosts.py
- src/wca/promos.py
- src/wca/promosdata.py
- src/wca/matched.py
- src/wca/offers.py
- src/wca/accas.py
- scripts/wca_event_ev.py
- scripts/wca_arb.py
- scripts/wca_promos_data.py
- scripts/wca_price_scorers.py
- tests/test_arb.py
- tests/test_boosts.py
- tests/test_promos*.py
- tests/test_matched.py
- tests/test_offers.py

Task:
Create one ranked opportunity engine that merges:
- model EV;
- Polymarket EV;
- advancement/knockout EV;
- arbs;
- boosts/promos/free-bet extraction;
- accas/bet-builders;
- hedges against existing exposure.

Each opportunity row must include:
- kind: model / hedge / arb / offer / punt candidate;
- market and settlement key;
- venue(s);
- current price(s);
- fair price/probability;
- edge/EV;
- stake recommendation;
- max loss and max win;
- liquidity/stake-limit caveat;
- correlation/exposure note;
- source data timestamp;
- confidence / reason not actionable.

Rules:
- Never mix 90-minute and ET/pens markets.
- Do not multiply correlated same-game legs naively.
- Free-bet/offer extraction is tracked separately from model CLV.
- Promos/boosts can be the largest edge; treat them as first-class.
- Punts must be labelled as punts and not credited to the model.

Outputs:
- CLI report;
- Telegram command output;
- website opportunity page/feed if the UI agent has a contract ready.

Tests:
- fake-arb refusal;
- commission/fee netting;
- same-game correlation path;
- free-bet SNR/SR math;
- promo source accounting;
- exposure-aware hedge labels.
```

---

## Prompt F — Telegram, Website, Tracking, And Operator UX

Recommended model: **Claude Sonnet 4.5/4.6**

```text
You own the operator-facing experience.

Read the master prompt first. Focus:
- src/wca/bot/app.py
- src/wca/bot/telegram.py
- src/wca/bot/vision.py
- src/wca/sitedata.py
- src/wca/tracking.py
- src/wca/sync.py
- scripts/wca_site.py
- scripts/wca_tracking_data.py
- site/
- tests/test_bot_*.py
- tests/test_sitedata.py
- tests/test_tracking.py
- tests/test_sync.py

Task:
Make the bot and site clear enough that a tired operator can use them during a
match without misreading exposure.

Required:
1. `/summary`:
   - per-currency bankroll/P&L;
   - open exposure;
   - CLV;
   - source split;
   - account split;
   - promo extraction separate from model.

2. `/bets`:
   - readable table;
   - open positions grouped by kickoff/match/venue;
   - account/source/market/stake/odds/max loss/max win;
   - no giant horizontal mud.

3. `/scores`:
   - ranked scorelines;
   - fair odds and current best market price if available;
   - clear stale-data banner.

4. `/pm`:
   - dry-run/live mode;
   - parked orders;
   - stale warnings;
   - open PM exposure;
   - wallet/proxy readiness without exposing secrets.

5. Website:
   - open and closed positions correct;
   - tracking page refreshes with terminal page;
   - account/source/promo columns;
   - consistent venue colours;
   - opportunity page;
   - under-the-hood page current;
   - charts useful and labelled in local timezone.

6. Publishing:
   - every ledger reconciliation refreshes site data and tracking data;
   - tests cannot push fake data;
   - Vercel deploy limit respected.

Tests:
- bot formatting snapshots/string assertions;
- group read-only/admin gate;
- site data contracts;
- tracking refresh included in sync;
- no currency summing;
- open/closed table separation.
```

---

## Prompt G — Adversarial Review

Recommended model: **a different top-tier model from the implementer**

```text
You are the adversarial reviewer. Your job is to find ways the implementation
can lose real money, report false edge, or mislead the operator.

Read the master prompt first. Review the branch/diff from the assigned
implementer.

Attack areas:
1. Polymarket:
   - stale proposal execution;
   - wrong signer/funder/account class;
   - old PM id reuse;
   - wrong token id;
   - YES/NO ambiguity;
   - price moved before confirmation;
   - dry-run/live confusion;
   - live order not logged;
   - secret leakage.

2. Settlement:
   - 90-min vs ET/pens mismatch;
   - to-qualify paired with match odds;
   - correct-score settlement;
   - free-bet stake-return assumptions.

3. Data:
   - fabricated fixtures/prices;
   - stale odds;
   - missing market lines;
   - malformed feed not handled;
   - currency summed across GBP/USD.

4. Model:
   - look-ahead bias;
   - using closing odds as pre-match input;
   - overfitting blend weights;
   - false precision on props/goalscorers;
   - unmodelled same-game correlation.

5. Site/bot:
   - stale data shown without warning;
   - open/closed positions wrong;
   - source/account wrong;
   - tracking not refreshed;
   - unreadable or misleading PM prompt.

Output:
- Findings first, ordered by severity.
- Include file/line references.
- Include concrete reproduction steps or failing tests when possible.
- If clean, say so and list residual risks.
```

