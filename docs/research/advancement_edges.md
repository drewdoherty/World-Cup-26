# Tournament-advancement edges — sim vs Polymarket

_Generated 2026-06-18 10:35:36 UTC._

Monte-Carlo simulation of the 2026 FIFA World Cup (`20000` sims, seed `42`) compared to live Polymarket advancement and group-winner markets. Edges are **fee-adjusted** and sized **quarter-Kelly** on the $1310 Polymarket pool (5% per-bet cap).

## Methodology

1. **Models.** Elo (rating + ordered-logit outcome model) and a time-decayed Dixon-Coles model are fit on the full international results history (`wca.card.fit_models`).
2. **prob_fn (honest caveat).** Every simulated match is driven by a **straight 50/50 average of the Elo and Dixon-Coles 1X2 probabilities** — there is **no market term**. The group-stage card anchors ~50% on the de-vigged market, but there are *no* odds for the later rounds, so a market-anchored blend is impossible here. These edges are therefore an independent, noisier model view, not ground truth.
3. **Venue.** The three hosts (United States, Mexico, Canada) get the home-advantage bonus on their own group fixtures (derived from the scheduled-fixture `neutral` flag, as `wca.card` does). Every other group match and **all** knockout matches are neutral.
4. **Knockout draws / ET / penalties.** A 90-minute knockout draw is resolved by the simulator's extra-time / penalty model. "Advancing" therefore **includes** winning on penalties — matching Polymarket resolution ("reach stage X" = the team is in stage X, however it got there).
5. **Stage mapping.** `advance to Knockout Stages` = reach the Round of 32 (top-2 or one of the eight best third-placed teams); `Reach Round of 16/QF/SF/Final` = win the preceding knockout tie; `World Cup Winner` = win the final; `Group X Winner` = finish 1st in the group. These match each market's resolution exactly.
6. **Edge.** For each team-stage market we price BOTH sides. YES buy price = best ask (mid of bid/ask when ask missing); NO buy price = 1 − YES bid. The Polymarket sports **taker fee** `0.03·p·(1−p)` per share is subtracted. Fee-adjusted edge = `sim_prob − buy_price − fee`. We report whichever side the simulation favours.
7. **Sizing.** A binary at buy price `c` (incl. fee) is a fixed-odds bet at decimal odds `1/c`; stake = quarter-Kelly at the simulated win probability, capped at 5% of the $1310 pool (`fraction=0.25`).

**Coverage.** 468 Polymarket World-Cup events pulled; 18 scored (advancement + group-winner); 336 team-stage markets matched to the simulation.

## Top edges

| # | Team | Market | Side | Sim P | PM price | Fee | Fee-adj edge | Stake ($) |
|---|------|--------|------|-------|----------|-----|--------------|-----------|
| 1 | Iran | Reach R32 (knockout) | YES | 59.9% | 0.413 | 0.007 | **+17.8%** | 65.50 |
| 2 | Australia | Win Group D | YES | 38.8% | 0.216 | 0.005 | **+16.7%** | 65.50 |
| 3 | United States | Win Group D | NO | 47.7% | 0.310 | 0.006 | **+16.1%** | 65.50 |
| 4 | United States | Reach Quarterfinals | NO | 80.5% | 0.640 | 0.007 | **+15.8%** | 65.50 |
| 5 | Iran | Reach Round of 16 | YES | 30.6% | 0.150 | 0.004 | **+15.2%** | 58.70 |
| 6 | Colombia | Reach Quarterfinals | YES | 45.6% | 0.300 | 0.006 | **+15.0%** | 65.50 |
| 7 | Colombia | Win Group K | YES | 64.6% | 0.490 | 0.007 | **+14.8%** | 65.50 |
| 8 | Portugal | Win Group K | NO | 70.2% | 0.560 | 0.007 | **+13.4%** | 65.50 |
| 9 | Colombia | Reach Round of 16 | YES | 71.1% | 0.570 | 0.007 | **+13.3%** | 65.50 |
| 10 | Switzerland | Win Group B | NO | 66.9% | 0.530 | 0.007 | **+13.1%** | 65.50 |
| 11 | Australia | Reach Round of 16 | YES | 53.7% | 0.400 | 0.007 | **+13.0%** | 65.50 |
| 12 | Qatar | Reach R32 (knockout) | YES | 39.9% | 0.270 | 0.006 | **+12.3%** | 55.63 |
| 13 | Portugal | Reach Quarterfinals | NO | 67.8% | 0.550 | 0.007 | **+12.0%** | 65.50 |
| 14 | France | Reach Quarterfinals | NO | 50.3% | 0.380 | 0.007 | **+11.6%** | 61.76 |
| 15 | Portugal | Reach Round of 16 | NO | 42.2% | 0.300 | 0.006 | **+11.5%** | 54.41 |
| 16 | United States | Reach Round of 16 | NO | 48.1% | 0.360 | 0.007 | **+11.4%** | 59.12 |
| 17 | Portugal | Reach Semifinals | NO | 82.4% | 0.710 | 0.006 | **+10.8%** | 65.50 |
| 18 | Morocco | Win Group C | NO | 81.3% | 0.700 | 0.006 | **+10.7%** | 65.50 |
| 19 | France | Reach Semifinals | NO | 65.9% | 0.550 | 0.007 | **+10.2%** | 65.50 |
| 20 | United States | Reach Semifinals | NO | 94.1% | 0.840 | 0.004 | **+9.7%** | 65.50 |

## All matched markets (fee-adjusted edge, descending)

