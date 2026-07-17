#!/bin/bash
# Multi-venue research shadow-book cycle. PAPER ONLY: no order endpoint is used.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${WCA_REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$REPO" || { echo "no repo at $REPO"; exit 1; }
mkdir -p logs data

LOG="logs/shadow_book.log"
LOCK="data/.shadow_book_cycle.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cycle skipped: previous run still active" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

case "${WCA_SHADOWBOOK_OFF:-}" in
  1|true|yes|on|TRUE|YES|ON)
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) PAUSED via WCA_SHADOWBOOK_OFF" >> "$LOG"
    exit 0
    ;;
esac
if [ -f data/SHADOW_BOOK_PAUSED ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) PAUSED via data/SHADOW_BOOK_PAUSED" >> "$LOG"
  exit 0
fi

PY="python3"
[ -x "$REPO/.venv/bin/python" ] && PY="$REPO/.venv/bin/python"

# Hard isolation: these cycles may read public prices but never hold credentials
# and never leave dry-run mode, even if the production shell is live-enabled.
export PM_DRY_RUN=1
unset POLYMARKET_PRIVATE_KEY
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}src"

{
  echo "================ shadow-book cycle $(date -u +%Y-%m-%dT%H:%M:%SZ) ================"
  # A failed venue read is evidence, not permission to synthesize a price. The
  # engine consumes the prior feed and records stale/no-data abstentions.
  "$PY" scripts/wca_hl_xvenue.py \
    --out site/hl_xvenue.json \
    --history data/hl_xvenue_history.jsonl \
    || echo "WARN: cross-venue refresh failed; fail-closed staleness gate remains active"
  "$PY" scripts/wca_shadow_book.py \
    --db data/shadow_book.db \
    --out site/shadow_book.json \
    cycle \
    --forest site/forest_data.json \
    --hyperliquid site/hl_xvenue.json \
    --bankroll "${WCA_SHADOWBOOK_BANKROLL:-3227}"
} >> "$LOG" 2>&1
