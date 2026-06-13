"""Tests for :mod:`wca.linemove` — consensus line-movement series.

The consensus math is hand-computed below so the assertions pin exact values
rather than re-deriving the implementation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import linemove  # noqa: E402


MATCH_ID = "evt-mex-rsa"
HOME = "Mexico"
AWAY = "South Africa"

# Three timestamps x three books x three selections. The implied probs (1/odds)
# and their per-leg medians are hand-computed in the docstring of each helper
# below; see _seed_event for the exact prices.
TS = ["2026-06-10T18:00:00Z", "2026-06-10T19:00:00Z", "2026-06-10T20:00:00Z"]

EVENT_META = {
    MATCH_ID: {
        "fixture": "Mexico vs South Africa",
        "home": HOME,
        "away": AWAY,
        "kickoff": "2026-06-11T19:00:00Z",
    }
}


def _create_table(conn):
    conn.execute(
        """
        CREATE TABLE odds_snapshots (
            ts_utc       TEXT    NOT NULL,
            source       TEXT    NOT NULL,
            match_id     TEXT    NOT NULL,
            market       TEXT    NOT NULL,
            selection    TEXT    NOT NULL,
            decimal_odds REAL,
            raw          TEXT
        )
        """
    )


def _insert(conn, ts, selection, odds, market="h2h", match_id=MATCH_ID):
    conn.execute(
        "INSERT INTO odds_snapshots "
        "(ts_utc, source, match_id, market, selection, decimal_odds, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, "theoddsapi", match_id, market, selection, odds, "{}"),
    )


# Hand-computed consensus per timestamp.
#
# ts0 prices (book A / B / C):
#   home: 2.0, 2.5, 1.6   -> 1/odds = 0.5, 0.4, 0.625  -> median 0.5
#   draw: 4.0, 5.0, 4.0   -> 1/odds = 0.25, 0.2, 0.25  -> median 0.25
#   away: 4.0, 4.0, 5.0   -> 1/odds = 0.25, 0.25, 0.2  -> median 0.25
#   total 1.0 -> normalised home 0.5, draw 0.25, away 0.25
#
# ts1 prices (home shortens):
#   home: 1.6, 1.8, 2.0   -> 0.625, 0.555.., 0.5       -> median 0.5555...
#   draw: 4.0, 4.0, 4.0   -> 0.25 each                 -> median 0.25
#   away: 5.0, 6.0, 5.0   -> 0.2, 0.166.., 0.2         -> median 0.2
#   total 1.00555... -> normalised below
#
# ts2 prices: identical books -> exact, easy to verify.
#   home: 2.0, 2.0, 2.0   -> 0.5 each                  -> median 0.5
#   draw: 4.0, 4.0, 4.0   -> 0.25 each                 -> median 0.25
#   away: 4.0, 4.0, 4.0   -> 0.25 each                 -> median 0.25
PRICES = {
    TS[0]: {"home": [2.0, 2.5, 1.6], "draw": [4.0, 5.0, 4.0], "away": [4.0, 4.0, 5.0]},
    TS[1]: {"home": [1.6, 1.8, 2.0], "draw": [4.0, 4.0, 4.0], "away": [5.0, 6.0, 5.0]},
    TS[2]: {"home": [2.0, 2.0, 2.0], "draw": [4.0, 4.0, 4.0], "away": [4.0, 4.0, 4.0]},
}


def _seed_event(conn):
    sel_for_leg = {"home": HOME, "draw": "Draw", "away": AWAY}
    for ts, legs in PRICES.items():
        for leg, prices in legs.items():
            for price in prices:
                _insert(conn, ts, sel_for_leg[leg], price)


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "wca.db")
    conn = sqlite3.connect(path)
    try:
        _create_table(conn)
        _seed_event(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _series(out, leg):
    return out["events"][0]["series"][leg]


def test_consensus_median_and_normalisation(db_path):
    out = linemove.build_linemove(db_path, EVENT_META, now_utc="NOW")

    assert out["meta"]["generated"] == "NOW"
    assert len(out["events"]) == 1
    evt = out["events"][0]
    assert evt["fixture"] == "Mexico vs South Africa"
    assert evt["kickoff"] == "2026-06-11T19:00:00Z"

    # ts0: medians 0.5 / 0.25 / 0.25, total 1.0 -> already normalised.
    home0 = _series(out, "home")[0]
    draw0 = _series(out, "draw")[0]
    away0 = _series(out, "away")[0]
    assert home0[0] == TS[0]
    assert home0[1] == pytest.approx(0.5)
    assert draw0[1] == pytest.approx(0.25)
    assert away0[1] == pytest.approx(0.25)

    # Every consensus point must sum to exactly 1 after normalisation.
    for i in range(len(_series(out, "home"))):
        total = (
            _series(out, "home")[i][1]
            + _series(out, "draw")[i][1]
            + _series(out, "away")[i][1]
        )
        assert total == pytest.approx(1.0)


def test_ts1_consensus_exact(db_path):
    out = linemove.build_linemove(db_path, EVENT_META, now_utc="")

    # ts1 medians: home 0.5555..., draw 0.25, away 0.2; total 1.0055555...
    med_home = 1.0 / 1.8
    med_draw = 0.25
    med_away = 0.2
    total = med_home + med_draw + med_away

    home1 = _series(out, "home")[1]
    draw1 = _series(out, "draw")[1]
    away1 = _series(out, "away")[1]
    assert home1[0] == TS[1]
    assert home1[1] == pytest.approx(med_home / total)
    assert draw1[1] == pytest.approx(med_draw / total)
    assert away1[1] == pytest.approx(med_away / total)


def test_downsampling_cap(db_path):
    # Cap below the 3 distinct timestamps: must keep first & last only at cap=2.
    out = linemove.build_linemove(db_path, EVENT_META, max_points=2)
    home = _series(out, "home")
    assert len(home) == 2
    assert home[0][0] == TS[0]
    assert home[1][0] == TS[2]

    # Cap >= number of points leaves the series untouched.
    out_full = linemove.build_linemove(db_path, EVENT_META, max_points=10)
    assert len(_series(out_full, "home")) == 3


def test_single_timestamp_event_skipped(tmp_path):
    path = str(tmp_path / "one.db")
    conn = sqlite3.connect(path)
    try:
        _create_table(conn)
        for leg, sel in (("home", HOME), ("draw", "Draw"), ("away", AWAY)):
            _insert(conn, TS[0], sel, 2.0)
        conn.commit()
    finally:
        conn.close()
    out = linemove.build_linemove(path, EVENT_META)
    assert out["events"] == []


def test_event_absent_from_meta_skipped(db_path):
    out = linemove.build_linemove(db_path, {})  # no meta at all
    assert out["events"] == []


def test_missing_db_tolerated(tmp_path):
    missing = str(tmp_path / "does_not_exist.db")
    out = linemove.build_linemove(missing, EVENT_META, now_utc="X")
    assert out == {"meta": {"generated": "X"}, "events": []}


def test_missing_table_tolerated(tmp_path):
    path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    out = linemove.build_linemove(path, EVENT_META)
    assert out["events"] == []


def test_non_h2h_rows_ignored(tmp_path):
    path = str(tmp_path / "mixed.db")
    conn = sqlite3.connect(path)
    try:
        _create_table(conn)
        _seed_event(conn)
        # Pollute with a different market that must never enter the consensus.
        for ts in TS:
            _insert(conn, ts, HOME, 1.01, market="h2h_lay")
        conn.commit()
    finally:
        conn.close()
    out = linemove.build_linemove(path, EVENT_META)
    # Same result as the clean seed: lay rows are filtered out.
    assert _series(out, "home")[0][1] == pytest.approx(0.5)


def test_write_linemove(tmp_path, db_path):
    out_path = str(tmp_path / "sub" / "linemove.json")
    returned = linemove.write_linemove(
        db_path, out_path=out_path, event_meta=EVENT_META, now_utc="GEN"
    )
    assert returned == out_path
    with open(out_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["meta"]["generated"] == "GEN"
    assert payload["events"][0]["fixture"] == "Mexico vs South Africa"


# ---------------------------------------------------------------------------
# event_meta_from_snapshot_file.
# ---------------------------------------------------------------------------


def test_event_meta_from_canonical_snapshot(tmp_path):
    # Canonical Odds-API event shape keyed by "id" with nested bookmakers.
    fixture = [
        {
            "id": "abc123",
            "home_team": "Mexico",
            "away_team": "South Africa",
            "commence_time": "2026-06-11T19:00:00Z",
            "bookmakers": [{"key": "skybet"}],
        },
        {
            "id": "def456",
            "home_team": "Brazil",
            "away_team": "Croatia",
            "commence_time": "2026-06-12T16:00:00Z",
            "bookmakers": [],
        },
    ]
    path = tmp_path / "oddsapi_h2h_uk_canon.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    meta = linemove.event_meta_from_snapshot_file(str(path))
    assert set(meta) == {"abc123", "def456"}
    assert meta["abc123"] == {
        "fixture": "Mexico vs South Africa",
        "home": "Mexico",
        "away": "South Africa",
        "kickoff": "2026-06-11T19:00:00Z",
    }
    assert meta["def456"]["fixture"] == "Brazil vs Croatia"


def test_event_meta_from_flat_snapshot(tmp_path):
    # Flattened per-row dump keyed by "event_id"; the same event repeats.
    fixture = [
        {
            "event_id": "evt1",
            "home_team": "Mexico",
            "away_team": "South Africa",
            "commence_time": "2026-06-11T19:00:00Z",
            "bookmaker_key": "paddypower",
            "outcome_name": "Mexico",
            "decimal_odds": 1.36,
        },
        {
            "event_id": "evt1",
            "home_team": "Mexico",
            "away_team": "South Africa",
            "commence_time": "2026-06-11T19:00:00Z",
            "bookmaker_key": "paddypower",
            "outcome_name": "Draw",
            "decimal_odds": 4.5,
        },
        {
            "event_id": "evt2",
            "home_team": "Brazil",
            "away_team": "Croatia",
            "commence_time": "2026-06-12T16:00:00Z",
            "bookmaker_key": "skybet",
            "outcome_name": "Brazil",
            "decimal_odds": 1.5,
        },
    ]
    path = tmp_path / "oddsapi_h2h_uk_flat.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    meta = linemove.event_meta_from_snapshot_file(str(path))
    assert set(meta) == {"evt1", "evt2"}
    assert meta["evt1"]["fixture"] == "Mexico vs South Africa"
    assert meta["evt1"]["kickoff"] == "2026-06-11T19:00:00Z"
    assert meta["evt2"]["away"] == "Croatia"


def test_event_meta_missing_file(tmp_path):
    assert linemove.event_meta_from_snapshot_file(str(tmp_path / "nope.json")) == {}


def test_event_meta_malformed_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert linemove.event_meta_from_snapshot_file(str(path)) == {}


def test_event_meta_non_list_json(tmp_path):
    path = tmp_path / "obj.json"
    path.write_text(json.dumps({"id": "x"}), encoding="utf-8")
    assert linemove.event_meta_from_snapshot_file(str(path)) == {}


def test_end_to_end_with_derived_meta(tmp_path):
    # Build event_meta from a snapshot file, then feed it to build_linemove
    # using the real MATCH_ID so the series materialises.
    snapshot = [
        {
            "id": MATCH_ID,
            "home_team": HOME,
            "away_team": AWAY,
            "commence_time": "2026-06-11T19:00:00Z",
        }
    ]
    snap_path = tmp_path / "oddsapi_h2h_uk_e2e.json"
    snap_path.write_text(json.dumps(snapshot), encoding="utf-8")

    db = str(tmp_path / "e2e.db")
    conn = sqlite3.connect(db)
    try:
        _create_table(conn)
        _seed_event(conn)
        conn.commit()
    finally:
        conn.close()

    meta = linemove.event_meta_from_snapshot_file(str(snap_path))
    out = linemove.build_linemove(db, meta)
    assert len(out["events"]) == 1
    assert out["events"][0]["kickoff"] == "2026-06-11T19:00:00Z"
    assert len(out["events"][0]["series"]["home"]) == 3


def test_write_linemove_never_clobbers_good_file_with_empty(tmp_path):
    """Regression: a truncated snapshot read mid-write produced an empty meta
    and the empty linemove overwrote (and got pushed over) a healthy file."""
    import json
    from wca.linemove import write_linemove

    out = tmp_path / "linemove.json"
    good = {"meta": {"generated": "t"}, "events": [{"fixture": "A vs B",
            "kickoff": "2026-06-11T19:00:00", "series": {"home": [["t", 0.5]],
            "draw": [["t", 0.3]], "away": [["t", 0.2]]}}]}
    out.write_text(json.dumps(good))
    # Empty meta + missing db -> empty payload; must keep the good file.
    write_linemove(str(tmp_path / "missing.db"), out_path=str(out),
                   event_meta={}, now_utc="x")
    kept = json.loads(out.read_text())
    assert len(kept["events"]) == 1


def test_robust_event_meta_skips_truncated_newest(tmp_path):
    import json
    from wca.linemove import robust_event_meta

    older = tmp_path / "oddsapi_h2h_uk_20260611T100000Z.json"
    older.write_text(json.dumps([{"id": "e1", "home_team": "A", "away_team": "B",
                                  "commence_time": "2026-06-11T19:00:00Z"}]))
    newest = tmp_path / "oddsapi_h2h_uk_20260611T110000Z.json"
    newest.write_text('[{"id": "e2", "home_te')  # truncated mid-write
    meta = robust_event_meta(str(tmp_path))
    assert "e1" in meta and meta["e1"]["fixture"] == "A vs B"


# ---------------------------------------------------------------------------
# Model 1X2 resolution and the optional per-event "model" block.
# ---------------------------------------------------------------------------


def _write_scores(tmp_path, fixtures):
    path = tmp_path / "scores_data.json"
    path.write_text(json.dumps({"meta": {}, "fixtures": fixtures}))
    return str(path)


def _write_card(tmp_path, text):
    path = tmp_path / "card_latest.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


CARD = """\
*World Cup Alpha — bet card* (3 picks)

