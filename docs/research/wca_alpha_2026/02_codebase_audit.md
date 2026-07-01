# WCA Codebase Audit — Per-Subsystem

Repo: `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha`

Scope: full per-subsystem audit of the World Cup Alpha forecasting + trading stack, oriented toward two builds: **(A) a Polymarket "Perfect Knockout" bracket entry/optimizer** and **(B) trading remaining knockout matches + advancement/futures + match-event markets.**

Status legend used throughout: **live** (on a production path), **experimental** (built+tested, opt-in/research-only), **duplicated** (re-implements logic that exists elsewhere), **disconnected** (wired to scripts but cold in prod DB/feed), **stale** (no live caller / frozen to a past slate / dead), **misclassified** (belongs to a different subsystem).

All citations are `file:line` from the audit data. Line numbers are reproduced exactly as supplied; none are invented.

---

## 1. forecasting-core

**Role in pipeline:** Elo + Dixon-Coles + structural priors + venue-aware host advantage → blended 1X2 → score-matrix → 2026 World Cup Monte-Carlo tournament sim → advancement/outright edges vs Polymarket → outright informational metrics.

**Data-flow role:** This is the head of the pipeline. Features train the Elo rater and the time-decayed Dixon-Coles goals model; structural priors shrink both; venue logic adds a diluted/altitude-aware host bonus. The three legs are blended into a per-fixture 1X2, reconciled against the DC score-matrix, and either (i) persisted by `modelpreds` as the hand-off artefact for every downstream consumer, or (ii) fed through `advancement.make_prob_fn` into the 48-team Monte-Carlo bracket sim. The sim conditions on already-played group results and emits per-team reach/win probabilities, which `compare_to_polymarket` turns into fee-adjusted, quarter-Kelly advancement/outright edges. `outrightedge` provides CLV-replacement informational metrics for the no-fixed-close outright markets.

### Entrypoints
- `scripts/wca_advancement.py` → `wca.advancement.run_advancement` / `compare_to_polymarket` (tournament sim + PM advancement edges)
- `src/wca/advancement.py:430` `TournamentSimulator(grp, prob_fn, results=results)`; `:756` `simulate()`
- `src/wca/advancement.py:159` `make_prob_fn` (model→bracket bridge); `src/wca/sim/tournament2026.py:277` `thirds_assignment` (public allocation lookup)
- `src/wca/card.py:521` `fit_models` (Elo+DC+structural fit); `:1071` `build_card`; `:1286` `build_event_references`
- `scripts/wca_build_card.py` → `modelpreds.write_predictions` (persists blended 1X2 to `data/model_predictions.json`)
- `scripts/wca_outright_edge_data.py` → `outrightedge.convergence` via `pmhistory`

### Components

| File | Symbol | Line | Status | Role / Notes |
|------|--------|------|--------|--------------|
| `src/wca/models/elo.py` | `EloRater._rating_diff` / `EloOutcomeModel.predict_proba` | 261 | live | Elo rating engine + ordered-logistic 1X2; the Elo leg of the blend. host/host_points path (line 282) is the hook venues feeds. Consumed by `card.elo_probs`, `advancement.make_prob_fn`. |
| `src/wca/models/elo.py` | `EloRater.rate_matches` / `EloOutcomeModel.fit` | 334 | live | Batch chronological rating + MLE fit of the ordinal model; called once per card build from `card.fit_models`. The features→Elo training step. |
| `src/wca/models/dixon_coles.py` | `DixonColesModel.fit` / `score_matrix` / `expected_lambdas` | 451 | live | Time-decayed ridge DC goals model with optional structural attack/defence priors. Produces the DC 1X2 leg + score-matrix `scores.py` reconciles; `expected_lambdas` persisted by `modelpreds`. |
| `src/wca/models/scores.py` | `reconcile_scoreline_matrix` / `scoreline_card` | 129 | live | Score-matrix→blended-1X2 reconciliation (min-KL region rescale). Powers correct-score/O-U/BTTS + MatchEventsReference in `card.py`, `nextmatch.py`. |
| `src/wca/models/structural.py` | `dc_priors_from_factors` / `build_dc_priors` | 205 | live | Socio-economic shrinkage priors for DC (and Elo seeds). Wired into `card.fit_models` when `structural_prior=True`. |
| `src/wca/models/structural.py` | `outright_divergence` / `structural_outright_probs` | 249 | stale | Informational divergence flag over outright P(win). Only imported by tests; no live src/script consumer. Dead in the live pipeline. |
| `src/wca/models/venues.py` | `host_advantage_points` / `altitude_penalty_points` | 91 | live | Diluted + altitude-aware co-host bonus (opt-in). Consumed by `card.build_card` and `advancement.make_prob_fn` (venue_aware path). `load_venues` unused outside venuesbench/tests. |
| `src/wca/sim/tournament2026.py` | `TournamentSimulator.simulate` / `_run_knockout` | 756 | live | Vectorised 48-team MC bracket: FIFA-2026 group tie-breaks, 8-best-thirds via THIRDS_ALLOCATION (495 combos), knockout ET model. THE bracket engine. Returns reach/win/group_position. Driven only by `prob_fn`. |
| `src/wca/sim/tournament2026.py` | `THIRDS_ALLOCATION` / `thirds_assignment` / `R32_TIES` / `KNOCKOUT_FEED` | 1000 | live | Official FIFA 2026 third-placed allocation table + fixed R32/knockout bracket topology. Structural backbone any Perfect-Knockout optimizer needs. `thirds_assignment()` is a clean public reuse entrypoint. |
| `src/wca/advancement.py` | `make_prob_fn` | 159 | live | Builds `prob_fn` for the sim: market-anchored Elo/DC/market blend when a tradable 1X2 exists, Elo+DC fallback for generated knockout ties. The single bridge from forecasting models into the bracket sim. |
| `src/wca/advancement.py` | `WC2026_GROUPS` / `run_advancement` / `load_played_group_results` | 75 | live | Canonical 2026 group draw; runs sim conditioned on played group results → per-team stage-prob DataFrame. Entry from `scripts/wca_advancement.py`. |
| `src/wca/advancement.py` | `compare_to_polymarket` / `_fee_adjusted_kelly_stake` / `AdvancementEdge` | 624 | live | Market-compare + EV + sizing for advancement/group-winner/outright PM binaries: devig YES/NO, fee-adjusted edge, capped quarter-Kelly. Picks better of YES/NO per team-stage. |
| `src/wca/outrightedge.py` | `convergence` / `calibration` / `paired_skill` / `information_coefficient` | 44 | live | CLV-replacement informational metrics for no-close outright/advancement. `convergence` is the live leading signal; `calibration`/`paired_skill`/`IC` are COLLECTING (need ≥30 resolved; single-tournament correlation caps n_eff). |
| `src/wca/modelpreds.py` | `build_predictions` / `write_predictions` / `load_latest` / `load_lambdas` | 66 | live | Persists blended 1X2 + Elo/DC/market components + DC lambdas to `data/model_predictions.json(.jsonl)`. Hand-off artefact read by acca optimizer, scorespage, tracking, linemove, exposure model. |

### Bracket relevance
- **(A) Perfect Knockout optimizer — strongly enabled.** `TournamentSimulator` already encodes the exact 2026 topology (`R32_TIES`, `KNOCKOUT_FEED`), the official 8-best-thirds allocation (`THIRDS_ALLOCATION` + `thirds_assignment`), and a calibrated bridge (`advancement.make_prob_fn`). To score a *specific* picked bracket you need the JOINT path distribution, not marginal `reach[]`. The sim already computes every per-sim match winner inside `_run_knockout` (`tournament2026.py:756`) but discards them after crediting reach/win counts; exposing the per-sim `match_winner[mno]` matrix is the single missing primitive. A Perfect-Knockout entry = a fixed pick for all KO matches; its win prob = fraction of sims where every picked winner matches. Group stage is already conditioned on played results (`load_played_group_results`), so a mid-tournament remaining-bracket is supported.
- **(B) Trading remaining KO + advancement/futures + match-event — largely built.** `run_advancement` + `compare_to_polymarket` give per-team reach/win/group-winner edges with fee-adjusted quarter-Kelly today. Remaining single ties reduce to `prob_fn(a,b,knockout=True)` (ET/pens model) priceable vs PM moneyline/series. Match-event markets reuse `scores.scoreline_card` + `card.build_event_references`. Monitoring via `outrightedge.convergence` + `pmhistory`.

### Gaps
- JOINT path distribution not exposed: `_run_knockout` (`tournament2026.py:756`) returns only aggregate reach/win counts; no per-sim winner matrix or path-probability API. Marginal `reach[]` cannot value a correlated all-or-nothing bracket.
- No bracket-entry data structure or optimizer anywhere. Need a `Bracket` pick type over `R32_TIES`/`KNOCKOUT_FEED`, an exact joint-prob scorer, and a search (greedy/beam/MC). None present.
- Knockout ET/pens is a coarse single-parameter model (`et_skill_weight`, sim default 0.5); `advancement` constructs `TournamentSimulator` WITHOUT passing it, so it rides the default — untuned. No separate ET/pens calibration.
- `advancement.make_prob_fn` (`advancement.py:159`) duplicates host/altitude venue logic that `card.build_card` implements via `venues.host_advantage_points` (`venues.py:91`) — `HOST_VENUE_ALTITUDE_M` hard-coded in advancement vs the `venues_2026.csv` table. Two sources of truth for host advantage.
- In-sim goal sampling (`_sample_goals`/`_goal_rates`, mean_goals=2.7, independent Poisson) is decoupled from the `DixonColesModel` score-matrix used elsewhere; tie-break GD/GF realism and any in-sim correct-score view won't match `scores.py`.
- `structural.outright_divergence` (`structural.py:249`) stale (tests-only) — a built divergence/longshot flag not wired into any live report or the advancement output.
- `WC2026_GROUPS` and `THIRDS_ALLOCATION` are hard-coded with no runtime consistency check against the scheduled-fixtures source (validated manually 2026-06-11); draw/data drift would silently mis-bracket.
- Lagging outright metrics (`calibration`/`paired_skill`/`IC`) structurally under-powered for a single tournament (n_eff collapses); only `convergence` is actionable now.

### Reuse recommendations
1. **EXTEND `TournamentSimulator` (do not rewrite):** add opt-in `simulate(..., return_paths=True) -> (n_sims, match_no→winner)` ndarray from `_run_knockout`. Unlocks both bracket scoring and correlated parlay/series pricing with zero new modelling; reuses the tested vectorised `_play_ko`.
2. **Build the Perfect-Knockout optimizer on top of that matrix:** bracket = dict of picked winners over `R32_TIES` + `KNOCKOUT_FEED`; win-prob = mean over sims of all-picks-correct; seed with per-slot most-likely team (chalk) then beam/greedy-swap to maximise EV vs PM payout. Reuse `thirds_assignment` + `THIRDS_ALLOCATION` for slot resolution.
3. **Price remaining single ties** via `prob_fn(a,b,knockout=True)` (already ET/pens-inclusive) and compare to PM moneyline/to-advance using `_fee_adjusted_kelly_stake` + `pm_taker_fee`.
4. **Reuse `scores.scoreline_card` / `card.build_event_references`** for KO match-event markets (correct score, O/U, BTTS): feed the tie's DC matrix + blended 1X2. Output already reconciled to the blend.
5. **Dedupe host advantage:** have `advancement.make_prob_fn` consume `venues.load_venues` / `venues_2026.csv` (as `card` does) instead of its private `HOST_VENUE_ALTITUDE_M`.
6. **Persist tournament outputs** through a `modelpreds`-style artefact so bracket/advancement probs are snapshotted point-in-time and feedable to `outrightedge.convergence`.
7. **Expose and tune `et_skill_weight`** (pass explicitly from `run_advancement`); calibrate vs historical ET/pens frequencies — small localized change.
8. **Wire `structural.outright_divergence`** into the advancement report as a non-staking longshot/data-quality flag.

---

## 2. derivative-models-props

**Role in pipeline:** Event/prop/scoreline + acca/boost models for match-event markets (corners, cards, goalscorer, bet-builder team/player totals, accumulators, price-boost evaluation).

**Data-flow role:** Tail of the pipeline — pure consumers of DC lambdas + the blended 1X2/OU/BTTS feed and cached model JSON; they do not touch raw→clean→corrections→features→Elo→DC→devig→blend. Position is post-blend, at score-matrix → market-compare → EV. `props.py` is the calibrated engine layer (NB corners/cards, Poisson-thinning scorer). `scorers.py` adds brace/hat-trick tails + override store + Double-Delight EV. `betbuilder.py` extends to a full single-fixture bet-builder surface. `accas.py` is the live `/accas` orchestrator (loads `model_predictions.json` + odds_snapshots, builds +EV legs, exposure-gates, assembles accas/promos, PM reconcile; display-only). `boosts.py` is the live `/boost` pricer (1X2/OU/BTTS/CS only).

