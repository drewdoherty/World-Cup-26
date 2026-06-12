"""World Cup news / injury / lineup signal engine.

The motivating story: the Wataru Endo withdrawal (Japan captain ruled out)
was tradable for hours because the market hadn't moved — but it was only
caught because a friend texted. This module is the systematic version: it
continuously scans *public* sources for World Cup squad / injury / suspension
/ referee / lineup news, scores each item for relevance, dedupes against a
store, and (in the daemon) pushes high-signal items to Telegram *with* current
odds context so each alert is immediately actionable.

Honesty about sources
---------------------
* **Google News RSS** is the workhorse. Its search feed aggregates most
  reporter scoops (Romano, Ornstein, club/federation announcements, beat
  writers) within minutes and is free and reliable. We hit
  ``https://news.google.com/rss/search?q=<urlencoded>&hl=en-US&gl=US&ceid=US:en``.
* A handful of **core team/competition RSS feeds** (BBC, Guardian, ESPN) round
  out coverage and catch the editorial framing.
* **Twitter / X has no free API.** We do **not** fake it — there is no
  scraping, no unofficial endpoint, no pretend "tweets" source. Google News
  surfaces the *reporting* of a tweet (an outlet writing up Romano's "here we
  go") usually within minutes, which is what we act on. This gap is documented
  here deliberately so nobody believes we have first-party social coverage.

Public API (the shared spec — keep these signatures stable; ``wca_newsd.py``
and the tests both import them):

* ``SOURCES``            — list of core RSS source dicts.
* ``google_news_queries(teams)`` — list of Google-News RSS query specs.
* ``fetch_feed(url, ...)``       — fetch + parse one RSS feed -> list[NewsItem].
* ``score_item(item, teams)``    — relevance score (int) for an item.
* ``new_items(conn, items)``     — dedupe + insert, return only the new rows.
* ``odds_context(conn_or_db, team, event_meta)`` — current odds line for a team.
* ``format_alert(item, score, odds)`` — Telegram-ready Markdown alert string.

Everything here is stdlib + ``requests`` only. RSS is parsed with
``xml.etree.ElementTree`` (NO feedparser).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

#: User-Agent for feed fetches. Some feeds 403 a bare python-requests UA.
USER_AGENT = (
    "Mozilla/5.0 (compatible; WCA-newsbot/1.0; +https://example.invalid/wca)"
)

#: Hard cap on a single feed body we will buffer + parse (bytes). RSS/Atom feeds
#: are small (the live core feeds are well under 1 MB); a 10 MB+ response is
#: either hostile or broken. We stream the body and abort past the cap so one
#: runaway feed can't exhaust memory in the long-running daemon. 8 MB is ~10x
#: the largest legitimate feed we have seen, so it never truncates a real feed.
MAX_FEED_BYTES = 8 * 1024 * 1024

#: Core always-on RSS feeds. These are broad football feeds that catch the
#: editorial write-ups; team-specific signal comes from Google News queries.
#: ``kind`` is informational; ``url`` is fetched verbatim.
SOURCES: List[Dict[str, str]] = [
    {
        "name": "BBC Football",
        "kind": "rss",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    },
    {
        "name": "Guardian Football",
        "kind": "rss",
        "url": "https://www.theguardian.com/football/rss",
    },
    {
        "name": "ESPN Soccer",
        "kind": "rss",
        "url": "https://www.espn.com/espn/rss/soccer/news",
    },
    {
        "name": "Sky Sports Football",
        "kind": "rss",
        "url": "https://www.skysports.com/rss/12040",
    },
]

#: Google News RSS template. ``{q}`` is a url-encoded search string.
GOOGLE_NEWS_TEMPLATE = (
    "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
)

#: Terms that, combined with a team name, sharpen Google News toward the
#: actionable (squad / availability / lineup) signal we care about.
_TEAM_QUERY_TERMS: Tuple[str, ...] = (
    "injury OR injured OR doubt OR ruled out OR withdraw OR squad OR "
    "suspended OR suspension OR lineup OR \"starting XI\" OR fitness",
)

#: Tournament-wide queries (referee appointments, VAR, mass news) that aren't
#: tied to a single team.
_TOURNAMENT_QUERIES: Tuple[str, ...] = (
    "World Cup 2026 referee appointed",
    "World Cup 2026 injury squad ruled out",
    "World Cup 2026 suspension banned",
)


# ---------------------------------------------------------------------------
# Relevance keyword scoring weights
# ---------------------------------------------------------------------------

# High-value signals: an availability change to a named player/squad.
_KW_HIGH: Dict[str, int] = {
    "ruled out": 4,
    "ruled-out": 4,
    "withdraw": 4,
    "withdrawn": 4,
    "withdraws": 4,
    "out of the world cup": 4,
    "out for the tournament": 4,
    "suspended": 3,
    "suspension": 3,
    "banned": 3,
    "red card": 3,
    "injury blow": 3,
    "ruled out of": 4,
    "miss the": 2,
    "will miss": 3,
    "out injured": 3,
}

# Medium signals: uncertainty / squad churn / officials.
_KW_MED: Dict[str, int] = {
    "injury": 2,
    "injured": 2,
    "doubt": 2,
    "doubtful": 2,
    "fitness": 1,
    "fitness test": 2,
    "knock": 2,
    "strain": 2,
    "hamstring": 2,
    "calf": 1,
    "squad": 1,
    "call-up": 1,
    "call up": 1,
    "replacement": 2,
    "referee": 2,
    "officials": 1,
    "var": 1,
    "lineup": 2,
    "line-up": 2,
    "starting xi": 2,
    "team news": 2,
    "captain": 1,
    "return": 1,
    "comeback": 1,
}

# Lineup / confirmed-XI bonus phrases (most actionable right before kickoff).
_KW_LINEUP: Dict[str, int] = {
    "confirmed lineup": 3,
    "confirmed line-up": 3,
    "starting lineup": 2,
    "team to face": 2,
}

#: Words that mark sponsor/ticketing/merch/fantasy noise; subtract score.
_KW_NOISE: Dict[str, int] = {
    "ticket": 2,
    "fantasy": 2,
    "betting tips": 3,
    "predictions": 1,
    "merchandise": 2,
    "kit launch": 2,
    "watch live": 1,
    "how to watch": 2,
    "tv channel": 1,
}

#: Wrong-tournament / wrong-sport markers. A "ruled out" story about Euro 2024,
#: the Winter Olympics, the cricket/rugby World Cup, or the U20s scores high on
#: keywords but is *not* the 2026 men's WC. Heavy penalty so these never clear
#: the push gate on a team-name coincidence (e.g. "Scotland", "Canada").
_KW_OFF_TOPIC: Dict[str, int] = {
    "euro 2024": 6,
    "euro 2028": 6,
    "winter olympics": 6,
    "summer olympics": 6,
    "olympic": 4,
    "cricket": 6,
    "odi": 6,
    "t20": 6,
    " test match": 6,
    "test cricket": 6,
    "the ashes": 6,
    " ashes ": 6,
    "icc": 5,
    "wicket": 6,
    "innings": 6,
    "bowler": 5,
    "batsman": 6,
    "batter": 4,
    "all-rounder": 5,
    "county championship": 6,
    "ipl": 5,
    # High-collision cricket stars whose nations also play football WC2026
    # (England, Australia, South Africa…), so a bare-"World Cup" cricket story
    # doesn't tag the football side. Names, not generic words.
    "joe root": 6,
    "ben stokes": 6,
    "stokes suspension": 6,
    "jasprit bumrah": 6,
    "bumrah": 5,
    "virat kohli": 6,
    "rohit sharma": 6,
    "babar azam": 6,
    "jos buttler": 6,
    "rishabh pant": 6,
    # Rugby context (collides with England/France/SA/Wales/Scotland/Australia)
    "six nations": 6,
    "rugby world cup": 6,
    "fly-half": 6,
    "scrum-half": 6,
    "tennis": 6,
    "queen's club": 6,
    "wnba": 6,
    "nfl": 6,
    "nba": 6,
    "nhl": 6,
    "u20": 4,
    "u-20": 4,
    "u21": 4,
    "u-21": 4,
    "u23": 4,
    "u-23": 4,
    "women's world cup": 4,
    "womens world cup": 4,
    "club world cup": 5,
    "nations league": 4,
    "champions league": 3,
    "premier league injury": 3,
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class NewsItem:
    """A single parsed feed entry."""

    title: str
    link: str
    source: str
    summary: str = ""
    published: str = ""  # ISO-ish string as the feed gave it
    teams: List[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        """Stable dedupe key: prefers the link, falls back to title+source."""
        return item_uid(self.title, self.link, self.source)


def item_uid(title: str, link: str, source: str) -> str:
    """Deterministic short hash used as the dedupe primary key.

    The link is the strongest identity, but Google News rewrites links with
    tracking params, so we normalise: strip the query string and hash the
    (normalised-link or title) + source.
    """
    base = (link or "").split("?")[0].strip().rstrip("/")
    if not base:
        base = (title or "").strip().lower()
    key = "%s|%s" % (base, (source or "").strip().lower())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    uid         TEXT PRIMARY KEY,
    ts_utc      TEXT NOT NULL,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    link        TEXT,
    summary     TEXT,
    published   TEXT,
    teams       TEXT,
    score       INTEGER NOT NULL DEFAULT 0,
    pushed      INTEGER NOT NULL DEFAULT 0
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``news_items`` table if absent. Idempotent."""
    conn.execute(_SCHEMA)
    # `material` flags a confirmed squad change (logged whether or not it
    # produced a ping); added to pre-existing tables idempotently.
    try:
        conn.execute("ALTER TABLE news_items ADD COLUMN material INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    conn.commit()


def mark_material(conn: sqlite3.Connection, uids: Sequence[str]) -> None:
    """Flag items as material squad events (for the log / digest)."""
    if not uids:
        return
    conn.executemany(
        "UPDATE news_items SET material = 1 WHERE uid = ?", [(u,) for u in uids]
    )
    conn.commit()


def connect(db_path: str) -> sqlite3.Connection:
    """Open the news DB (same file as the ledger by default) and ensure schema."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def google_news_queries(teams: Sequence[str]) -> List[Dict[str, str]]:
    """Build the list of Google-News RSS feed specs for *teams*.

    Returns a list of ``{"name", "kind", "url", "team"}`` dicts: one focused
    query per team (team name + availability terms) plus the tournament-wide
    queries. ``team`` is the canonical team the query targets (empty for the
    tournament-wide ones) so the daemon can tag items even when the title is
    terse.
    """
    specs: List[Dict[str, str]] = []
    seen_urls = set()
    term_block = _TEAM_QUERY_TERMS[0]
    for team in teams:
        if not team:
            continue
        q = '"%s" %s' % (team, term_block)
        url = GOOGLE_NEWS_TEMPLATE.format(q=quote_plus(q))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        specs.append(
            {
                "name": "GoogleNews:%s" % team,
                "kind": "google_news",
                "url": url,
                "team": team,
            }
        )
    for q in _TOURNAMENT_QUERIES:
        url = GOOGLE_NEWS_TEMPLATE.format(q=quote_plus(q))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        specs.append(
            {
                "name": "GoogleNews:tournament",
                "kind": "google_news",
                "url": url,
                "team": "",
            }
        )
    return specs


# ---------------------------------------------------------------------------
# Feed fetching + parsing
# ---------------------------------------------------------------------------

# Namespaces seen in the feeds we hit (Atom for some, Dublin Core for dates).
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
}


def _text(elem: Optional[ET.Element]) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def _strip_html(s: str) -> str:
    """Crude HTML/entity strip for RSS ``description`` blobs."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = (
        s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", s).strip()


def parse_published(published: str) -> Optional[datetime]:
    """Parse a feed ``published`` string to an aware UTC datetime, or None.

    Handles RSS RFC-822 (``Wed, 11 Jun 2026 18:00:00 GMT``) and ISO-8601
    (Atom ``updated``/``published``). Unparseable -> ``None`` (caller decides
    how to treat unknown-age items). Never raises.
    """
    if not published:
        return None
    s = published.strip()
    # RFC-822 via email.utils (handles GMT/named zones/offsets).
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    # ISO-8601 fallback.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def parse_feed(xml_bytes: bytes, source_name: str) -> List[NewsItem]:
    """Parse RSS-2.0 or Atom feed bytes into :class:`NewsItem`s.

    Tolerant: malformed XML yields an empty list (never raises). Handles both
    RSS ``<item>`` and Atom ``<entry>`` shapes.
    """
    if not xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    items: List[NewsItem] = []

    # RSS 2.0: channel/item
    for it in root.iter("item"):
        title = _text(it.find("title"))
        link = _text(it.find("link"))
        summary = _strip_html(_text(it.find("description")))
        published = _text(it.find("pubDate")) or _text(it.find("dc:date", _NS))
        # Google News nests the real source in <source>; keep our label.
        if title or link:
            items.append(
                NewsItem(
                    title=title,
                    link=link,
                    source=source_name,
                    summary=summary,
                    published=published,
                )
            )

    if items:
        return items

    # Atom: entry/link[@href]
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = _text(entry.find("atom:title", _NS))
        link = ""
        link_el = entry.find("atom:link", _NS)
        if link_el is not None:
            link = link_el.get("href", "") or _text(link_el)
        summary = _strip_html(
            _text(entry.find("atom:summary", _NS))
            or _text(entry.find("atom:content", _NS))
        )
        published = _text(entry.find("atom:updated", _NS)) or _text(
            entry.find("atom:published", _NS)
        )
        if title or link:
            items.append(
                NewsItem(
                    title=title,
                    link=link,
                    source=source_name,
                    summary=summary,
                    published=published,
                )
            )
    return items


def fetch_feed(
    url: str,
    source_name: str = "",
    timeout: float = 12.0,
    session: Any = None,
) -> List[NewsItem]:
    """Fetch one RSS/Atom feed and parse it into :class:`NewsItem`s.

    Network and parse errors are swallowed and surfaced as an empty list — the
    daemon isolates per-source failures, so one dead feed never kills a cycle.
    ``requests`` is imported lazily so this module parses standalone.
    """
    import requests  # lazy: keep module import-light / standalone-parseable

    name = source_name or url
    sess = session or requests
    try:
        resp = sess.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
            stream=True,
        )
    except TypeError:
        # An injected stub session may not accept ``stream=`` — retry plainly.
        try:
            resp = sess.get(
                url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
            )
        except Exception:  # noqa: BLE001 - transport error -> no items
            return []
    except Exception:  # noqa: BLE001 - transport error -> no items
        return []
    if getattr(resp, "status_code", 200) != 200:
        return []
    content = _read_capped(resp)
    if isinstance(content, str):
        content = content.encode("utf-8")
    return parse_feed(content, name)


