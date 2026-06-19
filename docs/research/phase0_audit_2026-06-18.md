# World Cup Alpha — Phase 0 Live-State Audit (2026-06-18)

Read-only audit per the Master Improvement Prompt, Phase 0. Every claim cites a
local file path; where the adversarial verification pass corrected an auditor,
the corrected ground truth is used. Branch `feature/market-anchored-advancement`.

## 1. What exists and is operational

| Subsystem | State | Entry points |
|---|---|---|
| Models & Card (Elo + Dixon-Coles + Shin blend → 1X2 + scoreline) | Runs end-to-end | `src/wca/card.py`, `src/wca/models/*` |
| Odds ingestion (The Odds API → `odds_snapshots`) | Live, one persisted path | `scripts/wca_snapshotd.py`, `src/wca/data/theoddsapi.py`, `src/wca/data/snapshot.py` |
| Ledger / CLV / bankroll / settlement | Live, money bugs present | `src/wca/ledger/*`, `src/wca/closecapture.py`, `scripts/wca_settle.py`, `src/wca/bot/app.py` |
| Polymarket order lifecycle | Built, **zero live fills** | `src/wca/pm/*`, bot park/confirm, `scripts/wca_pm_*.py` |
| Bot / Site / Tracking | Live, refresh-asymmetric | `src/wca/{dashboard,sitedata,tracking,sync}.py` |

**DB:** single `data/wca.db` (~718 MB); `odds_snapshots` = 1,254,905 rows, all
`source='theoddsapi'` (2026-06-11→17). `pm_order_log` = 6 rows, all `dry_run=1`,
0 non-null `order_id` (no order has ever round-tripped).

## 2. Discrepancy map (doc vs code)

| # | Claim | Doc says | Code is |
|---|---|---|---|
| D1 | Kelly ladder wiring | SYSTEM_MAP:418 "not yet invoked by build_card" | WIRED — `wca_build_card.py:186` calls `resolve_pool_bankroll`, feeds `kelly_fraction` into `PoolConfig`. Still unwired: longshot filter + arb-Kelly exemption (`markets/kelly.py`). |
| D2 | Base bankroll rung | README/SYSTEM_MAP/card docstrings say £1,000 | `card.py:112` `LADDER_BANKROLLS=(1500,2500,5000)` |
| D3 | Bot command count | SYSTEM_MAP:550 lists 9; README:63 says 13 | 12 dispatched: `/summary /bets /clv /card /next /scores /accas /structure /pm /settle /boost /ping` |
| D4 | `odds_snapshots.decimal_odds` | "best decimal odds" | per-bookmaker price; **no `bookmaker_key` column** (book id only inside `raw` JSON) |
| D5 | Polymarket historization | snapshot.py lists `polymarket` as a source | **no writer exists**; table 100% theoddsapi; new `get_prices_history`/`winner_market_implied` have **zero callers** |
| D6 | EV-scanner coverage | README:88 "BTTS/totals/DNB/alternate" | only `h2h,totals,btts` snapshotted; DNB/alternate/correct-score never stored |
| D7 | Calibration is future P3 | TODO:135 unchecked | **STALE** — Brier+log-loss live (`tracking.py:386-404`), `calibration_report` (`reports.py:311`). Missing: this diagnostics doc (now written). |
| D8 | Advancement premise | advancement.py "no market odds for later rounds" | **FALSE** — knockout/winner odds fetchable; re-wire staged |
| D9 | Branch refs | TODO references `feature/cross-venue-events` + HEAD `88a3806` | branch gone; main HEAD moved; `betfair.py`/`matchevents.py` absent |
| D10 | Test count | README "973"/"970+" | 993 |
| D11 | Betfair P0 | "build betfair.py" | read-side lay prices already flow via Odds API (`h2h_lay`); execution client still absent |

## 3. Dangerous-path test coverage

Covered: PM `place_order` caps/gates (`test_pm_trader`, `test_pm_gate`);
`store.settle_bet` back/free/lay/void (`test_settle_freebet_lay`); CLV capture
(`test_closecapture`, 33); arb settlement-key guard (`test_arb`, 11); currency
separation in the site feed (`test_currency`, `test_sitedata`).

