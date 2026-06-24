"""Tests for wca.bot.vision — betslip screenshot extraction via Anthropic vision.

No real network: the injected ``requests`` session's ``post`` is monkeypatched
to return a small :class:`FakeResp` carrying a canned Anthropic Messages-API
body. We exercise parsing of single/multiple bets, odds coercion (fractional /
EVS / string), markdown-fenced and prose-prefixed replies, and all the error
paths (missing key, API error body, empty bets).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from wca.bot import vision
from wca.bot.vision import (
    ExtractedBet,
    VisionError,
    extract_bets_from_image,
    fractional_to_decimal,
)


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


class FakeResp:
    """Minimal stand-in for a ``requests.Response``."""

    def __init__(self, payload: Any, status_code: int = 200, text: Optional[str] = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self._raise_on_json = isinstance(payload, _NoJSON)
        self.text = text if text is not None else (
            "<not json>" if self._raise_on_json else json.dumps(payload)
        )

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("no JSON could be decoded")
        return self._payload


class _NoJSON:
    """Sentinel signalling FakeResp.json() should raise (non-JSON body)."""


class FakeSession:
    """Captures the last POST and returns a queued response."""

    def __init__(self, resp: FakeResp) -> None:
        self._resp = resp
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, headers: Optional[Dict[str, Any]] = None,
             json: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> FakeResp:
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return self._resp


def _anthropic_body(text: str) -> Dict[str, Any]:
    """Wrap model output text in a realistic Anthropic Messages response body."""
    return {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }


def _session_returning(text: str, status_code: int = 200) -> FakeSession:
    return FakeSession(FakeResp(_anthropic_body(text), status_code=status_code))


# ---------------------------------------------------------------------------
# fractional_to_decimal unit tests.
# ---------------------------------------------------------------------------


class TestFractionalToDecimal:
    def test_simple_fraction(self) -> None:
        assert fractional_to_decimal("31/20") == pytest.approx(2.55)

    def test_odds_on_fraction(self) -> None:
        assert fractional_to_decimal("2/9") == pytest.approx(1.2222222, rel=1e-5)

    def test_evens_variants(self) -> None:
        for s in ("EVS", "evs", "Evens", "even", "1/1"):
            assert fractional_to_decimal(s) == pytest.approx(2.0)

    def test_plain_decimal_string(self) -> None:
        assert fractional_to_decimal("2.55") == pytest.approx(2.55)

    def test_whitespace_tolerated(self) -> None:
        assert fractional_to_decimal("  10 / 1 ") == pytest.approx(11.0)

    @pytest.mark.parametrize("bad", ["", "abc", "1/0", "/5", "5/"])
    def test_bad_inputs_raise(self, bad: str) -> None:
        with pytest.raises(ValueError):
            fractional_to_decimal(bad)


# ---------------------------------------------------------------------------
# Happy paths.
# ---------------------------------------------------------------------------


def test_single_bet_happy_path() -> None:
    body_text = json.dumps(
        {
            "bets": [
                {
                    "bookmaker": "bet365",
                    "match": "England vs France",
                    "market": "Match Result",
                    "selection": "England",
                    "odds_decimal": 2.5,
                    "stake": 10.0,
                    "returns": 25.0,
                    "status": "open",
                    "is_boost": False,
                    "confidence": 0.95,
                }
            ]
        }
    )
    sess = _session_returning(body_text)
    bets = extract_bets_from_image(b"fakeimg", api_key="k", session=sess)

    assert len(bets) == 1
    bet = bets[0]
    assert isinstance(bet, ExtractedBet)
    assert bet.bookmaker == "bet365"
    assert bet.match_desc == "England vs France"
    assert bet.market == "Match Result"
    assert bet.selection == "England"
    assert bet.decimal_odds == pytest.approx(2.5)
    assert bet.stake == pytest.approx(10.0)
    assert bet.potential_returns == pytest.approx(25.0)
    assert bet.status == "open"
    assert bet.is_boost is False
    assert bet.confidence == pytest.approx(0.95)
    assert bet.raw_text  # raw model text preserved


def test_request_shape_and_headers() -> None:
    sess = _session_returning(json.dumps({"bets": []}))
    extract_bets_from_image(
        b"abc", api_key="secret", model="my-model", media_type="image/png", session=sess
    )
    assert len(sess.calls) == 1
    call = sess.calls[0]
    assert call["url"] == vision.API_URL
    assert call["headers"]["x-api-key"] == "secret"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["headers"]["content-type"] == "application/json"

    payload = call["json"]
    assert payload["model"] == "my-model"
    assert payload["max_tokens"] == 1024
    content = payload["messages"][0]["content"]
    img, txt = content[0], content[1]
    assert img["type"] == "image"
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/png"
    # base64 of b"abc"
    assert img["source"]["data"] == "YWJj"
    assert txt["type"] == "text"
    assert "JSON" in txt["text"]
    assert "Paddy Power" in txt["text"]
    assert "do NOT call that Betfair" in txt["text"]


def test_multiple_bets_one_slip() -> None:
    body_text = json.dumps(
        {
            "bets": [
                {
                    "bookmaker": "Sky Bet",
                    "match": "Spain vs Italy",
                    "market": "Match Result",
                    "selection": "Spain",
                    "odds_decimal": 1.9,
                    "stake": 5.0,
                    "returns": None,
                    "status": "open",
                    "is_boost": False,
                    "confidence": 0.8,
                },
                {
                    "bookmaker": "Sky Bet",
                    "match": "Spain vs Italy",
                    "market": "Both Teams To Score",
                    "selection": "Yes",
                    "odds_decimal": 1.7,
                    "stake": 5.0,
                    "returns": None,
                    "status": "won",
                    "is_boost": True,
                    "confidence": 0.7,
                },
            ]
        }
    )
    sess = _session_returning(body_text)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)

    assert len(bets) == 2
    assert bets[0].selection == "Spain"
    assert bets[0].potential_returns is None
    assert bets[1].market == "Both Teams To Score"
    assert bets[1].status == "won"
    assert bets[1].is_boost is True


def test_fractional_and_evs_odds_coercion() -> None:
    # Model returns odds as strings — we must coerce to decimal.
    body_text = json.dumps(
        {
            "bets": [
                {
                    "bookmaker": None,
                    "match": "A vs B",
                    "market": "Result",
                    "selection": "A",
                    "odds_decimal": "31/20",
                    "stake": "10",
                    "returns": "25.50",
                    "status": "open",
                    "is_boost": False,
                    "confidence": 0.9,
                },
                {
                    "bookmaker": None,
                    "match": "A vs B",
                    "market": "Result",
                    "selection": "Draw",
                    "odds_decimal": "EVS",
                    "stake": "£10",
                    "returns": "20",
                    "status": "open",
                    "is_boost": False,
                    "confidence": 0.6,
                },
            ]
        }
    )
    sess = _session_returning(body_text)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)

    assert bets[0].decimal_odds == pytest.approx(2.55)
    assert bets[0].stake == pytest.approx(10.0)
    assert bets[0].potential_returns == pytest.approx(25.50)
    assert bets[1].decimal_odds == pytest.approx(2.0)  # EVS
    assert bets[1].stake == pytest.approx(10.0)  # currency symbol stripped


def test_json_fenced_response() -> None:
    inner = json.dumps({"bets": [
        {"bookmaker": "DraftKings", "match": "X vs Y", "market": "ML",
         "selection": "X", "odds_decimal": 1.8, "stake": 20, "returns": 36,
         "status": "open", "is_boost": False, "confidence": 0.88}
    ]})
    fenced = "```json\n" + inner + "\n```"
    sess = _session_returning(fenced)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)
    assert len(bets) == 1
    assert bets[0].bookmaker == "DraftKings"
    assert bets[0].decimal_odds == pytest.approx(1.8)


def test_leading_prose_response() -> None:
    inner = json.dumps({"bets": [
        {"bookmaker": None, "match": "P vs Q", "market": "Total",
         "selection": "Over 2.5", "odds_decimal": 2.1, "stake": None,
         "returns": None, "status": "open", "is_boost": False, "confidence": 0.5}
    ]})
    prose = "Here is the betslip I extracted for you:\n\n" + inner + "\n\nLet me know if you need anything else."
    sess = _session_returning(prose)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)
    assert len(bets) == 1
    assert bets[0].selection == "Over 2.5"
    assert bets[0].stake is None


def test_empty_bets_returns_empty_list() -> None:
    sess = _session_returning(json.dumps({"bets": []}))
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)
    assert bets == []


def test_status_normalized_and_defaults() -> None:
    body_text = json.dumps({"bets": [
        {"bookmaker": "X", "match": "m", "market": "k", "selection": "s",
         "odds_decimal": 2.0, "stake": 1, "returns": 2, "status": "WEIRD",
         "is_boost": "yes", "confidence": 1.5}
    ]})
    sess = _session_returning(body_text)
    bet = extract_bets_from_image(b"img", api_key="k", session=sess)[0]
    assert bet.status == "open"  # unknown status falls back to open
    assert bet.is_boost is True  # "yes" -> True
    assert bet.confidence == pytest.approx(1.0)  # clamped to [0, 1]


def test_to_dict_roundtrip() -> None:
    bet = ExtractedBet(
        match_desc="A vs B", market="ML", selection="A", bookmaker="bet365",
        decimal_odds=2.0, stake=10.0, potential_returns=20.0, status="open",
        is_boost=False, confidence=0.9, raw_text="{}",
    )
    d = bet.to_dict()
    assert d["match_desc"] == "A vs B"
    assert d["bookmaker"] == "bet365"
    assert d["decimal_odds"] == 2.0
    assert set(d.keys()) == {
        "match_desc", "market", "selection", "bookmaker", "decimal_odds",
        "stake", "potential_returns", "status", "is_boost", "is_free_bet",
        "confidence", "raw_text", "currency", "is_combo", "notes",
    }


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_missing_api_key_raises(monkeypatch: Any) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sess = _session_returning(json.dumps({"bets": []}))
    with pytest.raises(VisionError):
        extract_bets_from_image(b"img", session=sess)


def test_api_key_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "envkey")
    sess = _session_returning(json.dumps({"bets": []}))
    extract_bets_from_image(b"img", session=sess)
    assert sess.calls[0]["headers"]["x-api-key"] == "envkey"


def test_model_default_and_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("ANTHROPIC_VISION_MODEL", raising=False)
    sess = _session_returning(json.dumps({"bets": []}))
    extract_bets_from_image(b"img", api_key="k", session=sess)
    assert sess.calls[0]["json"]["model"] == "claude-sonnet-4-6"

    monkeypatch.setenv("ANTHROPIC_VISION_MODEL", "env-vision-model")
    sess2 = _session_returning(json.dumps({"bets": []}))
    extract_bets_from_image(b"img", api_key="k", session=sess2)
    assert sess2.calls[0]["json"]["model"] == "env-vision-model"


def test_api_error_body_raises() -> None:
    # Anthropic error body: HTTP 400 with {"type":"error","error":{...}} and no content.
    err_body = {"type": "error", "error": {"type": "invalid_request_error",
                                           "message": "bad image"}}
    sess = FakeSession(FakeResp(err_body, status_code=400))
    with pytest.raises(VisionError) as ei:
        extract_bets_from_image(b"img", api_key="k", session=sess)
    assert "bad image" in str(ei.value)


def test_body_without_content_raises() -> None:
    # HTTP 200 but malformed body lacking the content array.
    sess = FakeSession(FakeResp({"id": "msg", "type": "message"}, status_code=200))
    with pytest.raises(VisionError):
        extract_bets_from_image(b"img", api_key="k", session=sess)


def test_non_json_response_raises() -> None:
    sess = FakeSession(FakeResp(_NoJSON(), status_code=200, text="<html>oops</html>"))
    with pytest.raises(VisionError):
        extract_bets_from_image(b"img", api_key="k", session=sess)


def test_no_json_object_in_text_raises() -> None:
    sess = _session_returning("I could not read the slip, sorry.")
    with pytest.raises(VisionError):
        extract_bets_from_image(b"img", api_key="k", session=sess)


def test_network_failure_raises() -> None:
    import requests

    class BoomSession:
        def post(self, *a: Any, **k: Any) -> Any:
            raise requests.RequestException("connection reset")

    with pytest.raises(VisionError):
        extract_bets_from_image(b"img", api_key="k", session=BoomSession())


# ---------------------------------------------------------------------------
# Bet Builder / combo parsing — a SINGLE combined bet (N legs, one combined
# price, one total stake) must collapse to ONE row, never N rows each at the
# full stake.
# ---------------------------------------------------------------------------


def test_bet_builder_single_fixture_is_one_row() -> None:
    """The concrete failing case: USA v Australia Bet Builder (x3) @ 5.12, £10.

    Three legs (Over 3.5 Goals, Home Over/Under 2.5 -> Over, Anytime Goalscorer
    -> Folarin Balogun) at ONE combined price (5.12) with ONE £10 stake. Must
    be a single Bet Builder row, NOT three £10 bets at "@ ?".
    """
    body_text = json.dumps(
        {
            "bets": [
                {
                    "bookmaker": "Betfair Sportsbook",
                    "match": "USA v Australia",
                    "market": "Bet Builder",
                    "selection": None,
                    "is_combo": True,
                    "legs": [
                        {"market": "Total Goals", "selection": "Over 3.5 Goals"},
                        {"market": "Home Team Over/Under 2.5", "selection": "Over"},
                        {"market": "Anytime Goalscorer Safe Sub",
                         "selection": "Folarin Balogun"},
                    ],
                    "odds_decimal": 5.12,
                    "stake": 10.0,
                    "currency": "GBP",
                    "returns": 41.20,
                    "status": "open",
                    "is_boost": False,
                    "is_free_bet": True,
                    "confidence": 0.9,
                }
            ]
        }
    )
    sess = _session_returning(body_text)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)

    assert len(bets) == 1  # ONE bet, not three
    bet = bets[0]
    assert bet.is_combo is True
    assert bet.market == "Bet Builder"
    assert bet.match_desc == "USA v Australia"
    assert bet.bookmaker == "Betfair Sportsbook"
    assert bet.decimal_odds == pytest.approx(5.12)  # the COMBINED price
    assert bet.stake == pytest.approx(10.0)  # the SINGLE total stake
    assert bet.potential_returns == pytest.approx(41.20)
    assert bet.currency == "GBP"
    assert bet.is_free_bet is True
    # All three legs present in the joined selection and the notes, not as rows.
    for fragment in ("Over 3.5 Goals", "Over", "Folarin Balogun"):
        assert fragment in bet.selection
    assert "Folarin Balogun" in bet.notes
    assert bet.notes.startswith("legs:")


def test_multi_fixture_accumulator_is_one_row() -> None:
    """A genuine multi-fixture acca stays ONE row, market 'Accumulator'."""
    body_text = json.dumps(
        {
            "bets": [
                {
                    "bookmaker": "bet365",
                    "match": "England vs France | Spain vs Italy",
                    "market": "Accumulator",
                    "selection": None,
                    "is_combo": True,
                    "legs": [
                        {"market": "Match Result", "selection": "England"},
                        {"market": "Match Result", "selection": "Spain"},
                    ],
                    "odds_decimal": 4.0,
                    "stake": 20.0,
                    "currency": "GBP",
                    "returns": 80.0,
                    "status": "open",
                    "is_boost": False,
                    "is_free_bet": False,
                    "confidence": 0.85,
                }
            ]
        }
    )
    sess = _session_returning(body_text)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)

    assert len(bets) == 1
    assert bets[0].market == "Accumulator"
    assert bets[0].is_combo is True
    assert bets[0].decimal_odds == pytest.approx(4.0)
    assert bets[0].stake == pytest.approx(20.0)
    assert "England" in bets[0].selection and "Spain" in bets[0].selection


def test_single_leg_slip_unchanged_by_combo_path() -> None:
    """A single-selection slip still parses as one ordinary (non-combo) bet."""
    body_text = json.dumps(
        {
            "bets": [
                {
                    "bookmaker": "bet365",
                    "match": "Brazil vs Morocco",
                    "market": "Match Result",
                    "selection": "Brazil",
                    "is_combo": False,
                    "legs": [],
                    "odds_decimal": 1.8,
                    "stake": 25.0,
                    "currency": "GBP",
                    "returns": 45.0,
                    "status": "open",
                    "is_boost": False,
                    "is_free_bet": False,
                    "confidence": 0.95,
                }
            ]
        }
    )
    sess = _session_returning(body_text)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)

    assert len(bets) == 1
    assert bets[0].is_combo is False
    assert bets[0].market == "Match Result"
    assert bets[0].selection == "Brazil"
    assert bets[0].notes == ""


def test_combo_inferred_from_legs_without_flag() -> None:
    """Defensive: a 2+ leg ``legs`` array collapses even if is_combo is absent."""
    body_text = json.dumps(
        {
            "bets": [
                {
                    "bookmaker": "Betfair Sportsbook",
                    "match": "USA v Australia",
                    "market": None,
                    "legs": [
                        {"market": "Total Goals", "selection": "Over 3.5 Goals"},
                        {"market": "Anytime Goalscorer", "selection": "Folarin Balogun"},
                    ],
                    "odds_decimal": 5.12,
                    "stake": 10.0,
                    "returns": 51.20,
                    "confidence": 0.9,
                }
            ]
        }
    )
    sess = _session_returning(body_text)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)

    assert len(bets) == 1
    assert bets[0].is_combo is True
    # No market label + single fixture -> Bet Builder.
    assert bets[0].market == "Bet Builder"
    assert bets[0].stake == pytest.approx(10.0)


def test_leaked_legs_without_combo_flag_are_merged() -> None:
    """Fallback: model leaks legs as separate same-stake rows -> one acca row.

    Each leg carries its own price and the full stake; the product of the prices
    reproduces the returns, so _detect_accas collapses them rather than logging
    each leg at the full stake.
    """
    body_text = json.dumps(
        {
            "bets": [
                {"bookmaker": "bet365", "match": "A vs B", "market": "Result",
                 "selection": "A", "odds_decimal": 2.0, "stake": 10.0,
                 "returns": 60.0, "status": "open", "is_boost": False,
                 "confidence": 0.9},
                {"bookmaker": "bet365", "match": "C vs D", "market": "Result",
                 "selection": "C", "odds_decimal": 3.0, "stake": 10.0,
                 "returns": None, "status": "open", "is_boost": False,
                 "confidence": 0.9},
            ]
        }
    )
    sess = _session_returning(body_text)
    bets = extract_bets_from_image(b"img", api_key="k", session=sess)

    assert len(bets) == 1  # merged, not two £10 bets
    assert bets[0].market == "Accumulator"  # two distinct fixtures
    assert bets[0].decimal_odds == pytest.approx(6.0)  # 2.0 * 3.0
    assert bets[0].stake == pytest.approx(10.0)  # single stake, not doubled
    assert bets[0].is_combo is True


def test_prompt_instructs_combo_collapse() -> None:
    """The vision prompt must tell the model to collapse combos to one bet."""
    p = vision.PROMPT
    assert "Bet Builder" in p
    assert "is_combo" in p
    assert "legs" in p
    # The key money-safety instruction: one combined price + one total stake.
    assert "combined price" in p.lower()
    assert "total stake" in p.lower()
    # Betfair "O/<id>" hint and Bonus Used / money-back-as-free-bet.
    assert "O/" in p
    assert "Bonus Used" in p or "money-back" in p.lower()
