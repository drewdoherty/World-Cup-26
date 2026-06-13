"""Walk-forward backtest of the socio-economic Dixon-Coles shrinkage prior.

Purpose
-------
:func:`wca.card.fit_models` can optionally shrink Dixon-Coles attack/defence
toward a structural (socio-economic) estimate instead of the global mean
(``structural_prior=True``; default **off**). The claim being tested is narrow:
this should help **low-data teams** — minnows whose weak likelihood lets the
ridge dominate — and be ~neutral elsewhere, because for data-rich teams the
likelihood swamps any prior.

This script provides *evidence only* for whether to enable the flag. It does not
edit ``card.py``.

Design
------
Reuses the half-life backtest's holdout blocks (WC2018, WC2022, Euro+Copa2024),
loaders and scorers. For each block:

  1. Fit a baseline Dixon-Coles (shrink-to-mean) and a structural-prior
     Dixon-Coles on every *played* match strictly before the block start, decay
     ``reference_date`` pinned to the start, at the deployed half-life (8y).
  2. Predict each holdout match's 1X2 (respecting ``neutral``) under both models.
  3. Score multiclass log-loss / Brier vs the realised outcome, on:
       * **all** holdout matches, and
       * the **low-data subset**: matches in which at least one side had fewer
         than ``min_matches`` training appearances (this is exactly the regime
         the prior is meant to improve).

A structural prior that helps where it should will lower log-loss on the
low-data subset without hurting the full set materially.

Run
---
    ./.venv/bin/python backtests/structural_prior_backtest.py [--scale 0.15] [--md PATH]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
_BACKTESTS = os.path.dirname(os.path.abspath(__file__))
for _p in (_SRC, _BACKTESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reuse the half-life harness's holdouts, loaders and scorers.
from halflife_backtest import (  # noqa: E402
    DEFAULT_RESULTS,
    HOLDOUTS,
    Holdout,
    brier_one,
    load_results,
    log_loss_one,
    outcome_index,
    played_before,
)
from wca.data.teamnames import canonical  # noqa: E402
from wca.models.dixon_coles import DixonColesModel  # noqa: E402
from wca.models.structural import DEFAULT_PRIOR_SCALE, dc_priors_from_factors  # noqa: E402

DEPLOYED_HALF_LIFE = 8.0
MIN_MATCHES = 5  # matches the DixonColesModel low-data threshold default.


class BlockResult:
    def __init__(self, name: str, n_all: int, n_low: int):
        self.name = name
        self.n_all = n_all
        self.n_low = n_low
        # variant -> subset -> {log_loss, brier}
        self.metrics: Dict[str, Dict[str, Dict[str, float]]] = {}


def evaluate_block(
    df: pd.DataFrame,
    holdout: Holdout,
    scale: float,
    verbose: bool = True,
) -> Optional[BlockResult]:
    test = holdout.select(df)
    if len(test) == 0:
        if verbose:
            print("  [%s] no holdout matches -- skipping" % holdout.name)
        return None
    train = played_before(df, holdout.start)

    # Baseline (shrink-to-mean) and structural-prior models.
    base = DixonColesModel(half_life_years=DEPLOYED_HALF_LIFE)
    base.fit_dataframe(train, reference_date=holdout.start)

    atk_prior, dfc_prior = dc_priors_from_factors(scale=scale)
    struct = DixonColesModel(
        half_life_years=DEPLOYED_HALF_LIFE,
        attack_prior=atk_prior,
        defence_prior=dfc_prior,
    )
    struct.fit_dataframe(train, reference_date=holdout.start)

    counts = base.match_counts  # per-team training appearances

    # Tag each holdout match as low-data if either side is below the threshold.
    def is_low(home: str, away: str) -> bool:
        return min(counts.get(home, 0), counts.get(away, 0)) < MIN_MATCHES

    acc = {
        "baseline": {"all": [0.0, 0.0], "low": [0.0, 0.0]},
        "structural": {"all": [0.0, 0.0], "low": [0.0, 0.0]},
    }
    n_low = 0
    for _, r in test.iterrows():
        home, away = str(r["home_team"]), str(r["away_team"])
        neutral = bool(r["neutral"])
        y = outcome_index(int(r["home_score"]), int(r["away_score"]))
        low = is_low(home, away)
        n_low += int(low)
        for name, model in (("baseline", base), ("structural", struct)):
            p = model.predict(home, away, neutral=neutral, warn=False).one_x_two()
            ll = log_loss_one(p, y)
            br = brier_one(p, y)
            acc[name]["all"][0] += ll
            acc[name]["all"][1] += br
            if low:
                acc[name]["low"][0] += ll
                acc[name]["low"][1] += br

    res = BlockResult(holdout.name, len(test), n_low)
    for name in ("baseline", "structural"):
        res.metrics[name] = {}
        for subset, n in (("all", len(test)), ("low", n_low)):
            ll, br = acc[name][subset]
            res.metrics[name][subset] = {
                "log_loss": ll / n if n else float("nan"),
                "brier": br / n if n else float("nan"),
            }
    if verbose:
        d_all = (
            res.metrics["baseline"]["all"]["log_loss"]
            - res.metrics["structural"]["all"]["log_loss"]
        )
        d_low = (
            res.metrics["baseline"]["low"]["log_loss"]
            - res.metrics["structural"]["low"]["log_loss"]
        )
        print(
            "  [%s] n=%d (low=%d)  d_logloss all=%+.4f  low=%+.4f  "
            "(positive => structural better)" % (holdout.name, len(test), n_low, d_all, d_low)
        )
    return res


def aggregate(blocks: List[BlockResult]) -> Dict[str, Dict[str, Dict[str, float]]]:
    agg: Dict[str, Dict[str, Dict[str, float]]] = {}
    for name in ("baseline", "structural"):
        agg[name] = {}
        for subset, n_attr in (("all", "n_all"), ("low", "n_low")):
            total = sum(getattr(b, n_attr) for b in blocks)
            ll = br = 0.0
            for b in blocks:
                n = getattr(b, n_attr)
                if n == 0:
                    continue
                ll += b.metrics[name][subset]["log_loss"] * n
                br += b.metrics[name][subset]["brier"] * n
            agg[name][subset] = {
                "log_loss": ll / total if total else float("nan"),
                "brier": br / total if total else float("nan"),
                "n": total,
            }
    return agg


def build_markdown(blocks: List[BlockResult], agg: Dict, scale: float) -> str:
    L: List[str] = []
    L.append("# Dixon-Coles structural (socio-economic) shrinkage-prior backtest")
    L.append("")
    L.append(
        "Walk-forward, out-of-sample test of the optional structural shrinkage "
        "prior (`wca.card.fit_models(structural_prior=True)`, default **off**). "
        "**Evidence only** — `card.py` is not modified by this study. Prior scale "
        "= %g; deployed half-life = %g; low-data threshold = %d training matches."
        % (scale, DEPLOYED_HALF_LIFE, MIN_MATCHES)
    )
    L.append("")
    L.append(
        "The prior shrinks low-data teams toward a socio-economic estimate "
        "(population x football culture, an inverted-U in GDP/capita, "
        "confederation) instead of the global mean. The hypothesis is that it "
        "helps the **low-data subset** (matches with a minnow) and is ~neutral on "
        "the full set."
    )
    L.append("")
    L.append("## Aggregate (match-count-weighted log-loss / Brier)")
    L.append("")
    L.append("| subset | n | baseline LL | structural LL | dLL | baseline Brier | structural Brier |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for subset, label in (("all", "all holdout matches"), ("low", "low-data subset")):
        b = agg["baseline"][subset]
        s = agg["structural"][subset]
        dll = b["log_loss"] - s["log_loss"]
        L.append(
            "| %s | %d | %.4f | %.4f | %+.4f | %.4f | %.4f |"
            % (label, b["n"], b["log_loss"], s["log_loss"], dll, b["brier"], s["brier"])
        )
    L.append("")
    L.append("(dLL > 0 means the structural prior has the lower — better — log-loss.)")
    L.append("")
    L.append("## Per-holdout (low-data subset log-loss)")
    L.append("")
    L.append("| block | n | low-n | baseline LL | structural LL | dLL |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for b in blocks:
        bm = b.metrics["baseline"]["low"]["log_loss"]
        sm = b.metrics["structural"]["low"]["log_loss"]
        L.append(
            "| %s | %d | %d | %.4f | %.4f | %+.4f |"
            % (b.name, b.n_all, b.n_low, bm, sm, bm - sm)
        )
    L.append("")
    # Verdict.
    s_low = agg["structural"]["low"]["log_loss"]
    b_low = agg["baseline"]["low"]["log_loss"]
    s_all = agg["structural"]["all"]["log_loss"]
    b_all = agg["baseline"]["all"]["log_loss"]
    margin_low = b_low - s_low
    margin_all = b_all - s_all
    L.append("## Verdict")
    L.append("")
    helps_low = margin_low > 0
    hurts_all = margin_all < -0.005
    if helps_low and not hurts_all:
        verdict = (
            "The structural prior improves the low-data subset (%+.4f log-loss) "
            "without materially hurting the full set (%+.4f). It is a defensible "
            "default for the thin outright/advancement markets where low-data "
            "teams dominate; enable `structural_prior=True` there and keep "
            "monitoring calibration on live 2026 data." % (margin_low, margin_all)
        )
    elif not helps_low:
        verdict = (
            "The structural prior does **not** improve the low-data subset "
            "(%+.4f log-loss) on this holdout. Keep it **off** until a larger or "
            "more minnow-heavy holdout (e.g. live 2026 group-stage minnows) says "
            "otherwise — consistent with the project's keep-the-simple-default "
            "discipline." % margin_low
        )
    else:
        verdict = (
            "The prior helps low-data teams (%+.4f) but hurts the full set "
            "(%+.4f) — likely the scale is too aggressive. Re-run with a smaller "
            "`--scale` before considering deployment." % (margin_low, margin_all)
        )
    L.append("**" + verdict + "**")
    L.append("")
    L.append(
        "_Caveat: recent men's tournament holdouts are dominated by data-rich "
        "teams, so the low-data subset is small and noisy. The prior's real test "
        "is the 48-team 2026 field's minnows on thin Polymarket markets, which "
        "this historical holdout can only approximate._"
    )
    L.append("")
    return "\n".join(L)


def run_backtest(
    results_path: str = DEFAULT_RESULTS,
    scale: float = DEFAULT_PRIOR_SCALE,
    holdouts: Sequence[Holdout] = tuple(HOLDOUTS),
    verbose: bool = True,
) -> Tuple[List[BlockResult], Dict]:
    df = load_results(results_path)
    blocks: List[BlockResult] = []
    for ho in holdouts:
        if verbose:
            print("Block:", ho.name)
        r = evaluate_block(df, ho, scale, verbose=verbose)
        if r is not None:
            blocks.append(r)
    return blocks, aggregate(blocks)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", default=DEFAULT_RESULTS)
    p.add_argument("--scale", type=float, default=DEFAULT_PRIOR_SCALE)
    p.add_argument("--md", default=None, help="optional path to write the markdown report")
    args = p.parse_args(argv)

    t0 = time.time()
    blocks, agg = run_backtest(args.results, scale=args.scale, verbose=True)
    print("\nTotal runtime: %.1fs" % (time.time() - t0))

    md = build_markdown(blocks, agg, args.scale)
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
