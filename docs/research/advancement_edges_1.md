# Tournament-advancement edges — sim vs Polymarket

_Generated 2026-06-11 16:41:23 UTC._

Monte-Carlo simulation of the 2026 FIFA World Cup (`20000` sims, seed `42`) compared to live Polymarket advancement and group-winner markets. Edges are **fee-adjusted** and sized **quarter-Kelly** on the $1310 Polymarket pool (5% per-bet cap).

## Methodology

1. **Models.** Elo (rating + ordered-logit outcome model) and a time-decayed Dixon-Coles model are fit on the full international results history (`wca.card.fit_models`).
2. **prob_fn (honest caveat).** Every simulated match is driven by a **straight 50/50 average of the Elo and Dixon-Coles 1X2 probabilities** — there is **no market term**. The group-stage card anchors ~50% on the de-vigged market, but there are *no* odds for the later rounds, so a market-anchored blend is impossible here. These edges are therefore an independent, noisier model view, not ground truth.
3. **Venue.** The three hosts (United States, Mexico, Canada) get the home-advantage bonus on their own group fixtures (derived from the scheduled-fixture `neutral` flag, as `wca.card` does). Every other group match and **all** knockout matches are neutral.
4. **Knockout draws / ET / penalties.** A 90-minute knockout draw is resolved by the simulator's extra-time / penalty model. "Advancing" therefore **includes** winning on penalties — matching Polymarket resolution ("reach stage X" = the team is in stage X, however it got there).
5. **Stage mapping.** `advance to Knockout Stages` = reach the Round of 32 (top-2 or one of the eight best third-placed teams); `Reach Round of 16/QF/SF/Final` = win the preceding knockout tie; `World Cup Winner` = win the final; `Group X Winner` = finish 1st in the group. These match each market's resolution exactly.
6. **Edge.** For each team-stage market we price BOTH sides. YES buy price = best ask (mid of bid/ask when ask missing); NO buy price = 1 − YES bid. The Polymarket sports **taker fee** `0.03·p·(1−p)` per share is subtracted. Fee-adjusted edge = `sim_prob − buy_price − fee`. We report whichever side the simulation favours.
7. **Sizing.** A binary at buy price `c` (incl. fee) is a fixed-odds bet at decimal odds `1/c`; stake = quarter-Kelly at the simulated win probability, capped at 5% of the $1310 pool (`fraction=0.25`).

**Coverage.** 580 Polymarket World-Cup events pulled; 18 scored (advancement + group-winner); 336 team-stage markets matched to the simulation.

## Top edges

| # | Team | Market | Side | Sim P | PM price | Fee | Fee-adj edge | Stake ($) |
|---|------|--------|------|-------|----------|-----|--------------|-----------|
| 1 | Iran | Reach Round of 16 | YES | 41.6% | 0.200 | 0.005 | **+21.1%** | 65.50 |
| 2 | Ghana | Reach R32 (knockout) | NO | 70.2% | 0.510 | 0.007 | **+18.4%** | 65.50 |
| 3 | Australia | Reach R32 (knockout) | YES | 63.1% | 0.450 | 0.007 | **+17.4%** | 65.50 |
| 4 | Iran | Reach R32 (knockout) | YES | 79.6% | 0.618 | 0.007 | **+17.1%** | 65.50 |
| 5 | Norway | Reach Round of 16 | NO | 64.0% | 0.490 | 0.007 | **+14.3%** | 65.50 |
| 6 | Norway | Reach Quarterfinals | NO | 84.7% | 0.700 | 0.006 | **+14.1%** | 65.50 |
| 7 | United States | Reach Round of 16 | NO | 66.8% | 0.530 | 0.007 | **+13.0%** | 65.50 |
| 8 | Iraq | Reach R32 (knockout) | YES | 26.4% | 0.140 | 0.004 | **+12.0%** | 45.85 |
| 9 | France | Reach Quarterfinals | NO | 54.3% | 0.420 | 0.007 | **+11.5%** | 65.50 |
| 10 | United States | Reach Quarterfinals | NO | 88.8% | 0.770 | 0.005 | **+11.3%** | 65.50 |
| 11 | United States | Reach R32 (knockout) | NO | 28.5% | 0.170 | 0.004 | **+11.1%** | 43.99 |
| 12 | Belgium | Win Group G | NO | 42.5% | 0.310 | 0.006 | **+10.8%** | 51.90 |
| 13 | Ecuador | Reach Round of 16 | YES | 54.5% | 0.430 | 0.007 | **+10.8%** | 62.86 |
| 14 | Iran | Win Group G | YES | 23.5% | 0.126 | 0.003 | **+10.5%** | 39.62 |
| 15 | Australia | Reach Round of 16 | YES | 30.0% | 0.190 | 0.005 | **+10.5%** | 42.71 |
| 16 | Panama | Reach R32 (knockout) | YES | 46.0% | 0.350 | 0.007 | **+10.3%** | 52.28 |
| 17 | Portugal | Reach Semifinals | NO | 79.7% | 0.690 | 0.006 | **+10.1%** | 65.50 |
| 18 | Portugal | Reach Quarterfinals | NO | 61.6% | 0.510 | 0.007 | **+9.8%** | 65.50 |
| 19 | France | Reach Semifinals | NO | 69.4% | 0.590 | 0.007 | **+9.6%** | 65.50 |
| 20 | Norway | Reach R32 (knockout) | NO | 25.0% | 0.150 | 0.004 | **+9.6%** | 37.26 |

## All matched markets (fee-adjusted edge, descending)

