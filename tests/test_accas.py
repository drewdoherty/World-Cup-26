"""Tests for the rebuilt /accas generator (model-driven, exposure-aware)."""
from __future__ import annotations

from wca import accas
from wca.accas import (
    Leg, Offer, candidate_legs, build_exposure, assemble_accas,
    build_promo_accas, format_accas, _is_finished, _kelly_fraction,
)


# --------------------------------------------------------------------------
# Synthetic fixtures
# --------------------------------------------------------------------------
def _fixtures():
    return [
        {  # Alpha heavy fav; home + draw +EV
            "fixture": "Alpha vs Bravo",
            "model_1x2": {"home": 0.50, "draw": 0.25, "away": 0.25},
            "best_1x2": {"home": (2.20, "bk"), "draw": (4.50, "bk"), "away": (3.00, "bk")},
            "over_under": {"line": 2.5, "over": 0.55, "under": 0.45},
            "btts": 0.40,
        },
        {  # Charlie: away +EV moneyline
            "fixture": "Charlie vs Delta",
            "model_1x2": {"home": 0.40, "draw": 0.25, "away": 0.35},
            "best_1x2": {"home": (2.10, "bk"), "draw": (4.00, "bk"), "away": (3.20, "bk")},
            "over_under": {"line": 2.5, "over": 0.50, "under": 0.50},
            "btts": 0.45,
        },
        {  # Echo: a +EV longshot only (away 10% @ 12.0)
            "fixture": "Echo vs Foxtrot",
            "model_1x2": {"home": 0.78, "draw": 0.12, "away": 0.10},
            "best_1x2": {"home": (1.20, "bk"), "draw": (9.50, "bk"), "away": (12.0, "bk")},
            "over_under": {"line": 2.5, "over": 0.46, "under": 0.54},
            "btts": 0.30,
        },
    ]


# --------------------------------------------------------------------------
# candidate_legs — +EV gate, markets we can price, no player props
# --------------------------------------------------------------------------
def test_candidate_legs_ev_gate():
    legs = candidate_legs(_fixtures(), {}, min_edge=0.02, include_events=False)
    # Alpha home 0.50*2.20-1 = +10% ; draw 0.25*4.50-1 = +12.5% ; away 0.25*3.0-1 = -25% (excluded)
    sels = {(l.fixture, l.selection) for l in legs}
    assert ("Alpha vs Bravo", "Alpha") in sels
    assert ("Alpha vs Bravo", "Draw") in sels
    assert ("Alpha vs Bravo", "Bravo") not in sels  # -EV dropped
    for l in legs:
        assert l.edge >= 0.02


def test_only_priceable_markets_no_player_props():
    legs = candidate_legs(_fixtures(), {}, min_edge=-1.0)  # accept everything
    allowed = {"1X2", "totals", "btts", "dnb"}
    assert all(l.market in allowed for l in legs)
    # Never a scorer/cards/corners leg.
    assert not any("scor" in l.selection.lower() or "card" in l.selection.lower()
                   or "corner" in l.selection.lower() for l in legs)


def test_event_legs_priced_from_snapshot():
    snap = {accas._fixture_token("Alpha vs Bravo"): {
        ("totals", "Over 2.5"): (1.90, "bk"),  # 0.55*1.90-1 = +4.5%
        ("btts", "BTTS No"): (1.80, "bk"),      # 0.60*1.80-1 = +8%
    }}
    legs = candidate_legs(_fixtures()[:1], snap, min_edge=0.02)
    markets = {(l.market, l.selection) for l in legs}
    assert ("totals", "Over 2.5") in markets
    assert ("btts", "BTTS No") in markets


# --------------------------------------------------------------------------
# assemble_accas — one-leg-per-match, moneyline-first, longshot policy
# --------------------------------------------------------------------------
def test_one_leg_per_match():
    legs = candidate_legs(_fixtures(), {}, min_edge=0.02)
    accas_out = assemble_accas(legs, mode="value", min_legs=2)
    for a in accas_out:
        fixtures = [l.fixture for l in a.legs]
        assert len(fixtures) == len(set(fixtures))  # no two legs same match


