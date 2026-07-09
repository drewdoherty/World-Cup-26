"""Tests for wca.pmclose: the PM advancement close-capture/CLV-stamp core.

Covers:
* the committed artifact's idempotent read/append (one row per
  (token_id, close_ts_utc), a no-op rerun touches nothing on disk);
* deciding-match-kickoff resolution from the results JSON;
* question -> team/stage parsing;
* CLV arithmetic against the ledger-wide backed/close-1 convention; and
* the bet <-> close-row join used by the mini-side stamper, including its
  documented team-only fallback and its refusal to guess when ambiguous.
"""

from __future__ import annotations

import json
import os

import pytest

from wca import pmclose


# ---------------------------------------------------------------------------
# Artifact idempotency.
# ---------------------------------------------------------------------------


def _row(token_id, close_ts, mid, question="Will Ghana reach the Round of 16?",
         condition_id="0xabc", source="top_of_book", captured="2026-07-08T00:00:00+00:00"):
    return {
        "condition_id": condition_id,
        "token_id": token_id,
        "question": question,
        "close_ts_utc": close_ts,
        "mid": mid,
        "best_bid": mid - 0.01,
        "best_ask": mid + 0.01,
        "source": source,
        "captured_utc": captured,
    }


def test_append_closes_writes_new_file(tmp_path):
    path = str(tmp_path / "pm_closes.json")
    rows = [_row("tok1", "2026-06-20T18:00:00Z", 0.45)]
    merged, n_added = pmclose.append_closes(rows, path)
    assert n_added == 1
    assert os.path.exists(path)
    on_disk = json.load(open(path))
    assert len(on_disk) == 1
    assert on_disk[0]["token_id"] == "tok1"
    assert set(on_disk[0].keys()) == set(pmclose.CLOSE_FIELDS)


def test_append_closes_idempotent_rerun_is_noop(tmp_path):
    path = str(tmp_path / "pm_closes.json")
    rows = [_row("tok1", "2026-06-20T18:00:00Z", 0.45)]
    pmclose.append_closes(rows, path)
    mtime_1 = os.path.getmtime(path)

    # Rerun with the exact same row: must add nothing, and must not even
    # touch the file (mtime unchanged) — the mtime check catches an
    # implementation that rewrites unconditionally.
    import time

    time.sleep(0.01)
    merged, n_added = pmclose.append_closes(rows, path)
    mtime_2 = os.path.getmtime(path)
    assert n_added == 0
    assert mtime_1 == mtime_2
    assert len(merged) == 1


def test_append_closes_same_token_different_close_ts_both_kept(tmp_path):
    path = str(tmp_path / "pm_closes.json")
    pmclose.append_closes([_row("tok1", "2026-06-20T18:00:00Z", 0.45)], path)
    merged, n_added = pmclose.append_closes(
        [_row("tok1", "2026-06-27T18:00:00Z", 0.62)], path
    )
    assert n_added == 1
    assert len(merged) == 2


def test_append_closes_mixed_batch_only_new_rows_added(tmp_path):
    path = str(tmp_path / "pm_closes.json")
    pmclose.append_closes([_row("tok1", "2026-06-20T18:00:00Z", 0.45)], path)
    merged, n_added = pmclose.append_closes(
        [
            _row("tok1", "2026-06-20T18:00:00Z", 0.99),  # duplicate key -> ignored
            _row("tok2", "2026-06-21T15:00:00Z", 0.30),  # new -> added
        ],
        path,
    )
    assert n_added == 1
    assert len(merged) == 2
    # First-write-wins: tok1's original mid (0.45) is untouched by the dupe.
    tok1_row = next(r for r in merged if r["token_id"] == "tok1")
    assert tok1_row["mid"] == 0.45


def test_load_closes_missing_file_returns_empty(tmp_path):
    assert pmclose.load_closes(str(tmp_path / "nope.json")) == []


