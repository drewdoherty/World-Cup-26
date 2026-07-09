# World Cup Alpha — Analytics Dashboard

A **standalone** analytics dashboard, isolated from the main `site/`, that surfaces the
full *paper book* (every priced model selection, not just placed bets) per
`docs/research/analytics_design.md`. Served on its **own** localhost port (8001) so it
lives alongside — not on top of — the main terminal site on :8000.

## Sections (design modules)

| Section | Design module | Feed(s) |
|---|---|---|
| Book Headline | live aggregates | `data.json`, `tracking_data.json` |
| **D · Verdict** | repeatable-edge gate battery | `rigor.json` |
| **A · Risk & P&L** | portfolio P&L distribution (VaR/CVaR) | `risk_pnl.json`, `exposure_data.json` |
| **A · Futures forest** | per-team reach, model vs market | `mc_futures.json` |
| **B · Win-rate** | model vs realized book, acca autopsy | `winrate.json` |
| **C · Model CLV** | full-book CLV, beat-rate vs placebo, coverage | `tracking_clv_benchmark.json` |
| **P0 · Paper ledger** | prediction-ledger coverage by market | `predledger.json` |

Every panel degrades to an empty / "insufficient sample" / "pending" state rather than
throwing. CLV is fair-vs-fair; coverage is reported beside every aggregate; the verdict
defaults to `INSUFFICIENT SAMPLE`. Currencies are never summed except the FX-disclosed
distribution view. Footer: *monitoring, not betting advice*.

## Data feeds

`data/*.json` are static projections. The existing feeds (`data.json`, `tracking_data.json`,
`exposure_data.json`, `mc_futures.json`, `advancement_history.json`) are copied from `site/`.
The five new feeds are produced by the new backend builders:

```
PYTHONPATH=src python3 scripts/wca_predledger.py ensure   --db data/dev.db
PYTHONPATH=src python3 scripts/wca_predledger.py backfill  --db data/dev.db
PYTHONPATH=src python3 scripts/wca_predledger.py settle    --db data/dev.db
PYTHONPATH=src python3 scripts/wca_predledger.py close     --db data/dev.db
PYTHONPATH=src python3 scripts/wca_predledger.py publish   --db data/dev.db   # -> predledger.json
PYTHONPATH=src python3 scripts/wca_winrate_data.py       # -> winrate.json
PYTHONPATH=src python3 scripts/wca_clvbench_data.py      # -> tracking_clv_benchmark.json
PYTHONPATH=src python3 scripts/wca_rigor_data.py         # -> rigor.json
PYTHONPATH=src python3 scripts/wca_risk_pnl_data.py      # -> risk_pnl.json
```

> On the **mini** these run against `data/wca.db`. On this **dev box** the predledger
> writes to `data/dev.db` only — a guard refuses a `wca.db` basename unless
> `WCA_ALLOW_PROD_DB` is set. `wca.db` is opened strictly read-only for the CLV/risk joins.

## Local preview

```
python3 scripts/serve_analytics.py     # serves site-analytics/ on :8755
# open http://localhost:8755/
```

## Serving — localhost only (Vercel removed 2026-07-08)

There is no hosted deploy: Vercel was removed from the project entirely on
2026-07-08 (both `vercel.json` files deleted; deployments were already blocked
at the dashboard). The dashboard is served as static files from a local
Python HTTP server:

```
# the standing serving surface (mini + dev box)
python3 -m http.server 8001 --bind 127.0.0.1 --directory site-analytics
# open http://localhost:8001/
```

`scripts/serve_analytics.py` (above, :8755) remains available as an ad-hoc
local preview. Feeds under `data/` refresh via the mini's hourly `analytics`
job in its local working tree; the tree itself is frozen pending the
post-tournament site consolidation (see `CLAUDE.md` standing decisions and
`docs/overhaul/PHASE1_DESIGN.md` §7.c).