| Team | Grp | Market | Side | Sim P | YES mid | Buy price | Fee | Raw edge | Fee-adj edge | Stake ($) |
|------|-----|--------|------|-------|---------|-----------|-----|----------|--------------|-----------|
| Iran | G | Reach R32 (knockout) | YES | 59.9% | 0.411 | 0.413 | 0.007 | +18.6% | +17.8% | 65.50 |
| Australia | D | Win Group D | YES | 38.8% | 0.211 | 0.216 | 0.005 | +17.2% | +16.7% | 65.50 |
| United States | D | Win Group D | NO | 47.7% | 0.695 | 0.310 | 0.006 | +16.7% | +16.1% | 65.50 |
| United States | D | Reach Quarterfinals | NO | 80.5% | 0.365 | 0.640 | 0.007 | +16.5% | +15.8% | 65.50 |
| Iran | G | Reach Round of 16 | YES | 30.6% | 0.135 | 0.150 | 0.004 | +15.6% | +15.2% | 58.70 |
| Colombia | K | Reach Quarterfinals | YES | 45.6% | 0.290 | 0.300 | 0.006 | +15.6% | +15.0% | 65.50 |
| Colombia | K | Win Group K | YES | 64.6% | 0.485 | 0.490 | 0.007 | +15.6% | +14.8% | 65.50 |
| Portugal | K | Win Group K | NO | 70.2% | 0.445 | 0.560 | 0.007 | +14.2% | +13.4% | 65.50 |
| Colombia | K | Reach Round of 16 | YES | 71.1% | 0.540 | 0.570 | 0.007 | +14.1% | +13.3% | 65.50 |
| Switzerland | B | Win Group B | NO | 66.9% | 0.475 | 0.530 | 0.007 | +13.9% | +13.1% | 65.50 |
| Australia | D | Reach Round of 16 | YES | 53.7% | 0.385 | 0.400 | 0.007 | +13.7% | +13.0% | 65.50 |
| Qatar | B | Reach R32 (knockout) | YES | 39.9% | 0.265 | 0.270 | 0.006 | +12.9% | +12.3% | 55.63 |
| Portugal | K | Reach Quarterfinals | NO | 67.8% | 0.460 | 0.550 | 0.007 | +12.8% | +12.0% | 65.50 |
| France | I | Reach Quarterfinals | NO | 50.3% | 0.625 | 0.380 | 0.007 | +12.3% | +11.6% | 61.76 |
| Portugal | K | Reach Round of 16 | NO | 42.2% | 0.705 | 0.300 | 0.006 | +12.2% | +11.5% | 54.41 |
| United States | D | Reach Round of 16 | NO | 48.1% | 0.645 | 0.360 | 0.007 | +12.1% | +11.4% | 59.12 |
| Portugal | K | Reach Semifinals | NO | 82.4% | 0.295 | 0.710 | 0.006 | +11.4% | +10.8% | 65.50 |
| Morocco | C | Win Group C | NO | 81.3% | 0.305 | 0.700 | 0.006 | +11.3% | +10.7% | 65.50 |
| France | I | Reach Semifinals | NO | 65.9% | 0.460 | 0.550 | 0.007 | +10.9% | +10.2% | 65.50 |
| United States | D | Reach Semifinals | NO | 94.1% | 0.170 | 0.840 | 0.004 | +10.1% | +9.7% | 65.50 |
| DR Congo | K | Reach R32 (knockout) | NO | 52.1% | 0.590 | 0.420 | 0.007 | +10.1% | +9.3% | 53.41 |
| Portugal | K | Reach R32 (knockout) | NO | 14.4% | 0.952 | 0.050 | 0.001 | +9.4% | +9.3% | 31.98 |
| Australia | D | Reach Quarterfinals | YES | 20.6% | 0.102 | 0.119 | 0.003 | +8.7% | +8.4% | 31.26 |
| France | I | Reach Round of 16 | NO | 24.5% | 0.845 | 0.160 | 0.004 | +8.5% | +8.0% | 31.52 |
| Norway | I | Reach Quarterfinals | NO | 78.5% | 0.315 | 0.700 | 0.006 | +8.5% | +7.8% | 65.50 |
| Colombia | K | Reach Semifinals | YES | 23.2% | 0.140 | 0.150 | 0.004 | +8.2% | +7.8% | 30.22 |
| Iran | G | Win Group G | YES | 15.8% | 0.076 | 0.080 | 0.002 | +7.8% | +7.6% | 27.10 |
| Australia | D | Reach R32 (knockout) | YES | 94.7% | 0.865 | 0.870 | 0.003 | +7.7% | +7.3% | 65.50 |
| Uzbekistan | K | Reach R32 (knockout) | YES | 26.8% | 0.175 | 0.190 | 0.005 | +7.8% | +7.3% | 29.72 |
| Canada | B | Win Group B | YES | 43.0% | 0.345 | 0.350 | 0.007 | +8.0% | +7.3% | 37.01 |
| Norway | I | Reach Round of 16 | NO | 50.8% | 0.585 | 0.430 | 0.007 | +7.8% | +7.1% | 41.06 |
| Portugal | K | Reach Final | NO | 91.4% | 0.165 | 0.840 | 0.004 | +7.4% | +7.0% | 65.50 |
| France | I | Reach Final | NO | 79.3% | 0.285 | 0.720 | 0.006 | +7.3% | +6.7% | 65.50 |
| South Korea | A | Win Group A | NO | 72.3% | 0.355 | 0.650 | 0.007 | +7.3% | +6.6% | 62.72 |
| Netherlands | F | Win Group F | YES | 51.3% | 0.435 | 0.440 | 0.007 | +7.3% | +6.6% | 38.94 |
| England | L | Reach Final | NO | 82.1% | 0.255 | 0.750 | 0.006 | +7.1% | +6.5% | 65.50 |
| Japan | F | Reach Round of 16 | NO | 66.2% | 0.415 | 0.590 | 0.007 | +7.2% | +6.5% | 52.77 |
| South Africa | A | Reach R32 (knockout) | YES | 24.9% | 0.175 | 0.180 | 0.004 | +6.9% | +6.5% | 25.99 |
| France | I | Win the World Cup | NO | 88.4% | 0.184 | 0.816 | 0.005 | +6.8% | +6.3% | 65.50 |
| Argentina | J | Reach Semifinals | YES | 44.9% | 0.370 | 0.380 | 0.007 | +6.9% | +6.2% | 33.33 |
| Norway | I | Reach Semifinals | NO | 90.6% | 0.165 | 0.840 | 0.004 | +6.6% | +6.2% | 65.50 |
| Turkey | D | Reach Quarterfinals | NO | 93.6% | 0.135 | 0.870 | 0.003 | +6.6% | +6.2% | 65.50 |
| Argentina | J | Reach Final | YES | 28.6% | 0.215 | 0.220 | 0.005 | +6.6% | +6.1% | 25.83 |
| Senegal | I | Reach R32 (knockout) | NO | 41.7% | 0.660 | 0.350 | 0.007 | +6.7% | +6.0% | 30.64 |
| England | L | Reach Semifinals | NO | 68.6% | 0.390 | 0.620 | 0.007 | +6.6% | +5.9% | 51.93 |
| Turkey | D | Reach R32 (knockout) | NO | 57.6% | 0.505 | 0.510 | 0.007 | +6.6% | +5.8% | 39.44 |
| Brazil | C | Win Group C | YES | 65.5% | 0.585 | 0.590 | 0.007 | +6.5% | +5.8% | 47.12 |
| Ghana | L | Reach Round of 16 | NO | 87.2% | 0.200 | 0.810 | 0.005 | +6.2% | +5.8% | 65.50 |
| England | L | Reach Quarterfinals | NO | 47.4% | 0.600 | 0.410 | 0.007 | +6.4% | +5.6% | 31.75 |
| Czech Republic | A | Reach R32 (knockout) | NO | 52.3% | 0.545 | 0.460 | 0.007 | +6.3% | +5.6% | 34.22 |
| Cape Verde | H | Reach R32 (knockout) | YES | 50.3% | 0.435 | 0.440 | 0.007 | +6.3% | +5.5% | 32.81 |
| Scotland | C | Reach R32 (knockout) | YES | 83.0% | 0.765 | 0.770 | 0.005 | +6.0% | +5.5% | 65.50 |
| Mexico | A | Win Group A | YES | 67.1% | 0.605 | 0.610 | 0.007 | +6.1% | +5.4% | 45.82 |
| Switzerland | B | Reach R32 (knockout) | NO | 17.6% | 0.879 | 0.121 | 0.003 | +5.5% | +5.2% | 19.56 |
| Argentina | J | Win the World Cup | YES | 17.8% | 0.122 | 0.123 | 0.003 | +5.5% | +5.2% | 19.50 |
| Egypt | G | Reach R32 (knockout) | NO | 24.6% | 0.820 | 0.190 | 0.005 | +5.6% | +5.1% | 20.87 |
| Germany | E | Reach Round of 16 | NO | 33.7% | 0.730 | 0.280 | 0.006 | +5.7% | +5.1% | 23.23 |
| Iran | G | Reach Quarterfinals | YES | 10.9% | 0.045 | 0.059 | 0.002 | +5.0% | +4.9% | 16.97 |
| Colombia | K | Reach Final | YES | 11.7% | 0.059 | 0.067 | 0.002 | +5.0% | +4.8% | 17.03 |
| Qatar | B | Win Group B | YES | 8.5% | 0.035 | 0.036 | 0.001 | +4.9% | +4.8% | 16.40 |
| Germany | E | Reach Semifinals | NO | 80.3% | 0.260 | 0.750 | 0.006 | +5.3% | +4.8% | 64.09 |
| Belgium | G | Reach R32 (knockout) | NO | 9.8% | 0.954 | 0.049 | 0.001 | +4.9% | +4.7% | 16.28 |
| Japan | F | Win Group F | NO | 78.3% | 0.275 | 0.730 | 0.006 | +5.3% | +4.7% | 58.52 |
| Egypt | G | Win Group G | NO | 80.1% | 0.255 | 0.750 | 0.006 | +5.1% | +4.6% | 61.41 |
| Ivory Coast | E | Win Group E | YES | 26.7% | 0.214 | 0.217 | 0.005 | +5.0% | +4.5% | 18.88 |
| Bosnia and Herzegovina | B | Reach R32 (knockout) | NO | 36.9% | 0.685 | 0.320 | 0.007 | +4.9% | +4.2% | 20.51 |
| Uruguay | H | Win Group H | NO | 84.6% | 0.205 | 0.800 | 0.005 | +4.6% | +4.1% | 65.50 |
| Canada | B | Reach Round of 16 | NO | 63.8% | 0.415 | 0.590 | 0.007 | +4.8% | +4.1% | 33.01 |
| Colombia | K | Win the World Cup | YES | 5.8% | 0.017 | 0.017 | 0.001 | +4.1% | +4.0% | 13.37 |
| Scotland | C | Win Group C | YES | 15.6% | 0.108 | 0.114 | 0.003 | +4.2% | +3.9% | 14.53 |
| Japan | F | Reach Quarterfinals | NO | 82.4% | 0.230 | 0.780 | 0.005 | +4.4% | +3.9% | 59.45 |
| Senegal | I | Reach Quarterfinals | NO | 91.2% | 0.145 | 0.870 | 0.003 | +4.2% | +3.8% | 65.50 |
| Morocco | C | Reach Quarterfinals | NO | 75.4% | 0.295 | 0.710 | 0.006 | +4.4% | +3.7% | 43.12 |
| Portugal | K | Win the World Cup | NO | 95.9% | 0.081 | 0.920 | 0.002 | +3.9% | +3.7% | 65.50 |
| Iraq | I | Reach R32 (knockout) | YES | 8.8% | 0.045 | 0.050 | 0.001 | +3.8% | +3.6% | 12.58 |
| Germany | E | Reach Quarterfinals | NO | 66.3% | 0.400 | 0.620 | 0.007 | +4.3% | +3.6% | 31.60 |
| Morocco | C | Reach Round of 16 | NO | 54.3% | 0.505 | 0.500 | 0.007 | +4.3% | +3.5% | 23.57 |
| United States | D | Reach Final | NO | 98.1% | 0.060 | 0.945 | 0.002 | +3.6% | +3.5% | 65.50 |
| Croatia | L | Reach R32 (knockout) | YES | 81.0% | 0.755 | 0.770 | 0.005 | +4.0% | +3.5% | 50.56 |
| Senegal | I | Reach Round of 16 | NO | 77.0% | 0.280 | 0.730 | 0.006 | +4.0% | +3.4% | 42.64 |
| Brazil | C | Reach Semifinals | YES | 29.9% | 0.250 | 0.260 | 0.006 | +3.9% | +3.4% | 14.98 |
| Spain | H | Reach Quarterfinals | NO | 49.1% | 0.555 | 0.450 | 0.007 | +4.1% | +3.4% | 20.27 |
| France | I | Win Group I | YES | 79.9% | 0.755 | 0.760 | 0.005 | +3.9% | +3.3% | 46.19 |
| England | L | Reach Round of 16 | NO | 23.8% | 0.815 | 0.200 | 0.005 | +3.8% | +3.3% | 13.49 |
| Ecuador | E | Reach Quarterfinals | YES | 17.6% | 0.130 | 0.140 | 0.004 | +3.6% | +3.3% | 12.50 |
| Tunisia | F | Reach R32 (knockout) | YES | 10.5% | 0.065 | 0.070 | 0.002 | +3.5% | +3.3% | 11.50 |
| Uruguay | H | Reach Quarterfinals | NO | 83.7% | 0.205 | 0.800 | 0.005 | +3.7% | +3.2% | 54.28 |
| Saudi Arabia | H | Reach R32 (knockout) | NO | 60.9% | 0.435 | 0.570 | 0.007 | +3.9% | +3.2% | 24.87 |
| South Korea | A | Reach Round of 16 | YES | 51.0% | 0.455 | 0.470 | 0.007 | +4.0% | +3.2% | 20.10 |
| Ghana | L | Reach R32 (knockout) | NO | 32.8% | 0.720 | 0.290 | 0.006 | +3.8% | +3.2% | 14.83 |
| Austria | J | Reach Semifinals | YES | 6.5% | 0.031 | 0.032 | 0.001 | +3.3% | +3.2% | 10.74 |
| Sweden | F | Reach Round of 16 | NO | 70.8% | 0.335 | 0.670 | 0.007 | +3.8% | +3.1% | 31.77 |
| Netherlands | F | Reach Round of 16 | NO | 50.6% | 0.545 | 0.470 | 0.007 | +3.6% | +2.9% | 18.00 |
| Netherlands | F | Reach Quarterfinals | NO | 67.5% | 0.370 | 0.640 | 0.007 | +3.5% | +2.8% | 26.05 |
| Brazil | C | Win the World Cup | YES | 9.7% | 0.067 | 0.067 | 0.002 | +3.0% | +2.8% | 9.82 |
| England | L | Win the World Cup | NO | 90.2% | 0.129 | 0.872 | 0.003 | +3.0% | +2.6% | 65.50 |
| Sweden | F | Win Group F | NO | 74.1% | 0.295 | 0.710 | 0.006 | +3.1% | +2.5% | 28.82 |
| Mexico | A | Reach Quarterfinals | NO | 74.1% | 0.305 | 0.710 | 0.006 | +3.1% | +2.5% | 28.64 |
| Cape Verde | H | Reach Quarterfinals | NO | 98.0% | 0.051 | 0.955 | 0.001 | +2.5% | +2.4% | 65.50 |
| Curaçao | E | Reach R32 (knockout) | YES | 4.9% | 0.023 | 0.025 | 0.001 | +2.4% | +2.3% | 7.84 |
| Norway | I | Win Group I | NO | 81.8% | 0.215 | 0.790 | 0.005 | +2.8% | +2.3% | 36.62 |
| Uruguay | H | Reach R32 (knockout) | NO | 24.8% | 0.785 | 0.220 | 0.005 | +2.8% | +2.3% | 9.64 |
| Austria | J | Reach Round of 16 | YES | 34.9% | 0.300 | 0.320 | 0.007 | +2.9% | +2.3% | 11.07 |
| Cape Verde | H | Win Group H | YES | 5.6% | 0.031 | 0.032 | 0.001 | +2.4% | +2.3% | 7.68 |
| Ivory Coast | E | Reach Semifinals | NO | 96.0% | 0.070 | 0.936 | 0.002 | +2.4% | +2.3% | 65.50 |
| Paraguay | D | Reach Round of 16 | YES | 14.6% | 0.115 | 0.120 | 0.003 | +2.6% | +2.3% | 8.43 |
| Australia | D | Reach Semifinals | YES | 7.2% | 0.041 | 0.048 | 0.001 | +2.4% | +2.3% | 7.78 |
| Belgium | G | Win Group G | NO | 40.0% | 0.635 | 0.370 | 0.007 | +3.0% | +2.3% | 11.86 |
| DR Congo | K | Reach Quarterfinals | NO | 97.4% | 0.060 | 0.950 | 0.001 | +2.4% | +2.2% | 65.50 |
| Germany | E | Win Group E | NO | 31.8% | 0.715 | 0.290 | 0.006 | +2.8% | +2.2% | 10.34 |
| Morocco | C | Reach Semifinals | NO | 89.5% | 0.140 | 0.870 | 0.003 | +2.5% | +2.2% | 56.67 |
| Turkey | D | Reach Semifinals | NO | 97.3% | 0.055 | 0.950 | 0.001 | +2.3% | +2.2% | 65.50 |
| Norway | I | Reach Final | NO | 96.3% | 0.065 | 0.940 | 0.002 | +2.3% | +2.1% | 65.50 |
| Qatar | B | Reach Round of 16 | YES | 8.9% | 0.038 | 0.066 | 0.002 | +2.3% | +2.1% | 7.43 |
| Germany | E | Reach Final | NO | 90.4% | 0.125 | 0.880 | 0.003 | +2.4% | +2.0% | 57.13 |
| DR Congo | K | Reach Round of 16 | NO | 88.4% | 0.155 | 0.860 | 0.004 | +2.4% | +2.0% | 48.84 |
| Spain | H | Win the World Cup | YES | 16.1% | 0.138 | 0.138 | 0.004 | +2.3% | +2.0% | 7.57 |
| Japan | F | Reach Final | NO | 97.2% | 0.049 | 0.952 | 0.001 | +2.0% | +1.8% | 65.50 |
| DR Congo | K | Reach Semifinals | NO | 99.4% | 0.042 | 0.975 | 0.001 | +1.9% | +1.8% | 65.50 |
| South Korea | A | Reach Quarterfinals | YES | 19.2% | 0.165 | 0.170 | 0.004 | +2.2% | +1.8% | 7.07 |
| Ecuador | E | Reach Round of 16 | YES | 38.5% | 0.355 | 0.360 | 0.007 | +2.5% | +1.8% | 9.20 |
| Mexico | A | Reach R32 (knockout) | YES | 98.9% | 0.966 | 0.970 | 0.001 | +1.9% | +1.8% | 65.50 |
| United States | D | Win the World Cup | NO | 99.5% | 0.022 | 0.978 | 0.001 | +1.7% | +1.7% | 65.50 |
| Turkey | D | Win Group D | NO | 94.8% | 0.075 | 0.930 | 0.002 | +1.8% | +1.6% | 65.50 |
| Netherlands | F | Reach Final | NO | 91.8% | 0.105 | 0.900 | 0.003 | +1.8% | +1.5% | 49.98 |
| Sweden | F | Reach R32 (knockout) | YES | 96.0% | 0.938 | 0.944 | 0.002 | +1.6% | +1.5% | 65.50 |
| Spain | H | Win Group H | YES | 75.0% | 0.725 | 0.730 | 0.006 | +2.0% | +1.4% | 17.97 |
| Saudi Arabia | H | Win Group H | YES | 4.0% | 0.024 | 0.025 | 0.001 | +1.5% | +1.4% | 4.83 |
| Canada | B | Reach R32 (knockout) | NO | 18.8% | 0.835 | 0.170 | 0.004 | +1.8% | +1.4% | 5.60 |
| Turkey | D | Reach Round of 16 | NO | 80.9% | 0.220 | 0.790 | 0.005 | +1.9% | +1.4% | 22.40 |
| South Africa | A | Reach Round of 16 | YES | 7.3% | 0.045 | 0.058 | 0.002 | +1.5% | +1.4% | 4.83 |
| Japan | F | Reach Semifinals | NO | 92.6% | 0.110 | 0.910 | 0.002 | +1.6% | +1.4% | 51.23 |
| Mexico | A | Reach Final | NO | 96.2% | 0.053 | 0.947 | 0.002 | +1.5% | +1.3% | 65.50 |
| Morocco | C | Reach Final | NO | 95.5% | 0.068 | 0.940 | 0.002 | +1.5% | +1.3% | 65.50 |
| Czech Republic | A | Reach Quarterfinals | YES | 7.4% | 0.055 | 0.060 | 0.002 | +1.4% | +1.2% | 4.26 |
| Norway | I | Win the World Cup | NO | 98.7% | 0.026 | 0.974 | 0.001 | +1.3% | +1.2% | 65.50 |
| Bosnia and Herzegovina | B | Reach Semifinals | NO | 99.2% | 0.026 | 0.980 | 0.001 | +1.2% | +1.2% | 65.50 |
| DR Congo | K | Win Group K | NO | 95.2% | 0.062 | 0.939 | 0.002 | +1.3% | +1.2% | 64.53 |
| Brazil | C | Reach Round of 16 | YES | 68.8% | 0.660 | 0.670 | 0.007 | +1.8% | +1.1% | 11.41 |
| Argentina | J | Win Group J | YES | 86.5% | 0.845 | 0.850 | 0.004 | +1.5% | +1.1% | 24.48 |
| Uruguay | H | Reach Final | YES | 3.4% | 0.021 | 0.022 | 0.001 | +1.2% | +1.1% | 3.65 |
| Haiti | C | Reach Quarterfinals | NO | 99.9% | 0.035 | 0.988 | 0.000 | +1.1% | +1.1% | 65.50 |
| Scotland | C | Reach Quarterfinals | YES | 10.3% | 0.080 | 0.090 | 0.002 | +1.3% | +1.1% | 3.80 |
| Cape Verde | H | Reach Semifinals | NO | 99.7% | 0.019 | 0.986 | 0.000 | +1.1% | +1.0% | 65.50 |
| Paraguay | D | Reach R32 (knockout) | NO | 63.7% | 0.390 | 0.620 | 0.007 | +1.7% | +1.0% | 9.03 |
| Sweden | F | Reach Final | NO | 98.8% | 0.029 | 0.977 | 0.001 | +1.1% | +1.0% | 65.50 |
| Jordan | J | Reach Semifinals | NO | 99.9% | 0.015 | 0.989 | 0.000 | +1.0% | +1.0% | 65.50 |
| Ecuador | E | Reach R32 (knockout) | NO | 23.5% | 0.785 | 0.220 | 0.005 | +1.5% | +1.0% | 4.10 |
| Bosnia and Herzegovina | B | Reach Final | NO | 99.9% | 0.014 | 0.989 | 0.000 | +1.0% | +1.0% | 65.50 |
| Austria | J | Reach R32 (knockout) | YES | 94.1% | 0.925 | 0.930 | 0.002 | +1.1% | +0.9% | 45.47 |
| Germany | E | Win the World Cup | NO | 95.5% | 0.057 | 0.944 | 0.002 | +1.1% | +0.9% | 54.85 |
| Algeria | J | Reach R32 (knockout) | NO | 45.6% | 0.565 | 0.440 | 0.007 | +1.6% | +0.9% | 5.31 |
| Canada | B | Reach Quarterfinals | NO | 88.2% | 0.135 | 0.870 | 0.003 | +1.2% | +0.9% | 23.04 |
| Panama | L | Reach R32 (knockout) | YES | 12.1% | 0.105 | 0.110 | 0.003 | +1.1% | +0.9% | 3.14 |
| Switzerland | B | Reach Quarterfinals | YES | 22.3% | 0.195 | 0.210 | 0.005 | +1.3% | +0.8% | 3.33 |
| Curaçao | E | Reach Semifinals | NO | 100.0% | 0.013 | 0.992 | 0.000 | +0.8% | +0.8% | 65.50 |
| Spain | H | Reach R32 (knockout) | NO | 4.2% | 0.968 | 0.033 | 0.001 | +0.9% | +0.8% | 2.61 |
| Ghana | L | Reach Final | NO | 99.9% | 0.015 | 0.991 | 0.000 | +0.8% | +0.8% | 65.50 |
| Saudi Arabia | H | Reach Semifinals | NO | 99.7% | 0.027 | 0.989 | 0.000 | +0.8% | +0.7% | 65.50 |
| Ecuador | E | Reach Semifinals | YES | 7.0% | 0.055 | 0.061 | 0.002 | +0.9% | +0.7% | 2.58 |
| Canada | B | Reach Final | NO | 99.3% | 0.017 | 0.985 | 0.000 | +0.8% | +0.7% | 65.50 |
| New Zealand | G | Win Group G | YES | 4.3% | 0.034 | 0.035 | 0.001 | +0.8% | +0.7% | 2.32 |
| South Korea | A | Reach R32 (knockout) | YES | 94.8% | 0.935 | 0.940 | 0.002 | +0.9% | +0.7% | 38.24 |
| Uzbekistan | K | Reach Semifinals | NO | 99.5% | 0.017 | 0.988 | 0.000 | +0.7% | +0.7% | 65.50 |
| Morocco | C | Win the World Cup | NO | 98.4% | 0.024 | 0.977 | 0.001 | +0.7% | +0.7% | 65.50 |
| Uzbekistan | K | Reach Round of 16 | YES | 6.6% | 0.035 | 0.058 | 0.002 | +0.8% | +0.7% | 2.30 |
| Belgium | G | Win the World Cup | YES | 2.4% | 0.017 | 0.017 | 0.001 | +0.7% | +0.6% | 2.17 |
| Paraguay | D | Win Group D | YES | 3.7% | 0.026 | 0.030 | 0.001 | +0.7% | +0.6% | 2.19 |
| Japan | F | Win the World Cup | NO | 99.1% | 0.017 | 0.984 | 0.000 | +0.7% | +0.6% | 65.50 |
| Saudi Arabia | H | Reach Final | NO | 99.9% | 0.012 | 0.993 | 0.000 | +0.6% | +0.6% | 65.50 |
| Ghana | L | Reach Quarterfinals | NO | 96.8% | 0.049 | 0.961 | 0.001 | +0.7% | +0.6% | 52.97 |
| Norway | I | Reach R32 (knockout) | YES | 97.7% | 0.960 | 0.970 | 0.001 | +0.7% | +0.6% | 65.50 |
| Czech Republic | A | Reach Final | NO | 99.5% | 0.011 | 0.989 | 0.000 | +0.6% | +0.6% | 65.50 |
| Uruguay | H | Win the World Cup | YES | 1.3% | 0.007 | 0.007 | 0.000 | +0.6% | +0.6% | 1.93 |
| Ecuador | E | Reach Final | YES | 2.9% | 0.018 | 0.023 | 0.001 | +0.6% | +0.6% | 1.95 |
| Australia | D | Reach Final | YES | 2.4% | 0.012 | 0.018 | 0.001 | +0.6% | +0.6% | 1.94 |
| Ghana | L | Reach Semifinals | NO | 99.3% | 0.028 | 0.987 | 0.000 | +0.6% | +0.6% | 65.50 |
| Ecuador | E | Win the World Cup | YES | 1.1% | 0.005 | 0.005 | 0.000 | +0.6% | +0.6% | 1.84 |
| New Zealand | G | Reach Final | NO | 100.0% | 0.017 | 0.994 | 0.000 | +0.6% | +0.5% | 65.50 |
| New Zealand | G | Reach R32 (knockout) | YES | 32.2% | 0.305 | 0.310 | 0.006 | +1.2% | +0.5% | 2.46 |
| Austria | J | Win Group J | NO | 87.8% | 0.135 | 0.870 | 0.003 | +0.8% | +0.5% | 12.56 |
| South Africa | A | Reach Semifinals | NO | 99.8% | 0.021 | 0.993 | 0.000 | +0.5% | +0.5% | 65.50 |
| Paraguay | D | Reach Quarterfinals | NO | 95.6% | 0.060 | 0.950 | 0.001 | +0.6% | +0.5% | 32.53 |
| Brazil | C | Reach R32 (knockout) | YES | 97.5% | 0.964 | 0.970 | 0.001 | +0.5% | +0.5% | 50.90 |
| Argentina | J | Reach Quarterfinals | YES | 60.2% | 0.585 | 0.590 | 0.007 | +1.2% | +0.4% | 3.61 |
| Ghana | L | Win Group L | NO | 95.1% | 0.056 | 0.945 | 0.002 | +0.6% | +0.4% | 26.60 |
| Saudi Arabia | H | Reach Quarterfinals | NO | 98.4% | 0.036 | 0.979 | 0.001 | +0.5% | +0.4% | 65.50 |
| Sweden | F | Reach Quarterfinals | NO | 87.8% | 0.145 | 0.870 | 0.003 | +0.8% | +0.4% | 10.88 |
| Croatia | L | Win the World Cup | YES | 1.1% | 0.007 | 0.007 | 0.000 | +0.4% | +0.4% | 1.37 |
| Tunisia | F | Win Group F | YES | 1.1% | 0.005 | 0.007 | 0.000 | +0.4% | +0.4% | 1.30 |
| Spain | H | Reach Final | YES | 26.0% | 0.245 | 0.250 | 0.006 | +1.0% | +0.4% | 1.73 |
| Haiti | C | Reach Round of 16 | NO | 99.4% | 0.022 | 0.990 | 0.000 | +0.4% | +0.4% | 65.50 |
| Senegal | I | Reach Final | NO | 99.1% | 0.017 | 0.987 | 0.000 | +0.4% | +0.4% | 65.50 |
| Panama | L | Reach Quarterfinals | NO | 99.3% | 0.019 | 0.989 | 0.000 | +0.4% | +0.4% | 65.50 |
| Ivory Coast | E | Reach R32 (knockout) | YES | 96.4% | 0.952 | 0.959 | 0.001 | +0.5% | +0.3% | 28.13 |
| South Africa | A | Reach Final | NO | 100.0% | 0.007 | 0.996 | 0.000 | +0.4% | +0.3% | 65.50 |
| Senegal | I | Win the World Cup | NO | 99.7% | 0.007 | 0.994 | 0.000 | +0.3% | +0.3% | 65.50 |
| Algeria | J | Win Group J | NO | 99.1% | 0.014 | 0.987 | 0.000 | +0.4% | +0.3% | 65.50 |
| Tunisia | F | Reach Quarterfinals | NO | 99.6% | 0.017 | 0.992 | 0.000 | +0.4% | +0.3% | 65.50 |
| Colombia | K | Reach R32 (knockout) | YES | 99.2% | 0.980 | 0.988 | 0.000 | +0.4% | +0.3% | 65.50 |
| Senegal | I | Reach Semifinals | NO | 97.1% | 0.058 | 0.967 | 0.001 | +0.4% | +0.3% | 31.10 |
| Austria | J | Reach Final | YES | 2.2% | 0.018 | 0.019 | 0.001 | +0.3% | +0.3% | 0.95 |
| Tunisia | F | Reach Semifinals | NO | 99.9% | 0.011 | 0.996 | 0.000 | +0.3% | +0.3% | 65.50 |
| Germany | E | Reach R32 (knockout) | YES | 99.8% | 0.992 | 0.995 | 0.000 | +0.3% | +0.3% | 65.50 |
| Panama | L | Win Group L | YES | 0.9% | 0.005 | 0.006 | 0.000 | +0.3% | +0.3% | 0.91 |
| DR Congo | K | Reach Final | NO | 99.9% | 0.007 | 0.996 | 0.000 | +0.3% | +0.3% | 65.50 |
| Panama | L | Reach Round of 16 | NO | 97.5% | 0.043 | 0.972 | 0.001 | +0.3% | +0.3% | 30.52 |
| Curaçao | E | Reach Quarterfinals | NO | 100.0% | 0.004 | 0.997 | 0.000 | +0.3% | +0.2% | 65.50 |
| Cape Verde | H | Reach Final | NO | 100.0% | 0.006 | 0.997 | 0.000 | +0.3% | +0.2% | 65.50 |
| Iran | G | Win the World Cup | YES | 0.3% | 0.001 | 0.001 | 0.000 | +0.2% | +0.2% | 0.73 |
| Sweden | F | Win the World Cup | NO | 99.7% | 0.005 | 0.995 | 0.000 | +0.2% | +0.2% | 65.50 |
| Iraq | I | Win Group I | YES | 0.4% | 0.002 | 0.002 | 0.000 | +0.2% | +0.2% | 0.70 |
| Ivory Coast | E | Win the World Cup | NO | 99.7% | 0.005 | 0.995 | 0.000 | +0.2% | +0.2% | 65.50 |
| Argentina | J | Reach R32 (knockout) | YES | 99.8% | 0.994 | 0.996 | 0.000 | +0.2% | +0.2% | 65.50 |
| Jordan | J | Reach Final | NO | 100.0% | 0.005 | 0.998 | 0.000 | +0.2% | +0.2% | 65.50 |
| United States | D | Reach R32 (knockout) | NO | 3.3% | 0.973 | 0.030 | 0.001 | +0.3% | +0.2% | 0.65 |
| Austria | J | Win the World Cup | YES | 0.7% | 0.005 | 0.005 | 0.000 | +0.2% | +0.2% | 0.63 |
| Qatar | B | Reach Final | NO | 100.0% | 0.010 | 0.998 | 0.000 | +0.2% | +0.2% | 65.50 |
| Haiti | C | Reach Semifinals | NO | 100.0% | 0.004 | 0.998 | 0.000 | +0.2% | +0.2% | 65.50 |
| Tunisia | F | Reach Final | NO | 100.0% | 0.007 | 0.998 | 0.000 | +0.2% | +0.2% | 65.50 |
| Curaçao | E | Win Group E | NO | 99.9% | 0.004 | 0.997 | 0.000 | +0.2% | +0.2% | 65.50 |
| Sweden | F | Reach Semifinals | NO | 96.0% | 0.056 | 0.957 | 0.001 | +0.3% | +0.1% | 11.49 |
| Qatar | B | Reach Semifinals | NO | 99.8% | 0.012 | 0.997 | 0.000 | +0.1% | +0.1% | 65.50 |
| Uzbekistan | K | Win Group K | YES | 0.9% | 0.005 | 0.007 | 0.000 | +0.2% | +0.1% | 0.43 |
| Ivory Coast | E | Reach Final | NO | 98.9% | 0.016 | 0.987 | 0.000 | +0.2% | +0.1% | 31.54 |
| Bosnia and Herzegovina | B | Reach Quarterfinals | NO | 95.3% | 0.060 | 0.950 | 0.001 | +0.3% | +0.1% | 7.92 |
| Australia | D | Win the World Cup | YES | 0.6% | 0.005 | 0.005 | 0.000 | +0.1% | +0.1% | 0.35 |
| Iran | G | Reach Semifinals | YES | 3.6% | 0.022 | 0.034 | 0.001 | +0.2% | +0.1% | 0.34 |
| France | I | Reach R32 (knockout) | YES | 99.6% | 0.993 | 0.995 | 0.000 | +0.1% | +0.1% | 65.50 |
| Curaçao | E | Reach Final | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Haiti | C | Reach Final | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Haiti | C | Win Group C | NO | 99.8% | 0.005 | 0.997 | 0.000 | +0.1% | +0.1% | 65.50 |
| Turkey | D | Reach Final | NO | 99.1% | 0.010 | 0.990 | 0.000 | +0.1% | +0.1% | 32.17 |
| Iraq | I | Reach Final | NO | 100.0% | 0.007 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Panama | L | Reach Final | NO | 100.0% | 0.003 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Czech Republic | A | Reach Round of 16 | NO | 80.6% | 0.215 | 0.800 | 0.005 | +0.6% | +0.1% | 1.43 |
| Turkey | D | Win the World Cup | NO | 99.7% | 0.005 | 0.996 | 0.000 | +0.1% | +0.1% | 65.50 |
| Bosnia and Herzegovina | B | Win the World Cup | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Ghana | L | Win the World Cup | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| DR Congo | K | Win the World Cup | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Iraq | I | Reach Quarterfinals | NO | 99.6% | 0.011 | 0.995 | 0.000 | +0.1% | +0.1% | 47.31 |
| Uzbekistan | K | Reach Final | NO | 99.9% | 0.005 | 0.998 | 0.000 | +0.1% | +0.1% | 65.50 |
| Iraq | I | Reach Semifinals | NO | 100.0% | 0.002 | 0.999 | 0.000 | +0.1% | +0.1% | 65.50 |
| Egypt | G | Win the World Cup | NO | 99.9% | 0.003 | 0.998 | 0.000 | +0.1% | +0.1% | 65.50 |
| Scotland | C | Reach Final | NO | 99.2% | 0.014 | 0.991 | 0.000 | +0.1% | +0.1% | 21.84 |
| Curaçao | E | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Jordan | J | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Iraq | I | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Tunisia | F | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Haiti | C | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Panama | L | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Qatar | B | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| South Korea | A | Win the World Cup | YES | 0.5% | 0.004 | 0.004 | 0.000 | +0.1% | +0.0% | 0.16 |
| Japan | F | Reach R32 (knockout) | NO | 15.4% | 0.860 | 0.150 | 0.004 | +0.4% | +0.0% | 0.18 |
| Panama | L | Reach Semifinals | NO | 99.9% | 0.006 | 0.998 | 0.000 | +0.1% | +0.0% | 65.50 |
| Cape Verde | H | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Canada | B | Win the World Cup | NO | 99.8% | 0.003 | 0.998 | 0.000 | +0.0% | +0.0% | 65.50 |
| Uzbekistan | K | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| New Zealand | G | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Saudi Arabia | H | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| England | L | Reach R32 (knockout) | YES | 99.6% | 0.994 | 0.996 | 0.000 | +0.0% | +0.0% | 27.89 |
| Czech Republic | A | Win the World Cup | YES | 0.1% | 0.001 | 0.001 | 0.000 | +0.0% | +0.0% | 0.10 |
| South Africa | A | Win the World Cup | NO | 100.0% | 0.001 | 1.000 | 0.000 | +0.0% | +0.0% | 65.50 |
| Ivory Coast | E | Reach Quarterfinals | NO | 88.3% | 0.140 | 0.880 | 0.003 | +0.3% | +0.0% | 0.65 |
| Brazil | C | Reach Final | YES | 17.4% | 0.165 | 0.170 | 0.004 | +0.4% | +0.0% | 0.09 |
| Switzerland | B | Win the World Cup | YES | 0.9% | 0.009 | 0.009 | 0.000 | +0.0% | +0.0% | 0.04 |
| Scotland | C | Win the World Cup | NO | 99.8% | 0.003 | 0.998 | 0.000 | +0.0% | +0.0% | 6.77 |
| Jordan | J | Win Group J | YES | 0.4% | 0.003 | 0.004 | 0.000 | +0.0% | +0.0% | 0.01 |
| Algeria | J | Win the World Cup | YES | 0.2% | 0.002 | 0.002 | 0.000 | +0.0% | -0.0% | 0.00 |
| South Africa | A | Win Group A | YES | 0.9% | 0.006 | 0.009 | 0.000 | +0.0% | -0.0% | 0.00 |
| Paraguay | D | Win the World Cup | NO | 99.9% | 0.002 | 0.999 | 0.000 | +0.0% | -0.0% | 0.00 |
| Switzerland | B | Reach Round of 16 | NO | 51.7% | 0.500 | 0.510 | 0.007 | +0.7% | -0.0% | 0.00 |
| Netherlands | F | Reach Semifinals | NO | 83.4% | 0.195 | 0.830 | 0.004 | +0.4% | -0.0% | 0.00 |
| Qatar | B | Reach Quarterfinals | NO | 98.5% | 0.028 | 0.985 | 0.000 | +0.0% | -0.0% | 0.00 |
| Scotland | C | Reach Round of 16 | YES | 28.6% | 0.260 | 0.280 | 0.006 | +0.6% | -0.0% | 0.00 |
| New Zealand | G | Reach Quarterfinals | NO | 98.3% | 0.018 | 0.983 | 0.001 | +0.0% | -0.0% | 0.00 |
| Netherlands | F | Win the World Cup | NO | 96.2% | 0.040 | 0.961 | 0.001 | +0.1% | -0.0% | 0.00 |
| Paraguay | D | Reach Final | NO | 99.6% | 0.013 | 0.996 | 0.000 | -0.0% | -0.0% | 0.00 |
| New Zealand | G | Reach Round of 16 | YES | 9.2% | 0.075 | 0.090 | 0.002 | +0.2% | -0.1% | 0.00 |
| Czech Republic | A | Win Group A | YES | 4.3% | 0.039 | 0.042 | 0.001 | +0.1% | -0.1% | 0.00 |
| Morocco | C | Reach R32 (knockout) | NO | 8.2% | 0.935 | 0.080 | 0.002 | +0.2% | -0.1% | 0.00 |
| New Zealand | G | Reach Semifinals | NO | 99.7% | 0.014 | 0.998 | 0.000 | -0.1% | -0.1% | 0.00 |
| Croatia | L | Win Group L | NO | 97.7% | 0.026 | 0.977 | 0.001 | -0.0% | -0.1% | 0.00 |
| Mexico | A | Win the World Cup | NO | 98.7% | 0.013 | 0.987 | 0.000 | -0.0% | -0.1% | 0.00 |
| Croatia | L | Reach Final | YES | 3.1% | 0.025 | 0.031 | 0.001 | +0.0% | -0.1% | 0.00 |
| Belgium | G | Reach Final | YES | 6.1% | 0.055 | 0.060 | 0.002 | +0.1% | -0.1% | 0.00 |
| Ecuador | E | Win Group E | YES | 5.0% | 0.045 | 0.050 | 0.001 | +0.0% | -0.1% | 0.00 |
| Jordan | J | Reach Quarterfinals | NO | 99.5% | 0.011 | 0.996 | 0.000 | -0.1% | -0.1% | 0.00 |
| Algeria | J | Reach Final | NO | 99.4% | 0.013 | 0.995 | 0.000 | -0.1% | -0.2% | 0.00 |
| Netherlands | F | Reach R32 (knockout) | NO | 8.1% | 0.925 | 0.080 | 0.002 | +0.1% | -0.2% | 0.00 |
| Senegal | I | Win Group I | NO | 98.5% | 0.017 | 0.986 | 0.000 | -0.1% | -0.2% | 0.00 |
| Czech Republic | A | Reach Semifinals | NO | 98.2% | 0.028 | 0.983 | 0.001 | -0.1% | -0.2% | 0.00 |
| Paraguay | D | Reach Semifinals | NO | 98.6% | 0.022 | 0.988 | 0.000 | -0.2% | -0.3% | 0.00 |
| Brazil | C | Reach Quarterfinals | YES | 48.5% | 0.470 | 0.480 | 0.007 | +0.5% | -0.3% | 0.00 |
| Curaçao | E | Reach Round of 16 | NO | 99.5% | 0.026 | 0.998 | 0.000 | -0.3% | -0.3% | 0.00 |
| England | L | Win Group L | YES | 91.9% | 0.915 | 0.920 | 0.002 | -0.1% | -0.3% | 0.00 |
| South Korea | A | Reach Final | YES | 1.9% | 0.017 | 0.022 | 0.001 | -0.3% | -0.4% | 0.00 |
| Haiti | C | Reach R32 (knockout) | NO | 95.4% | 0.046 | 0.957 | 0.001 | -0.3% | -0.4% | 0.00 |
| Egypt | G | Reach Round of 16 | YES | 34.2% | 0.320 | 0.340 | 0.007 | +0.2% | -0.4% | 0.00 |
| Austria | J | Reach Quarterfinals | YES | 16.0% | 0.140 | 0.160 | 0.004 | -0.0% | -0.4% | 0.00 |
| Egypt | G | Reach Final | NO | 99.3% | 0.009 | 0.998 | 0.000 | -0.5% | -0.5% | 0.00 |
| Croatia | L | Reach Quarterfinals | NO | 84.9% | 0.160 | 0.850 | 0.004 | -0.1% | -0.5% | 0.00 |
| Switzerland | B | Reach Semifinals | YES | 7.7% | 0.070 | 0.080 | 0.002 | -0.3% | -0.5% | 0.00 |
| Spain | H | Reach Semifinals | NO | 60.2% | 0.415 | 0.600 | 0.007 | +0.2% | -0.5% | 0.00 |
| Switzerland | B | Reach Final | YES | 2.7% | 0.026 | 0.032 | 0.001 | -0.5% | -0.6% | 0.00 |
| Iran | G | Reach Final | YES | 1.0% | 0.009 | 0.016 | 0.000 | -0.6% | -0.6% | 0.00 |
| Belgium | G | Reach Round of 16 | NO | 39.0% | 0.615 | 0.390 | 0.007 | +0.0% | -0.7% | 0.00 |
| Iraq | I | Reach Round of 16 | NO | 98.4% | 0.030 | 0.990 | 0.000 | -0.6% | -0.7% | 0.00 |
| Belgium | G | Reach Quarterfinals | YES | 33.9% | 0.335 | 0.340 | 0.007 | -0.1% | -0.7% | 0.00 |
| Egypt | G | Reach Quarterfinals | YES | 10.5% | 0.100 | 0.110 | 0.003 | -0.5% | -0.8% | 0.00 |
| Tunisia | F | Reach Round of 16 | NO | 98.2% | 0.033 | 0.990 | 0.000 | -0.8% | -0.8% | 0.00 |
| Ivory Coast | E | Reach Round of 16 | YES | 39.9% | 0.370 | 0.400 | 0.007 | -0.1% | -0.8% | 0.00 |
| Bosnia and Herzegovina | B | Win Group B | NO | 84.6% | 0.155 | 0.850 | 0.004 | -0.4% | -0.8% | 0.00 |
| South Africa | A | Reach Quarterfinals | NO | 98.4% | 0.019 | 0.992 | 0.000 | -0.8% | -0.8% | 0.00 |
| Uzbekistan | K | Reach Quarterfinals | YES | 1.7% | 0.015 | 0.025 | 0.001 | -0.8% | -0.9% | 0.00 |
| Spain | H | Reach Round of 16 | YES | 70.7% | 0.695 | 0.710 | 0.006 | -0.3% | -0.9% | 0.00 |
| Jordan | J | Reach R32 (knockout) | YES | 9.3% | 0.090 | 0.100 | 0.003 | -0.7% | -0.9% | 0.00 |
| Argentina | J | Reach Round of 16 | YES | 74.6% | 0.745 | 0.750 | 0.006 | -0.4% | -0.9% | 0.00 |
| Canada | B | Reach Semifinals | NO | 97.2% | 0.030 | 0.982 | 0.001 | -0.9% | -1.0% | 0.00 |
| Bosnia and Herzegovina | B | Reach Round of 16 | YES | 19.4% | 0.190 | 0.200 | 0.005 | -0.6% | -1.1% | 0.00 |
| Belgium | G | Reach Semifinals | YES | 14.3% | 0.135 | 0.150 | 0.004 | -0.7% | -1.1% | 0.00 |
| Croatia | L | Reach Semifinals | YES | 8.0% | 0.079 | 0.088 | 0.002 | -0.8% | -1.1% | 0.00 |
| Mexico | A | Reach Semifinals | NO | 90.1% | 0.105 | 0.910 | 0.002 | -0.9% | -1.1% | 0.00 |
| South Korea | A | Reach Semifinals | YES | 5.7% | 0.053 | 0.067 | 0.002 | -1.0% | -1.2% | 0.00 |
| Cape Verde | H | Reach Round of 16 | YES | 9.3% | 0.070 | 0.104 | 0.003 | -1.1% | -1.4% | 0.00 |
| Uruguay | H | Reach Semifinals | NO | 91.8% | 0.085 | 0.930 | 0.002 | -1.2% | -1.4% | 0.00 |
| Jordan | J | Reach Round of 16 | NO | 97.6% | 0.146 | 0.990 | 0.000 | -1.4% | -1.5% | 0.00 |
| Egypt | G | Reach Semifinals | NO | 97.2% | 0.029 | 0.987 | 0.000 | -1.4% | -1.5% | 0.00 |
| Algeria | J | Reach Quarterfinals | YES | 6.7% | 0.060 | 0.080 | 0.002 | -1.3% | -1.5% | 0.00 |
| Algeria | J | Reach Semifinals | NO | 97.8% | 0.024 | 0.993 | 0.000 | -1.5% | -1.5% | 0.00 |
| Scotland | C | Reach Semifinals | NO | 97.1% | 0.033 | 0.987 | 0.000 | -1.6% | -1.7% | 0.00 |
| Saudi Arabia | H | Reach Round of 16 | NO | 93.5% | 0.085 | 0.950 | 0.001 | -1.5% | -1.7% | 0.00 |
| Uruguay | H | Reach Round of 16 | NO | 69.7% | 0.310 | 0.710 | 0.006 | -1.3% | -1.9% | 0.00 |
| Croatia | L | Reach Round of 16 | NO | 63.2% | 0.430 | 0.650 | 0.007 | -1.8% | -2.5% | 0.00 |
| Mexico | A | Reach Round of 16 | YES | 60.1% | 0.590 | 0.620 | 0.007 | -1.9% | -2.6% | 0.00 |
| Algeria | J | Reach Round of 16 | YES | 18.5% | 0.180 | 0.210 | 0.005 | -2.5% | -3.0% | 0.00 |

