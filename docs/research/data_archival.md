# Data Archival & Backtest Store

**Status:** built (this PR). Local parquet path works immediately; cloud mirror
is the next, creds-gated layer.

## Why

Every edge we have is in **execution/cost**, not alpha (see the market-
microstructure recon). To benchmark betting *speed* and *accuracy* as volume
grows — and to backtest systematically across the three API venues — we need a
durable, point-in-time record of **exactly what we saw when we decided**. The
live `odds_snapshots` table + ad-hoc `data/raw/snapshots/*.json` cover only
TheOddsAPI h2h/totals and are not partitioned for analytic reads. This pipeline
tees **everything ingested** into partitioned, snappy parquet plus periodic
ledger snapshots.

Goals: (1) lossless raw capture of every API payload; (2) normalized, query-
ready tables for backtests; (3) point-in-time ledger state so a backtest can be
graded against the bets actually on the book; (4) zero impact on betting
behavior (additive TEE, crash-proof, never mutates `wca.db`).

## Storage target — recommendation

**Recommended: cloud object storage (Cloudflare R2 or Backblaze B2), with the
local `data/archive` directory as the always-on primary + an optional external
drive as a cold local mirror.**

| Option | Durability | Cost (this volume) | Backtest access | Failure risk |
|---|---|---|---|---|
| **Cloud object storage** (R2 / B2 / S3) | 11 nines, off-site, versioned | R2 ~$0.015/GB‑mo, **$0 egress**; B2 ~$0.006/GB‑mo | Readable from mini, this dev box, and CI/cloud backtests with one set of creds | Provider outage only; no single-disk SPOF |
| **External drive** | Single disk; no redundancy | $0 marginal | Only when physically attached to one machine | **Drive failure = total loss**; not reachable from CI/cloud |
| **Local `data/archive` only** | Same disk as everything else | $0 | Local only | Lost with the mini |

Reasoning:

- **Volume is tiny.** Odds snapshots are a few hundred small payloads/day;
  parquet+snappy is highly compressible. Even a full World Cup is single-digit
  GB → **R2/B2 cost is cents/month** and R2 has **no egress fees**, so pulling
  the whole archive into a backtest is free.
- **Backtest accessibility is the deciding factor.** Backtests should run
  anywhere (CI, a cloud box, this dev box) without SSH-ing the mini or plugging
  in a drive. Object storage gives every consumer the same read path.
- **A single external drive is the worst durability profile** for the one asset
  we most want to keep — it has no redundancy and is offline most of the time.
  It's fine as an *extra* cold mirror, not as the system of record.

**What you must provide to enable cloud:** an R2/B2/S3 bucket + keys, set in the
mini's `.env` (and as GitHub repo secrets for the optional CI path):

```
WCA_ARCHIVE_S3_BUCKET=wca-archive
WCA_ARCHIVE_S3_ENDPOINT=https://<accountid>.r2.cloudflarestorage.com   # omit for AWS S3
WCA_ARCHIVE_S3_ACCESS_KEY_ID=...
WCA_ARCHIVE_S3_SECRET_ACCESS_KEY=...
WCA_ARCHIVE_S3_REGION=auto            # 'auto' for R2
WCA_ARCHIVE_S3_PREFIX=wca/archive     # optional key prefix
# plus: pip install -e '.[cloud]'   (boto3)
```

Until those are set, **everything works against the local directory** (default
`data/archive`, override with `WCA_ARCHIVE_DIR`). To add the external drive as a
local mirror, point `WCA_ARCHIVE_DIR` at the drive (or rsync `data/archive`
there on a cron). Secrets live only in `.env`; they are never written into the
archive.

## Layout & schema

Hive-partitioned, **snappy**-compressed parquet, uniform partitioning by
**`date` / `venue` / `market`**. Part files are *self-describing* (partition
columns are also kept in-file), so read with `pyarrow.dataset.dataset(path)`
(default `partitioning=None`) and the directory names are ignored — in-file
columns drive every filter.

```
data/archive/
  raw/               date=…/venue={oddsapi|polymarket|betfair|model}/market=…/part-*.parquet
  odds/              date=…/venue={theoddsapi|betfair|polymarket|merged}/market={h2h|totals|…}/…
  model_predictions/ date=…/venue=model/market=predictions/…
  ledger_bets/       date=…/venue=ledger/market=bets/…
  ledger_db/         date=…/wca-<stamp>.db.gz          (compressed full-DB copies)
  <dataset>/_manifest.jsonl                            (append-only index, dedup keys)
```

Datasets (full field lists in `src/wca/archive/schemas.py`):

