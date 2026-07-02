"""Tests for the odds-snapshot staleness guard (F4).

Covers the shared :mod:`wca.snapshot_freshness` helper (fresh / stale /
unparseable) and its wiring into ``wca.accas._load_snapshot_derivatives`` so a
stale latest snapshot is skipped rather than silently priced off for EV.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from wca import accas
from wca.snapshot_freshness import (
    DEFAULT_MAX_AGE_HOURS,
    check_snapshot_freshness,
    parse_snapshot_ts,
)

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def test_parse_microsecond_offset_form():
    dt = parse_snapshot_ts("2026-06-23T06:52:27.484258+00:00")
    assert dt == datetime(2026, 6, 23, 6, 52, 27, 484258, tzinfo=timezone.utc)


def test_parse_z_form():
    dt = parse_snapshot_ts("2026-06-23T06:52:27Z")
    assert dt == datetime(2026, 6, 23, 6, 52, 27, tzinfo=timezone.utc)


def test_parse_bare_naive_assumed_utc():
    dt = parse_snapshot_ts("2026-06-23T06:52:27")
    assert dt == datetime(2026, 6, 23, 6, 52, 27, tzinfo=timezone.utc)


def test_parse_empty_and_garbage_is_none():
    assert parse_snapshot_ts(None) is None
    assert parse_snapshot_ts("") is None
    assert parse_snapshot_ts("not-a-timestamp") is None


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------

def test_fresh_snapshot_not_stale():
    ts = (NOW - timedelta(hours=1)).isoformat()
    r = check_snapshot_freshness(ts, now=NOW, max_age_hours=6.0)
    assert r.is_stale is False
    assert r.age_hours == pytest.approx(1.0, abs=1e-6)


def test_stale_snapshot_flagged_and_logs(caplog):
    ts = (NOW - timedelta(hours=10)).isoformat()
    with caplog.at_level(logging.WARNING, logger="wca.snapshot_freshness"):
        r = check_snapshot_freshness(ts, now=NOW, max_age_hours=6.0, context="unit")
    assert r.is_stale is True
    assert r.age_hours == pytest.approx(10.0, abs=1e-6)
    assert any("STALE" in rec.message for rec in caplog.records)


def test_boundary_at_threshold_is_fresh():
    ts = (NOW - timedelta(hours=6.0)).isoformat()
    r = check_snapshot_freshness(ts, now=NOW, max_age_hours=6.0)
    assert r.is_stale is False  # exactly at threshold is not "older than"


def test_unparseable_ts_is_failsafe_stale(caplog):
    with caplog.at_level(logging.WARNING, logger="wca.snapshot_freshness"):
        r = check_snapshot_freshness("garbage", now=NOW)
    assert r.is_stale is True
    assert r.age_hours is None


def test_default_threshold_constant_is_positive():
    assert DEFAULT_MAX_AGE_HOURS > 0


# ---------------------------------------------------------------------------
# Wiring into accas._load_snapshot_derivatives
# ---------------------------------------------------------------------------

def _make_db(path, ts_utc):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE odds_snapshots ("
        "ts_utc TEXT, source TEXT, match_id TEXT, market TEXT, "
        "selection TEXT, decimal_odds REAL, raw TEXT)"
    )
    raw = '{"home_team": "Brazil", "away_team": "Morocco", "bookmaker": "smarkets"}'
    con.execute(
        "INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)",
        (ts_utc, "smarkets", "ev1", "totals", "Over 2.5", 1.9, raw),
    )
    con.commit()
    con.close()


def test_derivatives_returned_when_fresh(tmp_path):
    db = str(tmp_path / "fresh.db")
    fresh_ts = (NOW - timedelta(hours=1)).isoformat()
    _make_db(db, fresh_ts)
    out = accas._load_snapshot_derivatives(db, now=NOW, max_age_hours=6.0)
    assert out, "fresh snapshot should yield derivative prices"
    # The totals row landed under its fixture token.
    assert any(("totals", "Over 2.5") in by_key for by_key in out.values())


def test_derivatives_skipped_when_stale(tmp_path, caplog):
    db = str(tmp_path / "stale.db")
    stale_ts = (NOW - timedelta(hours=48)).isoformat()
    _make_db(db, stale_ts)
    with caplog.at_level(logging.WARNING, logger="wca.snapshot_freshness"):
        out = accas._load_snapshot_derivatives(db, now=NOW, max_age_hours=6.0)
    assert out == {}, "stale snapshot must not be priced off"
    assert any("STALE" in rec.message for rec in caplog.records)


def test_empty_db_returns_empty(tmp_path):
    db = str(tmp_path / "empty.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE odds_snapshots ("
        "ts_utc TEXT, source TEXT, match_id TEXT, market TEXT, "
        "selection TEXT, decimal_odds REAL, raw TEXT)"
    )
    con.commit()
    con.close()
    assert accas._load_snapshot_derivatives(db, now=NOW) == {}
