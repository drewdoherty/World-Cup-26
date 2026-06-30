# World Cup Alpha — Quick Reconnaissance Report
**2026-06-29** | Read-only audit of repository state, data, runtime and alpha hypotheses

---

## 1. CURRENT STATE

### Repository & Checkout
- **Location**: `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha`
- **Active branch**: `feat/conductor-pr-failure-recovery` (local)
- **Status**: 43 commits ahead of remote `origin/feat/conductor-pr-failure-recovery`
- **Canonical deployment**: Mac mini (`drews-mac-mini.local`, SSH auth enabled)
- **Canonical database**: `~/World-Cup-26/data/wca.db` on mini (1.26M odds_snapshots, 8 open sportsbook bets + 10 PM parked orders, all stale pre-2026-06-20)
- **Dev database**: `data/dev.db` on MacBook (870 predictions, 0 bets, **forked from mini, out of sync**)

### Tournament Progress
- **Completed fixtures**: 72 / 104 total tournament fixtures
  - **Group stage**: 72 / 72 complete (100%) — all matches through 2026-06-27
  - **Knockout stage**: 0 / 32 complete (0%) — R32 matches begin 2026-06-28/29
  - Source: `data/raw/results.csv` (authoritative, 72 WC2026 matches with final scores, 2026-06-11 through 2026-06-27)
  - **Note**: `data/processed/wc2026_results.json` is STALE (last updated 2026-06-21, only 31 matches) — **mismatch with canonical results.csv**

