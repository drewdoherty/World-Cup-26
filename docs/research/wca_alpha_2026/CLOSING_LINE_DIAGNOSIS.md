# Cross-cutting unlock: true closing-line capture — diagnosis + fix (2026-06-30)

The #1 dependency under nearly every microstructure/CLV edge (per
`docs/research/market_microstructure_execution.md`, `improvement_plan.md`,
`phase2_research_program.md`) is **odds capture that runs to kickoff** so a true
closing line exists. It is currently broken — and worse than the recon doc said.

## Root cause (precisely located)
Odds capture has been **silently dead for ~12 days on BOTH durable paths**:

1. **`odds_snapshots` table (mini-local)** — newest row `2026-06-23T06:52Z`. Populated
   only by the mini `snapshotd` daemon (`deploy/macmini`, `WCA_DAEMONS=(... snapshotd ...)`),
   which **stopped on 06-23** (mini down / daemon crash). The table lives in the
   gitignored `data/wca.db`, so nothing else writes it.
2. **Committed raw dumps `data/raw/snapshots/*.json` (cloud)** — newest file
   `oddsapi_multi_uk_20260618T123854Z.json`; last commit touching the dir is **06-18**.
   The cloud `hourly-odds.yml` job still runs (it commits the scores feed on 06-29),
   but its **"Ingest odds snapshot" step (`wca_snapshot_odds.py`) crashes before the
   raw write** (line 87 writes raw first, yet none accrue → the failure is at
   fetch/import time), under `continue-on-error: true`, so it fails invisibly.

The cadence LOGIC is correct and already in `main`: `pollsched.next_poll_delay`
caps idle sleep to wake at the pre-close window and treats closing-line polls as
sacred; `closecapture.py` stamps the last pre-KO pull as the close. **This is not a
logic gap — it is a durability + ops failure.** PM survived only because it persists
to a committed JSONL (`pm_price_history.jsonl`) that needs no DB.

## Fix (two layers)

### A. Immediate operator unblock (no code) — recovers capture for the remaining knockouts
- **Restart the mini `snapshotd` daemon** (`deploy/macmini/install.sh snapshotd`) and
  confirm it stays up. It is the adaptive to-KO capturer; restarting it resumes fine
  closing-line capture for R16 (Jul 4-7), QF, SF, Final.
- **Fix the cloud odds step**: surface why `wca_snapshot_odds.py` fails on the CI runner
  (run `.github/workflows/hourly-odds.yml` via `workflow_dispatch`, read the red log).

### B. Durable fix (code) — make odds capture mini-independent like PM
Mirror the PM pattern so capture survives the mini AND a DB-less runner:
1. **DB-less JSONL mirror.** Append every odds pull to a committed
   `data/odds_price_history.jsonl` (one row per fixture×market×book×selection×ts),
   written BEFORE/independent of any DB touch — exactly how `pmhistory` works. CI can
   then commit it with no DB.
2. **Idempotent ingest.** `wca_ingest_raw_odds` loads the committed raw dumps + the
   JSONL into `odds_snapshots` wherever the DB lives (mini/dev), dedup by ts+fixture+book.
   Recovers history and decouples capture from ingestion.
3. **Robust workflow.** Make the odds step write the JSONL even on partial failure and
   FAIL LOUD (drop `continue-on-error`), so a silent 12-day outage cannot recur. Add a
   near-KO finer-cadence trigger (every ~15 min during match windows) so the cloud path
   captures a real close, not just the hourly grid.

**Verification gates:** `pollsched`/`closecapture` unit tests already exist; add JSONL
round-trip + DB-less-safety + ingest-idempotency tests. Confirm a `workflow_dispatch`
run commits a fresh `odds_price_history.jsonl` row.

## Why this is the unlock
With a true closing line accruing again, the walk-forward CLV harness, convergence
profile, movement-sign, consensus, and disagreement re-tests (all currently NULL purely
from truncated/absent capture) become runnable — and CLV can move from diagnostic to a
gating objective, which is the discipline both directions depend on.
