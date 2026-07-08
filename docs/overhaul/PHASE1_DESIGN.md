# PHASE 1 — Target Architecture and Sequenced Implementation Plan

Status: DESIGN ONLY. Nothing in this document is implemented. Nothing beyond increment 0
(test de-landmining) and documentation merges proceeds without explicit user sign-off on
this plan. Baseline for all rollbacks: tag `pre-overhaul-2026-07-01`; every increment
additionally carries its own revert point (one PR = one revert).

Evidence basis: origin/main @ `957112a` (2026-07-02) plus the ten Phase-0 evidence
reports. Facts that could not be verified on the live mini this session (SSH denied) are
marked `[UNVERIFIED-MINI: <2026-07-01 snapshot value>]`.

---

## 1. Design principles

1. **Deletion over addition.** 23 dead scripts, 47 near-zero-risk branch deletions, a
   duplicate benchmark package, a legacy installer, two redirect-stub pages and an
   orphaned terminal page all go before any new capability lands. Every increment is
   measured first by what it removes.
2. **Never fork `src/wca/markets/bankroll.py`.** It is the single sizing authority
   (quarter-Kelly of GBP 3,000 ± realised P&L at 1.33 USD/GBP, 4%/bet cap, 75%
   whole-book cap). It is extended in place (section 4); no second sizing path is ever
   created. Its import path does not move during the overhaul — path stability on the
   money brain is a safety property.
3. **Protect the two-bot isolation.** The conductor (`@WorldCupDev`) has zero
   trader/ledger imports (verified: zero hits for `ClobTrader|record_bet|place_order`
   under `src/wca/conductor/`), strips the PM key and forces `PM_DRY_RUN=1` + `dev.db`
   (`src/wca/conductor/config.py:20-22`). No design below gives the code-writing agent a
   path to money, and the ops bot (`@gamble1_bot`) never gains code-writing ability.
4. **Every increment is PR-sized and pytest-gated.** One reviewable unit per PR; the
   suite must be green before merge (increment 0 makes green meaningful again — the
   suite is currently red for data-coupling reasons unrelated to code,
   `tests/test_betrecs_open_exposure.py:91`).
5. **Sign-off gate before live-money behavior changes.** Any change that alters what
   orders are proposed, sized, fired, settled, or how the canonical ledger is written on
   the mini requires an explicit user sign-off recorded in `CHANGELOG.md` before merge.
   Shadow/tracking-mode changes (persist and compare, stake nothing) need only the
   normal services sign-off for new mini jobs.
6. **Rollback is always defined.** Global: `git reset --hard pre-overhaul-2026-07-01`
   plus launchd reinstall from that tag's `deploy/macmini/install.sh`. Per increment:
   revert the increment's PR(s); increments are ordered so that reverting N does not
   strand N−1.
7. **CLV is the acceptance metric for every model change.** No model or consensus
   change drives sizing until it has beaten (or matched) the incumbent in shadow via
   `wca.clvbench` and the snapshot benchmark, and the user has signed off.

---

## 2. Target module layout

### 2.1 Package structure

`src/wca` today: 149 modules, 39 loose top-level modules (excluding `__init__.py`), 16
subpackages. Target: the loose modules collapse into scoped packages; the model core and
the money path keep their import paths.

| Package | Status | Contents / rule |
|---|---|---|
| `wca/models/` | UNTOUCHED (one rename) | dixon_coles, elo, goalblend, scores, scorers, props, playerprops, betbuilder, structural; `models/venues.py` → `models/stadiums.py` (stadium/altitude model, `host_advantage_points` at `models/venues.py:91`) |
| `wca/pricing/` | NEW | one devig module with named methods, one CLV formula, arb primitives, FX |
| `wca/data/` | UNTOUCHED (+1) | 19 feed adapters (+`__init__`) stay; `news.py` moves in |
| `wca/ledger/` | KEPT (+1 rename-in) | store, reports, sync-adjacent; `wca/venues.py` → `wca/ledger/bookmakers.py` (`canon_platform`, `venues.py:16`) |
| `wca/pm/` | KEPT AS THE EXECUTION PACKAGE | trader, relayer, positions; `positions_sync.py` moves in. Deliberately NOT renamed — zero churn on the money path |
| `wca/risk/` | NEW | exposure slate + the one canonical correlation engine + accas |
| `wca/analytics/` | NEW | model-CLV benchmark, venue benchmark pair, winrate, outright edge, PM analytics family, advancement |
| `wca/publish/` | NEW | every site-feed builder module |
| `wca/promo/` | NEW | boosts, matched, offers, promos |
| `wca/ops/` | NEW | closecapture, sync, pollsched, snapshot_freshness |
| `wca/markets/` | KEPT | `bankroll.py` (principle 2), `devig.py` re-exported from `wca/pricing/devig.py` during transition |
| `wca/bot/`, `wca/conductor/`, `wca/intel/`, `wca/mc/`, `wca/predledger/`, `wca/rigor/`, `wca/sim/`, `wca/testbook/`, `wca/archive/` | KEPT | unchanged |
| `wca/bench/` | **DELETED** | wired to nothing; duplicates clvbench (`bench/report.py:121`) with a private devig (`bench/sources.py:217`) |
| `wca/card.py` | KEPT TOP-LEVEL | the single sanctioned loose module: it is the most-wired orchestrator in the repo (bot, buildcard job, CI daily-card, propose daemon); moving it buys nothing and risks the most |

### 2.2 Mapping table — the 39 loose modules

| Current (`src/wca/`) | Target | Notes |
|---|---|---|
| `card.py` | `wca/card.py` (stays) | sanctioned orchestrator |
| `cardcache.py` | `wca/publish/cardcache.py` | |
| `sitedata.py` | `wca/publish/sitedata.py` | |
| `scorespage.py` | `wca/publish/scorespage.py` | |
| `dashboard.py` | `wca/publish/dashboard.py` | its private CLV loop (`dashboard.py:171-216`) is deleted; it calls `ledger.reports.clv_report` |
| `tracking.py` | SPLIT | `devig_consensus` (`tracking.py:348-384`) becomes a thin wrapper over `wca/pricing/devig.py`; the feed-building remainder → `wca/publish/tracking.py` |
| `linemove.py` | `wca/publish/linemove.py` | |
| `promosdata.py` | `wca/publish/promosdata.py` | |
| `arbdata.py` | `wca/publish/arbdata.py` | stays a presenter |
| `nextmatch.py` | `wca/publish/nextmatch.py` | |
| `clvbench.py` | `wca/analytics/clvbench.py` | **canonical model-CLV benchmark** |
| `venuesbench.py` | `wca/analytics/venuebench/engine.py` | rename only — no logic merge (engine/data pair, zero duplicated math) |
| `venuesdata.py` | `wca/analytics/venuebench/data.py` | |
| `winrate.py` | `wca/analytics/winrate.py` | |
| `outrightedge.py` | `wca/analytics/outrightedge.py` | |
| `snapshot_freshness.py` | `wca/ops/snapshot_freshness.py` | |
| `exposure.py` | `wca/risk/exposure.py` | slate building, blind spots, gap plugs only; legacy engine deleted (2.3) |
| `exposure_corr.py` | `wca/risk/exposure_corr.py` | **canonical correlation engine** |
| `exposure_dashboard.py` | `wca/risk/exposure_dashboard.py` | metrics/presentation layer, not an engine |
| `accas.py` | `wca/risk/accas.py` | already consumes exposure_corr (`accas.py:357`) |
| `pmanalytics.py` | `wca/analytics/pm/analytics.py` | |
| `pmhistory.py` | `wca/analytics/pm/history.py` | its `pm_snapshots` sink (`pmhistory.py:29-49`) is superseded by the CLOB tick tables (3d) |
| `pmmovers.py` | `wca/analytics/pm/movers.py` | |
| `pmtrends.py` | `wca/analytics/pm/trends.py` | |
| `positions_sync.py` | `wca/pm/positions_sync.py` | execution-adjacent reconciliation |
| `arb.py` | `wca/pricing/arb.py` | SHRUNK to primitives: `settlement_key`, `effective_back`, `pm_yes_to_decimal`, `_arb_from_net`, `two_way_arb`, `three_way_arb`; the detectors `find_cross_book_arbs` (`arb.py:214`), `find_pm_book_arbs` (`arb.py:281`), `rank_arbs` (`arb.py:397`) are retired — **canonical scanner = `wca.intel.arb`** (delegation already verified at `intel/arb.py:257,326`) |
| `arbfx.py` | `wca/pricing/arbfx.py` | keeps FX/commission math; `DEFAULT_BETFAIR_COMMISSION = 0.06` (`arbfx.py:27`) corrected to a configurable real tier (Betfair Rewards 5% / Basic 2%) |
| `fx.py` | `wca/pricing/fx.py` | |
| `boosts.py` | `wca/promo/boosts.py` | |
| `matched.py` | `wca/promo/matched.py` | |
| `offers.py` | `wca/promo/offers.py` | |
| `promos.py` | `wca/promo/promos.py` | |
| `advancement.py` | `wca/analytics/advancement.py` | |
| `modelpreds.py` | `wca/predledger/modelpreds.py` | prediction persistence belongs with the prediction ledger |
| `news.py` | `wca/data/news.py` | |
| `closecapture.py` | `wca/ops/closecapture.py` | |
| `sync.py` | `wca/ops/sync.py` | pytest publish guard (`sync.py:113-118`) moves with it |
| `pollsched.py` | `wca/ops/pollsched.py` | |
| `venues.py` | `wca/ledger/bookmakers.py` | kills the venues name collision |

