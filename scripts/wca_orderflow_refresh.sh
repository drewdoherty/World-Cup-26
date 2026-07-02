#!/bin/bash
# Recurring (hourly) Polymarket orderflow refresh: incremental fill capture +
# analytics feed regen.
#
# WHY: the data-api /trades history is hard-capped per market filter (offset
# ceiling 3000, ~3,500 rows) and busy match markets blow through that within
# hours on match day — without a recurring sweep the older fills scroll out of
# the window and the history is gone for good. Trades only occur while a
# market is OPEN (a closed market's tape is frozen), so the ingest runs
# --open-only: full discovery every time (refreshes closed flags, catches
# newly listed events such as new knockout 1x2 markets), then sweeps only the
# ~open markets plus exactly one guaranteed final sweep for any market that
# closed since its last sweep. Cheap enough to run hourly, never skips a
# market that could still gain fills.
#
# Writes: data/pm_orderflow.db (append-only ingest, own sqlite),
# site/microstructure/orderflow.json (regenerated analytics feed) and
# data/orderflow_alert_state.json (freshness-alert debounce state).
# Never touches data/wca.db. Nothing is committed here — the mini's publish
# job commits the refreshed JSON.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
cd "$REPO"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
log() { echo "$(date -u +%FT%TZ) orderflow: $*"; }

rc=0

# 1) Discover + incremental ingest (open markets + final sweeps of newly
#    closed ones) into data/pm_orderflow.db.
if PYTHONPATH=src "$PY" scripts/pm_orderflow_ingest.py --open-only; then
  log "ingest ok"
else
  log "ingest FAILED"; rc=1
fi

# 2) Regenerate the analytics feed the microstructure page renders (reads the
#    orderflow db strictly read-only). Still worth running after a failed
#    ingest: it republishes from whatever was captured (the feed carries
#    last_successful_ingest_utc so consumers can see the capture age; only
#    generated_utc advances on such runs). stdout is chatty -> suppressed;
#    stderr is kept so a failure leaves its traceback in the job log.
if PYTHONPATH=src "$PY" scripts/microstructure/orderflow.py >/dev/null; then
  log "orderflow.json ok"
else
  log "orderflow.json FAILED"; rc=1
fi

# 3) Freshness gate (pm1x2snapshot-style --notify): launchd ignores interval-
#    job exit codes and watchdog.sh only covers daemons, so a permanently
#    failing ingest (API change, sustained TLS block) would otherwise stall
#    silently while the data-api offset window scrolls match-day fills away
#    for good. Debounced Telegram DM to the admin when the last SUCCESSFUL
#    market sweep is older than --stale-hours (or never happened). Creds come
#    from .env the same way watchdog.sh reads them (launchd env has none).
get_env() { grep -E "^$1=" "$REPO/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'"; }
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(get_env TELEGRAM_BOT_TOKEN)}"
TELEGRAM_ADMIN_USER_ID="${TELEGRAM_ADMIN_USER_ID:-$(get_env TELEGRAM_ADMIN_USER_ID)}"
export TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_USER_ID
if PYTHONPATH=src "$PY" scripts/pm_orderflow_ingest.py --check-freshness --stale-hours 3 --notify; then
  log "freshness gate ok"
else
  log "freshness gate FAILED"; rc=1
fi

log "done (rc=$rc)"
exit $rc