def test_moneyline_first_ordering():
    # Alpha has a moneyline +EV AND a derivative +EV; anchor must be the moneyline.
    snap = {accas._fixture_token("Alpha vs Bravo"): {("totals", "Over 2.5"): (1.99, "bk")}}
    legs = candidate_legs(_fixtures(), snap, min_edge=0.02)
    a = assemble_accas(legs, mode="value", min_legs=2)
    assert a, "expected at least one acca"
    alpha_leg = next(l for l in a[0].legs if l.fixture == "Alpha vs Bravo")
    assert alpha_leg.is_moneyline


def test_longshot_skipped_in_value_used_in_longshot():
    legs = candidate_legs(_fixtures(), {}, min_edge=0.02)
    # Echo away @12.0 (10%) is the only Echo +EV leg and it is a longshot.
    value_legs = [l for l in legs if l.fixture == "Echo vs Foxtrot"]
    assert value_legs and all(l.is_longshot for l in value_legs)
    val = assemble_accas(legs, mode="value", min_legs=3)
    assert all("Echo" not in l.fixture for a in val for l in a.legs)
    lng = assemble_accas(legs, mode="longshot", min_legs=3)
    assert any("Echo" in l.fixture for a in lng for l in a.legs)


def test_quarter_kelly_stake():
    legs = candidate_legs(_fixtures(), {}, min_edge=0.02)
    a = assemble_accas(legs, mode="value", min_legs=2, bankroll=2500.0,
                       kelly_fraction=0.25)[0]
    o, p = a.combined_odds, a.model_prob
    expected = round(0.25 * _kelly_fraction(p, o) * 2500.0, 2)
    assert abs(a.stake - expected) < 0.01


# --------------------------------------------------------------------------
# Exposure — dedup + concentration
# --------------------------------------------------------------------------
def test_exposure_dedup_drops_held_leg():
    legs = candidate_legs(_fixtures(), {}, min_edge=0.02)
    held = [{"match": "Alpha vs Bravo", "selection": "Alpha", "market": "1X2"}]
    exp = build_exposure(held)
    out = assemble_accas(legs, exp, mode="value", min_legs=2)
    for a in out:
        assert not any(l.fixture == "Alpha vs Bravo" and l.selection == "Alpha"
                       for l in a.legs)


def test_exposure_note_flags_concentration():
    legs = candidate_legs(_fixtures(), {}, min_edge=0.02)
    held = [{"match": "Charlie vs Delta", "selection": "something", "market": "1X2"}]
    exp = build_exposure(held)
    out = assemble_accas(legs, exp, mode="value", min_legs=2)
    notes = " ".join(a.note for a in out)
    assert "Charlie vs Delta" in notes or "diversifies" in notes


# --------------------------------------------------------------------------
# FT filter
# --------------------------------------------------------------------------
def test_is_finished():
    fin = [("alpha", "bravo")]
    assert _is_finished("Alpha vs Bravo", fin)
    assert not _is_finished("Charlie vs Delta", fin)


# --------------------------------------------------------------------------
# Promo mode
# --------------------------------------------------------------------------
def test_promo_qualifier_min_legs_and_combined_floor():
    legs = candidate_legs(_fixtures(), {}, min_edge=-1.0)  # accept all for pool
    off = Offer("X", "betfred", "1", 3, 0.0, 4.0, "qualifier", 10.0)
    out = build_promo_accas(legs, [off])
    assert out, "qualifier should produce an acca"
    a = out[0]
    assert len(a.legs) >= 3
    assert a.combined_odds >= 4.0
    assert "qualifier" in a.note