Migration mechanics: one package per PR; atomic move + repo-wide import update; a
deprecation shim (old path re-exports new, warns) is left ONLY for modules imported by
launchd-wired scripts, and removed one increment later once the mini has pulled.

### 2.3 Canonical choices (the duplication verdicts, restated as design)

| Family | Canonical | Action |
|---|---|---|
| CLV formula | ONE function, `wca/pricing/clv.py`: `clv(odds_taken, fair_close) = odds_taken/fair_close − 1`, `fair_close` = multiplicative devigged consensus from `closecapture.consensus_close` | The four already-consistent writers (`closecapture.py:435`, `ledger/store.py:838`, `bot/app.py:1011`, `wca_settle.py:102`) import it instead of restating it |
| Bet-CLV aggregation | `wca.ledger.reports.clv_report` | `dashboard.py:171-216` re-loop deleted; eligibility filters unified behind one documented rule |
| Model-CLV benchmark | `wca.clvbench` (`p_close/p_model − 1`, `clvbench.py:246`; wired via `wca_build_analytics.sh:31`) | `wca/bench/report.py:121` duplicate + private devig `bench/sources.py:217` deleted with the whole `bench/` package |
| Devig | `wca/pricing/devig.py` with named methods (`multiplicative`, `power`, `shin` — from `markets/devig.py:63`) | `tracking.devig_consensus` becomes a wrapper; `bench/sources._devig_three` deleted; `scripts/microstructure/clv.py` either sources its close from `closecapture.consensus_close` or declares its Shin basis in output — the three-devig-regime problem (model input: per-book Shin + cross-book median `card.py:767-789`; ledger close: multiplicative; microstructure: Shin) becomes ONE module, callers name their method explicitly |
| Exposure | `exposure_corr` (shared scoreline matrix + cross-fixture convolution) | legacy `_portfolio_scenarios` (`exposure.py:442`, still executing inside `build_exposure_data` at `:272`) is retired; if a 1X2-marginal view is still wanted it is derived by marginalising exposure_corr, not by a second engine |
| Arb | scanner = `wca.intel.arb`; math = `pricing/arb.py` primitives + `pricing/arbfx.py` | `wca_arb_history.py` is dead — deleted (2.4); `wca_arb.py` is operator-manual (Phase-0 tier M, README.md:135) — it moves to `scripts/manual/` and is ported to the intel scanner (or archived) when `arb.py` is shrunk to primitives |
| Venues | renames only | `wca/ledger/bookmakers.py` vs `wca/models/stadiums.py` vs `wca/analytics/venuebench/` — a naming collision, not duplication; zero logic merges |

### 2.4 `scripts/` — tiering, deletions, naming

Target layout (110 files today → ~87-88: 110 − 22 deletions = 88, or 87 if
`wca_betbuilder` is also retired at the increment-3 decision):

- `scripts/` — WIRED ONLY (launchd 16 + CI 10 + chain 13 + bot-backed 2, per the
  Phase-0 manifest). A checked-in `scripts/MANIFEST.md` lists every script with its
  tier and wiring evidence; a new test fails if any script file is absent from the
  manifest (the manifest can never silently rot again).
- `scripts/manual/` — the 38 operator-manual tools (incl. the 11
  `scripts/microstructure/*` research generators, which stay manual). Wired paths in
  `deploy/` and `.github/` are updated in the same PR as any move.
- `scripts/archive/` — the existing 7, plus `wca_validation_report.py` (dead by the
  zero-wiring definition but carries a documented manual re-run procedure in
  `docs/research/model_and_rec_validation_report.md:14,58,256` — archive, not delete).
- **DELETE the other 22 confirmed-dead scripts**: `wca_event_ev` (F8, see 2.5),
  `wca_recalibrate_dc_level`, `wca_backfill_model`, `wca_backfill_pm_ev`, `wca_pm_try`,
  `acca_coverage_optimizer`, `build_benchmarks`, `wca_morning`, `wca_fix_betfair_venue`,
  `wca_benchmark`, `wca_arb_history`, `wca_import_polymarket`, `wca_pm_transfer`,
  `wca_reconcile_1x2_mc_report`, `wca_snapshots`, `gen_wc_calendar`, `wca_tracking`,
  `wca_clv_by_bet` (labels EV as CLV, `wca_clv_by_bet.py:49`), `wca_recompute_open_bets`,
  `wca_matchevents_data`, `wca_price_scorers`, `wca_exposure_sizer`. Each was verified
  zero-execution-referenced by an adversarial second pass. NOT deleted (rescued as
  alive): `wca_backfill_accounts` + `wca_canon_venues` (importlib-executed by tests) and
  `wca_pm_probe` (cited by live bot code, `app.py:2099`).
- Name-collision traps dissolve mostly by deletion (`wca_tracking` vs
  `wca_tracking_data`; `wca_arb_history`/`wca_benchmark`/`build_benchmarks` all dead).
  Convention going forward: `wca_<domain>_data.py` = site-feed builder;
  `wca_<domain>d.py` = daemon; anything else = CLI.
- **`wca_betbuilder.py` gap resolved**: the bot calls it "the cron build"
  (`app.py:847`) but no scheduler exists anywhere. Recommendation: retire the script and
  the `/betbuilder` command, consistent with the research kill of SGM-class markets
  (section 5). Alternative (if the user wants the display): add a 6-h interval job.
  Decision at increment 3 sign-off.

### 2.5 Dark features — F7 / F8 / F9

