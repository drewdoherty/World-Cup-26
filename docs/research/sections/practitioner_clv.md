# Practitioner Evidence: Closing Line Value and Betting Operations

**Research scope:** CLV as an edge proxy; predictive power of CLV for long-run profit; line movement and steam; optimal bet timing; Pinnacle/exchange close as the efficiency benchmark; documented soft-book account restrictions; sample sizes needed to distinguish skill from luck at typical football edges.

---

## 1. Dixon, M. J. & Coles, S. G. (1997)

**Full citation:** Dixon, M. J., & Coles, S. G. (1997). Modelling association football scores and inefficiencies in the football betting market. *Journal of the Royal Statistical Society: Series C (Applied Statistics)*, 46(2), 265–280. https://doi.org/10.1111/1467-9876.00065

**Summary:** The foundational peer-reviewed paper for quantitative football modelling and the direct ancestor of the Dixon-Coles component in the v1 system. The authors fit a bivariate independent Poisson model with attack/defence parameters to English league and cup data (1992–1995), adding a low-score correction factor (the "rho" parameter) for 0-0, 1-0, 0-1, and 1-1 scorelines where independence fails. They then test the model against season-end 1995–96 bookmaker fixed odds by betting whenever model probability exceeds implied bookmaker probability by a threshold, generating statistically significant positive returns.

**Methodology:** Poisson regression with club-level attack and defence parameters estimated by maximum likelihood on ~4 seasons of English football. A Dixon-Coles correction term adjusts cell probabilities for very low scorelines. Betting simulation tests strategy at varying probability-advantage thresholds against actual bookmaker odds.

**Data:** English Football League seasons 1992–95 (estimation); 1995–96 season (out-of-sample test). Fixed odds sourced from bookmaker records.

**Key findings:**
- Positive abnormal returns demonstrated on the out-of-sample test season.
- Favourite–longshot bias confirmed in UK football markets, mirroring the horse-racing literature.
- The rho correction significantly improves calibration on low-scoring games (typically 3–5% of total games affect estimated margins meaningfully).
- No specific ROI numbers are published in the abstract; the paper demonstrates the principle rather than producing a stable back-tested yield.

**Limitations and sceptical notes:** The out-of-sample test covers a single season—insufficient to draw strong conclusions about sustained edge. The bookmaker data is pre-internet-era, from an era of much wider and less efficient spreads; replication in modern markets yields far smaller advantages. The model does not include time decay of historical performance (that extension came later with Koopman & Lit 2015 and related work). Over 500 academic citations by 2023 means this is heavily studied; most modern replications on post-2005 data confirm the original structural insight but find shrinking raw edge due to improved market efficiency.

**Relevance to v1:** This paper is the direct scholarly basis for the Dixon-Coles component. The time-decayed variant in v1 directly addresses the paper's static parameter limitation. The finding that low-score correction matters remains valid and should be retained. The original betting-threshold approach maps to the v1 logistic blend's value filter.

**Implementation ideas:**
- Retain the rho correction specifically for World Cup knockout matches, where 0-0/1-0 draws in 90 minutes are structurally more likely.
- Use the 1995–96 out-of-sample exercise structure as a template for v1 model validation: hold out a final year, measure ROI vs. Pinnacle-implied probabilities rather than raw bookmaker odds.

---

## 2. Shin, H. S. (1992, 1993) — The Shin model for insider-adjusted probabilities

**Full citation (primary papers):**
- Shin, H. S. (1992). Prices of state contingent claims with insider traders, and the favourite-longshot bias. *The Economic Journal*, 102(411), 426–435.
- Shin, H. S. (1993). Measuring the incidence of insider trading in a market for state-contingent claims. *The Economic Journal*, 103(420), 1141–1153.

**Summary:** Shin's two-paper sequence provides the theoretical foundation for the devigging method used as the market baseline in v1. The 1992 paper solves for equilibrium bookmaker pricing in the presence of a known fraction of informed ("insider") bettors and shows that rational bookmakers will systematically shade longshots shorter relative to their true probability—creating the favourite-longshot bias as a rational response to adverse selection rather than a bettor irrationality. The 1993 paper derives a closed-form estimator of the insider fraction z from observed market overround and demonstrates it on UK horse-racing data.

**Methodology:** Game-theoretic model of a competitive bookmaking market; equilibrium pricing derived analytically. Empirical validation through bookmaker odds data from UK horse racing.

**Data:** UK horse racing fixture data, 1987–1991.

