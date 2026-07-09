# World Cup Alpha — Analytics Design Document & Swarm-Ready Roadmap

**Project:** World Cup Alpha (WCA) · 2026 FIFA World Cup quant betting platform
**Branch context:** `feat/accas-rebuild` · **Author role:** Lead quant + product owner
**Status:** Design — single source of truth for the analytics rebuild
**Date:** 2026-06-24

> **Reading guide.** §1 sets the vision and the one structural bet. §2 specifies the prediction ledger (lands first). §3 specifies the four analytics modules. §4 is data architecture. §5 is the statistical-integrity contract — *read it before §3 if you grade any "edge is real" claim; it is the part that keeps us honest.* §6 is the swarm-ready PR roadmap. Two adversarial reviews (a statistics review and a codebase-completeness review) have been folded in throughout; the most consequential corrections are called out inline as **[corrected]**.

---

## 1. Vision & Principle

### 1.1 The full paper book, not just the bets

Today WCA tracks two disjoint things:

- **The realized book** — the `bets` table: only outcomes we staked real money on, with CLV auto-stamped for 1X2 markets.
- **The model book** — `model_predictions_log.jsonl`: 1X2 triples only, append-only, **never settled, never CLV'd**.

Every analytic we want to trust — calibration, Brier skill, "is the edge real" — is currently computed on the *placed-bet* subset. That subset is **selection-biased**: we only place a bet when the model's edge clears an EV threshold, so the sample is conditioned on "the model already thought this was good." It can tell us whether *the bets we took* worked; it structurally **cannot** tell us whether *the model* is good, because it never sees the fixtures we passed, faded, or sized to zero.

**The governing move of this entire design:** treat *every priced selection the model emits* — 1X2, scoreline, O/U, BTTS, advancement/futures — as a **paper bet**: persist it, settle it against the result regardless of whether money moved, and stamp CLV against the close. A nullable foreign key links a paper prediction to a real bet when one exists. The realized book becomes a strict *subset* of the paper book. This kills *prediction-selection* bias at the data layer, and every downstream analytic gets a `book ∈ {paper, realized}` toggle for free.

> **[corrected — the full book fixes only one of two selection biases].** Persisting every prediction removes "we only graded bets we placed." It does **not** remove **market-existence selection**: CLV is computable only where a closing line exists, and the markets with no liquid close (scoreline, thin advancement, many pre-tournament O/U lines) are exactly where the model is *most likely to claim edge and least likely to be checkable*. The CLV-graded subset of the paper book is therefore itself selected toward liquid, efficient markets where true edge is ≈0. **Consequence, wired in throughout:** CLV coverage (% of paper book with a non-NULL close, by market) is a first-class number beside every CLV aggregate; and **calibration/Brier against outcomes is the primary skill signal wherever CLV is structurally NULL** — the verdict must never read "no CLV edge" when the truth is "no CLV measurable." See §3.3-C10 and §5.

### 1.2 CLV is the primary, lower-variance edge signal

The project charter names CLV the primary KPI for a quantitative reason, not a stylistic one:

- **ROI's noise is outcome variance** — the irreducible coin-flip. For a real edge near 4% ROI at evens the per-bet SD is ≈1.0 (payoff ≈ ±1). **One-sided** 80%-power detection of ROI > 0 needs `n ≈ 6.18·(σ/δ)² ≈ 6.18/0.0016 ≈ 3,860` bets; the **two-sided** figure is `n ≈ 7.85/0.0016 ≈ 4,900`. We quote **≈3,860 (one-sided)** to match the one-sided gates used in §3.4.
- **CLV's noise is line-disagreement variance** — much smaller and observable *the instant the line closes, before the ball is kicked*. *If* mean CLV ≈ 3% with SD ≈ 0.06, then `n ≈ 6.18·(0.06/0.03)² ≈ 25` clustered bets.

That is a **~150× difference in sample need**, which is why CLV leads the verdict and ROI lags.

> **[corrected — the "≈25" is a conditional promise, not a measured fact].** (i) The 0.06 SD is **asserted, not measured** — `clv_odds` is right-skewed (§5.8), and a t-test on ~25 observations of a skewed quantity is exactly where the normal approximation is worst. The 25-bet figure is downgraded to *"≈25 **if** SD≈0.06 **and** using the sign test / cluster BCa rather than a t-test."* (ii) **Re-estimate σ from the observed book once n ≥ 15** and recompute the gate; never anchor the power gate to a guessed σ. (iii) Always report `n_eff` (cluster-adjusted), not raw `n` — with acca clustering and same-match leg correlation, 25 raw legs can be 8 effective. Label one-sided vs two-sided everywhere the numbers appear.

### 1.3 Honesty about sample size is a first-class feature

At 24 played group matches and single-digit settled accas, every aggregate is fat-tailed. The design bakes in:

- **Wilson intervals everywhere** — never a bare `p̂` at current N.
- **Cluster-robust standard errors** — the *match* (or acca, or for futures the *tournament*) is the unit, never the leg.
- **A verdict whose default is `INSUFFICIENT SAMPLE`** — greyed, with the threshold shown, until power gates are met.
- **Multiple-testing, survivorship, look-ahead, peeking, and non-stationarity guards** stated centrally (§5) and wired into the verdict, not buried as caveats.
- **Reproducing existing humility** — the card backtest already found the fitted blend does *not* beat the de-vigged market (Δ = −0.0031 nats, 95% CI [−0.0224, +0.0155]). The new estimators must reproduce that result, not paper over it.

---

## 2. Foundation: The Prediction Ledger (lands FIRST)

Everything in §3–§5 reads from this. **No other module ships until the prediction ledger exists.**

### 2.1 Core idea

At every card build, persist *every priced selection across every market* as a row in a new `predictions` table in `wca.db` (same database as `bets`). Settle each row against results regardless of whether money moved. Stamp CLV against the close, reusing the exact `closecapture` machinery. Link to a real bet via nullable FK.

> Every prediction is a paper bet. A subset are real bets. The link is `predictions.bet_id`.

**Result sources — [corrected paths]:** 1X2 / scoreline / O-U / BTTS settle against **`data/processed/wc2026_results.json`** (the bare `wc2026_results.json` path used in the draft exists nowhere). Advancement / futures settle against **`data/advancement_played_results.json`** (a separate source). Both are named explicitly in every task that touches them; the CLI exposes `--results` (default `data/processed/wc2026_results.json`) and `--advancement-results` (default `data/advancement_played_results.json`), mirroring `scripts/wca_tracking_data.py` and `scripts/wca_advancement_data.py`.

### 2.2 Schema — `predictions` + `acca_legs` (in `wca.db`)

> **[corrected — the acca hole].** The draft asserted "cluster = acca id" and an acca strike-rate on the MODEL book, but the schema had **no acca representation** (the `market` enum had no `acca`, and accas live only in `bets` via `_decompose_legs`). Resolution: the paper book carries **single-selection predictions only** in `predictions`; **paper accas are materialized in a companion `acca_legs` table** that references existing `predictions` rows as legs. This keeps the leg cross-section (§3.2 W6) and acca clustering (§3.4) backed by real data, and keeps `predictions` rows atomic (one selection each) so CLV/settle stay simple.

```sql
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id   TEXT PRIMARY KEY,            -- sha1(build_id|match_id|stage|market|selection|line)[:16]
    build_id        TEXT NOT NULL,               -- one card build = one batch (ISO ts)
    ts_utc          TEXT NOT NULL,

    match_id        TEXT,                         -- NULL for futures
    fixture         TEXT,
    kickoff_utc     TEXT,
    market          TEXT NOT NULL,                -- '1x2','scoreline','ou_2.5','btts','advancement'
    selection       TEXT NOT NULL,                -- 'home'|'draw'|'away'|'2-1'|'over'|'under'|'yes'|'no'|'<Team> R16'
    line            REAL    NOT NULL DEFAULT -1,  -- O/U goal line; -1 sentinel where N/A  [corrected: NOT NULL sentinel]
    stage           TEXT    NOT NULL DEFAULT '',  -- futures only: 'R32'..'F','win','group_winner'; '' sentinel  [corrected]
    n_outcomes      INTEGER NOT NULL,             -- 3 (1x2), 2 (ou/btts), K (scoreline)  [corrected: never pool across]

    model_prob        REAL NOT NULL,
    model_fair_odds   REAL NOT NULL,              -- 1/model_prob
    elo_prob          REAL,                        -- 1x2 diagnostics only
    dc_prob           REAL,

    market_devig_prob REAL,                        -- de-vigged fair market prob at build (NULL where no market line)
    market_best_odds  REAL,
    market_book       TEXT,
    devig_method      TEXT,                        -- 'proportional'|'shin'|... recorded per row  [corrected: + n_outcomes]
    edge              REAL,                        -- model_prob - market_devig_prob (NULL where no market line)
    ev_per_unit       REAL,                        -- model_prob*market_best_odds - 1 (NULL where no market line)

    bet_id            INTEGER,                     -- FK -> bets.id; NULL = paper-only
    placed            INTEGER NOT NULL DEFAULT 0,

    closing_devig_prob REAL,                        -- de-vigged consensus at kickoff
    closing_odds       REAL,                        -- 1/closing_devig_prob
    clv                REAL,                        -- (model_fair_odds / closing_odds) - 1; NULL (not 0) where no close
    close_ts           TEXT,
    close_lag_seconds  INTEGER,                     -- kickoff - close_ts; flags stale/leaky closes  [corrected: A3]
    n_books_at_close   INTEGER,                     -- consensus breadth at close  [corrected: A3/C11]
    close_is_prematch  INTEGER,                     -- 1 only if verified pre-KO by status, not just ts  [corrected: A3]

    status          TEXT NOT NULL DEFAULT 'open',   -- open|won|lost|push|void
    settled_ts      TEXT,
    settle_source   TEXT,                            -- 'results_json'|'advancement_json'|'manual'|'sim_final'

    model_source    TEXT,                            -- 'card_build'|'backfill'|'from_bet'
    notes           TEXT,
    FOREIGN KEY (bet_id) REFERENCES bets(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pred_natural
    ON predictions(build_id, match_id, market, selection, line, stage);  -- advisory; PK is the real guard
CREATE INDEX IF NOT EXISTS idx_pred_build  ON predictions(build_id);
CREATE INDEX IF NOT EXISTS idx_pred_match  ON predictions(match_id);
CREATE INDEX IF NOT EXISTS idx_pred_market ON predictions(market, selection);
CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);

-- Paper accas: an acca is a set of predictions rows acting as legs.  [corrected: closes the acca hole]
CREATE TABLE IF NOT EXISTS acca_legs (
    acca_id        TEXT NOT NULL,                  -- synthetic; sha1(build_id|sorted(prediction_id...))[:16]
    prediction_id  TEXT NOT NULL,                  -- FK -> predictions.prediction_id
    build_id       TEXT NOT NULL,
    bet_id         INTEGER,                         -- FK -> bets.id when this acca was actually staked; NULL = paper
    PRIMARY KEY (acca_id, prediction_id),
    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id),
    FOREIGN KEY (bet_id) REFERENCES bets(id)
);
CREATE INDEX IF NOT EXISTS idx_acca_id  ON acca_legs(acca_id);
CREATE INDEX IF NOT EXISTS idx_acca_bet ON acca_legs(bet_id);
```

