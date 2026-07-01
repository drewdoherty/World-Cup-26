# WCA Alpha 2026 — Foundation Dossier

**Program:** World Cup Alpha 2026 — Bracket + Trading Research
**As of:** 2026-06-30
**Status:** Phase 0 (verification) complete; Phase 2 (codebase audit) summarized. Read-only. No trades placed, no DB written.
**Confidence on competition rules:** MEDIUM. Several load-bearing rule facts are UNVERIFIED (see §2). This dossier deliberately preserves that uncertainty and makes no profitability claim.

---

## 1. Executive Summary

- **The Polymarket "Perfect Bracket" entry deadline HAS PASSED.** `polymarket.com/perfect` now reads "The contest is closed." The FIFA Round of 32 began 2026-06-28, and entries necessarily locked on/before the first knockout match. As of 2026-06-30, entry is closed. *(Exact deadline timestamp/timezone is UNVERIFIED — inferred to be on/before 2026-06-28.)*
- **No bracket was ever submitted by us, and none exists in the repo.** Exhaustive search of code, `data/`, `docs/`, `reports/`, the bets ledger, and `ChatExport_2026-06-25/` found zero saved-picks artifact. No `bets` row is a bracket-contest entry. `docs/research/wca_alpha_2026/` was empty before this file.
- **Implication for Objective A (live bracket optimization for entry): DEAD.** We cannot enter the $2M Perfect Bracket contest — the window closed and we never submitted. Any "build the optimal bracket to win $2M" objective is now moot for *this* contest cycle.
- **Implication for Objective B (retrospective + remaining-match trading): LIVE and primary.** The bracket-optimizer machinery still has full value as (i) a **retrospective** exercise — what bracket *would* the model have picked, and how is it scoring vs. realized R32 results — and (ii) a **pricing engine** for the remaining knockout markets (R16 → Final) that Polymarket and the books still trade.
- **Implication for Objective C (hedging existing exposure): LIVE and urgent-ish.** We hold 8 open bets, several of which reference teams already eliminated or matches already played. These need a clear-eyed settle/hedge review (see §4).
- **The tournament is mid-R32.** Group stage is fully resolved in the repo (qualified teams at P(R32)=1.00). R32 ran 2026-06-28 → ~2026-07-02. R16 Jul 4–7, QF Jul 9–11, SF Jul 14–15, Final Jul 19 (MetLife, NJ).
- **Major upsets already reshaped the bracket (web-sourced, NOT in repo):** Germany (beaten by Paraguay on pens) and Netherlands (beaten by Morocco on pens) are OUT, along with South Africa and Japan. These eliminations materially reopen the relevant half of the bracket. **Repo data does NOT yet corroborate any knockout result** — `advancement_played_results.json` holds only the 24 fixed group-stage scorelines.
- **The codebase already gives us ~80% of a bracket engine for free:** the exact 2026 bracket topology (`R32_TIES`, `KNOCKOUT_FEED`, official thirds allocation), a working vectorised Monte-Carlo tournament sim, a calibrated `prob_fn(a,b,knockout)`, fitted Elo+Dixon-Coles models on disk, and current per-team stage probabilities. See §4–§5.
- **The single missing primitive is the JOINT path distribution.** `TournamentSimulator._run_knockout` computes per-sim match winners but discards them, returning only marginal `reach[]`/`win` counts. A bracket scorer needs the `(n_sims × n_matches)` winner matrix. Exposing it (`return_paths=True`) is the one change that unlocks both bracket scoring and correlated parlay pricing. No optimizer, no contest-scoring model, and no concrete-fixture enumerator exist yet.
- **Three rule facts that would change strategy are UNVERIFIED:** exact scoring/ranking of imperfect brackets, whether a best/closest-bracket consolation prize exists, and tie-break rules. Do not assume a consolation prize exists. Do not borrow Kalshi NCAA-contest exclusions — those are a different contest.
- **Live data pipeline is partially stalled.** `odds_snapshots` newest row is 2026-06-23 (group stage); `ODDS_API_KEY` is flagged revoked; Betfair exchange is dormant (no creds). Any *live* remaining-match trading requires restoring a feed first. Polymarket price series is not persisted at all.
- **Bottom line:** Objective A is closed by deadline. Pivot the program to B (retrospective scoring + remaining-knockout/advancement/event pricing) and C (hedge the 8 open bets), and use the freed bracket-sim work as the joint-probability engine that B's remaining-match and advancement pricing both need.

