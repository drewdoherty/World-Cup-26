# World Cup Alpha — Analytics Dashboard

A **standalone** analytics dashboard, isolated from the main `site/`, that surfaces the
full *paper book* (every priced model selection, not just placed bets) per
`docs/research/analytics_design.md`. Deployed as its **own** Vercel project so it lives
alongside — not on top of — the existing terminal site.

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

## Deploy — BRAND-NEW, SEPARATE Vercel project (do NOT touch production)

> ⛔ **Hard guardrail.** The existing production site
> (`fifa-world-cup-2026-betting-gambling.vercel.app` and the model URL) must stay **exactly
> as-is**. This dashboard deploys as its **own new project**. Never run `vercel link` /
> `vercel --prod` against the existing project, never change its Root Directory, never
> repoint its domain. If a `vercel link` prompt offers an existing project, choose
> **“create a new project”** — never the existing one.

The `vercel` CLI was not installed in the build environment, so deploy with one of:

### Option A — Vercel CLI (recommended)

```
npm i -g vercel                 # if not installed
cd site-analytics
vercel login                    # if not already authed
vercel link                     # at the prompt: "Set up and deploy" -> create a NEW project
                                #   name e.g. "wca-analytics-lilac". DO NOT pick the existing
                                #   fifa-world-cup-2026-betting-gambling project.
vercel --prod                   # deploys THIS dir as its own project; prints the new *.vercel.app URL
```

`site-analytics/` is the project **root** (it contains `index.html` + `vercel.json`), so no
`outputDirectory` is needed — Vercel serves the directory as a static site. Because the root
is `site-analytics/`, this project can never serve or overwrite the production `site/`.

### Option B — Vercel dashboard (no CLI)

1. Push this branch (done by the accompanying PR).
2. vercel.com → **Add New… → Project** → import this Git repo as a **brand-new** project.
   It must NOT be the existing `fifa-world-cup-2026-betting-gambling` project.
3. Set **Root Directory = `site-analytics`**. Framework preset: **Other** (static). No build command.
4. Deploy → Vercel returns the new `https://<project>.vercel.app` URL. The production project
   is untouched.

To keep it auto-refreshing later, add a **separate** GitHub Action (or a step that writes the
five feeds into `site-analytics/data/`) — do not modify the production deploy workflow.
