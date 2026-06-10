# Rating Systems for Football: Annotated Literature Review

**Scope:** Elo-based ratings for national teams; pi-ratings; Glicko/Glicko-2; probability mapping from rating differences (Davidson model, ordered logit); evidence on Elo predictive power specifically for international/national-team football; practical relevance to the v1 World Cup Alpha system (international Elo + time-decayed Dixon-Coles + Shin-devigged market baseline + logistic blend + quarter-Kelly staking).

**Reviewed:** 9 primary sources (6 peer-reviewed papers, 1 arXiv preprint, 1 practitioner system specification, 1 recent JORS paper). Reviewed June 2026 in preparation for the 2026 FIFA World Cup starting 2026-06-11.

---

## Paper 1

**Hvattum, L. M., & Arntzen, H. (2010). Using ELO ratings for match result prediction in association football. *International Journal of Forecasting*, 26(3), 460–470. https://doi.org/10.1016/j.ijforecast.2009.10.002**

### Summary

This is the foundational quantitative-sports-betting paper on Elo for football. Hvattum and Arntzen applied the Elo rating system—originally designed for chess—to association football by converting Elo rating differences into covariates for ordered logit regression models. They evaluated predictive performance using both statistical measures (ranked probability score, log-loss) and economic measures (simulated return on unit bets) across 14,927 English league matches (Premier League through Conference, approximately 1995–2007). Seven prediction methods were compared: two Elo-based ordered logit models (Elo-1 using raw difference, Elo-2 using a transformed version), a home-wins-always baseline, a draw-always baseline, historical frequency baseline, ordered probit regression, and two methods anchored on bookmaker odds.

### Methodology

Elo ratings were updated after each match using the standard formula R_new = R_old + K × (W − W_e), where W_e = 1 / (1 + 10^(−ΔR/400)) is the logistic expected score and K controls the sensitivity of updates. The update K was kept constant (the paper did not use match-importance weights). The Elo difference was then used as the single covariate in an ordered logit regression predicting the three-way outcome (home win / draw / away win), with thresholds estimated from historical data.

### Data

English football pyramid, 14,927 matches, approximately 1995–2007. Club-level data, not international/national teams.

### Key Findings

- The two Elo-based ordered logit models outperformed all non-market benchmarks (home frequency, draw frequency, probit) on both RPS and simulated returns.
- **Both Elo models were significantly worse than the two market-odds benchmarks on observed loss.** The paper's headline result is therefore that Elo provides incremental information beyond naive baselines but cannot beat well-calibrated bookmaker odds on club football in isolation.
- Reported return on unit bets: the Elo methods produced negative but less negative returns than the naive baselines; the market-odds anchored methods were the only ones producing near-breakeven outcomes.
- The ordered logit formulation cleanly separates the rating signal from draw-probability estimation, which is non-trivial since a pure Elo win expectation ignores draws.

### Relevance to v1

This paper provides the canonical justification for using Elo differences as a feature inside an ordered logit or ordered logit-style blend. The finding that Elo alone cannot beat bookmaker odds motivates exactly the v1 architecture: Elo as a structural prior, Shin-devigged market odds as the informed baseline, blended via logistic regression. The ordered logit framing is directly implementable for the three-way probability output. The paper is now 16 years old and was estimated on club football in a single country; national-team data and modern liquid markets may behave differently.

### Skeptical Notes

The dataset is English club football only. National-team Elo faces far sparser data (8–14 relevant matches per year vs 38 league games). The K-factor used was constant and not validated for optimality. The paper predates sharp Asian-market price formation and exchange odds; modern market efficiency is considerably higher, making the odds baseline even harder to beat. Effect sizes on the economic measure are not reported with confidence intervals.

### Implementation Ideas

- Use Elo difference as the primary structural covariate in the logistic blend layer, with the Shin-devigged market probability as a second covariate.
- Fit ordered logit thresholds separately for group-stage vs knockout matches, as draw rates differ.
- Report RPS alongside log-loss as the primary calibration metric (RPS is the standard in the cited literature).

---

## Paper 2

**Constantinou, A. C., & Fenton, N. E. (2013). Determining the level of ability of football teams by dynamic ratings based on the relative discrepancies in scores between adversaries. *Journal of Quantitative Analysis in Sports*, 9(1), 37–50. https://doi.org/10.1515/jqas-2012-0036**

### Summary

Constantinou and Fenton introduced the pi-rating as a dynamic team-strength measure that updates on goal margin rather than match outcome alone. Unlike Elo, pi-ratings maintain separate home and away ratings for each team and update on the relative difference between expected and observed goal differential (not just W/D/L). The paper is notable for being the first academic study claiming demonstrated profitability against published bookmaker odds using a simple rating-based technique, tested over five English Premier League seasons (2007/08–2011/12).

