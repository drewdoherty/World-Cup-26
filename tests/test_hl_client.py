"""Tests for the read-only Hyperliquid HIP-4 info client (wca.hl.client).

Fully offline: request construction is checked against a stub session
(nothing hits the network) and parsing runs on raw-response fixtures captured
live on 2026-07-09 (tests/fixtures/hl_xvenue/). The addressing scheme
(encoding = 10*outcome_id + side, coin "#<enc>", asset id 100_000_000+enc)
comes from the recon's docs capture and was verified against live l2Book/
recentTrades/candles responses.
"""
from __future__ import annotations

import json
import os

import pytest
import requests

from wca.hl import client

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "hl_xvenue")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------

def test_encoding_scheme():
    # Argentina-champion (outcome 173) Yes side -> #1730 (verified live).
    assert client.encoding(173, 0) == 1730
    assert client.encoding(173, 1) == 1731
    assert client.coin(173, 0) == "#1730"
    assert client.token_name(202, 1) == "+2021"
    assert client.order_asset_id(173, 0) == 100_001_730


def test_encoding_rejects_bad_side():
    with pytest.raises(ValueError):
        client.encoding(173, 2)
    with pytest.raises(ValueError):
        client.coin(173, -1)


# ---------------------------------------------------------------------------
# Request construction (stub session, no network)
# ---------------------------------------------------------------------------

class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StubSession:
    def __init__(self, payload=None):
        self.calls = []
        self._payload = payload if payload is not None else {}

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": json.loads(data), "timeout": timeout})
        return _StubResponse(self._payload)


def test_client_builds_verified_request_shapes():
    session = _StubSession()
    hl = client.HLInfoClient(session=session)
    hl.outcome_meta()
    hl.settled_outcome(172)
    hl.l2_book(173, 0)
    hl.l2_book(173, 1, n_sig_figs=4)
    hl.recent_trades(761, 0)
    hl.candle_snapshot(202, 0, "1h", 1000, 2000)
    hl.all_mids()
    bodies = [c["body"] for c in session.calls]
    assert bodies[0] == {"type": "outcomeMeta"}
    assert bodies[1] == {"type": "settledOutcome", "outcome": 172}
    assert bodies[2] == {"type": "l2Book", "coin": "#1730"}
    assert bodies[3] == {"type": "l2Book", "coin": "#1731", "nSigFigs": 4}
    assert bodies[4] == {"type": "recentTrades", "coin": "#7610"}
    assert bodies[5] == {
        "type": "candleSnapshot",
        "req": {"coin": "#2020", "interval": "1h", "startTime": 1000, "endTime": 2000},
    }
    assert bodies[6] == {"type": "allMids"}
    assert all(c["url"] == client.HL_INFO_URL for c in session.calls)


# ---------------------------------------------------------------------------
# Parsing captured raw responses
# ---------------------------------------------------------------------------

def test_parse_l2_book_norway_no_side():
    # Raw dump captured live 2026-07-09 18:14:44 UTC (Norway-champion No,
    # coin #2021). Best ask 0.936 x 249,600 shares is the leg of the one
    # real cross-venue hit found in the recon.
    book = client.parse_l2_book(_fixture("l2book_202_side1.json"))
    assert book["coin"] == "#2021"
    assert book["time_ms"] == 1783620884518
    assert book["bids"][0] == (0.9354, 527.0)
    assert book["asks"][0] == (0.936, 249600.0)
    # sorted: bids descending, asks ascending
    assert all(book["bids"][i][0] >= book["bids"][i + 1][0] for i in range(len(book["bids"]) - 1))
    assert all(book["asks"][i][0] <= book["asks"][i + 1][0] for i in range(len(book["asks"]) - 1))
    # REST l2Book truncates at 20 levels/side
    assert len(book["bids"]) <= 20 and len(book["asks"]) <= 20


def test_parse_l2_book_merged_dual_book_mirror():
    # HIP-4 books are merged dual books: side1 == 1 - side0 (buy Yes @ p is
    # sell No @ 1-p). Verified live for 202; exact here because both side
    # snapshots landed without an intervening tick.
    s0 = client.parse_l2_book(_fixture("l2book_202_side0.json"))
    s1 = client.parse_l2_book(_fixture("l2book_202_side1.json"))
    assert s0["bids"][0][0] == pytest.approx(1.0 - s1["asks"][0][0], abs=1e-9)
    assert s0["bids"][0][1] == s1["asks"][0][1] == 249600.0


def test_best_bid_ask_and_empty_book():
    book = client.parse_l2_book(_fixture("l2book_202_side0.json"))
    bid, bid_sz, ask, ask_sz = client.best_bid_ask(book)
    assert (bid, bid_sz, ask, ask_sz) == (0.064, 249600.0, 0.0646, 527.0)
    empty = client.parse_l2_book({"coin": "#9990", "time": 0, "levels": [[], []]})
    assert client.best_bid_ask(empty) == (None, None, None, None)


def test_outcome_meta_side_names():
    meta = _fixture("outcome_meta_wc.json")
    by_id = client.outcomes_by_id(meta)
    assert set(by_id) == {173, 176, 188, 189, 199, 202, 212, 214, 761, 778, 779, 788}
    # Champion markets are Yes/No; QF match markets are [teamA, teamB] with
    # NO draw side — the side order DEFINES the 0/1 encoding.
    assert client.side_names(meta, 173) == ["Yes", "No"]
    assert client.side_names(meta, 761) == ["France", "Morocco"]
    assert client.side_names(meta, 778) == ["Norway", "England"]
    assert client.side_names(meta, 779) == ["Spain", "Belgium"]
    assert client.side_names(meta, 788) == ["Argentina", "Switzerland"]
    with pytest.raises(KeyError):
        client.side_names(meta, 999)


def test_qf_market_descriptions_settlement_loadbearing():
    # The cross-venue pairing leans on the verbatim resolution text: ET+pens
    # valid, and the 0.5-void tail. Assert the on-chain description still
    # carries both (fixture = live outcomeMeta capture).
    meta = _fixture("outcome_meta_wc.json")
    desc = client.outcomes_by_id(meta)[761]["description"]
    assert "extra time, and penalties" in desc
    assert "resolves to 0.5" in desc
    assert "July 26, 2026 at 23:59 UTC" in desc


def test_is_vpn_drop_signature():
    assert client.is_vpn_drop(
        requests.exceptions.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
    )
    assert not client.is_vpn_drop(requests.exceptions.SSLError("certificate verify failed"))
    assert not client.is_vpn_drop(ValueError("WRONG_VERSION_NUMBER"))
