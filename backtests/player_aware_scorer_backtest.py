#!/usr/bin/env python
"""#8 — backtest the player-aware anytime-scorer model vs a no-player-awareness
baseline on the 2022 World Cup, out of sample (shares learned on WC2018).

Reads the local StatsBomb event cache (``data/raw/statsbomb``) — no network when
warm — and writes a JSON report. Reports Brier + log-loss for both models and an
adopt/don't-adopt recommendation. Monitoring/analytics only.

    .venv/bin/python backtests/player_aware_scorer_backtest.py \
        [--cache-dir data/raw/statsbomb] [--out backtests/_cache/player_aware_scorer_result.json]
"""
import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wca.data import statsbomb  # noqa: E402
from wca.models import scorer_backtest as sb  # noqa: E402

WC2018_SEASON = 3
WC2022_SEASON = 106


def _load_events(season_id, cache_dir):
    matches = statsbomb.fetch_matches(statsbomb.WC_COMPETITION_ID, season_id,
                                      cache_dir=cache_dir)
    out = {}
    for m in matches:
        mid = m["match_id"]
        out[mid] = statsbomb.fetch_events(mid, cache_dir=cache_dir)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", default=str(ROOT / "data" / "raw" / "statsbomb"))
    ap.add_argument("--out", default=str(ROOT / "backtests" / "_cache"
                                         / "player_aware_scorer_result.json"))
    ap.add_argument("--lambda-team", type=float,
                    help="override the team goal-expectation prior")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    logging.info("loading WC2018 (train) + WC2022 (test) from cache")
    train = _load_events(WC2018_SEASON, args.cache_dir)
    test = _load_events(WC2022_SEASON, args.cache_dir)

    res = sb.run_backtest(train, test, lambda_team=args.lambda_team)

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "generated_utc": ts,
        "train": "WC2018", "test": "WC2022",
        "n_test_matches": res.n_matches,
        "covered_player_matches": res.n_covered,
        "uncovered_player_matches": res.n_uncovered,
        "coverage_pct": round(100 * res.coverage, 1),
        "lambda_team_prior": round(res.lambda_team, 4),
        "player_aware": {"brier": round(res.pa_brier, 5),
                         "log_loss": round(res.pa_log_loss, 5)},
        "baseline_equal_share": {"brier": round(res.base_brier, 5),
                                 "log_loss": round(res.base_log_loss, 5)},
        "brier_improvement": round(res.brier_improvement, 5),
        "log_loss_improvement": round(res.log_loss_improvement, 5),
        "recommend_adopt": res.recommend_adopt,
        "basis": ("player-aware = DC-style team lambda x StatsBomb npxg-share "
                  "(learned WC2018) x minutes; baseline = same lambda spread "
                  "equally across appearing players. OOS: 2018->2022."),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print("\n=== #8 player-aware anytime-scorer backtest (WC2018 -> WC2022) ===")
    print("test matches: %d   covered player-matches: %d (%.1f%% coverage)"
          % (res.n_matches, res.n_covered, 100 * res.coverage))
    print("lambda_team prior: %.3f" % res.lambda_team)
    print("                        Brier     log-loss")
    print("  player-aware    :   %.5f    %.5f" % (res.pa_brier, res.pa_log_loss))
    print("  baseline (equal):   %.5f    %.5f" % (res.base_brier, res.base_log_loss))
    print("  improvement     :   %+.5f    %+.5f"
          % (res.brier_improvement, res.log_loss_improvement))
    print("  RECOMMEND ADOPT : %s" % ("YES" if res.recommend_adopt else "NO"))
    print("report: %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
