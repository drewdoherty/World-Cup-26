"""Tests for the Risk & Blind Spots dashboard metrics (F5).

Guards that ``wca.exposure_dashboard.compute_dashboard_metrics``:
* never ships the old hardcoded p_profit/p_win_50 constants,
* derives win probabilities from the honest model-conditional scenario engine,
* marks them unavailable (None) when the model can't score the open bets,
* keeps best/worst-case currency-coherent (GBP vs USD never summed).
"""
from __future__ import annotations

import json

import pytest

from wca import exposure_dashboard as ed
from wca.ledger import store


# A two-fixture model slate with round probabilities (home-favourite both).
MODEL_FIXTURES = [
    {"fixture": "Ateam vs Bteam", "kickoff": "2099-12-31 19:00:00+00:00",
     "model": {"home": 0.60, "draw": 0.25, "away": 0.15}},
    {"fixture": "Cteam vs Dteam", "kickoff": "2099-12-31 22:00:00+00:00",
     "model": {"home": 0.55, "draw": 0.25, "away": 0.20}},
]


def _write_preds(tmp_path, fixtures):
    p = tmp_path / "preds.json"
    p.write_text(json.dumps({"fixtures": fixtures}), encoding="utf-8")
    return str(p)


def _db_with_bets(tmp_path, bets):
    db = str(tmp_path / "ledger.db")
    store.init_db(db)
    conn = store._connect(db)
    for b in bets:
        conn.execute(
            "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
            "platform, decimal_odds, stake, model_prob, ev, status, source, account) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-06-13T10:00:00+00:00",
                b.get("match_id", "m1"),
                b.get("match_desc", ""),
                b.get("market", "Full-time result"),
                b.get("selection", ""),
                b.get("platform", "betfair_sportsbook"),
                float(b.get("decimal_odds", 2.0)),
                float(b.get("stake", 10.0)),
                b.get("model_prob"),
                b.get("ev"),
                "open",
                b.get("source", "model"),
                b.get("account", "1"),
            ),
        )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# No fabricated constants
# ---------------------------------------------------------------------------

def test_no_hardcoded_pprofit_constants():
    """The old 0.6/0.4/0.5 and 0.3/0.1 constants are gone from the source."""
    src = (ed.__file__)
    text = open(src, encoding="utf-8").read()
    assert "0.6 if total_ev > 0 else 0.4" not in text
    assert "0.3 if best_case > 50 else 0.1" not in text


# ---------------------------------------------------------------------------
# Honest win probabilities
# ---------------------------------------------------------------------------

def test_pprofit_derived_from_scenarios_when_modelable(tmp_path):
    """A 1X2 single on the slate yields a real, scenario-derived p_profit."""
    preds = _write_preds(tmp_path, MODEL_FIXTURES)
    # Back the away side (p .15) @ 6.0 on a GBP book.
    db = _db_with_bets(tmp_path, [
        {"match_desc": "Ateam vs Bteam", "selection": "Bteam",
         "market": "Full-time result", "decimal_odds": 6.0, "stake": 10.0},
    ])
    res = ed.compute_dashboard_metrics(db, preds_path=preds, now_utc="2026-06-13 10:00:00 UTC")
    m = res["metrics"]
    assert m["p_metrics_available"] is True
    # away wins with p .15 -> P(profit) ~ 0.15 (single +EV state)
    assert m["p_profit"] == pytest.approx(0.15, abs=0.02)
    assert m["p_loss"] is not None and m["p_loss"] > 0
    # not the old fabricated 0.6/0.4 constant
    assert m["p_profit"] not in (0.6, 0.4, 0.5)


