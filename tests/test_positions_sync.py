"""Tests for the venue-position reconciliation engine (SHADOW -> LIVE ladder)."""
from __future__ import annotations

import sqlite3

import pytest

from wca import positions_sync
from wca.ledger import store


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------


def _db(tmp_path):
    p = str(tmp_path / "wca.db")
    store.init_db(p)
    return p


def _row_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    finally:
        conn.close()


def _seed_open_bet(db_path, **kw):
    defaults = dict(
        ts_utc="2026-06-26T10:00:00",
        match_id="M1",
        match_desc="England vs Spain",
        market="1X2",
        selection="England",
        platform="Betfair",
        decimal_odds=2.5,
        stake=10.0,
        db_path=db_path,
    )
    defaults.update(kw)
    return store.record_bet(**defaults)


def _venue_pos(**kw):
    base = dict(
        venue="Betfair",
        market="1X2",
        selection="England",
        fixture_or_event="England vs Spain",
        stake=10.0,
        size=10.0,
        avg_price=2.5,
        odds=2.5,
        current_value=None,
        current_price=None,
        external_id="bf-1",
        account="1",
    )
    base.update(kw)
    return base


def _settled_pos(**kw):
    """A normalised SETTLED venue position (won by default)."""
    base = dict(
        venue="Betfair",
        market="1X2",
        selection="England",
        fixture_or_event="England vs Spain",
        settled_pnl=15.0,
        result="won",
        settled_ts="2026-06-26T11:00:00Z",
        external_id="bf-settled-1",
        account="1",
    )
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Reconcile classification.
# ---------------------------------------------------------------------------


def test_reconcile_classifies_new_gone_matched(tmp_path):
    db = _db(tmp_path)
    # ledger: a Betfair England bet (will match) + a Smarkets bet (gone)
    _seed_open_bet(db)
    _seed_open_bet(db, platform="smarkets", selection="Draw", match_desc="A vs B", market="1X2")
    ledger = positions_sync.load_open_ledger_bets(db)

    venue = [
        _venue_pos(),  # matches the Betfair England bet
        _venue_pos(venue="polymarket", selection="Brazil", market="World Cup Winner",
                   fixture_or_event="World Cup Winner", external_id="pm-x"),  # new
    ]
    rec = positions_sync.reconcile(venue, ledger)
    c = rec.as_dict()["counts"]
    assert c["matched"] == 1
    assert c["new_at_venue"] == 1   # Brazil at PM
    assert c["gone_from_venue"] == 1  # Smarkets Draw not at any venue
    assert c["review"] == 0
    assert rec.new_at_venue[0]["selection"] == "Brazil"
    assert rec.gone_from_venue[0]["selection"] == "Draw"


def test_reconcile_ambiguous_goes_to_review(tmp_path):
    db = _db(tmp_path)
    _seed_open_bet(db)
    _seed_open_bet(db)  # two identical open ledger bets -> ambiguous
    ledger = positions_sync.load_open_ledger_bets(db)
    rec = positions_sync.reconcile([_venue_pos()], ledger)
    c = rec.as_dict()["counts"]
    assert c["review"] == 1
    assert c["matched"] == 0
    assert c["gone_from_venue"] == 0


# ---------------------------------------------------------------------------
# SHADOW makes ZERO ledger writes.
# ---------------------------------------------------------------------------