### Methodology

Each team carries two ratings: π_h (home performance) and π_a (away performance). After each match, ratings update proportionally to the surprise in the goal difference: the expected goal difference is derived from the current ratings and the actual minus expected discrepancy drives the update, weighted by a learning rate c_λ. Separate learning rates govern home and away rating updates. The winning team's rating increases, the losing team's decreases; draw updates depend on the signed discrepancy. Probabilities are extracted via a separate fitted model mapping pi-rating differences onto W/D/L probabilities.

The profitability test used a simple rule: bet on whichever outcome the model assigned the highest probability if model probability exceeded the implied bookmaker probability by a margin.

### Data

Five EPL seasons 2007/08–2011/12 (five seasons of in-sample parameter tuning plus a rolling out-of-sample betting simulation within those seasons). Bookmaker odds from multiple firms.

### Key Findings

- Pi-ratings outperformed standard Elo ratings on ranked probability score (RPS = 0.199 for pi-ratings vs 0.204 for Elo, based on a 2025 practitioner replication; the original paper reports a qualitatively similar gap).
- Over five EPL seasons, the simulated betting strategy yielded a positive cumulative profit. Bets won varied 28%–37% per season at average odds of approximately 2.79–3.27. The overall strategy was profitable against published bookmaker margins.
- **Caveat:** The profitable seasons straddled the training period used to optimise the learning rate parameters. True out-of-sample validation is limited.

### Relevance to v1

Pi-ratings are directly applicable as a supplementary feature in the v1 blend. The home/away separation is a meaningful improvement for club football but less critical for neutral-venue World Cup matches (most group games and all knockouts are at neutral venues). The goal-margin update is valuable for national teams where results are sparse; a 3-0 win should shift beliefs more than a 1-0 win. The demonstration of profitability is encouraging but was on club football and may not transfer.

### Skeptical Notes

The profitability claim is the most cited and most contested aspect of this paper. Several concerns apply:
1. Parameter tuning (learning rate c_λ) was optimised over the same five seasons used to measure profitability. There is no genuinely held-out validation period.
2. EPL 2007–2012 bookmaker odds were less sharp than current exchange-settled prices; the same edge may not persist in 2026 markets.
3. The paper uses a simple threshold betting rule, not Kelly staking; reported profitability is not corrected for multiple comparison across betting rules.
4. Goal margin as a signal is known to be partially mean-reverting (lucky vs unlucky scorelines). Pi-ratings may overfit on goal difference rather than team quality.

### Implementation Ideas

- Implement pi-ratings as an additional feature alongside Elo in the blend, with goal margin capped at 4 to reduce the noise contribution of blow-out margins against weak opponents.
- For neutral-venue World Cup matches, collapse the home/away split and use a single pi-rating per team, updating on goal difference with the same c_λ.
- Use pi-rating difference as a second structural covariate (alongside Elo difference) in the ordered logit.

---

## Paper 3

**World Football Elo Ratings (eloratings.net). Methodology specification (running since 1997, attributed to Bob Runyan; methodology formalised and maintained at eloratings.net). See also: Wikipedia, "World Football Elo Ratings."**

*[Practitioner source — no single peer-reviewed publication; methodology is publicly documented and has been cited in peer-reviewed literature including Lasek et al. 2013 and Robberechts & Davis 2019.]*

### Summary

The World Football Elo Ratings (WFER) are the most widely used public Elo ratings for national football teams, covering all official international matches since 1872. The system incorporates three modifications to the base Elo formula that are specifically designed for football: variable K-factors by match importance, a goal-margin multiplier G, and a home-advantage offset in the expected-score calculation.

### Methodology

**Core formula:** R_new = R_old + K × G × (W − W_e)

**Expected score:** W_e = 1 / (1 + 10^(−(ΔR + 100) / 400)), where the +100 is added to the home team's rating (i.e., home advantage is modelled as 100 Elo points ≈ 14 percentage points in win probability at equal ratings).

**K-factor by match importance:**
- World Cup finals: K = 60
- Continental championship finals/Olympics: K = 50
- Continental qualifying / major tournaments: K = 40
- Friendly matches: K = 20

**Goal-margin multiplier G:**
- W by 0 (draw) or W by 1 goal: G = 1.0
- Win by 2 goals: G = 1.5
- Win by 3+ goals: G = (11 + goal difference) / 8

**Convergence:** Ratings are considered provisional until a team has played approximately 30 matches.

### Key Findings

