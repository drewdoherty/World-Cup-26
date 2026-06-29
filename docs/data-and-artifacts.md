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

- **What:** ~27 JSON feeds (data.json, linemove.json (~1.76 MB), scores_data.json,
  bet_recs.json, promos_data.json, the `microstructure/*` set, …) produced by the
  card / scores / promos / sync jobs (`src/wca/sitedata.py`, `linemove.py`,
  `scorespage.py`, `promosdata.py`, `arbdata.py`, … and the `scripts/wca_*_data.py`
  wrappers).
- **Target policy:** generated build output — should be rebuilt at deploy/serve
  time, not committed.
- **⚠ Current publish path is git — untracking is NOT yet a no-op.** Today these
  files are *published by being committed to `main`*:
  - `.github/workflows/daily-card.yml` → `git add site/data.json site/linemove.json
    site/scores_data.json site/bet_recs.json` then push to `main`.
  - `.github/workflows/daily-promos.yml` → `git add site/promos_data.json`.
  - `.github/workflows/hourly-odds.yml` → `git add site/scores_data.json
    site/scores_markets.json`.
  - Consumers read the tracked copies: Vercel serves `origin/main`; the Mac Mini
    autopulls `main` and serves it (and runs its own build that also pushes).
- **What breaks if we just `.gitignore` them now:**
  1. Those CI jobs name the files **explicitly** in `git add`; `git add` on an
     ignored path exits non-zero, and the workflow shell runs with `-e`, so the
     Auto-sync jobs would **fail**.
  2. Any consumer that gets site data from `origin/main` (Vercel; the mini when
     its own build is down) would go **stale**.
- **Migration required before untracking** (each is pipeline change, out of scope
  for a "touch-zero-logic" cleanup step — needs explicit sign-off):
  1. Stop committing site feeds: drop the `site/*.json` paths from the `git add`
     lists in the three workflows above (and the mini's build/push service).
  2. Replace the transport: have the deploy/serve layer build the feeds locally at
     startup/deploy (the generator scripts already exist), or publish them to
     object storage / a release asset the site reads.
  3. Only then add `site/*.json` + `site/microstructure/*.json` to `.gitignore`
     and `git rm --cached` them.
- The localhost serve on the mini (generates feeds locally) is not directly broken
  by untracking, but the git-based propagation to other consumers is.
