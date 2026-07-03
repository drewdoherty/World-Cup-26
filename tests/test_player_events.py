"""player_events store: idempotency, per-90 math, resolution order, shrink."""
from __future__ import annotations

import pytest

from wca.data import player_events as pe
from wca.models import playerprops as pp


def _row(**kw):
    base = dict(player="Mohamed Salah", team="Egypt", match_id="m1",
                date="2026-06-15", competition="WC2026", minutes=90.0,
                shots=3, sot=2, goals=1, source=pe.SOURCE_ANALYST)
    base.update(kw)
    return pe.PlayerMatchRow(**base)


def test_upsert_is_idempotent(tmp_path):
    db = str(tmp_path / "pe.db")
    con = pe.connect(db)
    assert pe.upsert_rows(con, [_row()], "t0") == 1
    assert pe.upsert_rows(con, [_row(sot=3)], "t1") == 1  # replace, not dup
    n, sot = con.execute(
        "SELECT COUNT(*), SUM(sot) FROM player_matches").fetchone()
    con.close()
    assert n == 1 and sot == 3


def test_empirical_rates_per90_math(tmp_path):
    db = str(tmp_path / "pe.db")
    con = pe.connect(db)
    pe.upsert_rows(con, [
        _row(match_id="m1", minutes=90.0, shots=3, sot=2, goals=1),
        _row(match_id="m2", minutes=99.0, shots=4, sot=1, goals=0),
    ], "t0")
    con.close()
    r = pe.empirical_rates("Egypt", "Mohamed Salah", db_path=db)
    assert r is not None
    assert r.sample_minutes == pytest.approx(189.0)
    assert r.sot_p90 == pytest.approx(3 * 90.0 / 189.0)
    assert r.shots_p90 == pytest.approx(7 * 90.0 / 189.0)
    assert r.goals_p90 == pytest.approx(1 * 90.0 / 189.0)
    assert "player_events.db (2 matches)" in r.rate_source


def test_empirical_rates_accent_and_missing(tmp_path):
    db = str(tmp_path / "pe.db")
    con = pe.connect(db)
    pe.upsert_rows(con, [_row(player="Mohaméd Salah")], "t0")
    con.close()
    # accent-folded match succeeds; unknown player -> None
    assert pe.empirical_rates("Egypt", "mohamed salah", db_path=db) is not None
    assert pe.empirical_rates("Egypt", "Omar Marmoush", db_path=db) is None
    # rows without minutes are unusable for rates
    con = pe.connect(db)
    pe.upsert_rows(con, [_row(player="NoMins", match_id="m9", minutes=None)], "t1")
    con.close()
    assert pe.empirical_rates("Egypt", "NoMins", db_path=db) is None


def test_rates_resolution_prefers_per_match_store(tmp_path):
    db = str(tmp_path / "pe.db")
    con = pe.connect(db)
    pe.upsert_rows(con, [_row()], "t0")
    con.close()
    r = pp.rates_from_players_db("Egypt", "Mohamed Salah",
                                 events_db_path=db,
                                 db_path=str(tmp_path / "absent_players.db"))
    assert r is not None and r.rate_source.startswith("player_events.db")
    # no per-match rows AND no players.db -> None (structural fallback upstream)
    r2 = pp.rates_from_players_db("Egypt", "Omar Marmoush",
                                  events_db_path=db,
                                  db_path=str(tmp_path / "absent_players.db"))
    assert r2 is None


def test_shrink_actually_engages(tmp_path):
    """The whole point: empirical evidence pulls the priced rate off the prior."""
    db = str(tmp_path / "pe.db")
    con = pe.connect(db)
    pe.upsert_rows(con, [
        _row(match_id="m%d" % i, minutes=90.0, sot=2, shots=4, goals=0)
        for i in range(3)
    ], "t0")
    con.close()
    rates = pe.empirical_rates("Egypt", "Mohamed Salah", db_path=db)
    shrunk, source = pp._sot_rate_p90(rates, shots_p90=1.6,
                                      shots_src="players.db")
    prior = pp.PLAYER_P90_PRIORS[pp.MK_SOT]
    empirical = rates.sot_p90  # 2 SoT per 90-min match = 2.0/90
    # strictly between prior and empirical, and n_eff=3 matches of 6 -> 1/3 weight
    assert prior < shrunk < empirical
    w = (270.0 / 90.0) / (270.0 / 90.0 + pp.SHRINK_K)
    assert shrunk == pytest.approx(w * empirical + (1 - w) * prior)
    assert source.startswith("player_events.db")


def test_statsbomb_match_rows_extracts_corners_and_stats():
    events = [
        {"type": {"name": "Shot"}, "player": {"name": "A"},
         "team": {"name": "X"},
         "shot": {"outcome": {"name": "Goal"}, "statsbomb_xg": 0.4}},
        {"type": {"name": "Shot"}, "player": {"name": "A"},
         "team": {"name": "X"},
         "shot": {"outcome": {"name": "Off T"}, "statsbomb_xg": 0.1}},
        {"type": {"name": "Pass"}, "player": {"name": "A"},
         "team": {"name": "X"}, "pass": {"type": {"name": "Corner"}}},
        {"type": {"name": "Pass"}, "player": {"name": "B"},
         "team": {"name": "X"}, "pass": {"type": {"name": "Corner"}}},
    ]
    rows = pe.statsbomb_match_rows(events, "m42", "2018-06-30", "WC2018")
    by = {r.player: r for r in rows}
    assert by["A"].shots == 2 and by["A"].goals == 1 and by["A"].sot == 1
    assert by["A"].corners_taken == 1
    assert by["A"].source == pe.SOURCE_STATSBOMB
    assert by["A"].match_id == "m42" and by["A"].competition == "WC2018"
