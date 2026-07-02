# Changelog

All notable changes to the World Cup Alpha platform are recorded here, per the
overhaul operating rules (`docs/FABLE_OVERHAUL_PROMPT.md` §0.6). Entries are
grouped by date; every entry states whether production / live-money behavior
changed.

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
