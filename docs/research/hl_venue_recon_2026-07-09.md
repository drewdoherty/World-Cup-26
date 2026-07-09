# Hyperliquid HIP-4 outcome markets — venue recon + cross-venue (vs Polymarket) analysis

**Date:** 2026-07-09 (capture window ~18:13–18:15 UTC, pre-kickoff of the France–Morocco QF)
**Status:** WATCHER-ONLY. No execution scaffold was built and none is approved — go/no-go criteria in §9.
**Code shipped with this doc:** `src/wca/hl/client.py` (read-only info client), `src/wca/hl/xvenue.py` +
`scripts/wca_hl_xvenue.py` (SHADOW-only cross-venue monitor feed `site/hl_xvenue.json`),
offline tests `tests/test_hl_client.py` / `tests/test_hl_xvenue.py`.

**Evidence.** Every figure below comes from a raw API response or docs page captured during the recon
session. Dump files are cited by name (`hlrecon/…` = the session capture directory; session-local
scratchpad, not committed — the load-bearing raw books and the trimmed `outcomeMeta` are preserved
verbatim in `tests/fixtures/hl_xvenue/`, and the committed `site/hl_xvenue.json` v1 is a deterministic
offline replay of the same capture). All load-bearing numbers were re-derived by an independent
verification pass (`hlrecon/verify_independent.py`, reading only raw dumps); where the first-pass
analysis was corrected, the CORRECTED value is stated here and flagged. No fabricated numbers; n is
stated for every aggregate.

---

## 1. Verdict (read this first)

- **Live arb today: technically yes — one trade, not worth firing.** Buy PM Norway win-WC Yes @ 6.0¢ +
  buy HL Norway-champion No @ 93.6¢ = **+0.231%/share after all modelled fees** ($331 profit on the full
  $249k executable depth; **~$4.63 at a $2k deployment, ~$9 at the full combined bankroll**). Legs were
  captured **66.7 s apart** (corrected; first pass said 64 s) — simultaneous fillability is unproven, n=1.
  The only other positive pair nets **$0.0127** total and carries a toxic settlement tail. **No arb worth
  firing today.**
- **Structural picture: real cross-venue disagreement, unproven opportunity.** 9/16 settlement-matched
  pairs were raw-crossed pre-fee in one snapshot; only 2 survived the PM taker fee, both at book extremes.
  Existence is established; frequency/persistence/fillability are not (n=1 snapshot, 36–104 s cross-venue
  skew).
- **Decision: cheap automated monitoring (this PR), no execution build.** The whole edge rests on HL fees
  being "currently zero … for initial testing" and on an **UNVERIFIED HL settlement fee** (§6) — plus HL
  is a new venue with zero price-capture/CLV/settlement automation, so the CLAUDE.md live-money gate is
  not cleared regardless of arb math.

---

## 2. API surface (all verified against live responses)

**Addressing** (`hlrecon/docs_asset_ids.md`, exercised live): `encoding = 10*outcome_id + side`
(side 0/1 per `sideSpecs` order in `outcomeMeta`); L2/trades/candles coin = `"#<enc>"` (Argentina-champion
Yes = `#1730`); token name `+<enc>`; order asset id `100_000_000 + enc`. Implemented in `wca.hl.client`.

**Working `POST /info` types** (dumps `hlrecon/probe_*.json`):

| type | returns | notes |
|---|---|---|
| `outcomeMeta` | all outcome specs + `questions` (`fallbackOutcome`/`namedOutcomes`/`settledNamedOutcomes`) | `hl_outcome_meta_raw.json`; 29 outcome markets, 12 World Cup |
| `settledOutcome` | `settleFraction` + human `details`; `null` if unsettled | early-No on elimination observed live: outcome 172 Algeria, settleFraction "0.0", "eliminated…" (`probe_settledOutcome_172.json`) |
| `l2Book` on `#coin` | standard L2, **max 20 levels/side over REST** | optional `nSigFigs`/`mantissa` aggregation (`docs_info_endpoint.md` line 424) |
| `recentTrades`, `candleSnapshot`, `allMids`, `userFills` | standard | side-0/side-1 candle volumes are the SAME mirrored tape — never sum sides (verified all 12 WC markets) |