def _read_capped(resp: Any, max_bytes: int = MAX_FEED_BYTES) -> bytes:
    """Read a response body but never buffer more than *max_bytes*.

    Prefers streaming via ``iter_content`` so a hostile/oversized feed is
    abandoned without materialising the whole body in memory; falls back to a
    plain ``.content`` read (and a hard slice) for stub responses in tests that
    don't implement streaming. Over-cap real feeds are dropped (``b""``) rather
    than truncated, since a half-feed is more likely to mis-parse than help.
    """
    iter_content = getattr(resp, "iter_content", None)
    if callable(iter_content):
        try:
            buf = bytearray()
            for chunk in iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    # Oversized: stop reading and discard. Close to free the
                    # socket promptly (best-effort).
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return b""
            return bytes(buf)
        except Exception:  # noqa: BLE001 - streaming hiccup -> fall back below
            pass
    content = getattr(resp, "content", b"") or b""
    if isinstance(content, str):
        content = content.encode("utf-8")
    if len(content) > max_bytes:
        return b""
    return content


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


def _team_aliases(team: str) -> List[str]:
    """Match strings for a team: its name plus a few common short forms."""
    aliases = [team.lower()]
    extra = {
        "south korea": ["korea", "korea republic"],
        "united states": ["usa", "u.s.", "u.s.a", "united states", "us men"],
        "bosnia and herzegovina": ["bosnia"],
        "czech republic": ["czechia", "czech"],
        "ivory coast": ["cote d'ivoire", "côte d'ivoire"],
        "dr congo": ["congo", "dr congo", "drc"],
        "cape verde": ["cabo verde"],
        "saudi arabia": ["saudi"],
        "new zealand": ["all whites"],
    }
    for a in extra.get(team.lower(), []):
        if a not in aliases:
            aliases.append(a)
    return aliases


