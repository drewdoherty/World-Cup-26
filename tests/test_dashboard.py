"""Tests for wca.dashboard and scripts/wca_dashboard.py.

Each test seeds an isolated temporary SQLite ledger (via the real
``wca.ledger.store`` helpers, so the schema and P&L conventions match
production) and asserts the dashboard rollups, venue mapping, graceful
handling of a missing database, HTML escaping, and the write round-trip.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

from wca import dashboard
from wca.ledger import store


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _tmp_db() -> str:
    """Return a path to a fresh (non-existent) temp .db file."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_dash_test_")
    os.close(fd)
    os.unlink(path)  # let SQLite create it fresh
    return path


def _seed(db: str) -> dict:
    """Seed a ledger with a spread of bets across platforms and statuses.

    Returns a dict of the inserted bet ids for convenience.

    Layout (stake / odds):
      * virginbet   1X2 Home    £10 @ 2.00  -> SETTLED WON   (pl = +10)
      * paddypower  1X2 Draw    £20 @ 3.50  -> OPEN
      * polymarket  WINNER ARG  £30 @ 1.80  -> OPEN
      * paddypower  BTTS Yes    £15 @ 1.90  -> VOID          (pl = 0)
      * virginbet   1X2 Away    £ 5 @ 4.00  -> SETTLED LOST  (pl = -5)
    """
    ids = {}
    ids["vb_won"] = store.record_bet(
        ts_utc="2026-06-11T10:00:00", match_id="M1", match_desc="Mexico vs Canada",
        market="1X2", selection="Home", platform="virginbet",
        decimal_odds=2.00, stake=10.0, db_path=db,
    )
    ids["pp_open"] = store.record_bet(
        ts_utc="2026-06-11T11:00:00", match_id="M1", match_desc="Mexico vs Canada",
        market="1X2", selection="Draw", platform="paddypower",
        decimal_odds=3.50, stake=20.0, db_path=db,
    )
    ids["poly_open"] = store.record_bet(
        ts_utc="2026-06-11T12:00:00", match_id="M2", match_desc="Argentina futures",
        market="WINNER", selection="Argentina", platform="polymarket",
        decimal_odds=1.80, stake=30.0, db_path=db,
    )
    ids["pp_void"] = store.record_bet(
        ts_utc="2026-06-11T13:00:00", match_id="M3", match_desc="USA vs Wales",
        market="BTTS", selection="Yes", platform="paddypower",
        decimal_odds=1.90, stake=15.0, db_path=db,
    )
    ids["vb_lost"] = store.record_bet(
        ts_utc="2026-06-11T14:00:00", match_id="M3", match_desc="USA vs Wales",
        market="1X2", selection="Away", platform="virginbet",
        decimal_odds=4.00, stake=5.0, db_path=db,
    )

    store.settle_bet(ids["vb_won"], "won", db_path=db)
    store.settle_bet(ids["vb_lost"], "lost", db_path=db)
    store.void_bet(ids["pp_void"], db_path=db)
    return ids


# ---------------------------------------------------------------------------
# Venue mapping.
# ---------------------------------------------------------------------------


class TestVenueMapping:
    def test_dedicated_venues(self) -> None:
        assert dashboard.venue_for_platform("polymarket") == "polymarket"
        assert dashboard.venue_for_platform("kalshi") == "kalshi"

    def test_sportsbook_catchall(self) -> None:
        for p in ("virginbet", "paddypower", "bet365", "betfair_ex",
                  "skybet", "williamhill", "unknown", "Bet365", "  VirginBet "):
            assert dashboard.venue_for_platform(p) == "sportsbook"

    def test_none_and_empty_map_to_sportsbook(self) -> None:
        assert dashboard.venue_for_platform(None) == "sportsbook"
        assert dashboard.venue_for_platform("") == "sportsbook"

    def test_case_insensitive_dedicated(self) -> None:
        assert dashboard.venue_for_platform("Polymarket") == "polymarket"
        assert dashboard.venue_for_platform("KALSHI") == "kalshi"


# ---------------------------------------------------------------------------
# gather_stats rollups.
# ---------------------------------------------------------------------------


