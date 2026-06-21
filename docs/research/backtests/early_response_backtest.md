# Early Response Backtest

## Data Source

- Ledger: `data/wca.db`
- Rows: settled `won`/`lost` bets with non-null `decimal_odds`, `closing_odds`, `clv`, `stake`, and `settled_pl`.
- Model-edge buckets use the stored `ev` field where present; rows without `ev` are reported in `missing edge`.
- Closing line policy: uses the already-captured `closing_odds` and `clv` fields on settled bets; this report does not re-derive closing prices.
- Related committed analysis file: `data/analysis/clv_by_bet.csv` documents the CLV-by-bet convention but does not contain stake/P&L, so ROI is computed from the ledger.

## Results

| Cohort | n | Date range | Stake | P&L | ROI | Avg CLV |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| overall | 0 | n/a | 0.00 | 0.00 | nan | nan |
| missing edge | 0 | n/a | 0.00 | 0.00 | nan | nan |
| <0% | 0 | n/a | 0.00 | 0.00 | nan | nan |
| 0% to <5% | 0 | n/a | 0.00 | 0.00 | nan | nan |
| 5% to <10% | 0 | n/a | 0.00 | 0.00 | nan | nan |
| 10% to <20% | 0 | n/a | 0.00 | 0.00 | nan | nan |
| >=20% | 0 | n/a | 0.00 | 0.00 | nan | nan |

## Verdict

**insufficient sample**

- overall n=0 is below min_overall_n=1
- >=20% edge n=0 is below min_bucket_n=1

Claims under test:

- Overall ROI: `14.42%`
- `>=20%` model-edge cohort ROI: `-6.90%`
