"""Tests for the live scores feed (theoddsapi.get_scores) and the positions
Data-API client (wca.pm.positions). Network is mocked.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from wca.data import theoddsapi
from wca.pm import positions


def _resp(json_data, headers=None):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = json_data
    m.headers = headers or {}
    m.raise_for_status = MagicMock()
    return m


class TestGetScores:
    def test_parses_live_scores(self, monkeypatch):
        monkeypatch.setenv("ODDS_API_KEY", "testkey")
        payload = [
            {
                "id": "evt1", "sport_key": "soccer_fifa_world_cup",
                "commence_time": "2026-06-13T18:00:00Z", "completed": False,
                "home_team": "Brazil", "away_team": "Morocco",
                "scores": [{"name": "Brazil", "score": "1"},
                           {"name": "Morocco", "score": "0"}],
                "last_update": "2026-06-13T18:25:00Z",
            },
            {
                "id": "evt2", "sport_key": "soccer_fifa_world_cup",
                "commence_time": "2026-06-13T21:00:00Z", "completed": False,
                "home_team": "Spain", "away_team": "Japan",
                "scores": None, "last_update": None,
            },
        ]
        monkeypatch.setattr(
            theoddsapi.requests, "get",
            lambda *a, **kw: _resp(payload, {"x-requests-remaining": "412"}),
        )
        events, quota = theoddsapi.get_scores("soccer_fifa_world_cup")
        assert len(events) == 2
        assert events[0]["scores"] == [
            {"name": "Brazil", "score": "1"}, {"name": "Morocco", "score": "0"}]
        assert events[0]["completed"] is False
        # Pre-kickoff match: scores normalised to [].
        assert events[1]["scores"] == []
        assert quota.remaining == 412

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        import pytest
        with pytest.raises(EnvironmentError):
            theoddsapi.get_scores("soccer_fifa_world_cup")


class TestFetchPositions:
    def test_parses_and_filters_open(self):
        payload = [
            {
                "asset": "TOK_A", "conditionId": "0xa", "size": 100,
                "avgPrice": 0.10, "curPrice": 0.08, "outcome": "Yes",
                "title": "Exact Score: A 0 - 0 B?", "slug": "s",
                "eventSlug": "fifwc-a-b", "endDate": "2026-06-13",
                "negativeRisk": True, "redeemable": False, "currentValue": 8.0,
            },
            {
                "asset": "TOK_B", "conditionId": "0xb", "size": 52,
                "avgPrice": 0.53, "curPrice": 0.0, "outcome": "Yes",
                "title": "Will Canada win on 2026-06-12?", "slug": "s2",
                "eventSlug": "fifwc-can", "endDate": "2026-06-12",
                "negativeRisk": True, "redeemable": True, "currentValue": 0.0,
            },
        ]
        session = MagicMock()
        session.get.return_value = _resp(payload)

        all_pos = positions.fetch_positions(session=session)
        assert len(all_pos) == 2
        assert all_pos[0].asset == "TOK_A"
        assert all_pos[0].neg_risk is True
        assert all_pos[0].is_open is True
        assert all_pos[1].is_open is False  # redeemable / resolved

        open_only = positions.fetch_positions(session=session, open_only=True)
        assert [p.asset for p in open_only] == ["TOK_A"]


class TestFetchTrades:
    def test_parses_trades(self):
        payload = [
            {"side": "SELL", "asset": "TOK_A", "size": "10", "price": "0.06",
             "transactionHash": "0xaaa", "timestamp": 100, "title": "X"},
            {"side": "BUY", "asset": "TOK_A", "size": "100", "price": "0.10",
             "transactionHash": "0xbbb", "timestamp": 90, "title": "X"},
        ]
        session = MagicMock()
        session.get.return_value = _resp(payload)
        trades = positions.fetch_trades("0xwallet", session=session)
        assert len(trades) == 2
        assert trades[0].side == "SELL" and trades[0].size == 10.0
        assert trades[0].price == 0.06 and trades[0].tx_hash == "0xaaa"


class TestReconcileSellFill:
    def test_sums_new_sell_fills(self):
        from wca.bot import app

        T = positions.Trade
        trades = [
            T("SELL", "TOK", 6.0, 0.06, "0xnew1", 101, "x"),
            T("SELL", "TOK", 4.0, 0.05, "0xnew2", 102, "x"),
            T("SELL", "TOK", 9.0, 0.07, "0xOLD", 99, "x"),   # already seen
            T("BUY", "TOK", 100.0, 0.10, "0xbuy", 50, "x"),  # wrong side
        ]
        filled, proceeds, hashes = app._reconcile_sell_fill(
            "0xwallet", "TOK", seen_hashes={"0xOLD"},
            fetch=lambda w: trades, retries=1,
        )
        assert filled == pytest.approx(10.0)            # 6 + 4, excludes old + buy
        assert proceeds == pytest.approx(6 * 0.06 + 4 * 0.05)

    def test_no_new_fills_returns_zero(self):
        from wca.bot import app
        filled, proceeds, hashes = app._reconcile_sell_fill(
            "0xwallet", "TOK", seen_hashes=set(), fetch=lambda w: [], retries=1)
        assert filled == 0.0 and proceeds == 0.0 and hashes == []

    def test_no_wallet_returns_none(self):
        from wca.bot import app
        assert app._reconcile_sell_fill(None, "TOK", set(), retries=1) is None