class TestGatherStats:
    def test_venue_wagered_sums(self) -> None:
        db = _tmp_db()
        _seed(db)
        stats = dashboard.gather_stats(db)
        bv = stats["by_venue"]

        # virginbet (won £10 + lost £5) -> sportsbook also gets paddypower 20+15
        assert bv["sportsbook"]["wagered"] == pytest.approx(10 + 20 + 15 + 5)
        assert bv["polymarket"]["wagered"] == pytest.approx(30.0)
        assert bv["kalshi"]["wagered"] == pytest.approx(0.0)

    def test_venue_bet_counts(self) -> None:
        db = _tmp_db()
        _seed(db)
        bv = dashboard.gather_stats(db)["by_venue"]
        assert bv["sportsbook"]["n_bets"] == 4
        assert bv["polymarket"]["n_bets"] == 1
        assert bv["kalshi"]["n_bets"] == 0

    def test_open_stake_excludes_settled_and_void(self) -> None:
        db = _tmp_db()
        _seed(db)
        bv = dashboard.gather_stats(db)["by_venue"]
        # Open: paddypower 20 (sportsbook) + polymarket 30.
        assert bv["sportsbook"]["open_stake"] == pytest.approx(20.0)
        assert bv["polymarket"]["open_stake"] == pytest.approx(30.0)

    def test_settled_pl_math(self) -> None:
        db = _tmp_db()
        _seed(db)
        bv = dashboard.gather_stats(db)["by_venue"]
        # virginbet won: (2.00-1)*10 = +10; lost: -5 -> net +5 (sportsbook).
        # void contributes 0.
        assert bv["sportsbook"]["settled_pl"] == pytest.approx(5.0)
        assert bv["polymarket"]["settled_pl"] == pytest.approx(0.0)

    def test_totals_match_sum_of_venues(self) -> None:
        db = _tmp_db()
        _seed(db)
        stats = dashboard.gather_stats(db)
        t = stats["totals"]
        assert t["wagered"] == pytest.approx(80.0)        # 10+20+30+15+5
        assert t["open_stake"] == pytest.approx(50.0)     # 20 + 30
        assert t["settled_pl"] == pytest.approx(5.0)      # +10 -5
        assert t["n_bets"] == 5

    def test_bets_newest_first(self) -> None:
        db = _tmp_db()
        _seed(db)
        bets = dashboard.gather_stats(db)["bets"]
        ids = [b["id"] for b in bets]
        assert ids == sorted(ids, reverse=True)
        assert len(bets) == 5

    def test_generated_inputs_ok_true_when_db_present(self) -> None:
        db = _tmp_db()
        _seed(db)
        assert dashboard.gather_stats(db)["generated_inputs_ok"] is True


# ---------------------------------------------------------------------------
# CLV rollup.
# ---------------------------------------------------------------------------


class TestClv:
    def test_clv_na_when_no_closing_odds(self) -> None:
        db = _tmp_db()
        _seed(db)
        clv = dashboard.gather_stats(db)["clv"]
        assert clv["n_with_close"] == 0
        assert clv["avg_clv"] is None
        assert clv["pct_beat_close"] is None

    def test_clv_aggregates_with_closing_odds(self) -> None:
        db = _tmp_db()
        ids = _seed(db)
        # Beat the close on one bet (took 2.00, closed 1.80 -> +ve CLV);
        # lost the close on another (took 1.80, closed 2.00 -> -ve CLV).
        store.set_closing_odds(ids["vb_won"], 1.80, db_path=db)
        store.set_closing_odds(ids["poly_open"], 2.00, db_path=db)
        clv = dashboard.gather_stats(db)["clv"]
        assert clv["n_with_close"] == 2
        assert clv["pct_beat_close"] == pytest.approx(0.5)
        expected_avg = ((2.00 / 1.80) - 1.0 + (1.80 / 2.00) - 1.0) / 2.0
        assert clv["avg_clv"] == pytest.approx(expected_avg)


# ---------------------------------------------------------------------------
# Missing / empty database.
# ---------------------------------------------------------------------------


class TestMissingDb:
    def test_missing_db_returns_zeros(self) -> None:
        stats = dashboard.gather_stats("/nonexistent/path/to/wca.db")
        assert stats["totals"] == {
            "wagered": 0.0, "open_stake": 0.0, "settled_pl": 0.0, "n_bets": 0,
        }
        for v in ("sportsbook", "polymarket", "kalshi"):
            assert stats["by_venue"][v]["wagered"] == 0.0
            assert stats["by_venue"][v]["n_bets"] == 0
        assert stats["bets"] == []
        assert stats["clv"]["n_with_close"] == 0
        assert stats["generated_inputs_ok"] is False

    def test_empty_db_handled(self) -> None:
        db = _tmp_db()
        store.init_db(db)  # tables exist but no bets
        stats = dashboard.gather_stats(db)
        assert stats["totals"]["n_bets"] == 0
        assert stats["bets"] == []
        assert stats["generated_inputs_ok"] is True

    def test_render_missing_db_does_not_crash(self) -> None:
        stats = dashboard.gather_stats("/nope.db")
        html_out = dashboard.render_html(stats, now_utc="2026-06-11 00:00:00 UTC")
        assert "World Cup Alpha" in html_out
        assert "No open bets." in html_out


