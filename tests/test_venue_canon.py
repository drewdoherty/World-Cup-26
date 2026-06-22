"""Tests for venue-name canonicalisation at the write choke point + backfill.

Covers:
  * the canon map (bet365 variants merge; Betfair exchange vs Sportsbook stay
    distinct; unknown/empty/None -> "Unknown")
  * store.record_bet persists the canonical platform (write "Bet365" -> "bet365",
    write "" -> "Unknown")
  * the backfill script's dry-run (no write) vs --apply on a temp DB, idempotent.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from wca.venues import canon_platform
from wca.ledger.store import record_bet, _connect

_BACKFILL_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wca_canon_venues.py"


def _load_backfill():
    spec = importlib.util.spec_from_file_location("wca_canon_venues", _BACKFILL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = _load_backfill()


# ---------------------------------------------------------------------------
# canon map.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("bet365", "bet365"),
        ("Bet365", "bet365"),
        ("BET365", "bet365"),
        ("bet 365", "bet365"),
        ("betfred", "Betfred"),
        ("Betfred", "Betfred"),
        ("betway", "Betway"),
        ("Betway", "Betway"),
        ("ladbrokes", "Ladbrokes"),
        ("paddypower", "Paddy Power"),
        ("paddy power", "Paddy Power"),
        ("skybet", "Sky Bet"),
        ("sky bet", "Sky Bet"),
        ("virginbet", "Virgin Bet"),
        ("virgin bet", "Virgin Bet"),
        # Betfair exchange vs sportsbook stay DISTINCT
        ("Betfair", "Betfair"),
        ("betfair_ex_uk", "Betfair"),
        ("Betfair Exchange", "Betfair"),
        ("Betfair Sportsbook", "Betfair Sportsbook"),
        ("betfair_sportsbook", "Betfair Sportsbook"),
        # unknown / empty / None
        ("", "Unknown"),
        ("   ", "Unknown"),
        ("unknown", "Unknown"),
        ("Unknown", "Unknown"),
        ("UNKNOWN", "Unknown"),
        (None, "Unknown"),
        # Non-sportsbook pool keys preserved verbatim (routing depends on them)
        ("polymarket", "polymarket"),
        ("polymarket-auto", "polymarket-auto"),
        ("kalshi", "kalshi"),
    ],
)
def test_canon_platform_map(raw, expected):
    assert canon_platform(raw) == expected


def test_bet365_variants_all_merge_to_one():
    out = {canon_platform(v) for v in ("bet365", "Bet365", "BET365", "bet 365")}
    assert out == {"bet365"}


def test_betfair_exchange_and_sportsbook_distinct():
    assert canon_platform("Betfair") != canon_platform("Betfair Sportsbook")


def test_canon_is_idempotent():
    for v in ("Bet365", "betway", "", None, "Betfair Sportsbook", "paddypower"):
        once = canon_platform(v)
        assert canon_platform(once) == once


# ---------------------------------------------------------------------------
# record_bet persists canonical platform.
# ---------------------------------------------------------------------------

def _record(tmp_db, platform):
    return record_bet(
        ts_utc="2026-06-11T14:00:00",
        match_id="GRP_A_01",
        match_desc="A vs B",
        market="1X2",
        selection="Home",
        platform=platform,
        decimal_odds=2.0,
        stake=10.0,
        db_path=tmp_db,
    )


def _platform_of(tmp_db, row_id):
    with _connect(tmp_db) as conn:
        return conn.execute(
            "SELECT platform FROM bets WHERE id = ?", (row_id,)
        ).fetchone()[0]


def test_record_bet_canonicalises_bet365(tmp_path):
    db = str(tmp_path / "t.db")
    rid = _record(db, "Bet365")
    assert _platform_of(db, rid) == "bet365"


def test_record_bet_empty_platform_becomes_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rid = _record(db, "")
    assert _platform_of(db, rid) == "Unknown"


# ---------------------------------------------------------------------------
# backfill script.
# ---------------------------------------------------------------------------

def _seed_dirty(tmp_db):
    """Insert rows with un-canonical names (bypassing record_bet's canon)."""
    rid = _record(tmp_db, "bet365")  # creates table; canonical already
    with _connect(tmp_db) as conn:
        conn.execute("UPDATE bets SET platform = 'Bet365' WHERE id = ?", (rid,))
        for raw in ("Bet365", "betway", "", "Betfair Sportsbook"):
            conn.execute(
                "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection,"
                " platform, decimal_odds, stake, status) VALUES"
                " ('t','m','d','1X2','Home', ?, 2.0, 10.0, 'open')",
                (raw,),
            )
        conn.commit()


def _platforms(tmp_db):
    with _connect(tmp_db) as conn:
        return sorted(r[0] for r in conn.execute("SELECT platform FROM bets"))


def test_backfill_dry_run_does_not_write(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_dirty(db)
    before = _platforms(db)
    merges = backfill.plan_merges(db)
    assert merges["bet365"]["Bet365"] == 2
    assert merges["Betway"]["betway"] == 1
    assert merges["Unknown"][""] == 1
    # Betfair Sportsbook is canonical -> not in the merge plan
    assert "Betfair Sportsbook" not in merges
    # Nothing written
    assert _platforms(db) == before


def test_backfill_apply_writes_and_is_idempotent(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_dirty(db)
    changed = backfill.apply_merges(db)
    assert changed == 4  # 2x Bet365 + betway + ''
    after = _platforms(db)
    assert "Bet365" not in after
    assert "betway" not in after
    assert "" not in after
    assert after.count("bet365") == 2
    assert "Betway" in after
    assert "Unknown" in after
    assert "Betfair Sportsbook" in after
    # Idempotent: second pass changes nothing.
    assert backfill.apply_merges(db) == 0
    assert backfill.plan_merges(db) == {}
