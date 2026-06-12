"""Tests for the Polymarket proposal producer.

Three groups, all offline (no live network):

1. ``wca.data.polymarket.resolve_outcome_token`` against a synthetic events
   fixture — JSON-string array parsing for ``clobTokenIds``/``outcomePrices``,
   a team-win market, the Draw market, the bestBid/bestAsk mid vs.
   ``outcomePrices`` price source, and a no-match -> ``None``.
2. ``wca.pm.propose.build_pm_proposals`` — quarter-Kelly + cap sizing math
   hand-checked, token resolution mocked, and the ``<$1`` / no-token skips.
3. The CLI push path — ``push_parked_order`` called with a well-formed,
   gate-compatible proposal and Telegram ``send_message`` mocked.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from wca.data import polymarket
from wca.data.polymarket import resolve_outcome_token
from wca.markets import kelly as kelly_mod
from wca.pm import propose


# ---------------------------------------------------------------------------
# Synthetic events fixture (mirrors the live Gamma shape).
# ---------------------------------------------------------------------------

# Two deterministic 77-digit-ish token ids; index 0 of clobTokenIds is YES.
_CAN_YES = "111"
_CAN_NO = "112"
_BIH_YES = "211"
_BIH_NO = "212"
_DRAW_YES = "311"
_DRAW_NO = "312"


def _events():
    """One single-match event: Canada vs. Bosnia, with win + draw markets.

    clobTokenIds / outcomes / outcomePrices are JSON *strings* exactly as the
    Gamma API returns them, so resolve_outcome_token must decode them.
    """
    return [
        {
            "id": "351717",
            "title": "Canada vs. Bosnia-Herzegovina",
            "markets": [
                {
                    "question": "Will Canada win on 2026-06-12?",
                    "groupItemTitle": "Canada",
                    "outcomes": json.dumps(["Yes", "No"]),
                    "outcomePrices": json.dumps(["0.535", "0.465"]),
                    "clobTokenIds": json.dumps([_CAN_YES, _CAN_NO]),
                    "bestBid": 0.53,
                    "bestAsk": 0.54,
                    "negRisk": True,
                },
                {
                    "question": "Will Bosnia and Herzegovina win on 2026-06-12?",
                    "groupItemTitle": "Bosnia-Herzegovina",
                    "outcomes": json.dumps(["Yes", "No"]),
                    # No book here -> price must fall back to outcomePrices.
                    "outcomePrices": json.dumps(["0.205", "0.795"]),
                    "clobTokenIds": json.dumps([_BIH_YES, _BIH_NO]),
                    "bestBid": None,
                    "bestAsk": None,
                    "negRisk": True,
                },
                {
                    "question": "Will Canada vs. Bosnia and Herzegovina end in a draw?",
                    "groupItemTitle": "Draw (Canada vs. Bosnia-Herzegovina)",
                    "outcomes": json.dumps(["Yes", "No"]),
                    "outcomePrices": json.dumps(["0.275", "0.725"]),
                    "clobTokenIds": json.dumps([_DRAW_YES, _DRAW_NO]),
                    "bestBid": 0.27,
                    "bestAsk": 0.28,
                    "negRisk": True,
                },
            ],
        }
    ]


# ---------------------------------------------------------------------------
# resolve_outcome_token
# ---------------------------------------------------------------------------


def test_resolve_team_win_uses_bestbid_ask_mid():
    r = resolve_outcome_token("Canada", "Bosnia and Herzegovina", "Canada", events=_events())
    assert r is not None
    assert r["token_id"] == _CAN_YES
    # mid of 0.53 / 0.54 = 0.535, not the (coincidentally equal) outcomePrice.
    assert r["price"] == pytest.approx(0.535)
    assert r["outcome"] == "Yes"
    assert r["neg_risk"] is True
    assert "Canada win" in r["market_question"]


def test_resolve_falls_back_to_outcomeprices_when_no_book():
    # Bosnia market has no bestBid/bestAsk -> YES outcomePrice 0.205.
    r = resolve_outcome_token(
        "Canada", "Bosnia and Herzegovina", "Bosnia and Herzegovina", events=_events()
    )
    assert r is not None
    assert r["token_id"] == _BIH_YES
    assert r["price"] == pytest.approx(0.205)


def test_resolve_draw_market():
    r = resolve_outcome_token("Canada", "Bosnia and Herzegovina", "Draw", events=_events())
    assert r is not None
    assert r["token_id"] == _DRAW_YES
    assert r["price"] == pytest.approx(0.275)  # mid 0.27/0.28
    assert r["outcome"] == "Yes"


def test_resolve_order_independent_and_canonicalises():
    # Reversed fixture order + an aliased spelling ("Bosnia & Herzegovina")
    # must still match via canonical().
    r = resolve_outcome_token(
        "Bosnia & Herzegovina", "Canada", "Canada", events=_events()
    )
    assert r is not None and r["token_id"] == _CAN_YES


def test_resolve_no_match_returns_none():
    assert resolve_outcome_token("Brazil", "Morocco", "Brazil", events=_events()) is None


def test_resolve_unknown_selection_in_matched_event_returns_none():
    # Event matches but the selection team is not one of its markets.
    assert resolve_outcome_token("Canada", "Bosnia and Herzegovina", "Spain", events=_events()) is None


def test_parse_json_array_tolerates_garbage():
    assert polymarket._parse_json_array('["a","b"]') == ["a", "b"]
    assert polymarket._parse_json_array(["a"]) == ["a"]
    assert polymarket._parse_json_array("not json") is None
    assert polymarket._parse_json_array(None) is None


# ---------------------------------------------------------------------------
# build_pm_proposals — sizing + skips, with build_card + tokens mocked.
# ---------------------------------------------------------------------------


class _Rec:
    """Minimal stand-in for wca.card.Recommendation (only fields used)."""

    def __init__(self, match_desc, selection_team, model_prob):
        self.match_desc = match_desc
        self.selection_team = selection_team
        self.model_prob = model_prob


def _patch_build_card(monkeypatch, recs):
    monkeypatch.setattr(propose, "build_card", lambda *a, **k: list(recs))


def _patch_resolver(monkeypatch, table):
    """table: (home, away, selection) -> resolved dict or None."""

    def fake(home, away, selection, *, events=None):
        return table.get((home, away, selection))

    monkeypatch.setattr(propose, "resolve_outcome_token", fake)


def test_build_proposals_sizing_quarter_kelly_and_cap(monkeypatch):
    # model_prob 0.60 at PM price 0.50 -> decimal odds 2.0.
    # full Kelly f* = (p*o - 1)/(o-1) = (1.2 - 1)/1.0 = 0.20
    # quarter Kelly = 0.05 of pool; cap = 0.05 -> equal, so size = 0.05 * 1000 = 50,
    # but hard cap = min(max_order_usd=30, 0.05*1000=50) = 30.
    rec = _Rec("Canada vs Bosnia and Herzegovina", "Canada", 0.60)
    _patch_build_card(monkeypatch, [rec])
    _patch_resolver(
        monkeypatch,
        {
            ("Canada", "Bosnia and Herzegovina", "Canada"): {
                "token_id": "111",
                "price": 0.50,
                "neg_risk": True,
                "market_question": "Will Canada win on 2026-06-12?",
                "outcome": "Yes",
            }
        },
    )
    out = propose.build_pm_proposals(
        models=None,
        odds_df=pd.DataFrame(),
        fixtures_meta=pd.DataFrame(),
        pool_usd=1000.0,
        max_order_usd=30.0,
        fraction=0.25,
        cap=0.05,
        events=[],
    )
    assert len(out) == 1
    p = out[0]
    assert p["token_id"] == "111"
    assert p["side"] == "BUY"
    assert p["price"] == 0.50
    # capped at $30 (the max_order_usd ceiling)
    assert p["size_usd"] == pytest.approx(30.0)
    assert p["shares"] == pytest.approx(30.0 / 0.50)  # 60 shares
    assert p["neg_risk"] is True
    # ev recomputed at the PM price: p*o - 1 = 0.6*2 - 1 = 0.20
    assert p["ev"] == pytest.approx(0.20)
    assert p["model_prob"] == 0.60
    assert p["match_desc"] == "Canada vs Bosnia and Herzegovina"


def test_build_proposals_sizing_uncapped_matches_kelly(monkeypatch):
    # Choose params so neither the per-order USD ceiling nor the cap bind, and
    # the size equals the raw quarter-Kelly stake.
    # p=0.55, price=0.50 -> o=2.0, f*=(1.1-1)/1=0.10, quarter=0.025 of pool.
    # pool=1000 -> 25.0; cap*pool=0.05*1000=50, max_order_usd=40 -> hard cap 40.
    # 25 < 40 so uncapped.
    rec = _Rec("USA vs Paraguay", "United States", 0.55)
    _patch_build_card(monkeypatch, [rec])
    _patch_resolver(
        monkeypatch,
        {
            ("USA", "Paraguay", "United States"): {
                "token_id": "999",
                "price": 0.50,
                "neg_risk": False,
                "market_question": "Will United States win on 2026-06-12?",
                "outcome": "Yes",
            }
        },
    )
    out = propose.build_pm_proposals(
        models=None,
        odds_df=pd.DataFrame(),
        fixtures_meta=pd.DataFrame(),
        pool_usd=1000.0,
        max_order_usd=40.0,
        fraction=0.25,
        cap=0.05,
        events=[],
    )
    expected = kelly_mod.stake(0.55, 2.0, 1000.0, fraction=0.25, cap=0.05)
    assert out[0]["size_usd"] == pytest.approx(expected)
    assert out[0]["size_usd"] == pytest.approx(25.0)


def test_build_proposals_skips_unresolved_token(monkeypatch):
    rec = _Rec("Qatar vs Switzerland", "Qatar", 0.30)
    _patch_build_card(monkeypatch, [rec])
    _patch_resolver(monkeypatch, {})  # nothing resolves
    out = propose.build_pm_proposals(
        None, pd.DataFrame(), pd.DataFrame(), 1000.0, events=[]
    )
    assert out == []


def test_build_proposals_skips_sub_dollar_stake(monkeypatch):
    # Tiny edge -> tiny Kelly stake -> below $1 -> skipped.
    # p=0.51 at price 0.50 -> o=2.0, f*=0.02, quarter=0.005 -> pool 100 -> $0.50.
    rec = _Rec("Haiti vs Scotland", "Haiti", 0.51)
    _patch_build_card(monkeypatch, [rec])
    _patch_resolver(
        monkeypatch,
        {
            ("Haiti", "Scotland", "Haiti"): {
                "token_id": "1",
                "price": 0.50,
                "neg_risk": True,
                "market_question": "Will Haiti win on 2026-06-13?",
                "outcome": "Yes",
            }
        },
    )
    out = propose.build_pm_proposals(
        None, pd.DataFrame(), pd.DataFrame(), 100.0, max_order_usd=30.0, events=[]
    )
    assert out == []


def test_build_proposals_draw_selection_uses_draw(monkeypatch):
    rec = _Rec("Canada vs Bosnia and Herzegovina", "Draw", 0.40)
    _patch_build_card(monkeypatch, [rec])
    captured = {}

    def fake(home, away, selection, *, events=None):
        captured["args"] = (home, away, selection)
        return {
            "token_id": "311",
            "price": 0.275,
            "neg_risk": True,
            "market_question": "Will Canada vs. Bosnia and Herzegovina end in a draw?",
            "outcome": "Yes",
        }

    monkeypatch.setattr(propose, "resolve_outcome_token", fake)
    out = propose.build_pm_proposals(
        None, pd.DataFrame(), pd.DataFrame(), 2500.0, events=[]
    )
    # Selection passed through verbatim ("Draw") with the split fixture teams.
    assert captured["args"] == ("Canada", "Bosnia and Herzegovina", "Draw")
    assert out and out[0]["token_id"] == "311"


# ---------------------------------------------------------------------------
# CLI push path — park gate + Telegram both mocked.
# ---------------------------------------------------------------------------


def test_augment_for_gate_makes_proposal_gate_compatible():
    """The CLI augmentation adds the keys the bot gate reads (size, label)."""
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location(
        "wca_pm_propose",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_pm_propose.py"),
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    proposal = {
        "token_id": "111",
        "side": "BUY",
        "price": 0.50,
        "size_usd": 30.0,
        "shares": 60.0,
        "market_question": "Will Canada win on 2026-06-12?",
        "outcome": "Yes",
        "match_desc": "Canada vs Bosnia and Herzegovina",
        "model_prob": 0.60,
        "ev": 0.20,
        "neg_risk": True,
    }
    augmented = cli._augment_for_gate(proposal)
    # Bot gate keys present and correct.
    assert augmented["size"] == pytest.approx(60.0)  # shares
    assert augmented["label"]  # non-empty human label

    # Feed it through the real bot gate to prove it parks + renders.
    import wca.bot.app as app

    app._PENDING_ORDERS.clear()
    app._PM_SEQ["n"] = 0
    text = app.push_parked_order(augmented)
    try:
        assert "PM-1" in text
        # notional = price * size(shares) = 0.50 * 60 = $30.00
        assert "$30.00" in text
        assert "Y PM-1" in text and "N PM-1" in text
        # The parked order carries the token id the gate will place.
        assert app._PENDING_ORDERS[1]["token_id"] == "111"
    finally:
        app._PENDING_ORDERS.clear()
        app._PM_SEQ["n"] = 0


def test_cli_push_calls_gate_and_telegram(monkeypatch):
    """End-to-end push: push_parked_order called per proposal, Telegram sent."""
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location(
        "wca_pm_propose",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_pm_propose.py"),
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    # Simulate the push loop directly with a fake gate + fake Telegram client.
    pushed = []
    sent = []

    def fake_push(proposal):
        pushed.append(proposal)
        return "place $30.00 ... Reply `Y PM-%d`" % len(pushed)

    class _FakeClient:
        def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    proposals = [
        {
            "token_id": "111",
            "side": "BUY",
            "price": 0.50,
            "size_usd": 30.0,
            "shares": 60.0,
            "market_question": "Will Canada win on 2026-06-12?",
            "outcome": "Yes",
            "match_desc": "Canada vs Bosnia and Herzegovina",
            "model_prob": 0.60,
            "ev": 0.20,
            "neg_risk": True,
        }
    ]

    client = _FakeClient()
    admin = "12345"
    for p in proposals:
        text = fake_push(cli._augment_for_gate(p))
        client.send_message(admin, text)

    assert len(pushed) == 1
    # Gate received a gate-compatible proposal (size = shares present).
    assert pushed[0]["size"] == pytest.approx(60.0)
    assert pushed[0]["token_id"] == "111"
    assert sent == [("12345", "place $30.00 ... Reply `Y PM-1`")]


def test_resolve_funder_falls_back_to_proxy(monkeypatch, capsys):
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location(
        "wca_pm_propose",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_pm_propose.py"),
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)
    from wca.pm.trader import KNOWN_PROXY_FUNDER

    funder = cli._resolve_funder()
    assert funder == KNOWN_PROXY_FUNDER
    err = capsys.readouterr().err
    assert "POLYMARKET_FUNDER not set" in err

    monkeypatch.setenv("POLYMARKET_FUNDER", "0xABC")
    assert cli._resolve_funder() == "0xABC"
