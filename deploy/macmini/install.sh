#!/bin/bash
# Install the World Cup Alpha production services as launchd agents on the Mac mini.
# Idempotent: re-running re-generates and reloads every job.
#
#   bash deploy/macmini/install.sh
#
# Daemons get KeepAlive (auto-restart + start at login). Interval jobs run on a timer.
# Nothing here touches application Python — it only schedules the existing scripts.
#
# ACTIVATION IS A HUMAN STEP. Merging a new job into services.env does NOT start
# it — launchd only learns about a job when this script runs on the mini and
# calls `launchctl load`. After any change here, SSH to the mini and re-run:
#     bash deploy/macmini/install.sh
# `autopull` pulls code but does NOT register new jobs; it only kickstarts
# daemons that already exist. A brand-new daemon stays dormant until install.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"
RUN1="$HERE/run_singleton.sh"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$REPO_ROOT/logs"

# shellcheck source=/dev/null
source "$HERE/services.env"

[ -x "$VENV_PY" ] || { echo "ERROR: venv not found at $VENV_PY — create it first."; exit 1; }
mkdir -p "$AGENTS" "$LOGS" "$REPO_ROOT/data/backups"
chmod +x "$RUN1" "$HERE"/*.sh 2>/dev/null || true

# Program arguments for each service (one token per line).
cmd_for() {
  case "$1" in
    bot)        printf '%s\n' "$RUN1" bot       "$VENV_PY" scripts/wca_bot.py       --db data/wca.db --env .env ;;
    conductor)  printf '%s\n' "$RUN1" conductor "$VENV_PY" scripts/wca_conductor.py --env .env.conductor ;;
    snapshotd)  printf '%s\n' "$RUN1" snapshotd "$VENV_PY" scripts/wca_snapshotd.py --db data/wca.db --env .env ;;
    newsd)      printf '%s\n' "$RUN1" newsd     "$VENV_PY" scripts/wca_newsd.py     --db data/wca.db --env .env --interval 600 --max-per-cycle 2 ;;
    promosd)    printf '%s\n' "$RUN1" promosd   "$VENV_PY" scripts/wca_promosd.py   --db data/wca.db --env .env --interval 21600 --max-per-cycle 2 ;;
    buildcard)  printf '%s\n' "$RUN1" buildcard "$VENV_PY" scripts/wca_build_card.py --db data/wca.db --env .env --hours-ahead 96 --skip-scorers ;;
    goalscorers) printf '%s\n' "$RUN1" goalscorers "$VENV_PY" scripts/wca_build_card.py --db data/wca.db --env .env --hours-ahead 96 --goalscorers-only ;;
    autopull)   printf '%s\n' "/bin/bash" "$HERE/autopull.sh" ;;
    backup)     printf '%s\n' "/bin/bash" "$HERE/backup.sh" ;;
    pmpropose)    printf '%s\n' "$RUN1" pmpropose    "$VENV_PY" scripts/wca_pm_propose.py    --db data/wca.db --env .env ;;
    pmredeem)     printf '%s\n' "$RUN1" pmredeem     "$VENV_PY" scripts/wca_pm_redeem.py     --db data/wca.db --env .env --notify ;;
    closecapture) printf '%s\n' "$RUN1" closecapture "$VENV_PY" scripts/wca_close_capture.py --db data/wca.db ;;
    publish)      printf '%s\n' "/bin/bash" "$REPO_ROOT/deploy/publish_site.sh" ;;
    watchdog)     printf '%s\n' "/bin/bash" "$HERE/watchdog.sh" ;;
    archive)      printf '%s\n' "$RUN1" archive      "$VENV_PY" scripts/wca_archive.py snapshot --db data/wca.db --env .env ;;
    pmdrift)      printf '%s\n' "$RUN1" pmdrift      "$VENV_PY" scripts/wca_pm_reconcile.py --check --db data/wca.db --env .env --notify ;;
    positions)    printf '%s\n' "$RUN1" positions    "$VENV_PY" scripts/wca_positions_sync.py --db data/wca.db --env .env --once ;;
    analytics)    printf '%s\n' "/bin/bash" "$HERE/../../scripts/wca_build_analytics.sh" ;;
    venues)       printf '%s\n' "$RUN1" venues       "$VENV_PY" scripts/wca_venues_benchmark.py --pred-db data/dev.db --odds-db data/wca.db ;;
    playersdb)    printf '%s\n' "$RUN1" playersdb    "$VENV_PY" scripts/wca_players_refresh.py ;;
    pm1x2snapshot) printf '%s\n' "$RUN1" pm1x2snapshot "$VENV_PY" scripts/wca_pm_1x2_snapshot.py --db data/wca.db --notify ;;
    orderflow)    printf '%s\n' "$RUN1" orderflow    "/bin/bash" "$HERE/../../scripts/wca_orderflow_refresh.sh" ;;
    *) echo "unknown service $1" >&2; return 1 ;;
  esac
}

emit_plist() { # label  keepalive(true|false)  interval(0=none)  <args-on-stdin>
  local label="$1" keepalive="$2" interval="$3"
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
  echo '<plist version="1.0"><dict>'
  echo "  <key>Label</key><string>${label}</string>"
  echo '  <key>ProgramArguments</key><array>'
  while IFS= read -r a; do [ -n "$a" ] && echo "    <string>${a}</string>"; done
  echo '  </array>'
  echo "  <key>WorkingDirectory</key><string>${REPO_ROOT}</string>"
  echo "  <key>StandardOutPath</key><string>${LOGS}/${label##*.}.log</string>"
  echo "  <key>StandardErrorPath</key><string>${LOGS}/${label##*.}.log</string>"
  echo '  <key>RunAtLoad</key><true/>'
  [ "$keepalive" = "true" ] && echo '  <key>KeepAlive</key><true/>' && echo '  <key>ThrottleInterval</key><integer>30</integer>'
  [ "$interval" -gt 0 ] && echo "  <key>StartInterval</key><integer>${interval}</integer>"
  echo '</dict></plist>'
}

install_one() { # name keepalive interval
  local name="$1" keepalive="$2" interval="$3"
  local label="${WCA_LABEL_PREFIX}.${name}"
  local plist="$AGENTS/${label}.plist"
  cmd_for "$name" | emit_plist "$label" "$keepalive" "$interval" > "$plist"
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load -w "$plist"
  echo "  loaded $label"
}

echo "Installing WCA services from $REPO_ROOT"
for d in "${WCA_DAEMONS[@]}"; do install_one "$d" true 0; done
for j in "${WCA_INTERVAL_JOBS[@]}"; do
  # Defensive: if this checkout somehow lacks orderflow's job script (stale
  # checkout, partial cherry-pick of the deploy wiring), skip loudly instead
  # of installing an hourly job that exits 127 forever.
  if [ "$j" = "orderflow" ] && [ ! -f "$REPO_ROOT/scripts/wca_orderflow_refresh.sh" ]; then
    echo "  SKIPPED ${WCA_LABEL_PREFIX}.orderflow: scripts/wca_orderflow_refresh.sh not in this checkout (update the checkout, then re-run install.sh)"
    continue
  fi
  var="WCA_INTERVAL_${j}"; install_one "$j" false "${!var}"
done
echo "Done. Verify with:  launchctl list | grep ${WCA_LABEL_PREFIX}"
