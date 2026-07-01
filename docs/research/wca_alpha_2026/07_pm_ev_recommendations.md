# 07 — Polymarket EV Recommendations (WCA Alpha 2026)

> ## ⛔ SUPERSEDED — DO NOT PLACE THESE BETS (correction added 2026-06-30, post-review)
>
> An independent re-pull of the live Polymarket book (lead-review pass, same day) shows the "edges" below are **systematic model miscalibration, not tradable alpha.** The prices are REAL and the market is EFFICIENT (World Cup Winner book sums to **0.985**; Reach-Final to **2.03** — both correct). The apparent edges arise because the conditional model is **pure, un-anchored Elo + Dixon-Coles** that disagrees with the liquid market by implausible margins — most starkly it **overrates Brazil (model 18.8% vs market 7.1% to win) and underrates France (12.9% vs 28%).**
>
> Per the repo's own backtest (`docs/research/backtests/blend_weights.md`), the model does **not** beat market-only with confidence (n=64, CI straddles zero). Given that, a +50–166% "edge" on the most-traded WC markets is model error by Bayesian reasoning, not a market mistake. The two adversarial verifiers passed everything because they checked *internal* consistency (live-price re-fetch + settlement identity), not the meta-question "is a dozen simultaneous 50–166% edges against an efficient market plausible?" — it is not.
>
> **Verdict: place NONE of the GREEN list. These are not +EV.** The tables below are retained only as a record of the model-vs-market disagreement and as motivation for the corrective work: build a **market-anchored** knockout/advancement pricer (blend the de-vigged PM book into the model, as the repo already does for pre-match 1X2) before any knockout staking — at which point real edges will be small and selective. The same correction applies to the bracket in `04_conditional_bracket.md`: the market is the better forecaster, so a market-anchored MAP completion would likely crown **France** (market favorite, 28%), not the pure-model champion Spain.
>
> The original (now-superseded) analysis follows unchanged for the audit trail.

---

> **ANALYSIS ONLY — NO ORDERS PLACED OR PENDING.** Claude did **NOT** and **WILL NOT** place any orders, trades, approvals, or DB writes. This document is a model-vs-market reading only. **You place each bet yourself.** Every price below is a single live snapshot from the public Polymarket Gamma API fetched **2026-06-30T06:09:40Z**; knockout/advancement markets move continuously and these edges can shrink, vanish, or invert within hours. **Re-check the live price (and that the market is still OPEN) at the exact point of placement** — the numbers here are stale the moment they are written.
>
> These are **model-edge CANDIDATES, not sure things.** The conditional bracket model has **no market anchoring** (see caveats); where it disagrees most with the market, the market is often the one that is right. Nothing here is a claim of profitability.

---

## How to read this (classification rules applied)

Two independent adversarial verifiers re-fetched every live price, re-read every model fair value from source, and re-checked every settlement identity:

- **Verifier 1 (Lens 1 — live price / liquidity / settlement):** re-fetched all prices read-only from Gamma, confirmed every market OPEN, matched `bestAsk` within ~1c, and confirmed settlement is inclusive of ET/penalties on every leg. Verdict: **all 12 keep.**
- **Verifier 2 (Lens 2 — model anchoring / reconciliation):** re-read every `model_fair_prob` from `conditional_bracket_probs.csv` / `remaining_ties_probs.json`, confirmed positive EV at `bestAsk` on all 12, and confirmed settlement on all 12 — but issued **keep / adjust / drop** verdicts based on model-error risk.

Both verifiers returned `ev_holds = true` and `settlement_ok = true` for **all 12** legs, with **zero settlement mismatches**. So the colour is driven by whether either verifier *flagged* a leg (a non-"keep" verdict):