def test_shadow_makes_zero_ledger_writes(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed_open_bet(db, platform="smarkets", selection="Draw", match_desc="A vs B")
    before = _row_count(db)

    fetchers = {"betfair": lambda: [_venue_pos()]}  # a NEW position at venue
    monkeypatch.setattr(positions_sync, "refresh_site_projection", lambda: 0)

    report = positions_sync.run_sync(db, live=False, fetchers=fetchers)
    assert report["mode"] == "SHADOW"
    assert report["applied"] is None
    assert report["reconciliation"]["counts"]["new_at_venue"] == 1
    assert _row_count(db) == before  # ZERO writes


# ---------------------------------------------------------------------------
# LIVE inserts new + marks gone-as-closed, idempotently, never settles P&L.
# ---------------------------------------------------------------------------


def test_live_inserts_and_closes_idempotent(tmp_path, monkeypatch):
    db = _db(tmp_path)
    gone_id = _seed_open_bet(db, platform="smarkets", selection="Draw",
                             match_desc="A vs B", market="1X2")
    # A second seeded Smarkets bet that the venue STILL reports open — this is
    # what makes the Smarkets fetch confirmable (VENUE_OK), so the matcher-safety
    # gate permits auto-closing the genuinely-gone Draw above.
    _seed_open_bet(db, platform="smarkets", selection="Other",
                   match_desc="C vs D", market="1X2")
    monkeypatch.setattr(positions_sync, "refresh_site_projection", lambda: 0)

    # Venue shows a NEW Betfair England position; Smarkets still has "Other"
    # (matched) but NOT the Draw (gone).
    fetchers = {
        "betfair": lambda: [_venue_pos()],
        "smarkets": lambda: [_venue_pos(venue="smarkets", selection="Other",
                                        market="1X2", fixture_or_event="C vs D",
                                        external_id="smk-other")],
    }

    r1 = positions_sync.run_sync(db, live=True, fetchers=fetchers)
    assert r1["mode"] == "LIVE"
    assert len(r1["applied"]["inserted"]) == 1   # only the new Betfair England
    assert gone_id in r1["applied"]["closed"]     # the gone Draw is auto-closed

    # gone bet is marked 'closed' pending settlement, NOT settled (no P&L).
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    gone = conn.execute("SELECT * FROM bets WHERE id=?", (gone_id,)).fetchone()
    assert gone["status"] == positions_sync.CLOSED_PENDING_STATUS
    assert gone["settled_pl"] is None  # NEVER auto-computed P&L
    ins = conn.execute(
        "SELECT * FROM bets WHERE source='manual' AND selection='England'"
    ).fetchone()
    assert ins["status"] == "open"
    conn.close()

    count_after_first = _row_count(db)

    # Re-run with the SAME venue state: the inserted bet now matches, the gone
    # bet is already closed -> a no-op.
    r2 = positions_sync.run_sync(db, live=True, fetchers=fetchers)
    assert r2["applied"]["inserted"] == []
    assert r2["applied"]["closed"] == []
    assert _row_count(db) == count_after_first  # idempotent
    assert r2["reconciliation"]["counts"]["matched"] == 2  # England + Other


# ---------------------------------------------------------------------------
# Each venue fetch degrades to empty on simulated network failure (never raises).
# ---------------------------------------------------------------------------


def test_betfair_fetch_degrades_on_network_failure(monkeypatch):
    from wca.data import betfair_exchange

    monkeypatch.setattr(betfair_exchange, "_resolve_session_token", lambda: "tok")
    monkeypatch.setattr(betfair_exchange, "_candidate_app_keys", lambda: ["k"])

    def boom(*a, **k):
        raise ConnectionError("mini cannot reach Betfair")

    monkeypatch.setattr(betfair_exchange, "_rpc", boom)
    assert betfair_exchange.list_current_orders() == []
    assert positions_sync.fetch_betfair_positions() == []


def test_smarkets_fetch_degrades_on_network_failure(monkeypatch):
    from wca.data import smarkets

    monkeypatch.setattr(smarkets, "session_login", lambda: "tok")

    import requests as _rq

    def boom(*a, **k):
        raise _rq.exceptions.ConnectionError("network down")

    monkeypatch.setattr(_rq, "get", boom, raising=False)
    assert smarkets.list_open_positions() == []
    assert positions_sync.fetch_smarkets_positions() == []


def test_smarkets_no_creds_degrades(monkeypatch):
    from wca.data import smarkets

    monkeypatch.delenv("SMARKETS_API_TOKEN", raising=False)
    monkeypatch.delenv("SMARKETS_USERNAME", raising=False)
    monkeypatch.delenv("SMARKETS_PASSWORD", raising=False)
    smarkets._CACHED_SESSION = None
    assert smarkets.session_login() is None
    assert smarkets.list_open_positions() == []


def test_polymarket_fetch_degrades_on_failure(monkeypatch):
    from wca import sitedata

    def boom(*a, **k):
        raise RuntimeError("PM data-api down")

    monkeypatch.setattr(sitedata, "live_pm_positions", boom)
    assert positions_sync.fetch_polymarket_positions() == []


def test_fetch_all_isolates_one_bad_venue(monkeypatch):
    def good():
        return [_venue_pos(venue="polymarket")]

    def bad():
        raise ConnectionError("down")

    rows = positions_sync.fetch_all_positions({"good": good, "bad": bad})
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Normalisers (pure).
# ---------------------------------------------------------------------------


def test_betfair_normalise_skips_unmatched_orders():
    from wca.data import betfair_exchange

    assert betfair_exchange._normalise_order({"sizeMatched": 0}) is None
    n = betfair_exchange._normalise_order({
        "sizeMatched": 5.0, "averagePriceMatched": 3.0, "side": "BACK",
        "itemDescription": {"runnerDesc": "The Draw", "eventDesc": "A v B",
                            "marketDesc": "Match Odds"},
        "betId": "b1",
    })
    assert n["selection"] == "Draw"
    assert n["odds"] == 3.0
    assert n["stake"] == 5.0


def test_smarkets_normalise_price_percent_to_decimal():
    from wca.data import smarkets

    assert smarkets._normalise_smk_position({"quantity": 0}) is None
    n = smarkets._normalise_smk_position({
        "quantity": 12.0, "avg_price": 25.0, "contract_name": "England",
        "market_name": "1X2", "event_name": "Eng v Spa", "id": "s1",
    })
    assert n["selection"] == "England"
    assert n["odds"] == pytest.approx(4.0)  # 25% -> decimal 4.0


# ===========================================================================
# v2: settled-position auto-settle (venue-truth), conservatism, idempotence.
# ===========================================================================


def test_reconcile_settle_matches_open_ledger_bet(tmp_path):
    db = _db(tmp_path)
    _seed_open_bet(db)  # open Betfair England bet
    ledger = positions_sync.load_open_ledger_bets(db)
    rec = positions_sync.reconcile([], ledger, [_settled_pos()])
    c = rec.as_dict()["counts"]
    assert c["settle"] == 1
    assert c["review"] == 0
    assert c["gone_from_venue"] == 0  # NOT also flagged gone (claimed by settle)
    assert rec.settle[0]["ledger"]["selection"] == "England"
    assert rec.settle[0]["venue"]["settled_pnl"] == 15.0


def test_live_settles_with_venue_pnl_and_is_idempotent(tmp_path, monkeypatch):
    db = _db(tmp_path)
    bet_id = _seed_open_bet(db, stake=10.0, decimal_odds=2.5)  # store would compute +15.0
    monkeypatch.setattr(positions_sync, "refresh_site_projection", lambda: 0)

    # Venue reports the bet WON with a venue-truth P&L of 14.25 (e.g. after
    # commission) — deliberately DIFFERENT from store's recomputed 15.0.
    settled_fetchers = {"betfair": lambda h: [_settled_pos(settled_pnl=14.25)]}

    r1 = positions_sync.run_sync(db, live=True, fetchers={"betfair": lambda: []},
                                 settled_fetchers=settled_fetchers)
    assert r1["mode"] == "LIVE"
    assert len(r1["applied"]["settled"]) == 1
    assert r1["applied"]["settled"][0]["bet_id"] == bet_id

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
    assert row["status"] == "won"
    assert row["settled_pl"] == pytest.approx(14.25)  # VENUE TRUTH, not 15.0
    assert row["settled_ts"] == "2026-06-26T11:00:00Z"
    conn.close()

    count_before = _row_count(db)
    # Re-run with the SAME settled snapshot: the bet is no longer open -> no-op.
    r2 = positions_sync.run_sync(db, live=True, fetchers={"betfair": lambda: []},
                                 settled_fetchers=settled_fetchers)
    assert r2["applied"]["settled"] == []  # not double-settled
    assert _row_count(db) == count_before

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row2 = conn.execute("SELECT settled_pl FROM bets WHERE id=?", (bet_id,)).fetchone()
    assert row2["settled_pl"] == pytest.approx(14.25)  # unchanged on re-run
    conn.close()


def test_shadow_does_not_settle(tmp_path, monkeypatch):
    db = _db(tmp_path)
    bet_id = _seed_open_bet(db)
    monkeypatch.setattr(positions_sync, "refresh_site_projection", lambda: 0)
    settled_fetchers = {"betfair": lambda h: [_settled_pos()]}

    report = positions_sync.run_sync(db, live=False, fetchers={"betfair": lambda: []},
                                     settled_fetchers=settled_fetchers)
    assert report["mode"] == "SHADOW"
    assert report["applied"] is None
    assert report["reconciliation"]["counts"]["settle"] == 1  # PROPOSED only

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
    assert row["status"] == "open"          # NOT settled in shadow
    assert row["settled_pl"] is None
    conn.close()


def test_ambiguous_settle_goes_to_review_not_settled(tmp_path):
    db = _db(tmp_path)
    _seed_open_bet(db)
    _seed_open_bet(db)  # two identical open ledger bets -> ambiguous settle
    ledger = positions_sync.load_open_ledger_bets(db)
    rec = positions_sync.reconcile([], ledger, [_settled_pos()])
    c = rec.as_dict()["counts"]
    assert c["settle"] == 0
    assert c["review"] == 1
    assert rec.review[0]["reason"] == "ambiguous_settle"


def test_settle_requires_unambiguous_result_and_pnl(tmp_path):
    db = _db(tmp_path)
    _seed_open_bet(db)
    ledger = positions_sync.load_open_ledger_bets(db)
    # PLACED/unknown outcome (no won/lost) -> review, never settle.
    rec = positions_sync.reconcile([], ledger, [_settled_pos(result="placed")])
    assert rec.as_dict()["counts"]["settle"] == 0
    assert rec.as_dict()["counts"]["review"] == 1
    # Missing P&L -> review, never settle.
    rec2 = positions_sync.reconcile([], ledger, [_settled_pos(settled_pnl=None)])
    assert rec2.as_dict()["counts"]["settle"] == 0
    assert rec2.as_dict()["counts"]["review"] == 1


def test_settle_contradicted_by_still_open_goes_to_review(tmp_path):
    """A key reported BOTH settled and still-open at the venue is contradictory."""
    db = _db(tmp_path)
    _seed_open_bet(db)
    ledger = positions_sync.load_open_ledger_bets(db)
    rec = positions_sync.reconcile([_venue_pos()], ledger, [_settled_pos()])
    assert rec.as_dict()["counts"]["settle"] == 0
    assert rec.as_dict()["counts"]["review"] == 1


# ===========================================================================
# v2: 24h window filters older settles.
# ===========================================================================


def test_smarkets_settled_window_filters_old(monkeypatch):
    from wca.data import smarkets
    import datetime as _dt

    monkeypatch.setattr(smarkets, "session_login", lambda: "tok")
    recent = (_dt.datetime.utcnow() - _dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (_dt.datetime.utcnow() - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"positions": [
                {"settled": True, "realised_profit": 500, "contract_name": "Recent",
                 "settled_at": recent, "id": "r1"},
                {"settled": True, "realised_profit": 300, "contract_name": "Old",
                 "settled_at": old, "id": "o1"},
            ]}

    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda *a, **k: _Resp(), raising=False)

    out = smarkets.list_settled_positions(since_hours=24)
    sels = {p["selection"] for p in out}
    assert "Recent" in sels
    assert "Old" not in sels  # 48h-old settle filtered out of a 24h window
    # P&L converted from pennies -> GBP.
    rec = next(p for p in out if p["selection"] == "Recent")
    assert rec["settled_pnl"] == pytest.approx(5.0)
    assert rec["result"] == "won"


def test_betfair_cleared_normalise_only_won_lost():
    from wca.data import betfair_exchange

    # PLACED / open outcome -> None (not an unambiguous settle).
    assert betfair_exchange._normalise_cleared_order({"betOutcome": "PLACED"}) is None
    n = betfair_exchange._normalise_cleared_order({
        "betOutcome": "WON", "profit": 12.5, "settledDate": "2026-06-26T10:00:00Z",
        "priceMatched": 2.5, "betId": "b9",
        "itemDescription": {"runnerDesc": "England", "eventDesc": "Eng v Spa",
                            "marketDesc": "Match Odds"},
    })
    assert n["result"] == "won"
    assert n["settled_pnl"] == 12.5
    assert n["selection"] == "England"


# ===========================================================================
# v2: settled-fetch degrades to [] on simulated failure (each venue).
# ===========================================================================


def test_betfair_settled_fetch_degrades(monkeypatch):
    from wca.data import betfair_exchange

    monkeypatch.setattr(betfair_exchange, "_resolve_session_token", lambda: "tok")
    monkeypatch.setattr(betfair_exchange, "_candidate_app_keys", lambda: ["k"])

    def boom(*a, **k):
        raise ConnectionError("cannot reach Betfair")

    monkeypatch.setattr(betfair_exchange, "_rpc", boom)
    assert betfair_exchange.list_cleared_orders() == []
    assert positions_sync.fetch_betfair_settled() == []


def test_smarkets_settled_fetch_degrades(monkeypatch):
    from wca.data import smarkets

    monkeypatch.setattr(smarkets, "session_login", lambda: "tok")
    import requests as _rq

    def boom(*a, **k):
        raise _rq.exceptions.ConnectionError("network down")

    monkeypatch.setattr(_rq, "get", boom, raising=False)
    assert smarkets.list_settled_positions() == []
    assert positions_sync.fetch_smarkets_settled() == []


def test_polymarket_settled_fetch_degrades(monkeypatch):
    from wca import sitedata

    def boom(*a, **k):
        raise RuntimeError("PM data-api down")

    monkeypatch.setattr(sitedata, "settled_pm_positions", boom)
    assert positions_sync.fetch_polymarket_settled() == []


def test_fetch_all_settled_isolates_one_bad_venue():
    def good(h):
        return [_settled_pos(venue="polymarket")]

    def bad(h):
        raise ConnectionError("down")

    rows = positions_sync.fetch_all_settled(24, {"good": good, "bad": bad})
    assert len(rows) == 1


# ===========================================================================
# v2: fetch-only snapshot (no DB access) + apply-from-snapshot round-trip.
# ===========================================================================


def test_fetch_snapshot_has_no_db_access_and_is_valid():
    fetchers = {"betfair": lambda: [_venue_pos()]}
    settled_fetchers = {"betfair": lambda h: [_settled_pos()]}
    snap = positions_sync.fetch_snapshot(
        fetchers=fetchers, settled_fetchers=settled_fetchers,
        settled_lookback_hours=24,
    )
    assert snap["snapshot_version"] == positions_sync.SNAPSHOT_VERSION
    assert snap["settled_lookback_hours"] == 24
    assert snap["counts"] == {"open": 1, "settled": 1}
    assert snap["open_positions"][0]["selection"] == "England"
    assert snap["settled_positions"][0]["result"] == "won"
    # Self-describing + JSON round-trippable.
    import json as _json
    assert _json.loads(_json.dumps(snap, default=str))["counts"]["open"] == 1


def test_apply_from_snapshot_round_trips(tmp_path, monkeypatch):
    db = _db(tmp_path)
    bet_id = _seed_open_bet(db)  # open Betfair England bet
    monkeypatch.setattr(positions_sync, "refresh_site_projection", lambda: 0)

    # Build a snapshot on the "MacBook" (no DB), then apply it on the "mini".
    snap = positions_sync.fetch_snapshot(
        fetchers={"betfair": lambda: []},
        settled_fetchers={"betfair": lambda h: [_settled_pos(settled_pnl=9.0)]},
    )
    import json as _json
    snap = _json.loads(_json.dumps(snap, default=str))  # simulate scp/serialise

    report = positions_sync.apply_snapshot(snap, db, live=True)
    assert report["source"] == "snapshot"
    assert len(report["applied"]["settled"]) == 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
    assert row["status"] == "won"
    assert row["settled_pl"] == pytest.approx(9.0)
    conn.close()


def test_apply_snapshot_shadow_does_not_write(tmp_path, monkeypatch):
    db = _db(tmp_path)
    bet_id = _seed_open_bet(db)
    monkeypatch.setattr(positions_sync, "refresh_site_projection", lambda: 0)
    snap = {
        "snapshot_version": positions_sync.SNAPSHOT_VERSION,
        "open_positions": [],
        "settled_positions": [_settled_pos()],
        "settled_lookback_hours": 24,
    }
    report = positions_sync.apply_snapshot(snap, db, live=False)
    assert report["mode"] == "SHADOW"
    assert report["applied"] is None

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM bets WHERE id=?", (bet_id,)).fetchone()
    assert row["status"] == "open"
    conn.close()


# ===========================================================================
# HARDENING: matcher-safety gate — an unconfirmable venue NEVER auto-closes its
# open ledger bets (the false-close bug fix). THIS IS THE KEY TEST.
# ===========================================================================


def test_unconfirmable_venue_never_gone_from_venue():
    """A ledger bet whose venue is auth_failed / empty / error stays OPEN and is
    routed to review — NEVER gone_from_venue. This is what kills the 30 false
    closes seen in the SHADOW dry-run.
    """
    from wca.venues import canon_platform

    ledger = [
        {"id": 1, "platform": "smarkets", "selection": "Draw",
         "market": "1X2", "match_desc": "A vs B"},
        {"id": 2, "platform": "Betfair", "selection": "England",
         "market": "1X2", "match_desc": "Eng v Spa"},
        {"id": 3, "platform": "polymarket", "selection": "Brazil",
         "market": "Winner", "match_desc": "World Cup"},
    ]
    venue_status = {
        canon_platform("smarkets"): positions_sync.VENUE_AUTH_FAILED,  # 401 login
        canon_platform("Betfair"): positions_sync.VENUE_ERROR,         # net down / throttled
        canon_platform("polymarket"): positions_sync.VENUE_EMPTY,      # authed but 0 positions
    }
    # No open venue positions came back for ANY venue (all unconfirmable).
    rec = positions_sync.reconcile([], ledger, [], venue_status)
    c = rec.as_dict()["counts"]
    assert c["gone_from_venue"] == 0          # <-- the safety property
    assert c["review"] == 3                    # all three routed to review
    assert {r["reason"] for r in rec.review} == {"venue_unavailable"}
    # Bets are untouched (still in review payload, not gone).
    review_ids = {r["ledger_bets"][0]["id"] for r in rec.review}
    assert review_ids == {1, 2, 3}


def test_canon_venue_status_keyed_correctly():
    """The safety gate keys venue status by CANON venue, so a ledger
    ``platform`` of 'Betfair' resolves to the same status as 'betfair_ex'."""
    from wca.venues import canon_platform

    ledger = [{"id": 9, "platform": "Betfair", "selection": "Draw",
               "market": "1X2", "match_desc": "A vs B"}]
    venue_status = {canon_platform("Betfair"): positions_sync.VENUE_ERROR}
    rec = positions_sync.reconcile([], ledger, [], venue_status)
    assert rec.as_dict()["counts"]["gone_from_venue"] == 0
    assert rec.as_dict()["counts"]["review"] == 1


def test_gone_from_venue_only_when_ok_and_truly_absent():
    """gone_from_venue is permitted ONLY for a VENUE_OK venue that genuinely
    lacks the position; a confirmed-present position matches instead."""
    from wca.venues import canon_platform

    ledger = [
        {"id": 1, "platform": "Betfair", "selection": "England",
         "market": "1X2", "match_desc": "Eng v Spa"},   # gone (venue ok, absent)
        {"id": 2, "platform": "Betfair", "selection": "Spain",
         "market": "1X2", "match_desc": "Eng v Spa"},    # present at venue
    ]
    venue = [_venue_pos(selection="Spain", fixture_or_event="Eng v Spa")]
    venue_status = {canon_platform("Betfair"): positions_sync.VENUE_OK}
    rec = positions_sync.reconcile(venue, ledger, [], venue_status)
    c = rec.as_dict()["counts"]
    assert c["matched"] == 1                       # Spain matched
    assert c["gone_from_venue"] == 1               # England safely gone
    assert rec.gone_from_venue[0]["selection"] == "England"
    assert c["review"] == 0


def test_empty_injected_fetcher_is_unconfirmable(tmp_path, monkeypatch):
    """An injected fetcher returning [] yields VENUE_EMPTY (authed-but-empty) and
    therefore its bets are NOT auto-closed — empty alone is never confirmable."""
    from wca.venues import canon_platform

    db = _db(tmp_path)
    bet_id = _seed_open_bet(db)
    monkeypatch.setattr(positions_sync, "refresh_site_projection", lambda: 0)

    report = positions_sync.run_sync(db, live=True, fetchers={"betfair": lambda: []})
    assert report["venue_status"][canon_platform("betfair")] == positions_sync.VENUE_EMPTY
    assert report["applied"]["closed"] == []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM bets WHERE id=?", (bet_id,)).fetchone()
    assert row["status"] == "open"
    conn.close()


def test_venue_status_in_snapshot_and_report():
    """fetch_snapshot stamps a venue_status map and apply/report surfaces it."""
    from wca.venues import canon_platform

    snap = positions_sync.fetch_snapshot(
        fetchers={"betfair": lambda: [_venue_pos()], "smarkets": lambda: []},
    )
    assert snap["venue_status"][canon_platform("betfair")] == positions_sync.VENUE_OK
    assert snap["venue_status"][canon_platform("smarkets")] == positions_sync.VENUE_EMPTY


def test_classify_fetch_statuses():
    """_classify_fetch maps each outcome to the right VENUE_* status."""
    cf = positions_sync._classify_fetch
    # auth probe says not authed -> auth_failed (fetcher not even called).
    assert cf("v", lambda: [_venue_pos()], lambda: False)[0] == positions_sync.VENUE_AUTH_FAILED
    # raises -> error.
    def boom():
        raise ConnectionError("down")
    assert cf("v", boom, lambda: True)[0] == positions_sync.VENUE_ERROR
    # authed + empty -> empty.
    assert cf("v", lambda: [], lambda: True)[0] == positions_sync.VENUE_EMPTY
    # authed + rows -> ok.
    st, rows = cf("v", lambda: [_venue_pos()], lambda: True)
    assert st == positions_sync.VENUE_OK and len(rows) == 1


# ===========================================================================
# HARDENING: Smarkets create-session payload (documented fields).
# ===========================================================================


def test_smarkets_session_payload_includes_documented_fields(monkeypatch):
    from wca.data import smarkets

    smarkets._CACHED_SESSION = None
    monkeypatch.delenv("SMARKETS_API_TOKEN", raising=False)
    monkeypatch.setenv("SMARKETS_USERNAME", "u")
    monkeypatch.setenv("SMARKETS_PASSWORD", "p")

    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "sess-tok"}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    import requests as _rq
    monkeypatch.setattr(_rq, "post", fake_post, raising=False)

    tok = smarkets.session_login()
    assert tok == "sess-tok"
    body = captured["json"]
    assert body["username"] == "u"
    assert body["password"] == "p"
    assert body["remember"] is True
    assert body["reopen_account"] is False
    assert body["use_auth_v2"] is False
    smarkets._CACHED_SESSION = None  # don't leak the cached token to other tests.


