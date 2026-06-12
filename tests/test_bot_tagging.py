"""Tests for account/source tagging in screenshot ingestion + /bets rendering.

Covers:
  * caption tag parsing matrix (resolve_tags)
  * defaults: untagged screenshot -> account 1 / source punt
  * confirmation prompt echoes the resolved tags
  * yes-reply tag overrides (``yes a2 offer``)
  * record_bet receives account/source (monkeypatched + real DB)
  * /bets renders the compact source tag (m/o/p) + A2 marker
"""
from __future__ import annotations

import sqlite3

import pytest

import wca.bot.app as app
import wca.bot.vision as vision
from wca.ledger.store import record_bet


def _bet(match="A vs B", sel="A", **kw):
    return vision.ExtractedBet(
        match_desc=match, market=kw.get("market", "h2h"), selection=sel,
        bookmaker=kw.get("bookmaker", "bet365"),
        decimal_odds=kw.get("decimal_odds", 2.0), stake=kw.get("stake", 10.0),
        is_boost=kw.get("is_boost", False), confidence=kw.get("confidence", 0.9),
    )


# ---------------------------------------------------------------------------
# resolve_tags matrix.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "caption, account, source",
    [
        (None, "1", "punt"),                       # default for screenshots
        ("", "1", "punt"),
        ("account 2", "2", "punt"),
        ("acc2", "2", "punt"),
        ("a2", "2", "punt"),
        ("A2 offer", "2", "offer"),
        ("offer", "1", "offer"),
        ("punt", "1", "punt"),
        ("model", "1", "model"),
        ("a2 model", "2", "model"),
        ("account 1 offer", "1", "offer"),
        ("a1", "1", "punt"),
        ("Canada free bet offer a2", "2", "offer"),
    ],
)
def test_resolve_tags_matrix(caption, account, source):
    tags = app.resolve_tags(caption)
    assert tags == {"account": account, "source": source}


def test_resolve_tags_respects_supplied_defaults():
    # A bare yes-reply carries no tag tokens -> falls back to the parked tags.
    assert app.resolve_tags("yes", default_account="2", default_source="offer") == {
        "account": "2", "source": "offer"
    }


@pytest.mark.parametrize(
    "reply, account, source",
    [
        ("yes", "1", "punt"),
        ("yes 2", "2", "punt"),
        ("yes offer", "1", "offer"),
        ("yes 2 punt", "2", "punt"),
        ("yes punt 2", "2", "punt"),
        ("yes 1", "1", "punt"),
        ("yes model 2", "2", "model"),
    ],
)
def test_resolve_tags_bare_account_in_yes_reply(reply, account, source):
    # The yes-reply path opts into bare-digit account tokens.
    tags = app.resolve_tags(reply, allow_bare_account=True)
    assert tags == {"account": account, "source": source}


def test_bare_digit_not_matched_in_captions():
    # Captions do NOT opt in, so a stray stake digit never flips the account.
    assert app.resolve_tags("stake 2 units offer")["account"] == "1"


def test_resolve_tags_does_not_falsematch_words():
    # 'a2'/'acc2' must be word-bounded; embedded substrings should not trip.
    assert app.resolve_tags("data2base")["account"] == "1"
    assert app.resolve_tags("modeller")["source"] == "punt"


# ---------------------------------------------------------------------------
# handle_photo echoes resolved tags / parks them.
# ---------------------------------------------------------------------------

def test_handle_photo_echoes_default_tags(monkeypatch):
    monkeypatch.setattr(vision, "extract_bets_from_image", lambda *a, **k: [_bet()])
    pending, ptags = {}, {}
    msg = app.handle_photo(b"img", 1, pending, caption=None, pending_tags=ptags)
    assert "account *1*" in msg and "source *punt*" in msg
    assert ptags[1] == {"account": "1", "source": "punt"}


def test_handle_photo_echoes_caption_tags(monkeypatch):
    monkeypatch.setattr(vision, "extract_bets_from_image", lambda *a, **k: [_bet()])
    pending, ptags = {}, {}
    msg = app.handle_photo(b"img", 1, pending, caption="a2 offer", pending_tags=ptags)
    assert "account *2*" in msg and "source *offer*" in msg
    assert ptags[1] == {"account": "2", "source": "offer"}


# ---------------------------------------------------------------------------
# Confirmation passes tags to record_bet.
# ---------------------------------------------------------------------------

def test_confirmation_uses_parked_caption_tags(tmp_path):
    db = str(tmp_path / "t.db")
    pending = {7: [_bet("USA vs Paraguay", "Paraguay")]}
    ptags = {7: {"account": "2", "source": "offer"}}
    out = app.handle_photo_confirmation("yes", 7, db, pending, pending_tags=ptags,
                                        ts_utc="2026-06-11T18:00:00")
    assert "offer" in out and "A2" in out
    con = sqlite3.connect(db)
    row = con.execute("select account, source from bets").fetchone()
    con.close()
    assert row == ("2", "offer")


def test_confirmation_default_tags_when_untagged(tmp_path):
    db = str(tmp_path / "t.db")
    pending = {7: [_bet()]}
    # No pending_tags entry -> screenshot default account 1 / punt.
    out = app.handle_photo_confirmation("yes", 7, db, pending, pending_tags={},
                                        ts_utc="2026-06-11T18:00:00")
    assert "punt" in out and "A2" not in out
    con = sqlite3.connect(db)
    row = con.execute("select account, source from bets").fetchone()
    con.close()
    assert row == ("1", "punt")