## Simulated stage probabilities (all 48 teams)

Sorted by P(win). Group letter in parentheses.

| Team | Grp | Win Grp | Reach R32 | R16 | QF | SF | Final | Win |
|------|-----|---------|-----------|-----|----|----|-------|-----|
| Argentina | J | 86.5% | 99.8% | 74.6% | 60.2% | 44.9% | 28.6% | 17.8% |
| Spain | H | 75.0% | 95.8% | 70.7% | 50.9% | 39.8% | 26.0% | 16.1% |
| France | I | 79.9% | 99.6% | 75.5% | 49.7% | 34.1% | 20.7% | 11.6% |
| England | L | 91.9% | 99.6% | 76.2% | 52.6% | 31.4% | 17.9% | 9.8% |
| Brazil | C | 65.5% | 97.5% | 68.8% | 48.5% | 29.9% | 17.4% | 9.7% |
| Colombia | K | 64.6% | 99.2% | 71.1% | 45.6% | 23.2% | 11.7% | 5.8% |
| Germany | E | 68.2% | 99.8% | 66.3% | 33.7% | 19.7% | 9.6% | 4.5% |
| Portugal | K | 29.8% | 85.6% | 57.8% | 32.2% | 17.6% | 8.6% | 4.1% |
| Netherlands | F | 51.3% | 91.9% | 49.4% | 32.5% | 16.6% | 8.2% | 3.8% |
| Belgium | G | 60.0% | 90.2% | 61.0% | 33.9% | 14.3% | 6.1% | 2.4% |
| Morocco | C | 18.7% | 91.8% | 45.7% | 24.6% | 10.5% | 4.5% | 1.6% |
| Norway | I | 18.2% | 97.7% | 49.2% | 21.5% | 9.4% | 3.7% | 1.3% |
| Mexico | A | 67.1% | 98.9% | 60.1% | 25.9% | 9.9% | 3.8% | 1.3% |
| Uruguay | H | 15.4% | 75.2% | 30.3% | 16.3% | 8.2% | 3.4% | 1.3% |
| Croatia | L | 2.3% | 81.0% | 36.8% | 15.1% | 8.0% | 3.1% | 1.1% |
| Ecuador | E | 5.0% | 76.5% | 38.5% | 17.6% | 7.0% | 2.9% | 1.1% |
| Switzerland | B | 33.1% | 82.3% | 48.3% | 22.3% | 7.7% | 2.7% | 0.9% |
| Japan | F | 21.7% | 84.6% | 33.8% | 17.6% | 7.4% | 2.8% | 0.9% |
| Austria | J | 12.2% | 94.1% | 34.9% | 16.0% | 6.5% | 2.2% | 0.7% |
| Australia | D | 38.8% | 94.7% | 53.7% | 20.6% | 7.2% | 2.4% | 0.6% |
| United States | D | 52.3% | 96.7% | 51.9% | 19.5% | 5.9% | 1.9% | 0.5% |
| South Korea | A | 27.7% | 94.8% | 51.0% | 19.2% | 5.7% | 1.9% | 0.5% |
| Iran | G | 15.8% | 59.9% | 30.6% | 10.9% | 3.6% | 1.0% | 0.3% |
| Turkey | D | 5.2% | 42.4% | 19.1% | 6.4% | 2.7% | 0.9% | 0.3% |
| Ivory Coast | E | 26.7% | 96.4% | 39.9% | 11.7% | 4.0% | 1.1% | 0.3% |
| Sweden | F | 25.9% | 96.0% | 29.2% | 12.2% | 4.0% | 1.2% | 0.3% |
| Senegal | I | 1.5% | 58.3% | 23.0% | 8.8% | 2.9% | 0.9% | 0.3% |
| Algeria | J | 0.9% | 54.4% | 18.5% | 6.7% | 2.2% | 0.6% | 0.2% |
| Scotland | C | 15.6% | 83.0% | 28.6% | 10.3% | 2.9% | 0.8% | 0.2% |
| Canada | B | 43.0% | 81.2% | 36.2% | 11.8% | 2.8% | 0.7% | 0.2% |
| Egypt | G | 19.9% | 75.4% | 34.2% | 10.5% | 2.8% | 0.7% | 0.1% |
| Czech Republic | A | 4.3% | 47.7% | 19.4% | 7.4% | 1.8% | 0.5% | 0.1% |
| Paraguay | D | 3.7% | 36.3% | 14.6% | 4.4% | 1.4% | 0.4% | 0.1% |
| DR Congo | K | 4.8% | 47.9% | 11.6% | 2.6% | 0.6% | 0.1% | 0.0% |
| Ghana | L | 4.9% | 67.2% | 12.8% | 3.2% | 0.7% | 0.1% | 0.0% |
| South Africa | A | 0.9% | 24.9% | 7.3% | 1.6% | 0.2% | 0.1% | 0.0% |
| Saudi Arabia | H | 4.0% | 39.1% | 6.5% | 1.6% | 0.3% | 0.1% | 0.0% |
| Bosnia and Herzegovina | B | 15.4% | 63.1% | 19.4% | 4.7% | 0.8% | 0.1% | 0.0% |
| Uzbekistan | K | 0.9% | 26.8% | 6.6% | 1.7% | 0.5% | 0.1% | 0.0% |
| New Zealand | G | 4.3% | 32.2% | 9.2% | 1.7% | 0.3% | 0.0% | 0.0% |
| Cape Verde | H | 5.6% | 50.3% | 9.3% | 2.0% | 0.3% | 0.1% | 0.0% |
| Qatar | B | 8.5% | 39.9% | 8.9% | 1.5% | 0.2% | 0.0% | 0.0% |
| Jordan | J | 0.4% | 9.3% | 2.4% | 0.5% | 0.1% | 0.0% | 0.0% |
| Iraq | I | 0.4% | 8.8% | 1.6% | 0.4% | 0.0% | 0.0% | 0.0% |
| Haiti | C | 0.2% | 4.6% | 0.6% | 0.1% | 0.0% | 0.0% | 0.0% |
| Tunisia | F | 1.1% | 10.5% | 1.8% | 0.4% | 0.1% | 0.0% | 0.0% |
| Curaçao | E | 0.1% | 4.9% | 0.5% | 0.0% | 0.0% | 0.0% | 0.0% |
| Panama | L | 0.9% | 12.1% | 2.5% | 0.7% | 0.1% | 0.0% | 0.0% |

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