- **🟢 GREEN** — both verifiers confirm `ev_holds` **and** `settlement_ok` **and neither flagged it** (both "keep"). These are the placement-order list, sorted by EV% desc.
- **🟠 AMBER** — exactly one verifier flagged it (Verifier 2 = `adjust` or `drop`; Verifier 1 still `keep`). EV and settlement both still confirmed by both verifiers — but one verifier wants it sized down or dropped. Place with caution / reduced size, or skip.
- **🔴 RED / dropped** — **both** verifiers refute `ev_holds`, **or** a settlement mismatch was found. **No leg met this bar** — no leg failed `ev_holds` or `settlement_ok` in either verifier, and there were no settlement mismatches. The two legs Verifier 2 hard-**dropped** (Sweden R16, Belgium R16) are recorded in the AMBER table and again in the "Verifier-2 drop calls" table below, since each was dropped by only one of the two verifiers.

EV convention: for a YES at price `p` with model fair `f`, gross EV/$1 = `f/p − 1`; edge (pp) = `f − p`. EV%/edge below are computed on the mid `pm_price`; the real buy is the (slightly higher) ask and faces spread + PM fee — **subtract roughly 0.5–0.8pp of EV; gross EV% is not net.**

---

## 🟢 GREEN — place in EV order (both verifiers "keep")

Ordered by EV% descending. These are the recommendations with the cleanest two-verifier agreement.

| Rank | Market | Buy (outcome) | Live price (mid) | Model fair | Edge (pp) | EV% (gross) | Settlement | Sugg. ¼-Kelly % | Exposure note | Confidence |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Nation To Reach R16 | **Ecuador** | 0.39 (ask ~0.39) | 0.4884 | +9.86 | +26.9% | Beat host Mexico in R32, incl ET/pens = P(R16). Clean. | 4.03% | No conflict. Fades a likely **host premium** on Mexico; uncorrelated with other clusters. | medium |
| 2 | Nation To Reach R16 | **Australia** | 0.435 (ask ~0.44) | 0.53356 | +9.86 | +22.7% | Beat Egypt in R32, incl ET/pens = P(R16). Clean. | 4.18% | No conflict. Near-coinflip; model has AUS slight fav where market has them underdog. | medium |
| 3 | Nation To Reach QF | **Brazil** | 0.665 (ask ~0.67) | 0.80058 | +13.56 | +20.4% | Reach QF incl ET/pens = P(QF). Clean. | 9.89% | **NESTED Brazil layer** (lowest-variance). Pick **one** Brazil leg only — do **not** stack with reach-SF/Final/Winner. qK math gives the largest single stake (~10% of pool); that is because it is the safest layer, not licence to add it on top of others. | medium-high |
| 4 | Nation To Reach SF | **Brazil** | 0.335 (ask ~0.34) | 0.51962 | +18.46 | +55.1% | Reach SF (win R16+QF) incl ET/pens = P(SF). Clean. | 6.80% | **NESTED Brazil layer** (best edge-vs-liquidity balance; largest pp edge in the whole set). Pick **one** Brazil leg only. ~$72k liq. | medium-high |
| 5 | Nation To Reach Final | **Spain** | 0.215 (ask ~0.22) | 0.27968 | +6.47 | +30.1% | Reach final incl ET/pens = P(Final). Clean. | 1.91% | No conflict. **NESTED with Spain Winner** — pick one Spain layer. Very deep (~$249k). Modest edge, most liquid path bet. | medium |

**Brazil within GREEN:** ranks 3 and 4 are the **same Brazil thesis** at different depths. Stake **one**: reach-SF (rank 4) for the bigger pp edge, *or* reach-QF (rank 3) for lower variance. Their ¼-Kelly stakes are **not additive**.

---

## 🟠 AMBER — one verifier flagged (size down or skip)

Both verifiers still confirm `ev_holds` and `settlement_ok`; Verifier 1 said "keep" on every one of these. They are AMBER because **Verifier 2** issued `adjust` (size down) or `drop`. Sorted by EV% desc within the table; the two Verifier-2 `drop` legs are at the bottom and repeated in the next table.

