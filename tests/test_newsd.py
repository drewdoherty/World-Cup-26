"""Tests for the World Cup news/injury alert engine (``wca.news``) and the
alert daemon (``scripts/wca_newsd.py``).

Everything is offline and deterministic:

* feed fetching is replaced by an injected ``fake_fetch`` returning canned
  RSS-shaped items — no network,
* the DB is an isolated temp SQLite seeded with the real ledger helpers + a
  hand-written ``odds_snapshots`` row so the schema matches production,
* the Telegram client is a recording stub,
* every cycle test exercises the ``PYTEST_CURRENT_TEST`` guard (it is always
  set under pytest) and, where it must assert real sends, deletes the env var
  for the duration.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import news  # noqa: E402
from wca.ledger.store import record_bet  # noqa: E402

# Load the daemon script as a module.
_SCRIPT = os.path.join(REPO_ROOT, "scripts", "wca_newsd.py")
_spec = importlib.util.spec_from_file_location("wca_newsd", _SCRIPT)
newsd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(newsd)


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _rss(items, pubdate="Wed, 11 Jun 2026 18:00:00 GMT"):
    """Build a tiny RSS-2.0 document from (title, link, desc) tuples."""
    body = "".join(
        "<item><title>%s</title><link>%s</link>"
        "<description>%s</description><pubDate>%s</pubDate></item>"
        % (t, l, d, pubdate)
        for (t, l, d) in items
    )
    xml = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Test</title>%s</channel></rss>" % body
    )
    return xml.encode("utf-8")


class FakeResp:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status


def _seed_odds(db_path, match_id="MID1"):
    """Insert one h2h fixture's latest odds into odds_snapshots."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS odds_snapshots ("
        "ts_utc TEXT, source TEXT, match_id TEXT, market TEXT, "
        "selection TEXT, decimal_odds REAL, raw TEXT)"
    )
    ts = "2026-06-11T17:30:00+00:00"
    for sel, odd in (("Japan", 2.40), ("Draw", 3.10), ("Netherlands", 2.80)):
        conn.execute(
            "INSERT INTO odds_snapshots "
            "(ts_utc, source, match_id, market, selection, decimal_odds, raw) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, "theoddsapi", match_id, "h2h", sel, odd, "{}"),
        )
    conn.commit()
    conn.close()


_META = {
    "MID1": {
        "fixture": "Netherlands vs Japan",
        "home": "Netherlands",
        "away": "Japan",
        "kickoff": "2026-06-11T18:00:00+00:00",
    }
}


class RecordingClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return {"ok": True}


# ===========================================================================
# engine: scoring
# ===========================================================================


def test_score_high_for_ruled_out_named_team():
    item = news.NewsItem(
        title="Wataru Endo ruled out of the World Cup as Japan captain withdraws",
        link="http://x/endo",
        source="GoogleNews:Japan",
        summary="Japan suffer a major injury blow before their World Cup opener.",
    )
    score = news.score_item(item, ["Japan", "Netherlands"])
    # 'ruled out' + 'withdraw' + 'injury blow' + team(Japan) + wc-context -> high
    assert score >= 8


def test_score_low_for_irrelevant_or_noise():
    item = news.NewsItem(
        title="How to watch World Cup 2026: TV channel and betting tips",
        link="http://x/tv",
        source="BBC Football",
        summary="Ticket info and fantasy predictions for the tournament.",
    )
    # noise penalties dominate -> floored at 0, below the default min-score 4
    assert news.score_item(item, ["Japan"]) < 4


def test_off_topic_tournament_penalised():
    # A "ruled out" story that is actually Euro 2024 / cricket must not clear
    # the push gate just because it names a WC team (Scotland here).
    euro = news.NewsItem(
        title="Kieran Tierney: Scotland defender ruled out of Euro 2024 with injury",
        link="http://x/tierney", source="GoogleNews:Scotland",
        summary="Scotland injury blow ruled out withdraws.",
    )
    cricket = news.NewsItem(
        title="Injury blow for Pakistan as Fakhar Zaman ruled out of World Cup",
        link="http://x/zaman", source="GoogleNews:tournament",
        summary="Cricket World Cup squad ruled out injured.",
    )
    assert news.score_item(euro, ["Scotland"]) < 4
    assert news.score_item(cricket, ["England"]) < 4