**Key findings:**
- The favourite-longshot bias arises rationally when bookmakers protect margins against insiders; no behavioural irrationality required.
- The Shin z-parameter (estimated insider fraction) runs at approximately 2–4% for liquid UK betting markets.
- Štrumbelj (2014) later confirmed empirically that Shin-adjusted implied probabilities outperform simple multiplicative normalisation as probability forecasts, with lower Brier scores on football data.

**Limitations and sceptical notes:** Derived from horse racing; direct calibration to football markets requires re-estimation. The model assumes a specific competitive structure among bookmakers that is less applicable in the modern market-maker / sharp-book ecosystem. The insider interpretation is contested—"sharp bettor" is a more accurate modern label than "insider." Whelan (2025) notes that some estimates of z in sports markets may be upward-biased due to model misspecification.

**Relevance to v1:** The Shin devigging method is directly used in the v1 market-baseline component. The theory justifies why simple multiplicative normalisation overstates longshot probabilities and understates favourite probabilities, which would systematically bias the blended model.

**Implementation ideas:**
- Apply Shin devigging to all Pinnacle opening and closing odds before computing CLV.
- Note that for a 48-team World Cup with many mismatches (Group Stage), the insider fraction may be lower, and the Shin z may be small; a sensitivity test swapping Shin for multiplicative devigging on large mismatches (>3.5 favourite implied probability) is warranted.
- Use Štrumbelj (2014)'s Brier score benchmarks as a calibration quality target.

---

## 3. Štrumbelj, E. (2014)

**Full citation:** Štrumbelj, E. (2014). On determining probability forecasts from betting odds. *International Journal of Forecasting*, 30(4), 934–943. https://doi.org/10.1016/j.ijforecast.2014.02.008

**Summary:** A systematic empirical comparison of four devigging methods—additive (uniform), multiplicative, power (Kuypers), and Shin—applied to three sports (football, basketball, tennis) from multiple bookmakers. The paper directly asks which method produces the most accurate probability forecasts and how accuracy varies across bookmakers. This is the only full-scale peer-reviewed horse-race of devigging methods.

**Methodology:** Retrieve odds from multiple bookmakers for each sport, apply each devigging method, evaluate resulting probability estimates against outcomes using Brier score (mean squared error), log loss, and calibration curves. Cross-bookmaker comparison also included.

**Data:** Odds from multiple major European bookmakers over several seasons; specific sample sizes not publicly available in abstract but reported as "several thousand events per sport."

**Key findings:**
- Shin's method yields the best Brier scores of the four methods tested, across all three sports.
- The gap between Shin and multiplicative normalisation is small in absolute terms (~0.001–0.002 Brier score units) but statistically significant in the larger samples.
- Some bookmakers provide materially better probability forecasts than others: Pinnacle/sharp-book odds outperform recreational-book odds on all metrics.
- "Several bookmakers are significantly different sources of probabilities in terms of forecasting accuracy," confirming that bookmaker choice matters when constructing a market baseline.

**Limitations and sceptical notes:** Pre-2014 data; the liquidity and efficiency landscape has changed. The Shin advantage may be small enough to be irrelevant in practice compared to choosing the right bookmaker as baseline. The paper does not directly address closing vs. opening odds.

**Relevance to v1:** Validates the choice of Shin devigging over multiplicative normalisation in v1's market baseline. Also validates using Pinnacle as the bookmaker source for the baseline.

**Implementation ideas:**
- Use Shin devigging as the default but log Brier score comparisons with multiplicative normalisation across the tournament; if differences are negligible (<0.002 over 50+ games), simplify to multiplicative.
- Archive per-bookmaker deviation statistics to surface systematic biases in any soft-book line (e.g. Paddy Power, Sky Bet) that could be exploited.

---

## 4. Buchdahl, J. (2011, 2017) — Primary practitioner texts on CLV

**Full citations:**
- Buchdahl, J. (2011). *How to Find a Black Cat in a Coal Cellar: The Truth About Sports Tipsters*. High Stakes Publishing.
- Buchdahl, J. (2017). *Squares and Sharps, Suckers and Sharks: The Science, Psychology & Philosophy of Gambling*. Oldcastle Books.

**Summary:** These two books constitute the most rigorous practitioner-level treatments of CLV, statistical significance in betting, and market efficiency in English-language literature. "Black Cat" (2011) is a systematic demolition of sports tipster claims using t-tests, variance analysis, and sample size requirements—it remains the clearest non-academic treatment of how many bets are needed before a positive ROI becomes statistically credible. "Squares and Sharps" (2017) extends this to market psychology, the wisdom-of-crowds in efficient markets, and the Pinnacle-close as a near-perfect probability estimate.

