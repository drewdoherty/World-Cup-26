# Google Sheets Bet Ledger — Setup Guide

## What this does

Creates a "WCA Bet Ledger" Google Sheet with two tabs:

- **Open Bets** — all active bets; updated live when `@gamble1_bot` logs a bet
- **Closed Bets** — settled bets; rows move here automatically at settlement

The sheet is editable from any device. Changes to `notes` and `closing_odds`
columns are pulled back to `data/wca.db` on the next sync (~10 min cadence).

---

## One-time Google Cloud setup (5 min)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project (e.g. "wca-sheets").
2. Enable two APIs: **Google Sheets API** and **Google Drive API**.
3. Go to **IAM & Admin → Service Accounts → Create Service Account**.
   - Name: `wca-ledger`
   - Role: none needed (access is granted via sheet share)
4. Click the service account → **Keys → Add Key → JSON**. Save the downloaded file to `data/sheets_creds.json` on the mini.
5. Note the service account email (looks like `wca-ledger@…iam.gserviceaccount.com`) — you'll need it in the next step.

---

## Create the sheet

On the mini, in the repo:

```bash
cd ~/World-Cup-26
SHEETS_CREDS_PATH=data/sheets_creds.json .venv/bin/python scripts/wca_sheets_setup.py
```

The script will:
- Create the spreadsheet and both tabs
- Load all 77 existing bets
- Print the sheet URL and the env-var line to add to `.env`

---

## Add env vars to the mini

Append to `~/.env` (or `.env.conductor`):

```
SHEETS_CREDS_PATH=data/sheets_creds.json
SHEETS_BET_LEDGER_ID=<id printed by the setup script>
```

Then restart the conductor:

```bash
launchctl kickstart -k gui/$(id -u)/com.wca.conductor
```

---

## Share the sheet with yourself

The setup script creates the sheet as "anyone with link can edit". To restrict
access, open the sheet → Share → change to "Restricted" and add your personal
Google account as an Editor.

---

## Sync cadence

- **Immediate**: new bets appear in Open Bets within seconds of being logged via `@gamble1_bot`.
- **Periodic**: `wca_rebuild_analytics.sh` runs every 10 min on the mini and calls `wca_sheets_sync.py` — this reconciles any manual edits and moves settled bets to Closed Bets.
- **On settlement**: `@gamble1_bot /settle` moves the row to Closed Bets immediately.

---

## Editable columns

Only two columns should be edited manually in the sheet — everything else is overwritten by the sync:

| Tab | Editable columns |
|-----|-----------------|
| Open Bets | `notes`, `closing_odds` |
| Closed Bets | `notes`, `closing_odds`, `clv_pct` |

Editing `closing_odds` in Closed Bets triggers a CLV recompute written back to `wca.db`.

---

## Reinstall / repopulate

If you change the schema or need to wipe and reload:

```bash
SHEETS_CREDS_PATH=data/sheets_creds.json SHEETS_BET_LEDGER_ID=<id> \
  .venv/bin/python scripts/wca_sheets_setup.py --repopulate
```