def test_parse_published_rfc822_and_iso():
    import datetime as dt

    d1 = news.parse_published("Wed, 11 Jun 2026 18:00:00 GMT")
    assert d1 == dt.datetime(2026, 6, 11, 18, 0, tzinfo=dt.timezone.utc)
    d2 = news.parse_published("2026-06-11T18:00:00Z")
    assert d2 == dt.datetime(2026, 6, 11, 18, 0, tzinfo=dt.timezone.utc)
    assert news.parse_published("not a date") is None
    assert news.parse_published("") is None


def test_team_bonus_capped():
    # Many teams named shouldn't let a weak story run away.
    title = "Japan, Netherlands, Spain, Brazil all in action at the World Cup"
    item = news.NewsItem(title=title, link="http://x/m", source="s")
    s = news.score_item(item, ["Japan", "Netherlands", "Spain", "Brazil"])
    # team bonus capped at 2*2=4, + wc context 1, no keyword signal -> 5
    assert s <= 5


# ===========================================================================
# engine: feed parsing & queries
# ===========================================================================


def test_parse_feed_rss():
    xml = _rss([("Endo out", "http://x/1", "desc one"),
                ("Squad named", "http://x/2", "desc two")])
    items = news.parse_feed(xml, "src")
    assert [i.title for i in items] == ["Endo out", "Squad named"]
    assert items[0].link == "http://x/1"
    assert items[0].source == "src"


def test_parse_feed_malformed_returns_empty():
    assert news.parse_feed(b"<not xml", "src") == []
    assert news.parse_feed(b"", "src") == []


def test_google_news_queries_one_per_team_plus_tournament():
    specs = news.google_news_queries(["Japan", "Spain"])
    team_specs = [s for s in specs if s["team"]]
    assert {s["team"] for s in team_specs} == {"Japan", "Spain"}
    assert all(s["url"].startswith("https://news.google.com/rss/search?q=") for s in specs)
    # tournament-wide queries appended (team == "")
    assert any(s["team"] == "" for s in specs)


def test_fetch_feed_isolates_transport_error():
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    assert news.fetch_feed("http://x", "src", session=Boom()) == []


def test_fetch_feed_non_200_returns_empty():
    class S:
        def get(self, *a, **k):
            return FakeResp(b"<rss/>", status=503)

    assert news.fetch_feed("http://x", "src", session=S()) == []


# ===========================================================================
# engine: dedupe / insert
# ===========================================================================


def test_new_items_dedupes(tmp_path):
    db = str(tmp_path / "n.db")
    conn = news.connect(db)
    items = [
        news.NewsItem("A", "http://x/a?utm=1", "s"),
        news.NewsItem("B", "http://x/b", "s"),
    ]
    first = news.new_items(conn, items, scores={items[0].uid: 5, items[1].uid: 2})
    assert len(first) == 2
    # Re-inserting the same items (even with tracking-param drift) -> nothing new.
    again = news.new_items(
        conn,
        [news.NewsItem("A", "http://x/a?utm=99", "s"),
         news.NewsItem("B", "http://x/b", "s")],
    )
    assert again == []
    conn.close()


def test_mark_pushed_persists(tmp_path):
    db = str(tmp_path / "n.db")
    conn = news.connect(db)
    it = news.NewsItem("A", "http://x/a", "s")
    rows = news.new_items(conn, [it], scores={it.uid: 9})
    assert int(rows[0]["pushed"]) == 0
    news.mark_pushed(conn, [it.uid])
    row = conn.execute("SELECT pushed FROM news_items WHERE uid=?", (it.uid,)).fetchone()
    assert int(row["pushed"]) == 1
    conn.close()


# ===========================================================================
# engine: odds context + format
# ===========================================================================


