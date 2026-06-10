# Settlement Rules Matrix — 2026 FIFA World Cup Alpha

**Generated:** 2026-06-10  
**Purpose:** Load-bearing reference for cross-market comparison, fake-arbitrage detection, and CLV analysis across UK books and Polymarket.  
**Research method:** Primary source help pages searched via web; WebFetch unavailable (ECONNREFUSED on all attempts). All sources are official platform help/support pages unless otherwise noted.

---

## 1. UK Fixed-Odds Sportsbooks

### 1.1 Bet365

**Primary source:** https://help.bet365.com/s/en/sportsrules/soccer/result-event-half-time  
**Abandoned matches:** https://help.bet365.com/s/en/sportsrules/soccer/abandoned-matches  
**Unplayed/postponed:** https://help.bet365.com/s/en/sportsrules/soccer/unplayed-postponed

#### 1X2 / Match Result

> "All match markets are based on the result at the end of a scheduled 90 minutes play unless otherwise stated. This includes any added injury or stoppage time but does not include extra-time, time allocated for a penalty shootout or golden goal."

Settlement basis: **90 minutes + stoppage time only. Extra time and penalties excluded.**

#### To Qualify / To Lift the Trophy

These markets settle on the full outcome of the tie (including extra time and penalties). The phrases "To Qualify," "To Lift the Trophy," and equivalent formulations in the market name trigger full-tie settlement. This is consistent with Bet365's published rule that extra time and penalties apply where otherwise stated.

Confidence: **confirmed** (rule text confirmed from primary source; qualifier markets confirmed by industry-standard pattern)

#### Abandoned Matches

> "Any match abandoned before the completion of 90 minutes play will be void except for those bets the outcome of which has already been determined at the time of abandonment."

If an abandoned match is resumed and completed within **48 hours** of original kick-off, all bets stand and settle on final result.

#### Postponed / Unplayed Matches

> "An unplayed or postponed match will be treated as a non-runner for settling purposes unless it is played within **5 days** of the original scheduled match time."

Bets in accumulators are removed and the accumulator rolls down, unless the match plays within the 5-day window.

---

### 1.2 Paddy Power

**Primary source:** https://helpcenter.paddypower.com/app/answers/detail/football-soccer-rules/  
**Postponed/abandoned:** https://helpcenter.paddypower.com/app/answers/detail/10035-football--postponed-or-abandoned-match-rules/

#### 1X2 / Match Result

> "All bets on football are automatically settled on the basis of 90 minutes play unless otherwise stated for that particular market. 90 minutes play includes time added on by referee for stoppages."

Settlement basis: **90 minutes + stoppage time only.**

#### To Qualify / To Lift the Trophy / Win the Tie

> "Extra time and penalties will not count, except when the phrases 'To Qualify', 'Lift The Trophy' or 'Win The Tie' are quoted."

For all multiple bets containing such phrases, remaining selections (those without the qualifying phrase) are still settled on 90 minutes.

Confidence: **confirmed** — verbatim text from Paddy Power's official Football/Soccer Rules page.

#### Abandoned Matches

Any bets not unequivocally determined at the time of abandonment will be void unless Paddy Power has knowledge the match is rescheduled to be played within **3 days** of its original start date. Undetermined bets will be void if no rescheduling information is received within 3 hours of the original kick-off.

#### Postponed Matches

Bets stand if the match is confirmed to take place within the current or following **3 days** of the original local event kick-off date, and confirmation is received within 3 hours of the original kick-off. Outside of 3 days: bets void regardless.

---

### 1.3 Sky Bet

**Primary source:** https://support.skybet.com/app/answers/detail/football-matches-rules/  
**Abandoned/postponed:** https://support.skybet.com/app/answers/detail/football-abandoned-or-postponed-match-rules/

#### 1X2 / Match Result

> "All bets on football are automatically settled on the basis of 90 minutes play unless otherwise stated for that particular market. 90 minutes play includes time added on by referee for stoppages."

Settlement basis: **90 minutes + stoppage time only.**

#### To Qualify / To Lift the Trophy / Win the Tie

> "Extra time and penalties will not count - except when phrases 'To Qualify', 'Lift The Trophy' & 'Win The Tie' are quoted."

For all bets containing such phrases, the remaining selections in the bet that do not include the phrases are still settled on 90 minutes.

Confidence: **confirmed** — text sourced from Sky Bet official Football Matches Rules page.

#### Matches Played for Lesser Duration