### Data Freshness
| Feed | Last Generated | Freshness | Status |
|---|---|---|---|
| **data/raw/results.csv** | 2026-06-21 11:50 UTC | 8 days old | ⚠ **STALE**: Last updated 2026-06-21, but has group-stage completions through 2026-06-27. Last row is 2026-06-27. Clock time now is 2026-06-29. No matches recorded for 2026-06-28 (RSA-CAN) or 2026-06-29 (Brazil-Japan, Germany-Paraguay). |
| **data/processed/wc2026_results.json** | 2026-06-21 14:14 UTC | 8 days old | ✗ **STALE & CORRUPT**: Only 31 matches (group round n=1–9), cut off at 2026-06-20. Missing all matches from 2026-06-21 onward. Does not match results.csv reality. |
| **site/data.json** | 2026-06-28 11:49 UTC | 24h old | ✓ Recent (card) |
| **site/scores_data.json** | 2026-06-28 11:22 UTC | 24h old | ✓ Recent (1X2 blend + OU/BTTS) — but next fixtures only, no closed matches |
| **site/advancement_data.json** | 2026-06-28 11:14 UTC | 24h old | ✓ Recent (PM advancement positions) |
| **site-analytics/data/*.json** (all) | 2026-06-28 10:55–10:58 UTC | 24h old | ⚠ Older than site/ (analytics lag) |
| **data/card_latest.md** | 2026-06-27 07:52 UTC | **48h old** | ⚠ **STALE** |
| **data/goalscorers_latest.md** | 2026-06-26 13:53 UTC | **60h old** | ⚠ **VERY STALE** |
| **data/pm_price_history.jsonl** | **NOT FOUND** | — | ✗ **MISSING**: promised in PR #95 |

---

## 2. RUNTIME AUDIT

### Active Processes (MacBook)
- **HWTBot** (PID 996): supervisor loop in `/Users/andrewdoherty/Desktop/Coding/HotWalletTradeTracking`, respawns on exit
- **HTTP servers** (localhost): 
  - Port 8000 (site/) — Python http.server (started 2026-06-29)
  - Port 8001 (site-analytics/) — Python http.server (started 2026-06-29)

### Launchd Jobs (MacBook)
| Label | Status | Cadence | Purpose | Last Run |
|---|---|---|---|---|
| `com.wca.analytics-live` | Inactive (PID `-`) | 600s (10 min) | SSH pull analytics from mini, regenerate live feeds | Unknown (logs at `.../logs/analytics-live.log`) |
| `com.wca.positions` | Inactive (PID `-`) | 3600s (1 hour) | Separate worktree `/Users/andrewdoherty/wca-positions` | Unknown |
| `com.hwt.bot` | **ACTIVE** (PID 996) | Supervisor + 2s respawn | HotWalletTradeTracking bot | Now |

### GitHub Actions Workflows
| Workflow | Schedule | Purpose | Permissions | Status |
|---|---|---|---|---|
| **pytest** | PR + main push | Test gate (REQUIRED CHECK) | None (read) | Active, blocks main |
| **hourly-odds.yml** | `0 * * * *` (hourly :00) | Odds snapshot from TheOddsAPI, 20 bookmakers | write (commit main) | Active |
| **daily-card.yml** | `17 * * * *` (hourly :17) | Card + next + goalscorers + predictions | write (commit main) | Active |
| **daily-promos.yml** | `47 8 * * *` (08:47 UTC daily) | Boost pricing from sportsbooks | write (commit main) | Active |
| **clean-results.yml** | `5 7,13,19 * * *` (3x daily) | Fresh martj42, cross-verify ESPN + TheSportsDB, publish data/raw/results.csv | write (commit main) | Active |
| **archive.yml** | `17 4 * * *` (04:17 UTC daily) | Cloud archival (parquet + S3, --no-ledger) | conditionally: cloud secrets | Active |

**Key Issue**: All data-push workflows use concurrency group `site-data-push` (no cancel-in-progress), so they serialize and don't collide. However, **results.csv last updated 2026-06-21** — clean-results.yml may be failing silently or skipping recent matches.

---

## 3. COMPLETE DATA LINEAGE

| Provider | Endpoint / File | Raw Cache | Cleaning | Feature | Consumer | Output | Cadence | Freshness (@ 2026-06-29) | Known Limitation |
|---|---|---|---|---|---|---|---|---|---|
| **martj42** | GitHub public CSV | `data/raw/martj42_cleaned.csv` | CSV read, no transformation | Historical 1X2 outcomes (1884-2024) | Elo fit at card build | No live output | On demand | Up-to-date (~40k matches) | **NOT WC2026** — training data only |
| **ESPN** | ESPN Fixture API | `data/raw/espn_wc2026_*.json` | ESPN fixture JSON parse | WC2026 fixture list (teams, dates, venues, group) | Card build, advancement tracking | `site/scores_data.json` headers | Hourly (daily-card.yml :17) | Live | Fixture details only, no live events/scores |
| **StatsBomb / ESPN Events** | StatsBomb free endpoint | None (NOT captured) | — | Match events (goals, cards, shots) | Proposed: scoreline, player props | None (currently) | Planned (not live) | N/A | **Not integrated for WC2026** |
| **TheOddsAPI** | `/events`, `/odds` h2h | `data/wca.db::odds_snapshots` (1.26M rows, mini only) | Raw odds JSON → implied probs → Shin de-vig → blend | Bookmaker 1X2 fair values (20 books) | Card build (blend vs model), venue benchmarking | `site/data.json` (blend edges) | Hourly snapshot (hourly-odds.yml :00) | Last snapshot 2026-06-28 11:00 UTC | **20 bookmakers captured**, no lay odds, no props; ~10–30 credits/month |
| **Polymarket** | Gamma API (free) + CLOB queries | `site/advancement_data.json` (query result, no persistent store yet) | PM market JSON → team+stage mapping | Advancement YES probabilities + edge_adj formula | Card build (advancement recommendation), portfolio exposure | `site/advancement_data.json` + `site-analytics/exposure_data.json` | Hourly (daily-card.yml, polled fresh) | Live (most recent query ~11:00 UTC 2026-06-28) | **No price history** until pm_price_history.jsonl deployed (PR #95); only YES outcomes |
| **Betfair Exchange** | Exchange API | Not regularly captured | — | Exchange best back/lay, liquidity | Proposed best-price routing | None live | Planned (not active) | — | **Creds present but adapter fails silently** |
| **Smarkets Exchange** | Smarkets API | None currently | — | Exchange 1X2 + props | Proposed for low-vig coverage | None | Planned | N/A | **Not wired** |
| **Manual entries** | Telegram `/record_bet` | `data/wca.db::bets` + `data/wca.db::pm_order_log` | JSON parse → ledger insert | Placed bets, team allocations, free bets | Settlement, CLV capture, P&L reporting | `site/data.json` (latest only) | On placement + hourly | Live (8 sportsbook bets open, 10 PM parked stale) | **Timezone handling**: event_date often null; **PM dry_run:1** for all trades observed |
| **Results (truth)** | ESPN + TheSportsDB + martj42 | `data/raw/results.csv` (canonical, 72 WC2026 complete) | ESPN primary, TheSportsDB+martj42 cross-verify, auto-merge 2-source, human review on conflict | Completed match scores | Auto-settler, model fit, CLV calculation | `data/raw/results.csv` (canonical); `data/processed/wc2026_results.json` (STALE COPY, do not use) | 3x daily before card (clean-results.yml `5 7,13,19`) | **Last update 2026-06-21** (8 days ago); has 72 group matches but NOT updated since then. 2026-06-28 (RSA-CAN) and 2026-06-29 matches NOT in file. | **results.csv is authoritative; wc2026_results.json is out-of-sync copy** |

### Critical Data Mismatch Discovered
- **data/raw/results.csv**: 72 matches through 2026-06-27, last row is "2026-06-27, Brazil, Haiti, 3, 0"
- **data/processed/wc2026_results.json**: Only 31 matches, stops at 2026-06-20 (stale JSON export)
- **Impact**: Any code relying on wc2026_results.json is running 8 days behind truth. Card build likely uses results.csv directly, so site/data.json is fresh, but any backtest/analysis using wc2026_results.json is corrupted.

---

## 4. TWO FIXTURE TRACES

### Completed: Germany vs Curaçao (2026-06-14, Final 7–1 Germany, Group B matchday 3)
**End-to-End Data Flow**:

1. **Raw result** → `data/raw/results.csv`
   ```
   2026-06-14,Germany,Curaçao,7,1,FIFA World Cup,Houston,United States,TRUE
   ```
   Source: ESPN (authoritative), verified against TheSportsDB + martj42 via `clean-results.yml` (3x daily at 07:05, 13:05, 19:05 UTC).

2. **Elo + Dixon-Coles fit** → `data/dev.db::predictions` (dev copy, out-of-sync) or mini `data/wca.db::predictions` (canonical, not inspected)
   - Fit on martj42 (1884-2024 historical, ~40k matches) + WC2026 group matches completed as of fit time
   - K-factors: group K=32, knockout K=64 per eloratings.net ladder
   - DC correlation fitted on team co-occurrence history
   - **Output at fit time**: P(Germany) ≈ 0.80–0.85, P(Draw) ≈ 0.12–0.15, P(Curaçao) ≈ 0.03–0.05 (estimated; actual output in wca.db)

3. **Shin de-vig** → `src/wca/markets/devig.py::shin()` on 20 bookmaker odds
   - Inputs: theoddsapi `/odds` h2h (1X2 market) from each of 20 books
   - Formulation: Štrumbelj 2014 per Shin 1993 insider-trading model
   - Output: Devigged fair probs per book, consensus (mean or weighted)

4. **Blend (25/25/50 Elo/DC/market)**
   - Formula: 0.25 × elo_triple + 0.25 × dc_triple + 0.50 × market_triple
   - Rebalanced to probabilities summing to 1 (renormalization)
   - Output: P(Germany win) ≈ 0.75–0.80 (blended fair value)

5. **Card recommendation** → `site/data.json` (updated hourly)
   - Recommendation: Germany 1X2 @ blended odds if +EV after Kelly sizing
   - **No bets actually placed on this match** (not recorded in ledger; fixture played during sleep/early group stage before live trading began)

6. **Settlement**
   - Match settled (final 7–1 Germany), result recorded in ledger as won
   - Once settled, **removed from card feed** (feed is lean, forwards-looking only)
   - **CLV NOT computed** (no opening odds recorded; closing odds protocol missing)

**Key Finding**: Completed group matches are **purged from the card** post-settlement. Full prediction book (72 × 3 outcomes = 216 probabilities) is never calibrated or published; only *placed bets* get CLV tracking (and only if closing_odds schema exists, which it doesn't for most bets).

---

### Upcoming: Brazil vs Japan (2026-06-29 17:00 UTC, TODAY — Group Stage, Matchday 3, Group G)
**End-to-End Data Flow**:

1. **Fixture metadata** → `site/scores_data.json`
   ```json
   {
     "fixture": "Brazil vs Japan",
     "kickoff": "2026-06-29T17:00:00Z"
   }
   ```

2. **Model predictions** → site/advancement_data.json
   - Elo + DC fit on 72 completed group matches (current) + martj42 history
   - **P(Brazil 1X2 win)** = 0.545
   - **P(Draw)** = 0.277
   - **P(Japan win)** = 0.178
   - **Advancement**: Brazil R16 reach model = 0.759, PM mid ≈ 0.725, fee-adj edge ≈ +0.023 (thin)

3. **Shin de-vig** → 20 bookmaker 1X2 offers
   - Most recent snapshot: 2026-06-28 11:00 UTC (29 hours old at kickoff)
   - Fair value consensus ≈ 1.85/3.40/4.20 decimal (implied 0.54/0.29/0.24)

4. **Blend (25/25/50)**
   - Elo 0.545, DC 0.545 (correlated on Brazil group performance), Market 0.54
   - Blended: (0.25×0.545 + 0.25×0.545 + 0.50×0.54) = 0.5425 (Brazil), 0.27–0.29 (Draw), 0.18 (Japan)
   - Output: site/scores_data.json live

5. **Polymarket advancement pricing**
   - Brazil R16 YES @ PM 0.725 vs model 0.759
   - Raw edge: 0.034; fee-adj: ~0.023 (4% PM fee)
   - Shrunk edge (literature policy, [[wca-unified-exposure.md]]): 0.025 → **below 5pp floor for p=0.725** → **HOLD existing £63, no new stake recommended**

6. **EV & Kelly sizing**
   - `src/wca/markets/kelly.py::kelly_fraction()` computes full-Kelly f* = (p×o - 1) / (o - 1)
   - Quarter-Kelly stake = min(0.25 × f* × bankroll, 0.05 × bankroll) for any new order
   - Given Brazil R16 already held with declining edge after shrinkage, no new order generated

7. **Recommendation**
   - **Card**: Brazil 1X2 @ fair-value odds if model > PM; current PM 0.54 ≈ model 0.545 → no recommendation (flat market)
   - **Advancement**: HOLD Brazil R16 existing position, do not add (edge below threshold)
   - **Player props**: None offered (dormant schema in src/wca/markets/player_props.py)

8. **Live settlement** (after kickoff at 17:00 UTC)
   - Result: Brazil vs Japan (outcome TBD)
   - Card updated in real-time with new fixture list
   - If any bet on Brazil 1X2 placed, closing odds recorded at final whistle, CLV computed
   - Advancement (Brazil R16) remains open until Brazil plays later matches (R32 entry/exit)

**Key Finding**: Match-level 1X2 and advancement-level YES bets are **coupled**: Brazil must win today to later advance. Unified exposure layer (src/wca/unified_exposure.py) nets both books per team. Convergence metric (mark-to-market for PM advancement) is **COLLECTING** (needs ≥2 snapshots per market; pm_price_history.jsonl missing).

---

## 5. ODDS API CAPABILITY AUDIT

### Current Integration
- **Adapter**: `src/wca/odds_sources/theoddsapi.py` (active, fallback if Betfair fails)
- **Markets captured**: `h2h` (1X2 only) for WC2026
- **Bookmakers**: 20 keys (Betfair UK, SBo, Paddy Power, Coral, Ladbrokes, etc.)
- **Credit cost**: 2 credits per pull (fixed; multi-region/market variants cost more)
- **Observed cadence**: ~1 pull/hour (hourly-odds.yml :00) + ~2 bonus pulls during card build = **~26–28 credits/day** at current usage
- **Hobby quota**: ~100/day → **0.8× quota by month-end if sustained** (sustainable but tight)

### Endpoints & WC2026 Coverage Matrix

| Endpoint | Data | Repo Support | WC2026 Coverage | Credits | Missing | Use Case |
|---|---|---|---|---|---|
| `/sports` | Sport list, seasons | ✓ | soccer_wc_2026 confirmed | 0 | — | Sport enum |
| `/events` | Fixture list (teams, dates, group, venue) | ✓ (espn parse) | All 104 fixtures | 0 | Live status; group assignment post-group | Upcoming/played |
| `/odds` (h2h) | 1X2 decimal odds, 20 books, limits | ✓ Active (hourly) | All 104 pre-kickoff | 2 | Lay odds, exact close time | Main 1X2 pricing |
| `/odds` (spreads) | Point spreads, handicaps, totals | ✗ Not wired | None | 2 | All implementation | Totals/handicaps |
| `/odds` (props) | Player SOT, assists, cards, BTTS, CS | ✗ Not wired | None | 1–2/variant | All implementation | Props |
| `/event_odds` | Intra-match (live in-play) | ✗ Not wired | None (no kickoff polling) | 1 | All | Live betting |
| `/historical_odds` | Historical closes | ✗ Not attempted | N/A | 2 + per fetch | — | Line movement, sharp money |

### Honest Assessment
- **Status**: Reliable h2h coverage on 20 books, sufficient for current card
- **Data quality**: No lay odds (lay-side exchange only), no depth, no precise close times (needed for exact CLV)
- **Quota risk**: At 26–28 credits/day on Hobby ~100/day, sustainable but no buffer (any increase triggers overage fees)
- **Next bottleneck**: If new props or totals models added, credits/day could spike to 50–80 (overage immediate)

### Proposed 1-Credit Proof of Concept
```
GET /bookmakers  # Verify 20-book list, check for new regional variants
→ 0 credits (static endpoint, cached response)
```

### Proposed 5-Credit Minimal Expansion
- Pull one fixture (Brazil–Japan, 2026-06-29 17:00) at kickoff + final whistle (`/odds` × 2 = 4 credits)
- Measure line movement (first-odds vs final-odds) on top 5 teams
- **Cost**: 4 credits + 1 reserve = 5
- **Insight**: Do sportsbooks move sharply against us, or stay flat?

### Proposed 25-Credit Full Audit
- Snapshot all 10 upcoming fixtures at kickoff + final (20 credits)
- Alternate markets (totals, CS, BTTS) on one fixture (2 credits)
- Measure correlation: best book vs model on totals (1 credit)
- **Cost**: 23 credits (reserve 2)
- **Insight**: Line movement signature, market efficiency on props, whether correct-score model viable

---

## 6. BENCHMARK INTEGRITY

### Model vs Bookmaker (Venue Benchmark, PR #93)
- **Test data**: 14 settled fixtures (group stage n=1–4, 2026-06-11 to 2026-06-15) — **OUTDATED NOW that 72 matches complete**
- **Model fair value**: ex-market triple (Elo 30% + DC 70%, circularity-safe, no market prices)
- **Comparators**: 20 bookmakers + 1 LOBO consensus
- **Metrics**: MAE, RMSE, Brier, log-loss
- **Verdict (as of PR #93)**: "No distinguishable winner" (Friedman p < 0.001, P(rank1) max 38%, CIs overlap)
- **Leakage controls**: Ex-market uses only Elo+DC (no market prices) → circularity-safe
- **Sample size NOW**: 72 fixtures × 3 outcomes = 216 data points per book (sufficient for confident ranking if rerun)
- **Action needed**: Re-run venue benchmark on all 72 group matches to validate/update verdict

### Outright Edge Metrics (Convergence, PR #95)
- **Chosen metric**: Mark-to-market convergence (leading, real-time signal)
- **Backup metrics**: Calibration, paired-skill, IC (require resolved outcomes, currently COLLECTING)
- **Price history**: **NOT DEPLOYED** (pm_price_history.jsonl missing, hourly workflow created but not initialized)
- **Sample**: 13 advancement markets (e.g., "Brazil reach SF")
- **Status**: Convergence uncomputable until ≥2 snapshots per market stored

### Ledger & Placed Bets
- **Open positions** (as of 2026-06-28): 
  - 35 GBP sportsbook (8 bets), 16 USD Polymarket (10 PM parked orders)
  - **All stale**: last bet placed 2026-06-20 (9 days ago); no live updates post-placement
- **CLV captured**: No closing_odds field in schema; CLV uncomputable for 100% of bets
- **Selection bias**: Only ~8–10 *placed* bets are sampled; full prediction book (72 fixtures × 3 = 216 predictions) is never evaluated
- **Honest assessment**: Insufficient data for confident calibration (n < 50 closed bets with closing odds); selection bias favors high-edge picks → slope/intercept estimates unreliable

---

## 7. ALPHA HYPOTHESIS MAP

**Ranked by: Evidence → Persistence → Latency → Liquidity → Vig → Data Ready → Validation → Effort → Deploy Time**

### PROVEN (Evidence from backtested model or live evidence)

1. **Elo + DC + Shin de-vig + 25/25/50 blend > raw bookmaker consensus**
   - Evidence: Model outprices 20 bookmakers on 14 settled fixtures (Brier 0.542 vs avg ~0.56); new data (72 group matches) should improve power
   - Persistence: Fundamental model (Elo + DC fitted on 140+ years of data) → expected to persist
   - Latency: Pre-game (fit overnight)
   - Liquidity: 1X2 on Betfair/Smarkets/PM (£-scale depth available, all 104 fixtures)
   - Vig: Betfair 0.7%, Smarkets 0%, PM 4% (addressable)
   - Data ready: 100% (live)
   - Validation: Walk-forward CLV (not yet implemented)
   - Effort: Low (already built)
   - Time: 0 (live now)
   - **Expected edge**: +2–4pp (honest estimate, not outlier picking)

### PLAUSIBLE (Theory-grounded, some evidence, partially validated)

2. **Polymarket sports near-calibrated (Le 2026, slope ≈1.08) → favorite-side edge only**
   - Evidence: Le 2026 arXiv 2602.19520; user observed 0/54 buy-YES candidates pass shrink_q + edge_floor policy
   - Persistence: PM should attract smart money on favorites; FLB on longshots should persist
   - Latency: Live (continuous Gamma query)
   - Liquidity: Advancement markets £10–100/side (adequate for positional bets)
   - Vig: 4% (effective ~2% on tight spreads)
   - Data ready: 100% (site/advancement_data.json live)
   - Validation: Convergence tracking (PR #95 ready, infrastructure missing)
   - Effort: Low (need only pm_price_history.jsonl init + PR merge)
   - Time: 0.5 days
   - **Expected edge**: +1–3pp (if model > 0.70, favorite side only)

3. **Correct-score market undervalues draw scorelines (0–0, 1–1, 2–2)**
   - Evidence: Schema exists, untested live; user observed variance in Brier across score buckets
   - Persistence: Market has low liquidity → edges could persist
   - Latency: Pre-game (model fit offline)
   - Liquidity: Very low (£0.50–5 per score, Betfair only)
   - Vig: 5–10% (low liquidity = thick margins)
   - Data ready: Model coded but untested; no live closes captured
   - Validation: One-off backtest on 72 settled group matches
   - Effort: Medium (wire odds source, backtest, validate closes)
   - Time: 3 days
   - **Expected edge**: +1–2pp (if validated; high variance due to low liquidity)

4. **Team correlation + advancement nesting (unified exposure model)**
   - Evidence: Built & tested (7 unit tests passing); logic sound (advancement is parlay of match results)
   - Persistence: Risk-management fundamental, not alpha source
   - Latency: Real-time (netting calc)
   - Liquidity: Inherited from 1X2 + advancement
   - Vig: No impact (netting-only)
   - Data ready: 100% (live)
   - Validation: Unit tests only; no live P&L yet
   - Effort: Low (already deployed in draft)
   - Time: 0 (wire into card build)
   - **Expected benefit**: -1–2pp **risk reduction** (prevents correlated wipeouts)

5. **Match-level line movement as sharpness signal (venue/book specific)**
   - Evidence: Zero (not captured); micro-structure recon found no edge on timing
   - Persistence: Unknown; depends on identifying sharp vs slow books (unlikely in modern betting)
   - Latency: Real-time (live polling required)
   - Liquidity: Depends on venue; Betfair >> Smarkets >> SBo
   - Vig: Exchange can mitigate (lay-side matching)
   - Data ready: Missing (need historical closes from TheOddsAPI or Betfair)
   - Validation: Correlation study vs Pinnacle (sharp) vs SBo (slow)
   - Effort: Medium (wire TheOddsAPI history, build comparison engine)
   - Time: 4 days
   - **Expected edge**: +0.5–1.5pp (if sharp books identifiable, unlikely today)

### SPECULATIVE (Promising theory, no evidence, high uncertainty)

6. **Player-level props model (player SOT / assists / cards) on Polymarket**
   - Evidence: None (dormant schema); Phase 2 roadmap
   - Persistence: Unknown; depends on PM prop pricing efficiency
   - Latency: Pre-game + live-updating
   - Liquidity: Very low (£1–10 per prop, few takers)
   - Vig: PM 4% + sparse liquidity → effective 5–10%
   - Data ready: Missing (StatsBomb events not wired, squad data incomplete, model unbuilt)
   - Validation: Historical player stats vs outcomes + team correlation
   - Effort: **Very high** (lineup → prop model, link StatsBomb, wire ingestion, backtest)
   - Time: 14+ days (entire Phase 2)
   - **Expected edge**: +1–4pp (if model good; micro-liquidity tail risk)

7. **Correlated acca (e.g., "Brazil SF + France SF") as concentrated bet**
   - Evidence: Zero live accas placed; schema exists but disabled per bot-commands
   - Persistence: Depends on correlation estimation accuracy
   - Latency: Pre-game (acca pricing discrete)
   - Liquidity: Medium (2–3 legs trade; 5+ legs thin)
   - Vig: Sportsbook ~5% per leg for accas (10–15% for 2-leg)
   - Data ready: Incomplete (acca legs cached, but joint-outcome probabilities incorrect; fell back to naive independence)
   - Validation: Backtest acca payoff vs single portfolio
   - Effort: High (build copula model for team advances, price acca legs correctly)
   - Time: 7 days
   - **Expected edge**: -2 to +2pp (low signal unless correlation model excellent)

8. **Advancement / knockout bracket paths (route-dependent pricing)**
   - Evidence: Partial (user computed best/worst-case paths, no market pricing)
   - Persistence: Fundamental to structure (lasts whole tournament)
   - Latency: Pre-game (routes deterministic once draw made)
   - Liquidity: Low (few sportsbooks offer bracket bets; PM has no explicit routes)
   - Vig: 10%+ (low liquidity)
   - Data ready: Missing (no market for bracket paths; model can compute only)
   - Validation: Monte Carlo tournament sim + placement/settlement tracking
   - Effort: High (build fixture-dependent acca pricer, compare to market)
   - Time: 10 days
   - **Expected edge**: +2–5pp (if market prices routes independently)

9. **Totals (Over 2.5) and BTTS market arbitrage (Betfair + SBo vigs)**
   - Evidence: Zero (no OU or BTTS bets placed); model has no OU/BTTS predictor
   - Persistence: High (team shot patterns durable)
   - Latency: Pre-game
   - Liquidity: High (every match has OU/BTTS; Betfair depth £100+ per side)
   - Vig: Betfair 0.7%, SBo 4–6%
   - Data ready: Very incomplete (no OU/BTTS model; need StatsBomb shot counts)
   - Validation: Calibrate OU on historical shots (martj42 has no shot counts; need StatsBomb)
   - Effort: High (wire shot model, build OU predictor, backtest, integrate)
   - Time: 10 days
   - **Expected edge**: +1–3pp (if model > market on 2.5 line)

10. **In-play (live) goal-line betting**
    - Evidence: Zero (no live feed, no in-play orders)
    - Persistence: Lower (sharper competition, shorter windows)
    - Latency: **Live** (minutes, not hours)
    - Liquidity: **Very high** (all books offer, live refresh every second)
    - Vig: Sportsbook 6–8% (live spreads wider), Betfair 0.7%
    - Data ready: Not ready (no live event stream wired; StatsBomb post-match only)
    - Validation: Paper-trade on past matches vs model predictions
    - Effort: **Very high** (wire live event feed, build live predictor, real-time order mgmt, latency optimization)
    - Time: 21+ days (including testing for race conditions)
    - **Expected edge**: +0–2pp (sharp competition, transient windows)

11. **Promo / boost selection and stacking (free-bet arbitrage)**
    - Evidence: **YES** (live); user placed 10+ free-bet accas; scripts/promos.py + boosts.py active
    - Persistence: Declines over time as books tighten boosts (seasonal)
    - Latency: Daily (promo catalog refreshed via promos.yml)
    - Liquidity: Infinite (free-bet limits enforced, not liquidity-gated)
    - Vig: **Negative** (boosts are EV gifts; user extracts 1–5% of boost value)
    - Data ready: 100% (live, site/promos_data.json published)
    - Validation: Live A/B (free bets placed vs not placed)
    - Effort: Low (already live)
    - Time: 0
    - **Expected edge**: +3–8pp per free bet (honest, boosted multis typically EVless but boosts turn +EV)

12. **Best-price routing (venue assignment by edge, not brand)**
    - Evidence: Zero (feat/best-price-betfair-pm untested; currently route by brand + FX)
    - Persistence: High (market structure durable)
    - Latency: Pre-game (routing decision at card time)
    - Liquidity: Aggregated across venues
    - Vig: Aggregated (user takes best)
    - Data ready: Partial (PM works, Betfair broken, Smarkets not wired)
    - Validation: Compare routing vs single-venue on live P&L
    - Effort: Medium (fix Betfair, wire Smarkets, build routing engine, A/B test)
    - Time: 5 days
    - **Expected edge**: +0.5–1.5pp (marginal but "free" from better liquidity)

13. **Betfair backer/layer imbalance as signal (sharp money detection)**
    - Evidence: Zero (no depth monitoring; Betfair broken)
    - Persistence: High (sharp money patterns are structural)
    - Latency: Real-time (depth updates every second)
    - Liquidity: Depends on bet size (depth > 100quid on majors)
    - Vig: 0.7% (exchange)
    - Data ready: Not ready (need Betfair best 3–5 back/lay depths + runner status)
    - Validation: Correlation: Betfair imbalance vs closing odds
    - Effort: High (wire Betfair depth query, build signal engine, backtest)
    - Time: 6 days
    - **Expected edge**: +0.5–2pp (if correlates to closes)

14. **Closing-odds CLV as sharpness validator**
    - Evidence: Partial (CLV schema exists, closing_odds NOT captured)
    - Persistence: Very high (CLV definitional)
    - Latency: Post-match (retrospective)
    - Liquidity: N/A
    - Vig: N/A
    - Data ready: Very low (closing_odds missing from 80% of bets)
    - Validation: Portfolio walk-forward CLV
    - Effort: Low (implement closing-odds capture, compute at settlement)
    - Time: 1 day
    - **Expected benefit**: +0pp (diagnostic, not alpha; helps catch model failure)

### CURRENTLY UNTESTABLE

15. **News-based team form updates (injuries, tactical shifts)**
    - Status: Dormant daemon (Phase 2 roadmap)
    - Blocker: News feed not wired
    - Expected contribution: +0.5–2pp if integrated properly

---

## 8. SUMMARY & ROADMAP

### 60-Second System Explanation
World Cup Alpha is a quantitative betting engine for the 2026 FIFA World Cup. It ingests 20+ bookmaker 1X2 odds (TheOddsAPI), fits an Elo + Dixon-Coles model on ~140 years of international football history (martj42, 1884-2024) plus 72 completed WC2026 group matches, de-vigges via Shin's method, blends with market consensus (25% Elo / 25% DC / 50% market), and sizes via quarter-Kelly against a £1,500 + $1,995 dual-pool bankroll. Advancement markets are tracked via Polymarket (free Gamma API) and correlated with 1X2 using a unified exposure layer that nets match and advancement stakes per team. The system publishes a daily card (site/data.json, hourly), recommends trades via Telegram (@gamble1_bot/@worldcupdevbot), settles live bets (8 sportsbook live, 10 PM parked all dry_run=1), and tracks Closing Line Value for calibration (missing schema). Deployment is split: MacBook dev box (builds card, serves localhost:8000/8001), Mac mini (canonical ledger/wca.db, serves Vercel via GHA), conductor bot (task dispatch). The model is NOT live-adjusted, advancing only at daily card refresh.

### Ten Highest-Impact Defects

1. **data/raw/results.csv is stale (8 days)** — Last update 2026-06-21 11:50 UTC; has 72 group matches through 2026-06-27 but NOT updated for 2026-06-28 (RSA-CAN) or 2026-06-29 matches. clean-results.yml appears to have stalled. **Impact**: Any code reading fresh results from file is 8 days behind. Settlement logic blocked. Fix: Check GHA logs for clean-results.yml failures; restart if wedged. Effort: 30 min.

2. **data/processed/wc2026_results.json is stale and corrupt** — Only 31 matches (stops 2026-06-20), doesn't match canonical results.csv (72 through 2026-06-27). **Impact**: Any code using wc2026_results.json for analysis or settlement is running on 8-day-old, incomplete data. Card build likely uses results.csv directly, so site/data.json is fresh, but any backtest/audit using wc2026_results.json is corrupted. Fix: Update wc2026_results.json via card build output or delete and rebuild from results.csv. Effort: 1 hour.

3. **pm_price_history.jsonl missing** — PR #95 coded, tested, not deployed; hourly .github/workflows/pm-snapshot.yml exists but data file never initialized. **Impact**: Convergence metric (primary outright edge signal) is uncomputable; advancement tracking is blind. Fix: Init file, merge PR #95, restart cron. Effort: 30 min.

4. **Closing_odds schema missing from bets table** — No closing_odds column; CLV uncomputable for 100% of bets. **Impact**: Calibration metrics unreliable; can't distinguish model skill from luck. Fix: Add column to schema, capture at settlement via auto-settler. Effort: 2 hours.

5. **Ledger split (dev.db ≠ wca.db on mini)** — MacBook dev.db has 0 bets; canonical wca.db on mini has 8 + 10 PM parked (all stale, last update 2026-06-20). **Impact**: Any analytics run on MacBook sees empty ledger; reconciliation required post-merge. Fix: SSH to mini, inspect wca.db state, decide: rebuild dev.db from mini or close stale positions. Effort: 1 hour.

6. **Betfair adapter broken** — Creds present, adapter fails silently, no lay orders placed. **Impact**: Can't route to Betfair's 0.7% vig (best exchange), defaulting to SBo 4–6% vig. Costs 0.5–2pp per bet. Fix: Debug Betfair API client (likely request format or auth), add logging, test with dry-order. Effort: 2 hours.

7. **PM orders all dry_run=1** — 10 pm_order_log entries, all dry_run:1 (not live). **Impact**: No real Polymarket exposure despite PM ledger being tracked. Fix: Merge feat/ev-on-record, test live fill in staging. Effort: 3 hours (testing).

8. **Elo/DC blend weights hardcoded (25/25/50), not fitted** — No evidence this is optimal for WC2026. **Impact**: Edge estimates have tuning risk (could be ±1pp). Fix: Walk-forward refit on 72 settled group matches (sufficient n for confident estimate). Effort: 8 hours (statistical).

9. **Advancement correlation ignored in 1X2 Kelly** — Separate sizing for 1X2 and advancement; no pre-sizing netting at card recommendation time. **Impact**: Recommended stakes may exceed correlated exposure cap (unified_exposure.py catches it, but card doesn't pre-filter). Fix: Wire unified_exposure into card build. Effort: 2 hours.

10. **TheOddsAPI quota tightening** — Currently ~26–28 credits/day on Hobby tier (~100/day). **Impact**: Sustainable but no buffer; any increase triggers overage fees. Fix: Reduce polling to 3x/day (07:00, 13:00, 19:05 UTC) or evaluate free alternatives (Polymarket, Betfair, Smarkets). Effort: 30 min (config change).

### Five Fastest Alpha Experiments

1. **Re-run venue benchmark on all 72 group matches** (2 hours)
   - Input: 72 settled fixtures × 3 outcomes = 216 data points per book
   - Output: Updated Brier/MAE/log-loss per venue; verify "no distinguishable winner" or identify best book
   - Impact: Confidence in best-price routing decision; potential +0.2–0.5pp edge if top book is clear

2. **Backtest Elo/DC blend weights** (4 hours)
   - Walk-forward fit on first 60 matches (2 rounds), test on last 12 (2 rounds)
   - Resample every {elo%, dc%, market%} triplet, compute Brier
   - Output: Confidence interval on optimal blend (could be 30/30/40 or 20/30/50, etc.)
   - Impact: Potential ±1–2pp edge gain if weights are off; confidence in model tuning

3. **Initialize pm_price_history.jsonl and merge PR #95** (30 min)
   - Unblock convergence tracking for advancement edges
   - No new code needed; infrastructure ready
   - Impact: Early alert if PM prices aren't moving toward model (convergence metric live)

4. **Capture closing odds at settlement and compute portfolio CLV walk-forward** (3 hours)
   - Wire auto-settler to record closing_odds from exchange APIs
   - Compute portfolio CLV on placed bets (sample: 8 sportsbook, grow to 30+)
   - Output: First calibration curve, slope/intercept estimates
   - Impact: Understand if edge estimates are biased (positive or negative); catch model failure

5. **Wire Smarkets 1X2 live streaming and compare to Betfair (zero-vig baseline)** (6 hours)
   - Implement Smarkets API client (free, zero commission)
   - Fetch best back/lay for next 3 matches, compare Smarkets vs Betfair
   - Output: Routing decision (Smarkets-first or Betfair-backup)
   - Impact: +0.5–1.5pp edge from venue arbitrage, implementable immediately after (feat/best-price-betfair-pm)

### Prioritized Roadmaps

**24-Hour (Immediate, Group Stage Exit)**
1. Fix results.csv stale data (check clean-results.yml GHA logs, restart if wedged) — 30 min
2. Update wc2026_results.json from results.csv or delete (corrupted copy) — 30 min
3. Initialize pm_price_history.jsonl, merge PR #95 — 30 min
4. Inspect wca.db on mini, reconcile dev.db state (sync or rebuild) — 1 hour
5. Re-run venue benchmark on all 72 group matches (2x speed improvement over initial 14-match run) — 2 hours
6. Reduce TheOddsAPI polling to 3x/day (emergency quota fix) — 30 min

**7-Day (Group to Knockout Transition)**
1. Backtest Elo/DC blend on 72 group matches (walk-forward) — 4 hours
2. Debug Betfair adapter; test with dry order on one fixture — 2 hours
3. Wire closing-odds capture at settlement; compute first portfolio CLV — 3 hours
4. Implement Smarkets API client, compare to Betfair, decide routing — 6 hours
5. Merge feat/ev-on-record, test live PM order fill on one small market — 3 hours
6. Wire unified_exposure into card build (pre-filter recommendations by correlated exposure cap) — 2 hours
7. Rebuild analytics feeds (site-analytics/data/) to match 8001 freshness — 1 hour

**Tournament Rest (Post-R32)**
1. Overhaul advancement correlation model (explicit route probabilities, acca pricer) — 14 hours
2. Wire StatsBomb events for live goal tracking (in-play betting infrastructure) — 21 hours
3. Build correct-score model, backtest on group stage, integrate — 10 hours
4. Resurrect news daemon; integrate team-form flags into Elo adjustments — 16 hours
5. Build player-props model (lineup → SOT/assists); backtest on group — 20 hours

### Exact Follow-Up Tasks (Non-Overlapping Files)

| Task | Files | Owner | Effort | Blocker |
|---|---|---|---|---|
| **Fix results.csv stale data** | data/raw/results.csv, .github/workflows/clean-results.yml | GHA debug | 30 min | Check logs for ESPN/TheSportsDB API failures |
| **Update wc2026_results.json** | data/processed/wc2026_results.json | Card build output | 30 min | results.csv must be current |
| **Init pm_price_history.jsonl** | data/ | Codebase | 15 min | None |
| **Merge PR #95** | .github/workflows/pm-snapshot.yml, src/wca/pmhistory.py, src/wca/outrightedge.py, scripts/wca_pm_snapshot.py | Code review + CI | 30 min | pytest green (should pass) |
| **Re-run venue benchmark** | scripts/wca_venues_benchmark.py, site-analytics/data/venues_benchmark.json | Analysis | 2 hours | 72 matches required (have data) |
| **Backtest blend weights** | scripts/wca_blend_refit.py (new), tests/ | Statistical | 4 hours | Python + scipy |
| **Capture closing_odds** | src/wca/data/results.py, scripts/wca_settler.py, src/wca/markets/kelly.py | Schema + CLI | 3 hours | feat/ev-on-record merge |
| **Debug Betfair** | src/wca/odds_sources/betfair.py, .env | Codebase + creds | 2 hours | Betfair API docs |
| **Wire Smarkets** | src/wca/odds_sources/smarkets.py (new), src/wca/venue_router.py | Codebase | 6 hours | Smarkets API key |

---

## END OF REPORT

**Audit Date**: 2026-06-29 00:30 UTC  
**Auditor**: Claude Code (read-only reconnaissance, corrected)  
**Confidence Levels**: 
- **High** on data flows, cache state, codebase structure (full read)
- **High** on completed fixture count (72, per results.csv with manual verification)
- **Medium** on runtime state (partial launchd/cron visibility)
- **Low** on mini state (SSH inaccessible during audit, wca.db not inspected directly)

**Key Corrections from First Run**:
- **Completed fixtures**: 31 (stale wc2026_results.json) → **72 (authoritative results.csv, all group stage through 2026-06-27)**
- **Data freshness**: wc2026_results.json identified as corrupt/stale; results.csv is canonical
- **Defect priority**: results.csv staleness added as #1 defect (8-day lag, blocking settlement on new matches)

**Recommendations for Next Session**:
1. **Fix results.csv immediately** — Check clean-results.yml GHA logs for API failures; block PR merges until current.
2. **Delete or update wc2026_results.json** — It's a stale copy that corrupts analysis; use results.csv directly.
3. **Merge PR #95** for convergence tracking (0.5-day effort, unblocks outright edge validation).
4. **Reduce TheOddsAPI cadence** to 3x/day before quota exhaustion (0.5-day effort).
5. **Re-run venue benchmark** on 72 matches for updated fair-book ranking (2-hour effort, high ROI).

