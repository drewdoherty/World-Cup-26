"""Tests for the odds-source orchestrator and its Betfair/Polymarket sources.

These cover the graceful-degradation contract that keeps /card, /next and
/scores from going stale when the upstream odds key is revoked:

* the orchestrator falls through dead/empty sources and never raises;
* Betfair is OFF (empty frame, not an error) until creds are configured;
* Polymarket share prices map cleanly to a 1/price decimal-odds frame.
"""
from __future__ import annotations

import pandas as pd
import pytest

from wca.data import betfair_exchange, odds_source, polymarket_odds


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_COLS = list(odds_source._COLUMNS)


def _frame(rows):
    return pd.DataFrame(rows, columns=_COLS)


def test_order_default_and_override(monkeypatch):
    monkeypatch.delenv("WCA_ODDS_SOURCES", raising=False)
    assert odds_source._order() == ["betfair", "theoddsapi", "polymarket"]
    monkeypatch.setenv("WCA_ODDS_SOURCES", "polymarket, theoddsapi")
    assert odds_source._order() == ["polymarket", "theoddsapi"]


def test_falls_through_to_first_nonempty(monkeypatch):
    """Betfair empty + Odds API raises -> Polymarket rows are returned."""
    monkeypatch.delenv("WCA_ODDS_SOURCES", raising=False)
    monkeypatch.setattr(betfair_exchange, "get_odds",
                        lambda *a, **k: (_frame([]), None))

    def _boom(*a, **k):
        raise RuntimeError("401 revoked key")

    monkeypatch.setattr(odds_source.theoddsapi, "get_odds", _boom)
    pm_row = {c: None for c in _COLS}
    pm_row.update({"bookmaker_key": "polymarket", "decimal_odds": 1.6,
                   "market": "h2h", "outcome_name": "Germany"})
    monkeypatch.setattr(polymarket_odds, "get_odds",
                        lambda *a, **k: (_frame([pm_row]), None))

    df, quota = odds_source.get_odds("soccer_fifa_world_cup")
    assert not df.empty
    assert df.iloc[0]["bookmaker_key"] == "polymarket"


