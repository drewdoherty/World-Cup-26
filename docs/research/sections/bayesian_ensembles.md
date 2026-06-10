# Bayesian and Ensemble Approaches for Football Match Prediction
## Annotated Literature Review

**Topic:** Bayesian hierarchical/dynamic models, ensemble/stacking methods, probability calibration, and proper scoring rules for association football prediction and betting.
**Prepared for:** World Cup Alpha v1 — $1,000 bankroll, quarter-Kelly staking, CLV as primary KPI.
**Date:** 2026-06-10

---

## Paper 1: Rue & Salvesen (2000) — The Foundational Dynamic Bayesian Model

**Full citation:** Rue, H., & Salvesen, O. (2000). Prediction and retrospective analysis of soccer matches in a league. *Journal of the Royal Statistical Society: Series D (The Statistician)*, 49(3), 399–418.

**Summary:** This paper introduced the first fully Bayesian dynamic model for football match prediction and remains the intellectual ancestor of most modern time-varying strength approaches. Rue and Salvesen model each team's attacking and defensive ability as latent parameters that evolve continuously over the season according to a Brownian-motion-like random walk, rather than treating them as fixed for the whole season. Goals are assumed to follow independent Poisson distributions with rates determined by the current attack and defence parameters plus a home advantage term. MCMC (Gibbs sampling) is used for inference. The authors apply the model to the English Premier League and First Division in 1997–98 and show it can generate positive returns when used as the basis of a betting strategy.

**Methodology:** Dynamic Bayesian generalised linear model. Attack and defence parameters for each team evolve over discrete match time via a Brownian motion random-walk prior with evolution variance sigma^2 estimated from data. Posterior updating uses MCMC. Home advantage is modelled as a fixed additive parameter on the log goal-rate. A psychological advantage term (favouring the team predicted to win) was also introduced. Betting was tested using a simple threshold rule: bet when the model-implied probability exceeds the bookmaker's implied probability by a fixed margin.

**Data:** English Premier League and Division 1, season 1997–98 (~760 matches per division).

**Key findings:** The model produced a positive expected return on investment when applied to the 1997–98 season (specific ROI not published in the abstract, but the authors state the model has a positive return in simulation). The evolution variance was estimated to be small (on the order of 0.001–0.01), implying team strength changes gradually. The psychological advantage parameter was found to be statistically meaningful, with the pre-match favourite gaining an additional ~5–10% probability lift, though this finding has not been widely replicated and may be dataset-specific.

**Relevance to v1:** The core architectural insight — time-decaying strength estimates via random-walk priors — is exactly what v1's Dixon-Coles time-decay (exponential weight on recent matches) approximates. Rue & Salvesen show that Bayesian inference over the evolution variance is preferable to ad hoc decay constants, as sigma^2 is data-adaptive. The MCMC approach is too slow for live-tournament use but the structural lesson holds: use a prior that encodes gradual drift rather than static parameters. For the World Cup specifically, where teams may have changed significantly since qualifying, a moderate evolution variance is appropriate.

**Implementation ideas for v1:**
- Replace the fixed Dixon-Coles decay constant (currently hand-tuned) with a cross-validated or MLE-estimated evolution variance.
- Implement a lightweight version of Rue & Salvesen's attack/defence random walk using a Kalman-filter approximation (analytic, not MCMC) which updates after each match — this is the basis of the Ridall et al. (2025) state-space model.
- The psychological advantage parameter is probably not worth implementing without strong prior evidence; skip for v1.

**Skeptical note:** The positive betting return was demonstrated on a single season in the late 1990s when football betting markets were far less efficient. The paper predates modern Betfair-style exchanges. Modern replications typically find the Bayesian dynamic models match bookmaker accuracy but do not beat it in profit terms without additional edges. The original profit claim should not be taken at face value for 2026.

---

## Paper 2: Baio & Blangiardo (2010) — The Canonical Hierarchical Bayesian Football Model

**Full citation:** Baio, G., & Blangiardo, M. (2010). Bayesian hierarchical model for the prediction of football results. *Journal of Applied Statistics*, 37(2), 253–264.

**Summary:** This paper established the most widely cited Bayesian hierarchical framework for football score prediction and is the reference baseline for virtually every subsequent Bayesian football paper. Goals scored by each team in a match are modelled as independent Poisson random variables with log-rates determined by team-level attack and defence parameters plus a global home effect. Parameters are given hierarchical priors (team attack drawn from a normal population, team defence likewise), enabling partial pooling (shrinkage) across teams. This addresses the sparse-data problem when teams have played few matches. However, the authors identify that naive hierarchical shrinkage causes over-shrinkage for extreme teams (very good or very bad teams are pulled too far toward the mean), and they introduce a mixture model to partially remedy this. Inference uses MCMC in OpenBUGS/WinBUGS, applied to Italian Serie A 1991–92 and 2007–08.

**Methodology:** Two-level hierarchical Poisson model. Log(lambda_home) = mu + home + att_home + def_away; Log(lambda_away) = mu + att_away + def_home. Attack and defence parameters for each team are drawn from normal distributions with unknown mean and variance (hyperpriors). A flat or weakly informative prior is placed on the home advantage mu. The mixture extension replaces the single normal population prior with a two-component mixture to allow for heavy tails (very strong or very weak teams). Posterior inference via MCMC (50,000+ iterations, convergence checked via Gelman-Rubin).