| Team | Grp | Market | Side | Sim P | YES mid | Buy price | Fee | Raw edge | Fee-adj edge | Stake ($) |
|------|-----|--------|------|-------|---------|-----------|-----|----------|--------------|-----------|
| Iran | G | Reach Round of 16 | YES | 41.6% | 0.195 | 0.200 | 0.005 | +21.6% | +21.1% | 65.50 |
| Ghana | L | Reach R32 (knockout) | NO | 70.2% | 0.500 | 0.510 | 0.007 | +19.2% | +18.4% | 65.50 |
| Australia | D | Reach R32 (knockout) | YES | 63.1% | 0.445 | 0.450 | 0.007 | +18.1% | +17.4% | 65.50 |
| Iran | G | Reach R32 (knockout) | YES | 79.6% | 0.615 | 0.618 | 0.007 | +17.8% | +17.1% | 65.50 |
| Norway | I | Reach Round of 16 | NO | 64.0% | 0.530 | 0.490 | 0.007 | +15.0% | +14.3% | 65.50 |
| Norway | I | Reach Quarterfinals | NO | 84.7% | 0.310 | 0.700 | 0.006 | +14.7% | +14.1% | 65.50 |
| United States | D | Reach Round of 16 | NO | 66.8% | 0.480 | 0.530 | 0.007 | +13.8% | +13.0% | 65.50 |
| Iraq | I | Reach R32 (knockout) | YES | 26.4% | 0.130 | 0.140 | 0.004 | +12.3% | +12.0% | 45.85 |
| France | I | Reach Quarterfinals | NO | 54.3% | 0.585 | 0.420 | 0.007 | +12.3% | +11.5% | 65.50 |
| United States | D | Reach Quarterfinals | NO | 88.8% | 0.240 | 0.770 | 0.005 | +11.8% | +11.3% | 65.50 |
| United States | D | Reach R32 (knockout) | NO | 28.5% | 0.835 | 0.170 | 0.004 | +11.5% | +11.1% | 43.99 |
| Belgium | G | Win Group G | NO | 42.5% | 0.695 | 0.310 | 0.006 | +11.5% | +10.8% | 51.90 |
| Ecuador | E | Reach Round of 16 | YES | 54.5% | 0.420 | 0.430 | 0.007 | +11.5% | +10.8% | 62.86 |
| Iran | G | Win Group G | YES | 23.5% | 0.123 | 0.126 | 0.003 | +10.9% | +10.5% | 39.62 |
| Australia | D | Reach Round of 16 | YES | 30.0% | 0.180 | 0.190 | 0.005 | +11.0% | +10.5% | 42.71 |
| Panama | L | Reach R32 (knockout) | YES | 46.0% | 0.340 | 0.350 | 0.007 | +11.0% | +10.3% | 52.28 |
| Portugal | K | Reach Semifinals | NO | 79.7% | 0.320 | 0.690 | 0.006 | +10.7% | +10.1% | 65.50 |
| Portugal | K | Reach Quarterfinals | NO | 61.6% | 0.500 | 0.510 | 0.007 | +10.6% | +9.8% | 65.50 |
| France | I | Reach Semifinals | NO | 69.4% | 0.415 | 0.590 | 0.007 | +10.4% | +9.6% | 65.50 |
| Norway | I | Reach R32 (knockout) | NO | 25.0% | 0.855 | 0.150 | 0.004 | +10.0% | +9.6% | 37.26 |
| Germany | E | Win Group E | NO | 43.2% | 0.675 | 0.330 | 0.007 | +10.2% | +9.5% | 47.13 |
| Australia | D | Win Group D | YES | 20.0% | 0.098 | 0.102 | 0.003 | +9.8% | +9.5% | 34.77 |
| Uzbekistan | K | Reach R32 (knockout) | YES | 42.1% | 0.315 | 0.320 | 0.007 | +10.1% | +9.4% | 45.75 |
| Qatar | B | Reach R32 (knockout) | YES | 30.8% | 0.205 | 0.210 | 0.005 | +9.8% | +9.3% | 39.00 |
| France | I | Reach Round of 16 | NO | 28.8% | 0.815 | 0.190 | 0.005 | +9.8% | +9.3% | 37.95 |
| Norway | I | Reach Semifinals | NO | 93.6% | 0.170 | 0.840 | 0.004 | +9.6% | +9.2% | 65.50 |
| Switzerland | B | Win Group B | NO | 53.9% | 0.565 | 0.440 | 0.007 | +9.9% | +9.2% | 54.29 |
| Turkey | D | Win Group D | NO | 73.8% | 0.370 | 0.640 | 0.007 | +9.8% | +9.2% | 65.50 |
| DR Congo | K | Reach R32 (knockout) | NO | 66.8% | 0.435 | 0.570 | 0.007 | +9.8% | +9.1% | 65.50 |
| England | L | Reach Quarterfinals | NO | 54.8% | 0.555 | 0.450 | 0.007 | +9.8% | +9.1% | 54.88 |
| Argentina | J | Reach Semifinals | YES | 40.6% | 0.305 | 0.310 | 0.006 | +9.6% | +8.9% | 42.73 |
| Portugal | K | Win Group K | NO | 47.6% | 0.625 | 0.380 | 0.007 | +9.6% | +8.9% | 47.52 |
| Iran | G | Reach Quarterfinals | YES | 15.4% | 0.048 | 0.064 | 0.002 | +9.0% | +8.8% | 30.89 |
| Bosnia and Herzegovina | B | Reach R32 (knockout) | NO | 44.2% | 0.655 | 0.350 | 0.007 | +9.2% | +8.5% | 43.42 |
| Ghana | L | Reach Round of 16 | NO | 94.7% | 0.150 | 0.860 | 0.004 | +8.7% | +8.3% | 65.50 |
| Portugal | K | Reach Final | NO | 89.7% | 0.195 | 0.810 | 0.005 | +8.7% | +8.2% | 65.50 |
| Canada | B | Win Group B | YES | 39.8% | 0.305 | 0.310 | 0.006 | +8.8% | +8.2% | 39.25 |
| Portugal | K | Reach Round of 16 | NO | 36.7% | 0.725 | 0.280 | 0.006 | +8.7% | +8.1% | 37.02 |
| Ecuador | E | Win Group E | YES | 30.4% | 0.210 | 0.220 | 0.005 | +8.4% | +7.9% | 33.48 |
| Austria | J | Reach R32 (knockout) | NO | 28.2% | 0.805 | 0.200 | 0.005 | +8.2% | +7.8% | 31.98 |
| United States | D | Win Group D | NO | 71.4% | 0.380 | 0.630 | 0.007 | +8.4% | +7.7% | 65.50 |
| France | I | Reach Final | NO | 82.3% | 0.265 | 0.740 | 0.006 | +8.3% | +7.7% | 65.50 |
| England | L | Reach Final | NO | 84.9% | 0.235 | 0.770 | 0.005 | +7.9% | +7.3% | 65.50 |
| England | L | Reach Semifinals | NO | 73.7% | 0.350 | 0.660 | 0.007 | +7.7% | +7.0% | 65.50 |
| Paraguay | D | Win Group D | YES | 25.3% | 0.175 | 0.180 | 0.004 | +7.3% | +6.8% | 27.48 |
| Austria | J | Win Group J | NO | 89.2% | 0.185 | 0.820 | 0.004 | +7.2% | +6.8% | 65.50 |
| Scotland | C | Reach R32 (knockout) | NO | 38.3% | 0.695 | 0.310 | 0.006 | +7.3% | +6.7% | 32.04 |
| Norway | I | Win Group I | NO | 84.2% | 0.235 | 0.770 | 0.005 | +7.2% | +6.6% | 65.50 |
| United States | D | Reach Semifinals | NO | 96.9% | 0.105 | 0.900 | 0.003 | +6.9% | +6.6% | 65.50 |
| Portugal | K | Win the World Cup | NO | 95.3% | 0.117 | 0.884 | 0.003 | +6.9% | +6.6% | 65.50 |
| Paraguay | D | Reach Round of 16 | YES | 36.1% | 0.280 | 0.290 | 0.006 | +7.1% | +6.5% | 30.26 |
| Argentina | J | Win the World Cup | YES | 15.6% | 0.088 | 0.089 | 0.002 | +6.7% | +6.5% | 23.42 |
| Ecuador | E | Reach Quarterfinals | YES | 25.5% | 0.175 | 0.190 | 0.005 | +6.5% | +6.0% | 24.53 |
| Argentina | J | Win Group J | YES | 78.6% | 0.715 | 0.720 | 0.006 | +6.6% | +6.0% | 65.50 |
| Japan | F | Reach Round of 16 | NO | 66.6% | 0.410 | 0.600 | 0.007 | +6.7% | +5.9% | 49.44 |
| France | I | Win the World Cup | NO | 90.1% | 0.161 | 0.840 | 0.004 | +6.1% | +5.7% | 65.50 |
| Spain | H | Reach Quarterfinals | NO | 46.4% | 0.605 | 0.400 | 0.007 | +6.4% | +5.6% | 31.19 |
| Bosnia and Herzegovina | B | Reach Round of 16 | NO | 83.0% | 0.245 | 0.770 | 0.005 | +6.0% | +5.5% | 65.50 |
| Argentina | J | Reach Final | YES | 25.8% | 0.195 | 0.200 | 0.005 | +5.8% | +5.3% | 21.83 |
| Turkey | D | Reach R32 (knockout) | NO | 26.6% | 0.795 | 0.210 | 0.005 | +5.6% | +5.1% | 21.39 |
| Colombia | K | Reach Quarterfinals | YES | 34.7% | 0.285 | 0.290 | 0.006 | +5.7% | +5.1% | 23.79 |
| Croatia | L | Reach R32 (knockout) | YES | 88.5% | 0.825 | 0.830 | 0.004 | +5.5% | +5.1% | 65.50 |
| South Korea | A | Reach Round of 16 | YES | 36.6% | 0.300 | 0.310 | 0.006 | +5.6% | +5.0% | 23.75 |
| England | L | Reach Round of 16 | NO | 31.5% | 0.745 | 0.260 | 0.006 | +5.5% | +4.9% | 22.02 |
| Turkey | D | Reach Quarterfinals | NO | 83.3% | 0.230 | 0.780 | 0.005 | +5.3% | +4.8% | 65.50 |
| Haiti | C | Reach R32 (knockout) | YES | 19.1% | 0.135 | 0.140 | 0.004 | +5.1% | +4.7% | 18.05 |
| Turkey | D | Reach Round of 16 | NO | 59.4% | 0.470 | 0.540 | 0.007 | +5.4% | +4.7% | 34.01 |
| Ivory Coast | E | Reach R32 (knockout) | NO | 27.0% | 0.785 | 0.220 | 0.005 | +5.0% | +4.5% | 19.00 |
| Ecuador | E | Reach R32 (knockout) | YES | 92.7% | 0.875 | 0.880 | 0.003 | +4.7% | +4.4% | 65.50 |
| Colombia | K | Win Group K | YES | 37.9% | 0.325 | 0.330 | 0.007 | +4.9% | +4.2% | 20.84 |
| Paraguay | D | Reach R32 (knockout) | YES | 68.9% | 0.635 | 0.640 | 0.007 | +4.9% | +4.2% | 38.95 |
| Croatia | L | Reach Round of 16 | YES | 44.9% | 0.395 | 0.400 | 0.007 | +4.9% | +4.2% | 23.15 |
| Japan | F | Reach Quarterfinals | NO | 82.4% | 0.240 | 0.780 | 0.005 | +4.4% | +3.9% | 59.60 |
| Uzbekistan | K | Reach Round of 16 | YES | 12.0% | 0.060 | 0.080 | 0.002 | +4.0% | +3.8% | 13.56 |
| Ecuador | E | Reach Semifinals | YES | 12.7% | 0.086 | 0.090 | 0.002 | +3.7% | +3.4% | 12.41 |
| DR Congo | K | Reach Quarterfinals | NO | 98.0% | 0.057 | 0.945 | 0.002 | +3.5% | +3.4% | 65.50 |
| Sweden | F | Reach Round of 16 | NO | 83.7% | 0.220 | 0.800 | 0.005 | +3.7% | +3.2% | 54.36 |
| Curaçao | E | Reach R32 (knockout) | YES | 11.5% | 0.075 | 0.080 | 0.002 | +3.5% | +3.2% | 11.52 |
| Ghana | L | Win Group L | NO | 97.6% | 0.059 | 0.942 | 0.002 | +3.4% | +3.2% | 65.50 |
| Germany | E | Reach Round of 16 | NO | 34.8% | 0.700 | 0.310 | 0.006 | +3.8% | +3.2% | 15.30 |
| Germany | E | Reach Quarterfinals | NO | 65.9% | 0.390 | 0.620 | 0.007 | +3.9% | +3.2% | 27.87 |
| Colombia | K | Reach Round of 16 | YES | 59.8% | 0.545 | 0.560 | 0.007 | +3.8% | +3.1% | 23.25 |
| Ivory Coast | E | Reach Round of 16 | NO | 70.6% | 0.335 | 0.670 | 0.007 | +3.6% | +3.0% | 29.94 |
| Ivory Coast | E | Reach Quarterfinals | NO | 91.3% | 0.125 | 0.880 | 0.003 | +3.3% | +2.9% | 65.50 |
| Ghana | L | Reach Quarterfinals | NO | 99.0% | 0.053 | 0.959 | 0.001 | +3.1% | +2.9% | 65.50 |
| Japan | F | Reach Semifinals | NO | 92.2% | 0.125 | 0.890 | 0.003 | +3.2% | +2.9% | 65.50 |
| Sweden | F | Reach Quarterfinals | NO | 94.1% | 0.105 | 0.910 | 0.002 | +3.1% | +2.9% | 65.50 |
| Brazil | C | Win the World Cup | YES | 11.7% | 0.086 | 0.087 | 0.002 | +3.0% | +2.8% | 10.04 |
| Colombia | K | Win the World Cup | YES | 4.6% | 0.018 | 0.018 | 0.001 | +2.8% | +2.8% | 9.28 |
| Spain | H | Reach Round of 16 | NO | 25.3% | 0.785 | 0.220 | 0.005 | +3.3% | +2.8% | 11.69 |
| Egypt | G | Reach R32 (knockout) | NO | 32.4% | 0.715 | 0.290 | 0.006 | +3.4% | +2.7% | 12.76 |
| Argentina | J | Reach R32 (knockout) | YES | 98.6% | 0.956 | 0.957 | 0.001 | +2.9% | +2.7% | 65.50 |
| Colombia | K | Reach Final | YES | 9.6% | 0.059 | 0.067 | 0.002 | +2.9% | +2.7% | 9.58 |
| Croatia | L | Win Group L | YES | 26.2% | 0.225 | 0.230 | 0.005 | +3.2% | +2.7% | 11.56 |
| New Zealand | G | Reach R32 (knockout) | NO | 71.3% | 0.325 | 0.680 | 0.007 | +3.3% | +2.7% | 28.07 |
| Australia | D | Reach Quarterfinals | YES | 10.9% | 0.060 | 0.080 | 0.002 | +2.9% | +2.7% | 9.54 |
| Netherlands | F | Reach Semifinals | NO | 82.1% | 0.225 | 0.790 | 0.005 | +3.1% | +2.6% | 42.29 |
| Norway | I | Reach Final | NO | 97.7% | 0.055 | 0.950 | 0.001 | +2.7% | +2.6% | 65.50 |
| Uzbekistan | K | Win Group K | YES | 5.8% | 0.029 | 0.031 | 0.001 | +2.7% | +2.6% | 8.76 |
| Sweden | F | Win Group F | NO | 88.9% | 0.145 | 0.860 | 0.004 | +2.9% | +2.6% | 61.44 |
| Germany | E | Reach Final | NO | 90.8% | 0.125 | 0.880 | 0.003 | +2.8% | +2.5% | 65.50 |
| Cape Verde | H | Reach Round of 16 | NO | 95.7% | 0.080 | 0.930 | 0.002 | +2.7% | +2.5% | 65.50 |
| DR Congo | K | Reach Round of 16 | NO | 91.8% | 0.115 | 0.890 | 0.003 | +2.8% | +2.5% | 65.50 |
| Senegal | I | Reach Quarterfinals | NO | 87.9% | 0.155 | 0.850 | 0.004 | +2.9% | +2.5% | 55.40 |
| Austria | J | Reach Round of 16 | NO | 75.1% | 0.295 | 0.720 | 0.006 | +3.1% | +2.5% | 29.53 |
| Turkey | D | Reach Semifinals | NO | 93.7% | 0.105 | 0.910 | 0.002 | +2.7% | +2.4% | 65.50 |
| Austria | J | Reach Quarterfinals | NO | 89.8% | 0.140 | 0.870 | 0.003 | +2.8% | +2.4% | 62.75 |
| Paraguay | D | Reach Quarterfinals | YES | 13.7% | 0.095 | 0.110 | 0.003 | +2.7% | +2.4% | 8.92 |
| England | L | Win the World Cup | NO | 91.9% | 0.108 | 0.892 | 0.003 | +2.7% | +2.4% | 65.50 |
| Panama | L | Reach Round of 16 | YES | 11.6% | 0.080 | 0.090 | 0.002 | +2.6% | +2.4% | 8.64 |
| Portugal | K | Reach R32 (knockout) | NO | 6.1% | 0.965 | 0.036 | 0.001 | +2.5% | +2.4% | 8.03 |
| Qatar | B | Reach Round of 16 | YES | 6.4% | 0.033 | 0.039 | 0.001 | +2.5% | +2.4% | 8.04 |
| Colombia | K | Reach Semifinals | YES | 18.7% | 0.150 | 0.160 | 0.004 | +2.7% | +2.3% | 9.04 |
| Cape Verde | H | Reach Quarterfinals | NO | 99.2% | 0.037 | 0.968 | 0.001 | +2.4% | +2.3% | 65.50 |
| Ecuador | E | Reach Final | YES | 5.3% | 0.027 | 0.031 | 0.001 | +2.2% | +2.1% | 7.17 |
| Senegal | I | Win Group I | YES | 14.4% | 0.115 | 0.120 | 0.003 | +2.4% | +2.1% | 7.86 |
| Iraq | I | Win Group I | YES | 2.9% | 0.007 | 0.008 | 0.000 | +2.1% | +2.0% | 6.69 |
| Canada | B | Reach R32 (knockout) | YES | 88.4% | 0.855 | 0.860 | 0.004 | +2.4% | +2.0% | 48.48 |
| Jordan | J | Reach R32 (knockout) | YES | 24.5% | 0.210 | 0.220 | 0.005 | +2.5% | +2.0% | 8.52 |
| Uruguay | H | Reach Semifinals | YES | 11.2% | 0.085 | 0.090 | 0.002 | +2.2% | +2.0% | 7.21 |
| Brazil | C | Reach Semifinals | YES | 33.6% | 0.300 | 0.310 | 0.006 | +2.6% | +2.0% | 9.53 |
| Egypt | G | Reach Round of 16 | NO | 71.6% | 0.315 | 0.690 | 0.006 | +2.6% | +1.9% | 20.80 |
| Bosnia and Herzegovina | B | Reach Quarterfinals | NO | 96.1% | 0.065 | 0.940 | 0.002 | +2.1% | +1.9% | 65.50 |
| England | L | Win Group L | NO | 33.6% | 0.695 | 0.310 | 0.006 | +2.6% | +1.9% | 9.19 |
| Czech Republic | A | Reach Round of 16 | YES | 31.5% | 0.285 | 0.290 | 0.006 | +2.5% | +1.9% | 8.92 |
| Panama | L | Win Group L | YES | 4.9% | 0.029 | 0.029 | 0.001 | +2.0% | +1.9% | 6.47 |
| South Korea | A | Reach R32 (knockout) | YES | 73.5% | 0.700 | 0.710 | 0.006 | +2.5% | +1.9% | 21.89 |
| Sweden | F | Reach R32 (knockout) | NO | 42.6% | 0.605 | 0.400 | 0.007 | +2.6% | +1.9% | 10.28 |
| Uruguay | H | Reach R32 (knockout) | YES | 90.1% | 0.875 | 0.880 | 0.003 | +2.1% | +1.8% | 51.25 |
| Switzerland | B | Reach Quarterfinals | YES | 27.4% | 0.240 | 0.250 | 0.006 | +2.4% | +1.8% | 8.00 |
| Sweden | F | Reach Semifinals | NO | 98.1% | 0.043 | 0.962 | 0.001 | +1.9% | +1.8% | 65.50 |
| Germany | E | Reach Semifinals | NO | 80.3% | 0.230 | 0.780 | 0.005 | +2.3% | +1.8% | 26.83 |
| Iran | G | Reach Semifinals | YES | 5.0% | 0.019 | 0.032 | 0.001 | +1.8% | +1.7% | 5.70 |
| Turkey | D | Reach Final | NO | 97.8% | 0.041 | 0.960 | 0.001 | +1.8% | +1.7% | 65.50 |
| Netherlands | F | Reach Round of 16 | NO | 48.4% | 0.550 | 0.460 | 0.007 | +2.4% | +1.7% | 10.21 |
| Netherlands | F | Win Group F | YES | 56.4% | 0.535 | 0.540 | 0.007 | +2.3% | +1.6% | 11.61 |
| Algeria | J | Reach R32 (knockout) | YES | 68.3% | 0.650 | 0.660 | 0.007 | +2.3% | +1.6% | 15.69 |
| Bosnia and Herzegovina | B | Win Group B | NO | 89.9% | 0.125 | 0.880 | 0.003 | +1.9% | +1.6% | 44.66 |
| Norway | I | Win the World Cup | NO | 99.3% | 0.024 | 0.977 | 0.001 | +1.6% | +1.5% | 65.50 |
| Belgium | G | Reach R32 (knockout) | NO | 6.5% | 0.955 | 0.048 | 0.001 | +1.6% | +1.5% | 5.21 |
| Senegal | I | Reach Semifinals | NO | 95.4% | 0.067 | 0.937 | 0.002 | +1.7% | +1.5% | 65.50 |
| Ivory Coast | E | Reach Semifinals | NO | 97.4% | 0.047 | 0.958 | 0.001 | +1.6% | +1.5% | 65.50 |
| Belgium | G | Reach Quarterfinals | NO | 65.2% | 0.375 | 0.630 | 0.007 | +2.2% | +1.5% | 13.36 |
| United States | D | Reach Final | NO | 99.2% | 0.030 | 0.977 | 0.001 | +1.5% | +1.5% | 65.50 |
| Cape Verde | H | Reach R32 (knockout) | NO | 72.1% | 0.305 | 0.700 | 0.006 | +2.1% | +1.4% | 16.06 |
| Spain | H | Win Group H | NO | 22.9% | 0.795 | 0.210 | 0.005 | +1.9% | +1.4% | 6.00 |
| Mexico | A | Reach Final | NO | 96.5% | 0.052 | 0.949 | 0.001 | +1.6% | +1.4% | 65.50 |
| Qatar | B | Win Group B | YES | 4.0% | 0.024 | 0.026 | 0.001 | +1.4% | +1.3% | 4.37 |
| Czech Republic | A | Reach Quarterfinals | YES | 11.5% | 0.095 | 0.100 | 0.003 | +1.5% | +1.3% | 4.64 |
| England | L | Reach R32 (knockout) | YES | 97.1% | 0.951 | 0.957 | 0.001 | +1.4% | +1.3% | 65.50 |
| Ghana | L | Reach Semifinals | NO | 99.9% | 0.028 | 0.986 | 0.000 | +1.3% | +1.2% | 65.50 |
| Iraq | I | Reach Round of 16 | YES | 6.1% | 0.036 | 0.048 | 0.001 | +1.3% | +1.2% | 4.14 |
| Netherlands | F | Reach Quarterfinals | NO | 65.9% | 0.365 | 0.640 | 0.007 | +1.9% | +1.2% | 10.98 |
| Saudi Arabia | H | Reach Round of 16 | NO | 93.4% | 0.090 | 0.920 | 0.002 | +1.4% | +1.2% | 49.64 |
| Spain | H | Win the World Cup | YES | 18.6% | 0.170 | 0.170 | 0.004 | +1.6% | +1.2% | 4.59 |
| Curaçao | E | Reach Quarterfinals | NO | 99.9% | 0.017 | 0.987 | 0.000 | +1.2% | +1.1% | 65.50 |
| Czech Republic | A | Reach R32 (knockout) | NO | 32.8% | 0.695 | 0.310 | 0.006 | +1.8% | +1.1% | 5.36 |
| Ecuador | E | Win the World Cup | YES | 2.0% | 0.009 | 0.009 | 0.000 | +1.1% | +1.1% | 3.56 |
| Ivory Coast | E | Reach Final | NO | 99.4% | 0.023 | 0.983 | 0.001 | +1.1% | +1.0% | 65.50 |
| Morocco | C | Reach Final | NO | 96.2% | 0.051 | 0.950 | 0.001 | +1.2% | +1.0% | 65.50 |
| Sweden | F | Reach Final | NO | 99.5% | 0.018 | 0.985 | 0.000 | +1.0% | +1.0% | 65.50 |
| Tunisia | F | Reach Semifinals | NO | 99.3% | 0.021 | 0.983 | 0.001 | +1.0% | +1.0% | 65.50 |
| Argentina | J | Reach Quarterfinals | YES | 54.7% | 0.525 | 0.530 | 0.007 | +1.7% | +0.9% | 6.60 |
| Spain | H | Reach R32 (knockout) | YES | 99.2% | 0.982 | 0.982 | 0.001 | +1.0% | +0.9% | 65.50 |
| Germany | E | Win the World Cup | NO | 96.0% | 0.051 | 0.949 | 0.001 | +1.1% | +0.9% | 61.46 |
| United States | D | Win the World Cup | NO | 99.8% | 0.011 | 0.989 | 0.000 | +0.9% | +0.9% | 65.50 |
| Colombia | K | Reach R32 (knockout) | YES | 92.2% | 0.900 | 0.910 | 0.002 | +1.1% | +0.9% | 33.83 |
| Japan | F | Reach Final | NO | 96.9% | 0.048 | 0.959 | 0.001 | +1.0% | +0.9% | 65.50 |
| Brazil | C | Reach Quarterfinals | YES | 51.6% | 0.490 | 0.500 | 0.007 | +1.6% | +0.9% | 5.75 |
| Bosnia and Herzegovina | B | Reach Final | NO | 99.9% | 0.015 | 0.990 | 0.000 | +0.9% | +0.9% | 65.50 |
| Brazil | C | Reach R32 (knockout) | YES | 98.1% | 0.968 | 0.972 | 0.001 | +0.9% | +0.8% | 65.50 |
| Uruguay | H | Win the World Cup | YES | 1.8% | 0.009 | 0.010 | 0.000 | +0.8% | +0.8% | 2.71 |
| Saudi Arabia | H | Reach Final | NO | 99.9% | 0.014 | 0.991 | 0.000 | +0.8% | +0.8% | 65.50 |
| Jordan | J | Reach Quarterfinals | NO | 99.0% | 0.018 | 0.982 | 0.001 | +0.9% | +0.8% | 65.50 |
| Czech Republic | A | Win Group A | NO | 84.2% | 0.175 | 0.830 | 0.004 | +1.2% | +0.8% | 15.44 |
| Tunisia | F | Reach R32 (knockout) | YES | 38.5% | 0.350 | 0.370 | 0.007 | +1.5% | +0.8% | 4.10 |
| Austria | J | Reach Final | NO | 98.7% | 0.022 | 0.979 | 0.001 | +0.8% | +0.8% | 65.50 |
| South Africa | A | Reach Final | NO | 100.0% | 0.013 | 0.992 | 0.000 | +0.8% | +0.7% | 65.50 |
| Saudi Arabia | H | Reach Semifinals | NO | 99.7% | 0.015 | 0.990 | 0.000 | +0.7% | +0.7% | 65.50 |
| Ghana | L | Reach Final | NO | 100.0% | 0.013 | 0.993 | 0.000 | +0.7% | +0.7% | 65.50 |
| Morocco | C | Reach Round of 16 | YES | 42.4% | 0.405 | 0.410 | 0.007 | +1.4% | +0.6% | 3.59 |
| Mexico | A | Reach Round of 16 | YES | 57.4% | 0.555 | 0.560 | 0.007 | +1.3% | +0.6% | 4.62 |
| Croatia | L | Win the World Cup | YES | 1.5% | 0.009 | 0.009 | 0.000 | +0.6% | +0.6% | 1.99 |
| Belgium | G | Win the World Cup | YES | 2.8% | 0.021 | 0.021 | 0.001 | +0.6% | +0.6% | 1.97 |
| DR Congo | K | Reach Final | NO | 99.9% | 0.011 | 0.993 | 0.000 | +0.6% | +0.6% | 65.50 |
| Iraq | I | Reach Semifinals | NO | 99.8% | 0.015 | 0.992 | 0.000 | +0.6% | +0.6% | 65.50 |
| Scotland | C | Reach Final | NO | 99.6% | 0.015 | 0.990 | 0.000 | +0.6% | +0.6% | 65.50 |
| Uzbekistan | K | Reach Final | NO | 99.9% | 0.013 | 0.993 | 0.000 | +0.6% | +0.5% | 65.50 |
| Haiti | C | Reach Quarterfinals | NO | 99.5% | 0.018 | 0.989 | 0.000 | +0.6% | +0.5% | 65.50 |
| Japan | F | Win the World Cup | NO | 98.9% | 0.018 | 0.983 | 0.001 | +0.6% | +0.5% | 65.50 |
| Bosnia and Herzegovina | B | Reach Semifinals | NO | 99.4% | 0.025 | 0.989 | 0.000 | +0.5% | +0.5% | 65.50 |
| Qatar | B | Reach Final | NO | 100.0% | 0.009 | 0.995 | 0.000 | +0.5% | +0.5% | 65.50 |
| Tunisia | F | Reach Final | NO | 99.9% | 0.010 | 0.994 | 0.000 | +0.5% | +0.4% | 65.50 |
| Senegal | I | Reach Round of 16 | NO | 69.1% | 0.325 | 0.680 | 0.007 | +1.1% | +0.4% | 4.41 |
| Senegal | I | Reach Final | NO | 98.5% | 0.025 | 0.980 | 0.001 | +0.5% | +0.4% | 65.50 |
| South Africa | A | Reach Semifinals | NO | 99.6% | 0.013 | 0.992 | 0.000 | +0.4% | +0.4% | 65.50 |
| South Korea | A | Reach Quarterfinals | YES | 13.7% | 0.120 | 0.130 | 0.003 | +0.7% | +0.4% | 1.50 |
| Germany | E | Reach R32 (knockout) | YES | 97.0% | 0.964 | 0.965 | 0.001 | +0.5% | +0.4% | 37.45 |
| New Zealand | G | Reach Final | NO | 100.0% | 0.009 | 0.996 | 0.000 | +0.4% | +0.4% | 65.50 |
| Tunisia | F | Reach Round of 16 | NO | 91.6% | 0.100 | 0.910 | 0.002 | +0.6% | +0.4% | 13.63 |
| Czech Republic | A | Reach Final | NO | 99.2% | 0.015 | 0.988 | 0.000 | +0.4% | +0.3% | 65.50 |
| Turkey | D | Win the World Cup | NO | 99.3% | 0.011 | 0.989 | 0.000 | +0.4% | +0.3% | 65.50 |
| Spain | H | Reach Final | YES | 28.9% | 0.275 | 0.280 | 0.006 | +0.9% | +0.3% | 1.51 |
| Mexico | A | Reach R32 (knockout) | YES | 92.5% | 0.915 | 0.920 | 0.002 | +0.5% | +0.3% | 11.96 |
| Senegal | I | Win the World Cup | NO | 99.6% | 0.007 | 0.993 | 0.000 | +0.3% | +0.3% | 65.50 |
| Paraguay | D | Win the World Cup | YES | 0.5% | 0.002 | 0.002 | 0.000 | +0.3% | +0.3% | 0.85 |
| Jordan | J | Reach Final | NO | 100.0% | 0.005 | 0.997 | 0.000 | +0.3% | +0.3% | 65.50 |
| Netherlands | F | Reach Final | NO | 91.5% | 0.095 | 0.910 | 0.002 | +0.5% | +0.3% | 9.51 |
| South Africa | A | Win Group A | YES | 5.9% | 0.054 | 0.055 | 0.002 | +0.4% | +0.3% | 0.88 |
| France | I | Win Group I | YES | 66.9% | 0.655 | 0.660 | 0.007 | +0.9% | +0.2% | 2.33 |
| Ivory Coast | E | Win the World Cup | NO | 99.8% | 0.005 | 0.996 | 0.000 | +0.2% | +0.2% | 65.50 |
| Netherlands | F | Reach R32 (knockout) | YES | 92.5% | 0.915 | 0.920 | 0.002 | +0.4% | +0.2% | 9.65 |
| Scotland | C | Reach Round of 16 | NO | 79.7% | 0.220 | 0.790 | 0.005 | +0.7% | +0.2% | 3.47 |
| Scotland | C | Win Group C | NO | 93.2% | 0.075 | 0.928 | 0.002 | +0.4% | +0.2% | 10.04 |
| Ivory Coast | E | Win Group E | NO | 87.8% | 0.129 | 0.873 | 0.003 | +0.5% | +0.2% | 5.49 |
| Curaçao | E | Win Group E | YES | 0.6% | 0.003 | 0.004 | 0.000 | +0.2% | +0.2% | 0.67 |
| Curaçao | E | Reach Final | NO | 100.0% | 0.003 | 0.998 | 0.000 | +0.2% | +0.2% | 65.50 |
| Curaçao | E | Reach Semifinals | NO | 100.0% | 0.005 | 0.998 | 0.000 | +0.2% | +0.2% | 65.50 |
| New Zealand | G | Reach Semifinals | NO | 99.8% | 0.011 | 0.996 | 0.000 | +0.2% | +0.2% | 65.50 |
| Switzerland | B | Win the World Cup | YES | 1.5% | 0.013 | 0.013 | 0.000 | +0.2% | +0.2% | 0.64 |
| Algeria | J | Reach Final | NO | 99.2% | 0.014 | 0.990 | 0.000 | +0.2% | +0.2% | 64.23 |
| Morocco | C | Reach Semifinals | NO | 90.5% | 0.110 | 0.900 | 0.003 | +0.5% | +0.2% | 6.40 |
| Haiti | C | Win Group C | YES | 1.1% | 0.007 | 0.009 | 0.000 | +0.2% | +0.2% | 0.62 |
| Iran | G | Win the World Cup | YES | 0.4% | 0.002 | 0.002 | 0.000 | +0.2% | +0.2% | 0.60 |
| Switzerland | B | Reach R32 (knockout) | NO | 6.8% | 0.938 | 0.064 | 0.002 | +0.4% | +0.2% | 0.63 |
| Belgium | G | Reach Final | NO | 93.4% | 0.075 | 0.930 | 0.002 | +0.4% | +0.2% | 8.65 |
| Iraq | I | Reach Final | NO | 100.0% | 0.006 | 0.998 | 0.000 | +0.2% | +0.2% | 65.50 |
| Sweden | F | Win the World Cup | NO | 99.9% | 0.004 | 0.997 | 0.000 | +0.2% | +0.2% | 65.50 |
| Cape Verde | H | Reach Final | NO | 100.0% | 0.003 | 0.998 | 0.000 | +0.2% | +0.2% | 65.50 |
| Brazil | C | Reach Final | YES | 20.6% | 0.190 | 0.200 | 0.005 | +0.6% | +0.1% | 0.54 |
| Cape Verde | H | Win Group H | YES | 1.5% | 0.012 | 0.013 | 0.000 | +0.2% | +0.1% | 0.39 |
| Haiti | C | Reach Semifinals | NO | 99.9% | 0.003 | 0.998 | 0.000 | +0.1% | +0.1% | 65.50 |
| Scotland | C | Win the World Cup | NO | 99.9% | 0.003 | 0.998 | 0.000 | +0.1% | +0.1% | 65.50 |
| Ghana | L | Win the World Cup | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Mexico | A | Reach Semifinals | NO | 90.4% | 0.110 | 0.900 | 0.003 | +0.4% | +0.1% | 3.20 |
| Haiti | C | Reach Final | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| DR Congo | K | Win the World Cup | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Bosnia and Herzegovina | B | Win the World Cup | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Haiti | C | Reach Round of 16 | NO | 97.1% | 0.036 | 0.969 | 0.001 | +0.2% | +0.1% | 8.69 |
| Algeria | J | Win Group J | NO | 90.9% | 0.095 | 0.906 | 0.003 | +0.3% | +0.1% | 2.67 |
| Egypt | G | Win the World Cup | NO | 99.9% | 0.003 | 0.998 | 0.000 | +0.1% | +0.1% | 65.50 |
| Mexico | A | Win the World Cup | NO | 98.8% | 0.013 | 0.987 | 0.000 | +0.1% | +0.1% | 15.97 |
| Canada | B | Win the World Cup | NO | 99.8% | 0.004 | 0.997 | 0.000 | +0.1% | +0.1% | 63.05 |
| Jordan | J | Reach Semifinals | NO | 99.8% | 0.013 | 0.997 | 0.000 | +0.1% | +0.1% | 57.42 |
| Cape Verde | H | Reach Semifinals | NO | 99.9% | 0.004 | 0.998 | 0.000 | +0.1% | +0.0% | 65.50 |
| Haiti | C | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Qatar | B | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Jordan | J | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Curaçao | E | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Cape Verde | H | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| New Zealand | G | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| DR Congo | K | Win Group K | NO | 96.1% | 0.042 | 0.959 | 0.001 | +0.2% | +0.0% | 3.87 |
| Australia | D | Win the World Cup | YES | 0.2% | 0.002 | 0.002 | 0.000 | +0.1% | +0.0% | 0.14 |
| South Africa | A | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Iraq | I | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Saudi Arabia | H | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Uzbekistan | K | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Austria | J | Win the World Cup | NO | 99.6% | 0.005 | 0.996 | 0.000 | +0.0% | +0.0% | 27.89 |
| South Korea | A | Win the World Cup | YES | 0.3% | 0.003 | 0.003 | 0.000 | +0.0% | +0.0% | 0.05 |
| New Zealand | G | Win Group G | NO | 96.6% | 0.036 | 0.965 | 0.001 | +0.1% | +0.0% | 1.32 |
| Algeria | J | Win the World Cup | YES | 0.2% | 0.002 | 0.002 | 0.000 | +0.0% | +0.0% | 0.03 |
| Czech Republic | A | Win the World Cup | YES | 0.2% | 0.002 | 0.002 | 0.000 | +0.0% | +0.0% | 0.03 |
| Panama | L | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 57.40 |
| Qatar | B | Reach Semifinals | NO | 99.9% | 0.003 | 0.999 | 0.000 | +0.0% | +0.0% | 6.76 |
| Tunisia | F | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | -0.0% | 0.00 |
| Austria | J | Reach Semifinals | NO | 96.0% | 0.057 | 0.959 | 0.001 | +0.1% | -0.0% | 0.00 |
| Saudi Arabia | H | Win Group H | YES | 2.0% | 0.017 | 0.020 | 0.001 | +0.0% | -0.0% | 0.00 |
| South Africa | A | Reach Quarterfinals | NO | 97.4% | 0.040 | 0.974 | 0.001 | +0.0% | -0.0% | 0.00 |
| Spain | H | Reach Semifinals | NO | 57.7% | 0.440 | 0.570 | 0.007 | +0.7% | -0.0% | 0.00 |
| Netherlands | F | Win the World Cup | NO | 95.9% | 0.043 | 0.958 | 0.001 | +0.1% | -0.1% | 0.00 |
| DR Congo | K | Reach Semifinals | NO | 99.6% | 0.009 | 0.996 | 0.000 | -0.0% | -0.1% | 0.00 |
| Jordan | J | Win Group J | YES | 1.6% | 0.014 | 0.016 | 0.000 | -0.0% | -0.1% | 0.00 |
| Paraguay | D | Reach Semifinals | YES | 4.6% | 0.042 | 0.045 | 0.001 | +0.1% | -0.1% | 0.00 |
| Qatar | B | Reach Quarterfinals | NO | 98.9% | 0.022 | 0.989 | 0.000 | -0.0% | -0.1% | 0.00 |
| Morocco | C | Win the World Cup | NO | 98.6% | 0.014 | 0.986 | 0.000 | -0.0% | -0.1% | 0.00 |
| South Africa | A | Reach R32 (knockout) | NO | 63.6% | 0.375 | 0.630 | 0.007 | +0.6% | -0.1% | 0.00 |
| Panama | L | Reach Final | NO | 99.8% | 0.004 | 0.999 | 0.000 | -0.1% | -0.1% | 0.00 |
| Japan | F | Reach R32 (knockout) | YES | 81.3% | 0.805 | 0.810 | 0.005 | +0.3% | -0.1% | 0.00 |
| Egypt | G | Reach Final | NO | 99.4% | 0.010 | 0.995 | 0.000 | -0.1% | -0.1% | 0.00 |
| Uzbekistan | K | Reach Semifinals | NO | 99.1% | 0.010 | 0.992 | 0.000 | -0.1% | -0.2% | 0.00 |
| Panama | L | Reach Quarterfinals | YES | 3.3% | 0.022 | 0.034 | 0.001 | -0.1% | -0.2% | 0.00 |
| Croatia | L | Reach Final | YES | 3.9% | 0.035 | 0.040 | 0.001 | -0.1% | -0.2% | 0.00 |
| New Zealand | G | Reach Quarterfinals | NO | 98.7% | 0.023 | 0.989 | 0.000 | -0.2% | -0.2% | 0.00 |
| France | I | Reach R32 (knockout) | NO | 3.9% | 0.963 | 0.040 | 0.001 | -0.1% | -0.3% | 0.00 |
| Scotland | C | Reach Semifinals | NO | 97.7% | 0.037 | 0.979 | 0.001 | -0.2% | -0.3% | 0.00 |
| Morocco | C | Reach R32 (knockout) | YES | 87.1% | 0.865 | 0.870 | 0.003 | +0.1% | -0.3% | 0.00 |
| Iraq | I | Reach Quarterfinals | NO | 98.6% | 0.020 | 0.989 | 0.000 | -0.3% | -0.3% | 0.00 |
| Panama | L | Reach Semifinals | NO | 99.2% | 0.009 | 0.995 | 0.000 | -0.3% | -0.3% | 0.00 |
| South Korea | A | Reach Final | NO | 98.7% | 0.015 | 0.990 | 0.000 | -0.3% | -0.3% | 0.00 |
| Canada | B | Reach Final | NO | 99.1% | 0.011 | 0.994 | 0.000 | -0.3% | -0.3% | 0.00 |
| Tunisia | F | Win Group F | NO | 94.4% | 0.057 | 0.946 | 0.002 | -0.2% | -0.4% | 0.00 |
| Curaçao | E | Reach Round of 16 | NO | 98.7% | 0.018 | 0.990 | 0.000 | -0.3% | -0.4% | 0.00 |
| Australia | D | Reach Final | NO | 99.0% | 0.011 | 0.994 | 0.000 | -0.4% | -0.4% | 0.00 |
| Brazil | C | Win Group C | NO | 29.2% | 0.715 | 0.290 | 0.006 | +0.2% | -0.4% | 0.00 |
| Iran | G | Reach Final | YES | 1.6% | 0.013 | 0.020 | 0.001 | -0.4% | -0.4% | 0.00 |
| Belgium | G | Reach Round of 16 | NO | 37.2% | 0.635 | 0.370 | 0.007 | +0.2% | -0.5% | 0.00 |
| Saudi Arabia | H | Reach R32 (knockout) | NO | 65.2% | 0.360 | 0.650 | 0.007 | +0.2% | -0.5% | 0.00 |
| Canada | B | Reach Semifinals | YES | 3.5% | 0.030 | 0.039 | 0.001 | -0.4% | -0.5% | 0.00 |
| Belgium | G | Reach Semifinals | NO | 85.8% | 0.145 | 0.860 | 0.004 | -0.2% | -0.5% | 0.00 |
| Uruguay | H | Reach Final | YES | 4.8% | 0.042 | 0.052 | 0.001 | -0.4% | -0.5% | 0.00 |
| Saudi Arabia | H | Reach Quarterfinals | NO | 98.4% | 0.029 | 0.989 | 0.000 | -0.5% | -0.6% | 0.00 |
| South Korea | A | Reach Semifinals | YES | 4.0% | 0.037 | 0.044 | 0.001 | -0.4% | -0.6% | 0.00 |
| Japan | F | Win Group F | NO | 73.0% | 0.275 | 0.730 | 0.006 | +0.0% | -0.6% | 0.00 |
| Australia | D | Reach Semifinals | YES | 3.6% | 0.025 | 0.041 | 0.001 | -0.5% | -0.6% | 0.00 |
| Croatia | L | Reach Semifinals | YES | 9.5% | 0.093 | 0.099 | 0.003 | -0.4% | -0.6% | 0.00 |
| New Zealand | G | Reach Round of 16 | NO | 92.6% | 0.075 | 0.930 | 0.002 | -0.4% | -0.6% | 0.00 |
| South Korea | A | Win Group A | NO | 79.8% | 0.210 | 0.800 | 0.005 | -0.2% | -0.6% | 0.00 |
| Scotland | C | Reach Quarterfinals | NO | 92.5% | 0.090 | 0.930 | 0.002 | -0.5% | -0.7% | 0.00 |
| Argentina | J | Reach Round of 16 | NO | 31.0% | 0.695 | 0.310 | 0.006 | -0.0% | -0.7% | 0.00 |
| Croatia | L | Reach Quarterfinals | YES | 20.8% | 0.200 | 0.210 | 0.005 | -0.2% | -0.7% | 0.00 |
| Canada | B | Reach Quarterfinals | YES | 14.6% | 0.140 | 0.150 | 0.004 | -0.4% | -0.8% | 0.00 |
| Morocco | C | Win Group C | NO | 78.7% | 0.215 | 0.790 | 0.005 | -0.3% | -0.8% | 0.00 |
| Egypt | G | Win Group G | YES | 15.6% | 0.155 | 0.160 | 0.004 | -0.4% | -0.8% | 0.00 |
| Brazil | C | Reach Round of 16 | YES | 71.8% | 0.715 | 0.720 | 0.006 | -0.2% | -0.8% | 0.00 |
| Mexico | A | Win Group A | NO | 41.9% | 0.585 | 0.420 | 0.007 | -0.1% | -0.9% | 0.00 |
| Switzerland | B | Reach Final | YES | 4.0% | 0.039 | 0.047 | 0.001 | -0.7% | -0.9% | 0.00 |
| Canada | B | Reach Round of 16 | NO | 57.9% | 0.435 | 0.580 | 0.007 | -0.1% | -0.9% | 0.00 |
| Paraguay | D | Reach Final | NO | 98.5% | 0.017 | 0.993 | 0.000 | -0.8% | -0.9% | 0.00 |
| Uruguay | H | Win Group H | NO | 80.6% | 0.195 | 0.810 | 0.005 | -0.4% | -0.9% | 0.00 |
| Algeria | J | Reach Round of 16 | YES | 21.4% | 0.210 | 0.220 | 0.005 | -0.6% | -1.1% | 0.00 |
| Uruguay | H | Reach Quarterfinals | NO | 78.3% | 0.220 | 0.790 | 0.005 | -0.7% | -1.1% | 0.00 |
| Switzerland | B | Reach Semifinals | YES | 10.1% | 0.100 | 0.110 | 0.003 | -0.9% | -1.2% | 0.00 |
| Egypt | G | Reach Semifinals | NO | 97.7% | 0.026 | 0.989 | 0.000 | -1.2% | -1.2% | 0.00 |
| South Africa | A | Reach Round of 16 | YES | 11.1% | 0.110 | 0.120 | 0.003 | -0.9% | -1.2% | 0.00 |
| Tunisia | F | Reach Quarterfinals | NO | 97.5% | 0.025 | 0.988 | 0.000 | -1.2% | -1.3% | 0.00 |
| Morocco | C | Reach Quarterfinals | NO | 77.2% | 0.230 | 0.780 | 0.005 | -0.8% | -1.3% | 0.00 |
| Czech Republic | A | Reach Semifinals | YES | 3.0% | 0.028 | 0.042 | 0.001 | -1.2% | -1.3% | 0.00 |
| Senegal | I | Reach R32 (knockout) | NO | 29.3% | 0.710 | 0.300 | 0.006 | -0.7% | -1.3% | 0.00 |
| Uzbekistan | K | Reach Quarterfinals | YES | 3.2% | 0.028 | 0.044 | 0.001 | -1.2% | -1.3% | 0.00 |
| Switzerland | B | Reach Round of 16 | NO | 42.3% | 0.580 | 0.430 | 0.007 | -0.7% | -1.4% | 0.00 |
| Mexico | A | Reach Quarterfinals | YES | 26.1% | 0.260 | 0.270 | 0.006 | -0.9% | -1.5% | 0.00 |
| Jordan | J | Reach Round of 16 | YES | 4.7% | 0.039 | 0.060 | 0.002 | -1.3% | -1.5% | 0.00 |
| Algeria | J | Reach Semifinals | NO | 97.1% | 0.030 | 0.988 | 0.000 | -1.7% | -1.7% | 0.00 |
| Algeria | J | Reach Quarterfinals | YES | 8.5% | 0.085 | 0.100 | 0.003 | -1.5% | -1.7% | 0.00 |
| Uruguay | H | Reach Round of 16 | NO | 60.9% | 0.395 | 0.620 | 0.007 | -1.1% | -1.8% | 0.00 |
| Egypt | G | Reach Quarterfinals | NO | 91.3% | 0.090 | 0.930 | 0.002 | -1.7% | -1.9% | 0.00 |

