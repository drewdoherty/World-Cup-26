"""Evidence-only fit of the Elo / Dixon-Coles / market blend weights.

This module produces *evidence* for choosing :class:`wca.card.BlendWeights`. It
never edits ``card.py``; the recommendation is reported in
``docs/research/backtests/blend_weights.md``.

Two independent pieces of evidence are produced:

Step 1 -- leave-one-tournament-out (LOTO), no market, no API credits.
    For each holdout tournament in {WC2018, WC2022, Euro2024, Copa2024} we fit
    Elo (rating + ordered-logit) and a time-decayed Dixon-Coles model on every
    international result *strictly before* that tournament's first match. We then
    evaluate the convex Elo/DC blend

        p = w_elo * p_elo + (1 - w_elo) * p_dc

    on the held-out tournament's matches. The relative weight ``w_elo`` is chosen
    by minimising pooled multiclass log-loss on the OTHER three holdouts and then
    scored, untouched, on the held-out one. We report the per-fold optimum, the
    pooled optimum, and the full log-loss curve.

Step 3 -- full 3-way convex blend on WC2022 (needs ``wc2022_closing_odds.json``).
    With models trained on pre-WC2022 data and the de-vigged market consensus per
    match, fit ``(w_elo, w_dc, w_market)`` on the simplex by minimising WC2022
    log-loss (scipy ``minimize`` on a softmax parameterisation). Compare the
    fitted blend against the current 0.25/0.25/0.50, market-only, and each
    component model alone, with 1000x match-resampled bootstrap CIs.

Run ``python backtests/blend_fit.py step1`` (slow: 4 model fits) or
``python backtests/blend_fit.py step3`` (after the odds pull). Intermediate
per-match model probabilities are cached under ``backtests/_cache`` so the grid
search and Step 3 do not refit.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from wca.card import dc_probs, elo_probs, fit_models, market_consensus
from wca.data.teamnames import canonical

# ---------------------------------------------------------------------------
# Paths.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
RESULTS_CSV = os.path.join(_REPO, "data", "raw", "results.csv")
WC2022_ODDS_JSON = os.path.join(_REPO, "data", "raw", "wc2022_closing_odds.json")
CACHE_DIR = os.path.join(_HERE, "_cache")

# Host bonus is granted to the genuine host nation on its (non-neutral) rows; the
# results.csv already encodes host games as neutral == False, so the Elo home
# advantage handles them. No extra host mapping is needed for past tournaments.

HALF_LIFE_YEARS = 8.0  # matches the deployed card (wca.card.fit_models default).

# 1X2 model output order is (home, draw, away). The realised-outcome index uses
# the same order: 0 home, 1 draw, 2 away.
OUTCOME_HOME, OUTCOME_DRAW, OUTCOME_AWAY = 0, 1, 2


# ---------------------------------------------------------------------------
# Holdout tournament definitions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Holdout:
    """A held-out finals tournament used as an out-of-sample test slate."""

    key: str
    label: str
    # Inclusive date window [start, end] for the *finals* matches.
    start: str
    end: str
    # Substring that must appear in the tournament column (case-insensitive).
    name_contains: str
    # Substrings that must NOT appear (excludes qualifiers etc.).
    name_excludes: Tuple[str, ...] = ("qualif",)

    def select(self, played: pd.DataFrame) -> pd.DataFrame:
        d = played
        name = d["tournament"].astype(str).str.lower()
        mask = name.str.contains(self.name_contains.lower(), na=False)
        for ex in self.name_excludes:
            mask &= ~name.str.contains(ex.lower(), na=False)
        mask &= d["date"] >= pd.Timestamp(self.start)
        mask &= d["date"] <= pd.Timestamp(self.end)
        return d[mask].reset_index(drop=True)


HOLDOUTS: Tuple[Holdout, ...] = (
    Holdout("wc2018", "World Cup 2018", "2018-06-14", "2018-07-15", "world cup"),
    Holdout("wc2022", "World Cup 2022", "2022-11-20", "2022-12-18", "world cup"),
    Holdout("euro2024", "Euro 2024", "2024-06-14", "2024-07-14", "uefa euro"),
    # Copa America 2024 *finals* only (June-July); the two March rows in the data
    # are CONCACAF play-ins mislabelled "Copa América" and are excluded by window.
    Holdout("copa2024", "Copa America 2024", "2024-06-20", "2024-07-14", "copa am"),
)


# ---------------------------------------------------------------------------
# Data loading.
# ---------------------------------------------------------------------------


def load_played(path: str = RESULTS_CSV) -> pd.DataFrame:
    """Load results.csv, keep only played matches, typed and date-sorted."""
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


def realised_outcome(home_score: int, away_score: int) -> int:
    """0 = home win, 1 = draw, 2 = away win (matches model (home,draw,away))."""
    if home_score > away_score:
        return OUTCOME_HOME
    if home_score == away_score:
        return OUTCOME_DRAW
    return OUTCOME_AWAY


# ---------------------------------------------------------------------------
# Per-match model probabilities for one holdout (cached).
# ---------------------------------------------------------------------------


def _host_for(home: str, away: str, country: str, neutral: bool) -> Optional[str]:
    """Host nation that should receive the Elo host bonus on a neutral venue.

    For past finals the data encodes host games as non-neutral, so this is only
    needed when a row is flagged neutral but one side is the host country. We
    pass ``host=country if it matches a side`` to mirror the live card.
    """
    if neutral and country:
        c = canonical(country)
        if c == home:
            return home
        if c == away:
            return away
    return None


def compute_holdout_probs(
    holdout: Holdout, played: pd.DataFrame, refit: bool = False
) -> Dict[str, object]:
    """Fit models on pre-tournament data and score the holdout's matches.

    Returns a dict with arrays ``elo`` (n,3), ``dc`` (n,3), ``y`` (n,) realised
    outcome index, plus metadata, and caches it to JSON.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, "step1_%s.json" % holdout.key)
    if os.path.exists(cache_path) and not refit:
        with open(cache_path) as fh:
            blob = json.load(fh)
        blob["elo"] = np.asarray(blob["elo"], dtype=float)
        blob["dc"] = np.asarray(blob["dc"], dtype=float)
        blob["y"] = np.asarray(blob["y"], dtype=int)
        return blob

    slate = holdout.select(played)
    if slate.empty:
        raise RuntimeError("no matches selected for holdout %s" % holdout.key)

    cut = pd.Timestamp(holdout.start)
    train = played[played["date"] < cut].reset_index(drop=True)
    ref = (cut - pd.Timedelta(days=1)).date().isoformat()

    t0 = time.time()
    models = fit_models(train, half_life_years=HALF_LIFE_YEARS, reference_date=ref)
    fit_secs = time.time() - t0

    elo_rows: List[List[float]] = []
    dc_rows: List[List[float]] = []
    y: List[int] = []
    matches_meta: List[Dict[str, object]] = []
    for _, r in slate.iterrows():
        home = canonical(str(r["home_team"]))
        away = canonical(str(r["away_team"]))
        neutral = bool(r["neutral"]) if "neutral" in slate.columns else True
        country = str(r.get("country", ""))
        host = _host_for(home, away, country, neutral)

        e_h, e_d, e_a = elo_probs(models, home, away, neutral=neutral, host=host)
        d_h, d_d, d_a = dc_probs(models, home, away, neutral=neutral)
        elo_rows.append([e_h, e_d, e_a])
        dc_rows.append([d_h, d_d, d_a])
        y.append(realised_outcome(int(r["home_score"]), int(r["away_score"])))
        matches_meta.append(
            {
                "date": str(pd.Timestamp(r["date"]).date()),
                "home": home,
                "away": away,
                "neutral": neutral,
                "score": "%d-%d" % (int(r["home_score"]), int(r["away_score"])),
            }
        )

    blob: Dict[str, object] = {
        "holdout": holdout.key,
        "label": holdout.label,
        "n_train": int(len(train)),
        "n_matches": int(len(slate)),
        "fit_secs": round(fit_secs, 1),
        "elo": np.asarray(elo_rows, dtype=float),
        "dc": np.asarray(dc_rows, dtype=float),
        "y": np.asarray(y, dtype=int),
        "matches": matches_meta,
    }

    serialisable = dict(blob)
    serialisable["elo"] = blob["elo"].tolist()
    serialisable["dc"] = blob["dc"].tolist()
    serialisable["y"] = blob["y"].tolist()
    with open(cache_path, "w") as fh:
        json.dump(serialisable, fh, indent=2)
    return blob


