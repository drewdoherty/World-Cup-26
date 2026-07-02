# SWARM AUDIT LEDGER + CLEANUP PLAN

Generated 2026-06-30. Read-only audit of the World Cup Alpha swarm state.
Inputs: per-branch classification JSON (109 unmerged branches), `git worktree list` (36 worktrees), `git branch --merged main` (57 merged branches + `main`).

> **This document is a PLAN. Nothing here has been executed.** Section 5 is the ordered command checklist for the operator/next step to run. Section 6 is the strictly-untouchable list.

---

## 1. EXECUTIVE SUMMARY

### 1.1 Counts

**Unmerged branches (109) by classification:**

| Classification | Count |
|---|---|
| stale | 60 |
| active | 21 |
| duplicate | 18 |
| dead | 8 |
| **Total** | **107 in JSON** |

> Note: the JSON payload supplied for this audit enumerates **107** distinct branch records (not 109). The two-record gap is most plausibly the two integration scaffolds (`integrate/conductor-v0`, `integrate/swarm-fixes`) being counted once each here while the upstream count double-counted, or two records deduped before hand-off. Treat 107 as the audited set; the conclusions below cover every record present. No branch in the JSON is unaccounted for.

**Unmerged branches (107) by disposition:**

| Disposition | Count |
|---|---|
| merge-into-integration | 49 |
| keep-canonical | 11 |
| leave-active | 11 |
| archive | 18 |
| delete | 18 |
| **Total** | **107** |

**Merged branches:** **57** branches are merged into `main` (excluding `main` itself). These are **SAFE DELETES** — their commits already live in `main`, so deleting the branch ref loses nothing. They are listed for deletion in Section 5, Step 1.

**Worktrees:** **36** total (including the primary `main` checkout).

### 1.2 Headline duplications

**The xG / goal-supply cluster (the most important call).** Three branches sit in/around the goal-totals calibration space:

| Branch | Date | Files | Verdict |
|---|---|---|---|
| `harden/xg-totals` | 2026-06-30 | `src/wca/models/dixon_coles.py`, `src/wca/card.py`, `tests/test_dixon_coles.py` | **CANONICAL** |
| `feat/dc-goal-supply-recalibration` | 2026-06-29 | `src/wca/models/dixon_coles.py`, `card.py`, `dc_params_corrected.json`, `test_dixon_coles.py` | duplicate → fold/drop |
| `feat/microstructure-goal-calibration` | 2026-06-29 | `scripts/microstructure/goal_calibration.py`, `site/microstructure/goal_calibration.json` | **NOT a dup — distinct layer, keep active** |

**Recommendation: `harden/xg-totals` is the ONE canonical model-layer branch.** Reasoning:
1. It is the **newest** (06-30 vs 06-29) and is **checked out in a live worktree** (`wt-clean`), so it is the actively-maintained head of the line.
2. It touches the **same core files** as `feat/dc-goal-supply-recalibration` (`dixon_coles.py`, `card.py`, `test_dixon_coles.py`) — they are genuine duplicates at the model layer. `harden/xg-totals` carries the DC level-anchor work that supersedes the 06-29 recalibration; the older branch should be archived/dropped rather than merged in parallel (merging both guarantees conflicts in `dixon_coles.py`).
3. **Critically, `feat/microstructure-goal-calibration` is NOT a file-level duplicate** despite the shared theme. It only writes `scripts/microstructure/goal_calibration.py` and a JSON artifact — the *presentation / microstructure* layer, not the model. It can coexist with `harden/xg-totals` with near-zero conflict risk and is classified **active**. Do not delete it; it belongs to the microstructure integration line as a consumer of the calibrated model.

So: **one canonical model branch (`harden/xg-totals`)**, the 06-29 model twin archived, and the microstructure JSON branch kept as a separate active feature.

**Other notable duplication clusters:**

