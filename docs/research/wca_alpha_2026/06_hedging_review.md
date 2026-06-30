# 06 — Hedging Review (Analysis Only)

> **STATUS: READ-ONLY ANALYSIS. NO ORDERS PLACED. NO SETTLEMENTS EXECUTED.**
> Nothing in this document has been actioned. No trades were placed, no `.db`
> was written, no Polymarket position was redeemed, `PM_DRY_RUN` was untouched.
> Every "settle" / "verify-first" recommendation below is **advisory**, pending
> oracle resolution or manual stat confirmation. Treat this file as a worksheet,
> not an execution log.

Generated: 2026-06-30. Source: hedge review JSON over 8 open ledger positions.

---

## TL;DR — what is actually live vs. just bookkeeping lag

The book shows **8 open positions**, but once each fixture name is checked
against the verified Round-of-32 (R32) slate, only **2 bets carry genuine live
risk**. The rest are either already decided or settle off **group-stage games
that have already been played** — they only *look* open because of settlement lag.

| Bucket | Bets | Meaning |
|---|---|---|
| **A — Settle now (decided)** | 14 | Resolved by a current R32 result; redeem. |
| **B — Resolvable from history (verify-first, NOT live)** | 88, 94, 99, 100, 101 | Name GROUP fixtures already played; grade from history, not live exposure. |
| **C — Genuinely live** | 11, 57 | True undecided outcomes still ahead. |

---

## Per-bet table

