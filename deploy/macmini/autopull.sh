#!/bin/bash
# Pull the latest code from origin/main and restart any daemon whose code changed.
# Runs on the Mac mini only. Coexists with the site/state auto-sync that also
# commits to main, by rebasing rather than resetting.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=/dev/null
source "$HERE/services.env"

before="$(git rev-parse HEAD 2>/dev/null || echo none)"
git fetch --quiet origin main || { echo "fetch failed"; exit 0; }
# Rebase local site/state commits on top of incoming code; never lose local work.
if ! git pull --rebase --autostash --quiet origin main; then
  echo "autopull: rebase conflict — leaving repo untouched for manual review" >&2
  git rebase --abort 2>/dev/null || true
  exit 0
fi
after="$(git rev-parse HEAD)"
[ "$before" = "$after" ] && exit 0

changed="$(git diff --name-only "$before" "$after" 2>/dev/null || true)"
echo "autopull: $before -> $after"
# Restart daemons if any executable code changed.
if echo "$changed" | grep -qE '^(src/|scripts/|deploy/)'; then
  for d in "${WCA_DAEMONS[@]}"; do
    label="${WCA_LABEL_PREFIX}.${d}"
    launchctl kickstart -k "gui/$(id -u)/${label}" 2>/dev/null \
      || { launchctl unload "$HOME/Library/LaunchAgents/${label}.plist" 2>/dev/null; \
           launchctl load -w "$HOME/Library/LaunchAgents/${label}.plist" 2>/dev/null; }
    echo "  restarted $label"
  done
fi
