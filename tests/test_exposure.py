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


# ---------------------------------------------------------------------------
# Hardening: the floor must count EVERY real-money position (the Qatar 1-1
# regression — a worst case that excluded exact-score/prop stakes).
# ---------------------------------------------------------------------------
def test_real_money_event_bet_lowers_the_floor():
    """A real-money exact-score punt must drag the worst case DOWN by its stake.

    Before the fix the engine never saw event/scoreline/prop bets, so the
    headline floor under-stated the true downside."""
    base = exposure.build_exposure_data(
        [_bet(match_desc="Ateam vs Bteam", selection="Bteam", stake=10.0)],
        MODEL_FIXTURES)
    with_punt = exposure.build_exposure_data(
        [_bet(match_desc="Ateam vs Bteam", selection="Bteam", stake=10.0),
         _bet(match_desc="Ateam vs Bteam", market="Exact Score",
              selection="Ateam 1-0 Bteam", stake=4.0, decimal_odds=6.0,
              source="punt")],
        MODEL_FIXTURES)
    # £4 real-money punt assumed to miss in the floor -> worst is £4 lower.
    assert abs(with_punt["portfolio"]["worst"]
               - (base["portfolio"]["worst"] - 4.0)) < 1e-6
    assert with_punt["portfolio"]["event_stake_at_risk"] == 4.0
    # stake-at-risk now includes the punt (£10 single + £4 punt).
    assert with_punt["portfolio"]["stake_at_risk"] == 14.0


def test_free_bet_never_lowers_the_floor():
    """Free / promo stakes cost £0 on loss, so they can only raise the floor."""
    base = exposure.build_exposure_data(
        [_bet(match_desc="Ateam vs Bteam", selection="Bteam", stake=10.0)],
        MODEL_FIXTURES)
    with_free = exposure.build_exposure_data(
        [_bet(match_desc="Ateam vs Bteam", selection="Bteam", stake=10.0),
         _bet(match_desc="Ateam vs Bteam", market="Exact Score",
              selection="Ateam 2-0 Bteam", stake=5.0, decimal_odds=8.0,
              source="offer")],
        MODEL_FIXTURES)
    assert with_free["portfolio"]["worst"] >= base["portfolio"]["worst"] - 1e-9
    assert with_free["portfolio"]["event_stake_at_risk"] == 0.0


def test_nonfree_acca_subtracts_stake_on_loss():
    """A real-money acca that misses must cost its stake (latent bug fix)."""
    acca = _bet(market="ACCA", match_desc="Acca double: Bteam+Cteam",
                decimal_odds=3.0, stake=10.0, source="model")  # real money
    data = exposure.build_exposure_data([acca], MODEL_FIXTURES)
    p = data["portfolio"]
    # Worst state: acca misses -> -£10. Best: both legs land -> +£20.
    assert p["worst"] == -10.0
    assert p["best"] == 20.0


def test_free_acca_floor_is_zero_not_negative():
    free = _bet(market="ACCA", match_desc="Acca double: Bteam+Cteam",
                decimal_odds=3.0, stake=10.0, source="offer")
    p = exposure.build_exposure_data([free], MODEL_FIXTURES)["portfolio"]
    assert p["worst"] == 0.0          # free acca: loss costs nothing
    assert p["best"] == 20.0


def test_blindspot_uses_hard_cash_not_soft_acca_ev():
    """An outcome 'covered' only by free-acca EV is still a blind spot.

    Back Ateam (home) real money; a free acca rides Bteam. The Bteam outcome
    has positive acca EV but zero hard cash -> must still flag as a blind spot,
    marked soft_only."""
    bets = [
        _bet(match_desc="Ateam vs Bteam", selection="Ateam", stake=10.0),
        _bet(market="ACCA", match_desc="Acca double: Bteam+Cteam",
             decimal_odds=4.0, stake=10.0, source="offer"),
    ]
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES)
    fx = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rows = {r["outcome"]: r for r in fx["results"]}
    assert rows["Bteam"]["acca_ev"] > 0          # soft cover exists
    assert rows["Bteam"]["cash_net"] <= 0        # but no hard cash
    assert rows["Bteam"]["blindspot"] is True    # still a blind spot
    assert rows["Bteam"]["soft_only"] is True


def test_conditional_floor_drops_dead_acca_and_pins_result():
    """Given a settled fixture, the floor is recomputed: a settled leg that
    lost kills its acca, and the settled result is pinned."""
    bets = [
        # real-money single backing the away side of the *unsettled* game
        _bet(match_desc="Cteam vs Dteam", selection="Dteam", stake=10.0,
             decimal_odds=2.0),
        # free acca needing Ateam (home of the settled game) — will die
        _bet(market="ACCA", match_desc="Acca double: Ateam+Dteam",
             decimal_odds=5.0, stake=10.0, source="offer"),
    ]
    # Ateam vs Bteam finished 0-2 -> away win (Bteam); the acca's Ateam leg lost.
    results = {"Ateam vs Bteam": {"outcome": "away", "score": "0-2"}}
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES, results=results)
    p = data["portfolio"]
    assert p["conditional"] is True
    assert p["dead_accas"] == 1
    assert p["alive_accas"] == 0
    # only the Cteam-vs-Dteam game still varies -> 3 states (not 9).
    assert p["n_scenarios"] == 3


