#!/usr/bin/env bash
# Install the WCA always-on services on macOS via launchd (the Mac's cron+daemon
# manager). Run ONCE on the Mac Mini after cloning the repo and creating .env.
# Re-run any time to refresh the plists (it unloads + reloads each service).
#
#   bash deploy/install_services.sh
#
# The repo path is auto-detected from this script's location, so it works
# wherever the repo lives. Override either path if your layout differs:
#   WCA_REPO=/path/to/repo  WCA_PY=/path/to/.venv/bin/python  bash deploy/install_services.sh
set -euo pipefail

WCA_REPO="${WCA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WCA_PY="${WCA_PY:-$WCA_REPO/.venv/bin/python}"
LA="$HOME/Library/LaunchAgents"
LOG="$WCA_REPO/data"
mkdir -p "$LA" "$LOG"

# Each entry: label | schedule | interpreter | args
#   schedule = "keepalive"  -> long-running daemon, restarted if it dies
#   schedule = <seconds>    -> StartInterval, runs every N seconds
SERVICES=(
  "com.wca.bot|keepalive|$WCA_PY|scripts/wca_bot.py"                  # Telegram bot (NEEDS Telegram VPN)
  "com.wca.snapshotd|keepalive|$WCA_PY|scripts/wca_snapshotd.py"      # dense odds snapshots near kickoffs (closing lines)
  "com.wca.closecapture|600|$WCA_PY|scripts/wca_close_capture.py"     # stamp closing_odds+CLV every 10 min (fixes stale CLV)
  "com.wca.pmpropose|1800|$WCA_PY|scripts/wca_pm_propose.py"          # park PM proposals + notify (needs PM + Telegram reachable) — every 30 min
  "com.wca.build_card|3600|$WCA_PY|scripts/wca_build_card.py"         # refresh card hourly (models + odds) so bot /card is always fresh
  "com.wca.publish|3600|/bin/bash|deploy/publish_site.sh"            # hourly: refresh scores + regen site + AUTO-COMMIT & push
  "com.wca.sync|300|/bin/bash|deploy/sync.sh"                        # every 5 min: git pull --rebase origin/main + restart daemons on code change
)

for entry in "${SERVICES[@]}"; do
  IFS='|' read -r label sched prog args <<<"$entry"
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
    <key>WCA_REPO</key><string>$WCA_REPO</string>
    <key>WCA_PY</key><string>$WCA_PY</string>
  </dict>
  <key>ProgramArguments</key><array>
    <string>$prog</string>
    $(for a in $args; do echo "<string>$a</string>"; done)
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