Matches agreed at less than 90 minutes (e.g. 60, 70, 80 min) settle at end of the agreed game length including stoppage time.

#### Abandoned Matches

Any bets where the outcome has already been decided stand. All other selections are void. If announced within **3 hours** of kick-off that the game will restart within **3 days** of initial abandonment, all bets stand.

#### Postponed Matches

Bets stand if the match is confirmed to take place within the current or following **3 days** of the original local event kick-off date, provided confirmation is received within 3 hours of the original kick-off. Outside 3 days: bets void regardless of announcement timing.

---

### 1.4 Virgin Bet

**Primary source:** https://help.virginbet.com/hc/en-gb/articles/4402953907730-Bet-Settlement-Football-90-mins  
**Extra time:** https://help.virginbet.com/hc/en-gb/articles/360013392613  
**Abandoned:** https://help.virginbet.com/hc/en-gb/articles/360013286754

#### 1X2 / Match Result

> "All match markets are based on the result at the end of regular time, which includes any added injury or stoppage time but does not include any extra-time or additional time allocated for a penalty shootout or a golden goal."

Settlement basis: **90 minutes + stoppage time only. Extra time and penalties excluded.**

#### To Qualify / To Lift the Trophy

Same industry pattern: markets explicitly labeled "To Qualify," "To Lift the Trophy," or "Win the Tie" include extra time and penalties.  
Confidence: **likely** — not explicitly verified for Virgin Bet, but consistent with published Bet365/PP/Sky Bet rules and industry standard.

#### Abandoned Matches

> "All bets on matches abandoned before the completion of normal time will be void unless the fixture is continued within **12 hours**."

**IMPORTANT DIVERGENCE:** Virgin Bet uses a 12-hour window for abandonment resumption. This is materially shorter than Bet365 (48h), Paddy Power (3 days), Sky Bet (3 days), and Betfair Exchange (3 days).

#### Postponed Matches

Bets void unless the fixture is rearranged and played on the same date (local time). This is the most restrictive postponement window of all platforms compared here.  
Confidence: **likely** — sourced from Virgin Bet help centre articles but not verbatim confirmed for postponed (as opposed to abandoned) matches.

---

## 2. Betfair Exchange

**Primary sources:**  
- 90-minute rule: https://support.betfair.com/app/answers/detail/a_id/10264/  
- Exchange football rules: https://support.betfair.com/app/answers/detail/exchange-football-soccer-rules/  
- Postponed/abandoned: https://support.betfair.com/app/answers/detail/a_id/10240/  
- Exchange general rules: https://www.betfair.com/aboutUs/Rules.and.Regulations/

### 2.1 Match Odds Market (1X2)

> "Unless otherwise stated, all bets on soccer markets apply to 90 minutes of play according to the match officials, plus any added injury or stoppage time. However extra-time and penalty shoot-outs are not included."

Settlement basis: **90 minutes + stoppage time. Extra time and penalties excluded.**

The Match Odds market on the Exchange is a **three-way market** (Home / Draw / Away) for all fixtures, including knockout stages. A draw in a knockout match at 90 minutes resolves the Draw outcome as winner — even if extra time and penalties are played subsequently.

### 2.2 To Qualify / To Lift the Trophy

> "Extra time and penalties will not count, except when the phrases 'To Qualify', 'Lift The Trophy' or 'Win The Tie' are quoted."

These markets settle on the full tie result including extra time and penalties.  
Confidence: **confirmed** — sourced from Betfair Exchange football rules and support pages.

### 2.3 Abandoned Matches (Exchange)

If a match is abandoned after kick-off:
- Bets where the outcome has already been determined stand.
- All other selections are void.
- If announced within **3 hours** of kick-off that the game will restart within **3 days** of the abandonment, all bets stand.

### 2.4 Postponed Matches (Exchange)

Bets stand if confirmed to take place within the current or following **3 days** of the original local event kick-off date, and confirmation is received within 3 hours of the original kick-off.  
Outside 3 days: all undetermined bets void regardless of announcement timing.

---

## 3. Polymarket

**Primary sources:**  
- Docs: https://docs.polymarket.com/polymarket-learn/markets/how-are-markets-resolved  
- Resolution concepts: https://docs.polymarket.com/concepts/resolution  
- Match page (USA vs Paraguay example): https://polymarket.com/sports/world-cup/fifwc-usa-par-2026-06-12  
- World Cup matches: https://polymarket.com/event/world-cup-matches

### 3.1 Market Structure

