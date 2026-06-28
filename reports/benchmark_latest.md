# Benchmark report — generated card & commands vs actual outcomes
_Generated: 2026-06-27T07:59:57+00:00_

## Sources & coverage
- Predictions: **jsonl** (576 builds → 54 latest-per-fixture)
- Results loaded: **1018** | Closing lines: **72** | Bets: **db**

## 1. Model 1X2 calibration & discrimination
- Fixtures scored: **39** (117 legs)
- Argmax hit-rate: **+66.7%** (95% CI +51.0%…+79.4%, 26/39)
- Brier — model **0.482** vs market **0.479** | skill vs market **-0.7%**
- Log-loss (model): **0.810** | ECE (legs, 10-bin): **0.083**

| pred bin | n | mean pred | realized | 95% CI |
|---|---:|---:|---:|---|
| [0.0,0.2) | 39 | 0.120 | 0.103 | 0.041…0.236 |
| [0.2,0.4) | 40 | 0.255 | 0.225 | 0.123…0.375 |
| [0.4,0.6) | 15 | 0.506 | 0.600 | 0.357…0.802 |
| [0.6,0.8) | 18 | 0.676 | 0.722 | 0.491…0.875 |
| [0.8,1.0) | 5 | 0.875 | 0.800 | 0.376…0.964 |

## 2. Walk-forward CLV (model fair vs consensus close)
- Fixtures matched to a close: **51** (153 legs)
- CLV mean **-1.2%** | median **-1.3%** | trimmed **-1.6%** | beat-close **+43.8%**
- **+EV-flagged legs (edge ≥ 2%)**: n=42, CLV mean **-11.4%**, beat-close **+14.3%** (this is the headline edge-validity check)

| build-time edge bucket | n | CLV mean | beat-close |
|---|---:|---:|---:|
| [-1.00,-0.02) | 34 | +4.9% | +82.4% |
| [-0.02,+0.00) | 33 | +6.6% | +69.7% |
| [+0.00,+0.02) | 44 | -2.1% | +22.7% |
| [+0.02,+0.05) | 36 | -9.6% | +13.9% |
| [+0.05,+0.10) | 6 | -21.9% | +16.7% |

## 3. Realized ledger (placed bets)
- Bets: **77** total, 69 settled, 62 decided
- Overall: staked £431.76, P&L £172.32, ROI **+39.9%**, mean CLV **+0.3%** (n=25)

| market family | n | staked | P&L | ROI | win% | mean CLV |
|---|---:|---:|---:|---:|---:|---:|
| correct_score | 11 | £38.37 | £-28.37 | -73.9% | +9.1% | +17.5% |
| btts | 1 | £8.0 | £-8.0 | -100.0% | +0.0% | — |
| cards | 1 | £1.0 | £-1.0 | -100.0% | +0.0% | — |
| corners | 1 | £1.0 | £-1.0 | -100.0% | +0.0% | — |
| other | 1 | £0.94 | £-0.94 | -100.0% | +0.0% | — |
| shots_on_target | 2 | £15.0 | £5.0 | +33.3% | +50.0% | — |
| goalscorer | 3 | £20.0 | £22.75 | +113.8% | +66.7% | — |
| acca_betbuilder | 12 | £114.0 | £30.83 | +27.0% | +16.7% | +0.0% |
| 1x2 | 30 | £233.45 | £153.05 | +65.6% | +36.7% | -0.4% |

| venue | n | staked | P&L | ROI | win% | mean CLV |
|---|---:|---:|---:|---:|---:|---:|
| betfred | 4 | £40.0 | £-19.0 | -47.5% | +25.0% | — |
| virginbet | 6 | £30.25 | £-17.5 | -57.9% | +16.7% | -2.9% |
| smarkets | 14 | £82.69 | £-11.31 | -13.7% | +14.3% | -1.4% |
| paddy power | 1 | £5.0 | £-5.0 | -100.0% | +0.0% | — |
| polymarket | 14 | £59.62 | £4.49 | +7.5% | +21.4% | +1.5% |
| bet365 | 2 | £17.0 | £51.97 | +305.7% | +100.0% | -2.9% |
| paddypower | 3 | £25.0 | £55.76 | +223.0% | +100.0% | +0.0% |
| betfair | 18 | £172.2 | £112.91 | +65.6% | +27.8% | +3.2% |

---
_Caveat: computed from a copy of the dev-box ledger, which forks from the canonical mini ledger; small samples — treat as harness validation, not the production scorecard._
