#!/bin/bash
# Single-instance launcher. Prevents a manual run from colliding with the
# launchd-managed one (e.g. two Telegram bots → 409 Conflict).
#
# Usage: run_singleton.sh <name> <command...>
# Exits 0 without running if another instance with the same <name> is alive.
set -uo pipefail

name="${1:?usage: run_singleton.sh <name> <cmd...>}"
shift

lock="/tmp/wca-${name}.pid"
if [ -f "$lock" ]; then
  old="$(cat "$lock" 2>/dev/null || true)"
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    echo "[$name] already running (pid $old) — not starting a second instance" >&2
    exit 0
  fi
fi

echo $$ > "$lock"
# exec preserves the PID written above, so the lockfile tracks the real process.
exec "$@"