def teams_in_text(text: str, teams: Sequence[str]) -> List[str]:
    """Return which *teams* are named in *text* (case-insensitive, alias-aware)."""
    low = (text or "").lower()
    hits: List[str] = []
    for team in teams:
        for alias in _team_aliases(team):
            # word-ish boundary so "us" doesn't match inside "thus"
            if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(alias), low):
                hits.append(team)
                break
    return hits


#: Score ceiling applied when a story is flagged off-topic (wrong tournament /
#: sport). Kept below the daemon's default ``--min-score`` (4) so an off-topic
#: piece can never clear the push gate on a team-name coincidence, no matter how
#: keyword-dense it is.
_OFF_TOPIC_CEILING = 2


def _keyword_score(text: str) -> int:
    """Sum keyword weights present in *text* (deduped per phrase)."""
    low = (text or "").lower()
    score = 0
    for table, _label in ((_KW_HIGH, "h"), (_KW_MED, "m"), (_KW_LINEUP, "l")):
        for phrase, weight in table.items():
            if phrase in low:
                score += weight
    for phrase, weight in _KW_NOISE.items():
        if phrase in low:
            score -= weight
    return score


def is_off_topic(text: str) -> bool:
    """True if *text* is about a different tournament / sport than the 2026 WC.

    A "ruled out" headline about Euro 2024, the Winter Olympics, the cricket /
    rugby World Cup, the U-20s or the Club World Cup scores high on availability
    keywords but is irrelevant to our event. We flag it so :func:`score_item`
    can apply a hard ceiling, regardless of keyword density.
    """
    low = (text or "").lower()
    return any(marker in low for marker in _KW_OFF_TOPIC)