**Not covered / broken:**
- `scripts/wca_settle.py` free-bet & lay settlement — **untested and buggy** (see Risk 3).
- arb **liquidity/fillable size** — not enforced anywhere (`arb.py` has no depth check).
- `reports.summary` cross-currency input — tests don't assert single-currency → GBP+USD silently summed.
- PM `run()`-level admin gate (non-admin `Y PM-n`) — units exist, no e2e test.
- `_venue_of` regression — **fixed in this Phase-0 pass** (`/bets` no longer crashes).

## 4. TOP RISKS (ranked; file:line + one-line fix)

1. **Ungated raw live order POST** — `scripts/wca_pm_try.py:87-88` POSTs a real order via `requests.post('/order')`, bypassing every `place_order` cap/allowlist/gate, behind a bare `--post`. *Fix:* delete the raw path; route through `place_order`; require `PM_*_LIVE=1 + --yes`.
2. **Ungated on-chain fund movement** — `scripts/wca_pm_fund.py:118`, `scripts/wca_pm_transfer.py:99` broadcast `eth_sendRawTransaction` with no arming flag (`--all` sends the whole balance). *Fix:* gate behind `PM_*_LIVE=1 + --yes`; canary default.
3. **Free-bet/lay loss mis-booked as real cash** — `scripts/wca_settle.py:95` books `-stake` unconditionally; SELECT at :62 omits `source` (repro: −10.0 where store/bot book 0.0). *Fix:* mirror `store.py:305-320` `is_free`/`is_lay` branch; add CLI test.
4. **Funder guard defeated** — `pm/trader.py:716-722` marks `_account_class_proven=True` for any forced funder with no balance check; default fallback is the $0 `0x4023…E191` wallet. *Fix:* require `POLYMARKET_SIG_TYPE=3` + non-zero balance before proving.
5. **Stale-price / no-expiry on `Y PM-n`** — `place_order` re-fetches tick only; `_parked_load` (`app.py:1302`) re-serves week-old `parked`/`failed` rows with frozen prices. *Fix:* TTL + re-quote/reject-on-move in the confirm path.
6. **Currency-blind headline P&L** — `reports.summary` sums GBP+USD into one `total_pl`/`roi` (`reports.py:430-433`), printed verbatim (`app.py:298`). *Fix:* never sum across `VENUE_CURRENCY` classes; surface per-currency. (`_venue_of` restored this pass.)
7. **Phantom-arb on thin liquidity** — `arb.py` has no fillable-size check. *Fix:* require min fillable size at the quoted price.
8. **Promo-conditional settlement unrepresentable** — `store.py:55-76` has no `settlement_clause`/`promo_kind`/per-leg `settlement_key`/`cashed`. A 2Up/Super-Sub winner that is a book loss is booked as a loss. *Fix:* schema migration + map clauses in `docs/recon/settlement_rules.md`.

## 5. SYSTEM_MAP.md deltas to apply

- §418/128/656/708: Kelly ladder is **WIRED** (`wca_build_card.py:186`); only the longshot filter + arb exemption remain unwired.
- §494: base rung £1,000 → **£1,500**.
- §550-554: replace the 9-command list with the **12** dispatched; flag `/bets` was broken (now fixed).
- Ledger section: note `reports.summary` sums across currencies; per-venue view depends on `_venue_of`.
- Models section: card uses the **undiluted 100-pt host bonus**; `venues.host_advantage_points` not wired.
- Calibration: Brier/log-loss/bins are **LIVE**; only the aggregate doc was outstanding.
- Data flow: `odds_snapshots` has no `bookmaker_key`/`settlement_key` column; PM not historized; `arb.py` enforces settlement-key but not liquidity.

## 6. README.md number fixes

base rung £1000→£1500; test count 973/"970+"→993; bot command count 13→12.

## 7. Phase-0 acceptance

Audit changed no model/strategy behavior. The only code change this pass is the
restoration of `_venue_of` (a severe operator-facing crash the audit surfaced,
explicitly permitted by the Phase-0 rule "unless a severe bug is found") — 9/9
`test_bot_bets` pass. All other fixes are recorded as P0 items in `TODO.md`.
