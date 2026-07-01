# Deployment Actions — 2026-06-30

> **⚠️ ANALYSIS ONLY — Claude did NOT place any bets. You place each one yourself. Re-check live odds at the moment of placement.** No orders were submitted, no DB/ledger writes were made, PM_DRY_RUN was untouched, nothing was pushed. Every price below was live as of this session's pull and **will move** — confirm in-app before staking. Nothing here is a guarantee of profit; EV figures are model estimates, several carry calibration risk, and some are upper bounds, not bankable alpha.

---

## How to read this file

Two independent verifiers (V1 = live-odds/EV/settlement lens; V2 = model-error/exposure lens) re-checked every item on the action card against a fresh live `theoddsapi` pull and the read-only `wca.db`. The **GREEN table below contains only the items BOTH verifiers chose to keep** (neither dropped). Items either verifier killed are in the **DROPPED/FLAGGED** table with the reason. Order follows the user rules: **moneyline +EV first → further-away kickoff first → then totals/promos/bet-builders.** Because the single surviving moneyline (Belgium) was dropped by V1 as model error, the GREEN cash slate is totals-only; promos and builders follow.

---

## 1) GREEN ACTION TABLE — both verifiers keep

| Rank | Category | Venue | Market | Selection | Stake | Odds | EV% | Kickoff (UTC) | Exposure note | Confidence |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Totals | **William Hill** (shop other books — see note) | Total Match Goals O/U 2.5 | **USA vs Bosnia — Over 2.5** | **£134 GBP** (¼-Kelly on £3,000; 5% cap £150 not binding) | **1.80** | +13.7% to +15.3% (model; **market de-vig −3.8%**) | 2026-07-02 00:00 | No open exposure. Same Over is also leg in P2 free-bet acca (rank 7) — shared outcome, mild positive correlation across tickets; acceptable. | **High** — single best item. xG-grounded totals layer (USA 2.28 + Bosnia 1.02 ≈ 3.30); both verifiers reproduced model P(O2.5)≈0.63–0.64; live WH 1.80 confirmed exactly. |
| 2 | Totals | **Matchbook 1.89 / others 1.78** (do NOT take WH 1.73 — soft side) | Total Match Goals O/U 2.5 | **Spain vs Austria — Over 2.5** | **£129 GBP** (¼-Kelly on £3,000; **only at 1.78+**) | **1.89** (target; 1.73 at WH is the wrong price) | +16.4% at 1.89 (V2); only +5.8% at WH 1.73; **market de-vig −8.5%** | 2026-07-02 19:00 | No open exposure. Also a leg in P2 acca (rank 7) — same shared-outcome note as rank 1. | **Med-high** — model P(O2.5)≈0.61. **V1 flagged WH 1.73 as the soft side** (ev_holds=false at 1.73); V2 shows the play is genuinely strong **at Matchbook 1.89**. SHOP UP — the named WH price leaves ~9% on the table. |
| 3 | Totals | **William Hill** (best Under price; others 1.64) | Total Match Goals O/U 2.5 | **Canada vs Morocco — Under 2.5** | **£49 GBP** (¼-Kelly on £3,000; smaller — only +4.4% edge) | **1.67** | +4.4% to +5.3% (model; **market de-vig −4.3%**) | 2026-07-04 17:00 | **Furthest-out fixture** (Jul 4) = softest line, satisfies prefer-further-out strongly. Distinct from the DROPPED Canada-draw 1X2 row — no self-conflict. No open exposure. | **Med** — model P(U2.5)≈0.625 (Canada 0.76 + Morocco 1.43 ≈ 2.19). Smallest of the totals edges and model-only; keep small. |

**Notes on the GREEN table**
- All three are **model-vs-market disagreements in the xG-grounded totals layer**, not soft-line captures: de-vigged market is mildly negative on every one. They survive because the totals fix is documented (08_xg_and_totals.md: real −0.66 goal/match under-forecast, paired-t p=0.0487) and is **1X2-preserving**, so it borrows nothing from the unproven moneyline blend. EVs are model estimates and **upper bounds** — sized down accordingly.
- **Settlement** for all three: 90-minute group-stage total goals, neutral venue, no void/participation risk (confirmed by both verifiers).
- Totals quotes in the feed come from a thin set of books — **shop every line**; a higher quote only increases the edge.

---

## 2) BET-BUILDERS — exact legs (free bets; no cash at risk)

> Exact builder prices **must be confirmed in the book UI** before placing — same-game builder combined odds are book-specific and the prices below are model-joint estimates, not quotes. All three are **free bets**, so any winning combined path is mechanically +EV regardless of calibration.