---

## 2. Phase 0 — Verified Competition Rules

Official name: **WC26 $2M Bracket Contest** (Polymarket "Perfect Bracket" challenge for the 2026 FIFA World Cup). Marketed as "win $2 million cash if you predict a perfect bracket."

| Field | Finding | Confidence |
|---|---|---|
| **Eligibility** | Official copy says "Free to enter." Specific exclusions (states, age, KYC, geo) NOT retrievable (rules page closed; X announcements behind HTTP 402 paywall). | UNVERIFIED |
| **Entry deadline** | UNKNOWN exact timestamp/tz. Inferred on/before 2026-06-28 (FIFA R32 start). `polymarket.com/perfect` now reads "contest is closed." | **Deadline PASSED (verified closed); exact time UNVERIFIED** |
| **Deadline passed?** | **YES** — page confirms closed as of 2026-06-30. | Verified |
| **Entries allowed/user** | Unknown (commonly one per verified account, but not confirmed here). | UNVERIFIED |
| **Scoring** | Headline mechanic: predict every knockout result (R32 → Final) correctly to win $2M. Per-round point allocation and ranking of imperfect brackets NOT retrievable. | UNVERIFIED (mechanic only) |
| **Perfect-bracket prize** | **$2,000,000 USD** cash. | **Verified** (polymarket.com/perfect + @Polymarket) |
| **Best/closest-bracket prize** | No consolation/runner-up/closest-bracket prize could be confirmed. Do NOT assume one exists. (Separate `$50` BRACKET sign-up promo is a trading incentive, not a placement prize.) | UNVERIFIED — assume none |
| **Tie-breaks** | How multiple perfect/tied brackets split the prize: not retrievable. | UNVERIFIED |
| **Amendment / edit-before-lock** | Edit/resubmit rules and Polymarket's right to amend/cancel: not retrievable. | UNVERIFIED |
| **Settlement / resolution source** | Polymarket WC markets resolve on "official, verifiable" FIFA results. Knockout ties: 90 min → 30 min ET → penalties; advancing team is the settled pick (FIFA-corroborated). A contest-*specific* settlement clause was not found verbatim — inferred from PM's standard market-resolution language + FIFA rules. | PARTIALLY VERIFIED (inferred) |

**UNVERIFIED — needs user confirmation (blocking-grade items in bold):**
1. Exact entry deadline date/time/timezone.
2. Eligibility/exclusion details (states, age, KYC, geo). Do NOT borrow Kalshi NCAA exclusions.
3. Number of entries allowed per user.
4. **Exact scoring system / per-round point values / how imperfect brackets are ranked.**
5. **Whether any best-/closest-bracket consolation prize exists, and its amount.**
6. Tie-break rules (splitting prize among perfect/tied brackets).
7. Amendment / edit-before-lock / contest-cancellation rules.
8. Contest-specific settlement clause (FIFA-results + ET/penalties language) — currently inferred, not verbatim.

> Note: rules pages are now closed and X announcements are paywalled (HTTP 402); Wayback had no usable snapshot. Items 4–5 are the ones most likely to change strategy *if* the program ever pivots to a future/closest-bracket-style contest — but for *this* (closed) contest they no longer block anything.

Sources: `polymarket.com/perfect`, `@Polymarket` status posts (402), `polymarket.com/sports/world-cup`, Wikipedia 2026 knockout stage, FIFA canadamexicousa2026 schedule.

---

## 3. Phase 0 — Tournament State on 2026-06-30

