# 12 — Finalized, Implementation-Ready Action Items & Plan

**Mode = IMPLEMENT.** Trust the model but improve it first; do **not** take the
match-event book as fair — beat the *less-efficient* match-event markets with an
**independent model calibrated to history**. Where existing code is sub-optimal,
**replace** it, but never break a dependent: every switch ships with
backward-compatible APIs (new params default to legacy behaviour), hard
fallbacks, and updated/added tests, leaving the tree green (AGENTS.md §2.8).

**Corrected operating facts (load-bearing):**
- `ODDS_API_KEY` is **LIVE** (~96k quota, 49 books on next fixtures). Live odds
  incl. totals/match-event markets are data-ready via
  `src/wca/data/theoddsapi.py` (present, confirmed). Treat match-event markets as
  feed-LIVE for live pricing + CLV. The earlier "stalled/blocked feed" framing
  was wrong.
- World Cup is mid-Round-of-32 (2026-06-30). The **bracket/advancement contest is
  closed** and is NOT the focus. Focus = match-event model **quality**,
  **tradeability**, and **safe implementation**.
- The keystone defect is **statistically significant**: the model forecasts xG /
  total goals **too low** (bias ≈ **−0.66 goals/match**, paired t p≈0.049, 95% CI
  [−1.30,−0.03], n=31; doc 08 §2). This is a **level** miss, **not** the top-k
  truncation artifact the prior report blamed (τ-matrix ≈ λ-sum to 0.002).

**This file is the return value / build order.** Read alongside its sources:
`08_xg_and_totals.md` (xG/totals keystone), `09_match_event_models.md`
(corners/cards/fouls/SoT model designs), `10_data_validation.md` (matchevents
pipeline + market universe + validation), `13_cache_fix.md` (advancement-cache
fragility).

---

## 0. Sequencing logic (read before the table)

The dependency spine, in build order, is:

```
A1  Raise DC total level (mu shift)        [doc 08]  ── KEYSTONE: every λ/xG/total consumer moves
        │  (raw λ rises; reconciled 1X2 pinned by scores.reconcile_scoreline_matrix)
        ▼
A2  Wire totals/corners/cards into wca_event_ev EVENT_MARKETS  [doc 09 §8, doc 10 §3]
        │  (makes the corrected totals + event markets tradeable / CLV-loggable NOW)
        ▼
B1  Create src/wca/data/matchevents.py (unified loader + EB prior builder)  [doc 09 §6, doc 10 §1]
        │  ── UNLOCK: emits data/processed/prop_priors.csv (team corner/foul/card/SoT rates)
        ├──────────────┬──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
B2 CornersModel   B3 FoulsModel   B4 CardsModel   B5 ShotsOnTargetModel  [doc 09 §1-4]
   (EB priors +    (NEW, NB,        (refit: foul→     (NEW, shots×ratio)
    team-k fix)     feeds cards)     aggression+ref)
        │
        ▼
C1  Live CLV harness + pre-registration  [doc 10 §4.2]   ── decisive forward-test
C2  Cache fragility fix (advancement JSON + __setstate__)  [doc 13]  ── shared-core hygiene, parallelizable
```

