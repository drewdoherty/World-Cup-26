"""Tests for the /scores bot command (handle_scores + dispatch routing)."""
from __future__ import annotations

import os

import pytest

import wca.bot.app as app
from wca import cardcache


# ---------------------------------------------------------------------------
# Synthetic card text with a scorelines section (mirrors _SYNTH_CARD in
# test_sitedata.py so the format is kept consistent).
# ---------------------------------------------------------------------------

_CARD_BODY = (
    "*World Cup Alpha — bet card* (2 picks)\n"
    "\n"
    "*1. Mexico vs South Africa* — Mexico @ *1.44* (betfair_ex_uk)\n"
    "    model 71.3% / mkt 69.3%  edge *+2.6%*  [elo 83% dc 64%]\n"
    "    stake: main 14.79\n"
    "\n"
    "*World Cup Alpha — scorelines* (2 fixtures)\n"
    "\n"
    "*Mexico vs South Africa*\n"
    "    xG: 1.47-0.89\n"
    "    1-0  16.9%  fair 5.91  back >= 6.03\n"
    "    2-0  15.5%  fair 6.45  back >= 6.57\n"
    "    2-1  10.2%  fair 9.84  back >= 10.03\n"
    "    3-0  9.1%  fair 10.94  back >= 11.16\n"
    "    1-1  8.8%  fair 11.40  back >= 11.63\n"
    "    0-0  7.6%  fair 13.09  back >= 13.36\n"
    "    O/U 2.5: over 45.8% / under 54.2%   BTTS 39.0%\n"
    "\n"
    "*South Korea vs Czech Republic*\n"
    "    xG: 1.12-1.05\n"
    "    1-1  13.8%  fair 7.24  back >= 7.39\n"
    "    1-0  13.0%  fair 7.69  back >= 7.84\n"
    "    0-0  11.5%  fair 8.66  back >= 8.83\n"
    "    0-1  10.2%  fair 9.81  back >= 10.01\n"
    "    2-1  8.4%  fair 11.87  back >= 12.11\n"
    "    2-0  7.0%  fair 14.30  back >= 14.59\n"
    "    O/U 2.5: over 38.0% / under 62.0%   BTTS 45.1%\n"
)


def _write_card(tmp_path: str, ts: str = "2026-06-11T12:00:00") -> str:
    """Write a synthetic card via cardcache.write_card and return path."""
    path = os.path.join(tmp_path, "card_latest.md")
    cardcache.write_card(_CARD_BODY, path, ts_utc=ts)
    return path


# ---------------------------------------------------------------------------
# handle_scores: missing card
# ---------------------------------------------------------------------------


def test_missing_card_returns_honest_message(tmp_path):
    path = os.path.join(str(tmp_path), "nonexistent.md")
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    assert "No card cached" in out
    assert "cron build" in out.lower() or "build" in out.lower()


# ---------------------------------------------------------------------------
# handle_scores: fixture formatting
# ---------------------------------------------------------------------------


def test_header_contains_generated_timestamp(tmp_path):
    path = _write_card(str(tmp_path), ts="2026-06-11T12:00:00")
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    assert "*Predicted scores*" in out
    assert "2026-06-11T12:00:00" in out


def test_most_likely_score_is_bolded(tmp_path):
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    # Mexico vs SA: top score is 1-0 at 16.9%; must appear bolded.
    assert "*1-0*" in out
    # South Korea vs Czech Republic: top score is 1-1; must also appear bolded.
    assert "*1-1*" in out


def test_top_score_probability_shown(tmp_path):
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    # Top prob for Mexico vs SA is 16.9%.
    assert "16.9%" in out


def test_runner_up_scores_shown_inline(tmp_path):
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    # For Mexico vs SA the next four are 2-0, 2-1, 3-0, 1-1.
    assert "2-0" in out
    assert "2-1" in out
    assert "3-0" in out
    lines = out.splitlines()
    # Fixture name is now on its own line; scorelines follow on the next non-xG line.
    mex_name_idx = next(
        i for i, l in enumerate(lines) if l.strip() == "*Mexico vs South Africa*"
    )
    # Skip the xG line (if present) to find the scoreline row.
    score_line = next(
        l for l in lines[mex_name_idx + 1:] if not l.startswith("xG")
    )
    assert "|" in score_line
    assert "2-0" in score_line
    assert "2-1" in score_line


def test_at_most_four_runners_shown(tmp_path):
    """The 5th runner-up (0-0 at 7.6%) must not appear on the scoreline row."""
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    lines = out.splitlines()
    mex_name_idx = next(
        i for i, l in enumerate(lines) if l.strip() == "*Mexico vs South Africa*"
    )
    score_line = next(
        l for l in lines[mex_name_idx + 1:] if not l.startswith("xG")
    )
    # 0-0 is the 6th scoreline — runner-up #5 — must be absent from the inline list.
    assert "0-0" not in score_line


def test_ou_and_btts_line_present(tmp_path):
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    assert "O/U 2.5" in out
    assert "BTTS" in out
    assert "45.8%" in out   # over for Mexico vs SA
    assert "39.0%" in out   # BTTS for Mexico vs SA


def test_fixture_order_preserved(tmp_path):
    """Mexico vs South Africa must come before South Korea vs Czech Republic."""
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    idx_mex = out.find("Mexico vs South Africa")
    idx_sk = out.find("South Korea vs Czech Republic")
    assert idx_mex >= 0
    assert idx_sk >= 0
    assert idx_mex < idx_sk