- **Round:** Round of 32 (new-for-2026 first knockout round), **in progress**. Group stage complete.
- **R32 window:** Jun 28 – Jul 2, 2026 (ET). Repo `.ics` is in Bahrain time (UTC+3), so US-evening matches shift +1 calendar day (window reads Jun 28 – Jul 4 there). Reconcile per-match local date before use.
- **Played R32 (web-sourced, NOT in repo):**
  - Match 73: Canada 1–0 South Africa
  - Match 74: Paraguay (1–1, 4–3 pens) **Germany** — major upset, **Germany OUT**
  - Match 75: Morocco (1–1, 3–2 pens) **Netherlands** — upset, **Netherlands OUT**
  - Match 76: Brazil 2–1 Japan — **Japan OUT**
  - First four eliminated from the expanded bracket: **South Africa, Germany, Netherlands, Japan.**
- **Current/next fixtures (Jun 30 ET onward):** Ivory Coast–Norway (M78, Arlington), France–Sweden (M77, MetLife), Mexico–Ecuador (M79, Azteca); then Jul 1: England–DR Congo, Belgium–Senegal, USA–Bosnia; Jul 2: Spain–Austria, Portugal–Croatia; Jul 2–3: Switzerland–Algeria, Argentina–Cape Verde, Colombia–Ghana, Australia–Egypt.
- **Key dates:** R16 Jul 4–7 · QF Jul 9–11 · SF Jul 14–15 · 3rd-place ~Jul 18 (repo `.ics` Jul 19 Bahrain) · **Final Jul 19, 2026, MetLife Stadium, East Rutherford NJ.**
- **Notable group-stage eliminations (fixed in repo):** Uruguay, Iran, Turkey, South Korea, Czech Republic, Saudi Arabia all out. **Cape Verde** and **DR Congo** advanced as surprise qualifiers.
- **Caveats:** R32 results are web-only and NOT corroborated by repo data. R16/QF/SF matchups beyond known R32 winners are still bracket placeholders ("Winner Mxx"). Removal of Germany + Netherlands materially reopens the relevant half of the bracket.

---

## 4. Phase 0 — Submission State & Existing Exposure

**Submission state:** No bracket submitted. No saved-picks artifact anywhere in the repo. `data/advancement_latest.json` (the lone `data/` hit on "bracket") is mislabeled — it is a Markdown advancement-edges report, not a picks set, and fails `json.load`. `ChatExport_2026-06-25/` contains only a `.DS_Store`. Conclusion: **the program never entered the contest.**

**Open exposure — 8 open bets (read-only; no P/L claimed; stake currency NOT recorded in schema).** Confirmed live against `data/wca.db` (`bets WHERE status='open'`):

| id | match_desc | selection | platform | odds | stake | acct |
|---|---|---|---|---|---|---|
| 11 | Golden Boot outright | Harry Kane (England) | betfair_sportsbook | 7.5 | 10 | 1 |
| 14 | Japan reach R16 | **Japan reach R16 — No** | polymarket | 1.6667 | 60 | 1 |
| 57 | Ghana elim R32 | **No — Ghana not eliminated in R32** | polymarket | 1.4708 | 1 | 2 |
| 88 | England vs Ghana | England −2.5 + handicap + correct score | betfair_sportsbook | 5.52 | 5 | 1 |
| 94 | Belgium vs Iran | Belgium win + Lukaku to score | paddypower | 2.37 | 10 | 1 |
| 99 | Ronaldo/Kane/Diaz treble | Player 1+ SOT (boosted) | betfred | 5.05 | 10 | 1 |
| 100 | England vs Ghana | England HT/2H + Kane/Bellingham SOT acca | betfred | 7.9 | 10 | 1 |
| 101 | 4-fold 2UP (Ger/IvCoast/Neth/Japan) | FT 2UP acca | virginbet | 3.37 | 50 | 1 |

Total open stake = **156.0 units** (currency unspecified — schema has no currency column; `stake` is a bare REAL).

