"""Tests for the durable, DB-less odds price-history store (wca.data.oddshistory).

The closing line was silently dead for ~12 days because both durable paths
needed something fragile up (the mini-local DB, or a raw-dump step that crashed
under continue-on-error). These tests pin the PM-style guarantees that prevent a
recurrence: the JSONL round-trips, it WRITES with no DB present, and re-ingest is
idempotent (no duplicate closing lines).
"""

from __future__ import annotations

import os
import sqlite3

import pandas as pd

from wca.data import oddshistory as oh


def _frame() -> pd.DataFrame:
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


def test_rows_from_odds_frame_grain_and_line_fold():
    rows = oh.rows_from_odds_frame(_frame())
    assert len(rows) == 4
    by_sel = {r["selection"]: r for r in rows}
    assert set(by_sel) == {"Brazil", "Draw", "Morocco", "Over 2.5"}  # line folded
    brz = by_sel["Brazil"]
    assert brz["fixture"] == "ev123"
    assert brz["market"] == "h2h"
    assert brz["book"] == "smarkets"
    assert brz["decimal_odds"] == 1.62
    assert brz["commence_time"] == "2026-06-13T22:00:00+00:00"


def test_rows_from_odds_frame_empty_and_missing_fixture():
    assert oh.rows_from_odds_frame(pd.DataFrame()) == []
    assert oh.rows_from_odds_frame(None) == []
    df = pd.DataFrame([{"event_id": None, "market": "h2h", "outcome_name": "X",
                        "outcome_point": None, "decimal_odds": 2.0}])
    assert oh.rows_from_odds_frame(df) == []  # no fixture -> dropped


def test_jsonl_roundtrip(tmp_path):
    p = str(tmp_path / "odds_hist.jsonl")
    n1 = oh.append_jsonl(p, oh.rows_from_odds_frame(_frame()), "2026-06-13T09:00:00Z")
    n2 = oh.append_jsonl(p, oh.rows_from_odds_frame(_frame()), "2026-06-13T10:00:00Z")
    assert n1 == 4 and n2 == 4
    recs = oh.load_records(p)
    assert len(recs) == 8
    assert {r["ts_utc"] for r in recs} == {"2026-06-13T09:00:00Z", "2026-06-13T10:00:00Z"}
    assert {r["selection"] for r in recs} == {"Brazil", "Draw", "Morocco", "Over 2.5"}


def test_load_records_missing_file_is_empty(tmp_path):
    assert oh.load_records(str(tmp_path / "nope.jsonl")) == []


def test_write_needs_no_db(tmp_path):
    """The whole point: capture must WRITE with no SQLite anywhere in sight."""
    p = str(tmp_path / "sub" / "deep" / "odds_hist.jsonl")  # parents auto-created
    n = oh.append_jsonl(p, oh.rows_from_odds_frame(_frame()), "2026-06-13T09:00:00Z")
    assert n == 4
    assert os.path.exists(p)
    # No .db file was created as a side effect of writing the durable history.
    assert not any(f.endswith(".db") for f in os.listdir(tmp_path))


def test_ingest_idempotent(tmp_path):
    p = str(tmp_path / "odds_hist.jsonl")
    db = str(tmp_path / "wca.db")
    oh.append_jsonl(p, oh.rows_from_odds_frame(_frame()), "2026-06-13T09:00:00Z")
    oh.append_jsonl(p, oh.rows_from_odds_frame(_frame()), "2026-06-13T10:00:00Z")

    first = oh.ingest(p, db)
    assert first == 8
    again = oh.ingest(p, db)  # re-ingest the SAME jsonl
    assert again == 0  # idempotent: no duplicate closing lines

    con = sqlite3.connect(db)
    try:
        total = con.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
        n_ts = con.execute(
            "SELECT COUNT(DISTINCT ts_utc) FROM odds_snapshots"
        ).fetchone()[0]
    finally:
        con.close()
    assert total == 8 and n_ts == 2


def test_ingest_dedups_on_full_key_but_admits_new_rows(tmp_path):
    p = str(tmp_path / "odds_hist.jsonl")
    db = str(tmp_path / "wca.db")
    oh.append_jsonl(p, oh.rows_from_odds_frame(_frame()), "2026-06-13T09:00:00Z")
    assert oh.ingest(p, db) == 4

    # A genuinely new pull (new ts) adds rows; the old ts is still deduped.
    oh.append_jsonl(p, oh.rows_from_odds_frame(_frame()), "2026-06-13T10:00:00Z")
    assert oh.ingest(p, db) == 4  # only the 4 new-ts rows

    con = sqlite3.connect(db)
    try:
        total = con.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    finally:
        con.close()
    assert total == 8


def test_ingest_distinguishes_book(tmp_path):
    """Same fixture/market/selection/ts on two books must NOT collapse."""
    db = str(tmp_path / "wca.db")
    recs = [
        {"ts_utc": "t", "fixture": "ev1", "market": "h2h", "book": "smarkets",
         "selection": "Brazil", "decimal_odds": 1.6},
        {"ts_utc": "t", "fixture": "ev1", "market": "h2h", "book": "betfair_ex_uk",
         "selection": "Brazil", "decimal_odds": 1.65},
    ]
    assert oh.ingest("ignored", db, records=recs) == 2
    assert oh.ingest("ignored", db, records=recs) == 0  # still idempotent

    con = sqlite3.connect(db)
    try:
        total = con.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    finally:
        con.close()
    assert total == 2
