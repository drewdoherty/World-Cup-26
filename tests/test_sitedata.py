"""Tests for wca.sitedata, scripts/wca_site.py and the static site assets.

Each test seeds an isolated temporary SQLite ledger (via the real
``wca.ledger.store`` helpers, so the schema matches production) and a synthetic
``card_latest.md`` with a scorelines section, then asserts the JSON structure,
graceful handling of missing inputs, the write round-trip, valid JSON on disk,
and that the shipped ``site/`` assets contain no disallowed external references.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

import pytest

from wca import sitedata
from wca.ledger import store


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SITE_DIR = os.path.join(_REPO_ROOT, "site")


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_site_test_")
    os.close(fd)
    os.unlink(path)  # let SQLite create it fresh
    return path


def _seed(db: str) -> dict:
    """Seed a spread of bets across platforms / statuses."""
    ids = {}
    ids["vb_won"] = store.record_bet(
        ts_utc="2026-06-11T10:00:00", match_id="M1", match_desc="Mexico vs Canada",
        market="1X2", selection="Home", platform="virginbet",
        decimal_odds=2.00, stake=10.0, model_prob=0.55, ev=0.10, db_path=db,
    )
    ids["pp_open"] = store.record_bet(
        ts_utc="2026-06-11T11:00:00", match_id="M1", match_desc="Mexico vs Canada",
        market="1X2", selection="Draw", platform="paddypower",
        decimal_odds=3.50, stake=20.0, model_prob=0.32, ev=0.05, db_path=db,
    )
    ids["poly_open"] = store.record_bet(
        ts_utc="2026-06-11T12:00:00", match_id="M2", match_desc="Argentina futures",
        market="WINNER", selection="Argentina", platform="polymarket",
        decimal_odds=1.80, stake=30.0, model_prob=0.60, ev=0.08, db_path=db,
    )
    ids["kalshi_open"] = store.record_bet(
        ts_utc="2026-06-11T12:30:00", match_id="M2", match_desc="Brazil futures",
        market="WINNER", selection="Brazil", platform="kalshi",
        decimal_odds=2.10, stake=7.5, model_prob=None, ev=None, db_path=db,
    )
    ids["vb_lost"] = store.record_bet(
        ts_utc="2026-06-11T14:00:00", match_id="M3", match_desc="USA vs Wales",
        market="1X2", selection="Away", platform="virginbet",
        decimal_odds=4.00, stake=5.0, model_prob=0.20, ev=-0.02, db_path=db,
    )
    store.settle_bet(ids["vb_won"], "won", db_path=db)
    store.settle_bet(ids["vb_lost"], "lost", db_path=db)
    return ids


_SYNTH_CARD = """<!-- generated: 2026-06-11T11:52:08 -->
*World Cup Alpha — bet card* (2 picks)

*1. South Korea vs Czech Republic* — South Korea @ *2.78* (betfair_ex_uk)
    model 37.6% / mkt 35.7%  edge *+4.6%*  [elo 49% dc 30%]
    stake: main 6.48
*2. Mexico vs South Africa* — Mexico @ *1.44* (betfair_ex_uk)
    model 71.3% / mkt 69.3%  edge *+2.6%*  [elo 83% dc 64%]
    stake: main 14.79

*World Cup Alpha — scorelines* (2 fixtures)

*Mexico vs South Africa*
    1-0  16.9%  fair 5.91  back >= 6.03
    2-0  15.5%  fair 6.45  back >= 6.57
    2-1  10.2%  fair 9.84  back >= 10.03
    3-0  9.1%  fair 10.94  back >= 11.16
    1-1  8.8%  fair 11.40  back >= 11.63
    0-0  7.6%  fair 13.09  back >= 13.36
    O/U 2.5: over 45.8% / under 54.2%   BTTS 39.0%

