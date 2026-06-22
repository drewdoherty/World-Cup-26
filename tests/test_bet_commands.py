"""End-to-end wiring and hardening tests for /card, /next, /goalscorers, /accas.

Covers:
  - Rule 1 (provenance): source path + generation timestamp in every reply.
  - NO BET fallback when generation timestamp is absent.
  - Rung-0 longshot guard: legs with odds > 10 excluded from accas.
  - accas.load_odds_df: model_1x2 probs -> fair odds conversion.
  - build_accas_from_odds: no longer requires fixtures_meta.
  - handle_accas: NO BET when feed has no generation timestamp or no data.
"""
from __future__ import annotations

import json
import os

import pandas as pd
import pytest

from wca import accas
from wca.bot import app
from wca.cardcache import write_card


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_cache(tmp_path, name: str, body: str, ts: str = "2026-06-21T10:00:00") -> str:
    path = str(tmp_path / name)
    write_card(body, path=path, ts_utc=ts)
    return path


def _write_feed(tmp_path, fixtures=None, generated="2026-06-21T10:00:00") -> str:
    path = str(tmp_path / "scores.json")
    data: dict = {}
    if generated is not None:
        data["meta"] = {"generated": generated}
    if fixtures is not None:
        data["fixtures"] = fixtures
    with open(path, "w") as fh:
        json.dump(data, fh)
    return path


# ---------------------------------------------------------------------------
# handle_card — provenance + NO BET
# ---------------------------------------------------------------------------

class TestHandleCard:
    def test_provenance_line_in_fresh_reply(self, tmp_path):
        path = _write_cache(tmp_path, "card_latest.md", "pick 1: Brazil Win")
        out = app.handle_card("irrelevant.db", card_path=path,
                              now_utc="2026-06-21T11:00:00")
        assert "2026-06-21T10:00:00" in out
        assert "card_latest.md" in out or "card" in out.lower()
        assert "STALE" not in out

    def test_provenance_includes_source_path(self, tmp_path):
        path = _write_cache(tmp_path, "card_latest.md", "picks here",
                            ts="2026-06-21T12:00:00")
        out = app.handle_card("irrelevant.db", card_path=path,
                              now_utc="2026-06-21T13:00:00")
        # Source path must appear in the output.
        assert path in out or os.path.basename(path) in out

    def test_no_bet_when_no_generated_timestamp(self, tmp_path):
        path = str(tmp_path / "card_latest.md")
        write_card("picks here", path=path, ts_utc=None)
        out = app.handle_card("irrelevant.db", card_path=path,
                              now_utc="2026-06-21T12:00:00")
        assert "NO BET" in out

    def test_stale_banner_present_when_old(self, tmp_path):
        path = _write_cache(tmp_path, "card_latest.md", "picks",
                            ts="2026-06-20T00:00:00")
        out = app.handle_card("irrelevant.db", card_path=path,
                              now_utc="2026-06-21T12:00:00")  # 36h later
        assert "STALE" in out

    def test_body_preserved_in_output(self, tmp_path):
        path = _write_cache(tmp_path, "card_latest.md",
                            "*1. Brazil vs Mexico* — Brazil @ *1.90*")
        out = app.handle_card("irrelevant.db", card_path=path,
                              now_utc="2026-06-21T11:00:00")
        assert "Brazil vs Mexico" in out

    def test_no_card_cached_message(self, tmp_path):
        path = str(tmp_path / "nonexistent.md")
        out = app.handle_card("irrelevant.db", card_path=path)
        assert "No card cached" in out


# ---------------------------------------------------------------------------
# handle_next — provenance + NO BET
# ---------------------------------------------------------------------------

class TestHandleNext:
    def test_provenance_line_in_fresh_reply(self, tmp_path):
        path = _write_cache(tmp_path, "next_latest.md",
                            "⚽ *Next match* — Alpha vs Bravo")
        out = app.handle_next(next_path=path, now_utc="2026-06-21T11:00:00")
        assert "2026-06-21T10:00:00" in out
        assert "STALE" not in out

    def test_body_present_in_fresh_reply(self, tmp_path):
        path = _write_cache(tmp_path, "next_latest.md",
                            "⚽ *Next match* — Alpha vs Bravo")
        out = app.handle_next(next_path=path, now_utc="2026-06-21T11:00:00")
        assert "Alpha vs Bravo" in out

    def test_no_bet_when_no_generated_timestamp(self, tmp_path):
        path = str(tmp_path / "next_latest.md")
        write_card("preview here", path=path, ts_utc=None)
        out = app.handle_next(next_path=path, now_utc="2026-06-21T12:00:00")
        assert "NO BET" in out

    def test_stale_banner_present_when_old(self, tmp_path):
        path = _write_cache(tmp_path, "next_latest.md",
                            "preview", ts="2026-06-20T00:00:00")
        out = app.handle_next(next_path=path, now_utc="2026-06-21T12:00:00")
        assert "STALE" in out

    def test_no_cache_message(self, tmp_path):
        out = app.handle_next(next_path=str(tmp_path / "missing.md"))
        assert "No preview cached" in out


