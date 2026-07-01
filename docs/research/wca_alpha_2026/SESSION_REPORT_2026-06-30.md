# Session Report — Changes, Caveats & State (2026-06-30)

**Read this first. Every number here was verified against real repo data/tests this session; where something is unverified or not-yet-done it is labelled as such. Nothing is invented.**

> **One-line state:** all work is on **review branches** — *nothing is on `main` or in production*. The live system still has the bugs the audit found until these branches are merged.

---

## 1. TL;DR

- The **xG/total-goals fix is built but NOT deployed.** Production `card.py` still fits the Dixon-Coles level freely, so it **under-forecasts goals by ~0.5/match** right now (feeds every totals/BTTS/scorer EV).
- An **integrity audit (12 high-risk findings, all independently verified, zero fabrication)** found stale data shown as current and hardcoded "risk" numbers on the site dashboard.
- The only true fabrication risk that reached a deliverable was a **naive "realized 3.000 / n=31" line in the earlier model-review PDF**; it has been replaced with verified, opponent-adjusted figures (below). That PDF (`MODEL_REVIEW_2026-06-30.pdf`) is **superseded by this report.**
- The **swarm was cleaned up** (167→85 branches, 36→25 worktrees, history preserved as 19 `archive/*` tags).
- The **data-usage/lineage report was deliberately not generated** (you asked to hold it until fixes land).

---

## 2. Changes made (all on branches; none merged to `main`)

