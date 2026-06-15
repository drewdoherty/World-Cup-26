"""Tests for the martj42 cleaning overlay and 2-source reconciliation.

No network: source results are constructed directly as FixtureResult objects.
"""
from __future__ import annotations

import io
import textwrap

import pandas as pd
import pytest

from wca.data import cleaning, reconcile
from wca.data.fixture_sources import FixtureResult


_RAW = textwrap.dedent("""\
    date,home_team,away_team,home_score,away_score,tournament,city,country,neutral
    2025-06-06,North Macedonia,Belgium,1,1,FIFA World Cup qualification,Skopje,North Macedonia,FALSE
    2026-06-06,Bermuda,Cape Verde,3,0,Friendly,East Hartford,United States,TRUE
    2026-06-04,Guatemala,Czech Republic,1,5,Friendly,Harrison,United States,TRUE
    2026-06-14,Germany,Curaçao,NA,NA,FIFA World Cup,Houston,United States,TRUE
""")


def _raw_df():
    return pd.read_csv(io.StringIO(_RAW), dtype=str, keep_default_na=False)


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

class TestApplyCorrections:
    def test_update_score(self):
        corr = [{"date": "2026-06-06", "home_team": "Bermuda", "away_team": "Cape Verde",
                 "corrected_home_score": 0, "corrected_away_score": 3, "source": "ESPN"}]
        df, audit = cleaning.apply_corrections(_raw_df(), corr)
        row = df[(df.home_team == "Bermuda")].iloc[0]
        assert (row.home_score, row.away_score) == ("0", "3")
        assert audit[0]["type"] == "update" and audit[0]["before"] == "3-0"

    def test_fill_missing_score(self):
        corr = [{"date": "2026-06-14", "home_team": "Germany", "away_team": "Curaçao",
                 "corrected_home_score": 7, "corrected_away_score": 1, "source": "FIFA"}]
        df, audit = cleaning.apply_corrections(_raw_df(), corr)
        row = df[df.home_team == "Germany"].iloc[0]
        assert (row.home_score, row.away_score) == ("7", "1")

    def test_insert_omitted_fixture(self):
        corr = [{"date": "2025-09-05", "home_team": "South Africa", "away_team": "Lesotho",
                 "corrected_home_score": 3, "corrected_away_score": 0, "source": "FIFA",
                 "tournament": "FIFA World Cup qualification", "neutral": False}]
        df, audit = cleaning.apply_corrections(_raw_df(), corr)
        assert len(df) == 5
        assert audit[0]["type"] == "insert"
        row = df[df.home_team == "South Africa"].iloc[0]
        assert (row.home_score, row.away_score) == ("3", "0")
        assert row.neutral == "FALSE"

    def test_idempotent_noop(self):
        corr = [{"date": "2026-06-06", "home_team": "Bermuda", "away_team": "Cape Verde",
                 "corrected_home_score": 3, "corrected_away_score": 0, "source": "x"}]
        df, audit = cleaning.apply_corrections(_raw_df(), corr)
        assert audit == []  # already matches -> nothing recorded

    def test_neutral_casing_preserved(self):
        df, _ = cleaning.apply_corrections(_raw_df(), [])
        assert set(df.neutral.unique()) <= {"TRUE", "FALSE"}

    def test_ambiguous_key_raises(self):
        raw = _raw_df()
        raw = pd.concat([raw, raw.iloc[[1]]], ignore_index=True)  # dup Bermuda row
        with pytest.raises(ValueError):
            cleaning.apply_corrections(raw, [
                {"date": "2026-06-06", "home_team": "Bermuda", "away_team": "Cape Verde",
                 "corrected_home_score": 0, "corrected_away_score": 3, "source": "x"}])


class TestValidate:
    def test_rejects_new_duplicates(self):
        raw = _raw_df()
        dup = pd.concat([raw, raw.iloc[[0]]], ignore_index=True)
        with pytest.raises(ValueError):
            cleaning.validate(dup, raw)

    def test_passes_clean(self):
        df, _ = cleaning.apply_corrections(_raw_df(), [])
        cleaning.validate(df, _raw_df())  # no raise


