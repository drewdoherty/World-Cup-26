# Football Betting Market Efficiency: Annotated Literature Review

**Topic:** Market efficiency in football betting: favourite-longshot bias, closing line value, de-vigging methods, and whether quantitative models can beat closing prices.

**Scope note:** This section covers peer-reviewed empirical literature plus serious practitioner sources. Findings are assessed critically: dated evidence, market-structure changes (pre/post online markets, exchange emergence), and replication concerns are flagged explicitly. The target application is a v1 system combining international Elo, time-decayed Dixon-Coles, Shin-devigged market baseline, logistic blend, and quarter-Kelly staking.

---

## 1. Dixon & Coles (1997) — "Modelling Association Football Scores and Inefficiencies in the Football Betting Market"

**Citation:** Dixon, M. J., & Coles, S. G. (1997). Modelling association football scores and inefficiencies in the football betting market. *Journal of the Royal Statistical Society: Series C (Applied Statistics)*, 46(2), 265–280.

**Summary:** This is the foundational quantitative football-betting paper and remains the standard baseline against which almost all later modelling work measures itself. Dixon and Coles extended the independent Poisson model for goals by introducing: (a) an interaction (correction) term ρ to adjust probabilities for low-scoring scorelines (0-0, 1-0, 0-1, 1-1) that the independent model systematically underestimates, and (b) a time-decay weighting function so that recent matches contribute more to team parameter estimation than older results. The model estimates attack and defence ratings for each team plus a global home advantage. The paper demonstrated that a value-betting strategy — backing matches where the model's implied probability exceeded the bookmaker's implied probability by a sufficient threshold — produced a positive return on three English league seasons (1992/93–1994/95 for fitting; 1995/96 for out-of-sample validation). This was the first rigorous peer-reviewed demonstration of statistical model-based positive returns in the English football market.

**Methodology:** Maximum-likelihood estimation of attack/defence/home parameters via bivariate Poisson with ρ correction. Time-decay exponential weighting of match history. Value-bet selection when model price exceeds bookmaker implied price by a pre-specified threshold.

**Data:** English Football League (all four divisions), 1992–1996. Out-of-sample betting evaluation on 1995/96 season. Single bookmaker's fixed odds.

**Key findings:** The ρ correction improved calibration meaningfully on low-scoring scorelines. The value-betting strategy was reported to yield a statistically meaningful positive return in-sample and a modest positive return out-of-sample, though the exact ROI figures were not prominently reported in percentage terms — the paper stated the strategy "has a positive return" with specific bets identified via odds threshold criteria. Later replication work (Goddard 2005, Wilkens 2026) has broadly confirmed that Dixon-Coles style models can identify small edges, though the magnitude shrinks in modern, more-liquid markets.

**Relevance to v1:** Time-decay weighting is directly implemented in v1's Dixon-Coles component. The ρ correction is especially important for international football, which has lower average goal counts than domestic leagues. The original paper's value-betting threshold logic is conceptually equivalent to v1's logistic blend: bet only when model probability exceeds Shin-devigged market probability by a margin sufficient to justify the stake.

**Critical caveats:** Data are from 1992–1996 fixed-odds single-bookmaker markets with typical overrounds of 10–15%. Modern online bookmarkers show overrounds of 4–6% on major markets (Pinnacle ~2%), and the competitive environment has changed fundamentally since exchange markets launched (2000 onward). The positive return figures may not replicate at current margins. The low-score correction ρ was estimated on domestic league data; its calibration on international tournament football (often even lower-scoring due to defensive tactics) has not been validated in the original paper.

**Implementation ideas:** Keep the ρ correction, validated specifically on international matches. Use time-decay half-life as a tunable hyperparameter — shorter for international football (national team rosters turn over more than clubs). Cross-validate the value threshold on historical World Cup data before applying.

---

## 2. Kuypers (2000) — "Information and Efficiency: An Empirical Study of a Fixed Odds Betting Market"

**Citation:** Kuypers, T. (2000). Information and efficiency: an empirical study of a fixed odds betting market. *Applied Economics*, 32(11), 1353–1363.

**Summary:** Kuypers provides both a demand-side model of bettor behaviour and an empirical test of market efficiency in the English football fixed-odds market. His key theoretical contribution is showing that a profit-maximising bookmaker does NOT need to set market-efficient (probability-reflecting) odds. If bettors are systematically biased — for example, overestimating their home team's chances — the bookmaker maximises expected profit by setting prices that exploit those biases rather than by reflecting true probabilities. This is an important departure from the "balanced-book" hypothesis that was common in earlier literature. Empirically, using OLS regression of outcomes on bookmaker-implied probabilities, Kuypers initially finds weak-form efficiency for 1993/94 and 1994/95 English league seasons. However, using an ordered logit model incorporating additional publicly available information (match significance, team form), he finds evidence of inefficiency and identifies profitable betting opportunities in some sub-samples.