def test_settled_real_money_exact_score_is_banked_not_at_risk():
    """A punt on a finished game is realised (banked), not counted at-risk."""
    bets = [_bet(match_desc="Ateam vs Bteam", market="Exact Score",
                 selection="Ateam 1-0 Bteam", stake=4.0, decimal_odds=6.0,
                 source="punt")]
    results = {"Ateam vs Bteam": {"outcome": "away", "score": "0-2"}}  # punt lost
    p = exposure.build_exposure_data(bets, MODEL_FIXTURES, results=results)["portfolio"]
    assert p["event_stake_at_risk"] == 0.0       # nothing live left
    assert p["banked"] == -4.0                   # realised loss


def test_single_v_match_desc_still_counts_in_floor():
    """REGRESSION (adversarial verify): a real single described with a single
    ' v ' instead of ' vs ' must still map — else its stake leaks out of BOTH
    the worst case and stake-at-risk, understating real downside."""
    vs = exposure.build_exposure_data(
        [_bet(match_desc="Ateam vs Bteam", selection="Bteam", stake=10.0)],
        MODEL_FIXTURES)["portfolio"]
    v = exposure.build_exposure_data(
        [_bet(match_desc="Ateam v Bteam", selection="Bteam", stake=10.0)],
        MODEL_FIXTURES)["portfolio"]
    assert v["worst"] == vs["worst"] == -10.0
    assert v["stake_at_risk"] == vs["stake_at_risk"] == 10.0


def test_model_fixture_with_single_v_builds_slate():
    fixtures = [{"fixture": "Ateam v Bteam",
                 "model": {"home": 0.5, "draw": 0.0, "away": 0.5}}]
    p = exposure.build_exposure_data(
        [_bet(match_desc="Ateam vs Bteam", selection="Bteam", stake=10.0)],
        fixtures)["portfolio"]
    assert p["worst"] == -10.0          # slate built, single mapped & counted


def test_unmapped_real_single_is_surfaced_not_dropped():
    """A real single we genuinely cannot map is surfaced as off-slate exposure,
    never silently dropped from the risk accounting."""
    data = exposure.build_exposure_data(
        [_bet(match_desc="Zteam vs Yteam", selection="Zteam", stake=12.0)],
        MODEL_FIXTURES)
    assert data["unmapped"]                       # recorded
    assert data["real_money_offslate"] == 12.0    # and its cash is surfaced


def test_exact_score_parser_ignores_leading_reference_numbers():
    """REGRESSION: the score parser must read the trailing 'h-a', not the first
    digit-sep-digit it sees (a ':' once let 'Bet #2: 1-0' misparse as 2-1)."""
    lost = exposure.build_exposure_data(
        [_bet(match_desc="Ateam vs Bteam", market="Exact Score",
              selection="Bet #2: Ateam 1-0 Bteam", stake=5.0, decimal_odds=8.0,
              source="punt")],
        MODEL_FIXTURES,
        results={"Ateam vs Bteam": {"outcome": "away", "score": "2-1"}})["portfolio"]
    assert lost["banked"] == -5.0     # 1-0 punt did NOT win on a 2-1 result
    won = exposure.build_exposure_data(
        [_bet(match_desc="Ateam vs Bteam", market="Exact Score",
              selection="Bet #2: Ateam 1-0 Bteam", stake=5.0, decimal_odds=8.0,
              source="punt")],
        MODEL_FIXTURES,
        results={"Ateam vs Bteam": {"outcome": "home", "score": "1-0"}})["portfolio"]
    assert won["banked"] == 35.0      # correctly settles as a win on 1-0


def test_full_time_result_variants_map_to_result_single():
    """'Full Time Result' / 'Türkiye' spellings must classify as a result bet,
    not leak into the event bucket (which would falsely open a blind spot)."""
    bets = [_bet(market="Full Time Result", match_desc="Ateam vs Bteam",
                 selection="Bteam", source="offer", stake=10.0, decimal_odds=3.0)]
    data = exposure.build_exposure_data(bets, MODEL_FIXTURES)
    assert data["unmapped"] == []
    fx = next(f for f in data["fixtures"] if f["fixture"] == "Ateam vs Bteam")
    rows = {r["outcome"]: r for r in fx["results"]}
    # free result single -> Bteam not a blind spot (free win covers it)
    assert rows["Bteam"]["net_pnl"] == 20.0
