# Desk selection rule — canonical spec

**Single source of truth: `src/wca/selection.py`.** This document is the
reference so the rule never has to be restated. Where any older doc or comment
disagrees, this file + `src/wca/selection.py` win.

`wca.selection` is a **human-approved-change file**, like the execution caps in
`pm/trader.py`. Editing `PROB_BUCKETS`, `LONGSHOT_PROB`, or
`preference_sort_key` moves ALL real-money orderings and cash decisions at once
— treat any change to it as a sized-book change requiring human sign-off.

---

## The rule (user-confirmed 2026-07-07; category-conditional refinement 2026-07-09)

Canonical ordering key: **`(bucket_rank, hours_term, -ev)`**, where `hours_term`
is **category-conditional** (see the 2026-07-09 refinement below).

1. **Bucket by MODEL probability (PRIMARY sort).**
   - `moneyline` — model `>= 0.50`
   - `mid` — `0.25 <= model < 0.50`
   - `longshot` — model `< 0.25`

   Inclusive lower bounds (`0.50` → moneyline, `0.25` → mid, `0.2499` →
   longshot). A higher bucket **ALWAYS** ranks above a lower bucket, regardless
   of EV.

2. **Further-out fixtures first (SECONDARY) — CATEGORY-CONDITIONAL (2026-07-09).**
   Raw continuous hours-to-kickoff, descending — but **only for multi-week
   futures / advancement markets**. For **90-minute MATCH markets** the
   hours-out term is **NEUTRALISED** (contributes `0`), so EV breaks ties
   within the bucket. Where it applies, the hours value is a continuous float
   and is **never** bucketed into day-tiers. (Futures/advancement surfaces use
   a **stage-depth** analogue — deeper/later-resolving stage first — which was
   never routed through `hours_out`, so those surfaces are unchanged.)

3. **EV breaks ties** — the effective secondary key for MATCH markets (hours
   neutral); the tertiary tie-break within the same bucket + further-out tier
   for FUTURES markets.

4. **No cash on longshots (`model < 0.25`).** Free-bet / lottery only: the
   stake is forced to 0 and the side is flagged. It may still be **displayed**
   (dimmed). The cash floor is a strict `< 0.25` (a model prob of exactly
   `0.25` is a stakeable `mid`, not a longshot). **Unchanged by 2026-07-09.**

---

## The 2026-07-09 category-conditional refinement

`-hours_out` ("further-out-first") is now **conditional on the candidate's
market category** — neutral for 90-min match markets, kept for multi-week
futures.

**Evidence** (backtest 2026-07-09, n=1,046 resolved PM markets,
composition-controlled, look-ahead-guarded):

- **Match (90-min) markets** — PM efficiency is **FLAT** from 168h out to
  kickoff (fixed-cohort Brier `0.131 → 0.124`, CIs overlap); early entry earns
  ~0 after fees inside 72h and a small **penalty** inside 12h. The apparent
  early premium is a favourite-firms / longshot-bleeds drift **already captured
  by bucketing + `longshot_no_cash`**. ⇒ "further-out-first" has **no basis**
  for match markets; hours-out is neutralised and **EV breaks ties**.
- **Multi-week futures / advancement** — a **real** tradeable early edge
  (`+6–7%` at 24–72h, n=60, truncation-caveated). ⇒ **keep further-out-first
  there** (via the stage-depth secondary key).

**The conditional lives in ONE place:** `wca.selection.hours_out_term(hours,
market_kind)` — returns `-hours` for `MARKET_FUTURES`, `0.0` for `MARKET_MATCH`.
`preference_sort_key` takes a `market_kind` and routes through it; surfaces that
build an inline sort key (`card.rank_card`, `accas.rank_key`,
`wca_betrecs._singles_sort_key`) MUST call `hours_out_term` rather than
hardcoding `-hours`.

