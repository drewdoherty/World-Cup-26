#!/bin/bash
# Remove stale agent worktrees that each carry a forked data/wca.db (the collision
# source). Dry-run by default — pass --force to actually remove.
#
#   bash scripts/wca_worktree_cleanup.sh          # list what would go
#   bash scripts/wca_worktree_cleanup.sh --force  # remove them
#
# Never touches the main checkout or any worktree with uncommitted changes.
set -uo pipefail
FORCE=0; [ "${1:-}" = "--force" ] && FORCE=1
MAIN="$(git rev-parse --show-toplevel)"

git worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
  [ "$wt" = "$MAIN" ] && continue
  case "$wt" in
    */.claude/worktrees/*|*/.codex/worktrees/*|/private/tmp/*) ;;
    *) continue ;;   # only ever touch known agent/tmp worktree locations
  esac
  dirty=""
  [ -d "$wt" ] && dirty="$(git -C "$wt" status --porcelain 2>/dev/null | head -1)"
  if [ -n "$dirty" ]; then
    echo "SKIP (uncommitted changes): $wt"
    continue
  fi
  if [ "$FORCE" = "1" ]; then
    git worktree remove --force "$wt" && echo "removed: $wt"
  else
    echo "would remove: $wt"
  fi
done

[ "$FORCE" = "1" ] && git worktree prune && echo "pruned worktree metadata"
[ "$FORCE" = "0" ] && echo "(dry-run — re-run with --force to apply)"