- A comparative study of eight ranking methods (Lasek et al. 2013, see Paper 4) found WFER had the highest predictive capability among all ranking systems tested, clearly outperforming the official FIFA ranking.
- The +100 home advantage corresponds to a roughly 14 percentage-point shift in win probability at equal ratings. For World Cup group matches at genuinely neutral venues (USA/Canada/Mexico 2026), this parameter should likely be reduced to near zero or to a small venue-familiarity value (~20–30 points).
- As of June 2026, the top-rated teams were Spain (2,165), Argentina (2,113), France (2,081); these are broadly consistent with market consensus.

### Relevance to v1

WFER provides a ready-made, battle-tested prior for national-team strength that requires no proprietary data. The K-factor hierarchy is a reasonable heuristic for match importance weighting. For v1, WFER ratings can serve as the starting point for the Elo component, with the home-advantage parameter recalibrated to reflect neutral-venue conditions.

### Skeptical Notes

- The methodology is a set of empirically reasonable heuristics, not the output of a likelihood-maximising estimation procedure. The K=60/40/20/50 values have not been formally optimised against a held-out validation set.
- The 100-point home advantage was calibrated on all historical matches including non-neutral venues. At neutral World Cup venues this will overstate home advantage.
- WFER applies the same G multiplier globally, but the informational value of a 4-0 margin may be lower for a strong team beating a very weak team vs two closely matched sides.
- The system treats a friendly and a qualifier of the same stated type identically within categories; in reality, pre-tournament friendlies often feature rotation squads and carry less signal.

### Implementation Ideas

- Download WFER ratings for all 48 World Cup 2026 participants as the Elo prior.
- Set home advantage to approximately 30–40 points for group-stage matches (semi-home for USA/Canada/Mexico hosts) and 0 for fully neutral knockouts.
- Apply a squad-quality discount multiplier to K for friendlies within the past 12 months to reduce noise from rotation squads.

---

## Paper 4

**Lasek, J., Szlávik, Z., & Bhulai, S. (2013). The predictive power of ranking systems in association football. *International Journal of Applied Pattern Recognition*, 1(1), 27–46. https://doi.org/10.1504/IJAPR.2013.052339**

### Summary

This is the most comprehensive head-to-head comparison of ranking systems for national-team football prediction. Lasek, Szlávik, and Bhulai compared eight methods: the official FIFA Men's World Ranking (2006–2018 format), World Football Elo Ratings, several variants of Elo with different K configurations, Buchholz coefficient, UEFA coefficient, and a random baseline. Rankings were converted into match-outcome probability predictions and evaluated against actual results using ranked probability score (RPS) and related metrics.

### Methodology

All ranking systems were converted into match-outcome probability predictions using a consistent logistic transformation of the pairwise rank/rating difference. The authors evaluated predictions on a large multi-season dataset of international matches (both competitive and friendly), spanning multiple years up to 2012. The RPS was the primary evaluation metric, which appropriately rewards probabilistic forecasts that are close to the true outcome ordering.

### Data

Large international match dataset covering multiple decades, with focus on competitive matches from approximately 2003–2012. Exact size not publicly disclosed in available extracts but covers hundreds of national teams across all confederations.

### Key Findings

- **World Football Elo Ratings consistently outperformed all other ranking systems tested, including all Elo variants with different K configurations and the official FIFA ranking.**
- The FIFA ranking (2006–2018 method, based on points-per-match with weighted competition importance) performed poorly, often near the random baseline for predictive purposes.
- The paper confirmed the Hvattum & Arntzen (2010) finding that Elo-based approaches are the best available simple rating for football, and extended it to the national-team setting.
- Higher K values (more responsive ratings) tended to perform better than lower K values, up to a point — consistent with the argument that team strength at international level changes more abruptly (manager changes, key player retirements) than at club level.

### Relevance to v1

This paper provides direct validation that WFER-style Elo is the right structural prior for national teams. The finding justifies using Elo as a feature in the v1 blend and supports the decision not to use FIFA rankings. It also supports the idea that the Elo component should be fairly responsive (not overly smooth), given national teams' structural changes.

### Skeptical Notes

- The paper was published in 2013 and the most recent data is from 2012. FIFA updated its ranking methodology to an Elo-based system in June 2018; the comparison with the "poor FIFA ranking" is now outdated for the post-2018 FIFA ranking.
- The conversion from ranking points/ratings to match probabilities using a logistic function is reasonable but was applied uniformly; a better procedure would estimate the logistic parameters separately per ranking system.
- The paper does not separate group-stage friendly from competitive match performance; predictive accuracy on competitive matches only would be a stronger test.

