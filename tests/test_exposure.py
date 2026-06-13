"""Tests for the portfolio exposure & blind-spot engine (wca.exposure)."""
from __future__ import annotations

from wca import exposure


# A two-match slate with simple round probabilities.
MODEL_FIXTURES = [
    {"fixture": "Ateam vs Bteam", "kickoff": "2026-06-13 19:00:00+00:00",
     "model": {"home": 0.25, "draw": 0.25, "away": 0.50}},
    {"fixture": "Cteam vs Dteam", "kickoff": "2026-06-13 22:00:00+00:00",
     "model": {"home": 0.60, "draw": 0.25, "away": 0.15}},
]


def _bet(**kw):
    base = {"status": "open", "stake": 10.0, "decimal_odds": 2.0,
            "source": "model", "market": "Full-time result",
            "match_desc": "", "selection": ""}
    base.update(kw)
    return base


def test_build_slate_splits_home_away():
    slate = exposure.build_slate(MODEL_FIXTURES)
    assert slate["Ateam vs Bteam"]["home"] == "Ateam"
    assert slate["Ateam vs Bteam"]["away"] == "Bteam"
    assert slate["Ateam vs Bteam"]["p"]["Bteam"] == 0.50


def test_acca_legs_parses_various_formats():
    assert exposure._acca_legs("Acca 4-fold: Ateam+Bteam+Cteam") == ["Ateam", "Bteam", "Cteam"]
    assert exposure._acca_legs("Treble: Ateam + Bteam + Cteam") == ["Ateam", "Bteam", "Cteam"]
    # alias normalisation
    assert exposure._acca_legs("X: Türkiye+Brazil") == ["Turkey", "Brazil"]


def test_result_single_exposure_and_loss():
    # Back Bteam (away) @2.0 real money. Wins +10 on Bteam, loses -10 otherwise.
    bets = [_bet(match_desc="Ateam vs Bteam", selection="Bteam",
                 decimal_odds=2.0, stake=10.0)]
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES)
    fx = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rows = {r["outcome"]: r for r in fx["results"]}
    assert rows["Bteam"]["net_pnl"] == 10.0      # profit on win
    assert rows["Ateam"]["net_pnl"] == -10.0     # stake lost
    assert rows["Draw"]["net_pnl"] == -10.0


def test_free_bet_loss_is_zero():
    bets = [_bet(match_desc="Ateam vs Bteam", selection="Bteam",
                 decimal_odds=3.0, stake=10.0, source="offer")]
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES)
    fx = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rows = {r["outcome"]: r for r in fx["results"]}
    assert rows["Bteam"]["net_pnl"] == 20.0      # free-bet profit
    assert rows["Ateam"]["net_pnl"] == 0.0       # free bet: no stake lost


def test_acca_contributes_conditional_ev_only_where_leg_wins():
    # Free acca: Bteam (away, p .50) + Cteam (home, p .60), profit 20.
    bets = [_bet(market="ACCA", match_desc="Acca double: Bteam+Cteam",
                 decimal_odds=3.0, stake=10.0, source="offer")]
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES)
    fxA = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rowsA = {r["outcome"]: r for r in fxA["results"]}
    # On Bteam: acca live, EV = profit(20) * P(Cteam=.60) = 12.0
    assert abs(rowsA["Bteam"]["acca_ev"] - 12.0) < 1e-6
    # On Ateam/Draw: leg dead -> 0
    assert rowsA["Ateam"]["acca_ev"] == 0.0
    assert rowsA["Draw"]["acca_ev"] == 0.0


def test_blindspot_flagged_for_probable_uncovered_outcome():
    # Only back Ateam (home, p .25). The away outcome (p .50) is uncovered.
    bets = [_bet(match_desc="Ateam vs Bteam", selection="Ateam")]
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES)
    fx = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rows = {r["outcome"]: r for r in fx["results"]}
    assert rows["Bteam"]["blindspot"] is True    # p .50, net -10
    assert rows["Ateam"]["blindspot"] is False   # covered (win)


def test_portfolio_scenarios_ev_best_worst():
    bets = [_bet(match_desc="Ateam vs Bteam", selection="Bteam",
                 decimal_odds=2.0, stake=10.0)]
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES)
    p = data["portfolio"]
    # EV = .50*(+10) + .50*(-10) = 0
    assert abs(p["ev"]) < 1e-6
    assert p["best"] == 10.0
    assert p["worst"] == -10.0
    assert p["n_scenarios"] == 9   # 3 x 3


def test_plug_suggestion_uses_best_odds_and_ev():
    bets = [_bet(match_desc="Ateam vs Bteam", selection="Ateam")]
    odds_index = {"Ateam vs Bteam": {"Bteam": {"bookA": 2.5, "bookB": 1.9}}}
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES, odds_index=odds_index)
    fx = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rows = {r["outcome"]: r for r in fx["results"]}
    plug = rows["Bteam"]["plug"]
    assert plug["available"] is True
    assert plug["best_odds"] == 2.5             # picks the longer price
    assert plug["best_venue"] == "bookA"
    # model .50 * 2.5 - 1 = +25% EV -> recommend plugging
    assert plug["ev_pct"] == 25.0
    assert "PLUG" in plug["recommendation"]
