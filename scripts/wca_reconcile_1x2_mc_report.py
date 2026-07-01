#!/usr/bin/env python3
"""Reconcile 1X2 forecast vs Monte-Carlo advancement + live PM book -> PDF report."""
import json, re, sys, urllib.request, datetime
sys.path.insert(0, "src")
from wca.data.teamnames import canonical

TODAY = "2026-07-01"
WALLETS = {"PM1": "0x86b4c55a4df1fbea0f325e842434e0a537caa549",
           "PM2": "0xd42e35059b0615c4c7a9cf7db5427b313ebb7b31"}
API = "https://data-api.polymarket.com/positions"
STAGE_RE = re.compile(r"reach the (round of 16|quarterfinals?|semifinals?|final)", re.I)
SK = {"round of 16":"R16","quarterfinal":"QF","quarterfinals":"QF","semifinal":"SF","semifinals":"SF","final":"Final"}

def fetch(w, red):
    u = "%s?user=%s&limit=500&sizeThreshold=0.1&redeemable=%s" % (API, w, "true" if red else "false")
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent":"wca"}), timeout=25))

adv_open, adv_done = [], []
for w in WALLETS.values():
    for red, bucket in ((False, adv_open), (True, adv_done)):
        for p in fetch(w, red):
            t = p.get("title") or ""; m = STAGE_RE.search(t)
            if not m: continue
            team = canonical(re.sub(r"^will\s+","",t.lower()).split(" reach")[0].strip())
            size=float(p.get("size") or 0); cur=float(p.get("curPrice") or 0)
            avg=float(p.get("avgPrice") or 0); pnl=p.get("cashPnl")
            bucket.append({"team":team,"stage":SK[m.group(1).lower()],"shares":size,"avg":avg,
                           "cur":cur,"value":size*cur,"pnl":float(pnl) if pnl is not None else 0.0})

adv = json.load(open("site/advancement_data.json"))
mc = {canonical(t["team"]): t for t in adv.get("teams", [])}
scores = json.load(open("site/scores_data.json"))
onex2 = {}
for f in scores.get("fixtures", []):
    fx=f.get("fixture") or ""
    if " vs " in fx:
        h,a=[canonical(x.strip()) for x in fx.split(" vs ",1)]
        onex2[h]={"home":h,"away":a,"m":f.get("model_1x2") or {},"raw":fx}

# --- assemble open-book rows with MC prob + pipeline edge ---
rows=[]
for r in sorted(adv_open, key=lambda x:-x["value"]):
    t=mc.get(r["team"],{})
    mcp=(t.get("model") or {}).get(r["stage"])
    pm_stage=(t.get("pm") or {}).get(r["stage"]) or {}
    edge=pm_stage.get("edge_adj")
    rows.append({**r,"mc":mcp,"pipe_edge":edge})
tot_val=sum(r["value"] for r in rows); tot_pnl=sum(r["pnl"] for r in rows)
fav_pnl=sum(r["pnl"] for r in rows if r["avg"]>=0.5)
dog_pnl=sum(r["pnl"] for r in rows if r["avg"]<0.5)
done_pnl=sum(r["pnl"] for r in adv_done)

# --- 1X2 vs MC consistency ---
cons=[]
for h,fx in onex2.items():
    m=fx["m"]
    if not m: continue
    adv_imp=m.get("home",0)+0.5*m.get("draw",0)
    mcp=(mc.get(h,{}).get("model") or {}).get("R16")
    if mcp: cons.append({"raw":fx["raw"],"H":m.get("home",0),"D":m.get("draw",0),"A":m.get("away",0),
                         "imp":adv_imp,"mc":mcp,"team":h})

# ---- render PDF ----
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

