# World Cup Alpha operations

**Reconciled:** 2026-07-17. This is the current runbook for the MacBook/Mac mini
deployment. It deliberately excludes live-position advice.

## 1. Stop conditions

Stop before running a command if any of these is unclear:

- which machine owns the operation;
- whether the database is canonical or a development copy;
- whether `PM_DRY_RUN` is explicitly safe;
- whether the command can sign, submit, settle, reconcile, or publish;
- whether the market settles at 90 minutes or after ET/penalties;
- whether the worktree contains another agent's uncommitted changes.

Never print `.env`, tokens, private keys, or credentials while diagnosing a
service. Never use a stale recommendation or local ledger copy for a money
decision.

## 2. Machines

### Mac mini - production

- SSH: `andrewdoherty@Drews-Mac-mini.local`.
- Repository: `~/World-Cup-26`.
- Python: repository `.venv`.
- Canonical ledger: `~/World-Cup-26/data/wca.db`.
- Production environment: `.env`; conductor environment: `.env.conductor`.
- launchd labels: `com.wca.*`.
- Local rotating ledger backups: `data/backups/`, every 15 minutes when the
  configured backup job is active.

Use the mDNS hostname. The historical `192.168.68.55` address is stale.

### MacBook - development and market-data gateway

- Repository: `~/Desktop/Coding/World Cup Alpha`.
- Python: `./.venv/bin/python`.
- Development environment: `.env.dev` and `data/dev.db`.
- PM access: NordVPN is required; native TLS is blocked.
- Hyperliquid public API: historically reachable from the MacBook with or
  without the PM VPN; verify per session and do not assume mini reachability.
- Local launchd: feed pull and cross-machine positions sync are separate from
  mini production jobs.

MacBook copies of `data/wca.db` and `data/dev.db` are not canonical. Never
settle or report live exposure from them.

## 3. Development workflow

Before editing:

```bash
git branch --show-current
git status --short
git fetch origin
```

Use a `codex/<topic>` branch. Do not discard unrelated dirty files. For code
changes, run focused tests and then:

```bash
./.venv/bin/pytest -q
git diff --check
```

The local merge driver must be installed once per checkout:

```bash
bash scripts/wca_install_merge_driver.sh
git config --get merge.freshest.driver
```

Generated feed conflicts should be resolved by rebuilding from current inputs,
not by hand-combining JSON.

## 4. Safe development environment

Do not rely on inherited environment state. Prefix PM-related development
commands explicitly:

```bash
PM_DRY_RUN=1 WCA_DB_PATH=data/dev.db \
  ./.venv/bin/python <script> --db data/dev.db
```

Spawned development agents must not receive `POLYMARKET_PRIVATE_KEY`. A live
signing key does not belong in a general MacBook shell.

## 5. Telegram development conductor

The `@WorldCupDev` conductor is a separate development service, process, token,
and state database from the `@gamble1_bot` operations bot. It is PR-only and
must never point at the canonical ledger or import an execution path.

Current safety boundaries:

- launcher environment should explicitly set `WCA_DB_PATH=data/dev.db`;
- every spawned task is forced to `PM_DRY_RUN=1` and
  `WCA_DB_PATH=data/dev.db`;
- `POLYMARKET_PRIVATE_KEY` is removed from the spawned environment;
- all other signing-key aliases must be absent from the conductor's source
  environment because the current strip list names only the canonical key;
- each task runs in a fresh worktree/branch and opens a PR rather than pushing
  `main`;
- tasks are isolated and cannot refer to another task's memory or output;
- duplicate feature requests must be detected before dispatch;
- concurrency defaults to one because parallel runs raced the shared git
  worktree registry.

Keep one feature per conductor task. A follow-up belongs on the same branch;
related overlapping branches should be integrated once, not merged in parallel.
Codex work in the current desktop task is independent of this service.

## 6. Polymarket network route

The configured NordVPN service identifier on the MacBook is:

```text
6C86610D-9709-483C-BA69-40A82DF9ABCD
```

Start it and verify connectivity before any PM public-data refresh:

```bash
scutil --nc start "6C86610D-9709-483C-BA69-40A82DF9ABCD"
scutil --nc status "6C86610D-9709-483C-BA69-40A82DF9ABCD"
curl -sS -o /dev/null -w '%{http_code}\n' https://clob.polymarket.com/
```

HTTP 200 confirms the CLOB route. TLS errors or HTTP code `000` mean the route
is blocked. The VPN can affect LAN/SSH routing; verify PM and mini connectivity
for each task instead of assuming both work simultaneously.

## 7. Complete event forest

Run the full builder only on a PM-capable host, with dry-run forced:

```bash
PM_DRY_RUN=1 WCA_DB_PATH=data/dev.db PYTHONPATH=src \
  ./.venv/bin/python scripts/wca_event_markets.py \
  --env .env.dev --db data/dev.db --days-ahead 7
```

Outputs:

- `site/forest_data.json`: complete priced/modelled/market-only forest;
- `site/event_market_recs.json`: governed recommendation subset.

The builder protects an existing priced feed when a blind run captures zero
market prices. Do not use `--force-blind` during ordinary operations.

The current primary publisher preserves these feeds. It rebuilds the full
forest only when `WCA_EVENT_MARKETS=1` is set on a PM-capable host. The mini is
not that host by default. The GitHub workflows still call the legacy reduced
forest builder; this is tracked in `TODO.md` because it can clobber full
coverage.

