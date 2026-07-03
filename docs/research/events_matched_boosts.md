# Match-Events Modelling Review + Matched-Betting / Boost-Extraction Plan
**Date:** 2026-07-03 (R32 finishing, R16 starts 2026-07-04) · **Author:** research agent
**Repo:** `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha` (src at parity with origin/main for all modules reviewed; `site/promos_data.json` read from origin/main)
**Data sources used:** repo src/tests, origin/main site feeds, mini `~/World-Cup-26` read-only (`data/wca.db` PRAGMA query_only, `site/*.json`, `data/model_predictions_log.jsonl`, `data/raw/results.csv`). Every number below is from a computation or query run today; n stated throughout.

---

## 1. Match-events modelling stack — as it EXISTS

### 1.1 The engines (all pure, all shipped)

| Module | Markets | Calibration (per docstrings/constants) | Status |
|---|---|---|---|
| `src/wca/models/props.py` `CornersModel` | match & team corners O/U | base 8.97/match, NB k=157.5 (near-Poisson), xG elasticity 0.30; team EB opt-in (league team mean 4.484); StatsBomb WC18+22, **n=128 matches** | shipped, tested |
| `props.py` `CardsModel` | match total cards | base 3.41, NB k=6.9; aggression = (fouls/14.262)^0.5 (fouls↔cards r=0.508); ref_factor hook (default 1.0) | shipped |
| `props.py` `FoulsModel` | team/player fouls | team mean 14.262, k=20.4 (exact MoM, WC18+22) | shipped |
| `props.py` `ShotsOnTargetModel` | team/player SoT | base shots 12.5 (exact), **on_target_ratio 0.345 is an EXTERNAL literature prior** — SoT absent from the StatsBomb props pull | shipped, honest caveat in code |
| `props.py` `AnytimeScorerModel` | anytime/first scorer | Poisson thinning of non-pen lambda; pen_xg 0.18 to designated taker | shipped |
| `models/scorers.py` `ScorerPricer` | anytime/first/brace/hat-trick **+ `double_delight_ev`** (Betfred DDHH boost EV: effective multiplier = P(exactly1)+2·P(brace)+3·P(3+) conditional on scoring) | wraps AnytimeScorerModel; `data/players.json` analyst npxg_shares (40,849 bytes, source-flagged estimates) | shipped — the boost-EV maths the offer desk needs already exists |
| `models/betbuilder.py` | team totals (goals/shots/SoT/fouls/corners), player SoT/fouls/to-be-booked, `fixture_betbuilder()` one-stop payload, `ev_vs_offer()` net-of-fee EV | shots/SoT/fouls priors "order-of-magnitude WC values pending refit"; `RateStore` → players.db → priors | shipped |
| `models/playerprops.py` | PM player-prop grid (goals/shots/SoT/assists k+, Poisson) | rate order: players.db → players.json → modest priors; **dominant uncertainty = lineup/minutes, no live lineup feed** (stated in code) | shipped; live join in `scripts/wca_player_props.py` |

### 1.2 Production reality check (what the models actually run on)
- `data/processed/prop_priors.csv` — **absent on BOTH boxes** (dev `data/processed/` has only props_matches/props_players csv; mini has neither). ⇒ every props model runs on the hard-coded WC18+22 fallback constants. Verified by `ls` on both machines today.
- Mini `data/players.db` is **0 bytes** (Jul 1). ⇒ in production, player rates degrade to `data/players.json` overrides + modest priors, exactly as the fallback design intends — but nothing sharper.

### 1.3 Priced-from-model vs market-only vs no-price-series
Query: `SELECT source, market, COUNT(*), MAX(ts_utc) FROM odds_snapshots GROUP BY 1,2` on mini `data/wca.db` (2026-07-03):