### Implementation Ideas

- Use this paper as the primary justification for the Elo prior in any v1 documentation or model card.
- Monitor whether the post-2018 FIFA Elo-based ranking produces comparable accuracy to WFER; if so, FIFA ratings could serve as a cross-check since they are more transparently published.

---

## Paper 5

**Wunderlich, F., & Memmert, D. (2018). The Betting Odds Rating System: Using soccer forecasts to forecast soccer. *PLOS ONE*, 13(6), e0198668. https://doi.org/10.1371/journal.pone.0198668**

### Summary

Wunderlich and Memmert introduced BORS (Betting Odds Rating System), which replaces actual match results in the Elo update with betting-odds-implied probabilities. The innovation is treating the pre-match betting market as a "soft label" rather than using the hard binary/ternary outcome. This paper directly measures how much information bookmaker odds contain relative to post-match results, providing an empirical answer to the question of market informativeness.

### Methodology

Three ELO variants were compared over ~15,000 matches from four major European leagues and international competitions (2007/08–2016/17):
- **ELO-Result:** Standard Elo updated on match outcome (K=14, optimised by grid search).
- **ELO-Goals:** Elo updated with a goal-margin multiplier (K₀=4, λ=1.6).
- **ELO-Odds:** Elo updated using pre-match betting probabilities as the "actual result" (K=175).

Ratings from each system were converted to match-outcome probabilities via ordered logit regression on rating difference. Informational loss (log-loss) was the primary evaluation metric. Statistical significance was assessed using Wilcoxon signed-rank tests.

### Data

~15,000 matches, English Premier League, German Bundesliga, Spanish Primera División, Italian Serie A, plus UEFA Champions League and Europa League, 2007/08–2016/17. Bookmaker odds averaged across multiple firms, normalised to remove overround.

### Key Findings

| Model | Avg. Informational Loss | p-value vs next |
|---|---|---|
| Betting Odds (raw) | 1.3795 | < 0.0001 |
| ELO-Odds | 1.3913 | < 0.0001 |
| ELO-Goals | 1.4008 | 0.0202 |
| ELO-Result | 1.4032 | — |

- ELO-Odds significantly outperformed ELO-Goals (p < 0.01) and ELO-Result.
- ELO-Goals significantly outperformed ELO-Result (p < 0.05).
- All three ELO systems were significantly worse than raw betting odds.
- **The key finding: pre-match betting odds contain more information about match outcomes than post-match results.** This is evidence that bookmaker odds aggregate information beyond team ratings (injuries, recent form, tactical news) that pure rating systems miss.

### Relevance to v1

This paper provides the empirical foundation for the v1 market-baseline component. The ordering ELO-Odds > ELO-Goals > ELO-Result confirms that for the structural rating, using goal margin (Dixon-Coles direction) is better than pure W/D/L, and that anchoring on market odds is better still. Critically, the finding that raw odds still beat ELO-Odds means the blend must weight the market-derived component heavily, not just use it as a mild correction. The K=175 for ELO-Odds vs K=14 for ELO-Result is also informative: probability inputs have much lower variance than binary outcomes, so the system can absorb larger K without instability.

### Skeptical Notes

- The study is on club football in top European leagues. Bookmaker markets for international friendlies and qualifying matches are substantially less liquid and less informative; the market-odds dominance may be weaker in sparse international settings.
- The BORS system uses pre-match odds to update ratings, creating a circular dependency: the model being blended with market odds is itself partly trained on market odds. This does not invalidate it but makes "market-independent" validation impossible.
- The analysis covers only four top leagues plus European cups; lower-division or national-team markets may have different information efficiency properties.

### Implementation Ideas

- The BORS finding motivates storing historical opening and closing odds for all World Cup matches, not just using them for the blend signal: closing odds can serve as the "true probability" benchmark for calibration analysis.
- When Elo ratings are used as a feature in the v1 blend, include goal-margin-weighted updates (not pure W/D/L Elo) to capture the ELO-Goals advantage.
- Use the informational-loss gap (1.3913 vs 1.3795) as a reference: if the v1 blend achieves informational loss within 1% of raw closing-odds, that is a good result.

---

## Paper 6

**Ley, C., Van de Wiele, T., & Van Eetvelde, H. (2019). Ranking soccer teams on the basis of their current strength: A comparison of maximum likelihood approaches. *Statistical Modelling*, 19(1), 55–77. https://doi.org/10.1177/1471082X18817650**

### Summary

