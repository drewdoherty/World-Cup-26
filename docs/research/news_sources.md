# World Cup team-news signal engine — sources, etiquette & scoring rubric

> Motivating miss: the **Wataru Endo** withdrawal (Japan captain ruled out) was
> tradable for *hours* because the Japan line had not moved — but it was only
> caught because a friend texted. This engine is the systematic version of that
> friend. It continuously scans public sources for World Cup squad / injury /
> suspension / referee / lineup news, scores each item for betting relevance,
> dedupes, and pushes the high-signal ones to Telegram **with current odds
> context** so every alert is immediately actionable.

Implementation: [`src/wca/news.py`](../../src/wca/news.py) (the deterministic
engine) driven by [`scripts/wca_newsd.py`](../../scripts/wca_newsd.py) (the
polling daemon). Tests: [`tests/test_news.py`](../../tests/test_news.py),
[`tests/test_newsd.py`](../../tests/test_newsd.py).

---

## 1. Sources

### 1a. Core always-on RSS feeds (`news.SOURCES`)

Broad, editorially-curated football feeds. They catch the write-ups and the
framing ("major blow", "race against time") that a bare wire item lacks, and
they are stable, free, and unauthenticated.

| Source | Feed URL | Why |
|---|---|---|
| BBC Sport — Football | `https://feeds.bbci.co.uk/sport/football/rss.xml` | Fast, authoritative, strong national-team desk |
| The Guardian — Football | `https://www.theguardian.com/football/rss` | Good tactical / availability reporting |
| ESPN — Soccer | `https://www.espn.com/espn/rss/soccer/news` | Wide CONCACAF / CONMEBOL / AFC coverage (host-confederation teams) |
| Sky Sports — Football | `https://www.skysports.com/rss/12040` | Breaking team-news, quick to publish |

Both **RSS 2.0** (`<channel><item>`) and **Atom** (`<feed><entry>`) shapes are
parsed by `news.parse_feed`, using only `xml.etree.ElementTree` from the stdlib
— deliberately **no `feedparser`** dependency. Malformed / truncated XML returns
`[]` rather than raising, so one broken feed never aborts a poll cycle.

### 1b. Per-team Google News RSS queries (`news.google_news_queries`)

The targeted layer. For each team of interest we build:

```
https://news.google.com/rss/search?q=<urlencoded>&hl=en-US&gl=US&ceid=US:en
```

with the query

```
"<Team>" injury OR injured OR doubt OR ruled out OR withdraw OR squad OR
suspended OR suspension OR lineup OR "starting XI" OR fitness
```

The quoted team name anchors the search to that nation; the `OR`-group narrows
to availability / team-news stories. Plus a handful of **tournament-wide**
queries for cross-team news that no single team query would catch:

- `World Cup 2026 referee appointed`
- `World Cup 2026 injury squad ruled out`
- `World Cup 2026 suspension banned`

**Why Google News RSS is the workhorse.** Its search feed aggregates most
reporter scoops — federation / club announcements, beat writers, and the wire /
outlet *write-ups of tweets* — usually within minutes of publication, and it is
free, reliable, and documented. It is the single highest-yield public source for
this use case.

**Teams of interest are a subset, by design.** The daemon does **not** query all
48 teams every cycle. `compute_teams_of_interest` restricts to:

1. teams with a fixture kicking off within `--horizon` hours (default 72h),
   from `wca.linemove.robust_event_meta` — including matches up to ~2.5h in the
   past so in-play lineup / injury news still counts; **plus**
2. every WC team named in an *open* ledger bet.

This keeps each poll cheap (a handful of Google queries, not 48) while still
covering exactly the teams whose news can move a price we care about.

---

## 2. The Twitter / X gap (read this — it is deliberate)

The fastest tier-1 injury scoops break on **Twitter / X** (Fabrizio Romano,
David Ornstein, national-team beat reporters). This engine does **not** read X,
and that is an honest, considered limitation, not an oversight:

- **X has no free API tier.** The historical free/`v1.1` access is gone; the
  current API is paid and rate-limited well beyond a hobby budget.
- **Scraping X is against its ToS and is unreliable** (login walls, shifting
  markup, IP blocks). We do not scrape it, we do not use an unofficial endpoint,
  and there is **no pretend "tweets" source** anywhere in the code. Faking
  first-party social coverage would be dishonest and would breed false
  confidence in alerts.

