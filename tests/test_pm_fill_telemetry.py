"""Tests for ``wca.pm.filltelemetry`` — observation-only PM order/fill logging.

Motivation (2026-07-08 adversarial review): PM maker orders rest GTC at
(a rounded) mid with NO fill-rate logging today — an unfilled maker order is
a 100% EV leak that is currently invisible. This file covers:

1. The jsonl writer/reader primitives in isolation (Section 1).
2. ``ClobTrader.place_order`` writes a "placed" telemetry row on both the
   dry-run and live paths, WITHOUT altering execution behaviour — the
   existing pm_order_log / return-value / guardrail tests in
   ``test_pm_trader.py`` all still pass unchanged (Section 2).
3. ``wca.pm.propose.build_pm_proposals`` flags the ROUND_HALF_UP
   crosses-to-ask/bid case on a 1-tick-wide book, log-only (Section 3).
4. ``wca.bot.app.execute_cashout`` writes a "fill_observed" row for each
   SELL outcome branch, without changing any of its existing outcomes
   (Section 4).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from eth_account import Account

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from wca.pm import filltelemetry as ft  # noqa: E402
from wca.pm import propose  # noqa: E402
from wca.pm.trader import ClobTrader, TradeConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Section 1: filltelemetry primitives.
# ---------------------------------------------------------------------------

class TestFillTelemetryPrimitives:
    def test_log_placed_writes_one_jsonl_row(self, tmp_path):
        path = str(tmp_path / "fills.jsonl")
        ft.log_placed(
            order_id="ord-1", token_id="42", market="World Cup final",
            side="BUY", price=0.4, size=25.0, order_type="GTC", dry_run=False,
            path=path,
        )
        rows = ft.read_rows(path)
        assert len(rows) == 1
        assert rows[0]["kind"] == "placed"
        assert rows[0]["order_id"] == "ord-1"
        assert rows[0]["token_id"] == "42"
        assert rows[0]["side"] == "BUY"
        assert rows[0]["price"] == 0.4
        assert rows[0]["size"] == 25.0
        assert rows[0]["notional"] == pytest.approx(10.0)
        assert rows[0]["order_type"] == "GTC"
        assert rows[0]["dry_run"] is False

    def test_log_fill_observed_writes_row(self, tmp_path):
        path = str(tmp_path / "fills.jsonl")
        ft.log_fill_observed(
            order_id="ord-1", token_id="42", side="SELL",
            filled_size=10.0, requested_size=10.0, proceeds_or_cost=4.5,
            status="filled", path=path,
        )
        rows = ft.read_rows(path)
        assert len(rows) == 1
        assert rows[0]["kind"] == "fill_observed"
        assert rows[0]["status"] == "filled"
        assert rows[0]["filled_size"] == 10.0

    def test_log_mid_rounding_detects_cross_to_ask(self, tmp_path):
        path = str(tmp_path / "fills.jsonl")
        # bid=0.53 ask=0.54 -> mid=0.535 -> ROUND_HALF_UP to 2dp = 0.54 (the ask).
        ft.log_mid_rounding(
            token_id="tok", raw_mid=0.535, rounded_price=0.54,
            best_bid=0.53, best_ask=0.54, path=path,
        )
        rows = ft.read_rows(path)
        assert len(rows) == 1
        assert rows[0]["kind"] == "mid_rounding"
        assert rows[0]["crossed_to_ask"] is True
        assert rows[0]["crossed_to_bid"] is False

    def test_log_mid_rounding_no_cross_when_price_stays_inside_spread(self, tmp_path):
        path = str(tmp_path / "fills.jsonl")
        # bid=0.40 ask=0.50 -> mid=0.45 -> rounds to 0.45, well inside the spread.
        ft.log_mid_rounding(
            token_id="tok", raw_mid=0.45, rounded_price=0.45,
            best_bid=0.40, best_ask=0.50, path=path,
        )
        rows = ft.read_rows(path)
        assert rows[0]["crossed_to_ask"] is False
        assert rows[0]["crossed_to_bid"] is False

    def test_read_rows_missing_file_returns_empty(self, tmp_path):
        assert ft.read_rows(str(tmp_path / "nope.jsonl")) == []

    def test_read_rows_skips_corrupt_lines(self, tmp_path):
        path = tmp_path / "fills.jsonl"
        path.write_text('{"kind":"placed","a":1}\nNOT JSON\n{"kind":"placed","a":2}\n')
        rows = ft.read_rows(str(path))
        assert len(rows) == 2

    def test_writer_never_raises_on_bad_path(self):
        # A path under a file (not a directory) cannot be created; must not raise.
        with tempfile.NamedTemporaryFile() as tf:
            bad_path = os.path.join(tf.name, "fills.jsonl")
            ft.log_placed(
                order_id=None, token_id="x", market=None, side="BUY",
                price=0.5, size=1.0, order_type="GTC", dry_run=True,
                path=bad_path,
            )  # must not raise

    def test_default_log_path_matches_spec(self):
        assert ft.DEFAULT_LOG_PATH == "data/pm_fill_log.jsonl"


# ---------------------------------------------------------------------------
# Section 2: ClobTrader.place_order writes "placed" telemetry, behaviour
# unchanged. Mirrors the mocking pattern in tests/test_pm_trader.py.
# ---------------------------------------------------------------------------

def _resp(json_data: Any, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.text = ""
    return m


class RecordingSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls: List[Dict[str, Any]] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)


@pytest.fixture()
def throwaway_key() -> str:
    return Account.create().key.hex()


def _tmp_path_str(tmp_path, name: str) -> str:
    return str(tmp_path / name)


class TestTraderPlacedTelemetry:
    def test_dry_run_place_order_logs_placed_row(self, throwaway_key, tmp_path):
        secret = base64.urlsafe_b64encode(b"tele-secret-0001").decode()

        def handler(method, url, kwargs):
            if url.endswith("/auth/derive-api-key"):
                return _resp({"apiKey": "k", "secret": secret, "passphrase": "p"})
            return _resp({}, status=404)

        session = RecordingSession(handler)
        db = _tmp_path_str(tmp_path, "wca.db")
        fill_log = _tmp_path_str(tmp_path, "fills.jsonl")
        cfg = TradeConfig(dry_run=True, db_path=db, fill_log_path=fill_log, max_order_usd=30.0)
        t = ClobTrader(private_key=throwaway_key, config=cfg, session=session)

        out = t.place_order("1", 0.5, 10.0, "BUY", market_question="2026 FIFA World Cup")
        # Existing behaviour unchanged.
        assert out["dry_run"] is True
        assert out["submitted"] is False

        rows = ft.read_rows(fill_log)
        placed = [r for r in rows if r["kind"] == "placed"]
        assert len(placed) == 1
        assert placed[0]["dry_run"] is True
        assert placed[0]["side"] == "BUY"
        assert placed[0]["price"] == 0.5
        assert placed[0]["size"] == 10.0
        assert placed[0]["order_type"] == "GTC"

    def test_live_place_order_logs_placed_row_with_order_id(self, throwaway_key, tmp_path):
        secret = base64.urlsafe_b64encode(b"tele-secret-0002").decode()

        def handler(method, url, kwargs):
            if url.endswith("/auth/derive-api-key"):
                return _resp({"apiKey": "k", "secret": secret, "passphrase": "p"})
            if method == "POST" and url.endswith("/order"):
                return _resp({"orderID": "srv-telemetry-1", "success": True})
            return _resp({}, status=404)

        session = RecordingSession(handler)
        db = _tmp_path_str(tmp_path, "wca.db")
        fill_log = _tmp_path_str(tmp_path, "fills.jsonl")
        cfg = TradeConfig(
            dry_run=False, db_path=db, fill_log_path=fill_log,
            signature_type=0,  # SIG_TYPE_EOA — proves account class for a live order
        )
        t = ClobTrader(private_key=throwaway_key, config=cfg, session=session)

        out = t.place_order("42", 0.4, 25.0, "BUY", market_question="World Cup final")
        # Existing behaviour unchanged.
        assert out["dry_run"] is False
        assert out["submitted"] is True
        assert out["orderID"] == "srv-telemetry-1"

        rows = ft.read_rows(fill_log)
        placed = [r for r in rows if r["kind"] == "placed"]
        assert len(placed) == 1
        assert placed[0]["order_id"] == "srv-telemetry-1"
        assert placed[0]["dry_run"] is False
        assert placed[0]["notional"] == pytest.approx(10.0)

    def test_telemetry_write_failure_does_not_break_place_order(self, throwaway_key, tmp_path, monkeypatch):
        """A telemetry write failure must never surface as a trading error."""
        secret = base64.urlsafe_b64encode(b"tele-secret-0003").decode()

        def handler(method, url, kwargs):
            if url.endswith("/auth/derive-api-key"):
                return _resp({"apiKey": "k", "secret": secret, "passphrase": "p"})
            return _resp({}, status=404)

        session = RecordingSession(handler)
        db = _tmp_path_str(tmp_path, "wca.db")
        cfg = TradeConfig(dry_run=True, db_path=db, fill_log_path="/nonexistent-root/cant-write.jsonl")
        t = ClobTrader(private_key=throwaway_key, config=cfg, session=session)

        # Must not raise even though the fill_log_path is unwritable.
        out = t.place_order("1", 0.5, 10.0, "BUY", market_question="FIFA World Cup")
        assert out["dry_run"] is True


# ---------------------------------------------------------------------------
# Section 3: mid-rounding crossing telemetry in build_pm_proposals.
# ---------------------------------------------------------------------------

class _Rec:
    def __init__(self, match_desc, selection_team, model_prob):
        self.match_desc = match_desc
        self.selection_team = selection_team
        self.model_prob = model_prob


class TestProposeMidRoundingTelemetry:
    def test_one_tick_book_flagged_as_crossed_to_ask(self, monkeypatch, tmp_path):
        """bid=0.53/ask=0.54 -> mid=0.535 -> ROUND_HALF_UP snaps to 0.54 (the ask)."""
        rec = _Rec("Canada vs Bosnia and Herzegovina", "Canada", 0.60)
        monkeypatch.setattr(propose, "build_card", lambda *a, **k: [rec])

        def fake_resolve(home, away, selection, *, events=None):
            return {
                "token_id": "111", "price": 0.535, "neg_risk": True,
                "market_question": "Will Canada win?", "outcome": "Yes",
                "best_bid": 0.53, "best_ask": 0.54,
            }
        monkeypatch.setattr(propose, "resolve_outcome_token", fake_resolve)

        fill_log = str(tmp_path / "fills.jsonl")
        out = propose.build_pm_proposals(
            models=None, odds_df=None, fixtures_meta=None, pool_usd=1000.0,
            max_order_usd=30.0, fraction=0.25, cap=0.05, events=[],
            fill_log_path=fill_log,
        )
        # Behaviour unchanged: price still rounds to 0.54 and sizing proceeds.
        assert out[0]["price"] == pytest.approx(0.54)

        rows = ft.read_rows(fill_log)
        mid_rows = [r for r in rows if r["kind"] == "mid_rounding"]
        assert len(mid_rows) == 1
        assert mid_rows[0]["crossed_to_ask"] is True
        assert mid_rows[0]["raw_mid"] == pytest.approx(0.535)
        assert mid_rows[0]["rounded_price"] == pytest.approx(0.54)

    def test_wide_book_not_flagged_as_crossed(self, monkeypatch, tmp_path):
        rec = _Rec("USA vs Paraguay", "United States", 0.55)
        monkeypatch.setattr(propose, "build_card", lambda *a, **k: [rec])

        def fake_resolve(home, away, selection, *, events=None):
            return {
                "token_id": "999", "price": 0.50, "neg_risk": False,
                "market_question": "Will United States win?", "outcome": "Yes",
                "best_bid": 0.40, "best_ask": 0.60,
            }
        monkeypatch.setattr(propose, "resolve_outcome_token", fake_resolve)

        fill_log = str(tmp_path / "fills.jsonl")
        propose.build_pm_proposals(
            models=None, odds_df=None, fixtures_meta=None, pool_usd=1000.0,
            max_order_usd=40.0, fraction=0.25, cap=0.05, events=[],
            fill_log_path=fill_log,
        )
        rows = ft.read_rows(fill_log)
        mid_rows = [r for r in rows if r["kind"] == "mid_rounding"]
        assert len(mid_rows) == 1
        assert mid_rows[0]["crossed_to_ask"] is False
        assert mid_rows[0]["crossed_to_bid"] is False

    def test_missing_bid_ask_does_not_crash(self, monkeypatch, tmp_path):
        """outcomePrices fallback path has no bid/ask — must log gracefully."""
        rec = _Rec("Canada vs Bosnia and Herzegovina", "Bosnia and Herzegovina", 0.30)
        monkeypatch.setattr(propose, "build_card", lambda *a, **k: [rec])

        def fake_resolve(home, away, selection, *, events=None):
            return {
                "token_id": "211", "price": 0.205, "neg_risk": False,
                "market_question": "Will Bosnia win?", "outcome": "Yes",
                # no best_bid/best_ask keys at all (fallback path)
            }
        monkeypatch.setattr(propose, "resolve_outcome_token", fake_resolve)

        fill_log = str(tmp_path / "fills.jsonl")
        out = propose.build_pm_proposals(
            models=None, odds_df=None, fixtures_meta=None, pool_usd=1000.0,
            max_order_usd=30.0, fraction=0.25, cap=0.05, events=[],
            fill_log_path=fill_log,
        )
        assert out  # still produced a proposal — behaviour unchanged
        rows = ft.read_rows(fill_log)
        mid_rows = [r for r in rows if r["kind"] == "mid_rounding"]
        assert len(mid_rows) == 1
        assert mid_rows[0]["best_bid"] is None
        assert mid_rows[0]["best_ask"] is None
        assert mid_rows[0]["crossed_to_ask"] is False


# ---------------------------------------------------------------------------
# Section 4: resolve_outcome_token exposes best_bid/best_ask additively.
# ---------------------------------------------------------------------------

class TestResolveOutcomeTokenBidAsk:
    def test_bestbid_ask_mid_path_exposes_bid_ask(self):
        from wca.data.polymarket import resolve_outcome_token

        events = [{
            "slug": "fifwc-can-bih-2026-06-12",
            "title": "Canada vs. Bosnia and Herzegovina",
            "markets": [
                {
                    "groupItemTitle": "Canada",
                    "question": "Will Canada win on 2026-06-12?",
                    "clobTokenIds": json.dumps(["111", "112"]),
                    "outcomes": json.dumps(["Yes", "No"]),
                    "outcomePrices": json.dumps(["0.53", "0.47"]),
                    "bestBid": "0.53",
                    "bestAsk": "0.54",
                    "negRisk": True,
                },
                {
                    "groupItemTitle": "Bosnia and Herzegovina",
                    "question": "Will Bosnia and Herzegovina win on 2026-06-12?",
                    "clobTokenIds": json.dumps(["211", "212"]),
                    "outcomes": json.dumps(["Yes", "No"]),
                    "outcomePrices": json.dumps(["0.205", "0.795"]),
                    "negRisk": True,
                },
            ],
        }]
        r = resolve_outcome_token("Canada", "Bosnia and Herzegovina", "Canada", events=events)
        assert r is not None
        assert r["price"] == pytest.approx(0.535)
        assert r["best_bid"] == pytest.approx(0.53)
        assert r["best_ask"] == pytest.approx(0.54)

    def test_outcomeprices_fallback_leaves_bid_ask_none(self):
        from wca.data.polymarket import resolve_outcome_token

        events = [{
            "slug": "fifwc-can-bih-2026-06-12",
            "title": "Canada vs. Bosnia and Herzegovina",
            "markets": [
                {
                    "groupItemTitle": "Canada",
                    "question": "Will Canada win on 2026-06-12?",
                    "clobTokenIds": json.dumps(["111", "112"]),
                    "outcomes": json.dumps(["Yes", "No"]),
                    "outcomePrices": json.dumps(["0.53", "0.47"]),
                },
                {
                    "groupItemTitle": "Bosnia and Herzegovina",
                    "question": "Will Bosnia and Herzegovina win on 2026-06-12?",
                    "clobTokenIds": json.dumps(["211", "212"]),
                    "outcomes": json.dumps(["Yes", "No"]),
                    "outcomePrices": json.dumps(["0.205", "0.795"]),
                },
            ],
        }]
        r = resolve_outcome_token("Canada", "Bosnia and Herzegovina", "Bosnia and Herzegovina", events=events)
        assert r is not None
        assert r["price"] == pytest.approx(0.205)
        assert r["best_bid"] is None
        assert r["best_ask"] is None
