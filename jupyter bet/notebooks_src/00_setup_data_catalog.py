# ---
# jupyter:
#   jupytext:
#     formats: py:percent
# ---

# %% [markdown]
# # 00 · Setup & data catalog
#
# **Purpose.** Verify the environment, load the typed research config, take a
# census of every REAL data source this environment can see, and explain the
# data architecture the other notebooks rely on.
#
# **Contents**
# 1. [Environment & run manifest](#env)
# 2. [Research parameters](#params)
# 3. [Data architecture (raw → bronze → silver → gold)](#arch)
# 4. [Canonical identifiers](#ids)
# 5. [Real repo data sources census](#census)
# 6. [Raw-layer & dataset catalog (lineage)](#catalog)
# 7. [Connectivity probe](#conn)
# 8. [Findings, caveats, next steps](#findings)
#
# **How to run.** Top-to-bottom, idempotent. `Params.offline = True` makes
# every notebook read cached data only. Copy `config.example.yaml` →
# `config.yaml` to change parameters persistently, or edit the parameter
# cell below for a one-off.

# %% [markdown]
# <a id="env"></a>
# ## 1 · Environment & run manifest
# Everything below is recorded to `outputs/run_logs/` so any result in this
# folder can be traced to package versions + git commit.

# %%
import sys, pathlib
_here = pathlib.Path.cwd().resolve()
JB = next(p for p in [_here, *_here.parents] if (p / "lib" / "bootstrap.py").exists())
sys.path.insert(0, str(JB))

import lib.bootstrap as bt
import pandas as pd

manifest = bt.run_manifest("00_setup_data_catalog")
pd.Series(manifest).to_frame("value")

# %% [markdown]
# Credential **names** present (values are never read into notebook state):

# %%
pd.Series(bt.secret_names_present()).to_frame("configured")

# %% [markdown]
# <a id="params"></a>
# ## 2 · Research parameters
# One typed dataclass drives every notebook. **What to change:** edit
# `config.yaml` (persistent) or pass overrides in a notebook's parameter
# cell (one-off). Unknown keys fail loudly.

# %%
import lib.config as cfg

p = cfg.load_params()          # defaults ← config.yaml (if present)
cfg.write_example_yaml()       # regenerate the example so docs never drift
p.to_frame()

# %% [markdown]
# <a id="arch"></a>
# ## 3 · Data architecture
#
# | layer | dir | contents | guarantees |
# |---|---|---|---|
# | **raw** | `data/raw/<source>/<endpoint>/` | immutable gzip JSON payload + `.meta.json` (endpoint, redacted params, headers, status, retrieval UTC, sha256) | never modified; doubles as the offline cache |
# | **bronze** | `data/bronze/` | source-shaped Parquet, minimal typing | 1:1 traceable to raw snapshots |
# | **silver** | `data/silver/` | canonical events/markets/outcomes/quotes/trades keyed on stable IDs | source IDs always preserved |
# | **gold** | `data/gold/` | features, fair values, opportunities, decisions, stress datasets | every build recorded in the lineage catalog |
#
# All reads/writes go through `lib.storage`, so the lineage table (§6) is
# complete by construction. Heavy transforms use **Polars** (lazy where the
# data is big); conversion to **Pandas** happens only at display/plot
# boundaries and always through `storage.to_pandas(df, reason=...)`.

# %% [markdown]
# <a id="ids"></a>
# ## 4 · Canonical identifiers
# Defined in `lib/ids.py` (unit-tested):
#
# * `competition_id` = `fifa-wc-2026`
# * `event_id` = `wc2026:<home>__<away>__<YYYY-MM-DDTHH>Z` — canonical team
#   names via production `wca.data.teamnames.canonical`, kickoff floored to
#   the hour so venue feed skews collide to one ID.
# * `market_id` = `<event_id>|<type>|<line>|<period>|<settlement>` —
#   **settlement basis is part of the key**: 1X2 settles at 90 minutes, PM
#   advancement includes ET+pens; they can never silently merge.
# * `outcome_id` = `<market_id>|<outcome>`; `snapshot_id` = raw-layer path.

# %%
import datetime as dt
import lib.ids as ids
demo_ko = dt.datetime(2026, 7, 6, 19, 0, tzinfo=dt.timezone.utc)
demo_ev = ids.event_id("Korea Republic", "Czechia", demo_ko)   # alias-proof
{"event_id": demo_ev,
 "market_90min": ids.market_id(demo_ev, "1x2", settlement=ids.S_90MIN),
 "market_etpens": ids.market_id(demo_ev, "advance", settlement=ids.S_ETPENS)}

# %% [markdown]
# <a id="census"></a>
# ## 5 · Real repo data sources census
# Row counts computed NOW from the actual files — nothing asserted from
# memory. The dev-box `wca.db` is a **stale ledger copy** (canonical ledger
# lives on the Mac mini only, read over SSH when needed).

# %%
import json, sqlite3

def _rows(db, sql):
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            c.execute("PRAGMA query_only=ON")
            return c.execute(sql).fetchone()
    except Exception as e:
        return (f"unavailable: {e}",)

census = []
def add(name, path, detail=""):
    path = pathlib.Path(path)
    census.append({
        "source": name, "path": str(path.relative_to(bt.REPO_ROOT)) if str(path).startswith(str(bt.REPO_ROOT)) else str(path),
        "exists": path.exists(),
        "size_mb": round(path.stat().st_size / 1e6, 1) if path.exists() else None,
        "detail": detail})