# ---------------------------------------------------------------------------
# Log-loss helpers.
# ---------------------------------------------------------------------------

_LL_EPS = 1e-15


def log_loss(probs: np.ndarray, y: np.ndarray) -> float:
    """Mean multiclass log-loss for an (n,3) probability matrix."""
    p = np.clip(probs, _LL_EPS, 1.0)
    p = p / p.sum(axis=1, keepdims=True)
    chosen = p[np.arange(p.shape[0]), y]
    return float(-np.mean(np.log(np.clip(chosen, _LL_EPS, 1.0))))


def blend_elo_dc(elo: np.ndarray, dc: np.ndarray, w_elo: float) -> np.ndarray:
    """Convex Elo/DC blend ``w_elo*elo + (1-w_elo)*dc`` (renormalised)."""
    p = w_elo * elo + (1.0 - w_elo) * dc
    return p / p.sum(axis=1, keepdims=True)


# ---------------------------------------------------------------------------
# Step 1 -- leave-one-tournament-out fit of w_elo.
# ---------------------------------------------------------------------------


def step1_loto(
    refit: bool = False, grid_step: float = 0.05
) -> Dict[str, object]:
    """Run the full LOTO fit and return a structured result dict."""
    played = load_played()
    grid = np.round(np.arange(0.0, 1.0 + 1e-9, grid_step), 4)

    # Per-holdout cached probabilities.
    data: Dict[str, Dict[str, object]] = {}
    for h in HOLDOUTS:
        data[h.key] = compute_holdout_probs(h, played, refit=refit)

    # Per-holdout log-loss curve over the w_elo grid (for the report curve and
    # the single-fold optimum).
    curves: Dict[str, List[float]] = {}
    fold_opt: Dict[str, float] = {}
    component_ll: Dict[str, Dict[str, float]] = {}
    for key, blob in data.items():
        elo, dc, y = blob["elo"], blob["dc"], blob["y"]  # type: ignore[index]
        curve = [log_loss(blend_elo_dc(elo, dc, w), y) for w in grid]
        curves[key] = curve
        fold_opt[key] = float(grid[int(np.argmin(curve))])
        component_ll[key] = {
            "elo_only": log_loss(elo, y),
            "dc_only": log_loss(dc, y),
        }

    # Leave-one-tournament-out: choose w on the OTHER three (pooled), score on
    # the held-out one.
    loto: Dict[str, Dict[str, float]] = {}
    for held in HOLDOUTS:
        others = [k for k in data if k != held.key]
        # Pool other holdouts' matches and grid-search pooled log-loss.
        elo_tr = np.vstack([data[k]["elo"] for k in others])  # type: ignore[index]
        dc_tr = np.vstack([data[k]["dc"] for k in others])  # type: ignore[index]
        y_tr = np.concatenate([data[k]["y"] for k in others])  # type: ignore[index]
        pooled_curve = [log_loss(blend_elo_dc(elo_tr, dc_tr, w), y_tr) for w in grid]
        w_star = float(grid[int(np.argmin(pooled_curve))])

        elo_te = data[held.key]["elo"]  # type: ignore[index]
        dc_te = data[held.key]["dc"]  # type: ignore[index]
        y_te = data[held.key]["y"]  # type: ignore[index]
        ll_blend = log_loss(blend_elo_dc(elo_te, dc_te, w_star), y_te)
        ll_elo = log_loss(elo_te, y_te)
        ll_dc = log_loss(dc_te, y_te)
        loto[held.key] = {
            "w_elo_chosen": w_star,
            "test_ll_blend": ll_blend,
            "test_ll_elo_only": ll_elo,
            "test_ll_dc_only": ll_dc,
        }

    # Global pooled optimum across all four holdouts (the headline w_elo).
    elo_all = np.vstack([data[h.key]["elo"] for h in HOLDOUTS])  # type: ignore[index]
    dc_all = np.vstack([data[h.key]["dc"] for h in HOLDOUTS])  # type: ignore[index]
    y_all = np.concatenate([data[h.key]["y"] for h in HOLDOUTS])  # type: ignore[index]
    pooled_curve_all = [log_loss(blend_elo_dc(elo_all, dc_all, w), y_all) for w in grid]
    w_pooled = float(grid[int(np.argmin(pooled_curve_all))])

    # Cross-validated test log-loss summary: mean over folds of the test LL when
    # each fold's w is chosen on the other folds (honest), versus the in-sample
    # pooled optimum.
    cv_test_ll = float(np.mean([loto[h.key]["test_ll_blend"] for h in HOLDOUTS]))
    cv_elo_ll = float(np.mean([loto[h.key]["test_ll_elo_only"] for h in HOLDOUTS]))
    cv_dc_ll = float(np.mean([loto[h.key]["test_ll_dc_only"] for h in HOLDOUTS]))

    return {
        "grid": grid.tolist(),
        "curves": curves,
        "fold_opt": fold_opt,
        "component_ll": component_ll,
        "loto": loto,
        "w_elo_pooled": w_pooled,
        "pooled_curve_all": pooled_curve_all,
        "cv_test_ll_blend": cv_test_ll,
        "cv_test_ll_elo_only": cv_elo_ll,
        "cv_test_ll_dc_only": cv_dc_ll,
        "n_per_holdout": {h.key: int(data[h.key]["n_matches"]) for h in HOLDOUTS},
        "n_total": int(len(y_all)),
    }


