"""Tests for wca.shadowscore — the SHADOW-model scorer.

Exercise:

* the metric helpers (Brier / log-loss, 1X2 and binary) on hand-built inputs
  with arithmetic checked by hand;
* the totals over/under probability from a goal-lambda pair;
* the paired-scoring pipeline on a tiny synthetic log + results set where the
  paired Brier/log-loss diffs are known exactly;
* the pre-kickoff dedup (last build before kickoff wins; post-hoc excluded);
* the PROMOTE / KILL / COLLECTING decision thresholds (n gate + CI sign);
* the guard that 1X2 shadows are recomputed over ALL rows (not just dual-writes)
  while totals shadows only score where the lambdas were logged.
"""
from __future__ import annotations

import math

from wca import shadowscore


# ---------------------------------------------------------------------------
# Metric helpers.
# ---------------------------------------------------------------------------


def test_brier_1x2_hand_value():
    triple = {"home": 0.6, "draw": 0.3, "away": 0.1}
    # outcome home: (0.6-1)^2 + (0.3-0)^2 + (0.1-0)^2 = 0.16+0.09+0.01 = 0.26
    assert abs(shadowscore.brier_1x2(triple, "home") - 0.26) < 1e-9
    # outcome away: (0.6)^2 + (0.3)^2 + (0.1-1)^2 = 0.36+0.09+0.81 = 1.26
    assert abs(shadowscore.brier_1x2(triple, "away") - 1.26) < 1e-9


def test_log_loss_1x2_hand_value():
    triple = {"home": 0.6, "draw": 0.3, "away": 0.1}
    assert abs(shadowscore.log_loss_1x2(triple, "home") - (-math.log(0.6))) < 1e-9
    assert abs(shadowscore.log_loss_1x2(triple, "away") - (-math.log(0.1))) < 1e-9


def test_brier_binary_hand_value():
    assert abs(shadowscore.brier_binary(0.7, 1) - 0.09) < 1e-9   # (0.7-1)^2
    assert abs(shadowscore.brier_binary(0.7, 0) - 0.49) < 1e-9   # (0.7-0)^2
    assert shadowscore.brier_binary(None, 1) is None


def test_log_loss_binary_hand_value():
    assert abs(shadowscore.log_loss_binary(0.7, 1) - (-math.log(0.7))) < 1e-9
    assert abs(shadowscore.log_loss_binary(0.7, 0) - (-math.log(0.3))) < 1e-9


def test_prob_over_line_is_poisson_total():
    # total goals ~ Poisson(lam_h + lam_a); P(>2.5) = P(>=3) = 1 - P(0..2).
    lam_h, lam_a = 1.5, 1.0
    lam = lam_h + lam_a
    want = 1.0 - sum(math.exp(-lam) * lam ** k / math.factorial(k) for k in range(3))
    got = shadowscore.prob_over_line(lam_h, lam_a, 2.5)
    assert abs(got - want) < 1e-6  # tail truncated at max_goals -> ~1e-8 slack
    assert shadowscore.prob_over_line(float("nan"), 1.0, 2.5) is None


# ---------------------------------------------------------------------------
# Bootstrap + decision thresholds.
# ---------------------------------------------------------------------------


def test_bootstrap_ci_is_deterministic_and_brackets_mean():
    diffs = [-0.02, -0.03, -0.01, -0.04, -0.02, -0.03] * 10  # all negative
    lo1, hi1 = shadowscore.bootstrap_ci(diffs)
    lo2, hi2 = shadowscore.bootstrap_ci(diffs)
    assert (lo1, hi1) == (lo2, hi2)          # deterministic (fixed seed)
    assert lo1 < 0 and hi1 < 0                # all-negative sample -> CI < 0
    assert lo1 <= sum(diffs) / len(diffs) <= hi1


def test_decide_promote_when_both_ci_below_zero():
    # n>=30, both Brier & log-loss CIs entirely below 0 -> PROMOTE.
    assert shadowscore.decide(40, -0.02, -0.005, -0.03, -0.01) == "PROMOTE-candidate"