def score_item(item: NewsItem, teams: Sequence[str]) -> int:
    """Relevance score for *item* given the teams-of-interest.

    Higher = more actionable. Combines:

    * keyword signal (injury / ruled out / suspension / lineup / referee),
    * a +2 bump per team-of-interest named (a story about a team we hold or
      that plays soon is worth more), capped so one article can't run away,
    * a small World-Cup-context bump so generic football noise that happens to
      use an injury word doesn't outrank a real WC squad story,
    * noise penalties for tickets / fantasy / betting-tips spam, and a hard
      ceiling for off-topic stories (Euro 2024, Olympics, cricket WC, U-20s…)
      so a wrong-tournament headline can't clear the push gate on a team-name
      coincidence.

    Never negative (floored at 0). The daemon's ``--min-score`` gate (default 4)
    means: at least one strong availability keyword *and* a relevant team, or a
    confirmed lineup, before we ping the phone.
    """
    blob = "%s. %s" % (item.title or "", item.summary or "")
    kw = _keyword_score(blob)

    named = teams_in_text(blob, teams)
    # Merge any teams the caller pre-tagged (e.g. Google-News query target).
    for t in item.teams:
        if t not in named:
            named.append(t)
    team_bonus = min(len(named), 2) * 2

    wc_bonus = 0
    low = blob.lower()
    if "world cup" in low or "fifa" in low or "2026" in low:
        wc_bonus = 1

    score = max(0, kw + team_bonus + wc_bonus)
    if is_off_topic(blob):
        score = min(score, _OFF_TOPIC_CEILING)
    return score


