# jupyter bet — transparent World Cup research environment

A hands-on Jupyter workspace over the **production** WCA stack: inspect
every intermediate DataFrame, change parameters, stress constraints, and
rerun — with the same de-vigging, sizing, matching and decision code that
runs the live system (`src/wca`, `scripts/wca_betrecs.py`). Nothing here is
a notebook-only re-model, and **nothing here places orders**.

## Quick start

```bash
cd "jupyter bet"

# run the full pipeline (00 → 08), executing each notebook top-to-bottom
../.venv/bin/python tools/build_notebooks.py

# or open interactively
../.venv/bin/python -m jupyter lab notebooks/     # (pip install jupyterlab if wanted)

# run the research test suite (61 tests)
../.venv/bin/pytest tests -q
# repo suite (unchanged, scoped to tests/ by pyproject testpaths)
cd .. && ./.venv/bin/pytest -q
```

Dependencies (already in `.venv`): polars, pandas, pyarrow, matplotlib,
pyyaml, jupytext, nbclient, nbformat, ipykernel. Python 3.9.

## Live vs offline

* **Live** (default): guarded API pulls — Odds API under a credit budget,
  Polymarket Gamma/CLOB/Data-API (needs the VPN route on this MacBook).
  Every payload is written to `data/raw/` before use.
* **Offline**: `cp config.example.yaml config.yaml`, set `offline: true`.
  Every notebook then runs entirely from cached raw snapshots + Parquet.
  Verified: the full 00→08 sequence executes cleanly offline.
* **Live refresh later** (when credentials/VPN are available):
  `../.venv/bin/python tools/build_notebooks.py 01 02` re-pulls both venues,
  then `tools/build_notebooks.py 03 04 05 06 07 08` rebuilds downstream.

## The notebooks

| # | notebook | what it does |
|---|---|---|
| 00 | setup & data catalog | env manifest, typed params, source census, lineage catalog, connectivity probe |
| 01 | Odds API full run | every v4 read endpoint (incl. props + 10×-cost historical, dry-run by default), quota guard, sport-key discovery, bronze/silver odds with production parsing + de-vig |
| 02 | Polymarket full run | 1,447-market universe + 2.09M-trade tape from the production capture DB; live gamma/books/history/holders; canonical events/outcomes/quotes |
| 03 | market matching | PM↔sportsbook links on teams+kickoff+type+line+period+**settlement** (90min vs ET+pens firewall), overrides file, accepted/rejected/ambiguous |
| 04 | pre-match convergence & volume | exact 48/24/0h marks (+72/12/6/3/1), observed/reconstructed/unavailable labels, model-edge convergence, volume velocity/VWAP, ex-post close benchmark (explicitly labelled) |
| 05 | in-play analytics | live-window tape: price paths, flow, jumps, suspension gaps, printed-volume executable size, hypothetical entry/exit traces (wall-clock labelled — no fake match clock) |
| 06 | decision pipeline | production `wca_betrecs` builders stage-by-stage: pools → singles → props → advancement, funnel with reject reasons, per-candidate decision traces, **parity check vs the shipped feed** |
| 07 | parameter stress tests | 118 real candidates (53 settled 1X2 with real labels + 65 open advancement); one-at-a-time + grid sweeps, gate-rejection ranking, calibration, realized P&L (settled only, n stated) |
| 08 | value areas | per-area tables: match 1X2 (selection rules encoded), advancement (PRIMARY), outrights vs sim, props (honestly dead), in-play, PM-internal arb coherence, boost/promo EV machinery |

Notebook sources live in `notebooks_src/` (jupytext percent scripts —
reviewable diffs); `tools/build_notebooks.py` converts + executes them into
`notebooks/`. The committed `.ipynb` always reflect a clean full run.

## Data architecture

`data/raw` (immutable gzip JSON + meta sidecars: endpoint, redacted params,
headers, status, retrieval UTC, sha256) → `bronze` (source-shaped Parquet)
→ `silver` (canonical IDs: events/markets/outcomes/quotes/trades) → `gold`
(fair values, candidates, decisions, stress datasets). Every dataset build
is recorded in `data/catalog.parquet` (rows, schema hash, inputs, notebook,
file sha256). All layers are gitignored — rebuild with the commands above.

Canonical IDs (`lib/ids.py`): settlement basis is **part of the market ID**,
so 90-minute 1X2 and ET+pens advancement can never silently merge.

## Guarantees & limits (read before trusting a number)

* **No fabricated data.** Every figure comes from a computation run in the
  notebook, with n stated; gaps are labelled `unavailable`, estimates are
  labelled estimates (e.g. the 2¢ spread assumption in 07 — and it's swept).
* **No look-ahead.** Historical decisions only see data time-stamped before
  their mark (`guard_lookahead`, `value_at_mark` — unit-tested). Closing
  prices appear only in `_expost`-suffixed benchmark columns.
* **No execution.** Read-only SQLite everywhere; no trading endpoints; no
  `pm_parked` writes; keys never printed (names-only checks).
* Dev-box `wca.db` is a stale ledger copy — pool numbers in 06 carry that
  caveat. The canonical ledger lives on the Mac mini.
* Historical PM order books were never captured → historical spread/depth
  are honestly unavailable. Run
  `../.venv/bin/python tools/pm_snapshot_collector.py --minutes 180` during
  open markets to start capturing them.

## Troubleshooting

* **Gamma/CLOB unreachable** → VPN down or curl-vs-python TLS quirk; cells
  record the failure and continue. Reconnect NordVPN and rerun 02.
* **`SkippedCall: over credit budget`** → raise `max_credits` in
  `config.yaml` (Odds API quota is real money — check the printed
  provider-remaining header first).
* **`FileNotFoundError: <layer>/<dataset> not built`** → run the earlier
  notebook named in the error (00's catalog shows what exists).
* **Parity diffs in 06** → the shipped feed was built from different feed
  snapshots; compare its `meta.generated` to the feed-age table.