**[corrected — idempotency rests on the PK, not the unique index].** SQLite treats NULLs as distinct in unique indexes, so two `line=NULL, stage=NULL` rows for the same build/match/market/selection would *not* be deduped by `ux_pred_natural`. We therefore store **sentinels** (`line = -1`, `stage = ''`) so the unique index actually dedupes, **and** rely on the deterministic `prediction_id` PK as the real idempotency guarantee. A test asserts "two upserts of the same NULL-line 1X2 row → one row."

**Row fan-out per fixture per build:** 1X2 → 3 rows; scoreline → top-K (≈6); O/U → 6 (1.5/2.5/3.5 × over/under); BTTS → 2; advancement → per-team-per-stage (`match_id` NULL, `stage` set). The `build_id` in the natural key means re-pricing a fixture in a later build yields a *new* prediction — we want a **time series of predictions**, not overwrites.

**Why one DB table, not JSON:** settle/close are UPDATE-heavy, keyed, idempotent operations — JSONL forces full-file rewrites; the FK to `bets.id` is the unifying join; `odds_snapshots` already lives in `wca.db` so the close path is a same-DB join.

### 2.3 Unifying views

```sql
CREATE VIEW IF NOT EXISTS v_model_book AS
SELECT p.*, b.stake, b.decimal_odds AS bet_odds, b.settled_pl, b.clv AS bet_clv,
       CASE WHEN p.bet_id IS NULL THEN 'paper' ELSE 'realized' END AS book
FROM predictions p LEFT JOIN bets b ON b.id = p.bet_id;

CREATE VIEW IF NOT EXISTS v_realized_book AS
SELECT * FROM v_model_book WHERE book = 'realized';
```

These two views are the API every downstream analytic consumes. Brier/calibration render on `paper` (full, unbiased) or `realized`; CLV-vs-P&L only has meaning on `realized`.

### 2.4 Module placement

```
src/wca/predledger/
    __init__.py
    store.py     # DDL bootstrap (ensure_schema), upsert_predictions(), upsert_acca(),
                 #   settle_prediction(), set_prediction_close(), link_bet(), query helpers
                 #   Uses wca.ledger.store._connect; sets PRAGMA busy_timeout per-connection here
    build.py     # flatten_card(recs, score_cards, advancement_df, now) -> (List[PredictionRow], List[AccaLeg])
    settle.py    # settle pass over both result files — all markets
    close.py     # CLV pass — wraps closecapture.consensus_close (+ new consensus_close_twoway)
    backfill.py  # reconstruct 1X2 history from model_predictions_log.jsonl + results
scripts/
    wca_predledger.py  # CLI: build | settle | close | backfill | report | publish
```

**Reuse, do not fork:** `wca.ledger.store._connect`, `wca.closecapture.consensus_close`/`fair_closing_odds`/`selection_leg`, `wca.tracking.devig_consensus`, `wca.data.teamnames.canonical`.

> **[corrected — `set_closing_odds` is NOT reusable for predictions].** `wca.ledger.store.set_closing_odds(bet_id, …)` writes the **`bets`** table only. `predledger.store.set_prediction_close` is a **new, parallel writer** that mirrors the *arithmetic* at `store.py:428` exactly — `clv = model_fair_odds / closing_odds − 1` (odds-ratio form) — and writes `predictions`. The draft's "reuse `set_closing_odds` verbatim" was wrong; the correct instruction is "mirror the formula, new writer," and CLV is **NULL (not 0)** when no close exists.

### 2.5 The four flows

**WRITE (at card build, beside `modelpreds.write_predictions`):**
```
build_card + build_score_cards + run_advancement
  → modelpreds.write_predictions(...)                          (existing JSON log, unchanged)
  → predledger.build.flatten_card(...) → upsert_predictions     (NEW: full paper book)
                                       → upsert_acca (paper acca legs)  [corrected: acca_legs]
  → record_bet(...) for +EV picks → predledger.store.link_bet(pred_id, bet.id)  (placed=1)
```
If a bet has no matching prediction row (manual punt, `source='punt'`), insert a synthetic prediction row with `model_source='from_bet'` so the realized book is always a strict subset — no orphan bets. Real (staked) accas link via `acca_legs.bet_id`.

> **[corrected — per-market column-population contract].** `build_score_cards` returns `List[ScorelineCard]` (one per surviving fixture, in odds-feed order); O/U + BTTS rows are derived from the reconciled scoreline matrix and **have no market price unless a totals/BTTS market exists in the feed.** Therefore the following columns are **NULL by market**, and the golden flatten test must assert these NULLs:

| market | `model_prob` | `market_devig_prob` / `market_best_odds` | `edge` / `ev_per_unit` | `clv` (after close) |
|---|---|---|---|---|
| `1x2` | set | set (3-way devig) | set | set |
| `scoreline` | set | **NULL** (no liquid exact-score book) | **NULL** | **NULL** by design |
| `ou_<L>` / `btts` | set | set **iff** totals/BTTS market in feed, else **NULL** | set iff market present, else **NULL** | set iff twoway close exists, else **NULL** |
| `advancement` | set | last pre-event Polymarket mid (thin) | set iff priced | thin-market caveat in `notes` |

`run_advancement` is the producer in `src/wca/advancement.py`; `flatten_card` consumes its DataFrame output. `flatten_card` returns **two** lists (`PredictionRow[]`, `AccaLeg[]`).

**CLOSE (kickoff daemon, alongside `closecapture`):**
- **1X2** — full reuse: `consensus_close(con, match_id, home_raw, away_raw, kickoff_utc)` → `clv = model_fair_odds / fair_closing_odds(p_leg) − 1`. **[corrected]** `consensus_close` is keyed on **`match_id` + raw team names** and takes **no `market` arg**; the close writer must pass `match_id`, not just team names (the single most likely cause of silent `None` for every row).
- **O/U & BTTS** — new `consensus_close_twoway(con, match_id, home_raw, away_raw, kickoff_utc, market)` — **[corrected signature: includes `match_id`]**; snapshot query is `WHERE match_id=? AND market=?`. De-vig the two-way pair; where no totals/BTTS snapshot exists → CLV NULL, `notes='no market close'`.
- **Scoreline** — no liquid exact-score consensus → CLV NULL by design (still settled).
- **Advancement** — closing reference = last pre-event Polymarket mid; honest "thin market" caveat in notes.
- CLV is **NULL (not 0)** where no close exists; charts filter on `clv IS NOT NULL` **and surface coverage** (§3.3-C10).
- **[corrected — close-side timing & leakage guards (A3)]:** enforce a hard pre-kickoff window `commence_time − ε ≤ close_ts ≤ commence_time`, record `close_lag_seconds`, and **verify the snapshot is pre-match by status, not just timestamp** (`close_is_prematch=1`); a delayed kickoff can otherwise let an in-play tick in and contaminate CLV with realized in-game information. Drop/flag rows whose lag exceeds a threshold. The §5.3 look-ahead guard protected only the *prediction* side; this is the symmetric guard on the *close* side. Idempotency guard: only stamp where `closing_odds IS NULL`.

**SETTLE (after result lands, independent of money):**

| market | rule (from `score="h-a"`, `outcome`) | source |
|---|---|---|
| `1x2` | won if `selection == outcome` | `wc2026_results.json` |
| `scoreline` | won if `selection == f"{h}-{a}"` | `wc2026_results.json` |
| `ou_<L>` | total = h+a; over won if > L; under won if < L; push if == L | `wc2026_results.json` |
| `btts` | both > 0 → yes won / no lost; else inverse | `wc2026_results.json` |
| `advancement` | won if team reached `stage`; lost if eliminated earlier; stays `open` until decidable | **`advancement_played_results.json`** |