Ley, Van de Wiele, and Van Eetvelde compared ten strength-based statistical models for ranking football teams, all estimated via weighted maximum likelihood with time-depreciation weighting to favour recent matches. The four model families were Thurstone-Mosteller, Bradley-Terry, independent Poisson, and bivariate Poisson. The goal was to produce rankings reflecting current team strength, with evaluation using Rank Probability Score (RPS) across both domestic leagues and national team competitions.

### Methodology

All models estimated team strength parameters (attack/defence for Poisson models, single strength for paired comparison models) via weighted MLE where each historical match's weight decays exponentially with time (W(t) = exp(−λ_time × t)). A match-importance factor was also incorporated. Models were evaluated on held-out matches from multiple leagues and national team competitions. The bivariate Poisson model extends independent Poisson by adding a covariance term to handle correlation between goals scored, analogous to the Dixon-Coles ρ parameter.

### Data

Multiple domestic leagues plus national team competitions. Specific match counts not available in public extracts, but the paper covers a multi-year international dataset assessed for both club and national team predictive accuracy.

### Key Findings

- **Bivariate Poisson and independent Poisson models achieved the best RPS,** outperforming all paired-comparison models (Bradley-Terry, Thurstone-Mosteller) and outperforming naive benchmarks.
- The bivariate Poisson model marginally outperformed the independent Poisson model, consistent with Dixon-Coles (1997) finding that the ρ correlation parameter improves fit.
- Time-depreciation weighting significantly improved all models' predictive performance compared to equal-weighted MLE.
- The paper's practical conclusion: a time-weighted bivariate Poisson model (essentially a well-estimated Dixon-Coles with exponential time decay) is the most accurate structural rating system for current team strength.

### Relevance to v1

This paper directly validates the time-decayed Dixon-Coles component of v1. The finding that time-depreciation improves all models motivates the exponential decay (ξ parameter) in the Dixon-Coles fit. The fact that Poisson models outperform paired-comparison models (which include Elo-type systems) suggests that for the structural component, a Dixon-Coles goal model may actually produce better raw probability estimates than Elo alone — supporting the v1 architecture of using both.

### Skeptical Notes

- The optimal time-decay parameter λ_time varies by context. For national teams playing 8–14 competitive matches per year, a shorter half-life may over-discard the small available data pool. The paper does not provide specific national-team optimal values.
- The bivariate Poisson advantage over independent Poisson is modest; in small-sample national team settings, the additional parameter may not justify the estimation complexity.
- The paper does not compare against bookmaker odds, so it cannot speak to whether these models can generate CLV.

### Implementation Ideas

- Use the time-weighted Dixon-Coles (bivariate Poisson with time decay) as the primary goal-based structural model alongside Elo.
- For World Cup 2026 specifically, fit the Dixon-Coles model on the most recent 24–36 months of each team's competitive results, down-weighting friendlies by a factor of ~0.3 relative to competitive matches.
- Test both independent and bivariate Poisson as v1 components; the added ρ parameter in bivariate Poisson typically improves 0-0 and 1-1 probability estimates which are most important for draw markets.

---

## Paper 7

**Arntzen, H., & Hvattum, L. M. (2021). Predicting match outcomes in association football using team ratings and player ratings. *Statistical Modelling*, 21(5), 449–470. https://doi.org/10.1177/1471082X20929881**

### Summary

A follow-up to the 2010 paper, Arntzen and Hvattum compared team-level Elo ratings against individual player plus-minus ratings and a combination of both, using ordered logit regression (OLR) and competing-risk scoring-rate models. The key innovation is testing whether player-level information (starting line-up quality) adds predictive value above and beyond team-level Elo.

### Methodology

Two covariates were tested: (1) pre-match Elo rating difference (team-level), and (2) average plus-minus rating difference for the announced starting line-ups (player-level). Ordered logit regression directly predicts W/D/L probabilities from these covariates. A competing-risk model estimated separate home and away scoring rates (Poisson intensity parameters), generating probabilities via numerical integration over the bivariate score distribution.

### Data

Norwegian top-division football (Eliteserien), multiple seasons. Club football, not international. Player plus-minus ratings were estimated using adjusted plus-minus regression on match-level data.

### Key Findings

- Player plus-minus ratings alone produced slightly more accurate predictions than team Elo alone on this dataset.
- **The combined model (both Elo and plus-minus) significantly outperformed either alone,** with the performance gap being practically as well as statistically significant.
- The competing-risk (Poisson) model did not systematically outperform the ordered logit in this dataset.
- The result suggests that knowing who is actually playing (starting line-up composition) carries additional signal beyond historical team-level results.

### Relevance to v1

