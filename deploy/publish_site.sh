#!/usr/bin/env bash
# Refresh results/scores + regenerate the public site feed so the live site stays
# current. The regenerated site/*.json feeds are served locally and rsynced to the
# MacBook (deploy/macbook/pull_feeds.sh); they are NOT committed to git (build
# output, see docs/data-and-artifacts.md). This job still commits + pushes the
# durable card/model caches. Driven by the com.wca.publish launchd job (hourly).
# Safe to run repeatedly:
#   * commits ONLY the card/model caches, and only when they actually changed
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
"$PY" -c "from wca.exposure_dashboard import publish_dashboard_json; publish_dashboard_json('data/wca.db')" >/dev/null 2>&1 || true
"$PY" scripts/wca_advancement_history.py >/dev/null 2>&1 || true
# advancement_data.json (Visuals progression panels): model probs are cached in
# data/advancement_current_vs_pretournament.json and only re-simmed when >12h old
# (a cached run is ~10s; a re-sim ~3 min, dominated by the Elo+DC fit), so this is
# cheap on most hourly runs. Live Polymarket prices + group standings refresh every run.
"$PY" scripts/wca_advancement_data.py >/dev/null 2>&1 || true

# 2. stage the cached cards + model predictions; bail if nothing changed.
#    The site/*.json feeds are regenerated above so the local serve + the MacBook
#    rsync (deploy/macbook/pull_feeds.sh) stay fresh, but they are NO LONGER
#    committed to git — feeds are built at serve time, not version-controlled
#    (see docs/data-and-artifacts.md). card_latest.md / next_latest.md /
#    model_predictions.json ARE still committed so the freshly built outputs
#    persist (card git-log history + the exact model 1X2 used by scores/exposure).
git add data/card_latest.md data/next_latest.md data/model_predictions.json \
        data/advancement_current_vs_pretournament.json
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