def test_decide_kill_when_both_ci_above_zero():
    assert shadowscore.decide(40, 0.005, 0.02, 0.01, 0.03) == "KILL-candidate"


def test_decide_collecting_under_n_gate():
    d = shadowscore.decide(10, -0.02, -0.005, -0.03, -0.01)
    assert d == "COLLECTING n=10/%d" % shadowscore.DECISION_MIN_N


def test_decide_collecting_when_ci_crosses_zero():
    # n>=30 but Brier CI straddles 0 -> not conclusive.
    assert shadowscore.decide(40, -0.02, 0.01, -0.03, -0.01).startswith("COLLECTING")


def test_decide_collecting_when_metrics_disagree():
    # Brier promotes but log-loss kills -> COLLECTING (strict: both must agree).
    assert shadowscore.decide(40, -0.02, -0.005, 0.01, 0.03).startswith("COLLECTING")


# ---------------------------------------------------------------------------
# Dedup to last pre-kickoff row.
# ---------------------------------------------------------------------------


def _row(fixture, generated, model, market=None, elo=None, dc=None, **extra):
    row = {"fixture": fixture, "generated": generated, "model": model}
    if market is not None:
        row["market"] = market
    if elo is not None:
        row["elo"] = elo
    if dc is not None:
        row["dc"] = dc
    row.update(extra)
    return row


_M = {"home": 0.6, "draw": 0.25, "away": 0.15}
_MKT = {"home": 0.55, "draw": 0.27, "away": 0.18}
_ELO = {"home": 0.5, "draw": 0.3, "away": 0.2}
_DC = {"home": 0.62, "draw": 0.23, "away": 0.15}


def test_dedup_picks_last_pre_kickoff_row():
    rows = [
        _row("Brazil vs Serbia", "2026-06-13T08:00:00", {"home": 0.4, "draw": 0.3, "away": 0.3}),
        _row("Brazil vs Serbia", "2026-06-14T08:00:00", _M),          # latest pre-kickoff
        _row("Brazil vs Serbia", "2026-06-14T19:00:00", {"home": 0.9, "draw": 0.05, "away": 0.05}),  # post-hoc
    ]
    result = {"fixture": "Brazil vs Serbia", "kickoff_utc": "2026-06-14T18:00:00Z",
              "date": "2026-06-14", "outcome": "home", "score": "2-1"}
    row = shadowscore.dedup_pre_kickoff(rows, result)
    assert row is not None
    assert row["generated"] == "2026-06-14T08:00:00"   # not the 08:00 stale, not the post-hoc


def test_dedup_none_when_only_post_hoc():
    rows = [_row("Brazil vs Serbia", "2026-06-14T20:00:00", _M)]
    result = {"fixture": "Brazil vs Serbia", "kickoff_utc": "2026-06-14T18:00:00Z",
              "date": "2026-06-14", "outcome": "home", "score": "2-1"}
    assert shadowscore.dedup_pre_kickoff(rows, result) is None


# ---------------------------------------------------------------------------
# Paired 1X2 scoring end to end.
# ---------------------------------------------------------------------------


def _full_row(fixture, generated):
    return _row(fixture, generated, _M, market=_MKT, elo=_ELO, dc=_DC)


