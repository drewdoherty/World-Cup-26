#!/usr/bin/env bash
# Refresh results/scores + regenerate the public site feed, then commit & push —
# so the live site stays current without a manual push. Driven by the
# com.wca.publish launchd job (hourly). Safe to run repeatedly:
#   * commits ONLY the four site feeds, and only when they actually changed
#   * rebases before pushing to absorb the cloud Actions' commits (no conflicts)
set -uo pipefail
cd "${WCA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${WCA_PY:-.venv/bin/python}"
export PYTHONPATH="$PWD/src"
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# 1. refresh results + regenerate feeds (tolerate transient failures)
"$PY" scripts/wca_scores_data.py   >/dev/null 2>&1 || true
"$PY" scripts/wca_site.py          >/dev/null 2>&1 || true
"$PY" scripts/wca_tracking_data.py >/dev/null 2>&1 || true
"$PY" scripts/wca_exposure_data.py >/dev/null 2>&1 || true
"$PY" scripts/wca_advancement_history.py >/dev/null 2>&1 || true

# 2. stage the site feed + the cached cards; bail if nothing changed.
#    card_latest.md / next_latest.md / model_predictions.json are committed here so
#    the freshly built outputs (1) persist to git instead of being reverted to a
#    stale blob by com.wca.sync and (2) feed downstream (card git-log history, and
#    the exact model 1X2 used by scores/exposure).
git add site/data.json site/linemove.json site/scores_data.json site/tracking_data.json \
        site/exposure_data.json site/advancement_history.json \
        data/card_latest.md data/next_latest.md data/model_predictions.json
if git diff --cached --quiet; then
  echo "$(stamp) publish: no site changes"; exit 0
fi
git commit -q -m "Auto-sync site: scheduled publish $(stamp)"

# 3. absorb any cloud-Action commits, then push (gated by WCA_AUTOPUSH; default on)
if [ "${WCA_AUTOPUSH:-1}" = "1" ]; then
  git -c rebase.autoStash=true pull --rebase origin main >/dev/null 2>&1 || true
  if git push origin main >/dev/null 2>&1; then
    echo "$(stamp) publish: pushed"
  else
    echo "$(stamp) publish: push FAILED (will retry next run)"
  fi
else
  echo "$(stamp) publish: committed locally (WCA_AUTOPUSH=0, push skipped)"
fi