> **CORRECTION (post-review):** the original Builder #1/#2 legs were too short — England is a ~1.17 favorite, so "England win + Over 2.5 + England O1.5" are the same correlated event and Paddy Power's same-game engine deflates the combined **below the £2.0 promo minimum** (the earlier "≈2.66 book" estimate under-deflated the correlation). Replaced with longer margin/total legs, joint-priced on the England 2.35 / DRC 0.54 grid, that clear ≥2.0 with margin.

### Builder #1 — Paddy Power, £5 FREE BET (P1, builder 1 of 2) — balanced (~27% hit)
**Fixture:** England vs DR Congo, 2026-07-01 16:00 UTC
**Legs (3, same-game, Kane-free):**
1. England **−1.5** (win by 2+)
2. **Over 3.5** total match goals
3. England team **Over 2.5** (England 3+ goals)

**Combined / constraint check:** Exact joint over the score grid → **joint P ≈ 0.274, model-fair ≈ 3.66** — comfortably above the **≥2.0** minimum even after PP's correlation deflation (single-leg fairs: −1.5 ≈1.83, Over 3.5 ≈3.05, Eng O2.5 ≈2.40; naive product ≈13.4 hugely overstates, so confirm the real combined in-app). Free bet ⇒ profit on any winning path.
**Exposure:** Kane-free → no stack on Golden Boot #11.

### Builder #2 — Paddy Power, £5 FREE BET (P1, builder 2 of 2) — higher upside (~12% hit)
**Fixture:** England vs DR Congo, 2026-07-01 16:00 UTC
**Legs (3, same-game, Kane-free):**
1. England **−2.5** (win by 3+)
2. England team **Over 3.5** (England 4+ goals)
3. **BTTS — No**

**Combined / constraint check:** Exact joint → **joint P ≈ 0.123, model-fair ≈ 8.14** — clears ≥2.0 with wide margin. Higher free-bet EV `(O−1)·p ≈ 0.88` vs Builder #1's ≈ 0.73, at a lower hit-rate.
**Exposure:** Kane-free (BTTS-No + margin legs, no scorer leg) → no Kane stack vs #11/#99/#100.
**Caveat (both):** same-fixture free bets are **positively correlated** — if England underperforms, both lose; unavoidable on a one-sided promo fixture. **Confirm exact combined odds in the Paddy Power UI**; if either shows <2.0, lengthen a leg (Over 4.5, or push the handicap).

### Builder #3 / P2 acca — Betfair Sportsbook, £10 FREE BET (P2) — **both verifiers KEEP (free bet only)**
**Type:** 3-leg cross-game acca
**Legs:**
1. Spain vs Austria — **Over 2.5** (≈1.73–1.89)
2. USA vs Bosnia — **Over 2.5** (≈1.80)
3. Colombia to win (≈1.50–1.56)

**Combined / constraint check:** Cross-game ⇒ legs independent ⇒ **naive product IS the correct combined price**: 1.73 × 1.80 × 1.50 = **4.67** (above the ~3.375 target). On a free bet any combined > 1.0 is +EV.
**Exposure:** Colombia-to-win **OFFSETS** open PM bet #57 (Ghana-not-eliminated) — a Colombia win eliminates Ghana, so this mildly hedges rather than double-loads #57 (both verifiers confirmed). No Kane leg. **Take ONLY as the P2 free bet — cash on this acca is marginal/-EV on de-vig.** Optional full-decorrelation swap from the rank-1/2 cash singles: replace Spain-Austria Over with Argentina–Cape Verde Over (~1.67 at LeoVegas).

---

## 3) DROPPED / FLAGGED — a verifier killed or downgraded these