**Exposure notes that matter for Objective C (hedging):**
- **Bet 14 (Japan reach R16 — No):** Japan lost R32 to Brazil (web). This looks like a **winner** pending settlement — confirm and settle.
- **Bet 101 (2UP 4-fold incl. Germany, Netherlands, Japan):** references already-played group fixtures (Ecuador–Germany, Tunisia–Netherlands, Japan–Sweden) — this is a **group-stage acca**, not a knockout position. Already-resolvable; settle don't hedge.
- **Bets 88/99/100 (England–Ghana, etc.):** England–Ghana is not a current fixture (England plays DR Congo in R32, Colombia–Ghana is the Ghana tie). These appear to be **group-stage builders** — verify match status before treating as live.
- **Bet 57 (Ghana not eliminated R32):** Ghana plays Colombia in R32 (M87, not yet played) — this is **genuinely live** and the model can price it directly via `prob_fn`.
- **Bet 11 (Golden Boot, Kane):** live outright; England still in.
- Currency mismatch (GBP sportsbook vs USD Polymarket) is real and unmodeled; hedging across pools needs an FX-aware netting step.

**What the codebase gives us for free toward a bracket entry:**
- **(A) Bracket STRUCTURE:** `src/wca/sim/tournament2026.py` hard-codes `R32_TIES` (matches 73–88), `KNOCKOUT_FEED` (R16→Final), `ROUND_MATCHES`, `THIRDS_SLOT_WINNERS`, and `thirds_assignment()`/`THIRDS_ALLOCATION` (official FIFA 495-combo thirds table).
- **(B) Monte-Carlo engine:** `TournamentSimulator.simulate` plays the full bracket per sim, crediting reach/win. Per-match winners ARE computed in `_run_knockout` (≈l.836–871) but discarded (≈l.828–834).
- **(C) Calibrated primitive:** `src/wca/advancement.make_prob_fn` → `prob_fn(a,b,knockout) -> (p_a,p_draw,p_b)` (Elo + Dixon-Coles blend, host adjustment, ET/penalty skill bias). Exactly the per-tie primitive an optimizer calls.
- **(D) Fitted models on disk:** `data/advancement_models.pkl`, `data/dc_params_corrected.json`, `data/elo_ratings_corrected.json`.
- **(E) Current stage probabilities:** `data/advancement_current_vs_pretournament.{json,csv}` — per-team P(R32…Final), P(win), P(group_winner) + deltas vs pre-tournament.
- **(F) Played group results:** `data/advancement_played_results.json` to seed mid-tournament state.
- **(G) Drivers:** `scripts/wca_advancement.py`, `wca_advancement_data.py`, `wca_structure.py`.
- **Still missing (the 3 things):** (i) expose per-sim `match_winner` / pairwise conditional win-probs, (ii) a slot-pick selector + contest-scoring optimizer, (iii) a thin writer to emit chosen picks. None exist.

---

## 5. Phase 2 — Codebase Data-Flow Audit Summary

**Live pipeline (happy path):** `raw → clean → corrections → features → Elo + Dixon-Coles → devig (Shin) → blend (0.10/0.30/0.60 Elo/DC/market) → score-matrix → market-compare → EV → fractional-Kelly sizing → ranked card → cache/ledger`. A parallel post-trade branch reads the ledger for exposure/CLV.

**Per-subsystem one-liners:**
- **forecasting-core:** Elo + Dixon-Coles + structural priors + venue/host adjustment → blended 1X2 → score-matrix → 2026 Monte-Carlo tournament sim → advancement/outright edges vs Polymarket. *Strongly enables a bracket optimizer; the joint-path distribution is the one missing output.*
- **derivative-models-props (corners/cards/scorer/bet-builder/accas/boosts):** post-blend consumers of DC lambdas pricing match-event/prop markets. *Weakly relevant to bracket; HIGH relevance to remaining-knockout event trading. `players.db` doesn't exist on disk → bet-builder runs on priors.*
- **markets-sizing (devig/Kelly/card/exposure-corr):** owns devig→blend→EV→sizing + correlated same-fixture P&L. *Reusable as-is for per-match knockout trading; has NO tournament-sim and NO match-WIN (ET/pens) model — both needed for bracket.*
- **arbitrage-crossvenue:** settlement-guarded cross-venue arb + matched betting. *`settlement_key` REFUSES advancement/outright markets by design — not reusable for bracket directly, but the settlement-discipline idea is the most valuable transferable concept. Build on the sim + `advancement.py` instead.*
- **polymarket-stack:** Gamma/CLOB read + sizing + guarded trader (PM_DRY_RUN-gated) + cash-out. *h2h knockout trading works end-to-end. Missing: a bracket-leg resolver mapping per-(team,round) advancement YES tokens to model survival probs; advancement/event sizing; ≥2 snapshot cadence for convergence.*
- **data-layer:** two odds spines (snapshot daemon via `theoddsapi` write; `odds_source` card read) + results spine (martj42 → reconcile → Elo/DC) + player/prop spine + `teamnames.canonical` glue. *Ingestion stalled (newest snapshot 2026-06-23; key revoked; Betfair dormant). PM price series not persisted.*
- **ledger-analytics-rigor:** money ledger (`record_bet` choke point + CLV) + paper prediction ledger + rigor verdict battery + CLV/venue/card benchmarks. *`predledger` already models `advancement` as a first-class row type; settlement of advancement is stubbed; CLV close-stamping is 1X2-only. Rigor correctly caps a single tournament at ~1 cluster → futures can't self-validate within-tournament.*
- **intel-microstructure:** live feed/poller/arb/metrics/normalise + news/closecapture/linemove. *`odds_snapshots` holds only h2h/totals/btts/h2h_lay — no outright/advancement/event ingest, so advancement+event trading is blind without new ingest. `market_snapshots`/`market_metrics` tables absent from prod DB.*