def test_promo_game_restriction():
    legs = candidate_legs(_fixtures(), {}, min_edge=-1.0)
    off = Offer("EngGha", "paddypower", "1", 3, 2.0, 0.0, "lose_free", 50.0, "alpha bravo")
    out = build_promo_accas(legs, [off])
    # Only Alpha vs Bravo legs allowed; that's one match -> cannot make a 3-leg
    # cross-match acca, so it should be skipped (or same-game if >=3 markets).
    for a in out:
        assert all("alpha" in accas._fixture_token(l.fixture)
                   and "bravo" in accas._fixture_token(l.fixture) for l in a.legs)


def test_promo_snr_min_leg_odds_enforced():
    legs = candidate_legs(_fixtures(), {}, min_edge=-1.0)
    off = Offer("BfSB", "betfair_sportsbook", "1", 3, 1.5, 0.0, "snr_free", 10.0)
    out = build_promo_accas(legs, [off])
    for a in out:
        assert all(l.odds >= 1.5 for l in a.legs)
        assert "SNR" in a.note or "retain" in a.note.lower()


def test_promo_lose_free_effective_risk_note():
    legs = candidate_legs(_fixtures(), {}, min_edge=-1.0)
    off = Offer("MB", "paddypower", "1", 3, 1.5, 0.0, "lose_free", 50.0)
    out = build_promo_accas(legs, [off])
    assert out
    assert "effective risk" in out[0].note


# --------------------------------------------------------------------------
# Formatting + empty state
# --------------------------------------------------------------------------
def test_format_empty_state():
    # value/low-win mode steers toward the higher-edge alternative
    txt = format_accas({"mode": "value", "accas": []})
    assert "NO BET" in txt
    assert "+EV favourite legs" in txt
    # edge/other modes explain the +EV gate
    edge_txt = format_accas({"mode": "edge", "accas": []})
    assert "NO BET" in edge_txt
    assert "no qualifying accas cleared the +EV gate" in edge_txt


def test_format_non_empty():
    legs = candidate_legs(_fixtures(), {}, min_edge=0.02)
    out = assemble_accas(legs, mode="value", min_legs=2)
    txt = format_accas({"mode": "value", "accas": out})
    assert "Acca" in txt
    assert "¼-Kelly" in txt
    assert "Alpha vs Bravo" in txt or "Charlie vs Delta" in txt


def test_format_promo_shows_offer_note():
    legs = candidate_legs(_fixtures(), {}, min_edge=-1.0)
    out = build_promo_accas(legs, [Offer("BfSB", "betfair_sportsbook", "1", 3, 1.5, 0.0, "snr_free", 10.0)])
    txt = format_accas({"mode": "promo", "accas": out})
    assert "betfair_sportsbook" in txt


# --------------------------------------------------------------------------
# LOW-LEVEL-WIN (default "value") mode — favourites by model prob, sane odds
# --------------------------------------------------------------------------
def _lowwin_fixtures():
    """Three fixtures where the favourite is genuinely +EV (sane short prices)
    AND a high-edge underdog/draw is also +EV (the trap the old builder fell
    into). Low-win must pick the favourites, not the longshots."""
    return [
        {  # Foxes strong fav, +EV @1.80; draw is a high-edge longshot @9.5
            "fixture": "Foxes vs Hounds",
            "model_1x2": {"home": 0.60, "draw": 0.22, "away": 0.18},
            "best_1x2": {"home": (1.80, "bk"), "draw": (9.50, "bk"), "away": (6.50, "bk")},
        },
        {  # Lions fav, +EV @1.70; away high-edge longshot @11.0
            "fixture": "Lions vs Mice",
            "model_1x2": {"home": 0.65, "draw": 0.21, "away": 0.14},
            "best_1x2": {"home": (1.70, "bk"), "draw": (5.00, "bk"), "away": (11.0, "bk")},
        },
        {  # Owls fav, +EV @1.90; draw +EV high-edge @8.5
            "fixture": "Owls vs Newts",
            "model_1x2": {"home": 0.58, "draw": 0.27, "away": 0.15},
            "best_1x2": {"home": (1.90, "bk"), "draw": (8.50, "bk"), "away": (7.00, "bk")},
        },
    ]