| ID | Bet | Status | Action | Rationale (short) | Hedge instrument |
|---|---|---|---|---|---|
| 11 | Golden Boot — Harry Kane @7.5, £10 free-bet, Betfair SB (offer) | **Live** | **Hold** | Tournament-long outright; cannot resolve before the Final (Jul 19). Free-bet win-only, so at-risk = **profit £65** (£10×(7.5−1)), not stake. No outright model price → no edge basis to hedge. | Conceptually: lay Kane top-scorer on an exchange. **Premature** — bracket barely into R16; **no hedge taken.** |
| 14 | Japan reach R16 — **NO** @1.6667, $60, Polymarket (model) | **Already-resolved** (pending PM oracle) | **Settle** | VERIFIED: M76 Brazil beat Japan 2-1, Japan OUT in R32 → NO **wins**. Payout $100.00 (≈$40 profit). Cleanest settle in the book. | None. Administrative redemption only — confirm PM oracle resolved market to NO. |
| 57 | Ghana NOT eliminated in R32 — **NO** @1.4708, $1, Polymarket acct2 (punt) | **Live** | **Hold** | Double-negative: position **wins if Ghana IS eliminated**. Colombia-Ghana R32 fixture **unplayed** → genuinely undecided. $1 de-minimis activation punt. | None warranted — stake too small to hedge. **No hedge.** |
| 88 | England -2.5 + England -2.0 + CS 1-0/2-0/3-0 @5.52, £5 (inc free bet), Betfair SB (offer) | **Ambiguous** | **Verify-first** | FIXTURE MISMATCH: names "England vs Ghana", but verified R32 is **England-DR Congo** (still to play). England-Ghana was a **GROUP** game → resolvable from history, **not** live. Win returns ≈£22.60 profit; loss costs £0 (free bet). | None — not a live exposure; settle from group scoreline once verified. |
| 94 | Belgium win + Lukaku to score @2.37, £10, Paddy Power (punt) | **Ambiguous** | **Verify-first** | Belgium-Iran **not** in R32 (Belgium's R32 tie is Belgium-Senegal, TBP). Belgium-Iran was a **GROUP** game → grade from history. EP on Belgium FTR + Lukaku ATS. £10→£23.75 (£13.75 profit) if both hit. **Venue uncertain** (PP vs Betfair SB). | None — verify group result + Lukaku scorer, then settle. **No hedge.** |
| 99 | Treble: Ronaldo / Kane / Diaz O1.5 SOT @5.05, £10 free-bet, Betfred (offer) | **Ambiguous** | **Verify-first** | All three legs are **GROUP** fixtures (Por-Uzb, Eng-Gha, Col-DRC) → resolvable from history. SOT props classify → 'other'; **netting backbone cannot auto-grade**. £10→£50.50 (stake-not-returned). Confirm Betfred venue. | None — manual SOT stat verification required; not scoreline-settleable. |
| 100 | England HT + England 2H + Kane O1.5 SOT + Bellingham O1.5 SOT @7.9, £10 free-bet, Betfred (offer) | **Ambiguous** | **Verify-first** | Same England-Ghana **GROUP** fixture (dated 2026-06-23), **not** the live England-DR Congo R32 tie. 4-leg acca; half-result + SOT legs need per-half + player-prop data the ledger lacks. £10→£79 (stake-not-returned). | None — manual verification (half-by-half + both players' SOT). |
| 101 | 4-fold FT-2UP: Germany + Ivory Coast + Netherlands + Japan @3.37, £50, Virgin (punt) | **Ambiguous** (resolvable) | **Verify-first** | CRITICAL: legs are **GROUP** games (Jun 25: Ecu-Ger, Cur-IvC, Tun-Ned, Jpn-Swe), **not** knockout ties. **'2UP' = early-payout offer**: leg pays once team leads by 2 at any point in its GROUP match and is **not reversed** if pegged back. Ignore the R32 results that knocked Ger/Ned/Jpn out. £50→£168.50 if all four triggered. Only real-money stake-at-risk in B. | None — settle from in-match 2-goal leads in the group games. |

---

## Aggregate / netting view

The 8 open positions split into three clean buckets once fixture names are
checked against the verified slate:

- **Bucket A — settle now:** only **bet 14** (Japan reach R16 NO, $100 gross /
  ≈$40 profit) is unambiguously decided by a current-state R32 result. Redeem on
  Polymarket.
- **Bucket B — resolvable-from-history (verify-first, NOT live):** bets **88, 94,
  99, 100, 101** all reference **group-stage** fixtures (England-Ghana,
  Belgium-Iran, Portugal-Uzbekistan, Colombia-DR Congo, and the Jun-25 4-fold).
  None appear in the live R32 slate. They feel "open" but are graded by games
  already played — **bookkeeping lag, not live risk.**
  Combined sportsbook stake-at-risk here: real-money **£50** (bet 101 punt) +
  **£10** punt (bet 94) + free-bet *profit*-at-risk on 88/99/100 (sources=offer,
  stake-not-returned: max win £22.60 + £50.50 + £79.00).
- **Bucket C — genuinely live:** bet **11** (Kane Golden Boot, tournament-long,
  £65 profit-at-risk as a free bet) and bet **57** (Ghana-out NO, $1 de-minimis).

**Correlation / netting backbone (via `exposure_corr`):**
- Player-SOT legs (99/100) classify → `'other'`; the Golden-Boot outright (11) is
  non-scoreline. So the correlated-scoreline backbone **cannot auto-grade most of
  this book** — only handicap / correct-score / result legs flow through
  `settle_on_scoreline`. The rest need manual stat verification.
- **No live double-exposure to net.** Colombia-Ghana / Colombia-DRC and England
  appear across several bets, but bets 88/99/100/101 are **group-settled** while
  bet 57 is the **live R32** Colombia-Ghana tie — different underlying events.
- Only same-underlying overlap is **Kane** (bet 11 outright vs Kane SOT legs in
  99/100), and those SOT legs are **group-game-settled**, so they do **not**
  compound the live Kane outright.

**Bounded outcome (qualitative):**
- Max-win if everything in B+C resolves favourably is modest: free-bet profits +
  the £168.50 on bet 101.
- Max-loss is bounded by **£50 (101) + £10 (94) + £1 (57)** real money, since the
  offer/free-bet legs cost **£0** on a loss.

---

## GBP/USD FX caveat (do not net across pools)

The book straddles **two currency pools that do NOT net 1:1.**

- **GBP sportsbook** positions — bets **11, 88, 94, 99, 100, 101** (Betfair SB,
  Paddy Power, Betfred, Virgin) settle in **£** into the ~£1,500 sportsbook
  bankroll.
- **USD Polymarket** positions — bets **14 and 57** settle in **$** into the
  ~$1,995 on-chain wallet.

`reports.py._platform_currency` / `exposure_corr` treat polymarket+kalshi as USD
and everything else GBP. `DEFAULT_BANKROLL=£3000` is an **FX-blended** figure
(£1,500 + $1,995 ≈ £1,500 at the assumed rate).

**Practical rule:** do **not** mentally hedge a USD Polymarket win against a GBP
sportsbook loss as if they offset — they sit in **separate wallets**, and
converting incurs FX + withdrawal friction/slippage. Any aggregate that adds the
$100 Japan redemption to GBP free-bet profits is implicitly applying a fixed
GBP/USD rate; realised cross-pool value drifts with spot FX and on-chain→fiat
conversion costs. **Keep the two pools' P&L reported separately; blend only at an
explicit, dated rate.**

---

## Cautions

1. **LOAD-BEARING FIXTURE MISMATCH.** Five "open" bets (88, 94, 99, 100, 101) name
   fixtures **NOT** in the verified R32 slate — they are **group-stage** games
   (England-Ghana, Belgium-Iran, Portugal-Uzbekistan, Colombia-DR Congo,
   Ecuador-Germany etc.). Resolvable from history, **not** live knockout
   exposures. Treat any tool/summary calling them "open knockout bets" as
   mislabelled; verify the underlying group results before settling.
2. **Bet 101 '2UP' semantics** are an **early-payout OFFER**, not a result bet.
   Each leg pays once the team leads by 2 goals **at any point** in its GROUP
   match and is **NOT** reversed if later pegged back. Settle on in-match 2-goal
   leads — **not** final scores, and definitely not the R32 knockout outcomes
   that eliminated Germany/Netherlands/Japan.
3. **Bet 14 double-check.** "Reach R16" = advancing FROM R32. Japan lost the R32
   tie (Brazil 2-1), so did not reach R16 → **NO wins**. Confirm Polymarket's
   market keys off **R32 advancement** (not group qualification): the verified
   rule grades the **advancing** team, and Japan did not advance.
4. **Bet 57 double-negative.** "No — Ghana not eliminated in R32" **PROFITS if
   Ghana IS eliminated.** Do not invert. Live via the unplayed Colombia-Ghana tie.
5. **Player-prop (SOT) and outright legs** (bets 11, 99, 100) classify as
   `'other'` in `exposure_corr` and are **NOT** settleable from a scoreline. The
   netting/correlation backbone cannot auto-grade them — **manual stat
   verification required.**
6. **Venue uncertain** in the notes for bets **94** (Paddy Power vs Betfair SB),
   **99** and **100** (Betfred). Confirm the actual book before any settlement
   record — payout/redemption channel differs.
7. **Free-bet (source=offer) bets 11, 88, 99, 100 are stake-not-returned.** A loss
   costs **£0**; the **profit** (not stake) is the at-risk/return figure — matches
   `exposure_corr._loss/_win` and `reports.py` free-bet `risk = stake*(odds-1)`.
8. **READ-ONLY review only.** No settlement executed, no `.db` written, no trades
   placed, `PM_DRY_RUN` untouched. All "settle" recommendations are **advisory**
   pending oracle/manual confirmation.
