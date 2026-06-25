# WCA deployment runbook — Mac Mini (always-on automation)

The Mac Mini is the **single source of truth** for the bet ledger (`data/wca.db`
is gitignored — it lives only on this host) and the home for everything that
must run continuously or touch the wallet/Telegram. Your MacBook Pro stays on
the **UK VPN for placing sportsbook bets manually** — that work never moves here.

## 0. Dev → ops workflow

Code is edited and tested on the **dev machine** (MacBook Pro), pushed to
`origin/main`, and pulled here automatically:

```
Dev machine (MacBook Pro)          Mac Mini (this host, always-on)
  ├─ edit code                       ├─ com.wca.sync pulls origin/main (every 5 min)
  ├─ test locally           ──push──▶│  └─ restarts the bot/snapshot daemons on a code change
  └─ git push main          origin   ├─ runs the Telegram bot + Polymarket proposals
                                      ├─ captures closing lines + publishes the site
                                      └─ launchd KeepAlive auto-restarts crashed daemons
```

This host **keeps its publisher role**: `data/wca.db` lives only here, so the
site feed is generated and pushed from here. `com.wca.sync` therefore updates
code with **`git pull --rebase`** (`deploy/sync.sh`), **never
`git reset --hard`** — a hard reset would discard this machine's own
site/ledger commits and freeze the public site. Deploy by hand any time:

```bash
bash deploy/sync.sh        # pull --rebase origin/main; restart daemons only if code changed
```

## 1. The network split (the crux)

| Service | Network it needs | Why |
|---|---|---|
| Odds API, card, model, site, git | **None** (native Bahrain) | plain HTTPS, no geo-block |
| **Polymarket** (API + on-chain) | **None / non-UK, non-US** | PM geo-blocks US **and UK** — it works natively from Bahrain (crypto-OK), but a **UK VPN would get PM blocked** |
| **Telegram bot** | **A VPN (non-UK, non-US)** | `api.telegram.org` is blocked from Bahrain (your `bot.log` shows DNS failures) |
| UK sportsbook *placement* | UK VPN — **on your MacBook, manual** | not on the Mac Mini |

**Key conflict:** Polymarket and the UK VPN are mutually exclusive (PM blocks UK).
So **do not run this host behind your UK sportsbook VPN.** You only need a VPN
here to unblock **Telegram**, and that VPN must **not** be UK or US.

**Chosen approach: full VPN to an EU region** (simplest — one connection for
everything). Requirements for the region:
- **Not UK, not US** (both geo-blocked by Polymarket).
- **Avoid France** (AMF — Polymarket has restricted FR access). Safe picks:
  **Germany, Netherlands, Ireland, Spain**. Telegram works across all of these.

Set the Mac Mini's VPN to e.g. **Germany or Netherlands**, leave it on
permanently, then verify:

```bash
bash deploy/check_connectivity.sh
```

You want **Odds + Polymarket + Telegram all `OK`** in one shot. If Polymarket
shows blocked, switch EU region and re-run. (Your MacBook keeps its separate UK
VPN for manual sportsbook betting — the two hosts run different VPNs.)

## 2. What needs NO VPN
Odds capture, card build, promos scan, model, site generation, git push,
close-line capture, settlement, **and Polymarket** — all fine on native Bahrain.
Only **Telegram** needs unblocking.

## 3. Services (installed via launchd — macOS's cron+daemon manager)

`bash deploy/install_services.sh` installs these into `~/Library/LaunchAgents`:

| Label | Cadence | Job | Notes |
|---|---|---|---|
| `com.wca.bot` | always-on | Telegram bot | **needs Telegram VPN**; sends PM proposals, takes `Y PM-n` confirmations |
| `com.wca.snapshotd` | always-on | dense odds snapshots near kickoffs | produces the **closing lines** CLV needs |
| `com.wca.closecapture` | every 10 min | stamp `closing_odds`+CLV after each KO | **this was scheduled nowhere — why your CLV froze** |
| `com.wca.build_card` | hourly | rebuild card (fit models + fetch odds) | ensures bot's `/card` is always fresh to within 1 hour |
| `com.wca.pmpropose` | every 12 h | park PM proposals + notify | needs PM **and** Telegram reachable |
| `com.wca.publish` | hourly | refresh scores → regen site → **auto-commit & push** | keeps the public site live with no manual push; rebases to absorb cloud-Action commits |
| `com.wca.sync` | every 5 min | `git pull --rebase origin/main` → restart daemons on code change | the auto-deploy puller; see §0. Logs to `data/com.wca.sync.run.log` |
| `com.wca.watchdog` | every 5 min | check every KeepAlive daemon is alive + not crash-looping | **pings Telegram if one is down/flapping/never-bootstrapped** — KeepAlive respawns silently, so this is the only thing that tells you a bot died. Read-only |

> ⚠️ **Adding a job to `services.env` does not start it.** launchd only learns
> about a new job when `install.sh` runs on the mini (`launchctl load`). A merge
> alone leaves a new daemon dormant — and `autopull`/`sync` only kickstart jobs
> that already exist, they don't register new ones. After any service change,
> SSH to the mini and re-run `bash deploy/macmini/install.sh`.

Free-bet accas and Double-Delight bets still need **manual settlement** (the
auto-settler uses the wrong convention for SNR free bets / boosts) — keep doing
those by hand until that's special-cased.

## 4. Cloud vs Mac Mini
Your GitHub Actions (`daily-card`, `daily-promos`, `hourly-odds`) are stateless
and fine to keep as a **redundant** odds/card feed. But anything that reads or
writes the **ledger** (close-capture, settlement, PM, the bot) **must** run here,
because the db is local. Don't duplicate ledger-touching jobs in the cloud — they
wouldn't see this host's `wca.db`.

## 5. Security
- `.env` (ODDS_API_KEY, TELEGRAM_*, POLYMARKET_FUNDER) is gitignored — keep it here only.
- Put the **wallet private key in the macOS Keychain**, not `.env`; have the PM
  trader read it from Keychain. Never commit it.
- `PM_DRY_RUN=1` until you've watched a full propose→Telegram→`Y`→execute cycle
  succeed once; then set `PM_DRY_RUN=0` to go live.

## 6. Bootstrap (run once on the Mac Mini)
```bash
git clone https://github.com/drewdoherty/World-Cup-26.git   # clone anywhere — paths auto-detect
cd World-Cup-26
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e . --no-deps
cp .env.example .env   # then fill in keys
bash deploy/check_connectivity.sh           # confirm Telegram + PM reachable
bash deploy/install_services.sh             # generate + load all launchd jobs (incl. com.wca.sync)
launchctl list | grep com.wca               # verify they're running
tail -f data/com.wca.sync.run.log           # watch auto-deploys (com.wca.bot.err.log for the bot)
```