- **Conductor (largest cluster).** ~14 branches touch `conductor/*`. Canonical: **`feat/conductor-persistence-chat`** (06-26, adds persistence store, most advanced standalone). The hardening sub-line canonical is **`fix/conductor-codex-cap`** (06-22, ahead 9, superseding `fix/conductor-worktree-lock` + `fix/conductor-error-surfacing`). Integration canonical is **`integrate/swarm-fixes`** (06-23). Many v0/v1/control/health/live/imgpaste iterations are dead/duplicate.
- **Bet-Recs tab.** `conductor/claude-p0-t2-...-ab3148` and `conductor/claude-p0-t4-...-85d798` are **byte-identical** to each other and both superseded by **`conductor/claude-rebuild-bet-recs-as-the-action-d-4eee9a`** (06-28, canonical). Delete T2 and T4.
- **Prediction-ledger.** `conductor/claude-p0-t3-...-4f410a` (06-25, schema+settle) is canonical over `conductor/claude-p0-t1-...-f9bc7f` (schema only).
- **Accas.** `feat/accas-low-win` canonical over `feat/accas-rebuild`.
- **Venue canonicalization.** `codex/venue-canon` canonical over `codex/venue-override`.
- **Under-the-hood architecture.js.** `feat/under-the-hood-qa-2026-06-21` canonical over the byte-identical `feat/under-the-hood-findings`.
- **Vision/acca detection.** Live `fix/vision-bet365-detection` (06-30) supersedes `claude/nice-gauss-e40a19`.
- **CI pytest gate.** `fix/ci-pytest-gate` (worktree) supersedes `ci/pytest-gate`.

---

## 2. LEDGER TABLE (grouped by feature)