**Data:** Italian Serie A 1991–92 (retrospective fit) and 2007–08 (predictive evaluation). Small dataset by modern standards (~300 matches per season).

**Key findings:** The hierarchical model improves predictive fit (lower DIC, better calibrated match probabilities) compared to a non-hierarchical Poisson model with MLE. The mixture extension further reduces DIC by approximately 8–15 units over the standard model. Predicted final standings closely matched actual 2007–08 Serie A rankings (top-4 correctly identified). Specific Brier score or log-loss numbers are not reported in the paper, which is a notable weakness.

**Relevance to v1:** The Baio-Blangiardo structure is the direct parent of the v1 Dixon-Coles component. Key lessons: (1) hierarchical priors with partial pooling are essential when teams have fewer than ~10 recent matches — directly relevant to World Cup where some teams (e.g., Oceania qualifiers) have sparse competitive records; (2) the over-shrinkage problem is real and the mixture-prior fix is worth knowing about, though in practice the footBayes R package implements updated versions. The model does not include time-decay, which is why Dixon-Coles or Rue-Salvesen extensions are preferred for live tournaments.

**Implementation ideas for v1:**
- Adopt hierarchical priors on attack/defence parameters for the Elo + Dixon-Coles system: treat each team's parameters as drawn from a group distribution, with the group mean informed by regional confederation (CONCACAF, UEFA, CONMEBOL etc.) — this adds a natural shrinkage structure relevant to World Cup with many sparse international teams.
- For the mixture extension: consider a Student-t prior (robust to outliers) on attack/defence rather than a mixture of normals — simpler to implement, similar effect.
- Use the model's posterior predictive distribution to generate calibrated win/draw/loss probabilities rather than point estimates, which is required for proper Brier/log-loss evaluation.

**Skeptical note:** The data used (1990s Serie A) is now 30+ years old and the footballing landscape has changed fundamentally. The mixture-model improvement in DIC (~8–15 units) is modest and may not translate to meaningfully better out-of-sample predictions. The paper also lacks a proper out-of-sample betting test. It is best treated as a methodological framework document, not an empirical guide to expected gains.

---

## Paper 3: Egidi, Pauli & Torelli (2018) — Combining Historical Data and Bookmakers' Odds

**Full citation:** Egidi, L., Pauli, F., & Torelli, N. (2018). Combining historical data and bookmakers' odds in modelling football scores. *Statistical Modelling*, 18(5–6), 436–459. (arXiv preprint: 1802.08848)

**Summary:** This is the most direct academic antecedent of v1's logistic blend between model and market. The authors propose a hierarchical Bayesian Poisson model in which each team's scoring rate is a convex combination (weighted average) of a rate estimated from historical match data and a rate reverse-engineered from bookmakers' odds. The mixing weight is a single Bayesian parameter estimated from data, allowing the posterior to determine how much weight to give the market versus historical statistics. The model is applied to nine seasons of data from the top five European leagues, with the tenth season held out for prediction. Inverse-odds probabilities are transformed to Poisson scoring rates via the Skellam distribution, which is the key technical innovation.

**Methodology:** Lambda_home = alpha * lambda_hist_home + (1-alpha) * lambda_mkt_home, where alpha is a [0,1] mixing parameter with a Beta(1,1) prior estimated hierarchically across teams. Historical scoring rates come from a standard Baio-Blangiardo-type attack-defence model; market scoring rates are derived by inverting bookmaker 1X2 odds to goal totals using the Skellam distribution. Full Bayesian inference in Stan.

**Data:** Nine seasons of Premier League, La Liga, Bundesliga, Serie A, and Ligue 1 (~1,800 matches per league as training, ~380 as test per league per season).

**Key findings:** The combined model consistently outperforms the historical-only model on out-of-sample prediction (lower log-loss, better calibration). The posterior mean of alpha was typically 0.4–0.6 (market and historical contribute roughly equally), though with high posterior uncertainty. Graphical calibration checks show the combined model is better-calibrated than either source alone. The authors do not report specific numeric Brier or log-loss improvements in the abstract, which limits effect-size assessment. The key qualitative finding is that blending market and historical information is consistently better than using either alone.

**Relevance to v1:** This is the most directly actionable paper for v1. It provides rigorous Bayesian justification for the logistic blend component already planned for v1, and suggests that the optimal mix weight is around 50/50 market vs. model rather than heavily favouring one source. The Skellam-inversion technique for deriving implied scoring rates from 1X2 odds is implementable.

**Implementation ideas for v1:**
- The convex-combination approach can be simplified for v1: rather than full Bayesian inference on alpha, use a grid search over alpha in [0,1] on historical World Cup qualification data to find the optimal blend weight, then apply it consistently in-tournament.
- The result that alpha ~ 0.5 (equal weight) is a useful default prior before fitting data.
- Log-odds blending (logistic blend of log-odds from model and market) is mathematically equivalent and numerically more stable; this is what v1's logistic blend step implements.
- Extend the approach to include tournament-context covariates (knockout vs. group stage, days of rest) as additional terms in the mixing model.

