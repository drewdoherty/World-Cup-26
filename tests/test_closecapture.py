"""Tests for automatic closing-line capture (wca.closecapture).

These exercise:

* selection -> leg mapping across the ledger's real spellings (team names,
  "Draw" / "The Draw", Polymarket "<Team> Yes" / "<Team> No" shares);
* the de-vigged consensus close from the last pre-kickoff snapshot — a
  post-kickoff pull must never contaminate the close;
* stamping semantics: only open 1X2 bets with a NULL ``closing_odds`` on
  fixtures that have kicked off; exact-score / multi-leg / future-fixture /
  already-stamped / settled bets are untouched; re-runs are no-ops;
* CLV arithmetic against the ledger-wide ``backed / close - 1`` convention;
* the dry-run mode and the ``capture_closes_db`` wrapper; and
* the settle CLI falling back to an auto-captured close (explicit
  ``--closing-odds`` still overriding it).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

import pytest

from wca import closecapture
from wca.ledger import store

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTLE_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "wca_settle.py")

# Fixture under test: kickoff 01:00Z, two books at the 00:56Z close.
_KICKOFF = "2026-06-13T01:00:00+00:00"
_PRE_TS = "2026-06-13T00:56:28.496652+00:00"
_EARLY_TS = "2026-06-12T20:00:00.000000+00:00"
_POST_TS = "2026-06-13T01:10:59.131135+00:00"
_NOW = "2026-06-13T01:05:00+00:00"
_MID = "c12986f447a515fbe641addd786dbb24"

# Close (book, USA, Draw, Paraguay): de-vig each book proportionally, mean.
_CLOSE_BOOKS = [("booka", 2.0, 3.2, 4.0), ("bookb", 2.1, 3.0, 4.2)]


def _devig_triple(books):
    legs = ("home", "draw", "away")
    devigged = []
    for _book, h, d, a in books:
        implied = {"home": 1 / h, "draw": 1 / d, "away": 1 / a}
        total = sum(implied.values())
        devigged.append({leg: implied[leg] / total for leg in legs})
    mean = {leg: sum(d[leg] for d in devigged) / len(devigged) for leg in legs}
    total = sum(mean.values())
    return {leg: mean[leg] / total for leg in legs}


def _expected_close_triple():
    return _devig_triple(_CLOSE_BOOKS)


def _insert_h2h(con, ts, book, outcome, dec, mid=_MID,
                home="USA", away="Paraguay", kickoff=_KICKOFF):
    raw = {
        "event_id": mid,
        "commence_time": kickoff,
        "home_team": home,
        "away_team": away,
        "bookmaker_key": book,
        "market": "h2h",
        "outcome_name": outcome,
        "decimal_odds": dec,
    }
    con.execute(
        "INSERT INTO odds_snapshots (ts_utc, source, match_id, market, "
        "selection, decimal_odds, raw) VALUES (?, 'theoddsapi', ?, 'h2h', ?, ?, ?)",
        (ts, mid, outcome, dec, json.dumps(raw)),
    )


def _insert_snapshot_set(con, ts, books=_CLOSE_BOOKS, **kwargs):
    for book, home_dec, draw_dec, away_dec in books:
        _insert_h2h(con, ts, book, "USA", home_dec, **kwargs)
        _insert_h2h(con, ts, book, "Draw", draw_dec, **kwargs)
        _insert_h2h(con, ts, book, "Paraguay", away_dec, **kwargs)


def _insert_bet(con, match_desc, market, selection, odds,
                status="open", closing_odds=None, clv=None,
                ts_utc="2026-06-12T22:00:00", notes=None):
    cur = con.execute(
        "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
        "platform, decimal_odds, stake, status, closing_odds, clv, notes) "
        "VALUES (?, 'm', ?, ?, ?, 'test', ?, 1.0, ?, ?, ?, ?)",
        (ts_utc, match_desc, market, selection, odds, status,
         closing_odds, clv, notes),
    )
    return cur.lastrowid


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "wca.db")
    # init_db now provisions settled_ts via the ledger's idempotent migration
    # (added in #42), so the column is already present here. The settle CLI
    # writes it unconditionally; nothing extra to ALTER.
    store.init_db(path)
    con = sqlite3.connect(path)
    yield con, path
    con.close()


@pytest.fixture()
def seeded(db):
    """DB with an early pull, the 00:56Z close and a post-kickoff pull."""
    con, path = db
    _insert_snapshot_set(con, _EARLY_TS,
                         books=[("booka", 2.4, 3.2, 3.3), ("bookb", 2.5, 3.0, 3.4)])
    _insert_snapshot_set(con, _PRE_TS)
    # Post-kickoff prices have steamed massively — must not be the close.
    _insert_snapshot_set(con, _POST_TS,
                         books=[("booka", 1.4, 4.5, 9.0), ("bookb", 1.45, 4.4, 9.5)])
    con.commit()
    return con, path


# ---------------------------------------------------------------------------
# selection -> leg mapping.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selection,expected",
    [
        ("Paraguay", ("away", False)),
        ("USA", ("home", False)),
        ("United States", ("home", False)),  # alias resolution
        ("Draw", ("draw", False)),
        ("The Draw", ("draw", False)),
        ("the draw", ("draw", False)),
        ("Paraguay Yes", ("away", False)),
        ("USA No", ("home", True)),
        ("Draw No", ("draw", True)),
        ("Over 2.5", None),
        ("United States 0-1 Paraguay", None),  # exact score
        ("Netherlands/Brazil/Paraguay all win", None),
        ("", None),
        (None, None),
    ],
)
def test_selection_leg(selection, expected):
    assert closecapture.selection_leg(selection, "USA", "Paraguay") == expected


def test_fair_closing_odds_yes_no_complement():
    triple = {"home": 0.5, "draw": 0.3, "away": 0.2}
    assert closecapture.fair_closing_odds(triple, "away", False) == pytest.approx(5.0)
    assert closecapture.fair_closing_odds(triple, "away", True) == pytest.approx(1.25)
    assert closecapture.fair_closing_odds({"home": 1.0, "draw": 0.0, "away": 0.0},
                                          "home", False) is None
    assert closecapture.fair_closing_odds({}, "home", False) is None


def test_is_1x2_market():
    for market in ("h2h", "Full-time result", "Full Time Result", "Match Odds",
                   "Match Winner", "MATCH", "pm_moneyline"):
        assert closecapture.is_1x2_market(market)
    for market in ("Exact Score", "ACCA", "acca_treble", "totals", "btts",
                   "outright_golden_boot", "polymarket", None, 3):
        assert not closecapture.is_1x2_market(market)


# ---------------------------------------------------------------------------
# Consensus close.
# ---------------------------------------------------------------------------


def test_consensus_close_uses_last_pre_kickoff_pull(seeded):
    con, _ = seeded
    close = closecapture.consensus_close(con, _MID, "USA", "Paraguay", _KICKOFF)
    assert close is not None
    assert close["ts"] == _PRE_TS
    assert close["books"] == 2
    expected = _expected_close_triple()
    for leg in ("home", "draw", "away"):
        assert close["triple"][leg] == pytest.approx(expected[leg])


def test_consensus_close_none_without_pre_kickoff_rows(db):
    con, _ = db
    _insert_snapshot_set(con, _POST_TS)  # only a post-kickoff pull
    con.commit()
    assert closecapture.consensus_close(
        con, _MID, "USA", "Paraguay", _KICKOFF
    ) is None


def test_match_index_uses_latest_raw(seeded):
    con, _ = seeded
    index = closecapture.match_index(con)
    assert index[_MID]["home"] == "USA"
    assert index[_MID]["away"] == "Paraguay"
    assert index[_MID]["kickoff"] == _KICKOFF


# ---------------------------------------------------------------------------
# Stamping semantics.
# ---------------------------------------------------------------------------


def test_capture_stamps_1x2_bets_with_fair_close_and_clv(seeded):
    con, _ = seeded
    expected = _expected_close_triple()
    fair_away = 1.0 / expected["away"]
    fair_draw = 1.0 / expected["draw"]

    bet_h2h = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6)
    bet_draw = _insert_bet(con, "USA vs Paraguay", "Full-time result", "Draw", 3.4)
    bet_pm = _insert_bet(con, "United States vs Paraguay", "pm_moneyline",
                         "Paraguay Yes", 4.348)
    bet_no = _insert_bet(con, "USA vs Paraguay", "pm_moneyline", "USA No", 1.8)
    con.commit()

    records = closecapture.capture_closes(con, _NOW)
    assert {r["bet_id"] for r in records} == {bet_h2h, bet_draw, bet_pm, bet_no}

    rows = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT id, closing_odds, clv FROM bets WHERE closing_odds IS NOT NULL"
    )}
    assert rows[bet_h2h][0] == pytest.approx(fair_away)
    assert rows[bet_h2h][1] == pytest.approx(4.6 / fair_away - 1.0)
    assert rows[bet_draw][0] == pytest.approx(fair_draw)
    assert rows[bet_draw][1] == pytest.approx(3.4 / fair_draw - 1.0)
    assert rows[bet_pm][0] == pytest.approx(fair_away)
    assert rows[bet_pm][1] == pytest.approx(4.348 / fair_away - 1.0)
    fair_home_no = 1.0 / (1.0 - expected["home"])
    assert rows[bet_no][0] == pytest.approx(fair_home_no)
    assert rows[bet_no][1] == pytest.approx(1.8 / fair_home_no - 1.0)

    # Status untouched — capture is not settlement.
    statuses = {r[0] for r in con.execute("SELECT status FROM bets")}
    assert statuses == {"open"}


def test_capture_skips_what_it_must(seeded):
    con, _ = seeded
    skipped = [
        _insert_bet(con, "United States vs Paraguay", "Exact Score",
                    "United States 0-1 Paraguay", 11.1),
        _insert_bet(con, "Treble: Netherlands + Brazil + Paraguay", "ACCA",
                    "Netherlands/Brazil/Paraguay all win", 12.87),
        _insert_bet(con, "USA (qualifier)", "MATCH", "USA", 2.2),  # unsplittable
        _insert_bet(con, "Brazil vs Morocco", "Full-time result", "Morocco", 3.0),
        # ^ no snapshots for this fixture at all
        _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.2,
                    status="lost"),
    ]
    pre_stamped = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.2,
                              closing_odds=4.0, clv=0.05)
    con.commit()

    records = closecapture.capture_closes(con, _NOW)
    assert records == []
    for bet_id in skipped:
        row = con.execute(
            "SELECT closing_odds, clv FROM bets WHERE id=?", (bet_id,)
        ).fetchone()
        assert row == (None, None)
    row = con.execute(
        "SELECT closing_odds, clv FROM bets WHERE id=?", (pre_stamped,)
    ).fetchone()
    assert row == (4.0, 0.05)  # manual stamp never overwritten


def test_capture_waits_for_kickoff(seeded):
    con, _ = seeded
    bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6)
    con.commit()

    before = "2026-06-13T00:59:00+00:00"
    assert closecapture.capture_closes(con, before) == []
    row = con.execute("SELECT closing_odds FROM bets WHERE id=?", (bet,)).fetchone()
    assert row == (None,)

    records = closecapture.capture_closes(con, _NOW)
    assert [r["bet_id"] for r in records] == [bet]


def test_capture_is_idempotent(seeded):
    con, _ = seeded
    _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6)
    con.commit()
    assert len(closecapture.capture_closes(con, _NOW)) == 1
    assert closecapture.capture_closes(con, _NOW) == []


def test_capture_matches_reversed_fixture_order(seeded):
    con, _ = seeded
    bet = _insert_bet(con, "Paraguay vs USA", "h2h", "USA", 2.3)
    con.commit()
    records = closecapture.capture_closes(con, _NOW)
    assert [r["bet_id"] for r in records] == [bet]
    # USA is the API home team regardless of the ledger's fixture order.
    expected = _expected_close_triple()
    assert records[0]["closing_odds"] == pytest.approx(1.0 / expected["home"])


def test_capture_dry_run_writes_nothing(seeded):
    con, _ = seeded
    bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6)
    con.commit()
    records = closecapture.capture_closes(con, _NOW, dry_run=True)
    assert [r["bet_id"] for r in records] == [bet]
    row = con.execute(
        "SELECT closing_odds, clv FROM bets WHERE id=?", (bet,)
    ).fetchone()
    assert row == (None, None)


def test_capture_closes_db_wrapper(seeded):
    con, path = seeded
    bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6)
    con.commit()
    records = closecapture.capture_closes_db(path, now_utc=_NOW)
    assert [r["bet_id"] for r in records] == [bet]
    row = con.execute(
        "SELECT closing_odds FROM bets WHERE id=?", (bet,)
    ).fetchone()
    assert row[0] is not None


def test_capture_closes_db_tolerates_fresh_db(tmp_path):
    # No tables at all — wrapper must return [] rather than raise.
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    assert closecapture.capture_closes_db(path, now_utc=_NOW) == []


# ---------------------------------------------------------------------------
# Rematch disambiguation (the team-pair collision guard).
# ---------------------------------------------------------------------------

# A second USA-Paraguay meeting (a hypothetical knockout rematch) — same
# canonical pair, different match_id, much later kickoff, distinct close.
_MID2 = "aaa_ko_rematch"
_KO2 = "2026-07-01T18:00:00+00:00"
_PRE2_TS = "2026-07-01T17:55:00+00:00"
_REMATCH_BOOKS = [("booka", 1.5, 4.5, 6.5), ("bookb", 1.55, 4.4, 6.8)]


def test_pick_event_single_candidate_ignores_placement():
    index = {"only": {"home": "USA", "away": "Paraguay", "kickoff": _KICKOFF}}
    assert closecapture._pick_event(["only"], index, "2099-01-01T00:00:00") == "only"
    assert closecapture._pick_event([], index, "2026-01-01") is None


def test_pick_event_chooses_earliest_kickoff_at_or_after_placement():
    index = {
        "grp": {"home": "USA", "away": "Paraguay", "kickoff": _KICKOFF},   # Jun 13
        "ko": {"home": "USA", "away": "Paraguay", "kickoff": _KO2},        # Jul 01
    }
    # Bet placed before the group match -> group event.
    assert closecapture._pick_event(
        ["ko", "grp"], index, "2026-06-11T10:00:00"
    ) == "grp"
    # Bet placed between the two -> the rematch is its fixture.
    assert closecapture._pick_event(
        ["grp", "ko"], index, "2026-06-20T10:00:00"
    ) == "ko"
    # Bet placed after both kickoffs -> none qualifies (ambiguous, skip).
    assert closecapture._pick_event(
        ["grp", "ko"], index, "2026-08-01T10:00:00"
    ) is None


def _seed_rematch(con):
    """Group meeting (kicked off) + a future rematch, both fully snapshotted."""
    _insert_snapshot_set(con, _PRE_TS)  # group close, kickoff _KICKOFF
    _insert_snapshot_set(con, _PRE2_TS, books=_REMATCH_BOOKS,
                         mid=_MID2, kickoff=_KO2)
    con.commit()


def test_capture_does_not_cross_contaminate_rematch(db):
    con, _ = db
    _seed_rematch(con)
    grp_away = 1.0 / _devig_triple(_CLOSE_BOOKS)["away"]
    ko_away = 1.0 / _devig_triple(_REMATCH_BOOKS)["away"]

    # Bet on the group match (placed before it) and a bet on the rematch
    # (placed after the group match) — same fixture string, same selection.
    grp_bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6,
                          ts_utc="2026-06-11T10:00:00")
    ko_bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 5.5,
                         ts_utc="2026-06-20T10:00:00")
    con.commit()

    # At a moment after the group match but before the rematch: only the
    # group bet is stamped, with the GROUP close (not the steamed rematch).
    skipped = []
    records = closecapture.capture_closes(
        con, "2026-06-26T00:00:00+00:00", skipped_out=skipped
    )
    assert [r["bet_id"] for r in records] == [grp_bet]
    assert records[0]["closing_odds"] == pytest.approx(grp_away)
    assert records[0]["close_ts"] == _PRE_TS
    assert any(s["bet_id"] == ko_bet and s["reason"] == "future" for s in skipped)

    # After the rematch kicks off, the rematch bet gets the REMATCH close;
    # the group bet is already stamped (idempotent).
    records2 = closecapture.capture_closes(con, "2026-07-02T00:00:00+00:00")
    assert [r["bet_id"] for r in records2] == [ko_bet]
    assert records2[0]["closing_odds"] == pytest.approx(ko_away)
    assert records2[0]["close_ts"] == _PRE2_TS

    rows = {r[0]: r[1] for r in con.execute(
        "SELECT id, closing_odds FROM bets WHERE closing_odds IS NOT NULL"
    )}
    assert rows[grp_bet] == pytest.approx(grp_away)
    assert rows[ko_bet] == pytest.approx(ko_away)


def test_capture_order_independent_for_rematch(db):
    # The collision bug was order-dependent (last hex-id wins); assert the
    # outcome is identical whichever match_id sorts first.
    con, _ = db
    # Insert the FUTURE event first, the past event second.
    _insert_snapshot_set(con, _PRE2_TS, books=_REMATCH_BOOKS,
                         mid="0000_future", kickoff=_KO2)
    _insert_snapshot_set(con, _PRE_TS, mid="zzzz_past", kickoff=_KICKOFF)
    con.commit()
    grp_away = 1.0 / _devig_triple(_CLOSE_BOOKS)["away"]

    bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6,
                      ts_utc="2026-06-11T10:00:00")
    con.commit()
    records = closecapture.capture_closes(con, "2026-06-26T00:00:00+00:00")
    assert [r["bet_id"] for r in records] == [bet]
    assert records[0]["closing_odds"] == pytest.approx(grp_away)  # past close, not future


# ---------------------------------------------------------------------------
# Skip surfacing + exclude notes.
# ---------------------------------------------------------------------------


def test_capture_surfaces_actionable_skips(seeded):
    con, _ = seeded
    # Unsplittable desc with NO KO hint -> unsplittable skip.
    unsplit = _insert_bet(con, "USA (qualifier)", "MATCH", "USA", 2.2)
    # Unsplittable desc whose KO hint matches no snapshot fixture -> unsplittable.
    no_match = _insert_bet(con, "Canada (qualifier)", "MATCH", "Canada", 2.0,
                           notes="KO 2026-06-13T01:00Z")
    unmatched = _insert_bet(con, "Brazil vs Morocco", "Full-time result",
                            "Morocco", 3.0)
    con.commit()

    skipped = []
    records = closecapture.capture_closes(con, _NOW, skipped_out=skipped)
    assert records == []
    by_id = {s["bet_id"]: s["reason"] for s in skipped}
    assert by_id[unsplit] == "unsplittable"
    assert by_id[no_match] == "unsplittable"  # KO hint found no unique fixture
    assert by_id[unmatched] == "unmatched"
    assert closecapture.ACTIONABLE_SKIPS == frozenset(
        {"unsplittable", "ambiguous", "unmatched"}
    )


def test_capture_via_ko_hint_for_unsplittable_offer_bet(seeded):
    con, _ = seeded
    # An offer/qualifier bet with no "Home vs Away" desc, matched by its KO
    # hint + selection team to the seeded USA-Paraguay fixture (kickoff 01:00).
    bet = _insert_bet(con, "USA (qualifier)", "MATCH", "USA", 2.2,
                      notes="Account 2 qualifier; KO 2026-06-13T01:00Z")
    con.commit()
    records = closecapture.capture_closes(con, _NOW)
    assert [r["bet_id"] for r in records] == [bet]
    expected = _expected_close_triple()
    assert records[0]["closing_odds"] == pytest.approx(1.0 / expected["home"])


def test_ko_hint_parsing():
    assert closecapture.ko_hint("foo; KO 2026-06-12T19:00Z") == "2026-06-12T19:00"
    assert closecapture.ko_hint("KO 2026-06-13T01:02:30+00:00") == "2026-06-13T01:02:30"
    assert closecapture.ko_hint("no hint here") is None
    assert closecapture.ko_hint(None) is None


def test_resolve_by_ko_hint_requires_unique_window_match():
    index = {
        "par": {"home": "USA", "away": "Paraguay",
                "kickoff": "2026-06-13T01:02:00+00:00"},
        "aus": {"home": "USA", "away": "Australia",
                "kickoff": "2026-06-19T19:00:00+00:00"},
    }
    # Within 3h of the first fixture only -> unique.
    assert closecapture._resolve_by_ko_hint("USA", "2026-06-13T01:00", index) == "par"
    # A KO hint far from every fixture -> no match.
    assert closecapture._resolve_by_ko_hint("USA", "2026-06-15T12:00", index) is None
    # No hint -> no match.
    assert closecapture._resolve_by_ko_hint("USA", None, index) is None


def test_capture_honors_exclude_note(seeded):
    con, _ = seeded
    excluded = _insert_bet(
        con, "USA vs Paraguay", "Full-time result", "Paraguay", 4.1,
        notes="Betfred qualifier on mum's account; exclude from CLV/calibration",
    )
    con.commit()
    skipped = []
    records = closecapture.capture_closes(con, _NOW, skipped_out=skipped)
    assert excluded not in [r["bet_id"] for r in records]
    assert any(s["bet_id"] == excluded and s["reason"] == "excluded"
               for s in skipped)
    row = con.execute(
        "SELECT closing_odds FROM bets WHERE id=?", (excluded,)
    ).fetchone()
    assert row == (None,)


def test_bare_ts():
    assert closecapture._bare_ts("2026-06-13T01:00:00+00:00") == "2026-06-13T01:00:00"
    assert closecapture._bare_ts("2026-06-13T01:00:00Z") == "2026-06-13T01:00:00"
    assert closecapture._bare_ts("2026-06-11T10:12:38") == "2026-06-11T10:12:38"
    assert closecapture._bare_ts("2026-06-13T00:56:28.49+00:00") == "2026-06-13T00:56:28.49"
    assert closecapture._bare_ts(None) == ""


# ---------------------------------------------------------------------------
# rebackfill_fair_closes: overwrite legacy raw-quote closes onto the fair basis.
# ---------------------------------------------------------------------------


def test_rebackfill_converts_raw_close_and_stamps_unclosed(seeded):
    con, _ = seeded
    expected = _expected_close_triple()
    fair_away = 1.0 / expected["away"]

    # A legacy SETTLED bet carrying a raw single-book quote (4.0) as its close.
    legacy = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.2,
                         status="lost", closing_odds=4.0, clv=4.2 / 4.0 - 1)
    # A SETTLED bet with NO close yet -> rebackfill should add the fair close.
    unclosed = _insert_bet(con, "USA vs Paraguay", "Full-time result", "Paraguay",
                           4.1, status="won", closing_odds=None, clv=None)
    con.commit()

    records = closecapture.rebackfill_fair_closes(con, _NOW)
    by_id = {r["bet_id"]: r for r in records}

    # Legacy raw close 4.0 -> fair close, clv recomputed; flagged changed.
    assert by_id[legacy]["old_closing"] == 4.0
    assert by_id[legacy]["new_closing"] == pytest.approx(fair_away)
    assert by_id[legacy]["changed"] is True
    # Previously-unclosed bet gains a fair close.
    assert by_id[unclosed]["old_closing"] is None
    assert by_id[unclosed]["new_closing"] == pytest.approx(fair_away)

    rows = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT id, closing_odds, clv, status FROM bets")}
    assert rows[legacy][0] == pytest.approx(fair_away)
    assert rows[legacy][1] == pytest.approx(4.2 / fair_away - 1)
    assert rows[legacy][2] == "lost"  # status + settled_pl untouched
    assert rows[unclosed][0] == pytest.approx(fair_away)


def test_rebackfill_is_idempotent(seeded):
    con, _ = seeded
    _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.2,
                status="lost", closing_odds=4.0)
    con.commit()
    first = closecapture.rebackfill_fair_closes(con, _NOW)
    assert any(r["changed"] for r in first)
    second = closecapture.rebackfill_fair_closes(con, _NOW)
    # Re-running changes nothing (already on the fair basis).
    assert all(not r["changed"] for r in second)


def test_rebackfill_dry_run_writes_nothing(seeded):
    con, _ = seeded
    bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.2,
                      status="lost", closing_odds=4.0)
    con.commit()
    records = closecapture.rebackfill_fair_closes(con, _NOW, dry_run=True)
    assert any(r["bet_id"] == bet and r["changed"] for r in records)
    row = con.execute("SELECT closing_odds FROM bets WHERE id=?", (bet,)).fetchone()
    assert row == (4.0,)  # unchanged on disk


def test_rebackfill_honors_exclude_note(seeded):
    con, _ = seeded
    excl = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.1,
                       status="lost", closing_odds=4.0,
                       notes="exclude from CLV/calibration")
    con.commit()
    records = closecapture.rebackfill_fair_closes(con, _NOW)
    assert excl not in [r["bet_id"] for r in records]
    row = con.execute("SELECT closing_odds FROM bets WHERE id=?", (excl,)).fetchone()
    assert row == (4.0,)  # left exactly as-is


# ---------------------------------------------------------------------------
# Settle CLI fallback to the auto-captured close.
# ---------------------------------------------------------------------------


def _load_settle():
    spec = importlib.util.spec_from_file_location("wca_settle", _SETTLE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settle_uses_auto_captured_close(seeded, capsys):
    con, path = seeded
    bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6)
    con.commit()
    closecapture.capture_closes(con, _NOW)

    settle = _load_settle()
    settle.main(["--db", path, "--bet-id", str(bet), "--outcome", "lost"])
    out = capsys.readouterr().out
    assert "auto-captured" in out

    row = con.execute(
        "SELECT status, closing_odds, clv FROM bets WHERE id=?", (bet,)
    ).fetchone()
    expected = _expected_close_triple()
    fair_away = 1.0 / expected["away"]
    assert row[0] == "lost"
    assert row[1] == pytest.approx(fair_away)
    assert row[2] == pytest.approx(4.6 / fair_away - 1.0)


def test_settle_explicit_close_overrides_auto(seeded, capsys):
    con, path = seeded
    bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6)
    con.commit()
    closecapture.capture_closes(con, _NOW)

    settle = _load_settle()
    settle.main([
        "--db", path, "--bet-id", str(bet),
        "--outcome", "won", "--closing-odds", "4.5",
    ])
    out = capsys.readouterr().out
    assert "manual" in out

    row = con.execute(
        "SELECT closing_odds, clv FROM bets WHERE id=?", (bet,)
    ).fetchone()
    assert row[0] == pytest.approx(4.5)
    assert row[1] == pytest.approx(4.6 / 4.5 - 1.0)


def test_settle_still_requires_close_when_none_stamped(db, capsys):
    con, path = db
    bet = _insert_bet(con, "USA vs Paraguay", "h2h", "Paraguay", 4.6)
    con.commit()

    settle = _load_settle()
    with pytest.raises(SystemExit):
        settle.main(["--db", path, "--bet-id", str(bet), "--outcome", "lost"])
    assert "closing-odds required" in capsys.readouterr().err
