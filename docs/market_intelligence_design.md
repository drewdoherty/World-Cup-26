# Market Intelligence & Cross‑Venue Analytics — Design

Status: design / RFC (2026‑06‑28). Goal is not just `/arb` — it's a **historical market‑intelligence database** to study price formation, venue behaviour, efficiency, CLV, disagreement, and repeatable structural edge.

## 0. Guiding principle — extend, don't rebuild

Most of this subsystem already exists in pieces. Reuse:

| Need | Existing module |
|---|---|
| Historical odds store | `odds_snapshots` (wca.db, 1.26M rows) — currently **h2h / totals / btts / h2h_lay only** |
| PM price trajectory | `wca.pmhistory` (PR #95) — append‑only sqlite + JSONL, Gamma API (free) |
| Vig removal | `wca.markets.devig` (`shin`, multiplicative, power) |
| Venue canonicalisation | `wca.venues` (`canon_book`, `canon_platform`, `EXCHANGE_VENUES`) |
| Cross‑venue consensus/distance | `wca.venuesbench` + `wca.venuesdata` (PR #93) — best/consensus/LOBO/ranking already built |
| Staking | `wca.markets.kelly` (`stake`, ¼‑Kelly, 5% cap) |
| CLV / small‑sample stats | `wca.closecapture`, `wca.rigor.clv` (wilson, n_eff, gates) |
| Arb math + commissions | `wca.arb` / `wca.arbfx` (`pm_yes_to_decimal`, `effective_back`; Betfair 6%, PM 3%·p(1−p)), `wca.arbdata` |
| Odds polling + budget | `scripts/wca_snapshot_odds.py` (tracks OddsAPI `quota.remaining`), `wca.data.odds_source` orchestrator |
| Dashboard | `site-analytics/` (8001) `analytics.js` feed‑render pattern; existing "E // Model vs Venues" |

**The new code is mostly: (a) generalise the schema, (b) add source adapters + a market registry, (c) a derived‑metrics builder, (d) one dashboard section, (e) the `/arb` command.** Almost no new *math*.

## 0a. Honest constraints (these bound what's collectable now)

- **Coverage is partial.** Only `h2h/totals/btts/h2h_lay` are captured today. AH is partial (lay only). **Corners, cards, shots, SoT are NOT offered by these books via OddsAPI** — we cannot collect what the source doesn't sell. CS / FGS / anytime / team‑totals / player‑props are *sometimes* on OddsAPI (player markets) and on PM — collect where offered, mark the rest unavailable.
- **Betfair is a relay, not a live exchange.** Exchange odds arrive *via OddsAPI* (3k lay rows). True **liquidity and exchange‑lead/lag analysis require the direct Betfair/Smarkets APIs** (creds‑gated) — Phase 1.
- **No true closing line.** Capture stops pre‑kickoff → CLV is approximate until past‑KO capture (the #1 microstructure fix).
- **OddsAPI budget is the binding constraint** (~98k credits; cost ≈ markets × regions per call). Polling discipline > everything. PM (Gamma) is free → poll freely.
- **The dependable edge is execution/cost, not prediction** (microstructure recon: exchange ~0.7% vs sportsbook ~6.8% overround; every timing/prediction edge came back NULL). This subsystem's *primary* payoff is measuring venue/cost/execution structure — design for that, not for a mythical price‑prediction alpha.

---

## 1. Architecture

Layered, source‑pluggable:

```
            ┌─ Collectors (per‑source adapters) ─┐
 OddsAPI ───┤  oddsapi_adapter  (20 books)        │
 Gamma   ───┤  polymarket_adapter (pmhistory)     │── Normaliser ──> market_snapshots
 Betfair*───┤  betfair_adapter   (Phase 1, creds) │   (canon venue,     (append‑only,
 Smarkets*──┤  smarkets_adapter  (Phase 1, creds) │    devig, dedup)     change‑gated)
            └────────────────────────────────────┘                          │
                                                                             v
                                            Derived‑metrics builder ──> market_metrics
                                            (best/consensus/EV/Kelly/CLV/vol/lead‑lag)
                                                          │                  │
                                                          v                  v
                                              market_intel.json        /arb command
                                              (8001 dashboard)         (extends arb.py)
```

- **Source adapter interface** (`src/wca/intel/sources/base.py`): `fetch(fixture, market_types) -> list[RawQuote]`; each adapter knows its venue(s), supported markets, cost model, and `remaining_budget()`. Register in a **venue registry** (extend `venues.py`): `{canon_name, kind: exchange|sportsbook|prediction_market, commission, has_liquidity, supported_markets, colour}`. New sportsbooks = a registry row + (usually) nothing else if they ride OddsAPI.
- **Normaliser** (`src/wca/intel/normalise.py`): raw → `MarketSnapshot` (decimal, implied_raw=1/odds, implied_devig via `devig.shin` over the complete market, venue via `canon_book`). One function, reused across sources.
- **Stores**: `market_snapshots` (raw + derived‑per‑row), `market_metrics` (cross‑venue per market×timestamp). See §2.
- **Schedulers**: extend the existing crons (`hourly-odds.yml`) with a tiered, budget‑aware poller (§3). PM snapshotting already exists (PR #95).

---

## 2. Data schema

Generalise `odds_snapshots` → **`market_snapshots`** (migrate in place; keep the table, add columns + a `market_type`/`line` discriminator so the 1.26M existing h2h rows remain valid):

```sql
market_snapshots(
  ts_utc TEXT,           -- capture time (UTC, ISO8601)
  fetched_at TEXT,       -- adapter request time (for lag analysis)
  fixture_id TEXT,       -- canonical fixture key (match_id)
  ko_utc TEXT,           -- kickoff
  mins_to_ko REAL,       -- derived at write
  source TEXT,           -- 'theoddsapi' | 'polymarket' | 'betfair' | 'smarkets'
  venue TEXT,            -- canon_book(): 'Betfair','Smarkets','bet365','Polymarket',...
  venue_kind TEXT,       -- exchange | sportsbook | prediction_market
  market_type TEXT,      -- moneyline|draw|ah|ou|btts|cs|corners|cards|sot|fgs|anytime|team_total|player_prop
  selection TEXT,        -- 'Home'/'Draw'/'Away'/'Over'/team/player/...
  line REAL,             -- handicap/total line (NULL for 1X2/BTTS)
  decimal_odds REAL,
  implied_raw REAL,      -- 1/odds
  implied_devig REAL,    -- vig-adjusted (shin), per complete market
  liquidity REAL,        -- exchange available stake (NULL unless direct exchange API)
  raw TEXT,              -- source JSON (debug)
  api_meta TEXT          -- response metadata: status, quota_remaining, request_id
)
-- index (fixture_id, market_type, selection, venue, ts_utc)
```

**Change‑gated writes** (`avoid duplicate writes`): a `market_last_seen` cache (fixture×market×selection×venue → last decimal). Write a new row only if |Δ implied| ≥ ε (e.g. 0.3pp) **or** ≥ a max‑staleness interval (so we still timestamp "no move"). *Note: this saves storage, not API spend — every poll costs regardless; budget is governed by §3.*

**Derived store** `market_metrics(fixture_id, market_type, selection, line, ts_utc, …)`: best/worst/avg/median odds, implied_range, spread, pct_improvement_best_vs_worst, stdev_across_venues, consensus_prob, median_prob, vig_adj_consensus, model_prob, ev_vs_model, kelly_stake, clv, line_move_since_prev, rolling_vol, implied_vol, largest_disagreement(venue_pair, gap), secs_since_last_move. (Most are one call into `venuesbench`/`devig`/`kelly`/`rigor`.)

---

## 3. Polling strategy (token‑efficient)

Budget = OddsAPI credits (cost ≈ #markets × #regions per fixture‑call). PM/Gamma free → poll freely; direct exchanges (Phase 1) cheap → poll freely.

**Tiered cadence by time‑to‑KO** (OddsAPI only):

| Window | Cadence | Markets |
|---|---|---|
| > 24h | 1× / 6h | 1X2 + totals only |
| 24h–3h | hourly | 1X2, totals, AH, BTTS |
| 3h–1h | 30 min | + props the *model prices* (FGS/anytime where offered) |
| 1h–KO | 10–15 min | full available set |
| lineup‑confirmed event | one burst | full set |

- **Market‑scope discipline:** only request markets we (a) can act on and (b) the book actually offers — never blanket‑request corners/cards (unsupported → wasted credits). PM gets its own (free) full sweep via `pmhistory`.
- **Budget guard:** snapshotter already reads `quota.remaining`; persist it, and a config‑driven governor **down‑shifts cadence / drops low‑priority markets** as remaining credits approach a floor; alert via the bot. Graceful degradation = drop props first, then AH, keep 1X2.
- **Config**: `data/intel_polling.yml` (per‑window cadence, per‑market priority, budget floor) — tune without code changes.

---

## 4. Analytics dashboard (8001, extend `analytics.js`)

Keep it simple first (per the brief). New feed `site-analytics/data/market_intel.json`; new section "**F // Market Intelligence**".

- **Consistent venue colours** — single source of truth `VENUE_COLOURS` in `analytics.css` + JS, used everywhere: Polymarket, Betfair Exchange, Betfair Sportsbook, Bet365, Smarkets, Paddy Power, Betway. Divergence becomes visible instantly.
- **Price history** (per market): decimal odds over time + implied prob over time, one line/venue (reuse the SVG line helpers).
- **Cross‑venue spread table**: best/worst/avg/median/range/spread/%‑improvement/stdev, with highlights for *persistent outlier books*, *temporary dislocations* (a venue >Nσ off consensus this snapshot), *convergence near KO* (spread shrinking). Most of this is already in `venuesbench` — surface it.

---

## 5. Derived‑metrics pipeline

`scripts/wca_market_intel.py` (mirrors the existing `wca_*_data.py` feed builders; RO DBs, atomic write):

1. Load `market_snapshots` per upcoming fixture×market.
2. Per snapshot: `devig.shin` → vig‑adj consensus; `venuesbench` → best/consensus/spread/stdev/disagreement; `kelly.stake` → recommended stake vs model EV; `closecapture` → CLV once close exists; `rigor` → rolling vol / CI.
3. **Price discovery (lead‑lag)** from the time‑series: first venue to move beyond ε after a consensus shift; per‑venue repricing lag; exchange‑leads‑sportsbook test; PM lead/follow; persistently‑stale books; best‑early‑price books; longest‑inefficient markets. *Quality scales with cadence + history — thin until §3's higher‑frequency windows accrue.*
4. Write `market_metrics` + `market_intel.json`.

---

## 6. `/arb` command spec (extends `arb.py`/`arbfx.py`)

`/arb [fixture]` → scan latest `market_snapshots` for guaranteed‑profit sets, commission‑aware (reuse `arbfx`: Betfair 6%, PM 3%·p(1−p), Smarkets 2%).

- **Cross‑book arb:** Σ(1/best_odds) over complementary outcomes < 1 → stake split + locked return %.
- **Exchange‑vs‑sportsbook:** back (sportsbook) vs lay (exchange), net of commission/liquidity.
- **PM‑vs‑sportsbook:** via `pm_yes_to_decimal` + fee, where a real‑world outcome matches.
- **Output per opportunity:** market, venues/sides, stake split (in £ for books, $ for PM at £1=$1.33), guaranteed return %, **quote age + staleness flag**, liquidity (if known), confidence.
- **Honest gate:** with OddsAPI‑relay odds (delayed, no live liquidity), most "arbs" are **stale/unexecutable** → require recency + (for exchange legs) live liquidity before surfacing as actionable; otherwise label "indicative — verify live." **Real arb confidence needs the direct Betfair/Smarkets APIs (Phase 1).**
- Future: synthetic arb (derive a price from related markets), multi‑market, cross‑venue hedging, smart execution + venue selection by execution quality.

---

## 7. Roadmap

- **Phase 0 — foundation (now, no new creds):** generalise schema → `market_snapshots`; OddsAPI + PM adapters via the registry; tiered budget‑aware polling; change‑gated writes; derived‑metrics builder; "F // Market Intelligence" dashboard (price history + spread); `/arb` v1 (staleness‑aware, flag‑only). *Delivers the historical DB immediately.*
- **Phase 1 — live venue feeds:** direct **Betfair Exchange + Smarkets** APIs (creds) → real liquidity, true exchange prices, **past‑KO capture → true closing line + real CLV**, executable arb.
- **Phase 2 — price‑discovery analytics:** lead‑lag, stale‑book detection, efficiency‑by‑market — once higher‑cadence history accrues; event‑driven lineup polling.
- **Phase 3 — execution engine:** venue selection by execution quality, auto‑hedge, smart routing — gated on Phase 1 + a *proven* (CLV‑positive) edge.

**The asset is the database.** Even before `/arb` is reliable, the accumulating `market_snapshots`/`market_metrics` history is what lets us study price formation, venue lead‑lag, efficiency decay, CLV and structural cost edges — the durable goal.
