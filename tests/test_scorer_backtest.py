"""Tests for wca.models.scorer_backtest (#8) on synthetic StatsBomb events."""
import math

from wca.models.scorer_backtest import (
    BacktestResult,
    brier_one,
    evaluate,
    log_loss_one,
    match_scorer_observations,
    run_backtest,
    team_npxg_shares,
)


def _xi(team, players):
    return {"type": {"name": "Starting XI"}, "team": {"name": team}, "minute": 0,
            "tactics": {"lineup": [{"player": {"name": p}} for p in players]}}


def _shot(team, player, xg, goal=False):
    shot = {"statsbomb_xg": xg}
    if goal:
        shot["outcome"] = {"name": "Goal"}
    return {"type": {"name": "Shot"}, "team": {"name": team},
            "player": {"name": player}, "shot": shot, "minute": 50}


def _end(team, minute=93):
    return {"type": {"name": "Half End"}, "team": {"name": team}, "minute": minute}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_metrics_basic():
    assert math.isclose(brier_one(0.25, 0), 0.0625)
    assert log_loss_one(1.0, 1) < 1e-6
    assert log_loss_one(0.0, 1) > 10  # clamped, large but finite


# ---------------------------------------------------------------------------
# Shares + observations
# ---------------------------------------------------------------------------

def test_team_npxg_shares_sum_to_one_per_team():
    events = [_xi("A", ["x", "y"]), _xi("B", ["z"]),
              _shot("A", "x", 0.6, goal=True), _shot("A", "y", 0.4),
              _shot("B", "z", 0.5)]
    shares = team_npxg_shares({1: events})
    assert math.isclose(sum(shares["A"].values()), 1.0, rel_tol=1e-9)
    assert math.isclose(shares["A"]["x"], 0.6)


def test_match_observations_scored_flag():
    events = [_xi("A", ["x", "y"]), _xi("B", ["z"]),
              _shot("A", "x", 0.6, goal=True), _shot("A", "y", 0.4),
              _end("A")]
    obs = match_scorer_observations(events)
    by = {o.player: o for o in obs}
    assert by["x"].scored is True
    assert by["y"].scored is False
    assert by["x"].minutes > 0


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def test_player_aware_beats_baseline_when_shares_informative():
    # Train: striker owns the team's npxg; squad-mate owns none.
    train = [_xi("A", ["Striker", "Mid"]), _xi("B", ["Def"]),
             _shot("A", "Striker", 0.9, goal=True), _shot("A", "Mid", 0.0),
             _shot("B", "Def", 0.3)]
    # Test: the striker scores, the midfielder doesn't (as the shares predict).
    test = [_xi("A", ["Striker", "Mid"]), _xi("B", ["Def"]),
            _shot("A", "Striker", 0.8, goal=True), _end("A")]
    res = run_backtest({1: train}, {2: test}, lambda_team=1.5)
    assert isinstance(res, BacktestResult)
    assert res.n_covered >= 2
    # Player-aware concentrates probability on the striker who actually scored,
    # so it should beat the equal-share baseline on both metrics here.
    assert res.brier_improvement > 0
    assert res.log_loss_improvement > 0
    assert res.recommend_adopt is True


def test_uncovered_players_are_skipped_not_scored():
    train = [_xi("A", ["Striker"]), _xi("B", ["Def"]),
             _shot("A", "Striker", 0.5, goal=True), _shot("B", "Def", 0.2)]
    # Test introduces a brand-new player with no 2018 share -> uncovered.
    test = [_xi("A", ["Striker", "NewGuy"]), _xi("B", ["Def"]),
            _shot("A", "Striker", 0.4, goal=True), _end("A")]
    res = run_backtest({1: train}, {2: test}, lambda_team=1.4)
    assert res.n_uncovered >= 1   # NewGuy
    assert 0.0 < res.coverage < 1.0


def test_reliability_bins_and_scale():
    from wca.models.scorer_backtest import (match_scorer_observations,
                                            reliability, team_npxg_shares)
    train = [_xi("A", ["Striker", "Mid"]), _xi("B", ["Def"]),
             _shot("A", "Striker", 0.9, goal=True), _shot("A", "Mid", 0.1),
             _shot("B", "Def", 0.3)]
    test = [_xi("A", ["Striker", "Mid"]), _xi("B", ["Def"]),
            _shot("A", "Striker", 0.6, goal=True), _end("A")]
    shares = team_npxg_shares({1: train})
    obs = {2: match_scorer_observations(test)}
    bins, scale = reliability(obs, shares, lambda_team=1.5, n_bins=5)
    assert len(bins) == 5
    assert sum(b.n for b in bins) >= 2
    assert scale > 0  # observed/predicted ratio


def test_recommend_requires_both_metrics():
    r = BacktestResult(n_covered=10, n_uncovered=0, n_matches=1, lambda_team=1.3,
                       pa_brier=0.20, pa_log_loss=0.60,
                       base_brier=0.21, base_log_loss=0.59)
    # Brier better but log-loss worse -> do not adopt.
    assert r.brier_improvement > 0
    assert r.log_loss_improvement < 0
    assert r.recommend_adopt is False
