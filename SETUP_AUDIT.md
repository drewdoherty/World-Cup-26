# World Cup Alpha — System Audit & Setup Checklist

## What You Have (Currently Working)

### 1. **Ledger (Database)**
- ✅ Bets table with full metadata (odds, stake, model prob, EV, CLV)
- ✅ Bet status tracking (open → won/lost/void)
- ✅ P&L calculation (realized P&L per bet)
- ✅ CLV tracking (closing-line-value per bet)
- ✅ Bankroll events (history of bankroll changes)
- ✅ Polymarket order log (parked + executed orders)
- ✅ Odds snapshots (line movement history)
- ✅ Sportsbook offers tracker

### 2. **Model & Card Generation**
- ✅ Elo ratings + ordered-logistic outcome model
- ✅ Dixon-Coles zero-inflated Poisson model
- ✅ Shin devigging (market de-vig)
- ✅ Kelly staking (quarter-Kelly + 5% cap)
- ✅ Full blend (Elo + DC + market consensus)
- ✅ Scoreline predictions (top-k correct-score ladder)
- ✅ Edge computation (model prob vs best available market odds)
- ✅ Card caching (`data/card_latest.md`) with picks + scorelines

### 3. **Telegram Bot**
- ✅ /summary — portfolio P&L, ROI, CLV by pool/venue
- ✅ /bets — open positions, max win/max loss
- ✅ /clv — closing-line-value calibration report
- ✅ /card — today's recommended bets
- ✅ /scores — predicted scorelines per fixture
- ✅ /structure — project metrics snapshot
- ✅ /pm — Polymarket parked orders + status
- ✅ Betslip screenshot parsing (Claude vision + OCR)
- ✅ Bet confirmation flow (Y/N with optional account/source tags)
- ✅ Admin gating (optional, per TELEGRAM_ADMIN_USER_ID)

### 4. **Website Dashboard**
- ✅ Live venue breakdown (£ sportsbook, $ prediction markets)
- ✅ Open positions (full table with stake, odds, model prob, EV)
- ✅ Closed positions (realized P&L, CLV per bet)
- ✅ Predictions // Scorelines (fixture-level forecasts with probabilities)
- ✅ Line movement chart (implied probability over time)
- ✅ P&L chart (cumulative realized P&L, sportsbook vs PMs)
- ✅ Cumulative stake chart (total wagered by currency)
- ✅ Links to Scores & Markets page + Architecture page

### 5. **Data Snapshots**
- ✅ Timestamped snapshots (card, site_data.json, metadata)
- ✅ CLI tool for list/create/compare
- ✅ Metadata capture (model version, blend weights, bankroll, CLV)

---

## What You're Missing (For Tonight & Beyond)

### 🔴 **CRITICAL — Blocking Live Deployment**

#### 1. **Automated Card Generation (Cron/Scheduler)**
**Status:** Manual only — you must run `wca_build_card.py` by hand.

**Needed:**
- Cron or cloud scheduler to run `wca_build_card.py` daily (or hourly)
- Pulls fresh odds from TheOddsAPI (costs credits)
- Fits models, generates picks, writes `data/card_latest.md`
- Triggers `wca_site.py` to regenerate `site/data.json` for the website

**Example:**
```bash
0 8,14,20 * * * cd /path && .venv/bin/python scripts/wca_build_card.py >> logs/card.log 2>&1
```

---

#### 2. **Automated Site Data Refresh (After Card Updates)**
**Status:** Manual only — you must run `wca_site.py` by hand.

**Needed:**
- Auto-trigger after card generation
- Pulls ledger + card + snapshots
- Regenerates `site/data.json` + `site/linemove.json`
- Website polls this JSON for live updates

---

#### 3. **Odds Snapshot Ingestion (Line Movement Tracking)**
**Status:** Partially implemented (`odds_snapshots` table exists, but ingestion is incomplete).