| Feature | State | Decision |
|---|---|---|
| **F7 goal blend** (`models/goalblend.py`; gate `card.py:611` default False; only True caller is `tests/test_goalblend.py:266`) | finished, tested, dark | **WIRE, tracking-only first**: pass `goal_blend=True` at `scripts/wca_build_card.py:258`, persist blended lambdas alongside `modelpreds`, compare CLV via clvbench for ≥2 weeks OOS. It drives sizing only after it beats the incumbent and the user signs off (the module's own no-fabrication contract, `goalblend.py:1-50`) |
| **F8 event-EV** (`scripts/wca_event_ev.py`, zero refs; duplicates fee math at `:43,:96`) | dead standalone; the LIVE correlated-exposure path is `exposure.py` `event_bets` → `wca_exposure_data.py:160` → `wca_build_analytics.sh:31` → launchd | **FOLD + DELETE**: its only unique value — the totals/BTTS-vs-book EV sweep — is folded into the live card path using canonical `pricing/arb.py` fee math; then the script is deleted |
| **F9 `card.py:1406 build_event_references`** (tested in `tests/test_card_events.py`, ZERO production callers; `02_codebase_audit.md:161` wrongly calls it live) | third dark surface | **WIRE as the landing zone for the F8 fold**: call it from the card build with an EV/edge gate and surface event references in the card + exposure event-ref path. If the wiring is not done by the end of increment 6, DELETE it — no third option |

---

## 3. Quant upgrade design (the underused data)

Ground rule for everything in this section: **shadow first**. Each change is persisted
next to the incumbent, benchmarked via `wca.clvbench` + the snapshot venue benchmark,
with CLV as the acceptance metric, and gets an explicit user sign-off before it drives a
staked price. No exceptions.

### 3.a StatsBomb ingestion pipeline

Today: `src/wca/data/statsbomb.py` (428 lines, per-shot xG/SoT/minutes/cards) is
production-dormant — no scheduled job builds `players.db`
(`playerprops.py:44-46` admits it), `DixonColes.fit` consumes integer goals only
(`dixon_coles.py:454-476`), and the only production trace is frozen CornersModel
constants (`props.py:125`). `data/raw/statsbomb/` and `data/players.db` on the mini:
[UNVERIFIED-MINI: absent as of 07-01].

Design:
1. **Scheduled build.** New mini interval job `playersdb` (weekly, 604800 s in
   `services.env`) running `scripts/wca_build_players_db.py` after a cache warm +
   `build_props_dataset` (`statsbomb.py:387`). Outputs (`data/players.db`,
   `data/processed/props_*.csv`) stay untracked (`.gitignore:9-13`) and ride the
   existing 6-h archive job off-box. CI runs the same build weekly as a smoke test
   (artifacts discarded).
2. **Team xG aggregates.** New module `wca/data/xg_aggregates.py`: per-team xG-for /
   xG-against per-90 from `props_matches.csv` (WC2018+WC2022, ~128 matches). Honesty
   constraint: this corpus covers two tournaments only — aggregates are tournament-level
   priors, not fresh form.
3. **Replace the 2.81 scalar with an xG-anchored empirical target.**
   `DEFAULT_DC_LEVEL_TARGET = 2.81` (`card.py:78`) — a hand-derived constant — is
   replaced by a target computed from the xG corpus (and blended with the market-totals
   prior, 3.b), recomputed by the weekly job and persisted to a small TRACKED JSON with
   `generated_at` + method so the anchor is auditable. The mechanism itself
   (`apply_wc_level_anchor`, `card.py:112`; post-MLE `mu += log(target/slate_total)`,
   `dixon_coles.py:654-671`) is kept — it is supremacy-invariant and already correct;
   only the constant becomes empirical. This also addresses the confirmed
   under-prediction of ~0.59 goals/game (xg-goal-calibration finding, p≈0.001).
4. **Shrink DC attack/defence toward xG priors.** Post-fit shrinkage of team
   attack/defence ratings toward xG-derived priors for teams present in the corpus,
   with a single shrinkage weight `w` fit by OOS log-loss/CLV on held-out matches.
   Implemented as an optional `fit_models` stage mirroring the goalblend gate pattern
   (default OFF, tracking-only first).
5. **Player props resurrected as shrunk overlays ONLY.** Per-90 xG shares
   (`statsbomb.py:287 player_shares`) refresh the scorer displays and betbuilder priors,
   raw probabilities shrunk toward market (the player-level finding: better
   discrimination, worse raw calibration). Scorer props remain EXCLUDED as a staked
   market (section 5) — this is a display/prior upgrade, not a market entry.

### 3.b Market-totals → lambda calibration loop

Today: snapshotd captures h2h+totals bulk and btts per-event
(`wca_snapshotd.py:58-61`), but the model devigs ONLY h2h (`card.py:767-789`); the
totals surface ([UNVERIFIED-MINI: ~427k totals rows of odds_snapshots' ~3.24M total,
per the 2026-07-01 snapshot]) feeds display/arb only.

Design:
1. Devig the totals surface per fixture (multiplicative, via `pricing/devig.py`) at the
   main line and adjacent lines; invert through the DC score matrix to a
   market-implied total-goals expectancy per fixture; aggregate across the slate into a
   daily **market-implied level prior**.
2. Blend with the xG empirical target: `target = w_m · market_implied + (1−w_m) ·
   xg_empirical`, `w_m` chosen by clvbench shadow comparison of the three candidates
   (xG-only, market-only, blend) against the incumbent 2.81.
3. **Explicit no-double-count rule:** the market-totals prior enters ONLY the
   slate-wide level target (the shared `mu` shift), never per-fixture lambdas. A
   per-fixture market-totals adjustment would feed the market back into the exact
   number we then bet against that market — betting the book back at itself. Totals/BTTS
   EV remains model-vs-market with the model's totals information limited to the daily
   slate-level anchor.

### 3.c Sharp-book weighting for the h2h devig

Today: per-book Shin devig then flat cross-book **median** (`card.py:786-789`), all
~49 [UNVERIFIED-MINI: 49 distinct bookmakers per 2026-07-01 snapshot] books weighted
equally.

Design: a sharpness weight per bookmaker, learned from the odds_snapshots history —
score each book by the log-loss/Brier of its own devigged h2h against results (and
against the eventual consensus close). Weights are built by a new analytics-job step,
persisted to a tracked JSON, and consumed by `market_consensus` as a weighted median.
Shadow protocol: run flat-median and weighted consensus side by side through clvbench;
the weighted version replaces the median only on CLV improvement + sign-off. (This also
future-proofs the consensus for the day a true sharp exchange feed — Betfair read relay
or Smarkets v3 — joins the panel.)

### 3.d Polymarket CLOB capture daemon (mini)

Today: `pm_clob_history.py:6-7` itself admits CLOB `/prices-history` is "far denser and
deeper than our own capture"; `/book` is fetched (`:35-75`) and persisted only as
top-of-book scalars for open PAPER-book positions (`testbook/store.py:279-300`, 10-min
plist); `data/pm_price_history.jsonl` is stalled at 2026-06-29 (2,057 rows, all
advancement, 13 distinct capture timestamps) despite a live twice-hourly cron —
a silent pipeline failure. The `pm_snapshots` DB rows written on CI are ephemeral (only
the JSONL is committed). Mini `pm_snapshots` table: [UNVERIFIED-MINI: absent].

Design:
1. **New daemon** `scripts/wca_clobd.py` (launchd daemon, alongside snapshotd) on the
   mini. Universe: all tokens for open ledger/testbook positions + tracked WC markets
   (advancement, group winner/top-2, matched 1X2). Two sinks in a NEW dedicated
   `data/pm_ticks.db` (keeps the 1.94 GB [UNVERIFIED-MINI] ledger DB lean):
   - `pm_ticks(token_id, ts_utc, price, source)` — `/prices-history` merged at 1-min
     fidelity, deduped by (token, minute);
   - `pm_book(token_id, ts_utc, bid, bid_size, ask, ask_size, mid, spread)` —
     periodic top-of-book (60 s for open positions, 5 min for the watchlist).
2. **One-time backfill** from CLOB prices-history (dense back to 2026-05-30) so
   trajectory analytics start with history, not a cold start.
3. **Consumers** (all shadow/analytics first): mark-to-market at transactable BID-side
   exits for every open PM position (replacing mid-price MTM); momentum/steam signals;
   advancement term-structure consistency (the `wca_pm_analytics_suite.py` scaffolding
   becomes a scheduled consumer instead of ad-hoc); exit-liquidity gating for Tier-3
   markets (section 5).
4. **Fix the stalled JSONL pipeline now, independently of the daemon:** pm-snapshot.yml
   swallows failures (`|| true` on the advancement refresh and on `git push`). Remove
   the `|| true`, add a workflow-failure notification, and add a freshness assertion
   (fail the run if the newest JSONL row is older than 2 h). The JSONL becomes a
   secondary/backup capture once `wca_clobd` lands.

---

## 4. Sizing scale-up design

### 4.1 Extend `markets/bankroll.py` (never fork)

Current single source of truth: quarter-Kelly of (GBP 3,000 ± realised P&L) at
`GBP_USD = 1.33` (`bankroll.py:22-31`), 4%/bet cap, 75% whole-book cap. Extensions, all
inside the same module:

1. **Per-pool native-currency quarter-Kelly.** A `Pool` abstraction: GBP sportsbook
   pool and USD Polymarket pool, each with its own base capital and realised-P&L feed
   (from the currency-split ledger reporting, 4.3). Stakes are computed and expressed in
   the pool's native currency; nothing crosses FX implicitly.
   NOTE (conflict flagged for sign-off): the interim operating rule sizes PM in USD at
   quarter-Kelly of the single GBP book (pm-sizing-rule-global, which superseded the
   earlier dual-pool rule of PR #100). This design returns to explicit per-pool sizing
   as the brief mandates. The user must pick one at the increment-7 gate; the module
   supports either via configuration, but only one is ever active.
2. **Correlation-aware whole-book cap.** The 75% stake-sum cap is replaced by a
   scenario-loss constraint consuming the canonical `exposure_corr` scoreline matrix +
   cross-fixture convolution: compute the joint worst-case (and P5) book P&L across all
   open bets plus the candidate bet; a candidate is admissible only if the post-bet
   worst case respects the floor (next item). This implements the standing rule that
   sizing must combine ALL bets and treat worst-case as the binding constraint, and it
   automatically sizes totals/BTTS/1X2 on the same fixture as ONE exposure.
3. **Hard cash floor as an inviolable constraint.** A configured floor (per pool, e.g.
   GBP 1,500 / USD equivalent) such that `pool_capital + worst_case_pnl ≥ floor` always
   holds. The floor is not a warning; `bankroll` refuses to return a stake that
   violates it.
4. **Explicit FX line for aggregate reporting.** Cross-pool aggregates are produced
   only by the reporting layer with an explicit `GBP_USD` rate line — never inside
   sizing.

### 4.2 Derive `TradeConfig` caps FROM bankroll (kill the decoupling)

Today `pm/trader.py:241-259` hardcodes `dry_run=True, max_order_usd=30 (BUY),
max_cashout_usd_per_order=100, max_daily_usd=100` with ZERO `bankroll.py` imports.

Design:
- **Caps stay static, versioned, fail-closed constants** — changed ONLY by a
  human-approved PR at the stage gates below, never computed at runtime.
  *(Amended 2026-07-02 after external review: the original design derived
  `max_order_usd`/`max_daily_usd` from the live pool via
  `TradeConfig.from_bankroll`. Phase 0 itself shows why that is wrong — the
  bankroll inputs can be silently bad (FX-blended `summary()`, §7.a; forked
  ledger history), and the safety ceiling must not be a function of a
  possibly-wrong live number.)* `bankroll.py` instead computes a RECOMMENDED
  cap (`min(per_bet_frac × pool_usd, ABSOLUTE_ORDER_CEILING)` etc.) that is
  surfaced next to the effective static cap in `/ping` and each proposal DM;
  drift between recommended and effective prompts a human to propose a stage
  raise — never an automatic change. The guardrail stack order in
  `place_order` (`trader.py:932`: funder-class refusal → per-order cap →
  keyword allowlist → daily cap) is unchanged.
- **Absolute ceilings raised only in stages, each with user sign-off:**
  Stage 0 (today): $30 order / $100 daily. Stage 1: proposed $75 / $250, gated on ≥50
  settled PM bets with non-negative aggregate execution CLV since Stage 0. Stage 2:
  proposed $150 / $500, gated on a further review. Numbers are proposals; the user sets
  them at each gate.
- **Kill-switch:** a `/halt` admin command persists a halt flag (DB table) that
  `place_order` checks before anything else and that forces dry-run everywhere;
  `/resume` (admin-only) clears it. The flag lives server-side so it survives bot
  restarts.
- Close the two documented allowlist skips while here: gate de-risk exits by token
  ownership instead of skipping the keyword check, and refuse live orders when
  `market_question is None` rather than skipping the allowlist
  (`trader.py:1013-1017`).

### 4.3 FX-correct reporting (the counterpart)

`reports.summary()` (`reports.py:545-633`) blends GBP+USD stakes/P&L into single
scalars while `_platform_currency` sits unused in the SAME file (`reports.py:200-203`).
Fix: `summary()` splits by currency (per-currency `total_staked/total_pl/roi/bankroll`),
with an optional combined view carrying an explicit FX line at `bankroll.GBP_USD`. The
existing `totals_by_currency` builders (`dashboard.py:222-234`, `sitedata.py:569-736`)
consume the same split. This ships in increment 4, before any sizing scale-up, so the
pool P&L feeds in 4.1 are real numbers.

---

## 5. Ranked exotic-FOOTBALL-market expansion

Universal shipping requirements — **every market entry ships with all four**:
(1) a settlement rule doc (explicitly stating 90-minute vs advancement basis),
(2) CLV capture wired for that market, (3) a sizing correlation entry in the
`exposure_corr` matrix, (4) a user sign-off gate before the first staked bet.

Hard prerequisite for Tier 1: **totals/BTTS CLV capture**. `closecapture.py:13-15` is
1X2-only today — totals/BTTS closes exist in `odds_snapshots` but are never stamped, so
Tier-1 markets would be flying without the primary KPI. Extend closecapture's market
matcher to totals/BTTS before the first Tier-1 bet.

| Market | Edge source | Venue + liquidity | Settlement cleanliness | Correlation with existing book | Verdict |
|---|---|---|---|---|---|
| **T1 — O/U totals ladder (1.5/2.5/3.5)** | same DC lambdas (`scores.py:270`) + xG/market level anchor (3.a/3.b); captured totals rows [UNVERIFIED-MINI: ~427k per 2026-07-01 snapshot] finally consumed | UK books + Smarkets (0% outrights logic n/a; 3% margin rule applies); deepest exotic market there is | CLEAN: objective, 90-min | HIGH with 1X2 + BTTS on same fixture — **sized as ONE exposure** via the scoreline matrix | **TRADE first** (after totals CLV capture) |
| **T1 — BTTS** | same matrix (`btts_from_matrix`, `scores.py:284`) | UK books; good liquidity | CLEAN: objective, 90-min | HIGH with totals (same lambdas) — one exposure with totals+1X2 | **TRADE with totals** |
| **T2 — Team totals** | per-side DC lambdas | UK books; thinner | CLEAN, 90-min | overlaps totals/1X2 | PILOT after T1 settles ≥25 bets |
| **T2 — Asian handicap (half-lines only)** | DC supremacy (invariant under the level anchor — a genuine strength) | exchange READ prices (Smarkets v3, Betfair read relay) + books; quarter-lines excluded (split-settlement mess) | half-lines CLEAN, 90-min; quarter-lines NOT | effectively a 1X2 transform | MONITOR via exchange reads; execution only if Smarkets execution is ever built (section 8) |
| **T2 — Double chance / DNB** | pure 1X2 derivatives — zero new model | UK books | DNB void-on-draw needs its own settle rule; otherwise clean | perfectly correlated with 1X2 | use ONLY when the derived price beats the line-shopped 1X2; a pricing identity in `pricing/`, not a new market |
| **T3 — PM advancement term structure** | CLOB tick/term-structure analytics (3.d) + advancement sim; advancement feeds go stale within days — mandatory re-run before acting | Polymarket CLOB; depth varies wildly — **entry permitted only where persisted book depth supports a bid-side exit** | TRAP: advancement includes ET + penalties; never hedge against a 90-min book price without the basis documented | correlated with futures/outright book | TRADE selectively, depth-gated, after CLOB capture has ≥2 weeks of book data |
| **T3 — Group winner / top-2** | advancement sim + PM prices | Polymarket; moderate depth | clean-ish (group standings rules documented) | correlated with advancement + match books | TRADE selectively, same depth gate |
| **EXCLUDED — Correct score** | — | — | — | — | research-killed: own-punt attribution −73.9%; permanently out |
| **EXCLUDED — Scorer props** | — | — | — | — | research-killed (PM longshots 0/20); xG shares are display priors only (3.a.5) |
| **EXCLUDED — SGM / same-game multiples** | — | — | — | — | research-killed; `/betbuilder` retirement recommended (2.4) |

90-minute-vs-advancement trap register (must appear in every settlement doc):
book 1X2/AH/totals/CS = 90 minutes; PM "advance"/qualify = includes ET/pens; knockout
"to qualify" ≠ "to win in 90"; DNB voids on draw; Betfair Match Odds is 90-min-only
(one reason execution there is not built).

---

## 6. Execution + ops hardening design

### 6.1 Telegram money-path hardening (ordered by risk closed per line changed)

1. **Gate `/settle` into `_MONEY_RE`.** `/settle` writes settlement to `bets` but is
   routed through un-gated dispatch (`app.py:2643-2644`); the money regex
   (`app.py:204-211`) covers only yes/no(+tags), Y/N BET-/PM-, REDEEM. Any authorized
   chat member can settle today. Add `/settle` (and any future ledger-writing slash
   command) to the money gate.
2. **Refuse to start when `TELEGRAM_ADMIN_USER_ID` is unset.** `_is_admin`
   (`app.py:218-227`) returns True for everyone when unset; startup only warns
   (`app.py:2672`). Change to hard refusal. Deploy caution: mini `.env` presence of the
   var is [UNVERIFIED-MINI: believed set]; verify on the mini BEFORE shipping or the
   bot goes down on restart.
3. **Fix the `PM_DRY_RUN` empty-string parse.** `app.py:1805-1807` treats an exported
   empty string as LIVE. Empty/unset → dry-run, full stop; only literal `0/false/no`
   arms live.
4. **Host-gate live fire.** Zero hostname checks exist repo-wide. `_pm_dry_run()` and a
   belt-and-braces check inside `ClobTrader.place_order` honour `PM_DRY_RUN=0` only
   when `socket.gethostname()` matches `WCA_LIVE_HOST` (default the mini's hostname);
   otherwise force dry-run and alert loudly.
5. **Wire the `Y/N BET-<id>` stub** (`app.py:2281-2284`) to a real `record_bet` write
   with the same provenance tags as the photo path.
6. **Kill-switch** `/halt` / `/resume` (defined in 4.2).

### 6.2 Richer confirmations

- **Pre-trade card** (before `Y PM-n` is accepted): top-of-book bid/ask + sizes from
  the CLOB `/book` (already fetchable, `pm_clob_history.py:38-75`), expected fill/
  slippage at proposed size, resulting per-fixture and whole-book exposure from
  `exposure_corr`, and remaining daily budget from `pm_order_log`.
- **Post-fill card:** filled size, average price, tx/order id (hash), updated position
  and pool P&L. The `LiveOrderUnconfirmed` alert path (`trader.py:168-201`,
  `app.py:2164-2186`) is unchanged — alerts, never retries.

### 6.3 Dev-box de-fang

The dev box today: no `.env.dev`; `.env` holds `PM_DRY_RUN=0` + THREE PM signing keys
(POLYMARKET_, PM1_, PM2_) + 7 BETFAIR_* + SMARKETS creds; no `WCA_DB*` line, so ~15
scripts default silently to the stale 723 MB `data/wca.db` (77 bets, frozen 06-25). A
hand-run script here fires LIVE against a stale ledger.

1. Ship a real `.env.dev` (`PM_DRY_RUN=1`, `WCA_DB_PATH=data/dev.db`, NO signing keys)
   and flip non-mini script defaults to it (the conductor already shows the pattern —
   `scripts/wca_conductor.py:1045` defaults `--env .env.dev`).
2. **Remove all signing keys and venue credentials from the dev box** (operational
   step, not a PR): execution is the mini's job under the operating model. Optionally
   rotate the PM keys after removal.
3. Quarantine/delete the stale dev `data/wca.db` (after the off-box replication of the
   mini copy exists, increment 4, so no one ever "restores" from the wrong file).
4. Extend the conductor's wca.db refusal (`scripts/wca_conductor.py:1057-1062`) to fire
   when `WCA_DB_PATH` is UNSET too — today it is vacuous exactly in the dev box's
   actual state.

### 6.4 Environment + transport unification

- **Unify on `WCA_DB_PATH`** (decision recorded). Three names exist today: `WCA_DB`
  (`bot/app.py:1625`, `wca_override.py:37`, `wca_tracking.py:22` — dead),
  `WCA_DB_PATH` (conductor/config.py:22, `wca_conductor.py:1058`,
  `wca_positions_sync.py:57`), `WCA_MINI_DB` (`wca_place_server.py:57`). Readers of the
  legacy names keep a one-release fallback that logs a deprecation warning.
- **Collapse the fire-path SSH hops:** `wca_place_server.py` moves to the mini as a
  launchd service, still bound to `127.0.0.1:8010`, invoking `wca_pm_fire.py` locally
  (no SSH). The dev-box browser reaches it via a single SSH local port-forward. This
  removes the dev-shell env forwarding (the current server forwards ITS OWN
  `PM_DRY_RUN` to the mini — a `.env`-sourced dev shell forwards `0`), and reduces the
  three-places-`PM_DRY_RUN`-must-agree problem to one place. Existing guards (loopback
  bind, fail-closed token, hostname-inert public page `arb.js:135-140`, `pm_fire_log`
  idempotency, `ABSOLUTE_MAX_USD=100`) all keep.

### 6.5 Typed command framework (design sketch only)

Replace the string-match if-chain dispatcher (`dispatch`, `app.py:2602`; the chain
itself is ~35 lines at `app.py:2615-2649`, inside the 2,808-line `app.py`) with a
command registry: `@command(name, args_schema, money: bool, admin_only: bool)` decorating
handlers; the run-loop resolves commands from the registry, enforces the money/admin
flags centrally (retiring `_MONEY_RE` as the single point of gating truth), and
generates `/help` + the Telegram menu from the registry. Long-poll transport
(`bot/telegram.py`) is unchanged. This is increment-9 work; the point of the sketch is
that gating becomes declarative-per-command instead of regex-adjacent.

---

## 7. Ledger + deploy + sites target

### 7.a Ledger evolution (strict sequence — cheap wins first)

1. **Off-box replication.** The mini's 6-hourly archive job already snapshots wca.db
   (`install.sh:49`); `CloudConfig.from_env` (`archive/config.py:53-66`) mirrors to
   S3/R2 only if `WCA_ARCHIVE_S3_*` creds are set in the mini `.env`
   [UNVERIFIED-MINI: creds presence unknown]. Step 1 = set the creds and VERIFY a
   restore (zero code). Step 2 (durable) = a dedicated continuous backup (litestream or
   an explicit `.backup`-then-upload job) so the single-point-of-failure closes
   properly. The CI archive.yml stays `--no-ledger` (correct: the repo is the wrong
   place for the ledger, `.gitignore:11`).
2. **FX-correct `summary()`** (4.3).
3. **Retire the Notion mirror.** `ledger/notion_diff.py` + `scripts/archive/
   wca_notion_ledger_diff.py` deleted; the operating docs stop referring to Notion as a
   ledger copy (it drifted past ~#236 and has no auto-sync;
   `wca_pm_fire.py:322-331` already documents the absence). Off-box replication (step
   1) is the real second copy.
4. **Close the write bypasses.** Direct `INSERT INTO bets` at `wca_pm_watch.py:121` and
   `wca_pm_reconcile.py:237` (apply mode) route through `record_bet` (or a new
   low-level `insert_bet_row` that enforces `canon_platform` and is the ONLY sanctioned
   raw insert); `wca_import_polymarket.py:112` is deleted with the dead scripts.
   `_insert_cashed_slice` (`store.py:752`) stays internal but adopts the same helper.
5. **Then the real overhaul — typed, event-sourced store** (increment 9):
   - migration framework: `PRAGMA user_version` + a `migrations/` directory; the six
     lazy ALTERs (store.py:294-340) and the doubly-defined `manual_override`
     (base DDL `store.py:91` AND `_ensure_manual_override_column`) collapse into
     migration 001; the hand-copied DDL stub in `predledger/store.py:195-196` is
     replaced by an import;
   - `fixtures` table + `match_id` becomes a real FK (exposure stops relying on
     fuzzy team-name matching);
   - append-only `bet_events` with `bets` as a projection — settle/void/cashout/
     close-stamp become events; merge of forked ledgers becomes a union of
     id-namespaced event logs (structurally eliminates the dev/mini divergence class);
   - **dual-write shadow period:** events are written alongside the existing mutators
     for ≥2 weeks; a nightly job proves projection == table before any reader switches;
     cutover has its own sign-off and a same-day rollback (stop writing events, table
     was never wrong).

### 7.b Deploy

1. **Artifact ownership — recommendation: untrack + single-committer (not a data
   branch).** Root cause of the autopull wedge: tracked files written by mini jobs and
   committed by CI (or by nothing). Per-file disposition:
   - `data/goalscorers_latest.md`, `data/model_predictions_log.jsonl`: UNTRACK (move
     under an ignored `data/local/`); they are consumed locally and by the archive job,
     not by the sites; CI stops committing them (`daily-card.yml:119-120` trimmed).
   - `data/card_latest.md` / `next_latest.md` / `model_predictions.json`: ONE
     committer. Recommendation: the mini publish job (it has the real ledger); CI
     daily-card keeps building them for its own steps but stops committing them.
   - `site-analytics/data/*.json` (16 tracked, committed by NOTHING): resolved by the
     site consolidation (7.c) — the tree is retired; interim, publish stages the whole
     feed dir (7.c.3) so nothing tracked is ever left permanently dirty.
   The detached-data-branch alternative (mini commits data to a `data` branch, main
   stays code-only) is rejected: it doubles the branch surface the day after a 98-branch
   cleanup and splits what the serving surfaces read across two refs (when recorded
   this meant the two Vercel builds; Vercel was removed 2026-07-08 — it now means the
   localhost 8000/8001 servers). Untracking is smaller,
   testable, and reversible per-file.
2. **Single installer.** Delete `deploy/install_services.sh` AND `deploy/sync.sh` (the
   legacy 5-min sync destroys fresh daemon artifacts: `git checkout -- .` + stash drop,
   `sync.sh:42-48`). `deploy/macmini/install.sh` becomes the only installer and gains
   an uninstall pass for the legacy labels (`com.wca.sync`, `com.wca.build_card`, …) so
   one re-run purges the old generation regardless of which set is currently loaded
   [UNVERIFIED-MINI: which generation is live]. `deploy/README.md:71-81` rewritten to
   document the current set.
3. **Watchdog upgrade.** `watchdog.sh` today checks daemon liveness only (zero git
   commands). Add: behind-count check (`git fetch --quiet && git rev-list --count
   HEAD..origin/main`) with a Telegram alert at ≥3 behind; rebase-abort loop detection
   (recent "rebase conflict" lines in `logs/autopull.log`); `autopull.sh:15-19` itself
   stays conservative (abort + exit 0) but is no longer silent.
4. **Make pytest a real gate — options, stated honestly.** Branch protection is
   unavailable today (`gh api .../protection` → 403; free-plan private repo), so the
   gate is advisory-only. Options: (a) GitHub Pro (cheapest deterministic protection on
   a private repo); (b) make the repo public — NOTE the contradiction to resolve:
   `deploy/macmini/backup.sh:1-3` already claims "the repo is public" while the API
   says private — betting-strategy privacy is a user call, not an engineering one;
   (c) a merge-queue convention: the conductor refuses to merge any PR whose head
   lacks a green pytest run (enforced in `conductor/runner.py`, applies only to
   bot-driven merges). Recommendation: (a) now, (c) as belt-and-braces; (b) only if the
   user actively wants it. Note the residual: `GITHUB_TOKEN` data pushes never trigger
   pytest — acceptable once the landmine tests are gone (increment 0).
5. **De-landmine the data-coupled tests (increment 0).** Replace shipped-artifact
   assertions with fixtures the tests build themselves: `test_betrecs_open_exposure.py:91`
   (drops the `n_open == 8` pin on the daemon-updated `site/bet_recs.json` — the value
   oscillates 0↔77 by committer), `test_arbfx.py:222` (`arbs == []` → schema assert),
   `test_advancement.py:74` and `test_build_wc2026_results.py:67` (frozen fixture CSV,
   not the thrice-daily-rewritten `martj42_cleaned.csv`), `test_scorers.py:149`
   (fixture players.json).

### 7.c Sites — consolidate to ONE lilac-shaped terminal

Today: 19,334 LOC across three trees; site-analytics feeds frozen on main since 06-25
because `publish_site.sh:44-47` copies feeds in but `:57-61` never stages them and no
workflow touches the tree; `terminal.html` is orphaned (builder `wca_terminal.py` does
not exist); `visuals.html`/`tracking.html` are 19-line redirect stubs; two feeds have
no generator at all (`site-analytics/data/mc_futures.json`,
`site/microstructure/index.json` — the only feed the microstructure page fetches).

1. **One site.** `site/` keeps its serving slot (localhost:8000 — the hosted Vercel
   URL was retired with Vercel's removal, 2026-07-08) and adopts the lilac shape: a
   single tabbed terminal (`site-lilac/` is the proven prototype). Analytics panels
   (the 13 feeds `analytics.js:1049-1063` fetches) and microstructure become tabs.
   Retired:
   `site-analytics/` tree (its Vercel project is already gone), `site-lilac/` as a separate tree (its
   template becomes the site), `terminal.html`, `visuals.html`, `tracking.html`,
   `benchmarks_data.json` (dead generator) — and `mc_futures.json` unless its generator
   is resurrected (user call: the docs reference a `wca_mc_futures.py` that does not
   exist on origin/main).
2. **One feed contract.** Every feed carries top-level `generated_at` +
   `schema_version` (feeds already carry `meta.generated` — the contract makes it
   uniform and machine-checkable); a publish-time validator refuses to stage a feed
   missing them.
3. **One publisher.** `publish_site.sh` is replaced by a manifest-driven publisher that
   regenerates from a single feed list and stages the WHOLE feed directory
   (`git add site/feeds/`) — the stage-list/copy-list mismatch class (the exact freeze
   bug) becomes unrepresentable. CI workflows call the same publisher entry point with
   a scope flag instead of maintaining their own `git add` lists.
4. **Per-feed freshness badges.** The UI renders age from `generated_at` and badges
   stale feeds (>2× expected cadence) — a 10-day-old panel can never again look live.
5. **Real-time tier.** Positions / bet recs / line-move only: short-poll (15–30 s) of
   small JSONs. Short-poll over SSE deliberately: the site is plain static-file
   serving with no SSE server (localhost 8000/8001 only since Vercel's 2026-07-08
   removal), so short-poll works with zero infra. Everything else stays batch
   (30-min publish).

---

## 8. Betfair decision (RECORDED)

**Keep Betfair read-only. Do not build Betfair execution.** Evidence: the execution
stub raises intentionally (`data/betfair.py:71`); `betfair_exchange.py` (727 lines) is
read-RPCs only; repo-wide grep for `placeOrders/cancelOrders/replaceOrders/
PlaceInstruction/limitOrder/persistenceType` = zero hits. The costs (execution client
from scratch, cert provisioning, ~£499 live-key gate, 2026 Passive Bet Delay) buy only
markets the project's own research killed or 90-min-settlement traps. **If a GBP
exchange execution venue is ever wanted, Smarkets is first** (stub seam ready at
`data/smarkets.py:344`; native v3 read support with back/lay/depth already wired).
Optional cheap win, not required by any increment: a MacBook/VPN relay for the Betfair
READ feed as a sharp-book input to the 3.c consensus and a CLV reference
[UNVERIFIED-MINI: mini geo-block `SSL: WRONG_VERSION_NUMBER` per 07-01 snapshot].

---

## 9. THE SEQUENCED PLAN

Ordering logic: make the safety net real (0), remove the loaded guns (1), stop the
silent deploy failures (2), shrink the estate (3), protect the ledger (4), unify the
math (5), unlock the data in shadow (6), and only then touch sizing (7) and new markets
(8), with the big rewrites (9) last because everything before them de-risks them.

**Tournament overlay (added 2026-07-02, external-review adjudication).** The
dependency order above stands, but the calendar does not: the World Cup is live now,
and increments 3/5/9 would spend the remaining tournament window on estate work.
Calendar split — **tournament track:** 0, 1, 2 (they protect live trading), 4's
off-box replication, 6's CLOB capture + the pm_price_history stall fix (they serve
the PRIMARY advancement-futures objective), 8-T1's totals-CLV capture prerequisite,
plus a minimal scale-up path (staged raises of the static caps + the existing
`exposure_corr` whole-book check, human-gated per §4.2) without waiting for the full
§4 framework. **Post-tournament track:** 3 (branch/worktree/dead-script cleanup),
5 (module re-layout), the full §4 framework of increment 7, and 9 (rewrites). All
sign-off gates unchanged.

| # | Increment | Contents | Prerequisite | Risk | Rollback | Gate |
|---|---|---|---|---|---|---|
| 0 | Green the suite + de-landmine tests | Fix `test_betrecs_open_exposure.py:91` + the 4 landmines (7.b.5); fixtures not shipped artifacts; zero production code | none | Minimal — tests only. Risk note: fixtures must not weaken real regression coverage; each keeps a structural assert | revert PR | none (explicitly allowed pre-sign-off) |
| 1 | Dev-box de-fang + admin-gate hardening | `.env.dev`; strip keys from dev box; host-gate `_pm_dry_run` + `place_order`; empty-string parse fix; refuse-start on unset admin id; `/settle` into `_MONEY_RE`; `WCA_DB_PATH` unification; conductor guard fires when unset | 0 | Touches security posture; intended zero mini behavior change — BUT refuse-start could down the bot if the mini admin id is unset [UNVERIFIED-MINI]; verify mini `.env` first. Host-gate mistyping could dry-run the mini: ship with the mini hostname pre-verified over SSH | revert PR; keys restorable from user's password manager (never from git) | SIGN-OFF (security posture) |
| 2 | Deploy structural fix | Delete legacy installer + `sync.sh`; installer purges legacy labels; artifact untrack/single-committer; watchdog git-behind + rebase-abort alerts; pytest-gate route chosen (7.b.4) | 0 | Touches mini services; a bad installer pass could stop scheduled jobs — mitigated by running install on the mini during a match-free window with a pre-taken launchd state dump | reinstall from `pre-overhaul-2026-07-01` tag's installer | SIGN-OFF (mini services) |
| 3 | Org cleanup | Delete 22 dead scripts + archive `wca_validation_report`; `scripts/` tiering + MANIFEST + CI manifest check; delete `bench/`; branch triage EXECUTION (prune 6 merged + 41 fully-superseded remotes, 14 merged locals; tag branch tips `archive/<name>` before deletion); worktree consolidation to the single repo `worktrees/` root (remove the 13 tmp-rooted [incl. the 9 BTC-STRC-scratchpad checkouts and 3 of the 6 detached], the 2 `.codex` detached, `~/wca-positions`, and the 7 worktrees pinning PRUNE-SUPERSEDED branches — those pins block the branch deletions in this same increment; ~11-14 GB reclaimed, ~20 GB only if `.claude/worktrees` goes too); `/betbuilder` retire-or-schedule decision | 0 | Low — every deletion is verified zero-reference; branches remain recoverable via archive tags. The 51 REVIEW branches and ~17 unmerged local-only branches are NOT deleted here — separate reviewed batches | archive tags + reflog | SIGN-OFF (bulk deletion) |
| 4 | Ledger cheap wins | Off-box replication (creds + verified restore, then continuous backup); FX-correct `summary()`; Notion retirement; bypass closure (7.a.4); THEN delete the stale dev wca.db | 1 (dev box safe first) | Money-adjacent code paths (`record_bet` routing); mitigated by the existing 36-test ledger suite + new tests per bypass | revert PR; ledger file untouched by all of it | SIGN-OFF (ledger code) |
| 5 | Analytics consolidation + module re-layout | Canonical CLV formula/devig/exposure_corr/intel.arb (2.3); delete legacy `_portfolio_scenarios`; venue renames; package moves (2.1/2.2), one package per PR | 0; ideally after 3 | Broad import churn — the increment most likely to break feeds; per-package PRs, pytest each, feed-diff spot checks (feed values must be identical before/after for pure moves) | revert individual package PR | SIGN-OFF (touches live feed builders) |
| 6 | Data unlocks — SHADOW MODE | `wca_clobd` daemon + `pm_ticks.db` + backfill; pm-snapshot.yml stall fix; StatsBomb weekly build; xG anchor + totals prior + sharp-book weighting as shadow candidates in clvbench; F7 tracking-only wire; F9 wire-or-delete executed | 2 (services), 5 (canonical devig) | New mini jobs + API load (CLOB rate limits — daemon backs off); ZERO staking change by construction (everything dual-written and compared) | stop daemons; revert PRs; incumbent constants untouched | SIGN-OFF (new mini services); staking flips are increment 7/8 gates |
| 7 | Sizing scale-up | bankroll.py per-pool + correlation cap + hard floor (4.1); static caps + bankroll-computed recommendations + kill-switch (4.2, amended 2026-07-02); staged ceiling raises; resolve the per-pool vs global-PM-rule conflict | 4 (FX P&L feeds), 5 (exposure_corr canonical), 6 (shadow evidence) | HIGHEST — this changes real stake sizes. Staged: framework lands with Stage-0 ceilings (numerically identical to today), then each raise is its own sign-off | flip ceilings back to Stage 0 (config, not code); revert PR | **HARD SIGN-OFF** (per stage) |
| 8 | Exotic markets, tier by tier | Totals/BTTS CLV capture in closecapture → T1 totals+BTTS live (ONE exposure/fixture) → T2 team totals pilot, DC/DNB pricing identity, AH monitoring → T3 PM term-structure + group markets, depth-gated | 6 (anchors + CLOB depth), 7 (correlation sizing) | Each market = new settlement surface; the four shipping requirements (section 5) are mandatory per market; T1 first because settlement is cleanest and the model already prices it | per-market disable (stop proposing); revert PR | SIGN-OFF per market tier |
| 9 | Big rewrites | Event-sourced ledger with dual-write shadow + migration framework + fixtures FK (7.a.5); ONE lilac-shaped site + one publisher + freshness badges + real-time tier (7.c); Telegram v2 typed commands + richer confirmation cards (6.2/6.5) | 4, 5; site rewrite benefits from 2 | Large units — each lands behind a shadow/parallel period (ledger: projection-equality proof; site: old tree serves until parity checklist passes; bot: command registry runs behind the existing dispatcher first) | ledger: stop event writes (table authoritative throughout shadow); site: re-point the localhost servers at the old tree; bot: revert PR | SIGN-OFF each |
| 10 | Betfair standing decision | No build. Record section 8 in ARCHITECTURE.md; optional MacBook VPN read-relay as a 3.c input if the user wants it | — | none | — | none (decision already recorded) |

**STOP. Nothing beyond increment 0 and documentation merges proceeds without explicit
user sign-off on this plan. Increments 1, 2, 3, 4, 5, 6 and 9 each require their own
sign-off at the gates marked above; increment 7 requires a hard sign-off per stage;
increment 8 requires a sign-off per market tier. Rollback baseline for everything:
`pre-overhaul-2026-07-01`.**

---

## Appendix A — Brief-drift log

Standing rule: **where the overhaul brief (`docs/FABLE_OVERHAUL_PROMPT.md`) and the
Phase-0 evidence differ, Phase-0 wins.** This register lists every brief claim that
Phase 0 falsified, so no stale claim is ever re-imported from the brief.

| Brief claim (FABLE_OVERHAUL_PROMPT.md) | Phase-0 finding | Falsified by |
|---|---|---|
| ~50 loose top-level `src/wca` modules (:51, :163) | 39 loose modules (40 files incl. `__init__.py`) | phase0 duplication census (module count table) |
| launchd job count implied 22 (the "+ autopull + publish + watchdog" phrasing double-counts them as extras) | 19 launchd-wired jobs: 5 daemons + 14 interval jobs, with autopull/publish/watchdog among the 14 | phase0 deploy/CI report (services.env ground truth) |
| `microstructure/` = 13 scripts (:54) | 11 microstructure scripts | phase0 scripts manifest (operator-manual 38 = 27 + 11) + sites report |
| CLV computed ~8 divergent ways, "high risk of divergent CLV math" (:52) | the four production writers already share one consistent formula; the only true duplicate is the UNWIRED `bench/report.py` (plus the microstructure Shin-basis discrepancy) — consolidation is hygiene, not a live divergence (2.3) | phase0 duplication report |
| `/movers` in the bot's read-only command list (:98) | no `/movers` command exists in `app.py` (zero grep hits) | phase0 telegram/execution report |
| dev box "on `feat/paper-testbook-pm-analytics` with dozens of modified files" (:11) | dev box is on a CLEAN `main` (4 behind); the branch exists locally but is checked out nowhere | phase0 dev-box footgun report §6 |
| `card.py build_event_references` "IS live" for correlated exposure (:53; also `02_codebase_audit.md:161`) | zero production callers — it is the third dark surface, F9 (2.5); the live correlated-exposure path is `exposure.py event_bets` | phase0 dark-features report |
| site-analytics = 17 feeds (:78) | 16 tracked JSON in `site-analytics/data/` | phase0 sites report §1.2 |
| dead-code delete list includes `wca_backfill_*`, `wca_canon_venues`, `wca_pm_probe`, `wca_validation_report` (:51) | `wca_backfill_accounts` + `wca_canon_venues` are importlib-executed by tests and `wca_pm_probe` is cited by live bot code (`app.py:2099`) — all three RESCUED (2.4); `wca_validation_report` is archived, not deleted | phase0 scripts manifest |

---

## Appendix B — External-review adjudication (2026-07-02)

An independent review (GPT 5.5) of the overhaul charter was adjudicated against the
Phase-0 evidence; these amendments were adopted. Everything else in this document
stands as committed.

1. **§4.2 amended in place** — execution caps are static, versioned, human-changed
   constants; `bankroll.py` only computes a surfaced recommendation. Increment 7's
   table row updated to match.
2. **§9 tournament overlay added** — tournament vs post-tournament tracks; gates
   unchanged.
3. **Phase-2 implementation agents follow `AGENTS.md`** (repo root) operating rules:
   one feature per task, duplicate pre-flight, file-overlap serialization, green tree
   before push, sequential by default. Phase-0's parallel read-only research sat
   outside those rules' failure domain (no PRs, no shared worktrees); implementation
   does not.
4. **Production-access rule:** no subagent ever receives SSH access or production
   credentials. Live-mini facts enter the docs only via a human-run, read-only
   sanitized snapshot (ARCHITECTURE.md Appendix A is the command list; increment 2
   ships it as a script whose output is committed as evidence).
5. **Incident-runbook rule:** the historical mini recovery (`git reset --hard
   origin/main` + kickstart) is a HUMAN-ONLY runbook step and gains a mandatory
   pre-step — archive the dirty tracked artifacts first (e.g. tar the files
   `git status --porcelain` lists), since reset destroys them. Agents never run it.
   Increment 2's structural fix removes the recurring need for it.
6. **Quant scope additions to §3** (all shadow-mode, same CLV/calibration gates):
   (a) an as-of full-slate prediction ledger — persist model probabilities for every
   offered outcome including passed/skipped bets, extending the existing predledger,
   so skip-value attribution stays measurable; (b) an explicit extra-time/penalties
   goal-rate model for knockout settlement surfaces (shootout WINNERS are anchored
   today via the runtime-downloaded shootouts.csv, `results.py:88`; extra-time
   scoring rates are not modeled anywhere in the audited code); (c) executable-EV
   framing for all CLOB models — depth, spread, trade flow, fill probability,
   adverse selection; mid-price momentum alone is not an acceptance basis. Polling
   (1-min prices-history + periodic `/book`) suffices for the shadow models; a
   WebSocket feed is justified only if fill-probability models graduate. Queue
   position is NOT publicly exposed by the CLOB (ARCHITECTURE.md §9.4 correction) —
   no model may assume it.
7. **ADRs recorded** in `docs/overhaul/ADRS.md` for the three largest calls (ledger
   evolution, site consolidation, Betfair no-build) with alternatives and rejection
   criteria: the event-sourced ledger and single-site consolidation are PROPOSED
   (decided at their increment-9 gates); Betfair no-build is ACCEPTED.
8. **`docs/ARCHITECTURE.md` superseded** — it predated Phase 0 and was missed by the
   sweep (a Phase-0 miss, caught by the review); it is now a redirect stub to the
   root `ARCHITECTURE.md`, and `docs/architecture/SYSTEM_MAP.md` carries an as-of
   warning.