### Entrypoints
- `src/wca/bot/app.py:1171` `handle_accas` → `wca.accas.build_accas`
- `src/wca/bot/app.py:1162` `handle_boost` → `wca.boosts.evaluate_boost` + `load_scores_feed`
- `src/wca/accas.py:1443` `build_accas`
- `src/wca/boosts.py:374` `evaluate_boost`
- `src/wca/nextmatch.py:391` `ScorerPricer` + `:481` `CornersModel` (live next-match card)
- `src/wca/card.py:1286` `build_event_references` → `CornersModel`/`CardsModel`
- `scripts/wca_betbuilder.py:98` `fixture_betbuilder` (cron-only → `data/betbuilder_latest.json`)
- `scripts/wca_price_scorers.py:93` `double_delight_ev` (offline-only)

### Components

| File | Symbol | Line | Status | Role / Notes |
|------|--------|------|--------|--------------|
| `src/wca/models/props.py` | `CornersModel` | 60 | live | NB total/team corners with damped xG elasticity (StatsBomb WC18+22). Consumed by `nextmatch.py:481`, `card.py:1331`, `betbuilder.py:374/489`, `accas.py:545`. Central event-model primitive. |
| `src/wca/models/props.py` | `CardsModel` | 146 | live | NB total cards with multiplicative aggression. Now wired into `card.py:1332` + `accas.py:546`. Aggression priors still default 1.0 (no team foul-rate feed). |
| `src/wca/models/props.py` | `AnytimeScorerModel` | 197 | live | Poisson-thinning anytime/first-scorer intensity engine. Base layer for `ScorerPricer` + nextmatch goalscorer block. |
| `src/wca/models/scorers.py` | `ScorerPricer` | 62 | live | Adds brace/hat-trick Poisson tails + `ScorerLine`. Used by `nextmatch.py:391`, `accas.py:466`, `betbuilder.py:514`. |
| `src/wca/models/scorers.py` | `ScorerPricer.intensity` | 77 | duplicated | Re-implements `AnytimeScorerModel._intensity` (`props.py:223`) byte-for-byte (npxg cap, penalty add-on, minutes prorate). Comment admits "mirrors … kept explicit". Drift risk. |
| `src/wca/models/scorers.py` | `ScorerPricer.double_delight_ev` | 147 | experimental | Double-Delight/Hat-Trick-Heaven first-scorer boost EV. Tested, but only called by offline `scripts/wca_price_scorers.py` — not in bot/daemon live path. |
| `src/wca/models/scorers.py` | `load_player_overrides` | 184 | live | Loads `data/players.json` (40KB) analyst npxg shares/penalty-taker/minutes. The only player-share source live (`players.db` absent). |
| `src/wca/models/betbuilder.py` | `fixture_betbuilder` | 477 | disconnected | Full single-fixture bet-builder payload. Only caller is cron `scripts/wca_betbuilder.py` → `data/betbuilder_latest.{md,json}`; bot serves cached file. Not reachable from `build_card`/`accas` live EV path. |
| `src/wca/models/betbuilder.py` | `RateStore` | 216 | stale | Loads `team_rates/players` from a Phase-2 `players.db` that does NOT exist on disk. Every lookup falls to tournament priors → all betbuilder shots/SoT/fouls means are unrefit WC priors. `TEAM_PRIORS`/`PLAYER_P90_PRIORS` placeholders. |
| `src/wca/models/betbuilder.py` | `_nb_sf_over` / `_pois_sf_over` / `_fair_pair` | 162 | duplicated | Local copies of `props.py` `_nb_sf_over`/`_fair_odds` ("kept local so the module stands alone"). Same math, separate maintenance surface. |
| `src/wca/accas.py` | `build_accas` | 1443 | live | `/accas` orchestrator (`bot/app.py:1190`). Loads cached preds+lambdas+odds_snapshots, builds legs, exposure-gates, assembles accas/promos, PM reconcile. Display-only, never writes ledger. |
| `src/wca/accas.py` | `candidate_prop_legs` | 532 | live | Builds corners/cards legs via `CornersModel`/`CardsModel`, ONLY when a matching book price exists in the snapshot. Cards uses model default aggression (all fixtures same cards mean). |
| `src/wca/accas.py` | `candidate_scorer_legs` | 441 | live | Anytime/first-scorer legs via `ScorerPricer` + cached lambdas + `players.json` + snapshot price. Gates on all three present, else silently empty. |
| `src/wca/boosts.py` | `evaluate_boost` | 374 | live | `/boost` pricer (`bot/app.py:1162/1224`). Pure map of one enhanced-odds offer onto `scores_data.json`: 1X2/OU/BTTS/correct-score only; player props/corners/cards/in-play → `priceable=False`. No EV write. |

### Bracket relevance
- **(A) Perfect Knockout — weakly relevant.** Prices match-EVENT/single-match markets, not multi-round advancement. Directly reusable: `exposure_corr.scoreline_matrix` (imported by accas for joint same-game probability) + the DC lambdas these modules consume. A bracket optimizer needs round-by-round advancement probabilities (tournament-sim, upstream). `accas.assemble_accas`'s correlated-exposure gate (`FIXTURE_CAP_FRACTION`, incremental-Kelly `_leg_passes_gate`) and `_joint_prob_same_game` are a good *template* for the bracket's correlated-leg handling (a Perfect-Knockout entry IS a giant correlated parlay) but operate per-fixture, not across the tree. boosts/props/scorers/betbuilder essentially irrelevant to bracket optimization.
- **(B) Trading remaining KO + match-event — HIGH relevance.** `CornersModel`/`CardsModel`/`AnytimeScorerModel` + `ScorerPricer` are calibrated, tested, live in the next-match card + `/accas` — directly reusable to price KO match-event markets. `accas.build_accas` already does market-compare→EV→sizing with exposure-aware Kelly. `boosts.evaluate_boost` reusable as-is for KO price boosts on 1X2/OU/BTTS/CS. Advancement/futures are NOT priced anywhere here — that is the gap.

### Gaps
- `players.db` does not exist on disk → `betbuilder.RateStore` (`betbuilder.py:216`) always falls to priors; every betbuilder mean is an unrefit WC order-of-magnitude prior. Docstring flags Phase-2 pending.
- `betbuilder.fixture_betbuilder` (`betbuilder.py:477`) disconnected from live EV: only cron consumes it, bot serves cached markdown; its fair-odds never compared to live book/PM prices.
- `CardsModel` aggression defaults to 1.0 everywhere live (`card.py` + `accas.candidate_prop_legs` pass no per-team foul priors) → every fixture gets the same ~3.41 cards mean; no team/referee/derby differentiation despite model support.
- Duplicated intensity math: `ScorerPricer.intensity` (`scorers.py:77`) re-implements `AnytimeScorerModel._intensity` (`props.py:223`); `betbuilder._nb_sf_over`/`_fair_pair` (`betbuilder.py:162`) duplicate props helpers. Changes must be made in 2–3 places.
- `double_delight_ev` (`scorers.py:147`) tested but only reachable from an offline script — a recurring promo class un-automated.
- No advancement/futures pricing in this subsystem: `/boost` returns unpriceable, `/accas` has no futures legs.
- No Perfect-Knockout bracket scorer/optimizer; correlated-parlay machinery is per-single-fixture only.
- `candidate_scorer_legs`/`candidate_prop_legs` fire only when a matching snapshot price exists; PM/exchange coverage for corners/cards/scorer is near-zero, so these legs rarely materialize — most event edge invisible to the live acca path.

