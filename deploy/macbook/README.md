# MacBook positions-sync (cross-machine, open + settled-24h)

This job runs **on the MacBook**, not the Mac mini. The Betfair Exchange API is
only reachable from the MacBook (the VPN lives here); the mini's connection to
Betfair is blocked. But the **canonical ledger** (`data/wca.db`) and the **site
publish** live on the mini (it hosts the other writers — the bot, settler,
closecapture). So the venue-position sync is split:

```
  MacBook (VPN on)                         Mac mini (canonical ledger)
  ─────────────────                        ───────────────────────────
  fetch open + settled-24h  ──snapshot──►  apply against data/wca.db
  (Betfair/Smarkets/PM)        (scp/ssh)   reconcile + (LIVE) write
                                           then publish the site
```

## What it does

`scripts/wca_positions_macbook.sh` (driven by `com.wca.positions.plist`, hourly):

1. **FETCH** locally — `wca_positions_sync.py --fetch-only --out <snapshot>` pulls
   every venue's **open** positions *and* its **settled positions from the last
   24h**. No DB access on the MacBook.
2. **SCP** the self-describing JSON snapshot to the mini.
3. **APPLY** on the mini — `--apply-from-snapshot <snapshot> --db data/wca.db`
   reconciles against the canonical ledger and, in LIVE, applies the
   conservative writes:
   - insert positions newly seen at a venue (open),
   - **settle** a matched open ledger bet with the venue's OWN realised P&L +
     result (venue truth — better than re-deriving it),
   - mark gone-from-venue open bets `closed` pending settlement.
4. **Publish** — trigger the mini's `deploy/publish_site.sh` so the site reflects
   the reconciliation (LIVE only).

## SHADOW-first

The plist does **not** set `WCA_POSITIONS_LIVE`, so the mini only **logs** the
proposed inserts/settles/closes — **zero** ledger writes. Inspect a SHADOW run's
report (`data/positions_apply_report.json` on the mini) first. Only once it looks
correct, promote to LIVE by uncommenting `WCA_POSITIONS_LIVE=1` in the plist (or
exporting it before running the wrapper) and re-loading the plist.

## The 24h window

The lookback (`--settled-lookback-hours`, default **24**) scopes only the
**settled** fetch — open positions are always all-current. 24h means the first
run captures a full day of settles; subsequent hourly runs overlap heavily, and
re-applying the same settle is a **no-op** (an already-settled bet is no longer
`open`, so the status-guarded update touches nothing).

## Conservatism (auto-settle safety)

A settle is applied **only** when the venue reports an unambiguous `won`/`lost`
result **and** a numeric realised P&L **and** it matches exactly **one** open
ledger bet (and the same selection is not still showing open at the venue). Any
ambiguity routes to a `review` list and is **never** auto-settled. The job is
read-only on the venues — it never places or cancels orders.

## Install

```bash
# 1. Edit the absolute paths in com.wca.positions.plist if your checkout differs.
cp deploy/macbook/com.wca.positions.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.wca.positions.plist 2>/dev/null || true
launchctl load   ~/Library/LaunchAgents/com.wca.positions.plist

# Run once by hand first (SHADOW) and read the report:
bash scripts/wca_positions_macbook.sh
ssh andrewdoherty@Drews-Mac-mini.local 'cat /Users/andrewdoherty/World-Cup-26/data/positions_apply_report.json'
```

Prereqs on the MacBook: the **Betfair VPN must be ON**, Betfair/Smarkets creds in
`.env`, and key-based SSH to `andrewdoherty@Drews-Mac-mini.local` (Remote Login
ON on the mini).

Override defaults via env (or the plist's `EnvironmentVariables`):
`WCA_MINI_HOST`, `WCA_MINI_REPO`, `WCA_MINI_PY`, `WCA_MINI_DB`,
`WCA_SETTLED_LOOKBACK_HOURS`, `WCA_POSITIONS_LIVE`, `WCA_POSITIONS_PUBLISH`.
