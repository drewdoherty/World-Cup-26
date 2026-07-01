# One-click PLACE (localhost bet-recs → live Polymarket order)

A "Place" button on the **localhost** Action Desk (`site/arb.html`) that fires a
single Polymarket advancement rec through the bot's existing signed-order +
ledger path. **DRY-RUN by default**; it goes live only when the human explicitly
sets `PM_DRY_RUN=0`.

## Pieces

| Component | Runs on | Role |
|---|---|---|
| `scripts/wca_pm_fire.py` | **mini** | Loads a rec by id, RE-RESOLVES the live PM market (current YES price, never the stale `pm_price`), hard-caps the stake, idempotency-guards (`pm_fire_log`), and calls `wca.bot.app._execute_parked_order` (honours `PM_DRY_RUN`, records `data/wca.db`). Prints one JSON line. |
| `scripts/wca_place_server.py` | **dev box** | Localhost-only (`127.0.0.1`) stdlib HTTP bridge. `POST /place {rec_id,nonce}` with `X-WCA-Place-Token` → SSHes to the mini and runs the fire script, **forwarding its own `PM_DRY_RUN` (default `1`)**. `GET /health`. |
| `site/arb.js` + `site/arb.html` | browser | Adds a "Place" column that renders **only** when `location.hostname` is `localhost`/`127.0.0.1` (never on Vercel). Click POSTs to `http://127.0.0.1:8010/place` with the shared secret from `localStorage` (`wcaPlaceToken`, prompted once). |

## Safety guarantees

- `PM_DRY_RUN` defaults **ON** (`wca.bot.app._pm_dry_run`); nothing in this
  feature sets it to `0`.
- Hard stake cap: `min(rec.stake, --max-usd)` clamped to `ABSOLUTE_MAX_USD=100`
  (USD). `--max-usd` can only lower it.
- Idempotency: `pm_fire_log` (unique `rec_id`+`nonce`; plus a 30-min per-rec
  window) blocks double-clicks / retries.
- Re-resolves at fire time and refuses if the live YES price moved
  > `PRICE_SANITY_BAND` (0.08) from the rec, if there is no live market, if the
  rec is not an actionable `ADD`, or if it is stale.
- The button is inert off-localhost; the `<th>` stays `hidden` on Vercel.
- The server refuses non-loopback clients and any request whose
  `X-WCA-Place-Token` ≠ env `WCA_PLACE_TOKEN`; a mismatched/unset secret 403s.
- If the mini is unreachable the server returns a clean JSON error — never a
  half-state.
- Notion: no single-bet append helper exists (only the bulk
  `wca.ledger.notion_diff` reconcile), so the fire script **skips** Notion and
  says so — it never invents credentials, and a Notion issue never breaks a fire.

## Run (dry-run, the default)

```bash
# dev box
export WCA_PLACE_TOKEN='<shared-secret>'         # PM_DRY_RUN unset => dry-run
python scripts/wca_place_server.py               # binds 127.0.0.1:8010
# open http://localhost:8001/arb.html (or wherever site-analytics serves it),
# paste the same secret when prompted, click "Place".
```

Direct (on the mini):

```bash
PM_DRY_RUN=1 .venv/bin/python scripts/wca_pm_fire.py \
    --rec-id belgium_qf_pm --max-usd 100 --db data/wca.db \
    --bet-recs site/bet_recs.json --nonce test-1
```

## Going LIVE (human only)

1. On the **mini**: `.env` has `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_FUNDER`
   (+ `POLYMARKET_SIG_TYPE=2` for the Gnosis-safe/deposit wallet). The trader
   still refuses a live order if the account class is unproven.
2. On the **dev box**, start the server with the flag explicitly set:
   ```bash
   PM_DRY_RUN=0 WCA_PLACE_TOKEN='<shared-secret>' python scripts/wca_place_server.py
   ```
   The server then forwards `PM_DRY_RUN=0` to the mini. There is no other way to
   go live — the default everywhere is dry-run.