**Default (documented):** an undeclared candidate is treated as
**`MARKET_MATCH`** (hours-out neutral). Match is the bulk of the book and the
evidence says neutral is correct there; **futures must OPT IN** to
further-out-first (pass `market_kind=MARKET_FUTURES` or carry a futures
settlement/category — `advancement` / `group_winner` / `outright` / `reach_*` /
`tournament` / `to_win`). A single-match "Team to Advance" leg (settlement
`ET+pens` but resolved within the match) is deliberately still a MATCH market —
the ruling targets **multi-week** futures, not any ET+pens leg. (The bare
tournament-winner token `win` is intentionally NOT a loose substring — it would
false-match `winner` / `winning margin`; the winner-futures case is caught by
`outright` / `to_win` / `tournament` / explicit `MARKET_FUTURES`.)

This does **not** touch `bucket_rank` (primary), `longshot_no_cash` (cash
floor), or any cap/bucket boundary.

---

## The 2026-07-07 REPLACE ruling

"Longshot" is now defined **PURELY** by model prob `< 0.25`. This **RETIRES**
the older 2026-06-29 decision to "cut all market outright-underdogs regardless
of probability."

- A market outsider the model rates 25–49% is now a **STAKEABLE MID**.
- The market-relative FAV / 2ND-FAV / longshot categories
  (`card.classify_outcome` / `card._CATEGORY_PRIORITY`) survive **ONLY** as
  cosmetic DISPLAY labels (the FAV / 2ND-FAV tags on the rendered card). They
  no longer feed the sort key or the cash-cut predicate.

Rationale: the market's opinion of who is the underdog is not the desk's edge —
the model's own probability is. Cutting a market-underdog the model likes at
30–45% threw away stakeable mids; keying the cut off the model prob keeps them
while still banning cash on genuine <25c longshots (the proven leak: 0-for-N in
backtests, `docs/research/pm_preferences_backtest_2026-07-02.md`).

---

## Module API (`wca.selection`)

| Symbol | Purpose |
| --- | --- |
| `PROB_BUCKETS` | `((0.50,"moneyline"),(0.25,"mid"),(0.0,"longshot"))` — ordered high→low, inclusive lower bounds. |
| `LONGSHOT_PROB` | `0.25` — the cash floor. Strict `<` means no cash. |
| `prob_bucket(model_prob)` | `"moneyline"` / `"mid"` / `"longshot"`. |
| `bucket_rank(model_prob)` | Sortable int `0` / `1` / `2` (lower ranks higher). PRIMARY key. |
| `longshot_no_cash(model_prob)` | `True` when `model < 0.25` → free-bet/lottery only. Applied at the SIZING step, kept SEPARATE from the sort. |
| `MARKET_MATCH` / `MARKET_FUTURES` | Market-category constants (`"match"` / `"futures"`). Match = hours-out neutral; futures = further-out-first. |
| `resolve_market_kind(*hints)` | Resolve category from hints (explicit `market_kind`, `settlement`, `market`, `family`, `stage`). Explicit constant wins; else a futures marker substring; else the SAFE DEFAULT `MARKET_MATCH`. |
| `hours_out(p, kick_by_match, now_dt)` | Continuous raw hours to kickoff (0.0 unknown). Never bucketed. Whether it feeds the sort is decided by `hours_out_term`. |
| `hours_out_term(hours, market_kind)` | **The ONE place the conditional lives (2026-07-09).** `-hours` for `MARKET_FUTURES`; `0.0` for `MARKET_MATCH` (default). Every inline-key surface routes its secondary term through here. |
| `preference_sort_key(p, kick_by_match, now_dt, market_kind=None)` | `(bucket_rank, hours_out_term(hours, kind), -ev)`. `kind` = explicit arg → candidate fields → `MARKET_MATCH` default. Only deprioritises longshots; does NOT enforce the cash ban. |

### Design invariants (do NOT "improve")

- `hours_out` stays a continuous raw-float value — never bucketed. Whether it
  FEEDS the sort is category-conditional via `hours_out_term` (2026-07-09).
- The match/futures conditional lives in ONE place — `hours_out_term`. Surfaces
  that build an inline sort key (`card.rank_card`, `accas.rank_key`,
  `wca_betrecs._singles_sort_key`) MUST call it; never re-derive the conditional
  at a call site.