def test_confirmation_reply_tags_override_parked(tmp_path):
    db = str(tmp_path / "t.db")
    pending = {7: [_bet()]}
    ptags = {7: {"account": "1", "source": "punt"}}
    out = app.handle_photo_confirmation("yes a2 offer", 7, db, pending,
                                        pending_tags=ptags, ts_utc="2026-06-11T18:00:00")
    assert "offer" in out and "A2" in out
    con = sqlite3.connect(db)
    row = con.execute("select account, source from bets").fetchone()
    con.close()
    assert row == ("2", "offer")


def test_confirmation_reply_partial_override_keeps_other_dim(tmp_path):
    db = str(tmp_path / "t.db")
    pending = {7: [_bet()]}
    ptags = {7: {"account": "2", "source": "offer"}}
    # Reply changes only source; account stays from the parked caption.
    out = app.handle_photo_confirmation("yes model", 7, db, pending,
                                        pending_tags=ptags, ts_utc="2026-06-11T18:00:00")
    con = sqlite3.connect(db)
    row = con.execute("select account, source from bets").fetchone()
    con.close()
    assert row == ("2", "model")


def test_confirmation_record_bet_kwargs_monkeypatched(monkeypatch):
    seen = {}

    def fake_record_bet(*a, **k):
        seen["account"] = k.get("account")
        seen["source"] = k.get("source")
        return 99

    monkeypatch.setattr(app, "record_bet", fake_record_bet)
    monkeypatch.setattr(app, "_autosync", lambda *a, **k: None)
    pending = {7: [_bet()]}
    ptags = {7: {"account": "2", "source": "offer"}}
    app.handle_photo_confirmation("yes", 7, "x.db", pending, pending_tags=ptags,
                                  ts_utc="2026-06-11T18:00:00")
    assert seen == {"account": "2", "source": "offer"}


def test_confirmation_bare_digit_account_override(tmp_path):
    db = str(tmp_path / "t.db")
    pending = {7: [_bet()]}
    ptags = {7: {"account": "1", "source": "punt"}}
    out = app.handle_photo_confirmation("yes 2 offer", 7, db, pending,
                                        pending_tags=ptags, ts_utc="2026-06-11T18:00:00")
    assert "offer" in out and "A2" in out
    con = sqlite3.connect(db)
    row = con.execute("select account, source from bets").fetchone()
    con.close()
    assert row == ("2", "offer")


@pytest.mark.parametrize("reply", ["yes", "yes 2", "yes 2 punt", "yes punt 2", "no offer"])
def test_yes_syntax_is_money_action(reply):
    # Tagged yes/no replies stay admin-gated.
    assert app._is_money_action(reply)


def test_confirmation_no_discards_and_clears_tags():
    pending = {7: [_bet()]}
    ptags = {7: {"account": "2", "source": "offer"}}
    out = app.handle_photo_confirmation("no", 7, "x.db", pending, pending_tags=ptags)
    assert "Discarded 1" in out and 7 not in pending and 7 not in ptags


# ---------------------------------------------------------------------------
# /bets renders the compact tags.
# ---------------------------------------------------------------------------

def test_bets_renders_source_and_a2_tags(tmp_path):
    db = str(tmp_path / "t.db")
    record_bet("2026-06-11T10:00:00", "M1", "Canada vs Bosnia", "h2h", "Canada",
               "betfair_sportsbook", 1.88, 10.0, account="2", source="punt", db_path=db)
    record_bet("2026-06-11T10:00:00", "M2", "USA vs Wales", "h2h", "USA",
               "bet365", 2.2, 10.0, account="1", source="offer", db_path=db)
    record_bet("2026-06-11T10:00:00", "M3", "Spain vs Japan", "h2h", "Spain",
               "bet365", 1.9, 10.0, account="1", source="model", db_path=db)
    out = app.handle_bets(db)
    # Account-2 punt bet shows the 'p' source initial and the A2 marker.
    assert "A2" in out
    lines = out.splitlines()
    canada = next(l for l in lines if "Canada" in l)
    assert canada.lstrip().startswith("#1p A2")
    usa = next(l for l in lines if "USA v Wales" in l)
    assert usa.lstrip().startswith("#2o ") and "A2" not in usa
    spain = next(l for l in lines if "Spain" in l)
    assert spain.lstrip().startswith("#3m ")


def test_bets_handles_legacy_db_without_columns(tmp_path):
    """A pre-migration bets table (no account/source) must still render."""
    db = str(tmp_path / "legacy.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE bets (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT, "
        "match_id TEXT, match_desc TEXT, market TEXT, selection TEXT, platform TEXT, "
        "decimal_odds REAL, stake REAL, status TEXT, notes TEXT)"
    )
    con.execute(
        "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, platform, "
        "decimal_odds, stake, status, notes) VALUES "
        "('t','M','Brazil vs Chile','h2h','Brazil','bet365',1.5,10,'open','')"
    )
    con.commit()
    con.close()
    out = app.handle_bets(db)
    # Defaults to model ('m'), account 1 (no A2).
    brazil = next(l for l in out.splitlines() if "Brazil" in l)
    assert brazil.lstrip().startswith("#1m ") and "A2" not in brazil
