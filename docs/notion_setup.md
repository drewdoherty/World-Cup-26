# Notion Bet Ledger — Setup Guide

## What this does

Creates a "WCA Bet Ledger" Notion database where each bet is a page.
Status property (`open` / `won` / `lost` / `void` / `cashed`) distinguishes
active from settled bets — filter by status or create views in Notion.

New bets appear within seconds of being logged via `@gamble1_bot`. Settlement
updates the page immediately. The 10-min sync pulls any manual edits
(`notes`, `closing_odds`) back to `data/wca.db`.

---

## One-time Notion setup (3 min)

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations).
2. **New integration** → name `wca-ledger` → Submit.
3. Copy the **Internal Integration Token** (`secret_xxx...`).
4. In Notion, open the page where you want the database to live.
   - **Share → Invite → search for `wca-ledger`** → Invite as Editor.
   - Copy the page ID from the URL (last segment after the final `/`):
     `https://notion.so/My-Page-<PAGE_ID_HERE>`

---

## Create the database

On the mini, in the repo:

```bash
cd ~/World-Cup-26
NOTION_TOKEN=secret_xxx \
  .venv/bin/python scripts/wca_notion_setup.py --parent-page-id <page_id>
```

The script loads all existing bets and prints:

```
NOTION_BET_DB_ID=<id>
```

---

## Add env vars to the mini

Append to `.env` (and `.env.conductor`):

```
NOTION_TOKEN=secret_xxx
NOTION_BET_DB_ID=<id from setup script>
```

Then restart the conductor:

```bash
launchctl kickstart -k gui/$(id -u)/com.wca.conductor
```

---

## Install the dependency

```bash
pip install -e '.[notion]'
```

---

## Wire into the periodic rebuild

Append to `~/wca_rebuild_analytics.sh` on the mini:

```bash
$PY scripts/wca_notion_sync.py --db data/wca.db >/dev/null 2>&1
```

---

## Sync cadence

- **Immediate**: new bets appear in Notion within seconds.
- **Immediate**: settlement updates the page via `@gamble1_bot /settle`.
- **Periodic**: `wca_rebuild_analytics.sh` reconciles every ~10 min —
  pulls manual edits back to `wca.db`.

---

## Editable properties

Only two properties should be edited manually in Notion — all others are
overwritten by the sync:

| Property | Notes |
|----------|-------|
| `notes` | Free-text; pulled back to `wca.db` |
| `closing_odds` | Enter the closing line; triggers CLV recompute |

---

## Repopulate after schema changes

```bash
NOTION_TOKEN=secret_xxx NOTION_BET_DB_ID=<id> \
  .venv/bin/python scripts/wca_notion_setup.py --repopulate
```