**No P&L is computed in settle** — settlement is pure correctness (won/lost/push). P&L lives only on the linked `bets` row. The paper book is about *forecast skill*; the realized book is about *money*; the view joins them.

**BACKFILL (one-shot, idempotent):** reconstruct 1X2 history from `model_predictions_log.jsonl` × `wc2026_results.json`. Confirmed scope: the JSONL carries only `{dc, elo, market, model, fixture, match_id, generated, kickoff}` (1X2 triples). Honest scope: ✅ 1X2 predictions + settlement; ⚠️ 1X2 CLV only where a pre-KO snapshot survives; ❌ scoreline/O-U/BTTS/advancement history **cannot** be backfilled — they accumulate from first live `flatten_card` forward. Backfill must not fabricate them (assertion-test enforced).

> **[corrected — backfill close is survivorship-biased (C11)].** Snapshot survival is **not random**: early/low-interest fixtures were polled less, and the cadence changed (3→6 min). Backfilled CLV is thus biased toward heavily-polled (high-interest, sharp) matches — sampling non-stationarity *separate from* market non-stationarity. Every backfilled CLV row carries `close_lag_seconds` and `n_books_at_close`; analyses condition on coverage; and **backfilled CLV must not be spliced into the live expanding-window t-stat without a regime indicator at the splice point** (§3.4-G2).

### 2.6 Dev-box / mini constraint

**Honor the MEMORY rule:** never mutate `data/wca.db` on this MacBook. Predledger writes go to `data/dev.db` here; build/settle/close run on the **mini** against `wca.db`, same as `closecapture`. The CLI takes `--db` (default from `.env.dev` → `dev.db`).

> **[corrected — make the rule code, not documentation].** Add a **guard**: any predledger write whose `--db` basename is `wca.db` **on the dev box** raises (config flag `WCA_ALLOW_PROD_DB` off by default); a test asserts the refusal. **[corrected — `busy_timeout` placement]:** `_connect` does *not* set `busy_timeout` today; rather than patch the shared hot-path connection used by the live bot, predledger sets `PRAGMA busy_timeout=5000` **per-connection in `predledger.store`**. (If we ever want it in shared `_connect`, that is its own tiny PR with the bot regression in scope — not buried in P0-T1.) Keep passes short-transaction (one BEGIN…COMMIT per build batch; per-fixture commits in settle/close). Publish a read-only `site/predledger.json` projection — the site never reads SQLite directly.

---

## 3. The Analytics Modules

All modules read from the prediction ledger (§2) and render as Tracking-tab panels in the established house style: pure inline SVG (`svgOpen()/sx()/sy()`, CSS-variable theming, `statTile()`/`tick()` helpers), deterministic builders, graceful `<div class="tr-empty">` fallback. **Existing panels 01–06** of `tracking_data.json`/`tracking.js` (Headline, FT Scoreline+Result, Prediction Scoreboard, Calibration Scatter, CLV-vs-P/L, Return-per-Unit) are **extended, not replaced** — each gains a `book` toggle, and the existing placed-only Panel 04 (CLV-vs-P/L) becomes the `placed:true` subset of the new full-book skill-vs-luck panel.

> **Project-wide invariant [corrected — added]:** every verdict / CLV / win-rate / risk panel carries the standard **"monitoring, not betting advice"** footer, and all bot-ticker keys (`model_beat_rate`, etc.) are **descriptive, not prescriptive.**

---

### 3.1 Module A — Monte Carlo upgrades + MC-driven analytics

**What it answers:** What is the *distribution* (not point EV) of our open book's P&L across every simulated tournament? How calibrated are our futures probabilities, with what uncertainty? What happens "if Brazil exits the group"?

**Structural move:** retain the per-sim joint state so one sim run serves many consumers.

> **[corrected — A1 over-claimed; current engine already decouples].** `_sample_goals` (tournament2026.py:477) **already** draws the 1X2 outcome exactly from `_probs`, then samples Poisson goals *coerced* to that outcome (`_coerce_outcome`) for GD/GF tie-breaks. So **published-card 1X2 is already consistent by construction** — A1 does *not* buy 1X2 consistency. What A1 actually buys is **O/U + BTTS + correct-score** consistency with the card. The reuse seam is **`_probs` + `reconcile_scoreline_matrix` (scores.py:129) wired into `_sample_goals`** — a deeper change than "Walker alias per ordered pair" implies. A1's value prop and acceptance test are reworded accordingly: the test that matters is *O/U/BTTS/correct-score within MC SE of the card*, not 1X2-matches-card (already true).

> **[corrected — `simulate`/`SimulationResult` are MINIMAL today].** Current signature is `simulate(self, n_sims=10000, rng_seed=None)`; `SimulationResult` carries only `teams, n_sims, group_position, reach, win`. All A-upgrades below are **net-new optional params and net-new dataclass fields defaulting to None/empty**, not extensions of existing ones. Every acceptance criterion must explicitly exercise existing consumers unchanged (`wca_mc_futures.py`, `advancement.py`, `as_dataframe`).

**Key engine upgrades (each flag-gated, additive to `SimulationResult`):**

| Upgrade | Effect |
|---|---|
| **A1 scoreline atomic draw** (`sample_mode="scoreline"`, net-new) | O/U / BTTS / correct-score become mutually consistent with the card (1X2 already is); truer GD/GF for tie-breaks. Seam: `_probs` + `reconcile_scoreline_matrix` |
| **A2 `retain_paths=True`** (net-new) | `(n_sims, n_teams)` uint8 survival bitsets per stage (~10 MB @ 40k) + `champion` + `group_pos_per_sim`; enables conditional futures = masked means |
| **A3 calibration + anchoring** | offline 2018/22 backtest → per-stage Brier/log-loss + recommended temperature `T`; live market anchor `p_anchored=(1-w)·p_sim + w·p_market` on marketed nodes only, taking the **more conservative** of model/anchored |
| **A4 CRN + antithetic** (default ON) | fixed uniform stream → scenario *deltas* get order-of-magnitude tighter CIs; 2–10× fewer sims for target SE |
| **A5 SE on every probability** | Wilson band per probability; optional `--target-se` adaptive stop in 5k batches |
| **A6 manifest** | every output `meta`: `{seed, n_sims, n_eff, sample_mode, vr[], git_sha, model_fit_ts, played_through, achieved_se, fx_rate, fx_ts}` |

> **[corrected — A4/A5 are in tension; reconcile the SE].** The marginal-probability SE `√(p̂(1−p̂)/n)` is correct for *marginal* probabilities but **wrong for scenario deltas under CRN/antithetic**, where draws are negatively correlated — and scenario deltas are the whole point of CRN. **Rule:** marginal probabilities use the closed-form/Wilson SE; **scenario deltas use the paired-difference variance across the common random stream**, never `√(p(1−p)/n)`. The manifest records which estimator produced each SE.

**Flagship analytic — portfolio P&L distribution:** value the actual open book against all `n_sims` tournaments (vectorised, milliseconds after the sim, reusing the exact `settle_bet` conventions including **lay liability and free-bet stake-not-returned** — confirmed at store.py:255), FX-adjusted at the position level. Produces mean/median/P5/P25/P75/P95, **VaR/CVaR** (the probabilistic version of the deterministic "hard cash floor" — floor becomes the 0th percentile, CVaR the realistic tail), `P(book down)`, and **per-team P&L contribution**.

> **[corrected — FX framing on-surface].** The P&L distribution is the *one* place currencies are combined; that is allowed because it is an **explicit FX conversion with recorded rate+timestamp**, not a naive sum. The MC-1 panel states on-surface: *"FX-converted to GBP @ ⟨rate, ts⟩ for the distribution view only; per-venue realized-P&L tables remain faceted GBP/USD and are never summed."*

**Tracking-tab panels:**

| Panel | Viz | JSON feed |
|---|---|---|
| **MC-1 Risk/P&L distribution** | P&L histogram + VaR/CVaR markers + hard-floor line; per-team contribution bars; FX-disclosure footer | `site/risk_pnl.json` |
| **MC-2 Fan charts** (extends `tracking_adv.js`) | per-team P(Final) over time, median + 95% band | `site/advancement_history.json` (banded) |
| **MC-3 Forest plot** | per-stage cross-team P(reach S) with CI95 whiskers; market price overlay | `site/mc_futures.json` (envelope `{value, se, ci95, n_eff}`) |
| **MC-4 Scenario panel** | picker swapping futures/exposure to conditional view; tornado chart of param sensitivity | `site/mc_scenarios.json` |
| **MC-5 Live MC calibration** (reuses reliability-curve renderer) | sim reliability curve + Brier-over-time + sharpness-vs-calibration scatter | `site/mc_calibration.json` |

**MC feeds other modules:** expected strike rates → §3.2; joint acca settle prob (`model_joint` vs `model_indep`) → §3.2 acca autopsy; distributional exposure (`{ev, p5, p95, cvar}`) → exposure panel.

**Probability envelope (cross-cutting):** every MC probability becomes `{"value":0.123,"se":0.0016,"ci95":[0.120,0.126],"n_eff":80000}`; `--flat` re-emits bare floats for legacy consumers.

---

### 3.2 Module B — Rolling win-rate (singles / accas / leg cross-sections)

**What it answers:** Is the model right (MODEL book, no selection bias)? Did we make money (REALIZED book)? Which acca leg types keep killing our accumulators?

