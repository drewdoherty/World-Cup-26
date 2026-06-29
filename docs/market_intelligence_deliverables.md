# Market Intelligence — 7 Deliverables (status & artifacts)

Companion to the design RFC (`market_intelligence_design.md`). Maps each requested
deliverable to the **concrete artifact(s)** that fulfil it and their status. The
long-term objective is a **historical market-intelligence database** for studying
price formation, venue behaviour, efficiency, CLV, disagreement and structural
edge — `/arb` is one consumer, not the goal.

Status legend: ✅ built · 🟡 partial / honest-stub · ⛔ planned (creds/data-gated).

| # | Deliverable | Artifact(s) | Status |
|---|---|---|---|
| 1 | Architecture | `docs/market_intelligence_design.md` §1; package `src/wca/intel/` | ✅ |
| 2 | Data schema for historical odds | `src/wca/intel/store.py` (`market_snapshots`, `market_metrics`) | ✅ |
| 3 | Efficient polling strategy | `src/wca/intel/poller.py` + `data/intel_polling.yml` + `scripts/wca_intel_collect.py` + `src/wca/intel/sources/` | ✅ planner / 🟡 live fetch |
| 4 | Analytics dashboards | `site-analytics/` "F // Market Intelligence" (spread + price history) | ✅ |
| 5 | Derived metrics pipeline | `src/wca/intel/metrics.py` + `scripts/wca_market_intel.py` → `market_intel.json` / `market_metrics` | ✅ |
| 6 | `/arb` command spec + impl | `src/wca/intel/arb.py` + `/arb` in `src/wca/bot/app.py` | ✅ v1 (indicative) |
| 7 | Roadmap → automated monitor/exec | `design.md` §7 + this table's "next" column | ✅ doc |

---

## 1 · Architecture
Layered, source-pluggable: **collectors** (per-source adapters) → **normaliser** →
`market_snapshots` (append-only, change-gated) → **derived-metrics builder** →
`market_metrics` + `market_intel.json` → **dashboard** + **`/arb`**. Package map:

```
src/wca/intel/
  registry.py    venue registry (canon, kind, commission, has_liquidity, colour)
  store.py       market_snapshots / market_metrics schema + change-gated writes
  normalise.py   decimal→implied→vig-adjusted (Shin); from_oddsapi_rows mapper
  metrics.py     cross-venue spread + consensus + EV/Kelly overlay
  feed.py        market_intel.json assembler (spread + bounded price history)
  poller.py      tiered, budget-aware cadence planner (pure)
  arb.py         arbitrage scanner (cross-book / back-lay / pm-book)
  sources/       adapter interface + OddsAPI + Polymarket adapters
scripts/         wca_market_intel.py (feed) · wca_intel_collect.py (collector)
```
**Reuses, never re-implements:** `wca.markets.devig`, `wca.markets.kelly`,
`wca.venues`, `wca.arbfx`/`wca.arb`, `wca.venuesbench`, `wca.rigor`.

