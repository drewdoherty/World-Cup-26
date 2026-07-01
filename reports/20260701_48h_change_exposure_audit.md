# World Cup Alpha — 48-hour change, request, and exposure audit

_Audit window: 2026-06-29 09:25 to 2026-07-01 09:25 Bahrain (UTC+3). Market and portfolio snapshot: 2026-07-01 06:21 UTC unless stated otherwise. This is a read-only review; no live orders, position changes, ledger reconciliation, service stops, merges, or deploys were performed._

## Executive verdict

The codebase made substantial progress, but the operating state is not yet trustworthy enough to generate new real-money “high conviction” adds.

- `origin/main` took 64 first-parent commits in the window: 49 automation/data commits and 15 feature/fix/merge commits. Across the full branch diff, 185 files changed (+68,263/-7,413). The meaningful product work is concentrated in the model-integrity integration, paper book/PM analytics, site/forest, market intelligence, PM snapshotting, and bot display.
- The active local checkout is the closed/superseded paper-book branch (`feat/paper-testbook-pm-analytics`, PR #118), not current `main`. At the start of the audit it was 51 commits behind `origin/main`; it finished 55 behind after `main` advanced through the #123–#126 paper hotfix chain. Local source edits combine open PRs #113/#114/#117 with uncommitted settlement and `/movers` work.
- The local full suite is red at collection because this branch archived `scripts/wca_canon_venues.py` while `tests/test_venue_canon.py` still imports the old path. The focused PM/test-book/bot/settlement set passes: **125 passed**.
- The real Polymarket wallets hold 15 live positions: cost **$1,365.55**, current value **$1,541.28**, open MTM **+$175.72**. Current marked exposure is **77.3%** of the documented $1,995 PM pool, far above the repository’s 15–20% simultaneous-exposure guidance.
- The canonical ledger has only one open PM row. The dry-run reconciler reports 15 inserts and one close, but it fails to match the existing Ghana-NO position and would falsely close it as a loss while inserting a duplicate. **Do not apply the reconciliation plan yet.**
- The Mac mini paper book is invalid as a model comparison. Its `liquidity_exit` rule sells wide-spread props near the bid, then the next scan buys the same token again because deduplication considers only open positions. Before a silent DB reset it had 52 adds, 31 closes, and **-$636.50 realised**, including **$315.82** of recorded spread cost. After the reset, one cycle immediately lost another **$232.18 realised** plus **-$62.04 MTM**.
- PRs #123/#124 attempt to block dead/started fixtures, but do not fix the exit/re-entry loop. #124 initially shipped with two `now` defects; they produced live Mac-mini tracebacks and were hotfixed by green PRs #125/#126. Four of ten current score-feed fixtures still have no kickoff, so the guard remains incomplete. The original defects passed #124’s full pytest run because its tests did not execute the default-`now` or player-prop paths.
- The live analytics join found match-result and exact-score prices close to PM, but a **systematic +12.5 percentage-point BTTS model bias** and a stale advancement model. Those are model/data warnings, not alpha.

## 1. Request and enhancement stage ledger

| Workstream / request | Stage | Evidence | What remains / glossed over |
|---|---|---|---|
| Model-integrity integration: xG level anchor, closing-line capture, results/snapshot freshness, exposure dashboard, match-event loader/models, goal blend | **Merged/deployed** via #115 | `origin/main` contains the integration | #115 itself was merged with a failed pytest check, violating the green-tree rule. F7 remains default-OFF/tracking-only. F8 models are implemented but not proven sizing signals; live EV/CLV wiring and forward validation remain incomplete. |
| Paper test book + PM analytics/decision-quality | **Merged/deployed but BLOCKED** via #120–#122 | Mac mini launch job is running; reports/charts execute | #120 was merged with a failed pytest check. Critical sell/rebuy churn, no portfolio cap, zero-volume prop entries, static model-q, duplicate fixture-family exposure, incomplete prop/advance settlement, and repeated unexplained DB resets invalidate results. Pause trade cycles or make them mark-only until fixed. |
| Dead/resolved fixture guards | **Merged + hotfixed, still incomplete** via #123–#126 | #123 adds market flags; #124 kickoff checks; #125/#126 fix the two live `now` crashes and passed pytest | Does not address re-entry. Four fixtures lack kickoff and therefore bypass the time guard. Add an end-to-end two-cycle player-prop regression, not just pure guard tests. |
| PM calibration/MTM Matplotlib suite | **Merged and runnable** | 296 model/PM joins; two suite PNGs generated; paper marks all 31 local positions | “Real” suite input is the stale SQLite ledger, not the two wallets, so its real-book section is incomplete. Advancement inputs were ~24h stale during the run. |
| PM share-price movers + three charts + `/movers` bot command | **Partially built / uncommitted** | `src/wca/pmmovers.py` and tests exist; local `bot/app.py` adds `/movers`; current charts generated | Stored history stopped at 2026-06-29 18:32 UTC and has no prop history. The CLI was moved under `scripts/archive/` without updating its root calculation. `/movers` is not merged. |
| Twice-hourly PM snapshot history | **Code merged; operation STALLED** via `e270a7d`/`d316ce6` | JSONL has 2,057 valid snapshots/161 markets; DB has 155 rows | Last stored trajectory was Jun 29; DB rows share one timestamp; token IDs are null, preventing direct live CLOB continuation. Props remain `COLLECTING`. |
| Site lilac theme + Event-Markets model-vs-market forest | **Merged/deployed** via #119 | Main site/forest files and build wiring landed | Open #117 still fixes a separate collapsed Bets grid. Verify the live Vercel surface after merging it. |
| Market Intelligence & cross-venue analytics | **Merged** via #104 | Module, feed, metrics, and tests landed | Measurement is ahead of execution: historical closing-line coverage and trustworthy venue liquidity remain limiting. |
| Bot fair %, decimal fair, and Kelly display | **Merged** via #105; acca repair #107 merged | Main contains display and repaired tests | Display is not evidence of edge; current benchmark says +EV flags have negative CLV. |
| PM 1X2 snapshotter | **Open PR #109; pytest green** | Open PR, branch `feat/pm-1x2-snapshotter` | Overlaps the broader PM history/analytics work. Rebase and integrate once, or close as superseded—do not parallel-merge duplicates. |
| Live lilac risk panel | **Open PR #110; pytest green** | Branch `fix/lilac-risk-panel-live-data` | Reconcile with #119 and #117 before merge to avoid overlapping site changes. |
| Microstructure goal calibration panel | **Open PR #111; pytest green** | Branch `feat/microstructure-goal-calibration` | Rebase against #115/#119; decide whether the new forest/calibration surfaces supersede it. |
| Bet365 vision detection | **Open PR #113; pytest green** | Local `vision.py` exactly matches the PR branch | Ready for normal review/rebase. |
| `/summary` realised/unrealised by book on £3,000 basis | **Open PR #114; pytest green but accounting defect** | Local summary implementation matches PR plus unrelated local edits | Open inventory is authoritative on-chain; lifetime realised P&L is not. Redeemed winners disappear while unresolved losing tokens remain, so current `positions` rows are survivorship-biased and cannot be treated as complete realised P&L. |
| Swarm cleanup | **Open PR #116; unstable** | Claims 167→85 branches and 36→25 worktrees | At least ten worktrees remain dirty; only Turso WIP changed in this 48h window, but older stranded work is still an operational hazard. Cleanup must preserve or explicitly retire each dirty patch. |
| Bets-grid exposure CSS fix | **Open PR #117; pytest green** | Local `site/style.css` exactly matches branch | Small/ready, but rebase after #119 and verify both Vercel targets. |
| Free-bet/lay settlement single source of truth | **Local WIP, not PR’d** | `ledger/store.py`, bot settlement path, and 164-line regression test are dirty; focused tests pass | Separate from the closed paper-book branch and open a dedicated PR after rebasing current main. |
| `/movers` Telegram delivery | **Local WIP, not PR’d** | Dirty `bot/app.py`; charts work locally | Must be split from #114/settlement work. Add freshness labels and refuse to describe Jun-29 history as current. |
| Turso cloud publish | **Prototype in dirty worktree, no PR** | `wt-turso` has `src/wca/db.py`, workflow, dependency and migration edits | Needs its own task/branch, tests, and a migration/rollback decision. Do not mix with paper-book repairs. |
| Research/model/action reports | **Mixed** | Several reports are committed on `origin/main`; this stale branch shows copies as untracked | Deduplicate before committing. The current checkout makes already-merged files look new because it predates #115. |

### Open PR queue at audit time

- #109 PM 1X2 snapshotter — pytest success.
- #110 lilac live risk panel — pytest success.
- #111 goal-expectancy calibration area — pytest success.
- #113 Bet365 screenshot detection — pytest success.
- #114 portfolio `/summary` — pytest success, but realised-P&L method needs correction.
- #116 swarm cleanup — pytest success, merge state unstable.
- #117 Bets grid-area fix — pytest success.

### Superseded/closed work

- #112 Dixon-Coles goal-supply PR was closed in favour of the #115 integration.
- #118 paper-book PR was closed and rebuilt as clean additive #120. The current local branch is #118’s branch and should not receive further mixed edits.

## 2. Real Polymarket book

### Aggregate

| Metric | Value |
|---|---:|
| Open positions | 15 |
| Open cost basis | $1,365.55 |
| Current value | $1,541.28 |
| Open MTM | **+$175.72** |
| Current value / documented $1,995 PM pool | **77.3%** |
| Resolved tokens still returned by API | 26 |
| Cost of those currently held resolved tokens | $390.53 |

The last line is **not** lifetime realised P&L: it is only the cost of resolved tokens still returned by the current-positions endpoint. Redeemed winners may no longer be present.

### Largest live concentrations

| Position | Cost | Value | MTM | Mark |
|---|---:|---:|---:|---:|
| France reach SF — YES | $214.96 | $328.61 | **+$113.65** | 78.5¢ |
| Brazil reach QF — YES | $169.97 | $215.96 | **+$45.99** | 64.0¢ |
| France reach QF — YES | $149.99 | $201.22 | **+$51.23** | 90.5¢ |
| USA reach R16 — YES | $165.77 | $166.87 | +$1.10 | 83.5¢ |
| Spain reach Final — YES | $199.92 | $162.50 | **-$37.42** | 19.5¢ |
| Colombia reach R16 — YES | $65.49 | $87.51 | +$22.01 | 79.5¢ |
| Belgium win Jul 1 — YES | $70.60 | $72.33 | +$1.73 | 45.5¢ |
| Argentina reach QF — YES | $70.00 | $72.19 | +$2.19 | 82.5¢ |
| USA–Bosnia draw — YES | $60.50 | $62.12 | +$1.62 | 19.5¢ |
| Belgium reach R16 — YES | $54.37 | $55.81 | +$1.44 | 58.5¢ |
| Colombia win Jul 3 — YES | $52.15 | $52.56 | +$0.41 | 64.5¢ |
| Portugal reach QF — YES | $66.99 | $39.30 | **-$27.69** | 30.5¢ |
| Colombia advance vs Ghana | $17.85 | $17.74 | -$0.11 | 80.5¢ |
| England not to win WC | $6.00 | $6.24 | +$0.24 | 90.65¢ |
| Ghana not eliminated R32 | $1.00 | $0.32 | -$0.68 | 21.5¢ |

France QF + France SF are nested/correlated and together represent **$529.83**, or **26.6%** of the entire documented PM pool. France/Brazil/USA advancement positions dominate the book.

## 3. Paper book versus real book

The paper book should not be compared to the real book as if it were an out-of-sample portfolio:

1. It can deploy 2% per token with no aggregate or correlation cap. The first local pass deployed $1,240/2,000 (62%) across 31 positions; a remote pass deployed $1,280 (64%) before closing thin markets.
2. Player props enter with `volume=0`, `spread=None`, and `min_volume=0`. Entry accepts exactly the markets whose later CLOB bid/ask causes a liquidity exit.
3. A >10-point spread triggers an immediate full close. Wide spreads are a reason not to enter; selling into the bad bid after entry crystallises the spread.
4. The next scan’s dedupe set contains only open token IDs. A closed token is immediately eligible for repurchase.
5. Several fixture/family duplicates are held because dedupe is token-only (for example, multiple BTTS-YES tokens for one fixture).
6. The model probability is frozen as `entry_static`; no freshness/staleness guard changes the exit belief.
7. `cmd_settle` passes `reached=None`, so advancement positions cannot settle. `grade()` has no `prop` handler, so player props cannot settle from outcomes either.
8. The Mac mini DB silently reset between 06:09 and 06:19 UTC. The log began a later cycle with seed $0/no positions, then reseeded and reopened 32 positions. No reset/audit event in the application explains the loss of continuity.

Snapshot immediately after the reset/reopen cycle:

| Paper family | Open | Deployed | Unrealised | Realised from churn |
|---|---:|---:|---:|---:|
| Advance | 8 | $320 | -$9.76 | $0.00 |
| BTTS | 5 | $200 | -$20.94 | -$15.17 |
| Props | 4 | $160 | -$32.13 | **-$217.00** |
| Totals | 5 | $200 | +$0.79 | $0.00 |

This is an execution-policy failure, not evidence that the player model lost $232 through match outcomes.

## 4. Analytics and largest movers

### Live model-vs-PM join

| Category | n | Mean model − PM | Mean absolute gap | Interpretation |
|---|---:|---:|---:|---|
| Match result | 27 | -0.7 pp | 1.4 pp | Close to market; no demonstrated edge |
| Exact score | 53 | -0.2 pp | 0.9 pp | Close to market; still a historically poor betting family |
| Advancement | 79 | -4.4 pp | 7.4 pp | Polluted by a ~24h stale pre-result model |
| BTTS | 27 | **+12.5 pp** | **15.2 pp** | Systematic model/mapping bias; do not treat as alpha |

The suite’s apparent France advancement “negative edges” are stale-state artefacts: the model was generated 2026-06-30 06:20 UTC while live prices reflected later matches. Do not trim solely from those raw deltas.

### Current largest share-price movers

The versioned history ended 2026-06-29 18:32 UTC. A read-only live remap was used to compare the same stable team/stage keys at 2026-07-01 06:17 UTC.

**Tournament futures:**

- France champion: 23¢ → 33¢, **+9.8 pp**.
- Argentina champion: 21¢ → 18¢, **-2.8 pp**.
- Mexico champion: 1¢ → 3¢, **+2.1 pp**.
- Morocco champion: 1¢ → 3¢, **+1.8 pp**.
- Brazil champion: 6¢ → 7¢, **+1.1 pp**.

**Advancement:** the biggest moves are dominated by qualifications/resolutions (Paraguay/Morocco/Mexico/Norway/Brazil R16 to 100¢), not new tradeable alpha. Among still-live ladders, Morocco QF rose 27¢→72¢, France SF 51¢→78¢, France QF 66¢→90¢, France Final 36¢→55¢, and Brazil QF 50¢→64¢.

No historical player-prop trajectory exists yet, so the prop mover panel correctly remains `COLLECTING`.

## 5. Add / hold / trim view

### Highest-conviction action: reduce risk, do not add

There is no new real-money add that clears the repository’s own evidence standard today. The earlier benchmark found +EV-flagged legs had mean CLV **-11.4%** and beat close only **14.3%** (n=42). The current live join adds a stale advancement model and a systematic BTTS bias. The book is already 77% marked-exposed.

Suggested risk actions, subject to checking executable CLOB bids rather than `curPrice` mids:

1. **Trim France SF first, then France QF.** They are nested, worth $529.83 combined, and have just repriced sharply upward. Target combined marked exposure no higher than roughly 10–12% of the PM pool ($200–240), implying a $290–330 reduction at current marks. Preserve more of the nearer QF leg; cut the longer SF leg more aggressively.
2. **Take partial profit on Brazil QF.** It is worth $215.96 (10.8% of the PM pool) and the stale model is not above the current 64¢ price. A 25–50% trim reduces concentration without making an all-or-nothing model call.
3. **Portugal QF is a trim/close candidate.** It is -$27.69 MTM and the stale model (28.1%) is below the current market (~30.5%); the sunk cost is not a reason to keep it.
4. **Do not average down Spain Final yet.** It is the clearest positive stale-model gap among existing positions (25.99% model vs ~19.5% market), but the model and tournament state must be refreshed first. Hold is defensible; add is not while total exposure is above cap.
5. **Keep the tiny Ghana/England positions operationally separate.** Ghana is dust and currently exposes a reconciler matching bug; do not “fix” it by applying the current dry-run plan.

### Conditional paper-only watchlist after the simulator is repaired

These are research candidates, not high-conviction real bets. Values use the stale Jun-30 advancement model against Jul-1 marks and must be recomputed after results refresh:

| Candidate | Stale model | Current PM | Raw gap | View |
|---|---:|---:|---:|---|
| Belgium reach SF | 18.57% | 10.5% | +8.1 pp | Paper-only; correlated with Belgium R16/Final |
| Belgium reach R16 | 66.53% | 58.5% | +8.0 pp | Best nearer-stage paper candidate |
| Croatia reach R16 | 37.28% | 30.0% | +7.3 pp | Paper-only |
| Spain reach Final | 25.99% | 19.5% | +6.5 pp | Hold existing; no add until refresh |
| Brazil reach Final | 23.97% | 17.5% | +6.5 pp | Paper-only; correlated with Brazil QF |
| Australia reach QF | 12.21% | 5.9% | +6.3 pp | Very high variance; not high conviction |

Explicit non-plays: BTTS (systematic +12.5 pp bias), player props (no history/zero-volume entries), exact-score punts (historical -73.9% ROI), and any stale post-kickoff market.

## 6. Ordered next actions

1. Pause the Mac mini paper **trade** cycle or make it mark-only; preserve a DB backup and the pre-reset log first.
2. Fix/test the paper state machine: permanent token/fixture-family cooldown, entry spread/depth/volume gates, aggregate and correlated exposure cap, safe liquidity handling, fresh `q_t`, and settlement support. Add regression tests for close→next-pass re-entry and a real 10-minute two-cycle fixture.
3. #125/#126 repaired #124’s two `now` crashes. Now block missing-kickoff fixtures conservatively and add an end-to-end two-cycle player-prop test. Do not rely on a green suite that never executes the affected path.
4. Refresh advancement/results before producing model-vs-market trade calls; encode model age in every report and refuse stale comparisons.
5. Fix PM reconciler outcome parsing and token matching, then backup and re-run dry-run. Only after it matches Ghana-NO correctly should it update the canonical ledger.
6. Change `/summary` to use wallet positions for **open inventory/MTM** and fills/ledger/cash history for **realised P&L**.
7. Restore PM snapshot cadence, persist token IDs and props, and label mover windows with actual elapsed hours. Separate resolution moves from still-tradeable moves.
8. Salvage local WIP into separate branches from current `main`: (a) settlement, (b) `/movers`, (c) #113/#114/#117 only if not merged. Do not continue on closed PR #118’s branch.
9. Re-run full `pytest -q` on the final integration branch and do not merge while pending/red.

## 7. Generated evidence

- `audit_20260701_real_pm_positions.png` — all live positions, cost vs current value.
- `audit_20260701_book_comparison.png` — real vs paper exposure/P&L by family.
- `audit_20260701_paper_churn.png` — paper losses caused by the liquidity-exit loop.
- `audit_20260701_pm_movers_current_futures.png` — current futures movers.
- `audit_20260701_pm_movers_current_advancement.png` — current advancement movers.
- `audit_20260701_pm_analytics_calibration_scatter.png` and `_category_bias.png` — model/PM diagnostics.
- `audit_20260701_pm_analytics.json` — machine-readable suite output.
