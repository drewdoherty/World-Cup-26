# Kelly Rung Review — 2026-06-15

Review of the CLV-gated Kelly ladder (`wca.markets.kelly.KellyPolicy` +
`wca.card.resolve_pool_bankroll`) now that a meaningful number of bets have
settled.

## Ladder design (unchanged)

| Rung | Bankroll | Kelly fraction | Promotion gate |
|------|----------|----------------|----------------|
| 0 | £1,500 | 0.25 | base |
| 1 | £2,500 | 0.35 | ≥50 settled-with-close **AND** to-date CLV > 0 |
| 2 | £5,000 | 0.50 | ≥100 settled-with-close **AND** to-date CLV > 0 |

Demotion: rolling-50 CLV < 0 steps the rung (and bankroll) back down one.
Rung 0 also filters out recommendations above 10.0 decimal odds.

## Current evidence (ledger as of 2026-06-15)

- **60 bets settled** (17 won / 43 lost), realised P&L **+£188.32**.
- **Only 25 settled WITH closing odds recorded** — and closing-odds capture is
  the ladder's *currency*, not raw settled count.
- **To-date CLV = −0.0005** (essentially flat, marginally negative).
- **Rolling-50 CLV = n/a** (fewer than 50 with-close bets exist).

## Verdict: hold at rung 0 — correct, and not yet promotable

The ladder is behaving exactly as designed. Two independent gates both block
promotion to rung 1, and either alone is sufficient to hold:

1. **Coverage**: 25 / 50 settled-with-close. Half-way to the count threshold.
2. **Edge**: to-date CLV is negative (−0.0005), so even at 50 bets the CLV>0
   gate would currently fail.

No parameter change is warranted. Promoting now would lever up a strategy that
has **not yet demonstrated positive closing-line value** — precisely the failure
mode the ladder exists to prevent.

## The real finding: closing-odds capture is the binding constraint

The headline "60 settled bets" overstates ladder progress: **35 of those 60
(58%) have no `closing_odds`**, so they are invisible to the ladder and to the
CLV kill-rule. The limiting factor on promotion is therefore **not** performance
— it's **close-capture coverage**.

Recommended action (not a ladder change): improve `closecapture.py` coverage so
that settled bets reliably carry closing odds. Each uncaptured close is a settled
bet that can never count toward rung 1 and never informs the CLV signal. At the
current ~42% capture rate, the desk would need ~120 settled bets to reach the 50
with-close needed for rung 1 — more than double the nominal threshold.

## Kill-rule status

The kill rule (pause real money if avg CLV < 0 after ~50 with-close bets) is
**not yet triggered**: it is measured on with-close bets (25 < 50). CLV is flat
rather than decisively negative, so there is no pause signal — but it is worth
watching as coverage grows, since to-date CLV is on the wrong side of zero.