| market | rows captured | last capture | model? | CLV measurable? |
|---|---|---|---|---|
| h2h (1X2) | 2,693,131 | 2026-07-03T08:41 | yes (Elo+DC blend) | **yes — the only market with working CLV** (avg CLV −2.65%, 40.5% beat close, n=42, site/data.json) |
| totals | 439,020 | 2026-07-03T08:41 | yes (same DC lambdas) | series exists but **card doesn't read it** — bet_recs coverage: "BTTS/totals: withheld — no live book price" (the builder reads only h2h; matches the OddsAPI-utilization finding) |
| btts | 165,650 | 2026-07-03T08:41 | yes (same DC lambdas) | same as totals — captured, unused |
| draw_no_bet | 37,344 | **2026-06-21 (capture stopped)** | derivable | dead series |
| corners, cards, fouls, team/player SoT, scorer markets, SGM/bet-builder combos, PM player props | **0 rows** | — | yes (all of §1.1) | **NO price series ⇒ no CLV ⇒ these models have never produced a measurable bet.** |
| polymarket 1X2 (venue) | **0 rows with source='polymarket'** | — | — | `pm1x2snapshot.py` shipped to close this gap, but no rows have landed on the mini; scheduling fix is the current branch (`fix/pm1x2-snapshot-scheduling`) |

### 1.4 What the card/bot currently withholds (bet_recs.json meta, generated 2026-07-02 06:16 UTC)
`meta.coverage` verbatim: corners/cards "**calibrated but withheld — no live sportsbook price snapshot**"; player scorer "**withheld — player xG-share not yet wired**"; BTTS/totals "**withheld — no live book price**". Withheld list n=20: 11 × 1X2 floor/minnow exclusions, 8 × corners/cards (4 fixtures × 2), 1 × blanket scorer-props row. `event_props` array: n=0. The bot's `/boost` path prices free-text boosts vs the scores feed (`wca/boosts.py`, supported: 1X2/totals/BTTS/correct-score; player props explicitly unpriceable) but **deliberately never persists** (bot/app.py:1114 "priced, never logged").

**Bottom line §1:** the events stack is a well-built pricing library wired to nothing that can measure it. Only 1X2 produces CLV. The single cheapest unlock is reading the already-captured 439k totals + 166k BTTS rows (devig vs the same DC lambdas; per the Phase-2 verdict, size totals+BTTS+1X2 as ONE exposure per fixture — same lambda). Corners/cards/scorers stay display-only until any sportsbook price capture exists; their only monetizable outlet TODAY is manual boost evaluation (`/boost`, `double_delight_ev`) and bet-builder construction for boost locks (§3).

---

## 2. Boost / offer inventory NOW (R16 window)

### 2.1 Feed & scrape honesty
`site/promos_data.json` (origin/main, generated 2026-07-02 11:18 UTC): 12 sites tracked. Scrape health: **ok = Virgin Bet, Ladbrokes, Matchbook only**; 403-blocked = Paddy Power, Sky Bet, bet365, William Hill, Betfair SB, Betfair Exchange; 200-but-blocked = Smarkets, Unibet, Polymarket. Mini `promotions` table: 37 rows, **all first_seen = last_seen = 2026-06-21T11:59:45** (recon-seeded, never re-verified by a successful scrape). `promo_snapshots`: 816 rows, daemon still running (last 2026-07-03T08:41:50) but every "ok" fetch found n=0 promos. `boost_evals`: **0 rows** (mini AND site feed) — no boost has ever been persisted. ⇒ everything below is *recon-catalog, 12 days unverified*: treat titles as standing features, verify prices in-app.

### 2.2 Returning-customer / existing-account items relevant to R16 (A1 = user)
- **bet365** (A1 per routing rule): *Bet Builder Boost 25%* winnings on WC builders (cap "typically £25–50"); *Super Boosts* (max £1–5); *2 Up early payout*; *Sub On Play On*. bet365 prices are NOT in our TheOddsAPI board (19 books, no bet365) — any bet365 number below is a proxy, marked INDICATIVE.
- **Paddy Power**: *Power Prices* daily boosts; *2 Up*; *Super Sub*; *Rewards Club* (~£50/wk free bets for weekly betting). Golden Boot £1-FB-per-SoT: qualifier window June 1–17 — **EXPIRED**.
- **William Hill**: *Epic Boost* daily (cap ~£25); *Daily Odds Boosts*; *Acca Winnings Boost* up to 25%; *2 Up*.
- **Sky Bet**: *Acca Edge/Freeze*; *£5m England Jackpot* (min £5 WC outright @1/2+ — England alive, trivial cost lottery); *Sky Bet Club* (£30/wk → FB).
- **Betfair Exchange**: no promos; commission note — new accounts default 5%, **switch to Basic 2%**.

