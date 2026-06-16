#!/usr/bin/env bash
# deploy/sync.sh — pull the latest code from origin/main and restart the
# long-running daemons so they run it. Built for the Mac Mini ops host and
# driven by the com.wca.sync launchd job (also runnable by hand any time).
#
# Model: this host KEEPS publishing (it alone holds data/wca.db), so we update
# with `git pull --rebase` — NEVER `git reset --hard`, which would discard this
# machine's own site/ledger commits. Local commits (e.g. from an offline push)
# are replayed on top of origin/main; normally it is a clean fast-forward.
#
# Safe to run repeatedly and alongside the publish / bot-autopush jobs: every
# git step tolerates failure and the script exits 0 so launchd never thrashes.
# Daemons are restarted ONLY when the code actually changed.
set -uo pipefail

REPO="${WCA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO" || { echo "sync: cannot cd to $REPO" >&2; exit 0; }
LOG="$REPO/data/com.wca.sync.run.log"
mkdir -p "$REPO/data"
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

before="$(git rev-parse HEAD 2>/dev/null || echo unknown)"

if ! git fetch origin main --quiet 2>>"$LOG"; then
  echo "$(ts) sync: fetch failed (offline?) — will retry next run" >>"$LOG"
  exit 0
fi

# build_card regenerates these tracked artifacts locally each cycle; discard any
# uncommitted local copies before pulling so the rebase can never hit an autostash
# conflict (the failure that corrupted data files on 2026-06-16). They are
# regenerated next cycle and republished by com.wca.publish.
# NOTE: model_predictions_log.jsonl is append-only history that feeds tracking —
# it is deliberately NOT discarded here (autostash preserves its appends).
git checkout -- data/card_latest.md data/next_latest.md data/model_predictions.json 2>/dev/null || true
# Rebase local commits onto origin/main. On ANY conflict, abort the rebase, discard
# stray working changes, and drop a half-applied stash so the tree is left clean
# (never with conflict markers).
if ! git pull --rebase --autostash origin main >>"$LOG" 2>&1; then
  echo "$(ts) sync: rebase failed — aborting + clearing stash to leave the repo clean" >>"$LOG"
  git rebase --abort 2>/dev/null || true
  git checkout -- . 2>/dev/null || true
  git stash list 2>/dev/null | grep -q . && git stash drop 2>/dev/null || true
  exit 0
fi

after="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
if [ "$before" = "$after" ]; then
  echo "$(ts) sync: up to date ($after)" >>"$LOG"
  exit 0
fi
echo "$(ts) sync: updated $before -> $after — restarting daemons" >>"$LOG"

# Restart only the long-running (KeepAlive) daemons; the interval jobs
# (closecapture / pmpropose / publish) fork a fresh process each tick and pick
# up new code on their own. Never restart com.wca.sync (this job itself).
LA="$HOME/Library/LaunchAgents"
for label in com.wca.bot com.wca.snapshotd; do
  plist="$LA/$label.plist"
  [ -f "$plist" ] || { echo "$(ts) sync: $label.plist not installed, skipping" >>"$LOG"; continue; }
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load   "$plist" 2>/dev/null || true
  echo "$(ts) sync: restarted $label" >>"$LOG"
done
echo "$(ts) sync: done" >>"$LOG"