Feature | Branches | Canonical pick | Others disposition | Direction
---|---|---|---|---
conductor-core | feat/conductor-persistence-chat, feat/conductor, feat/conductor-control, feat/conductor-health-routing, feat/conductor-imgpaste, feat/conductor-live, feat/conductor-menu, feat/conductor-v0, feat/conductor-v1 | **feat/conductor-persistence-chat** | conductor/control/health/imgpaste/live/menu → archive; v0/v1 → delete (dead); feat/conductor → merge | conductor
conductor-hardening | fix/conductor-codex-cap, fix/conductor-error-surfacing, fix/conductor-worktree-lock | **fix/conductor-codex-cap** | error-surfacing → archive; worktree-lock → delete | conductor
conductor-integration | integrate/swarm-fixes, integrate/conductor-v0, codex/claude-first-routing | **integrate/swarm-fixes** | conductor-v0 → fold forward; claude-first-routing → merge | conductor
conductor-codex-cleanup | chore/codex-salvage, chore/codex-removal | **chore/codex-salvage** | codex-removal → archive | conductor
conductor-deploy | deploy/conductor-on-mini | deploy/conductor-on-mini (merge) | — | conductor
xg-total-goals-calibration | harden/xg-totals, feat/dc-goal-supply-recalibration | **harden/xg-totals** | dc-goal-supply-recalibration → fold/drop (model twin) | forecasting
goal-expectancy-calibration | feat/microstructure-goal-calibration | **feat/microstructure-goal-calibration** (active, distinct layer) | — | microstructure
matchevents-pipeline | harden/matchevents-pipeline | **harden/matchevents-pipeline** (active) | — | forecasting
elo-calibration | conductor/codex-...-make-thr-962e47 | **...make-thr-962e47** (merge) | — | forecasting
advancement-model | conductor/codex-...-add-a-qu-aea4fe, conductor/codex-...-market-a-9d5016 | ...add-a-qu-aea4fe (merge) | market-a-9d5016 → merge (sibling) | forecasting
players-db | feat/players-db-phase2 | **feat/players-db-phase2** (merge, 10 ahead) | — | forecasting
accas-rebuild | feat/accas-low-win, feat/accas-rebuild | **feat/accas-low-win** (active) | accas-rebuild → delete | forecasting
clv-close-capture | claude/serene-grothendieck-29a0fa | serene-grothendieck (merge) | — | forecasting
next-scorer-names | claude/interesting-kalam-9a581b | — | archive | forecasting
venue-benchmark | feat/model-vs-venue-benchmark | feat/model-vs-venue-benchmark (merge) | coordinate w/ pm-1x2-snapshotter | forecasting
modeling-benchmark | feat/modeling-benchmark-betbuilder | feat/modeling-benchmark-betbuilder (merge) | — | forecasting
card-operating-rules | feat/card-refactor-operating-rules | feat/card-refactor-operating-rules (merge, careful) | — | forecasting
bot-game-panel | feat/bot-game-panel | feat/bot-game-panel (merge) | — | forecasting
bot-fair-kelly | feat/bot-fair-kelly-display | **feat/bot-fair-kelly-display** (active) | — | forecasting
kelly-analysis-script | conductor/claude-...-full-b-abcb90 | — | archive | forecasting
market-anchored-advancement | feature/market-anchored-advancement | — | archive (314-file data churn) | forecasting
prediction-ledger | conductor/claude-p0-t3-...-4f410a, conductor/claude-p0-t1-...-f9bc7f, claude/adoring-satoshi-615a68 | **conductor/claude-p0-t3-...-4f410a** | t1 → merge (base); adoring-satoshi → merge (lilac predledger) | ledger
notion-ledger | claude/laughing-grothendieck-93540a | **claude/laughing-grothendieck-93540a** (active canonical) | — | ledger
venue-canonicalization | codex/venue-canon, codex/venue-override | **codex/venue-canon** | venue-override → merge (complement) | ledger
betfair-venue-canon | conductor/claude-todays-...-1ec149 | — | archive (cherry-pick venues.py) | ledger / infra
ledger-ev-fields | feat/ev-on-record | feat/ev-on-record (merge) | — | ledger
positions-sync | feat/positions-sync-hardening | feat/positions-sync-hardening (merge) | — | ledger
pm-ledger-reconcile | feat/pm-ledger-reconcile | feat/pm-ledger-reconcile (merge) | — | ledger
ledger-auto-settlement | chore/ledger-audit-script | chore/ledger-audit-script (merge) | — | ledger
settler-results-source | fix/settler-stale-results-source | fix/settler-stale-results-source (merge) | — | ledger
pm-trader | conductor/codex-...-make-pol-ab0d92 | ...make-pol-ab0d92 (merge) | — | polymarket
pm-cashout | claude/jovial-northcutt-6d30cf | **claude/jovial-northcutt-6d30cf** (merge, P0, 3216 LOC) | — | polymarket
pm-venue-ranking | feat/pm-1x2-snapshotter | **feat/pm-1x2-snapshotter** (active, merge) | depends on venuesdata | polymarket
pm-outright-edge | feat/pm-price-history-outright-edge | feat/pm-price-history-outright-edge (merge) | — | polymarket
odds-source-betfair-pm | feat/odds-source-betfair-pm | feat/odds-source-betfair-pm (merge, 5 ahead) | — | polymarket
dual-pool-kelly | feat/dual-pool-kelly | **feat/dual-pool-kelly** (active) | — | polymarket
bet-recs-tab | conductor/claude-rebuild-bet-recs-...-4eee9a, conductor/claude-p0-t2-...-ab3148, conductor/claude-p0-t4-...-85d798, feat/bet-recs | **conductor/claude-rebuild-bet-recs-...-4eee9a** | t2 → delete; t4 → delete (byte-identical); feat/bet-recs → merge | site/polymarket
pm-tables-display | fix/pm-bets-table-display | fix/pm-bets-table-display (merge) | — | polymarket
pm-card-guards | fix/pm-halftime-contamination | **fix/pm-halftime-contamination** (active) | — | polymarket
market-microstructure-recon | feat/market-microstructure | **feat/market-microstructure** (merge, canonical base) | — | microstructure
early-response-backtest | conductor/codex-...-build-a-0c7f27 | ...build-a-0c7f27 (merge) | — | microstructure
correlated-exposure | feat/correlated-exposure-model | **feat/correlated-exposure-model** (active) | — | microstructure
acca-book-hedging | claude/determined-banzai-4b3996 | — | merge (overlaps corr-exposure) | microstructure
card-surface-events | feat/card-surface-events | **feat/card-surface-events** (active) | — | microstructure
card-goalscorers | fix/goalscorers-empty | fix/goalscorers-empty (merge) | — | microstructure
betfair-data | feat/betfair-keyfallback-aliases | **feat/betfair-keyfallback-aliases** (active) | — | microstructure
vision-bet365 | fix/vision-bet365-detection, claude/nice-gauss-e40a19 | **fix/vision-bet365-detection** (active) | nice-gauss → delete | microstructure
vision-acca-detection | (rolled into vision-bet365 above) | fix/vision-bet365-detection | claude/nice-gauss-e40a19 → delete | microstructure
lilac-dashboards | feat/lilac-8002-panel-merge, fix/lilac-risk-panel-live-data, feat/lilac-dashboard | **feat/lilac-8002-panel-merge** (active) | lilac-dashboard → archive; risk-panel-live-data → active (keep) | site
site-bet-recs-panel | claude/bold-moore-ddb0d2 | claude/bold-moore-ddb0d2 (merge) | — | site
site-nav-consolidation | codex/site-streamline | codex/site-streamline (merge) | — | site
site-nav-rename | chore/rename-terminal-to-bets | — | merge (overlaps bold-moore) | site
positions-ui-toggle | conductor/claude-add-a-toggle-...-c8b469 | conductor/...-c8b469 (merge) | — | site
terminal-exposure-display | conductor/claude-fix-the-terminal-...-0b93ac | conductor/...-0b93ac (merge) | — | site
scores-group-view | feat/by-group-view | feat/by-group-view (merge) | — | site
scores-command-overhaul | conductor/claude-overhaul-the-scores-...-564d62 | conductor/...-564d62 (canonical) | archive | site
scores-markets-panel | conductor/claude-on-the-panel-...-f03efd | — | delete (superseded) | site
exposure-dashboard | feat/exposure-dashboard | feat/exposure-dashboard (merge) | — | site
auto-sync-site-data | claude/frosty-cray-b19224 | **claude/frosty-cray-b19224** (active, live data) | — | site
venues-data-feed | feat/conductor-pr-failure-recovery | **feat/conductor-pr-failure-recovery** (active; ships venuesdata, NOT conductor) | — | site
site-auto-sync | claude/wizardly-hopper-a9c7cb | — | delete (regenerable data noise) | site
auto-site-publish | worktree-wf_0ae0b4a5-0c7-6 | — | delete (regenerable snapshot) | site
matched-betting-commission | claude/jovial-saha-42f4af | claude/jovial-saha-42f4af (merge) | — | site
docs-market-universe | harden/docs-feed-correction | **harden/docs-feed-correction** (active, mine) | — | site
analytics-design-doc | docs/analytics-design | — | archive | site
under-the-hood-docs | conductor/codex-find-...-e37857 | — | archive | site
site-architecture-uth | feat/under-the-hood-qa-2026-06-21, feat/under-the-hood-findings | **feat/under-the-hood-qa-2026-06-21** | findings → delete (byte-identical) | site
analytics-dashboard | feat/analytics-dashboard | — | archive (embeds worktrees, not mergeable) | site
promos-data | promos/wc-signups-20260622 | promos/wc-signups-20260622 (merge/archive) | — | site
matchday-tooling | feat/matchday-tooling | feat/matchday-tooling (merge) | — | other
ci-pytest-gate | fix/ci-pytest-gate, ci/pytest-gate | **fix/ci-pytest-gate** | ci/pytest-gate → delete | infra
ci-test-fixes | fix/ci-pytest-gate, fix/ci-pythonpath | fix/ci-pytest-gate (+ pythonpath companion) | both merge | infra
data-collection-deploy | feat/data-collection-wiring | feat/data-collection-wiring (merge) | — | infra
data-archival | feat/data-archival | feat/data-archival (keep for merge) | — | infra
deploy-watchdog | ops/mini-watchdog | ops/mini-watchdog (merge) | — | infra
vercel-deploy-config | chore/vercel-deploy-conservation | chore/vercel-deploy-conservation (merge) | — | infra
bot-commands | feat/restart-command, conductor/claude-audit-...-bfba9f | both merge | — | infra
ops-runbook | claude/bold-fermat-f57d4c | — | archive | infra
repo-docs | chore/contribution-rules | — | archive | infra
telegram-proxy | claude/epic-chaum-1afbb2 | — | delete (dead, trivial) | infra
dev-env-setup | test/dev-setup-verify | — | delete (throwaway .env.dev) | infra

