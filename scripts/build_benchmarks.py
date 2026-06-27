"""Build benchmarks_data.json for the benchmarking tab.

Joins model_predictions_log.jsonl (first card per fixture, pre-match) against
processed/wc2026_results.json to compute Brier, log-loss, calibration,
goals/xG accuracy, and modelling improvement suggestions.
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREDS_LOG = os.path.join(ROOT, "data", "model_predictions_log.jsonl")
RESULTS_FILE = os.path.join(ROOT, "data", "processed", "wc2026_results.json")
PROP_CAL_FILE = os.path.join(ROOT, "data", "prop_calibration.json")
OUT_FILE = os.path.join(ROOT, "site", "benchmarks_data.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _brier(probs, outcome):
    """3-way Brier score (divided by 2 as per Brier 1950)."""
    oh = 1 if outcome == "home" else 0
    od = 1 if outcome == "draw" else 0
    oa = 1 if outcome == "away" else 0
    ph, pd, pa = probs["home"], probs["draw"], probs["away"]
    return ((ph - oh) ** 2 + (pd - od) ** 2 + (pa - oa) ** 2) / 2.0


def _logloss(probs, outcome):
    p = probs.get(outcome, 1e-9)
    return -math.log(max(p, 1e-9))


def _accuracy(probs, outcome):
    best = max(probs, key=probs.get)
    return 1 if best == outcome else 0


def _poisson_pmf(lam, k):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _p_over(lam_h, lam_a, line=2.5):
    """P(total goals > line) under independent Poisson."""
    lam_total = lam_h + lam_a
    cutoff = int(line)
    p_under_eq = sum(_poisson_pmf(lam_total, k) for k in range(cutoff + 1))
    return 1.0 - p_under_eq


def _top_scoreline(lam_h, lam_a, n=6):
    """Return top-n (h, a, prob) scorelines."""
    scores = []
    for h in range(8):
        for a in range(8):
            p = _poisson_pmf(lam_h, h) * _poisson_pmf(lam_a, a)
            scores.append((h, a, p))
    scores.sort(key=lambda x: -x[2])
    return scores[:n]


def _calibration_bins(records, model_key, n_bins=10):
    """Reliability diagram data: predicted prob vs actual hit-rate per bin."""
    bins = [{"sum_pred": 0.0, "count": 0, "hits": 0} for _ in range(n_bins)]
    for r in records:
        probs = r[model_key]
        outcome = r["outcome"]
        for leg in ("home", "draw", "away"):
            p = probs[leg]
            actual = 1 if outcome == leg else 0
            b = min(int(p * n_bins), n_bins - 1)
            bins[b]["sum_pred"] += p
            bins[b]["count"] += 1
            bins[b]["hits"] += actual
    result = []
    for i, b in enumerate(bins):
        if b["count"] > 0:
            result.append({
                "mid": round((i + 0.5) / n_bins, 3),
                "mean_pred": round(b["sum_pred"] / b["count"], 4),
                "hit_rate": round(b["hits"] / b["count"], 4),
                "n": b["count"],
            })
    return result


def _slope_intercept(xs, ys):
    """OLS slope and intercept."""
    n = len(xs)
    if n < 2:
        return None, None
    xm = sum(xs) / n
    ym = sum(ys) / n
    num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    den = sum((x - xm) ** 2 for x in xs)
    if den == 0:
        return None, None
    slope = num / den
    intercept = ym - slope * xm
    return round(slope, 4), round(intercept, 4)


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

def load_first_predictions():
    """Return dict fixture -> first-card prediction entry."""
    seen = {}
    with open(PREDS_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            fx = entry["fixture"]
            if fx not in seen:
                seen[fx] = entry
    return seen


def load_results():
    with open(RESULTS_FILE) as f:
        data = json.load(f)
    results = {}
    for r in data.get("results", []):
        score = r.get("score") or ""
        parts = score.split("-") if score else []
        if len(parts) == 2:
            try:
                gh, ga = int(parts[0]), int(parts[1])
            except ValueError:
                gh, ga = None, None
        else:
            gh, ga = None, None
        results[r["fixture"]] = {
            "outcome": r["outcome"],
            "score": r.get("score"),
            "goals_home": gh,
            "goals_away": ga,
            "date": r.get("date"),
            "kickoff_utc": r.get("kickoff_utc"),
        }
    return results


def load_prop_cal():
    if not os.path.exists(PROP_CAL_FILE):
        return {}
    with open(PROP_CAL_FILE) as f:
        data = json.load(f)
    out = {}
    for fx in data.get("fixtures", []):
        out[fx["fixture"]] = fx
    return out


# ---------------------------------------------------------------------------
# Join & compute
# ---------------------------------------------------------------------------

def build_matched(preds, results):
    matched = []
    for fx, result in results.items():
        if result["outcome"] == "pending" or result["outcome"] is None:
            continue
        pred = preds.get(fx)
        if pred is None:
            # Try case-insensitive / simple token-match fallback
            for pk in preds:
                if pk.lower() == fx.lower():
                    pred = preds[pk]
                    break
        if pred is None:
            continue
        matched.append({
            "fixture": fx,
            "date": result["date"],
            "kickoff_utc": result.get("kickoff_utc"),
            "outcome": result["outcome"],
            "score": result["score"],
            "goals_home": result["goals_home"],
            "goals_away": result["goals_away"],
            "model": pred["model"],
            "elo": pred["elo"],
            "dc": pred["dc"],
            "market": pred["market"],
            "lambda_home": pred.get("lambda_home"),
            "lambda_away": pred.get("lambda_away"),
        })
    # Sort by date
    matched.sort(key=lambda r: r.get("date", "") or "")
    return matched


def compute_metrics(matched):
    models = ["model", "elo", "dc", "market"]
    agg = {}
    for m in models:
        briers = [_brier(r[m], r["outcome"]) for r in matched]
        losses = [_logloss(r[m], r["outcome"]) for r in matched]
        accs = [_accuracy(r[m], r["outcome"]) for r in matched]
        n = len(briers)
        agg[m] = {
            "n": n,
            "brier": round(sum(briers) / n, 4) if n else None,
            "logloss": round(sum(losses) / n, 4) if n else None,
            "accuracy": round(sum(accs) / n, 4) if n else None,
        }
    # Brier Skill Score: model vs market
    for m in ["model", "elo", "dc"]:
        bm = agg[m]["brier"]
        bmkt = agg["market"]["brier"]
        if bm is not None and bmkt is not None and bmkt > 0:
            bss = round(1.0 - bm / bmkt, 4)
        else:
            bss = None
        agg[m]["bss"] = bss
    agg["market"]["bss"] = 0.0
    return agg


def compute_goals_calibration(matched):
    rows = [r for r in matched if r["goals_home"] is not None and r["lambda_home"] is not None]
    if not rows:
        return {}
    home_lams = [r["lambda_home"] for r in rows]
    away_lams = [r["lambda_away"] for r in rows]
    actual_h = [r["goals_home"] for r in rows]
    actual_a = [r["goals_away"] for r in rows]
    total_lam = [h + a for h, a in zip(home_lams, away_lams)]
    total_act = [h + a for h, a in zip(actual_h, actual_a)]

    n = len(rows)
    mean_lam_h = sum(home_lams) / n
    mean_lam_a = sum(away_lams) / n
    mean_act_h = sum(actual_h) / n
    mean_act_a = sum(actual_a) / n
    mean_total_lam = sum(total_lam) / n
    mean_total_act = sum(total_act) / n

    # Calibration factor: actual / expected
    calib_h = mean_act_h / mean_lam_h if mean_lam_h else None
    calib_a = mean_act_a / mean_lam_a if mean_lam_a else None
    calib_total = mean_total_act / mean_total_lam if mean_total_lam else None

    # O/U 2.5 calibration
    ou_preds = [_p_over(r["lambda_home"], r["lambda_away"], 2.5) for r in rows]
    ou_actuals = [1 if h + a > 2.5 else 0 for h, a in zip(actual_h, actual_a)]
    mean_ou_pred = sum(ou_preds) / n
    mean_ou_actual = sum(ou_actuals) / n
    ou_slope, ou_intercept = _slope_intercept(ou_preds, ou_actuals)

    # Per-match scatter data
    scatter = []
    for r in rows:
        lam_t = (r["lambda_home"] or 0) + (r["lambda_away"] or 0)
        act_t = (r["goals_home"] or 0) + (r["goals_away"] or 0)
        scatter.append({
            "fixture": r["fixture"],
            "score": r["score"],
            "lambda_home": round(r["lambda_home"], 3),
            "lambda_away": round(r["lambda_away"], 3),
            "lambda_total": round(lam_t, 3),
            "actual_home": r["goals_home"],
            "actual_away": r["goals_away"],
            "actual_total": act_t,
            "p_over25": round(_p_over(r["lambda_home"], r["lambda_away"], 2.5), 3),
            "actual_over25": 1 if act_t > 2 else 0,
            "error_total": round(act_t - lam_t, 3),
        })

    # Goals distribution: actual vs Poisson expected
    max_g = max(max(total_act), 8)
    dist_actual = [0] * (max_g + 1)
    dist_expected = [0.0] * (max_g + 1)
    for r in rows:
        lt = (r["lambda_home"] or 0) + (r["lambda_away"] or 0)
        at = (r["goals_home"] or 0) + (r["goals_away"] or 0)
        if at <= max_g:
            dist_actual[at] += 1
        for g in range(max_g + 1):
            dist_expected[g] += _poisson_pmf(lt, g)
    dist_actual_pct = [round(v / n, 4) for v in dist_actual]
    dist_expected_pct = [round(v / n, 4) for v in dist_expected]

    return {
        "n": n,
        "mean_lambda_home": round(mean_lam_h, 3),
        "mean_lambda_away": round(mean_lam_a, 3),
        "mean_actual_home": round(mean_act_h, 3),
        "mean_actual_away": round(mean_act_a, 3),
        "mean_lambda_total": round(mean_total_lam, 3),
        "mean_actual_total": round(mean_total_act, 3),
        "calibration_factor_home": round(calib_h, 4) if calib_h else None,
        "calibration_factor_away": round(calib_a, 4) if calib_a else None,
        "calibration_factor_total": round(calib_total, 4) if calib_total else None,
        "mean_ou25_pred": round(mean_ou_pred, 4),
        "mean_ou25_actual": round(mean_ou_actual, 4),
        "ou_slope": ou_slope,
        "ou_intercept": ou_intercept,
        "scatter": scatter,
        "dist_actual": dist_actual_pct,
        "dist_expected": dist_expected_pct,
        "dist_goals": list(range(max_g + 1)),
    }


def compute_scoreline_accuracy(matched):
    """How often does the top-1 / top-3 / top-6 predicted scoreline match actual?"""
    rows = [r for r in matched if r["goals_home"] is not None and r["lambda_home"] is not None]
    if not rows:
        return {}
    top1_hits = 0
    top3_hits = 0
    top6_hits = 0
    per_match = []
    for r in rows:
        sl = _top_scoreline(r["lambda_home"], r["lambda_away"], n=6)
        actual_h = r["goals_home"]
        actual_a = r["goals_away"]
        top1 = sl[0][:2] == (actual_h, actual_a)
        top3 = any(s[:2] == (actual_h, actual_a) for s in sl[:3])
        top6 = any(s[:2] == (actual_h, actual_a) for s in sl[:6])
        top1_hits += top1
        top3_hits += top3
        top6_hits += top6
        top_preds = [{"score": f"{s[0]}-{s[1]}", "prob": round(s[2] * 100, 2)} for s in sl[:3]]
        per_match.append({
            "fixture": r["fixture"],
            "score": r["score"],
            "top3_preds": top_preds,
            "top1_hit": top1,
            "top3_hit": top3,
        })
    n = len(rows)
    return {
        "n": n,
        "top1_rate": round(top1_hits / n, 4),
        "top3_rate": round(top3_hits / n, 4),
        "top6_rate": round(top6_hits / n, 4),
        "per_match": per_match,
    }


def compute_outcome_table(matched):
    """Per-match table for display."""
    rows = []
    for r in matched:
        outcome = r["outcome"]
        model_correct = _accuracy(r["model"], outcome)
        market_correct = _accuracy(r["market"], outcome)
        rows.append({
            "fixture": r["fixture"],
            "date": r["date"],
            "score": r["score"],
            "outcome": outcome,
            "model": {k: round(v, 3) for k, v in r["model"].items()},
            "elo": {k: round(v, 3) for k, v in r["elo"].items()},
            "dc": {k: round(v, 3) for k, v in r["dc"].items()},
            "market": {k: round(v, 3) for k, v in r["market"].items()},
            "lambda_home": round(r["lambda_home"], 3) if r["lambda_home"] else None,
            "lambda_away": round(r["lambda_away"], 3) if r["lambda_away"] else None,
            "model_brier": round(_brier(r["model"], outcome), 4),
            "market_brier": round(_brier(r["market"], outcome), 4),
            "model_logloss": round(_logloss(r["model"], outcome), 4),
            "model_correct": model_correct,
            "market_correct": market_correct,
            "model_edge_brier": round(_brier(r["market"], outcome) - _brier(r["model"], outcome), 4),
        })
    return rows


def improvement_suggestions(matched, goals_cal, metrics):
    """Generate data-driven improvement suggestions."""
    suggestions = []
    n = len(matched)
    if n < 5:
        return suggestions

    # 1. Goals total calibration
    cal_total = goals_cal.get("calibration_factor_total")
    mean_lam = goals_cal.get("mean_lambda_total")
    mean_act = goals_cal.get("mean_actual_total")
    if cal_total and cal_total > 1.08:
        suggestions.append({
            "category": "Goals / xG",
            "severity": "high" if cal_total > 1.2 else "medium",
            "title": "Model systematically under-predicts total goals",
            "detail": (
                f"Expected avg {mean_lam:.2f} goals/match vs actual {mean_act:.2f} "
                f"(calibration factor {cal_total:.2f}x). WC 2018 avg was 2.64, "
                f"WC 2022 was 2.69. Group stage WC 2026 is running hotter — "
                f"consider raising DC attack priors or adding a WC-specific inflation parameter."
            ),
            "stat": f"+{round((cal_total - 1) * 100, 1)}% goals vs model",
        })
    elif cal_total and cal_total < 0.92:
        suggestions.append({
            "category": "Goals / xG",
            "severity": "medium",
            "title": "Model over-predicts total goals",
            "detail": (
                f"Expected avg {mean_lam:.2f} goals/match vs actual {mean_act:.2f} "
                f"(calibration factor {cal_total:.2f}x). Check DC attack/defence shrinkage."
            ),
            "stat": f"{round((1 - cal_total) * 100, 1)}% fewer goals than model",
        })

    # 2. Home/Away calibration asymmetry
    cal_h = goals_cal.get("calibration_factor_home")
    cal_a = goals_cal.get("calibration_factor_away")
    if cal_h and cal_a:
        asym = abs(cal_h - cal_a)
        if asym > 0.15:
            which = "home" if cal_h > cal_a else "away"
            other = "away" if which == "home" else "home"
            fac = cal_h if which == "home" else cal_a
            suggestions.append({
                "category": "Goals / xG",
                "severity": "medium",
                "title": f"Model miscalibrated for {which}-team goals",
                "detail": (
                    f"{which.title()} calibration factor = {fac:.2f} vs "
                    f"{other} = {(cal_a if which == 'home' else cal_h):.2f}. "
                    "This asymmetry affects BTTS and team-level O/U markets. "
                    "Consider separate home/away inflation parameters in the DC model."
                ),
                "stat": f"{which.title()} factor {fac:.2f}x",
            })

    # 3. Over/Under calibration
    ou_slope = goals_cal.get("ou_slope")
    ou_intercept = goals_cal.get("ou_intercept")
    mean_ou_pred = goals_cal.get("mean_ou25_pred")
    mean_ou_actual = goals_cal.get("mean_ou25_actual")
    if mean_ou_pred and mean_ou_actual:
        ou_diff = mean_ou_actual - mean_ou_pred
        if abs(ou_diff) > 0.05:
            direction = "over-priced" if ou_diff > 0 else "under-priced"
            suggestions.append({
                "category": "Over/Under",
                "severity": "high" if abs(ou_diff) > 0.1 else "medium",
                "title": f"O/U 2.5 fair prices are {direction}",
                "detail": (
                    f"Model predicted {mean_ou_pred:.1%} of matches go Over 2.5 goals, "
                    f"actual rate is {mean_ou_actual:.1%} (delta {ou_diff:+.1%}). "
                    "Recalibrate by applying the goals calibration factor to lambda before "
                    "pricing O/U markets, or use an empirical WC-specific baseline rate."
                ),
                "stat": f"O2.5 actual {mean_ou_actual:.0%} vs pred {mean_ou_pred:.0%}",
            })

    # 4. Brier Skill Score vs market
    bss_model = metrics.get("model", {}).get("bss")
    bss_dc = metrics.get("dc", {}).get("bss")
    if bss_model is not None:
        if bss_model < 0:
            suggestions.append({
                "category": "1X2 Prices",
                "severity": "high",
                "title": "Model performing worse than market consensus on 1X2",
                "detail": (
                    f"Brier Skill Score vs market = {bss_model:.3f} (negative = worse than market). "
                    f"DC alone BSS = {bss_dc:.3f}. The blend may need rebalancing. "
                    "Review blend weights (currently 0.10 Elo + 0.30 DC + 0.60 Market) — "
                    "increasing market weight or adding a Platt scaling pass on DC probabilities "
                    "could improve calibration."
                ),
                "stat": f"BSS {bss_model:+.3f}",
            })
        elif bss_model > 0.02:
            suggestions.append({
                "category": "1X2 Prices",
                "severity": "info",
                "title": "Model is beating market consensus on 1X2 (positive edge)",
                "detail": (
                    f"BSS vs market = {bss_model:+.3f}. The blend is adding positive value. "
                    "Monitor to see if this persists through the knockout rounds — "
                    "knockout-stage calibration historically diverges from group stage."
                ),
                "stat": f"BSS {bss_model:+.3f}",
            })

    # 5. Draw rate
    actual_draws = sum(1 for r in matched if r["outcome"] == "draw")
    pred_draws = sum(r["model"]["draw"] for r in matched)
    draw_ratio = actual_draws / pred_draws if pred_draws else None
    if draw_ratio and abs(draw_ratio - 1.0) > 0.15:
        direction = "more" if draw_ratio > 1 else "fewer"
        suggestions.append({
            "category": "1X2 Prices",
            "severity": "medium",
            "title": f"Model under-estimates draw frequency ({direction} draws than predicted)",
            "detail": (
                f"Actual draws: {actual_draws}/{n} ({actual_draws/n:.1%}), "
                f"model expected {pred_draws/n:.1%} of matches to draw. "
                "WC group stages often see strategic draws late in the group. "
                "A draw inflation parameter calibrated on WC18+22 data could correct this."
            ),
            "stat": f"{actual_draws/n:.0%} actual vs {pred_draws/n:.0%} pred",
        })

    # 6. Scoreline model
    # (injected from caller if scoreline data available)

    # 7. High-variance match signal (Norway-France style)
    high_err_matches = [
        r for r in goals_cal.get("scatter", [])
        if abs(r.get("error_total", 0)) >= 2.5
    ]
    if len(high_err_matches) >= 2:
        worst = max(high_err_matches, key=lambda r: abs(r.get("error_total", 0)))
        suggestions.append({
            "category": "Uncertainty / Variance",
            "severity": "medium",
            "title": "Large goal-total errors suggest model underestimates match variance",
            "detail": (
                f"{len(high_err_matches)} matches had total-goals error ≥ 2.5 goals. "
                f"Worst: {worst['fixture']} "
                f"(expected {worst['lambda_total']:.1f}, actual {worst['actual_total']}). "
                "A Negative Binomial goal model (with dispersion parameter fit on WC18+22) "
                "would better capture the fat tail. Also consider adding a 'blowout' prior "
                "for large team-strength mismatches (λ ratio > 3x)."
            ),
            "stat": f"{len(high_err_matches)} matches with ≥2.5 goal error",
        })

    return suggestions


def statsbomb_context():
    """Static WC18+22 StatsBomb reference stats for calibration comparison."""
    return {
        "source": "StatsBomb Open Data (WC2018 season 3, WC2022 season 106)",
        "wc2018": {
            "matches": 64,
            "avg_goals_per_match": 2.64,
            "over25_rate": 0.578,
            "btts_rate": 0.469,
            "avg_corners_per_match": 9.0,
            "avg_yellows_per_match": 3.4,
        },
        "wc2022": {
            "matches": 64,
            "avg_goals_per_match": 2.69,
            "over25_rate": 0.594,
            "btts_rate": 0.500,
            "avg_corners_per_match": 9.3,
            "avg_yellows_per_match": 3.8,
        },
        "note": (
            "Model corner/card priors are calibrated on WC18+22 StatsBomb data. "
            "WC2026 actuals are being tracked here in real-time to detect drift."
        ),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    preds = load_first_predictions()
    results = load_results()
    prop_cal = load_prop_cal()

    matched = build_matched(preds, results)
    print(f"Matched {len(matched)} fixtures", file=sys.stderr)

    if not matched:
        print("No matched fixtures — nothing to benchmark.", file=sys.stderr)
        out = {"meta": {"generated": datetime.now(timezone.utc).isoformat(), "n": 0}, "error": "no_data"}
        with open(OUT_FILE, "w") as f:
            json.dump(out, f, indent=2)
        return

    metrics = compute_metrics(matched)
    goals_cal = compute_goals_calibration(matched)
    scoreline_acc = compute_scoreline_accuracy(matched)
    outcome_table = compute_outcome_table(matched)
    calibration = {
        m: _calibration_bins(matched, m) for m in ["model", "elo", "dc", "market"]
    }
    suggestions = improvement_suggestions(matched, goals_cal, metrics)
    sb_context = statsbomb_context()

    # WC2026 summary stats from matched matches
    rows_with_goals = [r for r in matched if r["goals_home"] is not None]
    n_goals = len(rows_with_goals)
    if n_goals:
        total_goals = sum((r["goals_home"] or 0) + (r["goals_away"] or 0) for r in rows_with_goals)
        over25_actual = sum(1 for r in rows_with_goals if (r["goals_home"] or 0) + (r["goals_away"] or 0) > 2)
        wc26_summary = {
            "matches": n_goals,
            "avg_goals_per_match": round(total_goals / n_goals, 3),
            "over25_rate": round(over25_actual / n_goals, 3),
        }
    else:
        wc26_summary = {}

    out = {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "n_matched": len(matched),
            "n_results": len(results),
            "n_predictions": len(preds),
        },
        "metrics": metrics,
        "goals_calibration": goals_cal,
        "scoreline_accuracy": scoreline_acc,
        "outcome_table": outcome_table,
        "calibration_bins": calibration,
        "suggestions": suggestions,
        "statsbomb_context": sb_context,
        "wc26_summary": wc26_summary,
    }

    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Written to {OUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
