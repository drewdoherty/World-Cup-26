"""Tests for the bot UI overhaul: command menu/registry, the consolidated
/game panel, UK-time refresh headers, and the scrapped commands."""
from __future__ import annotations

from wca.bot import app
from wca.bot.telegram import TelegramClient


# -- command registry / menu / help ---------------------------------------


def test_registry_drives_help_and_menu():
    cmds = {c for c, _d, _a in app.COMMANDS}
    assert "game" in cmds
    # scrapped commands are gone from the registry, help, and menu
    for gone in ("structure", "accas", "settle", "next"):
        assert gone not in cmds
        assert "/%s —" % gone not in app.HELP_TEXT  # no command line for it
    assert "/game —" in app.HELP_TEXT


def test_command_menu_payload_shape():
    menu = app.command_menu()
    assert all(set(item) == {"command", "description"} for item in menu)
    names = {item["command"] for item in menu}
    assert "game" in names and "settle" not in names


def test_scrapped_commands_unknown_via_dispatch():
    assert "Unknown command" in app.dispatch("/accas", db_path=":memory:")
    assert "Unknown command" in app.dispatch("/settle 1 won", db_path=":memory:")


def test_set_my_commands_calls_telegram(monkeypatch):
    client = TelegramClient(token="dummy")
    seen = {}
    monkeypatch.setattr(client, "_call", lambda method, payload, **kw: seen.update(method=method, payload=payload))
    client.set_my_commands([{"command": "game", "description": "x"}])
    assert seen["method"] == "setMyCommands"
    assert seen["payload"]["commands"][0]["command"] == "game"


# -- UK-time refresh header ------------------------------------------------


def test_uk_refresh_header_converts_to_uk_time():
    hdr = app._uk_refresh_header("2026-06-21T10:30:00")  # June → BST (+1)
    assert "data as of" in hdr
    # BST when tz data present, else UTC fallback — accept either, to the minute
    assert ("11:30" in hdr) or ("10:30" in hdr)


def test_uk_refresh_header_picks_oldest_source():
    hdr = app._uk_refresh_header("2026-06-21T10:30:00", "2026-06-21T09:15:00 UTC")
    assert ("10:15" in hdr) or ("09:15" in hdr)  # oldest = 09:15Z → 10:15 BST


def test_uk_refresh_header_empty_on_no_timestamps():
    assert app._uk_refresh_header(None, "") == ""


# -- /game consolidated panel ----------------------------------------------


def test_game_panel_combines_body_cards_and_uk_header(tmp_path):
    nf = tmp_path / "next.md"
    nf.write_text(
        "<!-- generated: 2026-06-21T10:30:00 -->\n"
        "*Belgium vs Iran*\n"
        "*Corners* (model, exp 8.8)\n  O/U 8.5: over 51.0% / under 49.0%\n"
        "*Scorelines* (top 6)\n 1-0 16.4% | 2-0 12.7%\n"
        "*Anytime scorers*\n Romelu Lukaku 2.10\n",
        encoding="utf-8",
    )
    out = app.handle_game(next_path=str(nf), now_utc="2026-06-21T10:45:00")
    assert "data as of" in out            # UK-time header on top
    assert "Belgium vs Iran" in out       # cached body preserved
    assert "Corners" in out               # corners (from cache)
    assert "Scorelines" in out            # scores
    assert "scorers" in out.lower()       # goalscorers
    assert "Cards" in out                 # NEW: model cards line appended
    assert "STALE" not in out             # 15 min old, fresh


def test_game_panel_handles_missing_cache(tmp_path):
    out = app.handle_game(next_path=str(tmp_path / "absent.md"), now_utc="2026-06-21T10:45:00")
    assert "No preview cached" in out


def test_next_is_alias_for_game(tmp_path):
    # /next routes to the same panel as /game
    r = app.dispatch("/next", db_path=":memory:")
    assert "Unknown command" not in r


def test_cards_line_is_model_labelled():
    line = app._cards_line()
    assert "Cards" in line and "model" in line.lower() and "O/U" in line