**Methodology:** Statistical analysis of real-world tipster records using t-tests, standard deviation of returns, and simulation. The CLV discussion is grounded in the empirical observation that Pinnacle closing lines exhibit r² = 0.997 correlation between closing implied probabilities and observed outcome frequencies over 397,935 football matches.

**Data (for the CLV analysis):** 397,935 football matches priced by Pinnacle; personal betting records of ~20,000 wagers; a separate sample of 952 positive-EV football bets to test CLV.

**Key findings (with numbers):**
- Pinnacle closing odds achieve r² = 0.997 between implied probability and actual outcome frequency—an extraordinary degree of efficiency.
- For traditional profit/loss evaluation, standard deviation ≈ 1.0 per unit stake (at even money), requiring several thousand bets to reach p < 0.05 at a 5% ROI; specifically, Buchdahl estimates roughly 2,500–4,000 bets at typical football odds to reach conventional significance.
- CLV has standard deviation ≈ 0.10 per bet (the odds barely move relative to stake size), so "as few as 50 bets" can establish statistically significant positive CLV.
- In the 952-bet sample: 756 bets (79.4%) showed odds shortening by close; average shortening 3.94%; this result is "virtually impossible through random chance alone."
- Personal system: 3.4% actual ROI vs. 4.0% projected EV over ~20,000 bets—strong alignment confirming CLV's predictive validity.
- Closing line efficiency argument: bettors who beat the close by ≥2% on a sustained basis should be profitable long-run.

**Limitations and sceptical notes:**
- The r² = 0.997 statistic is for aggregate decile-level calibration, not individual match predictions. It should not be read as "odds are correct 99.7% of the time"—a misquotation that circulates widely online.
- The "50 bets to detect CLV significance" claim assumes a relatively stable and homogeneous edge; if CLV is noisy (edge varies per-bet), the number needed rises substantially.
- The 2% CLV threshold for long-run profitability is frequently cited but is a practitioner heuristic, not an academic finding. Actual breakeven depends on the soft-book margin being beaten, which varies.
- Buchdahl's practitioner authority is high, but neither book is peer-reviewed; methodology details are not replicable without the raw data.

**Relevance to v1:** The 50-bets-for-CLV-significance vs. 2,500–4,000-bets-for-profit-significance finding is critical for the World Cup project: 104 matches means at most ~104 betting events in the group stage, insufficient to make ROI statistically significant but more than enough for CLV. **This is why v1 must prioritise CLV as its primary KPI**, not raw P&L. The 3.94% average line movement finding provides a useful reference: if v1's edge vs. opening line is consistently less than the typical market movement, the model may not be adding much beyond replicating public information.

**Implementation ideas:**
- Record CLV for every bet placed (vs. Pinnacle close) and compute running mean CLV and z-score after each 50 bets.
- Set a session-level alert: if mean CLV drops below 0% over 100+ bets, pause and review the model.
- Use the 4% average market movement as a rough proxy: aim for bets placed at >2% above the likely close, not just above the opening line.

---

## 5. Buchdahl, J. (2015, updated 2017) — Wisdom of the Crowd betting system

**Full citation:** Buchdahl, J. (2015, updated 2017). *Using the Wisdom of the Crowd to Find Value in a Football Match Betting Market* [Working paper/blog analysis]. Football-Data.co.uk. Retrieved June 2026 from https://www.football-data.co.uk/The_Wisdom_of_the_Crowd_updated.pdf

**Summary:** A detailed practitioner study that operationalises the CLV argument into a concrete betting system: identify matches where the consensus of multiple bookmakers deviates materially from Pinnacle's de-vigged prices, and bet into the bookmakers whose odds are highest relative to Pinnacle. The system went live in August 2015 and has been tracked publicly since. Reported results: ~10.38% ROI over 2,544 bets at one tracking point (likely inflated due to early data), settling toward 5–6% ROI in longer samples (one user trial: 5.95% over 1,309 bets April–June 2018).

**Methodology:** For each match, compute de-vigged probability from Pinnacle. Identify bookmakers pricing at ≥X% above Pinnacle's implied probability. Bet at those bookmakers. Track ROI and CLV against Pinnacle close.

**Data:** Football results and bookmaker odds from football-data.co.uk; Pinnacle pricing from Pinnacle API. Data through May 2017 in the updated version.

**Key findings:**
- System produces positive CLV vs. Pinnacle close on ~79% of bets in the analysed sample.
- Long-run expected edge estimated at ~5% ROI (after soft-book overround).
- The system is directly vulnerable to account restrictions: soft bookmakers are the source of the better prices, and sharp action triggers limitation, typically within weeks.