### 2.3 A2 (Mum) state — mini `sb_offers` (n=2, both A2)
1. **Betfair** signup (2026-06-12): qualifier done (USA @2.18 — noted UNDER-laid ~£1.40), £2 free acca punted, settled Jun 13–14. Closed except any top-up lesson.
2. **Betfred** bet-£10-get-£30-SNR + £20 bet-builder tokens, status `qualified` 2026-06-12. Feed says Betfred FB expiry = **7 days ⇒ tokens very likely expired unused (~£20–30 of value lost).** VERIFY in app immediately; this is the concrete cost of having no expiry tracking.

### 2.4 Untouched sign-up pipeline (A2 only, promo extraction)
30 catalogued offers in `signup_offers`. UK-relevant unopened majors: Sky Bet £50 (promo runs to 19 Jul), Ladbrokes £50, Paddy Power £40 (code YSKATF; separate £50-Bet-Builders offer YSKASP), Betway £40, BetMGM/Bet UK £40 each (linked platform — space them out), kwiff/talkSPORT/Tote £40, SBK £40, Coral £30, bet365 £30, William Hill £30, Unibet £30, Virgin £30, Midnite £30, Parimatch £30, BoyleSports £40, Betano £20+boosts, Betfair SB £30 acca-only, Easybet £30, QuinnBet £50 cashback. Crypto/Bahrain: Cloudbet rakeback (cash, no rollover), BC.Game (high wagering — skip), Kalshi (US-only — skip). Polymarket $50 deposit bonus (code unverified).

### 2.5 PM advancement (the SECONDARY-mandate hedge venue) — currently BLOCKED for fresh numbers
- `site/advancement_data.json` on mini regenerated 2026-07-03 08:46 **but `n_pm_markets: 0`** (no PM prices joined) and the sim inputs are stale: mini `data/raw/results.csv` ends **2026-06-27 with NA scores** (checked today) ⇒ R32 results not folded in (e.g. England shows reach-R16 = 0.8647 while the Mexico–England R16 tie is already fixed in scores_markets.json). `data/advancement_latest.json` file date Jun 18.
- Last captured PM advancement prices: bet_recs.json 2026-07-02 06:16 (27 rows, e.g. Belgium reach-QF model 0.4857 vs PM 30.5¢, ev_net +17.4%, stake $65.50). **All INDICATIVE — pre-R32-completion probabilities on both legs.** Per the standing hygiene memory: re-run advancement + pull fresh PM before acting.

### 2.6 Live 1X2 board (fresh: mini odds_snapshots batch 2026-07-03T08:41, 19 books incl. smarkets/betfair_ex/matchbook)
R16 window (exchange back px; lay ≈ +1–2 ticks, stated where used):

| Fixture (KO UTC) | Exchange H/D/A | Best fixed-odds book |
|---|---|---|
| Australia–Egypt R32 (07-03 18:00) | 3.85 / 2.96 / 2.44 | Aus 3.85 smk; Egy 2.45 leovegas |
| Argentina–Cape Verde R32 (07-03 22:00) | 1.16 / 9.2 / 27.0 | — |
| Colombia–Ghana R32 (07-04 01:30) | 1.46 / 4.6 / 9.8 | — |
| Canada–Morocco R16 (07-04 17:00) | 5.7 / 3.6 / 1.81 | Mor 1.80 betfred/coral/lads/leo/unibet |
| Paraguay–France R16 (07-04 21:00) | 21.0 / 7.8 / 1.20 | — |
| Brazil–Norway R16 (07-05 20:00) | 1.92 / 3.75 / 4.6 | Bra 1.90 betfred; Nor 4.33 pp; Draw 3.75 betfred |
| Mexico–England R16 (07-06 00:00) | 3.25 / 3.3 / 2.52 | Eng 2.48 casumo / 2.43 virgin·livescore / 2.40 betfred·pp |
| Portugal–Spain R16 (07-06 19:00) | 4.2 / 3.65 / 2.0 | Por 4.1 casumo/grosvenor |
| USA–Belgium R16 (07-07 00:00) | 2.76 / 3.45 / 2.82 | — |