# ===========================================================================
# HARDENING: Betfair session-token persistence (env + disk cache, no re-login).
# ===========================================================================


def test_betfair_token_from_env_no_login(monkeypatch):
    from wca.data import betfair_exchange

    betfair_exchange._CACHED_TOKEN = None
    monkeypatch.setenv("BETFAIR_SESSION_TOKEN", "env-tok")

    def boom(*a, **k):
        raise AssertionError("must not hit the login endpoint when env token set")

    monkeypatch.setattr(betfair_exchange.requests, "post", boom, raising=False)
    assert betfair_exchange._resolve_session_token() == "env-tok"


def test_betfair_token_reused_from_disk_cache(tmp_path, monkeypatch):
    from wca.data import betfair_exchange

    betfair_exchange._CACHED_TOKEN = None
    monkeypatch.delenv("BETFAIR_SESSION_TOKEN", raising=False)
    cache = tmp_path / ".betfair_session.json"
    monkeypatch.setattr(betfair_exchange, "_SESSION_CACHE_PATH", str(cache))
    monkeypatch.setattr(betfair_exchange, "_SESSION_CACHE_TTL_SECONDS", 3600)

    # Mint once (interactive login) -> writes the cache.
    monkeypatch.setenv("BETFAIR_USERNAME", "u")
    monkeypatch.setenv("BETFAIR_PASSWORD", "p")
    monkeypatch.setenv("BETFAIR_APP_KEY", "ak")

    class _LoginResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "SUCCESS", "token": "minted-tok"}

    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _LoginResp()

    monkeypatch.setattr(betfair_exchange.requests, "post", fake_post, raising=False)
    assert betfair_exchange._resolve_session_token() == "minted-tok"
    assert calls["n"] == 1
    assert cache.exists()

    # New "process": clear the in-process cache; the disk cache must be reused
    # WITHOUT another login call.
    betfair_exchange._CACHED_TOKEN = None

    def boom(*a, **k):
        raise AssertionError("must not re-login when a fresh disk cache exists")

    monkeypatch.setattr(betfair_exchange.requests, "post", boom, raising=False)
    assert betfair_exchange._resolve_session_token() == "minted-tok"
    betfair_exchange._CACHED_TOKEN = None