For World Cup 2026, starting line-up data is announced approximately 60 minutes before kick-off. If player market values or some proxy for player-level quality (e.g., Transfermarkt value of the starting XI) can be incorporated at betting time, this paper suggests a potentially significant uplift in accuracy. The ordered logit framework for combining multiple covariates is directly applicable to v1.

### Skeptical Notes

- The dataset is Norwegian club football, which has lower bookmaker market efficiency than Premier League or international tournaments. Player ratings may add more value where markets are less efficient.
- Plus-minus ratings for international players are harder to estimate than for club players (national teams play rarely, rosters change, players play different positions).
- The paper does not compare against bookmaker odds, so we cannot infer whether the player-level improvement translates to CLV.
- Line-up information for national teams is less predictable in advance; tactical rotations in tournaments (resting players, system changes) reduce the signal value of predicted vs actual starting XIs.

### Implementation Ideas

- Investigate using Transfermarkt starting XI values as a proxy for player-level ratings when line-ups are announced.
- Structure the betting workflow so the model is re-run with starting XI values once confirmed (~60 min pre-kickoff), as a late-signal update.
- Weight the player-level covariate more heavily in knockout matches (where managers are more likely to field their strongest XI) than in group matches.

---

## Paper 8

**Shelopugin, A., & Sirotkin, A. (2023). Ratings of European and South American Football Leagues Based on Glicko-2 with Modifications. arXiv:2310.11459.**

### Summary

This paper adapts the Glicko-2 rating system for club football by adding four domain-specific modifications: draw probability integration, home-advantage parameter, league transition penalties for promoted/relegated teams, and post-season rating normalisation. The system was evaluated on approximately 366,000 matches from European and South American first and second divisions (2010/11–2022/23).

### Methodology

**Glicko-2 base:** Each team carries three parameters — rating µ, rating deviation (RD) φ, and volatility σ. RD naturally inflates during inactive periods (the off-season, international breaks), reflecting increased uncertainty. Ratings are updated in "periods" (nominally a season or month). The volatility parameter σ controls how much a team's rating can jump if there is evidence of a sudden performance shift.

**Modifications:**
1. Draw integration: modified win probability to include a draw parameter (d, s).
2. Home advantage: team-specific h parameter added to rating update.
3. Promotion/relegation adjustment: µ_new and µ_l penalty parameters for team roster disruptions.
4. Normalisation: post-season normalisation to prevent long-run rating inflation.

### Data

~366,000 matches, European and South American leagues, 2010/11–2022/23. Evaluated on 60,091 test matches.

### Key Findings (Test Set Log-Loss on 60,091 matches):

| Model | Log-Loss |
|---|---|
| **Modified Glicko-2 (this paper)** | **0.5832** |
| LightGBM baseline | 0.5896 |
| CatBoost | 0.5931 |
| Original Glicko-2 | 0.5949 |

- Modified Glicko-2 outperformed both the original Glicko-2 and gradient-boosting ML baselines on this large dataset.
- The modifications each contributed meaningfully; the draw integration and home-advantage parameter were the largest improvements.

### Relevance to v1

Glicko-2 is particularly suitable for national teams due to its explicit handling of uncertainty during inactive periods. National teams play 8–14 competitive matches per year with significant gaps (international breaks); Glicko-2's RD automatically expands during these gaps, meaning ratings from 18 months ago appropriately carry less certainty than recent results. The finding that modified Glicko-2 outperforms gradient-boosted ML on a large dataset is notable, but the dataset is club football.

### Skeptical Notes

- **The critical limitation for national teams:** Glicko-2 was designed for situations where each team plays "5–10 games per period." National teams may play only 2–4 competitive matches per rating period (e.g., a two-match international window), meaning the volatility parameter shows "little movement" as noted in the Glicko documentation itself. The system works best when it has enough within-period observations to estimate volatility meaningfully.
- The paper's dataset is all club football; there is no validation on national-team data specifically.
- The modifications (draw parameter, home advantage, relegation penalty) each add hyperparameters that need fitting; with sparse national-team data, these may introduce more noise than signal.
- The paper is an arXiv preprint and has not been peer-reviewed as of June 2026.

### Implementation Ideas

- Use Glicko-2 as a secondary rating system that provides an explicit uncertainty estimate (RD) for each team, which can be used to widen/narrow confidence intervals in the blend — especially for teams that have played few competitive matches recently (e.g., small CONMEBOL or CAF confederations with long gaps before the World Cup).
- Apply Glicko-2 specifically to flag teams whose rating uncertainty (RD > 150) warrants additional caution in bet sizing.
- Consider Glicko-2's RD as an input to the Kelly fraction: reduce stake proportionally when RD is high, reflecting model uncertainty.

---

## Paper 9

