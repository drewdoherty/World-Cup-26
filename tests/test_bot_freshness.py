"""Tests for the Telegram command staleness guards.

Every cache-backed command must make staleness impossible to miss: when the
underlying data is older than its window the reply carries a ⚠️ STALE banner,
and when fresh it does not. This locks in that guarantee for /scores, /accas,
/boost and /structure (the previously ungated / inconsistent commands), plus
the shared helpers.
"""
from __future__ import annotations

import json

from wca.bot import app


def test_stale_banner_helpers():
    now = "2026-06-18T12:00:00"
    # 7h old card with a 6h window -> stale banner.
    banner = app._stale_banner("2026-06-18T05:00:00", now, 6.0, label="card")
    assert "STALE" in banner and "card" in banner
    # 2h old -> fresh, no banner.
    assert app._stale_banner("2026-06-18T10:00:00", now, 6.0) == ""
    # Feed-style 'YYYY-MM-DD HH:MM:SS UTC' timestamps normalise correctly.
    age = app._staleness_age_hours("2026-06-18 06:00:00 UTC", now)
    assert age is not None and abs(age - 6.0) < 1e-6
    # Unparseable -> no banner (never crash, never false alarm).
    assert app._stale_banner(None, now, 6.0) == ""


def test_feed_generated_reads_meta(tmp_path):
    feed = tmp_path / "scores.json"
    feed.write_text(json.dumps({"meta": {"generated": "2026-06-18 09:00:00 UTC"},
                                "fixtures": []}), encoding="utf-8")
    assert app._feed_generated(str(feed)) == "2026-06-18 09:00:00 UTC"
    assert app._feed_generated(str(tmp_path / "missing.json")) is None


def test_accas_flags_stale_feed(tmp_path, monkeypatch):
    # A stale, populated feed must carry the banner. We stub the data layer so
    # the test isolates banner + glue logic without needing real fixture data.
    feed = tmp_path / "scores.json"
    feed.write_text(json.dumps({"meta": {"generated": "2026-06-01 00:00:00 UTC"},
                                "fixtures": [{"fixture": "Alpha vs Bravo"}]}),
                    encoding="utf-8")

    from wca import accas

    monkeypatch.setattr(accas, "build_accas",
                        lambda **k: {"mode": "value", "accas": []})
    monkeypatch.setattr(accas, "format_accas", lambda r: "ACCA-BODY")

    reply = app.handle_accas(scores_path=str(feed))
    assert "STALE" in reply
    assert "ACCA-BODY" in reply