**Reusable for the bracket vs stale/duplicated/disconnected:**
- *Reusable:* tournament topology + sim + `make_prob_fn` + fitted models + current stage probs (forecasting-core); `exposure_corr.scoreline_matrix`/`settle_on_scoreline` as a correlated-settlement template; `predledger` advancement schema + rigor verdict framework; PM read/sizing/guarded-trader for execution; `teamnames.canonical` as mandatory name glue.
- *Stale/disconnected:* `structural.outright_divergence` (tests-only, never wired live); `scripts/wca_exposure_sizer.py` (frozen group-stage slate, regex card parse, silent no-op on new fixtures); `intel/store.append_metrics` (dead code; `market_metrics` table never created); `data/betfair.py` thin client duplicating `betfair_exchange.py`; `exposure_dashboard.py` crude duplicate of `exposure.build_exposure_data`.
- *Duplicated math:* host-advantage logic in `advancement.make_prob_fn` vs `venues.host_advantage_points`; `ScorerPricer.intensity` byte-for-byte of `AnytimeScorerModel._intensity`; three Wilson/Brier/CLV reimplementations across rigor/clvbench/bench.

**Concrete capability gaps a bracket-optimizer needs:**
1. **Joint path distribution** — `simulate(..., return_paths=True)` returning the `(n_sims, match_no→winner)` matrix. *Single highest-leverage change.*
2. **Match-WIN (incl. ET/pens) model** — collapse 1X2 draw mass into a shootout/ET winner split (`match_win_probs` helper next to `elo_probs`/`dc_probs`). Currently no code does this; `et_skill_weight` is an untuned 0.5 default and `advancement` doesn't even pass it.
3. **Concrete-fixture enumerator** — emit the current named R32→Final fixtures from played results + thirds allocation, so a bracket can be filled against real ties.
4. **Bracket pick type + contest-scoring optimizer** — represent a bracket over `R32_TIES`/`KNOCKOUT_FEED`; score = mean-over-sims of all-picks-correct (joint, so correlation is free); seed with chalk (per-slot modal team) then beam/greedy-swap to maximise expected contest score / EV.
5. **Advancement-leg resolver + sizing path** — map PM per-(team,round) YES tokens to model survival probs; size via the existing `build_pm_proposals` core; persist PM advancement price series for CLV.
6. **Advancement settlement + non-1X2 CLV close** — wire `predledger.settle` advancement and generalise `stamp_closes` so the markets we'd trade get graded.

---

## 6. Open Questions for the User (blocking the next phase)

