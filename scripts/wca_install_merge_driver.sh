#!/bin/bash
# Registers the 'freshest' merge driver this repo's .gitattributes references.
# Git does NOT read driver commands from .gitattributes (arbitrary-code-exec
# guard) — every clone/worktree/machine must run this once. Idempotent.
#
#   bash scripts/wca_install_merge_driver.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

# REGRESSION NOTE (2026-07-09): the driver command is executed by git via sh,
# so the interpreter/script paths need their OWN embedded shell quoting. The
# previous unquoted form broke on any checkout whose path contains spaces
# (dev-box "…/World Cup Alpha" → "/Users/…/World: Permission denied") and
# silently fell back to conflict markers — disabling the whole freshest-wins
# class on that machine. Production (~/World-Cup-26, no spaces) was
# unaffected. Do NOT quote %P: git substitutes it PRE-QUOTED (verified via
# GIT_TRACE — wrapping it again hands the driver a path with literal quote
# characters and breaks its per-file merge-strategy matching). %O/%A/%B are
# git-generated relative temp names (.merge_file_XXXXXX), space-free.
git config merge.freshest.name "keep the fresher daemon-rebuilt artifact"
git config merge.freshest.driver "\"$PY\" \"$HERE/wca_merge_freshest.py\" %O %A %B %P"
echo "Registered git merge driver 'freshest' -> $HERE/wca_merge_freshest.py"
