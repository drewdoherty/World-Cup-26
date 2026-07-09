# WCA ARCHITECTURE — Current State (Phase 0)

**Verified against:** `origin/main` = `957112ae33622e459b5b30cdcc391d5be280059a`, 2026-07-02.
**Rollback point:** git tag `pre-overhaul-2026-07-01` (pushed).
**Verification method:** read-only `git show origin/main:<path>` / `git grep <pat> origin/main` only; the working tree was never trusted for "what is live". All `file:line` references point into origin/main blobs.
**Legend:** `[UNVERIFIED-MINI: <value>]` — live SSH to the Mac mini was DENIED this session; the bracketed value is the 2026-07-01 brief snapshot and must be re-verified before it is acted on (Appendix A is the ready-to-run checklist).

> **Read Section 12 (Operational Hazards) first.** H1 (deploy-lag silent-abort loop) and H2 (dev-box live-fire footgun) are the two findings that can cost money or ship stale code today.

**System thesis.** WCA is a real-money quantitative football-betting platform for the 2026 World Cup. An Elo + Dixon-Coles model (trained on ~49.5k international results, market-blended against de-vigged h2h consensus) prices 1X2, totals, BTTS, scorelines and tournament advancement; value detection compares model probabilities to sportsbook and Polymarket prices; execution is human-confirmed Polymarket CLOB orders (propose → Telegram `Y PM-n` or one-click fire → signed order behind a guardrail stack) plus hand-placed sportsbook bets reconstructed from betslip screenshots by an LLM vision call; every bet lands in a single SQLite ledger (`data/wca.db`, mini-only, git-untracked); closing-line value (CLV) is the primary KPI, auto-stamped at kickoff. This document maps that system exactly as it exists on origin/main — it PROPOSES changes (each marked DECISION or fix option) but executes none of them.

---

## Contents