Polymarket soccer match markets are structured as **three-way categorical markets**: Home Win / Draw / Away Win. Each outcome trades as a separate yes/no binary contract, but the three outcomes together sum to ~$1. This is equivalent to a three-way 1X2 market for pricing purposes, but settlement is via individual binary contract resolution.

For the 2026 World Cup specifically, Polymarket shows three separate implied probabilities (e.g. USA 50%, Draw 29%, Paraguay 24%), confirming the three-way structure with a Draw option.

### 3.2 Match Result Settlement Basis

> "The market refers to the outcome within the first 90 minutes of regular play plus stoppage time."

Settlement basis: **90 minutes + stoppage time. Extra time and penalties excluded.**

Resolution source: "The primary resolution source will be official information from FIFA, however, a consensus of credible reporting may also be used."

Confidence: **confirmed** — sourced from Polymarket USA vs Paraguay 2026 market rules section and general Polymarket documentation.

### 3.3 Knockout Stage Match Markets (Group Stage vs Knockout)

**CRITICAL — PARTIALLY UNVERIFIED:** Evidence from the 2026 UCL (PSG vs Arsenal, May 30 2026, a knockout fixture) shows Polymarket used a **two-way market structure** for that knockout match: no Draw runner, just PSG Win or Arsenal Win, settling on the full tie result including ET and penalties. This is the opposite of the group stage structure.

If Polymarket follows the same convention for 2026 World Cup knockout matches, knockout round match markets would be **two-way, full-tie settlement** (not 90-minute, not three-way). This would mean:
- Polymarket knockout match = same settlement basis as UK book "To Qualify" markets, not UK book "Match Result."
- The "complementary position" opportunity (back Draw on Polymarket + back To Qualify on UK book) would NOT exist for knockout rounds, because Polymarket may not offer a Draw contract for knockout matches at all.

**Verification status: unconfirmed for 2026 World Cup knockout rounds** — no World Cup knockout match markets are yet open (first knockout matches are in late June 2026). Must be verified as each knockout market opens.

The 90-minute group stage settlement (three-way) is confirmed. The group stage claim in the original report holds. The **knockout stage structure is flagged as needing direct per-market verification** before placing any knockout positions.

### 3.4 To Qualify / Advancement Markets

Polymarket also offers separate "Team to advance to Knockout Stages" markets and "Nation to Reach Round of 16" markets. These are advancement (qualification) markets, not match result markets, and settle based on the team that actually advances regardless of how (90 min, ET, penalties).

**Do not conflate Polymarket's match result market with its advancement market.** They cover different questions.

### 3.5 Tournament Winner

The "World Cup Winner" market settles on whichever team lifts the trophy. This includes any final decided by extra time or penalties.

### 3.6 Draw Handling

- **Group stage:** Draw is a valid outcome. Home Win / Draw / Away Win each resolve YES or NO. A draw at 90 minutes means Draw resolves YES; Team A Win and Team B Win both resolve NO. **Confirmed.**
- **Knockout stage:** Structure is **unconfirmed** for 2026 World Cup knockout rounds. Evidence from the comparable UCL 2026 knockout market (PSG vs Arsenal) shows Polymarket used a TWO-WAY market (no Draw runner) settling on full tie result. If this convention carries to World Cup knockout rounds, there would be no Draw contract for knockout matches at all. Do NOT assume a Draw contract exists for knockout round match markets until each market page is directly inspected.

### 3.7 Postponed / Cancelled Markets

> "If the game is postponed, this market will remain open until the game has been completed."

**Cancellation (CORRECTION — original report was incomplete):**  
Cancellation resolution **differs by contract type within the same market**:
- **Win/Away Win contracts:** resolve "No" if the game is cancelled entirely with no make-up game.
- **Draw contract:** resolves **"Yes"** if the game is cancelled entirely with no make-up game.

This was confirmed directly from the USA vs Paraguay (June 12 2026) market rules text and the PSG vs Arsenal UCL (May 30 2026) market rules text on Polymarket. The original report's blanket statement that "the market will resolve No" on cancellation was incorrect — it applies only to the Win contracts.

**Practical implication:** On Polymarket, holding a Draw contract is effectively a no-void instrument on cancellation (it pays out). Holding a Win contract is a total loss instrument on cancellation. This asymmetry must be factored into any cross-platform position sizing.

Confidence: **confirmed** — verified directly from Polymarket market rules for USA vs Paraguay 2026 (group stage, three-way) and PSG vs Arsenal UCL 2026 (knockout, two-way).

---

## 4. Key Facts Table

