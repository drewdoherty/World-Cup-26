# World Cup Alpha — Ready for Live (Tonight)

## ✅ Complete (Just Done)

### 1. **Bot Default Changed: 'model' (not 'punt')**
- All betslip screenshots now default to `source='model'` in the ledger
- You can still override with `yes a2 offer` or `yes punt` if needed
- A1/A2 account tags apply to sportsbooks only
- Polymarket orders are always `source='punt'` (override in caption if needed)

### 2. **Automated Card Generation (GitHub Actions)**
**What:** Daily 8 AM UTC cron job runs:
```bash
python scripts/wca_build_card.py --hours-ahead 48
python scripts/wca_site.py
```

**Commits:** Generated card + site data to `main` automatically

**Setup:** ✅ Done — just push to main and GitHub Actions runs on schedule

**Override:** If you want to trigger manually:
```bash
gh workflow run daily-card.yml
```

---

### 3. **Automated Odds Snapshot Ingestion (Hourly)**
**What:** Every hour, pull fresh odds from TheOddsAPI and store in `odds_snapshots` table

**Why:** Tracks line movement (do odds move in your favour after you bet?)

**Usage:** Automatic via ``.github/workflows/hourly-odds.yml` — no manual trigger needed

**Fallback:** Run manually:
```bash
python scripts/wca_snapshot_odds.py
```

---

### 4. **Settlement Logging**

**Option A: Via Telegram Bot** (Fastest)
```
/settle 42 won 3.20   # bet 42 won at odds 3.20
/settle 43 lost       # bet 43 lost (closed at backing odds)
/settle 44 void       # bet 44 voided
```

Bot computes:
- Realized P&L (stake × odds for wins, -stake for losses)
- CLV (log-ratio of closing vs fair odds)
- Updates ledger immediately

**Option B: Via CLI**
```bash
python scripts/wca_settle.py --db data/wca.db --bet-id 42 --outcome won --closing-odds 3.20
```

**Option C: Manual SQL** (not recommended, error-prone)

---

## 📋 What You Need to Do Tonight

### Before First Kickoff (June 12, 8:20 PM Bahrain time)

1. **Verify everything works:**
   ```bash
   # Test card generation
   python scripts/wca_build_card.py --hours-ahead 48
   
   # Verify site updates
   python scripts/wca_site.py
   
   # Test Telegram bot
   /summary      # should show portfolio
   /card         # should show picks
   /scores       # should show predictions
   /ping         # should respond "pong"
   ```

2. **Verify ODDS_API_KEY is set in GitHub Secrets:**
   - Go to https://github.com/drewdoherty/World-Cup-26/settings/secrets/actions
   - Confirm `ODDS_API_KEY` exists
   - If not, add it

3. **Test betslip parsing:**
   - Send a test betslip screenshot to the bot
   - Reply `yes` to confirm
   - Check that it appears as a `source='model'` bet in `/summary`

---

### After Each Match Settles

**Spend 5 minutes to:**

1. Check your books (Betfair, Smarkets, etc.)
2. Note the closing odds
3. For each settled bet: `/settle <id> <outcome> <closing-odds>`
4. Bot replies with P&L + CLV

**Example workflow:**
```
Qatar vs Switzerland finished 0-1 for Switzerland
Bet #12 (Qatar @ 6.50) lost → /settle 12 lost
Bet #13 (Draw @ 4.00) lost → /settle 13 lost
Bet #14 (Qatar or Draw @ 3.20) won @ 4.50 → /settle 14 won 4.50
```

---

## 🔄 What Runs Automatically (No Action Needed)

| Task | Schedule | What Happens |
|------|----------|--------------|
| Card generation | 8 AM UTC daily | Rebuilds picks + scorelines, commits to `main` |
| Site data refresh | 8 AM UTC daily | Updates website with latest predictions + positions |
| Odds snapshots | Every hour | Pulls odds, stores in `odds_snapshots` table for charts |

---

## 🎯 Your Workflow (Tonight & Beyond)

```
Morning (8 AM)
├─ GitHub Actions: card + site refresh (auto)
├─ You: check /card for new picks
└─ You: place any bets from the card

During Day
├─ GitHub Actions: hourly odds snapshots (auto)
├─ You: monitor positions via /summary or website
└─ You: receive alerts if you set them up (future)

Evening (After matches)
├─ GitHub Actions: auto-ingest odds results (future)
├─ You: settle each bet with /settle <id> <outcome> <odds>
├─ Bot: auto-compute P&L + CLV
└─ You: check CLV report `/clv` next morning
```

---

## ⚙️ GitHub Actions Secrets You Need

Go to [Settings → Secrets](https://github.com/drewdoherty/World-Cup-26/settings/secrets/actions) and verify:

- ✅ `ODDS_API_KEY` — required for card generation + odds pulls

That's it. Everything else is local.

---

## 📞 If Something Breaks

### Card Generation Fails
- Check: GitHub Actions workflow logs (https://github.com/drewdoherty/World-Cup-26/actions)
- Try manually: `python scripts/wca_build_card.py --hours-ahead 48`
- Look for: API quota exceeded, network error, or model fitting crash

### Bot `/settle` Command Fails
- Check that bet ID exists: `/bets` to list open bets
- Check syntax: `/settle 42 won 3.20` (bet-id, outcome, odds)

### Site Data Not Updating
- Manual trigger: `python scripts/wca_site.py`
- Check that card file exists: `ls -lh data/card_latest.md`

---

## 🚀 Next Phase (Tomorrow & Beyond)

Once tonight is stable:

1. **Add morning brief** (8 AM bot message with portfolio summary)
2. **Add match alerts** (bot messages 1 hour before each match you have bets on)
3. **Add new-pick notifications** (bot alerts when card updates with new edge >15%)
4. **Enhance charts** (website line-movement chart powered by hourly snapshots)

---

## Summary

**You're ready to go live.** Here's what's automated:

✅ Card generation (8 AM UTC)  
✅ Site data refresh (8 AM UTC)  
✅ Odds snapshots (hourly)  
✅ Bet settlement (/settle command)  

**Manual tasks (5 mins per match):**
- Settle bets after they finish

**Monitor:**
- Bot: `/summary`, `/card`, `/scores`, `/bets`
- Website: http://localhost:8000 (localhost-only; Vercel removed 2026-07-08)

Let me know if anything breaks or you need adjustments!