1. [Topology](#1-topology)
2. [Code map](#2-code-map)
3. [Scripts manifest](#3-scripts-manifest)
4. [Duplication inventory](#4-duplication-inventory)
5. [Dark features F7/F8/F9](#5-dark-features)
6. [Ledger](#6-ledger)
7. [Sites](#7-sites)
8. [Telegram + execution](#8-telegram--execution)
9. [Data assets: HAVE vs USE](#9-data-assets-have-vs-use)
10. [Sizing single source of truth](#10-sizing-single-source-of-truth)
12. [Operational hazards](#12-operational-hazards) *(there is no section 11 — hazards keep the number 12 to match the drift-log ordering)*
13. [Brief-vs-verified drift log](#13-brief-vs-verified-drift-log)
14. [Appendix A: UNVERIFIED-MINI checklist](#appendix-a-unverified-mini-checklist)

---

## 1. Topology

### 1.1 The machines

| Node | Role | Key facts |
|---|---|---|
| MacBook (`/Users/andrewdoherty/Desktop/Coding/World Cup Alpha`) | DEV box | Currently on a clean `main`, 4 commits behind origin, zero modified tracked files (devbox report §6 — the brief's "stale dirty feature branch" claim is outdated). Holds a live-armed `.env` and a stale 723 MB `wca.db` — see Hazard H2. |
| Mac mini (`~/World-Cup-26`, SSH `andrewdoherty@192.168.68.55`) | PRODUCTION | Runs both bots and all scheduled jobs; holds the canonical ledger `data/wca.db` [UNVERIFIED-MINI: 1.94 GB, 210+ bets]. Git-untracked by `.gitignore:11` (`data/*.db`). |
| GitHub | Source of truth + data mover | `origin/main` is what the mini pulls (and what the localhost sites serve once pulled). 5 of 7 CI workflows push data commits to main at hourly-or-better cadence (§1.4). |
| ~~Vercel~~ | **REMOVED 2026-07-08** | Serving is localhost-only: `site/` on :8000, `site-analytics/` on :8001 (§1.5). |

### 1.2 Deploy flow

```
                 PR + pytest (ADVISORY — see H-S1)         merge
  MacBook dev ──────────────────────────────────► GitHub origin/main ◄──────────────┐
                                                    │        ▲   ▲                  │
                        localhost sites serve       │        │   │ "Auto-sync" data │
                 site/ :8000 · site-analytics :8001 │        │   │ commits, hourly+ │
                     (Vercel REMOVED 2026-07-08)    │        │   │ (5 CI workflows) │
                                                    ▼        │   │                  │
                                             ┌──────────────┐│  GitHub Actions      │
                                             │  localhost   ││                      │
                                             │ :8000 /:8001 ││                      │
                                             └──────────────┘│                      │
                                                             │                      │
   Mac mini (PROD) ──── autopull every 5 min ────────────────┘                      │
   git pull --rebase --autostash origin main                                        │
     ├─ on conflict: rebase --abort; exit 0  ◄── SILENT (H1)                        │
     ├─ kickstart -k daemons whose src/|scripts/|deploy/ changed                    │
     └─ publish job every 30 min: regen feeds, commit+push site/*.json ─────────────┘

   Two writers race to commit the same tracked data paths to main
   (mini publish + CI daily-card), while other job-written tracked files
   are never committed by the mini at all → permanently dirty tree → H1.
```

### 1.3 Mini launchd jobs — 19 total (5 daemons + 14 interval jobs)

Source of truth: `deploy/macmini/services.env:10-12` (`WCA_DAEMONS`), `:59-76` (`WCA_INTERVAL_JOBS` + intervals); command lines generated by `deploy/macmini/install.sh:32-53` (`cmd_for`). Note: autopull, publish and watchdog ARE 3 of the 14 interval jobs — the brief's "14 + autopull + publish + watchdog = 22" double-counted.

**5 KeepAlive daemons:**

| Label | Runs | Notes |
|---|---|---|
| com.wca.bot | `scripts/wca_bot.py --db data/wca.db --env .env` | @gamble1_bot money surface |
| com.wca.conductor | `scripts/wca_conductor.py --env .env.conductor` | @WorldCupDev, dev-only (§8.6) |
| com.wca.snapshotd | `scripts/wca_snapshotd.py --db data/wca.db --env .env` | odds_snapshots writer (h2h+totals bulk, btts per-event) |
| com.wca.newsd | `scripts/wca_newsd.py … --interval 600` | news ingest |
| com.wca.promosd | `scripts/wca_promosd.py … --interval 21600` | promo scrape + boost grading |

**14 interval jobs:**

| Job | Interval | Runs |
|---|---|---|
| autopull | 300 s | `deploy/macmini/autopull.sh` — the auto-deploy puller (H1) |
| buildcard | 1800 s | `wca_build_card.py --hours-ahead 96 --skip-scorers` |
| goalscorers | 10800 s | `wca_build_card.py --hours-ahead 96 --goalscorers-only` |
| backup | 900 s | `deploy/macmini/backup.sh` (local-only ledger snapshots, keeps 48) |
| pmpropose | 1800 s | `wca_pm_propose.py` — parks PM proposals, DMs admin, never places |
| pmredeem | 3600 s | `wca_pm_redeem.py --notify` — emulated-GTD cancel of stale orders |
| closecapture | 600 s | `wca_close_capture.py` — closing odds + CLV stamps (1X2-only, §9.3) |
| publish | 1800 s | `deploy/publish_site.sh` — regen + auto-commit + push site feeds |
| watchdog | 300 s | `deploy/macmini/watchdog.sh` — daemon liveness ONLY, zero git checks |
| positions | 3600 s | `wca_positions_sync.py --once` (SHADOW) |
| archive | 21600 s | `wca_archive.py snapshot --db data/wca.db` (S3 mirror only if creds set) |
| pmdrift | 3600 s | `wca_pm_reconcile.py --check --notify` (detect-only) |
| analytics | 3600 s | `scripts/wca_build_analytics.sh` — 8001 feeds + predledger (local-only, §7.2) |
| venues | 7200 s | `wca_venues_benchmark.py --pred-db data/dev.db` |

**TWO-INSTALLERS caveat.** A legacy installer `deploy/install_services.sh:23-39` also lives on main and wires a DIFFERENT set of 9 services — including `com.wca.sync` (300 s → `deploy/sync.sh`, which on conflict runs `git checkout -- .` + stash drop, destroying fresh daemon artifacts — `deploy/sync.sh:42-48`) and differently-spelled labels (`com.wca.build_card` vs `com.wca.buildcard`), so both generations can coexist under launchd. `deploy/README.md:71-81` still documents the LEGACY set (and calls publish "hourly" — stale; it is 1800 s per `services.env:67`). Which generation is actually loaded on the mini is [UNVERIFIED-MINI: current macmini/ generation assumed]. `deploy/macmini/install.sh:10-15` warns activation is a HUMAN step — merging a job into services.env does not start it.

**Other plists (static, checked in):**

| Plist | Machine | Runs | Interval |
|---|---|---|---|
| deploy/macbook/com.wca.feedpull.plist | MacBook | `deploy/macbook/pull_feeds.sh` (rsync feeds for localhost) | 600 s |
| deploy/macbook/com.wca.positions.plist | MacBook | `scripts/wca_positions_macbook.sh` (Betfair fetch on VPN → SCP → mini apply; `WCA_POSITIONS_LIVE=1` commented out) | 3600 s |
| deploy/mini/com.wca.testbook.plist | mini | `scripts/wca_test_book_cycle.sh` (paper book settle/trade/mark; paused via `deploy/testbook.switch` = `off`) | 600 s |
| scripts/ops/com.wca.caffeinate.plist | mini | `/usr/bin/caffeinate -dimsu` (keep-awake) | KeepAlive |

### 1.4 CI — 7 workflows

| Workflow | Trigger | Purpose | Pushes data to main? |
|---|---|---|---|
| pytest.yml | PR to main + push to main | test suite, py3.9 | No. **Advisory-only** — branch protection cannot be enabled (private repo, free plan; `gh api …/branches/main/protection` → 403). Suite currently RED (H-S1). |
| daily-card.yml | cron `17 * * * *` (**hourly**, despite the name) | clean results, build card, site feeds, bet_recs | Yes — card artifacts + `site/*.json`, "Auto-sync: daily card + site data" (daily-card.yml:114-127) |
| hourly-odds.yml | cron `0 * * * *` | scores/forest/scores_markets feeds + odds snapshot | Yes — feeds + `data/odds_price_history.jsonl` |
| pm-snapshot.yml | cron `7,37 * * * *` (2×/hour) | advancement refresh + PM price snapshot | Yes — `data/pm_price_history.jsonl` only (DB rows ephemeral, §9.4) |
| clean-results.yml | cron `5 7,13,19 * * *` (3×/day) | refresh + verify martj42 dataset | Yes — `data/raw/martj42_cleaned.csv` + processed results |
| daily-promos.yml | cron `47 8 * * *` (daily) | `wca_promosd.py --once` + promos feed | Yes — `site/promos_data.json` |
| archive.yml | cron `17 4 * * *` (daily) | files-only parquet archive, `--no-ledger` | No (artifact upload only) |

GH Actions cadence is therefore **hourly-or-better** — the "~4×/day backup" figure survives only in stale comments (`deploy/publish_site.sh:6`, `services.env:67`). Auto-sync commits are pushed with the default `GITHUB_TOKEN`, and GITHUB_TOKEN pushes do **not** trigger `on: push` workflows — most recent main history has no pytest status at all (deploy_ci report §4).

### 1.5 Vercel — REMOVED (2026-07-08)

Vercel hosting is gone. Deployments had already been blocked at the dashboard level and the leftover Vercel PR checks permanently failed (blocking merges), so both `vercel.json` files (root + `site-analytics/`) were deleted from the repo. **The localhost servers are the ONLY serving surfaces: `site/` on `localhost:8000`, `site-analytics/` on `localhost:8001`** — each machine serves its own pulled tree. Note the Vercel↔GitHub INTEGRATION lives in the Vercel dashboard, not the repo: it must be disconnected there (Project Settings → Git → Disconnect, for both `fifa-world-cup-2026` and `world-cup-26-v2`) or Vercel's PR checks keep appearing. Historical note: while the analytics Vercel project existed, nothing ever committed `site-analytics/data/*.json` (§7.2), so its deployed feeds changed only when a human committed them — the mechanism of the analytics freeze.

---

## 2. Code map

### 2.1 Scale

- **149** `.py` modules under `src/wca` (verified `git ls-tree -r`).
- **16 subpackages**: 15 direct children (`archive, bench, bot, conductor, data, intel, ledger, markets, mc, models, pm, predledger, rigor, sim, testbook`) + 1 nested (`intel/sources`). The brief's "23 subpackages" is wrong.
- **39 loose top-level modules** (excl. `__init__.py`) — the brief's "~50" is high. Theme clusters (duplication report §5):

| Theme | Count | Modules |
|---|---|---|
| Card / site presentation | 10 | card, cardcache, sitedata, scorespage, dashboard, tracking, linemove, promosdata, arbdata, nextmatch |
| Benchmarks / analytics engines | 6 | clvbench, venuesbench, venuesdata, winrate, outrightedge, snapshot_freshness |
| Exposure / risk | 4 | exposure, exposure_corr, exposure_dashboard, accas |
| Polymarket | 5 | pmanalytics, pmhistory, pmmovers, pmtrends, positions_sync |
| Arb / FX / trading math | 3 | arb, arbfx, fx |
| Promo / matched betting | 4 | boosts, matched, offers, promos |
| Model & prediction persistence | 3 | advancement, modelpreds, news |
| Ops / capture / sync | 4 | closecapture, sync, pollsched, venues (name canon — ledger-adjacent) |

These cluster naturally into ~7 scoped subpackages in Phase 1, leaving the top level near-empty.

- **122 test files** under `tests/` (verified).

### 2.2 The clean model core — protect it

`src/wca/models/` is exactly 10 substantive modules + empty `__init__.py`, each with a matching test file (model report §1): `betbuilder, dixon_coles, elo, goalblend, playerprops, props, scorers, scores, structural, venues`. No duplication anywhere: `class DixonColesModel` / `EloRater` / `EloOutcomeModel` / `GoalBlend*` each defined once; backtests import and re-fit the canonical classes, never re-implement.

`ClobTrader` is exactly ONE class (`src/wca/pm/trader.py:377`); every execution path imports it, and both the Betfair and Smarkets modules declare it "the only supported execution path" (`src/wca/data/betfair.py:80`, `src/wca/data/smarkets.py:348`).

**Protected set (do not fork, do not casually refactor):** `models/` (all 10), `markets/bankroll.py` (sizing SSOT, §10), `pm/trader.py` (guardrail stack, §8.4), `ledger/store.py` (write choke point, §6), `src/wca/data/` feed adapters.

### 2.3 The microstructure research island

`scripts/microstructure/` is **11** scripts (not 13): `arbitrage, bookmaker_profiles, clv, consensus, disagreement, exchange_vs_book, liquidity, movement_prediction, polymarket, price_discovery, synthetic_pricing`. Wired to **nothing** — no launchd, no CI, no bot handler, no publish chain (`git grep -i microstructure origin/main -- .github deploy scripts/ops src/wca` → one docstring mention). Outputs are 12 committed static JSONs under `site/microstructure/` (11 script outputs + `index.json`, which **no script writes** — and index.json is the only feed `site/microstructure.js:176` actually fetches). One-shot research artifacts from the 2026-06-26 recon, frozen since.

---

## 3. Scripts manifest

**The headline deliverable.** `git ls-tree -r --name-only origin/main scripts` → **110 files**: 91 top-level + 7 `scripts/archive/` + 11 `scripts/microstructure/` + 1 `scripts/ops/` plist.

**Tier totals:** launchd-wired **17** (16 scripts + 1 plist) · CI-wired **10** · bot-backed **2** · chain-called **13** · operator-manual **38** · DEAD **23** · archived **7** = 110.

Adversarial second pass confirmed all 23 dead (zero execution references each) and **RESCUED three** scripts the brief claimed dead: `wca_backfill_accounts.py` + `wca_canon_venues.py` (importlib-executed by tests — test-referenced, keep), and `wca_pm_probe.py` (operator diagnostic cited in live bot code, `src/wca/bot/app.py:2099`).

**Standout gap:** `scripts/wca_betbuilder.py` is called "the cron build" by the live bot (`app.py:847`) and "cron-only" by the audit doc (`02_codebase_audit.md:85`) — but **no scheduler anywhere on origin/main runs it**. `/betbuilder` serves a cache nothing refreshes.

**Name-collision traps** (dead/wired siblings that differ by suffix — the #1 way to grep the wrong file):
- `wca_tracking.py` (DEAD) vs `wca_tracking_data.py` (wired) — every apparent live ref to the former was actually the latter.
- `wca_arb.py` (manual) vs `wca_arb_data.py` (wired) vs `wca_arb_history.py` (DEAD).
- `wca_advancement.py` (manual) vs `wca_advancement_data.py` (wired) vs `wca_advancement_history.py` (wired).
- `wca_benchmark.py` (DEAD) vs `wca_venues_benchmark.py` (wired) vs `build_benchmarks.py` (DEAD).

Bot wiring truth: the bot **never subprocess-executes scripts** (its only `subprocess.run` is `git pull`, `app.py:291`). "Bot-backed" means the handler serves a cache WRITTEN by a script; 12 grep hits map to only 4 distinct scripts (`wca_build_card`, `wca_betbuilder`, `wca_structure`, `wca_pm_probe` env-hint).

### 3.1 launchd-wired (17, incl. 1 plist)

| script | tier | wiring evidence | notes |
|---|---|---|---|
| scripts/wca_bot.py | L | deploy/macmini/install.sh:35; deploy/install_services.sh:23 | @gamble1_bot daemon (com.wca.bot, KeepAlive) |
| scripts/wca_conductor.py | L | deploy/macmini/install.sh:36 | @WorldCupDev conductor daemon, `.env.conductor` |
| scripts/wca_snapshotd.py | L | deploy/macmini/install.sh:37; deploy/install_services.sh:24 | dense odds-snapshot daemon |
| scripts/wca_newsd.py | L | deploy/macmini/install.sh:38 | news daemon, 600 s cycle |
| scripts/wca_promosd.py | L+CI | deploy/macmini/install.sh:39; daily-promos.yml:53 (`--once`) | daemon on mini AND daily CI catalog refresh |
| scripts/wca_build_card.py | L+CI+B | install.sh:40 (buildcard) + :41 (goalscorers); daily-card.yml:66; bot cache app.py:611,753,790 | most-wired script in repo |
| scripts/wca_pm_propose.py | L | deploy/macmini/install.sh:44; install_services.sh:26 | parks PM proposals → Telegram; never places |
| scripts/wca_pm_redeem.py | L | deploy/macmini/install.sh:45; install_services.sh:27 | emulated-GTD cancel of stale PM orders |
| scripts/wca_close_capture.py | L | deploy/macmini/install.sh:46; install_services.sh:25 | closing-odds + CLV stamping (1X2-only) |
| scripts/wca_archive.py | L+CI | deploy/macmini/install.sh:49; archive.yml:45-46 (`--no-ledger`) | dual-wired archival |
| scripts/wca_pm_reconcile.py | L | deploy/macmini/install.sh:50 (`--check --notify`, hourly) | drift ALERT only; `--apply` manual |
| scripts/wca_positions_sync.py | L | install.sh:51 (hourly `--once`, SHADOW); wca_positions_macbook.sh:43,63; install_services.sh:31 | 3 wiring paths |
| scripts/wca_build_analytics.sh | L | deploy/macmini/install.sh:52 (hourly `analytics` job) | chain-parent of 7 feed scripts |
| scripts/wca_venues_benchmark.py | L | deploy/macmini/install.sh:53 (2 h `venues` job) | depends on analytics job's predledger backfill |
| scripts/wca_positions_macbook.sh | L | deploy/macbook/com.wca.positions.plist | MacBook hourly; Betfair fetch → SCP → mini apply |
| scripts/wca_test_book_cycle.sh | L | deploy/mini/com.wca.testbook.plist (600 s) | paper book; paused via deploy/testbook.switch (`off`) |
| scripts/ops/com.wca.caffeinate.plist | L | itself a LaunchAgent (`caffeinate -dimsu`) | config artifact, not code |

### 3.2 CI-wired (10)

| script | tier | wiring evidence | notes |
|---|---|---|---|
| scripts/wca_clean_results.py | CI | clean-results.yml:43 (3×/day); daily-card.yml:43 | imports wca_build_wc2026_results (its :124) |
| scripts/wca_site.py | CI+CH | daily-card.yml:79; deploy/publish_site.sh:23 | main site feed (data.json + linemove.json) |
| scripts/wca_scores_data.py | CI+CH | hourly-odds.yml:53; daily-card.yml:96; publish_site.sh:17; imported by wca_forest_data.py:53 | 4 wiring paths |
| scripts/wca_scores_markets_data.py | CI+CH | hourly-odds.yml:66; publish_site.sh:21 | scorelines per-round breakout |
| scripts/wca_forest_data.py | CI+CH | daily-card.yml:100; hourly-odds.yml:70; publish_site.sh:22 | |
| scripts/wca_betrecs.py | CI+CH | daily-card.yml:113; publish_site.sh:37 | reads arb_data.json built at publish:36 first |
| scripts/wca_snapshot_odds.py | CI | hourly-odds.yml:84 | single-shot sibling of wca_snapshotd |
| scripts/wca_promos_data.py | CI | daily-promos.yml:57 | Promos tab feed |
| scripts/wca_advancement_data.py | CI+CH | pm-snapshot.yml:41; publish_site.sh:32 | 12 h model cache + live PM prices |
| scripts/wca_pm_snapshot.py | CI | pm-snapshot.yml:45 | twice-hourly PM price snapshot (JSONL committed; DB rows ephemeral) |

### 3.3 bot-backed (2 — plus wca_build_card above)

| script | tier | wiring evidence | notes |
|---|---|---|---|
| scripts/wca_betbuilder.py | B | app.py:828,847 serves its cache; 02_codebase_audit.md:85 calls it "cron-only" | **GAP: no cron/launchd entry exists — /betbuilder cache has no refresher** |
| scripts/wca_structure.py | B+M | app.py:916 ("Run scripts/wca_structure.py first"); tests/test_structure.py:10-12 exec it | manual regen; bot serves snapshot |

### 3.4 chain-called (13)

| script | tier | wiring evidence | notes |
|---|---|---|---|
| scripts/wca_predledger.py | CH | wca_build_analytics.sh:24-26 (ensure/backfill/publish) | under launchd `analytics` job |
| scripts/wca_winrate_data.py | CH | wca_build_analytics.sh:31-32 | |
| scripts/wca_rigor_data.py | CH | wca_build_analytics.sh:31-32 | |
| scripts/wca_risk_pnl_data.py | CH | wca_build_analytics.sh:31-32 | |
| scripts/wca_clvbench_data.py | CH | wca_build_analytics.sh:31-32 | |
| scripts/wca_tracking_data.py | CH | wca_build_analytics.sh:31-32; publish_site.sh:24 | NOT the dead wca_tracking.py |
| scripts/wca_exposure_data.py | CH | wca_build_analytics.sh:31-32; publish_site.sh:25 | runs the live event_bets exposure path (§5, F8) |
| scripts/wca_advancement_history.py | CH | deploy/publish_site.sh:27 | |
| scripts/wca_arb_data.py | CH | deploy/publish_site.sh:36 | refreshed before betrecs |
| scripts/wca_lilac_ledger.py | CH | deploy/publish_site.sh:50 | 8002 lilac terminal build |
| scripts/wca_test_book.py | CH | wca_test_book_cycle.sh:47-49 (settle/trade/mark) | paper book; persists PM top-of-book marks (§9.4) |
| scripts/wca_pm_fire.py | CH | wca_place_server.py:121 (SSH to mini); docs/operations/oneclick_fire.md:12 | REAL-MONEY path; PM_DRY_RUN gated; idempotent |
| scripts/wca_build_wc2026_results.py | CH | wca_clean_results.py:124 (`import`); tests/test_build_wc2026_results.py:24 | runs inside CI-wired clean_results |

### 3.5 operator-manual (38 = 27 top-level + 11 microstructure)

| script | tier | wiring evidence | notes |
|---|---|---|---|
| scripts/wca_cli.py | M | README.md:118; tests/test_ledger.py:547, tests/test_ev_on_record.py:21 exec it | canonical manual ledger CLI (mini) |
| scripts/wca_settle.py | M | src/wca/closecapture.py:15; tests/test_closecapture.py:608 | manual settle for non-1X2 |
| scripts/wca_override.py | M | src/wca/ledger/store.py:315; site/app.js:505 | manual_override writer |
| scripts/wca_dashboard.py | M | tests/test_dashboard.py:343 executes it | legacy HTML dashboard |
| scripts/wca_matched.py | M | tests/test_matched.py:196,219; tests/test_offers.py:214 | matched-betting CLI |
| scripts/wca_advancement.py | M | README.md:118; 02_codebase_audit.md:20 | advancement sim + PM edge CLI; NOT scheduled (advancement_latest.json goes stale) |
| scripts/wca_arb.py | M | README.md:135 | live-API sweep; audit says "DO NOT RUN"; overlaps intel/arb (§4.4) |
| scripts/wca_cashout_watch.py | M | .env.example:24; docs/cashout.md | manual watcher |
| scripts/wca_pm_approve.py | M | src/wca/pm/relayer.py:11; tests/test_pm_relayer.py:450 | dry-run relayer approve |
| scripts/wca_pm_watch.py | M | src/wca/pm/positions.py:3,29; ledger/store.py:468 | positions poll CLI; **direct INSERT bypass** (§6.2) |
| scripts/wca_pm_probe.py | M | src/wca/bot/app.py:2099 (env-hint); README.md:118; docs/polymarket_v2_funding.md:9,56 | **RESCUED** — operator diagnostic, not dead |
| scripts/wca_pm_analysis.py | M | docs/pm_snapshot_guide.md | snapshot analysis |
| scripts/wca_pm_analytics_suite.py | M | src/wca/pmanalytics.py:6 (CLI wrapper) | movers/MTM/term-structure suite; ad-hoc only |
| scripts/wca_outright_edge_data.py | M | tests/test_outright_edge_feed.py:12-13 exec | feed builder, unscheduled |
| scripts/wca_player_props.py | M | src/wca/models/playerprops.py:8 | props CLI |
| scripts/wca_build_players_db.py | M | src/wca/models/playerprops.py:45 ("NOT on every box") | players.db builder — the dormant StatsBomb entry point |
| scripts/wca_intel_collect.py | M | src/wca/intel/poller.py:14 | |
| scripts/wca_market_intel.py | M | src/wca/intel/feed.py:7 | |
| scripts/wca_props_data.py | M | src/wca/models/props.py:125; models/betbuilder.py:29 | calibration-constant provenance |
| scripts/serve_site.py | M | .env.example:38; docs/architecture/SYSTEM_MAP.md | localhost:8000 dev server |
| scripts/serve_analytics.py | M | site-analytics/README.md | localhost:8001 dev server |
| scripts/gh_pr.sh | M | AGENTS.md:19,58,63 | conductor PR fallback; runner uses in-process REST (runner.py:477), never execs this |
| scripts/wca_worktree_cleanup.sh | M | docs/operations/RUNBOOK.md; docs/operations/conductor.md | |
| scripts/wca_place_server.py | M | docs/operations/oneclick_fire.md:41,61 | localhost fire-button bridge; parent of wca_pm_fire.py |
| scripts/wca_ledger_audit.py | M | only ref = comment wca_override.py:7 | active operator tool per session memory (dry-run audit on mini) — keep |
| scripts/wca_backfill_accounts.py | M | tests/test_account_source.py:311-312 importlib-exec | **RESCUED** — test-referenced, not dead |
| scripts/wca_canon_venues.py | M | tests/test_venue_canon.py:20,24 importlib-exec | **RESCUED** — test-referenced, not dead |
| scripts/microstructure/arbitrage.py | M | self-doc run cmd :52 → site/microstructure/arbitrage.json | one-off research generator; no scheduler |
| scripts/microstructure/bookmaker_profiles.py | M | self-doc :36 → bookmaker_profiles.json | " |
| scripts/microstructure/clv.py | M | self-doc :31 → clv.json | " (Shin devig — §4.1) |
| scripts/microstructure/consensus.py | M | self-doc :49 → consensus.json | " |
| scripts/microstructure/disagreement.py | M | self-doc :33 → disagreement.json | " |
| scripts/microstructure/exchange_vs_book.py | M | self-doc :56 → exchange_vs_book.json | " |
| scripts/microstructure/liquidity.py | M | self-doc :42 → liquidity.json | " (the "NO depth/volume/queue" disclaimer, §9.4) |
| scripts/microstructure/movement_prediction.py | M | self-doc :55 → movement_prediction.json | " |
| scripts/microstructure/polymarket.py | M | self-doc :6 → polymarket.json | " |
| scripts/microstructure/price_discovery.py | M | self-doc :28 → price_discovery.json | " |
| scripts/microstructure/synthetic_pricing.py | M | self-doc; comment refs in two dead scripts | " |

### 3.6 DEAD candidates (23 — zero execution references on origin/main)

Verification: `git grep -P "<name>(?![a-zA-Z0-9_])" origin/main`; dead iff remaining hits are only itself, docs/README/TODO, comments/docstrings, or its own committed output artifact.

| script | remaining refs (all non-executable) | verdict |
|---|---|---|
| scripts/wca_event_ev.py | README.md:88,118,134; TODO.md:39,104; docs/research | CONFIRMED dead (F8 standalone — §5) |
| scripts/wca_recalibrate_dc_level.py | comments: card.py:84,700; dixon_coles.py:753 | CONFIRMED (one-off recalibration, done) |
| scripts/wca_backfill_model.py | none | CONFIRMED |
| scripts/wca_backfill_pm_ev.py | none | CONFIRMED |
| scripts/wca_pm_try.py | none | CONFIRMED |
| scripts/acca_coverage_optimizer.py | TODO.md:111 | CONFIRMED |
| scripts/build_benchmarks.py | own output provenance note site/benchmarks_data.json:7 | CONFIRMED (nothing regenerates the feed benchmarks.js reads) |
| scripts/wca_validation_report.py | docs/research/model_and_rec_validation_report.md:14,58,256 | CONFIRMED per zero-wiring definition; **doc positions it as a re-runnable manual report — flag delete-or-archive rather than silent delete** |
| scripts/wca_morning.py | 02_codebase_audit.md:162 | CONFIRMED |
| scripts/wca_fix_betfair_venue.py | none | CONFIRMED |
| scripts/wca_benchmark.py | none | extra find |
| scripts/wca_arb_history.py | none | extra find |
| scripts/wca_import_polymarket.py | none | extra find (also a record_bet bypass — §6.2) |
| scripts/wca_pm_transfer.py | none | extra find |
| scripts/wca_reconcile_1x2_mc_report.py | none | extra find |
| scripts/wca_snapshots.py | none | extra find |
| scripts/gen_wc_calendar.py | none | extra find |
| scripts/wca_tracking.py | zero for `wca_tracking(?!_data)` — every stem hit was wca_tracking_data | extra find (name-collision trap) |
| scripts/wca_clv_by_bet.py | comments in two dead scripts; committed output data/analysis/clv_by_bet.csv | extra find (mislabels EV as CLV — §4.1) |
| scripts/wca_recompute_open_bets.py | comments: card.py:84,696 | extra find (one-off migration, done) |
| scripts/wca_matchevents_data.py | TODO.md only | extra find |
| scripts/wca_price_scorers.py | TODO.md; 02_codebase_audit.md | extra find |
| scripts/wca_exposure_sizer.py | docs/research 00_foundation.md, 02_codebase_audit.md | extra find |

### 3.7 archived (7)

| script | evidence |
|---|---|
| scripts/archive/wca_ledger_decision_analysis.py | zero refs |
| scripts/archive/wca_notion_ledger_diff.py | zero refs (the dead Notion mirror CLI — §6.5) |
| scripts/archive/wca_pm_backfill_archive.py | zero refs |
| scripts/archive/wca_pm_frontier.py | zero refs |
| scripts/archive/wca_pm_movers.py | zero refs |
| scripts/archive/wca_pm_trends.py | referenced only by sibling wca_pm_trends_clob.py |
| scripts/archive/wca_pm_trends_clob.py | zero refs |

---

## 4. Duplication inventory

### 4.1 CLV — not 8 divergent computations; one real defect (three devig regimes)

CLV is the primary KPI, so the "CLV ×8" claim was audited line-by-line. The 8 sites decompose into consistent layers, not 8 formulas:

**(a) Writers of `bets.clv` — 4 sites, all algebraically identical** (`odds_backed / fair_close − 1`):
`src/wca/closecapture.py:435` · `src/wca/ledger/store.py:838` (`set_closing_odds`) · `src/wca/bot/app.py:1011` (manual `/settle`) · `scripts/wca_settle.py:21,102`. No divergence at the write layer.

**(b) Aggregators — re-loop the stored column, compute nothing new:**
`ledger/reports.py clv_report` :369-412 (canonical) + `ladder_summary` :420-442; `dashboard.py:171-216` (duplicate loop, subtly different eligibility guard :201-203); a 9th read-only copy in dead `scripts/wca_validation_report.py:522-554`.

**(c) Model-CLV benchmark (`p_close / p_model − 1` — a DIFFERENT KPI sharing the name):** implemented TWICE — `src/wca/clvbench.py:246` (canonical: placebo permutation, Wilson CIs, wired into the live analytics job) and `src/wca/bench/report.py:121` (unwired duplicate with its own private multiplicative devig `bench/sources.py:217-227`; `bench/metrics.py:6` admits duplicating `clvbench.trimmed_mean`).

**(d) Mislabel:** dead `scripts/wca_clv_by_bet.py:49-50` computes `odds × p_model_fair − 1` — that is EV vs the model, not CLV vs any close.

**(e) Basis divergence:** `scripts/microstructure/clv.py:69-83` builds its consensus close with **Shin** devig; the ledger close uses **multiplicative**.

**The real defect is THREE devig regimes** producing three different "fair closes"/"fair probs" for the same raw prices:
1. Model h2h input: per-book **Shin**, then cross-book **median**, renormalised (`src/wca/card.py:767-789`).
2. Ledger close: **multiplicative** per-book then mean (`src/wca/tracking.py:348-384 devig_consensus`, used by closecapture and clvbench) — plus the private copy in `bench/sources.py:217`.
3. Microstructure: **Shin** (`scripts/microstructure/clv.py:70-82`).
Shin vs multiplicative systematically disagree on longshots, so "the close" for the same bet differs across surfaces. The canonical devig library `src/wca/markets/devig.py` (`METHODS = ("multiplicative","power","shin")` :63) exists but `tracking.py` and `bench/sources.py` do not use it. Eligibility filters also differ per surface (clv_report: any bet with a close; ladder: settled won/lost only; dashboard: any closing_odds; microstructure: settled incl. void), so headline "avg CLV" differs from the same DB.

**DECISION (ratified):** one shared CLV formula function (`clv(odds_taken, fair_close) = odds_taken/fair_close − 1`) imported by all four writers; `ledger/reports.clv_report` is the canonical bet-CLV surface (dashboard.py re-loop calls it instead); `clvbench` is the canonical model-CLV benchmark; **DELETE** the `bench/report.py` duplicate and its private devig `bench/sources._devig_three`; `rigor/clv` stays (it is a statistics library over precomputed CLV, not a duplicate); unify all devig through `wca.markets.devig` with named methods (`tracking.devig_consensus` becomes a thin wrapper); rename or archive `wca_clv_by_bet.py`'s metric as EV (it is dead anyway); if `microstructure/clv.py` is kept it must declare its Shin basis in the output JSON or source the ledger close.

### 4.2 Exposure — two correlated engines CONFIRMED, both currently execute

`build_exposure_data` (`src/wca/exposure.py:173`) runs BOTH engines in one feed build:
- the **legacy 1X2-only joint scenario engine** `_portfolio_scenarios` (`exposure.py:442`, called at :272) — which `exposure_corr.py`'s own docstring (:1-15) declares wrong for same-fixture combos ("their joint P&L is wrong");
- the **scoreline-matrix engine** via `_correlated_exposure` (`exposure.py:289-349`) delegating to `exposure_corr.build_correlated_exposure` (shared scoreline matrix from persisted lambdas, cross-fixture convolution; also reused by `accas.py:357,868-869`).

`exposure_dashboard.py` is a thin presentation layer over the result (not a third engine). Live wiring: `scripts/wca_exposure_data.py` → `wca_build_analytics.sh:31` → launchd `analytics` job.

**DECISION (ratified):** canonical = **`exposure_corr`**. Retire the legacy `_portfolio_scenarios` engine (reimplement its output as a marginalisation of exposure_corr's per-fixture distributions, or delete outright). `exposure.py` keeps slate-building / blind-spot / gap-plug logic; `exposure_dashboard.py` stays as presentation.

### 4.3 Venues ×4 — a naming collision, NOT duplication

| Module | Actual domain |
|---|---|
| `src/wca/venues.py` (125 ln) | **Bookmaker-name canonicalisation** (`canon_platform`) — ledger-adjacent leaf module (store.py:57, bot app.py:327) |
| `src/wca/models/venues.py` (113 ln) | **Stadiums** — 2026 WC venue table, altitude/co-host Elo adjustment (`host_advantage_points` :91; live via card.py:1053, advancement.py:220) |
| `src/wca/venuesbench.py` (529 ln) | model-vs-venue benchmark **engine** (pure; explicitly delegates devig to `wca.markets.devig` and stats to `wca.rigor.clv`) |
| `src/wca/venuesdata.py` (638 ln) | read-only DB/join **data layer** for venuesbench |

Two unrelated domains plus an engine/data pair; zero duplicated logic. The problem is discoverability.

**DECISION (ratified):** renames only — e.g. `wca/venues.py` → `wca/bookmakers.py` (or `ledger/venue_names.py`); `models/venues.py` → `models/stadiums.py`; move the benchmark pair into a `venuebench/` subpackage. No merges.

### 4.4 Arb ×4 — one duplicated seam (two scanner orchestrations); math lives once

- `src/wca/arb.py` (403 ln): core math + settlement identity (`settlement_key` :40, `effective_back` :81, `pm_yes_to_decimal` :99, `_arb_from_net` :118, two/three-way :136/:164) **plus its own detectors** (`find_cross_book_arbs` :214, `find_pm_book_arbs` :281, `rank_arbs` :397) — the detectors are consumed only by unwired/dead scripts.
- `src/wca/arbfx.py` (251 ln): FX-adjusted PM↔exchange lock math; imports arb primitives (:26). `DEFAULT_BETFAIR_COMMISSION = 0.06` lives at `arbfx.py:27` — overstated vs real Betfair tiers (Rewards 5%, Basic 2%).
- `src/wca/arbdata.py` (207 ln): site-feed presenter → `site/arb_data.json` (publish chain).
- `src/wca/intel/arb.py` (580 ln): the **second scanner** over the intel-store quote shape, with staleness/`actionable` gates, wired to the live bot `/arb` (app.py:2481,2491). Verified to delegate ALL commission/FX/net-price math to arb/arbfx (`intel/arb.py:257` `_arb._arb_from_net`; `:326` `_arbfx.exchange_lay_net`) — duplication is orchestration-level only.

**DECISION (ratified):** canonical scanner = **`wca.intel.arb`**. Shrink `arb.py` to pure primitives (move/retire its detectors after porting or archiving `scripts/wca_arb.py` / `wca_arb_history.py`, neither wired). `arbfx.py` keeps FX/commission math (and its 0.06 default should be corrected to real tiers when touched); `arbdata.py` stays as the presenter.

---

## 5. Dark features

There are **three** fully-built-but-dark features, not two.

### F7 — two-timescale goal blend (`src/wca/models/goalblend.py`)

- Gate: `fit_models(..., goal_blend: bool = False)` at `card.py:611`; blend branch `card.py:714-724`; when False nothing runs and `FittedModels.goal_blend` stays None.
- The ONLY call site passing `True` is the test (`tests/test_goalblend.py:266`). All 8 production `fit_models(` callers omit it. Confirmed dark.
- Well-tested (~270-line test file: default-off invariance, credibility-k monotonicity, squad-adjustment data gate). Its own docstring declares "TRACKING-ONLY / OOS-gated". `GoalBlendModel.blended` is a drop-in `DixonColesModel` (same predict API, level re-anchored via `apply_wc_level_anchor`, `goalblend.py:317-319`).
- Directly addresses the one confirmed model bias (goals under-prediction ~0.59/game, p≈0.001 — memory: xG goal calibration).

**DECISION (ratified): WIRE, tracking-only first.** Flip `goal_blend=True` in `scripts/wca_build_card.py:258`, persist the blended lambdas alongside the existing `modelpreds` predictions, and compare CLV out-of-sample before the blend ever drives sizing. Staking swap only after the OOS gate clears.

### F8 — event-EV (`scripts/wca_event_ev.py`)

- The standalone script is a confirmed orphan (zero imports, no CI/launchd/tests; manual CLI). It re-fits models itself (:73) and duplicates fee math (inline 0.94 exchange commission :96; `fee_adj_pm_edge` :43) already canonical in `arb.py` — drift risk on a real-money system.
- The LIVE correlated-exposure path the brief conflated with it is real and separate: `exposure.py` `event_bets` (:208, :253) → `_correlated_exposure` (:289) folds O/U/BTTS/correct-score bets into the shared-scoreline joint P&L, run via `scripts/wca_exposure_data.py:160` → `wca_build_analytics.sh:31` → launchd.

**DECISION (ratified): fold any unique totals/BTTS-vs-book EV sweep into the live path (the card/exposure chain), then DELETE the standalone script.**

### F9 — `card.build_event_references` (NEW finding)

- Defined `src/wca/card.py:1406` (formatter :1485), display-only per its own docstring, tested in `tests/test_card_events.py:116-237` — and has **ZERO production callers** (`git grep build_event_references origin/main` minus docs/tests → nothing; the bot uses only `build_score_cards`, app.py:594). `docs/research/wca_alpha_2026/02_codebase_audit.md:161` calls it "live"; that reflects intent, not the call graph.

**DECISION (ratified): wire it into the exposure/event-reference path (it is the natural, already-tested landing zone for F8's EV sweep) or delete it.**

---

## 6. Ledger

### 6.1 Schema

Single SQLite file. `_DEFAULT_DB = "data/wca.db"` (`store.py:65`); `bets` DDL at `store.py:71-94` — 19 base columns: `id, ts_utc, match_id` (free text, NOT a FK), `match_desc, market, selection, platform` (canonicalised at write), `decimal_odds, stake` (PM: stake = shares×fill, odds = 1/fill), `model_prob, market_prob_devig, ev, kelly_fraction, status` (open→won/lost/void/cashed), `settled_pl, closing_odds, clv, notes, manual_override`.

**SIX lazy idempotent `ALTER TABLE` columns** applied on every connect (try/except-pass): `account`, `source` (store.py:294-299), `settled_ts` (:303-307), `manual_override` (:311-321 — **also in the base DDL at :91, i.e. doubly defined**), `token_id`, `cashout_proceeds` (:325-340). No migration framework, no `PRAGMA user_version`, no schema version stamp anywhere for the money ledger. The schema is additionally duplicated as a hand-copied stub in `src/wca/predledger/store.py:195-196` (`_DDL_BETS_STUB`, so predledger's LEFT JOIN views resolve) — drift-prone.

### 6.2 Write choke point — and its three bypasses

`store.record_bet()` (`store.py:162`) is the intended single write path: venue canonicalisation happens exactly here (`canon_platform` at :237); `sync_site` defaults **False** (:179) by deliberate design ("a low-level write must never trigger a git publish", :223-225 — a default-on caused junk commits historically). The pytest publish guard lives at `src/wca/sync.py:113-118` (NOT `ledger/sync.py`, which does not exist).

**The choke point is NOT airtight.** Direct `INSERT INTO bets` on origin/main:
1. `scripts/wca_pm_watch.py:121` — fill-watch inserts open PM rows directly.
2. `scripts/wca_pm_reconcile.py:237` — apply-mode inserts for untracked live positions.
3. `scripts/wca_import_polymarket.py:112` — bulk importer (dead script, but the bypass exists).
4. (internal, intentional) `_insert_cashed_slice` `store.py:752` — cashed-slice rows.
All four skip `canon_platform` (they hardcode the already-canonical string; nothing enforces it).

### 6.3 Lifecycle mutators — implemented and tested

| Mutator | Location | Behaviour | Tests |
|---|---|---|---|
| `settle_bet` | store.py:344 | source-aware P&L: free bet (`source == "offer"`) loss = £0 (:395, :409); lay loss = −liability, win = +stake (:403-405); back win = (odds−1)×stake (:407) | tests/test_ledger.py (36 tests); tests/test_settle_freebet_lay.py |
| `void_bet` | store.py:422-457 | status=void, pl=0, settled_ts stamped | test_ledger.py:201 |
| `settle_cashout` | store.py:515-708 | FIFO share allocation (ORDER BY id :496-513), boundary-row split :660-685, untracked excess at zero cost basis :687-708 | tests/test_cashout_ledger.py (15 tests) |
| `set_closing_odds` | store.py:780-843 | `clv = (taken_odds / c_odds) − 1.0` at :838 | test_ledger.py:227-283 |

### 6.4 Three reconcilers — all detect-only in production

| Reconciler | Default | Scheduled as | Live switch |
|---|---|---|---|
| `wca_pm_reconcile.py` | dry-run; DB opened read-only unless `--apply` (:260, :269) | hourly `pmdrift` = `--check --notify` only (install.sh:50) | `--apply` (manual) |
| `wca_positions_sync.py` | SHADOW (docstring :1); Betfair leg degrades to `[]` on failure (positions_sync.py:119-127) | hourly `--once`, no `--live` (install.sh:51) | `--live` or `WCA_POSITIONS_LIVE=1` |
| `wca_ledger_audit.py` | dry-run (docstring :17-19); `--apply` takes a .db backup first (:253) | not scheduled — human-only, mini-only | `--apply` |

Drift is the normal operating state, repaired manually.

### 6.5 The Notion mirror is dead

`src/wca/ledger/notion_diff.py` writes nothing by design (docstring :1-7); its CLI is archived (`scripts/archive/wca_notion_ledger_diff.py`); zero notion references in workflows or deploy; `scripts/wca_pm_fire.py:322-331` explicitly logs "notion sync skipped — no single-bet append helper exists". The manual snapshot drifts past ~#236 and bulk query is plan-gated. Recommendation carried from the brief: retire.

### 6.6 THE FX DEFECT

`reports.summary()` (`src/wca/ledger/reports.py:545-633`) sums `stake`, `settled_pl`, ROI, `current_bankroll` and the by-source rollup across **every** platform row with no currency column and no FX conversion. Polymarket rows are USD (`_USD_VENUES`, reports.py:187); sportsbook rows are GBP — so every headline scalar is a GBP+USD blend and is not economically meaningful.

The building blocks all exist and sit unused by `summary()`:
- `_platform_currency` — in the **same file**, `reports.py:200-203` (used by the exposure engine :318 and `mc/pnl.py:150`, never by summary; a second copy at `rigor/build.py:65`).
- `totals_by_currency` — built in `dashboard.py:222-234` ("GBP and USD must NEVER be summed") and `sitedata.py:569-736`.
- FX: `bankroll.py:22` `GBP_USD = 1.33` (env-overridable), `gbp_to_usd()` :29-31; `fx.py:16` fallback. None imported by reports.py.

### 6.7 Replication gap

Nothing in the repo guarantees `wca.db` leaves the mini. CI archive runs `--no-ledger` (archive.yml:45 — "cannot touch wca.db, files-only"); the mini's 6-hourly archive job snapshots the DB and mirrors to S3 **only if** `WCA_ARCHIVE_S3_*` creds are set in the mini's unversioned `.env` (`archive/config.py:53-66` returns None unless all set) [UNVERIFIED-MINI: creds presence unknown]; `deploy/macmini/backup.sh` is explicitly local-only (48 rotating snapshots, same machine). The canonical money record has no verified off-box copy.

### 6.8 Three DB-path env vars

| Var | Read by |
|---|---|
| `WCA_DB` | bot `app.py:1625`; `wca_override.py:37`; dead `wca_tracking.py:22` |
| `WCA_DB_PATH` | conductor `config.py:22` + `wca_conductor.py:1058`; `wca_positions_sync.py:57` |
| `WCA_MINI_DB` | `wca_place_server.py:57` (a third name the brief missed) |

Most scripts otherwise take `--db` defaulting to the literal `data/wca.db`. **DECISION (ratified): unify on `WCA_DB_PATH`.**

### 6.9 Pros / cons (verified subset of the brief's assessment)

**Pros:** single canonical file + one intended choke point; correct source-aware P&L (free-bet/lay/cash-out, all tested); CLV as a first-class auto-stamped column; defensive publish discipline (`sync_site=False` + pytest guard are battle scars); reconciliation is read-only everywhere by default.

**Cons:** untracked canonical file on ONE machine (no verified off-box copy); divergent dev/mini forks are a known recurring failure mode with no structural prevention; dead Notion mirror = false second copy; the FX-blended `summary()`; three fragmented reconcilers; schema-by-accretion (6 lazy ALTERs, one doubly defined, no version stamp, duplicated DDL stub); no event history (rows mutated in place; audit trail = free-text notes); `match_id` is an opaque string, not a FK; the choke point has three bypass writers.

---

## 7. Sites

### 7.1 Three trees, 19,334 LOC

| Tree | LOC | Shape | Feeds |
|---|---|---|---|
| `site/` | 9,844 | 8 nav pages: Trades (index), Scores & Markets, Event Markets (forest), Under The Hood (architecture), Promos, Action Desk (arb.html — nav label "Trade Recs"), Microstructure, Benchmarks (`site/index.html:19-28`) | 28 JSON: 16 top-level + 12 under `site/microstructure/` |
| `site-analytics/` | 2,958 | single page, served on localhost:8001 (was its own Vercel project until 2026-07-08) | 16 tracked JSON in `data/` (not 17); `analytics.js:1049-1063` fetches 13; **8 exclusive** to this tree (winrate, rigor, risk_pnl, predledger, venues_benchmark, market_intel, mc_futures, tracking_clv_benchmark) |
| `site-lilac/` | 6,532 | single-page 6-tab terminal (Open Exposure, Scores & Markets, Visuals, Under The Hood, Tracking, Promos — index.html:379-385); data baked into one `const DATA` blob | builder `wca_lilac_ledger.py:205-227` loads 15+ feeds from site-analytics/data; `inject()` at :277-287 |

Corrections to the brief: `site/visuals.html` and `site/tracking.html` are 19-line **redirect stubs** (meta-refresh to scores.html / index.html), not standalone variants. `site/terminal.html` IS standalone but **orphaned**: its data is a baked `const D` blob dated 2026-06-11 and its claimed builder `scripts/wca_terminal.py` does not exist anywhere on origin/main.

### 7.2 THE FREEZE BUG — proven

`deploy/publish_site.sh` copies fresh feeds into site-analytics on every 30-min publish (**lines 44-47**):

```bash
for _f in data scores_data scores_markets bet_recs arb_data exposure_data exposure_dashboard \
          tracking_data advancement_data advancement_history forest_data linemove; do
  [ -f "site/$_f.json" ] && cp "site/$_f.json" "site-analytics/data/$_f.json" 2>/dev/null || true
done
```

…but its `git add` (**lines 57-61**) never stages them:

```bash
git add site/data.json site/linemove.json site/scores_data.json site/scores_markets.json site/forest_data.json site/tracking_data.json \
        site/exposure_data.json site/exposure_dashboard.json site/advancement_history.json site/advancement_data.json \
        site/bet_recs.json site/arb_data.json site-lilac/index.html \
        data/card_latest.md data/next_latest.md data/model_predictions.json \
        data/advancement_current_vs_pretournament.json
```

Only `site/*.json`, `site-lilac/index.html` and four card artifacts are staged. No CI workflow touches site-analytics at all (`git grep site-analytics origin/main -- .github/workflows/` → zero). The mini's hourly `analytics` job regenerates the exclusive feeds but contains no git command. Result: mini-local 8001/8002 refresh; **origin/main (and the analytics Vercel deploy) froze** — 14 of 16 analytics feeds last committed 2026-06-25, `venues_benchmark` 06-28, `market_intel` 06-29; internal `meta.generated` values agree. As a side effect the copies also become 16 permanently-dirty tracked files on the mini — the fuel for Hazard H1.

### 7.3 Scheduler topology

1. **Mini publish job** — 30 min (`services.env:67`; `deploy/README.md:78` "hourly" is stale), commits + pushes site/ feeds.
2. **GitHub Actions** — **hourly or better** (daily-card hourly at :17, hourly-odds hourly, pm-snapshot 2×/hour), not the documented "4×/day".
3. **Manual** — microstructure (frozen at the 2026-06-26 analysis run) and benchmarks_data.json.

All spot-checked feeds DO carry `meta.generated`/`generated_at` — staleness is invisible only because **no UI surfaces it**.

### 7.4 Orphan artifacts (no generator on origin/main)

| Artifact | Problem |
|---|---|
| `site/terminal.html` | standalone page; builder `wca_terminal.py` absent; data baked 2026-06-11 |
| `site-analytics/data/mc_futures.json` | no generator anywhere (docs reference a `wca_mc_futures.py` that does not exist); meta.generated 2026-06-23 |
| `site/microstructure/index.json` | the ONLY feed microstructure.js fetches (:176); no script writes it — hand-maintained |
| `site/benchmarks_data.json` | generator `build_benchmarks.py` is dead; nothing regenerates the feed benchmarks.js reads |

### 7.5 Fire-button guard stack (site side)

`scripts/wca_place_server.py`: binds `127.0.0.1:8010` (:51-52, :276) + belt-and-braces client loopback check (:205-207, 403 otherwise); `X-WCA-Place-Token` shared secret **fails closed** when env unset (:233-236); forwards its own `PM_DRY_RUN` (default "1", :79) and never hardcodes 0; `WCA_PLACE_MAX_USD` default 100 (:68); CORS pinned to localhost (:199). `site/arb.js`: `IS_LOCAL` hostname guard (:135-140) — any non-localhost copy renders **no button at all** (placeCell returns "" :174; reveal no-ops :246-248); per-click idempotency nonce (:163-168). Downstream: `wca_pm_fire.py` idempotency + caps (§8.5).

---

## 8. Telegram + execution

### 8.1 Two isolated bots — the single best safety property; preserve it

- **Ops bot @gamble1_bot** (`src/wca/bot/app.py`, exactly **2,808 lines**; launchd com.wca.bot). The money surface: ledger reads, market pricing, betslip-screenshot ingest, PM fire/settle. Dependency-free long-poll (`bot/telegram.py`), string-match if-chain dispatch (`dispatch()` at app.py:2602, :2615-2649).
- **Conductor @WorldCupDev** (`src/wca/conductor/`; launchd com.wca.conductor). Infrastructure only — fans `/task` prompts to headless agents → PRs. Verified: **zero** imports of `pm.trader`/`ClobTrader`/`record_bet` anywhere under `src/wca/conductor` (its `store.py` is its own task-state store). Different tokens, different processes. The code-writing agent cannot place a bet.

### 8.2 Command surface

Read-only (any member of an authorized chat): `/start /help /summary /bets /clv /card /next /goalscorers /betbuilder /scores /accas /structure /pm /arb /boost /ping` (dispatch app.py:2615-2649). Corrections to the brief:
- **`/movers` does not exist** in the bot (zero grep hits in app.py) — the movers feature lives in the sites.
- **`/settle` is NOT money-gated.** `_MONEY_RE` (app.py:204-211, matched pre-dispatch at :2791-2793) gates only `yes/no` (+ provenance tags), `Y/N BET-/PM-`, and `REDEEM`. `/settle` routes through un-gated dispatch (:2643-2644) — any authorized chat member can write settlements to the bets table. The run() comment claiming everything else is read-only is wrong. (`/restart` is separately admin-gated in run(), :2761-2785.)
- Admin soft-gate: `_is_admin` (app.py:218-227) returns **True for everyone** when `TELEGRAM_ADMIN_USER_ID` is unset; startup only prints a warning (:2672) and continues. [UNVERIFIED-MINI: whether the mini .env sets it.]
- **`Y/N BET-<id>` is a stub** (app.py:2281-2284): acknowledges, writes nothing — "Ledger write pending card-generator wiring."

### 8.3 PM execution chain: propose → confirm → sign

1. `scripts/wca_pm_propose.py` (launchd, 30 min) builds proposals, parks them in the `pm_parked` SQLite table (DDL app.py:1638; survives restarts), DMs the admin — **never places** (docstring :10-15; refuses to park if admin id unset :793-797; its only ClobTrader use is a read).
2. Confirm path A: Telegram `Y PM-<n>` → `handle_confirmation` (:2257) → `_execute_parked_order` (**app.py:2071**) → `ClobTrader.place_order` (:2138-2157), forwarding `market_question` so the keyword allowlist actually gates.
3. Confirm path B: one-click PLACE → loopback server → SSH to mini → `wca_pm_fire.py` → same trader core.
4. Failure containment: `LiveOrderUnconfirmed` (trader.py:168-201, carries full order params) → admin alert "may be ON-CHAIN but UNLOGGED … do NOT blindly resend" (app.py:2164-2186); status marked `unconfirmed`, never auto-retried — the structural fix for the 2026-06-15 unlogged on-chain fill.

### 8.4 The guardrail stack — `ClobTrader.place_order` (trader.py:932)

Enforced in order:

| # | Guard | Value | Evidence |
|---|---|---|---|
| 0 | Funder-class refusal | refuses LIVE if account class unproven | trader.py:986-1000 |
| 1 | Per-order cap | **$30 BUY** / $100 de-risk cash-out SELL | trader.py:242, :259; enforced :1002-1011 |
| 2 | WC keyword allowlist | "world cup / fifa / wc / fifwc" | trader.py:248, :1013-1017 — **SKIPPED when `market_question` is None or `de_risk`** (documented :957-968) |
| 3 | Daily cap | $100 cumulative LIVE BUY notional / UTC day | trader.py:243, :1019-1027 (tracked in pm_order_log, DDL :276-290) |
| — | dry_run default | True | trader.py:241; per-call override at :977 |

`TradeConfig` (trader.py:241-259) is a hardcoded dataclass with **zero imports of `markets/bankroll.py`** — the caps and the sizing brain do not share a source of truth (§10). Live posture: [UNVERIFIED-MINI: mini .env PM_DRY_RUN=0; pm_order_log 13 rows, 6 LIVE with on-chain ids — PM execution has genuinely fired live].

**PM_DRY_RUN parse nuance** (app.py:1805-1807): `os.environ.get("PM_DRY_RUN", "1").strip().lower() not in {"0","false","no",""}` — unset means dry, but an exported **empty string arms live mode**.

### 8.5 One-click fire path

`wca_pm_fire.py`: `pm_fire_log` with `UNIQUE(rec_id, nonce)` (:204-208) + a same-rec time-window block even with a fresh nonce (:224-245); `ABSOLUTE_MAX_USD = 100.0` (:66); honours `_pm_dry_run` (default ON). `wca_place_server.py` + `arb.js` guards in §7.5. The whole path is hostname-inert off-localhost and dry-run by default; the caveat is that the server forwards whatever PM_DRY_RUN its own shell holds (H2).

### 8.6 Conductor isolation — and its one vacuous guard

`conductor/config.py:20-22`: strips `POLYMARKET_PRIVATE_KEY`, forces `PM_DRY_RUN=1` + `WCA_DB_PATH=data/dev.db` into every agent env (`agent_env()` :117-131 — "the load-bearing safety guard"). The **wca.db refuse-to-start guard is NOT in config.py** — it is in the launcher, `scripts/wca_conductor.py:1057-1062`, and only fires **if `WCA_DB_PATH` is set**; with the var unset (the dev box's actual state) it passes vacuously. `scripts/wca_conductor.py` is also the only script whose `--env` default is `.env.dev` (:1045); every other script defaults to `.env`.

### 8.7 Betfair — read-only, verified; DECISION recorded

- `src/wca/data/betfair.py:71` `betfair_execution_stub()` raises `NotImplementedError` ("monitoring-only… Polymarket ClobTrader for the only supported execution path", :78-82).
- `src/wca/data/betfair_exchange.py` (727 lines) — real JSON-RPC client, READ methods only (`listMarketCatalogue` :411, `listMarketBook` :423, `listCurrentOrders` :590, `listClearedOrders` :684).
- Repo-wide grep for `placeOrders|cancelOrders|replaceOrders|PlaceInstruction|limitOrder|persistenceType` → **zero hits**.
- Wired Betfair-first in `odds_source.py` (`_DEFAULT_ORDER` :40) but degrades to empty frame without creds. Mini network path to Betfair is geo-blocked [UNVERIFIED-MINI: SSL WRONG_VERSION_NUMBER].

**DECISION (ratified): keep Betfair read-only as a CLV/closing-line reference. Do NOT build Betfair execution.** If a GBP exchange execution venue is ever wanted, **Smarkets first**: `smarkets_execution_stub` (`src/wca/data/smarkets.py:344-349`, a named NotImplementedError boundary, test-asserted) plus native REST v3 read support with back+lay+depth (:30-60) already exist; no ~£499 live-key gate, 2% commission (0% outrights).

### 8.8 Vision

Betslip screenshots parsed by `claude-sonnet-4-6` (`src/wca/bot/vision.py:36`; `ANTHROPIC_VISION_MODEL` override :683/:778). Sportsbook bets have NO API execution — hand-placed, LLM-reconstructed, logged via `record_bet`.

---

## 9. Data assets: HAVE vs USE

### 9.1 martj42 results — HIGH utilization

- **Have:** `data/raw/martj42_cleaned.csv` = 49,498 match rows (90-min scorelines); `shootouts.csv` runtime-downloaded (`results.py:88` via `wca_clean_results.py:84`, scheduled 3×/day + inside daily-card).
- **Use:** THE DC training corpus (`wca_build_card.py:245,257-259` → `fit_models` → `DixonColes.fit`); shootouts pin real pens winners in the bracket/advancement sims (`advancement.py:425-429`, `wca_scores_markets_data.py:167-179`).
- **Gap:** corpus dominated by low-scoring internationals → MLE intercept undershoots WC scoring; patched by the scalar anchor (below).
- **Unlock:** replace the scalar with an empirical/xG-anchored prior (see StatsBomb).

**The goal-level fudge, precisely:** after the penalised MLE, a single constant is added to the DC intercept: `mu += log(target / slate_total)` (`dixon_coles.py:654-671` in-fit; `recalibrate_level` :720 post-fit; deployed via `apply_wc_level_anchor` `card.py:112` with `DEFAULT_DC_LEVEL_TARGET = 2.81` `card.py:78`). Supremacy-invariant (multiplies both lambdas equally), raises totals/BTTS only, and is fed by a hand-derived constant — **not** by any live market data.

### 9.2 StatsBomb — production-DORMANT

- **Have:** `src/wca/data/statsbomb.py` (428 lines) — full per-shot `statsbomb_xg`, SoT convention, per-player minutes, shot/SoT/xG/npxG shares, corners/cards/fouls with FIFA-correct second-yellow logic; WC2018+WC2022 hardcoded (:36); dataset builder `build_props_dataset` :387.
- **Use:** ~0% in production. All importers are manual scripts or CI tests; no scheduled job builds `players.db` (`playerprops.py:44-46` admits "NOT on every box"); `DixonColes.fit` consumes integer goals only (:454, :470-476) — **the core model never sees an xG number**. The only production trace is frozen constants: `CornersModel` priors from a one-off StatsBomb fit (`props.py:125`). [UNVERIFIED-MINI: `data/raw/statsbomb/` and `data/players.db` absent on the mini.]
- **Gap:** the model's one confirmed bias (goal level) is patched by a scalar precisely because no xG anchor exists.
- **Unlock:** ingest xG to (a) replace the 2.81 scalar with an empirical WC base rate, (b) shrink team attack/defence toward xG priors, (c) resurrect player props on real per-90 xG shares.

### 9.3 odds_snapshots — MEDIUM utilization

- **Have:** snapshotd writes h2h+totals bulk and btts per-event (`wca_snapshotd.py:58-61`; schema owner `data/snapshot.py:26-52`) [UNVERIFIED-MINI: ~3.24M rows, 49 bookmakers, 14 days]. CI additionally captures hourly to git-tracked `data/odds_price_history.jsonl`.
- **Use:** the model input devigs **ONLY h2h** (card build requests `markets="h2h"`, `wca_build_card.py:269-274`; per-book Shin → cross-book median, `card.py:767-789`). Totals/btts rows feed accas display, bot `/arb`, and the dead event-EV comparator only — never goal expectancy. O/U 2.5 and BTTS are priced from the model's own DC matrix (`scores.py:270,284`).
- **Gap (double):** (1) the paid-for totals/btts surface (hundreds of thousands of rows [UNVERIFIED-MINI: per-market count never audited this session — last audited figure was 162k totals+btts rows, memory: OddsAPI utilization; Appendix A query closes this]) is never devigged back into a lambda-level prior; (2) **closecapture is 1X2-ONLY** (`closecapture.py:13-15` — "a 1X2 close says nothing about … totals") so totals/btts bets never even get CLV stamps; their settlement is manual (`wca_settle.py`).
- **Unlock:** devig the totals surface into an empirical goal-level prior (replacing the scalar); sharp-book weighting instead of flat median; extend close capture to totals/btts so exotic-market CLV becomes measurable.

### 9.4 Polymarket CLOB — the richest live source, captured at chart resolution

- **Have:** `src/wca/data/pm_clob_history.py` — `/prices-history` (docstring :6-7: "authoritative trajectory source — **far denser and deeper than our own capture**", back to ~May 30 at ~1-min fidelity) and `/book` top-of-book fetch (`top_of_book()` :38-75: bid/ask/sizes/mid/spread).
- **Use:** display-resolution pulls for trajectory charts; and one real persistence path — the paper testbook marks PM top-of-book scalars every 10 min for its OPEN positions only (`wca_test_book.py:135-157` → `testbook/store.py:279-300` `marks` table in `data/test_book.db`, via com.wca.testbook — currently paused, `deploy/testbook.switch` = off). So "depth is always discarded" is false in the strict sense, but the true gap stands: **no market-wide depth/order-book capture exists anywhere**; only top-of-book scalars, only for open paper positions, only into the isolated paper DB.
- **Sink status:** `pmhistory.py` defines the `pm_snapshots` DB sink (:29-49) and pm-snapshot.yml runs 2×/hour — but the CI runner's DB is ephemeral (only the JSONL is committed), and **`data/pm_price_history.jsonl` has STALLED**: exactly 2,057 rows, all `kind == "advancement"`, 13 capture timestamps, last 2026-06-29 18:32 UTC — despite the live cron. A silent pipeline failure (`|| true` on both the refresh and the push). [UNVERIFIED-MINI: whether pm_snapshots exists as a table in the mini wca.db — 07-01 snapshot said absent.]
- **The microstructure scripts' own disclaimer** describes the gap (`scripts/microstructure/liquidity.py:6-8`): "NO order-book depth, NO matched volume, NO queue position … left as a FRAMEWORK". Corrected framing (2026-07-02 external review): of those three, the public CLOB verifiably exposes aggregated book depth (`/book` — bid/ask levels with sizes, `pm_clob_history.py:38-75`); matched-volume/trade-flow capture would need Polymarket's separate trades feed [not verified in this repo this session]; **queue position is not publicly exposed at all** (aggregated levels only — estimable solely for our own resting orders). The capturable-but-discarded data is depth (and possibly trade flow) — not queue position.
- **Unlock:** a dense CLOB capture daemon (1-min prices-history + periodic top-of-book into a real tick table) enables momentum/steam, mark-to-market at transactable bid-side exits, advancement term structure, realized-vol/order-flow work — all impossible while the data is discarded on arrival. `wca_pm_analytics_suite.py` scaffolding (calibration/term-structure/MTM) already exists, ad-hoc only.

### 9.5 Promos / boosts / news

- **Have:** promosd (launchd daemon + daily CI) scrapes hubs, seeds a catalog, and grades boosts via `_grade_boost` (`wca_promosd.py:397-484`) → `boost_evals` (`promos.py:246`, insert :1240); `boosts.py` prices boosts against `site/scores_data.json`. [UNVERIFIED-MINI: promotions=37, promo_snapshots=696, **boost_evals=0**, news_items≈30k.]
- **Use / why zero rows is code-plausible (not a scheduler failure):** grading fires only for candidates that are (a) freshly scraped, (b) NEW or CHANGED (`:312-330`), (c) `promo_type == "boost"`. The scraper honestly reports that most book hubs come back blocked/empty (no headless browser, `:34-38`); seeds are never graded; CI-written eval rows are discarded with the runner's DB; and the manual `/boost` command deliberately never persists (`app.py:1049-1051`).
- **Gap:** the `argmax(model_EV + promo − gub)` cross-account routing is **NOT implemented anywhere in code** — it exists only as prose (`docs/policy/matched_betting_strategy.md:76`) and operating-model memory.
- **Unlock:** persist evaluations for humanly-entered boosts (not just scraped ones) so promo extraction — a stated secondary objective — becomes measurable; implement the routing rule if it is to be more than a doc.

---

## 10. Sizing single source of truth

`src/wca/markets/bankroll.py` is the ONE sizing authority: **quarter-Kelly of (£3,000 ± realised P&L) at `GBP_USD = 1.33`** (:22, env-overridable), 4%/bet cap, 75% whole-book cap. Verified single source — no competing sizing module exists.

**The decoupling to fix:** the PM execution caps (`TradeConfig` $30/$100/$100, trader.py:241-259) are hardcoded and import nothing from bankroll.py. The safety ceiling and the sizing brain do not share a source of truth, which means scaling the bankroll cannot scale the caps, and editing one cannot be seen from the other. Phase-1 direction (from the brief, unopposed by evidence): derive TradeConfig caps from bankroll.py; extend bankroll.py (never fork it) into per-pool, multi-currency, correlation-aware sizing with a hard cash floor.

---

## 12. OPERATIONAL HAZARDS

*(Read this section first. H1 and H2 are the two structural fixes Phase 1 must schedule early; the secondary list is cheap-to-fix sharp edges.)*

### H1 — Deploy-lag silent-abort loop (production runs stale code with no alert)

**Evidence chain, all on origin/main:**
1. `deploy/macmini/autopull.sh:15-19` — verbatim:
   ```bash
   if ! git pull --rebase --autostash --quiet origin main; then
     echo "autopull: rebase conflict — leaving repo untouched for manual review" >&2
     git rebase --abort 2>/dev/null || true
     exit 0
   fi
   ```
   Any conflict → abort → **exit 0** → launchd sees success → retried and re-failed every 5 minutes, silently.
2. The legacy `deploy/sync.sh:42-48` variant is harsher: on failure it additionally runs `git checkout -- .` + stash drop — **destroying the daemons' fresh uncommitted artifacts** — then exits 0. Which puller generation is loaded is [UNVERIFIED-MINI].
3. `deploy/macmini/watchdog.sh` contains **zero git commands** — it checks the 5 KeepAlive daemons' liveness and log mtimes only (:54-79). A permanently failing autopull is invisible to it twice over (autopull is an interval job, and git-behind is never measured).
4. The collision fuel is structural: tracked files written by jobs — `data/goalscorers_latest.md` (3-hourly job write, never staged by publish, committed hourly by CI daily-card.yml:119), `data/model_predictions_log.jsonl` (appended every 30-min card build, same asymmetry), `data/card_latest.md`/`next_latest.md` (dual-committed by mini publish AND CI — two writers racing), and 16 `site-analytics/data/*.json` (rewritten every 30-60 min by two jobs, committed by NOTHING — §7.2). The mini working tree is therefore permanently dirty [UNVERIFIED-MINI: ~16 dirty tracked files at 07-01]; any upstream commit touching those paths wedges the pull.

**Structural fix options (propose, not executed):**
- **(a) Untrack the daemon-written artifacts on main** (move to gitignored paths or artifact storage), or **(b) give the mini a detached data branch** so code pulls never conflict with data churn. Either removes the collision class; (a) is simpler, (b) preserves data-in-git.
- Add **git-behind + rebase-abort detection to watchdog.sh** (alert to Telegram when `rev-list origin/main..HEAD` diverges or autopull logs an abort) — turns silent lag into a page.
- **Retire the legacy installer** (`deploy/install_services.sh` + `deploy/sync.sh`) and fix `deploy/README.md:71-81` so exactly one service generation can exist.
- **De-landmine the 5 data-coupled tests** (H-S1 list) so the pytest signal becomes meaningful again — otherwise a future *enforced* gate would block every data commit.

### H2 — Dev-box live-fire footgun (a hand-run script here fires real money)

**Evidence (devbox report, verified on the box + origin/main):**
- **No `.env.dev` exists.** The dev `.env` has **`PM_DRY_RUN=0` (LIVE)** and holds THREE PM signing keys (`POLYMARKET_PRIVATE_KEY`, `PM1_PRIVATE_KEY`, `PM2_PRIVATE_KEY`) plus 7 `BETFAIR_*` and `SMARKETS_*` creds.
- **No `WCA_DB`/`WCA_DB_PATH` line** in `.env` — so ~15 scripts whose `_load_dotenv(".env")` pattern defaults `--db data/wca.db` silently target the local **stale** ledger: 723,238,912 bytes, 77 bets total (2 open), frozen 2026-06-25. (`data/dev.db` exists with 0 bets. Note: the shipped `n_open_bets=77` artifact came from the MINI ledger — the local 77 is total rows, a numeric coincidence, devbox report §3.)
- **Zero hostname checks repo-wide** (`git grep hostname|gethostname|platform.node|uname` over src/wca + scripts → nothing). Nothing in the live path knows which machine it is on.
- The only dry-run enforcement is conductor-scoped (§8.6), and its wca.db guard is **vacuous when `WCA_DB_PATH` is unset** — the dev box's actual state.
- Net: `python scripts/wca_pm_propose.py` + a `Y PM-n`, or `wca_pm_fire.py` from any `.env`-sourced shell, signs and POSTs a real Polymarket order sized against a 6-days-stale ledger. Mitigations that exist ($30/$100 caps, fire idempotency) bound the damage; they do not prevent it.
- Dev-box git state drift: the box is on a clean `main` (4 behind), NOT the dirty feature branch the brief described.

**Fixes (propose, not executed):**
1. Ship a real `.env.dev` (PM_DRY_RUN=1, WCA_DB_PATH=data/dev.db, NO private keys) and **strip the live keys off the dev box entirely** — execution is the mini's job under the operating model.
2. **Host-gate live mode**: honour `PM_DRY_RUN=0` only when `socket.gethostname()` matches an allowlist (mini hostname; `wca_place_server.py:54` already carries a `MINI_HOST` default) or an explicit `WCA_LIVE_HOST` override — enforced in `_pm_dry_run()` (app.py:1806) and belt-and-braces inside `ClobTrader.place_order`.
3. **Fix the vacuous guard**: make the conductor (and any refuse-live check) fire when `WCA_DB_PATH` is unset, not only when it names wca.db.
4. **Unify the DB env var on `WCA_DB_PATH`** (§6.8) so one line in one env file controls every script's target.
5. Quarantine/delete the stale dev `wca.db`.

### Secondary hazards

| # | Hazard | Evidence | Cheap fix direction |
|---|---|---|---|
| H-S1 | **pytest gate is advisory-only AND the suite is red on main.** Branch protection returns 403 (free-plan private repo); GITHUB_TOKEN auto-sync pushes trigger no pytest at all; `tests/test_betrecs_open_exposure.py:91` asserts `n_open == 8` against `site/bet_recs.json`, a daemon/CI-rewritten artifact oscillating 0↔77 — red on every push regardless of the code. Four more data-coupled landmines: test_arbfx.py:222 (`arbs == []`), test_advancement.py:74 (exact team-set equality on the auto-committed CSV), test_build_wc2026_results.py:67, test_scorers.py:149. | Delete/fixture-ize the artifact assertions; make pytest a required check once the plan allows (or a merge-queue convention); until then treat green pytest as a manual gate. |
| H-S2 | **`/settle` is un-gated** — any authorized chat member can write settlements (app.py:2643-2644 outside `_MONEY_RE`); and `_is_admin` returns True for ALL when `TELEGRAM_ADMIN_USER_ID` is unset (soft gate, warning-only, :2672). | Add `/settle` to the money regex; refuse to start when admin id unset. |
| H-S3 | **PM_DRY_RUN empty-string arms live** (app.py:1805-1807 treats `""` as live). | Treat empty as dry; log the parsed posture at startup. |
| H-S4 | **Single-wallet / single-flag live posture** — the entire live/dry state hinges on one `PM_DRY_RUN` value on one machine, agreeing across three places for the button path (dev server shell → SSH env → mini). | Kill-switch command; posture surfaced in `/ping`; host gating (H2 fix 2). |
| H-S5 | **/betbuilder serves a cache nothing refreshes** — no scheduler runs `wca_betbuilder.py` (§3, bot text at app.py:847 claims a cron exists). | Schedule it or remove the command; either way stop the bot lying about a cron. |
| H-S6 | **Silent pm_price_history stall** — `data/pm_price_history.jsonl` frozen 2026-06-29 despite a live 2×/hour workflow; `\|\| true` on both capture and push swallows every failure (§9.4). | Fail the workflow loudly on empty diff/error; add a freshness check on the JSONL. |

---

## 13. Brief-vs-verified DRIFT LOG

Every material discrepancy between the overhaul brief (docs/FABLE_OVERHAUL_PROMPT.md, snapshot 2026-07-01) / project memory and what was verified against origin/main @ 957112a on 2026-07-02. Severity: **H** = changes a safety/money conclusion; **M** = changes a plan or a factual framing; **L** = count/location correction.

| # | Brief/memory claim | Verified reality | Sev |
|---|---|---|---|
| 1 | ~104 remote branches, ~6 merged, ~95 local | 98 remote excl. main (6 merged, 92 unmerged); 96 local incl. main; 31 locals with no remote, ~17 local-only unmerged = the only permanently-losable work | L |
| 2 | 32 worktrees across FOUR roots | 32 worktrees across **SIX** root groups, ~20+ GB; 9 under the BTC-STRC scratchpad (cross-project contamination); 13 under /private/tmp orphan on reboot; 7 pin 100%-superseded branches; 6 detached-HEAD | M |
| 3 | Dev box "on feat/paper-testbook-pm-analytics with dozens of modified files" | Dev box on clean `main`, 4 behind, zero modified tracked files | M |
| 4 | ~104 scripts (+7 archived) | 110 files under scripts/: 91 top-level + 7 archive + 11 microstructure + 1 plist | L |
| 5 | microstructure = 13 scripts | 11 | L |
| 6 | ~50 loose top-level src/wca modules | 39 (excl. `__init__.py`) | L |
| 7 | 23 subpackages | 16 (15 direct + intel/sources) | L |
| 8 | Dead list incl. `wca_canon_venues`, `wca_backfill_*` (all), `wca_pm_try/probe` | 23 confirmed dead (13 not on the claimed list); **3 RESCUED**: wca_backfill_accounts + wca_canon_venues (test-executed), wca_pm_probe (cited in live bot code app.py:2099) | M |
| 9 | "5 daemons + 14 interval jobs + …" phrased so autopull/publish/watchdog read as extra (→22) | 19 mini jobs TOTAL; autopull/publish/watchdog ARE 3 of the 14 | L |
| 10 | Single deploy mechanism (autopull) | TWO divergent installers both on main; legacy `com.wca.sync` → sync.sh destroys fresh artifacts on conflict; deploy/README.md:71-81 documents the LEGACY set; live generation [UNVERIFIED-MINI] | M |
| 11 | GH Actions backup "~4×/day" | Hourly-or-better (daily-card hourly, hourly-odds hourly, pm-snapshot 2×/hour); "4×/day" survives only in stale comments | M |
| 12 | pytest is "the required check; no merge with red suite" | Gate is almost certainly ADVISORY (403 on branch protection, free plan); suite RED on main from a data-coupled test; GITHUB_TOKEN data pushes trigger no pytest at all | H |
| 13 | "CLV computed ~8 different ways — high risk of divergent CLV math" | 4 writers fully consistent; aggregators re-loop the stored column; real divergence = THREE devig regimes + differing eligibility filters + two model-CLV impls (one unwired dup) + one dead script mislabelling EV as CLV | M |
| 14 | Venues ×4 duplication | Naming collision across two unrelated domains + an engine/data pair; zero duplicated logic; renames only | L |
| 15 | Arb ×4 / "second engine" | Second SCANNER only; all commission/FX/net-price math verifiably delegated to arb/arbfx (intel/arb.py:257,326) | L |
| 16 | Two dark features (F7/F8) | THREE: F9 = `card.build_event_references` (card.py:1406) has zero production callers despite 02_codebase_audit.md:161 calling it live | M |
| 17 | site-analytics: 17 feeds, 5 exclusive (+2) | 16 tracked feeds; analytics.js fetches 13; **8** exclusive (incl. tracking_clv_benchmark, market_intel, mc_futures) | L |
| 18 | terminal/visuals/tracking.html = "standalone variants" | visuals.html + tracking.html are 19-line redirect stubs; terminal.html standalone but orphaned (builder wca_terminal.py absent; data baked 06-11) | L |
| 19 | "~45 feeds, ~40 generators" | 44 tracked + 1 gitignored; ~34 generators; two orphan feeds with NO generator (mc_futures.json, site/microstructure/index.json) | L |
| 20 | Bot command list includes `/movers` | `/movers` does not exist in the bot | L |
| 21 | `/settle` listed among admin-gated money commands | `/settle` is NOT gated by `_MONEY_RE` — any authorized chat member can settle | **H** |
| 22 | Conductor "refuses to start if WCA_DB_PATH resolves to wca.db" (config.py:22) | Guard lives in scripts/wca_conductor.py:1057-1062 and is VACUOUS when WCA_DB_PATH is unset — the dev box's actual state | **H** |
| 23 | PM_DRY_RUN: "going live requires a human to export PM_DRY_RUN=0" | Also true for an exported EMPTY STRING (app.py:1805-1807) | M |
| 24 | Keyword allowlist always enforced (guard #2) | Skipped when `market_question` is None or on de-risk exits (trader.py:1013-1017, documented) | M |
| 25 | "ALL writes go through record_bet()" | Three direct-INSERT bypasses (wca_pm_watch.py:121, wca_pm_reconcile.py:237 apply, dead wca_import_polymarket.py:112) + internal _insert_cashed_slice — all skip canon_platform | M |
| 26 | Lazy ALTER list named 5 columns ("six" total) | Six confirmed; the unnamed sixth is `manual_override`, which is ALSO in the base DDL (doubly defined) | L |
| 27 | Pytest publish guard "sync.py:113" (implied ledger/) | src/wca/sync.py:113-118; src/wca/ledger/sync.py does not exist | L |
| 28 | Schema at store.py:73-98; default path :63/73 | DDL :71-94; `_DEFAULT_DB` :65 | L |
| 29 | Env-name inconsistency: WCA_DB vs WCA_DB_PATH (two names) | THREE: + `WCA_MINI_DB` (wca_place_server.py:57) | L |
| 30 | Dev `.env` claim: PM_DRY_RUN=0 + POLYMARKET/PM1/BETFAIR/SMARKETS keys | Confirmed AND worse: a third signing key (PM2_PRIVATE_KEY) also present; no WCA_DB* line at all so scripts default to the stale wca.db | **H** |
| 31 | Stale dev wca.db "77 bets" framed against the CI-red "8 vs 77" | Local 77 = TOTAL rows (2 open); the shipped n_open=77 artifact was built from the MINI ledger — numeric coincidence; the red test pins a fixture-style count (8) on a live artifact | M |
| 32 | Memory: "fire button … $100 hard cap" / brief's "$100 hard cap" imprecision | Per-order LIVE BUY cap is **$30**; $100 is the daily cap, cash-out cap, fire-script ABSOLUTE_MAX_USD, and place-server default | M |
| 33 | DEFAULT_BETFAIR_COMMISSION "code hardcodes 0.06" (implied betfair module) | Lives at arbfx.py:27 (+ map :128); overstated vs real tiers | L |
| 34 | odds_source.py:28 Betfair-first | Substance confirmed; docstring :9-18 + `_DEFAULT_ORDER` :40 | L |
| 35 | "pm_snapshots does not exist in production" | pm-snapshot.yml IS live 2×/hour but its DB rows are ephemeral on CI (only JSONL committed); mini table existence [UNVERIFIED-MINI: absent at 07-01]. NEW: the committed JSONL itself STALLED 06-29 despite the cron — silent pipeline failure | M |
| 36 | "order-book depth fetched live and immediately discarded" | Top-of-book scalars ARE persisted — but only for open paper-testbook positions, 10-min, into data/test_book.db (currently paused); no market-wide depth capture anywhere | L |
| 37 | boost_evals=0 → "the argmax routing isn't persisting" | The argmax(model_EV+promo−gub) routing was NEVER implemented (prose only, matched_betting_strategy.md:76); zero rows is code-plausible: scrape-blocked hubs, grading gated to NEW/CHANGED scraped boosts, seeds never graded, /boost deliberately never persists, CI eval rows discarded | M |
| 38 | closecapture stamps closes for settlement fallback (generic) | closecapture is **1X2-ONLY** (closecapture.py:13-15); totals/btts bets get no CLV stamps at all | M |
| 39 | goal-level fudge at "dixon_coles.py:653" | Gate :654, shift :670; post-fit variant `recalibrate_level` :720; deployed via `apply_wc_level_anchor` card.py:112, constant 2.81 at card.py:78 | L |
| 40 | "exposure ×3 … TWO correlated engines" framed as suspicion | Confirmed — and BOTH execute inside every `build_exposure_data` run (exposure.py:272 legacy + :289-349 corr) | M |
| 41 | Smarkets stub "ready to fill" | A named NotImplementedError boundary (smarkets.py:344-349), no placement scaffolding; read support (REST v3 back/lay/depth) is real | L |
| 42 | wca_betbuilder "cron-only" (audit doc) / bot "cron build" text | NO scheduler anywhere runs it — /betbuilder cache has no refresher | M |
| 43 | Mini runtime facts (behind-count, ~16 dirty files, PM_DRY_RUN=0, admin id, wca.db 1.94GB/210+ bets, odds_snapshots 3.24M, pm_order_log 13/6 LIVE, pm_parked held batch, boost_evals=0, statsbomb/players.db absent, Betfair geo-block) | ALL [UNVERIFIED-MINI] this session — SSH denied; carried as 07-01 snapshot values throughout this document | M |
| 44 | Branch triage expectations ("DELETE-STALE" bucket would exist) | 0 DELETE-STALE, 0 DELETE-DATA; 47 near-zero-risk deletions (6 merged + 41 fully cherry-superseded); 51 REVIEW; partial-supersede cherry-pick candidates incl. paper-testbook-pm-analytics 66.7% | L |
| 45 | Notion ledger memory ("manual import snapshot … drifts") | Confirmed dead-weight; diff CLI archived; no auto-sync; wca_pm_fire.py:322-331 states no single-bet append helper exists | L |

**Post-commit corrections (2026-07-02, external-review adjudication).** (46) The Phase-0 sweep itself missed that `docs/ARCHITECTURE.md` (and its companion `docs/architecture/SYSTEM_MAP.md`) already existed on origin/main — this document unknowingly duplicated it. Resolution: `docs/ARCHITECTURE.md` is now a redirect stub to this file, SYSTEM_MAP.md carries an as-of warning, and THIS file is canonical. (47) §9.4's original phrasing implied the CLOB exposes queue position; corrected in place — queue position is not publicly exposed (aggregated depth is).

---

## Appendix A: UNVERIFIED-MINI checklist

Read-only SSH command list to close every [UNVERIFIED-MINI] marker in this document. Nothing below mutates state (sqlite opened `-readonly`; git commands are queries; grep prints presence, not secret values). Run as one block or line-by-line; host per memory: `andrewdoherty@192.168.68.55`, repo `~/World-Cup-26`.

```bash
ssh andrewdoherty@192.168.68.55 'cd ~/World-Cup-26 && \
  echo "== git sync state ==" && \
  git fetch origin --quiet && git rev-parse HEAD && \
  git rev-list --left-right --count origin/main...HEAD && \
  git status --porcelain | head -40 && git status --porcelain | wc -l && \
  echo "== which launchd generation is loaded ==" && \
  launchctl list | grep com.wca && \
  ls ~/Library/LaunchAgents/com.wca.* 2>/dev/null && \
  echo "(legacy markers: com.wca.sync / com.wca.build_card; current: com.wca.autopull / com.wca.buildcard)" && \
  echo "== env posture (values redacted except PM_DRY_RUN) ==" && \
  grep -E "^PM_DRY_RUN=" .env && \
  grep -cE "^TELEGRAM_ADMIN_USER_ID=." .env && \
  grep -cE "^WCA_ARCHIVE_S3_" .env ; \
  grep -cE "^WCA_ARCHIVE_ENABLED=" .env ; \
  echo "== ledger ==" && \
  stat -f %z data/wca.db && \
  sqlite3 -readonly data/wca.db "select count(*), sum(status='\''open'\''), max(ts_utc) from bets;" && \
  echo "== odds_snapshots rows ==" && \
  sqlite3 -readonly data/wca.db "select count(*) from odds_snapshots;" && \
  echo "== odds_snapshots rows by market (closes the totals/btts count marker, §9.3) ==" && \
  sqlite3 -readonly data/wca.db "select market, count(*) from odds_snapshots group by market;" && \
  echo "== pm_order_log (total / live) ==" && \
  sqlite3 -readonly data/wca.db "select count(*), sum(dry_run=0) from pm_order_log;" && \
  echo "== pm_parked (held batch?) ==" && \
  sqlite3 -readonly data/wca.db "select id, status, substr(ts_utc,1,16) from pm_parked order by id desc limit 12;" && \
  echo "== boost_evals ==" && \
  sqlite3 -readonly data/wca.db "select count(*) from boost_evals;" && \
  echo "== pm_snapshots table exists? ==" && \
  sqlite3 -readonly data/wca.db "select name from sqlite_master where name='\''pm_snapshots'\'';" && \
  echo "== statsbomb cache / players.db present? ==" && \
  ls -d data/raw/statsbomb 2>&1 ; ls -l data/players.db 2>&1 ; \
  echo "== autopull health (recent aborts?) ==" && \
  tail -20 logs/autopull.log 2>/dev/null && \
  echo "== testbook switch ==" && cat deploy/testbook.switch && \
  echo "== Betfair reachability ==" && \
  curl -sv --max-time 10 https://api.betfair.com 2>&1 | grep -iE "SSL|error|HTTP" | head -5'
```

Interpretation keys:
- **Installer generation:** presence of `com.wca.sync` or `com.wca.build_card` labels = legacy set (partly) live → retire per H1; only `com.wca.autopull`/`com.wca.buildcard` = current generation.
- **Dirty-file count** near ~16 with `data/goalscorers_latest.md`, `data/model_predictions_log.jsonl`, `site-analytics/data/*.json` in the list = H1 collision fuel confirmed live.
- **`TELEGRAM_ADMIN_USER_ID` grep count 0** = the admin soft-gate is fully open (H-S2 escalates).
- **`WCA_ARCHIVE_S3_*` count 0** = the ledger has no off-box copy at all (§6.7 escalates to a to-do this week).
- **pm_snapshots absent + pm_order_log LIVE rows present** = confirms §9.4 and §8.4 as written.

---

*End of ARCHITECTURE.md (Phase 0, current state). This document proposes; it executes nothing. Companion Phase-0 deliverables: the branch/worktree triage plan and the operational-hazards remediation plan.*