**Methodology:** OLS regression of binary outcome indicator on implied probability (weak-form test). Ordered logit with publicly available variables as semi-strong test. Data on one bookmaker's odds for English Football League 1993/94 and 1994/95.

**Data:** English Football League (four divisions), two seasons (1993/94–1994/95). Single bookmaker fixed odds.

**Key findings:** Weak-form efficiency broadly holds (no systematic exploitable bias in raw odds) but the bookmaker's profit margin is concentrated in longshot bets — favourites show a smaller margin — consistent with the favourite-longshot bias. The bookmaker's expected profit on favourites was close to zero; profit came disproportionately from longshots. Kuypers was also first to document that bookmakers may deliberately shade odds on teams with large supporter followings (e.g., Manchester United), in effect adding a "fan tax" on biased bettors.

**Relevance to v1:** The "profit-maximising bookmaker" framing matters for how we interpret market odds. Bookmaker prices are NOT purely probability estimates — they embed bettor-preference exploitation. Kuypers' findings imply: (1) de-vigging methods that assume uniform margin distribution (basic normalisation) will systematically misallocate margin; (2) the market is a better calibrator for mid-range probability events than for extreme outcomes; (3) for popular national teams (England, Brazil, etc.) in World Cup markets, expect bookmaker prices to shade against the bettors' home-nation bias.

**Critical caveats:** Data are from a single bookmaker in early 1990s — well before online competition, Betfair, or modern pricing technology. The finding of "weak-form efficiency" contradicts both Goddard (2004) and Wilkens (2026) who find persistent exploitable residuals using richer models. Kuypers' ordered logit result (suggesting inefficiency) used publicly available info circa 1999 — most of that information is now priced in far more quickly. The "fan tax" finding has not been rigorously replicated in international football contexts.

**Implementation ideas:** In the World Cup context, apply a scepticism discount to odds on large-following national teams (Brazil, Argentina, England, France). These may be shaded against biased home-nation bettors. Flag these as markets where the Shin-devigged price may still overstate the true probability assigned by a rational market participant.

---

## 3. Forrest, Goddard & Simmons (2005) — "Odds-Setters as Forecasters: The Case of English Football"

**Citation:** Forrest, D., Goddard, J., & Simmons, R. (2005). Odds-setters as forecasters: the case of English football. *International Journal of Forecasting*, 21(3), 551–564.

**Summary:** This paper provides the definitive benchmark study establishing that professional bookmakers are excellent forecasters of football match outcomes, and that their calibration improved significantly over the 1990s as competition intensified. The authors compare probabilistic forecasts derived from bookmaker odds against a statistical model fitted on historical results, using the Brier probability score as the primary calibration metric. Their core finding is that bookmaker odds outperformed the statistical model across nearly 10,000 English football games, and the accuracy gap widened in favour of bookmakers over the five-year study window — a trend they attribute to growing commercial pressure from competition forcing bookmakers to price more accurately. The Brier scores reported by the study (home win: 0.231–0.238; draw: 0.195–0.200; away win: 0.185–0.198) serve as a practical calibration benchmark.

**Methodology:** Brier probability score comparison of bookmaker-implied probabilities (after basic normalisation to remove overround) vs. ordered probit statistical model. Near 10,000 English league games over five seasons.

**Data:** English Football League (Premier League through lower divisions), five seasons in the late 1990s/early 2000s.

**Key findings:** Bookmaker odds exhibit better probabilistic calibration than the statistical model on Brier score. The bookmaker superiority was not present in the earliest part of the sample but emerged as market competition grew. This is consistent with the market incorporating private information (betting flows from sharp clients, team news, etc.) not available to the model. Forrest and Simmons earlier (2000) showed that newspaper tipsters also add only marginal value over simple models — confirming that easily available public information is already priced in.

**Relevance to v1:** This paper provides the core intellectual justification for using the bookmaker's de-vigged price as a baseline in v1. If the closing market outperforms standalone statistical models on calibration, the correct v1 architecture is a blend where the market anchors the estimate and the model provides an adjustment signal for matches where the model has genuine additional information (e.g., recent team form not yet reflected in the line). The Brier scores are useful calibration targets; a v1 model should aspire to Brier scores at or below the reported bookmaker benchmarks.

**Critical caveats:** The paper uses basic normalisation (additive de-vigging) to convert bookmaker odds to probabilities, which Strumbelj (2014) later shows is less accurate than Shin's method. The true bookmaker accuracy advantage may be even larger once more appropriate de-vigging is applied. The study covers English domestic football; international tournament markets (fewer matches, higher volatility, seasonal inactivity) may show different efficiency properties. The improvement-over-time finding (bookmakers getting more accurate) implies that models that "beat the market" on 1990s data will face tougher conditions today.

**Implementation ideas:** Use this paper's Brier scores as the calibration floor for v1. Set the v1 target as: Brier score on World Cup group-stage matches <= the best-available bookmaker Brier score benchmark. Track log-loss improvement versus Shin-devigged Pinnacle close as the primary model evaluation metric. Do not claim "market-beating" until log-loss is demonstrably below the de-vigged closing line.

