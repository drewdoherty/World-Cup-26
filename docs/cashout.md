# Event-driven cash-out

Sell an open Polymarket position when a live match event invalidates it — we
hold "0-0" and a goal goes in, "Under 2.5" and the 3rd goal, "BTTS No" and both
teams score — to capture residual value before Polymarket's book reprices to ~0.
The thesis is a latency arb on Polymarket's slow reaction; the risk is a goal
that VAR chalks off, which the **cooldown** below manages.

> Reality check: the edge is real in direction but small in magnitude, and
> structurally weakest on the clean binary kills (the marginal bidder watches
> the same TV; market-makers cancel fastest). On a longshot already priced near
> $0 there is almost nothing to capture. Treat SHADOW as the experiment that
> tells you whether LIVE is worth arming. See the kill criteria at the bottom.

## How it works

```
positions (data-api)  ─┐
                       ├─▶ wca.pm.cashout.decide_cashout ─▶ SELL? ─▶ wca.bot.app.execute_cashout
live scores (Odds API) ─┘        (kill predicates,            (place FOK, read the ACTUAL fill,
order book (CLOB)                 orientation, book pricing)    book via settle_cashout)
```

- **Detection** — `decide_cashout` classifies each held position (exact-score /
  totals / BTTS are the binary kills), maps the live score to the position's
  teams (`orient_score`, skips rather than guesses on any name mismatch), and
  applies the kill predicate. Only a *killed* position with real book value is a
  sell.
- **Execution** — `execute_cashout` places a **FOK** (fill-or-kill) SELL sized to
  the book, reads the *actual* fill, and books only what filled via
  `settle_cashout` (status `cashed`, realised P&L = proceeds − cost basis). It
  never creates a phantom open long.
- **Daemon** — `scripts/wca_cashout_watch.py` runs the loop during a match window
  with a VAR cooldown, dedup, kill switch, and single-instance lock.

## Safety ladder (opt up one rung at a time)

| Mode | Invocation | Behaviour |
|------|-----------|-----------|
| **SHADOW** (default) | `wca_cashout_watch.py` (no `--arm`) | Logs what it *would* sell. Never signs, never claims, never sells. |
| **DRY-ARM** | `--arm` with `PM_DRY_RUN=1` | Exercises the whole path: signs the order but does **not** submit, does **not** book. Doesn't lock the position. |
| **LIVE** | `--arm` with `PM_DRY_RUN=0` | Places real cash-out SELLs. |

Other rails (all on by default): **VAR cooldown** (`--var-cooldown`, default 45s —
a kill must persist this long before selling; a score that ticks back down
cancels the pending sell); **dedup** (a token is never sold twice, across ticks
or restarts; the cooldown clock is persisted so a restart doesn't reset it);
**min-proceeds floor** (`--min-proceeds`, default $1 — never dump for ~nothing);
**price floor** (`--price-floor`); **kill switch** (`WCA_CASHOUT_OFF=1` or a
`data/CASHOUT_OFF` file pauses selling without stopping the daemon); **flock**
(one watcher at a time).

## Commands

```bash
# Inspect every held killable position + what a goal would do (no orders, free):
python scripts/wca_cashout_watch.py --once --var-cooldown 0

# Continuous SHADOW measurement through a match window (logs only):
python scripts/wca_cashout_watch.py --until 2026-06-13T23:00:00Z --interval 15

# Armed but not submitting (wiring test):
PM_DRY_RUN=1 python scripts/wca_cashout_watch.py --arm --until <ISO>

# LIVE auto-sell (real money):
PM_DRY_RUN=0 python scripts/wca_cashout_watch.py --arm --until <ISO>

# Pause selling immediately without killing the daemon:
touch data/CASHOUT_OFF      # or: export WCA_CASHOUT_OFF=1
```

Env: `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_FUNDER` / `POLYMARKET_SIG_TYPE` to
trade; `ODDS_API_KEY` for the scores feed; `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_ADMIN_USER_ID` for alerts on sells/errors. See `.env.example`.

## Bookkeeping

A cash-out closes the held BUY row(s) to status `cashed` (FIFO across multiple
fills; a partial sale splits the boundary row; an untracked on-chain excess is
booked with zero cost basis so no proceeds are lost). `cashed` counts as realised
P&L in `/summary`, the bankroll curve, per-pool P&L and the site feed, and is
deliberately excluded from CLV/calibration (a mid-match exit has no closing
line). A SELL whose live fill can't be confirmed, or whose booking fails, is
flagged `settle_failed` and alerted — **never** silently booked or auto-retried;
reconcile it by hand.

## Kill criteria (switch it off / never arm LIVE)

- PM's best bid is already below the floor by the time we register the goal
  (PM repriced before us → no edge).
- Goals that survive long enough to sell are routinely the ones VAR is still
  reviewing (reversals cost more than captures).
- Per-event capture stays sub-$3 net on the actual inventory.
- The `/scores` cadence needed to be useful blows the Odds-API quota budget.
- Any wrong-position proposal in SHADOW (orientation flip, name miss that should
  have skipped) — this must be **zero** before arming LIVE.
```
