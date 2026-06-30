#!/bin/bash
# Keep localhost:8001 analytics feeds LIVE: regenerate on the canonical mini from
# the live ledger, then sync to this Mac. Wired as launchd com.wca.analytics-live.
MINI="andrewdoherty@drews-mac-mini.local"
DEST="/Users/andrewdoherty/Desktop/Coding/World Cup Alpha/site-analytics/data"
ssh -o ConnectTimeout=8 -o BatchMode=yes "$MINI" "bash ~/wca_rebuild_analytics.sh" 2>/dev/null
scp -o ConnectTimeout=8 -o BatchMode=yes "$MINI:~/World-Cup-26/site-analytics/data/*.json" "$DEST/" 2>/dev/null
echo "$(date -u +%FT%TZ) analytics-live synced -> $DEST"
# 8002 — rebuild the Lilac Ledger terminal from the freshly-synced feeds
cd "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" 2>/dev/null && \
  .venv/bin/python scripts/wca_lilac_ledger.py --template site-lilac/_template.html --out site-lilac >/dev/null 2>&1
