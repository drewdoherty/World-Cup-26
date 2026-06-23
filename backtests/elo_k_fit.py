"""Grid-search the Elo K scale against logged 2026 result outcomes.

This reuses the same Elo rating + ordered-logit scaffold as the existing
backtests, but targets the live logged results in ``data/processed``. The grid
multiplies the World Football Elo K ladder uniformly; the production default is
kept as a named knob in ``wca.card`` rather than rewriting the ladder itself.

Run:

    python backtests/elo_k_fit.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "src"))

from wca.card import (  # noqa: E402
    DEFAULT_NEUTRAL_HOST_FACTOR,
    _elo_initial_ratings_from_dc_prior,
)
from wca.data.teamnames import canonical  # noqa: E402
from wca.models import venues as venues_mod  # noqa: E402
from wca.models.elo import DEFAULT_K_FACTORS, EloOutcomeModel, EloRater  # noqa: E402
from wca.models.structural import DEFAULT_PRIOR_SCALE  # noqa: E402

RESULTS_CSV = os.path.join(_REPO, "data", "raw", "martj42_cleaned.csv")
LOGGED_RESULTS_JSON = os.path.join(_REPO, "data", "processed", "wc2026_results.json")
CACHE_DIR = os.path.join(_HERE, "_cache")

HOST_NATIONS = ("United States", "Mexico", "Canada")
OUTCOME_INDEX = {"home": 0, "draw": 1, "away": 2}


def load_played(path: str = RESULTS_CSV) -> pd.DataFrame:
    df = pd.read_csv(path)
    for col in ("home_score", "away_score"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["home_score", "away_score"]).copy()
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date", kind="mergesort")
    if "neutral" in df.columns:
        df["neutral"] = df["neutral"].map(
            lambda v: str(v).strip().lower() in ("true", "1", "yes")
        )
    return df.reset_index(drop=True)


def load_logged(path: str = LOGGED_RESULTS_JSON) -> List[Dict[str, object]]:
    with open(path, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    rows: List[Dict[str, object]] = []
    for r in blob.get("results", []):
        if r.get("outcome") not in OUTCOME_INDEX:
            continue
        home, away = str(r["fixture"]).split(" vs ", 1)
        rows.append(
            {
                "date": pd.Timestamp(r["date"]),
                "home": canonical(home),
                "away": canonical(away),
                "outcome": str(r["outcome"]),
            }
        )
    return sorted(rows, key=lambda r: (r["date"], r["home"], r["away"]))


def _host_for(home: str, away: str) -> Optional[str]:
    if home in HOST_NATIONS:
        return home
    if away in HOST_NATIONS:
        return away
    return None


def fit_elo(train: pd.DataFrame, k_scale: float) -> Tuple[EloRater, EloOutcomeModel]:
    k_factors = {k: v * float(k_scale) for k, v in DEFAULT_K_FACTORS.items()}
    seeds = _elo_initial_ratings_from_dc_prior(prior_scale=DEFAULT_PRIOR_SCALE)
    rater = EloRater(initial_ratings=seeds, k_factors=k_factors)
    out = rater.rate_matches(train, return_history=True)

    diffs: List[float] = []
    outcomes: List[int] = []
    scores = train[["home_score", "away_score"]].to_numpy()
    for rec, (hs, as_) in zip(out["history"], scores):
        adv = 0.0 if rec["neutral"] else rater.home_advantage
        diffs.append((rec["home_rating_pre"] + adv) - rec["away_rating_pre"])
        outcomes.append(2 if hs > as_ else (1 if hs == as_ else 0))
    return rater, EloOutcomeModel().fit(diffs, outcomes)


def log_loss_for_k(
    train: pd.DataFrame,
    logged: Sequence[Dict[str, object]],
    k_scale: float,
) -> float:
    rater, outcome = fit_elo(train, k_scale)
    losses: List[float] = []
    host_points = venues_mod.host_advantage_points(
        rater.home_advantage,
        factor=DEFAULT_NEUTRAL_HOST_FACTOR,
    )
    for row in logged:
        home, away = str(row["home"]), str(row["away"])
        host = _host_for(home, away)
        hp = host_points if host is not None else None
        diff = rater._rating_diff(home, away, neutral=True, host=host, host_points=hp)
        probs = np.asarray(outcome.predict_proba(diff), dtype=float)
        y = OUTCOME_INDEX[str(row["outcome"])]
        losses.append(float(-np.log(max(probs[y], 1e-15))))
    return float(np.mean(losses))


DEFAULT_GRID: Tuple[float, ...] = (
    0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.25, 1.5
)


def run_grid(grid: Sequence[float] = DEFAULT_GRID) -> Dict[str, object]:
    played = load_played()
    logged = load_logged()
    if not logged:
        raise RuntimeError("no logged results found")
    cut = min(pd.Timestamp(r["date"]) for r in logged)
    train = played[played["date"] < cut].reset_index(drop=True)
    curve = [
        {"k_scale": float(k), "log_loss": log_loss_for_k(train, logged, float(k))}
        for k in grid
    ]
    best = min(curve, key=lambda r: r["log_loss"])
    return {
        "n_train": int(len(train)),
        "n_logged": int(len(logged)),
        "k_scale_chosen": best["k_scale"],
        "log_loss_chosen": best["log_loss"],
        "curve": curve,
    }


def main(argv: Sequence[str]) -> int:
    grid = tuple(float(x) for x in argv[1:]) if len(argv) > 1 else DEFAULT_GRID
    res = run_grid(grid)
    print("logged matches: %d  train matches: %d" % (res["n_logged"], res["n_train"]))
    print(
        "chosen K scale: %.2f  log-loss: %.4f"
        % (res["k_scale_chosen"], res["log_loss_chosen"])
    )
    print("loss curve:")
    for row in res["curve"]:
        print("  %.2f  %.4f" % (row["k_scale"], row["log_loss"]))
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = os.path.join(CACHE_DIR, "elo_k_fit_result.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