# ---------------------------------------------------------------------------
# Step 3 -- full 3-way convex blend on WC2022.
# ---------------------------------------------------------------------------


def _softmax3(theta: np.ndarray) -> np.ndarray:
    """Map 2 free params -> a point on the 3-simplex (w_elo, w_dc, w_market)."""
    z = np.array([theta[0], theta[1], 0.0], dtype=float)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def load_wc2022_market(path: str = WC2022_ODDS_JSON) -> Dict[str, np.ndarray]:
    """Load the saved WC2022 closing-odds pull -> per-match market consensus.

    The JSON is the raw historical-odds snapshots keyed by event id (see
    ``wc2022_odds_pull.py``). For each event we de-vig every book with Shin and
    take the per-column median consensus, mapped onto the canonical home/away
    team names. Returns ``{ "home|away": np.array([ph,pd,pa]) }`` keyed by the
    canonical fixture for joining against the results slate.
    """
    with open(path) as fh:
        blob = json.load(fh)
    events = blob["events"] if isinstance(blob, dict) and "events" in blob else blob

    out: Dict[str, np.ndarray] = {}
    for ev in events:
        home = ev.get("home_team")
        away = ev.get("away_team")
        if not home or not away:
            continue
        # Build {book: {home/draw/away: decimal}} from the snapshot.
        books: Dict[str, Dict[str, float]] = {}
        for bm in ev.get("bookmakers", []) or []:
            key = bm.get("key", "")
            prices: Dict[str, float] = {}
            for mkt in bm.get("markets", []) or []:
                if mkt.get("key") != "h2h":
                    continue
                for oc in mkt.get("outcomes", []) or []:
                    name = oc.get("name")
                    price = oc.get("price")
                    if price is None:
                        continue
                    if name == home:
                        prices["home"] = float(price)
                    elif name == away:
                        prices["away"] = float(price)
                    elif str(name).lower() == "draw":
                        prices["draw"] = float(price)
            if len(prices) == 3:
                books[key] = prices
        consensus = market_consensus(books)
        if consensus is None:
            continue
        ch, ca = canonical(str(home)), canonical(str(away))
        out["%s|%s" % (ch, ca)] = np.asarray(consensus, dtype=float)
    return out