*1. United States vs Paraguay* — Paraguay @ *4.10* (betfair_ex_uk)
    model 26.8% / mkt 24.1%  edge *+10.1%*  [elo 31% dc 28%]
    stake: main 7.55
*2. Qatar vs Switzerland* — Qatar @ *17.01* (smarkets)
    model 7.7% / mkt 5.7%  edge *+30.5%*  [elo 9% dc 10%]
    stake: main 4.42
*3. Qatar vs Switzerland* — Draw @ *7.00* (betfair_ex_uk)
    model 16.2% / mkt 14.6%  edge *+13.2%*  [elo 16% dc 20%]
    stake: main 5.11
"""


def test_models_from_card_partial_legs(tmp_path):
    card = _write_card(tmp_path, CARD)
    models = linemove._models_from_card(card)

    # Picked legs only: USA away pick, Qatar home + draw picks.
    usa = models[("united states", "paraguay")]
    assert usa == {"away": pytest.approx(0.268)}
    qat = models[("qatar", "switzerland")]
    assert qat["home"] == pytest.approx(0.077)
    assert qat["draw"] == pytest.approx(0.162)
    assert "away" not in qat


def test_models_from_scores_normalised(tmp_path):
    scores = _write_scores(tmp_path, [{
        "fixture": "USA vs Paraguay",
        "model_1x2": {"home": 0.6, "draw": 0.3, "away": 0.3},  # sums to 1.2
    }])
    models = linemove._models_from_scores(scores)

    # Keyed by canonical names (USA -> United States) and re-normalised.
    probs = models[("united states", "paraguay")]
    assert probs["home"] == pytest.approx(0.5)
    assert probs["draw"] == pytest.approx(0.25)
    assert probs["away"] == pytest.approx(0.25)


def test_resolve_model_probs_prefers_scores(tmp_path):
    card = _write_card(tmp_path, CARD)
    scores = _write_scores(tmp_path, [{
        "fixture": "United States vs Paraguay",
        "model_1x2": {"home": 0.5, "draw": 0.35, "away": 0.15},
    }])
    models = linemove.resolve_model_probs(
        scores_path=scores,
        card_path=card,
        preds_path=str(tmp_path / "no_preds.json"),
    )

    # Scores triple wins for USA; the card still fills the Qatar fixture.
    assert models[("united states", "paraguay")]["away"] == pytest.approx(0.15)
    assert models[("qatar", "switzerland")]["home"] == pytest.approx(0.077)


def test_resolve_model_probs_prefers_predictions_snapshot(tmp_path):
    card = _write_card(tmp_path, CARD)
    scores = _write_scores(tmp_path, [{
        "fixture": "United States vs Paraguay",
        "model_1x2": {"home": 0.5, "draw": 0.35, "away": 0.15},
    }])
    preds = tmp_path / "model_predictions.json"
    preds.write_text(json.dumps({
        "meta": {"generated": "2026-06-13T00:00:00"},
        "fixtures": [{
            "fixture": "United States vs Paraguay",
            "model": {"home": 0.439, "draw": 0.295, "away": 0.266},
        }],
    }))
    models = linemove.resolve_model_probs(
        scores_path=scores, card_path=card, preds_path=str(preds)
    )

    # The card-build snapshot beats the scores-feed triple for USA; fixtures
    # it lacks still fall through to the other sources (Qatar from the card).
    assert models[("united states", "paraguay")]["away"] == pytest.approx(0.266)
    assert models[("qatar", "switzerland")]["home"] == pytest.approx(0.077)


def test_resolve_model_probs_missing_files(tmp_path):
    assert linemove.resolve_model_probs(
        scores_path=str(tmp_path / "no_scores.json"),
        card_path=str(tmp_path / "no_card.md"),
        preds_path=str(tmp_path / "no_preds.json"),
    ) == {}


def test_build_linemove_model_block(db_path):
    model_probs = {
        ("mexico", "south africa"): {"home": 0.55, "draw": 0.25, "away": 0.2},
    }
    out = linemove.build_linemove(db_path, EVENT_META, model_probs=model_probs)
    evt = out["events"][0]
    assert evt["model"] == {"home": 0.55, "draw": 0.25, "away": 0.2}

    # No matching fixture (or no map at all) -> no "model" key, same series.
    out_other = linemove.build_linemove(
        db_path, EVENT_META, model_probs={("a", "b"): {"home": 0.5}}
    )
    assert "model" not in out_other["events"][0]
    out_none = linemove.build_linemove(db_path, EVENT_META)
    assert "model" not in out_none["events"][0]
    assert out_other["events"][0]["series"] == out_none["events"][0]["series"]
