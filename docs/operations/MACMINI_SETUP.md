# Mac mini — production host setup (1-shot)

**Run this ON THE MAC MINI** (not the MacBook). Either:
- open a Claude Code session in the repo on the mini and say *"execute docs/operations/MACMINI_SETUP.md step by step, stopping if anything looks wrong"*, **or**
- `ssh mini` from the MacBook and paste each block.

A markdown alone does nothing — these steps must actually run on the mini.

## Goal
Make the Mac mini the **single** production host: it runs the bot + daemons + ledger,
auto-restarts them, and auto-pulls code from GitHub `main`. The MacBook becomes dev-only.

## Pre-flight (confirm, don't assume)
```
cd ~/path/to/"World Cup Alpha"        # adjust to the real path on the mini
git rev-parse --show-toplevel          # confirm you're in the repo
git switch main && git pull --ff-only origin main
ls deploy/macmini/ docs/operations/    # the deploy bundle must be present
```
If `deploy/macmini/` is missing, the deploy PR isn't merged yet — merge it on GitHub, then
re-run the pull.

## Step 1 — snapshot the live ledger first (safety)
```
bash deploy/macmini/backup.sh          # writes data/backups/wca_<ts>.db
```

## Step 2 — sanity-check secrets (do NOT print the key)
```
grep -c '^POLYMARKET_PRIVATE_KEY=' .env     # expect 1
grep '^POLYMARKET_FUNDER=' .env             # public address — confirm it's YOUR trading proxy
grep '^PM_DRY_RUN=' .env                     # 0 = live orders armed; set how you intend
./.venv/bin/python scripts/wca_pm_probe.py --env .env 2>&1 | grep -i 'funder\|balance\|class'
```
The probe's `Funder (maker)` must be the wallet that actually holds your trading funds, and
the derived EOA must own it. If they don't match, fix `.env` before going further.

## Step 3 — install the services
```
bash deploy/macmini/install.sh
launchctl list | grep com.wca
# expect: com.wca.bot com.wca.snapshotd com.wca.newsd com.wca.promosd
#         com.wca.autopull com.wca.buildcard com.wca.backup
```
This gives auto-restart + start-at-boot, single-instance locking, and 5-min auto-pull of `main`.

## Step 4 — verify it's healthy and is the ONLY bot
```
tail -n 30 logs/bot.log        # must NOT show "409 Conflict"
```
If you see 409, another bot is polling elsewhere — stop every other bot (on the MacBook or any
worktree) until this is the only one.

## Step 5 — report
Report back: which `com.wca.*` services are running, current `git rev-parse --short HEAD`,
probe funder/balance, and any 409 or error lines from `logs/`.

## Daily ops (after setup)
- **Deploy code:** merge a PR to `main` on GitHub → the mini's `autopull` pulls within 5 min and
  restarts changed daemons. No manual deploy.
- **Health:** `launchctl list | grep com.wca; tail -n 30 logs/bot.log`
- **Force restart:** `launchctl kickstart -k gui/$(id -u)/com.wca.bot`
- **Recover state:** `cp data/backups/wca_<ts>.db data/wca.db` then restart daemons.
- **Roll back services:** `bash deploy/macmini/uninstall.sh`