def test_odds_context_finds_team_line(tmp_path):
    db = str(tmp_path / "o.db")
    _seed_odds(db, match_id="MID1")
    ctx = news.odds_context(db, "Japan", _META)
    assert ctx is not None
    assert ctx["team_odds"] == 2.40
    assert ctx["fixture"] == "Netherlands vs Japan"
    assert set(ctx["lines"]) == {"Japan", "Draw", "Netherlands"}


def test_odds_context_none_when_no_fixture(tmp_path):
    db = str(tmp_path / "o.db")
    _seed_odds(db, match_id="MID1")
    assert news.odds_context(db, "Spain", _META) is None


def test_format_alert_includes_odds_and_link():
    item = news.NewsItem(
        title="Endo ruled out",
        link="http://x/endo",
        source="GoogleNews:Japan",
        summary="Captain injured.",
        teams=["Japan"],
    )
    odds = {
        "fixture": "Netherlands vs Japan",
        "kickoff": "2026-06-11T18:00:00+00:00",
        "team": "Japan",
        "team_odds": 2.40,
        "lines": {"Japan": 2.40, "Draw": 3.10, "Netherlands": 2.80},
        "as_of": "2026-06-11T17:30:00+00:00",
    }
    text = news.format_alert(item, 9, odds)
    assert "Endo ruled out" in text
    assert "2.40" in text and "Netherlands vs Japan" in text
    assert "http://x/endo" in text
    assert "score 9" in text


def test_format_alert_states_no_odds_plainly():
    item = news.NewsItem("Endo ruled out", "http://x/e", "s", teams=["Japan"])
    text = news.format_alert(item, 8, None)
    assert "no live odds line yet" in text


# ===========================================================================
# daemon: teams of interest
# ===========================================================================


def test_teams_from_meta_horizon(tmp_path):
    import datetime as dt

    now = dt.datetime(2026, 6, 11, 12, 0, tzinfo=dt.timezone.utc)
    meta = {
        "A": {"home": "Japan", "away": "Netherlands",
              "kickoff": "2026-06-11T18:00:00+00:00"},   # +6h -> in
        "B": {"home": "Spain", "away": "Brazil",
              "kickoff": "2026-06-20T18:00:00+00:00"},   # +9d -> out (72h horizon)
    }
    teams = newsd.teams_from_meta(meta, horizon_h=72, now=now)
    assert teams == {"Japan", "Netherlands"}


def test_teams_from_open_bets_reads_ledger(tmp_path):
    db = str(tmp_path / "led.db")
    record_bet("2026-06-11T10:00:00", "WC_JPN_NED", "Netherlands vs Japan",
               "h2h", "Japan", "virginbet", 2.4, 5.0, db_path=db)
    record_bet("2026-06-11T10:00:00", "WC_X", "Some settled match",
               "h2h", "France", "virginbet", 2.0, 5.0, db_path=db)
    # settle the France one so only the open bets count
    from wca.ledger.store import settle_bet

    settle_bet(2, "lost", db_path=db)
    teams = newsd.teams_from_open_bets(db)
    assert "Japan" in teams and "Netherlands" in teams
    assert "France" not in teams


# ===========================================================================
# daemon: full cycle
# ===========================================================================


def _fake_fetch_factory(by_url=None, raises_for=None):
    """Return a fetch(url, name) that yields canned items / raises per-source."""
    raises_for = raises_for or set()

    def fake_fetch(url, name=""):
        if any(tok in url for tok in raises_for):
            raise RuntimeError("dead feed: %s" % name)
        # One strong Endo story for any Japan-targeted google query; a noise
        # item for the core BBC feed.
        if "news.google.com" in url and "Japan" in url:
            return news.parse_feed(
                _rss([("Wataru Endo ruled out of World Cup, Japan captain withdraws injured",
                       "http://x/endo", "Major injury blow ruled out suspension")]),
                name,
            )
        if "bbc" in url:
            return news.parse_feed(
                _rss([("How to watch: TV channel, betting tips and fantasy",
                       "http://x/tv", "tickets predictions")]),
                name,
            )
        return []

    return fake_fetch


