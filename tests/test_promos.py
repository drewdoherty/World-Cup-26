"""Tests for wca.promos (promotions catalog + scraper) and the daemon cycle.

Covers:
* extraction from small inline HTML fixtures (finds an offer; [] on junk),
* diff_and_upsert new/changed/removed/unchanged across two runs,
* seed_from_recon populating rows idempotently,
* parse_boost_text best-effort odds extraction,
* the ISOLATION invariant mirrored from tests/test_offers.py::TestIsolation —
  running every promo op against a db that already holds ``bets`` and
  ``sb_offers`` rows must leave those counts unchanged,
* a daemon run_cycle smoke test with a stub fetch + stub Telegram client (no
  network, never sends under pytest).
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from wca import promos


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_promos_test_")
    os.close(fd)
    os.unlink(path)
    return path


def _conn(db: str) -> sqlite3.Connection:
    c = promos._connect(db)
    promos.init_db(c)
    return c


FIXED_NOW = "2026-06-13T12:00:00"
LATER_NOW = "2026-06-13T18:00:00"


# A tiny but realistic promotions-page HTML fragment with one real offer block
# plus chrome/nav that must NOT be mistaken for offers.
OFFER_HTML = """
<html><head><title>Promotions</title>
<style>.x{color:red}</style>
<script>var a = "free bet";</script>
</head>
<body>
  <nav><a href="/">Home</a><a href="/login">Log In</a><a>Free Bets</a></nav>
  <div class="promo">
    <h2>Bet £10 Get £30 in Free Bets</h2>
    <p>New customers: bet £10 at min odds 1/2 and get £30 in free bets. 18+ BeGambleAware.</p>
  </div>
  <div class="boost">
    <h3>Price Boost: Brazil to win was 2/1 now 5/2</h3>
  </div>
  <footer>Cookie policy. Terms and conditions apply. Gamble responsibly.</footer>
</body></html>
"""

# Pure junk — no offer-like content at all.
JUNK_HTML = """
<html><body>
  <p>Welcome to our website.</p>
  <p>Contact us at info@example.invalid.</p>
  <nav><a href="/about">About</a></nav>