1. **Is Objective A formally dead?** The contest is closed and we never entered. Confirm we drop "enter the $2M bracket" and re-scope to retrospective scoring + remaining-market trading + hedging. *(This is the one decision that determines the whole next phase.)*
2. **Live trading feed:** Do you want remaining-knockout/advancement/event *live* trading at all? If yes, restoring an odds feed (`ODDS_API_KEY` revoked; Betfair exchange dormant) is a prerequisite — confirm we should fix ingestion before any pricing is treated as actionable.
3. **The 8 open bets:** Should I do a full settle/hedge pass now? Several (14, 101, and likely 88/99/100) reference already-played matches and should be settled, not hedged. Confirm I can read the ledger settlement state and produce a netting view (no writes).
4. **Currency / bankroll:** `stake` has no currency column. Which pool is which (GBP sportsbook vs USD Polymarket), and what FX rate should a cross-pool hedge use?
5. **Retrospective scope:** For Objective B's retrospective, do you want (a) the model's would-be bracket scored against realized R32 results as a model-validation exercise, and/or (b) a forward P(exact remaining bracket) for the still-open ties?

*(Rule items in §2 — scoring detail, consolation prize, tie-breaks — are noted but no longer block anything for this closed contest. They'd only re-block if you pivot to a future contest.)*

---

## 7. Next-Phase Plan

**Workflow 2 — Objective functions + forecasting/markets (foundation for B):**
- Land the **joint-path primitive**: extend `TournamentSimulator._run_knockout` with `return_paths=True` → `(n_sims, n_matches)` winner matrix. Smallest change, unlocks bracket scoring *and* correlated parlay/advancement pricing. Reuse the already-tested vectorised `_play_ko`.
- Add a **`match_win_probs`** helper (ET/pens draw-mass reallocation) and expose/pass `et_skill_weight` explicitly from `advancement.run_advancement`; calibrate against historical ET/pens frequencies.
- Build a **concrete-fixture enumerator** (named R32→Final ties from played results + `thirds_assignment`) and a **bracket pick type** over `R32_TIES`/`KNOCKOUT_FEED`.
- Define **objective functions** for retrospective + remaining-bracket: (i) max joint P(exact path), (ii) expected-correct-slots, (iii) expected contest-points under a *user-confirmed* scoring rule. Seed search with chalk, refine via beam/greedy-swap. Score against realized R32 results for model validation.
- Persist **PM advancement price series** (`pmhistory` → wire into snapshot daemon) and stamp non-1X2 closes so `outrightedge.convergence` / `clvbench` can grade advancement edges. Wire `predledger.settle` advancement + a knockout settlement source.
- Dedupe host-advantage (one venues source of truth) and wire `structural.outright_divergence` as a non-staking longshot/data-quality flag.

**Workflow 3 — Live alpha (remaining knockout + advancement + events, B/C execution):**
- **Precondition:** restore an odds feed (fix `ODDS_API_KEY` or bring Betfair creds online) and confirm `PM_DRY_RUN` stays as-is until explicitly changed.
- **Remaining-knockout match trading:** price single ties via `prob_fn(a,b,knockout=True)` vs PM moneyline/to-advance and book 1X2; reuse `build_card`/`rank_card` + `_fee_adjusted_kelly_stake`. Route execution only through `ClobTrader.place_order` (guardrails intact) and the `record_bet` choke point.
- **Advancement/futures:** extend `polymarket.resolve_outcome_token` with a `resolve_advancement_tokens` leg resolver; size via the factored `build_pm_proposals` core; add positive ET-inclusive `settlement_key`s so advancement legs can be safely paired.
- **Match-event markets:** feed each knockout tie's DC matrix + blended 1X2 into `scores.scoreline_card` / `card.build_event_references` and the `props`/`scorers` models; add new-market ingest to `odds_snapshots` (registry already enumerates the types).
- **Objective C — hedging:** use `reports.sportsbook_open_exposure_by_match` + `exposure_corr` as the netting backbone for the 8 open bets; settle the resolved ones (14, 101, group-stage builders); price genuinely-live ones (57 Ghana, 11 Golden Boot, plus any live knockout legs) against the model; add an FX-aware cross-pool netting step (GBP vs USD).
- **Discipline:** rigor caps a single tournament near 1 cluster — treat any advancement/bracket "edge" as unconfirmable within-tournament; use the label-shuffle placebo, never claim profitability.

---

*Prepared read-only. No trades placed, no DB modified, `PM_DRY_RUN` untouched, `.env` not read. Repo files cited inline. All R32 results are web-sourced and NOT yet corroborated by repo data.*