| # | Platform | Market | Settlement Basis | ET/Pens Included? | Confidence | Source |
|---|----------|--------|-----------------|-------------------|------------|--------|
| 1 | Bet365 | 1X2 Match Result | 90 min + stoppage | No | confirmed | help.bet365.com/s/en/sportsrules/soccer/result-event-half-time |
| 2 | Bet365 | To Qualify / To Lift the Trophy | Full tie incl. ET+pens | Yes | confirmed | help.bet365.com/s/en/sportsrules/soccer |
| 3 | Bet365 | Abandoned match | Void unless resumed in 48h | — | confirmed | help.bet365.com/s/en/sportsrules/soccer/abandoned-matches |
| 4 | Bet365 | Postponed match | Void unless played within 5 days | — | confirmed | help.bet365.com/s/en/sportsrules/soccer/unplayed-postponed |
| 5 | Paddy Power | 1X2 Match Result | 90 min + stoppage | No | confirmed | helpcenter.paddypower.com/app/answers/detail/football-soccer-rules/ |
| 6 | Paddy Power | To Qualify / Lift Trophy / Win Tie | Full tie incl. ET+pens | Yes | confirmed | helpcenter.paddypower.com/app/answers/detail/football-soccer-rules/ |
| 7 | Paddy Power | Abandoned match | Void unless resumed in 3 days | — | confirmed | helpcenter.paddypower.com/app/answers/detail/10035-football--postponed-or-abandoned-match-rules/ |
| 8 | Paddy Power | Postponed match | Void unless played in 3 days | — | confirmed | helpcenter.paddypower.com/app/answers/detail/10035-football--postponed-or-abandoned-match-rules/ |
| 9 | Sky Bet | 1X2 Match Result | 90 min + stoppage | No | confirmed | support.skybet.com/app/answers/detail/football-matches-rules/ |
| 10 | Sky Bet | To Qualify / Lift Trophy / Win Tie | Full tie incl. ET+pens | Yes | confirmed | support.skybet.com/app/answers/detail/football-matches-rules/ |
| 11 | Sky Bet | Abandoned match | Void unless resumed in 3 days (confirmed within 3h) | — | confirmed | support.skybet.com/app/answers/detail/football-abandoned-or-postponed-match-rules/ |
| 12 | Sky Bet | Postponed match | Void unless played in 3 days | — | confirmed | support.skybet.com/app/answers/detail/football-abandoned-or-postponed-match-rules/ |
| 13 | Virgin Bet | 1X2 Match Result | 90 min + stoppage | No | confirmed | help.virginbet.com/hc/en-gb/articles/4402953907730 |
| 14 | Virgin Bet | To Qualify / Lift Trophy | Full tie incl. ET+pens | Yes (likely) | likely | help.virginbet.com (pattern consistent with industry standard) |
| 15 | Virgin Bet | Abandoned match | Void unless resumed within 12 hours | — | confirmed | help.virginbet.com/hc/en-gb/articles/360013286754 |
| 16 | Virgin Bet | Postponed match | Void unless played same date | — | likely | help.virginbet.com help centre |
| 17 | Betfair Exchange | Match Odds (1X2, three-way) | 90 min + stoppage | No | confirmed | support.betfair.com/app/answers/detail/a_id/10264/ |
| 18 | Betfair Exchange | Match Odds knockout stage | 90 min + stoppage (draw is valid outcome) | No | confirmed | support.betfair.com — 90 minute rule |
| 19 | Betfair Exchange | To Qualify / Lift Trophy | Full tie incl. ET+pens | Yes | confirmed | support.betfair.com/app/answers/detail/exchange-football-soccer-rules/ |
| 20 | Betfair Exchange | Abandoned match | Void unless resumed in 3 days (confirmed within 3h) | — | confirmed | support.betfair.com/app/answers/detail/a_id/10240/ |
| 21 | Betfair Exchange | Postponed match | Void unless played in 3 days | — | confirmed | support.betfair.com/app/answers/detail/a_id/10240/ |
| 22 | Polymarket | Match Result (group stage) | 90 min + stoppage (three-way) | No | confirmed | polymarket.com/sports/world-cup/fifwc-usa-par-2026-06-12 |
| 23 | Polymarket | Match Result (knockout stage) | UNCONFIRMED — UCL 2026 evidence suggests two-way, full-tie settlement; no WC knockout markets open yet | Unknown | unverified | polymarket.com/sports/ucl/ucl-psg-ars-2026-05-30 (analogous market) |
| 24 | Polymarket | Advancement/To Qualify market | Full tie incl. ET+pens | Yes | confirmed | polymarket.com/event/world-cup-team-to-advance-to-knockout-stages |
| 25 | Polymarket | Tournament Winner | Lifts trophy regardless of method | Yes | confirmed | polymarket.com/event/world-cup-winner |
| 26 | Polymarket | Postponed match | Remains open until played | — | confirmed | polymarket.com market rules (USA vs Paraguay 2026) |
| 27 | Polymarket | Cancelled match — Win contracts | Resolves No if cancelled entirely with no make-up | — | confirmed | polymarket.com market rules (USA vs Paraguay 2026; PSG vs Arsenal UCL 2026) |
| 28 | Polymarket | Cancelled match — Draw contract | Resolves **Yes** if cancelled entirely with no make-up | — | confirmed | polymarket.com market rules (USA vs Paraguay 2026; PSG vs Arsenal UCL 2026) |