if bt.ORDERFLOW_DB.exists():
    n_tr, lo, hi = _rows(bt.ORDERFLOW_DB,
        "SELECT count(*), datetime(min(ts),'unixepoch'), datetime(max(ts),'unixepoch') FROM pm_trades")
    n_mk, = _rows(bt.ORDERFLOW_DB, "SELECT count(*) FROM pm_markets")
    add("PM orderflow (production capture)", bt.ORDERFLOW_DB,
        f"{n_tr:,} trades {lo} → {hi} UTC; {n_mk:,} markets")
else:
    add("PM orderflow (production capture)", bt.ORDERFLOW_DB)

if bt.DEV_WCA_DB.exists():
    n_snap, lo, hi = _rows(bt.DEV_WCA_DB,
        "SELECT count(*), min(ts_utc), max(ts_utc) FROM odds_snapshots")
    add("sportsbook odds snapshots (STALE dev copy)", bt.DEV_WCA_DB,
        f"{n_snap:,} rows {lo} → {hi}")
else:
    add("sportsbook odds snapshots (STALE dev copy)", bt.DEV_WCA_DB)

if bt.MODEL_PRED_LOG.exists():
    n_lines = sum(1 for _ in open(bt.MODEL_PRED_LOG))
    add("model 1X2 prediction log", bt.MODEL_PRED_LOG, f"{n_lines:,} rows (jsonl)")
else:
    add("model 1X2 prediction log", bt.MODEL_PRED_LOG)

for name, path, key in [
        ("match results (settled)", bt.RESULTS_JSON, "results"),
        ("advancement model-vs-PM feed", bt.ADVANCEMENT_JSON, "markets"),
        ("shipped bet recommendations", bt.BET_RECS_JSON, "recommendations"),
        ("promos catalog", bt.PROMOS_JSON, "promotions")]:
    if pathlib.Path(path).exists():
        d = json.loads(pathlib.Path(path).read_text())
        items = d.get(key) or d.get("recs") or []
        gen = (d.get("meta") or {}).get("generated") or d.get("generated") or "?"
        add(name, path, f"{len(items)} items; generated {gen}")
    else:
        add(name, path)

add("StatsBomb raw (WC 43/106 history)", bt.REPO_ROOT / "data" / "raw" / "statsbomb")
add("players.db (mini-only — expected absent here)", bt.REPO_ROOT / "data" / "players.db")
add("player_events.db (mini-only — expected absent here)", bt.REPO_ROOT / "data" / "player_events.db")

pd.DataFrame(census)

# %% [markdown]
# <a id="catalog"></a>
# ## 6 · Raw-layer inventory & dataset lineage catalog
# Empty on first run — populated as notebooks 01–08 pull and build.

# %%
import lib.storage as st
raw_inv = st.list_raw()
print(f"raw snapshots: {raw_inv.height}")
raw_inv.tail(15).to_pandas()

# %%
cat = st.catalog()
print(f"datasets (latest builds): {cat.height}")
cat.to_pandas()

# %% [markdown]
# <a id="conn"></a>
# ## 7 · Connectivity probe
# Which live sources are reachable RIGHT NOW (Polymarket needs the VPN
# route on this MacBook; the VPN severs LAN to the mini — known trade-off).
# Skipped entirely when `offline=True`.

# %%
import requests
conn_rows = []
if p.offline:
    conn_rows.append({"target": "(all)", "status": "skipped — offline mode"})
else:
    for name, url in [
            ("polymarket gamma", "https://gamma-api.polymarket.com/events?limit=1"),
            ("polymarket clob", "https://clob.polymarket.com/ok"),
            ("polymarket data-api", "https://data-api.polymarket.com/trades?limit=1"),
            ("the-odds-api (unauth ping)", "https://api.the-odds-api.com/v4/sports?apiKey=invalid")]:
        try:
            r = requests.get(url, timeout=10)
            conn_rows.append({"target": name, "status": f"HTTP {r.status_code} (reachable)"})
        except Exception as e:
            conn_rows.append({"target": name, "status": f"UNREACHABLE: {type(e).__name__}"})
conn_df = pd.DataFrame(conn_rows)
conn_df

# %% [markdown]
# <a id="findings"></a>
# ## 8 · Findings, caveats, next steps
# *(static text; the tables above are the evidence)*
#
# **Findings.** The offline backbone is substantial and real: ~2M captured
# PM trades with per-market metadata, 1.26M sportsbook odds snapshots
# (2026-06-11→23 window), the full model 1X2 prediction log, settled
# results, and a fresh advancement feed. That is enough to run notebooks
# 02–08 fully offline.
#
# **Caveats.**
# * Dev-box `wca.db` ledger tables are stale — bankroll numbers derived from
#   it in notebook 06 are labelled accordingly.
# * Sportsbook odds snapshots end 2026-06-23 on this machine — recent-match
#   sportsbook convergence needs a live Odds API pull (notebook 01) or the
#   mini's DB.
# * Historical PM order books were never captured (books are live-only) —
#   historical spread/depth are honestly `unavailable`, not reconstructed.
#
# **Next.** Run notebook 01 (guarded live odds pull), then 02 (PM universe
# build) — everything downstream reads their datasets from the catalog.

# %%
import lib.plotting as plot
plot.save_table(conn_df, "00_connectivity")
plot.save_table(pd.DataFrame(census), "00_source_census")
print("saved outputs/tables/00_connectivity.csv, 00_source_census.csv")
