"""Tests for the Polymarket CLOB price-history client (parse path, no network)."""

from __future__ import annotations

from wca.data import pm_clob_history as CH


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_price_history_parses_and_filters(monkeypatch):
    payload = {"history": [
        {"t": 1751290000, "p": "0.25"},
        {"t": 1751293600, "p": 0.30},
        {"t": 1751297200, "p": 1.5},     # out of [0,1] -> dropped
        {"t": 1751300800, "p": "bad"},   # unparseable -> dropped
    ]}
    monkeypatch.setattr(CH.requests, "get", lambda *a, **k: _Resp(payload))
    out = CH.price_history("tok", interval="max", fidelity=60)
    assert [round(p, 2) for _, p in out] == [0.25, 0.30]
    assert out[0][0] < out[1][0]                      # time-sorted, tz-aware
    assert out[0][0].tzinfo is not None


def test_price_history_empty_token_no_call():
    assert CH.price_history("") == []


def test_price_history_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(CH.requests, "get", boom)
    assert CH.price_history("tok") == []             # best-effort: never raises
