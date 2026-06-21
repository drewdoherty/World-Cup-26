#!/bin/bash
# Unload and remove all World Cup Alpha launchd agents on the Mac mini.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
# shellcheck source=/dev/null
source "$HERE/services.env"

for name in "${WCA_DAEMONS[@]}" "${WCA_INTERVAL_JOBS[@]}"; do
  label="${WCA_LABEL_PREFIX}.${name}"
  plist="$AGENTS/${label}.plist"
  launchctl unload "$plist" 2>/dev/null || true
  rm -f "$plist"
  echo "  removed $label"
done
echo "All WCA agents removed."
