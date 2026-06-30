#!/usr/bin/env python3
"""Polymarket "Perfect" bracket optimiser — 2026 WC knockouts (31 games).

Reads the fitted advancement model (site/advancement_data.json: per-team marginal
P(reach round)) and the real R32 fixtures mapped onto the FIFA-2026 knockout tree
(src/wca/sim/tournament2026.py KNOCKOUT_FEED), then emits:

  * the model's joint-mode "chalk" bracket — the entry that MAXIMISES P(perfect),
    i.e. the favourite at every one of the 31 games;
  * P(perfect bracket) along that path (upper bound — see note);
  * expected correct picks E[#correct] (the $100k "best bracket" benchmark);
  * per-game pick probabilities and the spots where the model is most lopsided
    (lowest-variance locks) vs most coin-flip (where differentiation is cheap).

Reach-marginals are exact for R32 (fixed opponent) so P(R16)=P(win your R32 tie).
For R16+ the per-game conditional P(advance|reached)=P(reach n+1)/P(reach n) is
the model's *average-opponent* rate; on the all-favourites path the real opponent
is itself a favourite, so true conditional <= this, making P(perfect) an UPPER
bound and E[#correct] a slight over-estimate. Expected-correct *per slot* uses the
team's marginal P(reach that round), which IS exact.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADV = os.path.join(ROOT, "site", "advancement_data.json")

# R32 ties (match_no -> the two real teams), derived by mapping the CSV knockout
# fixtures onto R32_TIES group-letter slots in tournament2026.py.
R32 = {
    73: ("South Africa", "Canada"),
    74: ("Germany", "Paraguay"),
    75: ("Netherlands", "Morocco"),
    76: ("Brazil", "Japan"),
    77: ("France", "Sweden"),
    78: ("Ivory Coast", "Norway"),
    79: ("Mexico", "Ecuador"),
    80: ("England", "DR Congo"),
    81: ("United States", "Bosnia and Herzegovina"),
    82: ("Belgium", "Senegal"),
    83: ("Portugal", "Croatia"),
    84: ("Spain", "Austria"),
    85: ("Switzerland", "Algeria"),
    86: ("Argentina", "Cape Verde"),
    87: ("Colombia", "Ghana"),
    88: ("Australia", "Egypt"),
}
# winner(match) -> (source_a, source_b)
FEED = {
    89: (74, 77), 90: (73, 75), 91: (76, 78), 92: (79, 80),
    93: (83, 84), 94: (81, 82), 95: (86, 88), 96: (85, 87),
    97: (89, 90), 98: (93, 94), 99: (91, 92), 100: (95, 96),
    101: (97, 98), 102: (99, 100), 104: (101, 102),
}
# the marginal used to rank/the round a match's winner *reaches*
ROUND_OF = {**{m: "R16" for m in range(73, 89)},
            **{m: "QF" for m in range(89, 97)},
            **{m: "SF" for m in range(97, 101)},
            101: "Final", 102: "Final", 104: "win"}
PREV = {"R16": "R32", "QF": "R16", "SF": "QF", "Final": "SF", "win": "Final"}


def main():
    data = json.load(open(ADV))
    M = {t["team"]: t["model"] for t in data["teams"]}
    PM = {t["team"]: t.get("pm", {}) for t in data["teams"]}

    winner = {}          # match_no -> picked team
    pick_reach = {}      # match_no -> picked team's marginal P(reach round)
    cond = {}            # match_no -> conditional pick prob along chalk path

    def participants(m):
        if m in R32:
            return R32[m]
        a, b = FEED[m]
        return winner[a], winner[b]

    for m in list(R32) + list(FEED):       # 73..88 then 89..104 (tree order ok)
        rnd = ROUND_OF[m]
        a, b = participants(m)
        ra, rb = M[a][rnd], M[b][rnd]      # marginal P(reach this round's far side)
        fav, fr, lr = (a, ra, rb) if ra >= rb else (b, rb, ra)
        winner[m] = fav
        pick_reach[m] = fr
        prev_reach = M[fav][PREV[rnd]] if rnd != "R16" else 1.0
        cond[m] = fr / prev_reach if prev_reach > 0 else 0.0

    rounds = [("R32", range(73, 89)), ("R16", range(89, 97)),
              ("QF", range(97, 101)), ("SF", (101, 102)), ("Final", (104,))]

    print("=" * 78)
    print("MODEL-OPTIMAL 'PERFECT' BRACKET — 2026 WC  (model %s)" % data["meta"]["model_generated"])
    print("=" * 78)
    p_perfect = 1.0
    exp_correct = 0.0
    for name, ms in rounds:
        print(f"\n— {name} —")
        for m in ms:
            a, b = participants(m)
            opp = b if winner[m] == a else a
            print(f"  [{m}] {winner[m]:<22} beats {opp:<22}  "
                  f"win={cond[m]*100:5.1f}%  reach={pick_reach[m]*100:5.1f}%")
            p_perfect *= cond[m]
            exp_correct += pick_reach[m]

    print("\n" + "=" * 78)
    print(f"CHAMPION pick: {winner[104]}")
    print(f"P(perfect 31/31)  ~= {p_perfect:.3e}   (~1 in {1/p_perfect:,.0f})  [upper bound]")
    print(f"E[correct picks]   = {exp_correct:.2f} / 31")
    print("=" * 78)

    # locks (lowest variance) and coin-flips (cheapest to differentiate)
    flips = sorted(((cond[m], m) for m in cond), key=lambda x: x[0])
    print("\nMost coin-flip games (cheapest differentiation):")
    for c, m in flips[:6]:
        a, b = participants(m)
        print(f"  [{m}] {ROUND_OF[m]:>5}: {a} / {b}  -> {winner[m]} only {c*100:.1f}%")
    print("\nSafest locks:")
    for c, m in sorted(flips, reverse=True)[:6]:
        print(f"  [{m}] {ROUND_OF[m]:>5}: {winner[m]} {c*100:.1f}%")

    # where the model most disagrees with the Polymarket price (edge to lean on)
    print("\nBiggest model>market edges on the chalk picks (lean / value):")
    edges = []
    for m, fav in winner.items():
        rnd = ROUND_OF[m]
        ea = PM.get(fav, {}).get(rnd, {}).get("edge_adj")
        if ea is not None:
            edges.append((ea, m, fav, rnd))
    for ea, m, fav, rnd in sorted(edges, reverse=True)[:8]:
        print(f"  {fav:<14} {rnd:<5} edge {ea*100:+5.1f} pts (match {m})")


if __name__ == "__main__":
    main()