Model (fresh, `model_predictions_log.jsonl` 07-03): e.g. Brazil–Norway λ 2.25/0.84 (model 1X2 .596/.239/.165); Mexico–England λ 1.10/1.68 (.243/.287/.471); Canada–Morocco λ 0.77/1.37 (.215/.297/.488). Desk-canonical blended edges (bet_recs 07-02): Brazil home +5.6% @1.932, Colombia home +2.8%, Australia home +3.8%.

---

## 3. Matched-bet construction playbook

Ledger sizing base (computed): **B = £3,000 + £177.10 GBP settled + $115.60/1.33 = £3,264** (site/data.json totals_by_currency, n=212 bets). Quarter-Kelly on B; promo caps almost always bind first (max cap £50 = 1.5% of B).

### 3(a) Boost locks — shipped engine `wca.boostlock` (NOTE: zero importers outside tests — manual/REPL only today)
Construction: anchor = Team FT win (90'); implied legs = "over 0.5 team goals" + "double chance" ⇒ builder ≡ anchor ⇒ exchange lay is an EXACT hedge. Money math (module-verified, invariant-asserted):
`S = B·((o−1)(1+b) + 1)/(L − c)`, `locked = S(1−c) − B`. `promo_max_stake` clamps; non-implied legs flag `equivalent=False`.

**Worked example 1 (run through `build_lock` today):** bet365 25% Bet Builder Boost, England anchor (Mexico–England R16, 90'):
- Builder 3 legs @ 2.40 (bet365 price proxy = betfred/PP 2.40 — bet365 not captured, **INDICATIVE**), boost 0.25, stake £50→**clamped £25**, lay England @2.54 Smarkets 0% (back 2.52 + 1 tick).
- LAY £27.07 ⇒ **LOCKED +£2.07 both ways = 8.3% of stake.** Settlement basis: 90' both legs ✓.

**Worked example 2:** same boost, Brazil anchor (Brazil–Norway): builder 1.90, lay 1.94 ⇒ lock +£2.38 (9.5%) **but module flags quoted combo 1.90 < promo min 2.0** ⇒ must add the least-distorting real leg (e.g. Norway under 3.5 team goals, P(Nor≥4)≈e-tail of λ=0.84: 1−CDF(3)=0.9%) and accept `equivalent=False` with that ~0.9% residual.

**Worked example 3 (plain boost, not SGM):** WH Epic Boost pattern, Morocco 1.81→2.00 (INDICATIVE uplift, cap £25), lay 1.83 Smarkets: `qualifying_bet(2.00,1.83,25)` ⇒ lay £27.32, **locked +£2.32 (9.3%)**. Rule of thumb: any boost ≥ ~10% above the exchange lay locks ~boost−spread.

### 3(b) Plain matched pairs — `wca.matched` (measured on today's board)
Qualifying losses per £10 (lay=back+1–2 ticks): draw Bra–Nor 3.75/3.79 **−£0.11 (−1.1%)**; Morocco 1.80/1.83 −£0.16; Brazil 1.90/1.94 −£0.21; England 2.40/2.54 −£0.55; Portugal 4.00/4.25 −£0.59. ⇒ qualify on draws/tight favourites.
SNR free-bet retention (per £30, computed): draw Bra–Nor **72.6%**; Canada 4.60/5.8 62.1%; Mexico 3.00/3.27 61.2%; Norway 3.75/4.7 58.5%; England 2.40/2.54 55.1%. **Desk anchor: 55–73%, use 65% mid for planning.**

**When PM NO beats the Smarkets lay.** PM taker fee = 0.03·p·(1−p) per share (advancement.py:570) ⇒ ≤ **0.75% of notional** (peak p=0.5) vs Smarkets 0% + spread, Betfair 2–6% on winnings. Use PM NO when: (i) the exchange lay spread exceeds ~1.5% (PM fee + PM spread often tighter on US-facing games), (ii) you want the hedge in USD (PM pool, no GBP FX), or (iii) the SB leg itself settles on advancement. Equivalence for calculators: buying N shares of NO @ q ≡ laying at L = 1/(1−q), lay stake = N(1−q), liability = N·q, effective commission ≈ 0.03·q.
**SETTLEMENT WARNING (every construction must carry a basis tag):** SB 1X2 / win-anchored builders = **90'**. Smarkets/Betfair match odds = **90'** ✓. PM per-match team-win + draw markets are 3-way (polymarket_odds.py) which implies 90' — **verify each market's resolution text before first live use**. PM *advancement* = **incl. ET+pens** ✗ for 90' hedges: divergence probability = model P(draw at 90') = **0.287 Mexico–England, 0.280 Portugal–Spain, 0.269 USA–Belgium** — a ~27–29% chance the "hedge" and the bet decouple. Matched pairs MUST be settlement-aligned; PM advancement NO may only hedge advancement-settled SB bets ("to qualify").

**2-Up early payout overlay (PP/bet365/WH), Monte Carlo n=400,000 per fixture on fresh lambdas:** P(2-up at some point AND not win 90') = Brazil 1.16%, England 1.57%, Morocco 0.78%. In a matched pair the overlay pays BOTH sides. Computed EV: Brazil back £25@1.90 betfred(2Up? — betfred not a 2Up book; via PP@1.85 it's thinner)/lay 1.94: base −£0.52, overlay +0.0116×£47.98 ⇒ **≈ break-even instead of −2%**; England @2.40 PP / lay 2.54: −£1.38 + 0.0157×£60.0 ⇒ −£0.44 (−1.8% instead of −5.5%). ⇒ 2-Up books make qualifiers ~free; not a standalone edge at current spreads.

### 3(c) UNMATCHED (naked) allowance rule — concrete proposal
Let o_eff = boosted effective odds, p = desk blended prob (card blend), edge = p·o_eff − 1, lock% = best settlement-aligned locked profit.
**Run a boost/offer naked iff ALL of:**
1. **p ≥ 0.25** (likely-PnL moneyline bucket; evidence: <25% longshots went 0/20 on PM, correct-score punts −73.9% — attribution memory);
2. **edge ≥ 8%**, decomposed from measured desk numbers: 3% margin buffer (Smarkets 0-comm rule) + 2.65% measured execution-CLV drag (avg CLV −2.65%, n=42) + ~2.35% model/price-staleness buffer;
3. **edge ≥ lock% + 4%** (naked must beat the guaranteed lock by ≥ half a typical lock — otherwise take the lock);
4. **stake = min(promo cap, 0.25·edge/(o_eff−1)·B)**, B=£3,264 — at promo caps ≤£50 worst-case is ≤1.5% of B, so the hard cash floor is never threatened by a single boost.
Worked test: bet365 25% England builder, o_eff = 1+1.40×1.25 = 2.75; blended p ≈ 0.6×devig(0.394) + 0.4×model(0.471) = **0.425**; edge = +16.8% ≥ 8% ✓; ≥ lock 8.3%+4% ✓; p 0.425 ≥ 0.25 ✓ ⇒ naked permissible; quarter-Kelly £78 → **clamped to promo cap**. (Lock remains the default when in doubt — it's 8.3% guaranteed.)
**Free bets: default LOCK.** Naked SNR needs p(o−1)·face > locked retention (~0.65·face): England @2.52 gives 0.42×1.52 = 0.64 — below; only >+25%-edge selections clear it. Exception (user feedback memory): small free-bet longshot punts acceptable — cap at ≤25% of weekly free-bet face.

---

## 4. Cross-account routing (standing rule, unchanged)
- **A1 (user):** all bet365 activity incl. Bet Builder Boost / Super Boosts / 2Up; model-edge cash goes to exchanges (Smarkets 0%) — keep SB accounts promo-only to slow gubbing.
- **A2 (Mum):** promo extraction ONLY — the §2.4 sign-up ladder + free-bet locks. Betfair & Betfred already burned (06-12).
- `argmax(model_EV + promo − gub)` is **NOT implemented in code** (no such module; sb_offers is a manual log). It stays a manual decision at placement time. Bahrain/crypto items (Cloudbet, Polymarket bonus) route to the user's crypto side per jurisdiction memory.

## 5. Ranked extraction opportunities NOW + build-next

### Top opportunities (ranked by locked £ per unit effort; INDICATIVE where prices are proxies/stale)
1. **A2 sign-up ladder over the R16–QF window** — computed at measured retention (55.1–72.6%) and draw-qualifier loss (−1.1%): Sky Bet £50 → net **£27.4–36.2**; Ladbrokes £50 → £27.4–36.2; PP £40 (YSKATF) → £22.0–29.0; Betway £40 → £21.9–28.9; SBK £40 → £21.8–28.8; kwiff/talkSPORT/Tote £40 → ~£19.7–26.0 each; BetMGM £40 + Bet UK £40 (linked — separate weeks) → ~£18.6–24.6 each; Coral/WH/Unibet/bet365/Midnite/Parimatch £30 → ~£16.4–21.7 each; Virgin/Boyle → ~£14.8–19.5. **Pipeline total ≈ £404–477 (mid £477 across 22 offers; ex-Betfred/Betfair already used).** Pace 1–2/matchday; prefer draw lays (tightest spreads, min-odds compliant).
2. **Verify the Betfred £30 SNR + £20 BB tokens on A2 TODAY** (qualified 06-12, 7-day expiry ⇒ likely expired). If alive: draw Bra–Nor lock = **+£21.77 guaranteed** on the £30 (computed), BB tokens via win-anchored builder ≈ 50–60%.
3. **bet365 25% Bet Builder Boost lock, every matchday (A1)** — England-anchor worked example: **+£2.07 locked on £25 (8.3%)** INDICATIVE (b365 quote needed); or naked at +16.8% EV under the §3(c) rule. At ~1 boost/matchday × remaining ~14 matchdays ≈ £30–60 locked or ~£100+ EV naked.
4. **WH Epic Boost / PP Power Prices matched pairs (A1/A2)** — template locks ~(boost − spread) ≈ 9% of cap when uplift ≥10% over exchange (+£2.32/£25 worked). Requires manual price capture via `/boost` — persist it (build item 1).
5. **PM advancement singles** — last honest numbers (07-02): Belgium QF +17.4% ev_net $65.5, Brazil SF +13.9%, Brazil QF +12.8%, Paraguay QF +12.6% — **BLOCKED: re-run advancement (results.csv 6 days stale, NA scores) + fresh PM pull before staking; treat all as expired quotes.**
6. **2-Up qualifier routing** — do every qualifying/matched back at a 2Up book when spread ≤ ~2%: overlay worth +1.2–1.6% of combined payout (MC n=400k), turning −2% qualifiers ~free. Not standalone.
7. **Sky Bet Club + PP Rewards Club (recurring)** — £30/wk turnover → FB; fold qualifying bets into the ladder anyway. Kane-goals FB rides with the Sky Bet sign-up (England alive; pens don't count).
8. **£5m England Jackpot** — £5 outright @1/2+ = cheap lottery alongside item 1's Sky Bet qualifier.

### Build-next (to make this repeatable)
1. **Persist manual boost evals**: `/boost` already prices; `promos.record_boost_eval` already exists; wire bot → `boost_evals` (source='bot'). Today: 0 rows ever.
2. **/boostlock bot command**: `wca.boostlock` has zero importers. Wire `build_lock`+`format_lock` to the bot with a small promo-terms registry (site, boost_frac, promo_max_stake, min_combined_odds) so every boost gets an instant lock plan; log to sb_offers.
3. **Read the captured totals/BTTS** (604,670 unused rows): devig O/U 2.5 + BTTS vs DC lambdas in the card; size fixture-correlated markets as ONE exposure. This is the cheapest new CLV-measurable market.
4. **Expiry tracking on sb_offers/free bets** (`expires_utc` + bot nag) — the Betfred lesson is ~£20–30 real money.
5. **Advancement freshness gate**: repoint results source (results.csv 6d stale, NA scores), re-run sim + PM join pre-R16; land the pm1x2 snapshot scheduling fix so PM becomes a CLV-benchmarkable venue.
6. **Any props price capture** (single-book corners/cards scrape or manual snapshot command) — until one exists, the entire §1.1 props stack generates zero measurable bets.
7. **Lineup/expected-minutes feed** for player props (stated dominant uncertainty in playerprops.py).
