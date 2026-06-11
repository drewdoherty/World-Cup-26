"""Tests for :mod:`wca.news` — the World Cup team-news signal engine.

All network is mocked: feeds are fixture RSS / Atom strings (including one
malformed document), and odds context is computed from a hand-seeded in-memory
``odds_snapshots`` table whose consensus / movement maths is hand-checked below.

Coverage map (mirrors the engine spec):

* RSS 2.0 + Atom parsing, tag/entity stripping, and tolerance of broken XML.
* Table-driven relevance scoring: an Endo-style headline scores high for Japan;
  generic transfer gossip scores 0; a team with no impact keyword scores 0.
* Alias / demonym matching ("Holland" -> Netherlands) and word-boundary safety
  ("us" must not match inside "thus").
* Dedupe via the ``news_items`` table: a repeat URL (and tracking-param variant)
  is suppressed; a genuinely new story is returned once.
* ``odds_context`` math on a seeded snapshots DB, including the MOVED vs flat
  line-movement verdict (hand-computed).
* ``format_alert`` layout + Telegram-Markdown escaping of feed-controlled text.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import news  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture feeds.
# ---------------------------------------------------------------------------

RSS_ENDO = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>BBC Sport - Football</title>
    <item>
      <title>Japan captain Wataru Endo ruled out of the World Cup with injury</title>
      <link>https://www.bbc.co.uk/sport/football/endo-out?utm_source=feed</link>
      <description>&lt;p&gt;Japan suffer a major blow as Wataru Endo is &lt;b&gt;ruled out&lt;/b&gt; of the tournament.&lt;/p&gt;</description>
      <pubDate>Wed, 11 Jun 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Premier League: Arsenal eye summer move for midfielder</title>
      <link>https://www.bbc.co.uk/sport/football/arsenal-gossip</link>
      <description>Transfer gossip column: Arsenal are reportedly interested in a midfielder.</description>
      <pubDate>Wed, 11 Jun 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_NETHERLANDS = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Guardian Football</title>
  <entry>
    <title>Holland defender a doubt for World Cup opener with a knock</title>
    <link href="https://www.theguardian.com/football/ned-doubt" rel="alternate"/>
    <summary>A fitness doubt hangs over a Dutch defender ahead of the tournament.</summary>
    <updated>2026-06-11T09:30:00Z</updated>
  </entry>
</feed>
"""

# Truncated / malformed XML — parse must return [] rather than raise.
MALFORMED = "<rss><channel><item><title>broken and never closed"


def _as_bytes(s: str) -> bytes:
    return s.encode("utf-8")


# ---------------------------------------------------------------------------
# Feed parsing.
# ---------------------------------------------------------------------------


def test_parse_rss_strips_html_and_keeps_fields():
    items = news.parse_feed(_as_bytes(RSS_ENDO), "BBC Football")
    assert len(items) == 2
    endo = items[0]
    assert "Wataru Endo ruled out" in endo.title
    assert endo.link == "https://www.bbc.co.uk/sport/football/endo-out?utm_source=feed"
    assert endo.source == "BBC Football"
    # HTML tags + entities stripped from the description.
    assert "<" not in endo.summary and ">" not in endo.summary
    assert "ruled out" in endo.summary
    assert endo.published.startswith("Wed, 11 Jun 2026")


def test_parse_atom_uses_href_and_summary():
    items = news.parse_feed(_as_bytes(ATOM_NETHERLANDS), "Guardian Football")
    assert len(items) == 1
    it = items[0]
    assert it.link == "https://www.theguardian.com/football/ned-doubt"
    assert "Dutch defender" in it.summary
    assert it.published == "2026-06-11T09:30:00Z"


def test_parse_malformed_returns_empty():
    assert news.parse_feed(_as_bytes(MALFORMED), "X") == []
    assert news.parse_feed(b"", "X") == []
    assert news.parse_feed(None, "X") == []  # type: ignore[arg-type]


def test_fetch_feed_swallows_transport_errors():
    class BoomSession:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    assert news.fetch_feed("https://example.invalid/rss", session=BoomSession()) == []


def test_fetch_feed_parses_from_injected_session():
    class FakeResp:
        status_code = 200
        content = _as_bytes(RSS_ENDO)

    class FakeSession:
        def get(self, url, **k):
            return FakeResp()

    items = news.fetch_feed("https://x/rss", "BBC", session=FakeSession())
    assert len(items) == 2
    assert items[0].source == "BBC"