def test_second_fixture_scores_present(tmp_path):
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    # 1-1 top for South Korea, plus runners.
    lines = out.splitlines()
    sk_name_idx = next(
        i for i, l in enumerate(lines) if "South Korea vs Czech Republic" in l and l.startswith("*")
    )
    score_line = next(
        l for l in lines[sk_name_idx + 1:] if not l.startswith("xG")
    )
    assert "*1-1*" in score_line
    assert "1-0" in score_line


def test_xg_shown_between_fixture_name_and_scorelines(tmp_path):
    """xG line must appear after the fixture name and before the scoreline row."""
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    lines = out.splitlines()
    mex_name_idx = next(
        i for i, l in enumerate(lines) if l.strip() == "*Mexico vs South Africa*"
    )
    xg_line = lines[mex_name_idx + 1]
    assert xg_line.startswith("xG"), "xG line must follow immediately after fixture name"
    assert "1.47" in xg_line
    assert "0.89" in xg_line
    # Scoreline row must come after the xG line.
    score_line = lines[mex_name_idx + 2]
    assert "*1-0*" in score_line


def test_xg_absent_when_not_in_card(tmp_path):
    """Cards without an xG line must not show xG in the Telegram output."""
    path = os.path.join(str(tmp_path), "card.md")
    no_xg_body = (
        "*World Cup Alpha — scorelines* (1 fixtures)\n"
        "\n"
        "*A vs B*\n"
        "    1-0  20.0%  fair 5.00  back >= 5.10\n"
        "    O/U 2.5: over 50.0% / under 50.0%   BTTS 40.0%\n"
    )
    cardcache.write_card(no_xg_body, path, ts_utc="2026-06-11T12:00:00")
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    assert "xG" not in out


# ---------------------------------------------------------------------------
# handle_scores: fair % / fair decimal odds / ¼-Kelly display (rebuilt #99)
# ---------------------------------------------------------------------------


def test_scores_show_fair_decimal_on_top_and_runners(tmp_path):
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    # Top score (1-0) shows its prob AND fair decimal odds (1/p = 5.91 from card).
    assert "16.9% / fair 5.91" in out
    # A runner-up also carries its fair decimal odds.
    assert "fair 6.45" in out  # 2-0 runner-up


def test_scores_show_quarter_kelly_on_top_score(tmp_path):
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    # The most-likely scoreline shows a display-only ¼-Kelly stake.
    assert "¼-K £" in out
    # And the header explains it is display-only against the reference bankroll.
    assert "display-only" in out and "1500" in out


def test_scores_kelly_value_matches_kelly_kernel(tmp_path):
    """The displayed ¼-K stake equals wca.markets.kelly.stake for the top score."""
    from wca.markets import kelly as kelly_mod
    path = _write_card(str(tmp_path))
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    # Mexico v SA top: 1-0 at 16.9%, min back 6.03 (from the card body).
    expected = kelly_mod.stake(0.169, 6.03, app.SCORES_DISPLAY_BANKROLL)
    if expected > 0:
        assert ("¼-K £%.2f" % expected) in out


def test_scores_fair_unknown_renders_question_mark(tmp_path):
    """A scoreline without a parsed fair price degrades to 'fair ?', never crashes."""
    path = os.path.join(str(tmp_path), "card.md")
    cardcache.write_card(
        "*World Cup Alpha — scorelines* (1 fixtures)\n\n*A vs B*\n"
        "    1-0  20.0%\n"
        "    O/U 2.5: over 50.0% / under 50.0%   BTTS 40.0%\n",
        path, ts_utc="2026-06-11T12:00:00")
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    assert "fair ?" in out


# ---------------------------------------------------------------------------
# handle_scores: card with no scorelines section
# ---------------------------------------------------------------------------


def test_card_without_scorelines_section(tmp_path):
    path = os.path.join(str(tmp_path), "card.md")
    cardcache.write_card(
        "*World Cup Alpha — bet card* (0 picks)\n", path, ts_utc="2026-06-11T12:00:00"
    )
    out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
    assert "No scorelines" in out or "not" in out.lower()


# ---------------------------------------------------------------------------
# dispatch routing
# ---------------------------------------------------------------------------


def test_dispatch_scores_routes_to_handle_scores(tmp_path, monkeypatch):
    """dispatch('/scores', db) must call handle_scores (monkeypatched)."""
    calls = []

    def fake_handle_scores(card_path=app.CARD_PATH, now_utc=None):
        calls.append({"card_path": card_path, "now_utc": now_utc})
        return "scores reply"

    monkeypatch.setattr(app, "handle_scores", fake_handle_scores)
    reply = app.dispatch("/scores", "irrelevant.db")
    assert reply == "scores reply"
    assert len(calls) == 1


def test_dispatch_scores_with_real_card(tmp_path, monkeypatch):
    """dispatch('/scores', db) with a real card file returns scoreline output."""
    path = _write_card(str(tmp_path))
    # Point CARD_PATH at our temp card so handle_scores uses it by default.
    monkeypatch.setattr(app, "CARD_PATH", path)
    reply = app.dispatch("/scores", "irrelevant.db")
    assert "*Predicted scores*" in reply
    assert "*1-0*" in reply


def test_dispatch_scores_missing_card(tmp_path, monkeypatch):
    """dispatch('/scores', db) with no card returns the 'not cached' message."""
    monkeypatch.setattr(app, "CARD_PATH", os.path.join(str(tmp_path), "no_card.md"))
    reply = app.dispatch("/scores", "irrelevant.db")
    assert "No card cached" in reply


# ---------------------------------------------------------------------------
# HELP_TEXT includes /scores entry
# ---------------------------------------------------------------------------


def test_help_text_mentions_scores():
    assert "/scores" in app.HELP_TEXT
    assert "scoreline" in app.HELP_TEXT.lower()