- Default is `MARKET_MATCH` (hours neutral); futures must OPT IN.
- `preference_sort_key` ONLY deprioritises longshots (rank 2); it does NOT
  enforce the cash ban. That is `longshot_no_cash()`, applied at the SIZING
  step, kept separate so a surface can display a longshot dimmed while sizing it
  at zero.
- Boundaries: `>= 0.50` moneyline, `>= 0.25` mid, `< 0.25` longshot (inclusive
  lower bounds); cash floor is strict `< 0.25`.

---

## Per-surface compliance table

Every surface that ranks, selects, or sizes trades imports `wca.selection`.
The **"Further-out"** column now records the 2026-07-09 category-conditional
state of the secondary key:

- **MATCH (neutral)** — 90-min match markets; hours-out contributes 0, EV
  breaks ties within the bucket (via `hours_out_term(..., MARKET_MATCH)` or the
  `preference_sort_key` default).
- **FUTURES (stage depth)** — multi-week futures/advancement; further-out-first
  KEPT via each surface's own `stage_further_out` depth map (never routed
  through `hours_out`, so unchanged by 2026-07-09).
- **fixture-coverage** — orders the FIXTURE list (not per-trade ranking) to
  choose coverage before truncation/browse; not a within-bucket trade sort, so
  the 2026-07-09 match-neutralisation does not apply.

"Cash floor" = `longshot_no_cash` gates the stake (UNCHANGED by 2026-07-09).

| Surface | File | Bucket sort | Further-out | Cash floor | Notes |
| --- | --- | --- | --- | --- | --- |
| PM proposer (reference) | `scripts/wca_pm_propose.py` | ✅ | MATCH (neutral) | (sizing elsewhere) | Scorer props + game 1X2 = 90-min match; passes `market_kind=MARKET_MATCH`. |
| Trade card | `src/wca/card.py` (`rank_card`, WATCH tier, `_cut_reason`) | ✅ | MATCH (neutral) | ✅ | 1X2 match markets; inline key routes hours via `hours_out_term(..., MARKET_MATCH)`. Keeps the hard `SELECTION_MIN_PROB=0.20` floor; `classify_outcome`/`_CATEGORY_PRIORITY` DISPLAY labels only. The separate `IMMINENT_EDGE_DISCOUNT` edge haircut is UNCHANGED. |
| Advancement | `src/wca/advancement.py` (`compare_to_polymarket`, `_fee_adjusted_kelly_stake`) | ✅ | FUTURES (stage depth) | ✅ | Uses `stage_further_out` — never used `hours_out`, so UNCHANGED (correctly keeps further-out-first). |
| Trade recs | `scripts/wca_betrecs.py` (`build_match_singles`, `build_advancement_futures`) | ✅ | MATCH (singles, neutral) / FUTURES (advancement, stage depth) | ✅ | `_singles_sort_key` routes hours via `hours_out_term(..., MARKET_MATCH)`; `build_advancement_futures` keeps its `_stage_further_out` map (UNCHANGED). `build_event_props`/`build_guaranteed_arbs` EXEMPT. |
| Next match / goalscorers | `src/wca/nextmatch.py` | ✅ (1X2 outcomes) | fixture-coverage (goalscorer card) | ✅ | `/next` SCHEDULE stays soonest-first (it IS the next-match schedule); within-market scorer ranking by implied prob exempt; anytime-scorer legs gated to no-cash. |
| Accas | `src/wca/accas.py` (`assemble_accas.rank_key`, cross-acca sort) | ✅ | MATCH (neutral) | ✅ | Match-leg parlays: `rank_key`/`_acca_key` route hours via `hours_out_term(..., MARKET_MATCH)`. Old accas-local `LONGSHOT_PROB=0.12` replaced by canonical `0.25`; `is_moneyline` flag replaced by `bucket_rank`. |
| Testbook paper-trader | `src/wca/testbook/trader.py` (`run_paper_pass`) | ✅ | — | ✅ | Automated PM paper-trader; imports `wca.selection` so it can't drift. |
| Market-intel metrics | `src/wca/intel/metrics.py` (`build_market_metrics`) | — (per-selection) | — | ✅ | Kelly fraction from `bankroll.PM_KELLY_FRACTION`. |
| Market-intel feed | `src/wca/intel/feed.py` (`build_feed`) | — | fixture-coverage | ✅ (via metrics) | Orders the FIXTURE browse list further-out first (not a per-trade within-bucket sort) — unaffected by the 2026-07-09 match-neutralisation. |
| Event EV CLI | `scripts/wca_event_ev.py` | ✅ | — | flag (`†`) | Decision-support; no stakes. |
| Player props CLI | `scripts/wca_player_props.py` | ✅ | — | flag (`†`) | Scorer props are structurally <25c → decision-support/no-cash. |
| Betbuilder CLI | `scripts/wca_betbuilder.py` | — | fixture-coverage | (no stakes) | Sorts the FIXTURE list further-out first BEFORE `--max-fixtures` truncation (coverage cap, not a trade sort) — unaffected by 2026-07-09. |
| Advancement matrix (client) | `site/adv_edge_matrix.js` | ✅ | FUTURES (stage depth) | ✅ | `STAGE_OUT` depth map; never used hours — UNCHANGED. Greys/flags <25c cells; `advKelly` returns 0 on <25c. |
| Edge desk (SHADOW) | `scripts/wca_edge_desk.py` | ✅ | FUTURES (stage depth) | ✅ (longshots capped at WATCH) | Advancement rows use `_STAGE_FURTHER_OUT` (UNCHANGED). SHADOW-only, no stakes; the tier-2 withheld-near-miss tail orders 90-min fixtures by raw kickoff — a shadow display tail, out of scope for the sort-key ruling. |
| Bot /matchevents | `src/wca/bot/app.py` (`handle_matchevents`) | ✅ (moneyline ONLY by spec) | MATCH (neutral) | ✅ (ML-only filter + killed markets) | Single-match 90-min exotics; passes `market_kind=MARKET_MATCH` to `preference_sort_key`. Display-only. |
| Event markets (PM) | `src/wca/eventmarkets.py` (`build_event_market_recs`) + `scripts/wca_event_markets.py` | ✅ | MATCH (neutral) | ✅ | Single-match PM coverage feed; passes `market_kind=MARKET_MATCH` (even the same-match "Team to Advance" leg resolves within the match). Kill-list + totals-under ban + same-fixture correlation cap on top. |