**Two books, one outcome stream:** every metric computed twice — **MODEL** (every prediction; win = model argmax == outcome) and **REALIZED** (placed bets; win = `status=='won'`, void excluded not zeroed). Paper accas come from `acca_legs` (§2.2), so the MODEL acca strike rate is now backed by data.

**Inputs:** `predictions` + `acca_legs` (paper book) + `v_realized_book`, both result files, `reports._decompose_legs`/`_leg_is_result` for realized accas.

**Key statistics (implementation-ready):**
```
Wilson(k,n,z=1.96): p=k/n; d=1+z²/n; c=(p+z²/2n)/d; h=z/d·√(p(1−p)/n+z²/4n²); [c−h,c+h]
Rolling(W=10):  p_roll=mean(x[t−W+1..t]); band=Wilson
EWMA(H=8):      λ=1−2^(−1/H); s_t=λx_t+(1−λ)s_{t−1}; n_eff=(Σw)²/Σw²; band=Wilson(round(s·n_eff),n_eff)
Expanding:      p_cum=mean(x[0..t]); band=Wilson(Σx,t+1)
Cal-bias:       bias=mean(p)−mean(x); z_bias=bias/√(mean(p(1−p))/n)
Brier/BSS:      (p−x)²; BSS_cum=1−Σbrier_model/Σbrier_market
Acca indep:     P=Πp_j; lift=actual/expected
Leg-corr:       φ / tetrachoric on co-occurrence of same-acca legs using REALIZED outcomes  [corrected: B7]
Counterfactual: would_win_without[ℓ]=1[all legs≠ℓ won]; recovered_pl=(Π_survivors odds−1)·stake
```

> **[corrected — overdispersion is not the correlation statistic (B7)].** The draft's `var_obs / Σ p_j(1−p_j) > 1` conflates **model miscalibration** with **leg correlation** (it uses *model* `p_j`, so an overconfident model inflates it even with independent legs), and below ~30 accas there is no sampling distribution attached. **Replacement:** estimate leg correlation **directly from same-acca legs via φ / tetrachoric on realized outcomes**; reserve overdispersion strictly as a *calibration* diagnostic, and only ever show it against a **parametric bootstrap reference under independence using *market* probs** (not model probs). Below **N ≈ 30 accas, label "not estimable" — do not print ">1".**

**Acca autopsy (the heart of B):** decompose every acca into legs; per-leg-type Wilson hit-rate; **survival drag** (share of acca losses attributed to each odds-bucket/leg-type); **counterfactual "would have won if leg X removed"** near-miss list; **direct leg-correlation** (φ/tetrachoric, ≥30-acca gate). Three-line calibration (actual / model-implied / market-implied) directly exposes the documented "blend doesn't beat the de-vigged market" finding.

**Tracking-tab panels (new "Win-rate" sub-tab, `site/winrate.js`):**

| Panel | Viz | Feed |
|---|---|---|
| **W0 Headline tiles** | win-rate (Model/Realized) with Wilson sub; calibration z; BSS; acca strike (N=…); coverage | `winrate.json` |
| **W1 Rolling win-rate over time** (centerpiece) | rolling/EWMA/cumulative lines + Wilson ribbon + model/market expected reference lines | `winrate.json` |
| **W2 Calibration of win-rate** | gap time-series (`p_roll − exp_model`) + reliability scatter w/ Wilson y-bars | `winrate.json` |
| **W3 Brier/skill rolling** | model vs market Brier; BSS shading | `winrate.json` |
| **W4 Segment win-rate bars** | horizontal Wilson-whisker bars by leg/odds/source/stage | `winrate.json` |
| **W5 Acca strike + expected/actual** | wide-ribbon rolling strike (point hidden when N<8); lift annotation | `winrate.json` |
| **W6 Leg cross-section (acca autopsy)** | per-leg-type bars + drag stacked bar + near-miss table + φ/tetrachoric strip (≥30-acca) | `winrate.json` |

**Low-N contract (mandatory):** `n==0` → empty state; singles `n<5` or accas `n<8` → band-only, point greyed, `"N=… — band only"` badge; void/push bets excluded from numerator *and* denominator everywhere (identically to the CLV beat-rate, §3.3).

---

### 3.3 Module C — CLV benchmarking (full book)

**What it answers:** Is the model's *claimed* edge a real leading indicator of the close — on the **full book of recommendations**, not the self-selected handful that became bets?

**Core primitive — per-leg model CLV** (a property of the *prediction*, not the bet):
```
clv_odds(f,ℓ)   = o_model / o_close − 1 = p_close / p_model − 1   # EV/leading signal
clv_prob(f,ℓ)   = p_model − p_close                               # calibration residual
edge_build(f,ℓ) = p_model − p_market_build                        # the (endogenous) bucketing variable
```
**Fair-vs-fair only:** `p_model` is a true triple; `p_close`/`p_open` are de-vigged. Never compare to a raw vigged book price — that manufactures phantom CLV equal to the overround. The two sign conventions are labelled and **never averaged together**.

> **[corrected — A1: self-consistency / line-shopping artifact is the most dangerous hole].** `consensus_close` (closecapture.py:240) de-vigs a *consensus across books*; our recommendations are priced off `market_best_odds`/`market_book` drawn from the **same snapshot universe**. If we take the best price and the close is the consensus of that same set, `clv_odds` is **mechanically positive whenever we took best-of-N**, even for a pure-noise model — we'd be measuring **line-shopping / overround capture, not forecasting skill.** **Mandatory correction:** grade the skill claim on **consensus-vs-consensus, leave-one-book-out CLV** (exclude the transacted book from the consensus for that leg). Report **two separate columns** — "best-price CLV" (descriptive) and "consensus-vs-consensus CLV" (inferential). The verdict's CLV gates use the latter only.