# ---------------------------------------------------------------------------
# handle_goalscorers — provenance + NO BET
# ---------------------------------------------------------------------------

class TestHandleGoalscorers:
    def test_provenance_line_in_fresh_reply(self, tmp_path):
        path = _write_cache(tmp_path, "goalscorers_latest.md",
                            "⚽ *Goalscorers* — next 5 games")
        out = app.handle_goalscorers(goalscorers_path=path,
                                     now_utc="2026-06-21T11:00:00")
        assert "2026-06-21T10:00:00" in out
        assert "STALE" not in out

    def test_body_present_in_fresh_reply(self, tmp_path):
        path = _write_cache(tmp_path, "goalscorers_latest.md",
                            "Alpha Striker — anytime 2.50")
        out = app.handle_goalscorers(goalscorers_path=path,
                                     now_utc="2026-06-21T11:00:00")
        assert "Alpha Striker" in out

    def test_no_bet_when_no_generated_timestamp(self, tmp_path):
        path = str(tmp_path / "goalscorers_latest.md")
        write_card("scorer picks", path=path, ts_utc=None)
        out = app.handle_goalscorers(goalscorers_path=path,
                                     now_utc="2026-06-21T12:00:00")
        assert "NO BET" in out

    def test_stale_banner_present_when_old(self, tmp_path):
        path = _write_cache(tmp_path, "goalscorers_latest.md",
                            "scorers", ts="2026-06-20T00:00:00")
        out = app.handle_goalscorers(goalscorers_path=path,
                                     now_utc="2026-06-21T12:00:00")
        assert "STALE" in out

    def test_no_cache_message(self, tmp_path):
        out = app.handle_goalscorers(goalscorers_path=str(tmp_path / "missing.md"))
        assert "No card cached" in out


# ---------------------------------------------------------------------------
# accas.load_odds_df — model_1x2 -> fair odds conversion
# ---------------------------------------------------------------------------

class TestLoadOddsDf:
    def _feed(self, tmp_path, fixtures):
        path = str(tmp_path / "scores.json")
        with open(path, "w") as fh:
            json.dump({"meta": {"generated": "2026-06-21T10:00:00"},
                       "fixtures": fixtures}, fh)
        return path

    def test_returns_empty_df_on_missing_file(self, tmp_path):
        df = accas.load_odds_df(str(tmp_path / "missing.json"))
        assert df.empty

    def test_returns_empty_df_on_empty_fixtures(self, tmp_path):
        path = self._feed(tmp_path, [])
        assert accas.load_odds_df(path).empty

    def test_converts_model_probs_to_fair_odds(self, tmp_path):
        path = self._feed(tmp_path, [{
            "fixture": "Brazil vs Morocco",
            "model_1x2": {"home": 0.50, "draw": 0.25, "away": 0.25},
        }])
        df = accas.load_odds_df(path)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["home_team"] == "Brazil"
        assert row["away_team"] == "Morocco"
        assert abs(row["home_odds"] - 2.0) < 1e-4   # 1/0.50
        assert abs(row["draw_odds"] - 4.0) < 1e-4   # 1/0.25
        assert abs(row["away_odds"] - 4.0) < 1e-4   # 1/0.25

    def test_splits_vs_and_v_spellings(self, tmp_path):
        path = self._feed(tmp_path, [
            {"fixture": "Alpha vs Bravo", "model_1x2": {"home": 0.4, "draw": 0.3, "away": 0.3}},
            {"fixture": "Charlie v Delta", "model_1x2": {"home": 0.5, "draw": 0.25, "away": 0.25}},
        ])
        df = accas.load_odds_df(path)
        assert len(df) == 2
        assert set(df["home_team"].tolist()) == {"Alpha", "Charlie"}

    def test_zero_prob_gives_zero_odds(self, tmp_path):
        path = self._feed(tmp_path, [{
            "fixture": "Alpha vs Bravo",
            "model_1x2": {"home": 0.0, "draw": 0.5, "away": 0.5},
        }])
        df = accas.load_odds_df(path)
        assert df.iloc[0]["home_odds"] == 0.0

    def test_skips_fixtures_without_fixture_key(self, tmp_path):
        path = self._feed(tmp_path, [
            {"model_1x2": {"home": 0.5, "draw": 0.25, "away": 0.25}},  # no "fixture"
            {"fixture": "Alpha vs Bravo", "model_1x2": {"home": 0.5, "draw": 0.25, "away": 0.25}},
        ])
        df = accas.load_odds_df(path)
        assert len(df) == 1


