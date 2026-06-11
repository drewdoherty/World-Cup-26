"""Walk-forward backtest of the Dixon-Coles time-decay half-life.

Purpose
-------
The deployed matchday card fits Dixon-Coles with ``half_life_years = 8.0`` (set
in :func:`wca.card.fit_models`; the module default is 2.0). This script provides
*evidence only* for whether 8.0 is a good choice, by walking the model forward
over three recent holdout tournaments and scoring out-of-sample 1X2 forecasts at
a grid of half-lives. It does **not** edit ``card.py``.

Design
------
Holdout tournaments (each predicted with a model that has seen *only* matches
strictly before the tournament's first match):

  * WC2018      -- FIFA World Cup, Jun/Jul 2018 (host Russia)
  * WC2022      -- FIFA World Cup, Nov/Dec 2022 (host Qatar)
  * Euro2024 + Copa2024 -- Jun/Jul 2024 (hosts Germany / USA), pooled as one
    holdout "block" because they run concurrently off the same training cutoff.

For each holdout block and each ``half_life_years`` in {1, 2, 4, 8, 16}:

  1. Fit a fresh :class:`DixonColesModel` on every *played* match strictly before
     the block's start date, with ``reference_date`` pinned to that start date so
     the decay clock is anchored at prediction time.
  2. Predict each holdout match's 1X2 probabilities, respecting the ``neutral``
     flag. Hosts are encoded in the data as non-neutral rows with the host as the
     home team, so passing ``neutral`` straight through gives the host its home
     advantage automatically.
  3. Score multiclass log-loss and Brier against the realised outcome.

Elo (rating engine + ordered-logit outcome model) has no decay parameter, so it
is fit *once per block* on the same training window and reused across half-lives.
We also score a fixed 50/50 Elo+DC blend per half-life.

Aggregation is a match-count-weighted mean of per-match log-loss / Brier across
the three blocks, reported per half-life for DC-only and for the blend. The
recommendation reports the best half-life, its margin over the deployed 8.0 in
log-loss, and per-holdout consistency (not just the pooled mean).

Run
---
    ./.venv/bin/python backtests/halflife_backtest.py

Writes nothing by default beyond stdout; ``--md PATH`` also writes the markdown
report. ``--quick`` uses a reduced half-life grid and is only for smoke tests.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# Make ``import wca`` work when run as a script from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.models.dixon_coles import DixonColesModel  # noqa: E402
from wca.models.elo import EloOutcomeModel, EloRater  # noqa: E402

# 1X2 outcome order: home, draw, away.
OUTCOMES = ("home", "draw", "away")

DEFAULT_HALF_LIVES = (1.0, 2.0, 4.0, 8.0, 16.0)
DEPLOYED_HALF_LIFE = 8.0

DEFAULT_RESULTS = os.path.join(_REPO_ROOT, "data", "raw", "results.csv")

_EPS = 1e-12


# ---------------------------------------------------------------------------
# Holdout definition.
# ---------------------------------------------------------------------------


class Holdout:
    """A holdout block: one or more tournaments sharing a training cutoff."""

    def __init__(self, name: str, start: str, end: str, selectors: Sequence[Tuple[str, ...]]):
        self.name = name
        self.start = pd.Timestamp(start)
        self.end = pd.Timestamp(end)
        # Each selector is (tournament_match_substring,) used case-insensitively.
        self.selectors = [s if isinstance(s, tuple) else (s,) for s in selectors]

    def select(self, df: pd.DataFrame) -> pd.DataFrame:
        """Played holdout matches within [start, end] matching any selector."""
        in_window = (df["date"] >= self.start) & (df["date"] <= self.end)
        tourn = df["tournament"].astype(str).str.lower()
        sel_mask = pd.Series(False, index=df.index)
        for (sub,) in self.selectors:
            sel_mask = sel_mask | tourn.str.contains(sub.lower(), na=False, regex=False)
        sub = df[in_window & sel_mask].copy()
        # Played only (real integer scores).
        sub = sub.dropna(subset=["home_score", "away_score"])
        return sub.sort_values("date", kind="mergesort").reset_index(drop=True)


HOLDOUTS = [
    Holdout("WC2018", "2018-06-01", "2018-07-31", [("fifa world cup",)]),
    Holdout("WC2022", "2022-11-01", "2022-12-31", [("fifa world cup",)]),
    Holdout("Euro2024+Copa2024", "2024-06-01", "2024-07-31",
            [("uefa euro",), ("copa am",)]),
]


# ---------------------------------------------------------------------------
# Data loading.
# ---------------------------------------------------------------------------


def load_results(path: str) -> pd.DataFrame:
    """Load and clean the martj42 results frame to *played* matches only."""
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("home_score", "away_score"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date"])
    # Exclude WC selectors from matching qualifiers: the FIFA World Cup
    # qualification rows contain "fifa world cup" too, but they fall outside the
    # tournament date windows so the window filter removes them. Belt-and-braces:
    # we keep the raw frame intact and let Holdout.select handle windows.
    if "neutral" in df.columns:
        df["neutral"] = df["neutral"].astype(str).str.lower().isin(["true", "1", "yes"])
    return df.reset_index(drop=True)


def played_before(df: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    """Played matches (real scores) strictly before ``cutoff``."""
    mask = (
        (df["date"] < cutoff)
        & df["home_score"].notna()
        & df["away_score"].notna()
    )
    train = df[mask].copy()
    train["home_score"] = train["home_score"].astype(int)
    train["away_score"] = train["away_score"].astype(int)
    return train.sort_values("date", kind="mergesort").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Scoring.
# ---------------------------------------------------------------------------


def outcome_index(home_score: int, away_score: int) -> int:
    """1X2 index: 0=home, 1=draw, 2=away."""
    if home_score > away_score:
        return 0
    if home_score == away_score:
        return 1
    return 2


def log_loss_one(probs: Sequence[float], y: int) -> float:
    """Multiclass log-loss for one match (natural log)."""
    p = max(_EPS, min(1.0, float(probs[y])))
    return -math.log(p)


def brier_one(probs: Sequence[float], y: int) -> float:
    """Multiclass Brier score for one match (sum of squared errors over 3 classes)."""
    target = [0.0, 0.0, 0.0]
    target[y] = 1.0
    return float(sum((float(probs[k]) - target[k]) ** 2 for k in range(3)))


# ---------------------------------------------------------------------------
# Elo fit (once per training window).
# ---------------------------------------------------------------------------


def fit_elo(train: pd.DataFrame) -> Tuple[EloRater, EloOutcomeModel]:
    """Fit Elo ratings + ordered-logit outcome model on a training window."""
    rater = EloRater()
    out = rater.rate_matches(train, return_history=True)
    history = out["history"]

    diffs: List[float] = []
    outcomes: List[int] = []
    scores = train[["home_score", "away_score"]].to_numpy()
    for rec, (hs, as_) in zip(history, scores):
        adv = 0.0 if rec["neutral"] else rater.home_advantage
        diff = (rec["home_rating_pre"] + adv) - rec["away_rating_pre"]
        diffs.append(diff)
        # EloOutcomeModel encoding: 0=away,1=draw,2=home.
        outcomes.append(2 if hs > as_ else (1 if hs == as_ else 0))
    elo_outcome = EloOutcomeModel().fit(diffs, outcomes)
    return rater, elo_outcome


def elo_probs_for(
    rater: EloRater, elo_outcome: EloOutcomeModel, home: str, away: str, neutral: bool
) -> Tuple[float, float, float]:
    """Elo (home, draw, away). Host handling: neutral=False gives home the edge,
    which is exactly how host-at-home rows are encoded in the data."""
    diff = rater._rating_diff(home, away, neutral=neutral, host=None)
    return elo_outcome.predict_proba(diff)


# ---------------------------------------------------------------------------
# Per-block, per-half-life evaluation.
# ---------------------------------------------------------------------------


class BlockResult:
    def __init__(self, name: str, n: int):
        self.name = name
        self.n = n
        # half_life -> dict of metric -> value
        self.dc: Dict[float, Dict[str, float]] = {}
        self.blend: Dict[float, Dict[str, float]] = {}


def evaluate_block(
    df: pd.DataFrame,
    holdout: Holdout,
    half_lives: Sequence[float],
    verbose: bool = True,
) -> BlockResult:
    """Fit + score every half-life for one holdout block."""
    test = holdout.select(df)
    n = len(test)
    res = BlockResult(holdout.name, n)
    if n == 0:
        if verbose:
            print("  [%s] no holdout matches found -- skipping" % holdout.name)
        return res

    train = played_before(df, holdout.start)
    if verbose:
        print(
            "  [%s] train=%d matches (< %s), test=%d matches"
            % (holdout.name, len(train), holdout.start.date(), n)
        )

    # Elo once per block.
    t0 = time.time()
    rater, elo_outcome = fit_elo(train)
    if verbose:
        print("    elo fit: %.1fs" % (time.time() - t0))

    # Precompute Elo per-match probs (shared across half-lives).
    elo_match_probs: List[Tuple[float, float, float]] = []
    y_list: List[int] = []
    for _, r in test.iterrows():
        neutral = bool(r["neutral"])
        ep = elo_probs_for(rater, elo_outcome, str(r["home_team"]), str(r["away_team"]), neutral)
        elo_match_probs.append(ep)
        y_list.append(outcome_index(int(r["home_score"]), int(r["away_score"])))

    for hl in half_lives:
        t0 = time.time()
        dc = DixonColesModel(half_life_years=hl)
        dc.fit_dataframe(train, reference_date=holdout.start)
        fit_s = time.time() - t0

        dc_ll = dc_br = bl_ll = bl_br = 0.0
        for i, (_, r) in enumerate(test.iterrows()):
            neutral = bool(r["neutral"])
            pred = dc.predict(str(r["home_team"]), str(r["away_team"]), neutral=neutral, warn=False)
            d_h, d_d, d_a = pred.one_x_two()
            dc_probs = (d_h, d_d, d_a)
            e_h, e_d, e_a = elo_match_probs[i]
            blend = (
                0.5 * e_h + 0.5 * d_h,
                0.5 * e_d + 0.5 * d_d,
                0.5 * e_a + 0.5 * d_a,
            )
            y = y_list[i]
            dc_ll += log_loss_one(dc_probs, y)
            dc_br += brier_one(dc_probs, y)
            bl_ll += log_loss_one(blend, y)
            bl_br += brier_one(blend, y)

        res.dc[hl] = {
            "log_loss": dc_ll / n,
            "brier": dc_br / n,
            "fit_s": fit_s,
        }
        res.blend[hl] = {
            "log_loss": bl_ll / n,
            "brier": bl_br / n,
            "fit_s": fit_s,
        }
        if verbose:
            print(
                "    hl=%5.1f  dc logloss=%.4f brier=%.4f | blend logloss=%.4f brier=%.4f  (fit %.1fs)"
                % (hl, res.dc[hl]["log_loss"], res.dc[hl]["brier"],
                   res.blend[hl]["log_loss"], res.blend[hl]["brier"], fit_s)
            )
    return res


# ---------------------------------------------------------------------------
# Aggregation + reporting.
# ---------------------------------------------------------------------------


def aggregate(
    blocks: List[BlockResult], half_lives: Sequence[float]
) -> Dict[str, Dict[float, Dict[str, float]]]:
    """Match-count-weighted mean per half-life for DC-only and the blend."""
    agg: Dict[str, Dict[float, Dict[str, float]]] = {"dc": {}, "blend": {}}
    total_n = sum(b.n for b in blocks if b.n > 0)
    for kind in ("dc", "blend"):
        for hl in half_lives:
            ll = br = 0.0
            for b in blocks:
                if b.n == 0:
                    continue
                store = getattr(b, kind)
                ll += store[hl]["log_loss"] * b.n
                br += store[hl]["brier"] * b.n
            agg[kind][hl] = {
                "log_loss": ll / total_n if total_n else float("nan"),
                "brier": br / total_n if total_n else float("nan"),
            }
    return agg


def _best_hl(agg_kind: Dict[float, Dict[str, float]]) -> float:
    return min(agg_kind, key=lambda hl: agg_kind[hl]["log_loss"])


def build_markdown(
    blocks: List[BlockResult],
    agg: Dict[str, Dict[float, Dict[str, float]]],
    half_lives: Sequence[float],
) -> str:
    """Render the full markdown report including per-holdout tables."""
    lines: List[str] = []
    lines.append("# Dixon-Coles time-decay half-life backtest")
    lines.append("")
    lines.append(
        "Walk-forward, out-of-sample evaluation of the Dixon-Coles decay "
        "half-life on three recent tournament holdouts. **Evidence only** -- "
        "`card.py` is not modified by this study. The deployed card uses "
        "`half_life_years = %g` (`wca.card.fit_models`); the module default is 2.0."
        % DEPLOYED_HALF_LIFE
    )
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "For each holdout block, a fresh Dixon-Coles model is fit on every "
        "*played* international strictly before the block's first match, with the "
        "decay `reference_date` pinned to that start date. Each holdout match's "
        "1X2 is predicted respecting the `neutral` flag (hosts appear as "
        "non-neutral home rows, so they receive home advantage automatically). "
        "Scores are multiclass natural-log log-loss and 3-class Brier vs the "
        "realised outcome. Elo (ratings + ordered logit) carries no decay and is "
        "fit once per block; the blend is a fixed 50/50 Elo+DC mix per half-life. "
        "Aggregates are match-count-weighted means across blocks."
    )
    lines.append("")

    # Holdout sizes.
    lines.append("Holdout blocks:")
    lines.append("")
    lines.append("| Block | Matches | Train cutoff |")
    lines.append("|---|---:|---|")
    for b in blocks:
        cutoff = next(h.start.date() for h in HOLDOUTS if h.name == b.name)
        lines.append("| %s | %d | < %s |" % (b.name, b.n, cutoff))
    lines.append("")

    # Aggregate tables.
    for kind, label in (("dc", "Dixon-Coles only"), ("blend", "50/50 Elo + DC blend")):
        lines.append("## Aggregate (%s)" % label)
        lines.append("")
        lines.append("Match-count-weighted mean across all holdouts.")
        lines.append("")
        lines.append("| half-life (yr) | log-loss | Brier |")
        lines.append("|---:|---:|---:|")
        best = _best_hl(agg[kind])
        for hl in half_lives:
            ll = agg[kind][hl]["log_loss"]
            br = agg[kind][hl]["brier"]
            mark = ""
            if hl == best:
                mark = " **(best)**"
            if hl == DEPLOYED_HALF_LIFE:
                mark += " *(deployed)*"
            lines.append("| %g | %.4f | %.4f |%s" % (hl, ll, br, mark))
        lines.append("")

    # Per-holdout log-loss tables (DC only) for consistency inspection.
    lines.append("## Per-holdout log-loss (Dixon-Coles only)")
    lines.append("")
    header = "| half-life (yr) | " + " | ".join(b.name for b in blocks if b.n) + " |"
    sep = "|---:|" + "|".join(["---:"] * sum(1 for b in blocks if b.n)) + "|"
    lines.append(header)
    lines.append(sep)
    live_blocks = [b for b in blocks if b.n]
    for hl in half_lives:
        row = ["%g" % hl]
        # mark per-block best in bold
        for b in live_blocks:
            row.append("%.4f" % b.dc[hl]["log_loss"])
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    # Per-block argmin row.
    argmins = []
    for b in live_blocks:
        bhl = min(half_lives, key=lambda hl: b.dc[hl]["log_loss"])
        argmins.append("%s: %g" % (b.name, bhl))
    lines.append("Per-holdout best half-life (DC log-loss): " + "; ".join(argmins) + ".")
    lines.append("")

    # Per-holdout log-loss tables (blend) for consistency inspection.
    lines.append("## Per-holdout log-loss (50/50 blend)")
    lines.append("")
    lines.append(header)
    lines.append(sep)
    for hl in half_lives:
        row = ["%g" % hl]
        for b in live_blocks:
            row.append("%.4f" % b.blend[hl]["log_loss"])
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    argmins_b = []
    for b in live_blocks:
        bhl = min(half_lives, key=lambda hl: b.blend[hl]["log_loss"])
        argmins_b.append("%s: %g" % (b.name, bhl))
    lines.append("Per-holdout best half-life (blend log-loss): " + "; ".join(argmins_b) + ".")
    lines.append("")

    # Recommendation.
    lines.append("## Recommendation")
    lines.append("")
    best_dc = _best_hl(agg["dc"])
    best_bl = _best_hl(agg["blend"])
    dep_dc = agg["dc"][DEPLOYED_HALF_LIFE]["log_loss"]
    best_dc_ll = agg["dc"][best_dc]["log_loss"]
    margin_dc = dep_dc - best_dc_ll  # positive => best beats deployed
    dep_bl = agg["blend"][DEPLOYED_HALF_LIFE]["log_loss"]
    best_bl_ll = agg["blend"][best_bl]["log_loss"]
    margin_bl = dep_bl - best_bl_ll

    # Per-holdout consistency: how many blocks prefer best_dc over deployed?
    dc_consistency = []
    for b in live_blocks:
        d = b.dc[DEPLOYED_HALF_LIFE]["log_loss"]
        bb = b.dc[best_dc]["log_loss"]
        dc_consistency.append((b.name, d - bb))
    n_favor = sum(1 for _, m in dc_consistency if m > 0)

    lines.append(
        "- **Best half-life (DC-only, pooled log-loss):** %g "
        "(log-loss %.4f)." % (best_dc, best_dc_ll)
    )
    lines.append(
        "- **Best half-life (50/50 blend, pooled log-loss):** %g "
        "(log-loss %.4f)." % (best_bl, best_bl_ll)
    )
    lines.append(
        "- **Margin vs deployed 8.0 (DC-only):** %+.4f log-loss "
        "(deployed %.4f -> best %.4f; positive means best is better)."
        % (margin_dc, dep_dc, best_dc_ll)
    )
    lines.append(
        "- **Margin vs deployed 8.0 (blend):** %+.4f log-loss "
        "(deployed %.4f -> best %.4f)." % (margin_bl, dep_bl, best_bl_ll)
    )
    lines.append(
        "- **Per-holdout consistency (DC-only):** %d of %d blocks favour "
        "half-life %g over 8.0. Per-block (deployed-best) log-loss deltas: %s."
        % (
            n_favor,
            len(live_blocks),
            best_dc,
            ", ".join("%s %+.4f" % (nm, m) for nm, m in dc_consistency),
        )
    )
    lines.append("")

    # Meaningfulness verdict (heuristic): a log-loss margin under ~0.005 pooled
    # and not consistent across all blocks is not decision-grade.
    meaningful = (margin_dc >= 0.005) and (n_favor == len(live_blocks))
    if best_dc == DEPLOYED_HALF_LIFE:
        verdict = (
            "The deployed half-life of 8.0 is already the pooled-best for "
            "DC-only, so there is no evidence to change it. "
        )
    elif meaningful:
        verdict = (
            "The margin is material (>= 0.005 pooled log-loss) **and** all "
            "holdouts agree, so switching to half-life %g is justified. " % best_dc
        )
    else:
        verdict = (
            "The pooled margin over 8.0 is small (%.4f log-loss) and/or "
            "inconsistent across holdouts (%d/%d blocks favour it), so the "
            "difference is **not decision-grade**: keep 8.0 unless a larger "
            "study confirms %g. " % (margin_dc, n_favor, len(live_blocks), best_dc)
        )
    lines.append("**Verdict.** " + verdict)
    lines.append("")
    lines.append(
        "_Caveat: three holdouts is a small sample (~%d matches total); "
        "log-loss differences of a few thousandths are within tournament-level "
        "noise. Treat this as directional, not definitive._"
        % sum(b.n for b in live_blocks)
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point (importable for the smoke test).
# ---------------------------------------------------------------------------


def run_backtest(
    results_path: str = DEFAULT_RESULTS,
    half_lives: Sequence[float] = DEFAULT_HALF_LIVES,
    holdouts: Sequence[Holdout] = tuple(HOLDOUTS),
    verbose: bool = True,
) -> Tuple[List[BlockResult], Dict[str, Dict[float, Dict[str, float]]]]:
    """Run the full walk-forward backtest and return (blocks, aggregate)."""
    df = load_results(results_path)
    blocks: List[BlockResult] = []
    for ho in holdouts:
        if verbose:
            print("Block:", ho.name)
        blocks.append(evaluate_block(df, ho, half_lives, verbose=verbose))
    agg = aggregate(blocks, half_lives)
    return blocks, agg


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=DEFAULT_RESULTS, help="path to results.csv")
    parser.add_argument("--md", default=None, help="optional path to write the markdown report")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="reduced half-life grid {2,8} for a fast smoke run",
    )
    args = parser.parse_args(argv)

    half_lives = (2.0, 8.0) if args.quick else DEFAULT_HALF_LIVES

    t_start = time.time()
    blocks, agg = run_backtest(args.results, half_lives=half_lives, verbose=True)
    print("\nTotal runtime: %.1fs" % (time.time() - t_start))

    md = build_markdown(blocks, agg, half_lives)
    print("\n" + "=" * 70 + "\n")
    print(md)

    if args.md:
        os.makedirs(os.path.dirname(args.md), exist_ok=True)
        with open(args.md, "w") as fh:
            fh.write(md)
        print("\nWrote markdown report to", args.md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
