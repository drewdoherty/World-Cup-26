# World Cup Alpha — Model Review (regenerated 2026-06-30, corrected)

> **Supersedes the earlier `MODEL_REVIEW_2026-06-30.pdf`**, which had formatting errors and a stale-sample figure ("realized 3.000 / n=31"). Every number here was verified against real repo data this session; nothing is invented. Where data is missing it is labelled, not faked.

---

## 1. Executive summary

- **The fix (A1):** the Dixon-Coles goal-level intercept `mu` was under-forecasting WC goals. Corrected by a single scalar shift (`mu` 0.205 → 0.383) that anchors the model's mean total on the WC fixtures to a conservative recent-WC base rate (2.81). 1X2-preserving; backward-compatible (default-off flag).
- **Verified impact (73 played matches, 2026-06-11→06-28):** realized mean total **2.959**; model **OLD 2.441 (bias −0.518)** → **NEW 2.917 (bias −0.042)** — the anchor removes ~92% of the under-forecast.
- **What it changes in outputs:** total xG, Over 2.5 and BTTS rise on every fixture; the 1X2 is ~invariant (it's a totals correction, not a 1X2 change). Edge it unlocks is in goal-derived **match-event markets**, not liquid winner markets.
- **Status:** all work is on branch `integrate/fixes-2026-06-30` (full suite **2052 passed, 2 skipped**). **Nothing is on `main`/production yet** — so the live model still under-forecasts until merged.

---

## 2. The xG/totals fix — before vs after (verified)

Remaining-knockout fixtures, neutral venue. OLD = production level (`mu` 0.205); NEW = WC-anchored (`mu` 0.383). Computed by running the real fitted model both ways (`contrast.csv`).

| Fixture | Total xG (old→new) | P(Over 2.5) | P(BTTS) |
|---|---|---|---|
| France v Sweden | 2.42 → 2.89 | 43% → 55% | 45% → 54% |
| Mexico v Ecuador | 2.03 → 2.43 | 33% → 44% | 41% → 50% |
| England v DR Congo | 2.44 → 2.92 | 44% → 56% | 32% → 38% |
| Spain v Austria | 2.64 → 3.16 | 49% → 61% | 44% → 52% |
| Argentina v Cape Verde | 2.62 → 3.13 | 49% → 60% | 30% → 36% |
| Belgium v Senegal | 2.36 → 2.82 | 42% → 54% | 46% → 54% |
| Portugal v Croatia | 2.32 → 2.78 | 41% → 52% | 46% → 55% |
| USA v Bosnia & Herz. | 2.30 → 2.75 | 40% → 52% | 46% → 55% |
| **Mean** | **2.39 → 2.86** | **42.8% → 54.3%** | — |

![Before/after: expected total goals (xG)](CHART_XG)

![Before/after: P(Over 2.5)](CHART_OVER)

**1X2 is preserved:** the `mu` shift adds the same constant to both teams' log-rates, so the supremacy log-ratio is **exactly invariant** (Δ = 3.3e-16); the matrix-implied 1X2 moves ≤ ~4pp only via the non-linear low-score (tau) correction.

**Anchor context (verified historical WC means, `martj42_cleaned`):** WC 2018+2022 = 2.833; since-2010 = 2.864 (n=3801); all pre-2026 = 2.907. The 2.81 target is a deliberately conservative choice, slightly below these.

### Caveat — is the anchor durable?
It **re-applies on every refit** (not a frozen number), so it keeps enforcing 2.81. **But it's a static stopgap:** it targets a *fixed* 2.81, not the live level (realized WC2026 = 2.96), and doesn't learn beyond the negligible drift of refitting (90 WC rows in ~49,500). The −0.04 was measured on **group-stage** matches and partly relies on host advantage lifting host games above 2.81 — as **hosts are eliminated in the knockouts** and the scoring regime shifts, the bias will likely re-open. **Durable fix = the F7 opponent-adjusted decay+long-term blend** (self-updates with results), or making the anchor target a rolling recent rate. Treat the anchor as a band-aid; validate F7 OOS before sizing off it.

---

## 3. Under-the-hood changes this session (all on `integrate/fixes-2026-06-30`)

| Change | What | Status |
|---|---|---|
| **A1** xG/totals anchor | `mu` 0.205→0.383; shared `apply_wc_level_anchor`; default-off flag | green |
| **F3** stale results | `wc2026_results.json` **31→73** matches (authoritative `martj42_cleaned`); new builder, wired into clean-results | green |
| **F4** snapshot staleness guard | `odds_snapshots` reads now age-checked (was read 7-day-stale unguarded at `accas.py:1302`) | green |
| **F5** honest exposure metrics | removed hardcoded `p_profit`/`p_win_50` constants; currency-coherent best/worst (GBP vs USD split) | green |
| **F6** live-ledger exposure | `bet_recs.json` open exposure from the live ledger: **n_open 59→8** | green |
| **F7** goal-blend | opponent-adjusted tournament-decay + long-term DC blend — **flag-gated, tracking-only, OOS-gated** | green (+13 tests) |
| **F8** match-event models | new `ShotsOnTargetModel` + `FoulsModel`; corners de-hardcoded; cards refit — fallback-guarded | green (+24 tests) |
| Durable closing-line capture | DB-less `odds_price_history.jsonl` mirror + idempotent ingest + fail-loud workflow | green |
| A6 docs | corrected stale "feed revoked" claims | green |

Full suite: **2052 passed, 2 skipped.** Backward-compatible: F7/F8 default off ⇒ production bit-identical until validated.

---

## 4. Integrity audit — fabricated/stale data found (12 high-risk, all verified)

| Issue | Where | Fixed by |
|---|---|---|
| xG fix not in production | `card.py:604-610` (no anchor) | A1 (on branch; needs merge) |
| `wc2026_results.json` stale (31, frozen 06-20) drives settlement + metrics | `predledger/settle.py`, `winrate.py`, `rigor/build.py` | F3 |
| `odds_snapshots` 7 days stale, read unguarded | `accas.py:1302`, `closecapture.py` | F4 |
| hardcoded `p_profit`/`p_win_50` shown as live risk | `exposure_dashboard.py:88,91` | F5 |
| best/worst mix GBP+USD, labelled GBP | `exposure_dashboard.py:75-84` | F5 |
| `bet_recs.json` stale 59-bet exposure vs real 8 | `site/bet_recs.json` | F6 |
| `players.json` scorer shares all `analyst_estimate` (238) | `data/players.json` | data-gated (labelled, not fixed) |

The only fabrication that had reached a deliverable was the earlier PDF's "realized 3.000 / n=31" (a stale-file artifact) — corrected here. Adversarial verifier reproduced all 12 findings; none overstated.

---

## 5. Model vs market

The model is a good 1X2 **calibrator** (ECE 0.083, Brier 0.150, n=825) but **not a winner-market alpha source**: its large disagreements with the liquid Polymarket book — Brazil **18.8%** model vs **7.1%** market to win; France 12.9% vs 28% — are **model error, not edge** (the book is efficient: Winner sums to 0.985). The repo's own backtest (n=64) shows no confident edge over market-only. **Edge lives in less-efficient match-event markets + execution/cost**, which is what A1/F7/F8 target.

## 6. Conditional bracket (as-if-open)

Real fitted model, 50k sims, conditioned on the 4 played R32 results. Pure-model MAP champion **Spain** (marginal title leader is Brazil 18.8%, but Brazil loses the modal SF coin-flip to Argentina ~0.50). **P(perfect-from-here) ≈ 1.7e-5** (~1 in 60,000); risk concentrated in 5 near-coin-flip nodes. A *market-anchored* completion would instead crown **France** (28% favourite) — consistent with §5.

## 7. OddsAPI credit usage

**3,793 used / 96,207 remaining** of ~100k monthly (verified live). Cost = 1 credit × markets × regions per call; `/sports` free. Dominant spender: the snapshot daemon (315 capture cycles, 06-11→06-23, markets h2h/totals/btts/h2h_lay) — then the hourly scores cron (`h2h`, 1 credit/call) and the daily-card scorer-event fetches. Capture **stopped 06-23** (daemon died, since restarted), so the last week burned little. *(No per-request credit log exists, so per-source split is the cost-model × observed cadence, not an itemized ledger.)* Runway is ample; finer near-KO capture for true closing lines will raise burn (a budget governor exists in `pollsched`).

## 8. Caveats & what is NOT done

- **Nothing merged to `main`/pushed** — all on `integrate/fixes-2026-06-30` (push + PR pending).
- **F7 goal-blend and F8 models are tracking-only / OOS-gated** — they do not size real money until they clear an out-of-sample CLV gate (per the project's own rule). Shrinkage knobs are design defaults, not validated.
- **Squad adjustment is data-gated** (`squads.json` covers 2/48 teams; `players.json` is analyst-estimated shares) — implemented as an honest no-op hook, no fabricated squad strengths.
- **`prop_priors.csv` absent in a fresh checkout** (gitignored local artifact) — F8 models run on documented fallback constants until the pipeline produces it.
- Anchor durability caveat (§2): expect the −0.04 to re-open in the knockouts.

## 9. Where things live

- This report: `docs/research/wca_alpha_2026/MODEL_REVIEW_2026-06-30.{md,pdf}`.
- Also: `SESSION_REPORT_2026-06-30.{md,pdf}`, `INTEGRITY_AUDIT_AND_GOALMODEL.md`, `SWARM_LEDGER.md`, the `00–15` dossier.
- Branch `integrate/fixes-2026-06-30` (+13 commits ahead of `main`, full suite green, not pushed).