---

## 3. WORKTREE CLEANUP LIST

36 worktrees. Classification of each below. **SAFE-REMOVE** = its branch is merged into `main` OR classified dead/superseded AND it is not an active session. **KEEP** = active session, dirty user tree, or live canonical work.

### 3.1 KEEP (active sessions / live work / user main)

| Worktree path | Branch | Why keep |
|---|---|---|
| `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha` | `main` | **User's primary tree — DIRTY (27 modified + untracked). DO NOT TOUCH.** |
| `.../scratchpad/wt-clean` | `harden/xg-totals` | Active canonical xG model work (06-30) |
| `.../scratchpad/wt-a2` | `harden/matchevents-pipeline` | Active canonical (06-30) |
| `.../scratchpad/wt-a6` | `harden/docs-feed-correction` | Active canonical (06-30, this audit's docs line) |
| `.../07554979.../wt-cardfix` | `fix/pm-halftime-contamination` | Active PM card guard work (06-30) |
| `/private/tmp/wca-size-opt` | `claude/project-size-optimization-1whb9s` | Active size-optimization (06-29) |
| `.claude/worktrees/frosty-cray-b19224` | `fix/vision-bet365-detection` | Active canonical vision fix (06-30) |
| `worktrees/bf-fix` | `feat/betfair-keyfallback-aliases` | Active betfair data |
| `worktrees/ci-fix` | `fix/ci-pytest-gate` | Canonical CI gate (stale but live worktree; keep until merged) |
| `worktrees/ci-fix2` | `fix/ci-pythonpath` | CI companion (keep until merged) |
| `worktrees/corr-exposure` | `feat/correlated-exposure-model` | Active correlated-exposure |
| `worktrees/modeling-bench` | `feat/modeling-benchmark-betbuilder` | Has worktree; keep until merged |
| `worktrees/odds-source` | `feat/odds-source-betfair-pm` | 5-ahead odds source; keep until merged |
| `worktrees/phase2-players` | `feat/players-db-phase2` | Canonical players-db (10 ahead) |
| `.claude/worktrees/wf_47433f20-dab-1` | `feat/card-surface-events` | Active (06-28) |
| `.claude/worktrees/wf_47433f20-dab-2` | `feat/bot-fair-kelly-display` | Active (06-28) |
| `.claude/worktrees/laughing-grothendieck-93540a` | `claude/laughing-grothendieck-93540a` | Active canonical notion-ledger (06-29) |

### 3.2 SAFE-REMOVE — branch is MERGED into main (worktree is dead weight)

| Worktree path | Branch | Note |
|---|---|---|
| `.../scratchpad/wt-models` | `harden/match-event-models` | At `e4287ab` = main HEAD; merged |
| `worktrees/bestprice` | `ci/cloud-card-hourly` | merged |
| `worktrees/report-send` | `codex/report-send-command` | merged |
| `worktrees/llw` | `feat/accas-low-level-win` | merged (distinct from active `feat/accas-low-win`) |
| `worktrees/pm-tables` | `fix/pm-positions-tables` | merged |
| `.claude/worktrees/awesome-perlman-23e908` | `claude/awesome-perlman-23e908` | merged |
| `.claude/worktrees/bold-bardeen-fce711` | `claude/bold-bardeen-fce711` | merged |
| `.claude/worktrees/charming-dhawan-432e4f` | `claude/charming-dhawan-432e4f` | merged |
| `.claude/worktrees/interesting-nightingale-68ec31` | `claude/interesting-nightingale-68ec31` | merged |
| `.claude/worktrees/modest-wozniak-1148e8` | `claude/modest-wozniak-1148e8` | merged |
| `.claude/worktrees/priceless-maxwell-e7887d` | `claude/priceless-maxwell-e7887d` | merged |

### 3.3 SAFE-REMOVE — dead/superseded branch, no active session

| Worktree path | Branch | Note |
|---|---|---|
| `.claude/worktrees/affectionate-edison-ed818b` | `feat/conductor-menu` | stale/superseded by conductor-persistence-chat; worktree prunable |
| `.claude/worktrees/wizardly-hopper-a9c7cb` | `claude/wizardly-hopper-a9c7cb` | dead (regenerable data noise) → delete |
| `.claude/worktrees/bold-moore-ddb0d2` | `claude/bold-moore-ddb0d2` | stale, fold-into-integration; worktree not active |
| `.claude/worktrees/determined-banzai-4b3996` | `claude/determined-banzai-4b3996` | stale WIP, overlaps corr-exposure; worktree not active |

### 3.4 DETACHED-HEAD worktrees — REMOVE WITH CARE (verify no uncommitted work first)

| Worktree path | State | Recommendation |
|---|---|---|
| `/Users/andrewdoherty/.codex/worktrees/2b54/World Cup Alpha` | detached `1a36adc` | Likely stale codex scratch. Verify clean (`git -C <path> status`), then remove. |
| `/Users/andrewdoherty/.codex/worktrees/5f01/World Cup Alpha` | detached `f78cc4f` | Same — verify clean, then remove. |
| `/Users/andrewdoherty/wca-positions` | detached `cf908d7` | Verify clean, then remove. Conservative default: leave if unsure. |

> When unsure, these detached worktrees are classified **leave-active** until manually inspected — do not blind-remove.

---

## 4. PRIORITY-DIRECTION INTEGRATION PLANS

Two integration branches to build off current `main` (`e4287ab`). Listed in dependency order with conflict-risk notes. **Do not merge active-session branches into these** — only stale/merge-into-integration ones. Build each as a fresh branch, merge sequentially, run tests, resolve conflicts as flagged.

### 4.1 `integrate/polymarket`

Base off `main`. Merge order (data/model foundations first, then traders, then UI/edge, then reconcile):

1. `conductor/codex-in-repo-world-cup-alpha-make-pol-ab0d92` — PM trader logic + tests. *Foundation. Low risk (isolated `pm/trader.py`).*
2. `feat/odds-source-betfair-pm` — betfair+PM odds source (5 ahead). *Touches `data/odds_source.py`, `polymarket_odds.py`. Low-moderate; merge before anything consuming odds.* **Active worktree (odds-source) — coordinate / merge from its tip.**
3. `claude/jovial-northcutt-6d30cf` — PM cash-out P0 (3216 LOC, `pm/trader.py` + tests). *MODERATE-HIGH conflict with step 1 in `pm/trader.py`. Merge step 1 first, then this; expect to reconcile trader.py.*
4. `feat/pm-price-history-outright-edge` — `pmhistory.py`, `outrightedge.py`, snapshot workflow. *Low risk (new files).*
5. `feat/pm-1x2-snapshotter` — `pm1x2snapshot.py` + `venuesdata.py` (+5 lines). *Depends on `venuesdata.py`; **coordinate with `feat/model-vs-venue-benchmark`** which also extends `venuesdata.py` — MODERATE conflict. Merge benchmark first if including it.* **Active.**
6. `feat/bet-recs` — `wca_betrecs.py`, `bet_recs.json`, `site/arb.js`. *MODERATE conflict with the bet-recs-tab site cluster (see microstructure/site). Prefer the canonical `conductor/claude-rebuild-bet-recs-...-4eee9a` for the tab UI; take `feat/bet-recs` only for its PM edge logic.*
7. `fix/pm-bets-table-display` — 1-line `pollsched.py`. *Trivial.*
8. `conductor/codex-in-repo-world-cup-alpha-make-pol-562658` *(merged into main already per branch list — skip; listed only to note it is upstream).* 

**Excluded (leave active, do NOT auto-merge):** `feat/dual-pool-kelly` (active, touches `card.py` — high conflict, needs human review), `fix/pm-halftime-contamination` (active worktree).

**Top conflict watch:** `src/wca/pm/trader.py` (steps 1 ↔ 3) and `src/wca/venuesdata.py` (step 5 ↔ venue-benchmark).

### 4.2 `integrate/microstructure`

Base off `main`. Merge order (recon engine base first, then calibration consumers, then exposure, then backtests):

1. `feat/market-microstructure` — core recon engine + `site/microstructure/*.json`. *Canonical base. Merge first. Low risk (new microstructure tree).*
2. `feat/microstructure-goal-calibration` — `scripts/microstructure/goal_calibration.py` + JSON. *Active but distinct presentation layer. Low risk — does NOT touch `dixon_coles.py`. Safe to include; depends conceptually on the calibrated model from `harden/xg-totals` but no file overlap.* **Active — merge from its tip or coordinate.**
3. `fix/goalscorers-empty` — `wca_build_card.py` goalscorers fix + tests. *Low risk.*
4. `conductor/codex-in-repo-world-cup-alpha-build-a-0c7f27` — early-response backtest harness (`backtests/early_response_backtest.py`). *Low risk (new backtest file).*
5. `feat/card-surface-events` — `card.py` match-events surfacing + tests. *Active. MODERATE conflict on `card.py` — merge after model branches, review.* **Active worktree.**
6. `claude/determined-banzai-4b3996` — acca book-hedging WIP, `test_exposure.py`. *Overlaps `feat/correlated-exposure-model` (active). MODERATE conflict in exposure tests — merge corr-exposure direction first or keep banzai out until corr-exposure lands.*
7. `feat/correlated-exposure-model` — `exposure_corr.py`, `modelpreds.py`, `exposure.py`. *Active, self-contained. Include from its tip; it is the canonical exposure model that banzai overlaps.* **Active worktree.**

**Model dependency note:** the microstructure direction consumes the goal model. Land `harden/xg-totals` (canonical model branch) into `main` (or a shared model integration) BEFORE finalizing `integrate/microstructure`, so the calibration JSON in step 2 reflects the canonical DC level-anchor. Do not merge `harden/xg-totals` itself into `integrate/microstructure` (it is active model-layer work) — depend on it via main.

**Excluded (leave active):** `feat/betfair-keyfallback-aliases`, `feat/correlated-exposure-model` and `feat/card-surface-events` may instead be left active and only their *merged* state pulled — operator's call. Conservative default: keep active, merge from tips only when sessions are idle.

**Top conflict watch:** `src/wca/card.py` (step 5) and `src/wca/exposure*.py` (steps 6 ↔ 7).

---

## 5. SAFE CLEANUP SEQUENCE (CHECKLIST — DO NOT EXECUTE HERE)

> Ordered, explicit commands for the operator/next step. All `git` runs with `-C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha"`. **Run from a clean state; the user's main tree is dirty — do NOT stash/checkout/clean it.** Verify each step before proceeding. Nothing in Section 6 is touched.

### Step 0 — Pre-flight (verify, no mutation)
```
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree list
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch --merged main
# Confirm main tree dirtiness is expected; do NOT clean it:
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" status --short
```

### Step 1 — Delete the 57 MERGED branches (SAFE — commits are in main)
```
# Review first:
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch --merged main | grep -vE '^\*| main$' > /tmp/merged_branches.txt
cat /tmp/merged_branches.txt   # expect 57 lines
# NOTE: several merged branches are checked out in worktrees (e.g. ci/cloud-card-hourly,
# claude/awesome-perlman-23e908, codex/report-send-command, feat/accas-low-level-win,
# fix/pm-positions-tables, harden/match-event-models, the claude/* worktree branches).
# A branch checked out in a worktree CANNOT be -d deleted until its worktree is removed.
# So Step 2 (prune those worktrees) must run for THOSE branches before they delete.
# Delete the non-worktree merged branches now:
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch --merged main \
  | grep -vE '^\*| main$' \
  | xargs -n1 git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch -d
# (-d, not -D: refuses anything not actually merged — extra safety. Worktree-held ones
#  will error harmlessly; they get deleted after Step 2.)
```

### Step 2 — Prune DEAD / MERGED worktrees (Section 3.2 + 3.3)
```
# Merged-branch worktrees (3.2):
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".../scratchpad/wt-models"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove "worktrees/bestprice"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove "worktrees/report-send"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove "worktrees/llw"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove "worktrees/pm-tables"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/awesome-perlman-23e908"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/bold-bardeen-fce711"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/charming-dhawan-432e4f"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/interesting-nightingale-68ec31"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/modest-wozniak-1148e8"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/priceless-maxwell-e7887d"
# Dead/superseded, no active session (3.3) — verify clean first with `git -C <wt> status`:
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/affectionate-edison-ed818b"   # feat/conductor-menu
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/wizardly-hopper-a9c7cb"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/bold-moore-ddb0d2"
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree remove ".claude/worktrees/determined-banzai-4b3996"
# Use --force ONLY after confirming the worktree has no uncommitted work you want.
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree prune
```

### Step 2b — Now delete the merged branches that were freed from worktrees
```
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch --merged main \
  | grep -vE '^\*| main$' \
  | xargs -n1 git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch -d
# (Idempotent re-run of Step 1; the worktree-held ones now delete cleanly.)
```

### Step 3 — Delete the DEAD/DUPLICATE unmerged branches (disposition=delete; NOT in active worktrees)
```
for b in \
  ci/pytest-gate \
  claude/epic-chaum-1afbb2 \
  claude/nice-gauss-e40a19 \
  claude/wizardly-hopper-a9c7cb \
  conductor/claude-on-the-panel-from-the-attached-i-f03efd \
  conductor/claude-p0-t2-flatten-card-write-wire-in-ab3148 \
  conductor/claude-p0-t4-close-clv-pass-two-way-ext-85d798 \
  feat/accas-rebuild \
  feat/conductor-v0 \
  feat/conductor-v1 \
  feat/under-the-hood-findings \
  fix/conductor-worktree-lock \
  test/dev-setup-verify \
  worktree-wf_0ae0b4a5-0c7-6 ; do
  git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch -D "$b"
done
# -D (force) because these are unmerged-but-intentionally-discarded. Confirm the list
# matches Section 2 'delete' rows before running.
```

### Step 4 — (Optional) Tag-and-archive the ARCHIVE-disposition branches before deleting
```
# Preserve history without keeping live branch refs. Example pattern per archive branch:
#   git -C <repo> tag archive/<branch> <branch> && git -C <repo> branch -D <branch>
# Archive set (18): chore/codex-removal, chore/contribution-rules, claude/bold-fermat-f57d4c,
#   claude/interesting-kalam-9a581b, conductor/claude-generate-analysis-...-abcb90,
#   conductor/claude-overhaul-the-scores-...-564d62, conductor/claude-todays-...-1ec149,
#   conductor/codex-find-...-e37857, docs/analytics-design, feat/analytics-dashboard,
#   feat/conductor-control, feat/conductor-health-routing, feat/conductor-imgpaste,
#   feat/conductor-live, feat/conductor-menu, feat/lilac-dashboard,
#   fix/conductor-error-surfacing, feature/market-anchored-advancement
# (Do this only if the operator wants reference history; otherwise skip.)
```

### Step 5 — Build the two integration branches (off main; do NOT touch active sessions)
```
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch integrate/polymarket main
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree add ../wca-int-pm integrate/polymarket
# Merge in dependency order from Section 4.1 inside ../wca-int-pm, running pytest between merges:
#   make-pol-ab0d92 -> odds-source-betfair-pm -> jovial-northcutt(cashout, reconcile trader.py)
#   -> pm-price-history-outright-edge -> pm-1x2-snapshotter(+venue-benchmark) -> fix/pm-bets-table-display

git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch integrate/microstructure main
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree add ../wca-int-micro integrate/microstructure
# Merge in dependency order from Section 4.2 inside ../wca-int-micro, pytest between merges:
#   feat/market-microstructure -> microstructure-goal-calibration -> fix/goalscorers-empty
#   -> early-response-backtest -> card-surface-events(card.py) -> corr-exposure(then banzai)
```

### Step 6 — Final verification
```
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" worktree list
git -C "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" branch --no-merged main | wc -l
# Confirm: active sessions (Section 6) still present; merged branches gone; two integrate/* exist.
```

---

## 6. LEAVE STRICTLY ALONE

Do not delete, prune, merge into, check out, stash, or clean any of the following.

### 6.1 The user's main working tree (DIRTY)
- **`/Users/andrewdoherty/Desktop/Coding/World Cup Alpha` on `main`** — 27 modified tracked files (incl. `src/wca/bot/app.py`, `src/wca/bot/vision.py`, `src/wca/ledger/store.py`, `scripts/wca_settle.py`, many `site*/data` JSONs) plus untracked (`logs/`, `worktrees/`, report PNGs, `docs/research/wca_alpha_2026/taskD_betbuilders_2026-06-30.md`, cache JSONs). **Live uncommitted work — absolutely untouched.**

### 6.2 Active session worktrees + their branches (classification=active / live)
- `harden/xg-totals` — `.../scratchpad/wt-clean`
- `harden/matchevents-pipeline` — `.../scratchpad/wt-a2`
- `harden/docs-feed-correction` — `.../scratchpad/wt-a6`
- `fix/pm-halftime-contamination` — `.../07554979.../wt-cardfix`
- `claude/project-size-optimization-1whb9s` — `/private/tmp/wca-size-opt`
- `fix/vision-bet365-detection` — `.claude/worktrees/frosty-cray-b19224`
- `feat/betfair-keyfallback-aliases` — `worktrees/bf-fix`
- `fix/ci-pytest-gate` — `worktrees/ci-fix`
- `fix/ci-pythonpath` — `worktrees/ci-fix2`
- `feat/correlated-exposure-model` — `worktrees/corr-exposure`
- `feat/modeling-benchmark-betbuilder` — `worktrees/modeling-bench`
- `feat/odds-source-betfair-pm` — `worktrees/odds-source`
- `feat/players-db-phase2` — `worktrees/phase2-players`
- `feat/card-surface-events` — `.claude/worktrees/wf_47433f20-dab-1`
- `feat/bot-fair-kelly-display` — `.claude/worktrees/wf_47433f20-dab-2`
- `claude/laughing-grothendieck-93540a` — `.claude/worktrees/laughing-grothendieck-93540a`

### 6.3 Active (no worktree, but live/recent — do not delete the branch)
- `claude/frosty-cray-b19224` (auto-publish, 06-30)
- `feat/conductor-pr-failure-recovery` (venues data feed, 06-29)
- `feat/conductor-persistence-chat` (canonical conductor, 06-26)
- `feat/accas-low-win` (canonical accas)
- `feat/dual-pool-kelly` (06-28, touches card.py)
- `feat/pm-1x2-snapshotter` (06-29)
- `feat/lilac-8002-panel-merge`, `fix/lilac-risk-panel-live-data` (live lilac, 06-29)

### 6.4 Detached-HEAD worktrees — leave until manually inspected
- `/Users/andrewdoherty/.codex/worktrees/2b54/World Cup Alpha`
- `/Users/andrewdoherty/.codex/worktrees/5f01/World Cup Alpha`
- `/Users/andrewdoherty/wca-positions`

> Conservative rule applied throughout: when a branch's status was ambiguous, it was classified **leave-active** rather than delete.