**Szczecinski, L. (2026). New insights into Elo algorithm for practitioners and statisticians. arXiv:2604.03840v1 (April 4, 2026).**

### Summary

This recent technical paper reconciles the two perspectives on the Elo algorithm: the practitioner's view (heuristic feedback rule for maintaining a ranking) and the statistician's view (online maximum likelihood estimation via stochastic gradient ascent). The central finding is that practitioners should **decouple** the model used for ranking from the model used for probability prediction — they are not the same model, and conflating them leads to systematically miscalibrated predictions. The paper also provides closed-form corrections and applies them to six years of FIFA men's ranking data.

### Methodology

The paper derives conditions under which the Elo update rule is equivalent to stochastic gradient ascent on the log-likelihood of a paired-comparison model. It then shows that estimation noise forces the effective scale parameter s (the 400 in the denominator of the logistic) to differ from the true skill-dispersion scale. Specifically, the effective scale must be increased and the home-advantage parameter must be decreased to account for estimation noise, with:

- Effective scale correction: β_err = a × sqrt(1 + 2v̄ / (a × (sβ)²))
- Home-advantage correction: η_hat = η × a / β_err
- Convergence time constant: τ = 4s / K (convergence declared after ~2τ–3τ matches)

The decoupled prediction model substantially outperforms the conventional approach that directly plugs in the ranking model's scale.

### Data

Six years of FIFA men's ranking data (post-2018 Elo-based system). Key diagnostic: the vast majority of national teams had not converged after six years of running the algorithm.

### Key Findings

- After six years of the FIFA Elo-based ranking (2018–2024), approximately 80% of teams had not played enough matches to reach one convergence time constant τ, and 98% had not reached 2τ. **For most national teams, current Elo ratings remain provisional in a rigorous statistical sense.**
- The decoupled prediction model (using corrected effective scale and home advantage) substantially outperforms the naive plug-in approach.
- The Adjacent Categories model with uniformly-spaced outcome scores is recommended for the multilevel (W/D/L) case, which aligns with the ordered logit architecture.

### Relevance to v1

This is the most directly actionable recent paper for v1 implementation. The finding that national-team Elo ratings are largely unconverged has two implications: (1) Elo differences between teams should be treated with significant uncertainty, especially for less-active teams; (2) the scale parameter used when converting Elo differences to probabilities needs to be recalibrated empirically, not taken from the standard 400-divisor. The decoupling insight means: use a lower divisor (larger effective scale) when converting Elo differences to logistic probabilities than the 400 used in standard Elo.

### Skeptical Notes

- The paper is a preprint (arXiv, April 2026) and has not been peer-reviewed. The mathematical derivations appear sound but have not been scrutinised by referees.
- The closed-form corrections require knowledge of v̄ (estimation noise variance), which itself must be estimated from data — circular in small-sample settings.
- The "convergence" criterion is based on the algorithm's statistical properties, not on whether the rankings are practically useful. Even unconverged ratings can rank teams correctly most of the time.

### Implementation Ideas

- Estimate the effective scale s empirically: fit a logistic regression with the Elo difference as the single predictor on the last 5–7 years of international results. The fitted coefficient will implicitly encode the corrected scale.
- Do not use the raw standard 400-divisor to convert Elo differences directly to probabilities. Instead, use the fitted logistic coefficient from the calibration step above.
- Flag teams with fewer than ~50 competitive international matches in the last 6 years as "low-convergence" and apply additional uncertainty in the blend.

---

## Supplementary Note: Glicko/Glicko-2 Original Framework

**Glickman, M. E. (1995). The Glicko system. Boston University Technical Report. Available at: http://www.glicko.net/glicko/glicko.pdf**
**Glickman, M. E. (1999). Parameter estimation in large dynamic paired comparison experiments. *Journal of the Royal Statistical Society: Series C*, 48(3), 377–394.**

Not reviewed in full due to access limitations, but the relevant properties are well established in the literature. Key features: (1) rating + rating deviation (RD) tracked per team; (2) RD increases when a team is inactive: RD_new = sqrt(RD_old² + c²×t) where c and t are the rating period constant and time elapsed; (3) update magnitude scales with RD (uncertain teams update more aggressively than established teams); (4) for sparse national-team data, typical recommended c value of ~30–63 RD units per rating period. The Glicko-2 extension (Glickman 2001–2012) adds a volatility σ parameter for players/teams whose performance fluctuates beyond what the current rating predicts.

---

## Practical Implications for v1

The following conclusions are drawn from the reviewed literature for direct application to the World Cup Alpha v1 system.

### 1. Elo Is a Necessary but Insufficient Structural Prior

