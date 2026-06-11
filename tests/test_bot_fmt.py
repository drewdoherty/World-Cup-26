"""Tests for improved Telegram formatting in handle_summary, handle_bets, handle_scores.

Checks:
- Emoji title lines are present.
- Triple-backtick code-block fences are present.
- All legacy substrings from test_bot_bets.py and test_bot_scores.py still hold.
"""
from __future__ import annotations

import os

import pytest

import wca.bot.app as app
from wca import cardcache
from wca.ledger.store import add_bankroll_event, record_bet


# ---------------------------------------------------------------------------
# Shared seed helpers (mirrors test_bot_bets._seed exactly).
# ---------------------------------------------------------------------------


def _seed(db: str) -> None:
    record_bet(
        "2026-06-11T10:00:00", "M1", "USA vs Paraguay", "h2h", "Paraguay",
        "virginbet", 4.2, 5.68, db_path=db,
    )
    record_bet(
        "2026-06-11T10:00:00", "M2", "Treble", "acca", "3 legs",
        "betfair_sportsbook", 20.41, 2.0, notes="FREE bet SNR", db_path=db,
    )
    record_bet(
        "2026-06-11T10:00:00", "M3", "Mexico vs South Africa", "pm_moneyline",
        "Mexico Yes", "polymarket", 1.449, 22.0, notes="currency=USD", db_path=db,
    )
    add_bankroll_event(
        "2026-06-11T09:00:00", 1310.0,
        "deposit pool=polymarket currency=USD", db_path=db,
    )
    add_bankroll_event(
        "2026-06-11T09:00:00", 1000.0,
        "notional pool=sportsbook currency=GBP", db_path=db,
    )


# Synthetic card body (same as test_bot_scores._CARD_BODY).
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
    "    1-0  16.9%  fair 5.91  back >= 6.03\n"
    "    2-0  15.5%  fair 6.45  back >= 6.57\n"
    "    2-1  10.2%  fair 9.84  back >= 10.03\n"
    "    3-0  9.1%  fair 10.94  back >= 11.16\n"
    "    1-1  8.8%  fair 11.40  back >= 11.63\n"
    "    0-0  7.6%  fair 13.09  back >= 13.36\n"
    "    O/U 2.5: over 45.8% / under 54.2%   BTTS 39.0%\n"
    "\n"
    "*South Korea vs Czech Republic*\n"
    "    1-1  13.8%  fair 7.24  back >= 7.39\n"
    "    1-0  13.0%  fair 7.69  back >= 7.84\n"
    "    0-0  11.5%  fair 8.66  back >= 8.83\n"
    "    0-1  10.2%  fair 9.81  back >= 10.01\n"
    "    2-1  8.4%  fair 11.87  back >= 12.11\n"
    "    2-0  7.0%  fair 14.30  back >= 14.59\n"
    "    O/U 2.5: over 38.0% / under 62.0%   BTTS 45.1%\n"
)


def _write_card(tmp_path: str, ts: str = "2026-06-11T12:00:00") -> str:
    path = os.path.join(tmp_path, "card_latest.md")
    cardcache.write_card(_CARD_BODY, path, ts_utc=ts)
    return path


# ---------------------------------------------------------------------------
# /summary formatting
# ---------------------------------------------------------------------------