---

## 4. Shin (1991, 1992, 1993) — Insider Information Model and Biased Bookmaker Odds

**Citations:**
- Shin, H. S. (1991). Optimal betting odds against insider traders. *Economic Journal*, 101, 1179–1185.
- Shin, H. S. (1992). Prices of state contingent claims with insider traders, and the favourite-longshot bias. *Economic Journal*, 102, 426–435.
- Shin, H. S. (1993). Measuring the incidence of insider trading in a market for state-contingent claims. *Economic Journal*, 103, 1141–1153.

**Summary:** Shin's three-paper sequence provides a game-theoretic model for why bookmaker odds systematically overestimate the win probability of longshots (favourite-longshot bias). In his framework, a bookmaker sets prices knowing that some fraction z of bettors are insiders who know the true outcome. To protect against informed bettors, the bookmaker prices each outcome as if the probability that a randomly chosen bettor is an insider is z. The key mathematical consequence is that the bookmaker's margins are distributed non-uniformly: longshots carry higher embedded margins than favourites, because the expected damage from an insider on a longshot that wins is larger (the payout is higher). This creates the observed favourite-longshot pattern as a rational risk-management response by the bookmaker rather than irrational bias. The 1993 paper derived the equilibrium relationship between z (insider proportion), the quoted odds, and the true probabilities — giving a practical formula for estimating z from observed odds and recovering de-vigged probabilities. This became the foundation for the "Shin probabilities" used as the best-practice de-vigging method in the literature.

**Methodology:** Game-theoretic equilibrium model. Closed-form solution for two-outcome markets; numerical iteration required for three-outcome (home/draw/away) football markets. Regression to estimate z from empirical odds data.

**Data (original):** UK horserace betting data for parameter estimation; conceptual framework applied to football by subsequent authors.

**Key findings:** The Shin equilibrium implies the bookmaker's overround is higher on longshots than on favourites — consistent with observed data across multiple sports. The z parameter estimated from UK racing data was in the range of 2–5% (roughly 2–5% of money bet comes from insiders with perfect information). Strumbelj (2014) later demonstrated that de-vigging using the Shin model yields more accurate probability forecasts than basic normalisation.

**Relevance to v1:** The Shin model is v1's specified de-vigging method. The z parameter allows recovery of fair probabilities from bookmaker odds without assuming the overround is distributed equally across outcomes — which basic normalisation wrongly assumes. For a three-outcome football market, z must be solved numerically by minimising the squared difference between the Shin equilibrium overround and the observed overround. The resulting fair probabilities are the "market baseline" that v1 blends with model output.

**Critical caveats:** Whelan (2025, see entry 7 below) has shown that Shin's insider-trading framing is likely incorrect as the mechanism for the favourite-longshot bias in modern fixed-odds markets. The bias can be reproduced by a model with no insiders — just bettor disagreement and bookmaker market power. This does not invalidate Shin probabilities as a de-vigging tool, but it does undermine the z parameter's interpretation as "insider fraction." More practically, z estimated from modern online football markets tends to be very small (<1%), suggesting the model is removing a mild longshot premium rather than insider-driven distortion. For a three-outcome market with typical overrounds of 4–6% (Pinnacle), the difference between Shin and basic normalisation on individual probability estimates is on the order of 0.5–2 percentage points — material for staking but not transformative.

**Implementation ideas:** Implement Shin de-vigging via Newton-Raphson iteration on the z parameter for each match's odds. Verify the implementation against the octosport.io benchmark (see references). For Pinnacle's low-margin markets (~2% overround), the difference between Shin and basic normalisation is smaller — but still preferred. Apply Shin de-vigging uniformly across all bookmakers used as market baseline inputs; do not mix normalisation methods across a single model run.

---

## 5. Strumbelj (2014) — "On Determining Probability Forecasts from Betting Odds"

**Citation:** Strumbelj, E. (2014). On determining probability forecasts from betting odds. *International Journal of Forecasting*, 30(4), 934–943.

**Summary:** This is the key methodological paper comparing de-vigging approaches for converting bookmaker odds to probability forecasts. Strumbelj tests four methods: (1) basic normalisation (dividing inverse odds by the booksum — equivalent to assuming equal margin distribution); (2) the additive method (subtracting the same absolute margin from each inverse probability); (3) the power method (raising each inverse probability to a constant power until they sum to one); and (4) Shin's model (iterative solution of the insider-equilibrium z). The paper evaluates each method's performance using the Brier score and log-loss over 63,861 football matches from seven bookmakers. Main finding: Shin and power methods outperform basic normalisation, especially when a clear favourite exists. The multiplicative (basic normalisation) method performs worst. Some bookmakers are significantly better probability calibrators than others — implying that the choice of which bookmaker's odds to de-vig matters as much as which method to use.

