# World Cup Alpha: Shared TODO

**Last updated:** 2026-06-18 | **Scope:** cross-session (main + feature/cross-venue-events) + cross-device (Drews-Mac-mini + dev MacBook)

## 🏗️ Infrastructure & Coordination

- [ ] **Shared TODO discipline** — commit & push this file to `origin/main` after each update; all sessions/devices pull on startup
- [ ] **Device sync verification** — Drews-Mac-mini (ops host) reaches origin over SSH; dev MacBook syncs via origin; git identity = user@world-cup-alpha.local where applicable
- [ ] **Branch strategy** — `main` = live (auto-crons only, no feature branches). Feature branches (e.g. `feature/cross-venue-events`) commit work, collide-test against main via periodic git merge --no-ff, cherry-pick to main only when blessed. Track branch ownership in comment below.

## 🎯 P0: Betfair Exchange API Client

**Decision:** Adopt FREE Betfair Delayed Application Key as the PRIMARY pre-match odds source (reduce the-odds-api quota pressure).

- [ ] **Account setup** (one-time, manual)
  - [ ] Register/verify Betfair account
  - [ ] Create app key at developer.betfair.com → copy DELAYED key (free)
  - [ ] Generate RSA 2048-bit self-signed client cert + private key
  - [ ] Upload .crt to Betfair account security settings
  - [ ] Save .key + .crt securely on Drews-Mac-mini, reference from `.env` (NOT committed)

- [ ] **Environment setup**
  - [ ] Uncomment `.env`: `BETFAIR_APP_KEY`, `BETFAIR_USERNAME`, `BETFAIR_PASSWORD`, `BETFAIR_CERT_PATH`, `BETFAIR_KEY_PATH`
  - [ ] Mirror the pattern in `src/wca/data/theoddsapi.py`

- [ ] **Build `src/wca/data/betfair.py`**
  - [ ] Non-interactive certificate login (_login): POST to identitysso-cert.betfair.com/api/certlogin
  - [ ] Session maintenance: keep_alive() before 12h/24h expiry, logout() on exit
  - [ ] JSON-RPC caller to api.betfair.com/exchange/betting/json-rpc/v1
  - [ ] Discovery: list_event_types(), list_competitions(), list_events(), list_market_catalogue()
  - [ ] Pricing: list_market_book() with EX_BEST_OFFERS, parse runner prices → implied probs
  - [ ] Fixture matching: canonicalise team names + kickoff-time window (±90 min)
  - [ ] Return shape: DataFrames matching theoddsapi.py shape (market_id, home_odds, draw_odds, away_odds, etc.)
  - [ ] Throttling: respect 200-point weight cap (EX_BEST_OFFERS = 5 pts), ~5 req/sec per market

- [ ] **Wire into pipeline**
  - [ ] Odds-fetch layer: try Betfair first; fallback to theoddsapi.py only when no Betfair market matched or book is stale/empty
  - [ ] Log source per price (Betfair or OddsAPI) — never fabricate
  - [ ] Update CONFIG_MARKETS / EVENT_MARKETS / wca_event_ev.py to use Betfair for 1X2 + totals when available

- [ ] **Tests** (`tests/test_betfair.py`)
  - [ ] Mock certlogin response, listMarketCatalogue payload, listMarketBook payload
  - [ ] Canonical-name + kickoff-window matching
  - [ ] Overround removal (prices → implied probs)
  - [ ] Weight batching (≤40 marketIds per request with EX_BEST_OFFERS)

---

## 🎯 P1: Historical Match Events Ingestion (Cards / Corners / SOT / Fouls / Possession)

**Decision:** Two-tier free combo (football-data.co.uk + StatsBomb).

### Tier 1: football-data.co.uk CSV backbone

- [ ] **Create `src/wca/data/matchevents.py`**
  - [ ] Unified loader schema: match_id, source, competition, season, date, team, opponent, is_home, neutral, goals, shots, shots_on_target (SOT), corners, fouls, yellows, reds, possession, xg
  - [ ] Missing fields → NaN (football-data has no possession/xg)
  - [ ] Download main-league CSVs (E0-E3, EC, SC0-SC3, D1-D2, I1-I2, SP1-SP2, F1-F2, N1, B1, P1, T1, G1) from football-data.co.uk/mmz4281
  - [ ] Fallback mirrors: footballcsv/cache.footballdata + jokecamp/FootballData (official site was returning ECONNREFUSED)
  - [ ] Column mapping: HS/AS → shots, HST/AST → sot, HC/AC → corners, HF/AF → fouls, HY/AY → yellows, HR/AR → reds
  - [ ] Split home/away rows into two team rows per match
  - [ ] Skip extra-league country files (no stat columns in those CSVs)