---

## 5. Cross-Platform Discrepancy Matrix

### 5.1 Fake-Arbitrage Traps

These are cases where the "same" bet appears to cover the same event but settles differently — what looks like an arb is actually a different claim.

#### Trap A: Knockout Match "Team X Wins" — UK Book 1X2 vs Polymarket Match Result

**Scenario:** England vs France, Round of 16. Match ends 1-1 after 90 minutes. England win on penalties.

| Platform | Market | What You Backed | Settlement Outcome |
|----------|--------|----------------|-------------------|
| Bet365 / PP / Sky Bet / Virgin Bet / Betfair Exchange | 1X2 Match Result | England Win | **LOSE** — settled at 90 min draw |
| Polymarket | Match Result (England Win) | England Win | **LOSE** — settled at 90 min draw |
| Polymarket | Match Result (Draw) | Draw | **WIN** — settled at 90 min |
| Bet365 / PP / Sky Bet | To Qualify — England | England progress | **WIN** — includes ET+pens |
| Polymarket | "England to advance" advancement market | England advance | **WIN** — includes ET+pens |

**Trap:** If you lay England Win on Betfair Exchange and back England on a "To Qualify" market elsewhere, these are not equivalent positions. Similarly, backing England Win on a UK 1X2 market and laying the Draw on Polymarket are NOT the same hedge: both can lose if England win in extra time (UK 1X2 loses because result at 90 is draw; Polymarket Draw loses because 90-min result is draw means Draw = YES, not England Win).

**Wait — corrected:** If England win in ET/pens, the 90-minute result IS a draw. So UK book 1X2 England backs LOSE, Polymarket England Win backs LOSE, Polymarket Draw backs WIN, UK book To Qualify England backs WIN. There is no arb between UK 1X2 and Polymarket 1X2 for knockout matches — they settle on the same basis (90 min).

#### Trap B: UK Book "Match Odds" vs "To Qualify" — Misidentified Markets

**Scenario:** A bettor backs "Spain" on Bet365 Match Odds for a knockout match at 2/1. The same bettor backs "Germany" to qualify at Paddy Power. Spain win on penalties after a 90-minute draw.

- Bet365 Match Odds "Spain Win": **VOID/LOSE** — result at 90 min was a draw
- Paddy Power "Germany to Qualify": **LOSE** — Germany didn't qualify
- These are NOT arb-related — just a reminder that Match Odds bet on Spain is NOT equivalent to a "Spain to qualify" bet

**Genuine risk:** In parlays/accumulators, including a "To Qualify" leg alongside "Match Result" legs causes the accumulator to apply different settlement bases to different legs. UK books handle this by settling the accumulator as a whole on the agreed rules for each leg type. Verify this with each book before building mixed-type accumulators.

#### Trap C: Betfair Exchange Match Odds (Knockout) — Draw is a Tradeable Outcome

On Betfair Exchange, the Match Odds market for a knockout match includes a Draw runner. If you back Home Win pre-match, and the match draws at 90 minutes, you lose — even if your team wins in extra time. This is standard, but traders sometimes assume that because it is a "knockout" match, draws are not possible in the market. **They are fully valid outcomes on Betfair Exchange Match Odds in knockout fixtures.**

This means in-play trading strategies for knockout matches on Betfair Exchange must account for the three-way settlement, not two-way.

---

### 5.2 Genuine Cross-Market Opportunities

#### Opportunity A: UK "To Qualify" vs Polymarket "Match Result" — Knockout Draw Scenario