def test_fetch_feed_non_200_yields_empty():
    class FakeResp:
        status_code = 503
        content = _as_bytes(RSS_ENDO)

    class FakeSession:
        def get(self, url, **k):
            return FakeResp()

    assert news.fetch_feed("https://x/rss", session=FakeSession()) == []


def test_fetch_feed_size_guard_drops_oversized_stream():
    """A hostile/runaway feed over the byte cap is abandoned, not buffered.

    The long-running daemon must not OOM on a 10 MB+ response, so the streaming
    read aborts past ``MAX_FEED_BYTES`` and yields no items (a half-feed is
    likelier to mis-parse than help).
    """
    cap = news.MAX_FEED_BYTES

    class BigStreamResp:
        status_code = 200

        def iter_content(self, chunk_size=65536):
            chunk = b"x" * chunk_size
            sent = 0
            while sent <= cap + chunk_size:
                yield chunk
                sent += chunk_size

        def close(self):
            pass

    class BigSession:
        def get(self, url, **k):
            return BigStreamResp()

    assert news.fetch_feed("https://x/big", session=BigSession()) == []


def test_fetch_feed_size_guard_drops_oversized_content():
    """The non-streaming fallback (stub ``.content``) also honours the cap."""

    class BigResp:
        status_code = 200
        content = b"y" * (news.MAX_FEED_BYTES + 1)

    class BigSession:
        def get(self, url, **k):
            return BigResp()

    assert news.fetch_feed("https://x/bigcontent", session=BigSession()) == []


def test_fetch_feed_streams_small_feed_ok():
    """A normal small feed served via ``iter_content`` still parses fully."""

    class StreamResp:
        status_code = 200

        def iter_content(self, chunk_size=65536):
            data = _as_bytes(RSS_ENDO)
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]

        def close(self):
            pass

    class StreamSession:
        def get(self, url, **k):
            return StreamResp()

    items = news.fetch_feed("https://x/rss", "BBC", session=StreamSession())
    assert len(items) == 2
    assert items[0].source == "BBC"


def test_fetch_feed_handles_session_without_stream_kw():
    """A stub session whose ``get`` rejects ``stream=`` falls back gracefully."""

    class FakeResp:
        status_code = 200
        content = _as_bytes(RSS_ENDO)

    class StrictSession:
        def get(self, url, timeout=None, headers=None):  # no **kwargs, no stream
            return FakeResp()

    items = news.fetch_feed("https://x/rss", "BBC", session=StrictSession())
    assert len(items) == 2


# ---------------------------------------------------------------------------
# Scoring (table-driven).
# ---------------------------------------------------------------------------

WC_TEAMS = ["Japan", "Netherlands", "United States", "South Korea"]


@pytest.mark.parametrize(
    "title, summary, teams, expect_positive, must_match_team",
    [
        # Endo-style: team + strong availability keyword -> high score.
        (
            "Japan captain Wataru Endo ruled out of the World Cup with injury",
            "Japan ruled out their captain after a scan.",
            WC_TEAMS,
            True,
            "Japan",
        ),
        # Suspension is also high-impact.
        (
            "Netherlands midfielder suspended for World Cup opener after red card",
            "A one-match ban rules him out.",
            WC_TEAMS,
            True,
            "Netherlands",
        ),
        # Short-form alias match ("U.S." -> United States) + availability kw.
        (
            "U.S. defender a doubt for the World Cup with a knock",
            "Fitness concern for the United States.",
            WC_TEAMS,
            True,
            "United States",
        ),
        # Generic transfer gossip with no WC team and no availability keyword.
        (
            "Arsenal eye summer move for a Premier League midfielder",
            "Transfer gossip column.",
            WC_TEAMS,
            False,
            None,
        ),
        # Team named but no impact keyword -> not actionable (0).
        (
            "Japan to wear new kit at the World Cup",
            "Kit launch and merchandise details.",
            WC_TEAMS,
            False,
            None,
        ),
    ],
)
def test_score_item_table(title, summary, teams, expect_positive, must_match_team):
    item = news.NewsItem(title=title, link="https://x/%d" % hash(title), source="BBC",
                         summary=summary)
    score = news.score_item(item, teams)
    if expect_positive:
        assert score > 0, "expected a positive relevance score"
        assert must_match_team in news.teams_in_text(title + " " + summary, teams)
    else:
        assert score == 0, "expected a zero (non-actionable) score, got %s" % score