Concurrent worktree files define an hourly MacBook `com.wca.research` cycle
for the full forest, HL snapshot, and shadow book. They are not yet tracked or
verified as installed. Review the database input before activation: the draft
uses the MacBook `data/wca.db`, which is non-canonical and must not silently
contribute stale realised P&L to sizing.

## 8. Shadow book

The shadow book is paper-only and separate from the money ledger.

Run a cycle:

```bash
PM_DRY_RUN=1 PYTHONPATH=src \
  ./.venv/bin/python scripts/wca_shadow_book.py \
  --db data/shadow_book.db --out site/shadow_book.json cycle
```

Refresh the report without a new decision cycle:

```bash
PYTHONPATH=src ./.venv/bin/python scripts/wca_shadow_book.py \
  --db data/shadow_book.db --out site/shadow_book.json report
```

Settle explicit canonical markets with a JSON list of
`{market_key, outcome}` where outcome is `0`, `0.5`, or `1`:

```bash
PYTHONPATH=src ./.venv/bin/python scripts/wca_shadow_book.py \
  --db data/shadow_book.db --out site/shadow_book.json \
  settle --settlements settlements.json
```

Structured fixture settlement is available through `settle-fixtures`; missing
event fields leave affected observations open. Never fill missing scorer,
corner, half, ET, or penalty results by inference.

`scripts/wca_shadow_book_cycle.sh` adds a lock, pause switches, logging,
`PM_DRY_RUN=1`, and removal of the PM signing key. Pause with either:

```text
WCA_SHADOWBOOK_OFF=1
data/SHADOW_BOOK_PAUSED
```

The launchd definition is not active merely because it exists in git. After
the branch is merged, a human must install it on the mini and verify the job.

## 9. Publishing and serving

The mini's `com.wca.publish` job is the primary feed publisher at a 30-minute
interval. It rebuilds offline/mini-capable feeds, mirrors selected feeds into
`site-analytics/data/`, commits only selected artifacts, rebases, and pushes.

Run manually on the mini only when the worktree is understood:

```bash
bash deploy/publish_site.sh
```

Use `WCA_AUTOPUSH=0` for a diagnostic build that must not push. Do not run the
publisher from a dirty development branch.

Serve local dashboards on the supported ports:

```bash
PORT=8000 ./.venv/bin/python scripts/serve_site.py
PORT=8001 ./.venv/bin/python scripts/serve_analytics.py
```

There is no supported Vercel or other hosted deployment.

Concurrent worktree files also define a loopback-only
`com.wca.analytics-live` MacBook service for ports 8000/8001. Treat it as a
candidate until it is committed, installed, and observed under launchd.

## 10. Deploying code to the mini

Normal flow:

```text
codex branch -> tests -> pull request -> human merge -> origin/main
             -> mini autopull within about five minutes
             -> changed KeepAlive daemons restarted
```

Autopull does not register a new launchd plist. After merging a new or changed
job definition, a human runs on the mini:

```bash
cd ~/World-Cup-26
git pull --rebase origin main
bash deploy/macmini/install.sh
launchctl list | grep com.wca
```

The installer also registers the `merge=freshest` driver for that checkout.

Useful service operations on the mini:

```bash
launchctl kickstart -k gui/501/com.wca.<name>
launchctl bootout gui/501/com.wca.<name>
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.wca.<name>.plist
```

Do not treat an old log tail as proof of liveness; some logs are buffered.
Check the launchd row, process, and expected network/file heartbeat together.

## 11. Canonical ledger reads

Read the mini ledger without mutation. Python is preferred because SQLite CLI
`-readonly` can fail with the live WAL:

```python
import sqlite3

con = sqlite3.connect("data/wca.db")
con.execute("PRAGMA query_only=ON")
try:
    rows = con.execute("SELECT id, status FROM bets ORDER BY id DESC LIMIT 10").fetchall()
finally:
    con.close()
```

Run that code on the mini over SSH. Do not copy a result into a write command
without a separate backup, review, and explicit human approval.

## 12. Match-day capture

PM orderflow has an upstream offset cap. Missing a busy market window can lose
history permanently. On the MacBook with the PM route verified:

```bash
PM_DRY_RUN=1 PYTHONPATH=src ./.venv/bin/python \
  scripts/pm_orderflow_ingest.py --db data/pm_orderflow.db \
  --open-only --workers 8
```

Closing-price capture, settlement, and result refresh must be checked after
each remaining fixture. The exact live position set must come from the mini
ledger and venues at that time.

## 13. Recovery

For a stale site or wedged mini checkout:

1. Inspect `git status`, launchd state, and recent logs.
2. Back up every dirty tracked file and the ledger before destructive action.
3. Determine whether dirty files are current daemon outputs or human work.
4. Only with explicit human approval, reset/reconcile against `origin/main`.
5. Reinstall or kickstart the affected service and verify a fresh timestamp.

Never run `git reset --hard`, drop autostashes, delete worktrees, or overwrite
the canonical ledger as an autonomous recovery step.

## 14. Verification checklist

After a deploy or incident, verify without exposing secrets:

- mini HEAD and distance from `origin/main`;
- dirty tracked files and pending rebases/stashes;
- expected `com.wca.*` jobs loaded and not crash-looping;
- fresh card, scores, forest, exposure, and shadow timestamps;
- canonical ledger backup recency and an off-box restore path;
- PM route only on the intended gateway;
- dry-run posture on development processes;
- no stale or divergent settlement basis in proposed cross-venue rows.
