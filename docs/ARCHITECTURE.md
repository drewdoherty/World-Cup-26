# Architecture: World Cup Alpha

## Data Flow

```
Match History        Market Snapshots        Betfair Exchange
       │                      │                       │
       └──────────┬───────────┴───────────────────────┘
                  ↓
         Data Ingestion Layer
         (results.py, theoddsapi.py, polymarket.py)
                  │
                  ↓
         Elo Ratings Model          Dixon-Coles Model        Market Odds Snapshots
         (elo.py)                   (dixon_coles.py)         (snapshot.py)
         │                          │                        │
         ├─ Home advantage          ├─ Goal distribution      └─ Raw probabilities
         ├─ K-factors               └─ Time decay                (1/odds)
         └─ Win/draw/loss probs            │
             (ordered-logistic)            └─ Match outcome probs
                  │
                  └──────────────────┬──────────────────────┘
                                     ↓
                          De-vigging Layer
                          (devig.py: Shin, power, multiplicative)
                          │
                          ├─ Fair probabilities
                          └─ Margin estimates
                                     │
                                     ↓
                          Model Blend & Comparison
                          │
                          ├─ Elo vs Shin-devigged
                          ├─ Dixon-Coles vs Shin-devigged
                          └─ Consensus forecast
                                     │
                                     ↓
                          EV & Kelly Staking
                          (kelly.py)
                          │
                          ├─ Edge computation
                          ├─ Kelly fraction (full → fractional)
                          ├─ Hard cap (5% per bet)
                          └─ Portfolio exposure scaling
                                     │
                                     ↓
                          Recommendation
                          │
                          ├─ Forecast probability
                          ├─ Fair odds (de-vigged)
                          ├─ Market odds (closing)
                          ├─ CLV estimate
                          └─ Suggested stake (quarter-Kelly capped)
                                     │
                                     ↓
                          Ledger & Reporting
                          (ledger/{store,reports}.py)
                          │
                          ├─ Bet recording
                          ├─ Settlement & P&L
                          ├─ Closing-line value (CLV) replay
                          ├─ Calibration metrics (Brier, log-loss)
                          └─ Drawdown tracking
```

## Component Summary

### 1. Data Layer (`src/wca/data/`)

| Module | Status | Purpose |
|--------|--------|---------|
| `results.py` | ✓ Implemented | Ingest international match history (martj42/international_results CSV or similar) |
| `theoddsapi.py` | ✓ Implemented | Snapshot live odds from TheOddsAPI (free tier: some markets) |
| `polymarket.py` | ✓ Implemented | Fetch onchain market data (Polymarket, Uniswap, etc.) |
| `snapshot.py` | ✓ Implemented | Store & replay market snapshots for CLV backtesting |

### 2. Models (`src/wca/models/`)

| Module | Status | Purpose | Key Formula |
|--------|--------|---------|-------------|
| `elo.py` | ✓ Implemented | Elo ratings + ordered-logistic outcome model | `E = 1 / (1 + 10^(-dr/400))` for expected score; MCullagh (1980) for proportional odds |
| `dixon_coles.py` | ✓ Implemented | Zero-inflated Poisson model for goals | Dixon & Coles (1997) |

### 3. Markets Layer (`src/wca/markets/`)

| Module | Status | Purpose | Key Variants |
|--------|--------|---------|-------------|
| `devig.py` | ✓ Implemented | Remove bookmaker margin from odds | Multiplicative (fastest), power (odds-ratio), Shin (informed trading model) |
| `kelly.py` | ✓ Implemented | Stake sizing & portfolio exposure control | `f* = (p*o - 1)/(o-1)` (full Kelly); fractional (0.25 default); capped |

### 4. Ledger (`src/wca/ledger/`)

| Module | Status | Purpose |
|--------|--------|---------|
| `store.py` | ✓ Implemented | Record bets, settlements, ledger state |
| `reports.py` | ✓ Implemented | CLV, Brier score, log-loss, ROI, drawdown |

### 5. Simulation (`src/wca/sim/`)

| Module | Status | Purpose |
|--------|--------|---------|
| `__init__.py` | 🔲 Planned | Harness for forward-testing recommendations against live matches |

## Key Design Decisions

### 1. Shin De-vigging as Baseline
We use Shin (1993) devigging for all market comparisons because it explicitly models informed trading, giving us a sophisticated baseline hypothesis. De-vigging is the first filter: a bet passes through only if our model disagrees with the de-vigged market fair probability in a profitable direction.

### 2. Fractional Kelly with Hard Cap
Full-Kelly is mathematically growth-optimal but empirically too aggressive. We default to **quarter-Kelly** (25% of optimal) capped at **5% of bankroll per bet**. A same-day portfolio cap of **5% total exposure** prevents cascading losses if multiple outcomes on the same slate go wrong.

### 3. CLV as Primary KPI
Closing-line value (log-ratio of closing odds vs model-implied fair odds) is the most information-dense metric for a research platform. Unlike P&L (noisy and lagging), CLV immediately reflects whether you're beating the market at recommendation time.

### 4. Model Agnosticism
V1 uses Elo + Dixon-Coles (proven in football). ML ("features") is *not* added unless CLV backtests prove a signal. This respects the curse of dimensionality and overfitting at small sample sizes (~100–300 matches).

### 5. Ledger-Driven Feedback
Every bet goes into a ledger with metadata (model probs, market odds, stake, settlement odds, outcome). The ledger is the single source of truth for CLV, calibration, and P&L analysis.

## Build Status

### Implemented
- ✓ Core data loaders (results, TheOddsAPI, Polymarket snapshots)
- ✓ International Elo system with ordered-logistic outcome model
- ✓ Dixon-Coles zero-inflated Poisson framework
- ✓ Three de-vigging methods (multiplicative, power, Shin)
- ✓ Kelly staking with fractional scaling and exposure caps
- ✓ Ledger recording and CLV/Brier/log-loss reporting
- ✓ pytest suite covering models and markets

### In Progress / Planned
- 🔲 Live blend logic (composite forecast from Elo + DC + market)
- 🔲 Simulation harness for forward-testing
- 🔲 Dashboard / CLI for daily recommendation generation
- 🔲 Integration with betting exchanges (Betfair, UK bookies) for odds pull & settlement

## Testing

All models are tested against:
1. **Unit tests**: correctness of staking, de-vigging, outcome probabilities.
2. **Numerical tests**: match published benchmarks (e.g., Elo expected scores, Kelly fractions).
3. **Integration tests**: data ingestion → model → ledger → reports.

Run tests with:
```bash
cd "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha"
./.venv/bin/pytest tests/ -v
```

## Dependencies & Constraints

- **Python 3.9+**: No match statements, no `X | Y` union syntax in annotations.
- **Minimal external libs**: numpy, scipy, pandas, requests only. No sklearn, no TensorFlow.
- **Scientific code**: All algorithms cited (source/paper in docstrings).

## References

1. Elo ratings: https://www.eloratings.net/about
2. Dixon & Coles (1997): "Modelling association football scores..." *JRSS-B* 160(2).
3. Shin (1993): "Measuring the incidence of insider trading..." *Economic Journal* 103(420).
4. Kelly criterion: MacLean, Thorp & Ziemba (2011), *Kelly Capital Growth Investment Criterion*.
5. McCullagh (1980): "Regression Models for Ordinal Data", *JRSS-B* 42(2).