def test_scoreboard_scores_1x2_shadows_over_all_rows():
    # Two settled fixtures, each with a pre-kickoff row carrying full triples but
    # NO gb/tl lambdas: mw90/shrink/market must still be scored (n=2), totals not.
    rows = [
        _full_row("Brazil vs Serbia", "2026-06-13T08:00:00"),
        _full_row("Spain vs Japan", "2026-06-20T08:00:00"),
    ]
    results = [
        {"fixture": "Brazil vs Serbia", "kickoff_utc": "2026-06-13T18:00:00Z",
         "date": "2026-06-13", "outcome": "home", "score": "2-0"},
        {"fixture": "Spain vs Japan", "kickoff_utc": "2026-06-20T18:00:00Z",
         "date": "2026-06-20", "outcome": "draw", "score": "1-1"},
    ]
    sb = shadowscore.build_scoreboard(rows, results, "2026-07-08T00:00:00")
    by_family = {r["family"]: r for r in sb["shadows"]}
    assert sb["meta"]["matched_fixtures"] == 2
    for fam in ("mw90", "shrink", "market"):
        assert by_family[fam]["market"] == "1x2"
        assert by_family[fam]["n"] == 2          # scored over ALL rows
        assert by_family[fam]["decision"].startswith("COLLECTING")
    # No gb/tl lambdas logged -> totals families score nothing.
    assert by_family["gb"]["n"] == 0
    assert by_family["tl"]["n"] == 0


def test_paired_market_diff_matches_hand_brier():
    # One fixture, outcome=home. Compare the market family vs live directly.
    rows = [_full_row("Brazil vs Serbia", "2026-06-13T08:00:00")]
    results = [{"fixture": "Brazil vs Serbia", "kickoff_utc": "2026-06-13T18:00:00Z",
                "date": "2026-06-13", "outcome": "home", "score": "2-0"}]
    sb = shadowscore.build_scoreboard(rows, results, "2026-07-08T00:00:00")
    market_row = next(r for r in sb["shadows"] if r["family"] == "market")
    b_live = shadowscore.brier_1x2(_M, "home")
    b_mkt = shadowscore.brier_1x2(_MKT, "home")
    assert abs(market_row["brier_diff"] - (b_mkt - b_live)) < 1e-9
    assert abs(market_row["brier_shadow"] - b_mkt) < 1e-9
    assert abs(market_row["brier_live"] - b_live) < 1e-9


def test_group_knockout_split():
    rows = [
        _full_row("Brazil vs Serbia", "2026-06-13T08:00:00"),   # group
        _full_row("Spain vs Japan", "2026-06-30T08:00:00"),     # knockout
    ]
    results = [
        {"fixture": "Brazil vs Serbia", "kickoff_utc": "2026-06-13T18:00:00Z",
         "date": "2026-06-13", "outcome": "home", "score": "2-0"},
        {"fixture": "Spain vs Japan", "kickoff_utc": "2026-06-30T18:00:00Z",
         "date": "2026-06-30", "outcome": "away", "score": "0-1"},
    ]
    sb = shadowscore.build_scoreboard(rows, results, "2026-07-08T00:00:00")
    market_row = next(r for r in sb["shadows"] if r["family"] == "market")
    assert market_row["split"]["group"]["n"] == 1
    assert market_row["split"]["knockout"]["n"] == 1


def test_totals_shadow_scored_only_where_lambdas_present():
    # Live DC lambdas + gb lambdas logged on ONE fixture; scored on O/U 2.5.
    row = _full_row("Brazil vs Serbia", "2026-06-13T08:00:00")
    row["lambda_home"], row["lambda_away"] = 1.6, 1.1
    row["gb_lambda_home"], row["gb_lambda_away"] = 1.4, 1.0
    results = [{"fixture": "Brazil vs Serbia", "kickoff_utc": "2026-06-13T18:00:00Z",
                "date": "2026-06-13", "outcome": "home", "score": "3-1"}]  # 4 goals -> over
    sb = shadowscore.build_scoreboard([row], results, "2026-07-08T00:00:00")
    gb_row = next(r for r in sb["shadows"] if r["family"] == "gb")
    assert gb_row["n"] == 1
    # Hand-check the paired Brier diff for the over (hit=1).
    p_gb = shadowscore.prob_over_line(1.4, 1.0, 2.5)
    p_live = shadowscore.prob_over_line(1.6, 1.1, 2.5)
    want = shadowscore.brier_binary(p_gb, 1) - shadowscore.brier_binary(p_live, 1)
    assert abs(gb_row["brier_diff"] - want) < 1e-9