### Exempt (deliberately NOT wired)

These are analytics, accounting, faithful pass-throughs, or settlement-locked
arithmetic — applying the selection rule would be wrong:

- `src/wca/pmanalytics.py` `top_edges` — calibration diagnostics (abs-edge),
  NOT a rec feed. (Comment added so it is not mistaken for one.)
- `src/wca/outrightedge.py` — statistical IC / Brier.
- `src/wca/exposure_dashboard.py`, `dashboard.py`, `sitedata.py`,
  `exposure.py` — accounting / exposure views.
- `site/app.js`, `site/tracking_adv.js` — chart / ledger views.
- `site/arb.js` + `site/arb.html` — already order-preserving.
- `src/wca/bot/app.py` — faithful pass-through of pre-sorted feeds (EXCEPT
  `handle_matchevents`, which applies the rule — see the table above; the
  `wca.displayfmt` helpers show `bucket_tag` labels but never re-sort a feed).
- Guaranteed-arb sorts; `boostlock.py` / `matched.py` — boost-hedge arithmetic.

---

## Pinning tests

`tests/test_selection.py` pins the module: bucket boundaries, `bucket_rank`,
the strict `longshot_no_cash` floor, `hours_out`, `resolve_market_kind` (incl.
the safe MATCH default and the `win`-substring guard), `hours_out_term` (match
neutral / futures `-hours`), and `preference_sort_key` ordering over fixed
slates — MATCH (EV breaks ties within bucket, hours neutral), FUTURES
(further-out-first kept), a MIXED feed, and the same-teams 1X2-vs-advancement
BOUNDARY (hours applies to the advancement one only).
`tests/test_pm_propose_prefs.py` pins the reference surface;
`tests/test_card_operating_rules.py`, `tests/test_bot_percent_display.py`, and
`tests/test_eventmarkets.py` pin the per-surface orderings. If any breaks, a
real-money ordering changed — revise only when this ruling itself is being
revised.