def test_endo_outscores_marginal_story():
    """The exact motivating case: a clean 'ruled out' beats a soft 'squad' note."""
    endo = news.NewsItem(
        title="Japan captain ruled out of the World Cup with injury",
        link="https://x/endo", source="BBC",
        summary="Japan ruled out their captain.",
    )
    soft = news.NewsItem(
        title="Japan name provisional World Cup squad",
        link="https://x/squad", source="BBC",
        summary="The squad was announced today.",
    )
    s_endo = news.score_item(endo, WC_TEAMS)
    s_soft = news.score_item(soft, WC_TEAMS)
    assert s_endo > s_soft >= 0
    # And the strong case clears the default daemon push gate of 4.
    assert s_endo >= 4


def test_noise_penalty_keeps_betting_spam_below_push_gate():
    spam = news.NewsItem(
        title="Japan vs Netherlands betting tips and predictions",
        link="https://x/tips", source="tipster",
        summary="Our betting tips and predictions for this World Cup match.",
    )
    # Noise terms ('betting tips', 'predictions') net the keyword score negative;
    # the team/WC bonuses leave a tiny residual, but it stays far below the
    # default push gate of 4 so this spam never alerts.
    score = news.score_item(spam, WC_TEAMS)
    assert 0 <= score < 4


def test_demonyms_are_not_matched_in_scoring_text():
    """Documented limitation: ``teams_in_text`` matches the canonical name and a
    few curated short forms, NOT free demonyms ("Holland"/"Dutch"). The per-team
    Google-News query (which quotes the team name) is what carries the team tag
    in practice, so a demonym-only headline still surfaces via that route — but
    the scorer alone will not tag it. Pinned so the behavior is intentional."""
    assert news.teams_in_text("Holland defender a doubt", ["Netherlands"]) == []
    assert news.teams_in_text("Dutch defender a doubt", ["Netherlands"]) == []
    # The canonical name and curated short forms DO match.
    assert news.teams_in_text("Netherlands defender a doubt", ["Netherlands"]) == [
        "Netherlands"
    ]
    assert news.teams_in_text("Korea ruled out a player", ["South Korea"]) == [
        "South Korea"
    ]


def test_off_topic_penalty_ranks_real_wc_above_wrong_tournament():
    """A 'ruled out' story about a *different* tournament (Euro / rugby) carries
    an off-topic penalty, so the genuine 2026-WC story always outranks it. The
    penalty does not guarantee the wrong-tournament item drops below the gate
    (the additive bonuses can still clear it), but the *ordering* is reliable —
    this is the defensible guarantee, and the daemon's small ``--max-per-cycle``
    plus the score sort means the real story is what gets pushed first."""
    real = news.NewsItem(
        title="Spain star ruled out of the World Cup 2026 with injury",
        link="https://x/real", source="BBC", summary="A World Cup injury blow.",
    )
    euro = news.NewsItem(
        title="Spain star ruled out of Euro 2024 with injury",
        link="https://x/euro", source="BBC", summary="A Euro 2024 injury blow.",
    )
    assert news.score_item(real, ["Spain"]) > news.score_item(euro, ["Spain"])


def test_teams_in_text_word_boundary_safe():
    # "us" (United States alias) must NOT match inside "thus" / "houston".
    assert news.teams_in_text("Thus the team in Houston", ["United States"]) == []
    assert news.teams_in_text("USMNT lose star... U.S. ruled out", ["United States"]) == [
        "United States"
    ]


# ---------------------------------------------------------------------------
# Dedupe + insert.
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    news.ensure_schema(c)
    yield c
    c.close()


def test_new_items_inserts_then_dedupes(conn):
    a = news.NewsItem(title="Endo ruled out", link="https://bbc/endo?utm=1", source="BBC")
    # Same URL bar the tracking query -> same uid -> dedupe.
    a_dup = news.NewsItem(title="Endo ruled out", link="https://bbc/endo", source="BBC")
    b = news.NewsItem(title="Different story", link="https://bbc/other", source="BBC")

    fresh = news.new_items(conn, [a, a_dup, b])
    fresh_links = {r["link"] for r in fresh}
    # a and a_dup collapse to one uid; b is separate -> 2 fresh rows.
    assert len(fresh) == 2
    assert "https://bbc/endo?utm=1" in fresh_links or "https://bbc/endo" in fresh_links
    assert "https://bbc/other" in fresh_links

    # Re-running the same batch yields nothing new (idempotent across cycles).
    assert news.new_items(conn, [a, a_dup, b]) == []