def test_all_empty_returns_empty_frame_never_raises(monkeypatch):
    monkeypatch.delenv("WCA_ODDS_SOURCES", raising=False)
    for mod in (betfair_exchange, polymarket_odds):
        monkeypatch.setattr(mod, "get_odds", lambda *a, **k: (_frame([]), None))
    monkeypatch.setattr(odds_source.theoddsapi, "get_odds",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

    df, quota = odds_source.get_odds("soccer_fifa_world_cup")
    assert df.empty
    assert list(df.columns) == _COLS  # correctly-shaped, so downstream is safe
    assert quota is None


def _fix_rows(book, home, away, n=1):
    out = []
    for i in range(n):
        r = {c: None for c in _COLS}
        r.update({"bookmaker_key": book, "home_team": home, "away_team": away,
                  "market": "h2h", "outcome_name": f"{home}|{i}", "decimal_odds": 2.0})
        out.append(r)
    return out


def test_merge_gap_fills_without_duplicating_fixtures(monkeypatch):
    """WCA_ODDS_MERGE: keep Betfair's fixture, add Polymarket-only fixtures."""
    monkeypatch.delenv("WCA_ODDS_SOURCES", raising=False)
    monkeypatch.setenv("WCA_ODDS_MERGE", "1")
    # Betfair covers Ivory Coast v Curacao (sharp).
    monkeypatch.setattr(betfair_exchange, "get_odds",
                        lambda *a, **k: (_frame(_fix_rows("betfair_ex", "Ivory Coast", "Curacao")), None))
    monkeypatch.setattr(odds_source.theoddsapi, "get_odds",
                        lambda *a, **k: (_frame([]), None))
    # Polymarket re-covers the same fixture (diff spelling) + a NEW one.
    pm = _fix_rows("polymarket", "Cote d'Ivoire", "Curacao") + _fix_rows("polymarket", "Spain", "Uruguay")
    monkeypatch.setattr(polymarket_odds, "get_odds", lambda *a, **k: (_frame(pm), None))

    df, _ = odds_source.get_odds("soccer_fifa_world_cup")
    books_by_fixture = df.groupby(["home_team"])["bookmaker_key"].agg(set).to_dict()
    # The dual-covered fixture keeps ONLY Betfair (no Polymarket dup).
    assert books_by_fixture.get("Ivory Coast") == {"betfair_ex"}
    assert "Cote d'Ivoire" not in books_by_fixture  # Polymarket dup dropped
    # The Polymarket-only fixture is gap-filled in.
    assert books_by_fixture.get("Spain") == {"polymarket"}


def test_first_source_with_rows_wins(monkeypatch):
    monkeypatch.delenv("WCA_ODDS_MERGE", raising=False)
    monkeypatch.delenv("WCA_ODDS_SOURCES", raising=False)
    bf_row = {c: None for c in _COLS}
    bf_row.update({"bookmaker_key": "betfair_ex", "decimal_odds": 1.5,
                   "market": "h2h", "outcome_name": "Germany"})
    monkeypatch.setattr(betfair_exchange, "get_odds",
                        lambda *a, **k: (_frame([bf_row]), None))
    # Later sources must NOT be consulted once Betfair has rows.
    monkeypatch.setattr(odds_source.theoddsapi, "get_odds",
                        lambda *a, **k: pytest.fail("theoddsapi should not be called"))
    df, _ = odds_source.get_odds("soccer_fifa_world_cup")
    assert df.iloc[0]["bookmaker_key"] == "betfair_ex"


# ---------------------------------------------------------------------------
# Polymarket -> odds adapter
# ---------------------------------------------------------------------------

def _pm_market(git, question, bid, ask):
    return {
        "groupItemTitle": git,
        "question": question,
        "clobTokenIds": '["111","222"]',
        "outcomes": '["Yes","No"]',
        "bestBid": bid,
        "bestAsk": ask,
    }


def _pm_event():
    # Mirrors the real "Ecuador vs. Germany" single-match event shape.
    return {
        "id": "evt1",
        "slug": "fifwc-ecu-ger-2026-06-25",
        "title": "Ecuador vs. Germany",
        "endDate": "2026-06-25T20:00:00Z",
        "markets": [
            _pm_market("Draw (Ecuador vs. Germany)",
                       "Will Ecuador vs. Germany end in a draw?", 0.197, 0.198),
            _pm_market("Germany", "Will Germany win on 2026-06-25?", 0.622, 0.623),
            _pm_market("Ecuador", "Will Ecuador win on 2026-06-25?", 0.184, 0.185),
        ],
    }


def test_polymarket_rows_from_events_maps_1x2():
    rows = polymarket_odds.rows_from_events([_pm_event()])
    by_outcome = {r["outcome_name"]: r for r in rows}
    assert set(by_outcome) == {"Ecuador", "Germany", "Draw"}
    # Home/away taken from the title order.
    assert all(r["home_team"] == "Ecuador" and r["away_team"] == "Germany"
               for r in rows)
    assert all(r["bookmaker_key"] == "polymarket" and r["market"] == "h2h"
               for r in rows)
    # Decimal odds = 1 / mid(bestBid,bestAsk). Germany mid ~0.6225 -> ~1.606.
    assert by_outcome["Germany"]["decimal_odds"] == pytest.approx(1.606, abs=0.01)
    assert by_outcome["Ecuador"]["decimal_odds"] > by_outcome["Germany"]["decimal_odds"]


def test_polymarket_skips_non_fixture_events():
    outright = {"id": "o1", "title": "World Cup Winner", "markets": []}
    assert polymarket_odds.rows_from_events([outright]) == []


def test_polymarket_get_odds_injected_events_no_network():
    df, quota = polymarket_odds.get_odds(events=[_pm_event()])
    assert not df.empty
    assert quota is None
    assert set(df["outcome_name"]) == {"Ecuador", "Germany", "Draw"}


# ---------------------------------------------------------------------------
# Betfair Exchange — creds gate + parser
# ---------------------------------------------------------------------------

_ALL_BF_VARS = (
    "BETFAIR_APP_KEY", "BETFAIR_APP_KEY_LIVE", "BETFAIR_APP_KEY_DELAYED",
    "BETFAIR_SESSION_TOKEN", "BETFAIR_USERNAME", "BETFAIR_PASSWORD",
    "BETFAIR_CERT_PATH", "BETFAIR_CERT_KEY_PATH",
)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_betfair_missing_creds_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(betfair_exchange, "_CACHED_TOKEN", None)
    # Point the disk session cache at an empty tmp dir so a real cached token
    # (if one exists on this machine) cannot mask the "no creds" condition.
    monkeypatch.setattr(betfair_exchange, "_SESSION_CACHE_PATH",
                        str(tmp_path / ".betfair_session.json"))
    for var in _ALL_BF_VARS:
        monkeypatch.delenv(var, raising=False)
    assert betfair_exchange.creds_available() is False
    missing = betfair_exchange.missing_creds()
    assert "BETFAIR_APP_KEY" in missing
    assert any("SESSION_TOKEN" in m for m in missing)


def test_betfair_interactive_login_with_username_password(monkeypatch, tmp_path):
    """Username+password (no cert) mints a token via the interactive endpoint."""
    monkeypatch.setattr(betfair_exchange, "_CACHED_TOKEN", None)
    # Isolate the on-disk session cache so the mint path is exercised (and we
    # never write a token into the real data/ dir).
    monkeypatch.setattr(betfair_exchange, "_SESSION_CACHE_PATH",
                        str(tmp_path / ".betfair_session.json"))
    for var in _ALL_BF_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BETFAIR_APP_KEY_DELAYED", "appkey123")
    monkeypatch.setenv("BETFAIR_APP_KEY_PREFER", "delayed")
    monkeypatch.setenv("BETFAIR_USERNAME", "user")
    monkeypatch.setenv("BETFAIR_PASSWORD", "pass")

    calls = {}

    def _fake_post(url, **kwargs):
        calls["url"] = url
        return _FakeResp({"status": "SUCCESS", "token": "TKN-XYZ", "error": ""})

    monkeypatch.setattr(betfair_exchange.requests, "post", _fake_post)
    assert betfair_exchange._resolve_session_token() == "TKN-XYZ"
    assert calls["url"] == betfair_exchange._INTERACTIVE_LOGIN_URL
    assert betfair_exchange.creds_available() is True


def test_betfair_get_odds_empty_without_creds(monkeypatch):
    monkeypatch.setattr(betfair_exchange, "_CACHED_TOKEN", None)
    for var in _ALL_BF_VARS:
        monkeypatch.delenv(var, raising=False)
    df, quota = betfair_exchange.get_odds("soccer_fifa_world_cup")
    assert df.empty
    assert list(df.columns) == list(betfair_exchange._COLUMNS)
    assert quota is None


def test_betfair_parse_market_book_maps_runners():
    catalogue = [{
        "marketId": "1.1",
        "marketStartTime": "2026-06-25T20:00:00Z",
        "event": {"id": "e1", "name": "Ecuador v Germany",
                  "openDate": "2026-06-25T20:00:00Z"},
        "runners": [
            {"selectionId": 1, "runnerName": "Ecuador"},
            {"selectionId": 2, "runnerName": "Germany"},
            {"selectionId": 3, "runnerName": "The Draw"},
        ],
    }]
    books = [{
        "marketId": "1.1",
        "runners": [
            {"selectionId": 1, "ex": {"availableToBack": [{"price": 5.4}]}},
            {"selectionId": 2, "ex": {"availableToBack": [{"price": 1.6}]}},
            {"selectionId": 3, "ex": {"availableToBack": [{"price": 4.2}]}},
        ],
    }]
    df = betfair_exchange.parse_market_book(catalogue, books)
    by_outcome = {r["outcome_name"]: r["decimal_odds"] for _, r in df.iterrows()}
    assert by_outcome == {"Ecuador": 5.4, "Germany": 1.6, "Draw": 4.2}
    assert set(df["home_team"]) == {"Ecuador"}
    assert set(df["away_team"]) == {"Germany"}
    assert set(df["bookmaker_key"]) == {"betfair_ex"}