| Item | Card EV | Verifier action | Reason |
|---|---|---|---|
| **Belgium vs Senegal — Belgium (moneyline, h2h) @ 2.20** | Card +4.1% | **V1 = DROP** (model-error); V2 = size-down | **NOT GREEN — one verifier killed it.** V1: de-vigged consensus P(Belgium)=0.428 ⇒ EV at 2.20 = **−5.8%**; model 0.473 sits 4.5pp above market — the classic large-gap moneyline **MODEL-ERROR signature** the guardrails flag, not alpha. Card's "+4.1%" relies on the raw model. A 50/50 model/market blend ≈ −0.9%. Card's recorded 2.26 exists only on exchange (net < 2.20 after commission). **Do not place as a cash single.** |
| **2-Up early payout on the Belgium moneyline** | n/a (free option) | **V1 = redirect**; V2 = keep | 2-Up itself is a sound structural free option (~+1–4%), but it **cannot rescue the dropped −5.8% Belgium base**. Do NOT place the Belgium single just to harvest 2-Up. **Redirect** the 2-Up mechanic onto a near-fair heavy favourite you'd take anyway (Spain ~1.30, Argentina, Portugal) **IF** that fixture carries the 2-Up logo (verify in-app; unverifiable from feed). |
| **Argentina vs Cape Verde — Over 2.5 (cash single)** | Card −1.2% (no-cash) | **V1 = DROP** as cash; V2 = small-stake-OK at 1.67 | Verifiers disagree on whether a tiny cash stake is OK, so **not both-keep**. V1: EV_model −2.0% (even the model says no edge) at the card's 1.62; V2: +5.3% only at LeoVegas 1.67. **Use only as a free-bet acca-leg substitution** (P2). Do not cash as a single. |
| **Spain-Austria Over 2.5 @ WH 1.73 specifically** | Card +6.6% | **V1 ev_holds=false at 1.73** | The play is GREEN (rank 2) **but only at 1.78+ / Matchbook 1.89.** The named WH price is the soft side — flagged so you don't take 1.73. |
| **Daily builder boost (Bet365 25% / Betfred 50%)** | n/a | **Conditional only** (V1 keep-if-confirmed; V2 size-down) | Mechanically +EV on the boosted portion **IF** it lands on a model-favoured fixture, but **UNVERIFIABLE from the feed — `boost_evals` table is empty (0 rows)** and offers rotate daily. Confirm live in-app, price via the JOINT scoreline distribution (never naive leg product), avoid Kane / Ghana-result legs. **Do not bank EV blind.** Not a committed bet. |
| **Card's 13 cut longshots** (Sweden +47.7%, Bosnia +32%, etc.) | Large +EV headline | Excluded upstream | Classic model-error signature — large model-vs-market moneyline gaps. Correctly excluded; listed here for completeness. |
| **Canada DRAW 1X2 @ ~4.00** | Card +9.2% | Excluded upstream | A ~27%-prob draw with a model-vs-market gap is exactly the calibration-risk profile that is NOT a buy. Dropped despite headline EV. |
| **Australia–Egypt Under 2.5 @ 1.44** | −11.2% | Excluded | Book has fully priced it; strong model Under read but no edge at the live price. |

---

## 4) EXPOSURE RECONCILIATION vs the 8 OPEN BETS