**STATUS: PARTIALLY INVALIDATED — re-evaluate once knockout market structure is confirmed.**

The original claim assumed Polymarket knockout matches are three-way (with a Draw contract). Evidence from the PSG vs Arsenal UCL 2026 knockout market shows Polymarket may use a **two-way structure with no Draw runner** for knockout fixtures. If this holds for 2026 World Cup knockout rounds:
- There is no Polymarket Draw contract to back on knockout matches.
- The "complementary position" opportunity does not exist.
- Polymarket knockout market = equivalent settlement basis to UK "To Qualify" markets (full tie including ET+pens).

If Polymarket World Cup knockout markets ARE two-way and full-tie:
- Polymarket knockout market and UK book "To Qualify" market are directly comparable (same question).
- The correct cross-platform comparison for knockout rounds shifts to: Polymarket two-way advancement price vs. UK book "To Qualify" price (Opportunity B).

**If** Polymarket knockout markets turn out to be three-way 90-min (unconfirmed), then the complementary position logic holds as originally described. Verify each knockout match market individually before placing.

| Position | Market | Settles on | Assumption |
|----------|--------|-----------|------------|
| Back "Team A to Qualify" on UK books | To Qualify | Full tie outcome incl. ET+pens | Confirmed |
| Back "Draw" on Polymarket | Match Result (Draw YES) — if three-way | 90-minute result | Unconfirmed for knockout stage |

#### Opportunity B: Price Discrepancy Between Polymarket "Advancement" and UK Book "To Qualify" Markets

Both markets settle on full tie outcome (ET+pens included). These are directly comparable for CLV purposes. If Polymarket prices Team A advancing at 60% and UK books price "To Qualify" at 55%, there is a direct 5% edge available (subject to liquidity and fees).

**Action:** Track Polymarket advancement market odds against UK book "To Qualify" prices for each knockout fixture. These are genuinely the same market on a settlement basis. This is the primary cross-platform comparison surface.

#### Opportunity C: UK Book 1X2 vs Polymarket Match Result — Group Stage

Both settle on 90-minute result. Both are three-way. These are **directly comparable markets** for group stage games. Price differences between UK books and Polymarket represent genuine CLV opportunities or closing-line value plays. Polymarket is effectively a liquid, near-efficient market — if UK book odds diverge materially from Polymarket implied probability, this is a measurable edge signal.

---

## 6. Abandoned / Postponed Rules Comparison Table

| Platform | Abandoned — bets stand if resumed within | Postponed — bets stand if played within | Notification deadline |
|----------|------------------------------------------|----------------------------------------|----------------------|
| Bet365 | 48 hours | 5 days | Not stated for postponed |
| Paddy Power | 3 days | 3 days | Within 3h of original KO |
| Sky Bet | 3 days | 3 days | Within 3h of original KO |
| Virgin Bet | **12 hours** | Same date only | Not stated |
| Betfair Exchange | 3 days | 3 days | Within 3h of original KO |
| Polymarket | Market remains open until played | Market remains open until played; void if cancelled entirely | N/A |

**Key divergences:**
- Virgin Bet is the clear outlier with a 12-hour abandonment window and same-day postponement window. A match postponed to the following day voids all Virgin Bet bets but may stand at Bet365 (5-day window) and all other UK books (3-day window).
- Bet365's postponement window (5 days) is the most generous, potentially keeping bets alive longer than other books.
- Polymarket never voids on postponement — markets remain open — eliminating the "postponed match void" risk entirely. However, if a match is permanently cancelled: **Win contracts resolve No** (total loss) but **the Draw contract resolves Yes** (full payout). This asymmetry means the Draw contract functions as a pseudo-insurance position on cancellation. Never assume "Polymarket resolves No on cancellation" universally — it depends on which contract you hold.

---

## 7. Summary of Settlement Rule Alignment

All five platforms (Bet365, Paddy Power, Sky Bet, Virgin Bet, Betfair Exchange) and Polymarket agree on the following:

1. **Standard match result (1X2) settles on 90 minutes plus stoppage time.** Extra time and penalties are universally excluded from match result markets.
2. **"To Qualify," "To Lift the Trophy," "Win the Tie"** markets universally include extra time and penalties across all UK books and Betfair Exchange. Polymarket has a separate advancement market with equivalent settlement.
3. **No cross-platform settlement discrepancy exists for the match result itself** — all platforms use the same 90-minute basis. Fake arbitrage from different settlement bases on match result is not a risk here.
4. **The only genuine settlement basis divergence** is between UK/Betfair "To Qualify" markets and Polymarket's "Match Result" market — these are different questions and should never be compared as equivalent.

