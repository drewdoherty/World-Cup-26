#!/usr/bin/env bash
# gh_pr.sh — open a GitHub PR with gh when available, else the REST API.
#
# Why this exists: the dev-conductor's spawned agents were launched with a
# minimal PATH (launchd/cron/GUI) that did not include ~/.local/bin, so `gh`
# was reported "not found" even though it is installed — every task pushed a
# branch but no PR opened. This script is the belt-and-braces fallback the
# conductor (and a human) can call from anywhere: it augments PATH to find an
# installed gh, and if gh is genuinely missing it falls back to a direct REST
# call using a token from `gh auth token`, $GH_TOKEN, or $GITHUB_TOKEN.
#
# Usage:
#   scripts/gh_pr.sh --head <branch> [--base main] --title "T" --body "B" [--repo owner/name]
#
# Exit 0 prints the PR URL on stdout. Non-zero prints a compare URL (still
# actionable) and the error on stderr — it never hard-fails the caller.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0

# Make installed user/Homebrew tools discoverable regardless of launch context.
export PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

BASE="main"; HEAD=""; TITLE=""; BODY=""; REPO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --base)  BASE="$2";  shift 2;;
    --head)  HEAD="$2";  shift 2;;
    --title) TITLE="$2"; shift 2;;
    --body)  BODY="$2";  shift 2;;
    --repo)  REPO="$2";  shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$HEAD" ]  || { echo "--head is required" >&2; exit 2; }
[ -n "$TITLE" ] || { echo "--title is required" >&2; exit 2; }

# Resolve repo slug (owner/name) from the origin remote if not given.
if [ -z "$REPO" ]; then
  url="$(git remote get-url origin 2>/dev/null || true)"
  REPO="$(printf '%s' "$url" | sed -E 's#(git@[^:]+:|https?://[^/]+/)##; s#\.git$##; s#/$##')"
fi
compare_url="https://github.com/${REPO}/compare/${BASE}...${HEAD}?expand=1"

# Tier 1: gh, if it resolves on the (augmented) PATH.
if command -v gh >/dev/null 2>&1; then
  if url="$(gh pr create --base "$BASE" --head "$HEAD" --title "$TITLE" --body "$BODY" 2>/dev/null)"; then
    printf '%s\n' "$url"; exit 0
  fi
fi

# Tier 2: REST API with a token.
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [ -z "$TOKEN" ] && command -v gh >/dev/null 2>&1; then
  TOKEN="$(gh auth token 2>/dev/null || true)"
fi
if [ -n "$TOKEN" ] && [ -n "$REPO" ]; then
  payload="$(printf '{"title":%s,"head":%s,"base":%s,"body":%s}' \
    "$(printf '%s' "$TITLE" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
    "$(printf '%s' "$HEAD"  | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
    "$(printf '%s' "$BASE"  | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
    "$(printf '%s' "$BODY"  | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")"
  resp="$(curl -fsS -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "User-Agent: wca-conductor" \
    "https://api.github.com/repos/${REPO}/pulls" \
    -d "$payload" 2>/dev/null || true)"
  html_url="$(printf '%s' "$resp" | python3 -c 'import json,sys;
try: print(json.load(sys.stdin).get("html_url",""))
except Exception: pass' 2>/dev/null || true)"
  if [ -n "$html_url" ]; then printf '%s\n' "$html_url"; exit 0; fi
fi

# Tier 3: never hard-fail — hand back a compare link.
echo "could not open PR automatically (gh missing/unauth and no token)" >&2
printf '%s\n' "$compare_url"
exit 1
