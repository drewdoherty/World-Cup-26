# 10 — Historical Data Pipeline, Exotic Match-Event Markets & Validation

**Track C.** Design of (1) a new additive historical-data pipeline `src/wca/data/matchevents.py`, (2) an enumerated universe of *exotic* match-event markets with settlement identities and live-tradeability via the now-LIVE OddsAPI feed, (3) a validation protocol (historical backtest + live forward-test/CLV), and (4) a verdict on which markets plausibly carry edge vs the book and which do not.

**Operative steer (corrected).** TRUST the model but IMPROVE it first. Do **not** treat the match-event book as fair; the goal is to **beat the less-efficient match-event markets** with an **independent model calibrated to history**. The ODDS_API_KEY is **LIVE** (~96k quota, 49 books on next fixtures) — match-event markets incl. totals are **data-ready** for live pricing and CLV via `src/wca/data/theoddsapi.py`. Mode is **IMPLEMENT**: where existing code is sub-optimal, REPLACE it, but keep callers working (backward-compatible APIs + fallbacks + updated tests).

**Guardrail note.** This is a read-only design/spec doc. No `src/` edits, no `.db` writes, no trades, no `.env` reads were performed. All numbers below are computed read-only from `data/` via `.venv/bin/python`; scripts cited live under `docs/research/wca_alpha_2026/scripts/`. **No profitability is claimed anywhere.**

---

## 0. The core finding that motivates everything: the model's TOTAL is biased low

Re-running `scripts/xg_bias_repro.py` (Dixon-Coles refit on international history with cutoff `2026-06-10`, so no WC2026 leakage; output `data/xg_bias_repro.json`):

| Quantity | Value |
|---|---|
| Played WC2026 fixtures (n) | **31** (mid-R32) |
| Mean model-implied total goals (λ_home+λ_away) | **2.346** |
| Mean realized total goals | **3.00** |
| **Bias (model − realized)** | **−0.654 goals/match** |
| SD of per-match diff | 1.784 |
| t-stat / one-sided p | **−2.04 / 0.025** |
| Model mean P(Over 2.5) | 0.408 |
| Realized Over-2.5 rate | 0.452 (+0.043) |

**Interpretation, with the mechanism named.** The xG lambdas are derived by calibrating to the blended 1X2 via independent-Poisson/Skellam (`data/prop_calibration.json` `method`). **1X2 pins the goal _difference_, not the _total_** — the total rests on the `base_goals ≈ 3.07` assumption (`src/wca/models/props.py:83`, CornersModel docstring; matched by the StatsBomb WC18+22 sample where total goals mean = 3.07 and total xG mean = 3.057, computed read-only from `data/processed/props_matches.csv`). The DC fit on *full international history* (`data/raw/results.csv`, 49,477 matches 1872→2026-06-27) pins the *per-fixture* lambdas about 0.65 goals/match **below** what WC2026 is realizing. So two internally-consistent calibrations disagree: the **1X2-implied total ≈ 2.35** vs the **WC-base-rate total ≈ 3.07**.

This is **the single most important input to every match-event total market** (totals, BTTS, team O/U, exact totals, corners/cards via `base_goals`). A total biased ~0.65 goals low systematically **under-prices Overs and over-prices Unders / BTTS-No / low correct-scores** across the entire match-event surface. The improvement, the markets, and the validation below are all organized around correcting and exploiting this.

**Caveat (load-bearing, repeated throughout).** n=31 is **~1 tournament cluster**. WC2026 may simply be a high-scoring tournament (expanded 48-team field → weaker matchups → more goals is a plausible structural story, not noise). t=−2.04 at n=31 is *suggestive, not decisive*. The forward-test must treat this as one correlated observation, not 31 independent ones.

---

## 1. `src/wca/data/matchevents.py` — additive historical pipeline (DESIGN)

**Status:** file does **not** exist (`ls src/wca/data/` confirmed) → **no existing dependents** → purely additive; zero backward-compat risk on creation. It sits beside `statsbomb.py` and reuses its primitives.

### 1.1 What history is actually on disk (the real constraint)

