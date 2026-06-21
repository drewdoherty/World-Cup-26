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

## Caveats
- Do the final seed during a quiet window; reconcile any divergence between the two existing
  ledgers (mini vs laptop) **first** so the seed is correct.
- Local `data/backups/` rotation (from `deploy/macmini/backup.sh`) still applies as disaster
  recovery; point it at a periodic `turso db dump` once migrated.
