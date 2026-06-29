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

See the entry added in Step 2. These are build output produced by the card /
feed / sync jobs and published at deploy time; they are not source.