class TestMergeCorrection:
    def test_insert_new(self):
        merged, changed = cleaning.merge_correction([], {
            "date": "d", "home_team": "h", "away_team": "a",
            "corrected_home_score": 1, "corrected_away_score": 0})
        assert changed and len(merged) == 1

    def test_noop_when_identical(self):
        base = [{"date": "d", "home_team": "h", "away_team": "a",
                 "corrected_home_score": 1, "corrected_away_score": 0}]
        merged, changed = cleaning.merge_correction(base, dict(base[0]))
        assert not changed and len(merged) == 1

    def test_update_existing(self):
        base = [{"date": "d", "home_team": "h", "away_team": "a",
                 "corrected_home_score": 1, "corrected_away_score": 0}]
        merged, changed = cleaning.merge_correction(base, {
            "date": "d", "home_team": "h", "away_team": "a",
            "corrected_home_score": 2, "corrected_away_score": 2})
        assert changed and merged[0]["corrected_home_score"] == 2


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _gathered(a_list, b_list):
    return {"espn": a_list, "thesportsdb": b_list}


class TestReconcile:
    def test_both_agree_and_differ_stages_update(self):
        d = "2026-06-04"
        a = FixtureResult(d, "Czech Republic", "Guatemala", 3, 1, "espn")
        b = FixtureResult(d, "Czech Republic", "Guatemala", 3, 1, "thesportsdb")
        rec = reconcile.reconcile_date(_raw_df(), _gathered([a], [b]), d)
        assert len(rec.staged) == 1
        s = rec.staged[0]
        # martj42 lists Guatemala home -> consensus re-expressed in that orientation
        assert s["home_team"] == "Guatemala" and s["away_team"] == "Czech Republic"
        assert s["corrected_home_score"] == 1 and s["corrected_away_score"] == 3

    def test_orientation_flip_handled(self):
        # Source lists teams in opposite order to martj42 (Bermuda home).
        d = "2026-06-06"
        a = FixtureResult(d, "Cape Verde", "Bermuda", 3, 0, "espn")
        b = FixtureResult(d, "Cape Verde", "Bermuda", 3, 0, "thesportsdb")
        rec = reconcile.reconcile_date(_raw_df(), _gathered([a], [b]), d)
        s = rec.staged[0]
        assert s["home_team"] == "Bermuda" and s["corrected_home_score"] == 0
        assert s["corrected_away_score"] == 3

    def test_fill_missing(self):
        d = "2026-06-14"
        a = FixtureResult(d, "Germany", "Curaçao", 7, 1, "espn")
        b = FixtureResult(d, "Germany", "Curaçao", 7, 1, "thesportsdb")
        rec = reconcile.reconcile_date(_raw_df(), _gathered([a], [b]), d)
        assert rec.staged[0]["_op"] == "fill"
        assert rec.staged[0]["corrected_home_score"] == 7

    def test_sources_disagree_goes_to_review(self):
        d = "2026-06-04"
        a = FixtureResult(d, "Czech Republic", "Guatemala", 3, 1, "espn")
        b = FixtureResult(d, "Czech Republic", "Guatemala", 2, 1, "thesportsdb")
        rec = reconcile.reconcile_date(_raw_df(), _gathered([a], [b]), d)
        assert not rec.staged and rec.review[0]["issue"] == "sources_disagree"

    def test_single_source_goes_to_review(self):
        d = "2026-06-04"
        a = FixtureResult(d, "Czech Republic", "Guatemala", 3, 1, "espn")
        rec = reconcile.reconcile_date(_raw_df(), _gathered([a], []), d)
        assert not rec.staged and rec.review[0]["issue"] == "single_source"

    def test_agree_and_already_correct_no_stage(self):
        d = "2025-06-06"
        a = FixtureResult(d, "North Macedonia", "Belgium", 1, 1, "espn")
        b = FixtureResult(d, "North Macedonia", "Belgium", 1, 1, "thesportsdb")
        rec = reconcile.reconcile_date(_raw_df(), _gathered([a], [b]), d)
        assert not rec.staged  # martj42 already correct -> nothing to do

    def test_omitted_fixture_stages_insert(self):
        d = "2025-09-05"
        a = FixtureResult(d, "South Africa", "Lesotho", 3, 0, "espn", "WC Qualifier")
        b = FixtureResult(d, "South Africa", "Lesotho", 3, 0, "thesportsdb", "WC Qualifier")
        rec = reconcile.reconcile_date(_raw_df(), _gathered([a], [b]), d)
        assert rec.staged[0]["_op"] == "insert"