def test_betfair_disk_cache_expires_by_mtime(tmp_path, monkeypatch):
    from wca.data import betfair_exchange
    import json as _json
    import os as _os
    import time as _time

    betfair_exchange._CACHED_TOKEN = None
    monkeypatch.delenv("BETFAIR_SESSION_TOKEN", raising=False)
    cache = tmp_path / ".betfair_session.json"
    cache.write_text(_json.dumps({"session_token": "stale-tok"}))
    # Age the file well past the TTL.
    old = _time.time() - 10_000
    _os.utime(cache, (old, old))
    monkeypatch.setattr(betfair_exchange, "_SESSION_CACHE_PATH", str(cache))
    monkeypatch.setattr(betfair_exchange, "_SESSION_CACHE_TTL_SECONDS", 100)
    assert betfair_exchange._read_cached_token() is None  # expired -> re-mint


def test_betfair_cache_write_chmod_0600(tmp_path, monkeypatch):
    from wca.data import betfair_exchange
    import os as _os
    import stat as _stat

    cache = tmp_path / "sub" / ".betfair_session.json"
    monkeypatch.setattr(betfair_exchange, "_SESSION_CACHE_PATH", str(cache))
    betfair_exchange._write_cached_token("secret-tok")
    assert cache.exists()
    mode = _stat.S_IMODE(_os.stat(cache).st_mode)
    assert mode == 0o600  # owner-only — never world-readable.