*South Korea vs Czech Republic*
    1-1  13.8%  fair 7.24  back >= 7.39
    1-0  13.0%  fair 7.69  back >= 7.84
    0-0  11.5%  fair 8.66  back >= 8.83
    0-1  10.2%  fair 9.81  back >= 10.01
    2-1  8.4%  fair 11.87  back >= 12.11
    2-0  7.0%  fair 14.30  back >= 14.59
    O/U 2.5: over 38.0% / under 62.0%   BTTS 45.1%
"""


def _write_card(tmp_path) -> str:
    path = os.path.join(str(tmp_path), "card_latest.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_SYNTH_CARD)
    return path


# ---------------------------------------------------------------------------
# Scoreline parsing.
# ---------------------------------------------------------------------------


class TestParseScorelines:
    def test_two_fixtures_parsed(self) -> None:
        preds = sitedata.parse_scorelines(_SYNTH_CARD)
        assert [p["fixture"] for p in preds] == [
            "Mexico vs South Africa",
            "South Korea vs Czech Republic",
        ]

    def test_bet_card_headings_not_treated_as_fixtures(self) -> None:
        # The numbered "*1. ... *" headings live in the bet-card section, which
        # is *before* the scorelines header, so they must be ignored.
        preds = sitedata.parse_scorelines(_SYNTH_CARD)
        names = [p["fixture"] for p in preds]
        assert not any(n.startswith("1.") or n.startswith("2.") for n in names)

    def test_score_rows_typed(self) -> None:
        preds = sitedata.parse_scorelines(_SYNTH_CARD)
        mex = preds[0]
        assert len(mex["scores"]) == 6
        top = mex["scores"][0]
        assert top["score"] == "1-0"
        assert isinstance(top["prob"], float) and top["prob"] == pytest.approx(16.9)
        assert isinstance(top["fair"], float) and top["fair"] == pytest.approx(5.91)
        assert isinstance(top["back"], float) and top["back"] == pytest.approx(6.03)

    def test_over_under_and_btts(self) -> None:
        preds = sitedata.parse_scorelines(_SYNTH_CARD)
        mex = preds[0]
        assert mex["over_under"]["line"] == pytest.approx(2.5)
        assert mex["over_under"]["over"] == pytest.approx(45.8)
        assert mex["over_under"]["under"] == pytest.approx(54.2)
        assert mex["btts"] == pytest.approx(39.0)

    def test_no_section_returns_empty(self) -> None:
        assert sitedata.parse_scorelines("just some text\nno section here") == []
        assert sitedata.parse_scorelines("") == []

    def test_score_without_fair_back(self) -> None:
        text = (
            "*World Cup Alpha — scorelines* (1 fixtures)\n\n"
            "*A vs B*\n"
            "    1-0  20.0%\n"
            "    O/U 2.5: over 50.0% / under 50.0%\n"
        )
        preds = sitedata.parse_scorelines(text)
        assert preds[0]["scores"][0]["score"] == "1-0"
        assert preds[0]["scores"][0]["prob"] == pytest.approx(20.0)
        assert preds[0]["scores"][0]["fair"] is None
        assert preds[0]["scores"][0]["back"] is None
        # BTTS absent -> None.
        assert preds[0]["btts"] is None


# ---------------------------------------------------------------------------
# build_site_data structure.
# ---------------------------------------------------------------------------


class TestBuildSiteData:
    def test_full_structure(self, tmp_path) -> None:
        db = _tmp_db()
        _seed(db)
        card = _write_card(tmp_path)
        data = sitedata.build_site_data(db, card_path=card, now_utc="2026-06-11 15:00:00 UTC")

        assert set(data.keys()) == {
            "meta", "totals", "totals_by_currency", "venues", "source_summary",
            "platforms", "closed_positions", "pnl_series", "clv", "positions",
            "predictions"
        }
        assert data["meta"]["generated"] == "2026-06-11 15:00:00 UTC"

    def test_venues_sum_to_totals(self, tmp_path) -> None:
        db = _tmp_db()
        _seed(db)
        data = sitedata.build_site_data(db, card_path=_write_card(tmp_path))
        venues = data["venues"]
        # Canonical venues plus the per-account sportsbook split.
        assert set(venues.keys()) == {
            "sportsbook", "polymarket", "kalshi",
            "sportsbook_1", "sportsbook_2",
        }

        # Sum only the canonical venues (the split keys would double-count).
        venue_sum = sum(venues[v]["wagered"]
                        for v in ("sportsbook", "polymarket", "kalshi"))
        assert venue_sum == pytest.approx(data["totals"]["wagered"])
        # Legacy combined sportsbook == sum of its account splits.
        assert venues["sportsbook"]["wagered"] == pytest.approx(
            venues["sportsbook_1"]["wagered"] + venues["sportsbook_2"]["wagered"]
        )
        # sportsbook = virginbet(10+5) + paddypower(20) = 35
        assert venues["sportsbook"]["wagered"] == pytest.approx(35.0)
        assert venues["polymarket"]["wagered"] == pytest.approx(30.0)
        assert venues["kalshi"]["wagered"] == pytest.approx(7.5)
        # totals: wagered 72.5, settled won (+10) lost (-5) = +5
        assert data["totals"]["wagered"] == pytest.approx(72.5)
        assert data["totals"]["settled_pl"] == pytest.approx(5.0)
        assert data["totals"]["n_bets"] == 5

    def test_positions_fields_and_open_only(self, tmp_path) -> None:
        db = _tmp_db()
        _seed(db)
        data = sitedata.build_site_data(db, card_path=_write_card(tmp_path))
        positions = data["positions"]
        # Two settled (won/lost) excluded; three open remain.
        assert len(positions) == 3
        for p in positions:
            assert set(p.keys()) == {
                "id", "ts_utc", "match", "match_id", "market", "selection",
                "platform", "venue", "account", "source", "currency",
                "decimal_odds", "stake", "model_prob", "market_prob_devig",
                "ev", "kelly_fraction", "notes",
            }
        # Every position has a known venue.
        venues = {p["venue"] for p in positions}
        assert venues <= {"sportsbook", "polymarket", "kalshi"}
        # The kalshi open bet preserves None model_prob/ev (not coerced to 0).
        kal = [p for p in positions if p["venue"] == "kalshi"][0]
        assert kal["model_prob"] is None
        assert kal["ev"] is None
        assert kal["stake"] == pytest.approx(7.5)

    def test_predictions_probs_are_floats(self, tmp_path) -> None:
        db = _tmp_db()
        _seed(db)
        data = sitedata.build_site_data(db, card_path=_write_card(tmp_path))
        assert len(data["predictions"]) == 2
        for fx in data["predictions"]:
            for s in fx["scores"]:
                assert isinstance(s["prob"], float)

    def test_missing_db_tolerated(self, tmp_path) -> None:
        missing = os.path.join(str(tmp_path), "nope.db")
        data = sitedata.build_site_data(missing, card_path=_write_card(tmp_path))
        assert data["totals"]["n_bets"] == 0
        assert data["totals"]["wagered"] == pytest.approx(0.0)
        assert data["positions"] == []
        # Card still parsed even with no db.
        assert len(data["predictions"]) == 2
        # Venues present and zeroed.
        assert set(data["venues"].keys()) == {
            "sportsbook", "polymarket", "kalshi",
            "sportsbook_1", "sportsbook_2",
        }
        for v in data["venues"].values():
            assert v["wagered"] == pytest.approx(0.0)

    def test_missing_card_tolerated(self) -> None:
        db = _tmp_db()
        _seed(db)
        data = sitedata.build_site_data(db, card_path="/no/such/card.md")
        assert data["predictions"] == []
        # Ledger-derived sections still populated.
        assert data["totals"]["n_bets"] == 5

    def test_both_missing_tolerated(self, tmp_path) -> None:
        data = sitedata.build_site_data(
            os.path.join(str(tmp_path), "x.db"),
            card_path=os.path.join(str(tmp_path), "x.md"),
            now_utc="now",
        )
        assert data["positions"] == []
        assert data["predictions"] == []
        assert data["totals"]["n_bets"] == 0
        assert data["meta"]["generated"] == "now"

    def test_clv_na_before_closing_lines(self, tmp_path) -> None:
        db = _tmp_db()
        _seed(db)
        data = sitedata.build_site_data(db, card_path=_write_card(tmp_path))
        assert data["clv"]["avg_clv"] is None
        assert data["clv"]["n_with_close"] == 0

    def test_clv_present_with_closing_line(self, tmp_path) -> None:
        db = _tmp_db()
        ids = _seed(db)
        store.set_closing_odds(ids["poly_open"], 1.50, db_path=db)
        data = sitedata.build_site_data(db, card_path=_write_card(tmp_path))
        assert data["clv"]["n_with_close"] == 1
        assert data["clv"]["avg_clv"] is not None


# ---------------------------------------------------------------------------
# write_site_data round-trip.
# ---------------------------------------------------------------------------


class TestWriteSiteData:
    def test_round_trip_valid_json(self, tmp_path) -> None:
        db = _tmp_db()
        _seed(db)
        card = _write_card(tmp_path)
        out = os.path.join(str(tmp_path), "nested", "data.json")
        returned = sitedata.write_site_data(
            db, out_path=out, card_path=card, now_utc="2026-06-11 15:00:00 UTC"
        )
        assert returned == out
        assert os.path.exists(out)

        with open(out, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)  # raises if not valid JSON

        # Round-trips to the same structure build_site_data produced.
        built = sitedata.build_site_data(
            db, card_path=card, now_utc="2026-06-11 15:00:00 UTC"
        )
        assert on_disk == built

    def test_creates_parent_dirs(self, tmp_path) -> None:
        db = _tmp_db()
        out = os.path.join(str(tmp_path), "a", "b", "c", "data.json")
        sitedata.write_site_data(db, out_path=out, card_path="/no/card.md")
        assert os.path.exists(out)

    def test_unicode_preserved(self, tmp_path) -> None:
        db = _tmp_db()
        store.record_bet(
            ts_utc="2026-06-11T10:00:00", match_id="M9",
            match_desc="Côte d'Ivoire vs Türkiye",
            market="1X2", selection="Home", platform="virginbet",
            decimal_odds=2.0, stake=4.0, db_path=db,
        )
        out = os.path.join(str(tmp_path), "data.json")
        sitedata.write_site_data(db, out_path=out, card_path="/no/card.md")
        with open(out, "r", encoding="utf-8") as fh:
            raw = fh.read()
        # ensure_ascii=False keeps the accented chars literal.
        assert "Côte d'Ivoire vs Türkiye" in raw


# ---------------------------------------------------------------------------
# Static site asset hygiene.
# ---------------------------------------------------------------------------


class TestSiteAssets:
    def test_index_references_data_json(self) -> None:
        with open(os.path.join(_SITE_DIR, "index.html"), "r", encoding="utf-8") as fh:
            html = fh.read()
        # The app loads ./data.json (via app.js fetch). Index must wire app.js
        # and the app must reference ./data.json.
        assert "./app.js" in html
        with open(os.path.join(_SITE_DIR, "app.js"), "r", encoding="utf-8") as fh:
            js = fh.read()
        assert "./data.json" in js

    def test_no_external_assets_in_html(self) -> None:
        with open(os.path.join(_SITE_DIR, "index.html"), "r", encoding="utf-8") as fh:
            html = fh.read()
        # No http(s) external asset references anywhere in the HTML.
        urls = re.findall(r"https?://[^\s\"'<>]+", html)
        assert urls == [], "unexpected external URLs in index.html: %r" % urls

    def test_only_allowed_external_url_is_polymarket_in_js(self) -> None:
        with open(os.path.join(_SITE_DIR, "app.js"), "r", encoding="utf-8") as fh:
            js = fh.read()
        urls = re.findall(r"https?://[^\s\"'<>]+", js)
        for u in urls:
            assert "gamma-api.polymarket.com" in u, (
                "disallowed external URL in app.js: %r" % u
            )

    def test_no_external_urls_in_css(self) -> None:
        with open(os.path.join(_SITE_DIR, "style.css"), "r", encoding="utf-8") as fh:
            css = fh.read()
        urls = re.findall(r"https?://[^\s\"'<>)]+", css)
        assert urls == [], "unexpected external URLs in style.css: %r" % urls


# ---------------------------------------------------------------------------
# CLI smoke test.
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_writes_json(self, tmp_path) -> None:
        import subprocess
        import sys

        db = _tmp_db()
        _seed(db)
        card = _write_card(tmp_path)
        out = os.path.join(str(tmp_path), "cli_data.json")
        script = os.path.join(_REPO_ROOT, "scripts", "wca_site.py")

        proc = subprocess.run(
            [sys.executable, script, "--db", db, "--card", card, "--out", out],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert os.path.exists(out)
        with open(out, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["totals"]["n_bets"] == 5
        assert out in proc.stdout
        assert "totals:" in proc.stdout


class TestClosedPositionsAndPnl:
    def test_closed_positions_and_pnl_series(self, tmp_path):
        from wca.ledger.store import record_bet, settle_bet
        from wca.sitedata import build_site_data

        db = str(tmp_path / "t.db")
        b1 = record_bet("2026-06-11T10:00:00", "M1", "A vs B", "h2h", "A",
                        "virginbet", 2.0, 10.0, db_path=db)
        b2 = record_bet("2026-06-11T11:00:00", "M2", "C vs D", "pm", "C Yes",
                        "polymarket", 1.5, 20.0, db_path=db)
        b3 = record_bet("2026-06-11T12:00:00", "M3", "E vs F", "h2h", "E",
                        "bet365", 3.0, 5.0, db_path=db)  # stays open
        settle_bet(b1, "won", db_path=db, settled_ts_utc="2026-06-11T21:00:00")
        settle_bet(b2, "lost", db_path=db, settled_ts_utc="2026-06-11T22:00:00")

        d = build_site_data(db, card_path=str(tmp_path / "none.md"))
        closed = d["closed_positions"]
        assert len(closed) == 2
        by_id = {c["id"]: c for c in closed}
        assert by_id[b1]["pl"] == 10.0 and by_id[b1]["currency"] == "GBP"
        assert by_id[b2]["pl"] == -20.0 and by_id[b2]["currency"] == "USD"
        assert by_id[b1]["settled_ts"] == "2026-06-11T21:00:00"
        # open bet not in closed
        assert b3 not in by_id

        ps = d["pnl_series"]
        assert ps["sportsbook"]["points"] == [["2026-06-11T21:00:00", 10.0]]
        assert ps["prediction_markets"]["points"] == [["2026-06-11T22:00:00", -20.0]]
        assert ps["sportsbook"]["currency"] == "GBP"

    def test_void_counts_as_closed_zero_pl(self, tmp_path):
        from wca.ledger.store import record_bet, void_bet
        from wca.sitedata import build_site_data

        db = str(tmp_path / "t.db")
        b = record_bet("2026-06-11T10:00:00", "M", "A vs B", "h2h", "A",
                       "virginbet", 2.0, 10.0, db_path=db)
        void_bet(b, db_path=db)
        d = build_site_data(db, card_path=str(tmp_path / "none.md"))
        assert d["closed_positions"][0]["status"] == "void"
        assert d["closed_positions"][0]["pl"] == 0.0