**Needed:**
- Periodic pulls from TheOddsAPI (e.g., every 30 mins or hourly)
- Store raw odds feed in `odds_snapshots` table
- Track line movement (how prices shift over time)
- Feed line movement chart on the website

**Why it matters:** Validates that model edges are real (not just model error). If odds move in your favour after you bet, that's external validation.

---

#### 4. **Live Bet Placement (or Easy Manual Workflow)**
**Status:** None — currently fully manual. You must log bets via the Telegram bot (screenshot → parse → confirm).

**Options:**
- **Option A (Recommended for safety):** Keep manual but speed it up.
  - Bot parses betslip screenshots ✅
  - You confirm via `yes a2 offer` in Telegram ✅
  - Bot writes to ledger ✅
  - **Missing:** Auto-sync to live website (you must run `wca_site.py` after each bet to see it live)

- **Option B (Risky, not recommended yet):** API-driven auto-placement.
  - Requires Betfair / Smarkets / other bookmakers' APIs
  - Auth token management
  - Error handling (connection drops, odds changes, order rejection)
  - **For now:** Skip this. You're not at scale yet.

---

#### 5. **Settlement & P&L Tracking**
**Status:** Manual + template.

**Current flow:**
- You place a bet (Telegram bot logs it as `status='open'`)
- Match settles
- You manually query the bet status
- You record the closing odds + settled P&L manually

**Needed:**
- Either:
  - **Option A (Manual):** Daily task: check settled bets on your books, record closing odds + P&L via Telegram bot
  - **Option B (Automated):** API integration with each venue to auto-pull settled results (Betfair offers a results feed)

**For tonight:** Option A is fine. Just dedicate 5 mins after each match to log the result.

---

### 🟡 **IMPORTANT — Makes Life Much Easier (Not Blocking)**

#### 6. **Match Schedule + Kickoff Alerts**
**Status:** Calendar file created (but not integrated into bot/dashboard).

**You have:** `World_Cup_2026_Calendar_Bahrain.ics` (timestamped fixtures, 1-hour pre-match alerts).

**Needed:**
- Bot command like `/next` or `/upcoming` to show next 3 fixtures + kickoff times
- Bot should send a proactive alert 1 hour before each match you have open positions on
- Or integrate into the website ticker (show live countdown to next match)

---

#### 7. **Daily Summary / Morning Brief**
**Status:** `/summary` exists but must be manually requested.

**Needed:**
- Bot sends a daily morning message (e.g., 8:00 AM Bahrain time) with:
  - Portfolio summary (total wagered, open stake, settled P&L, CLV)
  - Today's fixtures (with your recommendations from the card)
  - Open positions that settle today
  - Any overnight news/updates

---

#### 8. **Bet Recommendation Alerts (New Picks from Card)**
**Status:** You can call `/card` anytime, but no proactive notification.

**Needed:**
- Bot sends a message when the card is updated (e.g., 8 AM after overnight card generation)
- Shows the new picks with edges + stakes
- Maybe a summary: "5 new picks, avg edge 12%, total stake £25"

---

#### 9. **Real-Time Position Monitoring (Website Auto-Refresh)**
**Status:** Website shows data, but you must manually refresh your browser.

**Needed:**
- Website `app.js` polls `site/data.json` every 30-60 seconds
- Or WebSocket for true real-time updates (overkill for now)

---

#### 10. **Bankroll Ladder / Account Switching**
**Status:** Ledger supports `account` field, but no UI to switch pools or view breakdown.

**You have:** 
- Sportsbook accounts (Pool 1, Pool 2 in GBP)
- Polymarket account (USD)
- Kalshi account (USD)

**Needed:**
- Bot command `/accounts` to show current bankroll per account
- Dashboard UI to toggle between account views
- Clear labeling of which pool each bet goes to

---

### 🟢 **NICE TO HAVE (Lower Priority)**