class TestSummaryFormat:
    def test_emoji_title_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_summary(db)
        # The unicode money bag emoji must appear in the title line.
        assert "\U0001f4b0" in out

    def test_title_includes_portfolio_text(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_summary(db)
        assert "World Cup Alpha" in out
        assert "portfolio" in out.lower()

    def test_code_block_fence_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_summary(db)
        assert "```" in out

    def test_code_block_table_headers_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_summary(db)
        assert "POOL" in out
        assert "BANK" in out
        assert "AT RISK" in out
        assert "P&L" in out

    # Legacy substrings from test_bot_bets.py must still hold.

    def test_legacy_at_risk_open_label(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_summary(db)
        assert "At risk (open):" in out

    def test_legacy_sportsbook_pool_line(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_summary(db)
        assert "sportsbook: £1000.00" in out

    def test_legacy_polymarket_pool_line(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_summary(db)
        assert "polymarket: $1310.00" in out

    def test_legacy_at_risk_amount(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_summary(db)
        assert "at risk $22.00" in out


# ---------------------------------------------------------------------------
# /bets formatting
# ---------------------------------------------------------------------------


class TestBetsFormat:
    def test_emoji_title_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "\U0001f3af" in out

    def test_title_includes_open_bets_and_count(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "Open bets" in out
        assert "(3)" in out

    def test_code_block_fences_present(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "```" in out

    def test_code_block_table_header_row(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        # Table header columns.
        assert "#" in out          # per-bet id prefix
        assert "\u2192" in out      # stake->win arrow rows
        # match names render on the id line (no fixed header row anymore)
        assert "USA v Paraguay" in out
        # selection + odds + stake all live on the indented second line
        assert "Paraguay      4.20" in out

    # Legacy substrings from test_bot_bets.py must still hold verbatim.

    def test_legacy_venue_headers(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "SPORTSBOOK" in out and "POLYMARKET" in out

    def test_legacy_paraguay_win(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "£18.18" in out

    def test_legacy_free_marker(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "(free)" in out

    def test_legacy_sportsbook_max_win_loss(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "max win £57.00 / max loss £5.68" in out

    def test_legacy_polymarket_max_win_loss(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "max win $9.88 / max loss $22.00" in out

    def test_legacy_total_line(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed(db)
        out = app.handle_bets(db)
        assert "TOTAL" in out


# ---------------------------------------------------------------------------
# /scores formatting
# ---------------------------------------------------------------------------


class TestScoresFormat:
    def test_emoji_title_present(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        assert "⚽" in out  # soccer ball emoji

    def test_title_includes_predicted_scores(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        assert "*Predicted scores*" in out

    def test_title_includes_timestamp(self, tmp_path):
        path = _write_card(str(tmp_path), ts="2026-06-11T12:00:00")
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        assert "2026-06-11T12:00:00" in out

    def test_blank_line_between_fixtures(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        lines = out.splitlines()
        # Find the Mexico fixture line and verify a blank line exists between
        # its O/U line and the South Korea fixture line.
        mex_idx = next(i for i, l in enumerate(lines) if "Mexico vs South Africa" in l and ":" in l)
        sk_idx = next(i for i, l in enumerate(lines) if "South Korea" in l and ":" in l)
        between = lines[mex_idx + 1: sk_idx]
        assert "" in between, "expected at least one blank line between fixtures"

    # Legacy substrings from test_bot_scores.py must still hold verbatim.

    def test_legacy_predicted_scores_header(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        assert "*Predicted scores*" in out

    def test_legacy_bold_top_score_mexico(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        assert "*1-0*" in out

    def test_legacy_bold_top_score_south_korea(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        assert "*1-1*" in out

    def test_legacy_top_prob_shown(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        assert "16.9%" in out

    def test_legacy_runner_ups_inline(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        mex_line = [l for l in out.splitlines() if "*Mexico vs South Africa*:" in l]
        assert mex_line, "expected a line with '*Mexico vs South Africa*:'"
        line = mex_line[0]
        assert "|" in line
        assert "2-0" in line
        assert "2-1" in line

    def test_legacy_ou_and_btts(self, tmp_path):
        path = _write_card(str(tmp_path))
        out = app.handle_scores(card_path=path, now_utc="2026-06-11T13:00:00")
        assert "O/U 2.5" in out
        assert "BTTS" in out
        assert "45.8%" in out
        assert "39.0%" in out


def test_authorized_accepts_comma_separated_list():
    from wca.bot.app import _authorized
    assert _authorized(12345, "12345")
    assert _authorized(12345, "12345,-100987")
    assert _authorized(-100987, "12345, -100987")
    assert not _authorized(99999, "12345,-100987")
    assert not _authorized(12345, "")
    assert not _authorized(12345, None)


def test_money_action_detection():
    from wca.bot.app import _is_money_action
    assert _is_money_action("yes")
    assert _is_money_action("  NO ")
    assert _is_money_action("Y PM-3")
    assert _is_money_action("n bet-12")
    assert not _is_money_action("/summary")
    assert not _is_money_action("yes please")
    assert not _is_money_action("/scores")


def test_admin_gate():
    from wca.bot.app import _is_admin
    assert _is_admin("123", None)        # unset -> everyone (single-user mode)
    assert _is_admin("123", "123")
    assert not _is_admin("456", "123")
    assert not _is_admin("", "123")
