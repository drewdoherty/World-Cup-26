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
