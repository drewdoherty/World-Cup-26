#!/bin/bash
# Keep the two private localhost dashboards alive.  This is a long-running
# launchd service; feed generation belongs to feedpull/research_cycle.
set -uo pipefail

REPO="${WCA_REPO:-/Users/andrewdoherty/Desktop/Coding/World Cup Alpha}"
PY="${WCA_PY:-$REPO/.venv/bin/python}"
cd "$REPO" || exit 1
mkdir -p logs

child_8000=""
child_8001=""
stop_children() {
  [ -n "$child_8000" ] && kill "$child_8000" 2>/dev/null || true
  [ -n "$child_8001" ] && kill "$child_8001" 2>/dev/null || true
}
trap stop_children EXIT INT TERM

env PORT=8000 BIND=127.0.0.1 PYTHONUNBUFFERED=1 \
  "$PY" scripts/serve_site.py &
child_8000=$!

PYTHONUNBUFFERED=1 "$PY" -m http.server 8001 \
  --bind 127.0.0.1 --directory site-analytics &
child_8001=$!

# macOS still ships Bash 3.2 (no ``wait -n``). Poll both children; if either
# dies, stop the sibling and exit so launchd restarts a complete pair.
while kill -0 "$child_8000" 2>/dev/null && kill -0 "$child_8001" 2>/dev/null; do
  sleep 5
done
exit 1
