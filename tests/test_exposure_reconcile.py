"""Tests for ``scripts/wca_exposure_reconcile.py`` — the cross-feed CLI wrapper
around ``wca.tie_exposure``. See tests/test_tie_exposure.py for the matching
logic itself; these pin the file-level read/mutate/rewrite behaviour.
"""
from __future__ import annotations

import importlib.util
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "wca_exposure_reconcile", os.path.join(_ROOT, "scripts", "wca_exposure_reconcile.py"))
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
reconcile = _mod.reconcile


def _bet_recs(advancement_futures):
    return {"meta": {}, "advancement_futures": advancement_futures, "withheld": []}


def _event_recs(recs):
    return {"meta": {}, "recs": recs}


def test_reconcile_zeroes_the_weaker_advancement_futures_leg():
    bet_recs = _bet_recs([
        {"id": "england_sf_pm", "team": "England", "stage": "SF",
         "stake": 136.39, "ev_net": 0.0505},
    ])
    event_recs = _event_recs([
        {"fixture": "Norway vs England", "label": "England to advance",
         "team": "England", "tie_stage": "SF", "stake_usd": 96.61, "ev": 0.102169,
         "family": "advance"},
    ])

    n = reconcile(bet_recs, event_recs)

    assert n == 1
    # advancement_futures had the worse edge (0.0505 < 0.102169) -> it loses,
    # gets moved to withheld with stake zeroed, and the event_market leg
    # (better edge) is untouched.
    assert bet_recs["advancement_futures"] == []
    assert len(bet_recs["withheld"]) == 1
    assert bet_recs["withheld"][0]["stake"] == 0.0
    assert bet_recs["withheld"][0]["reason_code"] == "dup_tie_exposure"
    assert event_recs["recs"][0]["stake_usd"] == 96.61
    assert event_recs["recs"][0].get("dimmed") is not True


def test_reconcile_zeroes_the_weaker_event_market_leg():
    bet_recs = _bet_recs([
        {"id": "england_sf_pm", "team": "England", "stage": "SF",
         "stake": 136.39, "ev_net": 0.20},
    ])
    event_recs = _event_recs([
        {"fixture": "Norway vs England", "label": "England to advance",
         "team": "England", "tie_stage": "SF", "stake_usd": 96.61, "ev": 0.05,
         "family": "advance"},
    ])

    n = reconcile(bet_recs, event_recs)

    assert n == 1
    assert bet_recs["advancement_futures"][0]["stake"] == 136.39
    assert bet_recs["withheld"] == []
    assert event_recs["recs"][0]["stake_usd"] == 0.0
    assert event_recs["recs"][0]["dimmed"] is True
    assert "dup_tie_exposure" in event_recs["recs"][0]["no_cash_reason"]


def test_reconcile_no_op_when_no_overlap():
    bet_recs = _bet_recs([
        {"id": "norway_final_pm", "team": "Norway", "stage": "Final",
         "stake": 50.0, "ev_net": 0.05},
    ])
    event_recs = _event_recs([
        {"fixture": "Norway vs England", "label": "England to advance",
         "team": "England", "tie_stage": "SF", "stake_usd": 96.61, "ev": 0.10,
         "family": "advance"},
    ])

    n = reconcile(bet_recs, event_recs)

    assert n == 0
    assert bet_recs["advancement_futures"][0]["stake"] == 50.0
    assert event_recs["recs"][0]["stake_usd"] == 96.61


def test_reconcile_ignores_non_advance_family_rows():
    bet_recs = _bet_recs([
        {"id": "england_sf_pm", "team": "England", "stage": "SF",
         "stake": 136.39, "ev_net": 0.05},
    ])
    event_recs = _event_recs([
        {"fixture": "Norway vs England", "label": "Over 2.5",
         "team": "England", "tie_stage": "SF", "stake_usd": 50.0, "ev": 0.10,
         "family": "total_goals"},
    ])

    n = reconcile(bet_recs, event_recs)

    assert n == 0
    assert bet_recs["advancement_futures"][0]["stake"] == 136.39