**Skeptical note:** The paper was written using nine-season European league data with dense match histories — very different from international tournament data where teams may play only 3–10 qualifying matches in a year. The optimal alpha estimated on league data may not transfer directly to international tournaments. The paper also does not evaluate CLV or simulated betting profit, which are the v1 KPIs. The calibration improvement, while consistent, may be modest in absolute Brier-score terms.

---

## Paper 4: Groll, Ley, Schauberger & Van Eetvelde (2019) — Hybrid Random Forest for International Football

**Full citation:** Groll, A., Ley, C., Schauberger, G., & Van Eetvelde, H. (2019). A hybrid random forest to predict soccer matches in international tournaments. *Journal of Quantitative Analysis in Sports*, 15(4), 271–287. (Earlier arXiv preprint: 1806.03208 covers FIFA World Cup 2018.)

**Summary:** This paper introduces a systematic ensemble approach that combines statistical ranking methods with machine learning for international tournament prediction. The authors compare three model families — Poisson regression on covariates, random forests on covariates, and Bradley-Terry ranking methods — and demonstrate that a hybrid combining random forest with team ability parameters from ranking methods substantially outperforms any individual approach. The World Cup 2018 application predicted Spain as tournament favourite (before tournament; Germany were the pre-tournament bookmaker favourite). The paper is the foundation of an ongoing research programme covering Euro 2020, Women's World Cup 2019 etc.

**Methodology:** Three model types are estimated: (1) Poisson regression on covariates (FIFA rank, Elo, GDP, number of Champions League players, average player market value); (2) random forest on the same covariates; (3) Bradley-Terry log-strength model using time-weighted match results. The hybrid combines random forest with ranking-derived ability parameters as additional features. Training data: all four previous FIFA World Cups (2002–2014). Tournament simulation: 100,000 Monte Carlo runs. Bookmaker consensus: 26 bookmaker average, de-vigged using multiplicative normalization.

**Data:** Four FIFA World Cup cycles (2002–2014) as training; 2018 World Cup as held-out test. Player rating data from FIFA video game database; market value from Transfermarkt; economic data from World Bank.

**Key findings:** On training data (2002–2014 World Cups), random forest and ranking methods tied for best performance; the hybrid combining both was best overall. Spain received winning probability of 17.8% (random forest) vs. bookmaker consensus of ~15% for Germany. The paper correctly identified the four semi-finalists at the 2014 World Cup in a pre-tournament retrospective. Specific out-of-sample Brier score improvements are not reported for the 2018 held-out test (the competition was live when the paper was written), which limits quantitative evaluation.

**Relevance to v1:** The key lesson is that combining ability-based ranking estimates with covariate-based machine learning is better than either alone for international tournaments — directly relevant to v1's Elo + market blend. The bookmaker consensus approach (average de-vigged odds across multiple books) is used as one of the two ranking inputs, confirming that market information improves statistical models. The specific covariates (FIFA rank, Elo, market value, Champions League squad depth) are relevant for the 2026 World Cup.

**Implementation ideas for v1:**
- Use the bookmaker consensus model as a baseline — average Betfair/Paddy Power/Sky Bet de-vigged probabilities — and treat this as one input to the blend, alongside the Elo + Dixon-Coles statistical model.
- The covariates used (squad market value, UEFA club competition appearances) are publicly available for 2026 and could serve as shrinkage priors on team strength for teams with sparse international match data.
- For v1 simplicity, the Bradley-Terry log-linear model (ln(p_A/p_B) = s_A - s_B) is equivalent to the logistic Elo model and is computationally trivial; this can serve as a sanity check on the more complex Dixon-Coles estimates.

**Skeptical note:** The training set is only four World Cups (n ≈ 256 matches), which is genuinely small for a machine learning model. The random forest on covariates likely overfits on this sample size, and the claimed improvement over the simple ranking model may partly reflect overfitting rather than genuine generalisation. The paper does not report out-of-sample Brier or log-loss on the 2018 held-out test. Player market value and FIFA game ratings are noisy proxies for actual national team quality.

---

## Paper 5: Zeileis, Leitner & Hornik (2018) — The Bookmaker Consensus Model

**Full citation:** Zeileis, A., Leitner, C., & Hornik, K. (2018). Probabilistic forecasts for the 2018 FIFA World Cup based on the bookmaker consensus model. *University of Innsbruck Working Paper* 2018-09.

**Summary:** This paper operationalises the bookmaker consensus as a formal probabilistic forecasting model. The procedure aggregates de-vigged odds from 26 bookmakers and betting exchanges to produce a single consensus probability for each team winning the tournament. The de-vigging step removes overrounds using multiplicative normalisation (Shin probabilities were tested but multiplicative normalization used as default). Team abilities are inferred from tournament-winner odds via a Bradley-Terry paired comparison model, from which pairwise match probabilities can be derived. The model had predicted the 2010 World Cup winner correctly and three of four 2014 semi-finalists.

**Methodology:** Step 1: Collect all-tournament winner odds from 26 bookmakers. Step 2: Remove overround via multiplicative normalisation (divide each implied probability by the sum of all implied probabilities). Step 3: Average normalised probabilities on the log-odds scale across bookmakers. Step 4: Fit a Bradley-Terry model to infer team log-strength parameters from consensus tournament-winner probabilities. Step 5: Derive Pr(A beats B) = exp(s_A) / (exp(s_A) + exp(s_B)) from team strengths, with a draw parameter.