def test_load_closes_bad_json_returns_empty(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w") as fh:
        fh.write("{not json")
    assert pmclose.load_closes(path) == []


# ---------------------------------------------------------------------------
# Deciding-match kickoff resolution.
# ---------------------------------------------------------------------------


def _write_results(tmp_path, rows):
    path = str(tmp_path / "wc2026_results.json")
    with open(path, "w") as fh:
        json.dump({"results": rows, "_comment": "test"}, fh)
    return path


def test_load_team_last_kickoff_picks_latest_match(tmp_path):
    path = _write_results(
        tmp_path,
        [
            {"date": "2026-06-11", "fixture": "Ghana vs Egypt",
             "score": "1-0", "outcome": "home", "kickoff_utc": "2026-06-11T18:00:00Z"},
            {"date": "2026-06-20", "fixture": "Ghana vs Brazil",
             "score": "0-2", "outcome": "away", "kickoff_utc": "2026-06-20T21:00:00Z"},
        ],
    )
    last = pmclose.load_team_last_kickoff(path)
    assert last["ghana"] == "2026-06-20T21:00:00Z"
    assert last["egypt"] == "2026-06-11T18:00:00Z"
    assert last["brazil"] == "2026-06-20T21:00:00Z"


def test_load_team_last_kickoff_alias_resolution(tmp_path):
    path = _write_results(
        tmp_path,
        [
            {"date": "2026-06-12", "fixture": "USA vs Paraguay",
             "score": "1-1", "outcome": "draw", "kickoff_utc": "2026-06-12T13:00:00Z"},
        ],
    )
    last = pmclose.load_team_last_kickoff(path)
    # canonical() maps "USA" -> "United States"; index is keyed casefolded.
    assert "united states" in last
    assert "usa" not in last


def test_load_team_last_kickoff_missing_file(tmp_path):
    assert pmclose.load_team_last_kickoff(str(tmp_path / "missing.json")) == {}


def test_load_team_last_kickoff_skips_rows_without_kickoff(tmp_path):
    path = _write_results(
        tmp_path,
        [{"date": "2026-06-11", "fixture": "Ghana vs Egypt",
          "score": "1-0", "outcome": "home"}],
    )
    assert pmclose.load_team_last_kickoff(path) == {}


# ---------------------------------------------------------------------------
# Question parsing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected_team,expected_stage",
    [
        ("Will Ghana reach the Round of 16 at the 2026 FIFA World Cup?", "Ghana", "R16"),
        ("Will Japan reach the Round of 32 at the 2026 FIFA World Cup?", "Japan", "R32"),
        ("Will Brazil win the 2026 FIFA World Cup?", "Brazil", "win"),
        ("Will France reach the Quarterfinals at the 2026 FIFA World Cup?", "France", "QF"),
        ("Will England reach the Semifinals at the 2026 FIFA World Cup?", "England", "SF"),
        ("Will Argentina reach the Final at the 2026 FIFA World Cup?", "Argentina", "F"),
        ("Will USA win Group D in the 2026 FIFA World Cup?", "United States", "GW"),
        ("Will Ghana advance to the knockout stages at the 2026 FIFA World Cup?",
         "Ghana", "R32"),
    ],
)
def test_question_parsing(question, expected_team, expected_stage):
    assert pmclose.team_from_question(question) == expected_team
    assert pmclose.stage_from_question(question) == expected_stage


def test_question_parsing_non_string_inputs():
    assert pmclose.team_from_question(None) is None
    assert pmclose.stage_from_question(None) is None
    assert pmclose.team_from_question("random unrelated text") is None


# ---------------------------------------------------------------------------
# CLV arithmetic.
# ---------------------------------------------------------------------------


def test_clv_from_mid_matches_backed_over_close_minus_one():
    # Close mid 0.40 -> fair decimal 2.5; backed at 3.0 -> CLV = 3.0/2.5-1 = 0.20
    assert pmclose.clv_from_mid(3.0, 0.40) == pytest.approx(0.20)


def test_clv_from_mid_negative_when_price_worse_than_close():
    # Close mid 0.60 -> fair decimal 1.6667; backed at 1.5 -> CLV negative.
    clv = pmclose.clv_from_mid(1.5, 0.60)
    assert clv < 0
    assert clv == pytest.approx(1.5 / (1 / 0.60) - 1.0)


def test_clv_from_mid_degenerate_mid_returns_none():
    assert pmclose.clv_from_mid(2.0, 0.0) is None
    assert pmclose.clv_from_mid(2.0, 1.0) is None
    assert pmclose.clv_from_mid(2.0, None) is None


