#!/bin/bash
# Regenerate the localhost:8001 analytics feeds from the live ledger, and replay
# the prediction ledger so the Model-vs-Venue benchmark has its input.
#
# WHY: these feeds (winrate / rigor / risk_pnl / clvbench / tracking / exposure)
# and the venues benchmark had NO scheduled regen job — they went stale (06-25)
# while everything else refreshed. This is the 'analytics' interval job
# (deploy/macmini/services.env); the MacBook feedpull agent then syncs the fresh
# feeds to localhost:8001.
#
# Safety: READ-ONLY w.r.t. the production ledger data/wca.db. It writes only
# data/dev.db (the predledger paper book — wca.db writes are hard-blocked in
# predledger/store.py) and the site-analytics/data + site feed JSON.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
cd "$REPO"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
log() { echo "$(date -u +%FT%TZ) analytics: $*"; }

# 1) Replay model predictions into the paper ledger (data/dev.db) so the venues
#    benchmark's --pred-db has rows. backfill replays model_predictions_log.jsonl.
"$PY" scripts/wca_predledger.py --db data/dev.db ensure   >/dev/null 2>&1 || log "predledger ensure FAILED"
"$PY" scripts/wca_predledger.py --db data/dev.db backfill >/dev/null 2>&1 && log "predledger backfill ok" || log "predledger backfill FAILED"
"$PY" scripts/wca_predledger.py --db data/dev.db publish   >/dev/null 2>&1 || log "predledger publish FAILED"

# 2) Regenerate the analytics feeds from the live ledger (each defaults to
#    data/wca.db read-only -> site-analytics/data/*.json).
rc=0
for s in wca_winrate_data wca_rigor_data wca_risk_pnl_data wca_clvbench_data wca_tracking_data wca_exposure_data; do
  if "$PY" "scripts/$s.py" >/dev/null 2>&1; then log "$s ok"; else log "$s FAILED"; rc=1; fi
done
log "done (rc=$rc)"
exit $rc