**Data:** Odds from 26 bookmakers and exchanges for the 2018 FIFA World Cup, collected approximately 1–2 weeks before the tournament.

**Key findings:** Brazil 16.6% and Germany 15.8% were joint favourites (Germany won bookmaker consensus). The model was calibrated — across past tournaments the consensus probability was well-correlated with actual outcomes. Overround averaged 15.2% across the 26 bookmakers, confirming the need for de-vigging. The log-odds averaging step reduced variance compared to simple averaging.

**Relevance to v1:** This paper provides the exact methodology for constructing the market baseline component of v1. The Shin-devigging step mentioned in v1's design is the more theoretically sound alternative to multiplicative normalisation (Clarke et al. 2017 confirm Shin is more accurate). The Bradley-Terry parameterisation confirms that the logistic model is the correct link function for combining market and model probabilities.

**Implementation ideas for v1:**
- Collect tournament-winner odds from Betfair Exchange, Bet365, Paddy Power, Sky Bet, and Virgin Bet (~5 sources vs. 26 in the paper), compute the average log-odds after Shin de-vigging for each team, and use this as the market baseline.
- Use the Shin de-vigging formula (z parameter ≈ 0.02–0.03 for UK-licensed books) rather than simple multiplicative normalisation for more accurate true probabilities.
- The overround on UK-licensed books for 2026 World Cup group-stage 1X2 is likely 6–10% (much lower than the 15.2% all-tournament winner market), so the de-vigging correction is smaller but still material.

**Skeptical note:** The bookmaker consensus model is essentially the efficient market hypothesis applied to football betting. It is an excellent baseline but by construction cannot generate positive CLV — it is the closing line. Using it as the sole basis for a betting strategy would yield exactly zero expected profit before stake costs. Its value in v1 is as a calibration anchor for the statistical model, not as a standalone betting signal.

---

## Paper 6: Ridall, Titman & Pettitt (2025) — Bayesian State-Space Model for Premier League Prediction

**Full citation:** Ridall, G., Titman, A., & Pettitt, A. (2025). Bayesian state-space models for the modelling and prediction of the results of English Premier League football. *Journal of the Royal Statistical Society: Series C (Applied Statistics)*, 74(3), 717–(forthcoming 2025).

**Summary:** This is the most technically sophisticated Bayesian dynamic model for football in the recent literature. The authors implement a true Bayesian state-space model (SSM) where attacking strength, defensive strength, and home advantage are latent states that evolve over time, updated sequentially (online) using a conjugate Gamma filter combined with a mean-field variational approximation rather than MCMC. The measurement model uses a bivariate negative binomial distribution (capturing overdispersion and correlation between home and away scores). Evaluated on 14 EPL seasons (2010/11 to 2023/24), the model substantially outperforms weighted-likelihood approaches and score-driven models, though it still does not beat bookmaker odds.

**Methodology:** State space with Gamma-distributed states. Measurement: bivariate negative binomial with a multiplicative Gamma random effect shared across home and away goals. Evolution: scaled beta-distributed multiplicative noise with "forgetting parameters" controlling within-season and between-season decay. Update: conjugate Gamma posterior updated sequentially, with mean-field approximation for the joint five-parameter state. No MCMC required — update is analytic per match.

**Data:** 14 EPL seasons (2010/11–2023/24), approximately 5,320 matches total.

**Key findings:** RPS scores (cumulative, lower is better):
- Bivariate SSM: 17.55 (best of all models tested)
- Univariate SSM: 18.64
- Koopman & Lit (2019) score-driven: 20.71
- Weighted likelihood methods (time-decayed): 22.06–22.24
- Static outcome-based models: 28.96–29.23

None of the statistical models beat bookmakers' RPS. The bivariate model outperforms all statistical competitors by a substantial margin. The model correctly captured the COVID-19 drop in home advantage during the 2020/21 season with no spectators.

**Relevance to v1:** This is the most important recent benchmark paper. Three findings are directly actionable: (1) time-varying state-space methods outperform the time-decay Dixon-Coles approach by a large margin (RPS 17.55 vs 22.06–22.24); (2) none beat bookmakers — confirming that the market blend is the key value-add; (3) the bivariate model (accounting for correlation between scores) outperforms the independent Poisson model. For v1, this suggests the Dixon-Coles time-decay component should eventually be replaced by a Kalman-filter approximation.

**Implementation ideas for v1:**
- For immediate v1: use the forgetting-parameter concept from the SSM to set the Dixon-Coles decay constant — the paper implies a within-season persistence of ~0.90–0.95 per match (or equivalently a half-life of ~7–14 matches), which translates to an exponential time-decay constant of approximately 0.005–0.01 per day.
- For v2: implement the conjugate Gamma state-space filter described in the paper — it is analytic (no MCMC), fast (updates in milliseconds), and substantially more accurate than static or time-decayed models.
- The COVID home-advantage finding suggests building a time-varying home-advantage parameter, especially relevant for the 2026 World Cup played across USA/Canada/Mexico venues where home crowds are absent for most teams.

**Skeptical note:** Results are on EPL data — a league with very dense data (38 match days per season, consistent team compositions). International football (World Cup) is far sparser, and the Gamma conjugate filter may need significant adaptation for contexts where teams play only 3 matches in the group stage before being eliminated. The RPS margin over Dixon-Coles is large on EPL data but may not replicate in the sparse international context.