**Limitations and sceptical notes:**
- The 10.38% ROI figure is likely a positive early-sample fluctuation; the 5–6% longer-run figure is more credible but still based on a limited ~2,500-bet record. 
- The analysis does not control for staking size vs. book limits, which in practice collapse rapidly once an account is flagged.
- Results are denominated as "bets placed"—a survivorship issue: some bets cannot be placed once limits hit.
- The updated PDF is from 2017; the market has continued to mature, and early-line advantages have likely compressed further.

**Relevance to v1:** This is the most directly analogous operational template to v1's intended workflow. The 5% long-run expected edge vs. a 5-6% soft-book margin implies bets must beat Pinnacle close by at least 1–2% net to clear the bookmaker margin—consistent with the 2% CLV threshold from Buchdahl (2017). The system also foregrounds the account-restriction problem as the primary operational constraint, not model accuracy.

**Implementation ideas:**
- Use Pinnacle API (or Betfair pre-match exchange close) as the baseline for all CLV calculations, not soft-book opening lines.
- Prioritise early placement (within 24–48 hours of Pinnacle opening the market) on World Cup group stage matches where model has largest divergence.
- Track ROI separately for bets beaten vs. Pinnacle close vs. bets not beaten—this disaggregation will reveal whether the v1 model is adding genuine pre-close signal.

---

## 6. Simon, J. R. (2024) — Inefficient Forecasts at the Sportsbook

**Full citation:** Simon, J. R. (2024). Inefficient forecasts at the sportsbook: An analysis of real-time betting line movement. *Management Science*, 70(12), 8583–8611. https://doi.org/10.1287/mnsc.2022.00456

**Summary:** A peer-reviewed Management Science paper using detailed intraday line movement data from four sportsbooks across 3,681 MLB games to directly test weak-form market efficiency in sports betting. The central finding is that betting lines overreact—line changes exhibit significant negative autocorrelation—meaning the market undershoots on initial moves and then partially reverses. Crucially, this is not merely a noise artefact: simple strategies exploiting the autocorrelation were ex-ante profitable.

**Methodology:** Collect moneyline data at high frequency (every few minutes) from opening to close at four sportsbooks. Test for autocorrelation in sequential line changes. Simulate "contrarian" strategies (bet opposite to recent large line moves) and evaluate profitability. Extended analysis to NFL, NBA, and NHL (2019–2023) to test generalisability.

**Data:** 3,681 MLB regular-season and post-season games (primary); additional NFL, NBA, NHL seasons (2019–2023) for replication.

**Key findings (numbers):**
- Price changes show statistically significant negative autocorrelation in all four sports (p < 0.01).
- Betting lines for weekend-day MLB games are significantly worse forecasts at game-start than they were 90 minutes before first pitch—the first-pitch lines are actually noisier than 90-minutes-prior lines.
- A simple contrarian strategy (bet opposite to large same-day line moves) produces statistically significant positive expected returns across MLB, NFL, NBA, and NHL.
- Effect is described as "broad characteristic of sports betting markets," not sport-specific.

**Limitations and sceptical notes:**
- The study is North American sports; whether football (soccer) markets exhibit the same negative autocorrelation is untested. Football markets are arguably more efficient, with sharper international action and less public bettor influence per-market.
- Transaction costs are not fully modelled; the contrarian returns may not survive realistic bid-ask spreads and limits.
- The weekend-day timing effect may reflect a specific pattern in US retail betting inflows and is not directly transferable to international football timing.
- The paper does not address football or World Cup betting specifically.

**Relevance to v1:** Provides academic backing for the bet-timing strategy of not placing bets at a single moment but monitoring for overreaction in Pinnacle's line after news events (injury announcements, team selection leaks). If a line moves sharply in one direction around squad announcement, it may temporarily overshoot. **However**, the football market is almost certainly more efficient than North American leagues in this respect—apply this insight cautiously.

**Implementation ideas:**
- Monitor Pinnacle World Cup lines intraday around key news events (official lineup releases, typically 60-75 minutes before kickoff for international tournaments).
- Flag any Pinnacle line move >1.5% within 30 minutes as a potential overreaction candidate; track whether fade strategies on these moves would have been profitable over the tournament.
- Do not place bets within 60 minutes of kickoff unless the model's edge is sustained and the line has not moved materially against the model's predicted direction.

---

## 7. Hvattum, L. M. & Arntzen, H. (2010)

