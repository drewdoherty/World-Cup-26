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
# fetch_page block-marker classification (2026-07-08 fix regression coverage)
#
# The bug: a bare "captcha" substring marker false-positived on ordinary SPA
# login-form i18n JSON (e.g. Unibet ships `"captchaRequired":"..."` on every
# page load — a validation-message KEY, not a challenge shown to the visitor)
# and silently discarded a genuinely scrapeable, real-content page as
# 'blocked' every single day. Fixed by requiring a full challenge PHRASE
# ("complete the captcha", "verify you are human", ...) instead of the bare
# token. These tests pin the fix + prove genuine blocks are still caught.
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "promos"


def _read_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


class _StubResponse:
    """Minimal stand-in for a ``requests.Response`` (status + .content)."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.content = text.encode("utf-8")

    def close(self) -> None:  # pragma: no cover - parity with requests.Response
        pass


class _StubSession:
    def __init__(self, status_code: int, text: str) -> None:
        self._status_code = status_code
        self._text = text

    def get(self, url, timeout=None, headers=None, stream=None):  # noqa: ANN001
        return _StubResponse(self._status_code, self._text)


class TestBlockMarkers:
    def test_login_form_captcha_json_is_not_misclassified_as_blocked(self) -> None:
        """Regression: a real page with an unrelated captcha-flavoured i18n
        key must classify 'ok' and its real offers must be extractable —
        NOT be swallowed as 'blocked' by a bare substring match."""
        html = _read_fixture("spa_with_login_i18n_captcha_key.html")
        session = _StubSession(200, html)
        status, text, fetch_status = promos.fetch_page("http://x", session=session)
        assert fetch_status == "ok", (
            "a page merely mentioning 'captcha' in an unrelated login-form "
            "i18n key must not be classified as blocked"
        )
        items = promos.extract_promos(text, "Unibet")
        titles = " || ".join(i["title"].lower() for i in items)
        assert "bet builder profit boost" in titles
        assert "free bets" in titles

    def test_genuine_cloudflare_js_challenge_is_still_blocked(self) -> None:
        """A REAL Cloudflare challenge page (the /cdn-cgi/challenge-platform/
        script injection) must still classify 'blocked' after the captcha
        marker was tightened — other markers carry the detection."""
        html = _read_fixture("cloudflare_js_challenge.html")
        session = _StubSession(200, html)
        status, text, fetch_status = promos.fetch_page("http://x", session=session)
        assert fetch_status == "blocked"

    def test_genuine_spa_shell_yields_no_offers(self) -> None:
        """A real client-rendered SPA shell classifies 'ok' (it IS a real 200
        with real bytes, no block marker hit) but must yield ZERO extracted
        offers rather than fabricating one from script/style boilerplate — this
        is the live shape of Virgin Bet / Matchbook / Ladbrokes / William Hill,
        confirmed by manual probe 2026-07-08 and recorded as ``manual_check``
        in the :data:`wca.promos.SITES` registry."""
        html = _read_fixture("spa_empty_shell.html")
        session = _StubSession(200, html)
        status, text, fetch_status = promos.fetch_page("http://x", session=session)
        assert fetch_status == "ok"
        assert promos.extract_promos(text, "X") == []

    def test_tiny_body_is_classified_empty(self) -> None:
        """A 200 with a body under the useful-length floor (e.g. a redirect
        stub or a near-blank error page) classifies 'empty', not 'ok'."""
        session = _StubSession(200, "<html><body>hi</body></html>")
        status, text, fetch_status = promos.fetch_page("http://x", session=session)
        assert fetch_status == "empty"


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
# manual_check_sites (2026-07-08: honest fallback for genuinely unscrapeable
# sources — a real probe confirmed these are pure JS-shells or sit behind an
# active bot-challenge, so the daemon still fetches them every cycle but the
# feed also surfaces them as a dated, URL'd, one-line-reason click-through list
# for the human's daily sweep instead of a silent "nothing here").
# ---------------------------------------------------------------------------


class TestManualCheckSites:
    def test_shape_and_nonempty(self) -> None:
        out = promos.manual_check_sites()
        assert out, "expected at least one manual-check source in the registry"
        for entry in out:
            assert set(entry.keys()) == {"site", "url", "reason"}
            assert entry["site"]
            assert entry["reason"], "every manual-check entry must state why"

    def test_only_flagged_sites_included(self) -> None:
        out = promos.manual_check_sites()
        flagged_names = {s["name"] for s in promos.SITES if s.get("manual_check")}
        assert {e["site"] for e in out} == flagged_names

    def test_unflagged_registry_entries_excluded(self) -> None:
        # Unibet is scrapeable (2026-07-08 fix) and must NOT be manual-check.
        out = promos.manual_check_sites()
        assert "Unibet" not in {e["site"] for e in out}
        # Polymarket has no manual_check flag set at all (expect_promos=False
        # is a different, orthogonal concept) -> also excluded.
        assert "Polymarket" not in {e["site"] for e in out}

    def test_urls_match_registry(self) -> None:
        out = {e["site"]: e["url"] for e in promos.manual_check_sites()}
        for entry in promos.SITES:
            if entry.get("manual_check"):
                assert out[entry["name"]] == entry["promos_url"]


# ---------------------------------------------------------------------------
# active_boost_promotions / graded_fingerprints_today (2026-07-08: boost_evals
# was permanently empty because only SCRAPED boost candidates ever reached the
# grading step — a seed-sourced "Power Prices" row from the recon doc never
# got graded no matter how many cycles ran, since every UK book hub is a
# JS-rendered SPA the scraper can't extract from. See scripts/wca_promosd.py
# step "2.5" for the daemon-side wiring these readers support.)
# ---------------------------------------------------------------------------


class TestActiveBoostPromotions:
    def test_returns_active_boost_rows_any_source(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        # A seed-sourced boost row (mirrors seed_from_recon's shape).
        promos._upsert_seed_row(
            conn,
            {"site": "Paddy Power", "title": "Power Prices",
             "description": "Daily enhanced odds on selected matches.",
             "promo_type": "boost", "terms": "", "url": ""},
            FIXED_NOW,
        )
        # A scraped boost row + a non-boost row, for contrast.
        promos.diff_and_upsert(
            conn, "Sky Bet",
            [{"title": "Brazil was 2/1 now 5/2", "description": "boost",
              "promo_type": "boost", "terms": "", "url": ""},
             {"title": "Bet £10 Get £30", "description": "signup-ish",
              "promo_type": "ongoing", "terms": "", "url": ""}],
            FIXED_NOW, source="scrape",
        )
        rows = promos.active_boost_promotions(conn)
        sites = {r["site"] for r in rows}
        assert sites == {"Paddy Power", "Sky Bet"}
        assert all(r["promo_type"] == "boost" for r in rows)
        conn.close()

    def test_empty_catalog_returns_empty(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        assert promos.active_boost_promotions(conn) == []
        conn.close()


class TestGradedFingerprintsToday:
    def test_dedup_key_matches_same_day_evals(self) -> None:
        db = _tmp_db()
        conn = _conn(db)
        promos.record_boost_eval(
            conn, ts_utc="2026-06-13T09:00:00", site="Paddy Power",
            fixture=None, market=None, selection="Power Prices",
            boosted_odds=None, was_odds=None, model_prob=None, fair_odds=None,
            edge=None, is_plus_ev=False, priceable=False,
            reason="could not parse boost text", source="scrape",
        )
        graded = promos.graded_fingerprints_today(conn, "2026-06-13")
        assert ("Paddy Power", "Power Prices") in graded
        # A different day's prefix must not match.
        assert promos.graded_fingerprints_today(conn, "2026-06-14") == set()
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

    def test_seed_boost_promos_get_graded_even_when_scraping_is_fully_blocked(
        self,
    ) -> None:
        """Regression for the 2026-07-08 fix: before this, ``boost_evals`` was
        permanently empty because grading only ever ran on SCRAPED boost
        candidates — a book whose live scraper never returns 'ok' (every UK
        book hub, in production) meant a seed-sourced "Power Prices" row from
        the recon doc was never graded no matter how many cycles ran. Step 2.5
        in run_cycle now grades every active boost promo (any source) once a
        day, so even a fully-blocked scrape cycle still produces an honest
        (if unpriceable) boost-eval row for a book known to run boosts."""
        daemon = _load_daemon()
        db = _tmp_db()

        def all_blocked(url):
            return (403, None, "blocked")

        stats = daemon.run_cycle(
            db, max_per_cycle=5, seed=False, fetch=all_blocked, now_utc=FIXED_NOW,
        )
        # The empty-catalog auto-seed ran, and the recon doc's "boost" rows
        # (e.g. Paddy Power's Power Prices) must now have been graded despite
        # every live scrape coming back blocked.
        assert stats["boosts_seen"] >= 1
        conn = _conn(db)
        evals = promos.recent_boost_evals(conn, limit=100)
        assert evals, "expected at least one boost_evals row from seed grading"
        # Seed rows are generic promo-mechanic descriptions (no "was X now Y"
        # instance), so the honest outcome is unpriceable with a stated reason
        # — never a fabricated price.
        assert all(not e["priceable"] for e in evals)
        assert all(e["reason"] for e in evals)
        conn.close()

    def test_seed_boost_not_regraded_twice_in_the_same_day(self) -> None:
        """A second same-day cycle must not re-insert a boost_evals row for a
        seed promo already graded earlier today (would flood the table on a
        long-running hourly daemon for a perfectly static seed description)."""
        daemon = _load_daemon()
        db = _tmp_db()

        def all_blocked(url):
            return (403, None, "blocked")

        s1 = daemon.run_cycle(
            db, max_per_cycle=5, seed=False, fetch=all_blocked, now_utc=FIXED_NOW,
        )
        conn = _conn(db)
        n1 = conn.execute("SELECT COUNT(*) FROM boost_evals").fetchone()[0]
        conn.close()
        assert n1 >= 1

        # Same day, a later timestamp -> no re-seed (catalog non-empty) and no
        # re-grading of the same static seed boosts.
        same_day_later = FIXED_NOW[:11] + "18:00:00"
        s2 = daemon.run_cycle(
            db, max_per_cycle=5, seed=False, fetch=all_blocked,
            now_utc=same_day_later,
        )
        conn = _conn(db)
        n2 = conn.execute("SELECT COUNT(*) FROM boost_evals").fetchone()[0]
        conn.close()
        assert n2 == n1, "same-day re-run must not duplicate boost_evals rows"