def step3_wc2022(refit: bool = False) -> Dict[str, object]:
    """Fit the full 3-way blend on WC2022 and compare against baselines."""
    if not os.path.exists(WC2022_ODDS_JSON):
        raise FileNotFoundError(
            "WC2022 closing odds not found at %s; run wc2022_odds_pull.py first."
            % WC2022_ODDS_JSON
        )

    played = load_played()
    holdout = next(h for h in HOLDOUTS if h.key == "wc2022")
    blob = compute_holdout_probs(holdout, played, refit=refit)
    elo = blob["elo"]  # type: ignore[index]
    dc = blob["dc"]  # type: ignore[index]
    y = blob["y"]  # type: ignore[index]
    matches = blob["matches"]  # type: ignore[index]

    market_by_fixture = load_wc2022_market()

    # Align market rows to the slate order; keep only matches we have a market for.
    keep_idx: List[int] = []
    mkt_rows: List[np.ndarray] = []
    for i, m in enumerate(matches):  # type: ignore[arg-type]
        key = "%s|%s" % (m["home"], m["away"])
        rev = "%s|%s" % (m["away"], m["home"])
        if key in market_by_fixture:
            mkt_rows.append(market_by_fixture[key])
            keep_idx.append(i)
        elif rev in market_by_fixture:
            # Odds feed had the fixture with sides swapped: swap home/away probs.
            p = market_by_fixture[rev]
            mkt_rows.append(np.array([p[2], p[1], p[0]], dtype=float))
            keep_idx.append(i)

    keep = np.asarray(keep_idx, dtype=int)
    elo_k = elo[keep]
    dc_k = dc[keep]
    y_k = y[keep]
    mkt_k = np.vstack(mkt_rows) if mkt_rows else np.empty((0, 3))
    n = len(keep)

    def blend3(w: np.ndarray, e=elo_k, d=dc_k, mk=mkt_k) -> np.ndarray:
        p = w[0] * e + w[1] * d + w[2] * mk
        return p / p.sum(axis=1, keepdims=True)

    # Fit on the simplex via softmax parameterisation.
    from scipy.optimize import minimize

    def obj(theta: np.ndarray) -> float:
        w = _softmax3(theta)
        return log_loss(blend3(w), y_k)

    best = None
    for seed in ((0.0, 0.0), (1.0, -1.0), (-1.0, 1.0), (2.0, 0.0), (0.0, 2.0)):
        res = minimize(obj, np.array(seed, dtype=float), method="Nelder-Mead",
                       options={"xatol": 1e-7, "fatol": 1e-10, "maxiter": 2000})
        if best is None or res.fun < best.fun:
            best = res
    w_fit = _softmax3(best.x)

    current = np.array([0.25, 0.25, 0.50])
    configs: Dict[str, np.ndarray] = {
        "fitted": w_fit,
        "current_0.25/0.25/0.50": current,
        "market_only": np.array([0.0, 0.0, 1.0]),
        "elo_only": np.array([1.0, 0.0, 0.0]),
        "dc_only": np.array([0.0, 1.0, 0.0]),
        "equal_thirds": np.array([1 / 3, 1 / 3, 1 / 3]),
    }
    point_ll = {name: log_loss(blend3(w), y_k) for name, w in configs.items()}

    # Bootstrap CIs: resample matches with replacement 1000x.
    rng = np.random.default_rng(20262026)
    B = 1000
    boot_ll: Dict[str, np.ndarray] = {name: np.empty(B) for name in configs}
    boot_w_fit = np.empty((B, 3))
    boot_fit_ll = np.empty(B)
    boot_market_ll = np.empty(B)
    for b in range(B):
        idx = rng.integers(0, n, size=n)
        e_b, d_b, m_b, y_b = elo_k[idx], dc_k[idx], mkt_k[idx], y_k[idx]
        for name, w in configs.items():
            p = w[0] * e_b + w[1] * d_b + w[2] * m_b
            p = p / p.sum(axis=1, keepdims=True)
            boot_ll[name][b] = log_loss(p, y_b)

        def obj_b(theta: np.ndarray) -> float:
            w = _softmax3(theta)
            p = w[0] * e_b + w[1] * d_b + w[2] * m_b
            p = p / p.sum(axis=1, keepdims=True)
            return log_loss(p, y_b)

        rb = minimize(obj_b, best.x, method="Nelder-Mead",
                      options={"xatol": 1e-6, "fatol": 1e-9, "maxiter": 1500})
        w_b = _softmax3(rb.x)
        boot_w_fit[b] = w_b
        boot_fit_ll[b] = boot_ll["fitted"][b]
        boot_market_ll[b] = boot_ll["market_only"][b]

    def ci(arr: np.ndarray) -> Tuple[float, float]:
        return float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))

    # Paired bootstrap: does the fitted blend beat market-only on the SAME
    # resample? (delta = fitted_ll - market_ll; negative is better.)
    delta = boot_fit_ll - boot_market_ll
    p_fit_beats_market = float(np.mean(delta < 0.0))

    return {
        "n_matches": int(n),
        "w_fit": w_fit.tolist(),
        "point_ll": point_ll,
        "ll_ci": {name: ci(boot_ll[name]) for name in configs},
        "w_fit_ci": {
            "elo": ci(boot_w_fit[:, 0]),
            "dc": ci(boot_w_fit[:, 1]),
            "market": ci(boot_w_fit[:, 2]),
        },
        "w_fit_median": [
            float(np.median(boot_w_fit[:, 0])),
            float(np.median(boot_w_fit[:, 1])),
            float(np.median(boot_w_fit[:, 2])),
        ],
        "delta_fit_minus_market": {
            "mean": float(np.mean(delta)),
            "ci": ci(delta),
            "p_fitted_beats_market": p_fit_beats_market,
        },
        "matched_fixtures": [
            "%s vs %s" % (matches[i]["home"], matches[i]["away"]) for i in keep_idx
        ],
    }


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _print_step1(res: Dict[str, object]) -> None:
    print("=" * 70)
    print("STEP 1 -- leave-one-tournament-out Elo/DC relative weight")
    print("=" * 70)
    print("matches per holdout:", res["n_per_holdout"], " total:", res["n_total"])
    print()
    print("Per-fold (single-tournament) log-loss optimum w_elo:")
    for k, v in res["fold_opt"].items():  # type: ignore[union-attr]
        c = res["component_ll"][k]  # type: ignore[index]
        print("  %-10s w_elo*=%.2f   elo_only=%.4f  dc_only=%.4f"
              % (k, v, c["elo_only"], c["dc_only"]))
    print()
    print("LOTO (w chosen on other 3, scored on held-out):")
    for k, v in res["loto"].items():  # type: ignore[union-attr]
        print("  %-10s w_elo=%.2f  blend_ll=%.4f  elo=%.4f  dc=%.4f"
              % (k, v["w_elo_chosen"], v["test_ll_blend"],
                 v["test_ll_elo_only"], v["test_ll_dc_only"]))
    print()
    print("CV mean test log-loss:  blend=%.4f  elo_only=%.4f  dc_only=%.4f"
          % (res["cv_test_ll_blend"], res["cv_test_ll_elo_only"],
             res["cv_test_ll_dc_only"]))
    print("Pooled (all 4) optimum w_elo = %.2f" % res["w_elo_pooled"])
    print()
    print("Pooled log-loss curve (w_elo -> ll):")
    grid = res["grid"]
    curve = res["pooled_curve_all"]
    for w, ll in zip(grid, curve):  # type: ignore[arg-type]
        bar = "#" * int((ll - min(curve)) * 4000)  # type: ignore[arg-type]
        print("  %.2f  %.4f %s" % (w, ll, bar))


