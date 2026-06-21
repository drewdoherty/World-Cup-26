#!/bin/bash
# Rotating online backup of the ledger DB. Keeps the most recent KEEP snapshots.
# These stay local (the repo is public — the ledger must never be committed).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
DB="$REPO_ROOT/data/wca.db"
DIR="$REPO_ROOT/data/backups"
KEEP=48

[ -f "$DB" ] || { echo "no db at $DB"; exit 0; }
mkdir -p "$DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DIR/wca_${ts}.db"
# .backup is a consistent online copy even while daemons hold the DB open.
sqlite3 "$DB" ".backup '$out'" && echo "backup -> $out"
# Prune all but the newest $KEEP.
ls -1t "$DIR"/wca_*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; done