**Full citation:** Hvattum, L. M., & Arntzen, H. (2010). Using ELO ratings for match result prediction in association football. *International Journal of Forecasting*, 26(3), 460–470. https://doi.org/10.1016/j.ijforecast.2009.10.002

**Summary:** The peer-reviewed academic validation of Elo-based strength ratings as input features for football outcome prediction. The authors convert Elo rating differentials into covariates for ordered logistic regression (predicting home win/draw/away win) and evaluate out-of-sample predictive power across English league football. The key contribution is demonstrating that dynamic, history-discounted ratings (i.e., recent performance weighted more heavily) improve forecasting accuracy over static ratings or league position.

**Methodology:** Elo ratings computed by a standard K-factor update rule; differences entered into ordered logit model; out-of-sample evaluation using log-loss and ranked probability score. Comparison against betting market implied probabilities as a benchmark.

**Data:** English football league seasons 1995–2009 (approximately 14 seasons across the top four tiers).

**Key findings:**
- Elo-based models outperform naive benchmarks (home advantage only, league position).
- Dynamic ratings (recent form weighted) improve over static.
- Betting market odds still outperform Elo-only models on raw calibration—the market incorporates more information.
- A blended approach (Elo + market) performs better than either alone.

**Limitations and sceptical notes:**
- The Elo system tested uses a simple K-factor with no home-advantage or goal-margin adjustment; WorldFootballElo and similar modern implementations substantially improve on this.
- English domestic league football is different from international tournament football in several important respects (no relegation pressure, home advantage structure, roster depth differences).
- The out-of-sample period (pre-2010) predates the modern sharp-book arbitrage ecosystem; the edge from Elo models has compressed.
- Not specifically designed for World Cup / international team ratings.

**Relevance to v1:** Direct academic validation of the Elo component in v1's model stack. The finding that Elo + market blend outperforms either alone supports the logistic blend architecture. The limitation around domestic vs. international football is particularly relevant: the v1 system uses an international Elo, which is better suited than club Elo for a World Cup context.

**Implementation ideas:**
- Ensure the international Elo used in v1 discounts results older than ~18–24 months substantially (per the time-decay rationale validated here and in Koopman & Lit 2015).
- Validate that the Elo component improves Brier score vs. the market-alone baseline before the tournament; if it does not, reduce its weight in the blend.
- Consider adding a separate rest/travel penalty parameter for teams arriving at the host venues (USA/Canada/Mexico) from very different time zones.

---

## 8. Statsbet.org / Betformatics (2024–2026) — Prediction market vs. bookmaker accuracy

**Full citation:** statsbet.org research team. (2026). *Prediction market vs. bookmaker accuracy: Betfair exchange vs. bet365 closing odds across top-5 European football leagues, August 2024–April 2026* [Data study]. Retrieved June 2026 from https://statsbet.org/blog/prediction-market-odds-efficiency

**Summary:** A contemporary empirical calibration comparison of Betfair Exchange and bet365 as CLV benchmarks using 1,635 matches across the Big Five European leagues over two full seasons. The study rigorously strips overround (multiplicative normalisation) and computes Expected Calibration Error (ECE) and Brier score for both platforms.

**Methodology:** Pre-match closing odds only (0–120 minutes pre-kickoff); contamination filter removes post-kickoff in-play prices; ECE computed using equal-sample deciles; separate analysis by league and season.

**Data:** 1,635 matches; N = 4,905 predictions per platform (three outcomes per match); August 2024 – April 2026.

**Key findings (numbers):**
- ECE: bet365 = 1.21%, Betfair = 1.72% (bet365 is marginally tighter by 0.51pp).
- Brier score: bet365 = 0.1935, Betfair = 0.1934 (functionally identical; difference < 0.0002).
- Both platforms exhibit the same favourite-longshot bias pattern: longshots (D1: 3–16% implied probability) are overpriced by ~3.5pp (bet365) and ~2.8pp (Betfair).
- Strong favourites (D10: 57–92% range) are underpriced by ~3.0pp (bet365) and ~1.8pp (Betfair).
- In the Premier League subset: strong favourites (45–81% implied probability) are overpriced by 5.9–6.0pp vs. actual 51.2% win rate (N = 201 matches)—a robust finding despite the small PL sample.
- Cost comparison: bet365 overround 5.6–5.7%; Betfair back-side overround 7.7–9.6% (before commission); on high-liquidity PL fixtures, Betfair narrows to ~3.7%.

