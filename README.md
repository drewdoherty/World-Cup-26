# World Cup Alpha

A $1,000-bankroll quantitative betting research platform for the 2026 FIFA World Cup. This project tests whether systematic +EV betting on international football is achievable through disciplined modelling, market analysis, and staking discipline. We report evidence first, money second.

## Mission

- **Primary goal**: Demonstrate that closing-line value (CLV) can be generated through a combination of international Elo ratings, time-decayed Dixon-Coles modelling, market de-vigging, and data-driven blending.
- **Secondary goal**: Measure calibration (Brier score, log-loss) against a de-vigged market baseline.
- **Bankroll outcome**: Track P&L only as a lagging, noisy confirmation signal (100–300 bets at these stakes cannot statistically resolve an ROI edge).

## Design: V1

### Models
- **International Elo** (World Football Elo Ratings conventions): home advantage baked in, K-factor weighted by match importance.
- **Time-decayed Dixon-Coles**: zero-inflated Poisson model for goal counts, with exponential decay of older matches.
- **Market baseline**: Shin-de-vigged odds (controls for informed trading) against which all other forecasts are benchmarked.
- **Blend**: Logistic or ensemble combination of Elo, Dixon-Coles, and market priors; no ML-heavy features unless CLV proves a signal.

### Staking
- **Quarter-Kelly default**: 25% of full-Kelly stake, hard-capped at 5% of bankroll per bet.
- **Same-day portfolio cap**: 5% total daily exposure across all concurrent bets.
- **Kill-rule**: Recommendations-only; system never places bets automatically. Human review required for each stake.

## Key Performance Indicators

1. **Closing-Line Value (CLV)**: The primary KPI. Measured as the log-ratio of closing odds vs. model-implied fair odds at recommendation time.
2. **Calibration**: Brier score and log-loss vs. de-vigged market baseline; measures forecast accuracy.
3. **Bankroll**: P&L per stake and cumulative ROI; lagging and noisy at this sample size (use for sanity checks only).

### Pre-Registered Pause Rule
If aggregate CLV is negative after 50 consecutive bets, pause real-money recommendations pending diagnostic review.

## Repository Structure

```
World Cup Alpha
├── src/wca/                      # Main package (import as wca.*)
│   ├── models/
│   │   ├── elo.py                # World Football Elo + ordered-logistic outcomes
│   │   └── dixon_coles.py        # Zero-inflated Poisson goal model
│   ├── markets/
│   │   ├── devig.py              # Multiplicative, power, Shin de-vigging
│   │   └── kelly.py              # Fractional Kelly & staking discipline
│   ├── data/
│   │   ├── results.py            # Historical match data ingestion
│   │   ├── theoddsapi.py         # Live odds snapshot capture
│   │   ├── polymarket.py         # Onchain / crypto market sources
│   │   └── snapshot.py           # Market snapshots (for CLV replay)
│   ├── ledger/
│   │   ├── store.py              # Bet recording & balance tracking
│   │   └── reports.py            # CLV, calibration & P&L reports
│   └── sim/
│       └── __init__.py           # Simulation harness (planned)
├── docs/
│   ├── ARCHITECTURE.md           # Component map & design decisions
│   ├── research/                 # Literature & methodology notes
│   └── recon/                    # Market & live data analysis
├── tests/                        # pytest suite
├── data/                         # Historical match results, snapshots
├── backtests/                    # Replay logs & CLV analysis
├── notebooks/                    # Exploratory analysis
├── scripts/                      # CLI tools & batch jobs
├── pyproject.toml                # Package config & dependencies
├── .env.example                  # Template for API keys
└── README.md                     # This file
```

## Quickstart

### 1. Set up environment
```bash
python3 -m venv .venv
source .venv/bin/activate  # or on Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Configure API keys
```bash
cp .env.example .env
# Edit .env with your keys (see below)
```

### 3. Run tests
```bash
pytest
```

### 4. Compute a forecast
```python
from wca.models.elo import EloRatings
from wca.markets.devig import devig_shin
from wca.markets.kelly import stake

# Load ratings, compute win probability, de-vig market odds, compute stake
ratings = EloRatings()
p_home = 0.55  # from model
decimal_odds = 1.90  # market
fair_prob = devig_shin([1/decimal_odds])
stake_amt = stake(p=p_home, odds=decimal_odds, bankroll=1000.0, fraction=0.25, cap=0.05)
```

## Environment & API Keys

Copy `.env.example` to `.env` and fill in your credentials:

```bash
# TheOddsAPI (market snapshot capture)
ODDS_API_KEY=your_key_here

# Betfair (live exchange data)
BETFAIR_APP_KEY=your_app_key
BETFAIR_USERNAME=your_username
BETFAIR_PASSWORD=your_password
```

- **ODDS_API_KEY**: Request from https://theoddsapi.com (free tier available).
- **Betfair credentials**: Register at https://www.betfair.com (UK-based, requires verification).

## Responsible Gambling

⚠️ **This is a research platform for disciplined, data-informed decision-making. Use responsibly.**

- Only risk money you can afford to lose.
- Minimum age: 18+.
- If gambling becomes a problem, visit [GambleAware](https://www.gambleaware.co.uk).
- The $1,000 bankroll is allocated strictly for research; the system never auto-executes bets.
- **Jurisdiction**: This tool is licensed for use in the UK (Paddy Power, Sky Bet, Virgin Bet, Bet365, Betfair). Availability outside the UK depends on local regulations.

## Dependencies

- **numpy** 2.0, **scipy** 1.13, **pandas** 2.3 – numerical computing & data wrangling.
- **requests** – HTTP for API calls.
- **pytest** – testing framework (dev only).

No additional dependencies will be added without justification.

## References

- **Elo System**: https://www.eloratings.net/about
- **Dixon-Coles**: Dixon & Coles (1997), "Modelling association football scores and inefficiencies in the football betting market", *Journal of the Royal Statistical Society* 160(2):357–388.
- **De-vigging**: Clarke, Kovalchik & Ingram (2017), "Adjusting bookmaker's odds to allow for overround", *American Journal of Sports Science* 5(6):45–49. Štrumbelj (2014), "On determining probability forecasts from betting odds", *International Journal of Forecasting* 30(4):934–943.
- **Kelly Criterion**: MacLean, Thorp & Ziemba (2011), *The Kelly Capital Growth Investment Criterion*.

## License

Research use only. See LICENSE file for details.