# ===========================================================================
# HARDENING: Betfair open-position name resolution (marketId/selectionId -> name).
# ===========================================================================


def test_betfair_build_name_maps_from_catalogue():
    from wca.data import betfair_exchange

    catalogue = [{
        "marketId": "1.234",
        "event": {"id": "ev1", "name": "England v Spain"},
        "runners": [
            {"selectionId": 47973, "runnerName": "England"},
            {"selectionId": 47974, "runnerName": "Spain"},
            {"selectionId": 58805, "runnerName": "The Draw"},
        ],
    }]
    ev, runners = betfair_exchange.build_name_maps(catalogue)
    assert ev["1.234"] == "England v Spain"
    assert runners[("1.234", 47973)] == "England"
    assert runners[("1.234", 58805)] == "Draw"  # "The Draw" normalised


def test_betfair_resolve_order_names_backfills(monkeypatch):
    from wca.data import betfair_exchange

    # An open position with only raw IDs (no itemDescription was returned).
    positions = [{
        "venue": "Betfair", "market": "1.234", "selection": "47973",
        "fixture_or_event": "", "market_id": "1.234", "selection_id": 47973,
    }]

    def fake_rpc(method, params, token, app_key=None):
        assert method == "listMarketCatalogue"
        assert params["filter"]["marketIds"] == ["1.234"]
        assert "EVENT" in params["marketProjection"]
        assert "RUNNER_DESCRIPTION" in params["marketProjection"]
        return [{
            "marketId": "1.234",
            "event": {"name": "England v Spain"},
            "runners": [{"selectionId": 47973, "runnerName": "England"}],
        }]

    monkeypatch.setattr(betfair_exchange, "_rpc", fake_rpc)
    out = betfair_exchange.resolve_order_names(positions, "tok", app_key="ak")
    assert out[0]["fixture_or_event"] == "England v Spain"
    assert out[0]["selection"] == "England"