def test_clv_from_mid_bad_decimal_odds_returns_none():
    assert pmclose.clv_from_mid(None, 0.5) is None
    assert pmclose.clv_from_mid(0, 0.5) is None
    assert pmclose.clv_from_mid(-1, 0.5) is None


def test_closing_odds_from_mid():
    assert pmclose.closing_odds_from_mid(0.25) == pytest.approx(4.0)
    assert pmclose.closing_odds_from_mid(0) is None
    assert pmclose.closing_odds_from_mid(1) is None
    assert pmclose.closing_odds_from_mid("bad") is None


# ---------------------------------------------------------------------------
# Yes/No complement (fair_close_mid_for_bet).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selection,mid,expected",
    [
        ("Mexico Yes", 0.90, 0.90),
        ("Mexico reach R32 - Yes", 0.90, 0.90),
        ("Mexico No", 0.90, pytest.approx(0.10)),
        ("Japan reach R16 - No", 0.70, pytest.approx(0.30)),
        ("No — Ghana not eliminated in Round of 32", 0.55, pytest.approx(0.45)),
        ("Yes — Japan reaches R16", 0.70, 0.70),
    ],
)
def test_fair_close_mid_for_bet_yes_no_complement(selection, mid, expected):
    row = _row("tok", "2026-06-20T00:00:00Z", mid)
    assert pmclose.fair_close_mid_for_bet(selection, row) == expected


def test_fair_close_mid_for_bet_missing_mid_returns_none():
    row = _row("tok", "2026-06-20T00:00:00Z", 0.5)
    row["mid"] = None
    assert pmclose.fair_close_mid_for_bet("Mexico Yes", row) is None


# ---------------------------------------------------------------------------
# Bet <-> close join.
# ---------------------------------------------------------------------------


def test_match_bet_to_close_by_team_and_stage():
    closes = [
        _row("tokA", "2026-06-20T21:00:00Z", 0.55,
             question="Will Ghana be eliminated in the Round of 32 at the 2026 FIFA World Cup?"),
    ]
    by_team_stage, by_team = pmclose.index_closes(closes)
    match = pmclose.match_bet_to_close(
        "Ghana eliminated R32 of the World Cup",
        "No — Ghana not eliminated in Round of 32",
        None,
        by_team_stage,
        by_team,
    )
    assert match is not None
    assert match["token_id"] == "tokA"


def test_match_bet_to_close_team_only_fallback_when_unambiguous():
    closes = [
        _row("tokA", "2026-06-20T21:00:00Z", 0.55,
             question="Will Japan reach the Round of 16 at the 2026 FIFA World Cup?"),
    ]
    by_team_stage, by_team = pmclose.index_closes(closes)
    # selection carries no parseable stage text.
    match = pmclose.match_bet_to_close(
        "2026 FIFA World Cup - Japan Round of 16",
        "Japan reach R16 - No",
        None,
        by_team_stage,
        by_team,
    )
    assert match is not None
    assert match["token_id"] == "tokA"


def test_match_bet_to_close_ambiguous_multi_stage_no_guess():
    closes = [
        _row("tokA", "2026-06-20T21:00:00Z", 0.55, condition_id="0x1",
             question="Will Japan reach the Round of 16 at the 2026 FIFA World Cup?"),
        _row("tokB", "2026-06-27T21:00:00Z", 0.30, condition_id="0x2",
             question="Will Japan reach the Quarterfinals at the 2026 FIFA World Cup?"),
    ]
    by_team_stage, by_team = pmclose.index_closes(closes)
    match = pmclose.match_bet_to_close(
        "Japan advancement bet, no parseable stage in desc",
        "Japan Yes",
        None,
        by_team_stage,
        by_team,
    )
    assert match is None


def test_match_bet_to_close_no_team_parsed_returns_none():
    by_team_stage, by_team = pmclose.index_closes([])
    assert pmclose.match_bet_to_close("Exact Score", "Ghana 1-0 Egypt", None,
                                       by_team_stage, by_team) is None


def test_index_closes_drops_unparseable_question():
    closes = [_row("tokX", "2026-06-20T21:00:00Z", 0.5, question="some unrelated text")]
    by_team_stage, by_team = pmclose.index_closes(closes)
    assert by_team_stage == {}
    assert by_team == {}