- **`raw`** — one row per fetched payload, JSON kept verbatim:
  `ts_utc, date, venue, market, kind, sha256, n_bytes, payload_json`. Covers
  OddsAPI (`odds`/`event_odds`/`scores`), Polymarket (Gamma endpoints), Betfair
  (each JSON-RPC method), and model builds. `sha256` is the idempotency key.
- **`odds`** — normalized 1X2/totals rows flattened from a `get_odds` frame:
  `ts_utc, date, venue, market, event_id, commence_time, home_team, away_team,
  bookmaker_key, selection, point, decimal_odds`.
- **`model_predictions`** — one row per fixture per build: `ts_utc, match_id,
  fixture, kickoff, p_home, p_draw, p_away, lambda_home, lambda_away,
  payload_json`.
- **`ledger_bets`** — point-in-time export of the `bets` table (the bet's own
  market is stored as `bet_market` so it doesn't collide with the `market`
  partition).

**Schema stability** is enforced: each dataset has a fixed, explicitly-typed
pyarrow schema and rows are coerced/filled via `from_pylist(rows, schema=…)`, so
appends made months apart concatenate with no column drift. **Append
idempotency**: each write carries a content-hash `dedup_key` recorded in the
manifest; a repeat key is skipped whole.

## What's wired

- **Inline TEE** (additive, crash-proof, guarded import — never changes betting
  behavior): `theoddsapi.get_odds/get_event_odds/get_scores`,
  `polymarket._get`, `betfair_exchange._rpc` tee raw payloads;
  `odds_source.get_odds` tees the normalized, source-attributed frame;
  `modelpreds.write_predictions` tees each model build.
- **Scheduled job** `scripts/wca_archive.py snapshot`:
  - read-only **online-backup copy** of `wca.db` → gzip (never mutates the live
    DB), plus a parquet export of the `bets` table;
  - verbatim capture of `card_latest.md`, `next_latest.md`,
    `goalscorers_latest.md`, `model_predictions.json`, `advancement_*.json`.
  - Mini: `archive` interval job every 6 h (`deploy/macmini/services.env` +
    `install.sh`; activate by re-running `install.sh` on the mini).
  - CI option: `.github/workflows/archive.yml` (daily; files-only; cloud mirror
    if secrets set; uploads an artifact otherwise).
- **Storage backend abstraction** (`src/wca/archive/backends.py`): local is the
  source of truth; an S3-compatible mirror is built only when creds are complete
  **and** boto3 imports — otherwise it **degrades to local-only**.

## How to query for backtesting

```python
import pyarrow.dataset as ds
import pandas as pd

ARCHIVE = "data/archive"   # or s3://… via pyarrow.fs once cloud is on

# All h2h odds we ever saw, any venue:
odds = ds.dataset(f"{ARCHIVE}/odds").to_table(
    filter=ds.field("market") == "h2h"
).to_pandas()

# Model 1X2 by fixture:
preds = ds.dataset(f"{ARCHIVE}/model_predictions").to_table().to_pandas()

# Point-in-time bets as of the latest snapshot:
bets = ds.dataset(f"{ARCHIVE}/ledger_bets").to_table().to_pandas()
latest = bets[bets.snapshot_ts == bets.snapshot_ts.max()]

# Raw payloads for a venue/day (lossless replay):
raw = ds.dataset(f"{ARCHIVE}/raw").to_table(
    filter=(ds.field("venue") == "betfair") & (ds.field("date") == "2026-06-26")
).to_pandas()
payloads = [pd.read_json(s) for s in raw.payload_json]  # or json.loads
```

Partition pruning on `date`/`venue`/`market` keeps scans cheap as volume grows.
The `_manifest.jsonl` per dataset is a quick index of every part file + its
dedup key.

## Configuration summary

| Env var | Default | Purpose |
|---|---|---|
| `WCA_ARCHIVE_DIR` | `data/archive` | local archive root (point at a drive to mirror) |
| `WCA_ARCHIVE_ENABLED` | `1` | `0` disables every TEE (pure no-op) |
| `WCA_ARCHIVE_S3_BUCKET` / `_ENDPOINT` / `_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` / `_REGION` / `_PREFIX` | unset | cloud mirror (all-or-nothing; degrades to local) |

## Next layers (not in this PR)

- Turn on the cloud mirror once a bucket exists (creds → `.env` + repo secrets).
- Optional periodic **compaction** of many small part files into daily files.
- A backtest harness that reads `odds` + `model_predictions` + `ledger_bets` to
  measure CLV/accuracy and execution latency as the sample grows.
