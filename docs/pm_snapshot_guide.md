# Polymarket Price History Snapshots

## Overview

The snapshot system captures Polymarket share prices for all World Cup advancement markets twice-hourly, storing them in:
- **`data/pm_price_history.jsonl`** — Append-only versioned dataset (tracked in git)
- **`data/wca.db` pm_snapshots table** — Indexed database for fast time-series queries

This provides the price trajectory needed to analyze convergence and edge for outright/advancement markets, where CLV is undefined (no fixed close, correlated outcomes).

## Schedule

**Frequency:** Twice-hourly (at :07 and :37 UTC past each hour)  
**Workflow:** `.github/workflows/pm-snapshot.yml`  
**Mini-independent:** Runs in GitHub Actions cloud so history accrues even when the mini sleeps

## What's Captured

Each snapshot records all World Cup advancement markets:
- **32 teams** × **5 stages** = 155 advancement markets (minus Haiti R32 knockout)
- **Fields per snapshot:**
  - `ts_utc` — Capture timestamp
  - `kind` — "advancement" (or "outright", "knockout", etc.)
  - `team` — Team name
  - `stage` — R32, R16, QF, SF, Final, win, group_winner
  - `pm_mid` — Polymarket YES price (implied probability 0–1)
  - `model_prob` — WCA model probability at capture time
  - `market_slug` — Stable market identifier (team:stage)
  - `token_id` — Polymarket token address (for linking to live orders)
  - `raw` — Full market snapshot JSON (optional, for audit)

## Storage

### JSONL Dataset (Versioned)

File: `data/pm_price_history.jsonl`  
Format: One JSON record per line, append-only

```json
{"ts_utc": "2026-06-29 07:15 UTC", "kind": "advancement", "team": "Brazil", "stage": "QF", "pm_mid": 0.495, "model_prob": 0.607, ...}
```

**Size estimate:**
- Current: ~0.4 MB (155 markets × 13 days data)
- Full tournament (30 days @ 48 snapshots/day): ~18 MB
- Git-friendly, no compression needed

### Database (Indexed)

Table: `pm_snapshots` in `data/wca.db`

```sql
CREATE TABLE pm_snapshots (
  ts_utc TEXT NOT NULL,
  kind TEXT NOT NULL,
  team TEXT,
  stage TEXT,
  market_slug TEXT,
  token_id TEXT,
  pm_mid REAL,
  model_prob REAL,
  raw TEXT
);
CREATE INDEX ix_pm_snap_market ON pm_snapshots(kind, team, stage, ts_utc);
```

**Performance:**
- Indexed on (kind, team, stage, ts_utc) for fast market traversal
- ~1-2 MB for full tournament
- Direct SQL queries for aggregation/analysis

## Usage

### Query Single Market

```bash
PYTHONPATH=src python3 scripts/wca_pm_analysis.py --team Brazil --stage QF
```

Output: Latest 10 snapshots (price, model probability, edge)

### Convergence Analysis

Check if Polymarket is drifting toward or away from model probability:

```bash
PYTHONPATH=src python3 scripts/wca_pm_analysis.py --team Brazil --stage QF --convergence
```

Shows:
- Entry vs latest PM price
- Distance to model probability (entry and now)
- Convergence rate (% reduction in distance)
- Price/model direction trends

### Market Statistics

Show all markets, ranked by edge (model advantage vs PM):

```bash
PYTHONPATH=src python3 scripts/wca_pm_analysis.py --market-stats
```

### Direct SQL Queries

```python
import sqlite3

con = sqlite3.connect('data/wca.db')
cursor = con.cursor()

# All Brazil snapshots this hour
cursor.execute("""
  SELECT ts_utc, stage, pm_mid, model_prob 
  FROM pm_snapshots 
  WHERE team = 'Brazil' AND ts_utc > datetime('now', '-1 hour')
  ORDER BY ts_utc DESC, stage
""")

# Markets with >10% edge
cursor.execute("""
  SELECT team, stage, pm_mid, model_prob, 
         (model_prob - pm_mid) * 100 as edge
  FROM pm_snapshots 
  WHERE ts_utc = (SELECT MAX(ts_utc) FROM pm_snapshots)
    AND ABS(model_prob - pm_mid) > 0.1
  ORDER BY edge DESC
""")
```