#### 11. **Model Validation Dashboard**
- Backtests on historical tournaments (already done)
- But: live calibration report (Brier score, log-loss, gap analysis)

#### 12. **Competitor Line Feeds**
- Track moves from specific bookmakers (which one moved first, by how much)

#### 13. **Advanced Analytics**
- Edge attribution (which model component earned the edge — Elo, DC, or market?)
- Per-venue performance breakdown

---

## What To Do Tonight

### Phase 1: Get to Live (Tonight)
1. ✅ **Verify the model card generates correctly**
   - Run: `python scripts/wca_build_card.py --hours-ahead 48`
   - Confirm: `data/card_latest.md` has picks + scorelines
   - Confirm: picks are visible on `/card` in Telegram

2. ✅ **Verify the website updates**
   - Run: `python scripts/wca_site.py`
   - Confirm: `site/data.json` has predictions
   - Visit: https://fifa-world-cup-2026-betting-gamblin.vercel.app
   - Confirm: Predictions section shows scorelines

3. ✅ **Verify Telegram bot**
   - Send `/summary` — should show portfolio status
   - Send `/card` — should show today's picks
   - Send a test betslip screenshot — should parse correctly

4. ⚠️ **Set up daily card generation cron**
   - Add to your crontab or GitHub Actions or cloud scheduler:
   ```bash
   0 8 * * * cd /path/to/World\ Cup\ Alpha && .venv/bin/python scripts/wca_build_card.py
   0 8 * * * cd /path/to/World\ Cup\ Alpha && .venv/bin/python scripts/wca_site.py
   ```

5. ⚠️ **Manual settlement workflow for tonight**
   - After each match settles:
     - Check your books for the closing odds
     - Send: `Y BET-<id>` to confirm settlement (or use Telegram to log the closing odds + result)
     - Wait for the bot to update the ledger

### Phase 2: Polish (This Week)
1. Add `/next` command (upcoming fixtures + times)
2. Add daily morning brief (8 AM Bahrain time)
3. Add bet alert when card updates
4. Enable website auto-refresh

### Phase 3: Scale (Next Week)
1. Automate settlement (API integration with Betfair or similar)
2. Add `/accounts` command for multi-pool visibility
3. Model validation dashboard

---

## Summary: What's Blocking You?

**For LIVE tonight:**
- ✅ Model predictions ← READY
- ✅ Betting bot ← READY
- ✅ Dashboard website ← READY
- ⚠️ **Automated card refresh ← NEEDS SETUP** (cron or cloud scheduler)
- ⚠️ **Automated site refresh ← NEEDS SETUP** (follows card generation)
- ⚠️ **Manual settlement workflow ← YOU MUST DO THIS BY HAND** (5 mins per match)

**For smooth operation:**
- ✅ Snapshot system for model tracking ← JUST ADDED
- ⚠️ Kickoff alerts ← NEEDS BOT COMMAND
- ⚠️ Morning brief ← NEEDS SCHEDULER + BOT MESSAGE
- ⚠️ New-pick alerts ← NEEDS SCHEDULER + BOT MESSAGE

---

## Questions to Answer Before Live

1. **How often do you want the card rebuilt?**
   - Option A: Once daily at 8 AM
   - Option B: Hourly (to catch odds moves)
   - Option C: Manual (you trigger it)

2. **Do you want proactive bot alerts?**
   - Option A: Just alerts when you request `/card`
   - Option B: Daily 8 AM brief + new-pick notifications
   - Option C: Full alerts (next match, 1 hour before kickoff, etc.)

3. **Settlement: Manual or Automated?**
   - Option A: Manual (you check books, log via bot)
   - Option B: Betfair API (auto-pull results)

4. **Which bookmakers / exchanges will you bet on?**
   - Listed in the card: Betfair, Smarkets, Matchbook, Coral, others?
   - This affects: which odds snapshots to track, which APIs to integrate