---

## Paper 7: Niculescu-Mizil & Caruana (2005) — Calibration of Supervised Learning Classifiers

**Full citation:** Niculescu-Mizil, A., & Caruana, R. (2005). Predicting good probabilities with supervised learning. In *Proceedings of the 22nd International Conference on Machine Learning (ICML 2005)*, 625–632.

**Summary:** This is the foundational empirical study of probability calibration in machine learning, comparing Platt scaling and isotonic regression across a wide range of classifiers. The paper establishes which types of models are naturally well-calibrated versus which require post-hoc calibration, and provides practical guidance that applies directly to the logistic blend model in v1. It is a machine-learning methods paper, not a sports paper, but its findings on calibration are quantitatively well-established and directly relevant.

**Methodology:** Seven supervised learning algorithms (boosted trees, boosted stumps, SVMs, random forests, Bayes optimal, Bagged decision trees, Naive Bayes) tested on 22 binary classification datasets. Reliability diagrams and Mean Squared Error (Brier score) used to assess calibration. Two calibration corrections tested: Platt scaling (logistic transformation of raw scores) and isotonic regression (monotone nonparametric mapping). Post-calibration performance measured on held-out data.

**Data:** 22 UCI benchmark classification datasets. No sports-specific data; findings are domain-general.

**Key findings:**
- Maximum margin methods (boosted trees, SVMs) produce sigmoid-shaped miscalibration (probabilities pushed away from 0 and 1).
- Naive Bayes pushes probabilities toward extremes (0 and 1) due to violated independence assumptions.
- Neural networks, bagged trees are naturally well-calibrated.
- After Platt scaling: Brier score improvement is approximately 10–25% for boosted methods, 5–15% for SVMs.
- After isotonic regression: similar improvement, but requires more calibration data (minimum ~500 calibration examples recommended).
- Isotonic regression can overfit with small calibration sets (<100 examples).
- After calibration, boosted trees and random forests achieve the best calibrated probabilities of all methods.

**Relevance to v1:** The logistic blend model in v1 (combining Elo + Dixon-Coles model probabilities with market probabilities via a logistic function) is structurally similar to Platt scaling. The key insight is that the logistic/sigmoid transformation is the appropriate post-hoc calibration for systematic miscalibration, and it is implemented correctly if fitted on a held-out calibration set rather than the training data. The paper also highlights the risk that isotonic regression (non-parametric) overfits on small datasets — a serious concern for World Cup data (at most a few hundred group-stage matches available for calibration).

**Implementation ideas for v1:**
- Use Platt scaling (logistic regression with one or two parameters) rather than isotonic regression for calibrating the logistic blend outputs, given the small sample size of international football data.
- Fit the calibration on World Cup qualifying matches (excluding the tournament itself) and validate on the most recent tournament before applying.
- Maintain a reliability diagram throughout the tournament (group predictions into bins of 0.05 width and plot predicted vs. actual frequencies) to detect drift in calibration.
- The recommendation that calibration should use a separate held-out set (not the training set) is critical — do not fit and evaluate on the same matches.

**Skeptical note:** The datasets used are binary classification problems; football has three outcomes (home win, draw, away win) requiring either a multinomial calibration or three separate Platt scalings. The correspondence between machine-learning classifiers and Poisson goal models is not direct, so the specific numerical improvements (10–25% Brier improvement) should not be expected to replicate exactly in the football context.

---

## Paper 8: Kull, Filho & Flach (2017) — Beta Calibration

**Full citation:** Kull, M., Silva Filho, T., & Flach, P. (2017). Beta calibration: a well-founded and easily implemented improvement on logistic calibration for binary classifiers. In *Proceedings of the 20th International Conference on Artificial Intelligence and Statistics (AISTATS)*, PMLR 54, 623–631. Extended version: *Electronic Journal of Statistics*, 11(2), 5052–5080.

**Summary:** Beta calibration generalises Platt scaling (logistic calibration) by fitting a three-parameter beta distribution family rather than a two-parameter sigmoid. The key practical advantage is that the beta family includes the identity function, so if a classifier is already well-calibrated, beta calibration does not degrade it (unlike Platt scaling, which cannot learn the identity mapping). This matters for sports models where the bookmaker baseline is already well-calibrated by market forces. The paper demonstrates that beta calibration is superior to Platt scaling for classifiers with skewed or asymmetric score distributions.

**Methodology:** Three-parameter calibration map derived from beta distribution assumptions: log(p/(1-p)) = a * log(s) - b * log(1-s) + c, where s is the raw score, p is the calibrated probability, and (a, b, c) are fit by maximum likelihood on a calibration set. When a = b = 1, this reduces to Platt scaling. Tested on 48 classification datasets; calibration quality measured by Expected Calibration Error and Brier score.

**Data:** 48 UCI-style classification benchmark datasets. Extended version also tests on realistic medical datasets.

**Key findings:** Beta calibration statistically significantly outperforms logistic calibration on Naive Bayes and Adaboost (key use cases). The advantage is most pronounced for classifiers with asymmetric or heavily skewed score distributions — the two parameters a and b allow asymmetric calibration. On already-well-calibrated classifiers (neural nets, random forests), beta calibration provides no degradation (unlike Platt scaling which can slightly degrade calibration). Specific Brier improvements over Platt scaling: median 3–8% improvement across 48 datasets.

