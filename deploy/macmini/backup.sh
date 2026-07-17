#!/bin/bash
# Rotating online backup of the ledger DB. Gzip'd (sqlite3 .backup is ~18:1
# compressible) with tiered retention: keep everything from the last 3h, then
# 1/day out to 7d, then 1/week out to 28d. Steady state ~2.5-3GB instead of
# the old flat KEEP=48 uncompressed scheme (~97GB for ~12h of history).
#
# Restore: gunzip -k data/backups/wca_<ts>.db.gz && \
#          sqlite3 data/backups/wca_<ts>.db "PRAGMA integrity_check;"
#
# These stay local (the repo is public — the ledger must never be committed).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
DB="$REPO_ROOT/data/wca.db"
DIR="$REPO_ROOT/data/backups"

RECENT_WINDOW_S=$((3 * 3600))     # keep every backup newer than this
DAILY_WINDOW_S=$((7 * 86400))     # beyond that, keep the newest 1/calendar-day
WEEKLY_WINDOW_S=$((28 * 86400))   # beyond that, keep the newest 1/ISO-week
                                   # beyond that: deleted

[ -f "$DB" ] || { echo "no db at $DB"; exit 0; }
mkdir -p "$DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
raw="$DIR/wca_${ts}.db"
# .backup is a consistent online copy even while daemons hold the DB open.
if sqlite3 "$DB" ".backup '$raw'"; then
    gzip -f "$raw" && echo "backup -> ${raw}.gz"
else
    echo "backup failed" >&2
    rm -f "$raw"
fi

# --- tiered prune (also gzips any surviving legacy uncompressed backup) ---
# Bucket keys are compared against the previous (newer) file's bucket since
# the list is walked newest-first — first file seen in a bucket is the one
# kept. Plain scalars, not associative arrays: the mini's /bin/bash is 3.2.
now="$(date -u +%s)"
last_day=""
last_week=""
for f in $(ls -1 "$DIR"/wca_*.db "$DIR"/wca_*.db.gz 2>/dev/null | sort -r); do
    base="$(basename "$f")"
    stamp="$(echo "$base" | sed -E 's/^wca_([0-9]{8}T[0-9]{6}Z)\.db(\.gz)?$/\1/')"
    [ "$stamp" = "$base" ] && continue  # not one of ours (e.g. wedge-*, precard_*)
    file_epoch="$(date -j -u -f "%Y%m%dT%H%M%SZ" "$stamp" +%s 2>/dev/null)" || continue
    age=$(( now - file_epoch ))

    keep=1
    if [ "$age" -le "$RECENT_WINDOW_S" ]; then
        keep=1
    elif [ "$age" -le "$DAILY_WINDOW_S" ]; then
        day="$(date -j -u -f "%Y%m%dT%H%M%SZ" "$stamp" +%Y%m%d)"
        if [ "$day" = "$last_day" ]; then keep=0; else keep=1; last_day="$day"; fi
    elif [ "$age" -le "$WEEKLY_WINDOW_S" ]; then
        week="$(date -j -u -f "%Y%m%dT%H%M%SZ" "$stamp" +%G-%V)"
        if [ "$week" = "$last_week" ]; then keep=0; else keep=1; last_week="$week"; fi
    else
        keep=0
    fi

    if [ "$keep" -eq 0 ]; then
        rm -f "$f"
    elif [[ "$f" == *.db ]]; then
        gzip -f "$f"  # compress a legacy uncompressed survivor in place
    fi
done
