"""wca_ledger_audit: results-source freshness gate.

P2 (docs/HANDOFF_2026-07-03.md sec4): the auto-settler's default results
source was already repointed from the laggy data/raw/results.csv to the
fresher data/raw/martj42_cleaned.csv in PR #57 (see memory
wca-settle-stale-results-source.md). What was still missing was a freshness
gate: if the chosen results source is older than 24h, the settler must
refuse to settle from it rather than silently grading real-money bets off a
stale file.

These tests cover:
  (a) the default --results path is the fresher file, not data/raw/results.csv
  (b) a stale source is rejected with a clear error, not silently settled
  (c) an already-fresh source settles exactly as before (no regression)
"""
from __future__ import annotations

import csv
import datetime
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import wca_ledger_audit as audit  # noqa: E402


def _write_results_csv(path, rows):
    fieldnames = ["date", "home_team", "away_team", "home_score", "away_score",
                  "tournament", "city", "country", "neutral"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            row = {
                "date": r["date"], "home_team": r["home_team"], "away_team": r["away_team"],
                "home_score": r["home_score"], "away_score": r["away_score"],
                "tournament": r.get("tournament", "World Cup"),
                "city": r.get("city", ""), "country": r.get("country", ""),
                "neutral": r.get("neutral", "TRUE"),
            }
            w.writerow(row)


def _touch_mtime(path, hours_ago):
    ts = (datetime.datetime.now() - datetime.timedelta(hours=hours_ago)).timestamp()
    os.utime(path, (ts, ts))


# -- (a) default now points at the fresher path -----------------------------


def test_default_results_path_is_the_fresh_cleaned_file():
    """The default must be martj42_cleaned.csv, not the laggy raw mirror."""
    ns = _parse_default_args()
    assert ns.results == "data/raw/martj42_cleaned.csv"
    assert ns.results != "data/raw/results.csv"


def _parse_default_args():
    """Build the same ArgumentParser main() builds, without running main()."""
    import argparse
    p = argparse.ArgumentParser(description="One-time ledger audit & repair (dry-run by default)")
    p.add_argument("--db", default="data/wca.db")
    p.add_argument("--results", default="data/raw/martj42_cleaned.csv")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup-dir", default="data/backups")
    p.add_argument("--skip-closes", action="store_true")
    p.add_argument("--since", default="2026-06-01")
    p.add_argument("--max-age-hours", type=float, default=audit.DEFAULT_MAX_AGE_HOURS)
    p.add_argument("--skip-freshness-check", action="store_true")
    return p.parse_args([])


# -- (b) stale source is rejected, not silently settled ---------------------


def test_stale_results_source_raises(tmp_path):
    results = tmp_path / "martj42_cleaned.csv"
    _write_results_csv(results, [
        {"date": "2026-06-20", "home_team": "England", "away_team": "Ghana",
         "home_score": "2", "away_score": "0"},
    ])
    _touch_mtime(results, hours_ago=48)  # older than the 24h gate

    with pytest.raises(audit.StaleResultsError, match="stale"):
        audit.check_results_freshness(str(results), max_age_hours=24.0)


def test_missing_results_source_raises(tmp_path):
    missing = tmp_path / "does_not_exist.csv"
    with pytest.raises(audit.StaleResultsError):
        audit.check_results_freshness(str(missing), max_age_hours=24.0)


def test_stale_source_leaves_bet_open_via_cli(tmp_path, capsys):
    """End-to-end: main() must refuse (exit 1, no settle) on a stale source."""
    results = tmp_path / "martj42_cleaned.csv"
    _write_results_csv(results, [
        {"date": "2026-06-20", "home_team": "England", "away_team": "Ghana",
         "home_score": "2", "away_score": "0"},
    ])
    _touch_mtime(results, hours_ago=72)

    db_path = tmp_path / "wca_test.db"
    _init_test_db(db_path)
    _insert_open_bet(db_path, match_desc="England vs Ghana", market="h2h",
                      selection="England", decimal_odds=1.5, stake=10.0)

    rc = audit.main([
        "--db", str(db_path), "--results", str(results),
        "--since", "2026-06-01",
    ])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "stale" in out.lower() or "does not exist" in out.lower()

    # bet must still be open -- refusing to settle means no silent grading
    con = sqlite3.connect(str(db_path))
    row = con.execute("SELECT status FROM bets WHERE match_desc=?", ("England vs Ghana",)).fetchone()
    con.close()
    assert row[0] == "open"


# -- (c) a fresh source settles exactly as before (no regression) ----------


def test_fresh_results_source_settles_normally(tmp_path, capsys):
    results = tmp_path / "martj42_cleaned.csv"
    _write_results_csv(results, [
        {"date": "2026-06-20", "home_team": "England", "away_team": "Ghana",
         "home_score": "2", "away_score": "0"},
    ])
    # fresh -- default mtime (just written) is well within 24h

    db_path = tmp_path / "wca_test.db"
    _init_test_db(db_path)
    _insert_open_bet(db_path, match_desc="England vs Ghana", market="h2h",
                      selection="England", decimal_odds=1.5, stake=10.0)

    rc = audit.main([
        "--db", str(db_path), "--results", str(results),
        "--since", "2026-06-01", "--apply", "--skip-closes",
        "--backup-dir", str(tmp_path / "backups"),
    ])
    assert rc == 0

    con = sqlite3.connect(str(db_path))
    row = con.execute("SELECT status FROM bets WHERE match_desc=?", ("England vs Ghana",)).fetchone()
    con.close()
    assert row[0] == "won"


def test_skip_freshness_check_bypasses_gate(tmp_path):
    """--skip-freshness-check is the explicit, logged escape hatch."""
    results = tmp_path / "martj42_cleaned.csv"
    _write_results_csv(results, [
        {"date": "2026-06-20", "home_team": "England", "away_team": "Ghana",
         "home_score": "2", "away_score": "0"},
    ])
    _touch_mtime(results, hours_ago=72)

    db_path = tmp_path / "wca_test.db"
    _init_test_db(db_path)
    _insert_open_bet(db_path, match_desc="England vs Ghana", market="h2h",
                      selection="England", decimal_odds=1.5, stake=10.0)

    rc = audit.main([
        "--db", str(db_path), "--results", str(results),
        "--since", "2026-06-01", "--apply", "--skip-closes",
        "--skip-freshness-check", "--backup-dir", str(tmp_path / "backups"),
    ])
    assert rc == 0
    con = sqlite3.connect(str(db_path))
    row = con.execute("SELECT status FROM bets WHERE match_desc=?", ("England vs Ghana",)).fetchone()
    con.close()
    assert row[0] == "won"


# -- fixtures ----------------------------------------------------------------


def _init_test_db(db_path):
    from wca.ledger import store
    store.init_db(str(db_path))


def _insert_open_bet(db_path, match_desc, market, selection, decimal_odds, stake):
    from wca.ledger import store
    return store.record_bet(
        ts_utc="2026-06-19T14:00:00",
        match_id="TEST_1",
        match_desc=match_desc,
        market=market,
        selection=selection,
        platform="Bet365",
        decimal_odds=decimal_odds,
        stake=stake,
        db_path=str(db_path),
    )