| Source | Path | Rows | Fields available | Usable for |
|---|---|---|---|---|
| **martj42 internationals** | `data/raw/results.csv` (and `martj42_cleaned.csv`) | **49,477** (1872→2026-06-27) | date, teams, **scores**, tournament, city, country, neutral. **NO shots/corners/cards.** | **Goal-based** markets only: totals, BTTS, team-goals O/U, exact totals, margins, goal-both-halves(✗ no half data), clean sheet |
| **StatsBomb WC18+22** | `data/raw/statsbomb/` (128 event files cached) | **128** matches | corners, cards, fouls, shots, **SoT**, xG, goals, player shares (via `statsbomb.py`) | corners/cards/fouls/shots/SoT/scorer models — but only **128 matches, ~1.5 tournaments** |
| **football-data.co.uk CSVs** | **NOT present locally** | — | would carry HS/AS, HST/AST, HC/AC, HF/AF, HY/AY, HR/AR | **club-league** shots/corners/cards at scale (~50k+ rows) — *requires download* |

**The honest data-availability verdict:** the "50k historical matches" the brief references is the **goal-only** martj42 file. The rich HS/HST/HC/HF/HY columns the brief specifies a column-map for live in football-data.co.uk **club** CSVs that are **not yet on disk**. So `matchevents.py` must support both, but at design time only the goal-based markets get a 50k backtest; shot/corner/card markets get **128 WC matches** until the football-data CSVs are fetched. This is the dominant limitation of Track C and is flagged in every market verdict below.

### 1.2 Exact football-data.co.uk column map (per brief)

The loader maps the canonical football-data.co.uk schema (one row per match) to a unified schema:

| football-data col | Unified field | football-data col | Unified field |
|---|---|---|---|
| `FTHG` / `FTAG` | goals_home / goals_away | `HC` / `AC` | corners_home / corners_away |
| `HS` / `AS` | shots_home / shots_away | `HF` / `AF` | fouls_home / fouls_away |
| `HST` / `AST` | sot_home / sot_away | `HY` / `AY` | yellows_home / yellows_away |
| `Date` | date (parse `%d/%m/%y` and `%d/%m/%Y`) | `HR` / `AR` | reds_home / reds_away |
| `HomeTeam`/`AwayTeam` | home / away | `Div` | competition |

**Card convention (must match `statsbomb.py` docstring, lines 11–18):** football-data.co.uk's `HY`/`HR` already count a second-yellow→red as ONE red (the sending-off row). The unified schema therefore stores `yellows` and `reds` directly, with a derived `card_points = yellows + 2*reds` for book-style "booking points" markets (yellow=1, red/2nd-yellow=2 per the cards settlement convention in `05_market_universe.md`). No double-count.

### 1.3 StatsBomb internationals (reuse, don't reinvent)

`matchevents.py` does **not** re-implement event parsing. It calls `statsbomb.build_props_dataset()` (and `match_props` / `player_shares`) to produce the *same* match-level rows, then normalizes them into the **unified schema** so StatsBomb-WC and football-data rows are interchangeable downstream. The current cached `data/processed/props_matches.csv` (128 rows) **lacks `sot_home`/`sot_away`** (older build — confirmed: `KeyError: 'sot_home'`); the loader must re-run `build_props_dataset` or read the per-event cache so SoT is present (the SoT counts already exist in `match_props`, lines 218–225; see `data/sot_empirics.json` for the recomputed values).

### 1.4 Unified schema (one row per match)

```
match_id, source ("football-data"|"statsbomb"|"martj42"), competition, date,
home, away, neutral,
goals_home, goals_away,
shots_home, shots_away, sot_home, sot_away,        # NaN where unavailable (martj42)
corners_home, corners_away, fouls_home, fouls_away,
yellows_home, yellows_away, reds_home, reds_away,
xg_home, xg_away                                   # StatsBomb only; NaN elsewhere
```
NaN-where-unavailable is the key design choice: a goal-market backtest filters to `goals_*` non-null (49,477 rows); a corners backtest filters to `corners_*` non-null (128 rows today, +football-data once fetched). No silent zero-filling (which would corrupt the EB priors).

### 1.5 Empirical-Bayes priors → `data/processed/prop_priors.csv`

The pipeline's output artifact. For each countable market, fit a **negative-binomial via method-of-moments** on the unified table and a **team-level EB shrinkage** of per-team rates toward the global mean. Grounded global moments (computed read-only):