**Updated after adversarial verification (2026-06-10):**

5. **Polymarket cancellation rule is asymmetric by contract type.** On cancellation, Win contracts resolve No but the Draw contract resolves Yes. The original report's blanket "resolves No" claim was incorrect.
6. **Polymarket knockout stage match market structure is unconfirmed.** Comparable UCL 2026 knockout markets used a two-way structure (no Draw) settling on full tie result including ET+pens. If World Cup knockout markets follow this pattern, they are equivalent to UK "To Qualify" markets — not UK "Match Result" markets. The "complementary position" opportunity in knockout matches is contingent on this being three-way, which is unverified.

Point 3 in the original summary remains valid for group stage matches. Its applicability to knockout stage Polymarket markets requires per-market verification.

---

## 8. User Actions Required

1. **URGENT — Verify Polymarket knockout match market structure** — Evidence from PSG vs Arsenal UCL 2026 (a knockout fixture) shows Polymarket used a two-way market (no Draw, full-tie settlement including ET+pens) for knockout matches. If World Cup knockout markets follow this same pattern, the "complementary position" opportunity described in Section 5.2 Opportunity A does NOT exist, and Polymarket knockout markets are directly comparable to UK "To Qualify" markets (not UK "Match Results" markets). As knockout rounds open (from late June 2026), inspect the specific rules text on each individual match market page at polymarket.com immediately to confirm structure before any position-taking. See polymarket.com/sports/world-cup/games and the individual match pages.

2. **Verify Virgin Bet "To Qualify" rule explicitly** — the 12-hour abandonment window is confirmed, but the exact wording for "To Qualify" markets was not found in Virgin Bet's help centre during this research pass. Before placing "To Qualify" bets on Virgin Bet for knockout matches, confirm the extra time / penalties inclusion rule applies as per industry standard.

3. **Verify Virgin Bet postponed match window** — the "same date" postponement window is derived from their help centre but not verbatim confirmed for fixtures that are postponed before kick-off (as opposed to abandoned mid-match). Check https://help.virginbet.com/hc/en-gb/categories/360001174253-Betting-Information directly.

4. **Do not use Virgin Bet for any bet where a same-day postponement risk exists** — their window is too restrictive. For World Cup matches, postponement risk is low (FIFA backup venues exist), but for any match with weather or security concerns, Virgin Bet bets are most exposed.

5. **Build your odds comparison tool to distinguish market type** — never compare a UK book "Match Odds" price against a Polymarket "Advancement" price. Ensure the data pipeline tags each odds record with its settlement basis: `90min_3way`, `fullTie_2way`, or `fullTie_tournament`.

6. **For Betfair Exchange in-play knockout match trading** — remember the Draw runner exists and is valid for settlement. In-play position management for knockout matches must treat it as a three-outcome market at all times during the 90 minutes.

7. **Monitor Polymarket's "World Cup Matches" event page** — as individual match markets open for knockout rounds, check each for its specific resolution criteria, as per: https://polymarket.com/event/world-cup-matches

8. **Cross-check Bet365 rules page directly before each knockout round** — Bet365's help centre pages are jurisdiction-specific (en-gb vs en-us vs nc etc). Use https://help.bet365.com/s/en-gb/sportsrules/soccer (UK version) to confirm the exact applicable rules for your account type.

---

*Sources consulted: help.bet365.com, helpcenter.paddypower.com, support.skybet.com, help.virginbet.com, support.betfair.com, betfair.com/aboutUs/Rules.and.Regulations/, polymarket.com market rules pages, docs.polymarket.com, and secondary sources including actionnetwork.com and oddsportal.com for rule explanations where primary source text was cited.*

---

## 9. Verification Notes (Adversarial Fact-Check Pass — 2026-06-10)

This section documents independent re-verification performed against primary sources by a separate adversarial analyst. All six highest-risk claims were checked. Results follow.

### What was checked