**NOT available — n=15 negative probes, all HTTP 422** (`hlrecon/probe_*.json`): `outcomeCtxs`,
`outcomeMetaAndCtxs`, `outcomeAssetCtxs`, `outcomeStates`, `outcomeL2Book`, `outcomeBook`,
`activeAssetCtx` on `#` coins, `tokenDetails` on `+` tokens, `outcomeOpenInterest`, `outcomeSupply`,
`questionMeta`, … ⇒ **no public open-interest or day-volume ctx endpoint exists**; OI is not retrievable,
24h volume must be derived from candles. Outcome tokens are absent from `spotMeta` (0 `+` tokens —
`spotMeta_raw.json`).

**WebSocket** (`docs_ws_subscriptions.md` lines 44–50, 93–95, 431–456): outcome channel
`outcomeMetaUpdates` (created/settled/question updates); generic `l2Book`/`trades` channels accept `#`
coins. This is the path a real monitor should use (§9) — REST polling reproduces the multi-second skew
that plagues this snapshot.

**Order placement — documented only, NOTHING placed** (`docs_exchange_endpoint.md`): standard signed
`order` action on asset id `100_000_000+enc`; min order value $10; quote USDC. Outcome-specific
`userOutcome` actions: `splitOutcome`/`mergeOutcome`/`mergeQuestion`/`negateOutcome` (doc lines
1147–1260). **Books are merged dual books** (buy Yes @ p ≡ sell No @ 1−p; `docs_hip4_outcome_markets.md`)
— verified side1 = 1−side0 exactly for 10/12 markets, the other 2 off by exactly one 1e-5 tick consistent
with ~0.5 s between snapshot fetches (`depth_summary_computed.json` `mirror_consistent` flags). Hence
**no intra-HL arb can exist** (mint/merge at $1, fee zero).

**Tick/lot**: min observed price gap 1e-5 across all 24 books; all 480 visible level sizes integers ⇒
szDecimals=0, 1 share = $1 max payout. Settlement auto-converts Yes → settleFraction USDC, No →
1−settleFraction.

**SDK**: official Hyperliquid Python SDK (master) has **zero** outcome support (`hlrecon/sdk_info.py`,
`sdk_exchange.py`, grep n=0) — addressing must be hand-constructed (done in `wca.hl.client`).

## 3. Breadth: 12 HL markets vs the PM universe

HL World Cup universe (all of it): **8 champion Yes/No** (question 32 — ids 173 Argentina, 176 Belgium,
188 England, 189 France, 199 Morocco, 202 Norway, 212 Spain, 214 Switzerland; 40 of 48 team outcomes
already settled, `fallbackOutcome` 171) + **4 QF match markets** (761 France–Morocco Jul 9,
779 Spain–Belgium Jul 10, 778 Norway–England Jul 11, 788 Argentina–Switzerland Jul 11; two sides, **no
Draw**). That's it — no reach-SF/F, no group markets, no totals, no props.

Polymarket's captured universe for the same teams (28 markets, `hlrecon/pm_snapshot_results.json`):
per-team win-WC (negRisk event 30615), per-team reach-SF (event 551781), per-match 3-way 1X2, plus the
broader advancement ladders we already trade. **⇒ exactly 16 settlement-matched cross-venue pairs**
(8 champion + 8 QF team-sides); everything else on either venue has no counterpart. **PM per-match 1X2
NEVER pairs with HL QF markets** — PM 1X2 is 3-way and settles on the first 90 minutes; HL QF is 2-way,
ET+pens-inclusive, with a 0.5-void tail (HL France 77.48¢ vs PM France-90min mid 60.4¢ is settlement
basis, not edge — `l2book_761_side0.json` vs `book_match_fra_mar_France_Yes.json`). This exclusion is
structural in `wca.hl.xvenue.pair_configs()`.

## 4. Depth + volume (12/12 markets, single snapshot 18:14–18:15 UTC)

