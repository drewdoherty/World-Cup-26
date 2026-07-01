# State source of truth → Turso (libSQL)

**Why:** today the ledger is a per-machine `wca.db` file, so any session that writes on a
different machine forks the ledger. A single hosted DB removes file forks entirely while
keeping the data **private** (unlike committing to the public repo). Turso is SQLite-compatible
(libSQL) so the code change is minimal, and the free tier covers this workload.

This is the only remaining step to fully end state collisions. It needs **your account** and
should be done **with the mini's authoritative DB as the seed**, ideally between matchdays
(not while a game is live).

## Steps

1. **Create the DB** (one-time):
   ```
   curl -sSfL https://get.tur.so/install.sh | bash
   turso auth signup
   turso db create world-cup-alpha
   turso db show world-cup-alpha --url        # -> WCA_DB_URL
   turso db tokens create world-cup-alpha     # -> WCA_DB_AUTH_TOKEN
   ```

2. **Seed it from the authoritative ledger** (the mini's `data/wca.db`):
   ```
   sqlite3 data/wca.db .dump > /tmp/wca_seed.sql
   turso db shell world-cup-alpha < /tmp/wca_seed.sql
   ```

3. **Add credentials** to the mini's `.env` (and to the laptop's `.env.dev` if dev should
   read live state):
   ```
   WCA_DB_URL=libsql://world-cup-alpha-<org>.turso.io
   WCA_DB_AUTH_TOKEN=<token>
   ```

4. **Swap the connection layer.** Centralise DB access behind one helper that, when
   `WCA_DB_URL` is set, connects via libSQL instead of `sqlite3.connect(path)`:
   ```python
   # wca/db.py
   import os
   def connect():
       url = os.environ.get("WCA_DB_URL")
       if url:
           import libsql_experimental as libsql      # pip install libsql-experimental
           return libsql.connect(database=url, auth_token=os.environ["WCA_DB_AUTH_TOKEN"])
       import sqlite3
       return sqlite3.connect(os.environ.get("WCA_DB_PATH", "data/wca.db"))
   ```
   Then route ledger/odds/parked writes through `wca.db.connect()`. Keep the file path as the
   fallback so dev with no token still works offline.

5. **Cut over:** stop daemons, do the final seed (step 2), set the env, restart. From then on
   every machine/session shares one private DB — forks become impossible.

## Implementation (shipped on `feat/turso-cloud-publish`)

### `wca.db.connect()` — the single connection choke point
`src/wca/db.py` provides `connect(db_path=None)`:

- **`WCA_DB_URL` set** (`libsql://…`): lazily `import libsql_experimental as libsql` and
  return `libsql.connect(database=WCA_DB_URL, auth_token=WCA_DB_AUTH_TOKEN)` against the shared
  Turso database. `libsql-experimental` is imported **only** on this branch, so it stays an
  optional dependency.
- **`WCA_DB_URL` unset** (dev, tests, the mini until cut-over): `sqlite3.connect(db_path or
  $WCA_DB_PATH or data/wca.db)` with `row_factory = sqlite3.Row` — byte-for-byte the legacy
  behaviour.

The ledger store now routes through it: `wca.ledger.store._connect(db_path)` calls
`wca.db.connect(db_path)` and then re-applies `row_factory = sqlite3.Row` plus
`PRAGMA journal_mode=WAL` / `PRAGMA foreign_keys=ON`. When `WCA_DB_URL` is unset this is
identical to the old inline `sqlite3.connect`; when it is set those SQLite-only steps are applied
best-effort (the experimental libSQL client may not support them). The store's public function
signatures and every call site are unchanged.

> **Row access on libSQL.** The experimental client does not expose a settable `row_factory`
> the way `sqlite3` does, so name-indexed rows (`row["col"]`) are not guaranteed on the Turso
> path. This is set best-effort and swallowed if unsupported. The libSQL write/read path is
> **not exercised by the test suite** (there is no live Turso instance in CI), so treat it as
> validated only after a manual smoke test against the real database.

### `publish-site.yml` — 2×/hour cloud publish
`.github/workflows/publish-site.yml` regenerates the public site feeds from the shared Turso DB
and commits the changed `site/*.json` — independent of the Mac mini:

- **Triggers:** `schedule: cron "0,30 * * * *"` (every 30 min) and `workflow_dispatch`.
- **Missing-secret guard:** a first step checks `secrets.WCA_DB_URL`; if empty it sets
  `configured=false` and every later step is `if: steps.guard.outputs.configured == 'true'`, so
  the run is a clean **no-op (exit 0)**, never a red failure. Safe to merge before cut-over.
- **Steps when configured:** checkout `main` (full history) → Python 3.11 → `pip install -r
  requirements.txt && pip install -e . && pip install libsql-experimental` → export
  `WCA_DB_URL`/`WCA_DB_AUTH_TOKEN` from secrets → run the **same feed generators** as
  `deploy/publish_site.sh` (`wca_scores_data`, `wca_forest_data`, `wca_site`,
  `wca_tracking_data`, `wca_exposure_data`, the `publish_dashboard_json` one-liner,
  `wca_advancement_history`, `wca_advancement_data`) with `PYTHONPATH=src` → commit changed
  `site/*.json` with author **and** committer
  `drewdoherty <132697109+drewdoherty@users.noreply.github.com>` (this exact address — Vercel
  rejects other authors) and message `chore(site): 2x/hour cloud publish from Turso [skip ci]`,
  then `git push`. `git diff --cached --quiet || (commit && push)` makes an empty diff a no-op.
- **Read-only:** it never calls the odds API and never writes to the ledger — it only
  regenerates `site/*.json` from existing DB state. A `concurrency: site-data-push` group keeps
  it from racing the other feeds-to-main jobs.

### Required GitHub secrets
| Secret | Value | Used by |
| --- | --- | --- |
| `WCA_DB_URL` | `libsql://world-cup-alpha-<org>.turso.io` | guard + `wca.db.connect()` |
| `WCA_DB_AUTH_TOKEN` | Turso token from `turso db tokens create` | `wca.db.connect()` |

### sqlite fallback keeps dev/tests working with no token
With no `WCA_DB_URL` set, `wca.db.connect()` returns a plain `sqlite3` connection and the
ledger behaves exactly as before, so local development, the full pytest suite, and the mini all
run unchanged and offline until the operator deliberately configures the two secrets and cuts
over.

## Caveats
- Do the final seed during a quiet window; reconcile any divergence between the two existing
  ledgers (mini vs laptop) **first** so the seed is correct.
- Local `data/backups/` rotation (from `deploy/macmini/backup.sh`) still applies as disaster
  recovery; point it at a periodic `turso db dump` once migrated.