def _print_step3(res: Dict[str, object]) -> None:
    print("=" * 70)
    print("STEP 3 -- full 3-way blend on WC2022")
    print("=" * 70)
    print("matched matches:", res["n_matches"])
    wf = res["w_fit"]  # type: ignore[index]
    print("fitted weights (elo, dc, market) = %.3f / %.3f / %.3f" % tuple(wf))
    print("fitted weights bootstrap median  = %.3f / %.3f / %.3f"
          % tuple(res["w_fit_median"]))  # type: ignore[arg-type]
    print()
    print("Point log-loss (lower better) with 95%% bootstrap CI:")
    for name, ll in res["point_ll"].items():  # type: ignore[union-attr]
        lo, hi = res["ll_ci"][name]  # type: ignore[index]
        print("  %-26s %.4f  [%.4f, %.4f]" % (name, ll, lo, hi))
    print()
    d = res["delta_fit_minus_market"]  # type: ignore[index]
    print("fitted - market_only log-loss delta: mean=%.4f CI=[%.4f, %.4f]"
          % (d["mean"], d["ci"][0], d["ci"][1]))
    print("P(fitted beats market_only on resample) = %.1f%%"
          % (d["p_fitted_beats_market"] * 100))


def main(argv: Sequence[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "step1"
    refit = "--refit" in argv
    if cmd == "step1":
        res = step1_loto(refit=refit)
        _print_step1(res)
        out = os.path.join(CACHE_DIR, "step1_result.json")
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(out, "w") as fh:
            json.dump(res, fh, indent=2)
        print("\nwrote", out)
    elif cmd == "step3":
        res = step3_wc2022(refit=refit)
        _print_step3(res)
        out = os.path.join(CACHE_DIR, "step3_result.json")
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(out, "w") as fh:
            json.dump(res, fh, indent=2)
        print("\nwrote", out)
    else:
        print("usage: blend_fit.py [step1|step3] [--refit]")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
