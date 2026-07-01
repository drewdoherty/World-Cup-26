"""Tests for the test-book paper-trader (wca.testbook.trader)."""

from __future__ import annotations

from wca.testbook import store, trader


def _mkt(git, ask, bid, toks, q="", outs=("Yes", "No"), prices=None):
    return {"groupItemTitle": git, "question": q, "outcomes": list(outs),
            "outcomePrices": prices or [str(ask), str(round(1 - ask, 3))],
            "clobTokenIds": list(toks), "bestBid": bid, "bestAsk": ask, "volumeNum": 5000}


def _model():
    scores = {"fixtures": [{
        "fixture": "France vs Sweden",
        "model_1x2": {"home": 0.80, "draw": 0.13, "away": 0.07},
        "over_under": {"line": 2.5, "over": 55.0, "under": 45.0},
        "btts": 40.0,
        "scores": [{"score": "2-0", "prob": 15.0}],
    }]}
    advancement = {"teams": [{"team": "France", "model": {"R16": 0.95, "QF": 0.70}}]}
    return trader.load_model(scores, advancement)


def _events():
    return [
        {"title": "France vs. Sweden", "markets": [
            _mkt("France", 0.76, 0.74, ["tF", "tFn"], "Will France win on 2026-06-30?"),
            _mkt("Draw", 0.16, 0.14, ["tD", "tDn"], "Will France vs. Sweden end in a draw?"),
            _mkt("Sweden", 0.08, 0.06, ["tS", "tSn"], "Will Sweden win on 2026-06-30?"),
        ]},
        {"title": "World Cup: Nation To Reach Round of 16", "markets": [
            _mkt("France", 0.90, 0.89, ["tR", "tRn"], "Will France reach the Round of 16?"),
        ]},
        {"title": "France vs. Sweden - More Markets", "markets": [
            _mkt("O/U 2.5", 0.50, 0.49, ["tO", "tU"],
                 outs=("Over", "Under"), prices=["0.50", "0.50"]),           # MATCH total (should match)
            _mkt("France O/U 2.5", 0.06, 0.05, ["tFT", "tFTn"],
                 outs=("Over", "Under"), prices=["0.06", "0.94"]),           # TEAM total (must NOT match)
            _mkt("1st Half O/U 2.5", 0.20, 0.19, ["tHT", "tHTn"],
                 outs=("Over", "Under"), prices=["0.20", "0.80"]),           # half total (must NOT match)
            _mkt("Both Teams to Score", 0.38, 0.37, ["tB", "tBn"]),
            _mkt("Spread: France (-1.5)", 0.53, 0.47, ["tHf", "tHs"],
                 outs=("France", "Sweden"), prices=["0.53", "0.47"]),
        ]},
        {"title": "France vs. Sweden - Exact Score", "markets": [
            _mkt("France 2 - 0 Sweden", 0.12, 0.11, ["tE", "tEn"]),
        ]},
    ]


def test_kelly_fraction():
    assert round(trader.kelly_fraction(0.80, 0.76), 4) == round((0.80 - 0.76) / 0.24, 4)
    assert trader.kelly_fraction(0.5, 0.6) == 0.0           # no edge -> 0
    assert trader.kelly_fraction(0.5, 0.0) == 0.0


def test_build_candidates_covers_all_families_with_bases():
    cands = trader.build_candidates(_model(), _events())
    by = {(c.market_type, c.resolution_basis): c for c in cands}
    assert ("match_result", "FT") in {(c.market_type, c.resolution_basis) for c in cands}
    assert ("advance", "advance") in {(c.market_type, c.resolution_basis) for c in cands}
    assert ("totals_ou25", "totals") in {(c.market_type, c.resolution_basis) for c in cands}
    assert ("btts", "btts") in {(c.market_type, c.resolution_basis) for c in cands}
    assert ("exact_score", "exact") in {(c.market_type, c.resolution_basis) for c in cands}
    assert ("handicap_15", "handicap") in {(c.market_type, c.resolution_basis) for c in cands}
    # totals priced from the [Over,Under] outcomes (not Yes/No): Over @0.50 vs model 0.55
    over = next(c for c in cands if c.market_type == "totals_ou25" and "Over" in c.selection)
    assert abs(over.price - 0.50) < 1e-9 and abs(over.model_prob - 0.55) < 1e-9
    # Only the MATCH total ("O/U 2.5") is priced — exactly 2 totals candidates
    # (Over + Under). Team totals ("France O/U 2.5") and half-totals are excluded.
    totals = [c for c in cands if c.market_type == "totals_ou25"]
    assert len(totals) == 2
    assert all(abs(c.price - 0.50) < 1e-9 for c in totals)   # never the 0.06 team-total price
    # FT France: model 0.80 vs ask 0.76 -> edge 0.04
    ft = next(c for c in cands if c.market_type == "match_result" and "France" in c.selection)
    assert abs(ft.edge - 0.04) < 1e-6 and ft.resolution_basis == "FT"
    # advance France R16: 0.95 vs 0.90 -> 0.05
    adv = next(c for c in cands if c.market_type == "advance")
    assert abs(adv.edge - 0.05) < 1e-6 and adv.resolution_basis == "advance"


def test_ft_and_advance_are_distinct_candidates():
    cands = trader.build_candidates(_model(), _events())
    ft = [c for c in cands if c.resolution_basis == "FT" and "France" in c.selection]
    adv = [c for c in cands if c.resolution_basis == "advance"]
    assert ft and adv
    # different markets, different prices/tokens — never conflated
    assert ft[0].token_id != adv[0].token_id
    assert ft[0].price != adv[0].price


def test_player_props_event_routes_to_prop_pricer(monkeypatch):
    from wca.testbook import trader as T
    sentinel = T.Candidate(
        fixture="France vs Sweden", market_type="player_goals", selection="Mbappe 1+ goals",
        resolution_basis="prop", token_id="tP", price=0.30, model_prob=0.42, edge=0.12,
        volume=0.0, spread=None)
    calls = {}

    def _fake(fx, ev):
        calls["fx"] = fx
        return [sentinel]

    monkeypatch.setattr(T, "_price_player_props", _fake)
    events = [{"title": "France vs. Sweden - Player Props", "markets": []}]
    cands = T.build_candidates(_model(), events)
    assert calls and calls["fx"]["raw"] == "France vs Sweden"
    assert any(c.resolution_basis == "prop" and c.market_type == "player_goals" for c in cands)


def test_run_paper_pass_places_and_dedupes():
    con = store.connect(":memory:")
    store.seed_bankroll(con, 2000.0, ts_utc="t0")
    model, events = _model(), _events()
    r1 = trader.run_paper_pass(con, model, events, ts_utc="t1",
                               edge_threshold=0.04, kelly_mult=0.5, max_stake_frac=0.02)
    # edges >= 0.04: FT France(0.04), advance(0.05), totals over(0.05) -> 3 placed
    assert r1["n_placed"] == 3
    bases = {b["basis"] for b in r1["placed"]}
    assert bases == {"FT", "advance", "totals"}
    # each stake capped at 2% of 2000 = $40
    assert all(b["stake"] <= 40.0 + 1e-9 for b in r1["placed"])
    # second pass: same tokens already held -> nothing new
    r2 = trader.run_paper_pass(con, model, events, ts_utc="t2", edge_threshold=0.04)
    assert r2["n_placed"] == 0