| Market (match total) | Mean | Var | Var/Mean | NB k (MoM) | Source n |
|---|---|---|---|---|---|
| Corners | 8.969 | 9.479 | **1.057** (near-Poisson) | ~158 | 128 (SB) |
| Yellows | 3.352 | 4.891 | 1.459 | ~6.9 | 128 |
| Reds | 0.062 | 0.059 | 0.945 | — (rare; use Poisson) | 128 |
| Fouls | 28.523 | 54.141 | 1.898 | — | 128 |
| Shots | 25.000 | 52.661 | 2.106 | ~22.6 | 128 |
| **SoT** | 8.320 | 14.613 | **1.756** | ~11.0 | 128 (`sot_empirics.json`) |
| Goals | 3.07 | — | — | (Poisson/DC) | 128 |

These **exactly reproduce** the hard-coded constants in `props.py` (corners base 8.97, k 157.5; cards base 3.41, k 6.9) and `betbuilder.py` `TEAM_PRIORS` (sot 4.2/team k 9.0, shots 12/team) — confirming the existing constants are honest MoM fits, **but on only 128 matches**. The EB layer's job is to (a) widen the support with football-data club data once fetched, and (b) emit **per-team** aggression/attack multipliers (the `aggression_home/away` and `npxg_share` slots that currently default to 1.0).

`prop_priors.csv` schema: `entity (GLOBAL|team), market, mean, dispersion_k, n_matches, shrinkage_weight`.

### 1.6 Backward-compatibility contract (IMPLEMENT-mode discipline)

Because `matchevents.py` is additive, the risk is **not** in creating it but in the *downstream switch* — if `props.py`/`betbuilder.py` are later refit to read `prop_priors.csv` instead of hard-coded constants. To keep dependents unbroken:

1. **Keep constructor defaults.** `CornersModel(base_corners=8.97, …)` etc. keep their literal defaults; the pipeline *injects* refit values as arguments. Callers that pass nothing are unchanged. (This is already the design intent — `props.py:4–7`.)
2. **Provide a loader with a hard fallback.** A new `matchevents.load_priors(path="data/processed/prop_priors.csv")` returns the hard-coded defaults if the file is missing/malformed, so `card.py:1753`, `betbuilder`, `accas`, `scorers` never crash on a missing artifact.
3. **The `base_goals` fix is the one behavioral change.** Raising the total (Section 2/3) changes `expected_goals()` consumers. Gate it behind a parameter (`total_target`/`base_goals`) defaulting to **current** behavior, ship the new value as opt-in, and only flip the default after the forward-test clears. Dependents to re-test on the flip: `src/wca/card.py` (xG emit l.1753), `scripts/wca_build_card.py`, `scripts/wca_event_ev.py`, `src/wca/accas.py`, `src/wca/bot/app.py`, `src/wca/sitedata.py`, `src/wca/predledger/*`, and the 973 tests in `tests/`.

---

## 2. The model IMPROVEMENT (do this before trusting the totals surface)

The mechanism (Section 0) says the defect is **total-level**, not difference-level. Two complementary fixes:

**Fix A — decouple the total from the 1X2-implied total.** Today the lambdas inherit whatever total the DC/Elo→1X2 reconciliation produces (≈2.35). Introduce an explicit **total-goals target** `μ_total` that the score matrix is scaled to *after* the 1X2 reconciliation pins the difference. Set `μ_total` from a blend of (i) the WC base rate 3.07 and (ii) a live-market-implied total devigged from the OddsAPI `totals` book (now available). This preserves the 1X2 marginals (difference) while correcting the total. Concretely: keep λ_home/λ_away ratio fixed, rescale both so λ_home+λ_away = μ_total.

**Fix B — calibrate `μ_total` to the live totals book, not to a fixed constant.** Because the feed is LIVE, the *fair* market total (devig of OddsAPI `totals` across 49 books) is the best available unbiased estimate of the total. Use it as the anchor and let the model's *edge* live in the **shape** (correct-score distribution, BTTS, team splits) and in **disagreement with mispriced match-event lines**, not in a blind total guess. This is the operative steer made concrete: we don't take the 1X2 line as fair on the total, but we *do* let the deepest book (the totals line) discipline the total while we hunt edge in the thinner derived markets.