# Phrases that mark a MATERIAL squad change — a confirmed availability swing,
# not soft chatter. Only these (on an on-topic 2026-WC story about a relevant
# team) clear the bar to even be considered for a phone ping. "doubt", "knock",
# "fitness test", "assessed", "could miss" deliberately do NOT qualify — they
# are uncertainty, not a change.
_KW_MATERIAL = (
    "ruled out", "ruled-out", "ruled out of",
    "withdraw", "withdrawn", "withdraws",
    "out of the world cup", "out for the tournament", "out of the squad",
    "cut from", "dropped from the squad", "left out of the squad",
    "replaced in the squad", "replaces", "called up to replace",
    "miss the world cup", "will miss the world cup", "out of world cup",
    "suspended", "suspension", "banned", "ban rules",
    "confirmed lineup", "confirmed line-up", "starting xi confirmed",
    "retires from international", "international retirement",
)


def is_material_squad_event(item: "NewsItem", teams: Optional[Sequence[str]] = None) -> bool:
    """True only for a confirmed, material squad/availability change.

    The high bar that gates phone pings: a real availability swing (withdrawal,
    ruled out, suspension, confirmed XI, retirement) on an on-topic 2026 World
    Cup story. Uncertainty words ("doubt", "knock", "assessed") return False —
    they are logged, never pinged. Off-topic (wrong tournament) returns False.
    """
    blob = "%s. %s" % (item.title or "", item.summary or "")
    low = blob.lower()
    if is_off_topic(blob):
        return False
    if not any(m in low for m in _KW_MATERIAL):
        return False
    # Must be anchored to the 2026 men's WC, not generic football.
    if not ("world cup" in low or "2026" in low or "fifa" in low):
        return False
    return True


# ---------------------------------------------------------------------------
# Dedupe + insert
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_items(
    conn: sqlite3.Connection,
    items: Iterable[NewsItem],
    scores: Optional[Dict[str, int]] = None,
) -> List[sqlite3.Row]:
    """Insert previously-unseen *items*; return the rows that were new.

    Dedupe key is :attr:`NewsItem.uid`. Already-stored items are skipped (no
    update — we keep the first sighting, including its ``pushed`` flag). Pass
    ``scores`` (uid -> score) to persist scores at insert time; otherwise the
    stored score is 0 and the caller can update it.

    Returns the freshly-inserted rows (as ``sqlite3.Row``), newest-relevant
    first is the caller's concern. Idempotent across cycles.
    """
    ensure_schema(conn)
    scores = scores or {}
    inserted_uids: List[str] = []
    ts = _now_iso()
    for it in items:
        uid = it.uid
        cur = conn.execute("SELECT 1 FROM news_items WHERE uid = ?", (uid,))
        if cur.fetchone() is not None:
            continue
        teams_str = ",".join(it.teams) if it.teams else ""
        conn.execute(
            "INSERT INTO news_items "
            "(uid, ts_utc, source, title, link, summary, published, teams, score, pushed) "
            "VALUES (?,?,?,?,?,?,?,?,?,0)",
            (
                uid,
                ts,
                it.source,
                it.title,
                it.link,
                it.summary,
                it.published,
                teams_str,
                int(scores.get(uid, 0)),
            ),
        )
        inserted_uids.append(uid)
    conn.commit()
    if not inserted_uids:
        return []
    qmarks = ",".join("?" for _ in inserted_uids)
    rows = conn.execute(
        "SELECT * FROM news_items WHERE uid IN (%s)" % qmarks, inserted_uids
    ).fetchall()
    return list(rows)


def mark_pushed(conn: sqlite3.Connection, uids: Sequence[str]) -> None:
    """Flag the given uids as pushed so they are never re-alerted."""
    if not uids:
        return
    conn.executemany(
        "UPDATE news_items SET pushed = 1 WHERE uid = ?", [(u,) for u in uids]
    )
    conn.commit()


def set_score(conn: sqlite3.Connection, uid: str, score: int) -> None:
    """Persist a computed score onto a stored row."""
    conn.execute("UPDATE news_items SET score = ? WHERE uid = ?", (int(score), uid))
    conn.commit()


# ---------------------------------------------------------------------------
# Odds context
# ---------------------------------------------------------------------------


def _resolve_conn(conn_or_db: Any) -> Tuple[sqlite3.Connection, bool]:
    """Accept either an open connection or a path; return (conn, owned)."""
    if isinstance(conn_or_db, sqlite3.Connection):
        return conn_or_db, False
    conn = sqlite3.connect(str(conn_or_db))
    conn.row_factory = sqlite3.Row
    return conn, True


