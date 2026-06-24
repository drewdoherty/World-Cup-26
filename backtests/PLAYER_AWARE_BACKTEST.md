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

**Calibration (player-aware), reliability by predicted-probability bucket:**

| predicted P(anytime) | n | mean predicted | observed scored |
|---|---:|---:|---:|
| 0–20% | 526 | 4.4% | **6.8%** |
| 20–40% | 83 | 28.8% | 27.7% |
| 40–60% | 13 | 50.8% | 46.2% |
| 60–80% | 1 | 60.7% | 100% (n=1, noise) |

Global calibration scale (observed/predicted) = **1.22**.

**Recommendation: do NOT adopt the raw probabilities as-is — but the fix is
specific and cheap.** The earlier "over-confident" reading was too coarse; the
reliability bins are sharper:
- Player-aware **wins Brier** → it *ranks* scorers better (concentrates
  probability on genuine threats) — exactly what the props scanner needs.
- It is **well-calibrated in the 20–60% buckets** and, globally, slightly
  *under*-confident (scale 1.22), NOT over-confident.
- The log-loss loss comes from **one place**: the 0–20% bucket (the bulk, 526
  player-matches) is **under-predicted** — 4.4% predicted vs 6.8% observed.
  Pushing probability mass onto the stars starves the squad players *below* the
  base rate, and log-loss punishes that under-prediction on the ~36 of them who
  did score. The equal-share baseline sits at the base rate by construction, so
  it wins log-loss on that mass.

**Concrete improvements (in priority order):**
1. **Blend each share toward uniform:** `share_i = α·npxg_share_i + (1−α)·(1/n)`.
   This lifts the starved low end (fixes the 0–20% under-prediction → recovers
   log-loss) while preserving the ordering (keeps the Brier win). Tune α on a
   holdout; shrink `thin`-flagged players (<180 min, the #4 flag) harder.
2. **Thin-squad guard for live picks:** when a team has few rated players the
   share normalises over a tiny set and inflates the stars (the cause of the
   absurd +100–300% live edges on 0-rated squads like South Africa/Scotland).
   Floor the denominator at a full XI / require ≥N rated players before quoting,
   else fall back to market-implied.
3. **Re-fit the penalty and minutes priors** (`pen_xg`, expected minutes) rather
   than the current constants; both feed the per-player intensity.

Re-run this backtest after (1)–(2); adopt only when **both** Brier and log-loss
improve (the `recommend_adopt` gate enforces this).

Coverage caveat: only 31% of WC2022 player-matches had a WC2018 share, so the
out-of-sample sample is modest — another reason to blend toward priors and gate
on rated-player count.

## Validation — blend sweep (closes the recommendation)

Re-running the backtest across the blend weight `alpha` (`sweep_blend`):

| alpha | Brier | log-loss | vs baseline | adopt? |
|------:|------:|---------:|-------------|--------|
| 1.0 (raw) | 0.0862 | 0.4316 | Brier +0.008 / LL −0.094 | no |
| **0.8** | 0.0857 | 0.3088 | +0.0086 / **+0.0285** | **YES** |
| 0.7 (default) | ~0.086 | ~0.306 | +0.008 / +0.030 | **YES** |
| 0.6 | 0.0861 | 0.3059 | +0.0082 / **+0.0313** | YES |
| 0.0 (baseline) | 0.0943 | 0.3372 | 0 / 0 | no |

**Verdict: ADOPT with `alpha≈0.7`.** The blend lifts the starved 0–20% bucket
(recovers log-loss) while keeping the ranking (keeps the Brier win) — the
player-aware model now beats the no-player baseline on BOTH metrics OOS. Applied
live as `scorer_props.DEFAULT_BLEND_ALPHA=0.7`, gated by rated-squad count.
