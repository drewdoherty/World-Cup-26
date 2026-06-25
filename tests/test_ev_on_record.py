"""Model fields + site-sync on the record_bet paths.

Regression cover for the EV-on-record fix:
  * the text (CLI ``bet add``) path persists model_prob / market_prob_devig / ev;
  * ``_enrich_bets_from_card`` lifts the card's de-vigged "mkt %" into
    market_prob_devig (and leaves it NULL when the card has no such price —
    never fabricated);
  * recording a bet fires the site-sync hook (and batch callers can skip it).
"""
from __future__ import annotations

import argparse
import importlib.util
import sqlite3
from pathlib import Path

import wca.bot.app as app
import wca.bot.vision as vision
from wca.ledger import store

_CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wca_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("wca_cli_under_test", _CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _model_row(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT model_prob, market_prob_devig, ev FROM bets"
        ).fetchone()
    finally:
        con.close()


# --- (a) model fields populate on the text path -----------------------------


def test_text_path_persists_all_model_fields(tmp_path, monkeypatch):
    # Don't touch the real site feed during the test.
    monkeypatch.setattr(store, "_sync_site_after_record", lambda *a, **k: None)
    cli = _load_cli()
    db = str(tmp_path / "t.db")
    cli.cmd_bet_add(argparse.Namespace(
        ts="2026-06-24T12:00:00", match_id="M1", match_desc="A vs B",
        market="1X2", selection="A", platform="bet365", odds=2.5, stake=10.0,
        model_prob=0.45, market_prob_devig=0.40, ev=0.125, kelly=None,
        notes=None, db=db,
    ))
    row = _model_row(db)
    assert abs(row["model_prob"] - 0.45) < 1e-9
    assert abs(row["market_prob_devig"] - 0.40) < 1e-9
    assert abs(row["ev"] - 0.125) < 1e-9


# --- (b) market_prob_devig from the card's de-vigged "mkt %" price ----------


def test_enrich_from_card_populates_devig_when_present(tmp_path):
    card = tmp_path / "card.md"
    card.write_text(
        "*1. Portugal vs Uzbekistan* — Uzbekistan @ *22.00* (betfair_ex_uk)\n"
        "    model 6.5% / mkt 4.1%  edge *+43.6%*  [elo 15% dc 9%]\n",
        encoding="utf-8",
    )
    bet = vision.ExtractedBet(
        match_desc="Portugal vs Uzbekistan", market="1X2",
        selection="Uzbekistan", decimal_odds=22.0,
    )
    app._enrich_bets_from_card([bet], card_path=str(card))
    assert abs(bet.model_prob - 0.065) < 1e-9
    assert abs(bet.market_prob_devig - 0.041) < 1e-9
    assert abs(bet.ev - 0.436) < 1e-9


def test_enrich_leaves_devig_none_when_card_has_no_mkt(tmp_path):
    # model + edge present, but no "mkt %": market_prob_devig must stay None.
    card = tmp_path / "card.md"
    card.write_text(
        "*1. A vs B* — A @ *2.00* (book)\n"
        "    model 55.0%  edge *+10.0%*\n",
        encoding="utf-8",
    )
    bet = vision.ExtractedBet(
        match_desc="A vs B", market="1X2", selection="A", decimal_odds=2.0,
    )
    app._enrich_bets_from_card([bet], card_path=str(card))
    assert bet.model_prob is not None
    assert getattr(bet, "market_prob_devig", None) is None


def test_photo_flow_records_devig_from_card(tmp_path, monkeypatch):
    # End-to-end: photo enrich -> confirm -> the de-vigged price lands in the
    # ledger column, proving the photo path now carries market_prob_devig too.
    monkeypatch.setattr(store, "_sync_site_after_record", lambda *a, **k: None)
    card = tmp_path / "card.md"
    card.write_text(
        "*1. Portugal vs Uzbekistan* — Uzbekistan @ *22.00* (betfair_ex_uk)\n"
        "    model 6.5% / mkt 4.1%  edge *+43.6%*\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app, "CARD_PATH", str(card))
    parsed = [vision.ExtractedBet(
        match_desc="Portugal vs Uzbekistan", market="1X2", selection="Uzbekistan",
        decimal_odds=22.0, stake=5.0, bookmaker="bet365", confidence=0.9,
    )]
    monkeypatch.setattr(vision, "extract_bets_from_image", lambda *a, **k: parsed)
    pending = {}
    app.handle_photo(b"img", 9, pending)  # enriches in place
    db = str(tmp_path / "t.db")
    app.handle_photo_confirmation("yes", 9, db, pending, ts_utc="2026-06-24T12:00:00")
    row = _model_row(db)
    assert abs(row["model_prob"] - 0.065) < 1e-9
    assert abs(row["market_prob_devig"] - 0.041) < 1e-9


# --- (c) the site-sync fires on record --------------------------------------


def test_site_sync_fires_when_opted_in(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        store, "_sync_site_after_record",
        lambda db_path, bet_id: calls.append((db_path, bet_id)),
    )
    db = str(tmp_path / "t.db")
    bid = store.record_bet(
        "2026-06-24T12:00:00", "M1", "A vs B", "1X2", "A", "bet365", 2.0, 10.0,
        sync_site=True, db_path=db,
    )
    assert calls == [(db, bid)]


def test_site_sync_off_by_default(tmp_path, monkeypatch):
    # Default record_bet() must NOT trigger a publish: a low-level ledger write
    # never reaches out to git on its own (only opt-in callers like the CLI do).
    calls = []
    monkeypatch.setattr(
        store, "_sync_site_after_record",
        lambda *a, **k: calls.append(a),
    )
    db = str(tmp_path / "t.db")
    store.record_bet(
        "2026-06-24T12:00:00", "M1", "A vs B", "1X2", "A", "bet365", 2.0, 10.0,
        db_path=db,
    )
    assert calls == []


def test_cli_text_path_opts_into_site_sync(tmp_path, monkeypatch):
    # The manual `wca_cli bet add` path opts in, so a hand-entered bet publishes.
    calls = []
    monkeypatch.setattr(
        store, "_sync_site_after_record",
        lambda db_path, bet_id: calls.append(bet_id),
    )
    cli = _load_cli()
    db = str(tmp_path / "t.db")
    cli.cmd_bet_add(argparse.Namespace(
        ts="2026-06-24T12:00:00", match_id="M1", match_desc="A vs B",
        market="1X2", selection="A", platform="bet365", odds=2.5, stake=10.0,
        model_prob=None, market_prob_devig=None, ev=None, kelly=None,
        notes=None, db=db,
    ))
    assert calls == [1]