| Market | Buy (outcome) | Live price (mid) | Model fair | Edge (pp) | EV% (gross) | Settlement | Sugg. ¼-Kelly % | Exposure note | V2 verdict | Confidence |
|---|---|---|---|---|---|---|---|---|---|---|
| World Cup Winner (outright) | **Brazil** | 0.07 (ask ~0.071) | 0.1878 | +11.73 | +166.4% | Champion incl ET/pens = P(Win). Clean. | 3.14% | **NESTED Brazil layer** (highest-variance expression of the Brazil thesis). Do not stack with other Brazil legs. | **adjust** — 2.7x multiple on a 7c longshot; deep-tail model error compounds; size down hard. | medium |
| Nation To Reach R16 | **Bosnia & Herzegovina** | 0.175 (ask ~0.18) | 0.3402 | +16.52 | +94.4% | Beat USA in R32, incl ET/pens = P(R16); cross-checked vs NO-on-elim-R32 (16.5pp vs 16.3pp). Clean; no USA position on the book. | 4.88% | No conflict. Pure underdog-advance disagreement (34% vs ~18%). | **adjust** — ~2x multiple on a sub-20c underdog; model-error zone; size small. | medium-low |
| Nation To Reach Final | **Brazil** | 0.18 (ask ~0.18) | 0.3093 | +13.43* | +76.7% | Win through SF incl ET/pens = P(Final). Clean. | 3.94% | **NESTED Brazil layer.** Pick one Brazil leg total; prefer GREEN reach-SF/QF over this. | **adjust** — four ties deep, compounding model error. *At live 0.18 the edge is 12.9pp / EV +71.8% (candidate's 13.43pp used a slightly lower price) — immaterial. | medium |
| Nation To Reach QF | **Paraguay** | 0.139 (ask ~0.139) | 0.29012 | +15.16 | +109.5% | Win R16 tie incl ET/pens = P(QF); already through to R16 (beat Germany on pens). Clean. | 4.39% | No conflict, uncorrelated. **Thinnest leg (~$17.8k) — fill-size limited.** | **adjust** — 2.1x multiple on a 14c underdog; size small. | medium-low |
| Nation To Reach QF | **Belgium** | 0.315 (ask ~0.32) | 0.48666 | +17.17 | +54.5% | Win R32 (vs Senegal) + R16, incl ET/pens = P(QF). Clean. Deepest advancement leg (~$160k). | 6.13% | **CORRELATED** with open **bet 94 (Belgium+Lukaku)** and with Belgium R16 leg — one stacked Belgium-run thesis. Two-step contingent (unplayed R32). | **adjust** — pick this as the **single** Belgium leg (best edge), size **down** for the existing book; not independent. | medium |
| Nation To Reach R16 | **Sweden** | 0.105–0.11 (ask ~0.11) | 0.23532 | +13.03 | +124.1% | Beat France in R32, incl ET/pens = P(R16); cross-checked vs NO-on-elim-R32 (13.0pp vs 12.1pp). Clean. *V1 noted a favourable drift to mid 0.105.* | 3.52% | No conflict. **CONTRADICTS the model's own MAP pick (France, 76.4%).** Most model-vs-market-divergent single-tie underdog. | **DROP** — most probable explanation is the model is wrong, not the market. | low-medium |
| Nation To Reach R16 | **Belgium** | 0.59 (ask ~0.59) | 0.67326 | +8.83 | +15.1% | Beat Senegal in R32, incl ET/pens = P(R16). Clean. | 5.08% | **CORRELATED/REDUNDANT** with bet 94 and the Belgium reach-QF leg above. | **DROP** — smallest edge of the set; do not add a third correlated Belgium leg; consolidate into reach-QF only. | medium |

---

## 🔴 Verifier-2 drop calls (one-verifier drops — treat as do-not-place)

No leg qualified as RED under the strict rule (RED requires **both** verifiers to refute `ev_holds`, or a settlement mismatch — neither occurred; both verifiers returned `ev_holds = true`, `settlement_ok = true` on all 12, zero mismatches). The two legs below were hard-**dropped by Verifier 2 only**, so they sit in AMBER above; they are repeated here so the drop signal is unmissable. Recommendation: **do not place these.**

| Market | Buy (outcome) | Live price | Model fair | Edge (pp) | EV% (gross) | Reason dropped (Verifier 2) |
|---|---|---|---|---|---|---|
| Nation To Reach R16 | Sweden | 0.11 | 0.23532 | +13.03 | +124.1% | EV is highest among single-tie underdogs **on paper only**. The bet **contradicts the model's own MAP pick** (France, 76.4% — model says France advances). Single most model-vs-market-divergent unanchored underdog. Most probable explanation: the model is wrong, not the market. **Drop.** |
| Nation To Reach R16 | Belgium | 0.59 | 0.67326 | +8.83 | +15.1% | **Smallest edge in the entire set.** Redundant — correlated with open **bet 94 (Belgium+Lukaku)** *and* with the Belgium reach-QF candidate (which carries more edge). Adding it is a third correlated Belgium leg. **Drop; consolidate into Belgium reach-QF only.** |

> Strict-rule footnote: if you instead read a Verifier-2 "drop" as a settlement-or-EV refutation, these two would still not be RED, because Verifier 1 independently confirmed both `ev_holds` and `settlement_ok` for each — only one of the two verifiers flagged them. They remain one-verifier flags (AMBER), surfaced here as do-not-place.

---

## Exposure reconciliation vs the 8 open bets

Net new directional exposure if you act on the GREEN list, mapped against the existing book:

| Open bet | Status / thesis | Interaction with candidates |
|---|---|---|
| **Bet 14** — Japan reach R16 (No) | **Pending WIN** (Japan out) — settled, no action. | No candidate touches Japan. No interaction. |
| **Bet 57** — Ghana eliminated R32 (short Ghana advancing) | Open, short Ghana. | **ALIGNED, not contradicted.** The Colombia reach-QF/R16 reads (Colombia beating Ghana) **double** this thesis rather than oppose it. No Ghana-to-advance candidate surfaced (model gives Ghana only ~17.5% to advance — no positive edge). Colombia reach-QF was a sub-threshold candidate (edge +4.38pp, EV +8.8%) flagged for exactly this overlap — **do not stake full Colombia size on top of bet 57.** |
| **Bet 94** — Belgium + Lukaku | Open, long Belgium progression. | **CORRELATED** with Belgium reach-QF (AMBER) and Belgium reach-R16 (AMBER/dropped). Pick **one** Belgium leg (reach-QF) and **size down** for this existing exposure; do not treat as independent. |
| **Bet 88** — England builder | Open. | No overlap — England produced **no** positive-edge advancement candidate. |
| **Bet 11** — Kane Golden Boot | Open, player-prop. | No overlap — award market, not priced by the bracket model. Left untouched. |
| **Bets 99 / 100** — SOT accumulators | Open, player-prop. | No overlap. Left untouched. |
| **Bet 101** | Resolved. | No interaction. |

**Nesting within the candidate set (NOT independent):**
- **Brazil** appears as Winner / Final / SF / QF — perfectly positively correlated cumulative-advance markets. **Stake at most ONE Brazil layer.** Recommended: reach-SF (GREEN, best edge) *or* reach-QF (GREEN, lowest variance). Their ¼-Kelly stakes are **not additive**.
- **Spain** appears as Winner / Final — same nesting. Pick one Spain layer (reach-Final is the surfaced GREEN one).
- **Belgium** reach-QF + reach-R16 + bet 94 — one stacked Belgium-run thesis; consolidate to one leg.

**Net new exposure if acting on GREEN:** one Brazil deep-run leg, one (sized-down, correlated-with-bet-94) Belgium leg if you take the AMBER Belgium-QF, one Spain final leg, and a basket of single-tie coinflip/underdog R16 advances (Ecuador, Australia in GREEN; Bosnia, Paraguay, Sweden in AMBER) that are **uncorrelated with each other and with the existing book.**

---

## Caveats (read before placing anything)

1. **Single live snapshot.** Prices fetched once at **2026-06-30T06:09:40Z** from the public Polymarket Gamma API. Knockout/advancement markets move continuously; these edges can evaporate or invert within hours. **Re-fetch before any decision.**
2. **The model has NO market anchoring.** These are conditional Elo+Dixon-Coles probabilities (50k sims, conditioned on the 4 played R32 results) — the model's **own** fair values, **not** a blend with the market. Where model and market disagree most — the underdog-advance bets (Sweden 23.5% vs 11%, Bosnia 34% vs 17.5%, Paraguay 29% vs 14%) — the EV% is largest **precisely because** the disagreement is largest, **and the market is often right.** Treat high-EV% sub-20c legs as the **lowest-confidence** candidates, not the best ones.
3. **Conditioning is ~1.5 days old and web-sourced.** The model is conditioned on R32 results gathered from the web (Japan, Germany, Paraguay-over-Germany, etc.). That conditioning is roughly a day and a half old and only as reliable as the web sources behind it; any result error propagates into every downstream advance probability.
4. **PM fees + spread are NOT netted in.** `pm_price` shown is the bestBid/bestAsk **mid**; the actual buy is the (higher) ask, and real fills face spread plus the Polymarket sports taker fee (~`0.03·p·(1−p)` per share, not included in the EV figures). **Subtract roughly 0.5–0.8pp of EV for fees/spread on mid-price legs.** Gross EV% is **not** net EV%.
5. **Per-match `fifwc-*` events were EXCLUDED.** Those ("Will France win on 2026-06-30?") are **90-minute 3-way** (home/draw/away) markets that settle on **regulation time only** — not comparable to the model's advance probabilities (which include ET/penalties). The R32 to-advance reads were instead derived from the "Stage of Elimination" markets (NO on "eliminated in R32") and cross-checked against the dedicated "Nation To Reach Round of 16" event; the two agreed to within ~1pp, confirming both settle inclusive of ET/pens.
6. **Brazil (Win/Final/SF/QF) and Spain (Win/Final) are NESTED cumulative-advance markets** — perfectly positively correlated. Their ¼-Kelly stakes are **NOT additive**. Stake at most one layer per team, or the effective Kelly fraction will be far above quarter-Kelly.
7. **¼-Kelly percentages are model suggestions, not dollar amounts.** Computed on the buy (ask) price as `f_kelly = (f − p)/(1 − p)` then `/4`, expressed as a fraction of the PM pool. They assume **independent** bets at the stated fair probs; for correlated legs (Brazil cluster, Belgium cluster + bet 94) the combined stake must be reduced. The pool was ~$1,310 USDC per the README, but the **current balance is unknown** — these are fractions, not asserted dollars.
8. **The single-tie underdog/coinflip legs (Bosnia, Sweden, Ecuador, Australia, Paraguay)** are conditional bets where a host-team or name premium in the market is the *likely* source of edge — but could equally be the market correctly pricing information the Elo+DC model lacks (form, injuries, lineups). **Lowest-confidence tier.**
9. **Edges are CANDIDATES, not locks, and nothing here claims profitability.** Consistent with the repo README's honest-expectations note: a positive model edge with no market anchoring is a hypothesis to be re-tested at the point of placement, not a guaranteed positive-EV trade.
10. **Full candidate set on disk.** All candidates with edge ≥3pp and 0.02 ≤ mid ≤ 0.97 are in `docs/research/wca_alpha_2026/data/pm_ev_candidates.csv` (the full 161-row file also includes negative-edge and out-of-band rows). Verifier-1 artifact: `docs/research/wca_alpha_2026/data/lens1_live_price_verify.json`. Model fair values: `docs/research/wca_alpha_2026/data/conditional_bracket_probs.csv` and `remaining_ties_probs.json`. **Analytical only — no orders placed, no trades, no DB writes, no `.env` access; `PM_DRY_RUN` untouched.**

---

*Generated 2026-06-30 from the EV compute result and two independent adversarial verifier reports. Analysis only — you place each bet yourself, and prices must be re-checked at the point of placement.*
