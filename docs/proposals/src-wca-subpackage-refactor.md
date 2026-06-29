# Proposal: fold the loose `src/wca/*.py` modules into subpackages

**Status:** proposed, not executed. **Why not yet:** this is a wide import
rewrite (every `from wca.<mod>` across ~132 modules + 99 test files + 85 scripts
must change). The repo is worked by a parallel conductor swarm with many live
worktrees/branches off `main`; doing the move now would conflict with all of
them, and the suite can't be verified from a size-cleanup branch. Run it as a
**dedicated PR in a quiet window**, in slices, with `pytest -q` green per slice.

## Current state

`src/wca` = ~50k LOC across 132 modules: 14 subpackages
(`archive bench bot conductor data intel ledger markets mc models pm predledger
rigor sim`) **plus 35 loose top-level `*.py`** that should live in a subpackage.

The import graph (AST scan of `src` + `scripts` + `tests`) found **no dead loose
modules** — every one has ≥1 internal importer or a shell/CI entry point. So this
is pure reorganization, **not** deletion. (Caveat: the graph cannot see
`python -c` / `python -m` / shell-invoked entry points, e.g. `exposure_dashboard`
is reached only via `deploy/publish_site.sh`.)

## Proposed mapping (loose module → subpackage)

| Target subpackage | Loose modules | Notes |
|---|---|---|
| `wca/site/` (new) | `sitedata`, `scorespage`, `linemove`, `dashboard`, `exposure_dashboard`, `sync`, `cardcache` | the site-feed builders |
| `wca/card/` (new) | `card`, `nextmatch`, `accas`, `modelpreds` | card + selection build (high fan-in: `card`←23, `accas`←5) |
| `wca/promos/` (new) | `promos`, `promosdata`, `boosts`, `offers`, `matched` | promo/matched-betting |
| `wca/exposure/` (new) | `exposure`, `exposure_corr` | whole-book risk/sizing |
| `wca/arb/` (new) | `arb`, `arbdata`, `arbfx`, `fx` | arbitrage + FX |
| `wca/tracking/` (new) | `tracking`, `advancement` | prediction tracking / advancement |
| `wca/intel/` (exists) | `venues`, `venuesbench`, `venuesdata` | venue benchmark already adjacent to intel |
| `wca/pm/` (exists) | `pmhistory`, `pollsched`, `positions_sync`, `closecapture` | Polymarket-side |
| `wca/bench/` (exists) | `clvbench`, `winrate`, `outrightedge` | benchmarking feeds |
| `wca/data/` (exists) | `news` | news ingestion |

Result: loose top-level count 35 → 0; 50k LOC gains a real package structure.

## Execution procedure (per slice)

1. `git mv src/wca/<mod>.py src/wca/<pkg>/<mod>.py` (one subpackage at a time).
2. Rewrite importers: `from wca.<mod>` → `from wca.<pkg>.<mod>` (and
   `from wca import <mod>` → `from wca.<pkg> import <mod>`) across `src`,
   `scripts`, `tests`.
3. Grep for **non-import** references the AST graph misses: `python -m wca.<mod>`,
   `from wca.<mod> import` inside shell `python -c` strings (`deploy/*.sh`,
   `*.plist`, `.github/workflows/*`), and any `importlib`/string module names.
4. Optional: leave a thin re-export shim at the old path for one release if an
   out-of-tree caller (mini launchd, other branches) imports the old name.
5. `pytest -q` green before moving to the next slice.

## Sequencing

Do the **leaf** subpackages first (`arb`, `exposure`, `tracking`, `bench`
additions) — fewer importers, smaller blast radius — then the high-fan-in
`card`/`site` groups last. Coordinate with the conductor swarm so no other task
is mid-edit on `src/wca/bot/app.py`, `accas.py`, or `wca_build_card.py` (the
known high-overlap files per AGENTS.md §2.7).
