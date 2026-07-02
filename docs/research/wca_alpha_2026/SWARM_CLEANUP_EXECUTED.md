# Swarm cleanup — EXECUTED (2026-06-30)

Companion to `SWARM_LEDGER.md` (the plan). This records what was actually run.

## Retired
- **Branches: 167 → 82.** Deleted 52 merged-into-main branches (commits already in main),
  14 dead/duplicate unmerged branches, and 19 archive-disposition branches (history
  preserved as `archive/<name>` tags — `git tag -l 'archive/*'`). Included the xG model
  twin `feat/dc-goal-supply-recalibration` (canonical = `harden/xg-totals`).
- **Worktrees: 36 → 25.** Removed 11 dead/merged worktrees (clean or regenerable-data only).

## Left intact (surfaced, not deleted)
- **4 merged-branch worktrees with uncommitted CODE** (not regenerable): `worktrees/report-send`
  (bot/app, telegram), `worktrees/llw` (accas, bot/app), `worktrees/pm-tables` (sitedata.py),
  `.claude/worktrees/awesome-perlman-23e908` (predledger/store.py). Review then remove.
- **All active sessions** (Section 6 of the ledger) and the **dirty `main` working tree**.
- **3 detached-HEAD worktrees** (codex x2, wca-positions) — verify-then-remove manually.

## Integration branches (off main; partial — far-behind branches conflict)
- `integrate/polymarket`: merged PM-trader (`make-pol-ab0d92`) + `fix/pm-bets-table-display`.
  Pending (conflict, hand-resolve): `feat/pm-price-history-outright-edge`,
  `claude/jovial-northcutt-6d30cf` (cashout, trader.py), plus active-session branches
  (`odds-source-betfair-pm`, `pm-1x2-snapshotter`, `dual-pool-kelly`) — merge from tips when idle.
- `integrate/microstructure`: merged early-response backtest (`build-a-0c7f27`).
  Pending (conflict): `feat/market-microstructure`, `fix/goalscorers-empty`, plus active
  (`microstructure-goal-calibration`, `card-surface-events`, `correlated-exposure-model`).

## Next safe actions
1. Resolve the four pending conflict-merges per direction (sessions idle) → finish the two integrations.
2. Land `harden/xg-totals` (canonical xG) into main via PR; then `integrate/microstructure` calibration reflects it.
3. Review + remove the 4 code-dirty worktrees and 3 detached worktrees.
4. Commit the untracked `docs/research/wca_alpha_2026/` dossier on main when ready.
