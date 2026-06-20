# Local Runtime

This is the active runtime plan when World Cup Alpha runs from this computer
rather than the Mac mini.

## Always-On Daemons

Run these from the repo root with `.env` present:

```bash
nohup ./.venv/bin/python scripts/wca_bot.py --db data/wca.db --env .env > logs/bot.local.log 2>&1 &
nohup ./.venv/bin/python scripts/wca_snapshotd.py --db data/wca.db --env .env > logs/snapshotd.local.log 2>&1 &
nohup ./.venv/bin/python scripts/wca_newsd.py --db data/wca.db --env .env --interval 600 --max-per-cycle 2 > logs/newsd.local.log 2>&1 &
nohup ./.venv/bin/python scripts/wca_promosd.py --db data/wca.db --env .env --interval 21600 --max-per-cycle 2 > logs/promosd.local.log 2>&1 &
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
./.venv/bin/python scripts/wca_site.py --db data/wca.db
./.venv/bin/python scripts/wca_tracking_data.py --db data/wca.db
./.venv/bin/python scripts/wca_promos_data.py --db data/wca.db --scores site/scores_data.json --out site/promos_data.json
git add site/data.json site/linemove.json site/tracking_data.json site/promos_data.json
git commit -m "Refresh site feeds"
git push
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
