# World Cup Alpha

World Cup Alpha is a quantitative football trading and forecasting system for
the 2026 FIFA World Cup. It combines an international Elo model, a time-decayed
Dixon-Coles goal model, de-vigged market prices, tournament simulation, venue
price capture, a real-money ledger, and research-only paper books.

The system is an evidence instrument first. Closing-line value is the primary
trading KPI; calibration against the market is the primary model check; realised
P&L is a lagging, high-variance outcome.

The two tournament fixtures still pending on the current feeds are the
third-place play-off, France vs England, and the final, Spain vs Argentina. See
[`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) for exact kickoffs and the dated
feed snapshot. That file contains state, not trade advice.

## Current system

- **Model:** Elo + Dixon-Coles + market blend. The deployed match probability is
  shrunk toward the de-vigged market; the raw blend is retained for scoring and
  diagnostics.
- **Selection:** model-probability bucket first, then EV for match markets.
  Multi-week futures retain further-out-first ordering. Model probabilities
  below 25% are display/free-bet only and receive no cash.
- **Sizing:** one combined base bankroll of GBP 3,000 plus total realised P&L
  across GBP books and Polymarket, converted at the fixed project rate of
  USD 1.33/GBP. The default stake fraction is quarter Kelly, subject to static
  execution and whole-book caps.
- **Complete event forest:** `scripts/wca_event_markets.py` enumerates every
  PM event family for each fixture, attaches live Gamma/CLOB prices, and model
  prices every supported family from the reconciled score matrix. Unsupported
  rows remain visible as market-only instead of receiving fabricated forecasts.
- **Shadow book:** `src/wca/shadowbook.py` records every observation, simulated
  entry, and abstention in `data/shadow_book.db`. It is isolated from the live
  ledger and has no order-signing code.
- **Cross-venue research:** the Hyperliquid watcher is monitor-only. Generic
  dominance bounds compare ET+pens advancement contracts with PM 90-minute 1X2
  legs, but unknown settlement fees and divergent tails prevent live-arbitrage
  claims.
- **Execution:** sportsbook trades are placed manually. Polymarket proposals
  park first and require a human `Y PM-<n>` confirmation. Hyperliquid execution
  is not implemented.

## Surfaces

The supported dashboards are local only:

- `http://localhost:8000`: primary trades, scores, event forest, shadow-book
  audit, recommendations, benchmarks, and system views.
- `http://localhost:8001`: analytics surface; retained through the tournament
  and scheduled for post-tournament consolidation.

Hosted deployments were removed. Each machine serves the feeds in its own
pulled checkout.

## Topology

```text
MacBook development and PM/HL data gateway
        |
        | branches, pull requests, tracked public feeds
        v
GitHub main  <------------------------------+
        |                                    |
        | mini autopull every 5 minutes      | scheduled feed commits
        v                                    |
Mac mini production -------------------------+
  canonical data/wca.db
  launchd jobs, Telegram bots, backups, publisher
```

The Mac mini is production and owns the canonical real-money ledger. The
MacBook is the development box and the Polymarket network gateway through
NordVPN. Never use a MacBook ledger copy to infer current exposure or settle a
real position.

## Repository map

```text
src/wca/
  models/          Elo, Dixon-Coles, score grids, props, shadows
  markets/         de-vig, Kelly, and bankroll rules
  sim/             48-team World Cup tournament simulation
  data/            results and venue adapters
  ledger/          canonical trade, settlement, CLV, and reporting logic
  pm/              Polymarket reads, signing, execution guards, and telemetry
  hl/              read-only Hyperliquid client and cross-venue research
  bot/             Telegram operations bot
  conductor/       isolated Telegram development-task service
  eventmarkets.py  pure event-family pricing and governed recommendation core
  shadowbook.py    isolated multi-venue paper book

scripts/           builders, daemons, operators, and research CLIs
deploy/            Mac mini and MacBook launchd/publish tooling
site/              primary localhost dashboard and JSON feeds
site-analytics/    secondary localhost analytics dashboard
tests/             unit, integration, policy, and safety tests
docs/              architecture, operations, current state, and research
```

## Development quickstart

Python 3.9 or newer is required.

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest -q

cp .env.example .env.dev
PM_DRY_RUN=1 WCA_DB_PATH=data/dev.db \
  ./.venv/bin/python scripts/wca_build_card.py \
  --env .env.dev --db data/dev.db
```

Serve the local dashboards explicitly on their documented ports:

```bash
PORT=8000 ./.venv/bin/python scripts/serve_site.py
PORT=8001 ./.venv/bin/python scripts/serve_analytics.py
```

Do not run PM-touching commands from an inherited shell without explicitly
forcing `PM_DRY_RUN=1`. Never commit `.env`, database files, keys, tokens, or
credentials.

## Documentation

- [`AGENTS.md`](AGENTS.md): Codex rules and protected behavior.
- [`ARCHITECTURE.md`](ARCHITECTURE.md): current component and data-flow map.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md): machine topology, deploy, feed,
  recovery, and security runbooks.
- [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md): dated tournament and research
  snapshot plus explicitly unverified runtime facts.
- [`docs/SELECTION_RULES.md`](docs/SELECTION_RULES.md): canonical ranking and
  sizing compliance details.
- [`docs/research/shadow_book_methodology.md`](docs/research/shadow_book_methodology.md):
  shadow-book design and evaluation policy.
- [`docs/research/hl_venue_recon_2026-07-09.md`](docs/research/hl_venue_recon_2026-07-09.md):
  venue reconnaissance and its unresolved fee/settlement questions.

## Research posture

The simple defaults remain because available holdouts have not justified more
complexity. Sources of measurable edge are expected to appear first in
promotions, line shopping, market-structure mistakes, and newly introduced
event markets. Model alpha is not assumed: it must beat a de-vigged market
baseline out of sample and produce positive CLV at a meaningful sample size.

Research use only.