def _match_ids_for_team(team: str, event_meta: Dict[str, Dict[str, Any]]) -> List[str]:
    """Find match_ids in *event_meta* whose home/away matches *team* (aliases)."""
    aliases = set(_team_aliases(team))
    out: List[str] = []
    for mid, meta in event_meta.items():
        for side in ("home", "away"):
            val = (meta.get(side) or "").lower()
            if val in aliases or val == team.lower():
                out.append(mid)
                break
    return out


def odds_context(
    conn_or_db: Any,
    team: str,
    event_meta: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the most recent h2h odds line for *team*'s next/current fixture.

    Looks up the team's match_id(s) in ``event_meta`` (from
    :func:`wca.linemove.robust_event_meta`), then reads the freshest
    ``odds_snapshots`` row for each h2h selection of that fixture. Returns a
    dict::

        {
            "match_id": ..., "fixture": "Home vs Away", "kickoff": "...",
            "team": <team>, "team_odds": <decimal or None>,
            "lines": {selection: decimal_odds, ...},
            "as_of": <ts_utc of the freshest row>,
        }

    or ``None`` if we can't tie the team to a fixture or have no odds yet. This
    is the "immediately actionable" half of every alert: it tells the trader
    whether the market has *already* moved on the news.
    """
    if not team:
        return None
    mids = _match_ids_for_team(team, event_meta or {})
    conn, owned = _resolve_conn(conn_or_db)
    try:
        for mid in mids:
            # Latest snapshot timestamp for this match's h2h market.
            row = conn.execute(
                "SELECT MAX(ts_utc) AS mx FROM odds_snapshots "
                "WHERE match_id = ? AND market = 'h2h'",
                (mid,),
            ).fetchone()
            as_of = row["mx"] if row else None
            if not as_of:
                continue
            sel_rows = conn.execute(
                "SELECT selection, decimal_odds FROM odds_snapshots "
                "WHERE match_id = ? AND market = 'h2h' AND ts_utc = ?",
                (mid, as_of),
            ).fetchall()
            lines: Dict[str, float] = {}
            for sr in sel_rows:
                if sr["decimal_odds"] is not None:
                    lines[sr["selection"]] = float(sr["decimal_odds"])
            if not lines:
                continue
            meta = (event_meta or {}).get(mid, {})
            team_odds = _best_team_match(team, lines)
            move = team_line_movement(conn, mid, team, team_odds, as_of)
            return {
                "match_id": mid,
                "fixture": meta.get("fixture", ""),
                "kickoff": meta.get("kickoff", ""),
                "team": team,
                "team_odds": team_odds,
                "lines": lines,
                "as_of": as_of,
                # Additive movement context: a fresh story + a *flat* line is the
                # Endo-style tradable signature, so the alert surfaces this.
                "move_verdict": move["verdict"],
                "move_delta_pp": move["delta_pp"],
                "team_implied_now": move["implied_now"],
                "move_window_h": move["window_h"],
            }
        return None
    finally:
        if owned:
            conn.close()


def _best_team_match(team: str, lines: Dict[str, float]) -> Optional[float]:
    """Find the decimal-odds selection that names *team* (alias-aware)."""
    aliases = set(_team_aliases(team))
    aliases.add(team.lower())
    for sel, odds in lines.items():
        if sel.lower() in aliases:
            return odds
    # looser containment (e.g. selection "South Korea" vs team "South Korea")
    for sel, odds in lines.items():
        sl = sel.lower()
        if any(a in sl or sl in a for a in aliases):
            return odds
    return None


# A line move of more than 1.5 percentage points (implied probability) of the
# team's win price over the last ~6h is treated as the market having reacted.
_MOVE_THRESHOLD_PP = 1.5


def _ts_to_dt(ts: str) -> Optional[datetime]:
    """Parse a snapshot ts_utc to a tz-aware UTC datetime (``None`` on failure)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def team_line_movement(
    conn: sqlite3.Connection,
    match_id: str,
    team: str,
    latest_team_odds: Optional[float],
    latest_ts: Optional[str],
    window_hours: float = 6.0,
) -> Dict[str, Any]:
    """Verdict on whether *team*'s win price moved over the last *window_hours*.

    Compares the latest team decimal odds (already resolved by the caller) to the
    team's odds in the latest snapshot at or before ``latest_ts - window_hours``.
    Both are converted to implied probabilities (``1/odds``) and the change in
    percentage points is taken; ``MOVED`` when ``abs(delta) > 1.5pp`` else
    ``flat``. A fresh, high-impact story paired with a **flat** line is exactly
    the Endo-style tradable signature.

    Returns ``{"verdict", "delta_pp", "implied_now", "window_h"}``. ``verdict`` is
    ``"n/a"`` when there is no usable reference snapshot (a single snapshot, or
    the team odds can't be resolved) — never raises.
    """
    out: Dict[str, Any] = {
        "verdict": "n/a",
        "delta_pp": None,
        "implied_now": None,
        "window_h": window_hours,
    }
    latest_dt = _ts_to_dt(latest_ts or "")
    if latest_team_odds is None or latest_team_odds <= 0 or latest_dt is None:
        return out
    implied_now = 1.0 / float(latest_team_odds)
    out["implied_now"] = implied_now

    cutoff = (latest_dt - timedelta(hours=window_hours)).isoformat()
    try:
        # Distinct earlier snapshot timestamps (strictly before latest), newest
        # first; we want the freshest one at or before the cutoff.
        ts_rows = conn.execute(
            "SELECT DISTINCT ts_utc FROM odds_snapshots "
            "WHERE match_id = ? AND market = 'h2h' AND ts_utc < ? "
            "ORDER BY ts_utc DESC",
            (match_id, latest_ts),
        ).fetchall()
    except sqlite3.Error:
        return out

    ref_ts: Optional[str] = None
    earliest_ts: Optional[str] = None
    for row in ts_rows:
        ts = row[0] if not isinstance(row, sqlite3.Row) else row["ts_utc"]
        earliest_ts = ts  # rows are DESC, so the last seen is the earliest
        if ref_ts is None and ts is not None and ts <= cutoff:
            ref_ts = ts
    # If nothing is old enough to clear the window, fall back to the earliest
    # available snapshot so we still report a movement over the data we have.
    if ref_ts is None:
        ref_ts = earliest_ts
    if ref_ts is None:
        return out

    try:
        sel_rows = conn.execute(
            "SELECT selection, decimal_odds FROM odds_snapshots "
            "WHERE match_id = ? AND market = 'h2h' AND ts_utc = ?",
            (match_id, ref_ts),
        ).fetchall()
    except sqlite3.Error:
        return out
    ref_lines: Dict[str, float] = {}
    for sr in sel_rows:
        odds = sr[1] if not isinstance(sr, sqlite3.Row) else sr["decimal_odds"]
        sel = sr[0] if not isinstance(sr, sqlite3.Row) else sr["selection"]
        if odds is not None:
            ref_lines[sel] = float(odds)
    ref_odds = _best_team_match(team, ref_lines)
    if ref_odds is None or ref_odds <= 0:
        return out

    implied_ref = 1.0 / ref_odds
    delta_pp = (implied_now - implied_ref) * 100.0
    out["delta_pp"] = delta_pp
    out["verdict"] = "MOVED" if abs(delta_pp) > _MOVE_THRESHOLD_PP else "flat"
    return out


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------