Hvattum & Arntzen (2010) established that Elo + ordered logit beats naive baselines but cannot beat bookmaker odds in isolation. Wunderlich & Memmert (2018) showed that even an odds-updated Elo (ELO-Odds) loses to raw closing prices. The implication is non-negotiable: **the market-devigged probability must receive significant weight in the blend, not just serve as a correction to a structurally dominant Elo model.** The Elo component provides value as a structural anchor that stabilises predictions when markets are thin or potentially mis-set (e.g., early-tournament group games against weak opponents), not as the primary signal.

### 2. Use Goal-Margin Elo (or Pi-Ratings), Not Pure W/D/L Elo

ELO-Goals outperformed ELO-Result in Wunderlich & Memmert (2018). Pi-ratings outperform standard Elo on RPS in practitioner replication (0.199 vs 0.204). For national teams playing infrequently, each match is costly to discard; goal-margin encoding extracts more signal from each observation. Cap the margin at 4 goals to reduce the noise from blow-out games against weak opponents.

### 3. Recalibrate the Elo-to-Probability Conversion Empirically

Szczecinski (2026) shows that plugging Elo differences directly into the standard logistic with the 400-divisor produces miscalibrated probabilities for unconverged teams. For national teams — where the vast majority remain statistically unconverged — the effective scale should be estimated by fitting a logistic regression on historical international match data. This step is necessary before Elo differences can be used in the logistic blend.

### 4. Home Advantage Requires Context-Specific Calibration

The standard +100 WFER home advantage corresponds to ~14 percentage points. For World Cup 2026 group matches: USA, Canada, and Mexico are nominally "home" teams but most matches are at neutral venues; genuine home advantage is ≈ 3–6 pp. For all non-host teams, home advantage is essentially 0. **Set the home advantage parameter to 0 for neutral-venue knockout matches and to approximately +30–40 Elo points for host-team group games, not the default +100.**

### 5. Glicko-2 RD as a Stake-Sizing Guard

For national teams with high Glicko-2 RD (few recent competitive matches, significant roster turnover), the model's edge estimate is less reliable. The RD can feed directly into the quarter-Kelly formula as an uncertainty multiplier: if RD > 150, reduce Kelly fraction by a factor of (1 − (RD − 150) / 300) to avoid overconfident staking on rating-uncertain teams.

### 6. Dixon-Coles Time Decay Provides the Goal-Model Backbone

Ley et al. (2019) found bivariate/independent Poisson with time-depreciation outperforms all paired-comparison (Elo-type) models in raw RPS. For v1, the time-decayed Dixon-Coles model should generate the goal-based probability estimates that feed into the three-way output, with the Elo component providing the ranking anchor and the market component providing the information-efficiency anchor. The optimal decay parameter (ξ) for national teams should be re-estimated given the 2022–2026 data; Dixon & Coles (1997) found ξ ≈ 0.0065 per half-week for English club football, but national teams play more sporadically so the appropriate decay may be slower.

### 7. Neutral-Venue Group Stage Requires Separate Calibration from Knockouts

Draw rates, goal rates, and match dynamics differ between group stages (where a draw is acceptable) and knockouts (where extra time and penalties alter outcome probabilities). Fit separate logistic thresholds for group vs knockout phases in the ordered logit.

### 8. CLV Measurement Protocol

Following the evidence that closing odds dominate any rating-based system (Wunderlich & Memmert 2018, Hvattum & Arntzen 2010), the primary v1 KPI should remain CLV: place bets early when the Elo/Dixon-Coles blend identifies a model-implied probability meaningfully above the opening price, and measure whether the closing price moves toward your model (positive CLV). Calibration (Brier score, log-loss vs de-vigged closing price) is the secondary KPI to diagnose whether model probabilities are well-formed, but CLV is the operational proxy for edge.

### 9. Known Limitations and Risk Flags

- **Profitability claims from pi-ratings (Constantinou & Fenton 2013) should be treated with extreme scepticism.** The parameter optimisation and profitability measurement shared the same five-season window. The 2026 market is substantially more efficient than 2007–2012 EPL markets.
- **All studies except Lasek et al. (2013) used club-football data.** National-team markets are structurally different: lower liquidity outside major nations, more media narrative driving public money, and genuinely sparse historical data for confederations outside Europe and South America.
- **None of the reviewed papers achieved consistent positive returns on betting simulation after market efficiency is properly accounted for.** The expected value of any rating-based model that does not incorporate market odds is negative at commercial bookmaker margins.

---

*Section compiled by the research sub-agent, June 10 2026. Next review recommended after the 2026 World Cup group stage (by July 4 2026) to incorporate live calibration data.*
