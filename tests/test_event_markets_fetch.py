"""Tests for scripts/wca_event_markets.py's Gamma /events pagination."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import requests

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_event_markets as em  # noqa: E402


def _resp(json_data: Any, status_code: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_data
    m.raise_for_status = MagicMock()
    return m


class TestFetchSoccerEvents:
    def test_stops_gracefully_on_gamma_offset_ceiling_422(self, monkeypatch: Any) -> None:
        """Mirrors the fix in wca.data.polymarket.find_world_cup_markets:
        Gamma's /events paginator 422s past an undocumented offset ceiling
        (observed live 2026-07-13: offset=2000 -> 200, offset=2100 -> 422).
        fetch_soccer_events has its own separate pagination loop (broader
        scope, no WC-keyword filter) and needs the same fix.
        """
        page1 = [{"id": "1", "slug": "fifwc-a-b", "title": "A vs. B", "markets": []}]
        call_count = [0]

        def mock_get(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _resp(page1)
            resp = _resp({}, status_code=422)
            error = requests.HTTPError("422 Client Error", response=resp)
            resp.raise_for_status = MagicMock(side_effect=error)
            return resp

        monkeypatch.setattr(em.PM.requests, "get", mock_get)
        events = em.fetch_soccer_events()

        assert [e["id"] for e in events] == ["1"]
