"""Tests for the bet account/source dimensions: store persistence + defaults,
idempotent legacy-db migration, the sitedata venue split and source_summary,
and that positions carry the new keys.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from wca import sitedata
from wca.ledger import store
from wca.ledger import reports


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_acctsrc_")
    os.close(fd)
    os.unlink(path)
    return path


# ---------------------------------------------------------------------------
# store.record_bet persistence + defaults.
# ---------------------------------------------------------------------------


def test_record_bet_persists_account_source():
    db = _tmp_db()
    bid = store.record_bet(
        ts_utc="2026-06-11T10:00:00", match_id="M1", match_desc="A vs B",
        market="1X2", selection="Home", platform="betfair_sportsbook",
        decimal_odds=2.0, stake=10.0, account="2", source="offer", db_path=db,
    )
    row = store.get_bet(bid, db_path=db)
    assert row["account"] == "2"
    assert row["source"] == "offer"


def test_record_bet_defaults_account_one_source_model():
    db = _tmp_db()
    bid = store.record_bet(
        ts_utc="2026-06-11T10:00:00", match_id="M1", match_desc="A vs B",
        market="1X2", selection="Home", platform="bet365",
        decimal_odds=2.0, stake=10.0, db_path=db,
    )
    row = store.get_bet(bid, db_path=db)
    assert row["account"] == "1"
    assert row["source"] == "model"


def test_record_bet_coerces_to_str():
    db = _tmp_db()
    bid = store.record_bet(
        ts_utc="2026-06-11T10:00:00", match_id="M1", match_desc="A vs B",
        market="1X2", selection="Home", platform="bet365",
        decimal_odds=2.0, stake=10.0, account=2, source="punt", db_path=db,
    )
    row = store.get_bet(bid, db_path=db)
    assert row["account"] == "2"
    assert isinstance(row["account"], str)


# ---------------------------------------------------------------------------
# Migration idempotency on a legacy (old-schema) database.
# ---------------------------------------------------------------------------


def test_migration_idempotent_on_legacy_db():
    db = _tmp_db()
    # Build an old-schema bets table WITHOUT account/source.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE bets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts_utc TEXT, match_id TEXT, match_desc TEXT, market TEXT, "
        "selection TEXT, platform TEXT, decimal_odds REAL, stake REAL, "
        "model_prob REAL, market_prob_devig REAL, ev REAL, "
        "kelly_fraction REAL, status TEXT DEFAULT 'open', settled_pl REAL, "
        "closing_odds REAL, clv REAL, notes TEXT)"
    )
    conn.execute(
        "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
        "platform, decimal_odds, stake) VALUES "
        "('2026-06-11T10:00:00','M1','A vs B','1X2','Home','bet365',2.0,10.0)"
    )
    conn.commit()
    conn.close()

    # First migration adds columns.
    conn = sqlite3.connect(db)
    store._ensure_account_source_columns(conn)
    conn.commit()
    # Second call is a no-op (idempotent), must not raise.
    store._ensure_account_source_columns(conn)
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bets)")]
    conn.close()
    assert "account" in cols and "source" in cols

    # The legacy row defaults to '1'/'model'; a fresh insert works too.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    legacy = conn.execute("SELECT account, source FROM bets WHERE id=1").fetchone()
    conn.close()
    assert legacy["account"] == "1"
    assert legacy["source"] == "model"

    bid = store.record_bet(
        ts_utc="2026-06-11T11:00:00", match_id="M2", match_desc="C vs D",
        market="1X2", selection="Away", platform="virginbet",
        decimal_odds=3.0, stake=5.0, account="2", source="offer", db_path=db,
    )
    assert store.get_bet(bid, db_path=db)["source"] == "offer"


# ---------------------------------------------------------------------------
# sitedata venue split + source_summary + position keys.
# ---------------------------------------------------------------------------


def _seed_split(db: str) -> None:
    # acct-1 sportsbook, open.
    store.record_bet(
        ts_utc="2026-06-11T10:00:00", match_id="M1", match_desc="A vs B",
        market="1X2", selection="Home", platform="virginbet",
        decimal_odds=2.0, stake=10.0, account="1", source="model", db_path=db,
    )
    # acct-2 sportsbook, open, offer.
    store.record_bet(
        ts_utc="2026-06-11T11:00:00", match_id="M2", match_desc="C vs D",
        market="MATCH", selection="Canada", platform="betfair_sportsbook",
        decimal_odds=1.88, stake=10.0, account="2", source="offer", db_path=db,
    )
    # polymarket, open, punt (USD).
    store.record_bet(
        ts_utc="2026-06-11T12:00:00", match_id="M3", match_desc="E futures",
        market="WINNER", selection="E", platform="polymarket",
        decimal_odds=2.0, stake=20.0, account="1", source="punt", db_path=db,
    )
    # acct-1 sportsbook, settled won.
    wid = store.record_bet(
        ts_utc="2026-06-11T09:00:00", match_id="M4", match_desc="G vs H",
        market="1X2", selection="Home", platform="bet365",
        decimal_odds=2.0, stake=10.0, account="1", source="model", db_path=db,
    )
    store.settle_bet(wid, "won", db_path=db)


def test_sitedata_venue_split_and_legacy_sum():
    db = _tmp_db()
    _seed_split(db)
    data = sitedata.build_site_data(db, card_path="/nonexistent.md", now_utc="x")
    v = data["venues"]

    # Legacy combined sportsbook key retained.
    assert "sportsbook" in v
    assert "sportsbook_1" in v and "sportsbook_2" in v
    assert v["sportsbook_1"]["label"] == "Sportsbook 1"
    assert v["sportsbook_2"]["label"] == "Sportsbook 2"

    # acct-1 has the virginbet open (10) + bet365 won (10) = 20 wagered.
    assert v["sportsbook_1"]["wagered"] == 20.0
    assert v["sportsbook_1"]["open_stake"] == 10.0
    assert v["sportsbook_1"]["settled_pl"] == 10.0  # (2.0-1)*10
    # acct-2 has the betfair qualifier (10).
    assert v["sportsbook_2"]["wagered"] == 10.0
    assert v["sportsbook_2"]["open_stake"] == 10.0

    # Legacy combined == sum of the two accounts.
    assert v["sportsbook"]["wagered"] == (
        v["sportsbook_1"]["wagered"] + v["sportsbook_2"]["wagered"]
    )
    assert v["sportsbook"]["settled_pl"] == (
        v["sportsbook_1"]["settled_pl"] + v["sportsbook_2"]["settled_pl"]
    )
    # polymarket untouched.
    assert v["polymarket"]["wagered"] == 20.0


def test_sitedata_source_summary_math():
    db = _tmp_db()
    _seed_split(db)
    data = sitedata.build_site_data(db, card_path="/nonexistent.md", now_utc="x")
    ss = data["source_summary"]

    # model: virginbet 10 (GBP open) + bet365 10 (GBP won).
    assert ss["model"]["GBP"]["wagered"] == 20.0
    assert ss["model"]["GBP"]["open_stake"] == 10.0
    assert ss["model"]["GBP"]["settled_pl"] == 10.0
    assert ss["model"]["GBP"]["n_bets"] == 2
    # offer: betfair 10 GBP open.
    assert ss["offer"]["GBP"]["wagered"] == 10.0
    assert ss["offer"]["GBP"]["n_bets"] == 1
    # punt: polymarket 20 USD open.
    assert ss["punt"]["USD"]["wagered"] == 20.0
    assert ss["punt"]["USD"]["open_stake"] == 20.0


def test_positions_carry_account_source():
    db = _tmp_db()
    _seed_split(db)
    data = sitedata.build_site_data(db, card_path="/nonexistent.md", now_utc="x")
    for p in data["positions"]:
        assert "account" in p and "source" in p
    for p in data["closed_positions"]:
        assert "account" in p and "source" in p
    # The acct-2 offer bet is present in positions with the right tags.
    acc2 = [p for p in data["positions"] if p["account"] == "2"]
    assert acc2 and acc2[0]["source"] == "offer"


def test_totals_by_currency_unchanged_by_split():
    db = _tmp_db()
    _seed_split(db)
    data = sitedata.build_site_data(db, card_path="/nonexistent.md", now_utc="x")
    # GBP totals: 20 (acct1) + 10 (acct2) = 30 wagered. Split must not double.
    assert data["totals_by_currency"]["GBP"]["wagered"] == 30.0
    assert data["totals_by_currency"]["USD"]["wagered"] == 20.0


# ---------------------------------------------------------------------------
# reports.summary() by_source breakdown (additive).
# ---------------------------------------------------------------------------


def test_summary_by_source_math():
    db = _tmp_db()
    _seed_split(db)
    summ = reports.summary(db)
    bs = summ["by_source"]

    # All three canonical sources always present.
    assert set(("model", "offer", "punt")).issubset(bs.keys())

    # model: virginbet 10 (open) + bet365 10 (won) -> n=2, staked=20,
    # settled_pl=(2.0-1)*10=10.
    assert bs["model"]["n"] == 2
    assert bs["model"]["staked"] == 20.0
    assert bs["model"]["settled_pl"] == 10.0
    # offer: betfair 10 open -> n=1, staked=10, no settled P&L.
    assert bs["offer"]["n"] == 1
    assert bs["offer"]["staked"] == 10.0
    assert bs["offer"]["settled_pl"] == 0.0
    # punt: polymarket 20 open -> n=1, staked=20, no settled P&L.
    assert bs["punt"]["n"] == 1
    assert bs["punt"]["staked"] == 20.0
    assert bs["punt"]["settled_pl"] == 0.0

    # Additive: existing keys untouched.
    assert summ["total_bets"] == 4
    assert "avg_clv" in summ and "brier_model" in summ


def test_summary_by_source_empty_db():
    db = _tmp_db()
    store.init_db(db)
    bs = reports.summary(db)["by_source"]
    for s in ("model", "offer", "punt"):
        assert bs[s] == {"n": 0, "staked": 0.0, "settled_pl": 0.0}


# ---------------------------------------------------------------------------
# Backfill idempotency: running twice == running once.
# ---------------------------------------------------------------------------


def _legacy_db_for_backfill() -> str:
    """A legacy-schema DB seeded with a couple of bets so the backfill's
    per-id source mapping has something to update."""
    db = _tmp_db()
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE bets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts_utc TEXT, match_id TEXT, match_desc TEXT, market TEXT, "
        "selection TEXT, platform TEXT, decimal_odds REAL, stake REAL, "
        "model_prob REAL, market_prob_devig REAL, ev REAL, "
        "kelly_fraction REAL, status TEXT DEFAULT 'open', settled_pl REAL, "
        "closing_odds REAL, clv REAL, notes TEXT)"
    )
    # id 1 -> model, id 3 -> offer per the backfill mapping.
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
            "platform, decimal_odds, stake) VALUES "
            "(?,?,?,?,?,?,?,?)",
            ("2026-06-11T10:00:00", "M%d" % i, "A vs B", "1X2", "Home",
             "virginbet", 2.0, 10.0),
        )
    conn.commit()
    conn.close()
    return db


def _all_rows(db: str) -> list:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, account, source, selection, platform FROM bets ORDER BY id"
        )]
    finally:
        conn.close()


def test_backfill_idempotent():
    import importlib.util

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "wca_backfill_accounts",
        os.path.join(here, "scripts", "wca_backfill_accounts.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = _legacy_db_for_backfill()
    mod.backfill(db)
    once = _all_rows(db)
    mod.backfill(db)
    twice = _all_rows(db)

    assert once == twice, "backfill must be idempotent"
    # Per-id mapping applied: id 1 model, id 3 offer, all account '1'.
    by_id = {r["id"]: r for r in twice}
    assert by_id[1]["account"] == "1" and by_id[1]["source"] == "model"
    assert by_id[3]["source"] == "offer"
    # Account-2 Betfair inserts present exactly once (3 of them).
    acct2 = [r for r in twice if r["account"] == "2"]
    assert len(acct2) == 3
