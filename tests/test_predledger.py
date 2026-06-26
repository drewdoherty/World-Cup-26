"""Tests for the prediction ledger (wca.predledger).

Covered:
* idempotent upsert — two upserts of a NULL-line/stage 1X2 row collapse to ONE
  row (deterministic prediction_id);
* dev-box wca.db write refusal raises (and the WCA_ALLOW_PROD_DB override
  lifts it);
* the v_model_book view returns both paper and realized rows with the right
  ``book`` tag;
* settle correctness for 1X2 / scoreline / O-U (incl. integer-line push) /
  BTTS, and that pushes are excluded from rates;
* Wilson edge cases (n=0, k=0, k=n, n=1);
* fair-vs-fair CLV (model_fair_odds / closing_odds - 1) and NULL-when-no-close;
* flatten_card populating NULL market columns for scoreline.

All tests use temporary SQLite files and synthetic inputs — fully offline and
deterministic.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from wca.predledger import build as pl_build
from wca.predledger import publish as pl_publish
from wca.predledger import settle as pl_settle
from wca.predledger import store


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="predledger_test_")
    os.close(fd)
    os.unlink(path)  # let sqlite create it fresh
    return path


def _row(**over):
    base = {
        "build_id": "b1",
        "ts_utc": "2026-06-13T00:00:00Z",
        "match_id": "m1",
        "fixture": "United States vs Paraguay",
        "kickoff_utc": "2026-06-13T01:00:00Z",
        "market": "1X2",
        "selection": "Home",
        "line": -1.0,
        "stage": "",
        "n_outcomes": 3,
        "model_prob": 0.45,
        "model_fair_odds": 1.0 / 0.45,
        "status": "open",
        "model_source": "test",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Idempotent upsert.
# ---------------------------------------------------------------------------


def test_upsert_is_idempotent_on_null_line_stage():
    db = _tmp_db()
    store.ensure_schema(db)
    store.upsert_predictions([_row()], db)
    store.upsert_predictions([_row()], db)  # identical -> same id -> one row
    rows = store.all_predictions(db)
    assert len(rows) == 1
    assert rows[0]["selection"] == "Home"
    # line defaulted, stage defaulted.
    assert rows[0]["line"] == -1.0
    assert rows[0]["stage"] == ""


def test_upsert_distinct_legs_are_distinct_rows():
    db = _tmp_db()
    store.ensure_schema(db)
    store.upsert_predictions(
        [_row(selection="Home"), _row(selection="Draw"), _row(selection="Away")], db
    )
    assert len(store.all_predictions(db)) == 3


def test_upsert_partial_merge_preserves_model_columns():
    db = _tmp_db()
    store.ensure_schema(db)
    store.upsert_predictions([_row(model_prob=0.42)], db)
    pid = store._row_prediction_id(_row())
    # A settle-style partial upsert (only status) must not nuke model_prob.
    store.upsert_predictions(
        [{"build_id": "b1", "match_id": "m1", "market": "1X2", "selection": "Home",
          "line": -1.0, "stage": "", "status": "won"}],
        db,
    )
    row = store.get_prediction(pid, db)
    assert row["status"] == "won"
    assert abs(row["model_prob"] - 0.42) < 1e-9


# ---------------------------------------------------------------------------
# Production-DB write guard.
# ---------------------------------------------------------------------------


def test_prod_db_write_refused(tmp_path):
    prod = str(tmp_path / "wca.db")
    os.environ.pop("WCA_ALLOW_PROD_DB", None)
    with pytest.raises(PermissionError):
        store.ensure_schema(prod)


def test_prod_db_write_allowed_with_override(tmp_path):
    prod = str(tmp_path / "wca.db")
    os.environ["WCA_ALLOW_PROD_DB"] = "1"
    try:
        store.ensure_schema(prod)  # must not raise
        assert os.path.exists(prod)
    finally:
        os.environ.pop("WCA_ALLOW_PROD_DB", None)


# ---------------------------------------------------------------------------
# Views: paper + realized.
# ---------------------------------------------------------------------------


def test_view_join_returns_paper_and_realized():
    db = _tmp_db()
    store.ensure_schema(db)
    store.upsert_predictions(
        [_row(selection="Home"), _row(selection="Away")], db
    )
    # Insert a real bet and link it to the Home prediction.
    import sqlite3

    con = sqlite3.connect(db)
    cur = con.execute(
        "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
        "platform, decimal_odds, stake, status) "
        "VALUES (?,?,?,?,?,?,?,?, 'open')",
        ("2026-06-13T00:00:00Z", "m1", "United States vs Paraguay", "1X2",
         "Home", "bet365", 2.2, 10.0),
    )
    con.commit()
    bet_id = cur.lastrowid
    con.close()
    home_pid = store._row_prediction_id(_row(selection="Home"))
    store.link_bet(home_pid, bet_id, db)

    book = store.model_book(db)
    tags = sorted(r["book"] for r in book)
    assert tags == ["paper", "realized"]
    realized = store.realized_book(db)
    assert len(realized) == 1
    assert realized[0]["selection"] == "Home"
    assert realized[0]["b_stake"] == 10.0


# ---------------------------------------------------------------------------
# Settle correctness.
# ---------------------------------------------------------------------------


def _write_results(tmp_path, fixture, score, outcome):
    p = tmp_path / "results.json"
    p.write_text(json.dumps({"results": [
        {"date": "2026-06-13", "fixture": fixture,
         "kickoff_utc": "2026-06-13T01:00:00Z", "score": score, "outcome": outcome}
    ]}))
    adv = tmp_path / "adv.json"
    adv.write_text(json.dumps([]))
    return str(p), str(adv)


def test_settle_1x2_and_market_rules(tmp_path):
    db = _tmp_db()
    store.ensure_schema(db)
    fixture = "United States vs Paraguay"  # final 4-1: home win, total 5, BTTS yes
    rows = [
        _row(market="1X2", selection="Home"),       # won
        _row(market="1X2", selection="Away"),       # lost
        _row(market="scoreline", selection="4-1"),  # won
        _row(market="scoreline", selection="2-2"),  # lost
        _row(market="ou", selection="Over 2.5", line=2.5),   # won (5>2.5)
        _row(market="ou", selection="Under 2.5", line=2.5),  # lost
        _row(market="ou", selection="Over 5", line=5.0),     # push (total==5)
        _row(market="btts", selection="Yes"),  # won
        _row(market="btts", selection="No"),   # lost
    ]
    store.upsert_predictions(rows, db)
    results, adv = _write_results(tmp_path, fixture, "4-1", "home")
    tally = pl_settle.settle_open(results, adv, db)
    assert tally["won"] == 4
    assert tally["lost"] == 4
    assert tally["push"] == 1

    by_sel = {r["selection"]: r["status"] for r in store.all_predictions(db)}
    assert by_sel["Home"] == "won"
    assert by_sel["Away"] == "lost"
    assert by_sel["4-1"] == "won"
    assert by_sel["Over 2.5"] == "won"
    assert by_sel["Over 5"] == "push"
    assert by_sel["Yes"] == "won"

    # Idempotent: re-settle touches nothing (all already settled).
    tally2 = pl_settle.settle_open(results, adv, db)
    assert tally2["won"] == 0 and tally2["lost"] == 0 and tally2["push"] == 0


def test_settle_integer_line_under_push(tmp_path):
    db = _tmp_db()
    store.ensure_schema(db)
    fixture = "A vs B"
    store.upsert_predictions(
        [_row(fixture=fixture, market="ou", selection="Under 3", line=3.0)], db
    )
    results, adv = _write_results(tmp_path, fixture, "2-1", "home")  # total 3
    tally = pl_settle.settle_open(results, adv, db)
    assert tally["push"] == 1


# ---------------------------------------------------------------------------
# Wilson edge cases.
# ---------------------------------------------------------------------------


def test_wilson_edge_cases():
    # n=0 -> all None
    assert pl_publish.wilson(0, 0) == (None, None, None)
    # k=0: p=0, lo=0, hi>0 and <1
    p, lo, hi = pl_publish.wilson(0, 10)
    assert p == 0.0 and lo == 0.0 and 0.0 < hi < 1.0
    # k=n: p=1, hi=1, lo<1 and >0
    p, lo, hi = pl_publish.wilson(10, 10)
    assert p == 1.0 and hi == 1.0 and 0.0 < lo < 1.0
    # n=1 finite
    p, lo, hi = pl_publish.wilson(1, 1)
    assert p == 1.0 and lo is not None and hi is not None and lo <= hi
    # symmetric-ish midpoint
    p, lo, hi = pl_publish.wilson(5, 10)
    assert abs(p - 0.5) < 1e-9 and lo < 0.5 < hi


# ---------------------------------------------------------------------------
# Fair-vs-fair CLV.
# ---------------------------------------------------------------------------


def test_clv_fair_vs_fair_and_null_when_no_close():
    db = _tmp_db()
    store.ensure_schema(db)
    store.upsert_predictions([_row(model_prob=0.5, model_fair_odds=2.0)], db)
    pid = store._row_prediction_id(_row())
    # model_fair_odds=2.0, closing_odds=1.8 -> clv = 2.0/1.8 - 1
    clv = store.set_prediction_close(pid, 1.8, 2.0, db)
    assert abs(clv - (2.0 / 1.8 - 1.0)) < 1e-9
    row = store.get_prediction(pid, db)
    assert abs(row["clv"] - clv) < 1e-9
    assert row["closing_odds"] == 1.8

    # No close -> CLV NULL, not 0.
    store.upsert_predictions([_row(selection="Away", model_prob=0.3)], db)
    pid2 = store._row_prediction_id(_row(selection="Away"))
    clv2 = store.set_prediction_close(pid2, None, None, db)
    assert clv2 is None
    assert store.get_prediction(pid2, db)["clv"] is None


# ---------------------------------------------------------------------------
# flatten_card: NULL market columns for scoreline.
# ---------------------------------------------------------------------------


def test_flatten_card_scoreline_null_market_columns():
    recs = [
        {
            "match_id": "m1",
            "fixture": "A vs B",
            "kickoff_utc": "2026-06-13T01:00:00Z",
            "stage": "group",
            "model": {"home": 0.5, "draw": 0.3, "away": 0.2},
            "market": {"home": 0.48, "draw": 0.30, "away": 0.22},
            "best_odds": {"home": 2.1, "draw": 3.4, "away": 4.8},
            "scoreline": [{"score": "1-0", "model_prob": 0.12}],
            "btts": [{"side": "Yes", "model_prob": 0.55, "market_devig_prob": 0.5,
                      "best_odds": 1.9}],
        }
    ]
    rows, accas = pl_build.flatten_card(recs, None, None, "2026-06-13T00:00:00Z")
    by = {(r["market"], r["selection"]): r for r in rows}
    # 1X2 priced -> edge/ev populated.
    home = by[("1X2", "Home")]
    assert home["market_devig_prob"] == 0.48
    assert home["edge"] is not None and home["ev_per_unit"] is not None
    # scoreline -> NULL market columns (no price supplied).
    sl = by[("scoreline", "1-0")]
    assert sl["market_devig_prob"] is None
    assert sl["edge"] is None
    assert sl["ev_per_unit"] is None
    assert sl["model_prob"] == 0.12
    # btts priced -> populated.
    btts = by[("btts", "Yes")]
    assert btts["market_devig_prob"] == 0.5 and btts["edge"] is not None


def test_flatten_card_deterministic_ids():
    recs = [{"match_id": "m1", "fixture": "A vs B", "kickoff_utc": "k",
             "model": {"home": 0.5, "draw": 0.3, "away": 0.2}}]
    r1, _ = pl_build.flatten_card(recs, None, None, "2026-06-13T00:00:00Z")
    r2, _ = pl_build.flatten_card(recs, None, None, "2026-06-13T00:00:00Z")
    assert [x["prediction_id"] for x in r1] == [x["prediction_id"] for x in r2]


def test_publish_feed_shape(tmp_path):
    db = _tmp_db()
    store.ensure_schema(db)
    store.upsert_predictions(
        [_row(market="1X2", selection="Home", status="won"),
         _row(market="1X2", selection="Away", status="lost")],
        db,
    )
    feed = pl_publish.build_feed("2026-06-25T00:00:00Z", db)
    assert feed["meta"]["db"] == "dev.db"
    assert feed["meta"]["n_predictions"] == 2
    assert "1X2" in feed["coverage"]
    # zero-data markets still present.
    assert "scoreline" in feed["coverage"]
    assert any(m["market"] == "1X2" for m in feed["by_market"])
    hr = feed["headline"]["paper_win_rate"]
    assert hr["n"] == 2 and hr["p"] is not None
