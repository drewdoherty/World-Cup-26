# Local Runtime

This is the active private runtime plan. The dashboard is served from this
machine instead of the public Vercel deployment.

## Local Dashboard

One command refreshes the static feeds and serves the site locally:

```bash
WCA_AUTOPUSH=0 ./.venv/bin/python scripts/wca_local_site.py --db data/wca.db
```

Open:

```text
http://127.0.0.1:8742
```

Useful variants:

```bash
# Refresh feeds without starting a server.
WCA_AUTOPUSH=0 ./.venv/bin/python scripts/wca_local_site.py --db data/wca.db --refresh-only

# Serve existing files without touching the ledger/feeds.
WCA_AUTOPUSH=0 ./.venv/bin/python scripts/wca_local_site.py --no-refresh
```

`WCA_AUTOPUSH=0` is now the safe default in code and `.env.example`: bot and
daemon syncs regenerate local JSON, but they do not commit/push the site unless
you explicitly set `WCA_AUTOPUSH=1`.

## Turning Off The Public Vercel Site

The repo can stop publishing, but the actual Vercel project must be disabled in
Vercel because it is controlled by their project settings. In the Vercel
dashboard for `fifa-world-cup-2026-betting-gambling`, do one of:

- disconnect the GitHub repository integration;
- enable deployment protection if you still want the URL but private access;
- delete/archive the Vercel project if you want the public URL gone.

Do this after confirming the local dashboard works.

## Always-On Daemons

Run these from the repo root with `.env` present:

```bash
WCA_AUTOPUSH=0 nohup ./.venv/bin/python scripts/wca_bot.py --db data/wca.db --env .env > logs/bot.local.log 2>&1 &
WCA_AUTOPUSH=0 nohup ./.venv/bin/python scripts/wca_snapshotd.py --db data/wca.db --env .env > logs/snapshotd.local.log 2>&1 &
WCA_AUTOPUSH=0 nohup ./.venv/bin/python scripts/wca_newsd.py --db data/wca.db --env .env --interval 600 --max-per-cycle 2 > logs/newsd.local.log 2>&1 &
WCA_AUTOPUSH=0 nohup ./.venv/bin/python scripts/wca_promosd.py --db data/wca.db --env .env --interval 21600 --max-per-cycle 2 > logs/promosd.local.log 2>&1 &
```

Cadence:

- Bot: long-polling Telegram, on demand.
- Odds snapshots: adaptive. Hourly when idle, tight pre-close/live, and pushes
  site/tracking after close captures or periodic live line-history updates.
- News: every 10 minutes, capped to 2 alerts per cycle.
- Promos: every 6 hours, capped to 2 alerts per cycle.

## On-Demand Refresh

Use this after manual settlements or ledger corrections:

```bash
WCA_AUTOPUSH=0 ./.venv/bin/python scripts/wca_local_site.py --db data/wca.db --refresh-only
```

## Heavy / On-Demand Only

`scripts/wca_build_card.py` currently refits Dixon-Coles and can stall during
the optimization step. Do not rely on it as an always-on loop yet. Until the
fit is cached or bounded, `/scores` and `/goalscorers` must fail closed when
their cached feed is FT rather than showing stale markets.

## Telegram Command Surface

Primary:

- `/today`
- `/open` or `/bets`
- `/scores`
- `/goalscorers`
- `/accas`
- `/boost`
- `/pm`
- `/summary`
- `/settle`
- `/ping`

Quiet/debug commands still work: `/next`, `/card`, `/clv`, `/structure`.
