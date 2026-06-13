# Dixon-Coles structural (socio-economic) shrinkage-prior backtest

Walk-forward, out-of-sample test of the optional structural shrinkage prior (`wca.card.fit_models(structural_prior=True)`, default **off**). **Evidence only** — `card.py` is not modified by this study. Prior scale = 0.15; deployed half-life = 8; low-data threshold = 5 training matches.

The prior shrinks low-data teams toward a socio-economic estimate (population x football culture, an inverted-U in GDP/capita, confederation) instead of the global mean. The hypothesis is that it helps the **low-data subset** (matches with a minnow) and is ~neutral on the full set.

## Aggregate (match-count-weighted log-loss / Brier)

| subset | n | baseline LL | structural LL | dLL | baseline Brier | structural Brier |
|---|---:|---:|---:|---:|---:|---:|
| all holdout matches | 211 | 0.9785 | 0.9786 | -0.0001 | 0.5793 | 0.5794 |
| low-data subset | 0 | — | — | — | — | — |

(dLL > 0 means the structural prior has the lower — better — log-loss.)

> **The low-data subset is empty.** Every team appearing in these tournament-finals holdouts has more than 5 matches over the full international history, so the extra low-data shrinkage the prior targets never engages here. This holdout therefore **cannot** test the prior's core hypothesis; the all-matches row only confirms the prior is inert for data-rich teams (as designed).

## Per-holdout (low-data subset log-loss)

| block | n | low-n | baseline LL | structural LL | dLL |
|---|---:|---:|---:|---:|---:|
| WC2018 | 64 | 0 | — | — | — |
| WC2022 | 64 | 0 | — | — | — |
| Euro2024+Copa2024 | 83 | 0 | — | — | — |

## Verdict

**Inconclusive — and unfixable on this holdout.** The low-data subset is empty, so the prior's core hypothesis cannot be tested here, and on the full set the prior is inert (-0.0001 log-loss). There is no evidence to enable it, and these historical tournament finals cannot produce that evidence (they contain no data-poor teams). Keep `structural_prior=False` (the default); the real test is the 48-team 2026 field's minnows on thin Polymarket outright/advancement markets, evaluated on live data.

_Caveat: recent men's tournament holdouts are dominated by data-rich teams, so the low-data subset is small or empty and noisy. The prior's real test is the 48-team 2026 field's minnows on thin Polymarket markets, which this historical holdout can only approximate._