## Simulated stage probabilities (all 48 teams)

Sorted by P(win). Group letter in parentheses.

| Team | Grp | Win Grp | Reach R32 | R16 | QF | SF | Final | Win |
|------|-----|---------|-----------|-----|----|----|-------|-----|
| Spain | H | 77.1% | 99.2% | 74.7% | 53.6% | 42.3% | 28.9% | 18.6% |
| Argentina | J | 78.6% | 98.6% | 69.0% | 54.7% | 40.6% | 25.8% | 15.6% |
| Brazil | C | 70.8% | 98.1% | 71.8% | 51.6% | 33.6% | 20.6% | 11.7% |
| France | I | 66.9% | 96.1% | 71.2% | 45.7% | 30.6% | 17.7% | 9.9% |
| England | L | 66.4% | 97.1% | 68.5% | 45.2% | 26.3% | 15.2% | 8.1% |
| Portugal | K | 52.4% | 93.9% | 63.3% | 38.4% | 20.3% | 10.3% | 4.7% |
| Colombia | K | 37.9% | 92.2% | 59.8% | 34.7% | 18.7% | 9.6% | 4.6% |
| Netherlands | F | 56.4% | 92.5% | 51.6% | 34.1% | 17.9% | 8.5% | 4.1% |
| Germany | E | 56.8% | 97.0% | 65.2% | 34.1% | 19.7% | 9.2% | 4.0% |
| Belgium | G | 57.5% | 93.5% | 62.8% | 34.8% | 14.2% | 6.6% | 2.8% |
| Ecuador | E | 30.4% | 92.7% | 54.5% | 25.5% | 12.7% | 5.3% | 2.0% |
| Uruguay | H | 19.4% | 90.1% | 39.1% | 21.6% | 11.2% | 4.8% | 1.8% |
| Croatia | L | 26.2% | 88.5% | 44.9% | 20.8% | 9.5% | 3.9% | 1.5% |
| Switzerland | B | 46.1% | 93.2% | 57.7% | 27.4% | 10.1% | 4.0% | 1.5% |
| Morocco | C | 21.3% | 87.1% | 42.4% | 22.8% | 9.5% | 3.8% | 1.4% |
| Mexico | A | 58.1% | 92.5% | 57.4% | 26.1% | 9.6% | 3.5% | 1.2% |
| Japan | F | 27.0% | 81.3% | 33.4% | 17.6% | 7.8% | 3.1% | 1.1% |
| Turkey | D | 26.2% | 73.4% | 40.6% | 16.7% | 6.3% | 2.2% | 0.7% |
| Norway | I | 15.8% | 75.0% | 36.0% | 15.3% | 6.4% | 2.3% | 0.7% |
| Paraguay | D | 25.3% | 68.9% | 36.1% | 13.7% | 4.6% | 1.6% | 0.5% |
| Senegal | I | 14.4% | 70.7% | 30.9% | 12.1% | 4.6% | 1.5% | 0.4% |
| Iran | G | 23.5% | 79.6% | 41.6% | 15.4% | 5.0% | 1.6% | 0.4% |
| Austria | J | 10.8% | 71.8% | 24.9% | 10.2% | 4.0% | 1.3% | 0.4% |
| South Korea | A | 20.2% | 73.5% | 36.6% | 13.7% | 4.0% | 1.3% | 0.3% |
| Australia | D | 20.0% | 63.1% | 30.0% | 10.9% | 3.6% | 1.0% | 0.2% |
| Canada | B | 39.8% | 88.4% | 42.1% | 14.6% | 3.5% | 0.9% | 0.2% |
| Czech Republic | A | 15.8% | 67.2% | 31.5% | 11.5% | 3.0% | 0.8% | 0.2% |
| Algeria | J | 9.1% | 68.3% | 21.4% | 8.5% | 2.9% | 0.8% | 0.2% |
| United States | D | 28.6% | 71.5% | 33.2% | 11.2% | 3.1% | 0.8% | 0.2% |
| Ivory Coast | E | 12.2% | 73.0% | 29.4% | 8.7% | 2.6% | 0.6% | 0.2% |
| Egypt | G | 15.6% | 67.6% | 28.4% | 8.7% | 2.3% | 0.6% | 0.1% |
| Sweden | F | 11.1% | 57.4% | 16.3% | 5.9% | 1.9% | 0.5% | 0.1% |
| Scotland | C | 6.8% | 61.7% | 20.3% | 7.5% | 2.3% | 0.4% | 0.1% |
| Tunisia | F | 5.6% | 38.5% | 8.4% | 2.5% | 0.7% | 0.1% | 0.1% |
| Panama | L | 4.9% | 46.0% | 11.6% | 3.3% | 0.8% | 0.2% | 0.0% |
| Bosnia and Herzegovina | B | 10.1% | 55.8% | 17.0% | 3.9% | 0.6% | 0.1% | 0.0% |
| DR Congo | K | 3.9% | 33.2% | 8.2% | 2.0% | 0.4% | 0.1% | 0.0% |
| Uzbekistan | K | 5.8% | 42.1% | 12.0% | 3.2% | 0.9% | 0.1% | 0.0% |
| Saudi Arabia | H | 2.0% | 34.8% | 6.6% | 1.6% | 0.3% | 0.1% | 0.0% |
| Iraq | I | 2.9% | 26.4% | 6.1% | 1.4% | 0.2% | 0.0% | 0.0% |
| South Africa | A | 5.9% | 36.4% | 11.1% | 2.6% | 0.4% | 0.0% | 0.0% |
| Qatar | B | 4.0% | 30.8% | 6.4% | 1.1% | 0.1% | 0.0% | 0.0% |
| Curaçao | E | 0.6% | 11.5% | 1.4% | 0.1% | 0.0% | 0.0% | 0.0% |
| Jordan | J | 1.6% | 24.5% | 4.7% | 1.0% | 0.2% | 0.0% | 0.0% |
| Cape Verde | H | 1.5% | 27.9% | 4.3% | 0.8% | 0.1% | 0.0% | 0.0% |
| New Zealand | G | 3.4% | 28.7% | 7.4% | 1.3% | 0.2% | 0.0% | 0.0% |
| Ghana | L | 2.4% | 29.8% | 5.3% | 1.0% | 0.1% | 0.0% | 0.0% |
| Haiti | C | 1.1% | 19.1% | 2.9% | 0.5% | 0.1% | 0.0% | 0.0% |

