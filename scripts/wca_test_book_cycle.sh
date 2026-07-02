#!/bin/bash
# Paper test-book trade cycle — run every 5-10 min by launchd on the mini.
#
# PAPER ONLY: writes solely to data/test_book.db (a fake-$2000 book). It places
# NO real orders and never touches the real ledger (data/wca.db). Safe to loop.
#
# Each cycle: settle resolvable bets -> scan + place new +EV paper fills ->
# mark open positions to the latest CLOB price. Appends to logs/test_book.log.
set -uo pipefail

REPO="${WCA_REPO:-$HOME/World-Cup-26}"
cd "$REPO" || { echo "no repo at $REPO"; exit 1; }
mkdir -p logs

# --- pause / kill switch --------------------------------------------------
# Pause WITHOUT touching the mini: commit deploy/testbook.switch = "off" (from
# anywhere). The mini's autopull (origin/main) brings it in and the next cycle
# no-ops; set it back to "on" to resume. Local hands-on overrides on the mini:
#   env WCA_TESTBOOK_OFF=1   or   a data/TESTBOOK_PAUSED kill-file.
# When paused the whole cycle (settle/trade/mark) is skipped — no paper fills.
_paused=""
case "${WCA_TESTBOOK_OFF:-}" in 1|true|yes|on|TRUE|YES|ON) _paused="env WCA_TESTBOOK_OFF" ;; esac
[ -z "$_paused" ] && [ -f "$REPO/data/TESTBOOK_PAUSED" ] && _paused="kill-file data/TESTBOOK_PAUSED"
if [ -z "$_paused" ] && [ -f "$REPO/deploy/testbook.switch" ]; then
  _state="$(tr -d '[:space:]' < "$REPO/deploy/testbook.switch" | tr '[:upper:]' '[:lower:]')"
  case "$_state" in off|0|paused|no|false) _paused="deploy/testbook.switch=$_state" ;; esac
fi
if [ -n "$_paused" ]; then
  echo "================ test-book cycle $(date -u +%Y-%m-%dT%H:%M:%SZ) ================" >> logs/test_book.log
  echo "PAUSED via $_paused — skipping settle/trade/mark (no paper fills)" >> logs/test_book.log
  exit 0
fi

# Load @worldcupdevbot credentials so the paper book can ping the dev chat.
# Prefers a dedicated test-book env, falls back to the conductor's.
for envf in "${WCA_TESTBOOK_ENV:-}" "$REPO/.env.testbook" "$REPO/.env.conductor" "$HOME/.env.testbook" "$HOME/.env.conductor"; do
  [ -n "$envf" ] && [ -f "$envf" ] && set -a && . "$envf" && set +a && break
done

# Use the project venv python (has requests/numpy); fall back to python3.
PY="python3"
[ -x "$REPO/.venv/bin/python" ] && PY="$REPO/.venv/bin/python"

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
{
  echo "================ test-book cycle $TS ================"
  PYTHONPATH=src "$PY" scripts/wca_test_book.py settle
  PYTHONPATH=src "$PY" scripts/wca_test_book.py trade --edge "${WCA_TESTBOOK_EDGE:-0.04}"
  PYTHONPATH=src "$PY" scripts/wca_test_book.py mark
} >> logs/test_book.log 2>&1
