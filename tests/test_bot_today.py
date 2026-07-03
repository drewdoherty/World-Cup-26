"""/today: cache-composed instructions, freshness-stamped, missing-feed honest."""
from __future__ import annotations

import json

from wca.bot import app


def _write(p, obj):
    p.write_text(json.dumps(obj), encoding="utf-8")


def test_today_composes_sections(tmp_path):
    recs = tmp_path / "recs.json"
    ideas = tmp_path / "ideas.json"
    promos = tmp_path / "promos.json"
    _write(recs, {"meta": {"generated": "2026-07-03 11:00:00 UTC", "withheld_count": 3},
                  "match_singles": [{"match": "A vs B", "selection": "A",
                                     "price": 2.1, "ev_net": 0.06, "stake": 25.0}],
                  "advancement_futures": [], "event_props": []})
    _write(ideas, {"meta": {"generated": "2026-07-03T11:05:00Z"},
                   "ideas": [{"bucket": "moneyline", "match": "C vs D",
                              "side": "BUY", "selection": "C", "price_c": 55.0,
                              "model_c": 60.0, "ev_pct": 5.0, "size_usd": 40.0,
                              "hours_out": 30.0}]})
    _write(promos, {"promotions": [{"id": 1}, {"id": 2}]})
    out = app.handle_today("db-unused", recs_path=str(recs),
                           ideas_path=str(ideas), promos_path=str(promos))
    assert "Bet recs" in out and "A vs B" in out and "stake $25.00" in out
    assert "PM trade ideas" in out and "[MONEYLINE] C vs D" in out
    assert "Y PM-<n>" in out
    assert "2 catalogued" in out
    assert "withheld: 3" in out
    assert "combined £3,000" in out


def test_today_missing_feeds_are_flagged(tmp_path):
    out = app.handle_today("db-unused",
                           recs_path=str(tmp_path / "nope1.json"),
                           ideas_path=str(tmp_path / "nope2.json"),
                           promos_path=str(tmp_path / "nope3.json"))
    assert out.count("feed missing ⚠") >= 2
    assert "promos feed missing" in out


def test_today_stale_feed_is_marked(tmp_path):
    recs = tmp_path / "recs.json"
    _write(recs, {"meta": {"generated": "2026-07-01 06:00:00 UTC"},
                  "match_singles": []})
    out = app.handle_today("db-unused", recs_path=str(recs),
                           ideas_path=str(tmp_path / "n.json"),
                           promos_path=str(tmp_path / "n2.json"))
    assert "⚠STALE" in out


def test_dispatch_routes_today(tmp_path):
    out = app.dispatch("/today", str(tmp_path / "db.db"))
    assert "Today — betting instructions" in out