def _md_escape(s: str) -> str:
    """Escape the few chars that break Telegram legacy-Markdown links/emphasis.

    We use legacy Markdown (parse_mode='Markdown'). The risky chars in free
    text are ``_ * [ ` ``. We escape them so a player's name with an underscore
    or a title with asterisks doesn't corrupt the message.
    """
    if not s:
        return ""
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def format_alert(
    item: NewsItem,
    score: int,
    odds: Optional[Dict[str, Any]] = None,
) -> str:
    """Render a Telegram-ready Markdown alert for *item* with *odds* context.

    Layout::

        🚨 WC NEWS · score N · <source>
        <title>
        <one-line summary>
        Teams: <a, b>
        📊 <Fixture> (kickoff …)
           <Team>: 2.40  | Draw 3.10 | Other 2.80   — as of <ts>
        🔗 <link>

    When ``odds`` is ``None`` we say so plainly ("no live odds line yet") so the
    reader knows the market angle is unconfirmed rather than silently dropping
    it. The whole point is that the alert is *actionable*: it pairs the scoop
    with whether the price has moved.
    """
    teams = item.teams or []
    head = "🚨 *WC NEWS* · score %d · %s" % (int(score), _md_escape(item.source))
    lines: List[str] = [head, _md_escape(item.title.strip())]

    summary = (item.summary or "").strip()
    if summary:
        if len(summary) > 240:
            summary = summary[:237].rstrip() + "…"
        lines.append("_%s_" % _md_escape(summary))

    if teams:
        lines.append("Teams: %s" % _md_escape(", ".join(teams)))

    if odds:
        fixture = odds.get("fixture") or odds.get("match_id", "")
        kickoff = odds.get("kickoff") or ""
        head2 = "📊 %s" % _md_escape(str(fixture))
        if kickoff:
            head2 += " (k/o %s)" % _md_escape(_short_kickoff(kickoff))
        lines.append(head2)
        odds_lines = odds.get("lines") or {}
        team_odds = odds.get("team_odds")
        if team_odds is not None:
            implied = odds.get("team_implied_now")
            tail = " (%.1f%%)" % (float(implied) * 100.0) if implied else ""
            lines.append(
                "   *%s*: %s%s"
                % (_md_escape(odds.get("team", "")), _fmt_odds(team_odds), tail)
            )
        if odds_lines:
            pretty = " | ".join(
                "%s %s" % (_md_escape(sel), _fmt_odds(o))
                for sel, o in odds_lines.items()
            )
            lines.append("   line: %s" % pretty)
        # Movement verdict: the load-bearing token. A *flat* line on a fresh
        # high-impact story is the tradable Endo signature; MOVED means the
        # market has likely already digested the news.
        verdict = odds.get("move_verdict")
        if verdict and verdict != "n/a":
            delta = odds.get("move_delta_pp")
            win = odds.get("move_window_h", 6.0)
            if verdict == "MOVED" and delta is not None:
                lines.append(
                    "   ⚠️ line *MOVED* %+.1fpp last %gh — market may have reacted"
                    % (delta, win)
                )
            elif verdict == "flat":
                lines.append(
                    "   ✅ line *FLAT* last %gh — likely not yet priced in" % win
                )
        as_of = odds.get("as_of")
        if as_of:
            lines.append("   _as of %s_" % _md_escape(_short_ts(as_of)))
    else:
        lines.append("📊 _no live odds line yet — market angle unconfirmed_")

    if item.link:
        lines.append("🔗 %s" % item.link)  # raw URL; not markdown-escaped

    return "\n".join(lines)