# ---------------------------------------------------------------------------
# render_html content + escaping.
# ---------------------------------------------------------------------------


class TestRenderHtml:
    def test_contains_venue_amounts(self) -> None:
        db = _tmp_db()
        _seed(db)
        stats = dashboard.gather_stats(db)
        html_out = dashboard.render_html(stats, now_utc="2026-06-11 12:00:00 UTC")
        # Wagered-by-venue amounts (sportsbook 50.00, polymarket 30.00).
        assert "£50.00" in html_out      # sportsbook wagered
        assert "£30.00" in html_out      # polymarket wagered
        # Venue labels present.
        assert "sportsbook" in html_out
        assert "polymarket" in html_out
        assert "kalshi" in html_out

    def test_is_standalone_html_no_external_requests(self) -> None:
        stats = dashboard.gather_stats(_tmp_db_with_seed())
        html_out = dashboard.render_html(stats, now_utc="2026-06-11 12:00:00 UTC")
        assert html_out.lstrip().startswith("<!DOCTYPE html>")
        assert "</html>" in html_out
        # No external asset references of any kind. The only http(s) URL allowed
        # is the SVG XML namespace declaration (not a network request); strip it
        # before checking so a real external reference would still be caught.
        stripped = html_out.replace("http://www.w3.org/2000/svg", "")
        for needle in ("http://", "https://", "src=", "<link", "<script", "cdn",
                       "@import", "url("):
            assert needle not in stripped

    def test_escapes_malicious_match_desc(self) -> None:
        db = _tmp_db()
        store.record_bet(
            ts_utc="2026-06-11T10:00:00", match_id="X",
            match_desc="<script>alert(1)</script>",
            market="1X2", selection="Home", platform="virginbet",
            decimal_odds=2.0, stake=10.0, db_path=db,
        )
        stats = dashboard.gather_stats(db)
        html_out = dashboard.render_html(stats, now_utc="2026-06-11 12:00:00 UTC")
        assert "<script>alert(1)</script>" not in html_out
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out

    def test_open_bet_appears_in_table(self) -> None:
        db = _tmp_db()
        _seed(db)
        stats = dashboard.gather_stats(db)
        html_out = dashboard.render_html(stats)
        # Open polymarket selection should be in the table.
        assert "Argentina" in html_out
        assert "Argentina futures" in html_out

    def test_clv_na_rendered_cleanly(self) -> None:
        db = _tmp_db()
        _seed(db)
        html_out = dashboard.render_html(dashboard.gather_stats(db))
        assert "N/A" in html_out
        assert "no closing lines yet" in html_out

    def test_generated_timestamp_present(self) -> None:
        html_out = dashboard.render_html(
            dashboard.gather_stats("/nope.db"), now_utc="2026-06-11 09:30:00 UTC"
        )
        assert "2026-06-11 09:30:00 UTC" in html_out


def _tmp_db_with_seed() -> str:
    db = _tmp_db()
    _seed(db)
    return db


# ---------------------------------------------------------------------------
# write_dashboard round-trip.
# ---------------------------------------------------------------------------


class TestWriteDashboard:
    def test_round_trip_writes_file(self, tmp_path) -> None:
        db = _tmp_db()
        _seed(db)
        out = tmp_path / "nested" / "dir" / "index.html"
        returned = dashboard.write_dashboard(
            db, str(out), now_utc="2026-06-11 12:00:00 UTC"
        )
        assert returned == str(out)
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "World Cup Alpha" in content
        assert "£30.00" in content  # polymarket wagered survived the round-trip

    def test_creates_parent_dirs(self, tmp_path) -> None:
        db = _tmp_db()
        store.init_db(db)
        out = tmp_path / "a" / "b" / "c" / "page.html"
        dashboard.write_dashboard(db, str(out), now_utc="now")
        assert out.exists()


# ---------------------------------------------------------------------------
# CLI smoke test.
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_writes_and_prints_totals(self, tmp_path) -> None:
        db = _tmp_db()
        _seed(db)
        out = tmp_path / "dash" / "index.html"
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(repo_root, "scripts", "wca_dashboard.py")
        result = subprocess.run(
            [sys.executable, script, "--db", db, "--out", str(out)],
            capture_output=True, text=True, cwd=repo_root,
        )
        assert result.returncode == 0, result.stderr
        assert out.exists()
        assert str(out) in result.stdout
        assert "totals:" in result.stdout
        assert "n_bets=5" in result.stdout
        assert "World Cup Alpha" in out.read_text(encoding="utf-8")
