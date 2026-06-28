"""Tests for the /arb bot command (locked-in mispricings)."""
from __future__ import annotations

import json

from wca.bot.app import dispatch, handle_arb


def test_arb_no_feed(tmp_path):
    out = handle_arb(arb_path=str(tmp_path / "missing.json"))
    assert "No arb feed cached" in out


def test_arb_empty_feed(tmp_path):
    p = tmp_path / "arb.json"
    p.write_text(json.dumps({"meta": {"generated": "2026-06-28T15:00:00"}, "arbs": []}))
    out = handle_arb(arb_path=str(p), now_utc="2026-06-28T15:30:00")
    assert "No risk-free lock-ins" in out


def test_arb_renders_venue_units(tmp_path):
    p = tmp_path / "arb.json"
    feed = {
        "meta": {"generated": "2026-06-28T15:00:00"},
        "arbs": [{
            "fixture": "South Africa vs Canada", "market": "1X2", "selection": "Canada",
            "guaranteed_pct": 0.031,
            "legs": [
                {"venue": "Bet365", "currency": "GBP", "side": "back", "net": 2.10, "stake": 47.6},
                {"venue": "Polymarket", "currency": "USD", "side": "back", "net": 2.27, "stake": 58.7},
            ],
        }],
    }
    p.write_text(json.dumps(feed))
    out = handle_arb(arb_path=str(p), now_utc="2026-06-28T15:30:00")
    assert "+3.10% guaranteed" in out
    assert "Bet365 @ 2.10 — £47.60" in out      # sportsbook: £ + decimal
    assert "Polymarket @ 44¢ — $58.70" in out    # PM: $ + cent share price


def test_arb_routed_by_dispatch():
    assert "Arbitrage" in dispatch("/arb", "data/wca.db")
    assert "/arb" in dispatch("/help", "data/wca.db")
