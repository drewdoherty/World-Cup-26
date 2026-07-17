# Changelog

All notable changes to the World Cup Alpha platform are recorded here, per the
overhaul operating rules (`docs/FABLE_OVERHAUL_PROMPT.md` §0.6). Entries are
grouped by date; every entry states whether production / live-money behavior
changed.

## 2026-07-17 - Codex-first documentation reconciliation (no behavior change)

**Production impact: none.** Documentation only; no code, data, site, ledger,
deployment, or live-money behavior changed by this reconciliation.

- Replaced the stale agent/conductor-only entry point with a Codex-first
  `AGENTS.md` that preserves real-money, secret, ledger, settlement, and
  MacBook/Mac-mini safety boundaries.
- Rewrote `README.md` and `ARCHITECTURE.md` around current model, selection,
  sizing, execution, event-forest, shadow-book, and cross-venue research paths.
- Added `docs/OPERATIONS.md` as the current two-machine runbook and
  `docs/CURRENT_STATE.md` as the dated home for tournament/runtime facts.
- Retired stale June setup plans, Betfair/Kalshi build proposals, hosted-site
  guidance, separate-bankroll descriptions, and historical live-position
  advice from the owned documentation set.
- Recorded the complete two-fixture event forest, the isolated multi-venue
  shadow book, and generic HL/PM dominance bounds. The shadow service is
  defined in the current branch but mini launchd activation was not verified;
  dominance code remains concurrent/untracked research.
- Replaced the June TODO with a verified closeout queue for the final two
  fixtures, shadow settlement, full-forest durability, dominance-fee research,
  and post-tournament simplification.

## 2026-07-02 to 2026-07-13 - tournament operating baseline (behavior changed)

**Production impact: yes, across model probabilities, selection/sizing, public
feeds, display, and safety gates.** This entry consolidates the verified merged
baseline that older continuation notes described across the combined-bankroll
lineage and PRs #171-#198.

- Centralized ranking and the no-cash floor in `src/wca/selection.py`:
  moneyline `p >= 0.50`, mid `0.25 <= p < 0.50`, longshot `p < 0.25`; match
  markets use EV within bucket while multi-week futures retain
  further-out-first ordering.
- Promoted shrink-to-market into the live card probability with
  `WCA_SHRINK_LIVE=0` as a reversible kill switch; preserved raw probabilities
  for calibration.
- Corrected staking to one combined GBP 3,000 plus total-realised-P&L bankroll,
  expressed in each venue currency at the fixed project FX rate and sized at
  quarter Kelly. Nested same-team advancement rungs became one correlated path
  exposure.
- Added complete PM event-market discovery, fair pricing for supported
  score-matrix families, honest market-only rows, governed recommendations,
  and a PM-blind no-clobber guard. Fixed Gamma discovery past the pagination
  ceiling on 2026-07-13.
- Added duplicate same-tie exposure detection across ordinary and event-market
  recommendation feeds.
- Added PM advancement price capture and CLV relay, park-only in-play proposal
  ingest, freshness/poison guards, and additive order/fill telemetry.
- Standardized operator display on percentages, explicit EV markers, trade
  terminology, `/matchevents`, and the `/card` watch tier. Wire identifiers and
  ledger table names remained unchanged.
- Removed hosted Vercel surfaces; localhost ports 8000 and 8001 became the only
  supported dashboards.
- Added the read-only Hyperliquid HIP-4 client and matched-settlement HL/PM
  monitor. It remained shadow-only with no execution path and an unresolved HL
  settlement fee.

## 2026-07-02 — Phase 0 amendments: external-review adjudication (no behavior change)

**Production impact: none.** Documentation only, same branch/PR as Phase 0.

- Adjudicated an independent review (GPT 5.5) against the Phase-0 evidence;
  adopted items recorded in `docs/overhaul/PHASE1_DESIGN.md` Appendix B.
- Execution caps redesigned to static, versioned, human-changed constants
  (never runtime-derived from bankroll); `PHASE1_DESIGN.md` §4.2 amended.
- Added the tournament vs post-tournament track split to the sequenced plan.
- Recorded ADRs (`docs/overhaul/ADRS.md`) for ledger evolution, site
  consolidation, and the Betfair no-build decision.
- Superseded the pre-existing `docs/ARCHITECTURE.md` (missed by the Phase-0
  sweep — caught by the review) with a redirect stub; stamped
  `docs/architecture/SYSTEM_MAP.md` with an as-of warning.
- Corrected ARCHITECTURE.md §9.4: CLOB queue position is not publicly exposed.

## 2026-07-02 — Phase 0: inventory & source-of-truth (no behavior change)

**Production impact: none.** Read-only audit; documentation only.

- Froze the rollback baseline: tag `pre-overhaul-2026-07-01` created at
  `origin/main` = `957112a` and pushed.
- Ran the Phase 0 verification sweep (33 read-only agents) against
  `origin/main`; live-mini SSH was not permitted this session, so mini-runtime
  facts are carried as `[UNVERIFIED-MINI]` with 2026-07-01 snapshot values.
- Authored `ARCHITECTURE.md` — current-state map, scripts manifest (110 files
  tiered; 23 verified dead, 3 of the brief's dead-candidates rescued as alive),
  duplication inventory with canonical decisions, dark features F7/F8/F9,
  operational hazards, and a brief-vs-verified drift log.
- Authored `docs/overhaul/BRANCH_WORKTREE_TRIAGE.md` — 98 remote branches
  (47 zero-risk deletions, 51 review), 96 local branches, 32 worktrees across
  six roots; **proposal only, nothing deleted**.
- Authored `docs/overhaul/PHASE1_DESIGN.md` — target architecture and the
  sequenced, gated implementation plan. **Nothing beyond documentation ships
  without user sign-off.**