## 2 · Data schema
`market_snapshots(ts_utc, fetched_at, fixture_id, ko_utc, mins_to_ko, source,
venue, venue_kind, market_type, selection, line, decimal_odds, implied_raw,
implied_devig, liquidity, raw, api_meta)` — one row per venue×market×selection×time,
indexed `(fixture_id, market_type, selection, venue, ts_utc)`. Generalises the
existing `odds_snapshots` (1.26M h2h/totals/btts/h2h_lay rows remain valid via
`from_oddsapi_rows`). **Change-gated writes**: a row is written only on a material
implied move (≥ `eps`) or after `max_staleness_s` — compact history, no lost signal.
`market_metrics(...)` holds the per-market×time derived row (see #5).

## 3 · Efficient polling strategy
Pure deterministic planner `poller.plan_polls(fixtures, config, now, last_polled,
remaining_credits)` → per-fixture {due, markets, reason}. Cadence by time-to-KO:

| window | cadence | markets |
|---|---|---|
| >24h | 6h | moneyline, ou |
| 24h–3h | 1h | moneyline, ou, ah, btts |
| 3h–1h | 30m | + player props (model-priced) |
| 1h–KO | 12m | full available set |

**Budget governor** (`data/intel_polling.yml`: soft floor 500 / hard floor 100
credits): above floor no degradation; ≤ soft floor sheds priority<2 markets (props
first, then team totals); ≤ hard floor keeps only moneyline+OU and doubles the
interval. **Moneyline is always pinned.** PM/Gamma is free → poll freely; only
OddsAPI spend is governed. `scripts/wca_intel_collect.py` runs the planner and
persists change-gated snapshots (this phase reads the existing store read-only;
🟡 live OddsAPI fetch on the planner's cadence is the Phase-1 wiring).

## 4 · Analytics dashboards
`site-analytics/` "F // Market Intelligence" (localhost:8001):
- **Cross-venue spread** — best/worst/avg/median odds, implied range, spread,
  %-improvement, dispersion, largest disagreement; headline table sorted by the
  largest cross-venue gap (the execution edge); **stable per-venue colours** from
  the registry so divergence is instantly visible; fresh/stale badges.
- **Price history** — per fixture's 1X2: implied-probability-over-time and
  decimal-odds-over-time, one line per venue (bounded, tail-capped series). Shows
  an honest "history accrues" state until ≥2 snapshots exist per venue.

## 5 · Derived metrics pipeline
`metrics.build_market_metrics()` computes per selection: best/worst/avg/median
odds, implied range, spread, %-improvement, std-dev, **vig-adjusted consensus**
(Shin over complete books only — never fabricated from a partial book), median
prob, **EV vs model + ¼-Kelly** at the best (commission-adjusted) price, and the
largest pairwise venue disagreement. CLV, line-move, rolling/implied volatility
and time-since-last-move columns exist in `market_metrics` and fill as time-series
and a true close accrue. `scripts/wca_market_intel.py` writes `market_intel.json`
(feed) and can persist `market_metrics`.

## 6 · `/arb` command
`arb.scan_market()` runs three detectors, all delegating commission/lay/PM-fee
math to `wca.arbfx`:
1. **cross_book** — best decimal per outcome; arb iff Σ(1/best) < 1.
2. **back_lay** — back a sportsbook, lay on Betfair/Smarkets, net of commission.
3. **pm_book** — Polymarket YES vs the complementary book outcome.
Each `ArbOpportunity` carries legs (venue/side/selection/odds/stake; £ books, $ PM
at £1=$1.33), guaranteed return %, per-leg + overall **quote age & stale flag**,
liquidity-known flag, and `actionable`/`confidence`. **Honest gate:** with
OddsAPI-relay odds, anything stale beyond the window or with unknown exchange
liquidity is `actionable=False` ("indicative — verify live"). Bot: `/arb [team]`
→ `handle_arb` (read-only) → Markdown report; **never places a bet**.

## 7 · Roadmap
- **Phase 0 (this PR)** — schema, registry, normaliser, metrics, feed, dashboard
  (spread + price history), tiered budget-aware planner + collector, `/arb` v1.
  *Delivers the historical DB immediately.*
- **Phase 1 — live venue feeds (creds):** direct Betfair Exchange + Smarkets APIs
  → real liquidity, true exchange prices, **past-KO capture → true closing line +
  real CLV**, executable arb. Wire live OddsAPI fetch onto the planner cadence.
- **Phase 2 — price-discovery analytics:** lead-lag (who moves first), per-venue
  repricing lag, exchange-leads-sportsbook test, PM lead/follow, persistently-stale
  vs best-early books, longest-inefficient markets — once higher-cadence history
  accrues; event-driven lineup-confirmed polling bursts.
- **Phase 3 — execution engine:** venue selection by execution quality, auto-hedge,
  smart routing — gated on Phase 1 **and** a proven (CLV-positive) edge.

**The asset is the database.** Even before `/arb` is reliable, the accumulating
`market_snapshots` / `market_metrics` history is what lets us study price
formation, venue lead-lag, efficiency decay, CLV and structural cost edges.