**Why this order.** A1 (the `mu` shift) is the keystone: it raises *every* λ-derived
number (xG, totals, BTTS, team-goals, corners-via-`base_goals`, SoT-via-elasticity).
Building the event-model EB priors (B2–B5) on top of the *uncorrected* λ would
bake the −0.66 bias into the corner/SoT means and force a double-refit later
(doc 09 §9 names this explicitly: "build the prior pipeline to read λ *after* the
Track-A total correction, not before"). So **A1 ships first**, then A2 makes the
corrected surface tradeable, then `matchevents.py` (B1) is the **unlock** that the
four event models (B2–B5) all depend on for their team priors. C2 (cache fix) is
independent shared-core hygiene and can run in parallel any time.

---

## 1. RANKED ACTION ITEMS (value × readiness × safety)

Ranking = (trading value of the unlock) × (readiness: code/data on disk now) ×
(safety: how cleanly back-compat). Each ties to its source deliverable.

### A1 — KEYSTONE: raise the DC total-goals level anchor (`mu` shift) · source: **doc 08 §4–5**
- **Rank: 1.** Value HIGH (corrects the one significant, structural defect that
  feeds the entire totals/BTTS/team-goals/corners/SoT surface), readiness HIGH
  (single scalar, fitted file + fit method on disk), safety HIGH (1X2 pinned by
  reconciliation — verified end-to-end, doc 08 §5.1).
- **What is sub-optimal:** the level anchor is the fitted global intercept
  `mu=0.20438` (`src/wca/models/dixon_coles.py:629`, consumed `:682-683`
  `log_lh = self.mu + atk_h − dfc_a + γ`). `exp(mu)·2 = 2.45` baseline vs recent
  WC base rate **2.81** (FIFA WC since 2010, n=3,750) and realized WC2026 **3.00**.
  The slate-mean model total is **2.336** — ~0.4–0.5 below the recent base rate,
  0.66 below realized. Root cause: ridge shrinkage compresses dispersion; `mu` is
  a single intercept fit over a 49k-match corpus dominated by low-scoring
  defensive internationals; time-decay does not re-center the level.
- **The replacement:** add an opt-in level calibration. **Prefer (A):** a
  `level_target: Optional[float] = None` param to `DixonColesModel.fit`
  (`dixon_coles.py:451`) and/or a post-fit `recalibrate_level(target_total)`
  method that shifts `mu` by `Δ = log(target_total / model_slate_total)`.
  **Recommended target = 2.81** (recent-WC training mean) ⇒ `Δmu ≈ +0.185`,
  closes ~70% of the gap, **out-of-sample** (calibrates to history, not the
  31-match realized sample — avoids fitting the test set). Do **not** chase 3.00.
  Then regenerate `data/dc_params_corrected.json` (the deployed `mu`). Fallback
  (B): one-off rewrite of the params file `mu += 0.185` (zero `src/` change) — use
  only if code change is undesired; (A) is preferred for traceability.
- **Dependents to protect:**
  - *Reconciled-card surface (SAFE, auto-inherits, 1X2 pinned):*
    `scores.py:391 scoreline_card` (O/U, BTTS, correct-score, implied 1X2),
    `card.py` xG emit + O/U 2.5 + BTTS lines, `scripts/wca_event_ev.py` totals/
    alternate_totals/BTTS EV, `sitedata.py` (regex parse, no hard-coded values),
    `bot/app.py`, `predledger/*`. The firewall is
    `scores.reconcile_scoreline_matrix` (`scores.py:129`): it rescales each 1X2
    region to the blended target exactly, so **only totals/BTTS move; 1X2 is
    pinned** (verified: US-Paraguay, Over2.5 0.330→0.466, BTTS 0.399→0.532, 1X2
    unchanged to 3dp).
  - *Raw-λ surface (moves WITH the fix, intended):* `card.py:1336` →
    `CornersModel`, `scorers.py` first-scorer split, `betbuilder.team_total_goals`.
    Corners shift toward base (more correct — `base_goals=3.07` was calibrated for
    a higher λ regime). **Do NOT change `props.base_goals=3.07` or
    `betbuilder.BASE_TEAM_LAMBDA=1.35`** — the fix moves the input λ into the
    regime those anchors were built for (doc 08 §5.3).
- **The backward-compatible switch:** `level_target` defaults to `None` =
  byte-for-byte current behaviour; all existing fits are bit-identical unless
  opted in. Set the flag in `card.fit_models` (`card.py:521`/`605-610`) and
  regenerate the params file as the single deploy step. Supremacy invariant: the
  shift is a scalar add to `mu` only — `log(λ_h/λ_a)` is invariant, so raw 1X2
  *difference* and reconciled 1X2 are preserved by construction.
- **Tests to add/update:** (i) `fit(level_target=2.81)` ⇒ fitted-data mean total
  ≈ 2.81; (ii) `level_target=None` ⇒ identical params to current (regression
  lock); (iii) reconciled `scoreline_card` 1X2 unchanged after a `mu` shift (lock
  the §5.1 invariant); (iv) `expected_lambdas` log-ratio invariant to the shift.
  No existing test loads `dc_params_corrected.json` or asserts a specific total/
  `mu` (`grep -rl dc_params_corrected tests/` → empty), so the lift breaks **no**
  existing assertion. `tests/test_scores.py`, `tests/test_dixon_coles.py`,
  `tests/test_props.py`, `tests/test_card_events.py` are all SAFE (synthetic /
  relational / dynamic-from-defaults).
- **Rough effort:** S–M (1 method + 1 flag wire-through + params regen + 4 tests).

### A2 — wire corrected totals + event markets into the live EV path · source: **doc 09 §8, doc 10 §3**
- **Rank: 2.** Value HIGH (turns A1 into tradeable/CLV-loggable signal on the LIVE
  feed), readiness HIGH (feed live, markets confirmed in theoddsapi), safety HIGH
  (read-side string change).
- **What is sub-optimal:** `scripts/wca_event_ev.py:38`
  `EVENT_MARKETS = "btts,draw_no_bet,totals,alternate_totals"` — **omits**
  `totals_corners`, `team_totals_corners`, `alternate_totals_corners`,
  `totals_cards`, `team_totals_cards`, `team_totals`. The corrected totals surface
  (A1) and the event models (B2–B5) cannot be EV'd / CLV-logged until their book
  keys are pulled.
- **The replacement:** extend `EVENT_MARKETS` (and/or split into a base-bulk vs
  per-event prop list, since prop keys must route through `get_event_odds`,
  `theoddsapi.py:178`) to include `team_totals` first (cleanest expression of
  "total too low" per doc 10 §5: favourite-team-over), then corners/cards keys as
  B2/B4 land. Gate each new market behind a model availability check so a missing
  model never crashes the EV loop.
- **Dependents to protect:** the EV loop must tolerate a market key the model
  cannot price yet (skip-and-log, not raise). The 422-prone prop markets already
  have an event-odds path (`theoddsapi.py:178`); reuse it, don't add bulk keys
  that 422.
- **The backward-compatible switch:** added market keys are pure additions; an
  empty/absent model for a key ⇒ that market is skipped (no EV row), existing
  totals/BTTS/DNB rows unchanged.
- **Tests to add/update:** unit test that an unpriceable market key is skipped
  cleanly; test that `team_totals` EV rows appear when a model is supplied.
- **Rough effort:** S.

### B1 — UNLOCK: create `src/wca/data/matchevents.py` (unified loader + EB prior builder) · source: **doc 09 §6, doc 10 §1**
- **Rank: 3.** Value HIGH (single injection point for all four event models'
  team priors — the corner/card/foul/SoT edge lives in the EB priors, not the xG
  elasticity), readiness MEDIUM (StatsBomb 128 WC matches on disk; football-data
  CSVs NOT on disk — limits shot/corner/card backtest to n=128 until fetched),
  safety MAX (file does not exist ⇒ zero existing dependents, purely additive).
- **What is sub-optimal:** the pipeline is **missing** (`ls src/wca/data/` →
  confirmed absent). Today every event model hard-codes a single league-mean base
  with no per-team rates; the cross-fixture signal (high-corner vs low-corner
  teams, high-foul refs) is thrown away.
- **The replacement:** new additive module beside `statsbomb.py`:
  (1) football-data.co.uk column-map loader (doc 10 §1.2: `FTHG/FTAG`→goals,
  `HS/AS`→shots, `HST/AST`→sot, `HC/AC`→corners, `HF/AF`→fouls, `HY/AY`→yellows,
  `HR/AR`→reds; date `%d/%m/%y`+`%d/%m/%Y`); (2) StatsBomb reuse via
  `statsbomb.build_props_dataset()` (re-run so SoT is present — cached
  `props_matches.csv` lacks `sot_home/away`); (3) **unified schema**
  (one row/match, NaN-where-unavailable — no zero-filling, which would corrupt EB
  priors); (4) `build_prop_priors()` → `data/processed/prop_priors.csv` (NB-via-MoM
  + team-level EB shrinkage; schema `entity(GLOBAL|team), market, mean,
  dispersion_k, n_matches, shrinkage_weight`); (5) `load_priors(path=...)` with a
  **hard-coded fallback** returning today's `props.py`/`betbuilder.py` constants
  if the file is missing/malformed.
- **Dependents to protect:** none on creation (additive). The downstream switch
  (event models reading `prop_priors.csv`) is gated in B2–B5, not here.
- **The backward-compatible switch:** `load_priors()` returns hard-coded defaults
  on missing/malformed file ⇒ `card.py`, `betbuilder`, `accas`, `scorers` never
  crash on a missing artifact.
- **Tests to add/update:** loader maps football-data schema correctly; unified
  schema NaN-preserving; `build_prop_priors` reproduces known global moments
  (corners 8.969, yellows 3.352, fouls 28.523, shots 25.0, SoT 8.320,
  doc 10 §1.5); `load_priors` fallback returns defaults on missing file.
- **Rough effort:** M (new module, loader + builder + fallback + 4–5 tests).

### B2 — CornersModel: EB team priors + fix the team-dispersion bug · source: **doc 09 §2**
- **Rank: 4.** Value MEDIUM (corner *match-total* edge is weak — corners↔xG 0.315,
  near-Poisson match total; but the **team-corner tail** fix is a real, correct
  numeric improvement), readiness HIGH after B1, safety HIGH (optional params).
- **What is sub-optimal:** (a) `CornersModel.__init__` hard-codes
  `base_corners=8.97` for *every* fixture (`props.py:91`), so cross-fixture corner
  variance is driven only by the damped xG term (elasticity 0.30) — but the lever
  with signal is **team identity**, not xG. (b) **Bug:** team corners are priced
  with the *total* dispersion `k=157.5` (`props.py`) though team corners are
  overdispersed (var/mean 1.47, k≈9.5) — team-corner O/U tails are far too thin.
- **The replacement:** add optional `team_priors`, `league_team_mean=4.484`,
  `eb_tau=4.0`, and **`team_dispersion=9.5`**; add optional `home/away` (and
  `team/opponent`) names to `mean_total`/`team_mean`/`prob_team_over`. With names
  omitted ⇒ exact legacy 8.97 / k=157.5 path. With names ⇒ EB-blended corner-for/
  corner-against priors + light xG nudge, and **team O/U uses k=9.5**.
- **Dependents to protect:** `card.py:1331 cm = corners_model or CornersModel()`,
  `accas.py:545`, `betbuilder.py:374 team_corners`, `nextmatch.py:481`, bot,
  `sitedata.py` — all default-construct ⇒ unaffected until they pass names.
- **The backward-compatible switch:** two independent fallbacks — names omitted ⇒
  byte-for-byte legacy; empty `team_priors` ⇒ `_eb_rate` returns league mean.
- **Tests to add/update:** `CornersModel(team_priors={})` ≡ `CornersModel()` on a
  λ grid (match total); **the team-corner O/U snapshot is the one *intended*
  numeric change** (tails widen) — update, don't assert-equal, and call it out in
  the PR (doc 09 §7.4). Match-total corner numbers unchanged.
- **Rough effort:** M.

### B3 — FoulsModel (NEW, team NB, feeds cards) · source: **doc 09 §4**
- **Rank: 5.** Value MEDIUM (fouls markets are thin/soft; the *same* foul estimate
  double-duties as the CardsModel driver), readiness HIGH after B1, safety MAX
  (new model, no callers).
- **What is sub-optimal:** no fouls model; `betbuilder.team_total_fouls`
  (`betbuilder.py:361`) runs on a flat tuple prior with no team rates.
- **The replacement:** `FoulsModel` NB, `base_fouls=14.262`, `dispersion=20.4`
  (exact MoM), team EB priors from `prop_priors.csv`, `team_mean`/`prob_team_over`/
  `player_mean`. Becomes the source for CardsModel aggression (B4).
- **Dependents to protect:** none new; route `betbuilder.team_total_fouls` through
  `FoulsModel.team_mean` when team is known, keep `TEAM_PRIORS["fouls"]` tuple as
  fallback.
- **The backward-compatible switch:** new model, opt-in; flat prior remains the
  fallback when `team` is unknown.
- **Tests to add/update:** mean monotone, NB→Poisson as k→∞, `prob_over∈[0,1]`,
  thinning sums to team mean, unknown-team ⇒ league mean.
- **Rough effort:** S–M.

### B4 — CardsModel refit: foul→aggression + referee priors · source: **doc 09 §3**
- **Rank: 6.** Value MEDIUM (overdispersed k≈6.9 ⇒ tails mis-priced by Poisson
  books — doc 10 §5 ranks total cards as *partial* edge, but **only after** per-team
  aggression replaces the 1.0 default), readiness MEDIUM (needs B3 fouls + a
  referee table; ref defaults to 1.0 until a table lands), safety HIGH.
- **What is sub-optimal:** `CardsModel` multiplies `base_cards=3.41` by
  `aggression_home·aggression_away` that **both default to 1.0** (`props.py`), so
  every match prices at the base rate. Real signal: team fouls↔cards r=0.508; ref
  identity is the largest exogenous driver.
- **The replacement:** add `aggression_from_fouls(foul_rate, league_foul_mean=
  14.262, beta=0.5)` (sub-linear, β≈0.5 ≈ r²≈0.26) and an optional `ref_factor`
  (referee EB cards/match ÷ league mean; default 1.0). `mean_total` signature
  preserved; `ref_factor` new + optional.
- **Dependents to protect:** `card.py:1332`, `accas.py:546`, `betbuilder.py:490`,
  bot, `sitedata.py` — default-constructed, aggression=1.0, ref=1.0 ⇒ identical
  output. Opt-in by injecting foul-derived aggression + ref_factor.
- **The backward-compatible switch:** aggression and ref_factor default to 1.0 ⇒
  reproduce 3.41 exactly.
- **Tests to add/update:** `CardsModel()` ≡ legacy; `aggression_from_fouls`
  monotone and =1.0 on None/≤0; ref_factor scales linearly.
- **Rough effort:** S–M (depends on B3).

### B5 — ShotsOnTargetModel (NEW, team + player NB) · source: **doc 09 §1**
- **Rank: 7.** Value MEDIUM (player SoT is high-liquidity; shots↔xG 0.696 gives a
  clean xG link), readiness MEDIUM (SoT absent from current props pull —
  `on_target_ratio≈0.345` is an external prior until a StatsBomb SoT pull lands;
  no per-player rate store yet ⇒ doc 10 §5 rates player SoT "not edge yet"),
  safety MAX (new model).
- **What is sub-optimal:** no SoT model; `betbuilder.py:68` carries a bare
  `("sot",(4.2,9.0))` tuple with no attack scaling and no player layer.
- **The replacement:** `ShotsOnTargetModel`: team SoT = `shots_mean(λ) ×
  on_target_ratio` (shots scaled off xG, elasticity 0.6 = `betbuilder.
  SHOT_ELASTICITY`), player SoT = Poisson-thinning by player shot share,
  minutes-prorated. NB k≈6.0 team / 4.0 player.
- **Dependents to protect:** replace the `TEAM_PRIORS["sot"]` tuple path in
  `betbuilder` so SoT routes through the class; keep the tuple as the **fallback**
  when `lambda_team` is unavailable. Add `team_total_sot(...,model=None)` /
  `player_sot(...)` mirroring `team_total_shots` (`betbuilder.py:341`),
  `model = model or ShotsOnTargetModel()`.
- **The backward-compatible switch:** new model + `model or Default()` call sites;
  tuple fallback preserved.
- **Tests to add/update:** mean monotone in λ, NB→Poisson, thinning sums to team
  mean, `on_target_ratio` flagged for refit.
- **Rough effort:** M.

### C1 — live CLV harness + pre-registration · source: **doc 10 §4.2**
- **Rank: 8.** Value HIGH (the decisive out-of-sample test on the LIVE feed),
  readiness HIGH (feed live, `predledger/*` exists), safety HIGH (logging only —
  no trades). Sequenced after A1+A2 (needs the corrected, wired surface to log).
- **What is sub-optimal:** no pre-registered forward-CLV ledger for match-event
  legs; the only honest scoring path (no post-match event-stat feed) is CLV vs
  closing line.
- **The replacement:** pull `totals`/`h2h`/`btts` (bulk) + `team_totals`/
  `player_goal_scorer_anytime` (per-event) at T−24h/T−1h/close; log model-fair vs
  book; settle goal-markets on `wc2026_results.json`. CLV = log(odds taken /
  closing). Archive via existing `_archive_tee`. **Pre-register** the totals-fix
  `μ_total` value + market shortlist with a timestamp under
  `docs/research/wca_alpha_2026/data/` **before** the next kickoff (no curve-fit
  to seen results).
- **Dependents to protect:** logging only; never writes a trade.
- **The backward-compatible switch:** new harness, additive.
- **Tests to add/update:** CLV computation unit test (known odds → known log
  ratio); pre-registration file round-trips.
- **Hard caveat (encode it):** the ~31 remaining knockout matches are **one
  correlated tournament**, not 31 independent trials; per-match p-values overstate
  significance (n_eff ≈ 1–few). Report CLV/calibration **descriptively**; a single
  tournament can only fail-to-refute the historical backtest, never establish edge.
- **Rough effort:** M.

### C2 — fix advancement-cache fragility (pickle → versioned JSON + `__setstate__`) · source: **doc 13**
- **Rank: 9.** Value LOW-for-trading (advancement has no proven edge and the
  contest is closed) but MEDIUM-for-hygiene (the Elo/DC core is a **shared
  dependency** any match-event model reusing ratings sits on top of), readiness
  HIGH, safety HIGH. Parallelizable — independent of the A/B spine.
- **What is sub-optimal:** `data/advancement_models.pkl` was written by a stale
  worktree whose `EloRater` lacked `initial_ratings`; current `main`
  `get_rating` (`elo.py:218-222`) reads `self.initial_ratings`, so the un-pickled
  rater raises `AttributeError` on first use. `scripts/wca_advancement.py:50-79`
  swallows it in a broad `except` and silently **refits every run** (~2-min, cache
  = dead weight, no audit trail). Pickle couples on-disk format to live class
  schema — recurs on the next attribute change.
- **The replacement:** **(c)** versioned JSON of params
  (`data/advancement_models.json`, `schema_version`, reconstruct via the existing
  `to_dict`/`from_dict` — `EloRater` `elo.py:393-419`; `DixonColesModel` has both,
  `dixon_coles.py:745/768`, confirmed). A version mismatch ⇒ **explicit logged
  refit**, not a swallowed exception. Ship **(b)** a defensive `EloRater.
  __setstate__` backfilling `initial_ratings={}` as a one-line safety net (verified
  read-only: reconstruction yields Brazil=2062.03 over 336 teams).
- **Dependents to protect:** `scripts/wca_advancement.py` writer/reader;
  `advancement.py:236-250` Elo leg. Keep `.pkl` for one release writing **both**
  `.pkl`+`.json`, prefer `.json` on read, then drop `.pkl`.
- **The backward-compatible switch:** dual-write transition; `from_dict` `.get`
  defaults absorb added fields; `__setstate__` keeps in-flight pickles usable.
- **Tests to add/update:** JSON round-trip of `FittedModels`; version-mismatch ⇒
  raises/logs refit (not silent); `__setstate__` backfills missing attr.
- **Rough effort:** S–M. (Do **not** run `--refit` here — that regenerates the
  production cache and is out of the read-only scope; it's the operator's unblock.)

---

## 2. RANKED ALPHA HYPOTHESES — match-event markets (feed-LIVE)

Each: thesis · validation · failure mode. Priority follows doc 10 §5: the only
markets where an independent, history-calibrated model plausibly beats the
less-efficient book **right now** are the **goal-derived ones whose mispricing is
implied by the −0.66 total bias**.

**H1 — Favourite team-goals Over (e.g. Brazil Over 1.5). [HIGHEST CONVICTION]**
- *Thesis:* the −0.66 total bias is a systematic under-pricing of favourite-team
  overs; A1 directly raises the favourite team's λ. Cleanest expression of the
  whole thesis, clean half-int settlement (no push), LIVE (`team_totals`).
- *Validation:* 49,477-row walk-forward log-loss vs WC2022 closing line for team
  O/U; live CLV on `team_totals` for remaining knockouts (C1).
- *Failure mode:* WC2026's high scoring is a 31-match fluke, not structural — the
  bias reverses out-of-sample. Mitigation: anchor `μ_total` to history (2.81), not
  realized 3.00; let the live devigged totals line discipline the mean (doc 10
  Fix B).

**H2 — Clean sheet / opponent-scores-zero (mispriced by the same bias).**
- *Thesis:* if the total was too low, P(opponent=0) was too high ⇒ clean sheets
  over-priced; A1 lowers them correctly. Clean settlement, LIVE-derived from
  `btts`/`totals`/`h2h`.
- *Validation:* matrix P(opp=0) calibration on 49,477 rows + CLV.
- *Failure mode:* same fluke risk as H1; also sensitive to getting the *difference*
  (not just total) right — guard with the 1X2-pinned reconciliation.

**H3 — Match total Over 2.5 (the base market, directional).**
- *Thesis:* model P(Over 2.5) 0.405 → realized 0.452; A1 moves it toward truth ⇒
  long Overs / lay Unders. Deepest book.
- *Validation:* totals calibration intercept → 0 on held-out walk-forward; CLV vs
  closing totals line (the deepest, so hardest to beat — the honest bar).
- *Failure mode:* the totals book is the *most* efficient surface; edge may
  vanish at the close. Treat the live devigged total as the anchor, hunt edge in
  thinner derived markets (team totals, exact totals shape), not the base line.

**H4 — Total cards / booking points Over (tail mispricing). [PARTIAL — gated]**
- *Thesis:* cards are overdispersed (k≈6.9); books pricing Poisson under-price the
  O 5.5/6.5 tails. Our NB + foul→aggression + referee priors (B4) capture the
  team/matchup interaction additive book nudges miss.
- *Validation:* 128-match (then football-data-scale) tail calibration; CLV on
  `totals_cards`.
- *Failure mode:* aggression priors stay at 1.0 (no referee table / weak foul
  signal) ⇒ no differentiation from the book base rate. Do not size until B3+B4
  land with real per-team rates.

**H5 — Team-corner O/U tails (dispersion-bug correction). [WEAK]**
- *Thesis:* current k=157.5 makes team-corner tails far too thin (true k≈9.5);
  the B2 fix widens them correctly, exposing book lines that priced near-Poisson.
- *Validation:* team-corner O/U calibration on 128 matches; CLV on
  `team_totals_corners`.
- *Failure mode:* corners↔xG only 0.315, match total near-Poisson (var/mean
  1.057) ⇒ little independent fixture-level signal beyond the book base rate
  (doc 10 §5 verdict: corners are NOT durable edge). Trade only the tail, small.

**DO-NOT-TRADE (enumerated so they are not mistaken for alpha):** match-total
corners & corner race (corners↔goals ≈0.02, near-Poisson); player shots/SoT/
assists & multiscorer (order-of-magnitude priors, no per-player rate store,
participation-void over-states EV); penalty-awarded / goal-both-halves /
race-to-2 (no model and/or book-varying settlement); first-card team (aggression
defaults 1.0). Source: doc 10 §5.

**Binding settlement discipline (every hypothesis):** all goal-count markets are
**90-MINUTES ONLY** (ET + shootout excluded). Corners/cards ET-inclusion varies
by book (Trap 6) and player props are **VOID on non-appearance** (Trap 4) — encode
void-mixtures before any EV claim. Never mix an ET/pens advancement leg with a
90-min match-event leg (headline fake-arb, Trap 1). Source: doc 10 §3.

---

## 3. PR-SIZED, NON-OVERLAPPING TASK LIST (AGENTS.md-compliant)

One feature per task, each leaves the tree green (`pytest -q` in the worktree
before push, AGENTS.md §2.8). Ordered by the dependency spine. **File-overlap
note (AGENTS.md §2.7):** tasks touching the same hot file are serialized — flagged
inline. Branch names follow `conductor/claude-<slug>-<shortid>`.

| # | Task (one feature) | Primary files | Depends on | Overlap guard |
|---|---|---|---|---|
| T1 | Add opt-in `level_target` to `DixonColesModel.fit` + `recalibrate_level`; regen `dc_params_corrected.json` to mu+0.185 | `dixon_coles.py`, `card.py` (fit_models flag), `data/dc_params_corrected.json` | — | touches `card.py`: serialize vs T6/T7 |
| T2 | Extend `wca_event_ev` `EVENT_MARKETS` (+`team_totals`) with skip-on-unpriceable guard | `scripts/wca_event_ev.py` | T1 | sole owner of event_ev |
| T3 | Create `src/wca/data/matchevents.py` (loader + `build_prop_priors` + `load_priors` fallback) → `prop_priors.csv` | `src/wca/data/matchevents.py` (NEW) | — (B1 independent of T1) | new file, no overlap |
| T4 | CornersModel: EB team priors + `team_dispersion=9.5` fix (optional names) | `src/wca/models/props.py` | T3 | `props.py`: serialize vs T5 |
| T5 | CardsModel refit (`aggression_from_fouls`, `ref_factor`) + FoulsModel (NEW) | `src/wca/models/props.py` | T3 | `props.py`: serialize after T4 |
| T6 | ShotsOnTargetModel (NEW) + route `betbuilder` SoT through it (tuple fallback) | `src/wca/models/props.py` (or new), `betbuilder.py` | T3 | `props.py`/`betbuilder.py` after T4/T5 |
| T7 | Live CLV harness + pre-registration writer | `src/wca/predledger/*`, new script | T1,T2 | predledger owner |
| T8 | Advancement cache → versioned JSON + `EloRater.__setstate__` | `scripts/wca_advancement.py`, `elo.py` | — (parallel) | independent of A/B |

**Integration rule (AGENTS.md §2.6):** T4/T5/T6 all touch `props.py` — if dispatched
near-simultaneously, merge into ONE `integrate/match-event-models` branch, resolve
the `props.py` conflict once, get green, one PR. Never merge two parallel `props.py`
branches directly.

---

## 4. EXACT FIRST-FIVE CONDUCTOR PROMPTS (self-contained imperative text)

Plain imperative, one feature each, everything the isolated agent needs in the
prompt (AGENTS.md §2.3/2.5). Paste verbatim as `/task` bodies.

**Prompt 1 (T1 — the keystone):**
> Add an opt-in total-goals level calibration to the Dixon-Coles model and
> regenerate the deployed params. In `src/wca/models/dixon_coles.py`, add a
> `level_target: Optional[float] = None` parameter to `DixonColesModel.fit` and a
> `recalibrate_level(self, target_total: float)` method that shifts only `self.mu`
> by `delta = log(target_total / current_model_slate_total)`, leaving `attack`,
> `defence`, `rho`, `home_advantage` untouched (so `log(lambda_home/lambda_away)`
> and the 1X2 difference stay invariant). When `level_target is None`, behaviour
> must be byte-for-byte identical to today. In `src/wca/card.py` `fit_models`, add
> a flag to pass `level_target=2.81` through to the fit. Regenerate
> `data/dc_params_corrected.json` with the new `mu` (current 0.20438 → ~0.389;
> Δmu ≈ +0.185). Add tests: (a) `level_target=None` reproduces current params
> exactly; (b) `level_target=2.81` yields fitted-data mean total ≈ 2.81;
> (c) a `mu` shift leaves `scores.reconcile_scoreline_matrix`-derived 1X2
> unchanged to 3dp while Over 2.5 rises; (d) `expected_lambdas` log-ratio is
> invariant to the shift. Do NOT change `props.base_goals` or
> `betbuilder.BASE_TEAM_LAMBDA`. Run `pytest -q` and leave it green. Context:
> `docs/research/wca_alpha_2026/08_xg_and_totals.md`.

**Prompt 2 (T2 — make it tradeable):**
> Extend the live event-EV market list so the corrected totals and team-goals
> surface is priced against the live OddsApi feed. In `scripts/wca_event_ev.py`,
> add `team_totals` to `EVENT_MARKETS` (line 38) and add a guard so any market key
> the supplied model cannot price is skipped-and-logged rather than raising. Keep
> the existing `btts,draw_no_bet,totals,alternate_totals` rows unchanged. Route
> any per-event-only prop keys through the event-odds path
> (`src/wca/data/theoddsapi.py` `get_event_odds`, ~line 178), not the bulk path
> (bulk 422s on prop keys). Add tests: an unpriceable market key is skipped
> cleanly; `team_totals` EV rows appear when a model is supplied. Run `pytest -q`
> and leave it green. Context:
> `docs/research/wca_alpha_2026/10_data_validation.md` §3,
> `docs/research/wca_alpha_2026/09_match_event_models.md` §8.

**Prompt 3 (T3 — the unlock):**
> Create the additive historical match-event pipeline `src/wca/data/matchevents.py`
> (the file does not exist yet; it has no dependents). Implement: (1) a
> football-data.co.uk column-map loader mapping FTHG/FTAG→goals, HS/AS→shots,
> HST/AST→sot, HC/AC→corners, HF/AF→fouls, HY/AY→yellows, HR/AR→reds, parsing
> dates as both `%d/%m/%y` and `%d/%m/%Y`; (2) a StatsBomb path that reuses
> `src/wca/data/statsbomb.py` `build_props_dataset()` (re-run so shots-on-target
> is present — the cached `data/processed/props_matches.csv` lacks `sot_home`/
> `sot_away`); (3) a unified one-row-per-match schema with NaN where a field is
> unavailable (never zero-fill); (4) `build_prop_priors()` writing
> `data/processed/prop_priors.csv` with schema
> `entity(GLOBAL|team),market,mean,dispersion_k,n_matches,shrinkage_weight`, using
> negative-binomial method-of-moments + team-level empirical-Bayes shrinkage;
> (5) `load_priors(path="data/processed/prop_priors.csv")` that returns the current
> hard-coded `props.py`/`betbuilder.py` constants if the file is missing or
> malformed. Add tests: column-map correctness, NaN preservation, global moments
> reproduce (corners 8.969, yellows 3.352, fouls 28.523, shots 25.0, sot 8.320),
> and `load_priors` fallback returns defaults on a missing file. Run `pytest -q`
> and leave it green. Context:
> `docs/research/wca_alpha_2026/10_data_validation.md` §1,
> `docs/research/wca_alpha_2026/09_match_event_models.md` §6.

**Prompt 4 (T4 — CornersModel, depends on T3):**
> Replace the single-base CornersModel with empirical-Bayes team priors and fix
> the team-corner dispersion bug, backward-compatibly. In
> `src/wca/models/props.py` `CornersModel`, add optional constructor params
> `team_priors=None`, `league_team_mean=4.484`, `eb_tau=4.0`, and
> `team_dispersion=9.5`, and add optional `home/away` (and `team/opponent`) name
> arguments to `mean_total`, `team_mean`, and `prob_team_over`. When names are
> omitted, reproduce the exact current behaviour (`base_corners=8.97`, total
> dispersion `k=157.5`). When names are supplied, blend EB corner-for/
> corner-against priors with a light xG nudge, and price the TEAM corner O/U with
> `team_dispersion=9.5` (NOT 157.5 — this is the bug fix). Load priors via
> `src/wca/data/matchevents.py` `load_priors`; an empty/missing prior dict must
> fall back to the league mean. Add tests: `CornersModel(team_priors={})` equals
> `CornersModel()` on a lambda grid for the match total; and UPDATE (do not
> assert-equal) the team-corner O/U snapshot — the widened tails are the one
> intended numeric change; call this out in the PR description. Do not change the
> match-total corner numbers. Run `pytest -q` and leave it green. Context:
> `docs/research/wca_alpha_2026/09_match_event_models.md` §2.

**Prompt 5 (T8 — cache fix, parallel-safe):**
> Replace the fragile pickle advancement-model cache with a versioned JSON format
> and add a defensive un-pickler. The current `data/advancement_models.pkl` raises
> `AttributeError: 'EloRater' object has no attribute 'initial_ratings'` on first
> use (written by a stale checkout) and `scripts/wca_advancement.py` silently
> refits every run. In `scripts/wca_advancement.py`, write the fitted
> `FittedModels` to `data/advancement_models.json` with a `schema_version` and
> reconstruct via the existing `to_dict`/`from_dict` on `EloRater`
> (`src/wca/models/elo.py:393-419`), `EloOutcomeModel`, and `DixonColesModel`
> (`src/wca/models/dixon_coles.py:745,768`); a version mismatch must trigger an
> explicit, logged refit, not a swallowed exception. For one release, write BOTH
> `.pkl` and `.json` and prefer `.json` on read. Also add a defensive
> `EloRater.__setstate__` in `src/wca/models/elo.py` that backfills
> `initial_ratings={}` when absent. Do NOT run the script with `--refit` and do
> NOT regenerate the production cache — only change the code and add tests: JSON
> round-trip of `FittedModels`; version mismatch logs a refit instead of failing
> silently; `__setstate__` backfills the missing attribute. Run `pytest -q` and
> leave it green. Context: `docs/research/wca_alpha_2026/13_cache_fix.md`.

---

## 5. WHAT NEEDS THE USER (decisions / inputs only the user can make)

1. **Confirm the totals-anchor calibration choice (BLOCKING for T1).** The
   recommendation is `level_target = 2.81` (recent FIFA-WC-since-2010 training
   mean, n=3,750) ⇒ `Δmu ≈ +0.185`, closing ~70% of the −0.66 gap **out-of-sample**.
   Alternatives: 2.70 (betbuilder prior, `Δmu +0.145`) or 3.00 (realized WC2026,
   `Δmu +0.250` — **in-sample, over-fits the test set, not recommended**). A second
   defensible option (doc 10 Fix B) is to anchor `μ_total` per-fixture to the
   **live devigged totals line** instead of a fixed constant. **Confirm: fixed
   2.81, or live-totals-anchored?** Default to 2.81 if no response.
2. **Bankroll & sizing.** No size is set anywhere in scope. The plan logs CLV
   only (C1/T7) — no trades. To move from CLV-logging to sizing, the user must
   provide a bankroll figure and a staking rule (e.g. fractional-Kelly cap).
3. **Venues / books.** 49 books are on the feed. Confirm which books are
   **actually accessible/funded** for execution (and any per-book settlement
   quirks — corners/cards ET-inclusion, 2nd-yellow card convention, player-prop
   void rules) so settlement identities are encoded per the book actually used.
4. **football-data.co.uk fetch authorization (unblocks H4/H5 at scale).** The
   shot/corner/card backtest is stuck at n=128 (StatsBomb WC only) until the
   football-data club CSVs (HS/HST/HC/HF/HY/HR) are downloaded. Confirm whether to
   fetch them (network egress) — until then, do not promote corners/cards/SoT
   markets to live sizing.
5. **Pre-registration timing (C1).** Lock the `μ_total` value + market shortlist
   **before** the next knockout kickoff so the forward-test is not curve-fit to
   already-seen results. Needs a user go/no-go before the next match window.

---

## 6. Guardrails honoured

Read-only phase: no `src/` modified, no `.db` written, no trades, no `.env` reads.
SQLite (if opened) is read-only (`file:…/data/wca.db?mode=ro`). Only this NEW
research file under `docs/research/wca_alpha_2026/` was written. Numbers cited are
read-only from `data/` / source `file:line`. **No profitability is claimed** — the
sole quantified claim is that the model's total-goals **level** is significantly
and structurally too low (≈−0.66 goals/match, p≈0.049), and the keystone fix
removes that mis-anchoring without disturbing the 1X2 the rest of the book depends
on; everything downstream is a calibration/edge **hypothesis** to validate against
closing lines.