## Honest caveats

- **No market anchor in the sim.** Unlike the group-stage card, the simulated probabilities use only the 50/50 Elo+DC blend. They embed all of that blend's known limitations (the blend does not beat the de-vigged market with confidence on the backtest) and add Monte-Carlo noise on top. Large edges most likely reflect model error, not free money — size conservatively.
- **Monte-Carlo noise.** With `20000` sims the standard error on a 50% probability is ~0.35 pp; deep-run probabilities (SF/Final/Win) are smaller and proportionally noisier. Re-run with more sims before acting on a marginal edge.
- **NO-side price approximation.** When a YES bid is missing we approximate the NO ask as `1 − YES_mid`, which is slightly optimistic; verify the live NO order book before sizing a NO position.
- **Host venue in Dixon-Coles.** The host home bonus is applied via Elo only; Dixon-Coles is queried neutral (it has no per-host venue term). This very slightly understates host strength in their group.
- **Group table.** The 12 groups are the FIFA final-draw result (5 Dec 2025), verified against the Wikipedia draw page and cross-checked to be consistent with the 72 scheduled fixtures in `results.csv` (every fixture intra-group, 6 per group).

### Group table used

- **Group A:** Mexico, South Africa, South Korea, Czech Republic
- **Group B:** Canada, Bosnia and Herzegovina, Qatar, Switzerland
- **Group C:** Brazil, Morocco, Haiti, Scotland
- **Group D:** United States, Paraguay, Australia, Turkey
- **Group E:** Germany, Curaçao, Ivory Coast, Ecuador
- **Group F:** Netherlands, Japan, Sweden, Tunisia
- **Group G:** Belgium, Egypt, Iran, New Zealand
- **Group H:** Spain, Cape Verde, Saudi Arabia, Uruguay
- **Group I:** France, Senegal, Iraq, Norway
- **Group J:** Argentina, Algeria, Austria, Jordan
- **Group K:** Portugal, DR Congo, Uzbekistan, Colombia
- **Group L:** England, Croatia, Ghana, Panama