def format_trade_idea(item: "NewsItem", odds: Dict[str, Any]) -> str:
    """Render a TRADE IDEA ping: a material squad change on an unmoved line.

    Only fired when a confirmed availability swing meets a line that has stayed
    flat over the wide window (so the news is plausibly *not yet priced* — the
    Endo signature, not the Morocco trap). Leads with a suggested direction.
    """
    team = odds.get("team", "")
    fixture = odds.get("fixture") or odds.get("match_id", "")
    kickoff = odds.get("kickoff") or ""
    implied = odds.get("team_implied_now")
    win = odds.get("move_window_h", 18.0)
    delta = odds.get("move_delta_pp")

    head = "🎯 *NEW TRADE IDEA* — %s" % _md_escape(team)
    lines = [head, _md_escape(item.title.strip())]
    line2 = "📊 %s" % _md_escape(str(fixture))
    if kickoff:
        line2 += " (k/o %s)" % _md_escape(_short_kickoff(kickoff))
    lines.append(line2)
    if odds.get("team_odds") is not None:
        tail = " (%.1f%%)" % (float(implied) * 100.0) if implied else ""
        lines.append("   *%s*: %s%s" % (_md_escape(team), _fmt_odds(odds.get("team_odds")), tail))
    odds_lines = odds.get("lines") or {}
    if odds_lines:
        lines.append("   line: %s" % " | ".join(
            "%s %s" % (_md_escape(s), _fmt_odds(o)) for s, o in odds_lines.items()))
    verdict = odds.get("move_verdict")
    if verdict == "flat" and delta is not None:
        lines.append("   ✅ line *unmoved* %gh (%+.1fpp) — likely NOT priced" % (win, delta))
    else:  # n/a — no movement history yet; still worth flagging fast
        lines.append("   ✅ line *unmoved / untracked* — no sign the market has reacted")
    lines.append(
        "   *Angle:* material squad change weakens %s — fade them / back the opponent "
        "before the line corrects." % _md_escape(team))
    if item.link:
        lines.append("🔗 %s" % item.link)
    return "\n".join(lines)


def _fmt_odds(o: Optional[float]) -> str:
    if o is None:
        return "—"
    return "%.2f" % float(o)


def _short_kickoff(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%a %d %b %H:%M UTC")
    except (ValueError, AttributeError):
        return iso


def _short_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S UTC")
    except (ValueError, AttributeError):
        return iso


# ---------------------------------------------------------------------------
# Convenience: end-to-end scan for one set of teams (used by the daemon).
# ---------------------------------------------------------------------------


def gather_items(
    teams: Sequence[str],
    fetch=fetch_feed,
    include_core: bool = True,
    sleep_between: float = 0.0,
) -> List[NewsItem]:
    """Fetch every source + Google-News query and return all parsed items.

    ``fetch`` is injectable for tests (defaults to the real :func:`fetch_feed`).
    Per-source exceptions are caught here so a single bad feed can't abort the
    gather. Items from Google-News team queries are pre-tagged with their
    target team.
    """
    items: List[NewsItem] = []
    specs: List[Dict[str, str]] = []
    if include_core:
        specs.extend(SOURCES)
    specs.extend(google_news_queries(teams))
    for spec in specs:
        try:
            got = fetch(spec["url"], spec.get("name", spec["url"]))
        except Exception:  # noqa: BLE001 - isolate per-source failure
            continue
        target = spec.get("team") or ""
        for it in got or []:
            if target and target not in it.teams:
                it.teams.append(target)
            items.append(it)
        if sleep_between:
            time.sleep(sleep_between)
    return items
