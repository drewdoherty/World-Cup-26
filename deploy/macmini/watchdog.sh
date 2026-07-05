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
#
# GIT-BEHIND CHECK (P5 / PHASE1_DESIGN.md §9 increment 2)
#   autopull.sh runs `git pull --rebase --autostash` every 5 min but is
#   otherwise silent: a network blip, a rebase conflict (it aborts and exits 0
#   — see autopull.sh), or the job simply not firing all leave the mini stuck
#   on an old commit with fresh data/code sitting un-applied on origin/main
#   and nobody told. The visible symptom is stale data with no alert (this
#   already happened once: a 25h-stale Scores/Bracket page went unnoticed
#   until a screenshot showed wrong results). This check does its own
#   READ-ONLY `git fetch` + `git rev-list --count HEAD..origin/main` and
#   alerts once the repo has been measured behind on ${GIT_BEHIND_STRIKES}
#   consecutive samples (default 2, ~10 min at the 5-min cadence) — long
#   enough to ride out a normal in-flight autopull cycle without false-
#   positiving on it. It never fetches/rebases/resets on its own.
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
GIT_BEHIND_STRIKES="${WCA_WATCHDOG_GIT_BEHIND_STRIKES:-2}"  # consecutive behind-samples before alerting (~10 min @ 5-min cadence)

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

# --- Git-behind check (read-only: fetch + rev-list only, never pull/reset) --
# A failed fetch (network blip) is treated the SAME as "behind" for strike
# purposes — either way we can't confirm the repo is current, and a fetch
# that keeps failing is exactly the kind of silent staleness this exists to
# catch. Only a successful fetch showing zero behind resets the strike.
GIT_BEHIND_LABEL="git.behind"
prev_gb="$(read_strikes "$GIT_BEHIND_LABEL")"; prev_gb="${prev_gb:-0}"
behind_count=""
fetch_ok=1
if git fetch --quiet origin main 2>/dev/null; then
  fetch_ok=0
  behind_count="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo "")"
fi

if [ "$fetch_ok" -ne 0 ] || { [ -n "$behind_count" ] && [ "$behind_count" -gt 0 ] 2>/dev/null; }; then
  cur_gb=$(( prev_gb + 1 ))
  new_state+=("$GIT_BEHIND_LABEL $cur_gb")
  # Alert once, exactly when we cross the strike threshold (no per-run spam);
  # re-alert (without resetting the strike count) if it keeps growing so a
  # widening gap doesn't go silent after the first ping.
  if [ "$cur_gb" -eq "$GIT_BEHIND_STRIKES" ] || { [ "$cur_gb" -gt "$GIT_BEHIND_STRIKES" ] && [ $(( (cur_gb - GIT_BEHIND_STRIKES) % GIT_BEHIND_STRIKES )) -eq 0 ]; }; then
    if [ "$fetch_ok" -ne 0 ]; then
      alerts+=("• ${GIT_BEHIND_LABEL}: git fetch failed for ${cur_gb} consecutive checks — can't confirm repo is current (network? auth?)")
    else
      alerts+=("• ${GIT_BEHIND_LABEL}: local repo is ${behind_count} commit(s) behind origin/main for ${cur_gb} consecutive checks — autopull may be stuck/failing (check logs/autopull.log)")
    fi
  fi
else
  new_state+=("$GIT_BEHIND_LABEL 0")
  [ "$prev_gb" -ge "$GIT_BEHIND_STRIKES" ] && alerts+=("• ${GIT_BEHIND_LABEL}: ✅ recovered (repo is up to date with origin/main)")
fi

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
