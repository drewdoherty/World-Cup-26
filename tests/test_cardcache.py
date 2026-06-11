"""Tests for wca.cardcache — file-backed matchday card cache.

All tests use tmp_path fixtures; no network, no model fit, no clock calls.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

from wca.cardcache import read_card, write_card


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _card_path(tmp_path: Path, name: str = "card_latest.md") -> str:
    return str(tmp_path / name)


# ---------------------------------------------------------------------------
# Round-trip: write then read
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_body_preserved(self, tmp_path: Path) -> None:
        path = _card_path(tmp_path)
        body = "*World Cup Alpha* — 3 picks\n\nMatch A vs B"
        write_card(body, path=path, ts_utc="2026-06-11T12:00:00")
        result = read_card(path=path)
        assert result is not None
        assert result["text"] == body

    def test_header_stripped(self, tmp_path: Path) -> None:
        """The returned 'text' must not include the HTML comment header line."""
        path = _card_path(tmp_path)
        write_card("hello", path=path, ts_utc="2026-06-11T10:00:00")
        result = read_card(path=path)
        assert result is not None
        assert "<!-- generated:" not in result["text"]

    def test_generated_parsed(self, tmp_path: Path) -> None:
        path = _card_path(tmp_path)
        write_card("body", path=path, ts_utc="2026-06-11T18:30:00")
        result = read_card(path=path)
        assert result is not None
        assert result["generated"] == "2026-06-11T18:30:00"

    def test_stale_false_by_default(self, tmp_path: Path) -> None:
        """stale defaults to False when now_utc / max_age_hours are not given."""
        path = _card_path(tmp_path)
        write_card("body", path=path, ts_utc="2026-06-11T00:00:00")
        result = read_card(path=path)
        assert result is not None
        assert result["stale"] is False

    def test_multiline_body(self, tmp_path: Path) -> None:
        path = _card_path(tmp_path)
        body = "line1\nline2\nline3"
        write_card(body, path=path, ts_utc="2026-06-11T08:00:00")
        result = read_card(path=path)
        assert result is not None
        assert result["text"] == body

    def test_empty_body(self, tmp_path: Path) -> None:
        path = _card_path(tmp_path)
        write_card("", path=path, ts_utc="2026-06-11T09:00:00")
        result = read_card(path=path)
        assert result is not None
        assert result["text"] == ""

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = str(tmp_path / "a" / "b" / "card.md")
        write_card("data", path=nested, ts_utc="2026-06-11T11:00:00")
        assert os.path.exists(nested)


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

class TestMissingFile:
    def test_returns_none(self, tmp_path: Path) -> None:
        path = str(tmp_path / "nonexistent.md")
        assert read_card(path=path) is None

    def test_returns_none_with_staleness_args(self, tmp_path: Path) -> None:
        path = str(tmp_path / "nonexistent.md")
        result = read_card(
            path=path,
            now_utc="2026-06-11T12:00:00",
            max_age_hours=2.0,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

class TestStaleness:
    """Pass fixed ISO strings; the cache module must never read the clock."""

    _GENERATED = "2026-06-11T10:00:00"   # card was written at 10:00 UTC

    def _write(self, tmp_path: Path) -> str:
        path = _card_path(tmp_path)
        write_card("card body", path=path, ts_utc=self._GENERATED)
        return path

    def test_not_stale_within_window(self, tmp_path: Path) -> None:
        path = self._write(tmp_path)
        # Card is 1 hour old; window is 2 hours → not stale.
        result = read_card(
            path=path,
            now_utc="2026-06-11T11:00:00",
            max_age_hours=2.0,
        )
        assert result is not None
        assert result["stale"] is False

    def test_stale_outside_window(self, tmp_path: Path) -> None:
        path = self._write(tmp_path)
        # Card is 3 hours old; window is 2 hours → stale.
        result = read_card(
            path=path,
            now_utc="2026-06-11T13:00:00",
            max_age_hours=2.0,
        )
        assert result is not None
        assert result["stale"] is True

    def test_exactly_at_boundary_not_stale(self, tmp_path: Path) -> None:
        path = self._write(tmp_path)
        # Card is exactly 2 hours old; window is 2 hours → NOT stale (strict >).
        result = read_card(
            path=path,
            now_utc="2026-06-11T12:00:00",
            max_age_hours=2.0,
        )
        assert result is not None
        assert result["stale"] is False

    def test_stale_false_when_now_utc_missing(self, tmp_path: Path) -> None:
        path = self._write(tmp_path)
        result = read_card(path=path, max_age_hours=2.0)
        assert result is not None
        assert result["stale"] is False

    def test_stale_false_when_max_age_missing(self, tmp_path: Path) -> None:
        path = self._write(tmp_path)
        result = read_card(path=path, now_utc="2026-06-11T13:00:00")
        assert result is not None
        assert result["stale"] is False


# ---------------------------------------------------------------------------
# Empty / missing timestamp in header
# ---------------------------------------------------------------------------

class TestEmptyTimestamp:
    def test_generated_is_none(self, tmp_path: Path) -> None:
        """write_card(ts_utc=None) → generated == None in read result."""
        path = _card_path(tmp_path)
        write_card("some body", path=path, ts_utc=None)
        result = read_card(path=path)
        assert result is not None
        assert result["generated"] is None

    def test_stale_false_when_generated_none(self, tmp_path: Path) -> None:
        path = _card_path(tmp_path)
        write_card("some body", path=path, ts_utc=None)
        result = read_card(
            path=path,
            now_utc="2026-06-11T13:00:00",
            max_age_hours=1.0,
        )
        assert result is not None
        assert result["stale"] is False

    def test_body_still_round_trips(self, tmp_path: Path) -> None:
        path = _card_path(tmp_path)
        body = "no timestamp body"
        write_card(body, path=path, ts_utc=None)
        result = read_card(path=path)
        assert result is not None
        assert result["text"] == body


# ---------------------------------------------------------------------------
# Unparseable timestamp — treat as not-stale rather than crash
# ---------------------------------------------------------------------------

class TestUnparseableTimestamp:
    def test_stale_false_on_bad_generated(self, tmp_path: Path) -> None:
        """Manually write a file with a garbage timestamp."""
        path = _card_path(tmp_path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<!-- generated: not-a-date -->\nbody text")
        result = read_card(
            path=path,
            now_utc="2026-06-11T13:00:00",
            max_age_hours=1.0,
        )
        assert result is not None
        assert result["stale"] is False

    def test_stale_false_on_bad_now_utc(self, tmp_path: Path) -> None:
        path = _card_path(tmp_path)
        write_card("body", path=path, ts_utc="2026-06-11T10:00:00")
        result = read_card(
            path=path,
            now_utc="not-a-date",
            max_age_hours=1.0,
        )
        assert result is not None
        assert result["stale"] is False