## Integration Points

### Outright Edge Metrics

The feed `scripts/wca_outright_edge_data.py` uses these snapshots to compute:
- **Convergence** (leading) — Does PM drift toward model over holding period?
  - Needs only 2+ snapshots per market
  - Live capture_fraction when ≥2 captures
- **Calibration** (lagging) — Historical reliability of model
  - Needs outcome resolution (at market close)
  - Cluster-aware effective N to avoid overcounting
- **Paired Skill** — Brier skill + log-loss comparison
- **Information Coefficient** — Spearman correlation (edge ↔ outcome)

### Site Analytics

Feed output → `site-analytics/data/outright_edge.json` → "Outright Edge" dashboard tab

Honest state indicators:
- `COLLECTING` — Not enough data yet
- `INSUFFICIENT` — Low effective N
- Numeric values when valid

## Workflow Details

**Trigger:** Cron `7,37 * * * *` (every hour at :07 and :37 UTC)  
**Steps:**
1. Checkout main (latest committed pm_snapshots)
2. Refresh `site/advancement_data.json` (model + PM pairing)
   - Reuses existing PM snapshot from card build
   - No extra Polymarket API calls
3. Run `wca_pm_snapshot.py`:
   - Parses advancement feed
   - Appends to JSONL (with dedup check)
   - Writes to pm_snapshots table
4. Commit JSONL to git (if changed)
5. Log summary stats to `logs/pm_snapshot.log`

**Logs:** Check `.github/workflows/pm-snapshot.yml` run output or `logs/pm_snapshot.log`

## Data Quality

- **Freshness:** Captures PM prices as soon as advancement feed refreshes
  - Card build updates model → feed runs → snapshot captures
  - Typical delay: <5 minutes from model generation
- **Idempotency:** Re-running with same advancement feed produces same snapshot (no duplicates)
- **Durable:** JSONL is versioned in git; database is local (auto-regenerated from JSONL if lost)

## Examples

### Find strongest model edges (Brazil perspective)

```python
import sqlite3

con = sqlite3.connect('data/wca.db')
cursor = con.cursor()
cursor.execute("""
  SELECT team, stage, pm_mid, model_prob, 
         ABS(model_prob - pm_mid) as abs_edge
  FROM pm_snapshots 
  WHERE ts_utc = (SELECT MAX(ts_utc) FROM pm_snapshots)
    AND model_prob > pm_mid
  ORDER BY abs_edge DESC LIMIT 5
""")
for row in cursor.fetchall():
    print(f"{row[0]:15} {row[1]:5} | Model +{row[4]:.1%} vs PM")
```

### Track price movement for a position

```python
cursor.execute("""
  SELECT ts_utc, pm_mid FROM pm_snapshots 
  WHERE team = 'Brazil' AND stage = 'QF'
  ORDER BY ts_utc ASC
""")

for ts, price in cursor.fetchall():
    change = (price / 0.5037 - 1) * 100  # % change from entry
    print(f"{ts}: {price:.4f} ({change:+.1f}%)")
```

### Export for backtesting

```python
import json

cursor.execute("""
  SELECT team, stage, ts_utc, pm_mid, model_prob FROM pm_snapshots
  WHERE kind = 'advancement'
  ORDER BY ts_utc, team, stage
""")

with open('pm_trajectory.jsonl', 'w') as f:
    for team, stage, ts, pm_mid, model_prob in cursor.fetchall():
        f.write(json.dumps({
            'team': team, 'stage': stage, 'ts': ts,
            'pm': pm_mid, 'model': model_prob
        }) + '\n')
```

## Future Enhancements

- [ ] Wire resolved-outcomes source for lagging metric computation
- [ ] Backfill to beginning of tournament (currently starts from merge)
- [ ] Add mini launchd job for local pm_snapshots persistence
- [ ] Dashboard: time-series chart of selected market prices
- [ ] Alert on large convergence jumps (>5% in one hour)