Side-0 space. "mv±1c" = $ to consume all visible levels within 1¢ of best ask/bid; 24h volume from 1h
candles (n=20–25 candles per market, stated per market in `volume_24h_computed.json`); books
`hlrecon/l2book_<id>_side{0,1}.json`, computed `depth_summary_computed.json`.

| id | market | bid/ask | spread | mv+1c | mv−1c | 24h shares | 24h $ (lo–hi) | 24h trades |
|---|---|---|---|---|---|---|---|---|
| 173 | Champ Argentina | 0.19377/0.19495 | 0.00118 | $61.7k | $48.1k | 614,935 | $119.2k–123.6k | 13,502 |
| 176 | Champ Belgium | 0.02101/0.02592 | 0.00491 | $9.1k | $1.2k | 30,751 | $759–765 | 25 |
| 188 | Champ England | 0.15702/0.16121 | 0.00419 | $33.3k | $53.3k | 25,716 | $4.06k–4.09k | 147 |
| 189 | Champ France | 0.32071/0.32082 | 0.00011 | $81.0k | $41.6k | 560,867 | $179.5k–182.1k | 11,689 |
| 199 | Champ Morocco | 0.031/0.035 | 0.004 | $1.1k | $12.6k | 195,358 | $6.4k–7.4k | 171 |
| 202 | Champ Norway | 0.064/0.0646 | 0.0006 | $3.7k | $28.0k | 405,572 | $24.7k–26.0k | 125 |
| 212 | Champ Spain | 0.19145/0.19165 | 0.0002 | $25.2k | $65.7k | 493,862 | $92.2k–93.5k | 11,079 |
| 214 | Champ Switzerland | 0.01501/0.02101 | 0.006 | $10.0k | $1.5k | 2,834 | $49–60 | 10 |
| 761 | QF France–Morocco | 0.77478/0.77485 | **0.00007** | ≥$168.9k† | ≥$18.3k† | 509,659 | $393.3k–395.8k | 2,583 |
| 778 | QF Norway–England | 0.35055/0.35211 | 0.00156 | $13.0k | $15.7k | 25,077 | $8.76k–8.84k | 84 |
| 779 | QF Spain–Belgium | 0.736/0.738 | 0.002 | $117.4k | $112.1k | 40,328 | $29.8k–29.9k | 165 |
| 788 | QF Argentina–Switz | 0.73179/0.7375 | 0.00571 | $24.8k | $30.8k | 17,674 | $12.9k–13.1k | 87 |

† 761 (match day) had all 20 visible levels inside the 1¢ band ⇒ lower bounds (`depth_truncation_flags.json`);
all other markets' band figures are complete within the snapshot. **Open interest: not retrievable (§2).**
Headline: the liquid HL books (173/189/212/761/779) carry real five-figure near-touch depth and six-figure
daily dollar volume; the tails (176/214) are near-dead.

## 5. Microstructure comparison (HL vs PM)

