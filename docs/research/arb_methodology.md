# Arbitrage methodology

## What an arb is
A back-only arbitrage exists across a set of mutually-exclusive and exhaustive
outcomes when the sum of the inverse *net* decimal prices is below 1:

    sum_i (1 / net_i) < 1   =>   guaranteed return = (1 / sum) - 1

Stakes are split in proportion to ``1/net_i`` so every outcome pays out the
same amount; the stake fractions sum to the bankroll.

## Net prices (commission & fees)
- Plain bookmakers: net = raw decimal odds.
- Exchanges (back side): net = 1 + (odds - 1) * (1 - commission). Betfair is
  currently 6% (2% from July), Smarkets/Matchbook 2%.
- Polymarket YES at price p: one share costs ``p + 0.03*p*(1-p)`` and pays 1,
  so net decimal = 1 / cost. (Maker fee is 0.)

## The settlement-key guard (the fake-arb trap)
Every market carries an explicit *settlement key* describing what it resolves
on. Two prices may be paired ONLY if their settlement keys are identical.

- UK 1X2, Betfair Match Odds, h2h_lay, and Polymarket match-winner all settle
  on 90 minutes + stoppage in the group stage -> key ``1x2_90min``. Backing /
  laying across these is valid arb.
- BTTS settles 90-min -> ``btts_90min``.
- Totals settle 90-min, keyed per line -> ``totals_2.5_90min`` etc. Only the
  same line is paired (Over 2.5 vs Under 2.5, never vs Under 3.5).
- Draw-no-bet -> ``dnb_90min``.
- "To qualify" / tournament outright markets include extra time and penalties.
  Their settlement key is ``None`` and they are REFUSED for pairing against any
  90-minute market.

## Detectors
1. ``find_cross_book_arbs`` - best net back per outcome across books within a
   single (event, market, line); flags 3-way (1X2) and complementary 2-way
   (BTTS / DNB / totals) arbs.
2. ``find_pm_book_arbs`` -
   - PM-internal: YES + NO priced so both shares cost < 1 after fee.
   - Book-vs-PM 3-way: back two 1X2 outcomes at the book + the third via a PM
     YES share, only when the PM market settles ``1x2_90min``.

Results are filtered by ``min_profit`` (default 0.5%) and ranked by guaranteed
return.

## Liquidity caveat (an arb you cannot match is not an arb)
Exchange (Betfair/Smarkets/Matchbook) and Polymarket prices are only real if
there is size available at the quoted price. A reported back/lay or PM leg that
cannot actually be matched for the required stake is NOT a realisable arb. The
detector works from top-of-book prices and does NOT model available size, so
every leg sourced from an exchange or Polymarket must be liquidity-checked
manually before staking. Soft books also void/limit obvious arbs.
