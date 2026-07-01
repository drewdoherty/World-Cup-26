"""Dead/resolved markets must not be entered (skip already-played fixtures)."""
from __future__ import annotations
from wca.testbook import trader


def _mkt(**kw):
    m = {"clobTokenIds": '["a","b"]', "outcomes": '["Yes","No"]',
         "outcomePrices": '["0.40","0.60"]', "bestBid": 0.39, "bestAsk": 0.41,
         "volumeNum": 5000}
    m.update(kw)
    return m


def test_market_tradeable_flags():
    assert trader._market_tradeable(_mkt()) is True
    assert trader._market_tradeable(_mkt(closed=True)) is False
    assert trader._market_tradeable(_mkt(active=False)) is False
    assert trader._market_tradeable(_mkt(acceptingOrders=False)) is False


def test_yes_quote_skips_dead():
    assert trader.yes_quote(_mkt()) is not None
    assert trader.yes_quote(_mkt(closed=True)) is None
    assert trader.yes_quote(_mkt(acceptingOrders=False)) is None


def test_outcome_quote_skips_dead():
    ou = dict(outcomes='["Over","Under"]', outcomePrices='["0.50","0.50"]')
    assert trader.outcome_quote(_mkt(**ou), "Over") is not None
    assert trader.outcome_quote(_mkt(closed=True, **ou), "Over") is None


def test_build_candidates_skips_kicked_off_fixture():
    from datetime import datetime, timezone
    scores = {"fixtures": [{"fixture": "France vs Sweden",
        "model_1x2": {"home": 0.80, "draw": 0.13, "away": 0.07},
        "over_under": {"line": 2.5, "over": 55, "under": 45}, "btts": 40,
        "scores": [{"score": "2-0", "prob": 15}], "kickoff": "2026-06-30T17:00:00+00:00"}]}
    model = trader.load_model(scores, {"teams": []})
    ev = {"title": "France vs. Sweden", "markets": [
        {"groupItemTitle": "France", "question": "Will France win?", "outcomes": '["Yes","No"]',
         "outcomePrices": '["0.50","0.50"]', "clobTokenIds": '["t1","t2"]',
         "bestBid": 0.49, "bestAsk": 0.50, "volumeNum": 5000, "acceptingOrders": True}]}
    after_ko = datetime(2026, 7, 1, tzinfo=timezone.utc)
    before_ko = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    assert trader.build_candidates(model, [ev], now=after_ko) == []     # kicked off -> skip
    assert len(trader.build_candidates(model, [ev], now=before_ko)) >= 1  # pre-KO -> priced
