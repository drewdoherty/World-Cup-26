"""End-to-end wiring and hardening tests for /card, /next, /goalscorers, /accas.

Covers:
  - Rule 1 (provenance): source path + generation timestamp in every reply.
  - NO BET fallback when generation timestamp is absent.
  - Rung-0 longshot guard: legs with odds > 10 excluded from accas.
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
        assert "NO TRADE" in out

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
        assert "NO TRADE" in out

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
        assert "NO TRADE" in out

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
