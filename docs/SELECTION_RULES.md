# Desk selection rule — canonical spec

**Single source of truth: `src/wca/selection.py`.** This document is the
reference so the rule never has to be restated. Where any older doc or comment
disagrees, this file + `src/wca/selection.py` win.

`wca.selection` is a **human-approved-change file**, like the execution caps in
`pm/trader.py`. Editing `PROB_BUCKETS`, `LONGSHOT_PROB`, or
`preference_sort_key` moves ALL real-money orderings and cash decisions at once
— treat any change to it as a sized-book change requiring human sign-off.

---

## The rule (user-confirmed 2026-07-07)

Canonical ordering key: **`(bucket_rank, -hours_out, -ev)`**.

1. **Bucket by MODEL probability (PRIMARY sort).**
   - `moneyline` — model `>= 0.50`
   - `mid` — `0.25 <= model < 0.50`
   - `longshot` — model `< 0.25`

   Inclusive lower bounds (`0.50` → moneyline, `0.25` → mid, `0.2499` →
   longshot). A higher bucket **ALWAYS** ranks above a lower bucket, regardless
   of EV.

2. **Further-out fixtures first (SECONDARY).** Raw continuous hours-to-kickoff,
   descending — thin/soft early markets are more likely mispriced. This is a
   continuous float and is **never** bucketed into day-tiers.

3. **EV breaks ties ONLY (tertiary)** — within the same bucket + further-out
   tier.

4. **No cash on longshots (`model < 0.25`).** Free-bet / lottery only: the
   stake is forced to 0 and the side is flagged. It may still be **displayed**
   (dimmed). The cash floor is a strict `< 0.25` (a model prob of exactly
   `0.25` is a stakeable `mid`, not a longshot).

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
| `hours_out(p, kick_by_match, now_dt)` | Continuous hours to kickoff (0.0 unknown). SECONDARY key (used negated). Never bucketed. |
| `preference_sort_key(p, kick_by_match, now_dt)` | `(bucket_rank, -hours_out, -ev)`. Only deprioritises longshots; does NOT enforce the cash ban. |

### Design invariants (do NOT "improve")

- `hours_out` stays a continuous raw-float secondary key — never bucketed.
- `preference_sort_key` ONLY deprioritises longshots (rank 2); it does NOT
  enforce the cash ban. That is `longshot_no_cash()`, applied at the SIZING
  step, kept separate so a surface can display a longshot dimmed while sizing it
  at zero.
- Boundaries: `>= 0.50` moneyline, `>= 0.25` mid, `< 0.25` longshot (inclusive
  lower bounds); cash floor is strict `< 0.25`.

---

## Per-surface compliance table

Every surface that ranks, selects, or sizes bets imports `wca.selection`.
"Further-out" = the secondary key is applied (raw hours, or the stage-depth
analogue for advancement). "Cash floor" = `longshot_no_cash` gates the stake.

| Surface | File | Bucket sort | Further-out | Cash floor | Notes |
| --- | --- | --- | --- | --- | --- |
| PM proposer (reference) | `scripts/wca_pm_propose.py` | ✅ | ✅ | (sizing elsewhere) | The rule was extracted verbatim from here; now imports back. |
| Bet card | `src/wca/card.py` (`rank_card`, `_cut_reason`) | ✅ | ✅ | ✅ | Keeps the hard `SELECTION_MIN_PROB=0.20` floor; `classify_outcome`/`_CATEGORY_PRIORITY` kept as DISPLAY labels only. |
| Advancement | `src/wca/advancement.py` (`compare_to_polymarket`, `_fee_adjusted_kelly_stake`) | ✅ | ✅ (stage depth) | ✅ | `bucket`/`no_cash` tags propagated into `advancement_data.json`. |
| Bet recs | `scripts/wca_betrecs.py` (`match_singles`, `advancement_futures`) | ✅ | ✅ | ✅ | `build_event_props` (returns `[]`) and `build_guaranteed_arbs` (settlement-locked) EXEMPT. |
| Next match / goalscorers | `src/wca/nextmatch.py` | ✅ (1X2 outcomes) | ✅ (goalscorer card) | ✅ | `/next` SCHEDULE stays soonest-first (it IS the next-match schedule); within-market scorer ranking by implied prob exempt; anytime-scorer legs gated to no-cash. |
| Accas | `src/wca/accas.py` (`assemble_accas.rank_key`, cross-acca sort) | ✅ | ✅ (per-leg kickoff) | ✅ | Old accas-local `LONGSHOT_PROB=0.12` replaced by canonical `0.25`; `is_moneyline` market-TYPE flag replaced by `bucket_rank`. |
| Testbook paper-trader | `src/wca/testbook/trader.py` (`run_paper_pass`) | ✅ | — | ✅ | Automated PM paper-trader; imports `wca.selection` so it can't drift. |
| Market-intel metrics | `src/wca/intel/metrics.py` (`build_market_metrics`) | — (per-selection) | — | ✅ | Kelly fraction now sourced from `bankroll.PM_KELLY_FRACTION` (the two `0.25`s no longer collide). |
| Market-intel feed | `src/wca/intel/feed.py` (`build_feed`) | — | ✅ | ✅ (via metrics) | Fixtures ordered further-out first. |
| Event EV CLI | `scripts/wca_event_ev.py` | ✅ | — | flag (`†`) | Decision-support; no stakes. |
| Player props CLI | `scripts/wca_player_props.py` | ✅ | — | flag (`†`) | Scorer props are structurally <25c → decision-support/no-cash. |
| Betbuilder CLI | `scripts/wca_betbuilder.py` | — | ✅ | (no stakes) | Fixtures sorted further-out BEFORE `--max-fixtures` truncation. |
| Advancement matrix (client) | `site/adv_edge_matrix.js` | ✅ | ✅ (stage depth) | ✅ | Drives off the server-computed `bucket` tag; greys/flags <25c cells; `advKelly` returns 0 on <25c. |
| Edge desk (SHADOW) | `scripts/wca_edge_desk.py` | ✅ | ✅ (stage depth) | ✅ (longshots capped at WATCH) | SHADOW-only decision feed, no stakes; imports `prob_bucket`/`PROB_BUCKETS`/`longshot_no_cash`/`LONGSHOT_PROB`; consumers render feed order. |
| Bot /matchevents | `src/wca/bot/app.py` (`handle_matchevents`) | ✅ (moneyline ONLY by spec) | ✅ (`preference_sort_key`) | ✅ (ML-only filter + killed markets) | Display-only exotics view: `prob_bucket(model) == "moneyline"` AND net edge > 0; ordering via `preference_sort_key`; display-only sizing via the PM pool + kelly kernel. |
| Event markets (PM) | `src/wca/eventmarkets.py` (`build_event_market_recs`) + `scripts/wca_event_markets.py` | ✅ | ✅ | ✅ | Full single-match PM coverage feed (`site/event_market_recs.json`); imports `preference_sort_key`/`bucket_rank`/`longshot_no_cash`/`prob_bucket`; adds kill-list (correct score / scorer props) + totals-under ban + same-fixture correlation cap on top of the canonical rule; consumers (arb.html panel) render feed order. |

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
the strict `longshot_no_cash` floor, `hours_out`, and `preference_sort_key`
ordering over a fixed mixed slate (mixed buckets / hours / EV incl. ties and
boundary values). `tests/test_pm_propose_prefs.py` pins the reference surface.
If either breaks, a real-money ordering changed — revise only when this ruling
itself is being revised.
