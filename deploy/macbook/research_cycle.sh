#!/bin/bash
# Refresh the complete PM event forest and paper-only cross-venue shadow book.
# This host owns the job because public Polymarket is not reachable from the
# mini.  It has no execution path and explicitly strips all trading secrets.
set -uo pipefail

REPO="${WCA_REPO:-/Users/andrewdoherty/Desktop/Coding/World Cup Alpha}"
PY="${WCA_PY:-$REPO/.venv/bin/python}"
cd "$REPO" || exit 1
mkdir -p logs data site-analytics/data

LOCK="data/.research_cycle.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date -u +%FT%TZ) research cycle skipped: prior run active"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

export PM_DRY_RUN=1
unset POLYMARKET_PRIVATE_KEY POLYMARKET_API_KEY POLYMARKET_SECRET
export PYTHONPATH="$REPO/src"

"$PY" scripts/wca_event_markets.py \
  --preds data/model_predictions.json \
  --db data/wca.db \
  --env /dev/null \
  --days-ahead 5
"$PY" scripts/wca_punt_sizes.py \
  --in site/forest_data.json --out site/forest_data.json --bankroll 3227
bash scripts/wca_shadow_book_cycle.sh

for feed in forest_data event_market_recs shadow_book hl_xvenue; do
  [ -f "site/$feed.json" ] && cp "site/$feed.json" "site-analytics/data/$feed.json"
done