| dimension | Hyperliquid HIP-4 | Polymarket | source |
|---|---|---|---|
| book structure | merged dual book, side1≡1−side0; on-chain CLOB | separate Yes/No CLOB per market; champion event is negRisk-linked | `docs_hip4_outcome_markets.md`; `gamma_event_*.json` |
| matching latency | ~0.2 s median E2E | sports markets carry an in-play matching delay | `hl_docs_hypercore_overview.md`; `pm_docs_concepts_order-lifecycle.md` |
| trading fees | **0 today** ("currently zero … initial testing"); designed model: fee only on close/settle, no maker rebates | taker 0.03·p·(1−p)/share (sports), makers 0; `feesEnabled: true` on all paired markets | `docs_hip4_outcome_markets.md` line 25, `docs_fees.md`; `pm_docs_trading_fees.md` |
| settlement fee | **RESERVED in docs, UNVERIFIED for these markets** (§6) | none beyond the above | `outcome_docs_fees.md` |
| tick / lot | 1e-5 observed; integer shares; $10 min order | 0.001 tick on captured books; min_order_size 5 | book dumps; `docs_tick_lot.md` |
| REST depth | 20 levels/side cap | full book on `/book` | `docs_info_endpoint.md` |
| OI / volume telemetry | none (no ctx endpoint); candles only | gamma volume/liquidity fields | §2 probes |
| oracle / resolution | validator-quorum settlement per market description text; FIFA primary source | UMA | `docs_hip4_outcome_markets.md`; `pm_docs_concepts_resolution.md` |
| liquidity incentives | Outcome/Monarch WC program: rewards need the `Outcomexyz` builder code; "up to 3x" two-sided + 5x live multiplier, 600 USD/QF match | none equivalent captured | `outcome_docs_world-cup-program.md` |
| address rate limits | 1 action per 1 USDC lifetime volume + 10k buffer (fresh wallet can't quote/cancel aggressively); l2Book IP weight 2 (cheap to poll) | standard CLOB limits | `hl_docs_for-developers_api_rate-limits-and-user-limits.md`; `pm_docs_api-reference_rate-limits.md` |
| capital rails | Arbitrum USDC bridge: deposit min 5 USDC; withdraw flat 1 USDC, 2/3-validator signing + dispute period of undocumented duration | pUSD↔Arbitrum "instant and free", one Uniswap v3 pool, <10 bp enforced, split >$50k; $2 L2 min | `hl_docs_hypercore_bridge.md`, `hl_docs_faq_deposit_arbitrum.md`; `pm_docs_trading_bridge_*.md` |

Capital conclusion: end-to-end round-trip time is undocumented — treat as minutes-to-tens-of-minutes,
never intra-arb. At $2k the ~$1–2 bridge cost is ~5–10 bp ≈ a quarter-to-half of the best observed edge.
**Capital must be pre-positioned on both venues; per-arb bridging is uneconomic at these margins.**

## 6. Fee stack — what is verified and what is NOT

- **PM taker fee** = `0.03·p·(1−p)` per share, taker-only, makers 0 (`pm_docs_trading_fees.md`; same
  constant as `wca.advancement.PM_TAKER_FEE_COEF`, parity asserted in tests). At 6¢: 0.169¢/share; in the
  20–80¢ band: **0.48–0.75¢** (corrected; first pass said 0.45).
- **HL trading fee = 0 today** — doc line "Fees are currently zero for outcome markets for initial
  testing" (`docs_hip4_outcome_markets.md`). Empirics **(CORRECTED from the first-pass "40/40 fee=0"
  claim)**: `userfills_0xf1e8f807.json` holds 2,000 fills, **502** on outcome markets; **497 have
  fee=0.0, 5 carry non-zero fees** (0.00081–0.00557 USDC, each exactly **1.50 bp** of notional,
  fee==builderFee, crossed sells) — **builder-code fees, not protocol fees**; direct API orders without a
  builder code pay 0. Tension worth knowing: the WC liquidity-program rewards *require* the `Outcomexyz`
  builder code whose docs claim 0 fee, yet 1.5 bp builder fees appear in the wild. No announced end date
  for "currently zero" — re-check `userFills` every session.
- **HL settlement fee — UNVERIFIED, BLOCKING (new material gap from the verification pass).**
  `outcome_docs_fees.md`: "When a market resolves and your position settles, a settlement fee is deducted
  from the payout. The exact fee is shown in the market spec." The captured `outcomeMeta` has **no fee
  field**; nothing in any dump proves this fee is zero for these markets, and the fee=0 *fill* evidence
  does not test it. Nearly every branch of both arbs collects its $1 via HL settlement, so even a few bp
  here consumes much of a +0.23% edge. Carried as an explicit `hl_settlement_fee_verified: false` caveat
  in the feed; verification (observe one settled payout, or the outcome.xyz market spec) is a hard
  pre-money gate item (§9).

## 7. Settlement bases + divergence tails (load-bearing; verbatim market text)

