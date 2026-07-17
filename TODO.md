# World Cup Alpha - current work queue

**Reconciled:** 2026-07-17. This queue contains verified open work only. It is
not a live-position list and must never carry stale trade instructions.

## Tournament closeout - P0

- [ ] After France vs England, ingest the authoritative result, rebuild the
  card/scores/event feeds, and settle every shadow market supported by the
  available structured match events. Leave ambiguous rows open.
- [ ] After Spain vs Argentina, repeat the result/feed/shadow settlement cycle,
  then publish the first shadow calibration and paper-P&L report. State sample
  sizes and keep market-only exploration separate from model-backed entries.
- [ ] Correct the final-pairing provenance mismatch: current primary feeds list
  Spain vs Argentina as a fixed fixture, while the advancement edge-desk feed
  can still label the same final as projected.
- [ ] Verify closing-price capture and settlement on the canonical mini ledger
  for both remaining fixtures. Do not settle from a MacBook database copy.

## Event forest and shadow book - P0

- [ ] Merge the shadow-book branch, then have a human run
  `bash deploy/macmini/install.sh` on the Mac mini. Verify
  `com.wca.shadowbook` exists and is cycling before calling it scheduled.
- [ ] Give the complete event forest one durable PM-capable refresh path. The
  primary publish script now preserves the full feed and can rebuild it only
  when `WCA_EVENT_MARKETS=1`; GitHub workflows still invoke the legacy
  `wca_forest_data.py` builder and can overwrite the complete forest.
- [ ] Review and commit the concurrent MacBook `com.wca.research` job before
  installing it. Replace its use of the MacBook `data/wca.db` and hard-coded
  shadow bankroll with an explicit non-canonical/base input or a safe
  read-only relay from the mini; local ledger P&L must not size research rows
  as though it were current.
- [ ] Run `scripts/wca_exposure_reconcile.py` after both bet-rec builders or
  wire it into the publish chain so duplicate same-tie exposure cannot reappear.
- [ ] Add automatic, auditable shadow settlement inputs for all supported
  event families. Current structured settlement intentionally leaves labels
  unresolved when the result feed lacks the required event detail.
- [ ] Add log rotation for `logs/shadow_book.log` alongside the existing mini
  log-hygiene work.

## Hyperliquid/Polymarket research - P0 before any promotion

- [ ] Review and integrate `src/wca/hl/dominance.py` and its tests; the generic
  dominance-bounds work is currently concurrent/untracked, not production.
- [ ] Feed generic advancement-vs-1X2 dominance candidates into the shadow book
  without weakening matched-settlement and staleness gates.
- [ ] Verify the Hyperliquid settlement fee from an authoritative specification
  or an observed settled fill. Until then, positive margins remain
  `CANDIDATE_FEE_UNVERIFIED`.
- [ ] Capture synchronized depth snapshots with bounded leg skew and retain
  enough history to estimate persistence, fillability, fee drag, and adverse
  selection. A single snapshot proves existence only.
- [ ] Model every cancellation, postponement, deadline-gap, co-champion, and
  half-void branch before classifying a basket as covered.
- [ ] Keep Hyperliquid execution out of scope until price capture, CLV,
  settlement automation, controls, and a human go/no-go review all exist.

## Operations and data durability - P1

- [ ] Verify, without printing secret values, that the MacBook defaults to
  `PM_DRY_RUN=1`, uses `data/dev.db`, and holds no unnecessary live signing
  keys. Add a host-level live-mode gate if this cannot be guaranteed.
- [ ] Verify the mini's off-box ledger backup. Local 15-minute rotating copies
  are not disaster recovery; confirm the object-store mirror is configured and
  test a restore.
- [ ] Close durable price-history gaps. Advancement CLV relay exists, but PM
  CLOB history and sportsbook close history are not yet complete end to end.
- [ ] Keep match-day PM orderflow capture running while markets are open; the
  upstream offset cap makes missed history unrecoverable.
- [ ] Verify the `merge=freshest` driver on every active checkout after machine
  or account changes.
- [ ] Wire a recurring shootout/result-detail refresh if shadow settlement will
  depend on scorer, corner, half, extra-time, or penalty events.

## Model evidence - P1/P2

- [ ] Graduate or kill F7 goal-blend only after out-of-sample CLV/calibration
  evidence; do not promote from the present small sample.
- [ ] Build an ET/penalties goal-rate and conditional shootout model. The
  dominance identity accepts a conditional tie-win probability, but that does
  not make the current estimate decision-grade.
- [ ] Complete a full-slate prediction ledger and sharp-source weighting study
  with look-ahead guards.
- [ ] Revisit totals only with new evidence. Current under-side signals remain
  display-only because the measured under calls were materially poor.

## Post-tournament simplification - P2

- [ ] Consolidate `site-analytics/` into the primary localhost surface, then
  retire the duplicate serving/build path.
- [ ] Replace tracked generated-feed transport before untracking daemon-written
  `site/*.json` and `data/*_latest.md` artifacts.
- [ ] Triage and remove stale remote branches and scratch worktrees only after
  checking each for uncommitted work.
- [ ] Plan repository-history cleanup for large generated artifacts after the
  tournament; do not rewrite history during live operations.

## Standing non-goals

- Betfair execution remains a no-build decision; read-only reference data only.
- No hosted dashboard deployment; localhost remains the supported surface.
- No cash on correct scores, scorer props, unboosted same-game multiples, or
  model probabilities below 25%.