</body></html>
"""


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


class TestExtractPromos:
    def test_finds_offer_and_boost(self) -> None:
        promos_found = promos.extract_promos(OFFER_HTML, "Test Book")
        assert promos_found, "expected at least one offer extracted"
        titles = " || ".join(p["title"].lower() for p in promos_found)
        assert "free bet" in titles
        # The price-boost line is classified as a boost.
        types = {p["promo_type"] for p in promos_found}
        assert "boost" in types
        # The "Bet £10 Get £30" block is an ongoing offer.
        assert "ongoing" in types

    def test_junk_returns_empty(self) -> None:
        assert promos.extract_promos(JUNK_HTML, "Test Book") == []

    def test_empty_input(self) -> None:
        assert promos.extract_promos("", "X") == []
        assert promos.extract_promos(None, "X") == []  # type: ignore[arg-type]

    def test_no_fabrication_on_nav_only(self) -> None:
        # A page that is ONLY nav chrome (incl. a "Free Bets" menu item) must
        # not fabricate an offer.
        html = "<body><nav><a>Free Bets</a><a>Promotions</a><a>Login</a></nav></body>"
        assert promos.extract_promos(html, "X") == []


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_stable_under_whitespace_and_case(self) -> None:
        a = promos.fingerprint("Sky Bet", "Bet £10 Get £30", "Free bets!")
        b = promos.fingerprint("sky  bet", "bet  £10   get £30", "FREE BETS")
        assert a == b

    def test_differs_on_content(self) -> None:
        a = promos.fingerprint("Sky Bet", "Bet £10 Get £30", "")
        b = promos.fingerprint("Sky Bet", "Bet £10 Get £40", "")
        assert a != b


# ---------------------------------------------------------------------------
# parse_boost_text
# ---------------------------------------------------------------------------


class TestParseBoostText:
    def test_was_now_fractional(self) -> None:
        p = promos.parse_boost_text("Brazil to win was 2/1 now 5/2")
        assert p is not None
        assert abs(p["was_odds"] - 3.0) < 1e-9      # 2/1 -> 3.0
        assert abs(p["boosted_odds"] - 3.5) < 1e-9  # 5/2 -> 3.5
        assert "brazil" in (p["selection"] or "").lower()

    def test_boosted_to_decimal(self) -> None:
        p = promos.parse_boost_text("England & over 2.5 goals boosted to 4.5")
        assert p is not None
        assert abs(p["boosted_odds"] - 4.5) < 1e-9

    def test_unparseable_returns_none(self) -> None:
        assert promos.parse_boost_text("Today's big match preview") is None
        assert promos.parse_boost_text("") is None


# ---------------------------------------------------------------------------
# diff_and_upsert
# ---------------------------------------------------------------------------


class TestDiffAndUpsert:
    def test_new_then_unchanged_then_changed_then_removed(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        site = "Test Book"

        c1 = {"title": "Bet £10 Get £30", "description": "Bet £10 get £30 free",
              "promo_type": "ongoing", "terms": "", "url": "u1"}
        c2 = {"title": "Daily Price Boost", "description": "Brazil was 2/1 now 5/2",
              "promo_type": "boost", "terms": "", "url": "u2"}

        # Run 1: both new.
        r1 = promos.diff_and_upsert(conn, site, [c1, c2], FIXED_NOW)
        assert len(r1["new"]) == 2
        assert r1["changed"] == [] and r1["removed"] == [] and r1["unchanged"] == []

        # Run 2: c1 identical (unchanged), c2 url drift (changed).
        c2_changed = dict(c2, url="u2-new")
        r2 = promos.diff_and_upsert(conn, site, [c1, c2_changed], LATER_NOW)
        assert len(r2["unchanged"]) == 1
        assert len(r2["changed"]) == 1
        assert r2["new"] == [] and r2["removed"] == []
        # last_seen bumped on the unchanged row.
        row = conn.execute(
            "SELECT last_seen_utc FROM promotions WHERE url = 'u1'"
        ).fetchone()
        assert row["last_seen_utc"] == LATER_NOW

        # Run 3: only c1 present -> c2 vanished -> removed.
        r3 = promos.diff_and_upsert(conn, site, [c1], LATER_NOW)
        assert len(r3["removed"]) == 1
        active = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE status='active' AND site=?",
            (site,),
        ).fetchone()[0]
        assert active == 1
        conn.close()

    def test_scrape_does_not_remove_seeds(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        # Insert a seed row directly.
        promos._upsert_seed_row(
            conn,
            {"site": "Sky Bet", "title": "Bet £10 Get £50",
             "description": "signup", "promo_type": "signup",
             "terms": "", "url": ""},
            FIXED_NOW,
        )
        conn.commit()
        # A scrape run for Sky Bet that finds a different offer must NOT remove
        # the seed (different source).
        promos.diff_and_upsert(
            conn, "Sky Bet",
            [{"title": "Acca Edge", "description": "boost on accas",
              "promo_type": "ongoing", "terms": "", "url": ""}],
            LATER_NOW, source="scrape",
        )
        seed_status = conn.execute(
            "SELECT status FROM promotions WHERE source='seed' AND site='Sky Bet'"
        ).fetchone()["status"]
        assert seed_status == "active"
        conn.close()


# ---------------------------------------------------------------------------
# seed_from_recon
# ---------------------------------------------------------------------------


RECON_PATH = str(Path(__file__).resolve().parent.parent / "docs" / "recon" / "uk_books.md")


class TestSeedFromRecon:
    def test_populates_signup_and_ongoing(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        counts = promos.seed_from_recon(conn, path=RECON_PATH, now_utc=FIXED_NOW)
        assert counts["signup"] > 0, "expected sign-up rows seeded from 2a table"
        assert counts["ongoing"] > 0, "expected ongoing rows seeded from 2b bullets"

        # Sign-up offers reconstruct structured fields from the terms blob.
        offers = promos.signup_offers(conn)
        assert offers
        sites = {o["site"] for o in offers}
        # Paddy Power's offer carries the YSKATF promo code in the recon table.
        pp = [o for o in offers if o["site"] == "Paddy Power"]
        assert pp, "Paddy Power sign-up offer should be seeded"
        assert any(o["promo_code"] for o in offers), "some offers carry a promo code"
        assert "Sky Bet" in sites
        conn.close()

    def test_manual_current_promos_seed_betfred_slate(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        counts = promos.seed_manual_current_promos(conn, now_utc=FIXED_NOW)
        assert counts["new"] >= 8
        rows = conn.execute(
            "SELECT promo_type, title, source FROM promotions "
            "WHERE site='Betfred' AND status='active' ORDER BY title"
        ).fetchall()
        titles = {r["title"] for r in rows}
        assert "Sports Welcome Offer" in titles
        assert "Bet Builder Offer — Belgium vs Iran" in titles
        assert "Bet Builder Winning Bonus — Uruguay vs Cape Verde" in titles
        assert "2-Up Early Payout" in titles
        assert all(r["source"] == "manual" for r in rows)
        assert any(r["promo_type"] == "signup" for r in rows)
        # Idempotent: second run only refreshes unchanged rows.
        counts2 = promos.seed_manual_current_promos(conn, now_utc=LATER_NOW)
        assert counts2["new"] == 0
        conn.close()

    def test_idempotent(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        promos.seed_from_recon(conn, path=RECON_PATH, now_utc=FIXED_NOW)
        n1 = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        # Re-seed: no new rows (UNIQUE fingerprint), last_seen bumped.
        promos.seed_from_recon(conn, path=RECON_PATH, now_utc=LATER_NOW)
        n2 = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        assert n1 == n2, "re-seeding must not duplicate rows"
        conn.close()

    def test_missing_file_not_fatal(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        counts = promos.seed_from_recon(conn, path="/nonexistent/recon.md",
                                        now_utc=FIXED_NOW)
        assert counts == {"signup": 0, "ongoing": 0}
        conn.close()


# ---------------------------------------------------------------------------
# snapshots + boost evals + reader views
# ---------------------------------------------------------------------------


class TestSnapshotsAndEvals:
    def test_record_and_read_snapshot(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        promos.record_snapshot(conn, "Sky Bet", "u", 200, "ok", 3, ts_utc=FIXED_NOW)
        promos.record_snapshot(conn, "Sky Bet", "u", 200, "blocked", 0,
                               ts_utc=LATER_NOW)
        latest = promos.latest_snapshot_per_site(conn)
        assert latest["Sky Bet"]["fetch_status"] == "blocked"
        ok = promos.latest_ok_snapshot_per_site(conn)
        assert ok["Sky Bet"] == FIXED_NOW
        conn.close()

    def test_record_boost_eval_and_recent(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        eid = promos.record_boost_eval(
            conn, ts_utc=FIXED_NOW, site="Sky Bet", fixture="Brazil vs Serbia",
            market="match", selection="Brazil", boosted_odds=3.5, was_odds=3.0,
            model_prob=0.40, fair_odds=2.5, edge=0.40, is_plus_ev=True,
            priceable=True, reason=None, source="scrape",
        )
        assert isinstance(eid, int) and eid >= 1
        rows = promos.recent_boost_evals(conn, limit=10)
        assert len(rows) == 1
        assert rows[0]["is_plus_ev"] == 1
        conn.close()


# ---------------------------------------------------------------------------
# ISOLATION (mirror tests/test_offers.py::TestIsolation)
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_promo_ops_do_not_touch_bets_or_sb_offers(self) -> None:
        """The core invariant: the promo layer never touches the model-edge
        ledger (``bets`` / ``bankroll_events``) or the matched-betting ledger
        (``sb_offers``)."""
        db = _tmp_db()

        # Seed a real model bet + a deposit + an sb_offers row in the same file.
        from wca.ledger import store
        from wca import offers as sb_offers_mod

        store.add_bankroll_event("2026-06-10T12:00:00", 1000.0, db_path=db)
        bid = store.record_bet(
            ts_utc="2026-06-11T14:00:00", match_id="M1", match_desc="Test",
            market="1X2", selection="Home", platform="Bet365",
            decimal_odds=2.0, stake=25.0, db_path=db,
        )
        store.settle_bet(bid, "won", db_path=db)
        sb_offers_mod.record_offer("me", "Bet365", free_bet_value=30.0, db_path=db)

        def _count(table: str) -> int:
            c = sqlite3.connect(db)
            try:
                return c.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            finally:
                c.close()

        bets_before = _count("bets")
        offers_before = _count("sb_offers")
        bankroll_before = _count("bankroll_events")

        # Hammer every promo operation against the same db.
        conn = _conn(db)
        promos.seed_from_recon(conn, path=RECON_PATH, now_utc=FIXED_NOW)
        promos.diff_and_upsert(
            conn, "Test Book",
            [{"title": "Bet £10 Get £30", "description": "free bet offer",
              "promo_type": "ongoing", "terms": "", "url": ""}],
            FIXED_NOW,
        )
        promos.record_snapshot(conn, "Test Book", "u", 200, "ok", 1, ts_utc=FIXED_NOW)
        promos.record_boost_eval(
            conn, ts_utc=FIXED_NOW, site="Test Book", fixture="A vs B",
            market="m", selection="A", boosted_odds=3.0, was_odds=2.5,
            model_prob=0.4, fair_odds=2.5, edge=0.2, is_plus_ev=True,
            priceable=True, reason=None, source="scrape",
        )
        promos.active_promotions(conn)
        promos.signup_offers(conn)
        conn.close()

        assert _count("bets") == bets_before
        assert _count("sb_offers") == offers_before
        assert _count("bankroll_events") == bankroll_before

    def test_promo_tables_present_and_distinct(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        conn.close()
        c = sqlite3.connect(db)
        names = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        c.close()
        assert {"promotions", "promo_snapshots", "boost_evals"} <= names
        # The promo layer never creates the ledger tables on its own.
        assert "bets" not in names
        assert "sb_offers" not in names


# ---------------------------------------------------------------------------
# daemon cycle smoke (stub fetch + stub client; never sends under pytest)
# ---------------------------------------------------------------------------


def _load_daemon():
    """Import scripts/wca_promosd.py as a module (it inserts src on sys.path)."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "wca_promosd.py"
    spec = importlib.util.spec_from_file_location("wca_promosd_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class _RecordingClient:
    def __init__(self) -> None:
        self.sent = []

    def send_message(self, chat_id, text, parse_mode="Markdown"):  # noqa: ANN001
        self.sent.append((chat_id, text))
        return {"ok": True}


class TestDaemonCycle:
    def test_cycle_runs_with_stub_fetch_and_never_sends_under_pytest(self) -> None:
        daemon = _load_daemon()
        db = _tmp_db()

        # Stub fetch: the first registry site's promo page returns an offer;
        # everything else returns 'blocked' (the realistic default).
        first_url = promos.SITES[0]["promos_url"]

        def fake_fetch(url):
            if url == first_url:
                return (200, OFFER_HTML, "ok")
            return (403, None, "blocked")

        client = _RecordingClient()
        stats = daemon.run_cycle(
            db, max_per_cycle=5, seed=False, fetch=fake_fetch,
            client=client, chat_id="123", now_utc=FIXED_NOW,
        )
        # Seeded (empty catalog) + scraped one OK site + many blocked.
        assert stats["sites"] == len(promos.SITES)
        assert stats["ok"] >= 1
        assert stats["blocked"] >= 1
        assert stats["new"] >= 1
        # PYTEST_CURRENT_TEST is set during the test, so nothing is actually sent.
        assert os.environ.get("PYTEST_CURRENT_TEST")
        assert client.sent == []

        # The catalog now holds the scraped offer + the recon seed.
        conn = _conn(db)
        n_active = conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE status='active'"
        ).fetchone()[0]
        assert n_active >= 1
        conn.close()

    def test_blocked_everywhere_is_not_fatal(self) -> None:
        daemon = _load_daemon()
        db = _tmp_db()

        def all_blocked(url):
            return (403, None, "blocked")

        stats = daemon.run_cycle(
            db, max_per_cycle=5, seed=False, fetch=all_blocked,
            now_utc=FIXED_NOW,
        )
        assert stats["sites"] == len(promos.SITES)
        assert stats["blocked"] >= 1
        # Even with everything blocked, the empty-catalog seed ran, so we have
        # an active catalog and honest snapshots for every site.
        conn = _conn(db)
        n_snaps = conn.execute("SELECT COUNT(*) FROM promo_snapshots").fetchone()[0]
        assert n_snaps == len(promos.SITES)
        conn.close()
