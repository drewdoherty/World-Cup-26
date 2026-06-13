"""Tests for the one-shot odds-snapshot CLI (scripts/wca_snapshot_odds.py).

The hourly ingest historically dropped every row silently: its hand-rolled
INSERT used columns matching neither the real ``odds_snapshots`` schema nor
the ``get_odds`` frame, and the table was never created.  These tests
round-trip a synthetic ``get_odds``-shaped frame through the rewritten
ingest into a temp SQLite DB and assert rows land with the canonical schema,
plus the raw-JSON audit dump conventions.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import pytest

from wca.data.snapshot import read_snapshots, rows_from_odds_frame

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "wca_snapshot_odds.py",
)


def _load_cli():
    spec = importlib.util.spec_from_file_location("wca_snapshot_odds", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = datetime(2026, 6, 13, 9, 0, 0, tzinfo=timezone.utc)


def _frame() -> pd.DataFrame:
    """Synthetic frame with the exact columns theoddsapi.get_odds returns."""
    rows = []
    for outcome, odds in (("Brazil", 1.62), ("Draw", 4.1), ("Morocco", 6.2)):
        rows.append({
            "event_id": "ev123",
            "commence_time": pd.Timestamp("2026-06-13T22:00:00Z"),
            "home_team": "Brazil",
            "away_team": "Morocco",
            "bookmaker_key": "smarkets",
            "bookmaker_title": "Smarkets",
            "market": "h2h",
            "outcome_name": outcome,
            "outcome_point": None,
            "decimal_odds": odds,
            "retrieved_at": pd.Timestamp("2026-06-13T09:00:00Z"),
        })
    rows.append({
        "event_id": "ev123",
        "commence_time": pd.Timestamp("2026-06-13T22:00:00Z"),
        "home_team": "Brazil",
        "away_team": "Morocco",
        "bookmaker_key": "betfair_ex_uk",
        "bookmaker_title": "Betfair",
        "market": "totals",
        "outcome_name": "Over",
        "outcome_point": 2.5,
        "decimal_odds": 2.3,
        "retrieved_at": pd.Timestamp("2026-06-13T09:00:00Z"),
    })
    return pd.DataFrame(rows)


def test_rows_from_odds_frame_canonical_shape():
    rows = rows_from_odds_frame(_frame(), NOW.isoformat())
    assert len(rows) == 4
    first = rows[0].to_tuple()
    # (ts_utc, source, match_id, market, selection, decimal_odds, raw)
    assert first[0] == NOW.isoformat()
    assert first[1] == "theoddsapi"
    assert first[2] == "ev123"
    assert first[3] == "h2h"
    assert first[4] == "Brazil"
    assert first[5] == pytest.approx(1.62)
    raw = json.loads(first[6])
    assert raw["bookmaker_key"] == "smarkets"
    # Totals selections fold the line into the selection key.
    totals = rows[3].to_tuple()
    assert totals[4] == "Over 2.5"


def test_rows_from_odds_frame_market_filter_and_empty():
    rows = rows_from_odds_frame(_frame(), NOW.isoformat(), markets=["h2h"])
    assert {r.market for r in rows} == {"h2h"}
    assert rows_from_odds_frame(pd.DataFrame(), NOW.isoformat()) == []


def test_ingest_round_trip(tmp_path):
    cli = _load_cli()
    db = tmp_path / "fresh.db"  # does not exist — table must be created
    snaps = tmp_path / "snapshots"

    inserted, raw_path = cli.ingest_snapshot(
        _frame(),
        db_path=str(db),
        snapshots_dir=str(snaps),
        markets="h2h",
        regions="uk",
        now=NOW,
    )
    assert inserted == 4

    # Rows landed with the real schema in a brand-new database.
    got = read_snapshots(str(db))
    assert len(got) == 4
    assert set(got[0]) == {
        "ts_utc", "source", "match_id", "market", "selection",
        "decimal_odds", "raw",
    }
    assert {r["selection"] for r in got} == {"Brazil", "Draw", "Morocco", "Over 2.5"}
    assert all(r["source"] == "theoddsapi" for r in got)
    assert all(r["match_id"] == "ev123" for r in got)

    # Raw audit dump follows the existing naming convention and is replayable.
    assert raw_path is not None and raw_path.exists()
    assert raw_path.name == "oddsapi_h2h_uk_20260613T090000Z.json"
    replay = json.loads(raw_path.read_text())
    assert len(replay) == 4
    assert replay[0]["event_id"] == "ev123"


def test_ingest_multi_market_name_and_skip_raw(tmp_path):
    cli = _load_cli()
    db = tmp_path / "fresh.db"
    inserted, raw_path = cli.ingest_snapshot(
        _frame(),
        db_path=str(db),
        snapshots_dir=None,
        markets="h2h,totals",
        regions="uk",
        now=NOW,
    )
    assert inserted == 4
    assert raw_path is None
    assert cli._raw_snapshot_name("h2h,totals", "uk", NOW) == (
        "oddsapi_multi_uk_20260613T090000Z.json"
    )


def test_ingest_appends_not_replaces(tmp_path):
    cli = _load_cli()
    db = tmp_path / "fresh.db"
    cli.ingest_snapshot(_frame(), str(db), None, "h2h", "uk", now=NOW)
    cli.ingest_snapshot(
        _frame(), str(db), None, "h2h", "uk",
        now=datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc),
    )
    con = sqlite3.connect(str(db))
    try:
        n, n_ts = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT ts_utc) FROM odds_snapshots"
        ).fetchone()
    finally:
        con.close()
    assert n == 8 and n_ts == 2
