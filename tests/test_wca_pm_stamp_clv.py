"""Tests for scripts/wca_pm_stamp_clv.py (mini-side PM CLV stamper).

Uses a tmp sqlite ledger mirroring the production ``bets`` schema (as
inspected in the local dev copy of ``data/wca.db``, incl. the ``token_id``,
``account``, ``source``, ``manual_override``, ``cashout_proceeds`` columns
that ``wca.ledger.store.init_db``'s minimal DDL doesn't create) so the
stamper's raw SQL (which selects only a few named columns) is exercised
against the real column set it will see on the mini. Covers: join by
team+stage, join by team-only fallback, no-op when already stamped, no-op
when the artifact has nothing new, and the CLV arithmetic itself.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "wca_pm_stamp_clv.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("wca_pm_stamp_clv", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return _load_module()


_BETS_DDL = """
CREATE TABLE bets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc              TEXT    NOT NULL,
    match_id            TEXT    NOT NULL,
    match_desc          TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    selection           TEXT    NOT NULL,
    platform            TEXT    NOT NULL,
    decimal_odds        REAL    NOT NULL,
    stake               REAL    NOT NULL,
    model_prob          REAL,
    market_prob_devig   REAL,
    ev                  REAL,
    kelly_fraction      REAL,
    status              TEXT    NOT NULL DEFAULT 'open',
    settled_pl          REAL,
    closing_odds        REAL,
    clv                 REAL,
    notes               TEXT,
    settled_ts TEXT, account TEXT DEFAULT '1', source TEXT DEFAULT 'model',
    manual_override TEXT, token_id TEXT, cashout_proceeds REAL
)
"""


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "wca.db")
    con = sqlite3.connect(path)
    con.execute(_BETS_DDL)
    con.commit()
    yield con, path
    con.close()


def _insert_bet(con, match_desc, selection, odds, platform="polymarket",
                 status="open", closing_odds=None, clv=None, notes=None,
                 market="polymarket"):
    cur = con.execute(
        "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
        "platform, decimal_odds, stake, status, closing_odds, clv, notes) "
        "VALUES ('2026-06-19T10:00:00', 'm', ?, ?, ?, ?, ?, 10.0, ?, ?, ?, ?)",
        (match_desc, market, selection, platform, odds, status,
         closing_odds, clv, notes),
    )
    con.commit()
    return cur.lastrowid


def _close_row(token_id, close_ts, mid, question, condition_id="0xc"):
    return {
        "condition_id": condition_id,
        "token_id": token_id,
        "question": question,
        "close_ts_utc": close_ts,
        "mid": mid,
        "best_bid": mid - 0.01,
        "best_ask": mid + 0.01,
        "source": "top_of_book",
        "captured_utc": "2026-06-21T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# stamp_clv: join + arithmetic.
# ---------------------------------------------------------------------------


def test_stamp_clv_by_team_and_stage(db, mod):
    con, _ = db
    # Real ledger shape: a "No" bet on Ghana's elimination market — the
    # captured close row always carries the YES mid, so the fair mid for
    # THIS bet is the complement (1 - 0.55 = 0.45).
    bet_id = _insert_bet(
        con,
        "Ghana eliminated R32 of the World Cup",
        "No — Ghana not eliminated in Round of 32",
        1.85,
    )
    closes = [
        _close_row(
            "tok1", "2026-06-20T18:00:00Z", 0.55,
            "Will Ghana be eliminated in the Round of 32 at the 2026 FIFA World Cup?",
        )
    ]
    stamped = mod.stamp_clv(con, closes)
    assert len(stamped) == 1
    rec = stamped[0]
    assert rec["bet_id"] == bet_id
    fair_mid = 1.0 - 0.55  # No-bet complement
    expected_clv = 1.85 / (1.0 / fair_mid) - 1.0
    assert rec["clv"] == pytest.approx(expected_clv)
    assert rec["closing_odds"] == pytest.approx(1.0 / fair_mid)

    row = con.execute(
        "SELECT closing_odds, clv FROM bets WHERE id=?", (bet_id,)
    ).fetchone()
    assert row[0] == pytest.approx(1.0 / fair_mid)
    assert row[1] == pytest.approx(expected_clv)


def test_stamp_clv_yes_bet_uses_raw_mid(db, mod):
    con, _ = db
    bet_id = _insert_bet(con, "Mexico advancement", "Mexico reach R32 - Yes", 1.10)
    closes = [
        _close_row(
            "tok9", "2026-06-11T19:00:00Z", 0.90,
            "Will Mexico reach the Round of 32 at the 2026 FIFA World Cup?",
        )
    ]
    stamped = mod.stamp_clv(con, closes)
    assert len(stamped) == 1
    assert stamped[0]["closing_odds"] == pytest.approx(1.0 / 0.90)


def test_stamp_clv_team_only_fallback(db, mod):
    con, _ = db
    bet_id = _insert_bet(
        con, "2026 FIFA World Cup - Japan Round of 16", "Japan reach R16 - No", 2.6,
    )
    closes = [
        _close_row(
            "tok2", "2026-06-25T20:00:00Z", 0.30,
            "Will Japan reach the Round of 16 at the 2026 FIFA World Cup?",
        )
    ]
    stamped = mod.stamp_clv(con, closes)
    assert len(stamped) == 1
    assert stamped[0]["bet_id"] == bet_id


def test_stamp_clv_settled_bets_are_still_stamped(db, mod):
    """Unlike the 1X2 closecapture path, PM advancement bets are commonly
    already settled by the time a close is captured — status is not a
    stamping precondition, only closing_odds IS NULL."""
    con, _ = db
    bet_id = _insert_bet(
        con, "Mexico advancement", "Mexico reach R32 - Yes", 1.49, status="won",
    )
    closes = [
        _close_row(
            "tok3", "2026-06-11T19:00:00Z", 0.90,
            "Will Mexico reach the Round of 32 at the 2026 FIFA World Cup?",
        )
    ]
    stamped = mod.stamp_clv(con, closes)
    assert len(stamped) == 1
    row = con.execute("SELECT status, clv FROM bets WHERE id=?", (bet_id,)).fetchone()
    assert row[0] == "won"
    assert row[1] is not None


# ---------------------------------------------------------------------------
# Idempotency / no-op behaviour.
# ---------------------------------------------------------------------------


def test_stamp_clv_already_stamped_is_noop(db, mod):
    con, _ = db
    _insert_bet(
        con, "Ghana eliminated R32 of the World Cup",
        "No — Ghana not eliminated in Round of 32", 1.85,
        closing_odds=1.9, clv=0.05,  # already stamped
    )
    closes = [
        _close_row(
            "tok1", "2026-06-20T18:00:00Z", 0.55,
            "Will Ghana be eliminated in the Round of 32 at the 2026 FIFA World Cup?",
        )
    ]
    stamped = mod.stamp_clv(con, closes)
    assert stamped == []
    row = con.execute("SELECT closing_odds, clv FROM bets").fetchone()
    assert row == (1.9, 0.05)  # untouched, not overwritten


def test_stamp_clv_no_matching_close_leaves_bet_unstamped(db, mod):
    con, _ = db
    _insert_bet(con, "Iceland advancement", "Iceland reach R32 - Yes", 3.0)
    stamped = mod.stamp_clv(con, [])
    assert stamped == []
    row = con.execute("SELECT closing_odds, clv FROM bets").fetchone()
    assert row == (None, None)


def test_stamp_clv_non_polymarket_bets_ignored(db, mod):
    con, _ = db
    _insert_bet(
        con, "Ghana eliminated R32 of the World Cup",
        "No — Ghana not eliminated in Round of 32", 1.85, platform="bet365",
    )
    closes = [
        _close_row(
            "tok1", "2026-06-20T18:00:00Z", 0.55,
            "Will Ghana be eliminated in the Round of 32 at the 2026 FIFA World Cup?",
        )
    ]
    stamped = mod.stamp_clv(con, closes)
    assert stamped == []


def test_stamp_clv_moneyline_bet_not_misjoined_to_advancement_close(db, mod):
    """A pm_moneyline (single-match) bet on a team must NEVER be joined to
    that team's captured ADVANCEMENT close via the team-only fallback —
    they price completely different outcomes. Regression for a real bug: a
    "<Team> Yes"-style moneyline selection carries no stage text, so before
    the market-label exclusion it satisfied match_bet_to_close's team-only
    fallback whenever the team had exactly one captured advancement close."""
    con, _ = db
    bet_id = _insert_bet(
        con, "Ghana vs Egypt", "Ghana Yes", 2.10, market="pm_moneyline",
    )
    closes = [
        _close_row(
            "tokR32", "2026-06-20T18:00:00Z", 0.55,
            "Will Ghana be eliminated in the Round of 32 at the 2026 FIFA World Cup?",
        )
    ]
    stamped = mod.stamp_clv(con, closes)
    assert stamped == []
    row = con.execute(
        "SELECT closing_odds, clv FROM bets WHERE id=?", (bet_id,)
    ).fetchone()
    assert row == (None, None)


@pytest.mark.parametrize("market", ["h2h", "Full-Time Result", "Match Odds", "pm_moneyline"])
def test_is_moneyline_market_recognises_all_x12_labels(market, mod):
    assert mod._is_moneyline_market(market) is True
    assert mod._is_moneyline_market("Polymarket advancement: Will Ghana...") is False
    assert mod._is_moneyline_market("polymarket") is False


def test_stamp_clv_dry_run_does_not_write(db, mod):
    con, _ = db
    bet_id = _insert_bet(
        con, "Ghana eliminated R32 of the World Cup",
        "No — Ghana not eliminated in Round of 32", 1.85,
    )
    closes = [
        _close_row(
            "tok1", "2026-06-20T18:00:00Z", 0.55,
            "Will Ghana be eliminated in the Round of 32 at the 2026 FIFA World Cup?",
        )
    ]
    stamped = mod.stamp_clv(con, closes, dry_run=True)
    assert len(stamped) == 1
    row = con.execute(
        "SELECT closing_odds, clv FROM bets WHERE id=?", (bet_id,)
    ).fetchone()
    assert row == (None, None)  # dry run: nothing written


# ---------------------------------------------------------------------------
# main() / stamp_clv_db(): artifact-driven no-op + missing-file handling.
# ---------------------------------------------------------------------------


def test_stamp_clv_db_no_op_when_artifact_missing(db, mod):
    _, path = db
    rc = mod.main(["--db", path, "--artifact", "/nonexistent/pm_closes.json"])
    assert rc == 0


def test_main_rerun_after_stamping_is_noop(db, mod, tmp_path, capsys):
    con, path = db
    _insert_bet(
        con, "Ghana eliminated R32 of the World Cup",
        "No — Ghana not eliminated in Round of 32", 1.85,
    )
    artifact = str(tmp_path / "pm_closes.json")
    with open(artifact, "w") as fh:
        json.dump(
            [
                _close_row(
                    "tok1", "2026-06-20T18:00:00Z", 0.55,
                    "Will Ghana be eliminated in the Round of 32 at the 2026 FIFA World Cup?",
                )
            ],
            fh,
        )
    rc1 = mod.main(["--db", path, "--artifact", artifact])
    assert rc1 == 0
    out1 = capsys.readouterr().out
    assert "stamped bet" in out1

    rc2 = mod.main(["--db", path, "--artifact", artifact])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "no Polymarket bets matched" in out2


def test_main_missing_db_errors(tmp_path, mod):
    rc = mod.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 1