def test_lowwin_ranks_by_model_prob_not_edge():
    # Foxes home: 0.60*1.80-1 = +8% edge.  Foxes draw: 0.22*9.5-1 = +109% edge.
    # Edge-max would take the draw; low-win must take the favourite (home).
    legs = candidate_legs(_lowwin_fixtures(), {}, min_edge=0.0, include_events=False)
    out = assemble_accas(legs, mode="value", min_legs=2)
    assert out, "low-win should build an acca"
    sels = {(l.fixture, l.selection) for a in out for l in a.legs}
    foxes = next(l for a in out for l in a.legs if l.fixture == "Foxes vs Hounds")
    assert foxes.selection == "Foxes"        # the favourite, not the +109% draw
    assert ("Foxes vs Hounds", "Draw") not in sels


def test_lowwin_favours_favourites_every_leg_is_the_short_price():
    legs = candidate_legs(_lowwin_fixtures(), {}, min_edge=0.0, include_events=False)
    out = assemble_accas(legs, mode="value", min_legs=2)
    for a in out:
        for l in a.legs:
            assert l.model_prob >= 0.50      # only genuine favourites get in
            assert l.odds <= 2.5             # short, sane per-leg prices


def test_lowwin_modest_combined_odds_never_a_lottery():
    legs = candidate_legs(_lowwin_fixtures(), {}, min_edge=0.0, include_events=False)
    out = assemble_accas(legs, mode="value", min_legs=2)
    assert out
    for a in out:
        assert a.combined_odds <= accas.VALUE_MAX_COMBINED
        assert a.combined_odds <= 8.0        # 3 favs ~1.7-1.9 each -> ~5.5x
        assert a.model_prob >= 0.10          # decent chance of actually landing


def test_lowwin_no_longshot_draw_stack():
    # The pathological case: every fixture has a fat-edge draw/underdog. Low-win
    # must NOT assemble them; the old edge-max ("edge" mode) does.
    legs = candidate_legs(_lowwin_fixtures(), {}, min_edge=0.0, include_events=False)
    low = assemble_accas(legs, mode="value", min_legs=2)
    assert not any(l.selection == "Draw" or l.odds >= 5.0
                   for a in low for l in a.legs)
    edge = assemble_accas(legs, mode="edge", min_legs=2)
    # legacy edge-max happily stacks the high-edge longshots
    assert any(l.odds >= 5.0 for a in edge for l in a.legs)


def test_lowwin_legs_still_positive_ev():
    legs = candidate_legs(_lowwin_fixtures(), {}, min_edge=0.0, include_events=False)
    out = assemble_accas(legs, mode="value", min_legs=2)
    assert out
    for a in out:
        assert a.edge > 0
        for l in a.legs:
            assert l.edge > 0


def test_lowwin_combined_odds_ceiling_blocks_big_accas():
    legs = candidate_legs(_lowwin_fixtures(), {}, min_edge=0.0, include_events=False)
    # A punishing ceiling forces only the shortest (2-leg) combos through.
    out = assemble_accas(legs, mode="value", min_legs=2, max_combined_odds=3.5)
    for a in out:
        assert a.combined_odds <= 3.5


def test_build_accas_low_win_is_the_default(tmp_path):
    # /accas with no mode -> "value" -> low-win; build_accas accepts it end-to-end.
    res = accas.build_accas(
        preds_path=str(tmp_path / "missing.json"),
        scores_path=str(tmp_path / "missing.json"),
        db_path=str(tmp_path / "missing.db"),
        site_data=str(tmp_path / "missing.json"),
        mode="value",
    )
    assert res["mode"] == "value"
    assert isinstance(res["accas"], list)
