# Architecture Decision Records — overhaul (2026-07-02)

Added at external-review adjudication: the three largest calls in
`PHASE1_DESIGN.md`, recorded with alternatives and rejection criteria instead of
being presented as predetermined conclusions. Statuses: PROPOSED = decided at the
named gate; ACCEPTED = standing decision.

## ADR-001 — Ledger evolution path

- **Status:** PROPOSED (decided at the increment-9 gate).
- **Context:** single untracked SQLite on one machine; six lazy `ALTER`s, no
  migration framework; a history of forked copies; three direct-`INSERT` bypasses
  of `record_bet` (ARCHITECTURE.md §6).
- **Options:**
  - **A — harden in place:** off-box replication, FX-correct reporting, close the
    bypasses, add a migration framework. No storage rewrite.
  - **B — event-sourced store:** append-only `bet_events` + `bets` projection,
    fixtures FK, typed enums. Structurally eliminates fork-divergence (merge =
    union of event logs) and gives a full audit trail.
  - **C — hosted Postgres:** rejected — adds a network dependency to a
    laptop-hosted live-money path mid-tournament.
- **Decision:** A is the tournament-track baseline (increment 4) and is required
  under every outcome. B is the default candidate for increment 9, behind a
  dual-write shadow with a projection-equality proof.
- **Rejection criteria for B:** the shadow cannot prove projection equality over
  the agreed window, or A alone eliminates observed divergence incidents — in
  either case B is dropped and A stands.

## ADR-002 — Site consolidation

- **Status:** PROPOSED (decided at the increment-9 gate, post-tournament).
- **Context:** three overlapping trees (19,334 LOC), a proven publish-staging bug
  that froze `site-analytics` feeds, silent staleness (ARCHITECTURE.md §7).
- **Options:**
  - **A — consolidate to one lilac-shaped terminal** (charter's preference).
  - **B — keep three trees; fix the publisher staging bug + per-feed freshness
    badges.** Cheap and tournament-safe.
  - **C — retire `site-analytics` only.**
- **Decision:** B's fixes ship in the tournament track regardless (they close the
  proven freeze bug). A vs C is decided post-tournament with a parity checklist
  and visual acceptance criteria — responsive screenshots, freshness visibility,
  currency correctness, feature parity — satisfied **before** any tree is retired.
- **Rejection criteria for A:** parity checklist unmet, or maintenance cost of B
  proves acceptable in practice during the tournament.

## ADR-003 — Betfair execution

- **Status:** ACCEPTED — no build.
- **Evidence:** execution stub deliberately raises `NotImplementedError`
  (`betfair.py:71`); zero placement primitives repo-wide (grep for
  placeOrders/cancelOrders/replaceOrders/PlaceInstruction/limitOrder/
  persistenceType = no hits); delayed app key only, no client cert; ~£499
  non-refundable live-key gate (charter §2.7); mini reaches Betfair endpoints
  with SSL errors [UNVERIFIED-MINI: geo-block per 07-01 snapshot]; a cheaper GBP
  alternative is already stubbed (`smarkets.py:344`).
- **Consequence:** `betfair_exchange.py` stays as a read-only CLV/closing-line
  reference; optional MacBook/VPN read-relay if wanted.
- **Revisit trigger:** a sustained GBP-exchange execution need that Smarkets
  cannot serve — nothing else reopens this.
