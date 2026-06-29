# Data & Generated Artifacts — Storage Policy

**Rule: raw data and generated artifacts are NEVER committed to git.** They are
either re-downloadable from their source or rebuilt at deploy time. Git tracks
*source* (code, config, durable hand-curated datasets) — not runtime input or
build output.

## Raw odds-API snapshots — `data/raw/snapshots/`

- **What:** point-in-time JSON dumps from the odds API (`oddsapi_*.json`),
  written by the snapshot poller. ~533 files / ~625 MB at the time they were
  removed from tracking.
- **Status:** untracked. Covered by `data/raw/*` in `.gitignore`. The previous
  `!data/raw/snapshots/` exemption that force-re-added them has been removed.
- **They stay on disk locally** — only the git tracking was removed.
- **Where they should live instead** (pick per environment; not yet wired):
  - **Object storage** (S3 / R2 / GCS) under a `raw/snapshots/` prefix — the
    natural home; cheap, lifecycle-expirable, matches the existing
    `data/archive/` "mirrored to cloud object storage" pattern.
  - **DVC** (`dvc add data/raw/snapshots`) if we want content-addressed
    versioning with a git-tracked `.dvc` pointer but the bytes in a remote.
  - **Release assets / tarball** if an occasional frozen bundle is enough.
- **Recovery:** re-downloadable from the odds API; historical snapshots that
  predate any cloud mirror are only on the machines that captured them, so back
  them up to object storage before relying on this.

## Generated site feeds — `site/*.json`, `site/microstructure/*.json`

- **What:** 15 top-level JSON feeds (`site/data.json`, `site/linemove.json`
  (~1.76 MB), `scores_data.json`, `bet_recs.json`, `promos_data.json`,
  `tracking_data.json`, `exposure_*`, `advancement_*`, `arb_*`, …) produced by the
  card / scores / promos / sync jobs (`src/wca/sitedata.py`, `linemove.py`,
  `scorespage.py`, `promosdata.py`, `arbdata.py`, … and the `scripts/wca_*_data.py`
  wrappers).
- **Status: untracked.** `git rm --cached site/*.json` + `site/*.json` in
  `.gitignore`. Files stay on disk; they are regenerated, not committed.
- **How they reach the live sites (git is NOT the transport):**
  - The **Mac Mini** regenerates the feeds locally every hour
    (`deploy/publish_site.sh`) and serves them from its own working tree.
  - The **MacBook** localhost sites (ports 8000/8001) pull the feeds via **rsync
    over SSH** from the mini (`deploy/macbook/pull_feeds.sh`) — explicitly *"without
    going through git"*.
  - So the serving paths never depended on the git-tracked copies; the four git
    committers below were redundant churn (`site/linemove.json` alone was
    re-committed on most Auto-sync runs).
- **Publish path migrated off git** (this branch) — the four committers now
  regenerate feeds but no longer `git add`/commit them:
  - `.github/workflows/daily-card.yml`, `daily-promos.yml`, `hourly-odds.yml`
  - `deploy/publish_site.sh` (the mini's hourly `com.wca.publish` job)
- **One residual external check (not in this repo):** if a **Vercel** project is
  still serving `origin/main`, it no longer receives feeds via git and would go
  stale — repoint it to a deploy-time build or to rsync/object storage, or confirm
  it is retired (the sites were moved to localhost after hitting the Vercel quota).
- **`site/microstructure/*.json` is intentionally still tracked** for now: it is
  frozen one-off output of the `scripts/microstructure/` recon (no live job
  regenerates it, so it does not churn) and its consumer transport isn't wired to
  rsync. Untrack it in a follow-up once a regeneration/serve path is confirmed.
