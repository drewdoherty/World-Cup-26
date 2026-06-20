"""Tests for wca.promosdata.build_promos_data (deterministic site feed).

Feeds a known DB + a fixed ``now_utc`` and asserts the JSON shape/keys and that
``meta.generated == now_utc`` — proving the builder never reads the wall clock.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from wca import promos, promosdata


FIXED_NOW = "2026-06-13 12:00:00 UTC"


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_promosdata_test_")
    os.close(fd)
    os.unlink(path)
    return path


def _seeded_conn() -> sqlite3.Connection:
    """A db with a seed sign-up, an ongoing promo, a boost, a watchlist row, a
    snapshot per couple of sites, and one boost eval."""
    db = _tmp_db()
    conn = promos._connect(db)
    promos.init_db(conn)

    # Sign-up (seed) for Sky Bet via the structured terms encoder.
    promos._upsert_seed_row(
        conn,
        {
            "site": "Sky Bet",
            "title": "Bet £10 Get £50",
            "description": "Bet £10, get £50 in free bets",
            "promo_type": "signup",
            "terms": promos._encode_signup_terms(
                min_stake="£10", min_odds="evens", free_bet_value="£50",
                expiry="7 days", promo_code="",
            ),
            "url": "",
        },
        "2026-06-13T00:00:00",
    )
    # Ongoing + boost (scraped) for Paddy Power.
    promos.diff_and_upsert(
        conn, "Paddy Power",
        [
            {"title": "2 Up Early Payout", "description": "settle early at 2-0",
             "promo_type": "ongoing", "terms": "", "url": "pp1"},
            {"title": "Power Price boost", "description": "Brazil was 2/1 now 5/2",
             "promo_type": "boost", "terms": "", "url": "pp2"},
        ],
        "2026-06-13T01:00:00", source="scrape",
    )
    # Watchlist (seed) for Virgin Bet.
    promos._upsert_seed_row(
        conn,
        {
            "site": "Virgin Bet",
            "title": "No standing promos",
            "description": "Standard sign-up offer only; check the app",
            "promo_type": "watchlist",
            "terms": "", "url": "",
        },
        "2026-06-13T00:00:00",
    )
    conn.commit()

    # Snapshots: one OK, one blocked.
    promos.record_snapshot(conn, "Paddy Power", "u", 200, "ok", 2,
                           ts_utc="2026-06-13T01:00:00")
    promos.record_snapshot(conn, "Bet365", "u", 403, "blocked", 0,
                           ts_utc="2026-06-13T01:00:00")
    # A boost eval.
    promos.record_boost_eval(
        conn, ts_utc="2026-06-13T01:00:00", site="Paddy Power",
        fixture="Brazil vs Serbia", market="match", selection="Brazil",
        boosted_odds=3.5, was_odds=3.0, model_prob=0.40, fair_odds=2.5,
        edge=0.40, is_plus_ev=True, priceable=True, reason=None, source="scrape",
    )
    return conn


class TestBuildPromosData:
    def test_top_level_shape(self) -> None:
        conn = _seeded_conn()
        try:
            data = promosdata.build_promos_data(conn, scores_feed=None,
                                                now_utc=FIXED_NOW)
        finally:
            conn.close()
        # Exact top-level key set from the shared contract.
        assert set(data.keys()) == {
            "meta", "sites", "signup_offers", "watchlist",
            "boost_evals", "scrape_health",
        }
        assert data["meta"] == {"generated": FIXED_NOW}

    def test_determinism_no_wall_clock(self) -> None:
        conn = _seeded_conn()
        try:
            d1 = promosdata.build_promos_data(conn, None, FIXED_NOW)
            d2 = promosdata.build_promos_data(conn, None, FIXED_NOW)
        finally:
            conn.close()
        # Same DB + same now_utc -> byte-identical output (no clock read).
        assert d1 == d2
        assert d1["meta"]["generated"] == FIXED_NOW

    def test_sites_cover_registry_with_scrape_block(self) -> None:
        conn = _seeded_conn()
        try:
            data = promosdata.build_promos_data(conn, None, FIXED_NOW)
        finally:
            conn.close()
        names = [s["name"] for s in data["sites"]]
        # Every registry site appears.
        for entry in promos.SITES:
            assert entry["name"] in names
        # Each site card has the required keys.
        for s in data["sites"]:
            assert set(s.keys()) == {"name", "kind", "scrape", "ongoing", "boosts"}
            assert set(s["scrape"].keys()) == {"status", "last_seen"}
        # Paddy Power got an ongoing + a boost; Bet365 shows blocked; an
        # un-fetched site shows 'never'.
        pp = next(s for s in data["sites"] if s["name"] == "Paddy Power")
        assert pp["scrape"]["status"] == "ok"
        assert len(pp["ongoing"]) == 1
        assert len(pp["boosts"]) == 1
        b365 = next(s for s in data["sites"] if s["name"] == "Bet365")
        assert b365["scrape"]["status"] == "blocked"
        unfetched = next(s for s in data["sites"] if s["name"] == "Unibet")
        assert unfetched["scrape"]["status"] == "never"

    def test_signup_and_watchlist_and_boost_evals(self) -> None:
        conn = _seeded_conn()
        try:
            data = promosdata.build_promos_data(conn, None, FIXED_NOW)
        finally:
            conn.close()
        # Sign-up offer with structured fields.
        assert any(o["site"] == "Sky Bet" for o in data["signup_offers"])
        sky = next(o for o in data["signup_offers"] if o["site"] == "Sky Bet")
        assert set(sky.keys()) == {
            "site", "offer", "min_odds", "min_stake", "free_bet_value",
            "expiry", "promo_code", "url",
        }
        assert sky["free_bet_value"] == "£50"
        # Watchlist row.
        assert any(w["site"] == "Virgin Bet" for w in data["watchlist"])
        for w in data["watchlist"]:
            assert set(w.keys()) == {"site", "title", "description", "why"}
        # Boost evals carry the contract keys.
        assert data["boost_evals"]
        be = data["boost_evals"][0]
        assert set(be.keys()) == {
            "ts", "site", "fixture", "market", "selection", "boosted_odds",
            "model_prob", "fair_odds", "edge", "is_plus_ev", "source",
        }
        assert be["is_plus_ev"] is True
        # Scrape health: one row per registry site.
        assert len(data["scrape_health"]) == len(promos.SITES)
        for h in data["scrape_health"]:
            assert set(h.keys()) == {"site", "status", "http_status", "last_ok_utc"}

    def test_empty_db_is_clean(self) -> None:
        db = _tmp_db()
        conn = promos._connect(db)
        promos.init_db(conn)
        try:
            data = promosdata.build_promos_data(conn, None, FIXED_NOW)
        finally:
            conn.close()
        assert data["meta"]["generated"] == FIXED_NOW
        assert data["signup_offers"] == []
        assert data["watchlist"] == []
        assert data["boost_evals"] == []
        # Sites still listed (registry), all 'never' scraped.
        assert len(data["sites"]) == len(promos.SITES)
        assert all(s["scrape"]["status"] == "never" for s in data["sites"])
