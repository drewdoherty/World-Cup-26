# World Cup Alpha current state

**Snapshot:** 2026-07-17, Asia/Bahrain.

This file records dated facts verified from the current repository, generated
feeds, and focused tests. It does not contain live-position or trade advice.
Re-quote venues and query the canonical mini ledger before any money action.

## Source boundary

- `origin/main`: `b903f6e1`, last observed scheduled publish from
  2026-07-16 14:10 UTC.
- Current branch: `codex/integrate-shadow-continuation` at `06fe2306`, adding
  the multi-venue shadow book above `origin/main`.
- Concurrent worktree changes include full-forest publishing improvements,
  fresh generated feeds, generic HL/PM dominance research, draft MacBook
  research/dashboard launchd jobs, and a fail-closed 12-hour parked-proposal
  expiry. They are not all committed to the branch or deployed.
- Focused verification: event-market, shadow-book, and dominance tests pass
  (`64 passed` on 2026-07-17).

## Remaining tournament fixtures

The processed results are complete through both semi-finals:

- France 0-2 Spain on 2026-07-14;
- England 1-2 Argentina on 2026-07-15.

Those results fix the final two fixtures:

| Stage | Fixture | Kickoff UTC | Bahrain time |
|---|---|---|---|
| Third-place play-off | France vs England | 2026-07-18 21:00 | 2026-07-19 00:00 |
| Final | Spain vs Argentina | 2026-07-19 19:00 | 2026-07-19 22:00 |

The primary card, scores, event forest, and tracking feeds all list these two
fixtures. The advancement edge-desk feed can still call the final pairing
projected; that provenance mismatch is open in `TODO.md`.

## Complete event forest

Verified feed: `site/forest_data.json`, generated 2026-07-17 12:49:45 UTC by
`scripts/wca_event_markets.py` from live Gamma/CLOB data.

| Fixture | Priced market rows | Families |
|---|---:|---|
| France vs England | 102 | 1X2, BTTS, corners, exact score, extra time, first to score, half markets, halftime result, other, penalty shootout, scorer props, second-half result, spreads, team totals, total goals |
| Spain vs Argentina | 103 | the same core set plus team-to-advance; no separate `other` row in this snapshot |

Total: 205 priced rows. All rows carry a settlement basis. The forest includes
model-backed and market-only rows; it is not equivalent to the cash-rec feed.
The same snapshot's governed `site/event_market_recs.json` contains 17 rows
across the two fixtures.

Operational caveats:

- the full builder needs a working PM route;
- its zero-price guard preserves the last priced forest on PM blindness;
- the current primary publisher preserves the full feed and only rebuilds it
  when `WCA_EVENT_MARKETS=1`;
- scheduled GitHub workflows still invoke the legacy reduced builder and must
  be reconciled to prevent clobbering.

## Shadow book

Current branch components:

- `src/wca/shadowbook.py`;
- `scripts/wca_shadow_book.py`;
- `scripts/wca_shadow_book_cycle.sh`;
- `data/shadow_book.db` (local, isolated paper database);
- `site/shadow_book.json` and `site/shadow-book.html`;
- `docs/research/shadow_book_methodology.md`.

Latest verified local report, generated 2026-07-17 13:04:10 UTC:

| Metric | Value |
|---|---:|
| Latest run ID | 3 |
| Observations | 595 |
| Decisions | 595 |
| Simulated entries | 187 |
| Abstentions | 408 |
| Simulated positions | 187 |
| Open simulated stake | USD 945.12 |
| Settled positions | 0 |
| Simulated settled P&L | USD 0.00 |

The USD 945.12 figure is paper exposure, not real capital. No outcome has yet
been used to score or settle the book, so it provides coverage evidence only,
not calibration or strategy evidence.

The latest cycle consumed the full event forest and a cross-venue feed stamped
2026-07-17 13:03:59 UTC. It records both entries and abstentions. Market-prior
USD 1 exploration remains separate from model-backed simulated positions.

The repo defines a 10-minute `shadowbook` launchd interval and a guarded cycle
that strips the PM key and forces dry-run. Runtime activation on the Mac mini
has not been verified; a merged definition still requires a human installer
run.

## Hyperliquid/Polymarket research

### Matched-settlement watcher

`src/wca/hl/xvenue.py` and `scripts/wca_hl_xvenue.py` compare explicitly paired
HL/PM tournament markets. This is monitor-only. It gates stale quotes,
settlement divergences, sequential-leg skew, depth, and fees. Historical
single-snapshot results are existence evidence, not a frequency or fillability
estimate.

### Generic dominance bounds

Concurrent untracked work in `src/wca/hl/dominance.py` generalizes the research
to nested contracts:

```text
team advances = team wins in 90 minutes
                OR (90-minute draw AND team wins after the draw)
```

It evaluates two directly purchasable coverage baskets:

- buy HL advance YES plus PM team-win NO;
- buy HL advance NO plus PM team-win YES plus PM draw YES.

For ordinary played-match states each basket pays at least USD 1 before fees,
with a USD 2 state on one drawn-tie branch. The implementation includes PM
taker fees, supplied HL trading fees, and an optional conservative HL
settlement fee.

The HL settlement fee remains unverified. Therefore a positive zero-fee margin
is only `CANDIDATE_FEE_UNVERIFIED`. Cancellation, no-result, deadline-gap,
co-champion, administrative, depth, and timestamp branches remain mandatory
checks. There is no Hyperliquid execution path.

## Production and serving posture

- Mac mini is production and owns the canonical `data/wca.db`.
- MacBook is development and the PM/HL public-data gateway.
- GitHub `main` is the deploy and tracked-feed bus.
- Mini autopull is defined at five minutes; primary publish at 30 minutes.
- Sites are localhost-only: primary port 8000 and analytics port 8001.
- Betfair execution remains no-build; read-only reference only.
- Polymarket execution remains proposal -> park -> human confirmation -> guarded
  order path.
- Hyperliquid remains research-only.

## Facts not verified in this reconciliation

These require a fresh read-only production audit or external authoritative
source and must not be assumed:

- whether the shadow-book launchd job has been installed and is cycling on the
  Mac mini;
- whether the draft MacBook `com.wca.research` and
  `com.wca.analytics-live` jobs have been reviewed, committed, or installed;
- current mini HEAD, dirty state, loaded service set, and daemon liveness;
- current canonical ledger balances, open positions, parked proposals, order
  status, CLV, and realised P&L;
- current MacBook `.env` key inventory and dry-run value (not inspected to avoid
  exposing secrets);
- whether mini off-box ledger backup credentials are configured and restorable;
- current PM/HL reachability from the mini;
- authoritative Hyperliquid settlement fee for these outcome contracts;
- simultaneous fillability and persistence of any cross-venue margin;
- completion of human installer steps for recently added service definitions.

Older position sizes, trim/sell instructions, bankroll snapshots, and July 8
advisories were intentionally not migrated. They are stale by definition.