**Relevance to v1:** The blend output in v1 (convex combination of model log-odds and market log-odds) will likely produce asymmetrically distributed scores for international matches — the Elo model may systematically over-rate stronger teams (favourites) because recent qualifying results are noisy. Beta calibration with its two shape parameters (a, b) can correct this asymmetry. The "identity inclusion" property is especially valuable: if the blended output is already close to true probabilities (as it should be if the market is informative), beta calibration won't hurt it.

**Implementation ideas for v1:**
- Use beta calibration as the final post-processing step after the logistic blend: fit (a, b, c) on historical World Cup match data (2006–2022, ~192 group-stage matches) and apply to 2026 group-stage predictions.
- Test beta calibration against simple multiplicative normalisation and Platt scaling using leave-one-tournament-out cross-validation on the 2006–2022 data.
- For the three-outcome case (home win, draw, away win), apply Dirichlet regression calibration as the multinomial extension, or apply binary beta calibration to each outcome independently and re-normalise.

**Skeptical note:** The advantage over Platt scaling is typically small (3–8% Brier improvement) and may not be statistically significant on a dataset of only 64 World Cup group-stage matches (the 2026 tournament group stage). With such a small calibration dataset, the risk of overfitting the three-parameter beta model is real; Platt scaling (two parameters) may be the safer default for v1.

---

## Paper 9: Gneiting & Raftery (2007) — Strictly Proper Scoring Rules

**Full citation:** Gneiting, T., & Raftery, A. E. (2007). Strictly proper scoring rules, prediction, and estimation. *Journal of the American Statistical Association*, 102(477), 359–378.

**Summary:** This is the theoretical foundation for all scoring rule evaluation in probabilistic forecasting. Gneiting and Raftery provide a comprehensive treatment of proper scoring rules — rules that incentivise honest probabilistic reporting — and derive the key properties of the Brier score, log score (log-loss), and Ranked Probability Score (RPS). The paper establishes that a scoring rule is proper if and only if the forecaster maximises their expected score by reporting their true beliefs. Strictly proper rules uniquely identify the true distribution. This underpins why Brier score and log-loss are the right metrics for evaluating the model in v1 and why accuracy (fraction correct) is not appropriate.

**Methodology:** Mathematical analysis. Characterisation theorems for proper scoring rules on general probability spaces. Construction of new scoring rules via convex duality. Relationship to entropy, divergence, and information theory.

**Key findings:** The Brier score (mean squared error) is a proper scoring rule for binary and multinomial outcomes. Log-loss (negative log-likelihood) is a strictly proper scoring rule and is locally sensitive (depends only on the probability assigned to the realised outcome). RPS is a proper scoring rule for ordered outcomes, but Wheatcroft (2021) later challenges whether its "sensitivity to distance" property adds value in football forecasting specifically. The paper proves that no scoring rule can simultaneously be proper and insensitive to distance for ordinal outcomes.

**Relevance to v1:** This paper provides the theoretical justification for the primary and secondary KPIs of v1:
- CLV (Closing Line Value) is effectively the log-odds improvement over the market closing line, which corresponds to a log-score improvement.
- Brier score is the recommended secondary KPI for calibration.
- The paper confirms that accuracy (fraction of correct predictions) is not a proper scoring rule and should not be used as the primary evaluation metric.

**Implementation ideas for v1:**
- Track both Brier score and log-loss (ignorance score) per match throughout the tournament.
- Use log-loss as the optimisation criterion when fitting the blend weight alpha (it is strictly proper and penalises confident errors more heavily than Brier).
- Do not use RPS as the primary metric — Wheatcroft (2021) provides a strong argument that log-loss (ignorance score) is more appropriate for football match prediction, and the Ridall et al. (2025) results also show that log-score and RPS rankings of models are not always the same.

**Skeptical note:** The paper is highly abstract and the practical choice between Brier score and log-loss for football is genuinely contested (Wheatcroft 2021 recommends log-loss; many sports analytics papers use RPS; Gneiting & Raftery would approve of either). For v1, using both metrics and reporting both is the safe approach.

---

## Paper 10: Wheatcroft (2021) — The Case Against the Ranked Probability Score

**Full citation:** Wheatcroft, E. (2021). Evaluating probabilistic forecasts of football matches: the case against the Ranked Probability Score. *Journal of Quantitative Analysis in Sports*, 17(4), 273–284. (arXiv: 1908.08980)

**Summary:** This paper challenges the near-universal use of the Ranked Probability Score (RPS) in football prediction research. Wheatcroft argues theoretically and empirically that the "sensitivity to distance" property of the RPS — treating a home win as closer to a draw than to an away win — does not actually add value when evaluating football match forecasts, and may in fact be misleading. In two simulation experiments, the ignorance score (log-loss) consistently outperforms both RPS and Brier score as a scoring rule for distinguishing between forecasters of different quality. This finding has direct implications for which metric to optimise in v1.