# ---------------------------------------------------------------------------
# accas.build_accas_from_odds — longshot guard + no fixtures_meta
# ---------------------------------------------------------------------------

class TestBuildAccasFromOdds:
    def _make_df(self, rows):
        return pd.DataFrame(rows)

    def test_no_fixtures_meta_required(self):
        df = self._make_df([
            {"home_team": "A", "away_team": "B", "home_odds": 2.5, "draw_odds": 3.0, "away_odds": 3.5},
            {"home_team": "C", "away_team": "D", "home_odds": 2.0, "draw_odds": 3.2, "away_odds": 4.0},
            {"home_team": "E", "away_team": "F", "home_odds": 2.8, "draw_odds": 3.3, "away_odds": 2.9},
            {"home_team": "G", "away_team": "H", "home_odds": 3.0, "draw_odds": 3.5, "away_odds": 2.2},
        ])
        result = accas.build_accas_from_odds(df, min_legs=4)
        assert len(result) > 0

    def test_empty_df_returns_empty_list(self):
        assert accas.build_accas_from_odds(pd.DataFrame()) == []

    def test_fewer_fixtures_than_min_legs_returns_empty(self):
        df = self._make_df([
            {"home_team": "A", "away_team": "B", "home_odds": 2.5, "draw_odds": 3.0, "away_odds": 3.5},
            {"home_team": "C", "away_team": "D", "home_odds": 2.0, "draw_odds": 3.2, "away_odds": 4.0},
        ])
        assert accas.build_accas_from_odds(df, min_legs=4) == []

    def test_longshot_legs_excluded(self):
        # One fixture has an extreme away odds (>10) — it must be in excluded_longshot.
        df = self._make_df([
            {"home_team": "A", "away_team": "B", "home_odds": 2.5, "draw_odds": 3.0, "away_odds": 15.0},
            {"home_team": "C", "away_team": "D", "home_odds": 2.0, "draw_odds": 3.2, "away_odds": 4.0},
            {"home_team": "E", "away_team": "F", "home_odds": 2.8, "draw_odds": 3.3, "away_odds": 2.9},
            {"home_team": "G", "away_team": "H", "home_odds": 3.0, "draw_odds": 3.5, "away_odds": 2.2},
        ])
        result = accas.build_accas_from_odds(df, min_legs=4)
        assert result, "expected at least one acca"
        excluded = result[0].get("excluded_longshot", [])
        assert any(
            ex.get("selection") == "B" and ex.get("odds") == 15.0
            for ex in excluded
        ), "expected B @ 15.0 in excluded_longshot"
        # B at 15.0 must NOT appear in any leg.
        for acca in result:
            for leg in acca.get("legs", []):
                assert leg["odds"] != 15.0

    def test_legs_within_bounds_included(self):
        df = self._make_df([
            {"home_team": "A", "away_team": "B", "home_odds": 2.5, "draw_odds": 9.5, "away_odds": 3.0},
            {"home_team": "C", "away_team": "D", "home_odds": 2.0, "draw_odds": 3.2, "away_odds": 4.0},
            {"home_team": "E", "away_team": "F", "home_odds": 2.8, "draw_odds": 3.3, "away_odds": 2.9},
            {"home_team": "G", "away_team": "H", "home_odds": 3.0, "draw_odds": 3.5, "away_odds": 2.2},
        ])
        result = accas.build_accas_from_odds(df, min_legs=4)
        assert result
        # 9.5 is within bounds (≤ 10), so it may appear as a leg.
        all_odds = [leg["odds"] for a in result for leg in a.get("legs", [])]
        # At minimum the fixtures produce 4 legs, all with odds in [2.0, 10.0].
        assert all(2.0 <= o <= 10.0 for o in all_odds)

    def test_acca_total_odds_is_product_of_legs(self):
        df = self._make_df([
            {"home_team": "A", "away_team": "B", "home_odds": 2.5, "draw_odds": 3.0, "away_odds": 4.0},
            {"home_team": "C", "away_team": "D", "home_odds": 2.0, "draw_odds": 3.2, "away_odds": 4.0},
            {"home_team": "E", "away_team": "F", "home_odds": 2.8, "draw_odds": 3.3, "away_odds": 2.9},
            {"home_team": "G", "away_team": "H", "home_odds": 3.0, "draw_odds": 3.5, "away_odds": 2.2},
        ])
        result = accas.build_accas_from_odds(df, min_legs=4)
        for acca in result:
            product = 1.0
            for leg in acca["legs"]:
                product *= leg["odds"]
            assert abs(product - acca["total_odds"]) < 1e-6