def test_pmetrics_unavailable_for_offslate_only(tmp_path):
    """All-outright/futures book → win-probs are None (not a fabricated 0)."""
    preds = _write_preds(tmp_path, MODEL_FIXTURES)
    db = _db_with_bets(tmp_path, [
        {"match_desc": "Golden Boot", "selection": "Harry Kane",
         "market": "outright_golden_boot", "decimal_odds": 7.5, "stake": 10.0},
    ])
    res = ed.compute_dashboard_metrics(db, preds_path=preds, now_utc="2026-06-13 10:00:00 UTC")
    m = res["metrics"]
    assert m["p_metrics_available"] is False
    assert m["p_profit"] is None
    assert m["p_loss"] is None
    assert m["p_win_50"] is None


def test_pmetrics_unavailable_without_model_slate(tmp_path):
    """No model predictions file → win-probs unavailable, never fabricated."""
    db = _db_with_bets(tmp_path, [
        {"match_desc": "Ateam vs Bteam", "selection": "Bteam",
         "market": "Full-time result", "decimal_odds": 2.0, "stake": 10.0},
    ])
    res = ed.compute_dashboard_metrics(db, preds_path=str(tmp_path / "missing.json"),
                                       now_utc="2026-06-13 10:00:00 UTC")
    m = res["metrics"]
    assert m["p_metrics_available"] is False
    assert m["p_profit"] is None


# ---------------------------------------------------------------------------
# Currency coherence
# ---------------------------------------------------------------------------

def test_best_worst_split_by_currency(tmp_path):
    """GBP and USD legs never get summed into one number."""
    preds = _write_preds(tmp_path, MODEL_FIXTURES)
    db = _db_with_bets(tmp_path, [
        # GBP favourite (prob .60) @2.0 stake 10 -> best +10 GBP
        {"match_desc": "Ateam vs Bteam", "selection": "Ateam",
         "platform": "betfair_sportsbook", "decimal_odds": 2.0, "stake": 10.0,
         "model_prob": 0.60},
        # USD favourite (prob .70) @1.6667 stake 60 -> best +40 USD
        {"match_desc": "Japan R16", "selection": "Japan reach R16 - No",
         "platform": "polymarket", "market": "advancement", "decimal_odds": 1.6667,
         "stake": 60.0, "model_prob": 0.70},
    ])
    res = ed.compute_dashboard_metrics(db, preds_path=preds, now_utc="2026-06-13 10:00:00 UTC")
    m = res["metrics"]
    by = m["by_currency"]
    assert by["GBP"]["best_case"] == pytest.approx(10.0, abs=0.01)
    assert by["USD"]["best_case"] == pytest.approx(40.0, abs=0.01)
    # Legacy single keys carry the GBP book only; USD exposed separately.
    assert m["best_case"] == by["GBP"]["best_case"]
    assert m["best_case_usd"] == by["USD"]["best_case"]
    # The two are NOT summed (would be 50.0 if mixed).
    assert m["best_case"] != pytest.approx(50.0, abs=0.01)


def test_free_bet_loss_is_zero_in_worst_case(tmp_path):
    """A promo/free bet contributes £0 to worst_case, not its stake."""
    preds = _write_preds(tmp_path, MODEL_FIXTURES)
    db = _db_with_bets(tmp_path, [
        {"match_desc": "Ateam vs Bteam", "selection": "Bteam",
         "platform": "betfred", "decimal_odds": 3.0, "stake": 10.0,
         "model_prob": 0.15, "source": "offer"},  # underdog free bet
    ])
    res = ed.compute_dashboard_metrics(db, preds_path=preds, now_utc="2026-06-13 10:00:00 UTC")
    assert res["metrics"]["by_currency"]["GBP"]["worst_case"] == 0.0


def test_empty_ledger_returns_zeros(tmp_path):
    preds = _write_preds(tmp_path, MODEL_FIXTURES)
    db = str(tmp_path / "empty.db")
    store.init_db(db)
    res = ed.compute_dashboard_metrics(db, preds_path=preds, now_utc="2026-06-13 10:00:00 UTC")
    assert res["n_open_bets"] == 0
    assert res["metrics"]["p_profit"] is None
    assert res["metrics"]["best_case"] == 0.0
