#!/usr/bin/env bash
# Install the WCA always-on services on macOS via launchd (the Mac's cron+daemon
# manager). Run ONCE on the Mac Mini after cloning the repo and creating .env.
#
#   bash deploy/install_services.sh
#
# Override paths if your layout differs:
#   WCA_REPO=/path/to/repo  WCA_PY=/path/to/.venv/bin/python  bash deploy/install_services.sh
set -euo pipefail

WCA_REPO="${WCA_REPO:-$HOME/World-Cup-26}"
WCA_PY="${WCA_PY:-$WCA_REPO/.venv/bin/python}"
LA="$HOME/Library/LaunchAgents"
LOG="$WCA_REPO/data"
mkdir -p "$LA" "$LOG"

# Each entry: label | schedule | command
#   schedule = "keepalive"  -> long-running daemon, restarted if it dies
#   schedule = <seconds>    -> StartInterval, runs every N seconds
SERVICES=(
  "com.wca.bot|keepalive|scripts/wca_bot.py"                       # Telegram bot (NEEDS Telegram VPN)
  "com.wca.snapshotd|keepalive|scripts/wca_snapshotd.py"          # dense odds snapshots near kickoffs (closing lines)
  "com.wca.closecapture|600|scripts/wca_close_capture.py"         # stamp closing_odds+CLV every 10 min (fixes stale CLV)
  "com.wca.scores|3600|scripts/wca_scores_data.py"               # refresh results/scores hourly (drives settlement)
  "com.wca.pmpropose|1800|scripts/wca_pm_propose.py"             # park PM proposals + notify (needs PM + Telegram reachable)
)

for entry in "${SERVICES[@]}"; do
  IFS='|' read -r label sched cmd <<<"$entry"
  plist="$LA/$label.plist"
  if [ "$sched" = "keepalive" ]; then
    SCHED_XML="<key>KeepAlive</key><true/><key>RunAtLoad</key><true/>"
  else
    SCHED_XML="<key>StartInterval</key><integer>$sched</integer><key>RunAtLoad</key><true/>"
  fi
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>WorkingDirectory</key><string>$WCA_REPO</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONPATH</key><string>$WCA_REPO/src</string>
  </dict>
  <key>ProgramArguments</key><array>
    <string>$WCA_PY</string>
    $(for a in $cmd; do echo "<string>$a</string>"; done)
  </array>
  $SCHED_XML
  <key>StandardOutPath</key><string>$LOG/$label.out.log</string>
  <key>StandardErrorPath</key><string>$LOG/$label.err.log</string>
</dict></plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "loaded $label ($sched)"
done

echo
echo "Done. Inspect:  launchctl list | grep com.wca"
echo "Logs:         tail -f $LOG/com.wca.*.log"
echo "Stop one:     launchctl unload $LA/com.wca.<name>.plist"