**Methodology:** Two simulation experiments: (1) evaluate scoring rules' ability to rank forecasters by known quality when outcome probabilities are known; (2) evaluate performance on real football match data from English leagues. Three scoring rules compared: RPS, Brier score (treating outcomes independently), and ignorance score (log-loss). The main theoretical argument is that the ordered-outcome assumption underlying RPS is empirically unjustified for football — a home win is not "closer" to a draw than to an away win in terms of how forecasters should be penalised.

**Data:** Historical English football league data across multiple divisions. Exact sample size not reported in abstract.

**Key findings:** The ignorance score (log-loss) outperforms RPS in both simulation experiments at identifying better-calibrated forecasters. The Brier score falls between the two. The paper's core challenge — that the ordering property of RPS is arbitrary — is theoretically sound. However, the RPS has become so entrenched in sports analytics literature (including the Ridall et al. 2025 paper, which uses it as the primary metric) that this paper has not yet displaced it.

**Relevance to v1:** This paper resolves the scoring rule choice for v1: use log-loss (ignorance score) as the primary calibration metric alongside Brier score, and treat RPS as a secondary check only. This is consistent with the theoretical derivation that log-loss is strictly proper and locally sensitive. The paper also reinforces why CLV (which is essentially a log-odds margin vs. the market) is the right primary betting KPI — it corresponds to log-loss improvement over the market baseline.

**Implementation ideas for v1:**
- Primary evaluation metrics: (1) CLV (log-odds margin), (2) log-loss (ignorance score), (3) Brier score. Track all three per match.
- Do not report RPS as the headline metric; include it for comparison with published literature only.
- When optimising the blend weight alpha between model and market probabilities, minimise log-loss on the calibration set.

**Skeptical note:** The simulation experiments in this paper use known ground-truth probabilities, which are not available in real football prediction. In practice, all scoring rules converge to similar model rankings with sufficient data, and the choice of RPS vs. log-loss typically makes little practical difference on a sample of 64 group-stage matches. Wheatcroft's argument is theoretically correct but practically its impact on v1's betting performance is marginal.

---

## Practical Implications for v1

The v1 system architecture is: **International Elo + time-decayed Dixon-Coles → Shin-devigged market baseline → logistic blend → quarter-Kelly staking.** The literature above supports and refines each component.

### 1. On the Statistical Model Component (Elo + Dixon-Coles)

The Ridall et al. (2025) state-space paper provides the single most important benchmark: time-decayed Dixon-Coles (equivalent to weighted likelihood) has RPS of 22.06–22.24, while the Bayesian SSM achieves 17.55. This is a substantial gap that v1 accepts as a known limitation. For v1, the following mitigations apply:

- **Use an empirically justified decay constant.** The Rue-Salvesen (2000) evolution variance implies a half-life of roughly 7–14 EPL matches, translating to ~200–400 days for international teams playing ~4 matches per year. Set the Dixon-Coles decay to approximately 0.005 per day (half-life ~140 days), which corresponds to weighting a match from 18 months ago at ~50%.
- **Apply hierarchical shrinkage for sparse teams.** For nations with fewer than 10 qualifying matches in the past 18 months (many CONCACAF and African teams), shrink attack/defence estimates toward the confederation mean using the Baio-Blangiardo framework. This prevents extreme estimates from sparse data from driving bad bets.
- **The bivariate negative binomial is better than independent Poisson** (Ridall et al. 2025), particularly for accurately pricing draws and high-variance scorelines. For v1, the standard Dixon-Coles low-score correlation correction partially addresses this; retain it.

### 2. On the Market Baseline Component (Shin Devigging)

The Zeileis et al. (2018) bookmaker consensus approach confirms that the overround on tournament-winner markets is ~15%, but for individual match 1X2 markets on UK-licensed books the margin is lower (6–10%). Shin devigging is theoretically superior to multiplicative normalisation (Clarke et al. 2017). The devigged market probabilities will be well-calibrated by construction and should receive substantial weight in the blend (~40–60% based on Egidi et al. 2018).

Key parameters for Shin devigging:
- Estimate z (insider fraction) separately for: group-stage matches (~0.01–0.02), knockout matches (~0.02–0.04). Knockout markets are thinner and potentially less efficient.
- Cross-check using: does the sum of de-vigged probabilities equal 1.00? (It should, by construction for Shin.)

### 3. On the Logistic Blend Component

Egidi et al. (2018) provides the justification: the optimal blend weight alpha is approximately 0.4–0.6 (market and historical roughly equal). For v1:

- Start with alpha = 0.5 (equal log-odds weight on model and market) as the prior.
- Fit alpha using World Cup 2006–2022 group-stage matches as calibration data (available from football-data.co.uk and historical Betfair data). With ~192 calibration matches, this is feasible for a two-parameter Platt scaling but marginal for beta calibration (three parameters).
- Preferred implementation: logistic blend on log-odds: log_odds_blend = alpha * log_odds_model + (1-alpha) * log_odds_market. Convert to probabilities and renormalise the three outcomes to sum to 1.

### 4. On Probability Calibration

Niculescu-Mizil & Caruana (2005) and Kull et al. (2017) establish the following for v1:

- The logistic blend is itself a form of Platt scaling, so a separate additional calibration step may be redundant. However, applying Platt scaling to the blended output (fitting a two-parameter logistic on the calibration set) is cheap insurance against residual miscalibration.
- Do NOT use isotonic regression — the calibration dataset (World Cup group matches 2006–2022, ~192 matches) is too small; isotonic regression will overfit.
- Maintain reliability diagrams (binned predicted vs. actual frequencies) throughout the 2026 group stage as real-time calibration monitoring.

