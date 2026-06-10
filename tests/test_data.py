"""Tests for wca.data modules.

All network calls are mocked via monkeypatch on requests.get — no live
network access is required or performed during pytest.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_response(
    json_data: Any,
    status_code: int = 200,
    headers: Dict[str, str] | None = None,
) -> MagicMock:
    """Build a minimal mock that looks like a requests.Response."""
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_data
    m.headers = headers or {}
    m.raise_for_status = MagicMock()
    m.content = json.dumps(json_data).encode()
    return m


# ---------------------------------------------------------------------------
# wca.data.polymarket
# ---------------------------------------------------------------------------

class TestPolymarketParsePrices:
    """Test the JSON-string-encoded outcomes/outcomePrices decoding."""

    def test_string_encoded_fields_are_decoded(self) -> None:
        from wca.data.polymarket import _parse_market_prices

        market = {
            "id": "abc",
            "question": "Will Brazil win?",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.72","0.28"]',
        }
        result = _parse_market_prices(market)
        assert result["outcomes"] == ["Yes", "No"]
        assert result["outcomePrices"] == ["0.72", "0.28"]
        assert result["priceMap"] == {"Yes": 0.72, "No": 0.28}

    def test_already_parsed_lists_are_preserved(self) -> None:
        from wca.data.polymarket import _parse_market_prices

        market = {
            "id": "xyz",
            "outcomes": ["A", "B", "C"],
            "outcomePrices": ["0.5", "0.3", "0.2"],
        }
        result = _parse_market_prices(market)
        assert result["outcomes"] == ["A", "B", "C"]
        assert abs(result["priceMap"]["A"] - 0.5) < 1e-9

    def test_missing_fields_do_not_raise(self) -> None:
        from wca.data.polymarket import _parse_market_prices

        result = _parse_market_prices({"id": "empty"})
        assert "priceMap" not in result or result.get("priceMap") == {}


class TestPolymarketSearchEvents:
    """Test search_events with mocked HTTP."""

    def _sample_events(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "1",
                "slug": "wc-brazil-win",
                "title": "Will Brazil win the 2026 World Cup?",
                "markets": [
                    {
                        "id": "m1",
                        "question": "Yes/No",
                        "outcomes": '["Yes","No"]',
                        "outcomePrices": '["0.30","0.70"]',
                    }
                ],
            }
        ]

    def test_returns_list_with_decoded_markets(self, monkeypatch: Any) -> None:
        from wca.data import polymarket

        monkeypatch.setattr(
            polymarket.requests, "get",
            lambda *a, **kw: _make_response(self._sample_events()),
        )
        events = polymarket.search_events("World Cup")
        assert len(events) == 1
        mkt = events[0]["markets"][0]
        assert mkt["outcomes"] == ["Yes", "No"]
        assert mkt["priceMap"]["Yes"] == pytest.approx(0.30)

    def test_dict_wrapped_response(self, monkeypatch: Any) -> None:
        """Gamma sometimes wraps results in a ``data`` key."""
        from wca.data import polymarket

        monkeypatch.setattr(
            polymarket.requests, "get",
            lambda *a, **kw: _make_response({"data": self._sample_events()}),
        )
        events = polymarket.search_events("World Cup")
        assert len(events) == 1


class TestPolymarketGetEvent:
    def test_get_event_decodes_markets(self, monkeypatch: Any) -> None:
        from wca.data import polymarket

        payload = {
            "id": "99",
            "title": "Test",
            "markets": [
                {
                    "id": "m99",
                    "outcomes": '["Home","Away","Draw"]',
                    "outcomePrices": '["0.40","0.35","0.25"]',
                }
            ],
        }
        monkeypatch.setattr(
            polymarket.requests, "get",
            lambda *a, **kw: _make_response(payload),
        )
        event = polymarket.get_event("99")
        assert event["id"] == "99"
        mkt = event["markets"][0]
        assert mkt["priceMap"]["Home"] == pytest.approx(0.40)


class TestFindWorldCupMarkets:
    def test_filters_to_world_cup_only_and_deduplicates(self, monkeypatch: Any) -> None:
        from wca.data import polymarket

        # First page: two WC events + one non-WC
        # Second page: duplicate WC event + empty to end pagination
        page1 = [
            {"id": "1", "slug": "wc-group-a", "title": "World Cup Group A Winner", "markets": []},
            {"id": "2", "slug": "wc-group-b", "title": "FIFA World Cup Group B", "markets": []},
            {"id": "3", "slug": "epl-thing", "title": "EPL: Arsenal vs Chelsea", "markets": []},
        ]
        page2 = [
            {"id": "1", "slug": "wc-group-a", "title": "World Cup Group A Winner", "markets": []},  # dup
        ]
        # page3 empty -> stop
        page3: list = []

        call_count = [0]

        def mock_get(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(page1)
            elif call_count[0] == 2:
                return _make_response(page2)
            return _make_response(page3)

        monkeypatch.setattr(polymarket.requests, "get", mock_get)
        markets = polymarket.find_world_cup_markets()

        ids = [m["id"] for m in markets]
        # Only WC events, no duplicates
        assert "1" in ids
        assert "2" in ids
        assert "3" not in ids  # non-WC filtered out
        assert len(ids) == len(set(ids)), "Duplicates found"


# ---------------------------------------------------------------------------
# wca.data.theoddsapi
# ---------------------------------------------------------------------------

_SAMPLE_ODDS_RESPONSE = [
    {
        "id": "evt1",
        "sport_key": "soccer_fifa_world_cup",
        "commence_time": "2026-06-14T12:00:00Z",
        "home_team": "Brazil",
        "away_team": "Mexico",
        "bookmakers": [
            {
                "key": "paddypower",
                "title": "Paddy Power",
                "last_update": "2026-06-13T20:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Brazil", "price": 2.10},
                            {"name": "Mexico", "price": 3.50},
                            {"name": "Draw", "price": 3.20},
                        ],
                    }
                ],
            }
        ],
    }
]


class TestOddsApiGetOdds:
    def test_dataframe_shape_and_columns(self, monkeypatch: Any) -> None:
        from wca.data import theoddsapi

        monkeypatch.setenv("ODDS_API_KEY", "testkey")
        resp = _make_response(
            _SAMPLE_ODDS_RESPONSE,
            headers={"x-requests-remaining": "499", "x-requests-used": "1"},
        )
        monkeypatch.setattr(theoddsapi.requests, "get", lambda *a, **kw: resp)

        df, quota = theoddsapi.get_odds("soccer_fifa_world_cup")

        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        assert "decimal_odds" in df.columns
        assert "bookmaker_key" in df.columns
        assert "market" in df.columns
        assert "outcome_name" in df.columns
        # One bookmaker x 3 outcomes = 3 rows
        assert len(df) == 3

    def test_quota_headers_extracted(self, monkeypatch: Any) -> None:
        from wca.data import theoddsapi

        monkeypatch.setenv("ODDS_API_KEY", "testkey")
        resp = _make_response(
            _SAMPLE_ODDS_RESPONSE,
            headers={"x-requests-remaining": "450", "x-requests-used": "50"},
        )
        monkeypatch.setattr(theoddsapi.requests, "get", lambda *a, **kw: resp)

        _, quota = theoddsapi.get_odds("soccer_fifa_world_cup")
        assert quota.remaining == 450
        assert quota.used == 50

    def test_missing_quota_headers_are_none(self, monkeypatch: Any) -> None:
        from wca.data import theoddsapi

        monkeypatch.setenv("ODDS_API_KEY", "testkey")
        resp = _make_response(_SAMPLE_ODDS_RESPONSE, headers={})
        monkeypatch.setattr(theoddsapi.requests, "get", lambda *a, **kw: resp)

        _, quota = theoddsapi.get_odds("soccer_fifa_world_cup")
        assert quota.remaining is None
        assert quota.used is None

    def test_empty_response_gives_empty_dataframe(self, monkeypatch: Any) -> None:
        from wca.data import theoddsapi

        monkeypatch.setenv("ODDS_API_KEY", "testkey")
        resp = _make_response([], headers={})
        monkeypatch.setattr(theoddsapi.requests, "get", lambda *a, **kw: resp)

        df, _ = theoddsapi.get_odds("soccer_fifa_world_cup")
        assert df.empty

    def test_missing_api_key_raises(self, monkeypatch: Any) -> None:
        from wca.data import theoddsapi

        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        with pytest.raises(EnvironmentError):
            theoddsapi.get_odds("soccer_fifa_world_cup")

    def test_decimal_odds_values(self, monkeypatch: Any) -> None:
        from wca.data import theoddsapi

        monkeypatch.setenv("ODDS_API_KEY", "testkey")
        resp = _make_response(_SAMPLE_ODDS_RESPONSE, headers={})
        monkeypatch.setattr(theoddsapi.requests, "get", lambda *a, **kw: resp)

        df, _ = theoddsapi.get_odds("soccer_fifa_world_cup")
        odds = sorted(df["decimal_odds"].tolist())
        assert odds == pytest.approx([2.10, 3.20, 3.50])


class TestOddsApiListSports:
    def test_returns_list_and_quota(self, monkeypatch: Any) -> None:
        from wca.data import theoddsapi

        monkeypatch.setenv("ODDS_API_KEY", "testkey")
        sports_payload = [{"key": "soccer_fifa_world_cup", "title": "FIFA World Cup"}]
        resp = _make_response(
            sports_payload,
            headers={"x-requests-remaining": "499", "x-requests-used": "1"},
        )
        monkeypatch.setattr(theoddsapi.requests, "get", lambda *a, **kw: resp)

        sports, quota = theoddsapi.list_sports()
        assert isinstance(sports, list)
        assert sports[0]["key"] == "soccer_fifa_world_cup"
        assert quota.remaining == 499


# ---------------------------------------------------------------------------
# wca.data.results
# ---------------------------------------------------------------------------

_RESULTS_CSV_FIXTURE = textwrap.dedent(
    """\
    date,home_team,away_team,home_score,away_score,tournament,city,country,neutral
    2010-06-11,South Africa,Mexico,1,1,FIFA World Cup,Johannesburg,South Africa,False
    2022-11-20,Qatar,Ecuador,0,2,FIFA World Cup,Al Khor,Qatar,False
    2026-06-14,Brazil,Mexico,2,0,FIFA World Cup,Los Angeles,USA,False
    2026-06-14,Germany,France,1,1,FIFA World Cup,New York,USA,True
    """
)


class TestResultsLoadResults:
    def test_dtypes_are_correct(self, tmp_path: Path) -> None:
        from wca.data import results

        csv_path = tmp_path / "results.csv"
        csv_path.write_text(_RESULTS_CSV_FIXTURE)
        df = results.load_results(csv_path)

        assert pd.api.types.is_datetime64_any_dtype(df["date"]), "date should be datetime"
        assert df["neutral"].dtype == bool
        assert df["home_score"].dtype.name in ("Int64", "int64")
        assert len(df) == 4

    def test_boolean_parsing(self, tmp_path: Path) -> None:
        from wca.data import results

        csv_path = tmp_path / "results.csv"
        csv_path.write_text(_RESULTS_CSV_FIXTURE)
        df = results.load_results(csv_path)

        # Last row has neutral=True
        assert df.loc[df["away_team"] == "France", "neutral"].iloc[0] == True  # noqa: E712
        # First row has neutral=False
        assert df.loc[df["away_team"] == "Mexico", "neutral"].iloc[0] == False  # noqa: E712

    def test_load_from_string_io(self) -> None:
        from wca.data import results

        df = results.load_results(io.StringIO(_RESULTS_CSV_FIXTURE))
        assert len(df) == 4


class TestResultsFilterSince:
    def test_filter_returns_subset(self, tmp_path: Path) -> None:
        from wca.data import results

        csv_path = tmp_path / "results.csv"
        csv_path.write_text(_RESULTS_CSV_FIXTURE)
        df = results.load_results(csv_path)

        filtered = results.filter_since(df, "2026-01-01")
        assert len(filtered) == 2
        assert (filtered["date"].dt.year == 2026).all()

    def test_original_not_mutated(self, tmp_path: Path) -> None:
        from wca.data import results

        csv_path = tmp_path / "results.csv"
        csv_path.write_text(_RESULTS_CSV_FIXTURE)
        df = results.load_results(csv_path)
        _ = results.filter_since(df, "2022-01-01")
        assert len(df) == 4  # original unchanged


class TestResultsAddOutcome:
    def test_outcome_values(self, tmp_path: Path) -> None:
        from wca.data import results

        csv_path = tmp_path / "results.csv"
        csv_path.write_text(_RESULTS_CSV_FIXTURE)
        df = results.load_results(csv_path)
        df2 = results.add_outcome_column(df)

        # Brazil 2-0 Mexico -> H
        row_bra = df2[df2["home_team"] == "Brazil"].iloc[0]
        assert row_bra["outcome"] == "H"
        # Qatar 0-2 Ecuador -> A
        row_qat = df2[df2["home_team"] == "Qatar"].iloc[0]
        assert row_qat["outcome"] == "A"
        # South Africa 1-1 Mexico -> D
        row_sa = df2[df2["home_team"] == "South Africa"].iloc[0]
        assert row_sa["outcome"] == "D"

    def test_does_not_mutate_original(self, tmp_path: Path) -> None:
        from wca.data import results

        csv_path = tmp_path / "results.csv"
        csv_path.write_text(_RESULTS_CSV_FIXTURE)
        df = results.load_results(csv_path)
        _ = results.add_outcome_column(df)
        assert "outcome" not in df.columns


class TestResultsDownload:
    def test_downloads_when_missing(self, tmp_path: Path, monkeypatch: Any) -> None:
        from wca.data import results

        content = _RESULTS_CSV_FIXTURE.encode()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = content
        monkeypatch.setattr(results.requests, "get", lambda *a, **kw: resp)

        dest = tmp_path / "raw" / "results.csv"
        path = results.download_results(dest=str(dest), url="http://fake/results.csv")
        assert path.exists()
        assert path.read_bytes() == content

    def test_skips_when_fresh(self, tmp_path: Path, monkeypatch: Any) -> None:
        """If mtime == today, download should be skipped (get not called)."""
        from wca.data import results
        import time

        dest = tmp_path / "results.csv"
        dest.write_bytes(b"existing")

        get_called = [False]

        def mock_get(*a, **kw):
            get_called[0] = True
            return MagicMock()

        monkeypatch.setattr(results.requests, "get", mock_get)
        results.download_results(dest=str(dest), url="http://fake/results.csv")
        assert not get_called[0], "requests.get should not be called for fresh file"

    def test_force_re_downloads(self, tmp_path: Path, monkeypatch: Any) -> None:
        from wca.data import results

        dest = tmp_path / "results.csv"
        dest.write_bytes(b"old")

        new_content = b"new content"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = new_content
        monkeypatch.setattr(results.requests, "get", lambda *a, **kw: resp)

        results.download_results(dest=str(dest), url="http://fake/results.csv", force=True)
        assert dest.read_bytes() == new_content


# ---------------------------------------------------------------------------
# wca.data.snapshot
# ---------------------------------------------------------------------------

class TestSnapshotTableCreation:
    def test_creates_table_if_missing(self, tmp_path: Path) -> None:
        from wca.data import snapshot

        db = tmp_path / "test.db"
        n = snapshot.snapshot_all(db_path=str(db), sources={})
        assert n == 0
        assert db.exists()

        with sqlite3.connect(str(db)) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]
        assert "odds_snapshots" in table_names

    def test_schema_columns(self, tmp_path: Path) -> None:
        from wca.data import snapshot

        db = tmp_path / "test.db"
        snapshot.snapshot_all(db_path=str(db), sources={})

        with sqlite3.connect(str(db)) as conn:
            cur = conn.execute("PRAGMA table_info(odds_snapshots)")
            cols = {row[1] for row in cur.fetchall()}

        required = {"ts_utc", "source", "match_id", "market", "selection",
                    "decimal_odds", "raw"}
        assert required == cols


class TestSnapshotAppend:
    def _make_source(self, rows: List["snapshot.SnapshotRow"]):  # type: ignore[name-defined]
        def _fn():
            return rows
        return _fn

    def test_rows_are_inserted(self, tmp_path: Path) -> None:
        from wca.data import snapshot
        from wca.data.snapshot import SnapshotRow

        db = tmp_path / "wca.db"
        rows = [
            SnapshotRow(
                source="polymarket",
                match_id="evt42",
                market="winner",
                selection="Brazil",
                decimal_odds=3.10,
                raw={"q": "Will Brazil win?"},
            ),
            SnapshotRow(
                source="polymarket",
                match_id="evt42",
                market="winner",
                selection="Not Brazil",
                decimal_odds=1.45,
                raw={"q": "Will Brazil win?"},
            ),
        ]
        n = snapshot.snapshot_all(
            db_path=str(db),
            sources={"polymarket": self._make_source(rows)},
        )
        assert n == 2

        with sqlite3.connect(str(db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM odds_snapshots"
            ).fetchone()[0]
        assert count == 2

    def test_multiple_sources(self, tmp_path: Path) -> None:
        from wca.data import snapshot
        from wca.data.snapshot import SnapshotRow

        db = tmp_path / "wca.db"

        def src_a():
            return [SnapshotRow("a", "e1", "h2h", "Home", 1.9, {})]

        def src_b():
            return [
                SnapshotRow("b", "e1", "h2h", "Home", 1.85, {}),
                SnapshotRow("b", "e1", "h2h", "Away", 2.10, {}),
            ]

        n = snapshot.snapshot_all(
            db_path=str(db),
            sources={"src_a": src_a, "src_b": src_b},
        )
        assert n == 3

    def test_raw_serialised_as_json(self, tmp_path: Path) -> None:
        from wca.data import snapshot
        from wca.data.snapshot import SnapshotRow

        db = tmp_path / "wca.db"
        payload = {"question": "Win?", "price": 0.6}
        rows = [
            SnapshotRow("polymarket", "evt1", "winner", "Yes", 1.67, payload)
        ]
        snapshot.snapshot_all(
            db_path=str(db),
            sources={"pm": lambda: rows},
        )
        with sqlite3.connect(str(db)) as conn:
            raw_str = conn.execute(
                "SELECT raw FROM odds_snapshots LIMIT 1"
            ).fetchone()[0]
        parsed = json.loads(raw_str)
        assert parsed["price"] == pytest.approx(0.6)

    def test_source_exception_does_not_abort_others(self, tmp_path: Path) -> None:
        from wca.data import snapshot
        from wca.data.snapshot import SnapshotRow

        db = tmp_path / "wca.db"

        def bad_source():
            raise RuntimeError("intentional failure")

        def good_source():
            return [SnapshotRow("good", "e1", "h2h", "Home", 1.9, {})]

        n = snapshot.snapshot_all(
            db_path=str(db),
            sources={"bad": bad_source, "good": good_source},
        )
        assert n == 1

    def test_snapshot_row_ts_defaults_to_now(self) -> None:
        from wca.data.snapshot import SnapshotRow

        row = SnapshotRow("src", "id", "mkt", "sel", 2.0, {})
        assert row.ts_utc is not None
        # Should be parseable as ISO
        from datetime import datetime
        dt = datetime.fromisoformat(row.ts_utc.replace("Z", "+00:00"))
        assert dt.year >= 2026

    def test_read_snapshots_returns_dicts(self, tmp_path: Path) -> None:
        from wca.data import snapshot
        from wca.data.snapshot import SnapshotRow

        db = tmp_path / "wca.db"
        rows = [SnapshotRow("pm", "e1", "win", "Brazil", 3.0, {"x": 1})]
        snapshot.snapshot_all(db_path=str(db), sources={"pm": lambda: rows})

        data = snapshot.read_snapshots(db_path=str(db))
        assert len(data) == 1
        assert data[0]["source"] == "pm"
        assert data[0]["decimal_odds"] == pytest.approx(3.0)