def _cycle_db(tmp_path):
    db = str(tmp_path / "cycle.db")
    # open bet on Japan so it's a team of interest even without fixtures
    record_bet("2026-06-11T10:00:00", "WC_JPN_NED", "Netherlands vs Japan",
               "h2h", "Japan", "virginbet", 2.4, 5.0, db_path=db)
    _seed_odds(db, match_id="MID1")
    return db


def test_cycle_inserts_but_never_sends_under_pytest(tmp_path, monkeypatch):
    # PYTEST_CURRENT_TEST is set -> guard must prevent any send and any mark.
    db = _cycle_db(tmp_path)
    monkeypatch.setattr(newsd, "robust_event_meta", lambda *a, **k: _META, raising=False)
    # robust_event_meta is looked up via wca.linemove inside the daemon; patch there.
    import wca.linemove as lm
    monkeypatch.setattr(lm, "robust_event_meta", lambda *a, **k: _META)

    client = RecordingClient()
    stats = newsd.run_cycle(
        db, min_score=4, horizon_h=72, max_per_cycle=5,
        client=client, chat_id="123", fetch=_fake_fetch_factory(),
    )
    assert stats["new"] >= 1
    assert stats["eligible"] >= 1          # the Endo story scores high
    assert stats["pushed"] == 0            # guard blocked the send
    assert client.sent == []               # nothing went out
    # and the item is NOT marked pushed (nothing was actually sent)
    conn = sqlite3.connect(db)
    pushed = conn.execute(
        "SELECT pushed FROM news_items WHERE link='http://x/endo'"
    ).fetchone()[0]
    conn.close()
    assert pushed == 0


def test_cycle_pushes_eligible_and_marks(tmp_path, monkeypatch):
    db = _cycle_db(tmp_path)
    import wca.linemove as lm
    monkeypatch.setattr(lm, "robust_event_meta", lambda *a, **k: _META)
    # Disable the pytest guard so the real send path runs against our stub.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    client = RecordingClient()
    stats = newsd.run_cycle(
        db, min_score=4, horizon_h=72, max_per_cycle=5,
        client=client, chat_id="999", fetch=_fake_fetch_factory(),
    )
    assert stats["pushed"] >= 1
    assert len(client.sent) == stats["pushed"]
    chat, text = client.sent[0]
    assert chat == "999"
    assert "Endo" in text and "2.40" in text   # alert carries odds context

    # marked pushed -> a second identical cycle pushes nothing new
    client2 = RecordingClient()
    stats2 = newsd.run_cycle(
        db, min_score=4, horizon_h=72, max_per_cycle=5,
        client=client2, chat_id="999", fetch=_fake_fetch_factory(),
    )
    assert stats2["pushed"] == 0
    assert client2.sent == []


def test_cycle_only_pings_material_events(tmp_path, monkeypatch):
    # Gating is now MATERIAL-based, not score-based: a non-material story
    # (soft chatter) is still scraped + stored but never pinged.
    db = _cycle_db(tmp_path)
    import wca.linemove as lm
    monkeypatch.setattr(lm, "robust_event_meta", lambda *a, **k: _META)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def soft_fetch(url, name=""):
        if "news.google.com" in url and "Japan" in url:
            return news.parse_feed(
                _rss([("Japan boss praises squad spirit ahead of World Cup opener",
                       "http://x/soft", "fitness looks good, no fresh injury")]),
                name,
            )
        return []

    client = RecordingClient()
    stats = newsd.run_cycle(
        db, min_score=4, horizon_h=72, max_per_cycle=5,
        client=client, chat_id="1", fetch=soft_fetch,
    )
    assert stats["new"] >= 1          # stored
    assert stats["material"] == 0     # not a material squad change
    assert stats["pushed"] == 0       # so never pinged
    assert client.sent == []