**Why this is "improve then trust":** the −0.65 bias is in the part of the model (total) that is *least* constrained by the 1X2 data it was fit to. Once the total is anchored to either the WC base rate or the live totals line, the score-matrix-derived markets (BTTS, team O/U, exact totals, margins) become trustworthy enough to price against thinner book lines.

---

## 3. Exotic match-event market universe (enumerate · settlement identity · live tradeability · edge thesis)

**Tradeability legend.** **LIVE** = OddsAPI serves a matching market key now (per-event or bulk); **LIVE-derived** = priced from a LIVE base market (e.g. team totals from `totals`+`h2h`); **book-only** = sportsbook market that exists but is not in the standard OddsAPI key set / needs per-event pull to confirm; **no-feed** = no offered price anywhere we ingest.

OddsAPI market keys confirmed reachable via `theoddsapi.get_odds`/`get_event_odds` (`theoddsapi.py:99,178`): `h2h`, `totals`, `btts`, and per-event soccer keys (`alternate_totals`, `team_totals`, `spreads`, `draw_no_bet`, plus player-prop keys `player_goal_scorer_anytime`, `player_shots_on_target`, etc. — these return per-event and must be pulled via `get_event_odds`).

| # | Market | Settlement identity (the trap) | Model module | Tradeability | Edge thesis |
|---|---|---|---|---|---|
| 1 | **Team goals O/U** (e.g. Brazil over 1.5) | 90-min, that team's goals only. Half-int, no push. | `scores.py` matrix row/col sum; `betbuilder.team_total_goals` | **LIVE** (`team_totals` per-event) | **YES** — directly inherits the total-bias fix; favorite-team-over is the cleanest expression of "model total too low" |
| 2 | **Exact total goals** (exactly 2, exactly 3) | 90-min total = N exactly. ET excluded. | `scores.py` matrix anti-diagonal sums | **LIVE-derived** (from `totals` ladder devig) | **PARTIAL** — depends entirely on getting the *shape* right, not just the mean; thin |
| 3 | **Goal in both halves** | ≥1 goal in 1st AND ≥1 in 2nd, 90-min. | **NO model** (matrix is full-time only; needs half-split λ) | **book-only** | **NO (yet)** — no half-time intensity model; martj42 has no half data |
| 4 | **Race to 2 goals** (team first to 2) | First team to reach 2 goals in 90-min; "neither" if total<…; push rules vary. | derivable from score-path Poisson | **book-only / no-feed** | **NO** — settlement varies by book; no path model; do not price |
| 5 | **Multiscorer / 2+ goals player** | Player scores ≥2, 90-min. **VOID if player doesn't appear.** | `scorers.py` `ScorerPricer` (2+/3+) on `AnytimeScorerModel` | **LIVE** (`player_goal_scorer_anytime`; 2+ per-event) | **PARTIAL** — model ready, but participation-void + np-xG-share priors are weak |
| 6 | **Clean sheet** (team concedes 0) | Opponent scores 0 in 90-min. | `scores.py` matrix (P(opp=0)) | **LIVE-derived** (from `btts`/`totals`/`h2h`) | **YES** — direct function of the corrected total + difference; clean settlement |
| 7 | **Winning margin** (exact: win by 1, 2, 3+) | Exact 90-min margin bucket. Draw is its own bucket. | `scores.py` matrix diagonal-band sums | **book-only** (occasionally per-event) | **PARTIAL** — needs correct *shape*; the total-fix shifts mass into margin-2+ |
| 8 | **Penalty awarded in match** | A penalty KICK in 90(+ET varies) — **NOT the shootout.** | **NO model** | **book-only / no-feed** | **NO** — no incidence model; shootout-conflation trap (Trap 7, doc 05) |
| 9 | **Player shots / SoT O/U** | Player's 90-min shots/SoT. **VOID on non-appearance.** | `betbuilder.player_shots_on_target` (priors un-refit) | **LIVE** (`player_shots_on_target` per-event) | **NO (yet)** — order-of-magnitude priors only; no per-player rates; 128-match base |
| 10 | **Player assists** | Player credited an assist, 90-min. **VOID on non-appearance.** | **NO model** | **book-only** | **NO** — unmodelled entirely |
| 11 | **Corner race / first corner team** | Which team takes the next/first corner. | `props.CornersModel` team split (attack-share) | **book-only / no-feed** | **NO** — corners corr w/ xG only 0.15 (`props_matches.csv`); no edge signal |
| 12 | **First-card team / first booking** | Which team receives the first card, 90-min. | `CardsModel` × team aggression (defaults 1.0) | **book-only** | **NO** — aggression priors default 1.0 (no per-team foul rates yet) |
| 13 | **Total corners O/U** (baseline exotic) | 90-min corners awarded. ET varies by book (Trap 6). | `props.CornersModel` (NB k≈158, near-Poisson) | **book-only** | **NO** — near-Poisson + weak xG coupling ⇒ little independent edge vs book |
| 14 | **Total cards / booking points O/U** | 90-min; yellow=1/red=2; ET varies (Trap 6). | `props.CardsModel` (NB k≈6.9, overdispersed) | **book-only** | **PARTIAL** — overdispersed ⇒ tails mis-priced by Poisson books; but needs ref-driven aggression priors |