**Limitations and sceptical notes:**
- The study is not peer-reviewed; methodology is well-documented but cannot be independently replicated without access to the underlying database.
- The ECE advantage for bet365 is marginal; the Brier score difference is negligible. Neither platform is decisively more efficient as a probability baseline.
- The favourite-longshot bias finding is consistent across both platforms, suggesting it is a genuine market structural feature rather than a bookmaker error. This implies v1's model should expect positive CLV on moderate favourites more easily than on longshots.
- PL sub-sample (N = 201) is too small for strong inference; World Cup group stage has a similarly small sample.

**Relevance to v1:** Directly answers the question of which platform to use as CLV benchmark. Since accuracy is essentially equal, the choice should be based on availability and the Pinnacle precedent in the literature. For the World Cup specifically, where Betfair may have deeper liquidity on top matches, using both as references is prudent. The favourite-longshot bias finding is important: the v1 model should be tested for whether it correctly avoids longshot bets where both the market and historical data suggest systematic overpricing.

**Implementation ideas:**
- Use Pinnacle (when available) as primary CLV benchmark and Betfair Exchange pre-match closing price as secondary cross-check.
- Log cases where Pinnacle and Betfair diverge by >1.5% (de-vigged) and investigate; these are unusual and may indicate a pricing error or rapid news event.
- Flag all strong favourites (implied probability >55%) as carrying the 5–6pp overpricing risk; ensure v1 model probabilities for such teams are not systematically lower than the market before attributing "value."

---

## 9. UK Gambling Commission / ukbookmakers.org (2025) — Account restriction evidence

**Full citation:** UK Gambling Commission data, cited in: ukbookmakers.org. (2025, July). *More than 600,000 UK punters have accounts restricted for winning too often*. Retrieved June 2026 from https://www.ukbookmakers.org.uk/2025/07/more-than-600000-uk-punters-have-accounts-restricted/

**Summary:** The most comprehensive data set on the prevalence and severity of account restrictions by UK-licensed bookmakers. Drawn from a UK Gambling Commission review of ~15 million active accounts, the data quantifies how many accounts face restrictions, the severity of stake limits, and—critically—reveals that over half of restricted accounts are not in long-term profit, exposing that restriction is not a precise surgical tool targeting winning bettors but a broad algorithmic profiling net.

**Methodology:** Regulatory review by UK Gambling Commission; aggregate statistics derived from operator submissions. Exact methodology not fully public.

**Data:** ~15 million active UK licensed bookmaker accounts; restriction data as reported by operators.

**Key findings (numbers):**
- 643,779 accounts (4.3%) face some form of restriction.
- An additional 2.23% of all active accounts permanently closed due to profitability concerns.
- 62% of restricted accounts face maximum stake limits, often in pence.
- 22.41% of restricted accounts capped to <1% of the average non-restricted stake size.
- 36.55% of restricted accounts in the 1–9% stake-factor range; combined, >50% of restricted accounts can bet at <10% of normal stakes.
- 51.69% of restricted accounts are eventually permanently closed.
- **Key paradox:** Only 46.78% of restricted accounts show long-term net profit; 53.22% are restricted despite lifetime net losses. The Gambling Commission confirmed it has "no power to stop bookmakers from restricting accounts."

**Limitations and sceptical notes:**
- The data is aggregate; it does not identify which bookmakers are most aggressive or what specific betting patterns trigger restrictions.
- The finding that >53% of restricted accounts are net losers is striking and suggests that the restriction algorithm is either imprecise (false positives) or targets bonus-/arbitrage-adjacent behaviour regardless of overall account profitability.
- No timeline data: the study does not reveal how quickly accounts are restricted after opening.
- Regulatory context is UK-specific (UKGC licensees); Sky Bet, Paddy Power, Virgin Bet, and Bet365 all operate under this framework.

**Relevance to v1:** This is the primary evidence base for the v1 operational constraint: **account longevity is the binding constraint, not model accuracy, for soft-book UK operators.** Bet365 in particular is described in practitioner communities as one of the most aggressive stake-factor operators, sometimes restricting within the first week of sharp activity. Quarter-Kelly staking is one mitigation but is insufficient alone; betting patterns (market selection, timing, bet size relative to opening market volume) matter.

**Implementation ideas:**
- Diversify across all four UK soft-book accounts (Paddy Power, Sky Bet, Virgin Bet, Bet365) to reduce per-account exposure.
- On each account, mix value bets with some market-price bets and avoid exclusively betting on markets where opening line diverges from Pinnacle (a clear arb signal).
- Never bet the same selection at multiple soft books simultaneously from accounts that share IP/payment methods.
- Plan for a usable soft-book window of perhaps 20–50 bets before limits hit on the sharpest accounts; prioritise highest-CLV bets early in the tournament.
- Betfair Exchange has no account restrictions for winning—use it as the unconstrained benchmark and for any bet too large for remaining soft-book limits.

