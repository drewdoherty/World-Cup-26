# WCA deployment runbook — Mac Mini (always-on automation)

The Mac Mini is the **single source of truth** for the bet ledger (`data/wca.db`
is gitignored — it lives only on this host) and the home for everything that
must run continuously or touch the wallet/Telegram. Your MacBook Pro stays on
the **UK VPN for placing sportsbook bets manually** — that work never moves here.

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

**Two ways to get Odds + Polymarket + Telegram all green at once:**
- **(a) Split-tunnel (best):** keep the host native (Bahrain), route **only
  `api.telegram.org`** through a VPN. PM/odds stay native; Telegram tunnels.
- **(b) Full VPN to a neutral region** (e.g. an EU country that is neither UK
  nor US and where Polymarket is reachable). Simpler, but verify PM isn't blocked
  there.

Run `bash deploy/check_connectivity.sh` to see what's reachable on the current
network — the goal is Odds + Polymarket + Telegram all `OK`.

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
| `com.wca.scores` | hourly | refresh results → drives settlement | |
| `com.wca.pmpropose` | every 30 min | park PM proposals + notify | needs PM **and** Telegram reachable |

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
git clone git@github.com:drewdoherty/World-Cup-26.git ~/World-Cup-26
cd ~/World-Cup-26
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # or pandas scipy scikit-learn requests
cp .env.example .env   # then fill in keys
bash deploy/check_connectivity.sh           # confirm Telegram VPN + PM both green
bash deploy/install_services.sh             # load the launchd jobs
launchctl list | grep com.wca               # verify they're running
tail -f data/com.wca.bot.err.log            # watch the bot connect to Telegram
```