### Tier 2: StatsBomb international anchor

- [ ] **Extend `src/wca/data/statsbomb.py`**
  - [ ] Derive shots-on-target: classify Shot events with outcome in {Goal, Saved, Saved To Post} as on-target (freeze this mapping in docstring)
  - [ ] Derive possession: each team's pass share of total (completed_pass events)
  - [ ] Add helpers: get_sot(shot), get_possession(match_events)
  - [ ] Broaden STATSBOMB_COMPETITIONS: add Euro 2024 (comp 55, season 282), Euro 2020 (55, 43), Copa 2024 (223, 282), AFCON 2023 (1267, 107) for richer international sample

- [ ] **Build `scripts/wca_matchevents_data.py`**
  - [ ] Load football-data CSVs + StatsBomb internationals
  - [ ] Compute per-market baselines: corners mean/var, SOT mean/var, fouls, cards from football-data
  - [ ] Compute international-vs-domestic adjustment factors (e.g., WC corners ÷ EPL corners) from StatsBomb
  - [ ] Empirical-Bayes shrinkage: per-team priors = λ × baseline + (1-λ) × thin StatsBomb sample
  - [ ] Output: data/processed/prop_priors.csv with corners, sot, fouls, cards, yellows, reds per team

- [ ] **Wire into `src/wca/models/props.py`**
  - [ ] CornersModel: read from prop_priors.csv instead of hard-coded 8.97; keep as validation check
  - [ ] Add ShotsOnTargetModel (Negative Binomial, from prop_priors)
  - [ ] Add/enhance CardsModel with team aggression factors from prop_priors
  - [ ] Add FoulsModel (optional, for same-game acca correlation)

- [ ] **Tests**
  - [ ] football-data column mapping (HS/AS → shots, HST/AST → sot, etc.)
  - [ ] SOT derivation on a known StatsBomb match
  - [ ] Schema/NaN handling (missing fields = NaN, not 0)
  - [ ] Unified loader returns two team rows per match
  - [ ] **MEMORY rule**: every stat figure traces to a real fetched source, no placeholder numbers

- [ ] **Documentation**
  - [ ] docs/recon/: attribution (football-data.co.uk / J. Buchdahl, StatsBomb), licences, SOT outcome-name mapping decision