def test_cycle_respects_cap_and_logs_overflow(tmp_path, monkeypatch, capsys):
    db = _cycle_db(tmp_path)
    import wca.linemove as lm
    monkeypatch.setattr(lm, "robust_event_meta", lambda *a, **k: _META)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    # Fetch that returns several high-scoring Japan stories.
    def many_fetch(url, name=""):
        if "news.google.com" in url and "Japan" in url:
            return news.parse_feed(
                _rss([
                    ("Japan star A ruled out of World Cup injured withdraws", "http://x/a", "injury blow ruled out"),
                    ("Japan star B suspended banned for World Cup", "http://x/b", "suspension banned red card"),
                    ("Japan star C ruled out of World Cup injured withdraws", "http://x/c", "world cup injury blow ruled out"),
                ]),
                name,
            )
        return []

    client = RecordingClient()
    stats = newsd.run_cycle(
        db, min_score=4, horizon_h=72, max_per_cycle=2,
        client=client, chat_id="1", fetch=many_fetch,
    )
    assert stats["eligible"] >= 3
    assert stats["pushed"] == 2          # capped
    assert stats["overflow"] >= 1
    out = capsys.readouterr().out
    assert "exceed cap" in out


def test_cycle_isolates_dead_feed(tmp_path, monkeypatch):
    db = _cycle_db(tmp_path)
    import wca.linemove as lm
    monkeypatch.setattr(lm, "robust_event_meta", lambda *a, **k: _META)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    # Every core feed raises; google-news still delivers the Endo story.
    fetch = _fake_fetch_factory(raises_for={"bbc", "guardian", "espn", "skysports"})
    client = RecordingClient()
    stats = newsd.run_cycle(
        db, min_score=4, horizon_h=72, max_per_cycle=5,
        client=client, chat_id="1", fetch=fetch,
    )
    # Despite the dead core feeds, the Japan google story still pushed.
    assert stats["pushed"] >= 1


def test_cycle_freshness_gate_suppresses_stale(tmp_path, monkeypatch):
    db = _cycle_db(tmp_path)
    import datetime as dt

    import wca.linemove as lm
    monkeypatch.setattr(lm, "robust_event_meta", lambda *a, **k: _META)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    now = dt.datetime(2026, 6, 11, 20, 0, tzinfo=dt.timezone.utc)

    def stale_fetch(url, name=""):
        if "news.google.com" in url and "Japan" in url:
            # Published a year before 'now' -> stale.
            return news.parse_feed(
                _rss([("Endo ruled out of World Cup injured withdraws",
                       "http://x/old", "injury blow ruled out")],
                     pubdate="Tue, 11 Jun 2025 18:00:00 GMT"),
                name,
            )
        return []

    client = RecordingClient()
    stats = newsd.run_cycle(
        db, min_score=4, horizon_h=72, max_per_cycle=5,
        client=client, chat_id="1", fetch=stale_fetch,
        now=now, max_age_hours=36,
    )
    # Inserted (so it dedupes) but never pushed because it's a year old.
    assert stats["new"] >= 1
    assert stats["stale"] >= 1
    assert stats["pushed"] == 0
    assert client.sent == []


def test_cycle_freshness_gate_allows_recent(tmp_path, monkeypatch):
    db = _cycle_db(tmp_path)
    import datetime as dt

    import wca.linemove as lm
    monkeypatch.setattr(lm, "robust_event_meta", lambda *a, **k: _META)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    now = dt.datetime(2026, 6, 11, 20, 0, tzinfo=dt.timezone.utc)

    def fresh_fetch(url, name=""):
        if "news.google.com" in url and "Japan" in url:
            return news.parse_feed(
                _rss([("Endo ruled out of World Cup injured withdraws",
                       "http://x/fresh", "injury blow ruled out")],
                     pubdate="Wed, 11 Jun 2026 19:30:00 GMT"),  # 30 min before now
                name,
            )
        return []

    client = RecordingClient()
    stats = newsd.run_cycle(
        db, min_score=4, horizon_h=72, max_per_cycle=5,
        client=client, chat_id="1", fetch=fresh_fetch,
        now=now, max_age_hours=36,
    )
    assert stats["pushed"] >= 1
    assert stats["stale"] == 0