def test_betfair_resolve_order_names_degrades_on_failure(monkeypatch):
    from wca.data import betfair_exchange

    positions = [{
        "venue": "Betfair", "market": "1.234", "selection": "47973",
        "fixture_or_event": "", "market_id": "1.234", "selection_id": 47973,
    }]

    def boom(*a, **k):
        raise ConnectionError("catalogue down")

    monkeypatch.setattr(betfair_exchange, "_rpc", boom)
    out = betfair_exchange.resolve_order_names(positions, "tok")
    # Raw IDs are LEFT IN PLACE (never raises) when the catalogue call fails.
    assert out[0]["selection"] == "47973"
    assert out[0]["fixture_or_event"] == ""


def test_betfair_resolve_order_names_skips_already_named(monkeypatch):
    from wca.data import betfair_exchange

    # A position that already carries a runner name (itemDescription was present)
    # must NOT be rewritten, and no catalogue call is made.
    positions = [{
        "venue": "Betfair", "market": "Match Odds", "selection": "England",
        "fixture_or_event": "England v Spain", "market_id": "1.234",
        "selection_id": 47973,
    }]

    def must_not_call(*a, **k):
        raise AssertionError("no catalogue call when names already present")

    monkeypatch.setattr(betfair_exchange, "_rpc", must_not_call)
    out = betfair_exchange.resolve_order_names(positions, "tok")
    assert out[0]["selection"] == "England"