> **[corrected — A2: don't pool 2-way and 3-way CLV].** Proportional de-vig has different, outcome-asymmetric bias on 2-outcome books (worse on lopsided sides). The schema records `devig_method` **and `n_outcomes`** (§2.2); **never pool across `n_outcomes` in any headline or gate.** Strongly consider **Shin de-vig** for two-way markets and back-test the de-vig choice — proportional normalization under-corrects favorite-longshot bias, which directly contaminates the "edge on draws / on longshots" segment claims below.

**Inputs:** `model_predictions_log.jsonl` × `odds_snapshots` × `wc2026_results.json` — joined offline into `data/clv_benchmark_log.jsonl` (no new write path). Reuse `consensus_close`, `selection_leg`, `fair_closing_odds`, `devig_consensus`, `teamnames.canonical` verbatim.

**Central deliverable — CLV-by-edge-bucket × placed-vs-passed:**

| Slice | Question |
|---|---|
| Market type (1X2 home/draw/away) | Is the edge real on draws (documented ~14pt underprediction)? |
| Odds bucket (by `o_close`) | Edge on favs (efficient) or longshots (claimed edge but lost −6.9% historically)? |
| **Edge bucket** (by `edge_build`) | Monotone-rising CLV across edge buckets ⇒ edge real; flat/noisy ⇒ illusory |
| Venue/book | Which books does the model beat? (routing signal) |
| **Placed vs passed** | If placed-leg CLV ≫ passed-leg CLV, the +EV filter adds value; if equal, the threshold only adds variance |

> **[corrected — the edge-bucket monotonicity test is partly tautological].** `edge_build = p_model − p_market_devig_prob` is built from the **same book that becomes the "open,"** so regression-to-close makes high-edge picks have high CLV *by construction*, even with no skill. **Corrections:** (i) test the trend with a **single pre-registered isotonic / Jonckheere–Terpstra trend test**, not by eyeballing K bar heights (that is K-fold multiple testing in disguise); (ii) **deconfound with a label-shuffle placebo** — recompute the edge-bucket/CLV slope after shuffling outcomes within matchday; the real-edge signal is the **excess slope over the placebo slope**, not the raw slope.

**Coverage is a first-class output [corrected — C10].** Every CLV aggregate is shown beside **CLV coverage = % of the relevant paper book with a non-NULL, pre-match-verified close**, faceted by market. Where coverage is low (scoreline, thin advancement, pre-tournament O/U), **calibration/Brier-vs-outcome is the primary skill signal** and the panel states "CLV structurally unavailable" rather than implying "no CLV edge."

**Leading-indicator test:** `lead_gain = |p_open−p_close| − |p_model−p_close|`; directional `drift_agreement = mean(sign(p_close−p_open) == sign(p_model−p_open))`; regression `drift = α + β·signal` (β≈1, α≈0 ⇒ unbiased one-step-ahead predictor of the close). Use the **genuine first snapshot** for `p_open`, flag `open_source ∈ {first_snapshot, build_market}`.

**Beat-rate null is NOT 0.50 [corrected — A4].** Under zero skill, `P(CLV>0)` is inflated by the line-shopping/overround effect and deflated by pushes/NULLs. **Calibrate the null empirically via a permutation/placebo** — shuffle model probabilities across fixtures within matchday, recompute the beat-rate distribution; the gate is "observed beat-rate exceeds the 95th percentile of the placebo," not ">0.50." The same placebo anchors the **CLV-mean null** (replacing the implicit "mean CLV null = 0"). Pushes (`status='push'`, `clv IS NULL`) are excluded from numerator *and* denominator of beat-rate and from the t-test (specified identically for win-rate and CLV).

**Skill-vs-luck decomposition:** `E[realized_pl] ≈ p_close/p_model − 1 = clv_odds` — CLV is the variance-free estimator of *expected* realized P&L. `realized_pl = clv_odds (skill) + (realized_pl − clv_odds) (luck, mean≈0)`. Report `Σclv` vs `Σrealized` vs `luck`. Carry the **non-circular** Brier-skill-vs-outcome alongside (the P&L decomposition uses the close as `E[y]`, so it is circular for grading the close itself — see §3.4-E15).

> **[corrected — G: same central-tendency on both sides of the identity].** The draft reported **median** `clv_odds` in the headline but **mean** for `clv_points`, then equated `clv_odds` to *expected* (mean) P&L — internally inconsistent, because the skew we're avoiding is exactly what breaks median≈mean. **Use a trimmed mean or log-CLV mean with a bias-corrected CI**, and keep the **same operator on both sides** of the skill-vs-luck identity.

> **[corrected — missing estimator: does CLV actually predict P&L *in our own data*?].** The entire 150× thesis rests on CLV proxying ROI. **Add a rank-correlation / quintile-spread test of realized P&L on CLV (cluster-bootstrapped).** If high-CLV bets did *not* out-earn low-CLV bets within this book, the sample-efficiency argument collapses and the verdict must say so.

**Tracking-tab panels (new "Model CLV — full book" section in `tracking.js`):**

| Panel | Viz | Feed |
|---|---|---|
| **C-0 Headline strip** | beat_rate vs placebo-null, median+trimmed-mean CLV, lead_rate, drift β, Brier skill, **coverage %** | `tracking_clv_benchmark.json` |
| **C-1 CLV-by-edge-bucket** (deconfounder) | bars per edge bucket + **placebo slope overlay**; isotonic/JT trend p; CI error bars | same |
| **C-2 CLV histogram by market** (never pooled across `n_outcomes`) | overlaid home/draw/away, draw highlighted | same |
| **C-3 Lead-indicator scatter** | `drift` vs `signal` + fitted line + y=x, β annotated | same |
| **C-4 Skill-vs-luck** (extends Panel 04) | `clv_odds` vs `realized_pl`, placed filled / passed hollow, y=x, running Σ; **CLV→P&L quintile spread** | same |
| **C-5 Placed-vs-passed table** | two-row selection-bias scorecard | same |

Fold headline keys into `summary()` (`model_clv_median`, `model_beat_rate`, `clv_lead_rate`, `brier_skill`, `clv_coverage`), labelled **"all recommendations"** vs the existing placed-bet **"placed bets,"** with the "monitoring, not betting advice" footer.

---

### 3.4 Module D — Repeatable-edge estimator battery (the RIGOR layer)

**What it answers:** Is the edge *real yet*? A single traffic-light verdict, decided by a pre-specified rule over multiple estimators that must agree, whose default is `INSUFFICIENT SAMPLE`.

**Governing philosophy:** CLV is the leading indicator; ROI is the lagging confirmation; calibration is the mechanism check. **An edge is "real" only when a leading signal (CLV) AND an outcome-anchored signal (skill or calibration) AND stability all agree** — and we have enough effective samples to rule out luck after correcting for everything we tried.

**Inputs:** `bets` (CLV/ROI), full model book from `predictions`/`acca_legs` (survivorship-free skill), `odds_snapshots` (close), both result files.

**Clustering [corrected — match_id is wrong for futures (D13)].** Cluster id = `match_id` for match markets; **acca → synthetic `acca_id`** (§2.2); **futures/advancement → `team_tournament`** (a team in the final was in the semi — nested), and **cross-team futures share the simulation's common shocks, so the realistic unit is the *whole tournament* (one realization)**. Consequence: **futures CLV/skill is N≈1 in the deepest sense and is gated to `INSUFFICIENT SAMPLE` almost permanently** — the verdict panel must never green-light a futures edge off within-tournament cross-sectional "N."

**Effective-N [corrected — don't approximate DEFF (D14)].** Clusters are wildly unequal (a single = 1 leg; a 5-leg acca; advancement = dozens of nested legs), so the equal-size `DEFF = 1+(n̄−1)ρ` understates inflation. **The cluster bootstrap already yields correct inference — report the bootstrap-implied `n_eff` (ratio of naive to cluster variance) rather than a closed-form DEFF that disagrees with the bootstrap.**

**Estimator families:**

- **CLV (primary), consensus-vs-consensus only (§3.3-A1):** sign test + Wilson beat-rate **vs the placebo null** (§3.3-A4); **BCa bootstrap at cluster level** (B=10,000, headline uses BCa lower bound); cluster-robust SE; **expanding-window confidence sequence** (below).
- **Skill vs market (full book) [corrected — B6]:** primary statistic is the **Diebold–Mariano test on the paired per-cluster log-loss differential** (`BS_model,i − BS_market,i` / log-loss, paired per fixture, cluster-bootstrapped) — the standard for comparing two probabilistic forecasts; it handles the pairing/dependence the draft's two-independent-bootstrap framing got wrong. **BSS / info-gain (nats) are demoted to descriptive effect sizes** (and reproduce the card backtest's non-significant nats result). Reliability curve + **Murphy decomposition** (reliability − resolution + uncertainty).
- **Calibration [corrected — B8: drop the ECE<0.05 gate].** ECE is positively biased at small N and binning-dependent. **Gate instead on a calibration-slope regression** (`y ~ logit(p)`): **PASS iff the slope CI contains 1 and the intercept CI contains 0.** Report **adaptive equal-mass-binned, bootstrap-CI'd, debiased ECE** as a *descriptive* sidecar only; use **per-class one-vs-rest** (or a multiclass-proper variant) for 1X2 — never binary ECE on a 3-way market. **Spiegelhalter's z** as a secondary calibration test.
- **Profit (lagging):** stake-weighted + flat ROI; cluster BCa CI; **Bayesian credible interval** with a break-even-skeptical Beta prior; Sharpe. The **samples-to-significance curve** (ROI ≈3,860 one-sided vs CLV ≈25-conditional, log-y) is the centerpiece teaching chart.
- **Stability [corrected — B9: calibrate or replace the control chart].** Standardize the monitored series by a **rolling robust SD**; prefer a **block-permutation structural-break test** (consistent with the time-blocked bootstrap) over hand-set CUSUM constants. If CUSUM is kept, **calibrate `h` to a stated target in-control ARL via simulation** — `κ=0.5, h=5` are not magic numbers with a known false-alarm rate on an estimated-SD, non-stationary series. **OOS-vs-IS** ratio; **edge-decay** OLS slope.

**The verdict panel — gates (all on cluster-robust / full-book / consensus-vs-consensus quantities):**

| Gate | Statistic | PASS |
|---|---|---|
| G0 sample sufficiency | bootstrap-implied `n_eff` clustered | ≥25 (CLV), ≥100 (ROI); **futures permanently fail** (N≈1) |
| G1 CLV positive (primary) | BCa lower bound of mean CLV (consensus-vs-consensus) | **> cost-adjusted ROPE floor**, not just > 0 (G-E17) |
| G2 CLV significant | **expanding-window confidence sequence / always-valid boundary** | exceeds boundary & stays |
| G3 beat-rate | Wilson lower bound of P(CLV>0) | **> placebo 95th percentile**, not > 0.50 |
| G4 skill vs market | **Diebold–Mariano** on paired per-cluster log-loss | one-sided reject in model's favor |
| G5 calibration | **slope CI ∋ 1 and intercept CI ∋ 0** (full book, N≥100) | pass |
| G6 stability | block-permutation break test (or ARL-calibrated CUSUM) + OOS/IS | no break & ratio > 0.5 |
| G7 multiple testing | pre-registered endpoint; segments via **BH–Yekutieli / max-t permutation** (dependent) q=0.10 | pass |

> **[corrected — E15: CLV-only green is defeatable; require an outcome-anchored gate].** Because best-price CLV can be positive with zero outcome skill (§3.3-A1), **no green may be issued on CLV alone.** CLV is necessary but not sufficient: **base green requires (G1∧G2∧G3) AND (G4∨G5) AND G6.** This also resolves the circularity the design flags in §3.3 (CLV uses the close as `E[y]`).

> **[corrected — E16: stop peeking].** The expanding-window verdict is evaluated every matchday; testing a growing sample against a fixed `t*=1.65` is optional-stopping and will cross under the null with probability ≫5%. G2 uses an **always-valid sequential boundary** (confidence sequence / mixture-SPRT / α-spending), not a fixed critical value. "Crosses and stays" is a heuristic, not a test.

> **[corrected — E17: significance ≠ economically real].** G1's lower bound must clear a **pre-registered cost-adjusted ROPE floor** — overround half-spread + de-vig-method bias estimate + cost of capital — not merely 0. A +0.4% CLV "significant" at n_eff=40 is inside the de-vig bias band.

> **[corrected — F18: exclude, don't just flag, the 24 in-sample matches].** The blend was tuned on the 24 played matches; including them in G2/G4 inference is optimistic bias. They are **excluded from inferential gates** (or the series is strictly walk-forward with the fit frozen before each matchday) — flagging on charts is not sufficient.

> **[corrected — F19: stratify by stage].** Group→knockout is a structural break in the *outcome* DGP (single-leg, extra time, penalties, incentives), not just price efficiency. **The verdict is stratified by stage by default; any cross-stage pooled number is exploratory.**

```
if n_eff < 25 (or market == futures):  → "INSUFFICIENT SAMPLE" (grey)
elif (G1&G2&G3) and (G4 or G5) and G6: → "EDGE LIKELY (CLV + outcome-confirmed)" (green; +strong if G4&G5)
elif (G1&G3) and not (G2 and (G4|G5)): → "PROMISING — needs more data" (amber)
elif G6 fails:                         → "EDGE DECAYING / REGIME BREAK" (red-amber)
elif G1 fails at n_eff≥50:             → "NO EDGE DETECTED" (red)
else:                                  → "INCONCLUSIVE" (grey)
# ROI verdict reported SEPARATELY, gated on n_eff≥100, NEVER overrides the CLV+outcome verdict.
```

**Tracking-tab panels (new `site/rigor.js` tab, feed `site/rigor.json`):** Verdict banner (traffic light + reason + n/n_eff chip + "monitoring, not betting advice") · CLV block (mean+BCa, placebo-anchored beat-rate gauge, confidence-sequence path, consensus-vs-best toggle) · Skill block (DM-test stat, reliability curve, calibration-slope CI, Murphy bars) · Profit block (ROI BCa+Bayes, Sharpe, samples-to-significance curve, CLV→P&L quintile spread) · Stability block (rolling bands, block-permutation break, OOS/IS, by-stage strata) · Segments table (raw p, BH–Yekutieli-adjusted p, survives-FDR, coverage). Each block degrades to "insufficient sample (n=X, need Y)."

---

## 4. Data Architecture

### 4.1 New tables / feeds

| Artifact | Type | Producer | Consumer |
|---|---|---|---|
| `predictions` + `acca_legs` + 2 views | `wca.db` (mini) / `dev.db` (box) | `predledger.*` | everything |
| `data/clv_benchmark_log.jsonl` | append-only JSONL | offline joiner | Module C |
| `site/predledger.json` | read-only projection | `predledger publish` | site |
| `site/winrate.json` | derived | `wca_winrate_data.py` | Module B |
| `site/tracking_clv_benchmark.json` | derived | `clvbench.model_clv_benchmark` | Module C |
| `site/rigor.json` | derived | `rigor/build.py` | Module D |
| `site/risk_pnl.json`, `mc_scenarios.json`, `mc_calibration.json` | derived | `mc/*`, scenario/calibration scripts | Module A |
| envelope upgrades to `mc_futures.json`, `advancement_history.json`, `tracking_buckets.json` | derived | MC builders | Module A, §3.2 |

### 4.2 Pipeline fit

```
CARD BUILD (mini)
  fit_models → build_card + build_score_cards + run_advancement
    → modelpreds.write_predictions                 (unchanged)
    → predledger.build.flatten_card → upsert_predictions + upsert_acca   (NEW — paper book + paper accas)
    → record_bet → link_bet                        (realized subset)
    → MC: simulate(retain_paths=True) → risk_pnl.json + mc_futures.json (enveloped)

KICKOFF DAEMON (mini, alongside closecapture)
  closecapture.capture_closes  (bets, existing)
  predledger.close             (predictions — 1x2 full via consensus_close(match_id,…);
                                twoway via consensus_close_twoway(match_id,…,market); pre-KO verified)

RESULT LANDS
  predledger.settle            (1x2/scoreline/ou/btts ← wc2026_results.json;
                                advancement ← advancement_played_results.json; money or not)

SITE BUILD / PUBLISH (CI commits to origin/main; served from localhost 8000/8001 — Vercel removed 2026-07-08)
  predledger publish → predledger.json
  wca_winrate_data / clvbench / rigor build / mc builders → site/*.json
  → commit → GitHub Actions → origin/main → localhost serving
```

### 4.3 Constraints (hard rules)

- **Single-writer / WAL:** `_connect` sets `journal_mode=WAL`; predledger sets `busy_timeout=5000` **per-connection** (not in shared `_connect`); short transactions only. Concurrent reads never block the live bot.
- **Mini is the canonical ledger.** Dev-box & mini ledgers are forked; the mini drives the published feeds on origin/main. **Never mutate `wca.db` from this MacBook** — predledger/CLV/rigor writes go to `dev.db` here; a guard **refuses a `wca.db` basename on the dev box** unless `WCA_ALLOW_PROD_DB` is set. All stamping passes run on the mini. CLI `--db` defaults from `.env.dev`.
- **Site never reads SQLite** — only the derived `site/*.json` projections. DB is source of truth; JSON is a view.
- **Currency separation — £/$ NEVER summed.** CLV is dimensionless → CLV tables span all venues (but **never pool across `n_outcomes`**). Any realized-P&L/EV table is faceted `GBP`/`USD` per `reports._platform_currency` (`polymarket|kalshi → USD`, else GBP) and never summed. **MC P&L is the sole sanctioned cross-currency view** — FX-adjusted at the position level with rate+timestamp recorded in `meta` and disclosed on-panel as "distribution view only."

---

## 5. Statistical-Integrity Guardrails (cross-cutting, stated once)

Every analytic in §3 inherits these. They are first-class panel citizens, not footnotes.

1. **Selection / survivorship bias — two kinds.** *Prediction selection* (only +EV bets graded) → fixed by the full paper book (`predictions`). *Market-existence selection* (CLV only where a close exists, biased toward efficient markets) → **not** fixed by the paper book. **Guard:** report **CLV coverage by market** beside every CLV aggregate; grade skill on **calibration/Brier-vs-outcome** wherever CLV is structurally NULL; expose the `placed`-vs-`passed` gap. *(§1.1, §3.3-C10)*
2. **The CLV self-consistency trap.** Best-price-vs-consensus CLV is mechanically positive under line-shopping. **Guard:** inferential CLV is **consensus-vs-consensus, leave-one-book-out**; best-price CLV is descriptive only. *(§3.3-A1)*
3. **Multiple testing across *dependent* segments.** ~12 slices share fixtures, so plain BH is anti-conservative. **Guard:** one **pre-registered primary endpoint** (portfolio cluster-robust consensus-CLV, unpenalized); segments use **BH–Yekutieli / max-t permutation**; the edge-bucket trend uses a **single isotonic/JT test vs a label-shuffle placebo**, never K eyeballed bars. *(§3.3, §3.4-G7)*
4. **Look-ahead — both sides.** **Guard:** `generated < kickoff` on the prediction side **and** a verified-pre-match close (`close_is_prematch=1`, bounded `close_lag_seconds`) on the close side; exclude the 24 in-sample blend-tuning matches from inferential gates. *(§2.5, §3.4-F18)*
5. **Leg & futures autocorrelation.** **Guard:** cluster = match (singles), `acca_id` (accas), `team_tournament`/whole-tournament (futures, → effectively N≈1); all bootstraps resample clusters; report **bootstrap-implied `n_eff`**, not closed-form DEFF. *(§3.4-D13/D14)*
6. **Non-stationarity & regime.** The market sharpens and the *outcome* DGP changes at the knockouts. **Guard:** stratify the verdict by stage; block-permutation break test + time-blocked (contiguous-matchday) bootstrap; never extrapolate a group-stage edge into knockouts; flag the backfill splice point. *(§2.5-C11, §3.4-F19)*
7. **Low-N (esp. accas).** **Guard:** Wilson everywhere; never a bare `p̂`; acca points suppressed below N=8; leg-correlation (φ/tetrachoric) labelled "not estimable" below N≈30 accas; every metric renders `n`/`n_eff` and greys below its power threshold; re-estimate σ for the CLV power gate once n≥15. *(§1.2, §3.2-B7)*
8. **Fair-vs-fair / vig discipline.** Both sides vig-free; record `devig_method` **and `n_outcomes`**; never mix methods or `n_outcomes` within an aggregate; prefer Shin for two-way. *(§3.3-A2)*
9. **Right-skew of `clv_odds`.** Use a **trimmed-mean / log-CLV mean with bias-corrected CI**, and keep the **same central-tendency operator on both sides** of the skill-vs-luck identity (no median-vs-mean mismatch). *(§3.3-G)*
10. **Significance ≠ money & peeking.** **Guard:** a **cost-adjusted ROPE floor** on the CLV lower bound; an **always-valid sequential boundary** for the live expanding-window verdict; ROI gated separately and never overriding. *(§3.4-E16/E17)*
11. **CLV must actually predict P&L *here*.** **Guard:** the **CLV→realized-P&L quintile-spread / rank-correlation test** (cluster-bootstrapped); if absent in our own book, the verdict says the proxy is unconfirmed. *(§3.3)*

---

## 6. Phased Roadmap (swarm-ready)

Each task is a self-contained, PR-sized prompt: names files, deliverable, tests, acceptance, dependencies. Hand each to one dev-swarm agent (one worktree + branch + PR each). **Phases are sequential; tasks within a phase marked `[parallel]` have no inter-dependencies.**

### Phase P0 — Prediction-ledger foundation (lands first; everything depends on it)

**P0-T1 — Schema + store** `[blocks all]`
- **Files:** new `src/wca/predledger/__init__.py`, `src/wca/predledger/store.py`; `scripts/wca_predledger.py` (skeleton CLI).
- **Deliverable:** `ensure_schema(db)` creating `predictions` (+ `line=-1`/`stage=''` sentinels, `n_outcomes`, close-timing & devig-method columns), `acca_legs`, indexes, `v_model_book`/`v_realized_book`, `schema_meta`. `upsert_predictions`, `upsert_acca`, `link_bet`, `settle_prediction`, `set_prediction_close` (**new writer mirroring store.py:428 odds-ratio formula; CLV NULL when no close**), query helpers. Use `wca.ledger.store._connect`; set `PRAGMA busy_timeout=5000` **per-connection here**. Deterministic `prediction_id`/`acca_id` hashes. Dev-box `wca.db`-refusal guard (`WCA_ALLOW_PROD_DB`).
- **Tests:** `tests/test_predledger_store.py` — idempotent upsert incl. **two upserts of a NULL-line 1X2 row → one row** (PK guard); FK link sets `placed=1`; view join returns paper+realized; futures rows with NULL `match_id` keyed on stage sentinel; `acca_legs` references valid predictions; dev-box `wca.db` write raises.
- **Acceptance:** `ensure_schema` is additive (cannot touch `bets`/`odds_snapshots`); runs against a throwaway `dev.db`; all tests green.

**P0-T2 — flatten_card + write wire-in** `[after T1]`
- **Files:** new `src/wca/predledger/build.py`; edit card build path adjacent to `modelpreds.write_predictions`.
- **Deliverable:** `flatten_card(recs, score_cards, advancement_df, now) -> (List[PredictionRow], List[AccaLeg])` per §2.5; wire `upsert_predictions` + `upsert_acca` + `link_bet`; synthetic `from_bet` rows for orphan punts. `score_cards: List[ScorelineCard]`; `advancement_df` from `advancement.run_advancement` (DataFrame). **Enumerate the per-market column-population matrix (§2.5):** scoreline/O-U/BTTS have NULL `market_*`/`edge`/`ev_per_unit` unless a totals/BTTS market exists.
- **Tests:** golden flatten on a 2-fixture + 1-futures + 1-acca set; orphan-bet → synthetic row; **NULL-asserting golden** (scoreline `market_devig_prob/edge/ev` all NULL; O/U NULL unless market present); acca legs reference the right predictions.
- **Acceptance:** a build writes N = 3·fixtures(1x2) + scoreline-K + 6(O/U) + 2(BTTS) + advancement rows; placed picks `placed=1`; paper accas materialized.

**P0-T3 — settle pass** `[after T1, parallel with T4]`
- **Files:** new `src/wca/predledger/settle.py`; `wca_predledger settle`.
- **Deliverable:** `settle_open(results, adv_results, db)` per the §2.5 table; **1x2/scoreline/ou/btts ← `data/processed/wc2026_results.json`; advancement ← `data/advancement_played_results.json`** (both default flags). Pure correctness (no P&L); `settle_source` stamped; push/void handled.
- **Tests:** each market rule incl. O/U integer-line push, BTTS both-score, advancement stays-open-until-decidable; both result files resolved at the corrected paths.
- **Acceptance:** re-running settle is idempotent; open rows with landed results flip status.

**P0-T4 — close pass + twoway extension** `[after T1, parallel with T3]`
- **Files:** new `src/wca/predledger/close.py`; extend `src/wca/closecapture.py` with `consensus_close_twoway(con, match_id, home_raw, away_raw, kickoff_utc, market)`; `wca_predledger close`.
- **Deliverable:** 1X2 CLV via `consensus_close(con, match_id, home_raw, away_raw, kickoff_utc)` (**pass `match_id`**) + `fair_closing_odds`; O/U & BTTS via twoway (`WHERE match_id=? AND market=?`) where snapshot exists; scoreline/advancement per §2.5; record `close_lag_seconds`, `n_books_at_close`, `close_is_prematch` (status-verified, not ts-only); enforce pre-KO window; `closing_odds IS NULL` idempotency guard; CLV NULL (not 0) where no close; record `devig_method`+`n_outcomes`.
- **Tests:** 1X2 CLV matches hand calc using `match_id` query; twoway de-vig sums to 1; in-play tick rejected by `close_is_prematch`; idempotent (no overwrite of existing close).
- **Acceptance:** runs alongside `closecapture` cadence; never overwrites a stamped row; returns non-None for fixtures with snapshots (regression against the dropped-`match_id` bug).

**P0-T5 — backfill + publish** `[after T2/T3/T4]`
- **Files:** new `src/wca/predledger/backfill.py`; `wca_predledger backfill | publish`.
- **Deliverable:** reconstruct 1X2 history from `model_predictions_log.jsonl` + `data/processed/wc2026_results.json` (deterministic ids, `model_source='backfill'`, CLV only where snapshot survives with `close_lag_seconds`/`n_books_at_close`, **no fabrication** of scoreline/O-U/BTTS/futures); `publish` → `site/predledger.json`.
- **Tests:** backfill idempotent; **honest-scope assertion** (no scoreline rows emitted); publish projection schema stable; backfilled rows carry coverage fields.
- **Acceptance:** backfill on existing JSONL produces 3·(fixtures×builds) 1x2 rows, dedup-safe with live builds.

### Phase P1 — CLV benchmarking + rolling win-rate (the leading-signal analytics)

**P1-T1 — CLV offline joiner + report** `[after P0; parallel with T3]`
- **Files:** new `src/wca/clvbench.py`, `data/clv_benchmark_log.jsonl`; extend `src/wca/ledger/reports.py` with `model_clv_benchmark(...)` + new `summary()` keys.
- **Deliverable:** offline join (`model_predictions_log` × `odds_snapshots` × `wc2026_results.json`) → benchmark log; the §3.3 dict (headline + by_market/odds/edge/venue + placed_vs_passed + scatter + **coverage by market**); **consensus-vs-consensus leave-one-book-out** primary + best-price descriptive; **placebo null** for beat-rate & CLV-mean; **never pool across `n_outcomes`**; **isotonic/JT trend + placebo slope** for edge buckets; **CLV→P&L quintile-spread**; trimmed-mean/log-CLV headline with bias-corrected CI; pushes excluded from num+denom.
- **Tests:** hand-computed CLV on a 3-fixture file; fair-vs-fair + leave-one-book-out assertion; placed-vs-passed split; placebo null reproduces ~0.5+overround under noise; empty buckets emit `n:0` not dropped.
- **Acceptance:** runs on existing data with zero new write path; reproduces the non-significant headline honestly.

**P1-T2 — CLV site panel** `[after T1]`
- **Files:** `src/wca/tracking.py` + `site/tracking.js` (new "Model CLV — full book" section, panels C-0..C-5); `site/tracking_clv_benchmark.json`.
- **Deliverable:** pure-SVG panels; old Panel 04 becomes `placed:true` subset of C-4; coverage shown on C-0; `summary()` keys labelled "all recommendations" vs "placed bets"; "monitoring, not betting advice" footer.
- **Tests:** builder deterministic; degrades to "no data"; golden JSON shape.
- **Acceptance:** edge-bucket bar (with placebo overlay) + placed-vs-passed + coverage render from live feed.

**P1-T3 — rolling win-rate builder** `[after P0; parallel with T1]`
- **Files:** new `src/wca/winrate.py`, `scripts/wca_winrate_data.py`; reuse `tracking`/`reports` helpers + `acca_legs`.
- **Deliverable:** `build_winrate_data(...) -> dict` per §3.2; Wilson/EWMA/rolling/Brier/**φ-tetrachoric**/counterfactual helpers; MODEL+REALIZED books; acca autopsy from `acca_legs`; overdispersion demoted to calibration sidecar with parametric-bootstrap (market-prob) reference, "not estimable" < 30 accas; push/void excluded num+denom.
- **Tests:** `tests/test_winrate.py` — Wilson edge cases (k=0,k=n,n=1), EWMA n_eff steady-state, expanding monotone band shrink, φ recovers 0 under simulated independence, near-miss correctness, low-N degradation, "not-estimable" label < 30 accas.
- **Acceptance:** deterministic (no wall-clock/network); writes `site/winrate.json`.

**P1-T4 — win-rate site tab** `[after T3]`
- **Files:** new `site/winrate.js`; add "Win-rate" sub-tab to `site/tracking.html`.
- **Deliverable:** panels W0–W6 + control row (Book/Market/Estimator/Window/Stage, client-side filter); low-N states; "monitoring, not betting advice" footer.
- **Tests:** render smoke on golden `winrate.json`; empty/low-N badges present.
- **Acceptance:** acca point hidden below N=8; band always shown.

### Phase P2 — Repeatable-edge estimator battery (the verdict)

**P2-T1 — rigor stats core** `[after P0+P1-T1]`
- **Files:** new `src/wca/rigor/{__init__,clv,skill,profit,stability,pitfalls}.py` — **each submodule lands with its own test file** (acceptance requires it, else the PR balloons).
- **Deliverable:** cluster-robust consensus-CLV (BCa cluster bootstrap, **always-valid confidence sequence**, sign/Wilson **vs placebo null**, **cost-adjusted ROPE floor**), full-book **Diebold–Mariano** log-loss + Murphy + **calibration-slope regression** + per-class ECE sidecar (debiased, CI'd), ROI BCa+Bayes+Sharpe+power curves + **CLV→P&L quintile spread**, **block-permutation break** (or ARL-calibrated CUSUM)/Pettitt/OOS-IS/decay, **BH–Yekutieli**/Holm/bootstrap-`n_eff`. Cluster id = match / `acca_id` / `team_tournament` (futures → permanent insufficient). `numpy.random.Generator(seed=42)`, B=10,000, resample clusters not rows. Exclude 24 IS matches from inferential gates. Dependency-light.
- **Tests:** synthetic book with injected mean CLV=0.03 → green only past n_eff=25 **and** an outcome-anchored gate passes; null book stays grey/red; **best-price-only artifact book (positive best-price CLV, zero skill) must NOT green**; survivorship test (informative on bets, null on full book → skill gates fail); futures book → permanent insufficient; Wilson/BCa/DM/slope against known values.
- **Acceptance:** all guards (§5) exercised by tests; each submodule has a test file.

**P2-T2 — verdict + build + tab** `[after T1]`
- **Files:** new `src/wca/rigor/{verdict,build}.py`, `scripts/wca_rigor_data.py`, `site/rigor.js`; `site/rigor.json`; add Rigor tab.
- **Deliverable:** §3.4 gate logic → traffic-light verdict (default INSUFFICIENT; **base green requires CLV AND (skill OR calibration) AND stability**; ROI separate, never overrides; stratified by stage; futures permanently insufficient); all blocks pure-SVG; "monitoring, not betting advice" footer.
- **Tests:** verdict logic table-driven across gate combinations incl. **"CLV-only, no outcome gate → not green"**; every block degrades to "insufficient (n=X, need Y)".
- **Acceptance:** verdict can never be greener than the weakest *required* gate, and is never green on CLV alone; samples-to-significance curve renders.

### Phase P3 — Monte Carlo upgrades + MC-driven analytics

**P3-T1 — SE + manifest** `[after P0; cheapest, unblocks intervals]`
- **Files:** `src/wca/sim/tournament2026.py` (**net-new** SE-per-probability, `--target-se` adaptive 5k batches, run manifest); envelope `{value,se,ci95,n_eff}` + `--flat`.
- **Tests:** Wilson SE matches closed form; `--flat` re-emits bare floats; manifest fields present; **existing consumers (`wca_mc_futures.py`, `advancement.py`, `as_dataframe`) unchanged**.
- **Acceptance:** `SimulationResult` gains optional fields defaulting to None/empty; no behavior change at fixed n_sims.

**P3-T2 — CRN + antithetic** `[after T1]`
- **Files:** `src/wca/sim/tournament2026.py` (pre-generated uniform stream, deterministic match-slot indexing, antithetic pairing, default ON).
- **Deliverable:** **reconcile with A5** — marginal SE uses closed form; **scenario-delta SE uses paired-difference variance across the common stream**, not `√(p(1−p)/n)`; manifest records which.
- **Tests:** reproducibility under fixed seed; antithetic halves variance on a monotone functional; scenario delta CI tighter than two independent runs; delta SE uses paired variance (regression against the wrong closed-form).
- **Acceptance:** unbiased (means unchanged within SE); variance reduced.

**P3-T3 — retain_paths** `[after T1; parallel with T2]`
- **Files:** `src/wca/sim/tournament2026.py` (**net-new** optional `alive`/`champion`/`group_pos_per_sim`, gated by `retain_paths=True`).
- **Tests:** bitset marginals match `reach` means; memory ~10 MB @ 40k; default path unchanged when flag off; existing consumers exercised.
- **Acceptance:** `SimulationResult` contract additive; legacy callers unaffected.

**P3-T4 — portfolio P&L distribution (flagship)** `[after T3]`
- **Files:** new `src/wca/mc/pnl.py`; `site/risk_pnl.json`; MC-1 panel; extend `src/wca/exposure.py` (optional `sim_result` arg → `{ev,p5,p95,cvar}` rows).
- **Deliverable:** value open book over all sims (reuse `settle_bet` conventions incl. lay/free-bet, store.py:255), FX-adjust at position level with rate+ts in `meta`, VaR/CVaR/percentiles/per-team contribution; deterministic hard floor preserved + augmented with CVaR; **on-panel FX-display-only disclosure**.
- **Tests:** vectorised settle matches `settle_bet` on a known book; CVaR ≤ hard floor; FX base-currency conversion recorded; £/$ never summed except the disclosed FX view.
- **Acceptance:** reuses one `retain_paths` sim (no extra sim cost); milliseconds post-sim.

**P3-T5 — scoreline atomic draw** `[after T3; validate before defaulting]`
- **Files:** `src/wca/sim/tournament2026.py` (`sample_mode="scoreline"`, reuse seam **`_probs` + `reconcile_scoreline_matrix` (scores.py:129)** wired into `_sample_goals`).
- **Deliverable:** **O/U/BTTS/correct-score** consistency with the card (1X2 is already consistent — do not re-claim it); GD/GF from actual sampled goals.
- **Tests:** sampled **O/U/BTTS/correct-score** match the card within MC SE (the 1X2-already-consistent invariant asserted, not "fixed"); tie-break GD/GF from sampled goals.
- **Acceptance:** behind flag until validated vs card; default stays `outcome` until sign-off.

**P3-T6a — scenarios (conditional + counterfactual)** `[after T3/T4]`
- **Files:** new `src/wca/mc/scenarios.py`, `scripts/wca_mc_scenario.py`, `scripts/wca_mc_seed_scan.py`; `site/mc_scenarios.json`; MC-4 panel.
- **Deliverable:** conditional futures = masked means over retained bitsets; CRN counterfactual deltas + param-sweep tornado (paired-difference SE).
- **Tests:** conditional = masked mean matches re-sim within SE; scenario deltas low-noise via CRN.
- **Acceptance:** one sim run feeds all consumers.

**P3-T6b — calibration (live + historical + anchoring)** `[after T3]`
- **Files:** `backtests/mc_futures_calibration.py`, `backtests/mc_live_calibration.py`; `src/wca/advancement.py` (anchoring + envelope); `src/wca/tracking.py` (live MC calibration); `site/mc_calibration.json`, banded `advancement_history.json`; MC-2/MC-5 panels.
- **Deliverable:** A3 historical Brier/log-loss + temperature `T`; live reliability/Brier-over-time; anchoring takes the **more conservative** of model/anchored on marketed nodes only.
- **Tests:** anchoring uses the more conservative branch; reliability curve renders; envelope shape stable.
- **Acceptance:** additive; legacy `advancement.py` callers unaffected.

**P3-T6c — joint-acca + downstream feeds** `[after T3]`
- **Files:** `src/wca/accas.py` (`joint_settle_prob`), `src/wca/tracking.py` (expected strike rates); `tracking_buckets.json` (`expected` series), `mc_futures.json` envelope; MC-3 forest.
- **Deliverable:** joint acca settle prob (`model_joint` vs `model_indep`) feeding §3.2; expected strike rates feeding §3.2; enveloped forest feed.
- **Tests:** joint acca prob ≠ independent on a correlated bet-builder; expected-strike feed matches sim marginals.
- **Acceptance:** one sim run feeds all consumers.

### Dependency graph (top-level)

```
P0 (T1→T2,T3,T4→T5)  ──▶  P1 (T1→T2 ‖ T3→T4)  ──▶  P2 (T1→T2)
        └──────────────────────────────────────▶  P3 (T1→T2 ‖ T3→{T4,T5,T6a,T6b,T6c})
```
P0 gates everything. P1 and P3-T1/T2/T3 can start in parallel once P0 lands. P2 needs P0 + P1-T1 (full-book CLV inputs). P3-T4 (flagship P&L) needs only P0 + P3-T3. The former monolithic P3-T6 is split into T6a/T6b/T6c (each one reviewable PR).

---

### Key files index

- **New packages:** `src/wca/predledger/`, `src/wca/rigor/`, `src/wca/mc/`; modules `src/wca/winrate.py`, `src/wca/clvbench.py`.
- **Extended:** `src/wca/ledger/reports.py`, `src/wca/tracking.py` + `site/tracking.js`, `src/wca/closecapture.py` (`consensus_close_twoway` — **takes `match_id`**), `src/wca/exposure.py`, `src/wca/accas.py`, `src/wca/advancement.py`, `src/wca/sim/tournament2026.py` (`simulate`/`SimulationResult` gain **net-new** optional params/fields).
- **Reuse verbatim:** `src/wca/ledger/store.py` (`_connect`, `settle_bet`:255 incl. lay/free-bet, `clv=(taken/close)-1` @ :428), `src/wca/closecapture.py` (`consensus_close`:240 **needs `match_id`**, `fair_closing_odds`:196, `selection_leg`:162), `src/wca/tracking.py` (`devig_consensus`:348), `src/wca/models/scores.py` (`reconcile_scoreline_matrix`:129), `src/wca/data/teamnames.py` (`canonical`). **NOT reusable for predictions:** `store.set_closing_odds`:370 (writes `bets` only — mirror the formula in a new writer).
- **New feeds:** `site/predledger.json`, `winrate.json`, `tracking_clv_benchmark.json`, `rigor.json`, `risk_pnl.json`, `mc_scenarios.json`, `mc_calibration.json`; envelope upgrades to `mc_futures.json`, `advancement_history.json`, `tracking_buckets.json`.
- **Result sources:** `data/processed/wc2026_results.json` (1x2/scoreline/ou/btts) · `data/advancement_played_results.json` (advancement). **Not** bare `wc2026_results.json` (does not exist).
- **DB:** `data/wca.db` (mini, canonical) / `data/dev.db` (this MacBook — guard refuses prod-db writes here).

**The one structural bet of this entire design:** persist the full paper book first (P0) — singles in `predictions`, accas in `acca_legs` — then treat **consensus-vs-consensus CLV** as the cheap leading signal that, *only in combination with an outcome-anchored skill/calibration gate*, drives a conservative, peeking-safe, cost-adjusted verdict — with Monte Carlo's retained joint distribution as the single substrate that prices futures, the P&L distribution, scenarios, acca correlation, and exposure as views over one sim run.