### Reuse recommendations
1. **Extend `accas.build_accas` / `candidate_prop_legs` / `candidate_scorer_legs`** for KO match-event trading; add per-fixture `CardsModel` aggression + team dispersion as `players.db` lands.
2. **Populate `players.db`** (schema `RateStore` already reads) to turn betbuilder counts from priors into real edges, then wire `fixture_betbuilder` output into the live market-compare/EV path (currently cron-only).
3. **De-duplicate intensity math:** make `ScorerPricer` call `AnytimeScorerModel._intensity` (or promote to shared public helper); have `betbuilder` import `props._nb_sf_over`/`_fair_odds`.
4. **For bracket correlated-parlay handling, reuse `accas._joint_prob_same_game` + `exposure_corr.scoreline_matrix` as the *pattern*,** but build a new bracket-level joint-probability layer over a tournament-sim advancement matrix; extend (don't fork) the exposure/Kelly gate.
5. **Wire `ScorerPricer.double_delight_ev` into `/boost`:** when a boost is a first-goalscorer offer and a `players.json` share exists, route to the tested EV instead of declaring unpriceable.
6. **Add an advancement/futures pricing module fed by tournament-sim,** surfaced through the existing accas leg-builder + boosts mapping; reuse `OverUnderLine`/`Leg`/`BoostEval` + `ev_vs_offer`/`_eff` helpers.
7. **Keep `boosts.evaluate_boost` as the canonical boost pricer** for KO 1X2/OU/BTTS/CS; broaden its market coverage (scorer via scorers, corners/cards via props) rather than writing a parallel evaluator.

---

## 3. markets-sizing

**Role in pipeline:** De-vig, Kelly staking, card generation, exposure/correlation sizing. Owns the back half of raw→…→devig→blend→score-matrix→market-compare→EV→sizing→recommendation, plus a parallel exposure/risk surface that reads the ledger.

**Data-flow role:** Live happy path (`scripts/wca_build_card.py:main`): `load_results` → `card.fit_models` (Elo + ordered-logit + DC) → `odds_source.get_odds` → `card._index_odds` (groups flat odds into per-fixture books keyed by canonical team-pair) → `card.market_consensus` (Shin de-vig + per-column median; DEVIG) → `card._iter_fixture_blends` (BlendWeights 0.10/0.30/0.60; BLEND) → `card.build_card` (fee-adjusted best_price, `kelly.edge`, imminent-edge discount + min_edge gate, single-source "indicative" flag, per-pool `kelly.stake`; EV→SIZING) → `apply_daily_exposure_caps` → `rank_card` (hit-prob buckets, CUT mispriced-minnow longshots; RECOMMENDATION) → `format_ranked_card` + `cardcache.write_card`; `modelpreds.write_predictions` persists blended 1X2 + DC lambdas. Reference-only surfaces (`build_score_cards`/`build_event_references`) are NEVER staked. `resolve_pool_bankroll` reads ledger CLV for the footer but the deployed base is the fixed dual-pool (£1500 GBP + $1995 PM, 1/2-Kelly), so `KellyPolicy` is governance-only. Separate post-trade branch: `exposure.build_exposure_data` → `exposure_corr.build_correlated_exposure` (shared-scoreline same-fixture P&L, convolution, 5%-bankroll cap) → `site/exposure_data.json`; a cruder `exposure_dashboard.compute_dashboard_metrics` runs alongside.

### Entrypoints
- `scripts/wca_build_card.py:main` (line 85) — live card builder
- `src/wca/card.py:build_card` (line 1071) — gating layer
- `src/wca/card.py:rank_card` (line 1463) — hit-prob ranking + longshot CUT
- `src/wca/card.py:market_consensus` (line 647) — DEVIG (Shin + median)
- `src/wca/card.py:resolve_pool_bankroll` (line 318) — CLV-ladder rung (footer/governance)
- `src/wca/markets/devig.py:shin` (line 179) — the only de-vig on the live path
- `src/wca/markets/kelly.py:stake` (line 82) and `KellyPolicy.evaluate` (line 257)
- `scripts/wca_exposure_data.py:main` (line ~150) → `src/wca/exposure.py:build_exposure_data` (line 173)
- `src/wca/exposure_corr.py:build_correlated_exposure` (line 408)
- `src/wca/cardcache.py:read_card` (line 56) / `write_card` (line 29)

### Components

| File | Symbol | Line | Status | Role / Notes |
|------|--------|------|--------|--------------|
| `src/wca/markets/devig.py` | `shin` | 179 | live | DEVIG. Štrumbelj/Shin closed-form de-vig with favourite/longshot correction; the de-facto method (`market_consensus` calls only `shin`). Exact on fair books, bisection on z. |
| `src/wca/markets/devig.py` | `multiplicative` / `power` / `compare_methods` | 122 | experimental | Alternative de-vig + comparison table. Implemented + tested but NOT called by any live code. Research-only toolkit. |
| `src/wca/markets/kelly.py` | `stake` / `kelly_fraction` / `edge` / `simultaneous_exposure_scale` | 82 | live | SIZING. Fractional-Kelly with per-bet cap (`build_card`), edge gate, same-day exposure scaling (`apply_daily_exposure_caps`). Core staking math. |
| `src/wca/markets/kelly.py` | `KellyPolicy` | 217 | live | Pre-registered CLV-gated Kelly ladder. Still called by `resolve_pool_bankroll`, but its escalating 0.25/0.35/0.50 fractions are overridden by the fixed dual-pool 1/2-Kelly + `FLAT_KELLY_FRACTION`. Governance/footer reporting only. |
| `src/wca/card.py` | `build_card` | 1071 | live | Central EV→SIZING gate: best_price (fee-adjusted), edge, imminent discount, min_edge, single-source indicative flag, per-pool routing + `kelly.stake`. |
| `src/wca/card.py` | `resolve_pool_bankroll` / `default_pools` / `BlendWeights` | 318 | live | BLEND weights (0.10/0.30/0.60) + bankroll governance. `default_pools` is the live sizing base; `resolve_pool_bankroll` reads ledger CLV for the footer. Heavy policy churn documented inline (dual-pool 2026-06-28 supersedes rung-bankroll ladder). |
| `src/wca/card.py` | `rank_card` / `_cut_reason` / `classify_outcome` | 1463 | live | RECOMMENDATION rule: hit-prob bucketing, structural-draw band, longshot CUT (stakes zeroed). |
| `src/wca/card.py` | `build_score_cards` / `build_event_references` / `whole_book_exposure` | 1181 | live | Reference-only score-matrix + corners/cards/BTTS surfaces (NEVER staked) reconciled to the blend; `whole_book_exposure` is the independent-stake cross-venue cap. |
| `src/wca/cardcache.py` | `write_card` / `read_card` | 29 | live | Deterministic file cache for the formatted card; written by `wca_build_card.py`, read by `bot/app.py`, `wca_morning.py`. |
| `src/wca/exposure.py` | `build_exposure_data` | 173 | live | LEDGER→exposure feed: per-match/acca expected P&L, blind spots, gap-plugs, scenario distribution; delegates same-fixture correlation to `exposure_corr`. |
| `src/wca/exposure_corr.py` | `build_correlated_exposure` / `settle_on_scoreline` / `fixture_pnl_distribution` | 408 | live | Correlation-aware sizing: settles all same-fixture bets on one shared Poisson scoreline (from persisted DC lambdas), exact cross-fixture convolution, 5%-bankroll per-fixture cap. Also reused by `accas.py`. |
| `src/wca/exposure_dashboard.py` | `compute_dashboard_metrics` / `publish_dashboard_json` | 19 | duplicated | LEDGER→dashboard. Live (`publish_site.sh:18`; `wca_betrecs.py` consumes `site/exposure_dashboard.json`) but a crude hardcoded-heuristic duplicate of `exposure.build_exposure_data`: p_profit=0.6/0.4 stub, empty placeholders, best/worst-case ignore correlation. |
| `scripts/wca_exposure_sizer.py` | `classify` / `main` | 41 | stale | Standalone top-up sizer with `TEAM_OUTCOME`/`MATCH_OF` hardcoded to a past slate (AUS_TUR, HAI_SCO, …). Not referenced by any cron/deploy/Makefile; superseded by `exposure.py` + `exposure_corr.py`. No-ops once those fixtures pass. |

### Bracket relevance
- **(A) Perfect Knockout — de-vig + blend + score-matrix half directly reusable.** `devig.shin`/`multiplicative` handle n-way books; `card._iter_fixture_blends` produces clean per-match (home,draw,away) blends. A KO bracket needs MATCH-WIN (90+ET/pens) probs, not 1X2 — derive from the same DC lambdas/Elo by collapsing the draw into a shootout split. `exposure_corr.scoreline_matrix` + `settle_on_scoreline` give a tested within-match correlation engine. `kelly.stake`/`simultaneous_exposure_scale` size a bracket entry as a single +EV ticket once you have P(exact bracket) vs PM price. The "single shared scoreline → joint P&L" idea generalizes to "single shared bracket path → joint settlement". (Tournament-sim itself is NOT here — see gaps.)
- **(B) Trading remaining KO + match-event — reusable as-is.** `build_card`/`rank_card`/`resolve_pool_bankroll` for per-match KO 1X2; `_index_odds` line-shopping, fee-adjusted `net_odds`, indicative single-source guard, per-pool currency routing already target the Smarkets/Betfair/Polymarket set. `build_event_references` already prices corners/cards/BTTS off the live DC fit (reference-only; flip to staked by wiring an edge/Kelly gate). `exposure.py`/`exposure_corr.py` cover acca-style multi-leg correlation. CLV/ledger plumbing in place.

### Gaps
- No tournament-sim / advancement engine here: blend stops at single-match 1X2. A Perfect-Knockout optimizer needs P(advance per round) and P(exact bracket) — not present (`card.py` is per-fixture only).
- No match-WIN (incl. ET/pens) model: 1X2 includes draws; KO markets resolve on who advances. The draw mass must be reallocated to a shootout/ET winner; no code does this.
- `exposure_dashboard.py` (`exposure_dashboard.py:19`) is a crude live duplicate of `exposure.build_exposure_data` with placeholder blind_spots/worst_result_states + hardcoded 0.6/0.4 win-prob heuristics — two exposure feeds can disagree.
- `scripts/wca_exposure_sizer.py` (`:41`) stale: `TEAM_OUTCOME`/`MATCH_OF` frozen to a group-stage slate, parses the card by regex, silently no-ops on new fixtures.
- `devig.power`/`multiplicative`/`compare_methods` (`devig.py:122`) implemented + tested but unused on the live path.
- `exposure_corr` uses an independent-Poisson grid (no DC tau low-score correction) for settlement weighting — worth passing the exact reconciled DC matrix when valuing correlated correct-score/low-total bracket legs.
- Bankroll/Kelly policy has heavy inline churn: `KellyPolicy` ladder overridden by `FLAT_KELLY_FRACTION` and again by `default_pools` 1/2-Kelly; three layers coexist. A reader cannot tell from `kelly.py` alone what fraction actually deploys.
- No knockout-specific correlation in `build_card`'s whole_book/daily caps: same-team advancement + match-win + outright futures are highly correlated across markets; caps are per-fixture/per-day, not per-underlying-team across futures.

### Reuse recommendations
1. **Add `match_win_probs(models, home, away)`** next to `elo_probs`/`dc_probs` in `card.py` that collapses DC/Elo draw mass into an ET/penalty winner split → 2-way P(advance). The single missing primitive both bracket use-cases need.
2. **Build the tournament-sim as a NEW module (`wca/tournament.py`)** consuming `card.fixture_blends` + `match_win_probs`, Monte-Carlo/exact-chaining the bracket; keep it OUT of `card.py`. Feed its P(exact bracket) into `kelly.stake`/`edge`.
3. **Reuse `exposure_corr.settle_on_scoreline`/`fixture_pnl_distribution`** as the settlement engine for bracket/advancement legs — generalize to `settle_on_bracket_path(bet, path)` following the identical shared-state pattern; preserves tested convolution + cap machinery.
4. **For per-match KO + match-event, reuse `build_card`/`rank_card`/`format_ranked_card` unchanged;** to stake match-event markets, add an edge+Kelly gate onto `build_event_references` rather than a second pipeline.
5. **Promote the netting logic in `wca_exposure_sizer.py`** (`recommended_add = max(0, kelly_target - existing_real_money_exposure)`) into a tested function in `exposure.py` keyed off model fixtures, then delete the script.
6. **Consolidate the two exposure feeds:** make `exposure_dashboard.publish_dashboard_json` derive from `exposure.build_exposure_data` (or retire it and point `wca_betrecs.py` at `exposure_data.json`).
7. **Wire `devig.power` + `devig.shin` via `compare_methods`** as a de-vig sensitivity row on bracket/advancement markets — favourite/longshot bias is largest on long-priced advancement outcomes.
8. **Factor "which fraction actually deploys" into one `deployed_kelly_fraction()` function** so the rung-ladder vs flat-Kelly vs dual-pool layering is explicit.

---

## 4. arbitrage-crossvenue

**Role in pipeline:** Settlement-guarded cross-venue arbitrage + matched-betting/promo extraction.

**Data-flow role:** A market-compare/screening branch off the live odds feed. `arb.settlement_key` is the load-bearing safety primitive (refuses outright/to-qualify/advancement ET+pens markets; returns `1x2_90min`/`btts_90min`/`dnb_90min`/`totals_<line>_90min`). Two parallel orchestrators exist: `wca.arb.find_*` (used by scripts) and `wca.intel.arb.scan_*` (the live bot `/arb`, with staleness+liquidity gating). FX locks via `arbfx.best_lock`. Matched-betting (`matched.py`) and the promo ledger (`offers.py`) are intentionally isolated from the model/CLV ledger.

### Entrypoints
- `wca/arb.py:40` `settlement_key` — fake-arb settlement guard
- `wca/arb.py:214` `find_cross_book_arbs`; `wca/arb.py:281` `find_pm_book_arbs` (gated on `settlement_key==1x2_90min`)
- `wca/arbfx.py:221` `best_lock` (hard same-(fixture,market,outcome) guard at `arbfx.py:245` `_same_event`)
- `wca/arbdata.py:81` `build_arb_data` (monitoring feed; guard `settlement!='1x2_90min'` at `arbdata.py:108`)
- `wca/intel/arb.py:464` `scan_market` — LIVE bot orchestrator (`bot/app.py:2491`)
- `wca/matched.py:203` `qualifying_bet` / `:258` `free_bet_snr`
- `wca/offers.py:122` `record_offer` / `:287` `offers_summary`
- `wca/venues.py:104` `canon_book`
- `scripts/wca_arb.py:171` `main`; `scripts/wca_event_ev.py:51` `main` (NOT arbitrage; model-edge EV)

### Components

| File | Symbol | Line | Status | Role / Notes |
|------|--------|------|--------|--------------|
| `src/wca/arb.py` | `settlement_key` / `find_cross_book_arbs` / `find_pm_book_arbs` | 40 | live | Deterministic settlement-guarded arb core. `settlement_key` refuses ET/pens markets. Pure, no network. `find_cross_book_arbs` (arb.py:235) excludes h2h_lay; relies on `settlement_key` returning None for unsafe markets. `find_pm_book_arbs` guard at `arb.py:349`. Reused by arbfx, intel/arb, scripts; tested. |
| `src/wca/arbfx.py` | `evaluate_pair` / `best_lock` / `evaluate_lock` / `exchange_lay_net` | 221 | live | FX-adjusted PM(USD)↔exchange(GBP) lock + N-venue lock with hard cross-fixture/cross-team guard. Imports primitives from `arb.py`. Consumed by `arbdata.build_arb_data` + `intel/arb.scan_back_lay` (`intel/arb.py:326,329`). `evaluate_pair` (`arbfx.py:58`) is the 2-leg legacy path, tests-only. |
| `src/wca/arbdata.py` | `build_arb_data` | 81 | live | Pure builder for the monitoring-only Arb-tab feed (`site/arb_data.json`). Guard `1x2_90min` only; home/away slots only (Draw excluded, `arbdata.py:16-17`). Monitoring-only, never executes. |
| `src/wca/intel/arb.py` | `scan_market` / `scan_cross_book` / `scan_back_lay` / `scan_pm_book` | 464 | live | Newer market-intel orchestrator wired to live `/arb` (`bot/app.py:2491`). Adds staleness + liquidity "actionable" gating absent from the core. Delegates all math to `arb.py`/`arbfx.py`. DUPLICATED orchestration vs `arb.find_*` (math shared, control flow not). |
| `src/wca/matched.py` | `qualifying_bet` / `free_bet_snr` / `free_bet_sr` | 203 | live | Pure matched-betting lay calculators. No IO. Front-ended by `scripts/wca_matched.py`; tested. Deliberately separate from model edge / arb / CLV. |
| `src/wca/offers.py` | `record_offer` / `update_offer` / `offers_summary` | 122 | live | Isolated `sb_offers` SQLite ledger for promo extraction; never touches bets/bankroll tables. Sibling `wca/promos.py` (`sb_promos`). Excluded from `ledger.reports.summary`/CLV. |
| `src/wca/venues.py` | `canon_platform` / `canon_book` / `is_exchange` / `EXCHANGE_VENUES` | 104 | live | Leaf canonicaliser — single source of truth for venue names. High fan-in (`ledger/store.py`, `bot/app.py`, `positions_sync.py`, `intel/registry.py`, `venuesdata.py`, scripts). Hard rule: bare `betfair`→`Betfair Sportsbook`; explicit exchange keys→`Betfair`. |
| `src/wca/venuesdata.py` | `build_arm_a` / `assemble_feed` / `per_book_quotes_from_rows` | 270 | misclassified | Model-vs-Venue benchmark data layer (venues-benchmark subsystem), only shares `venues.canon_book` with arbitrage. Driven by `scripts/wca_venues_benchmark.py`. `link_model_bets` references `rigor_clv_MIN` before assignment (`venuesdata.py:470` uses it, defined `:477`) — works at call time but a smell. |
| `scripts/wca_arb.py` | `main` / `_build_pm_quotes` / `_fmt_legs` | 171 | live | Original-core live arbitrage sweep CLI: TheOddsAPI h2h + derivatives + Polymarket, runs both detectors, writes `docs/research/arb_methodology.md`. Live API + file write — DO NOT RUN. Overlaps `intel/arb`. |
| `scripts/wca_event_ev.py` | `main` / `fee_adj_pm_edge` | 51 | duplicated | NOT arbitrage — model-edge EV sweep. Re-implements PM fee-adjusted edge (`:43`) + exchange-commission haircut (0.94 inline, lines 96,155) instead of importing `arb.effective_back`/`pm_yes_to_decimal`. Belongs to EV subsystem. |

### Bracket relevance
REUSE POTENTIAL HIGH but the subsystem is deliberately scoped to **90-minute settlement** — exactly the wrong scope for a knockout bracket.
- **(A) Perfect Knockout — arb code NOT directly reusable.** `settlement_key` (`arb.py:40`) REFUSES every advancement/to-qualify/outright market (returns None), precisely the family a bracket optimizer trades. Reusable adjacent assets: `sim/tournament2026.py` + `advancement.py` (per-team per-stage advancement probs under PM resolution). From named files, reuse PM net-price helpers (`arb.pm_yes_to_decimal`, `arbfx.pm_no_net`) + `venues.canon_*`. Use `offers`/`matched` only if bracket entries are promo-funded.
- **(B) Trading remaining KO + advancement/futures + match-event.** For 90-min KO markets (1X2/BTTS/totals/DNB), `intel/arb.scan_market` + the core detectors work AS-IS. For advancement/futures the settlement guard correctly blocks 90-min pairing → value is in `advancement.py` + `outrightedge.py`. For match-event/prop markets the intel registry enumerates types (`registry.py:21-24`) but no model prices them yet. The settlement-key discipline is the single most valuable transferable idea.

### Gaps
- `settlement_key` (`arb.py:40`) has NO key for advancement/to-qualify/winner-with-ET markets — only returns None. A bracket/futures trader needs POSITIVE settlement keys (e.g. `advance_R16_inc_ET`, `win_tie_inc_pens`) so ET-inclusive markets can be paired with each OTHER and with the sim.
- Two parallel arb orchestrators (`wca.arb.find_*` scripts vs `wca.intel.arb.scan_*` live bot) implement the same three families; only intel/arb has actionable gating. Risk of divergent behaviour.
- No order-book DEPTH/SIZE anywhere — every leg assumes top-of-book fillable (docstrings flag it: `wca_arb.py:74-81`, `intel/arb.py:17-27`). Not execution-grade.
- Polymarket has no captured price SERIES in `odds_snapshots` (`venuesdata.py:620` "COLLECTING"); PM arb/edge runs off ad-hoc per-run fetches. `wca/pmhistory.py` exists but is not wired into the arb feed.
- `arbdata.build_arb_data` drops the Draw leg (`arbdata.py:16-17`), only home/away two-way — site feed is not the full 1X2 partition `find_pm_book_arbs` covers.
- `wca_event_ev.py` duplicates commission (0.94 literal) + PM-fee math instead of importing `arb.effective_back`/`pm_yes_to_decimal`. (Betfair commission already noted as 2% from July in `arb.py:26`.)
- No FX persistence/risk model beyond a flat haircut (`arbfx` DEFAULT_FX_HAIRCUT 0.5%, intel DEFAULT_FX_USD_PER_GBP 1.33 hardcoded) — weeks-held cross-currency positions carry unsized FX exposure.
- `venuesdata.py` misclassified into this set (venues-benchmark); `link_model_bets`/`rigor_clv_MIN` ordering (`:470` vs `:477`) is a latent smell.

### Reuse recommendations
1. **EXTEND `settlement_key`** (`arb.py:40`): add positive keys for ET-inclusive families (`advance_<stage>`, `win_tie_inc_pens`, `outright_winner`) so the guard can safely pair advancement markets across PM/exchange and against the sim. Keep None-refusal as default.
2. **Build the Perfect-Knockout optimizer on `sim/tournament2026.py` + `advancement.py`,** not on `arb.py`. Reuse `arb.pm_yes_to_decimal`/`arbfx.pm_no_net` for PM fee pricing and `venues.canon_*` for normalisation.
3. **For 90-min KO match-arb, reuse `intel.arb.scan_market` AS-IS** (live, gated, tested). Retire/thin `scripts/wca_arb.py` to call the same intel path → ONE orchestrator with the actionable gate.
4. **Reuse `arbfx.best_lock`'s hard same-event guard** (`arbfx.py:221-251`) as the template for a cross-settlement guard when pairing advancement legs; generalise to assert matching settlement keys.
5. **Persist PM advancement/futures prices via `wca/pmhistory.py`** and wire into arb/edge feeds so `outrightedge` convergence/calibration runs continuously.
6. **De-duplicate `wca_event_ev.py`:** import `arb.effective_back`/`arb.pm_yes_to_decimal` instead of inline 0.94 + `fee_adj_pm_edge`.
7. **Reuse `offers.py`/`matched.py` UNCHANGED** only if bracket entries are promo-funded; keep them isolated from the model/CLV ledger.
8. **Add a liquidity/depth adapter** behind the existing `intel/arb` actionable gate (`intel/arb.py:186-206`) before any KO/futures position is treated as execution-grade.

---

## 5. polymarket-stack

**Role in pipeline:** Polymarket Gamma/CLOB data + sizing + trader/guardrails + cash-out, all dry-run-gated by `PM_DRY_RUN`.

**Data-flow role:** The PM market-access + execution layer. `find_world_cup_markets` is the single source of WC events; `_yes_token_and_price` the canonical YES resolver reused everywhere. `polymarket_odds.get_odds` feeds `odds_source` (always-on floor). `propose.build_pm_proposals` re-sizes card recs at PM price (sizing→recommendation). `ClobTrader.place_order` is the only money-touching method (guardrailed, `PM_DRY_RUN`-gated). `positions.fetch_positions`/`fetch_trades` give read-only inventory + ground-truth fills. `cashout.decide_cashout` is a pure exit predicate. `pmhistory.append_snapshots`/`convergence_inputs` feed outright CLV.

### Entrypoints
- `src/wca/data/polymarket.py:448` `find_world_cup_markets`; `:215` `resolve_outcome_token`; `:155` `_yes_token_and_price`
- `src/wca/data/polymarket_odds.py:177` `get_odds`; `:91` `_is_full_match_event`
- `src/wca/pm/propose.py:45` `build_pm_proposals`
- `src/wca/pm/trader.py:932` `ClobTrader.place_order`; `:78` `resolve_funder_from_env`; `:701` `detect_account_class`
- `src/wca/pm/signing.py:696` `build_signed_order`
- `src/wca/pm/positions.py:156` `fetch_positions`; `:138` `fetch_trades`
- `src/wca/pm/cashout.py:472` `decide_cashout`; `:173` `evaluate_position`
- `src/wca/pmhistory.py:55` `append_snapshots`; `:136` `convergence_inputs`
- `src/wca/data/pm_inventory.py:207` `refresh_pm_events`; `:338` `get_cached_pm_events`

### Components

| File | Symbol | Line | Status | Role / Notes |
|------|--------|------|--------|--------------|
| `src/wca/data/polymarket.py` | `find_world_cup_markets` / `resolve_outcome_token` / `_yes_token_and_price` | 448 | live | Gamma read client; single source of WC events. `_yes_token_and_price` (155) canonical YES resolver. `resolve_outcome_token` (215) market-compare bridge for h2h. Also carries `resolve_player_anytime_token` (313) + `resolve_exact_scores` (385) — only `wca_event_ev.py` uses them; not in bot/card path. |
| `src/wca/data/polymarket_odds.py` | `get_odds` / `rows_from_events` / `_is_full_match_event` | 177 | live | raw→clean: PM YES mids → h2h odds frame; always-on floor in `odds_source`. Mid-of-book, no overround removed. `_is_full_match_event` (91) guards ancillary-event contamination (2026-06-29 bug). |
| `src/wca/pm/propose.py` | `build_pm_proposals` | 45 | live | sizing→recommendation: card recs re-sized quarter-Kelly at PM price, tick-snapped, hard-capped. Money-adjacent (decides stake) but never places. Called by `scripts/wca_pm_propose.py`. |
| `src/wca/pm/trader.py` | `ClobTrader.place_order` | 932 | live | execution: only order-placement client. Guardrails = per-order USD cap, keyword allowlist, rolling-UTC-day notional cap, funder-safety refusal, LiveOrderUnconfirmed reconciliation. MONEY-TOUCHING. `PM_DRY_RUN`-gated (`bot/app.py:1807` default '1'). Live fills did occur historically (16 polymarket bets rows). |
| `src/wca/pm/signing.py` | `build_signed_order` / `build_erc7739_1271_signature` | 696 | live | execution-crypto: pure EIP-712 ClobAuth + V1/V2 signing + HMAC. maker=funder, signer=EOA proxy fix. V1 path (`_build_signed_order_v1`, 883) + sig-type-3 ERC-7739 (554) regression/opt-in; V2 sig-type-2 is live. Heavily tested; reuse as-is. |
| `src/wca/pm/positions.py` | `fetch_positions` / `fetch_trades` / `Position` | 156 | live | ledger/CLV input: read-only Data API inventory + ground-truth fills. NOTE: `scripts/wca_pm_watch.py:40` defines its OWN inline `fetch_positions` (urllib) instead of importing this — partial duplication. |
| `src/wca/pm/cashout.py` | `decide_cashout` / `evaluate_position` / `build_sell_proposal` | 472 | live | execution(exit): pure kill predicates (exact-score/totals/BTTS) + marketable SELL plan; drives de_risk SELL via cashout_watch daemon. `classify_market` (64) already recognises advancement/team_win/totals/btts — extensible. |
| `src/wca/pmhistory.py` | `append_snapshots` / `convergence_inputs` / `append_jsonl` | 55 | live | CLV-proxy store: append-only PM price trajectory feeding `outrightedge.convergence`. Live DB has 155 advancement snapshots but all at a SINGLE timestamp (2026-06-29 07:15); convergence needs ≥2 snaps/market → currently yields nothing. Mechanism live, data thin. |
| `src/wca/data/pm_inventory.py` | `refresh_pm_events` / `get_cached_pm_events` / `_process_event` | 207 | disconnected | intended `/accas` match-event market cache. `pm_inventory` TABLE ABSENT from live `wca.db` (never populated). `_process_event` (146) RE-IMPLEMENTS YES mid-price logic inline instead of calling `_yes_token_and_price` — drift risk. Wired to scripts, cold in DB. |
| `src/wca/pm/trader.py` | `_build…` V1 / SIG_POLY_1271 path | 804 | experimental | regression-only V1 exchange + opt-in sig-type-3 deposit-wallet signing. Reachable only via env flags; live account is V2 sig-type-2. Parity, not live. |

### Bracket relevance
STRONG, with concrete gaps.
- **(A) Perfect Knockout.** Read layer directly reusable: `find_world_cup_markets` + `_yes_token_and_price` pull every WC event; `resolve_outcome_token` resolves single-match winners; `cashout.classify_market` labels advancement/team_win; `pmhistory` stores per-team advancement trajectories (155 rows). MISSING: a bracket-aware resolver — the Perfect-Knockout market is one neg-risk event with one YES token per (team,round) advancement leg; no function enumerates those legs or maps R32→…→Final survival probs onto them. The optimizer needs the tournament-sim JOINT distribution (not these marginal prices). `propose` is single-leg quarter-Kelly, not a correlated basket.
- **(B) Trading remaining KO + advancement/futures + match-event — the sweet spot.** h2h KO trading works end-to-end (`polymarket_odds`→`card`→`build_pm_proposals`→`place_order`). Advancement/futures: `pmhistory`+`outrightedge` give convergence edge but need a 2nd+ snapshot cadence; `propose.py` only sizes h2h. Match-event resolvers EXIST (`resolve_exact_scores:385`, `resolve_player_anytime_token:313`) and `cashout.evaluate_position` prices the kill side, but NO producer wires these into card/propose for ENTRY. Guardrails (per-order/daily caps, allowlist incl. `fifwc`, funder-safety, LiveOrderUnconfirmed) transfer unchanged.

### Gaps
- No bracket-leg resolver: nothing maps the Perfect-Knockout neg-risk event's per-(team,round) advancement YES tokens to model survival probabilities. `resolve_outcome_token` only handles single-match winner/draw.
- No bracket optimizer: choosing the single highest-expected-score bracket requires the tournament-sim JOINT distribution; the subsystem only exposes marginal prices. `propose.py` sizes legs independently and cannot treat a bracket as one correlated basket.
- `pmhistory` advancement snapshots exist but at a single timestamp only — `convergence_inputs` needs ≥2 snaps/market, so the outright/advancement edge signal is inert until a recurring snapshot job runs.
- `build_pm_proposals` only sizes h2h from `build_card`; no sizing/EV producer for advancement/futures or for the existing match-event resolvers.
- `pm_inventory` table absent from live DB and `_process_event` duplicates `_yes_token_and_price` logic — the `/accas` combo/event-market pricing path is cold and at price-logic-drift risk.
- Partial duplication: `scripts/wca_pm_watch.py` keeps its own inline urllib `fetch_positions` instead of `pm.positions.fetch_positions`.
- `cashout` kill predicates cover exact-score/totals/BTTS only; advancement/team_win/futures classified but out of cash-out scope — no exit logic for bracket/futures positions.

### Reuse recommendations
1. **EXTEND `polymarket.py`:** add `resolve_advancement_tokens(team, stages, events)` alongside `resolve_outcome_token`, reusing `_yes_token_and_price` + the proven canonical-name matching. The missing bracket-leg resolver.
2. **REUSE `pmhistory` + `outrightedge.convergence`:** schedule a recurring snapshot (≥2 captures/market) so `convergence_inputs` goes live.
3. **WRAP `build_pm_proposals`, don't fork:** factor its PM-price re-sizing core (`kelly.stake` at PM price, tick-snap, hard_cap, EV recompute) into a helper and feed it advancement/event selections — one sizing implementation.
4. **ROUTE all bracket/event execution through `ClobTrader.place_order`:** guardrails + V2 signing already cover neg-risk markets (negRisk envelope flag). Do NOT build a second client.
5. **BUILD the bracket optimizer on the tournament-sim joint distribution (outside this subsystem);** consume these modules only for prices/sizing/execution. Keep market access and bracket combinatorics separate.
6. **EXTEND `cashout.classify_market`/`evaluate_position`** to add advancement/futures kill+exit (re-elect once a team is eliminated) reusing the pure-predicate pattern.
7. **FIX duplication:** migrate `wca_pm_watch.py` to import `positions.fetch_positions`; refactor `pm_inventory._process_event` to call `_yes_token_and_price`.

---

## 6. data-layer (odds/results/event ingestion, cleaning, reconciliation, team-name canon)

**Role in pipeline:** `src/wca/data/` — the ingestion + cleaning head feeding everything downstream.

**Data-flow role:** Two largely independent ingestion spines.
- **ODDS SPINE.** Two live entrypoints that DO NOT share a path. (1) The snapshot daemon (`wca_snapshotd.py`/`wca_snapshot_odds.py`) calls `theoddsapi.get_odds`/`get_event_odds` directly, flattens via `snapshot.rows_from_odds_frame`, persists with `snapshot.snapshot_all` into the append-only `odds_snapshots` table (1,262,293 rows, source=`theoddsapi` only, ts 2026-06-11..2026-06-23) — the real raw→clean store for closecapture/clvbench/intel/accas/news/tracking/linemove/venuesdata. (2) `wca_build_card.py` calls `odds_source.get_odds`/`get_event_odds` (priority betfair_exchange→theoddsapi→polymarket_odds, graceful degrade), feeding the `/card` render but NOT writing `odds_snapshots`. `theoddsapi.py` is the single shared CLIENT, but snapshot-WRITE and card-READ go through different wrappers. `betfair_exchange.py` is wired into both `odds_source` (priority-0) and `positions_sync.py`, but is effectively OFF in prod (no creds → empty frame); ingestion appears stalled (newest snapshot 2026-06-23, consistent with the revoked ODDS_API_KEY).
- **RESULTS SPINE.** `results.download_results` mirrors martj42 CSV → `wca_clean_results.py`: `fixture_sources.gather` (ESPN + TheSportsDB) → `reconcile.reconcile_date` (stages a correction only on two-source agreement) → `cleaning.merge_correction`/`save_corrections` → `cleaning.build_cleaned` emits `martj42_cleaned.csv`. Consumers read via `cleaning.resolve_results_path` → `models/elo.py` + `models/dixon_coles.py`.
- **PLAYER/PROP SPINE.** `statsbomb.py` (WC2018+2022 open-data) → `players_db.build_players_db` joins squads + analyst overrides into `players.db`, consumed only by `models/betbuilder.py`.
- `teamnames.canonical` is the cross-cutting glue: every external name mapped to martj42 spelling before any model/results lookup (~20 modules).

### Entrypoints
- `scripts/wca_snapshotd.py`/`wca_snapshot_odds.py` → `theoddsapi.get_odds` (`theoddsapi.py:96`) → `snapshot.rows_from_odds_frame` (`snapshot.py:174`) → `snapshot.snapshot_all` (`snapshot.py:124`) → `odds_snapshots`
- `scripts/wca_build_card.py` → `odds_source.get_odds` (`odds_source.py:80`) / `get_event_odds` (`:183`)
- `scripts/wca_clean_results.py` → `fixture_sources.gather` (`:175`) → `reconcile.reconcile_date` (`:54`) → `cleaning.merge_correction`/`build_cleaned` (`cleaning.py:85,:204`)
- `cleaning.resolve_results_path` (`cleaning.py:233`) → `models/elo.py` + `models/dixon_coles.py`
- `scripts/wca_build_players_db.py` → `players_db.build_players_db` (`:240`) → `statsbomb.build_props_dataset` (`:387`)
- `src/wca/positions_sync.py` → `betfair_exchange.list_current_orders`/`list_cleared_orders` (`:574,:659`) + `smarkets.list_open_positions`/`list_settled_positions` (`:139,:238`)
- `src/wca/arbfx.py`/`scripts/wca_arb_data.py` → `betfair.betfair_odds` (`betfair.py:32`) + `smarkets.smarkets_odds` (`smarkets.py:310`)

### Components

| File | Symbol | Line | Status | Role / Notes |
|------|--------|------|--------|--------------|
| `src/wca/data/theoddsapi.py` | `get_odds` / `get_event_odds` / `get_scores` | 96 | live | raw ingest — single shared Odds API client; powers snapshot write path + odds_source card read path + live `get_scores` for cashout. |
| `src/wca/data/snapshot.py` | `rows_from_odds_frame` / `snapshot_all` / `odds_snapshots` schema | 124 | live | raw→clean store — flattens + persists to the append-only `odds_snapshots` (1.26M rows) every downstream reader uses. Schema is a hard contract ("MUST NOT change"). |
| `src/wca/data/odds_source.py` | `get_odds` / `get_event_odds` | 80 | live | raw ingest orchestrator for `/card` only — priority betfair_exchange→theoddsapi→polymarket, graceful degrade; never raises. NOT the snapshot writer. |
| `src/wca/data/betfair_exchange.py` | `get_odds` / `list_current_orders` / `list_cleared_orders` | 374 | live | raw ingest + ledger truth — real Betfair JSON-RPC client wired into odds_source (priority-0) + positions_sync; currently dormant in prod (no creds → empty frame), 0 rows today. |
| `src/wca/data/betfair.py` | `betfair_odds` / `filter_betfair` | 32 | duplicated | thin Odds-API→Betfair filter used ONLY by `arbfx.py`/`wca_arb_data.py`; superseded for the main pipeline by the real `betfair_exchange.py` client. Same intent, two implementations. |
| `src/wca/data/smarkets.py` | `smarkets_odds` / `list_open_positions` / `list_settled_positions` | 310 | live | raw ingest (arb via arbfx) + ledger reconcile (positions_sync). Positions endpoint shapes explicitly INFERRED/untested; odds path a documented monitoring-grade downgrade. |
| `src/wca/data/results.py` | `download_results` / `load_results` / `add_outcome_column` | 31 | live | raw ingest of martj42 international-results CSV → historical training set for Elo/DC. |
| `src/wca/data/cleaning.py` | `build_cleaned` / `apply_corrections` / `resolve_results_path` | 204 | live | clean+corrections overlay — idempotent `corrections.json` overlay → `martj42_cleaned.csv`. `resolve_results_path` is the single read seam every results consumer uses. |
| `src/wca/data/reconcile.py` | `reconcile_date` | 54 | live | corrections gate — conservative two-source-agreement reconciliation (ESPN+TheSportsDB) auto-staging only consensus, else human review. |
| `src/wca/data/fixture_sources.py` | `gather` / `espn_results` / `thesportsdb_results` / `FixtureResult` | 175 | live | independent result feeds for reconciliation; keyless, defensive, returns canonical names. |
| `src/wca/data/teamnames.py` | `canonical` / `ALIASES` | 45 | live | cross-cutting canon — maps every external spelling to martj42 before any lookup; flagged in-code as the single most dangerous failure mode (silent default rating). Alias list small/manual. |
| `src/wca/data/statsbomb.py` | `build_props_dataset` / `match_props` / `player_shares` | 387 | live | raw ingest+features for props — StatsBomb open-data WC2018/2022 aggregation; upstream of players_db; only WC2018/2022 seasons hardcoded. |
| `src/wca/data/players_db.py` | `build_players_db` | 240 | live | feature store — joins StatsBomb rates + 2026 squads + analyst overrides into `players.db`; consumed only by `models/betbuilder.py`. Missing-history players flagged event_history=0/thin. |

### Bracket relevance
- **(A) Perfect Knockout.** The bracket is a pure model+market problem and the data layer supplies both inputs. `teamnames.canonical` is mandatory glue (PM renders e.g. 'Curacao'/'USA' differently from martj42; `ALIASES` covers several PM cases) and must wrap every PM label before model lookup. The results spine (results→`resolve_results_path`→Elo/DC) gives the strengths that drive per-tie advancement; `advancement.py` already builds canonicalized structures off `resolve_results_path` — the natural place to compute the joint bracket distribution. For PM PRICES of bracket/advancement legs, Polymarket is realized only through `polymarket_odds` (in odds_source's chain) and is NOT captured in `odds_snapshots`, so any PM-vs-model bracket compare must read PM live via `odds_source`/`polymarket_odds`, not the snapshot DB.
- **(B) Trading remaining KO + advancement/futures + match-event — strongly reusable.** KO h2h: `theoddsapi.get_odds` + `odds_source` produce the shared flat frame; the snapshot daemon captures closing lines for CLV. In-play: `theoddsapi.get_scores` (~30s) feeds `pm/cashout`. Advancement/futures: the reconciliation+cleaning spine keeps the training set trustworthy; `positions_sync` handles execution truth. Match-event: `statsbomb`→`players_db`→`betbuilder` is purpose-built; `theoddsapi.get_event_odds` already pulls per-event btts/player-prop markets.

### Gaps
- ODDS INGESTION STALLED: newest `odds_snapshots` ts is 2026-06-23 (today 2026-06-30); 100% of 1.26M rows source=`theoddsapi`; `betfair_exchange.py` docstring says ODDS_API_KEY was revoked. Live KO/bracket trading needs a working feed.
- TWO PARALLEL ODDS PATHS that can silently diverge: snapshot daemon writes via `theoddsapi` directly; `/card` reads via `odds_source`. The card can show Betfair/Polymarket prices never captured to `odds_snapshots` → CLV/closecapture only sees theoddsapi closes. A bracket/PM backtest relying on `odds_snapshots` is blind to PM/Betfair closes.
- POLYMARKET PRICE SERIES NOT PERSISTED: `polymarket_odds` participates in odds_source's live chain but is absent from `odds_snapshots`. No PM CLV/backtest history exists.
- `betfair.py` (thin filter) vs `betfair_exchange.py` (real client) duplicated intent; arbfx/wca_arb_data still use the thin one → two different "Betfair" prices possible.
- SMARKETS POSITIONS/SETTLED ENDPOINTS INFERRED/UNTESTED (`_normalise_smk_position`, `_normalise_smk_settled`, `list_settled_positions` URLs/keys best-effort). Ledger reconcile for Smarkets unverified against live v3 API.
- statsbomb seasons hardcoded to WC2018+WC2022 (`WC_SEASONS`); no 2026 in-tournament data and no club/continental augmentation → betbuilder props lean on thin/prior data for many 2026 squad members.
- `teamnames.ALIASES` small hand-maintained dict with no test that every 2026 PM/Betfair label resolves; an unmapped KO-stage team silently yields a default rating. No completeness check against the actual 2026 field.
- `reconcile.reconcile_date` requires EXACTLY two sources (raises otherwise); only ESPN+TheSportsDB wired; single-source KO results (obscure confederations) always route to manual review.

### Reuse recommendations
1. **Build the advancement/bracket distribution on the EXISTING results→cleaning→Elo/DC spine + `advancement.py`** (already canonicalized via `resolve_results_path` + `teamnames.canonical`). Extend `advancement.py` with a KO joint-probability function; do not re-load `results.csv` — call `cleaning.resolve_results_path`.
2. **Persist Polymarket (and Betfair) prices into `odds_snapshots`** via `snapshot.rows_from_odds_frame`/`snapshot_all` (additive, schema unchanged) and have the snapshot daemon also pull `polymarket_odds` via `odds_source` — closes the "PM has no captured series" gap.
3. **Route ALL knockout odds ingestion through `odds_source.get_odds`** (tested, graceful-degrade, multi-venue) and make the snapshot daemon use `odds_source` too so card and snapshot store cannot diverge by venue.
4. **For match-event/prop trading, extend `statsbomb`→`players_db`→`betbuilder`:** add `WC_SEASONS`/continental competition_ids in `statsbomb.py`; pull live per-event prop prices via `odds_source.get_event_odds`. Prefer extending `build_players_db`.
5. **Harden teamnames before live bracket trading:** add a startup completeness check that every team in the actual 2026 PM/Betfair field resolves through `canonical()`, and add missing aliases. Cheapest, highest-leverage fix.
6. **Reuse `positions_sync` + `betfair_exchange`/`smarkets` normalisers for execution-side P&L/CLV truth, but first validate the INFERRED smarkets endpoint shapes** against the live v3 API.
7. **Consolidate the duplicated Betfair path:** migrate `arbfx.py`/`wca_arb_data.py` off `data/betfair.py` onto `betfair_exchange.py`; retire `betfair.py`.
8. **Keep the conservative two-source reconcile gate, but generalize it to accept N sources** so a third feed can break ESPN/TheSportsDB ties for obscure-confederation knockouts.

---

## 7. ledger-analytics-rigor

**Role in pipeline:** Bets/CLV money ledger, prediction ledger, rigor/skill verdict battery, full-book CLV benchmark, model-vs-venue benchmark, card benchmark report.

**Data-flow role:** Tail of the pipeline (…recommendation→execution→ledger→CLV) plus an out-of-band evaluation layer that reads the ledger to grade the model. Four blocks: (1) **MONEY ledger** (`wca.ledger.store`) — the LIVE sink; all executions funnel into `record_bet()` (single canonicalising write); `set_closing_odds()` computes per-bet CLV; loop closed by `closecapture` + morning/audit scripts; `wca.ledger.reports` produces bankroll/exposure/CLV/calibration. (2) **PREDICTION ledger** (`wca.predledger`, `data/dev.db`) — a parallel PAPER book grading the model's whole opinion set; `flatten_card` → flat 1X2/scoreline/OU/BTTS/advancement rows; `settle` from results; `close` stamps fair-vs-fair CLV reading `wca.db` read-only. (3) **RIGOR battery** (`wca.rigor.build.build_rigor`) — verdict engine over money ledger (CLV gates G0–G3, ROI) + model_predictions_log.jsonl join (skill gates G4/G5), assembling a colour-coded VERDICT whose inviolable rule is "no green on CLV alone". (4) **BENCHMARKS** — read-only graders: `clvbench` (full-book fair-vs-fair CLV with label-shuffle placebo), `venuesbench` (model-vs-venue probability-distance ranking), `bench.report` (calibration + walk-forward CLV + realized ledger markdown). All evaluation modules deterministic/seeded/offline, DBs opened read-only+immutable.

### Entrypoints
- `src/wca/ledger/store.py:record_bet` (L162); `:set_closing_odds` (L780, CLV at L838); `:settle_cashout` (L515, 'cashed' into REALIZED_STATUSES L53)
- `src/wca/ledger/reports.py:clv_report` (L369), `calibration_report` (L445), `summary` (L545), `sportsbook_open_exposure_by_match` (L270)
- `src/wca/predledger/build.py:flatten_card` (L334); `_flatten_advancement` (L296)
- `src/wca/predledger/settle.py:settle_open` (L136); `src/wca/predledger/close.py:stamp_closes` (L55)
- `src/wca/rigor/build.py:build_rigor` (L315), `load_money_ledger` (L117), `load_full_book_from_jsonl` (L170)
- `src/wca/rigor/clv.py:clv_block` (L252), `n_eff_clusters` (L95), `sequential_clv_significant` (L186), `placebo_beat_rate` (L211)
- `src/wca/rigor/skill.py:skill_vs_market` (L66, G4), `calibration` (L138, G5)
- `src/wca/rigor/verdict.py:assemble_verdict` (L136), `segments_block` (L94, G7); `src/wca/rigor/stability.py:stability_block` (L34, G6)
- `src/wca/clvbench.py:build_benchmark` (L647), `build_legs` (L251), `placebo_beat_rate` (L331)
- `src/wca/venuesbench.py:rank_venues` (L463), `DISTANCE_METRICS` (L147), `lobo_consensus` (L232)
- `src/wca/bench/report.py:build_report` (L206), `render_markdown` (L247)

### Components

| File | Symbol | Line | Status | Role / Notes |
|------|--------|------|--------|--------------|
| `src/wca/ledger/store.py` | `record_bet` / `settle_bet` / `settle_cashout` / `set_closing_odds` | 162 | live | execution→ledger→CLV sink. Single live write path for every venue. `settle_cashout` adds PM cash-out into REALIZED_STATUSES; `set_closing_odds` closes CLV. Schema self-migrates. Lay/free-bet P&L in `settle_bet` (L403-409). Cash-out excluded from CLV/calibration (no closing line). |
| `src/wca/ledger/reports.py` | `clv_report` / `calibration_report` / `summary` / `sportsbook_open_exposure_by_match` | 369 | live | ledger analytics: bankroll curve, CLV aggregates, model-vs-market Brier calibration, per-source breakdown, open-exposure-by-fixture. `sportsbook_open_exposure_by_match` (L270) decomposes accas/bet-builders into per-fixture legs with is_result flags — directly reusable for hedge/advancement. |
| `src/wca/predledger/build.py` | `flatten_card` | 334 | live | raw card → flat prediction rows across 1X2/scoreline/ou/btts/advancement. Deterministic build_id/prediction_id (re-runs upsert). The paper whole-opinion book. advancement (reach-stage/winner) already modelled — key for futures/bracket. |
| `src/wca/predledger/settle.py` | `settle_open` / `_settle_1x2` / `_settle_ou` / `_settle_btts` | 136 | live | settles paper predictions from results JSON; per-market won/lost/push. Idempotent. advancement settlement explicitly stubbed (`outcome=None` at L162) until bracket plays out — known gap. |
| `src/wca/predledger/close.py` | `stamp_closes` | 55 | live | stamps fair-vs-fair CLV onto 1X2 predictions, reading `wca.db` strictly RO/immutable. 1X2-only; scoreline/ou/btts/advancement never get a close (coverage gap). |
| `src/wca/rigor/build.py` | `build_rigor` / `load_money_ledger` / `load_full_book_from_jsonl` | 315 | live | assembles `rigor.json` verdict from money ledger + jsonl book (+ optional dev.db). Gates G0–G7. Futures/acca classified by keyword (L44-62); futures excluded from CLV n_eff. dev.db skill source is a defensive fallback. |
| `src/wca/rigor/clv.py` | `clv_block` / `n_eff_clusters` / `sequential_clv_significant` / `placebo_beat_rate` | 252 | live | CLV gates G0–G3 + shared small-sample stats (Wilson, cluster-bootstrap effective N, sequential z, placebo null). Reused by venuesbench + rigor segment FDR. Highest-value reusable stats core; dependency-light, seeded. |
| `src/wca/rigor/skill.py` | `skill_vs_market` (G4) / `calibration` (G5) / `brier_skill` / `_t_sf` | 66 | live | outcome-anchored skill gates: paired per-fixture log-loss differential (G4), logistic IRLS calibration (G5). Self-contained Student-t survival fn. G5 needs N≥100 → insufficient at WC sample sizes (returns None). |
| `src/wca/rigor/verdict.py` | `assemble_verdict` / `segments_block` / `benjamini_hochberg` | 136 | live | G7 BH-FDR over segments + final VERDICT. Enforces "green requires (G1&G2&G3) AND (G4|G5) AND G6". Clean decision layer; reusable for any new market-family verdict. |
| `src/wca/rigor/stability.py` | `stability_block` | 34 | live | G6 persistence gate: structural-break CUSUM + OOS/IS ratio on the time-ordered CLV series. Generic time-series-of-edge gate. |
| `src/wca/clvbench.py` | `build_benchmark` / `build_legs` / `placebo_beat_rate` / `_brier_skill` | 647 | live | Module-C full-book fair-vs-fair CLV benchmark: every leg vs consensus close, within-build label-shuffle placebo, edge/odds buckets, placed-vs-passed, drift_beta. Overlaps `bench.report` walk_forward_clv + `predledger.close`, but most rigorous (placebo null, skew-aware). |
| `src/wca/venuesbench.py` | `rank_venues` / `DISTANCE_METRICS` / `lobo_consensus` / `fixture_block_bootstrap` | 463 | live | model-vs-venue probability-distance engine: ranks closest venue to model 1X2 with common-support, fixture-block bootstrap CIs, Friedman, P(rank1), leave-one-book-out. Reuses `rigor.clv` seed + `markets.devig`. Pure/seeded/offline. |
| `src/wca/bench/report.py` | `build_report` / `_calibration_section` / `_clv_section` / `_ledger_section` / `render_markdown` | 206 | duplicated | card benchmark markdown: calibration+ECE, walk-forward CLV by edge bucket, realized ledger ROI/CLV by market-family/venue. Overlaps `clvbench` + `reports.calibration_report`. Distinct value: by market_family/venue ledger breakdown. Three near-parallel CLV computations now exist; consolidation candidate. |
| `src/wca/bench/metrics.py` | `brier_1x2` / `wilson` / `reliability_bins` / `ece` / `trimmed_mean` | 17 | duplicated | local scoring helpers for bench.report. Docstring states these MIRROR `tracking.brier_1x2`, `winrate.wilson`, `rigor.clv.wilson`, `clvbench.trimmed_mean` — kept local for package isolation. Intentional, low risk, but `wilson` now reimplemented in 3 places. |

### Bracket relevance
HIGH and largely ready, with two clear extension points.
- **(A) Perfect Knockout.** The prediction ledger already models the exact objects a bracket needs — the 'advancement' market (reach-stage/winner) is a first-class row in `flatten_card` (`_flatten_advancement`, L296) carrying model_prob/market_devig/edge/ev; `rigor.build` already classifies futures/advancement (`_FUTURES_KW`, L44). The score-matrix→tournament-sim that produces advancement probs lives UPSTREAM; this layer is the correct place to (i) store per-team reach-Rx/champion probs as advancement predictions, (ii) grade them, (iii) compute EV vs PM. A bracket OPTIMIZER is NOT implemented here — it needs joint-path probs from the tournament-sim (this layer only consumes them). `rigor`'s honest-sample machinery (`n_eff_clusters` treating one tournament as ~1 cluster; futures_only→INSUFFICIENT) correctly warns single-resolution futures cannot self-validate.
- **(B) Trading remaining KO + advancement/futures + match-event — directly supported.** `record_bet`/`settle_bet`/`set_closing_odds` + `reports.summary`/`clv_report`/`calibration` give a working execution→ledger→CLV loop. `reports.sportsbook_open_exposure_by_match` (`decompose_multileg` + is_result leg flags) is purpose-built for cross-venue hedging (GBP sportsbook vs USD PM, currency-tagged). `venuesbench` tells you which venue prices a fixture closest to model. `clvbench`/`bench.report` validate whether KO 1X2 edges are real. Match-event markets already flattened/settled/Brier-scored in predledger + reports; only `predledger.close` is 1X2-only, leaving non-1X2 event markets without a fair-close CLV.

### Gaps
- No bracket optimizer here: choosing the optimal full KO bracket requires JOINT path/advancement probs from the tournament-sim, which this layer consumes but does not produce. The ledger can store and grade a chosen bracket but cannot select one.
- `predledger.settle` leaves advancement settlement stubbed (`outcome=None`, L162) — reach-stage/winner predictions stay 'open' until bracket results exist. A knockout settlement source (per-round advanced/eliminated) must be wired before advancement CLV/skill can be graded.
- `predledger.close.stamp_closes` is 1X2-only: scoreline/OU/BTTS/advancement never receive a fair closing line → non-1X2 and futures markets have no fair-vs-fair CLV exactly where you want to trade.
- `rigor` G5 needs N≥100 settled outcomes; G6 needs ≥8 ordered CLV points; at remaining-KO sample sizes most outcome-anchored gates return None. Verdicts sit at PROMISING/INSUFFICIENT; bracket/futures edges cannot be statistically confirmed within-tournament (by design).
- Three near-parallel CLV implementations (`ledger.reports` via `set_closing_odds`, `clvbench` fair-vs-fair, `bench.report` walk_forward_clv) and three Wilson/Brier reimplementations (`rigor.clv`, `clvbench`, `bench.metrics`) increase drift risk.
- Money-ledger CLV (`set_closing_odds`) is `odds_taken/closing_odds-1` and warns the closing-odds BASIS must stay consistent (fair de-vig vs raw quote); mixing bases across manual vs auto-captured rows silently biases CLV.
- `venuesbench` expects a panel of fresh per-venue 1X2 quotes on common support; PM advancement/futures are not 1X2 triples → cannot rank PM on those markets without a new binary-market distance abstraction.

### Reuse recommendations
1. **EXTEND predledger (build/settle/close), do not rewrite:** add an advancement settlement source (per-round advanced/eliminated JSON) to `settle_open`, and generalise `stamp_closes` to stamp PM fair closes for advancement/futures so those markets gain a CLV — unlocks the existing rigor/clvbench grading.
2. **REUSE `reports.sportsbook_open_exposure_by_match`** (`decompose_multileg=True`, is_result flags) as the backbone of a KO/advancement hedging + cross-venue arbitrage engine (GBP vs USD already currency-tagged).
3. **REUSE `rigor.clv.{n_eff_clusters, clv_block, sequential_clv_significant, placebo_beat_rate}` + `rigor.verdict.assemble_verdict`** as the verdict framework for any new market family; it is generic over a value series + cluster ids. n_eff correctly caps futures at ~1 (do not fight this).
4. **REUSE `venuesbench.rank_venues` for ENTRY-VENUE selection** on remaining KO 1X2 fixtures (integrates leave-one-book-out to avoid circularity). For PM advancement, add a binary-market distance metric to `DISTANCE_METRICS` rather than reimplementing ranking/bootstrap.
5. **BUILD the bracket optimizer as a NEW upstream module** emitting advancement/path predictions into `flatten_card`'s existing advancement schema, then let the existing settle/close/rigor/clvbench stack grade it. Use `clvbench`'s within-build label-shuffle placebo to verify bracket-pick CLV beats a skill-free baseline, not 0.5.
6. **REUSE `bench.report.render_markdown` + `_ledger_section`** (by market_family/venue) as the human-facing KO/futures scorecard; extend EDGE_BUCKETS/market mapping rather than a new reporter.
7. **CONSOLIDATE the three Wilson/Brier/CLV reimplementations opportunistically** (point `bench.metrics` + `clvbench` at `rigor.clv` helpers) only if you touch them.
8. **PRESERVE the read-only/immutable DB discipline** (`predledger.store._connect_write` refusing `wca.db` without `WCA_ALLOW_PROD_DB`; `rigor/_open_ro`; `close.py` immutable URIs); record live bracket bets only through `ledger.store.record_bet`, never a new direct INSERT.

---

## 8. intel-microstructure

**Role in pipeline:** Live market intelligence — feed/poller/arb/metrics/normalise/store + news, sitedata, sync, linemove, closecapture, mc/pnl publishing.

**Data-flow role:** A price/market overlay + publishing layer on top of `odds_snapshots`. `normalise.from_oddsapi_rows` is the production ingress (maps legacy `odds_snapshots` rows → `MarketSnapshot`, devigging each group). `feed.build_feed` assembles `market_intel.json`; `metrics.build_market_metrics` is the devig→EV→sizing node; `intel/arb.scan_market` is the live `/arb` scanner. `closecapture.capture_closes` is the execution→ledger→CLV terminus (devigs last pre-KO snapshot, stamps closing_odds + clv). `news.odds_context` pairs scoops with line movement. `linemove`/`sitedata`/`sync` publish to the site. `mc/pnl.build_risk_pnl` Monte-Carlos the open book into a P&L distribution.

### Entrypoints
- `src/wca/intel/feed.py:build_feed` (line 120) — dashboard feed via `scripts/wca_market_intel.py`
- `src/wca/intel/metrics.py:build_market_metrics` (line 120) — devig consensus + EV/Kelly
- `src/wca/intel/arb.py:scan_market` (line 464) + `format_arb_report` (line 524) — live `/arb` (`bot/app.py:2491,2565`)
- `src/wca/intel/normalise.py:from_oddsapi_rows` (line 97) — production ingress
- `src/wca/intel/store.py:latest_per_selection` (line 180); `append_snapshots` (line 105)
- `src/wca/intel/poller.py:plan_polls` (line 301) — pure planner, caller `wca_intel_collect.py` (dev.db default)
- `src/wca/closecapture.py:capture_closes` (line 458) / `capture_closes_db` (line 629)
- `src/wca/news.py:odds_context` (line 848) / `gather_items` (line 1237)
- `src/wca/linemove.py:build_linemove` (line 397) / `write_linemove` (line 493)
- `src/wca/sitedata.py:build_site_data` (line 471) / `live_pm_positions` (line 303)
- `src/wca/sync.py:push_site` (line 103); `src/wca/mc/pnl.py:build_risk_pnl` (line 525)

### Components

| File | Symbol | Line | Status | Role / Notes |
|------|--------|------|--------|--------------|
| `src/wca/intel/feed.py` | `build_feed` | 120 | live | Assembles `market_intel.json`: groups snapshots by fixture×market, keeps latest per (selection,venue), runs `build_market_metrics`, emits staleness + 1X2 history. Pure/network-free. DEFAULT_STALE_S=3600. Consumes `from_oddsapi_rows` output, not `market_snapshots`. |
| `src/wca/intel/metrics.py` | `build_market_metrics` | 120 | live | Cross-venue derived metrics per selection: best/worst/avg/median, spread, pct_improvement, dispersion, Shin consensus_prob, and (with model+bankroll) ev_vs_model + quarter-Kelly stake. Reuses `markets.devig.shin` + `markets.kelly`. consensus_probs None on partial books. |
| `src/wca/intel/arb.py` | `scan_market` | 464 | live | Cross-venue arb scanner (cross_book, back_lay, pm_book). Live `/arb` (`bot/app.py:2565`). Never executes — actionable gate forces indicative on staleness/unknown depth. Delegates all math to `wca.arb`/`wca.arbfx`. PM↔book leg (`scan_pm_book`, line 375) is the bracket-relevant primitive. |
| `src/wca/intel/normalise.py` | `from_oddsapi_rows` | 97 | live | The actual production ingress: maps legacy `odds_snapshots` rows → `MarketSnapshot`, devigging each (match,market,venue,ts) group. Both live read paths (feed + `/arb`) enter here. `_ODDSAPI_MARKET` (line 79) covers only h2h/totals/btts/h2h_lay — no outright/advancement. |
| `src/wca/intel/store.py` | `append_snapshots` | 105 | experimental | Append-only change-gated historical `market_snapshots` store (intended persistent raw→clean DB). `latest_per_selection` (180) IS reused live but only against an ephemeral :memory: db in `/arb`; the on-disk `market_snapshots` table is NOT in prod wca.db. |
| `src/wca/intel/store.py` | `append_metrics` | 152 | stale | Would persist `build_market_metrics` into `market_metrics`. No live/test caller; table never created in prod. Dead code — wire up or remove. |
| `src/wca/intel/poller.py` | `plan_polls` | 301 | experimental | Pure tiered budget-aware polling planner (cadence by mins-to-KO, sheds markets under credit pressure, pins moneyline). Only caller `wca_intel_collect.py` defaults to dev.db and persists from existing store; live fetch-on-cadence is a future phase. Well unit-tested, not in live loop. |
| `src/wca/closecapture.py` | `capture_closes` | 458 | live | Auto closing-line capture: at kickoff devigs last pre-KO h2h snapshot into consensus 1X2 and stamps closing_odds + clv onto open 1X2 bets. The execution→ledger→CLV terminus. 25/77 bets carry close+clv. Idempotent. `_pick_event` handles rematch disambiguation — reusable for KO rounds. |
| `src/wca/closecapture.py` | `selection_leg` | 162 | live | Maps a bet selection (team / Draw / 'Team Yes/No' PM share) to (leg, is_no) for `fair_closing_odds`. Shared with `mc/pnl.py`. Already understands PM Yes/No spellings — load-bearing for PM advancement CLV. |
| `src/wca/news.py` | `odds_context` | 848 | live | Pairs an injury/lineup scoop with the team's current best h2h line + line_movement verdict (fresh news + flat line = tradable). Live via `wca_newsd.py`. `team_line_movement` (969) reads `odds_snapshots` directly. |
| `src/wca/linemove.py` | `build_linemove` | 397 | live | Collapses `odds_snapshots` h2h firehose into per-event normalised consensus 1X2 series + optional model overlay for `site/linemove.json`. `resolve_model_probs` (372) merges card/scores/predictions. write guards against clobbering populated file with empty payload. |
| `src/wca/sitedata.py` | `build_site_data` | 471 | live | Builds `site/data.json`: ledger rollups (via `dashboard.gather_stats`) + live PM holdings (data-API) + parsed scoreline predictions. `live_pm_positions` (303) hits PM data-API for on-chain holdings absent from ledger — reusable to read a live bracket/advancement book. |
| `src/wca/sync.py` | `push_site` | 103 | live | Best-effort regenerate+git-push of site JSON after a bot write. Hard PYTEST guard; WCA_AUTOPUSH toggle. Pure plumbing. |
| `src/wca/mc/pnl.py` | `build_risk_pnl` | 525 | live | Monte-Carlo valuation of the OPEN book into a P&L distribution (VaR95/CVaR95/hard_floor, per-currency, per-team) for `risk_pnl.json`. Independent-draw v1 (cross-bet correlation ignored — a real gap for bracket correlation). Reuses `closecapture.selection_leg`/`fair_closing_odds` for p_win. |

### Bracket relevance
HIGH for trading remaining KO/advancement/futures; MEDIUM for a Perfect-Knockout optimizer.
- **(A) Perfect Knockout.** This is a price/market layer, not a combinatorial optimizer — no bracket-enumeration or joint-advancement engine here. Reusable: `metrics.consensus_probs`/`linemove` consensus give devigged per-match 1X2 inputs to seed a knockout-tree model; `closecapture.selection_leg` already parses 'Team Yes/No' PM advancement shares; `sitedata.live_pm_positions` can read the live PM bracket book on-chain. The bracket-EV optimizer (enumerate paths, value each against a single PM Perfect-Knockout payout) must be NEW code, fed by these probabilities.
- **(B) Trading KO + advancement/futures + match-event.** Strongly reusable. `intel/arb.scan_market` (esp. `scan_pm_book` PM↔book lock), `build_market_metrics` EV/Kelly overlay, `news.odds_context` news-vs-line signal, and `closecapture` CLV all work per-match TODAY against `odds_snapshots`. The binding gap is COVERAGE: `odds_snapshots` only holds h2h/totals/btts/h2h_lay (no outright/to-advance/correct-score/cards/corners), so advancement+futures+match-event markets have no relay feed; the registry/MARKET_TYPES + poller already enumerate ah/cs/corners/cards/player_prop, so the schema is ready but the ingest is not.

### Gaps
- `market_snapshots` / `market_metrics` tables do NOT exist in prod `data/wca.db` — the intel persistent historical store is disconnected; live feed + `/arb` run off `odds_snapshots` via `from_oddsapi_rows` + an ephemeral :memory: db. `store.append_metrics` is dead (no caller, no table).
- No outright / advancement (to-qualify, to-win-group) / futures / correct-score / match-event (cards/corners/SOT/player_prop) data in `odds_snapshots` — only h2h/totals/btts/h2h_lay. `registry.MARKET_TYPES` + poller windows enumerate these but `normalise._ODDSAPI_MARKET` + the actual capture never ingest them. Bracket/advancement/match-event trading is blind without new ingest.
- `poller.plan_polls` (the tiered budget-aware scheduler) is not in the live loop: its only caller defaults to dev.db and re-persists from the existing store rather than live-fetching on cadence.
- Relay-only liquidity: every venue except Polymarket has has_liquidity=False (OddsAPI relay), so `arb.scan_market` can never mark anything actionable=True. No direct Betfair/Smarkets order-book API wired — arb is a watch-list, not executable.
- `odds_snapshots` freshness ends 2026-06-23 (group stage) and news_items 2026-06-12 — the live relay/news daemons appear idle for the knockout window.
- `mc/pnl.py` models positions as INDEPENDENT binaries. A knockout bracket is heavily path-correlated (same team across rounds, mutually exclusive advancement); reusing `build_risk_pnl` for bracket risk would understate tail dependence.
- No Polymarket Perfect-Knockout payout/structure model anywhere — `sitedata` reads PM holdings and `intel.sources/polymarket` adapts PM quotes, but no module values a full-bracket entry or optimizes a slate against a single multi-round payout.

### Reuse recommendations
1. **EXTEND ingest, don't rewrite analytics:** add an outright/advancement/futures + match-event source adapter under `src/wca/intel/sources` + a normalise mapping so these markets flow into the SAME `from_oddsapi_rows`→`build_market_metrics`/`scan_market` path. `registry.MARKET_TYPES` + poller priorities already anticipate ah/cs/corners/cards/player_prop.
2. **For KO/advancement TRADING, reuse `arb.scan_pm_book` + `metrics.build_market_metrics` as-is:** feed them PM advancement-share quotes vs book to-qualify prices. Extend `selection_leg` to to-advance/to-win-group selections rather than a new parser.
3. **Finally PERSIST:** wire the existing `store.append_snapshots` + the dead `append_metrics` into a live collector targeting `wca.db` (currently dev.db only) so advancement/futures price history accrues for CLV and line-movement — closecapture/linemove/tracking then get history for free.
4. **Reuse `closecapture.capture_closes` + `_pick_event` for knockout CLV:** it already disambiguates rematches (group vs knockout) and handles PM moneyline shares — extend `_X12_MARKETS`/`fair_closing_odds` for advancement closes rather than a new CLV engine.
5. **For the BRACKET OPTIMIZER, build NEW combinatorial code but seed it from `metrics.consensus_probs`/`linemove` consensus 1X2** (already devigged, tested) as per-tie inputs; value the enumerated tree against the live PM book read via `sitedata.live_pm_positions`. Do not reimplement devig or PM quote conversion — call `markets.devig` and `wca.arb.pm_yes_to_decimal`.
6. **Reuse `news.odds_context` + `team_line_movement` unchanged** for knockout team-news edges; point its event_meta at knockout fixtures.
7. **Upgrade `mc/pnl.simulate_book` to support correlated draws** (shared team-advancement factor) before trusting it for bracket/futures risk; keep the settlement payoff schedule + distribution_stats which are correct and tested.

---

## Consolidated: Live Pipeline vs Dead / Duplicated Code

### The live pipeline (single happy path, head → tail)

```
results.download → cleaning.build_cleaned (martj42_cleaned.csv, corrections-gated)
   → card.fit_models (Elo + ordered-logit + Dixon-Coles + structural priors + venues host bonus)
   → odds_source.get_odds  [betfair_exchange→theoddsapi→polymarket]   (CARD READ path)
   → card.market_consensus (devig.shin + median)   [DEVIG]
   → card._iter_fixture_blends (0.10/0.30/0.60)     [BLEND]
   → card.build_card (best_price, kelly.edge, min_edge gate, per-pool kelly.stake)  [EV→SIZING]
   → apply_daily_exposure_caps → rank_card (longshot CUT)    [RECOMMENDATION]
   → cardcache.write_card + modelpreds.write_predictions (blended 1X2 + DC lambdas)

PARALLEL ODDS WRITE path (does NOT share the above):
   theoddsapi.get_odds → snapshot.snapshot_all → odds_snapshots (1.26M rows, theoddsapi-only)
   → closecapture.capture_closes (CLV) → ledger.store.set_closing_odds
   → intel.from_oddsapi_rows → intel.build_feed / intel.arb.scan_market (/arb) / linemove / news

ADVANCEMENT/SIM branch:
   advancement.make_prob_fn → sim.tournament2026.TournamentSimulator.simulate
   → advancement.compare_to_polymarket (fee-adjusted quarter-Kelly edges)
   → outrightedge.convergence (via pmhistory)

EXECUTION (PM): build_pm_proposals → ClobTrader.place_order (PM_DRY_RUN-gated)
   → positions.fetch_positions → ledger.store.record_bet → reports.* → rigor.build_rigor / clvbench / venuesbench / bench.report

EVENT/PROP tail (display-only): accas.build_accas + boosts.evaluate_boost (bot /accas, /boost)
   reference-only: card.build_event_references (corners/cards/BTTS, NEVER staked)
```

### Two structural facts that dominate everything

1. **The odds WRITE path and the card READ path are different wrappers over one client.** Snapshot daemon → `theoddsapi` direct; `/card` → `odds_source`. Consequence: `odds_snapshots` is 100% theoddsapi, contains only h2h/totals/btts/h2h_lay, has zero Betfair/Polymarket rows, and is **stalled at 2026-06-23** (revoked ODDS_API_KEY). CLV/backtest is blind to PM/Betfair closes and to every non-1X2 market. This is the single biggest blocker for live knockout/bracket trading.
2. **The forecasting joint distribution is computed but discarded.** `tournament2026._run_knockout` already simulates every per-sim match winner but returns only aggregate `reach[]`/`win[]`. Marginal probabilities cannot value a correlated all-or-nothing bracket. Exposing the per-sim winner matrix is the one primitive that unlocks the entire Perfect-Knockout build.

### Dead / stale (no live caller)

| File:Line | Symbol | Why |
|-----------|--------|-----|
| `structural.py:249` | `outright_divergence` | tests-only; not wired into any live report |
| `betbuilder.py:216` | `RateStore` | reads a `players.db` absent from disk; all means fall to priors |
| `scripts/wca_exposure_sizer.py:41` | `classify`/`main` | frozen to a past group-stage slate; no-ops on new fixtures |
| `intel/store.py:152` | `append_metrics` | no caller; `market_metrics` table never created in prod |

### Disconnected (wired to scripts but cold in prod DB/feed)

| File:Line | Symbol | Why |
|-----------|--------|-----|
| `betbuilder.py:477` | `fixture_betbuilder` | cron-only → cached markdown; never compared to live prices |
| `pm_inventory.py:207` | `refresh_pm_events` | `pm_inventory` table absent from live wca.db; `_process_event` duplicates `_yes_token_and_price` |
| `intel/store.py:105` | `append_snapshots` | on-disk `market_snapshots` table not in prod wca.db; only :memory: use is live |
| `intel/poller.py:301` | `plan_polls` | tested planner, not in the live fetch loop (dev.db caller only) |

### Duplicated logic (multiple sources of truth — drift risk)

| Logic | Canonical | Duplicate(s) |
|-------|-----------|--------------|
| Scorer intensity (npxg cap, pen add-on, minutes) | `props.py:223` `_intensity` | `scorers.py:77` `ScorerPricer.intensity` (byte-for-byte) |
| NB survival / fair-odds helpers | `props.py` `_nb_sf_over`/`_fair_odds` | `betbuilder.py:162` local copies |
| Host/altitude advantage | `venues.py:91` (`venues_2026.csv`) | `advancement.py:159` private `HOST_VENUE_ALTITUDE_M` |
| PM fee-adjusted edge + exchange commission | `arb.effective_back`/`pm_yes_to_decimal` | `wca_event_ev.py:43,96,155` (inline 0.94) |
| Betfair price source | `betfair_exchange.py:374` (real client) | `betfair.py:32` (thin Odds-API filter; arbfx/wca_arb_data) |
| Wilson / Brier / CLV stats | `rigor/clv.py:252` | `clvbench.py`, `bench/metrics.py:17` (3× wilson) |
| CLV computation | `ledger.set_closing_odds` | `clvbench.build_benchmark`, `bench.report` walk_forward_clv |
| Arb orchestration (3 families) | `intel/arb.py:464` (gated, live) | `arb.find_cross_book_arbs`/`find_pm_book_arbs` (scripts, ungated) |
| Exposure feed | `exposure.build_exposure_data:173` | `exposure_dashboard.py:19` (crude placeholder duplicate) |
| PM position fetch | `pm/positions.py:156` | `wca_pm_watch.py:40` (inline urllib) |

### Misclassified
- `venuesdata.py:270` belongs to **venues-benchmark**, not arbitrage; only shares `venues.canon_book`. Latent smell: `link_model_bets`/`rigor_clv_MIN` ordering (`:470` uses before `:477` defines).

---

## Single Prioritized List: Reuse Opportunities for the Bracket + Trading Build

Ranked by leverage (enables the most, lowest build cost first).

1. **[ENABLER — do first] Expose the per-sim winner matrix from `tournament2026._run_knockout` (`tournament2026.py:756`).** Add `simulate(..., return_paths=True) -> (n_sims, match_no→winner)`. The single missing primitive for the entire Perfect-Knockout build; reuses the already-tested vectorised `_play_ko`. Zero new modelling.

2. **[ENABLER] Add `match_win_probs(models, home, away)` in `card.py`** next to `elo_probs`/`dc_probs`, collapsing DC/Elo draw mass into an ET/penalty winner split → 2-way P(advance). Needed by both the sim path and any direct KO-tie pricing. Also expose+tune `et_skill_weight` (`tournament2026`, default 0.5) and pass it explicitly from `advancement.run_advancement`.

3. **[ENABLER] Restore the odds feed and persist PM + Betfair into `odds_snapshots`.** Re-key/restore the API and have the snapshot daemon pull via `odds_source` (so PM/Betfair are captured); additive use of `snapshot.rows_from_odds_frame`/`snapshot_all` (`snapshot.py:124/174`), schema unchanged. Unblocks CLV, backtest, line-move, and `outrightedge.convergence` for every market the bracket/trading build touches.

4. **[BRACKET CORE] Build the Perfect-Knockout optimizer as a NEW upstream module** over the path matrix from (1): bracket = dict of picks over `R32_TIES`+`KNOCKOUT_FEED` (`tournament2026.py:1000`); win-prob = mean over sims of all-picks-correct; seed with per-slot chalk then beam/greedy-swap to max EV vs PM payout. Reuse `thirds_assignment`+`THIRDS_ALLOCATION`. Keep it OUT of `card.py`.

5. **[BRACKET LEG ACCESS] Add `resolve_advancement_tokens(team, stages, events)` to `polymarket.py`** alongside `resolve_outcome_token` (`polymarket.py:215`), reusing `_yes_token_and_price` (`:155`) + the proven canonical-name matching. The missing PM bracket-leg resolver. Pair with `cashout.classify_market` (already labels advancement/team_win).

6. **[BRACKET SETTLEMENT/GRADING] Extend predledger end-to-end** (it already models advancement as a first-class row via `_flatten_advancement`, `build.py:296`): wire an advancement settlement source into `settle_open` (`settle.py:136`, currently stubbed `outcome=None`) and generalise `stamp_closes` (`close.py:55`, 1X2-only) to stamp PM fair closes for advancement/futures. Then the existing rigor/clvbench/bench stack grades the bracket for free; use `clvbench`'s label-shuffle placebo as the skill-free baseline.

7. **[BRACKET RISK] Reuse `exposure_corr.settle_on_scoreline`/`fixture_pnl_distribution` (`exposure_corr.py:408`)** as the settlement engine — generalise to `settle_on_bracket_path(bet, path)` (identical shared-state pattern), preserving tested convolution + 5%-cap machinery. Upgrade `mc/pnl.simulate_book` (`mc/pnl.py:525`) to correlated draws (shared team-advancement factor) before trusting it for bracket tail risk.

8. **[EXECUTION — reuse unchanged] Route all bracket/event execution through `ClobTrader.place_order` (`trader.py:932`).** Its guardrails (per-order/daily caps, `fifwc` allowlist, funder-safety, LiveOrderUnconfirmed) + V2 signing already cover neg-risk markets. Do NOT build a second client. Wrap (don't fork) `build_pm_proposals` (`propose.py:45`) — factor its PM-price re-sizing core into a helper fed advancement/event selections.

9. **[TRADING — remaining KO matches, reuse as-is] `intel/arb.scan_market` (`intel/arb.py:464`) + `metrics.build_market_metrics` (`metrics.py:120`) + `build_card`/`rank_card` for per-match KO 1X2/BTTS/totals/DNB.** For PM↔book to-advance pricing, feed `scan_pm_book`. Reuse `news.odds_context` (`news.py:848`) unchanged for KO team-news edges; `closecapture.capture_closes` (`:458`, rematch-aware `_pick_event`) for KO CLV; `venuesbench.rank_venues` (`venuesbench.py:463`) for entry-venue selection.

10. **[TRADING — match-event markets, flip reference→staked] Add an edge+Kelly gate onto `card.build_event_references` (`card.py:1286`)** which already produces calibrated, blend-reconciled corners/cards/BTTS off the live DC fit; extend `accas.candidate_prop_legs`/`candidate_scorer_legs` with per-fixture `CardsModel` aggression. Populate `players.db` to turn `betbuilder` counts from priors into real edges and wire `fixture_betbuilder` into the live EV path. Wire `ScorerPricer.double_delight_ev` (`scorers.py:147`) into `/boost`.

11. **[SETTLEMENT GUARD] Extend `arb.settlement_key` (`arb.py:40`) with POSITIVE ET-inclusive keys** (`advance_<stage>`, `win_tie_inc_pens`, `outright_winner`) so advancement markets can be paired with each other and against the sim (today it only refuses them). Reuse `arbfx.best_lock`'s hard same-event guard (`arbfx.py:221-251`) as the cross-settlement guard template.

12. **[FRAMEWORK reuse] Grade any new market family with `rigor.clv.{n_eff_clusters, clv_block, sequential_clv_significant, placebo_beat_rate}` + `verdict.assemble_verdict`** (`clv.py:252`, `verdict.py:136`) — generic over a value series + cluster ids; n_eff correctly caps single-tournament futures at ~1 (do not fight it). Use `reports.sportsbook_open_exposure_by_match` (`reports.py:270`) as the KO/advancement cross-venue hedging backbone (GBP/USD already currency-tagged).

13. **[HARDENING — cheap, high-leverage] Add a startup teamnames completeness check** that every team in the actual 2026 PM/Betfair field resolves through `teamnames.canonical` (`teamnames.py:45`); add missing `ALIASES`. The in-code "most dangerous failure mode" (silent default rating) directly threatens bracket leg resolution.

14. **[DEBT — do opportunistically when touching] De-duplicate before forking yet another copy:** point `ScorerPricer.intensity`→`props._intensity`; `betbuilder`→`props` helpers; `advancement` host logic→`venues.load_venues`; `wca_event_ev`→`arb` fee helpers; `wca_pm_watch`→`pm.positions.fetch_positions`; `pm_inventory._process_event`→`_yes_token_and_price`; consolidate the two exposure feeds and the 3× Wilson/CLV reimplementations.

**Hard guardrail to preserve throughout:** record live bracket bets ONLY through `ledger.store.record_bet` (`store.py:162`, the canonicalising choke point); preserve the read-only/immutable DB discipline (`predledger.store._connect_write` refusing `wca.db` without `WCA_ALLOW_PROD_DB`; `rigor/_open_ro`; `close.py` immutable URIs). Keep `offers.py`/`matched.py` isolated from the model/CLV ledger; reuse only if bracket entries are promo-funded.
