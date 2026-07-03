"""PLACE button parks for TG approval by default (user, 2026-07-03)."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import wca_pm_fire as fire_mod  # noqa: E402


def _rec():
    return {"id": "belgium_qf_pm", "action_label": "ADD", "stale": False,
            "venue": "polymarket", "team": "Belgium", "stage": "QF",
            "market": "advancement", "selection": "reach_QF",
            "pm_price": 0.30, "stake": 40.0, "model_prob": 0.45,
            "ev_net": 0.12}


def _resolved():
    return {"token_id": "tok123", "price": 0.31, "neg_risk": False,
            "market_question": "Will Belgium reach the QF?",
            "market_title": "Belgium QF", "event_slug": "wc-qf"}


def test_default_mode_parks_and_notifies(tmp_path, monkeypatch):
    monkeypatch.delenv("WCA_FIRE_MODE", raising=False)
    monkeypatch.setattr(fire_mod, "_resolve_live_market",
                        lambda rec, find_markets=None: _resolved())
    parked, alerts = [], []
    import wca.bot.app as app
    monkeypatch.setattr(app, "push_parked_order",
                        lambda p: (parked.append(p) or "PM-7 Belgium QF $40"))
    monkeypatch.setattr(app, "_alert_admin",
                        lambda text: (alerts.append(text) or True))

    def _never_execute(*a, **k):
        raise AssertionError("park mode must NOT execute")

    out = fire_mod.fire(rec=_rec(), rec_id="belgium_qf_pm", nonce="n1",
                        max_usd=100.0, db_path=str(tmp_path / "wca.db"),
                        execute_fn=_never_execute)
    assert out["ok"] is True and out.get("parked") is True
    assert "Y PM-7" in out["message"]
    assert parked and parked[0]["token_id"] == "tok123"
    assert parked[0]["side"] == "BUY" and parked[0]["size"] > 0
    assert alerts and "PM-7" in alerts[0]
    # idempotency: the park is recorded — a second click is refused
    out2 = fire_mod.fire(rec=_rec(), rec_id="belgium_qf_pm", nonce="n1",
                         max_usd=100.0, db_path=str(tmp_path / "wca.db"),
                         execute_fn=_never_execute)
    assert out2["ok"] is False


def test_direct_mode_still_executes(tmp_path, monkeypatch):
    monkeypatch.setenv("WCA_FIRE_MODE", "direct")
    monkeypatch.setattr(fire_mod, "_resolve_live_market",
                        lambda rec, find_markets=None: _resolved())
    calls = []

    def _execute(n, proposal, db_path, ts_utc=None, trader=None):
        calls.append(proposal)
        return "order placed id=abc123 ledger #9"

    out = fire_mod.fire(rec=_rec(), rec_id="belgium_qf_pm", nonce="n2",
                        max_usd=100.0, db_path=str(tmp_path / "wca.db"),
                        execute_fn=_execute)
    assert calls, "direct mode must execute"
    assert out["ok"] is True and not out.get("parked")
