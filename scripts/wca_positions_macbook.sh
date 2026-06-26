#!/usr/bin/env bash
# Cross-machine venue-position sync orchestrator — runs ON THE MacBook.
#
# Betfair's API is reachable from the MacBook (the VPN lives here) but NOT from
# the Mac mini. The canonical ledger (data/wca.db) and the site publish live on
# the mini. So this wrapper:
#
#   1. FETCH locally (MacBook, VPN ON): pull every venue's open + settled-24h
#      positions into a self-describing JSON snapshot. NO DB access here.
#   2. SCP the snapshot to the mini.
#   3. APPLY on the mini against the canonical data/wca.db over SSH (SHADOW by
#      default; LIVE only when WCA_POSITIONS_LIVE=1 is exported here).
#   4. Trigger the mini's site publish so the site reflects the reconciliation.
#
# SHADOW-FIRST: with WCA_POSITIONS_LIVE unset (the default) NOTHING is written to
# the ledger — the mini just logs the proposed inserts/settles/closes.
#
# Read-only on the venues — never places or cancels orders.
set -uo pipefail

# --- Config (override via env) ---------------------------------------------
MINI_HOST="${WCA_MINI_HOST:-andrewdoherty@Drews-Mac-mini.local}"
MINI_REPO="${WCA_MINI_REPO:-/Users/andrewdoherty/World-Cup-26}"
MINI_PY="${WCA_MINI_PY:-$MINI_REPO/.venv/bin/python}"
MINI_DB="${WCA_MINI_DB:-data/wca.db}"
SETTLED_LOOKBACK_HOURS="${WCA_SETTLED_LOOKBACK_HOURS:-24}"
LIVE="${WCA_POSITIONS_LIVE:-}"        # unset/0 = SHADOW (default), 1 = LIVE
PUBLISH="${WCA_POSITIONS_PUBLISH:-1}" # 1 = trigger mini publish after apply

# Local repo = this script's parent's parent.
LOCAL_REPO="${WCA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOCAL_PY="${WCA_PY:-$LOCAL_REPO/.venv/bin/python}"
SNAP_LOCAL="$(mktemp -t wca_positions_snapshot.XXXXXX).json"
SNAP_REMOTE="/tmp/wca_positions_snapshot.json"

log() { echo "[wca-positions $(date -u +%H:%M:%SZ)] $*"; }
cleanup() { rm -f "$SNAP_LOCAL" 2>/dev/null || true; }
trap cleanup EXIT

# --- 1. FETCH locally (MacBook, VPN on) ------------------------------------
log "FETCH (open + settled-${SETTLED_LOOKBACK_HOURS}h) on MacBook -> $SNAP_LOCAL"
export PYTHONPATH="$LOCAL_REPO/src"
if ! "$LOCAL_PY" "$LOCAL_REPO/scripts/wca_positions_sync.py" \
        --fetch-only --out "$SNAP_LOCAL" \
        --settled-lookback-hours "$SETTLED_LOOKBACK_HOURS"; then
  log "FETCH failed; aborting (no snapshot to apply)."
  exit 1
fi
log "snapshot: $(wc -c < "$SNAP_LOCAL" | tr -d ' ') bytes"

# --- 2. SCP snapshot to the mini -------------------------------------------
log "SCP snapshot -> $MINI_HOST:$SNAP_REMOTE"
if ! scp -q "$SNAP_LOCAL" "$MINI_HOST:$SNAP_REMOTE"; then
  log "SCP to mini failed; aborting."
  exit 1
fi

# --- 3. APPLY on the mini against the canonical ledger ----------------------
MODE="SHADOW"; LIVE_EXPORT=""
if [ "$LIVE" = "1" ]; then MODE="LIVE"; LIVE_EXPORT="export WCA_POSITIONS_LIVE=1;"; fi
log "APPLY on mini ($MODE) against $MINI_DB"
ssh "$MINI_HOST" "cd '$MINI_REPO' && $LIVE_EXPORT export PYTHONPATH='$MINI_REPO/src' && \
  '$MINI_PY' scripts/wca_positions_sync.py \
    --apply-from-snapshot '$SNAP_REMOTE' --db '$MINI_DB' \
    --json '$MINI_REPO/data/positions_apply_report.json'" || {
  log "APPLY on mini failed."
  exit 1
}

# --- 4. Trigger the mini publish so the site reflects the apply -------------
if [ "$PUBLISH" = "1" ] && [ "$MODE" = "LIVE" ]; then
  log "Triggering mini site publish"
  ssh "$MINI_HOST" "cd '$MINI_REPO' && WCA_REPO='$MINI_REPO' WCA_PY='$MINI_PY' \
    /bin/bash deploy/publish_site.sh" >/dev/null 2>&1 || log "publish trigger failed (non-fatal)"
else
  log "Skipping publish (SHADOW or WCA_POSITIONS_PUBLISH=0)"
fi

log "done ($MODE)."