def test_odds_for_item_prefers_team_named_in_text(tmp_path):
    """A Japan-withdrawal story can surface in the *United States* Google-News
    query feed, arriving tagged ``[United States, Japan]`` (query tag first).
    The odds context must pair it with the team the headline is actually about
    (Japan), not the query tag (USA) — otherwise the alert shows the wrong
    market line. Regression for the live --once mismatch."""
    db = str(tmp_path / "o.db")
    # Two fixtures: Japan's and USA's, each with odds.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE odds_snapshots (ts_utc TEXT, source TEXT, match_id TEXT, "
        "market TEXT, selection TEXT, decimal_odds REAL, raw TEXT)"
    )
    ts = "2026-06-11T17:30:00+00:00"
    for mid, sels in (
        ("JPN", (("Japan", 3.5), ("Netherlands", 1.83), ("Draw", 2.88))),
        ("USA", (("USA", 1.83), ("Paraguay", 3.75), ("Draw", 2.88))),
    ):
        for sel, odd in sels:
            conn.execute(
                "INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)",
                (ts, "src", mid, "h2h", sel, odd, "{}"),
            )
    conn.commit()
    conn.close()
    meta = {
        "JPN": {"fixture": "Netherlands vs Japan", "home": "Netherlands",
                "away": "Japan", "kickoff": "2026-06-14T20:00:00Z"},
        "USA": {"fixture": "USA vs Paraguay", "home": "United States",
                "away": "Paraguay", "kickoff": "2026-06-13T01:00:00Z"},
    }

    item = news.NewsItem(
        title="World Cup Buzz: Japan Captain Withdraws From World Cup After Injury",
        link="http://x/endo", source="GoogleNews:United States",
        summary="Japan captain out.",
        teams=["United States", "Japan"],  # query tag first, text-team second
    )
    ctx = newsd._odds_for_item(db, item, meta)
    assert ctx is not None
    assert ctx["team"] == "Japan", "expected Japan's odds, got %s" % ctx["team"]
    assert ctx["fixture"] == "Netherlands vs Japan"

    # A genuine USA story (USA named in the text) still resolves to USA.
    usa_item = news.NewsItem(
        title="United States defender ready for World Cup opener",
        link="http://x/usa", source="GoogleNews:United States",
        summary="USMNT fit.", teams=["United States"],
    )
    ctx2 = newsd._odds_for_item(db, usa_item, meta)
    assert ctx2 is not None and ctx2["team"] == "United States"

    # Query-tag-only fallback: text names no known team -> use the tag.
    tagonly = news.NewsItem(
        title="Breaking camp update", link="http://x/c",
        source="GoogleNews:United States", summary="tbd",
        teams=["United States"],
    )
    ctx3 = newsd._odds_for_item(db, tagonly, meta)
    assert ctx3 is not None and ctx3["team"] == "United States"


def test_log_redacts_bot_token(capsys):
    """A Telegram transport error stringifies the Bot API URL, which embeds the
    bot token. ``_log`` must scrub any bot-token-shaped substring so a failed
    send (or any logged exception) never leaks the token to stdout."""
    fake = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAA_secret_BBB"
    msg = (
        "send failed for abc (telegram request failed: HTTPSConnectionPool("
        "host='api.telegram.org', port=443): Max retries exceeded with url: "
        "/bot%s/sendMessage)" % fake
    )
    newsd._log(msg)
    out = capsys.readouterr().out
    assert fake not in out
    assert "redacted" in out
    # A bare token (not in a /bot URL) is also scrubbed.
    newsd._log("leak %s here" % fake)
    out2 = capsys.readouterr().out
    assert fake not in out2


def test_redact_leaves_clean_text_untouched():
    clean = "cycle done: teams=3 fetched=12 new=2 eligible=1 pushed=1"
    assert newsd._redact(clean) == clean


def test_resolve_chat_id_prefers_news_then_admin(monkeypatch):
    monkeypatch.delenv("WCA_NEWS_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111,222")
    assert newsd.resolve_chat_id() == "111"
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "777")
    assert newsd.resolve_chat_id() == "777"
    monkeypatch.setenv("WCA_NEWS_CHAT_ID", "555")
    assert newsd.resolve_chat_id() == "555"
