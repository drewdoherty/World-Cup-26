# #8 — Player-aware scorer model vs baseline (WC2018 → WC2022)

**Question.** The Phase-2 player-level edge distributes a team's expected goals
across players by their StatsBomb non-penalty-xG share. Does that *player-aware*
model beat a no-player-awareness baseline at predicting **anytime goalscorer**?

The v1 system (Elo + Dixon-Coles + Shin) is a **1X2 match-outcome** model and
prices no players, so the honest player-aware-vs-baseline comparison is at the
scorer level. The baseline is the natural v1-equivalent: spread the same team
goal expectation **equally** across the players who appeared.

**Method (out of sample, leakage-controlled).**
- Shares learned on **WC2018**, evaluated on **WC2022** — strictly OOS.
- Both models get the *same* per-match inputs: a team goal-expectation prior
  (the WC2018 mean, 1.52/team) and each player's realised minutes. Only the
  *share* differs, so the test isolates the player signal.
- Metric: binary Brier + log-loss on "did the player score in this match",
  over every covered player-match.
- Reproduce: `python backtests/player_aware_scorer_backtest.py`
  (reads the local StatsBomb cache; writes `_cache/player_aware_scorer_result.json`).

**Result (64 test matches, 623 covered player-matches, 31.2% OOS coverage).**

| model | Brier | log-loss |
|---|---|---|
| player-aware (npxg-share) | **0.0862** | 0.4316 |
| baseline (equal share) | 0.0943 | **0.3372** |
| improvement | **+0.0080** | −0.0943 |

**Recommendation: do NOT adopt the raw player-aware probabilities as-is.**

The split is informative, not contradictory:
- Player-aware **wins Brier** → it *ranks* scorers better (concentrates
  probability on the genuine goal threats), which is exactly what the props
  scanner needs to surface the right players.
- Player-aware **loses log-loss** → its absolute probabilities are
  **over-confident**. The equal-share baseline sits near the ~10% per-player
  base rate, so it is well-calibrated by construction; the share model deviates
  from that and gets punished on the many matches where a high-share striker
  does not score.

**Action.** Use the player-aware share for **selection/ranking** (it has the
better discrimination), but **calibrate/shrink** the probabilities before
treating them as fair prices — consistent with the `thin`-sample shrinkage flag
already built into `players.db` (#4) and the analyst-override store. Concretely:
shrink each share toward the team mean by sample size, and/or fit a per-90
intensity scaler on a holdout before quoting model odds. Re-run this backtest
after calibration; adopt only when **both** Brier and log-loss improve (the
`recommend_adopt` gate enforces this).

Coverage caveat: only 31% of WC2022 player-matches had a WC2018 share, so the
out-of-sample sample is modest — another reason to shrink thin players toward
priors rather than trust raw shares.