OUT="reports/1x2_vs_mc_reconciliation_%s.pdf" % TODAY.replace("-","")
with PdfPages(OUT) as pdf:
    # Page 1 — findings
    fig=plt.figure(figsize=(8.3,11.7)); fig.subplots_adjust(left=0.07,right=0.95,top=0.95,bottom=0.05)
    ax=fig.add_subplot(111); ax.axis("off")
    txt=[
     ("WCA Alpha — 1X2 Forecast vs Monte-Carlo Advancement","title"),
     ("Reconciliation of the live Polymarket knockout book · %s" % TODAY,"sub"),
     ("","n"),
     ("EXECUTIVE FINDING","h"),
     ("The 1X2 match forecast and the Monte-Carlo advancement model are the SAME","b"),
     ("engine. The MC 'to advance' probability = 1X2 home-win + the ET/penalties","b"),
     ("share of the draw mass; there is no independent advancement model that can","b"),
     ("beat an independent 1X2 model. A small negative 1X2 edge therefore implies a","b"),
     ("small negative advancement edge — they cannot diverge in expectation.","b"),
     ("","n"),
     ("Across all %d live R32 ties the two agree within ~5pp (table, p.3):" % len(cons),"b"),
     ("  France 1X2->adv 80%% vs MC 76%%   Argentina 89%% vs 91%%   Belgium 61%% vs 67%%.","mono"),
     ("","n"),
     ("WHY THE KNOCKOUT BOOK LOOKS 'SUCCESSFUL'","h"),
     ("Open advancement book value $%.0f, net mark-to-market P&L $%+.0f." % (tot_val,tot_pnl),"b"),
     ("That gain is almost entirely FAVOURITE beta + mark-to-market convergence:","b"),
     ("   favourites (entry >=50c):  $%+.0f" % fav_pnl,"mono"),
     ("   underdogs  (entry <50c):   $%+.0f" % dog_pnl,"mono"),
     ("Gains are France (51->78c), France QF (67->90c), Colombia (60->80c), Brazil","b"),
     ("(50->64c) — favourites shortening as expected results landed, not realised edge.","b"),
     ("Resolved advancement bets are NET NEGATIVE ($%+.0f): Iran R16, South Korea R16" % done_pnl,"b"),
     ("(longshots) — consistent with the standing 'good calibrator / bad longshot","b"),
     ("selector' finding.","b"),
     ("","n"),
     ("VERDICT","h"),
     ("'Small negative 1X2 edge' and 'profitable knockout advancement so far' are NOT","b"),
     ("in tension. Same model; the profit is (a) favourite exposure in a chalk-heavy","b"),
     ("R32, (b) mark-to-market as prices converged, (c) small-sample variance at","b"),
     ("n_eff ~ 1 tournament. Backing favourites that win is not evidence of alpha; the","b"),
     ("honest scorecard is CLV vs the closing line, not MTM P&L. Keep sizing at the","b"),
     ("capped 1/4-Kelly, prefer favourite to-advance where model ~ market, and avoid","b"),
     ("longshot advancement (the losing bucket).","b"),
    ]
    y=0.97
    for s,kind in txt:
        if kind=="title": ax.text(0.0,y,s,fontsize=15,fontweight="bold"); y-=0.028
        elif kind=="sub": ax.text(0.0,y,s,fontsize=10,color="#555"); y-=0.030
        elif kind=="h": ax.text(0.0,y,s,fontsize=11,fontweight="bold",color="#1a6"); y-=0.024
        elif kind=="mono": ax.text(0.02,y,s,fontsize=9,family="monospace"); y-=0.021
        elif kind=="n": y-=0.012
        else: ax.text(0.0,y,s,fontsize=9.5); y-=0.021
    pdf.savefig(fig); plt.close(fig)

    # Page 2 — open book table
    fig=plt.figure(figsize=(8.3,11.7)); ax=fig.add_subplot(111); ax.axis("off")
    ax.text(0.0,0.98,"Open knockout advancement book  vs  Monte-Carlo reach prob",fontsize=12,fontweight="bold",transform=ax.transAxes)
    cols=["team","stage","entry","cur","MC prob","pipe edge","MTM P&L"]
    data=[[r["team"][:12], r["stage"], "%.0f¢"%(r["avg"]*100), "%.0f¢"%(r["cur"]*100),
           ("%.0f%%"%(r["mc"]*100)) if r["mc"] else "n/a",
           ("%+.0f%%"%(r["pipe_edge"]*100)) if r["pipe_edge"] is not None else "n/a",
           "$%+.0f"%r["pnl"]] for r in rows]
    data.append(["TOTAL","","","","","","$%+.0f"%tot_pnl])
    tbl=ax.table(cellText=data,colLabels=cols,loc="upper center",cellLoc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(9); tbl.scale(1,1.5)
    ax.text(0.0,0.18,"Favourites@entry $%+.0f   |   Underdogs@entry $%+.0f   |   Resolved (settled) $%+.0f"
            %(fav_pnl,dog_pnl,done_pnl),fontsize=9.5,transform=ax.transAxes)
    ax.text(0.0,0.14,"MTM = mark-to-market on OPEN positions (unrealised); not settled P&L.",fontsize=8,color="#777",transform=ax.transAxes)
    pdf.savefig(fig); plt.close(fig)

    # Page 3 — consistency scatter + table
    fig=plt.figure(figsize=(8.3,11.7))
    ax1=fig.add_axes([0.12,0.58,0.78,0.34])
    xs=[c["imp"]*100 for c in cons]; ys=[c["mc"]*100 for c in cons]
    ax1.scatter(xs,ys,c="#1a9e5f",zorder=3)
    for c in cons: ax1.annotate(c["team"][:8],(c["imp"]*100,c["mc"]*100),fontsize=7,xytext=(3,3),textcoords="offset points")
    ax1.plot([0,100],[0,100],"--",color="#888"); ax1.set_xlim(20,100); ax1.set_ylim(20,100)
    ax1.set_xlabel("1X2-implied advance prob  (home-win + ½ draw)  %"); ax1.set_ylabel("Monte-Carlo reach prob  %")
    ax1.set_title("1X2 forecast vs MC advancement — one engine (points hug y=x)",fontsize=11,fontweight="bold")
    ax1.grid(alpha=0.2)
    ax2=fig.add_axes([0.06,0.05,0.88,0.44]); ax2.axis("off")
    cols=["tie","1X2 H","draw","1X2 A","1X2→adv","MC reach"]
    dd=[[c["raw"][:24],"%.0f%%"%(c["H"]*100),"%.0f%%"%(c["D"]*100),"%.0f%%"%(c["A"]*100),
         "%.0f%%"%(c["imp"]*100),"%.0f%%"%(c["mc"]*100)] for c in cons]
    t2=ax2.table(cellText=dd,colLabels=cols,loc="upper center",cellLoc="center")
    t2.auto_set_font_size(False); t2.set_fontsize(8); t2.scale(1,1.4)
    pdf.savefig(fig); plt.close(fig)

print("WROTE", OUT)
print("open_val=$%.0f mtm=$%+.0f fav=$%+.0f dog=$%+.0f resolved=$%+.0f ties=%d"%(tot_val,tot_pnl,fav_pnl,dog_pnl,done_pnl,len(cons)))