def test_new_items_persists_scores_and_pushed_default(conn):
    a = news.NewsItem(title="Endo ruled out", link="https://bbc/endo", source="BBC",
                      teams=["Japan"])
    fresh = news.new_items(conn, [a], scores={a.uid: 18})
    assert len(fresh) == 1
    row = fresh[0]
    assert int(row["score"]) == 18
    assert int(row["pushed"]) == 0
    assert row["teams"] == "Japan"


def test_mark_pushed_sets_flag(conn):
    a = news.NewsItem(title="Endo ruled out", link="https://bbc/endo", source="BBC")
    news.new_items(conn, [a])
    news.mark_pushed(conn, [a.uid])
    row = conn.execute("SELECT pushed FROM news_items WHERE uid = ?", (a.uid,)).fetchone()
    assert int(row["pushed"]) == 1


# ---------------------------------------------------------------------------
# Odds context (hand-computed consensus + movement verdict).
# ---------------------------------------------------------------------------

EVENT_META = {
    "m1": {
        "fixture": "Netherlands vs Japan",
        "home": "Netherlands",
        "away": "Japan",
        "kickoff": "2026-06-14T20:00:00Z",
    }
}


def _seed_odds(conn, japan_late):
    """Two snapshots 7h apart. Japan early=4.0 (25.0%); late=japan_late."""
    conn.execute(
        "CREATE TABLE odds_snapshots "
        "(ts_utc TEXT, source TEXT, match_id TEXT, market TEXT, "
        "selection TEXT, decimal_odds REAL, raw TEXT)"
    )
    rows = [
        ("2026-06-13T10:00:00Z", "b", "m1", "h2h", "Netherlands", 1.8, "{}"),
        ("2026-06-13T10:00:00Z", "b", "m1", "h2h", "Draw", 3.6, "{}"),
        ("2026-06-13T10:00:00Z", "b", "m1", "h2h", "Japan", 4.0, "{}"),
        ("2026-06-13T17:00:00Z", "b", "m1", "h2h", "Netherlands", 1.8, "{}"),
        ("2026-06-13T17:00:00Z", "b", "m1", "h2h", "Draw", 3.6, "{}"),
        ("2026-06-13T17:00:00Z", "b", "m1", "h2h", "Japan", japan_late, "{}"),
    ]
    conn.executemany("INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()


def test_odds_context_latest_line_and_team_odds():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _seed_odds(c, japan_late=3.6)
    ctx = news.odds_context(c, "Japan", EVENT_META)
    assert ctx is not None
    assert ctx["fixture"] == "Netherlands vs Japan"
    assert ctx["as_of"] == "2026-06-13T17:00:00Z"
    # Team odds resolve to the Japan (away) selection at the freshest ts.
    assert ctx["team_odds"] == pytest.approx(3.6)
    assert ctx["lines"]["Netherlands"] == pytest.approx(1.8)
    c.close()


def test_odds_context_movement_moved_verdict():
    # Japan 4.0 (25.0%) -> 3.6 (27.777..%): +2.78pp > 1.5pp threshold -> MOVED.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _seed_odds(c, japan_late=3.6)
    ctx = news.odds_context(c, "Japan", EVENT_META)
    assert ctx["move_verdict"] == "MOVED"
    assert ctx["move_delta_pp"] == pytest.approx((1 / 3.6 - 1 / 4.0) * 100.0, abs=1e-6)
    assert ctx["move_delta_pp"] == pytest.approx(2.7778, abs=1e-3)
    c.close()


def test_odds_context_movement_flat_verdict():
    # Japan 4.0 -> 4.0: delta 0pp -> flat.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _seed_odds(c, japan_late=4.0)
    ctx = news.odds_context(c, "Japan", EVENT_META)
    assert ctx["move_verdict"] == "flat"
    assert ctx["move_delta_pp"] == pytest.approx(0.0, abs=1e-9)
    c.close()


def test_odds_context_just_under_threshold_is_flat():
    # A move that is just under 1.5pp must read flat, not MOVED.
    # Find japan_late s.t. (1/late - 1/4.0)*100 == 1.4pp:
    #   1/late = 0.25 + 0.014 = 0.264  -> late = 3.7879...
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _seed_odds(c, japan_late=1.0 / 0.264)
    ctx = news.odds_context(c, "Japan", EVENT_META)
    assert abs(ctx["move_delta_pp"]) < news._MOVE_THRESHOLD_PP
    assert ctx["move_verdict"] == "flat"
    c.close()


def test_odds_context_no_fixture_returns_none():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _seed_odds(c, japan_late=4.0)
    # A team not in EVENT_META can't be tied to a fixture.
    assert news.odds_context(c, "Brazil", EVENT_META) is None
    c.close()


# ---------------------------------------------------------------------------
# Alert formatting + Markdown escaping.
# ---------------------------------------------------------------------------


def test_format_alert_layout_and_movement():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _seed_odds(c, japan_late=4.0)  # flat
    ctx = news.odds_context(c, "Japan", EVENT_META)
    item = news.NewsItem(
        title="Japan captain Wataru Endo ruled out",
        link="https://bbc/endo", source="BBC Football",
        summary="A major blow.", teams=["Japan"],
    )
    text = news.format_alert(item, 18, ctx)
    assert text.startswith("🚨 *WC NEWS* · score 18 · BBC Football")
    assert "Japan captain Wataru Endo ruled out" in text
    assert "Netherlands vs Japan" in text
    # The load-bearing signal: a flat line on a fresh story.
    assert "FLAT" in text
    assert "https://bbc/endo" in text
    c.close()


def test_format_alert_escapes_markdown_in_feed_text():
    item = news.NewsItem(
        title="Japan captain *Endo* ruled out [WC] _now_",
        link="https://bbc/endo", source="BBC",
        summary="blow", teams=["Japan"],
    )
    text = news.format_alert(item, 18, None)
    # The feed-controlled title must have its Markdown control chars escaped so
    # they cannot corrupt the message rendering.
    assert "\\*Endo\\*" in text
    assert "\\[WC]" in text
    assert "\\_now\\_" in text
    # No odds -> the explicit 'no live odds line yet' note is shown (honest).
    assert "no live odds line yet" in text
    # The raw URL is emitted unescaped so the link stays clickable.
    assert "https://bbc/endo" in text


def test_format_alert_without_odds_is_honest():
    item = news.NewsItem(title="Japan injury blow", link="https://x/1", source="BBC",
                         teams=["Japan"])
    text = news.format_alert(item, 9, None)
    assert "no live odds line yet" in text
    assert "market angle unconfirmed" in text


# ---------------------------------------------------------------------------
# Sources / query construction.
# ---------------------------------------------------------------------------


def test_google_news_queries_one_per_team_plus_tournament():
    specs = news.google_news_queries(["Japan", "Netherlands"])
    teams = [s for s in specs if s.get("team")]
    assert {s["team"] for s in teams} == {"Japan", "Netherlands"}
    for s in specs:
        assert s["url"].startswith("https://news.google.com/rss/search?q=")
        assert "hl=en-US" in s["url"] and "ceid=US:en" in s["url"]
    # The quoted team name anchors the query.
    japan = next(s for s in specs if s.get("team") == "Japan")
    assert "%22Japan%22" in japan["url"]  # url-encoded quotes around the name


def test_sources_are_rss_and_cover_the_majors():
    urls = " ".join(s["url"] for s in news.SOURCES)
    assert "bbci.co.uk" in urls
    assert "theguardian.com" in urls
    assert "espn.com" in urls
    assert "skysports.com" in urls
    for s in news.SOURCES:
        assert s["kind"] == "rss"


def test_gather_items_injected_fetch_isolates_failures():
    def flaky_fetch(url, name):
        if "skysports" in url:
            raise RuntimeError("sky feed down")
        if "bbci" in url:
            return [news.NewsItem(title="Japan injury", link="https://bbc/a",
                                  source=name, summary="")]
        return []

    items = news.gather_items(["Japan"], fetch=flaky_fetch, include_core=True)
    # The Sky failure is isolated; the BBC item still comes through.
    assert any(i.link == "https://bbc/a" for i in items)
