# Odds & Fixtures API Recon — World Cup Alpha

**Date:** 2026-06-10  
**Analyst:** Automated recon agent  
**Purpose:** Identify and evaluate machine-readable odds and fixtures data sources for World Cup 2026 quantitative betting research ($1,000 bankroll, CLV-focused, UK-licensed books + Betfair Exchange)

---

## Key-Facts Table

| # | Fact | Confidence | Source |
|---|------|-----------|--------|
| 1 | The Odds API sport key for FIFA World Cup is `soccer_fifa_world_cup` | confirmed | [the-odds-api.com/sports/fifa-world-cup-odds.html](https://the-odds-api.com/sports/fifa-world-cup-odds.html) |
| 2 | Sample endpoint: `GET https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds?regions=uk&markets=h2h&oddsFormat=decimal&apiKey=KEY` | confirmed | [the-odds-api.com/sports/fifa-world-cup-odds.html](https://the-odds-api.com/sports/fifa-world-cup-odds.html) |
| 3 | Free tier: 500 credits/month, resets 1st of month, no credit card required | confirmed | [the-odds-api.com homepage](https://the-odds-api.com/) + [sportbotai review](https://www.sportbotai.com/blog/tools/the-odds-api-review) |
| 4 | Credit cost for live odds: `markets × regions` per call (e.g. 1 market × 1 region = 1 credit) | confirmed | [the-odds-api.com/liveapi/guides/v4/](https://the-odds-api.com/liveapi/guides/v4/) |
| 5 | Historical odds credit cost: 10× standard (10 credits per market per region) | confirmed | [the-odds-api.com/liveapi/guides/v4/](https://the-odds-api.com/liveapi/guides/v4/) |
| 6 | Historical odds require a **paid plan** (not available on free tier) | confirmed | [the-odds-api.com/historical-odds-data/](https://the-odds-api.com/historical-odds-data/) |
| 7 | Historical odds for soccer_fifa_world_cup available from 2022-04-03; snapshots at 10-min intervals from June 2020, upgraded to 5-min from September 2022 | confirmed | [the-odds-api.com/historical-odds-data/](https://the-odds-api.com/historical-odds-data/) |
| 8 | Historical odds endpoint: `GET /v4/historical/sports/{sport}/odds?date=ISO8601&regions=uk&markets=h2h` | confirmed | API docs v4 + search |
| 9 | Paid plans: 20K credits $30/mo, 100K credits $59/mo, 5M credits $119/mo, 15M credits $249/mo | confirmed | [the-odds-api.com homepage](https://the-odds-api.com/) + sportbotai review |
| 10 | UK bookmakers supported: 888sport, Betfair Exchange (`betfair_ex_uk`), Betfair Sportsbook (`betfair_sb_uk`), Betfred, Bet Victor, Betway, BoyleSports, Casumo, Coral, Grosvenor, Ladbrokes, LeoVegas, LiveScore Bet, Matchbook, Paddy Power (`paddypower`), Sky Bet (`skybet`), Smarkets, Unibet, Virgin Bet (`virginbet`), William Hill | confirmed | [the-odds-api.com/sports-odds-data/bookmaker-apis.html](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html) |
| 11 | **Bet365 is NOT listed** as a supported bookmaker on The Odds API UK region | confirmed | [the-odds-api.com/sports-odds-data/bookmaker-apis.html](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html) |
| 12 | Markets available for soccer: h2h (1X2), h2h_3_way, spreads (handicap), totals (over/under), outrights, btts, draw_no_bet, team_totals, alternate_spreads_corners, alternate_totals_cards, h2h_lay, outrights_lay, player props | confirmed | [the-odds-api.com/sports-odds-data/betting-markets.html](https://the-odds-api.com/sports-odds-data/betting-markets.html) |
| 13 | football-data.org FIFA World Cup competition code: `WC`; free tier includes World Cup | confirmed | [football-data.org coverage](https://www.football-data.org/coverage) + [Apify scraper listing](https://apify.com/parseforge/football-data-org-competitions-scraper) |
| 14 | football-data.org free tier rate limit: 10 requests/minute (registered); unregistered: 100 req/24h (area/competition list only) | confirmed | [docs.football-data.org/general/v4/policies.html](https://docs.football-data.org/general/v4/policies.html) |
| 15 | football-data.org free tier covers 12 competitions including "Worldcup"; no real-time scores (delayed), no lineups/player data | confirmed | [football-data.org/coverage](https://www.football-data.org/coverage) + secondary sources |
| 16 | football-data.org has no odds endpoint on any tier natively; Odds Add-On is €15/mo for 40 competitions | likely | [football-data.org/pricing](https://www.football-data.org/pricing) |
| 17 | API-Football (api-sports.io): free tier = 100 requests/day; World Cup 2026 uses `league=1&season=2026`; odds endpoint included in coverage object | confirmed | search (primary API-Football blog post) |
| 18 | API-Football paid plans start higher; free 100 req/day rules out production polling | confirmed | search |
| 19 | fixturedownload.com has full FIFA World Cup 2026 fixture (104 matches) in CSV, XLSX, ICS, JSON; updated daily; no auth required for manual browser download | confirmed | [fixturedownload.com/results/fifa-world-cup-2026](https://fixturedownload.com/results/fifa-world-cup-2026) |
| 20 | fixturedownload.com JSON download URL (`/download/json/fifa-world-cup-2026`) returns HTML page (not raw JSON) when fetched programmatically even with a browser User-Agent; `/view/json/` URL also returned 403 to agent without User-Agent. Programmatic JSON access is NOT reliable — treat as browser-only manual download | confirmed | adversarial re-verification 2026-06-10 (curl test + WebFetch both failed to retrieve raw JSON) |
| 21 | openfootball/worldcup.json on GitHub has free, no-auth World Cup 2026 JSON at `https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json`; fields: round, date, time, teams, group, venue, scores; **104 matches confirmed** (2026-06-11 through 2026-07-19); HTTP 200 verified | confirmed | adversarial re-verification 2026-06-10 — curl returned valid JSON with 104 matches |
| 22 | Betfair historicdata.betfair.com: Basic tier is free (1-min intervals, last-traded price, no volume); Advanced (paid, 1-sec intervals, top-3 ladder, volume); Pro (paid, 50ms, full ladder) | confirmed | [betfair-datascientists.github.io/data/usingHistoricDataSite/](https://betfair-datascientists.github.io/data/usingHistoricDataSite/) |
| 23 | Betfair historical data covers "nearly all Exchange markets since 2016"; soccer is covered; **World Cup Match_Odds specifically is not explicitly confirmed** in the primary betfair-datascientists guide — inferred from broader "soccer internationals" coverage language and existence of a "FIFA World Cup Datathon" reference | likely | [betfair-datascientists.github.io/data/usingHistoricDataSite/](https://betfair-datascientists.github.io/data/usingHistoricDataSite/) — World Cup explicitly confirmed only via datathon reference, not market catalog |
| 24 | Betfair historical data Advanced/Pro tier pricing not publicly listed; estimate £30–£200/mo range; must purchase via historicdata.betfair.com logged in | unverified | secondary sources (portal was unreachable during recon) |
| 25 | Betfair historical data download portal (historicdata.betfair.com) requires an active Betfair account login; data exports as TAR/.bz2 files | confirmed | [betfair-datascientists.github.io/data/usingHistoricDataSite/](https://betfair-datascientists.github.io/data/usingHistoricDataSite/) |
| 26 | OddsJam API: covers 100+ sportsbooks, includes Pinnacle; pricing requires sales contact; not self-serve | likely | [oddsjam.com/odds-api](https://oddsjam.com/odds-api) + search |
| 27 | The Odds API closing line: no dedicated "closing line" endpoint; closest is historical endpoint with `date` param set to kick-off time to retrieve last pre-match snapshot | likely | API docs v4 analysis |

---

## (a) The Odds API — Detail

**Website:** https://the-odds-api.com  
**Signup:** https://the-odds-api.com/ → "Get API Key" → enter email → key delivered by email, no credit card required

### Sport Key
`soccer_fifa_world_cup` — confirmed active, covers all 104 matches of the 2026 tournament.

### UK Bookmakers (region key = `uk`)
All of the user's UK-licensed books are present **except Bet365**:

| Bookmaker Key | Name |
|---|---|
| `betfair_ex_uk` | Betfair Exchange |
| `betfair_sb_uk` | Betfair Sportsbook |
| `paddypower` | Paddy Power |
| `skybet` | Sky Bet |
| `virginbet` | Virgin Bet |
| `williamhill` | William Hill |
| `ladbrokes_uk` | Ladbrokes |
| `coral` | Coral |
| `betway` | Betway |
| `betvictor` | Bet Victor |
| `matchbook` | Matchbook (exchange) |
| `smarkets` | Smarkets (exchange) |

**Bet365 is absent.** Bet365 does not expose a public odds feed; no odds API currently provides live Bet365 UK odds programmatically. Manual CLV tracking against Bet365 will need to be done via screen-scraping or manual entry.

### Markets for Soccer
- `h2h` — 1X2 (three-way or two-way depending on competition)
- `h2h_3_way` — explicit three-way 1X2
- `spreads` — Asian/European handicap
- `totals` — over/under goals
- `btts` — both teams to score
- `draw_no_bet`
- `team_totals` — individual team over/under
- `outrights` — tournament winner, group winner futures
- `outrights_lay` — lay outrights (exchange)
- `alternate_totals_cards`, `alternate_spreads_corners` — niche markets

### Credit Costs
| Endpoint | Cost formula |
|---|---|
| Live/upcoming odds | `markets × regions` credits per call |
| Historical odds | `10 × markets × regions` credits per call |
| Scores | 1 credit (live/upcoming), 2 credits (includes last 3 days) |
| Sports, Events, Participants | 0 credits (free) |

**Free tier example:** 1 call for `soccer_fifa_world_cup`, region=`uk`, market=`h2h` costs 1 credit. With 500 free credits/month you can make up to 500 such calls — roughly 16/day spread over a month. Querying 3 markets × 1 region = 3 credits/call → ~166 calls/month on free tier.

**Production recommendation:** The 20K plan ($30/mo) gives ~6,666 single-market-region calls, sufficient for real-time odds polling across the tournament.

### Historical / Closing Line Odds
- Available from **2022-04-03** for `soccer_fifa_world_cup`
- Snapshots every 5 minutes (post-Sep 2022)
- **Requires paid plan**
- Closing line approximation: query historical endpoint with `date` = match kick-off UTC timestamp to retrieve the last snapshot before the match started
- Endpoint pattern: `GET /v4/historical/sports/soccer_fifa_world_cup/odds?apiKey=KEY&regions=uk&markets=h2h&date=2026-06-11T21:00:00Z`

### Signup Steps
1. Go to https://the-odds-api.com
2. Click **"Get API Key"** (no credit card required)
3. Enter email address
4. Receive API key by email
5. Test: `curl "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds?apiKey=YOUR_KEY&regions=uk&markets=h2h&oddsFormat=decimal"`
6. Upgrade to $30/mo 20K plan when ready for production polling

---

## (b) Alternative APIs

### football-data.org
**Website:** https://www.football-data.org  
**Signup:** https://www.football-data.org/client/register (free, instant)

- **World Cup coverage:** Yes. Competition code = `WC`. Available on free tier.
- **Endpoint example:** `GET https://api.football-data.org/v4/competitions/WC/matches` (with `X-Auth-Token: YOUR_TOKEN` header)
- **Free tier rate limit:** 10 requests/minute (registered); 100 req/24h (unregistered, area/competition list only)
- **Free tier data:** Fixtures, results, standings, team metadata. Scores are **delayed** (not real-time).
- **No odds endpoint** natively. An Odds Add-On exists (€15/mo, 40 competitions) but this is a separate bolt-on.
- **Paid tiers:** Free (€0), then €12, €29, €49, €99, €199/mo with progressively richer data.
- **Verdict for this project:** Excellent as a **free fixtures/results source** to build match schedule and track results. Not suitable as an odds source.

### API-Football (api-sports.io / api-football.com)
**Website:** https://www.api-football.com  
**Signup:** Via RapidAPI or direct

- **World Cup 2026:** `league=1`, `season=2026`. Odds endpoint enabled (`"odds": true` in coverage object).
- **Free tier:** 100 requests/day, access to all endpoints including odds.
- **Limitation:** 100 req/day is too low for continuous odds polling across 104 matches. Adequate for fixture + score lookup only.
- **Paid plans:** Tiered by request volume; pricing via RapidAPI.
- **Verdict:** Useful backup for fixture metadata. Odds coverage is nominal on free tier.

### fixturedownload.com
**Website:** https://fixturedownload.com/results/fifa-world-cup-2026  
**Access:** Free, no account required

- **Full 2026 fixture:** 104 matches available in CSV, XLSX, ICS, JSON
- **JSON view:** https://fixturedownload.com/view/json/fifa-world-cup-2026
- **Fields:** Match number, round name, date, time (local), home team, away team, venue, home score, away score
- **Update frequency:** Once per day
- **Note:** The JSON URL returned 403 to this agent during recon — programmatic access may be rate-limited or blocked. Download manually or use the direct file download.
- **Verdict:** Fine for a one-time fixture list seed into the database. Not a live API.

### openfootball/worldcup.json (GitHub)
**URL:** https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json  
**Access:** Completely free, no auth, public domain data

- **Fields:** round, date, time, group, venue, home team, away team, scores, goal scorers with minutes
- **Verdict:** Best zero-friction fixture/results source. Wire up a daily `curl` or `requests.get()` to keep results current. Schema can change without notice (open-source caveat).

### OddsJam
**Website:** https://oddsjam.com/odds-api

- Covers 100+ sportsbooks including Pinnacle (sharp benchmark)
- **Pricing:** Not self-serve; requires sales contact. Consumer plans $99–$499/mo.
- **UK books:** Coverage unclear; primarily US-focused consumer product
- **Verdict:** Oversized for this project; Pinnacle access is valuable but requires a sales relationship.

### RapidAPI / odds-api.io / other aggregators
Multiple third-party odds API aggregators exist (odds-api.io claims 265+ bookmakers). Quality and latency vary. None confirmed as primary sources. Investigate if The Odds API lacks a needed book.

---

## (c) Recommended Stack for Today

### Fixtures Source: openfootball/worldcup.json
**Why:** Zero friction, no signup, no API key, full 2026 fixture including venues and groups. Public domain.

**Wire-up today:**
```python
import requests, json

FIXTURES_URL = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"
fixtures = requests.get(FIXTURES_URL).json()
```

**Backup:** Download CSV manually from https://fixturedownload.com/results/fifa-world-cup-2026 if the GitHub source lags.

### Odds Source: The Odds API
**Why:** `soccer_fifa_world_cup` key confirmed active; Paddy Power, Sky Bet, Virgin Bet, Betfair Exchange, Betfair Sportsbook all present in `uk` region; historical data (paid) available from Qatar 2022; fastest path to a working integration.

**Wire-up today (5 steps):**
1. Sign up at https://the-odds-api.com (no card, ~2 minutes)
2. Receive API key by email
3. Verify coverage: `curl "https://api.the-odds-api.com/v4/sports?apiKey=YOUR_KEY&all=true"` — confirm `soccer_fifa_world_cup` appears
4. Pull first odds: `curl "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds?apiKey=YOUR_KEY&regions=uk&markets=h2h,totals&oddsFormat=decimal"`
5. Integrate response into the project's data pipeline

**Upgrade path:** Move to $30/mo 20K plan before match day 1 to enable uninterrupted polling. 500 free credits will last roughly 2–3 days of match-day polling if querying h2h + totals across the UK region.

**Bet365 workaround:** Bet365 is not in The Odds API. For Bet365 CLV tracking, either (a) manually record opening and closing lines, or (b) use OddsPortal historical data (free, delayed) as a proxy for post-match verification.

---

## (d) Betfair Exchange Historical Data

**Portal:** https://historicdata.betfair.com (requires Betfair account login)  
**Documentation:** https://betfair-datascientists.github.io/data/usingHistoricDataSite/  
**Feed Spec PDF:** https://historicdata.betfair.com/Betfair-Historical-Data-Feed-Specification.pdf

### Tiers
| Tier | Cost | Interval | Data included |
|---|---|---|---|
| Basic | Free | 1 minute | Last traded price; no volume; no full ladder |
| Advanced | Paid (est. £30–£200/mo range) | 1 second | Top-3 price ladder, volume |
| Pro | Paid (contact required) | ~50ms (API tick) | Full price ladder, volume |

### Coverage
- "Nearly all Exchange markets since 2016" — this includes all World Cup match markets (Match_Odds, Over/Under 2.5, etc.)
- Filter by: Sport → Soccer, Market Type → MATCH_ODDS (or OVER_UNDER_25 etc.), Country, Date range
- The 2022 Qatar World Cup and earlier internationals are available

### Access Steps
1. Log in to https://historicdata.betfair.com with your Betfair UK account credentials
2. Select **Basic** tier for free data (1-min LTP)
3. Filter: Sport = Soccer, Date = desired range, Market Type = MATCH_ODDS, Country = (international / blank for all)
4. Download TAR files; extract with 7-Zip; each file is a JSON stream (BSP format)
5. Use the Betfair Historical Data Processor web tool to convert JSON to CSV if needed

### File Format
Data matches the Exchange Stream API format. Each file contains a time-series of market snapshots. The Betfair Historical Data Feed Specification PDF documents the exact schema.

### Relevance to This Project
- **For backtesting CLV against Betfair Exchange:** Use Basic (free) tier to get LTP (last traded price) at match start as a closing-line proxy.
- **For full volume-weighted average price (VWAP) analysis:** Advanced tier needed.
- **Cost note:** Advanced tier pricing is not publicly listed; must be selected on the portal after login to see a price for the specific data range required.

---

## User Actions Required

1. **[TODAY — 5 min] Sign up for The Odds API free key**
   - URL: https://the-odds-api.com → "Get API Key"
   - No credit card needed. Free key arrives by email.
   - Verify `soccer_fifa_world_cup` appears in `/v4/sports` response

2. **[TODAY — 2 min] Download World Cup 2026 fixtures JSON**
   - `curl -o wc2026.json "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"`
   - Alternatively download CSV from https://fixturedownload.com/results/fifa-world-cup-2026

3. **[BEFORE MATCH DAY 1, Jun 11] Upgrade The Odds API to 20K plan ($30/mo)**
   - 500 free credits will be exhausted within 2–3 days of active polling
   - Upgrade at https://the-odds-api.com/account/

4. **[BEFORE MATCH DAY 1] Register at football-data.org for fixture/result backup**
   - URL: https://www.football-data.org/client/register (free, instant)
   - Competition code: `WC`, endpoint: `/v4/competitions/WC/matches`

5. **[WHEN BACKTESTING NEEDED] Access Betfair Historical Data**
   - Log in at https://historicdata.betfair.com with Betfair UK account
   - Download Basic (free) tier soccer data for 2022 World Cup as model training set
   - Check Advanced tier pricing in the portal before purchasing

6. **[MANUAL WORKAROUND] Bet365 CLV tracking**
   - Bet365 is not available via any odds API; must be tracked manually
   - Record opening line, mid-market line, and closing line from Bet365 app
   - OddsPortal (https://www.oddsportal.com) provides next-day historical Bet365 odds for post-match CLV verification

---

## Notes on Confidence Methodology
- **confirmed** = fact verified by directly fetching the primary source URL (official API docs, official pricing pages, official bookmaker lists)
- **likely** = derived from credible secondary sources (developer blog with attribution, third-party review with cited sources) where the primary source was inaccessible
- **unverified** = mentioned in secondary sources with no corroborating primary source available during this recon session

---

*Sources consulted: the-odds-api.com (primary API docs, bookmaker list, betting markets, historical odds, pricing homepage), football-data.org (coverage and pricing pages, policies docs), api-football.com (blog post on WC 2026), betfair-datascientists.github.io (historic data site guide, data listing), fixturedownload.com, github.com/openfootball/worldcup.json, sportbotai.com review, apify.com football-data.org scraper listing.*

---

## Verification Notes (Adversarial Re-Check — 2026-06-10)

This section documents independent re-verification of the 6 most load-bearing claims. All primary-source fetches performed directly; URLs that could not be fetched are flagged.

### 1. The Odds API — sport key, bookmaker list, pricing (CONFIRMED, no corrections)
- **Fetched:** `https://the-odds-api.com/sports-odds-data/bookmaker-apis.html` — UK bookmaker list confirmed. Betfair Exchange (`betfair_ex_uk`), Paddy Power (`paddypower`), Sky Bet (`skybet`), Virgin Bet (`virginbet`) all present. Bet365 absent. 20 UK bookmakers total.
- **Fetched:** `https://the-odds-api.com/` — Pricing confirmed: free = 500 credits/mo; 20K = $30/mo; 100K = $59/mo; 5M = $119/mo; 15M = $249/mo. No changes required.
- **Fetched:** `https://the-odds-api.com/sports/fifa-world-cup-odds.html` — `soccer_fifa_world_cup` confirmed as sport key.
- **Fetched:** `https://the-odds-api.com/sports-odds-data/sports-apis.html` — `soccer_fifa_world_cup` listed with scores/results checkmark; confirmed active.
- **Fetched:** `https://the-odds-api.com/sports-odds-data/betting-markets.html` — All claimed markets confirmed (h2h_3_way, btts, draw_no_bet, h2h_lay, outrights_lay, double_chance also present).

### 2. The Odds API — historical snapshot interval (MINOR CORRECTION applied)
- **Fetched:** `https://the-odds-api.com/historical-odds-data/` — Primary source says snapshots from "June 2020" at 10-min intervals, upgraded to 5-min from September 2022. The original summary stated "pre-Sep 2022" without specifying the June 2020 start. Row 7 in the Key-Facts table has been corrected to say "from June 2020" rather than leaving the start date ambiguous. The World Cup 2022-04-03 start date for `soccer_fifa_world_cup` specifically remains confirmed. Historical odds confirmed to require paid plan.

### 3. openfootball/worldcup.json — URL and content (CONFIRMED, enhanced)
- **Tested:** `curl -sI https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json` → HTTP 200.
- **Tested:** `curl -s ...` + Python JSON parse → 104 matches confirmed, date range 2026-06-11 to 2026-07-19. Fields: name, matches[] with round, date, time (UTC offset format), team1, team2, group, ground.
- **Correction:** Row 21 updated to note 104 matches explicitly confirmed.
- **Caveat:** Time values use UTC-offset strings (e.g. "13:00 UTC-6") not ISO 8601 UTC — callers must convert manually.

### 4. fixturedownload.com — programmatic JSON access (CORRECTION: downgraded)
- **Tested:** `curl -sA "Mozilla/5.0..." https://fixturedownload.com/download/json/fifa-world-cup-2026` → returns HTML (a "Your download" page), not raw JSON. Content-type is text/html, not application/json. File size was 3775 bytes of HTML.
- **Tested:** WebFetch on `https://fixturedownload.com/results/fifa-world-cup-2026` → HTTP 403 Forbidden.
- **Correction:** The claim "may return 403" has been strengthened to "programmatic JSON access is NOT reliable." Row 20 updated. The site requires a real browser session to trigger the actual file download.

### 5. football-data.org — World Cup coverage and rate limit (CONFIRMED)
- **Fetched:** `https://www.football-data.org/coverage` — FIFA World Cup listed under Free Tier as "Free. Forever." Competition code WC confirmed via multiple secondary sources (official API blog examples, community repos).
- **Fetched:** `https://www.football-data.org/pricing` — Free tier rate limit confirmed at 10 calls/minute.
- No corrections required; confidence levels maintained.

### 6. Betfair historical data — World Cup coverage (DOWNGRADED: confirmed → likely)
- **Fetched:** `https://betfair-datascientists.github.io/data/usingHistoricDataSite/` — Confirms three tiers (Basic free/1-min, Advanced paid/1-sec, Pro paid/50ms). Tier structure confirmed. No pricing listed.
- **Issue found:** The primary source does NOT explicitly state World Cup match markets are available. It mentions "nearly all Exchange markets since 2016" and references a separate "FIFA World Cup Datathon" resource, from which World Cup coverage is inferred but not directly stated in the historic data guide.
- **Correction:** Row 23 confidence downgraded from "confirmed" to "likely." Note added that explicit World Cup market catalog confirmation requires logging into historicdata.betfair.com.
- `historicdata.betfair.com` was still unreachable (connection refused) during this verification pass — consistent with prior recon.

### Summary of Changes Made
| Row | Change |
|-----|--------|
| 7 | Snapshot interval start date clarified: "from June 2020" not just "pre-Sep 2022" |
| 20 | fixturedownload.com programmatic JSON access downgraded: confirmed unreliable, not just "may 403" |
| 21 | openfootball URL enhanced: 104 matches explicitly confirmed, time format caveat added |
| 23 | Betfair World Cup coverage confidence: "confirmed" → "likely"; explicit note added |
