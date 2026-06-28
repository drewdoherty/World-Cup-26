#!/bin/bash
# Keep the localhost sites (8000 + 8001) live by pulling the FRESH JSON data
# feeds from the canonical mini every run. Installed as a launchd agent
# (com.wca.feedpull) that fires on an interval, so 8001/8000 stay current
# whenever this Mac is on — without going through git (no working-tree churn,
# no conflicts) and WITHOUT touching local html/css/js (which carry the local
# lilac theming the mini's main branch does not have).
#
# Data feeds only: site-analytics/data/*.json + the handful of site/*.json the
# pages read. Safe to run repeatedly; idempotent; never writes the mini.
set -uo pipefail

REPO="/Users/andrewdoherty/Desktop/Coding/World Cup Alpha"
MINI="andrewdoherty@drews-mac-mini.local"
SRC="World-Cup-26"
LOG="$REPO/logs/feedpull.log"
mkdir -p "$REPO/logs" "$REPO/site-analytics/data"
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"

ts() { date -u +%FT%TZ; }

# 1) all analytics feeds (8001)
rsync -az --timeout=25 -e "$SSH" \
  "$MINI:$SRC/site-analytics/data/"*.json \
  "$REPO/site-analytics/data/" >>"$LOG" 2>&1
rc1=$?

# 2) the site (8000) data feeds the pages read — JSON only, never html/css/js
rsync -az --timeout=25 -e "$SSH" \
  "$MINI:$SRC/site/data.json" \
  "$MINI:$SRC/site/scores_data.json" \
  "$MINI:$SRC/site/scores_markets.json" \
  "$MINI:$SRC/site/exposure_data.json" \
  "$MINI:$SRC/site/advancement_data.json" \
  "$MINI:$SRC/site/advancement_history.json" \
  "$MINI:$SRC/site/promos_data.json" \
  "$MINI:$SRC/site/arb_data.json" \
  "$MINI:$SRC/site/linemove.json" \
  "$REPO/site/" >>"$LOG" 2>&1
rc2=$?

if [ $rc1 -eq 0 ] && [ $rc2 -eq 0 ]; then
  echo "$(ts) feedpull OK" >>"$LOG"
else
  echo "$(ts) feedpull PARTIAL rc1=$rc1 rc2=$rc2 (mini unreachable/asleep?)" >>"$LOG"
fi
# Keep the log from growing unbounded.
tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null || true