---

## 10. Mo_Nbg (beatthebookie.blog) (2022) — Scoring functions vs. betting profit

**Full citation:** Mo_Nbg [pseudonym]. (2022, March 29). *Scoring functions vs. betting profit – Measuring the performance of a football betting model*. beatthebookie.blog. Retrieved June 2026 from https://beatthebookie.blog/2022/03/29/scoring-functions-vs-betting-profit-measuring-the-performance-of-a-football-betting-model/

**Summary:** A practitioner-level empirical analysis of whether standard statistical scoring metrics (Brier score, log-loss, accuracy) predict betting profitability. The study tests five football prediction models against Bet365 1×2 odds over 8,877 Big-5-League matches (2016/17–2020/21) and finds a systematic disconnect: the model with the best Brier score is not the most profitable, and the most profitable model has the worst Brier score.

**Methodology:** Five models (Zero-inflated Poisson, three xG-based Vanilla Poisson variants, XGBoost) compared. Flat-stake betting simulation (€1 per bet) where model probability exceeds implied bookmaker probability (value filter). Evaluated on Brier score, log-loss, accuracy, and net profit.

**Data:** 8,877 matches; 8,165–8,375 bets per model depending on value detection; Big Five European leagues (EPL, La Liga, Bundesliga, Serie A, Ligue 1), 2016/17–2020/21.

**Key findings:**
- Overall result: -€268.81 over 5 years at flat stake (average loss ~€0.03/bet)—all models lost marginally at Bet365 odds.
- The shortest-term Vanilla Poisson EMA10 model (highest weight on recent matches) was the least profitable on Brier score but least unprofitable in betting simulation.
- Metric rankings (Brier, log-loss, accuracy) did not correlate with betting simulation rankings.
- Conclusion: "Known scoring metrics do not correspond with the optimization goal of a betting model."

**Limitations and sceptical notes:**
- All five models lost money vs. Bet365 odds—the paper demonstrates difficulty of beating the market, which is the main finding, not a validation of any positive system.
- The flat-stake simulation against Bet365 (a soft book with 5.6% overround) is arguably the hardest possible test; results vs. Pinnacle or using CLV-filtered betting would differ.
- N=8,877 matches over 5 seasons is a reasonable but not large sample; results may be sensitive to specific seasons.
- Practitioner blog, not peer-reviewed.

**Relevance to v1:** Critical calibration warning for v1 development. **Do not optimise the v1 model on Brier score or log-loss alone and assume betting profitability will follow.** The v1 model should be specifically evaluated against market-relative metrics (CLV vs. Pinnacle close, Brier score vs. de-vigged Pinnacle probabilities rather than raw model output) and simulated via a value-filtered betting simulation, not a general prediction accuracy metric.

**Implementation ideas:**
- In v1 model validation, compute Brier score vs. Pinnacle de-vigged baseline (not vs. actual outcomes alone): a model that beats the market's calibration on Brier score is providing genuine value signal.
- Add a custom loss function to model training that weights errors proportional to the betting edge they imply (EV-weighted log-loss), rather than uniform log-loss.
- Post-tournament, run the correlation test: do the v1 model's high-confidence bets (large predicted edge) actually achieve higher CLV than low-confidence bets? A positive correlation confirms the model is well-calibrated for value detection.

---

## Synthesis Table

| Source | Type | Key metric | Numbers | Directly applicable to v1? |
|--------|------|-----------|---------|---------------------------|
| Dixon & Coles (1997) | Peer-reviewed | Positive returns vs. bookmaker | Not quantified in abstract | Yes — model architecture basis |
| Shin (1992, 1993) | Peer-reviewed | Insider z ≈ 2–4%; FLB explained | z-parameter | Yes — devigging method |
| Štrumbelj (2014) | Peer-reviewed | Shin best Brier score of 4 methods | ~0.001–0.002 Brier advantage | Yes — devigging validation |
| Buchdahl (2011, 2017) | Practitioner books | CLV detectable in 50 bets; r² = 0.997 | 3.94% avg. line movement | Yes — primary KPI framework |
| Buchdahl WoC (2015/17) | Practitioner study | ~5% ROI at soft books vs. Pinnacle | 5.95% over 1,309 bets | Yes — operational template |
| Simon (2024) | Peer-reviewed | Line overreaction; neg. autocorrelation | p < 0.01; NFL/NBA/NHL replicated | Partial — football TBC |
| Hvattum & Arntzen (2010) | Peer-reviewed | Elo + market blend > either alone | Ordered logit log-loss improvement | Yes — model architecture |
| statsbet.org (2026) | Data study | ECE: bet365 1.21%, Betfair 1.72% | 4,905 predictions; Brier ≈ equal | Yes — benchmark selection |
| UKGC / ukbookmakers (2025) | Regulatory data | 643k restricted accounts; 53% not profitable | 4.3% of 15M accounts | Yes — operational planning |
| Mo_Nbg / beatthebookie (2022) | Practitioner blog | Brier score ≠ betting profit | 8,877 matches | Yes — model evaluation warning |

