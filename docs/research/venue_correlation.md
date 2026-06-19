# Venue Correlation & CLV-by-Venue — Plan (skeleton)

> **Status (2026-06-18):** drafted skeleton. No code/behavior change. Defines the
> data, sources, and method to quantify CLV and model-vs-close agreement per
> venue/book.

## 1. Motivation

`reports.summary` blends all venues into one `total_pl`/`roi`
(`src/wca/ledger/reports.py:430-433`) — and (until the Phase-0 fix) the only
currency-aware view (`_pool_rows`/`handle_bets`) was broken because `_venue_of`
was undefined (`src/wca/bot/app.py`). We need a per-venue lens on (a) realised
CLV and (b) how closely each book's close tracks our blended model — to route
stakes (`docs/policy/account_routing.md`) and detect structurally soft/sharp books.

## 2. CLV-by-venue plan

- **Grain:** one row per settled bet with a captured close. Group by `platform`
  (book) and derived `venue`/`currency` (`VENUE_CURRENCY`, `src/wca/sitedata.py`).
- **Metric:** mean and median CLV (`decimal_odds/closing_odds − 1`, the uniform
  convention at `closecapture.py:435`, `wca_settle.py:102`,
  `store.set_closing_odds`, `app.py:701`). Report n, win-rate, ROI alongside —
  **single-currency only, never sum GBP+USD**.
- **Source:** `bets` table joined to `closing_odds`/`clv` stamped by
  `closecapture.capture_closes` (`src/wca/closecapture.py:493-540`).
- **Caveat:** auto-close is **1X2-only** by design; totals/btts/DNB bets have no
  auto-captured close, so venue CLV for derivative markets is sparse/manual-only.

## 3. Model-vs-closing agreement matrix plan

- **Rows:** venue/book. **Columns:** Brier, log-loss, mean signed bias of
  (model prob − Shin-devigged close prob), per outcome (H/D/A).
- **Inputs:** the blended model 1X2 persisted at card build (`modelpreds`), and
  the Shin median-consensus close from `odds_snapshots` (`market='h2h'`,
  `src/wca/tracking.py:445`). A per-book close requires reading `bookmaker_key`
  from the `raw` JSON blob — **`odds_snapshots` has no `bookmaker_key` column**
  (`src/wca/data/snapshot.py:26-51`), so a per-book matrix needs a JSON-extract.
- **Reuse:** `brier_1x2`/`log_loss_1x2` (`src/wca/tracking.py:386-404`) as cells.

## 4. Data needed and where it lives

| Need | Source | Path | Gap |
|---|---|---|---|
| Settled bets + CLV | `bets` | `src/wca/ledger/store.py` | no currency column; account/source only |
| Per-book closing odds | `odds_snapshots.raw` JSON | `src/wca/data/snapshot.py` | no `bookmaker_key` column → JSON parse required |
| Model 1X2 at build | `modelpreds` | `src/wca/tracking.py` | exists |
| Shin consensus close | `odds_snapshots` (h2h) | `src/wca/tracking.py:445` | 1X2-only |
| Venue→currency map | `VENUE_CURRENCY` | `src/wca/sitedata.py:206-209` | exists |

## 5. Open dependencies

- `_venue_of` restored in `src/wca/bot/app.py` (Phase-0 fix) — per-venue surfacing
  works again.
- Add a `bookmaker_key` projection (read-only view or JSON-extract) before the
  per-book agreement matrix is feasible.
- Decide whether PM (USD) is included — PM prices are **not historized**
  (`odds_snapshots` is 100% `theoddsapi`), so PM venue CLV is not reconstructable
  from storage today. The new `get_prices_history` helper can begin historising
  PM winner/stage prices to close this gap.