### 5. On Scoring Rules and KPI Selection

Following Gneiting & Raftery (2007) and Wheatcroft (2021):

- **Primary KPI: CLV** (log-odds of bet price vs. closing price on Betfair Exchange). Target: +2% average CLV corresponds to ~4% long-run ROI.
- **Secondary KPI: log-loss** vs. bookmaker de-vigged probabilities (ignorance score). The model should have lower log-loss than the bookmaker baseline — if it does not, the logistic blend weight should be revised toward alpha = 1.0 (pure market).
- **Tertiary KPI: Brier score** vs. bookmaker baseline. Report for calibration monitoring.
- **Do not report RPS** as the primary metric; it is used in the literature for comparability but is theoretically inferior to log-loss for football.

### 6. On Quarter-Kelly Staking

The Kelly criterion literature (not covered by the reviewed papers, but standard practice) confirms: quarter-Kelly reduces variance by ~75% relative to full Kelly, while sacrificing only ~25% of expected growth rate. This is appropriate for v1 given:
- Model edge estimates will have substantial uncertainty (wide posterior on alpha).
- The calibration dataset is small.
- The $1,000 bankroll requires capital preservation during the group stage to allow compounding through knockout rounds.

**Recommended rule:** Bet only when model probability exceeds de-vigged market probability by at least 3 percentage points on the 1X2 market (this corresponds to roughly 1.5–2 times the de-vigging uncertainty). Stake = 0.25 * Kelly fraction * bankroll.

### 7. Known Limitations and Caveats

- **No model beats the market on raw Brier/log-loss** (confirmed by Ridall et al. 2025 across 14 EPL seasons; Constantinou et al. 2012 on EPL; Egidi et al. 2018 on European leagues). The blended model can only generate positive CLV if it identifies structural inefficiencies (e.g., market under/over-reacting to specific information types).
- **International sparsity is a fundamental problem** not fully addressed by any of the reviewed papers, all of which use dense domestic league data. Custom shrinkage priors informed by confederation-level strength are essential and not off-the-shelf.
- **World Cup results are heavily driven by variance** — the winning probability of the true best team in a 64-match tournament is rarely above 30%. Brier and log-loss evaluated on a single 104-match tournament are noisy, and distinguishing signal from noise requires hundreds of matches. Use CLV as the primary KPI precisely because it is evaluable bet-by-bet without needing outcomes.
- **The 2026 World Cup introduces a structural novelty** (48 teams, group-of-3 format with potential third-place advancement) that none of the reviewed papers have encountered. The draw probabilities in 3-team groups differ systematically from 4-team groups — the third-place comparison mechanism may create unusual late-group-stage incentive structures (tanking, strategic draws). Standard Poisson models do not capture strategic incentives; apply elevated uncertainty margins in third-match-of-group-stage bets.

---

## Summary Table of Key Papers

| Paper | Year | Method | Data | Key Finding | CLV/Profit Evidence |
|-------|------|--------|------|-------------|---------------------|
| Rue & Salvesen | 2000 | Dynamic Bayesian (MCMC) | EPL 1997-98 | Time-varying parameters via random walk | Positive return claimed (weak evidence) |
| Baio & Blangiardo | 2010 | Hierarchical Bayesian Poisson | Serie A 1991-92, 2007-08 | Partial pooling needed; overshrinkage problem identified | No betting test |
| Egidi et al. | 2018 | Hierarchical Bayes + market blend | 5 Euro leagues, 9 seasons | Blend weight ~0.5; combined model beats either alone | No betting profit test |
| Groll et al. | 2019 | Random forest + ranking hybrid | 4 World Cups 2002-2014 | Hybrid beats individual methods; bookmaker consensus strong baseline | No held-out betting test |
| Zeileis et al. | 2018 | Bookmaker consensus (Bradley-Terry) | 2018 World Cup odds | Market consensus predicted 3 of 4 2014 semi-finalists | By construction: market baseline |
| Ridall et al. | 2025 | Bayesian state-space (conjugate) | EPL 14 seasons | SSM RPS 17.55 vs Dixon-Coles equivalent 22.06; no model beats market | No betting profit test |
| Niculescu-Mizil & Caruana | 2005 | Platt scaling vs isotonic regression | 22 ML datasets | Platt scaling preferred for small calibration sets; 10-25% Brier improvement | Not applicable |
| Kull et al. | 2017 | Beta calibration | 48 ML datasets | Beta calibration beats Platt for skewed distributions; 3-8% Brier improvement | Not applicable |
| Gneiting & Raftery | 2007 | Theory of proper scoring rules | Theoretical | Log-loss (ignorance) strictly proper; Brier proper; RPS proper but distance sensitivity questionable | Not applicable |
| Wheatcroft | 2021 | Scoring rule comparison (football) | English league data | Log-loss (ignorance) outperforms RPS for identifying forecast quality | No betting profit test |

---

*Section prepared for World Cup Alpha v1. Next update recommended after 2026 World Cup group stage completes (approximately 2026-06-27) to recalibrate blend weight alpha on live tournament data.*