---

## Practical Implications for v1

### 1. CLV is the right primary KPI — but understand its limits

The combined weight of Buchdahl (2017), Štrumbelj (2014), and the Trademate Sports practitioner evidence establishes CLV vs. Pinnacle close as the most efficient KPI available for a 104-match sample. Raw P&L requires 2,500–4,000 bets at a 5% edge to reach p < 0.05; the full tournament offers at most 104 events. CLV needs as few as 50 bets to confirm skill. **Target: mean CLV > +2% over the tournament's first 30 bets before scaling stakes; exit value threshold if mean CLV < +1% over 60 bets.**

However: CLV measures performance relative to the efficiency of the closing market, not absolute truth. If the closing market itself is mispriced (possible in Group Stage dead-rubber games with low Pinnacle liquidity), positive CLV may reflect noise. Apply a liquidity filter: treat CLV as reliable only when Pinnacle maximum bet size is ≥ $5,000 at close.

### 2. Bet early on v1 model signals with large opening-line divergence

The statsbet.org data and Simon (2024) both support that early lines incorporate less information. Buchdahl's wisdom-of-crowd analysis confirms ~3.94% average market movement toward the efficient price. If v1 identifies a 4–6% divergence from Pinnacle's opening line, bet within 24 hours of market opening rather than waiting for better price discovery. For smaller divergences (<2%), consider waiting to see if the line confirms the model's direction before placing.

### 3. Quarter-Kelly is justified but model uncertainty requires additional shrinkage

The Kelly literature (and the Wharton study) consistently finds that full Kelly produces unacceptable drawdowns when edge estimates are imprecise, which they always are in a new model. Quarter-Kelly is the practitioner consensus for a system with limited backtest validation. For v1 with a 104-game first-run sample, consider further shrinkage to 0.15–0.20 Kelly until the first 30-bet CLV record is established. Full Kelly should never be used with a v1 system.

### 4. Soft-book restrictions are the binding operational constraint, not model quality

The UKGC data (643,779 restricted accounts; some within weeks of opening) means the operational lifespan of Paddy Power, Sky Bet, Bet365, and Virgin Bet accounts is finite. Do not sacrifice CLV quality for account longevity by placing low-edge bets to "look casual"—this is documented to be ineffective and merely wastes the operational window. Instead: (a) spread bets across all four accounts; (b) use Betfair Exchange for stakes that exceed soft-book limits; (c) document all bets for post-tournament analysis before accounts close.

### 5. Calibration metrics and betting profit are not the same thing

The beatthebookie.blog (2022) analysis and the Hvattum & Arntzen (2010) finding that market + model beats either alone both point to the same conclusion: optimise v1 not on raw Brier score but on market-relative Brier score (model vs. Pinnacle de-vigged baseline). A model that reduces Brier error by 0.001 vs. the Pinnacle baseline over 104 matches is genuinely valuable; one that improves absolute Brier score by the same amount is not necessarily profitable. Set the calibration target as: at least 80% of v1 predicted probabilities should lie within ±3% of Pinnacle de-vigged closing probability (confirming calibration), while mean v1 probability on selected bets exceeds Pinnacle implied probability by ≥3% (confirming edge).

### 6. Be sceptical of the "bet on favourites" implication from the FLB finding

Both the statsbet.org data (strong-favourite overpricing of 5–6pp in PL) and the established favourite-longshot bias suggest longshots are systematically overpriced by bookmakers. However, this is partly absorbed into the Pinnacle market: the Betfair and bet365 bias patterns are structurally similar. Do not interpret the FLB as a simple "always back favourites" rule without confirming that v1's model probability for the favourite exceeds the Shin-de-vigged Pinnacle implied probability; otherwise the apparent FLB edge is already priced in.

---

*Document prepared June 2026. Sources accessed via web search 10 June 2026.*