**Post-research cost note:** All free. Defer API-Football ($19/mo) to 2026 in-tournament only (Opta is FIFA's exclusive live provider).

---

## 🎯 P2: Expand Cross-Venue Acca Markets (Correlation-Aware, Multi-Leg Support)

**Decision:** Tier-1 four are the core (1X2, Over/Under goals, Draw No Bet, BTTS); Tier-2 (Correct Score, Corners) as book-only upside/coverage.

- [ ] **Extend `src/wca/accas.py`**
  - [ ] Add DNB (Draw No Bet) legs, pulling from EVENT_MARKETS (already fetched by wca_event_ev.py)
  - [ ] Add Over/Under goals at multiple lines (2.5, 1.5, 3.5, etc.), correlation-aware
  - [ ] Correlation check: same-game legs (e.g., Home + Over 2.5 + BTTS-Yes) → derive joint prob from scoreline grid (models/scores.py), not naive product
  - [ ] Tag every leg with arb.py settlement_key; only non-None keys are hedgeable (1x2_90min, dnb_90min, btts_90min, totals_<line>_90min)

- [ ] **Book-only tiers** (Correct Score, Corners)
  - [ ] Allow as upside/coverage legs but flag: "NO HEDGE KEY EXISTS" → never select as lay
  - [ ] Correct Score already used in acca_coverage_optimizer.py SCORELINE_PUNTS; extend to general acca builder

- [ ] **Hedge end-to-end wiring**
  - [ ] 1X2: Betfair SB + lay on Betfair Exchange/Smarkets/Polymarket 3-way
  - [ ] Over/Under + DNB + BTTS: same book → Betfair/Smarkets lay (per settlement_key) + Polymarket if liquid
  - [ ] Route through arb.py find_cross_book_arbs() + find_pm_book_arbs() + rank_arbs()
  - [ ] Net-of-commission edge (0.94 for Betfair until July, then 0.98; 0.98 for Smarkets; 0.97 for PM)

- [ ] **Tier-3 exclusions** (defer until model/data improves)
  - [ ] **Goalscorer**: use only for Betfred Double Delight/Hat-Trick Heaven promo EV via wca_price_scorers.py; no general acca support (npxG shares are estimated, not sourced)
  - [ ] **Cards**: CardsModel needs team aggression/referee priors not yet injected (currently near base-rate); no hedge liquidity
  - [ ] **SOT**: no model class exists (only a boosts.py string "unpriceable"); build from prop_priors, then add model
  - [ ] **Outrights/Advancement**: settle on ET/pens (NOT 90-min) — NEVER mix with 90-min legs (documented fake-arb trap); keep on separate sim/tournament track

- [ ] **Tests** (`tests/test_accas_extended.py`)
  - [ ] Correlation check: same-game legs vs naive product
  - [ ] Settlement-key tag enforcement (no None keys if hedging attempted)
  - [ ] Commission net-out (0.94 / 0.98 / 0.97 per venue)

---

## 🎯 P3: Model Diagnostics & Venue-Correlation Report

- [ ] **Calibration & weak-spots analysis** (from `src/wca/ledger/reports.py`)
  - [ ] Brier score: model vs market (target: model < market)
  - [ ] Calibration bins: observed win-rate vs model prob (check for overconfidence in thin regions)
  - [ ] Per-tournament consistency: halflife_backtest() per holdout (WC2018, WC2022, Euro2024, Copa2024)
  - [ ] Per-team residual variance: which teams/regions are systematically mispredicted?

- [ ] **Venue-correlation report** (historical bet recs vs actual book/PM movement)
  - [ ] Rank bet recommendations by source (Polymarket, Betfair, Smarkets, Paddypower, Virgin, Betfred, bet365)
  - [ ] For each venue: how often did model recs agree/disagree with closing line?
  - [ ] CLV decomposition: which venues generated the most CLV? Which the least?
  - [ ] Biases: does the model systematically overestimate favorites? Underestimate away teams? Etc.

- [ ] **Compile and report**
  - [ ] docs/research/model_diagnostics.md: calibration, per-tournament consistency, per-team residuals
  - [ ] docs/research/venue_correlation.md: CLV by venue, model-vs-closing agreement matrix, systematic biases

---

## 🛠️ Ledger & Site (AWAITING CONFIRM)

- [ ] **Settle last 4 open bets as lost + fix 'unknown' venue**
  - [ ] Verify exact bet IDs (never fabricate)
  - [ ] Mark bets as lost (status='lost', settled_pl=-stake)
  - [ ] Relabel 'unknown' → Betfair Sportsbook account 1
  - [ ] Rebuild + push site

**STATUS:** Awaiting your confirmation to proceed (instruction came via /model-command, not yet executed).

---

## 🔄 Branch Ownership / Coordination

| Branch | Owner | Focus | Status |
|--------|-------|-------|--------|
| `main` | auto-cron | live, clean | 88a3806 (Auto-clean) |
| `feature/cross-venue-events` | Mac Mini (this session) | Betfair + events + accas | in-progress, NOT pushed |
| Other session on main | [dev MacBook] | parallel work | TBD |

---

## 📋 Decision Log

- **Betfair**: FREE Delayed key (no paid Live key unless in-play is needed) — zero cost, unlimited quota, 1-180s latency acceptable for pre-match
- **Historical events**: football-data.co.uk (50k+ matches, free, exact SOT/corners/fouls columns) + StatsBomb (free, international, derive SOT) — defer API-Football ($19/mo) to 2026 in-tournament if Opta blocks free coverage
- **Cross-venue markets**: 1X2 / Over-Under / Draw-No-Bet / BTTS are the hedgeable core (all venues, all model-native, all settlement-keyed). Correct Score & Corners as book-only upside. Goalscorer (Betfred promo-only), Cards/SOT (weak models), Outrights (ET/pens incompatible) deferred.
- **Shared TODO**: commit to origin/main after each sprint so all sessions/devices see the same work queue