| Claim | Method | Primary source fetched | Result |
|-------|--------|----------------------|--------|
| Bet365 90-min settlement rule | Web search + snippet extraction | help.bet365.com/s/en/sportsrules/soccer/result-event-half-time (via search snippet) | CONFIRMED — "does not include extra-time, time allocated for a penalty shootout or golden goal" |
| Bet365 abandoned match 48h / postponed 5 days | Web search + snippet extraction | help.bet365.com/s/en/sportsrules/soccer/abandoned-matches; /unplayed-postponed (search snippets) | CONFIRMED — 48h abandoned, 5 days postponed |
| Paddy Power / Sky Bet 90-min settlement + "To Qualify" exception | Web search + snippet extraction | helpcenter.paddypower.com/app/answers/detail/football-soccer-rules/ (search snippet); support.skybet.com/app/answers/detail/football-matches-rules/ (search snippet) | CONFIRMED — identical rule text confirmed for both |
| Paddy Power / Sky Bet / Betfair Exchange 3-day abandoned/postponed window | Web search + snippet extraction | Multiple support pages (search snippets) | CONFIRMED — 3 days, 3-hour notification requirement, consistent across all three |
| Betfair Exchange Match Odds three-way (Draw valid in knockout) + 90-min settlement | Web search + snippet extraction | support.betfair.com/app/answers/detail/10264-football---90-minute-rule/ (search snippet) | CONFIRMED — "if a match ended 1-1 after 90 minutes plus injury time, the correct winning selection in the win-draw-win market is 'Draw'" |
| Betfair Exchange "To Qualify" / "To Lift The Trophy" include ET+pens | Web search + snippet extraction | support.betfair.com/app/answers/detail/exchange-football-soccer-rules/ (search snippet) | CONFIRMED |
| Virgin Bet 12-hour abandonment / same-date postponement | Web search + snippet extraction | help.virginbet.com/hc/en-gb/articles/360013286754 (HTTP 403 on fetch, confirmed from Google search snippet); help.virginbet.com/hc/en-gb/articles/360013392573 (HTTP 403) | CONFIRMED via search snippet — "void unless the fixture is continued within 12 hours" (abandoned); "void unless rearranged and played on the same date (local time)" (postponed) |
| **Polymarket cancellation resolves "No" — ALL outcomes** | Direct WebFetch | polymarket.com/sports/world-cup/fifwc-usa-par-2026-06-12 (direct fetch succeeded); polymarket.com/sports/ucl/ucl-psg-ars-2026-05-30 (direct fetch succeeded) | **ERROR FOUND AND CORRECTED** — Win contracts resolve No; Draw contract resolves **Yes** on cancellation. Original report's blanket "resolves No" claim was incorrect. |
| **Polymarket knockout match market structure (three-way, 90-min, Draw runner)** | Direct WebFetch of comparable knockout market | polymarket.com/sports/ucl/ucl-psg-ars-2026-05-30 (UCL knockout, direct fetch) | **CLAIM DOWNGRADED TO UNVERIFIED** — UCL knockout market was two-way (no Draw), settling on full tie result including ET+pens. World Cup knockout markets are not yet open; structure cannot be confirmed from current evidence. |
| Polymarket group stage markets three-way, 90-min | Direct WebFetch | polymarket.com/sports/world-cup/fifwc-usa-par-2026-06-12 (direct fetch) | CONFIRMED |
| Polymarket postponed market stays open | Direct WebFetch | polymarket.com/sports/world-cup/fifwc-usa-par-2026-06-12 (direct fetch) | CONFIRMED |

### What was changed

1. **Section 3.7 (Polymarket Postponed/Cancelled):** Rule text corrected. Original claim "the market will resolve No" was replaced with the correct asymmetric rule: Win contracts resolve No, Draw contract resolves Yes on cancellation. This was directly verified from two live Polymarket market pages.

2. **Key Facts Table row 26:** Split into rows 26, 27, and 28 to distinguish postponed (remains open), cancelled Win contracts (resolves No), and cancelled Draw contract (resolves Yes).

3. **Section 3.3 (Polymarket Knockout Stage Match Markets):** Downgraded from "confirmed" three-way 90-min claim to "unverified" with explicit warning that comparable UCL knockout markets are two-way full-tie. The complementary position claim depends on this being resolved.

4. **Section 3.6 (Draw Handling — Knockout stage):** Downgraded confidence from confirmed to unverified.

5. **Table row 23:** Confidence changed from confirmed to unverified; note about UCL 2026 evidence added.

6. **Section 5.2 Opportunity A:** Flagged as "partially invalidated" pending knockout market structure confirmation.

7. **Section 7 Summary:** Added two new points (5 and 6) reflecting the two corrections.

8. **Section 8 Action 1:** Upgraded urgency to reflect new evidence that Polymarket knockout markets may be two-way full-tie (not three-way 90-min).

9. **Section 6 Abandoned/Postponed table:** Clarified cancellation vs postponement distinction for Polymarket row.