**Champion pairs** (HL question 32 ↔ PM `world-cup-winner`): both = FIFA-declared champion, ET+pens
valid, **early No on mathematical elimination** (HL observed live on Algeria; PM description "eliminated →
immediate No" — `gamma_event_win_wc.json`). Residual tails **(added by the verification pass)**:
(1) HL all-No deadline 2026-10-14 23:59 UTC vs PM "October 13, 2026, 11:59 PM" (no TZ) — a champion
declared inside that ~19–24 h gap pays $0 to dir2 (needs a ~3-month postponement landing in a <24 h
window; caveat, not gate); (2) **co-champions: HL all-No is explicit, PM is silent (UMA)** → dir1
(buy HL Yes + buy PM No) can collect $0 — **dir1 champion pairs are GATED** in the monitor.

**QF pairs** (HL match winner ↔ PM reach-SF Yes): HL text verbatim "Game results after regular time,
extra time, and penalties, if applicable, are all valid for resolution purposes" — a KO-match winner IS
advancement, so the bases match. Asymmetric tail: cancellation/no result by the deadlines → HL pays
0.5/share both sides while PM reach-SF resolves No for both teams ⇒ **dir1 collects 1.5 (windfall,
safe); dir2 collects 0.5 on ~1.0 cost (toxic) — dir2 QF pairs are GATED**. A winner declared inside the
~20 h HL(Jul 26 23:59 UTC)/PM(Jul 25 23:59 ET) deadline gap collects $0 for dir2 (worse than 0.5).

Gating is encoded in `wca.hl.xvenue.TAILS` and tested both ways
(`test_gating_depends_on_pair_kind_both_ways`): a positive fee-adjusted edge in a gated direction can
never exceed `XV_MISMATCHED_SETTLEMENT`.

## 8. Cross-venue snapshot results (n=1 paired snapshot; per-pair skew 36.1–103.6 s — corrected range)

Full 16-pair table with raw books cited per row lives in the committed feed (`site/hl_xvenue.json`,
offline replay of the capture) and in `arb_math_computed.json` (independently re-derived: all 64
price/edge figures reproduced to the digit; every PM book's `asset_id` matched gamma `clobTokenIds`,
n=32). Summary:

- **9/16 pairs raw-crossed pre-fee; 2/16 survived the PM taker fee.** The PM fee is the binding cost
  (HL contributes zero), so survivors concentrate at extreme prices — exactly where the one real hit is.
- **ARB 1 — champion:Norway dir2** (buy PM Yes .060 × 87,086.63 sh, `book_win_wc_Norway_Yes.json`; buy
  HL No .936 × 249,600 sh, `l2book_202_side1.json` — that one level mirrors the side-0 bid and smells
  like the paid liquidity program): cost/share 0.997692 → **+$0.002308/share (+0.231%)**; walking both
  ladders: **249,600 sh / $249,268.57 / $331.43 (0.133% avg)**, best band $201. At WCA scale: **$2k ≈
  $4.63, full ~$4k bankroll ≈ $9.25**. Capital locked until Norway's elimination (~65% at the Jul 11 QF)
  or the Jul 19 final. Legs **66.7 s** apart — existence proven, simultaneity not. All these numbers are
  pinned as regression tests against the preserved raw books.
- **ARB 2 — qf:Belgium dir2**: +0.00029/share × 44.29 sh = **$0.0127 total**, AND carries the toxic
  cancellation tail → gated `XV_MISMATCHED_SETTLEMENT`. Dead on arrival. All 8 QF cross-team variants
  (buy HL A + buy PM B-Yes; both-No tail): 7 negative, the 1 positive IS this same trade
  (`arb_math_computed.json` `qf_crossteam_variants`).
- **Internal consistency**: HL QF bid/ask sums bracket 1 by exactly the spread (merged dual book —
  structural, no intra-HL arb): 761 = 0.99993/1.00007 … 788 = 0.99429/1.00571. PM reach-SF both-Yes cost
  incl. fees 1.0115–1.0236 (no intra-PM arb); ARG+SUI ask-sum exactly 1.000, mid-sum 0.9945 — loosest
  internal book, consistent with the both-No tail being worth a few bp.
- **Coherence of the disagreement (CORRECTED)**: HL priced Norway above PM in both its markets, but the
  premium is **+8.1% (champion mids .06430 vs .05950)** and only **+1.8% (QF mids .35133 vs .34500)** —
  directionally consistent, not the "~8% in both" first-pass claim.
- Both live hits sat at HL book extremes fed by what looks like the incentive program (5× live
  multiplier, 600 USD/QF rewards) — plausibly stale/insensitive quotes (an arb's favourite counterparty),
  and the program ends with the tournament.

## 9. Decision, go/no-go criteria, follow-ups

**Decision (this PR):** watcher-only. `scripts/wca_hl_xvenue.py` snapshots the 16 pairs, computes
fee-adjusted gaps + executable size both directions, applies the settlement gates, and writes
`site/hl_xvenue.json` with statuses `XV_WATCH / XV_ARB_CANDIDATE / XV_MISMATCHED_SETTLEMENT / XV_NO_DATA`
— never PLACE/FIRE, no ledger/Telegram/execution imports. Runs from the MacBook over NordVPN (mini is
PM-blind; HL rides the same route); `WCA_HL_XVENUE=1` gates the publish-loop hook, default OFF. The feed
carries `n_snapshots` from a local history file so no one mistakes a single snapshot for a distribution.

**Go/no-go for ever building an execution scaffold — ALL must pass:**

1. **Monitoring evidence** (the minimum spec): paired HL-WS + PM-WS capture with <1 s cross-venue skew
   (REST polling reproduces today's 66.7 s ambiguity), 10–30 s cadence plus event-driven on top-of-book
   change, across **≥3 full match days including in-play windows** (PM's in-play matching delay vs HL's
   ~0.2 s is where dislocations should peak). Decision metric: the distribution of
   **(fee-adjusted crossable edge ≥ 0.3%, size ≥ $500, persistence ≥ 5 s)** events per match day. Fewer
   than a handful per day over a week of logging ⇒ build is dead this cycle (final Jul 19 — ~10 days of
   runway) and the finding rolls to post-tournament research.
2. **HL settlement fee verified = 0** (or exactly quantified and the edge math re-run with it): observe
   one settled payout via `userFills`/balance delta, or obtain the outcome.xyz market-spec fee field. §6.
3. **HL trading fee still 0** at build time (`userFills` re-check; "currently zero" has no end date).
4. **CLAUDE.md new-venue gate**: price capture + CLV stamping + settlement automation for HL markets
   — a market without them does not get real money, arb or not.
5. **Capital pre-positioned** on both venues (per-arb bridging uneconomic, §5) + the HL address
   rate-limit warmed (1 action/1 USDC lifetime volume means a fresh wallet cannot manage orders actively).
6. **Human sign-off** on sizing within the standing caps (¼-Kelly combined bankroll; PM execution caps
   unchanged — an HL leg does not bypass `pm/trader.py` limits on the PM side).

**Follow-ups (not this PR):**

- **/pm + Action Desk surfacing of `hl_xvenue.json`** — deliberately deferred: open PRs #192/#193/#194
  own `src/wca/bot/app.py` and the event-market fire paths; wire the feed into the bot only after #193
  lands (new-files-only collision rule this cycle).
- WS-based paired capture (criterion 1) if the daily REST snapshots keep showing crossed quotes.
- Champion-side liquidity tracking: whether the incentive-program quotes (the .936 wall) persist
  off-match-days; they are the entire executable size of ARB 1.
- Settlement-fee probe (criterion 2) — cheapest path: watch outcome 761's settlement tonight via
  `settledOutcome` + a position-holder's `userFills`.
- If a GBP/other venue layer ever generalises this: `wca.hl.xvenue`'s pair-config + tail-gating shape is
  the template (settlement basis on BOTH legs, per-direction tails, fail-closed statuses).

**Corrections ledger (first-pass analysis → verified):** userfills evidence 40/40 fee=0 → **502 outcome
fills: 497 zero, 5 at 1.5 bp builder fee**; Norway premium "~8% both markets" → **+8.1% champion / +1.8%
QF**; leg skew 64 s → **66.7 s** (pair range 40–95 s → **36.1–103.6 s**); PM fee band 0.45–0.75¢ →
**0.48–0.75¢**; "returns exactly $1 in every tail" → **deadline-gap slivers pay $0 to dir2** (remote);
champion dir1 **co-champion tail** added (now gated); **HL settlement fee flagged UNVERIFIED** (now a
blocking gate item). Everything else in the first pass survived independent re-derivation.