**Settlement-identity discipline (carried from `05_market_universe.md` §2, still binding):** every goal-count market (1,2,6,7) is **90-MINUTES ONLY** — ET and shootout excluded. The corners/cards ET-inclusion (Trap 6) and the player-prop participation-void (Trap 4) are real and must be encoded as void-mixtures before any EV claim. Mixing any ET/pens market (advancement) with a 90-min match-event leg remains the headline fake-arb (Trap 1).

---

## 4. Validation protocol

### 4.1 Historical backtest (the 50k claim, scoped honestly)

**Goal-based markets (totals, BTTS, team O/U, exact total, margin, clean sheet): n ≈ 49,477** from `data/raw/results.csv`.
- **Splits:** strict **time-based** walk-forward. Fit DC on all matches before date *t*, score the markets for matches in [t, t+window). No look-ahead. Anchor evaluation on **competitive internationals** (exclude `Friendly` = 18,388 rows; 31,089 competitive remain) for relevance to WC, but report both.
- **Metrics per market:** **log-loss** and **Brier** (binary markets: Over/Under, BTTS, clean sheet), **ranked-probability score** (ordinal: exact-total, margin), and a **reliability/calibration curve** (10 bins) + **calibration slope/intercept** (the totals fix should move the totals intercept toward 0). Report the realized Over-2.5 rate vs predicted as the headline calibration number (currently +0.043 on WC2026; target ~0 after the total-fix on held-out data).
- **Baseline to beat:** the **WC2022 closing 1X2 book** (`data/raw/wc2022_closing_odds.json`) devigged, and a flat base-rate model. An independent model only "beats the less-efficient match-event market" if it improves log-loss vs the *closing* line out-of-sample.

**Shot/corner/card markets: n = 128** (StatsBomb WC) **until football-data.co.uk CSVs are fetched.** Same metrics, but the brief's "backtest on 50k historical matches" is **not achievable for these markets on current disk** — it requires downloading the football-data club CSVs (HS/HST/HC/HF/HY/HR) the §1.2 map targets. Until then, treat all corner/card/shot validation as **128-match, single-context, low-power** and do not promote any of these markets to live sizing.

### 4.2 Live forward-test & CLV (the LIVE feed, the decisive test)

Because the OddsAPI key is LIVE (~96k quota, 49 books), the remaining knockout matches (R32 leftovers → Final, ~31 matches) are a **genuine out-of-sample forward test** with **closing-line value** as the primary metric:
- **CLV** = log(odds taken / closing odds) per leg, on markets 1, 2, 6 (LIVE / LIVE-derived). Positive mean CLV across legs is the **least-noisy** evidence the model beats the book — it does not require the bet to win, only to have been taken at a better price than the close.
- **Capture protocol:** pull `totals`, `h2h`, `btts` (bulk) + `team_totals`, `player_goal_scorer_anytime` (per-event via `get_event_odds`) at T−24h, T−1h, and close; log model-fair vs book; settle on `wc2026_results.json`. Archive via the existing `_archive_tee` so the snapshot trail is auditable.
- **Respect the ~1-cluster caveat (hard rule).** ~31 knockout matches are **one correlated tournament**, not 31 independent trials. Per-match p-values (the t=−2.04 above) **overstate** significance. The forward-test reports CLV and calibration **descriptively**, with the explicit caveat that a single tournament cannot establish edge; it can only *fail to refute* the historical backtest. Effective sample size n_eff ≈ 1–few, exactly as `outrightedge.py` already treats outrights.