| # | Open bet | Stake | Interaction with today's GREEN actions |
|---|---|---|---|
| 11 | Kane Golden Boot (Betfair SB) | £10 | **Guarded.** Both PP builders made **Kane-free** (Builder #1 by design; Builder #2 swapped "Kane to score" out for BTTS-No + corners) to avoid triple-loading Kane. No new Kane exposure today. |
| 14 | Japan reach R16 — No (Polymarket) | £60 | **No interaction.** No Japan fixture in today's slate. Untouched. |
| 57 | Ghana NOT eliminated R32 (Polymarket) | £1 | **OFFSET, not doubled.** P2 acca (rank 7 / Builder #3) Colombia-to-win leg eliminates Ghana on a win → mildly hedges #57. Do NOT add any sportsbook Ghana-to-qualify action on top. |
| 88 | England −2.5 / handicap / correct score (Betfair SB) | £5 | **No live overlap.** This is the past Jun-23 England-Ghana group game (pending settlement). Today's England-DRC builders are fresh exposure. |
| 94 | Belgium win + Lukaku (Paddy Power) | £10 | **No live overlap.** This is Belgium-Iran (Jun 21, resolved/pending). Belgium-Senegal would have been fresh — but it's DROPPED anyway, so no new Belgium exposure. |
| 99 | Ronaldo/Kane/Diaz SOT treble (Betfred) | £10 | **No live overlap** (Jun-23 fixtures, pending). Contains a Kane leg → reinforces the decision to keep today's builders Kane-free. |
| 100 | England HT/2H + Kane/Bellingham SOT (Betfred) | £10 | **No live overlap** (Jun-23 England-Ghana, pending). Another Kane leg → Kane-free guard upheld. |
| 101 | 2UP 4-fold Germany/Ivory Coast/Netherlands/Japan (Virgin) | £50 | **No interaction** (Jun-25 fixtures, pending). No overlap with today's slate. |

**Net:** All 8 open positions reconciled. Today's GREEN actions introduce **no double-loads**: Kane is guarded (#11/#99/#100), the only PM fixture overlap (#57 Ghana) is **offset** by the Colombia acca leg, and no GREEN cash item shares a live outcome with an open bet. Resolved-but-unscored open bets (88/94/99/100/101) are pending settlement and have no bearing on the new actions.

---

## 5) PORTFOLIO NOTES — how the rules were applied

**Rule 1 (moneyline +EV first).** Ranked h2h above totals where edge survives — but the decisive finding is that **the card's recorded book odds were stale/optimistic**. Re-pricing against the live feed cut every moneyline edge: Belgium 2.26→2.20, Colombia 1.56→1.50, Switzerland 2.16→2.05 (went **negative**), Mexico 2.34→2.25 (≈breakeven). The one survivor (Belgium) was then **dropped by V1 as a likely model error** (model 0.473 vs de-vig consensus 0.428 ⇒ −5.8% EV). **Result: no moneyline cash bet today.** The repo blend does not beat market-only with confidence, so large 1X2 model-vs-market gaps are treated as model error, not alpha.

**Rule 2 (prefer further-out).** Canada-Morocco Under (Jul 4, **furthest fixture** on the board) retained as a GREEN totals item; the P2 acca's clean ~1.5 moneyline leg is **Colombia** (also Jul 4), the furthest-out clean favourite.

**Rule 3 (don't discard good EV).** The genuine, live-verified +EV sits in the **xG-grounded totals**, correctly elevated above the soft moneylines: USA-Bosnia O2.5, Spain-Austria O2.5 (at 1.89), Canada-Morocco U2.5. Both promos retained as mechanical free-bet harvests. The over-harsh Arg-Cape Verde no-cash call was relaxed only to "free-bet-leg eligible," not promoted to a GREEN cash single (verifiers split).

**Rule 4 (exposure).** P2 acca uses Colombia to **offset** PM #57; both PP builders made **Kane-free** to protect Golden Boot #11; Belgium-Senegal would have been fresh (#94 = resolved Belgium-Iran) but is dropped regardless.

**Rule 5 (conservatism).** No large model-vs-market 1X2 gap taken as a buy. All same-game builders priced via the **Poisson/Dixon-Coles joint scoreline distribution**, never the naive leg product (Builder #1 naive ≈3.54 vs joint-fair ≈2.02). Totals EVs treated as upper bounds and sized down.

### Total stake by pool / currency
- **Cash at risk (GBP): ≈£312** at **¼-Kelly on the full £3,000 bankroll** (corrected from the card's ~£276 resolved bankroll, which was crushing the sizes) — rank 1 USA-Bosnia O2.5 **£134** + rank 2 Spain-Austria O2.5 **£129** (at 1.89) + rank 3 Canada-Morocco U2.5 **£49**. ≈10.4% of bankroll across 3 independent totals; the 5% per-bet cap (£150) does not bind. *(V1's extra ½ size-down removed per user instruction — these are full ¼-Kelly on the model probability; the EVs remain model-vs-market upper bounds, so this is the aggressive end of the range.)*
- **Free bets (no cash): £20 face** — £10 P1 (2 × £5 Paddy Power builders) + £10 P2 (Betfair acca).
- **Optional / conditional (no committed stake):** 2-Up adds no stake (redirect to a near-fair favourite, not Belgium); daily boost £10–£25 **only if** confirmed live in-app on a model-favoured fixture.
- **Headline today ≈ £312 cash + £20 free bets.**

---

## 6) CAVEATS (read before placing)

- **Live-price repricing is the headline.** The card's recorded best-venue quotes (11:53 UTC) were better than the live feed now. At executable prices, Colombia/Mexico moneylines are ≈breakeven and Switzerland is negative — all dropped. **Belgium was dropped as a model-error cash single.** Re-verify every quote in-app before placing; a stale favourable number is the most common trap here.
- **Shop the totals.** Take Spain-Austria Over **at 1.78+ (Matchbook ~1.89), not WH 1.73.** Shop USA-Bosnia O2.5 and Canada-Morocco U2.5 too — a higher quote only widens the edge.
- **Moneyline calibration risk.** The xG-totals fix leaves 1X2 supremacy **invariant**, so any moneyline edge carries full model-vs-market risk and is an upper bound, not realizable alpha. That is precisely why Belgium was dropped and no moneyline cash bet remains.
- **Totals EVs are upper bounds too.** De-vigged market is mildly negative on all three GREEN totals; the edge is model-vs-market in the (now-corrected, but historically mis-calibrated) totals layer. Sized down deliberately. Not bankable certainty.
- **Free bets are mechanical.** Builder EV is positive on any qualifying combined-odds path because the stake is free; the leg mispricing is a bonus. **Exact builder prices must be confirmed in the book UI** — same-game combined odds are book-specific, and the naive leg product massively overstates correlated builders.
- **Promos T&Cs.** 2-Up per-fixture availability must be confirmed by the in-app logo (standing feature, not every game). Daily boosts (Bet365 25% / Betfred 50%) are **unverifiable from the feed** (`boost_evals` empty); confirm the live fixture/terms in-app and re-price via the joint distribution before staking. Betfred's seed fixture (Uruguay-Cape Verde) is already past.
- **Prices move.** Everything here is a snapshot; kickoffs are Jul 1–4 and lines will drift. Re-check at placement.
- **No profitability is claimed.** Several listed fixtures are simulated and cannot be web-verified; all probabilities are model outputs and must never be treated as guaranteed profit. **ANALYSIS ONLY — no orders placed, no DB/ledger writes, PM_DRY_RUN untouched, nothing pushed.**
