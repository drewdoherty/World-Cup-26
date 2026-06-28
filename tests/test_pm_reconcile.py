"""Tests for the Polymarket ledger-vs-account reconciler (pure logic).

No network / DB: exercises the normalisation, the cross-form key match between a
Data-API title and a ledger selection, and the build_plan classification
(redeemable->won, malformed->void, else->lost; live-not-in-ledger->insert).
"""

from __future__ import annotations

import importlib.util
import os

import pytest

# Load the script module directly (scripts/ is not a package).
_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_pm_reconcile.py")
_spec = importlib.util.spec_from_file_location("wca_pm_reconcile", _PATH)
rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rec)


def test_norm_type():
    assert rec.norm_type("Will France reach the Round of 16 ...") == "r16"
    assert rec.norm_type("Will Argentina reach the Quarterfinals ...") == "qf"
    assert rec.norm_type("Will France reach the Semifinals ...") == "sf"
    assert rec.norm_type("Will Iran advance to the knockout stages ...") == "advance"
    assert rec.norm_type("Will Brazil win the 2026 FIFA World Cup?") == "win_wc"
    assert rec.norm_type("Will Ghana be eliminated in the Round of 32 ...") == "elim"
    assert rec.norm_type("Will Colombia vs. Portugal end in a draw?") == "draw"
    assert rec.norm_type("Will England win on 2026-06-27?") == "match_win"
    assert rec.norm_type("pm_moneyline Yes") == "match_win"


def test_norm_subject_multiword_and_forms():
    # API-title form vs ledger-selection form must collapse to the same subject.
    assert rec.norm_subject("Will New Zealand advance to the knockout stages at ...") == "new zealand"
    assert rec.norm_subject("New Zealand advance to the knockout stages - Yes") == "new zealand"
    assert rec.norm_subject("Will USA reach the Round of 16 ...") == "usa"
    assert rec.norm_subject("Will United States reach the Round of 16 ...") == "usa"  # alias
    assert rec.norm_subject("Will DR Congo win on 2026-06-27?") == "dr congo"


def test_is_malformed():
    assert rec.is_malformed("Yes") is True
    assert rec.is_malformed("No") is True
    assert rec.is_malformed("") is True
    assert rec.is_malformed("Croatia win on 2026-06-27 - Yes") is False


def test_key_match_across_api_and_ledger_forms():
    api = rec.key_of("Will New Zealand advance to the knockout stages at the 2026 FIFA World Cup?",
                     rec.norm_type("Will New Zealand advance to the knockout stages ..."), "Yes")
    led = rec.key_of("New Zealand advance to the knockout stages - Yes",
                     rec.norm_type("pm_advancement New Zealand advance to the knockout stages - Yes"), "yes")
    assert api == led == "new zealand|advance|yes"


def _live(title, outcome, size, price, wallet="PM1"):
    return {"wallet": wallet, "title": title, "outcome": outcome,
            "type": rec.norm_type(title), "shares": size, "price": price,
            "value": round(size * price, 2), "token_id": "tok"}


def test_build_plan_classifies_inserts_and_closes():
    # Live open: USA R16 (missing from ledger -> insert).
    live_open = {rec.key_of("Will USA reach the Round of 16 ...", "r16", "Yes"):
                 _live("Will USA reach the Round of 16 ...", "Yes", 100.0, 0.8)}
    # Redeemable (won): New Zealand advance.
    redeem = {rec.key_of("Will New Zealand advance to the knockout stages ...", "advance", "Yes")}
    # Ledger open: NZ advance (won), Cape Verde advance (lost), malformed 'Yes' (void).
    ledger_open = {
        rec.key_of("New Zealand advance to the knockout stages - Yes", "advance", "yes"):
            {"id": 177, "market": "pm_advancement", "sel": "New Zealand advance to the knockout stages - Yes",
             "odds": 3.125, "stake": 17.27, "key": "x"},
        rec.key_of("Cape Verde advance to the knockout stages - Yes", "advance", "yes"):
            {"id": 173, "market": "pm_advancement", "sel": "Cape Verde advance to the knockout stages - Yes",
             "odds": 2.3815, "stake": 39.13, "key": "y"},
        rec.key_of("Yes", "match_win", "yes"):
            {"id": 197, "market": "pm_moneyline", "sel": "Yes", "odds": 1.81, "stake": 25.85, "key": "z"},
    }
    plan = rec.build_plan(live_open, redeem, ledger_open)
    assert len(plan["inserts"]) == 1 and plan["inserts"][0]["title"].startswith("Will USA")
    by_id = {c["id"]: c for c in plan["closes"]}
    assert by_id[177]["action"] == "won" and by_id[177]["settled_pl"] == pytest.approx(17.27 * (3.125 - 1), abs=0.01)
    assert by_id[173]["action"] == "lost" and by_id[173]["settled_pl"] == pytest.approx(-39.13)
    assert by_id[197]["action"] == "void" and by_id[197]["settled_pl"] == 0.0
