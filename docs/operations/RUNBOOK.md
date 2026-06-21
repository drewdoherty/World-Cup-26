# World Cup Alpha — Operations Runbook

The fix for the multi-machine / multi-session collisions: **one owner per source of truth, never duplicated.**

| Concern | Source of truth | Who writes it |
|---|---|---|
| Code | GitHub `main` | laptop pushes → Mac mini auto-pulls |
| Live state (ledger, odds, parked orders) | the Mac mini's `data/wca.db` | **Mac mini only** |
| Secrets / wallet | one `.env` on the Mac mini | nobody else |
| Control / dev | Tailscale SSH into the mini | you, from laptop or phone |

**Golden rules**
1. The **Mac mini is the only machine that runs the bot + daemons + ledger.** Never run a second bot anywhere (causes Telegram `409 Conflict` and ledger forks).
2. The **laptop is dev-only:** edit code, push to `main`. It uses a throwaway `.env.dev` with `PM_DRY_RUN=1`. It never writes the real ledger.
3. **Secrets (real wallet key, funder) live only in the mini's `.env`.** Never paste a private key into chat or copy it between machines.

---

## One-time setup (do in this order)

### 0. Designate the authoritative ledger
The mini's `data/wca.db` is the real one (it holds the live wallet + fired orders). Snapshot it before anything:
```
ssh mini 'cd ~/path/to/"World Cup Alpha" && bash deploy/macmini/backup.sh'
```

### 1. Remote access — Tailscale + SSH (this is what makes everything low-touch)
- Install Tailscale on **mini, laptop, and phone**, signed into the same account: https://tailscale.com/download
- On the mini: System Settings → General → Sharing → enable **Remote Login (SSH)**.
- Add an SSH host alias on the laptop (`~/.ssh/config`):
  ```
  Host mini
      HostName <mini-tailscale-ip-or-name>
      User <mini-username>
  ```
- Verify: `ssh mini 'hostname'`. From your phone use Blink/Termius with the same Tailscale identity.
- You can now run Claude Code **on the mini** over that SSH and operate production directly.

### 2. Prepare the mini's checkout
```
ssh mini
cd ~/path/to/"World Cup Alpha"
git checkout main && git pull
./.venv/bin/python -c "import wca"        # venv exists & imports
# Ensure .env has: the ONE real wallet key, matching POLYMARKET_FUNDER, PM_DRY_RUN as you intend.
```

### 3. Install the services
```
bash deploy/macmini/install.sh
launchctl list | grep com.wca       # bot, snapshotd, newsd, promosd, autopull, buildcard, backup
```
This gives every service **auto-restart + start-at-boot** (fixes daemons silently dying) and **single-instance locking** (fixes the 409).

### 4. Make the mini the only bot
- Stop any bot on the laptop / other machines: `pkill -f scripts/wca_bot.py`
- Confirm the mini's bot owns Telegram: `ssh mini 'tail -f logs/bot.log'` — no `409 Conflict` lines.

### 5. Clean up the sprawl (laptop)
```
bash scripts/wca_worktree_cleanup.sh          # review
bash scripts/wca_worktree_cleanup.sh --force  # remove stale forked-DB worktrees
```

---

## Daily operations (low-touch)

- **Deploy code:** push to `main` from the laptop. The mini's `autopull` job (every 5 min) pulls and restarts changed daemons. No manual deploy.
- **Check health:** `ssh mini 'launchctl list | grep com.wca; tail -n 30 logs/bot.log'`
- **Force a restart:** `ssh mini 'launchctl kickstart -k gui/$(id -u)/com.wca.bot'`
- **Operate bets:** through the Telegram bot as today (`/today`, `/bets`, screenshot → `yes`, `Y PM-n`). All writes land in the mini's single ledger.
- **Recover state:** backups in `data/backups/` (15-min rotation, 48 kept). Restore: `cp data/backups/wca_<ts>.db data/wca.db` then restart daemons.

## What still needs a human
- Placing sportsbook bets and confirming `Y PM-n` (by design).
- The cross-machine **state** source of truth is still a single file on the mini. To let dev/other sessions safely share live state, do the **Turso migration** ([turso_migration.md](turso_migration.md)) — the only remaining step to fully kill state forks.