**Mitigation — Google News RSS carries the reporting.** When a reporter tweets a
scoop, outlets write it up within minutes ("*X report: player ruled out*"), and
those write-ups appear in the Google News search feed. We act on the *reporting*
of the tweet, not the tweet itself. In practice the latency added is small —
minutes, not hours — and for the Endo-style signature (a confirmed withdrawal
the market hasn't priced) minutes is well inside the tradable window.

**Residual gap to be aware of:** the very first 30–120 seconds after a raw tweet,
before any outlet has written it up, are not covered. If first-party X latency
ever becomes the binding constraint, the upgrade path is a paid X API key wired
in as an additional `SOURCES` entry — not a scraper.

---

## 3. Polling etiquette

- **User-Agent.** Feeds are fetched with a descriptive UA
  (`news.USER_AGENT`); some feeds 403 a bare `python-requests` UA.
- **Interval.** Default cycle is **600 s (10 min)** (`--interval`). That is
  polite for RSS (these are static documents served from CDNs) and fast enough
  for the trade window. Do not tighten it below a couple of minutes — there is
  no payoff, and Google News in particular should not be hammered.
- **Per-source isolation.** Every fetch is wrapped: a transport error, a non-200,
  or a parse failure yields `[]` for that source only. One dead feed never kills
  a cycle (`news.gather_items`).
- **Timeouts.** Each fetch has a hard `timeout` (default 12 s) so a hung feed
  cannot stall the cycle.
- **Body-size cap.** `fetch_feed` streams the response and abandons any body over
  `news.MAX_FEED_BYTES` (8 MB) — a hostile or runaway feed (the live core feeds
  are all well under 1 MB) can never exhaust memory in the long-running daemon.
  Over-cap feeds yield `[]` (a half-feed is likelier to mis-parse than help).
- **Idempotence.** Dedupe (see §5) means re-polling the same feed re-inserts
  nothing and re-alerts nothing, so a tight retry after a transient failure is
  safe.

---

## 4. Alert-scoring rubric (`news.score_item`)

An item's score is

```
score = max(0, keyword_score + team_bonus + wc_bonus)
```

computed over `title + ". " + summary` (case-insensitive substring match).

### 4a. Keyword weights (`keyword_score`)

| Tier | Examples | Weight |
|---|---|---|
| **High** (availability change) | `ruled out`, `withdraw(n/s)`, `out of the world cup`, `out for the tournament`, `will miss`, `out injured` | **+4** (most) |
| | `suspended`, `suspension`, `banned`, `red card`, `injury blow` | **+3** |
| **Medium** (uncertainty / churn / officials) | `injury`, `injured`, `doubt(ful)`, `knock`, `strain`, `hamstring`, `replacement`, `lineup`, `starting xi`, `team news`, `referee` | **+1…+2** |
| | `fitness`, `squad`, `call-up`, `officials`, `var`, `captain`, `return` | **+1** |
| **Lineup / confirmed XI** | `confirmed lineup`, `starting lineup`, `team to face` | **+2…+3** |
| **Noise penalty** | `betting tips` (−3), `ticket`, `fantasy`, `merchandise`, `kit launch`, `how to watch`, `predictions`, `tv channel` | **−1…−3** |
| **Off-topic penalty** | `euro 2024/2028`, `rugby`/`cricket`/`olympic`, `nfl`/`nba`/`nhl`, `u20`/`u21`/`u23`, `women's world cup`, `club world cup`, `nations league`, `champions league` | **−3…−6** |

### 4b. Context bonuses

- **`team_bonus = min(#teams_named, 2) × 2`** — a story about a team that plays
  soon or that we hold is worth more; capped at two teams so one article can't
  run away. Teams are matched by `news.teams_in_text` (canonical name + a few
  curated short forms) **and** any team the Google-News query pre-tagged.
- **`wc_bonus = +1`** when the text mentions `world cup`, `fifa`, or `2026`, so a
  generic-football injury word doesn't outrank a real WC squad story.

### 4c. The push gate

The daemon only pushes items with `score >= --min-score` (**default 4**),
highest score first, capped at `--max-per-cycle` (default 5; overflow is logged
and re-alerted next cycle). Gate of 4 effectively requires *at least one strong
availability keyword and a relevant team* (or a confirmed lineup) before it pings
the phone. The Endo headline scores ~18; generic transfer gossip scores 0.

### 4d. Honest limitations of the scorer

- **Additive, not semantic.** Score is a weighted keyword sum, not NLP. A
  wrong-tournament story (e.g. a *rugby* "ruled out") carries an off-topic
  penalty but can still clear the gate on the strength of a team-name
  coincidence plus generic injury words. The reliable guarantee is *ordering*:
  the genuine 2026-WC story always outranks the wrong-tournament one, so with the
  small per-cycle cap and score sort the real story is what gets pushed first.
  Treat each alert as a *prompt to look*, not a verified fact — the human reads
  the headline and the odds line before acting.
- **No demonyms in the scorer.** `teams_in_text` matches the canonical team name
  and a few curated short forms (e.g. `Korea` → South Korea, `U.S.` → United
  States), but **not** free demonyms ("Holland"/"Dutch", "Three Lions"). In
  practice the per-team Google-News query (which quotes the team name) carries
  the team tag, so a demonym-only headline still surfaces via that route; the
  scorer alone will not tag it.

---

## 5. Dedupe (`news.new_items`, the `news_items` table)

Each item has a stable `uid` = `sha1(normalised_link_or_title + "|" + source)`,
where the link is normalised by stripping the query string (Google News rewrites
links with tracking params) and a trailing slash. `new_items` inserts only
previously-unseen `uid`s into `news_items` (an idempotent SQLite table in the
same `data/wca.db` ledger), so re-polling re-alerts nothing.

**Known limitation — cross-source syndication.** Because the `uid` includes the
`source`, the *same story* republished under different outlets (BBC vs Sky vs a
Google-News wire copy) produces *different* `uid`s and can alert more than once.
The per-cycle cap and the `--min-score` gate bound the blast radius, but a fuzzy
title-hash dedupe (collapsing near-identical titles within a 48h window) is the
clean upgrade if duplicate alerts become annoying in practice. Logged here so the
behavior is a known trade-off, not a silent bug.

---

## 6. Odds context — the "immediately actionable" half (`news.odds_context`)

Every alert is paired with the team's **current** market line so the reader can
see at a glance whether the market has already digested the news. For the team's
fixture (looked up in `event_meta` from `wca.linemove.robust_event_meta`), it
reads the freshest `odds_snapshots` h2h row and returns the team's decimal odds,
the full 1X2 line, and a **line-movement verdict**:

- The team's win price is converted to an implied probability (`1/odds`) at the
  latest snapshot and at the newest snapshot **at or before ~6h earlier**.
- The change in percentage points drives the verdict:
  **`MOVED`** when `|Δ| > 1.5pp`, else **`flat`**.

A fresh, high-impact story paired with a **`flat`** line is exactly the
Endo-style tradable signature — the market hasn't reacted yet — so the alert
emboldens this verdict. A `MOVED` verdict warns that the market may already have
priced the news in. When no fixture / no odds exist yet, the alert says so
plainly ("no live odds line yet — market angle unconfirmed") rather than
silently dropping the market angle.

**Which team's line do we show?** Google News search is fuzzy: a Japan-withdrawal
story can surface in the *United States* query feed and arrive tagged
`[United States, Japan]` (query tag first, text-team second). `wca_newsd._odds_for_item`
therefore pairs the alert with the team **named in the headline/summary text**
(via `news.teams_in_text`), falling back to the query-tag team only when the text
names no known team. Without this, a Japan story would be shown next to the USA's
odds — a misleading "FLAT" on the wrong fixture. (Caught and fixed during the
live `--once` verification.)

---

## 7. Determinism & test guards

- **Library code reads no clock and opens no network at import time.** Callers
  inject the `requests` session (tests pass a fake returning fixture RSS),
  timestamps come from the data, and the daemon stamps wall-clock only at the
  edge.
- **Never push during tests.** `wca_newsd.run_cycle` hard-guards on
  `PYTEST_CURRENT_TEST` (mirroring `wca.sync`): a test cycle scores, dedupes, and
  inserts, but never contacts Telegram. The full engine — parsing (RSS + Atom +
  malformed), scoring, dedupe, odds context (MOVED vs flat), and alert formatting
  / Markdown escaping — is covered offline.

---

## 8. Telegram push target

Alerts go to the **admin chat only**, resolved (in
`wca_newsd.resolve_chat_id`) in priority order:

1. `WCA_NEWS_CHAT_ID` (dedicated news channel), else
2. `TELEGRAM_ADMIN_USER_ID`, else
3. the first id in `TELEGRAM_CHAT_ID` (comma-separated).

The bot token (`TELEGRAM_BOT_TOKEN`) is never logged.
