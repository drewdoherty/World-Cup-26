"""Tests for the Polymarket 1X2 snapshotter (network-free)."""

import json
import sqlite3

import pytest

from wca import pm1x2snapshot as pms
from wca import venuesdata as vd


SCHEMA = (
    "CREATE TABLE odds_snapshots (ts_utc TEXT, source TEXT, match_id TEXT, "
    "market TEXT, selection TEXT, decimal_odds REAL, raw TEXT)"
)


def _con_with_book_fixture():
    con = sqlite3.connect(":memory:")
    con.execute(SCHEMA)
    # an existing bookmaker h2h row set establishes match_id 'M1' for Brazil v Japan
    for sel, odds in (("Brazil", 1.7), ("Draw", 3.6), ("Japan", 5.0)):
        raw = json.dumps({"bookmaker_key": "william_hill", "outcome_name": sel,
                          "home_team": "Brazil", "away_team": "Japan"})
        con.execute(
            "INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)",
            ("2026-06-20T10:00:00Z", "theoddsapi", "M1", "h2h", sel, odds, raw),
        )
    con.commit()
    return con


def test_build_match_index_bridges_team_pair():
    con = _con_with_book_fixture()
    idx = pms.build_match_index(con)
    assert idx[vd.pair_key("Brazil", "Japan")] == "M1"
    # order-independent
    assert idx[vd.pair_key("Japan", "Brazil")] == "M1"


def test_snapshot_inserts_matched_and_audits_unmatched():
    con = _con_with_book_fixture()
    pm_rows = [
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Brazil",
         "decimal_odds": 1.82, "event_id": "e1"},
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Draw",
         "decimal_odds": 3.40, "event_id": "e1"},
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Japan",
         "decimal_odds": 4.10, "event_id": "e1"},
        # a fixture with no bookmaker/model coverage -> unmatched, never inserted
        {"home_team": "Narnia", "away_team": "Atlantis", "outcome_name": "Narnia",
         "decimal_odds": 2.0, "event_id": "e2"},
    ]
    summary = pms.snapshot(con, pm_rows, "2026-06-20T11:30:00Z")
    assert summary["inserted"] == 3
    assert summary["n_unmatched_legs"] == 1
    assert "Narnia vs Atlantis" in summary["unmatched_fixtures"]

    got = con.execute(
        "SELECT match_id, selection, decimal_odds, "
        "json_extract(raw,'$.bookmaker_key') FROM odds_snapshots "
        "WHERE source='polymarket' ORDER BY selection"
    ).fetchall()
    assert got == [
        ("M1", "Brazil", 1.82, "polymarket"),
        ("M1", "Draw", 3.40, "polymarket"),
        ("M1", "Japan", 4.10, "polymarket"),
    ]


def test_benchmark_loader_sees_polymarket_after_snapshot():
    """End-to-end: once snapshotted, venuesdata's quote loader + per-book matcher
    treat Polymarket as a complete H/D/A venue."""
    con = _con_with_book_fixture()
    pm_rows = [
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Brazil", "decimal_odds": 1.82},
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Draw", "decimal_odds": 3.40},
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Japan", "decimal_odds": 4.10},
    ]
    pms.snapshot(con, pm_rows, "2026-06-20T11:30:00Z")
    rows = vd.load_match_quote_rows(con, "M1")
    books = {r[0] for r in rows}
    assert "polymarket" in books
    # the no-lookahead/freshness matcher forms a complete partition for Polymarket
    as_of = vd.parse_ts("2026-06-20T12:00:00Z")
    per_book = vd.per_book_quotes_from_rows(rows, as_of, freshness_s=6 * 3600.0)
    assert "polymarket" in per_book  # canon_book("polymarket") -> "polymarket"


def test_incomplete_pm_partition_is_dropped():
    """Only 2 of 3 PM outcomes captured -> incomplete book, omitted by the matcher."""
    con = _con_with_book_fixture()
    pm_rows = [
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Brazil", "decimal_odds": 1.82},
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Draw", "decimal_odds": 3.40},
    ]
    pms.snapshot(con, pm_rows, "2026-06-20T11:30:00Z")
    rows = vd.load_match_quote_rows(con, "M1")
    as_of = vd.parse_ts("2026-06-20T12:00:00Z")
    per_book = vd.per_book_quotes_from_rows(rows, as_of, freshness_s=6 * 3600.0)
    assert "Polymarket" not in per_book


def test_knockout_team_aliases_unify_oddsapi_and_pm_spellings():
    """OddsAPI vs Polymarket spellings of knockout teams resolve to one pair key,
    so the match_id bridge holds once R32 odds are captured."""
    assert vd.pair_key("Bosnia & Herzegovina", "USA") == vd.pair_key("Bosnia and Herzegovina", "United States")
    assert vd.pair_key("Cabo Verde", "Argentina") == vd.pair_key("Cape Verde", "Argentina")
    assert vd.pair_key("Congo DR", "England") == vd.pair_key("DR Congo", "England")
    assert vd.canon_team("Côte d'Ivoire") == vd.canon_team("Ivory Coast")


def test_bad_prices_and_draw_aliases():
    con = _con_with_book_fixture()
    idx = pms.build_match_index(con)
    rows = [
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "The Draw", "decimal_odds": 3.4},
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Brazil", "decimal_odds": 1.0},   # <=1 skipped
        {"home_team": "Brazil", "away_team": "Japan", "outcome_name": "Japan", "decimal_odds": None},   # skipped
    ]
    insert_rows, unmatched = pms.pm_rows_to_snapshot_rows(rows, idx, "2026-06-20T11:30:00Z")
    sels = sorted(r[4] for r in insert_rows)
    assert sels == ["Draw"]          # 'The Draw' canonicalised; bad prices dropped
    assert unmatched == []
