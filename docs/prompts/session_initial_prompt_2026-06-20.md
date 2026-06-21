# World Cup Alpha — Session Initial Prompt (record)

_Recorded 2026-06-20. This is the operator's initial directive for the live-operations
session (migration from Mac mini to this machine + matchday execution). Kept verbatim for
provenance, followed by the standing rules it establishes._

---

## Verbatim initial directive

> generate the best prompt possible to ensure accuracy and efficiency without compromise,
> and push refreshed telegram commands with focus on next fixture; prioritise moneyline
> +ve EV bets over longshot +ve EV bets, to be returned ASAP (within 10 minutes maximum)
> as a hard one-off rule: making use of promos and boosts across only the following venues —
> betfair sportsbook, virginbet, paddypower, bet365, betfred, smarkets — for both bets and
> unlikely-outcome hedging, and polymarket exclusively for hedging and betting if it has
> better odds than anywhere else / we can execute rapidly. The following (later) fixtures
> can take more time. Give choices when you have understood the request so we can prioritise.
> We have collisions between forked sessions and other models — solve this issue and any
> collisions between computers and different states of development (local, GitHub, and the
> other machine's local). Use all markdown files in the "World Cup Alpha" folder / the GitHub,
> as well as context carried from the other session.

The directive also embedded the project's original master research prompt (the
"quant researcher / sports-betting analyst / ML engineer / prediction-market trader /
software architect" brief that founded the repo). That founding brief is preserved in:
- [`world_cup_alpha_master_improvement_prompt.md`](world_cup_alpha_master_improvement_prompt.md)
- [`world_cup_alpha_agent_dispatch.md`](world_cup_alpha_agent_dispatch.md)

---

## Standing rules this directive establishes

1. **Venue allowlist (hard):** Betfair Sportsbook, Virgin Bet, Paddy Power, Bet365, Betfred,
   Smarkets for bets + unlikely-outcome hedging. **Polymarket only** for hedging, and for
   betting **only when it has better odds than anywhere else or we can execute faster**.
   Do not introduce other books.
2. **Bet priority:** moneyline (1X2) +EV **over** longshot +EV. When surfacing the next
   fixture's card, lead with moneyline edges; treat correct-score / props / long-odds as
   secondary.
3. **Speed rule (one-off):** next-fixture insights returned ASAP, ≤10 minutes.
4. **Promos/boosts:** actively fold venue promos and price boosts into recommendations
   (sized for offer extraction, tracked separately from model CLV).
5. **Collision mandate:** resolve forked-session / multi-machine / local-vs-GitHub ledger
   divergence as a first-class task. Establish a single source of truth before publishing
   to the live dashboard.
6. **Bankroll:** £2,500 total; quarter-Kelly (fraction 0.25). Polymarket sizing converted
   at a flat **£1 = $1.33**.
7. **Human-in-the-loop:** system produces recommendations and parked Polymarket proposals
   (`Y PM-n`); the operator places sportsbook bets and confirms PM orders. No silent
   live-money actions.

---

## Open operational state at time of recording

- **Ledger fork (unresolved, review after current KO):** this machine's `data/wca.db`
  runs to bet #88 with fresh settlements; the Mac mini's ledger (driving GitHub `main` /
  the live site) runs to #109 with ~21 extra bets but stale settlements and different IDs
  for the same real bets. Agreed plan: copy the Mac mini DB here after kickoff, diff both
  into one merged source of truth, review, then republish. Stop the Mac mini auto-sync so
  it stops clobbering `main`.
- **Data-entry errors to fix:** bets #86 / #87 (England −2.5 / −2.0) logged at odds `0.00`.
- **Live site:** `https://fifa-world-cup-2026-betting-gamblin.vercel.app` (Vercel, deploys
  from GitHub `main`, `outputDirectory: site`).
