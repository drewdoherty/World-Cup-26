import json
from pathlib import Path

adv = json.loads(Path("data/advancement_current_vs_pretournament.json").read_text())
P = {r["team"]: r for r in adv}

# R32 ties still to play (task-provided). Decided ones seeded separately.
r32_to_play = [
    ("France","Sweden"),("Ivory Coast","Norway"),("Mexico","Ecuador"),
    ("England","DR Congo"),("United States","Bosnia and Herzegovina"),
    ("Belgium","Senegal"),("Portugal","Croatia"),("Spain","Austria"),
    ("Switzerland","Algeria"),("Argentina","Cape Verde"),("Colombia","Ghana"),
    ("Australia","Egypt"),
]
r32_decided = {  # winner
    ("Canada","South Africa"):"Canada",
    ("Paraguay","Germany"):"Paraguay",
    ("Morocco","Netherlands"):"Morocco",
    ("Brazil","Japan"):"Brazil",
}

def cond_win_tie(a,b):
    # model-implied conditional P(a beats b) using P(R16)/P(R32). Both at R32 now.
    # P(reach R16)=P(win this tie). Normalize between the two.
    pa,pb = P[a]["P(R16)"], P[b]["P(R16)"]
    return pa/(pa+pb), pb/(pa+pb)

print("=== R32 chalk picks (model conditional, both teams alive) ===")
chalk_r32=[]
for a,b in r32_to_play:
    wa,wb = cond_win_tie(a,b)
    fav,fp = (a,wa) if wa>=wb else (b,wb)
    chalk_r32.append(fav)
    print(f"{a:24} vs {b:24} -> {fav:20} P~{fp:.0%}")

print("\n=== Market winner-odds (defirate snapshot 2026-06-30, current) ===")
mkt = {"France":.273,"Argentina":.196,"Spain":.113,"England":.102,"Brazil":.070,
       "Portugal":.061,"Morocco":.041,"United States":.028,"Colombia":.028,"Norway":.021}
for t,p in mkt.items():
    print(f"{t:18} {p:.1%}")

print("\n=== Deep-run favorites by model P(SF)/P(Final)/P(win) ===")
alive = [t for t in P if P[t]["P(R32)"]==1.0 and t not in ("Germany","Netherlands","Japan","South Africa")]
for t in sorted(alive,key=lambda x:-P[x]["P(win)"])[:14]:
    r=P[t]
    print(f"{t:22} QF {r['P(QF)']:.0%}  SF {r['P(SF)']:.0%}  F {r['P(Final)']:.0%}  Win {r['P(win)']:.1%}")