**Methodology:** Brier score and log-loss comparison across four de-vigging methods. Match-level evaluation (not binned calibration curves). Seven bookmakers, multiple football leagues.

**Data:** 63,861 football matches, seven major bookmakers, multiple European leagues. Exact date range not specified in available abstracts but covers the mid-2000s to early 2010s.

**Key findings:** Shin probabilities are more accurate forecasts than basic normalisation, with an improvement of approximately 1.5 percentage points in predictive accuracy on cases with a clear favourite (higher max implied probability). Power method performs comparably to Shin. Basic normalisation systematically overstates longshot probabilities and understates favourite probabilities relative to observed outcomes. Exchange (Betfair) odds are NOT always the best source of probabilities — performance depends on market liquidity, and for smaller markets a well-calibrated bookmaker can be better than the exchange. The advantage of Shin/power over basic normalisation is smaller in large, liquid markets and more pronounced in less-traded markets (consistent with Shin's framing — less liquid markets may have more insider-like money).

**Relevance to v1:** Directly validates v1's Shin de-vigging choice. The finding that exchange odds are not universally superior is important: for the 2026 World Cup, Betfair exchange liquidity on group-stage games involving smaller nations (e.g., Morocco vs. Saudi Arabia) may be lower than Pinnacle's fixed-odds market at kick-off. Use Pinnacle as the primary de-vigging source, with Betfair exchange as a secondary check for major games where exchange liquidity is demonstrably higher.

**Critical caveats:** The ~1.5% accuracy advantage of Shin over normalisation is an average across all market conditions; for liquid, symmetric (both teams roughly evenly matched) markets, the difference may be negligible. The paper evaluates opening/mid-market odds; closing-line odds have higher informational content (Forrest et al. 2005) and may show less favourite-longshot bias (reducing the advantage of Shin over normalisation). The 63,861 match sample is large, but covers primarily domestic European leagues — the calibration may differ for international tournaments where head-to-head history is sparser.

**Implementation ideas:** Use the Shin method as default. Validate that v1's Python/Rust implementation of the Shin iteration converges reliably for three-outcome markets, especially when one outcome has an implied probability above ~85% (deep favourites in group-stage mismatches). Cross-check the z value distribution across pre-tournament match odds — if z is consistently above ~3%, the market overround is unusually high, which may indicate a less efficient market or softer bookmaker.

---

## 6. Goddard & Asimakopoulos (2004) — "Forecasting Football Results and the Efficiency of Fixed-Odds Betting"

**Citation:** Goddard, J., & Asimakopoulos, I. (2004). Forecasting football results and the efficiency of fixed-odds betting. *Journal of Forecasting*, 23(1), 51–66.

**Summary:** Goddard and Asimakopoulos build an ordered probit model for English Football League match outcomes, using ten years of historical data. The model incorporates: recent match results (form), end-of-season significance (promotion/relegation pressure), cup competition involvement, and geographic distance between clubs (as a proxy for travel burden). They then use the model to test weak-form efficiency of fixed-odds bookmaker prices. The key finding is that a strategy of selecting bets late in the season — when matches have elevated significance (and bookmaker prices for such matches appear less carefully calibrated) — generates a positive expected return. The model outperforms a naive form-only approach and identifies a specific market anomaly: odds on end-of-season "crunch" matches appear to be set less efficiently than regular-season prices.

**Methodology:** Ordered probit regression on binary/ternary match outcomes. Variable set includes team form, league position, match significance index, cup schedule, and distance. Value-bet selection when model implied probability exceeds bookmaker implied probability.

**Data:** Ten years of English Football League data (all four divisions, roughly 1993–2003). Fixed-odds bookmaker prices from Football Data UK.

**Key findings:** The model achieved statistically significant outperformance on end-of-season bets. Return on investment from the identified strategy was positive, with the authors reporting profitability when betting where model and market disagree by a threshold. The match significance variable is the primary driver of excess returns — bookmakers appear to under-adjust odds for teams with strong end-of-season incentives (e.g., fighting relegation or chasing promotion). This is a semi-strong form inefficiency — publicly available information (league table position + season timeline) is not fully priced in.

**Relevance to v1:** The "match significance" finding is highly relevant to the group-stage structure of the 2026 World Cup. In the final round of group matches, teams with qualification already secured may select-rest key players; teams on the edge of elimination have heightened incentive. V1 should flag "dead rubber" vs. "must-win" matches as a categorical variable, similar to Goddard's significance index. This is an established, replicable source of market inefficiency.

**Critical caveats:** The English Football League is a very different competitive and informational environment from international tournament football. Club managers can genuinely rest players for domestic cups while national team coaches at a World Cup rarely rotate in group-stage matches (though it happens in the final group round). The efficiency anomaly Goddard found relates to domestic league-season incentive structures that may not map cleanly onto a 48-team, 3-match group stage. Replication evidence in international tournaments is limited. Modern markets have likely priced in the "match significance" factor more efficiently than in 2004.

**Implementation ideas:** Create a binary "meaningless fixture" flag for each match: set to 1 if both teams have already secured or been eliminated from round-of-32 qualification before the final group round kick-off. Apply a scepticism multiplier (reduce Kelly fraction by 50%) on all markets flagged as meaningful for one side and meaningless for the other — these are exactly the conditions where market efficiency is weakest. Do not bet on "meaningless vs. meaningless" matches.

---

## 7. Whelan (2025) — "On Estimates of Insider Trading in Sports Betting"

**Citation:** Whelan, K. (2025). On estimates of insider trading in sports betting. *The Manchester School*, 93(1). (Working paper version: UCD Economics Working Paper WP2024/19.)

Also: Whelan, K. (2025). How does inside information affect sports betting odds? *Scottish Journal of Political Economy*, 72(5).

**Summary:** Whelan provides a fundamental critique of the Shin (1993) insider-trading model as an explanation for the favourite-longshot bias. He shows that the estimation method for the insider fraction z is biased: z estimates will be positive even in realistic markets with no insiders at all, simply due to bettor disagreement and bookmaker market power. Whelan derives an alternative model where the favourite-longshot bias arises from: (a) heterogeneous beliefs among bettors (who have varying opinions on outcome probabilities) and (b) bookmakers with market power who face no competitive pressure to eliminate bias. He demonstrates that this alternative model fits modern fixed-odds football betting data at least as well as the Shin insider model, and that z estimates are driven primarily by bookmaker cost structures and competition levels, not by the actual incidence of inside information.

**Methodology:** Theoretical model with heterogeneous bettor beliefs and bookmaker market power. Simulation showing z > 0 in zero-insider market. Empirical fitting on modern UK sports betting data.

**Data:** Modern UK sports betting markets (exact leagues/seasons not publicly available in pre-print).

**Key findings:** The Shin z parameter should NOT be interpreted as the fraction of informed bettors. Variations in z across markets are better explained by competition intensity and bookmaker cost differences than by actual insider trading. The favourite-longshot bias persists in the data but is driven by bettor overweighting of longshot possibilities (probability weighting in the sense of Kahneman-Tversky) combined with bookmaker exploitation of that bias. Removing the insider interpretation does not invalidate Shin de-vigging as a computational tool — the iterative probability correction still reduces favourite-longshot distortion relative to basic normalisation.

**Relevance to v1:** V1 should use Shin de-vigging as a mechanical probability correction without relying on the insider interpretation. Do not try to estimate "insider fraction" as a market signal — Whelan shows it is not a reliable indicator of informed betting. However, the underlying mechanism (bettor miscalibration on extreme probabilities) reinforces the case for Shin over basic normalisation: longshots are more likely to be overpriced in the bookmaker's market because bettors overvalue them, so Shin's correction (which redistributes margin away from favourites and toward longshots relative to normalisation) moves in the right direction regardless of mechanism.

**Critical caveats:** Whelan (2025) is recent and has not been widely replicated yet. His alternative model is more complex and lacks some of Shin's mathematical elegance. The practical implication (use Shin de-vigging) is unchanged from the original Shin papers, so this is primarily a theoretical clarification rather than a change to v1's operational methodology.

**Implementation ideas:** Remove any references to "insider fraction" in v1 documentation. Refer to Shin probabilities as a "margin-adjusted probability estimate that corrects for the non-uniform distribution of bookmaker overround across outcomes." Log the z value per market as a diagnostic but do not use it as a trading signal.

---

## 8. Franck, Verbeek & Nuesch (2010) — "Prediction Accuracy of Different Market Structures: Bookmakers versus a Betting Exchange"

**Citation:** Franck, E., Verbeek, E., & Nüesch, S. (2010). Prediction accuracy of different market structures — bookmakers versus a betting exchange. *International Journal of Forecasting*, 26(3), 448–459.

**Summary:** This paper compares the forecasting accuracy of two fundamentally different market structures: traditional fixed-odds bookmakers (quote-driven) versus betting exchanges like Betfair (order-driven). Using 5,478 matches across the Big Five European leagues over three seasons, the authors find that Betfair exchange closing prices are marginally more accurate forecasters than bookmaker closing prices. They also document that bookmakers deliberately shade prices toward biases of fan-bettors: teams with large supporter followings tend to be underpriced (too short) because bookmakers can extract profit from fans who systematically overbet their team.

**Methodology:** Probit regression of outcomes on implied probabilities from bookmakers and Betfair exchange. Brier score comparison. Three-way (H/D/A) and binary efficiency tests.

**Data:** 5,478 English, Spanish, German, Italian, and French league matches, three seasons (approximately 2005–2008). Odds from 6 bookmakers plus Betfair exchange.

**Key findings:** Betfair exchange outperforms every single bookmaker and the average bookmaker prediction. The fan-bias effect is quantified: teams with a comparatively large supporter following are underpriced by bookmakers (offering value to bettors who back the *opponent*, not the popular team). In international competition, fan-betting biases are likely to be larger and more geographically concentrated (e.g., UK bettors heavily backing England), making this finding especially relevant for World Cup markets. Brier score differences between exchange and best bookmaker are small in absolute terms but statistically significant.

**Relevance to v1:** Betfair exchange odds (particularly closing prices) should be used as an additional signal alongside Pinnacle in constructing the v1 market baseline. When Betfair closing price diverges from Pinnacle closing price by more than ~1 percentage point on a Shin-devigged basis, investigate whether a large-following team is involved (fan-bias signal). The fan-bias channel is a persistent structural feature, not a random anomaly — it survives the competition that erodes most bet-able edges, because the biased bettors are structurally present in each new competition.

**Critical caveats:** The exchange-vs-bookmaker accuracy difference is small. For the 2026 World Cup, liquidity on Betfair for group-stage matches may be much lower than for domestic leagues, reducing the exchange's informational edge. The fan-bias finding predates social-media-driven betting hype and may have been partially arbitraged away since 2008. The UK-centric study may also have limited generalisability to international markets where Bahrain-based bettors operate.

**Implementation ideas:** Flag all World Cup group-stage and knockout matches involving England, Brazil, Argentina, France, and Spain as "fan-bias risk" markets. For these games, apply an additional 5–10% discount to the v1 confidence level before sizing Kelly stakes. Use Betfair closing prices as a cross-check for all such markets.

---

## 9. Angelini & De Angelis (2019) — "Efficiency of Online Football Betting Markets"

**Citation:** Angelini, G., & De Angelis, L. (2019). Efficiency of online football betting markets. *International Journal of Forecasting*, 35(2), 712–721.

**Summary:** This is the most comprehensive recent peer-reviewed test of online football betting market efficiency, covering 33,060 matches across 11 European leagues, 41 online bookmakers, and 11 years of data (approximately 2006–2017). The authors use a formal forecast-based efficiency test (whether bookmaker-implied probabilities can be improved by additional information). Their key result: when the *average* market price is used, most leagues show moderate efficiency; when the *best available odds* across 41 bookmakers are selected, 8 of 11 leagues are efficient, but 3 leagues (Italian Serie A, Greek Super League, and Spanish La Liga) show persistent inefficiencies with identifiable odds thresholds that support profitable ex-ante betting strategies. The study also finds that Betfair exchange shows systematic mispricing patterns that differ from the mild favourite-longshot bias in fixed-odds bookmakers.

**Methodology:** Formally tests whether bookmaker odds subsume publicly available information using a regression-based efficiency test. Identifies odds thresholds for profitable strategies ex post and ex ante. Brier score and log-loss comparison.

**Data:** 33,060 matches, 41 bookmakers, 11 European leagues, 2006–2017.

**Key findings:** Efficiency is league-dependent, not universal. Smaller, less-traded leagues show more persistent inefficiency. The best-odds approach across multiple bookmakers is more efficient than any single bookmaker's odds — consistent with competitive price discovery. The Betfair exchange does not uniformly dominate fixed-odds bookmakers; it shows different mispricing patterns (larger implied margins on certain draws in mid-table matches). Odds thresholds that identify profitable strategies (e.g., always back the home team at odds above 2.5 in La Liga) were identified but required very large sample sizes to confirm statistical significance.

**Relevance to v1:** This paper reinforces three v1 design choices: (1) use Pinnacle as the primary market baseline (closest to the "best odds" aggregate used in the study's efficient sub-samples); (2) the World Cup is not on the list of leagues studied, meaning efficiency properties need separate validation — international tournament markets may show more or less efficiency than domestic leagues; (3) use line-shopping across Paddy Power, Sky Bet, Virgin Bet, and Bet365 to capture the best-available-odds advantage, since combining multiple bookmakers produces more efficient baseline probabilities than relying on a single book.

**Critical caveats:** The identified profitable strategies in Italian Serie A and Greek Super League relied on specific team/match configurations unlikely to appear in a 48-team international tournament. The 2006–2017 time window pre-dates further market consolidation (many smaller bookmakers acquired by larger groups post-2017). Replication going forward may show different results. The threshold-based strategy is inherently data-mined on the in-sample window and the authors acknowledge that ex-ante profitability is marginal.

**Implementation ideas:** Build a multi-bookmaker odds-collection layer that captures Pinnacle, Bet365, Betfair, Paddy Power, and Sky Bet at or near kick-off. Compute the best-available odds for each outcome and use those as the raw input to Shin de-vigging. This approximately replicates the most-efficient signal in Angelini & De Angelis's study.

---

## 10. Wilkens (2026) — "Can Simple Models Predict Football — and Beat the Odds? Lessons from the German Bundesliga"

**Citation:** Wilkens, S. (2026). Can simple models predict football — and beat the odds? Lessons from the German Bundesliga. *Journal of Sports Analytics*. DOI: 10.1177/22150218261416681.

**Summary:** The most recent (February 2026) peer-reviewed paper directly testing whether a simple quantitative model can generate positive returns in modern online betting markets. Wilkens builds an xG (expected goals)-based model using a Skellam distribution (difference of two Poisson variables) to estimate home/draw/away probabilities, then applies isotonic regression for calibration on 11 Bundesliga seasons (2014/15–2024/25). The paper reports an ROI of approximately 10% using average market odds and ~15% using best-available odds — driven predominantly by home win bets. Away win bets are consistently loss-making. The paper explicitly notes that bookmaker odds show superior statistical calibration overall but the xG model captures pricing signals not fully reflected in market prices, particularly for home games.

**Methodology:** xG-based Skellam distribution for match probability estimation. Isotonic regression calibration. Kelly-fraction value betting. 11 Bundesliga seasons. Comparison to bookmaker Brier score and log-loss.

**Data:** German Bundesliga, 2014/15–2024/25 (11 seasons). Average market odds and best-available odds separately evaluated.

**Key findings:** Approximately 10% ROI on average odds and ~15% on best odds over 11 seasons — a substantial reported edge. However, the edge is almost entirely in home win bets; away wins are loss-making. The model's xG signal adds information over market prices specifically for home performance prediction. The paper is honest that bookmakers generally show better calibration (lower Brier/log-loss) but a narrowly focused strategy exploits a specific blind spot in bookmaker pricing.

**Relevance to v1:** This is the most directly relevant recent evidence that a simple quantitative model can beat modern bookmaker closing prices — with a realistic data set and post-2014 market conditions. The 10–15% ROI figures are exceptional and should be treated with scepticism: Bundesliga is a high-profile domestic league, not a once-every-four-years tournament; the sample size is large (hundreds of home win bets per season); and the xG metric has been publicly available and heavily traded since 2014, suggesting the edge may be eroding. For v1, the actionable lesson is: focus model edge on home advantage calibration, since that is where bookmakers appear to have a persistent blind spot.

**Critical caveats:** Bundesliga results from 2014–2025 cannot be extrapolated to World Cup group-stage matches, which have different incentive structures, shorter preparation timelines, and international player pools with heterogeneous form data. The 10–15% ROI figure is unusually high for a published academic paper — the absence of real-money validation is a concern. The "simple model" tested is xG-based; v1 uses a different signal (Elo + Dixon-Coles), so the transferability of the edge is not established. The home-win focus means the strategy requires large Bundesliga-style datasets; v1 will only have 48 group-stage matches to work with initially.

**Implementation ideas:** In v1, apply an xG-adjusted signal for teams with recent match data in UEFA/CONMEBOL competition (where xG data is available from StatsBomb or FBRef). This may replicate part of the Wilkens edge. Treat away win markets with particular scepticism relative to model price, given the consistent finding that away backs are loss-making even against a well-calibrated model. Size home win bets at full (quarter-)Kelly when model and market agree on direction but disagree on magnitude; size away win bets at half (quarter-)Kelly due to the additional uncertainty.

---

## Practical Implications for v1

The following section synthesises the above literature into concrete design principles for the v1 system (international Elo + time-decayed Dixon-Coles + Shin de-vigged market baseline + logistic blend + quarter-Kelly).

### 1. The closing line is the primary efficiency benchmark, not the profit/loss record

The literature is unambiguous (Forrest et al. 2005, Angelini & De Angelis 2019, Buchdahl practitioner work) that closing-line value (CLV) — whether the bet was placed at odds better than the Shin-devigged Pinnacle close — is the best leading indicator of long-run skill. In the short sample of the 2026 World Cup (up to 104 matches, of which only a fraction will be bet), realised P&L will be dominated by variance. Track CLV on every bet from day one. A model that consistently achieves +3 to +5% CLV across 50+ bets has demonstrated skill; one that achieves positive P&L without positive CLV has gotten lucky.

### 2. Use Shin de-vigging as the default, but understand its limitations

Strumbelj (2014) is the benchmark validation for Shin over basic normalisation. The improvement is meaningful (~1.5 pp accuracy gain on cases with a clear favourite) but not transformative. For v1's purpose — computing a fair market probability to blend with the model — the key practical point is: basic normalisation will systematically overstate longshot probabilities and understate favourite probabilities, which will cause v1 to *under-bet favourites and over-bet longshots* if used as the baseline. Shin corrects this. The Whelan (2025) critique does not change the operational recommendation.

### 3. Closing prices from Pinnacle (or equivalent sharp book) dominate opening prices

The literature demonstrates that as match time approaches, prices incorporate additional information (team news, sharp money, model outputs from other market participants). A v1 model that identifies an edge at opening lines but that edge evaporates by close is giving away information to the market; the signal was already known. Track what fraction of v1's expected value is captured in the opening vs. closing line. If most CLV is generated at open, v1 has useful private information; if CLV is neutral at open and negative at close, v1 is being picked off.

### 4. The favourite-longshot bias in football is mild and inconsistent in direction

Unlike horse racing (where the bias reliably favours backing favourites in expected-value terms), football shows both favourite-bias and longshot-bias evidence depending on the study sample and market conditions (Vlastakis et al. 2009; Franck et al. 2010; Angelini & De Angelis 2019). Do not build a systematic strategy around fading longshots or backing favourites in football without within-sample validation specific to international tournament markets. The bias exists but is small relative to transaction costs at typical UK bookmaker overrounds (5–8%).

### 5. Match significance and rotations are the most persistent semi-strong efficiency gap

Goddard & Asimakopoulos (2004) found the clearest market inefficiency relates to end-of-season incentive structures (teams that have nothing to play for). In the World Cup group stage, the analogous situation is the final round: a team that has already qualified may rotate its first XI. This is publicly known information. Flag all matches in round 3 of each group as "rotation risk" markets and apply explicit handicaps to team strength inputs where rotation is likely. The literature suggests bookmakers under-adjust for these matches.

### 6. Multi-bookmaker best-odds strategy, not single-book

Angelini & De Angelis (2019) demonstrated that selecting the best available odds across multiple bookmakers — rather than taking a single book's price — shifts several markets from inefficient to efficient. This means the best single-book odds approximate the market's true probability better than any individual bookmaker. For v1, systematically shop across Bet365, Paddy Power, Sky Bet, Virgin Bet, and Betfair exchange. When v1 flags a value bet, always place it at the best available price. The difference between best odds and average odds was worth ~5% additional ROI in Wilkens (2026).

### 7. Be skeptical about claimed model ROI from the literature

Dixon-Coles (1997) showed positive returns in a single bookmaker, 1990s, paper-based market. Kuypers (2000) found semi-strong inefficiency using basic logit. Goddard (2004) found profitable late-season bets. All of these used historical data with survivorship biases and smaller-margin markets. Wilkens (2026) is the most recent and methodologically careful, but its 10–15% ROI should not be extrapolated to the World Cup. V1's target should be a positive Brier/log-loss improvement over the Shin-devigged market baseline (demonstrating calibration skill), combined with consistent positive CLV (demonstrating price discovery skill). Profitability at quarter-Kelly on a $1,000 bankroll over 50–100 bets is a secondary, high-variance outcome that should not drive strategy adjustments in the first tournament.

### 8. International football efficiency is under-studied

The entire peer-reviewed literature reviewed here focuses on domestic European leagues. World Cup and international tournament betting has received almost no peer-reviewed treatment. This is a genuine research gap and a potential source of exploitable inefficiency: the market has less history to learn from and fewer specialist sharps focused on international match modelling. V1 should track whether its CLV in World Cup group matches is systematically higher than the domestic-league benchmarks from the literature — if it is, this suggests the international market is less efficient and v1's relative edge is larger.

---

## Summary Table

| Paper | Year | Core Finding | Relevance | Caution Level |
|---|---|---|---|---|
| Dixon & Coles | 1997 | Time-decay Poisson model generates positive betting returns | Direct: v1 uses DC as core model | High — 1990s market |
| Kuypers | 2000 | Bookmakers set inefficient odds to exploit bettor bias; longshots overpriced | Mechanism for FLB; World Cup fan-team adjustment | High — single book, 1990s |
| Forrest, Goddard & Simmons | 2005 | Bookmakers outperform statistical models on Brier score | Use market as baseline, not rival | Moderate |
| Shin | 1991–1993 | Insider model explains FLB; derives de-vigging formula | Core de-vigging method for v1 | Low (tool remains valid) |
| Strumbelj | 2014 | Shin > basic normalisation; power ≈ Shin; exchange not always best | Validates Shin de-vigging choice | Low |
| Goddard & Asimakopoulos | 2004 | End-of-season matches show persistent semi-strong inefficiency | "Dead rubber" / rotation flags in group stage | Moderate |
| Whelan | 2025 | Shin z-parameter does not measure insider fraction; FLB from bettor disagreement | Theoretical; don't interpret z as insider signal | Low (practical impact minimal) |
| Franck, Verbeek & Nuesch | 2010 | Exchange slightly better than bookmakers; fan-bias pricing documented | Fan-bias discount for popular national teams | Moderate |
| Angelini & De Angelis | 2019 | 8/11 leagues efficient at best-odds; 3 show persistent inefficiency | Use multi-book best-odds; don't expect uniform efficiency | Low–moderate |
| Wilkens | 2026 | Simple xG model yields ~10–15% ROI on Bundesliga home wins | Most recent evidence models can beat market; home-win focus | Moderate–High (novel, unvalidated in tournament context) |

---

*Section compiled: 2026-06-10. Next review recommended after World Cup group stage (2026-06-27) to incorporate closing-line realisation data.*
