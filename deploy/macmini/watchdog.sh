#!/bin/bash
# Liveness watchdog for the WCA launchd daemons (the Mac mini host).
#
# WHY THIS EXISTS
#   KeepAlive respawns a crashed daemon, but it is SILENT. A daemon that
#   crash-loops (e.g. @WorldCupDev with a missing .env.conductor → exits on
#   every launch), or a job that was never bootstrapped at all, looks "fine"
#   to launchd and nobody is told. The visible symptom is stale data with no
#   alert. This watchdog samples each daemon's launchd state, debounces
#   transient restarts, and pings Telegram when one is down/flapping or its log
#   has gone stale.
#
# It is READ-ONLY: it never restarts, loads, or mutates anything — diagnosis
# only, so it can never make an outage worse. It is wired as the 'watchdog'
# StartInterval job in services.env (every 5 min by default).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=/dev/null
source "$HERE/services.env"

LOGS="$REPO_ROOT/logs"
STATE="$LOGS/watchdog.state"            # "<label> <consecutive_down_count>" per line
mkdir -p "$LOGS"
touch "$STATE"

STALE_LOG_SECS="${WCA_WATCHDOG_STALE_LOG_SECS:-3600}"  # daemon log silent >1h ⇒ suspect-hung
STRIKES="${WCA_WATCHDOG_STRIKES:-2}"                   # consecutive bad samples before alerting

# --- Telegram (best-effort; bounded; never blocks the watchdog) -------------
# Reuse the live bot's creds from .env (the channel the operator already watches).
ENV_FILE="$REPO_ROOT/.env"
get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'"; }
TG_TOKEN="$(get_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(get_env TELEGRAM_ADMIN_USER_ID)"; [ -n "$TG_CHAT" ] || TG_CHAT="$(get_env TELEGRAM_CHAT_ID)"

notify() {  # message
  if [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ]; then
    echo "watchdog: no telegram creds in .env — alert not sent" >&2; return
  fi
  curl -s -m 10 -o /dev/null \
    "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT}" \
    --data-urlencode "text=$1" || true
}

read_strikes() { grep -E "^$1 " "$STATE" 2>/dev/null | tail -1 | awk '{print $2}'; }

now="$(date +%s)"
new_state=()
alerts=()

for d in "${WCA_DAEMONS[@]}"; do
  label="${WCA_LABEL_PREFIX}.${d}"
  prev="$(read_strikes "$label")"; prev="${prev:-0}"

  # launchctl list columns: PID  Status(last-exit)  Label
  status_line="$(launchctl list 2>/dev/null | awk -v l="$label" '$3==l {print}')"
  down=0; reason=""
  if [ -z "$status_line" ]; then
    down=1; reason="NOT LOADED (never bootstrapped, or unloaded) — run install.sh"
  else
    pid="$(echo "$status_line" | awk '{print $1}')"
    exitcode="$(echo "$status_line" | awk '{print $2}')"
    if [ "$pid" = "-" ]; then
      down=1; reason="not running (last exit ${exitcode}) — crash-loop or dead"
    fi
  fi

  # Even when launchd reports a PID, a wedged process stops logging. Treat a
  # long-silent log as suspect (only when we have a log to judge by).
  logf="$LOGS/${d}.log"
  if [ "$down" -eq 0 ] && [ -f "$logf" ]; then
    mtime="$(stat -f %m "$logf" 2>/dev/null || echo "$now")"
    age=$(( now - mtime ))
    if [ "$age" -gt "$STALE_LOG_SECS" ]; then
      down=1; reason="log silent ${age}s (>${STALE_LOG_SECS}s) — possibly hung"
    fi
  fi

  if [ "$down" -eq 1 ]; then
    cur=$(( prev + 1 ))
    new_state+=("$label $cur")
    # Alert once, exactly when we cross the strike threshold (no per-run spam).
    [ "$cur" -eq "$STRIKES" ] && alerts+=("• ${label}: ${reason}")
  else
    new_state+=("$label 0")
    # If it had previously alerted, announce the recovery.
    [ "$prev" -ge "$STRIKES" ] && alerts+=("• ${label}: ✅ recovered")
  fi
done

# Rewrite state atomically.
: > "$STATE.tmp"
for kv in "${new_state[@]}"; do echo "$kv" >> "$STATE.tmp"; done
mv "$STATE.tmp" "$STATE"

if [ "${#alerts[@]}" -gt 0 ]; then
  host="$(hostname -s 2>/dev/null || echo mini)"
  msg="🔴 WCA watchdog (${host}) — daemon health:"$'\n'"$(printf '%s\n' "${alerts[@]}")"
  echo "$msg" >&2
  notify "$msg"
fi
exit 0