# ---------------------------------------------------------------------------
# handle_accas — NO BET + provenance + data wiring
# ---------------------------------------------------------------------------

class TestHandleAccas:
    def test_no_bet_when_no_generated_timestamp(self, tmp_path):
        path = _write_feed(tmp_path, fixtures=[], generated=None)
        out = app.handle_accas(scores_path=path)
        assert "NO BET" in out

    def test_no_bet_when_feed_file_missing(self, tmp_path):
        out = app.handle_accas(scores_path=str(tmp_path / "missing.json"))
        assert "NO BET" in out

    def test_no_bet_with_concrete_reason_when_no_valid_accas(self, tmp_path):
        # All legs < 2.0: no qualifying legs -> NO BET with explanation.
        path = _write_feed(tmp_path, fixtures=[
            {"fixture": "A vs B", "model_1x2": {"home": 0.80, "draw": 0.15, "away": 0.05}},
            {"fixture": "C vs D", "model_1x2": {"home": 0.85, "draw": 0.10, "away": 0.05}},
        ], generated="2026-06-21T10:00:00")
        out = app.handle_accas(scores_path=path)
        assert "NO BET" in out
        assert "2.0" in out or "odds" in out.lower()

    def test_provenance_line_present_when_accas_found(self, tmp_path, monkeypatch):
        path = _write_feed(tmp_path, fixtures=[
            {"fixture": "A vs B", "model_1x2": {"home": 0.35, "draw": 0.30, "away": 0.35}},
        ], generated="2026-06-21T10:00:00")

        monkeypatch.setattr(accas, "load_odds_df",
                            lambda p: pd.DataFrame([{"home_team": "A", "away_team": "B",
                                                     "home_odds": 3.0, "draw_odds": 3.5,
                                                     "away_odds": 3.0}]))
        monkeypatch.setattr(accas, "build_accas_from_odds",
                            lambda *a, **k: [{"legs": [{"fixture": "A vs B",
                                                        "market": "Match Result",
                                                        "selection": "A", "odds": 3.0}],
                                              "total_odds": 3.0, "implied_prob": 0.33,
                                              "excluded_longshot": []}])
        monkeypatch.setattr(accas, "format_accas", lambda lst: "ACCA-BODY")

        out = app.handle_accas(scores_path=path)
        assert "2026-06-21T10:00:00" in out
        assert "ACCA-BODY" in out

    def test_stale_feed_shows_banner(self, tmp_path, monkeypatch):
        path = _write_feed(tmp_path, fixtures=[
            {"fixture": "A vs B", "model_1x2": {"home": 0.4, "draw": 0.3, "away": 0.3}},
        ], generated="2026-06-01T00:00:00")

        monkeypatch.setattr(accas, "load_odds_df",
                            lambda p: pd.DataFrame([{"home_team": "A", "away_team": "B",
                                                     "home_odds": 2.5, "draw_odds": 3.0,
                                                     "away_odds": 3.5}]))
        monkeypatch.setattr(accas, "build_accas_from_odds",
                            lambda *a, **k: [{"legs": [], "total_odds": 1.0,
                                              "implied_prob": 1.0, "excluded_longshot": []}])
        monkeypatch.setattr(accas, "format_accas", lambda lst: "ACCA-BODY")

        out = app.handle_accas(scores_path=path)
        assert "STALE" in out
        assert "ACCA-BODY" in out

    def test_end_to_end_with_real_feed(self, tmp_path):
        """Full pipeline from feed JSON -> accas output (no mocking)."""
        path = _write_feed(tmp_path, fixtures=[
            {"fixture": "Alpha vs Bravo",
             "model_1x2": {"home": 0.35, "draw": 0.30, "away": 0.35}},
            {"fixture": "Charlie vs Delta",
             "model_1x2": {"home": 0.30, "draw": 0.30, "away": 0.40}},
            {"fixture": "Echo vs Foxtrot",
             "model_1x2": {"home": 0.40, "draw": 0.25, "away": 0.35}},
            {"fixture": "Golf vs Hotel",
             "model_1x2": {"home": 0.30, "draw": 0.30, "away": 0.40}},
        ], generated="2026-06-21T10:00:00")
        out = app.handle_accas(scores_path=path)
        # Either accas found (shows "Acca") or a concrete NO BET reason.
        assert "Acca" in out or "NO BET" in out
        # Provenance line always present.
        assert "2026-06-21T10:00:00" in out