| Branch | What it does | Tests | Status |
|---|---|---|---|
| `harden/xg-totals` | **A1:** Dixon-Coles WC-slate level anchor (`mu` 0.205→0.383) — corrects the goal under-forecast; 1X2-preserving; backward-compatible (default-off flag); shared helper so card/recompute/recalibrate can't drift | full suite green | ready to deploy |
| `feat/closing-line-capture` | **Durable closing-line capture:** DB-less `odds_price_history.jsonl` mirror + idempotent ingest + fail-loud `hourly-odds.yml` (so capture can't silently die again) | green (13 new tests) | ready |
| `harden/matchevents-pipeline` | **A2:** historical match-event data pipeline (football-data + StatsBomb → prop_priors) | green | ready |
| `harden/docs-feed-correction` | **A6:** corrected stale "feed revoked/blocked" claims in docs (the key is live) | green | ready |
| `chore/swarm-cleanup` | Swarm audit ledger + executed cleanup (branches/worktrees retired) | n/a (docs) | done |
| `integrate/fixes-2026-06-30` | **Integration of the four above (+10 commits, 20 files, +2269/−120); full suite green** (the one "failure" seen earlier was my pytest CWD artifact, confirmed passing from repo root) | green | **the merge candidate** |

**Daemon:** the mini `snapshotd` was restarted by you, so live odds/closing-line capture is flowing again.

---

## 3. Caveats — what is still broken on LIVE `main` (verified by the integrity audit)

These are real and money-relevant. They remain until fixed/merged.

| # | Issue | Where (verified) | Why it matters |
|---|---|---|---|
| 1 | **xG fix not in production** — `dc_level_target` exists only on the branch | `src/wca/card.py:604-610` (no anchor) | live model under-forecasts goals ~0.5/match → all totals/BTTS/scorer EV biased low |
| 2 | **`wc2026_results.json` stale** (31 matches, frozen 06-20) used as current; authoritative source has 73 played to 06-28 | `data/processed/wc2026_results.json`; read by `predledger/settle.py`, `winrate.py`, `rigor/build.py` | settlement + all model-quality metrics computed on a stale partial sample |
| 3 | **`odds_snapshots` 7 days stale** (max ts 2026-06-23) read **without a staleness guard** | `src/wca/accas.py:1302`, `closecapture.py` | EV/CLV can silently use week-old prices |
| 4 | **Hardcoded "risk" metrics shown as live** — `p_profit` (0.6/0.4), `p_win_50` (0.3/0.1) | `src/wca/exposure_dashboard.py:88,91` → site | fabricated-looking numbers presented to you as computed |
| 5 | **`best_case`/`worst_case` mix GBP + USD** into one figure, labelled GBP | `exposure_dashboard.py:75-84` | exposure numbers are not currency-coherent |
| 6 | **`bet_recs.json` ships stale exposure** (59 open bets, worst −1096) vs the real **8** open bets | `site/bet_recs.json` | site shows exposure that doesn't match the ledger |
| 7 | **`players.json` scorer shares are all `analyst_estimate`** (238 players), no empirical xG — sets scorer fair odds | `data/players.json` | scorer/anytime markets run on estimates, not data (documented, but feeds odds) |

*(Full audit: `INTEGRITY_AUDIT_AND_GOALMODEL.md` — 12 high / 9 medium / 15 low, every finding cites file:line and was adversarially re-verified.)*

---

## 4. Verified numbers (these replace the earlier fabricated PDF line)

- **Matches played (authoritative `martj42_cleaned.csv`):** **73** (2026-06-11 → 06-28). *(The "n=31" came from the stale `wc2026_results.json`.)*
- **Realized mean total goals:** **2.959** (216/73). *(The earlier "3.000" was 93/31 from the stale file — real but stale & partial.)*
- **Model vs realized (per-fixture, opponent-adjusted — the correct benchmark, not naive averages):**
  - OLD (production level): mean expected total **2.441** → bias **−0.518**
  - NEW (WC-anchored): **2.917** → bias **−0.042** (anchor removes ~92% of the bias)
- **Historical WC base rates** (`martj42_cleaned`): 2018+2022 = **2.833**, since-2010 = **2.864** (n=3801), all pre-2026 = **2.907**. *(The "2.81" anchor is a deliberately conservative choice, slightly below these — NOT "the since-2010 rate" as the old PDF wrongly claimed.)*
- **OddsAPI credits:** **3,793 used / 96,207 remaining** (~3.8% of the monthly pool).

---

## 5. Methodology correction you directed (goal model)

Naive "avg goals so far" is **not** a valid knockout forecast input (matchups don't repeat; opponent quality confounds it). The right per-team goal expectation = a **blend of tournament-decayed + longer-term Dixon-Coles** attack/defence (already opponent-adjusted), squad-adjusted, evaluated **against the specific next opponent's ELO + their own blended rate**. Designed + prototyped (`INTEGRITY_AUDIT_AND_GOALMODEL.md`). **Caveat:** squad adjustment is **data-gated** (`squads.json`/`players.json` coverage is thin — cannot be faked); and per your own rule, this model must **clear an out-of-sample CLV gate before it sizes real money** — so it ships **tracking-only** until validated.

---

## 6. What is NOT done (honest)

- **Nothing merged to `main` / pushed.** All changes await your review of `integrate/fixes-2026-06-30`.
- **Integrity fixes F3–F6 (sec. 3 items 2–6) are diagnosed but NOT yet implemented** as code.
- **Goal-model blend (F7) and match-event models A3–A5 (F8)** are designed/prototyped, not production-built; both are OOS/data-gated.
- **Data-usage/lineage report** not generated (held per your instruction).

---

## 7. Where everything lives

- This report: `docs/research/wca_alpha_2026/SESSION_REPORT_2026-06-30.md` (+ `.pdf`).
- Audits/designs: `INTEGRITY_AUDIT_AND_GOALMODEL.md`, `SWARM_LEDGER.md`, `00_foundation.md`, `02_codebase_audit.md`, `08_xg_and_totals.md`, etc. (same folder).
- Superseded: `MODEL_REVIEW_2026-06-30.pdf` (had the naive 3.000/n=31 line).

## 8. Recommended next safe actions (your call)

1. **Review + merge `integrate/fixes-2026-06-30`** → deploys the xG fix + durable capture + matchevents (the highest-value, tested changes).
2. Let me implement **F3–F6** (the integrity fixes) on that branch, tested, then push.
3. Build **F7/F8** as a validated (OOS-gated) pass.
4. Then regenerate the **data-usage/lineage report** against the fixed state.