### 4.3 Pre-registration

Lock the totals-fix value of `μ_total` (Section 2) and the market shortlist **before** the next batch of knockout kickoffs, so the forward-test is not curve-fit to results already seen. Store the pre-registered fair odds in `docs/research/wca_alpha_2026/data/` with a timestamp.

---

## 5. Which markets plausibly have edge vs the book — and which do not

**Plausible edge (priority order):**
1. **Team goals O/U (#1)** and **clean sheet (#6)** — direct, clean-settlement expressions of the corrected total. LIVE-priceable. The −0.65 total bias, if real, is *exactly* a systematic under-pricing of favorite-team-overs and over-pricing of clean sheets. **Highest-conviction, and the cleanest test of the whole thesis.**
2. **Match totals Over (the base market)** — not exotic, but the bias points straight at "back the Over / lay the Under." The edge is real *only if* WC2026's high scoring is structural (48-team field) rather than a 31-match fluke; the live totals-book anchor (Fix B) is what keeps this honest.
3. **Total cards / booking points (#14)** — books that price cards Poisson under-price the tails of an overdispersed (k≈6.9) count; a correctly-overdispersed NB has edge in the wings — *but* only after per-team aggression priors replace the 1.0 default.

**No durable edge (do not trade as independent alpha):**
- **Total corners (#13) & corner race (#11)** — corners correlate with xG at only **0.15** and with goals **0.02** (`props_matches.csv`); the count is **near-Poisson** (var/mean 1.057). There is almost no fixture-level signal for an independent model to exploit beyond the book's own base rate. Avoid.
- **Player shots/SoT/assists (#9, #10)** and **multiscorer (#5)** — priors are order-of-magnitude (`betbuilder.PLAYER_P90_PRIORS`), there is no per-player rate store on disk, and the participation-void makes flat model probs over-state EV. **Not edge until a per-player rate pipeline + void-mixture exists.**
- **Penalty-awarded (#8), goal-both-halves (#3), race-to-2 (#4)** — **no model at all** and/or settlement varies by book. Do not price.
- **First-card team (#12)** — gated on per-team aggression priors that currently default to 1.0; no signal until those are fit.

**One-line verdict.** The only markets where an *independent, history-calibrated* model plausibly beats the less-efficient match-event book **right now** are the **goal-derived ones whose mispricing is implied by the −0.65 total bias** (team totals, clean sheet, match Over) — and even those must clear (a) the totals-fix, (b) a live-CLV forward-test, and (c) the structural-vs-fluke question on the 48-team field, before any size. Corners, player props, and the rarer exotics are **not** edge on current data and current models.

---

## 6. Concrete next actions (IMPLEMENT-ready, ordered)

1. **Create `src/wca/data/matchevents.py`** (additive): football-data column-map loader (§1.2) + StatsBomb reuse + unified schema (§1.4) + `build_prop_priors()` → `data/processed/prop_priors.csv` + `load_priors()` with hard-coded fallback (§1.6). No dependents to break.
2. **Fetch the football-data.co.uk CSVs** (the missing piece) to unlock the 50k-scale corner/card/shot backtest; until then those markets stay at n=128 and stay off the live book.
3. **Add an opt-in `total_target` (μ_total)** to the DC→matrix path (`scores.py` / `card.py`), defaulting to current behavior; anchor it to the **live devigged totals line** (Fix B). Re-run the full `tests/` suite (973 tests) and the dependents in §1.6 before flipping the default.
4. **Stand up the live CLV harness** (§4.2) on `theoddsapi.get_odds`/`get_event_odds` for `totals`/`team_totals`/`btts`, pre-register fair odds, and forward-test on the remaining knockout matches — reported with the ~1-cluster caveat.
5. **Backtest goal-markets on the 49,477-row file** with time-based walk-forward vs the WC2022 closing line; promote a market to live sizing only if it improves out-of-sample log-loss vs the close.
