#!/usr/bin/env python
"""Generate the offline WCA exposure 'terminal' — a self-contained HTML page.

Reads the ledger and emits ``site/terminal.html`` with the bet data EMBEDDED
(so it opens offline via file:// with perfect accuracy, no server/fetch needed).
Currency is kept separate per pool (never summed across £/$), matching the
portfolio convention. Regenerate after any ledger correction:

    PYTHONPATH=src python scripts/wca_terminal.py
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3

DB = os.environ.get("WCA_DB", "data/wca.db")
OUT = os.environ.get("WCA_TERMINAL_OUT", "site/terminal.html")

# platform -> currency symbol (GBP sportsbooks vs USD prediction markets)
_CCY = {
    "betfair_sportsbook": "£", "betfair_ex_uk": "£", "bet365": "£", "smarkets": "£",
    "paddypower": "£", "betfred": "£", "virginbet": "£", "virgin": "£",
    "polymarket": "$", "kalshi": "$",
}


def _ccy(platform: str) -> str:
    return _CCY.get((platform or "").lower(), "£")


def _is_free(notes: str) -> bool:
    """A free-bet STAKE risks no cash. The reliable marker is SNR (stake not
    returned). Notes that merely mention a free-bet *reward* (e.g. "£1 free bet
    per goal") or a "bet-£10-get-£10" qualifier are REAL cash and must not match.
    """
    n = (notes or "").lower()
    return "snr" in n or "stake not returned" in n


def load():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(bets)")}
    acc = "account" if "account" in cols else "'1' AS account"
    src = "source" if "source" in cols else "'model' AS source"
    rows = con.execute(
        f"SELECT id, ts_utc, match_desc, market, selection, platform, decimal_odds, "
        f"stake, model_prob, ev, status, settled_pl, settled_ts, clv, notes, {acc}, {src} "
        f"FROM bets ORDER BY id"
    ).fetchall()
    con.close()
    bets = []
    for r in rows:
        sym = _ccy(r["platform"])
        stake = float(r["stake"] or 0.0)
        odds = float(r["decimal_odds"] or 0.0)
        free = _is_free(r["notes"])
        bets.append({
            "id": r["id"], "ts": (r["ts_utc"] or "")[:16],
            "match": r["match_desc"] or "", "market": r["market"] or "",
            "sel": r["selection"] or "", "platform": r["platform"] or "",
            "acct": str(r["account"]), "ccy": sym, "odds": round(odds, 2),
            "stake": round(stake, 2),
            "max_win": round(stake * (odds - 1.0), 2),
            "max_loss": 0.0 if free else round(stake, 2),
            "status": r["status"],
            "pl": round(float(r["settled_pl"] or 0.0), 2),
            "settled_ts": (r["settled_ts"] or "")[:16],
            "clv": (None if r["clv"] is None else round(float(r["clv"]) * 100, 2)),
            "model_prob": (None if r["model_prob"] is None else round(float(r["model_prob"]) * 100, 1)),
            "source": r["source"], "free": free,
            "notes": r["notes"] or "",
        })
    return bets


def summarise(bets):
    """Per-currency summary — never sum across currencies."""
    out = {}
    for b in bets:
        s = out.setdefault(b["ccy"], {
            "open_n": 0, "open_at_risk": 0.0, "open_max_win": 0.0,
            "won": 0, "lost": 0, "void": 0,
            "settled_staked": 0.0, "settled_pl": 0.0,
        })
        if b["status"] == "open":
            s["open_n"] += 1
            s["open_at_risk"] += b["max_loss"]
            s["open_max_win"] += b["max_win"]
        elif b["status"] in ("won", "lost", "void", "cashed"):
            s[b["status"] if b["status"] in ("won", "lost", "void") else "won"] += 1
            s["settled_staked"] += b["stake"]
            s["settled_pl"] += b["pl"]
    for s in out.values():
        s["roi"] = (s["settled_pl"] / s["settled_staked"] * 100.0) if s["settled_staked"] else None
        for k in ("open_at_risk", "open_max_win", "settled_staked", "settled_pl"):
            s[k] = round(s[k], 2)
        if s["roi"] is not None:
            s["roi"] = round(s["roi"], 2)
    return out


def render(bets, summary, gen):
    data = json.dumps({"bets": bets, "summary": summary, "generated": gen}, separators=(",", ":"))
    return _HTML.replace("__DATA__", data)


_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WCA TERMINAL — exposure</title>
<style>
:root{--bg:#0a0e12;--panel:#10161d;--line:#1d2731;--txt:#cfe3d0;--dim:#6b8290;--grn:#33d17a;--red:#ff5d5d;--amb:#ffb454;--cyan:#4fd6e0;--hd:#0d1419}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:13px/1.45 ui-monospace,"SF Mono",Menlo,Consolas,monospace}
.wrap{max-width:1200px;margin:0 auto;padding:18px}
h1{font-size:15px;letter-spacing:2px;margin:0 0 2px;color:var(--cyan)}
.sub{color:var(--dim);font-size:11px;margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.card h2{font-size:11px;letter-spacing:1px;color:var(--amb);margin:0 0 8px;text-transform:uppercase}
.row{display:flex;justify-content:space-between;padding:2px 0}
.row .k{color:var(--dim)}
.pos{color:var(--grn)}.neg{color:var(--red)}.muted{color:var(--dim)}
.tabs{display:flex;gap:6px;margin:18px 0 8px}
.tab{background:var(--hd);border:1px solid var(--line);color:var(--dim);padding:6px 14px;border-radius:5px;cursor:pointer;font-size:12px}
.tab.on{color:var(--cyan);border-color:var(--cyan)}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:600;font-size:10px;letter-spacing:.5px;text-transform:uppercase;cursor:pointer;user-select:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:hover td{background:#0d141b}
.sel{white-space:normal;max-width:340px;color:#e8f3e8}
.match{color:var(--dim)}
.badge{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;border:1px solid}
.b-won{color:var(--grn);border-color:#1c5e3a}.b-lost{color:var(--red);border-color:#5e2222}
.b-void{color:var(--dim);border-color:#33414c}.b-open{color:var(--amb);border-color:#5e4a1c}
.ven{color:var(--cyan)}
.foot{color:var(--dim);font-size:10px;margin-top:20px;border-top:1px solid var(--line);padding-top:10px}
.flag{background:#1a1206;border:1px solid #5e4a1c;color:var(--amb);padding:8px 12px;border-radius:6px;margin-bottom:14px;font-size:12px}
</style></head>
<body><div class="wrap">
<h1>▌ WCA TERMINAL</h1>
<div class="sub">offline exposure &amp; P&amp;L · snapshot <span id="gen"></span> · currencies never summed</div>
<div id="cards" class="cards"></div>
<div class="tabs"><div class="tab on" data-v="open">OPEN EXPOSURE</div><div class="tab" data-v="closed">CLOSED / SETTLED</div></div>
<div id="table"></div>
<div class="foot">Read-only snapshot embedded at generation. Regenerate after ledger edits:
<code>PYTHONPATH=src python scripts/wca_terminal.py</code></div>
</div>
<script>
const D = __DATA__;
document.getElementById('gen').textContent = D.generated;
const money=(s,v)=>{const c=v<0?'neg':(v>0?'pos':'muted');return `<span class="${c}">${s}${v>=0?'':'-'}${Math.abs(v).toFixed(2)}</span>`};
function cards(){
  const order=['£','$']; let h='';
  for(const sym of order){ const s=D.summary[sym]; if(!s) continue;
    h+=`<div class="card"><h2>${sym==='£'?'GBP — sportsbook':'USD — prediction mkts'} (${sym})</h2>
      <div class="row"><span class="k">open bets</span><span>${s.open_n}</span></div>
      <div class="row"><span class="k">at risk</span><span class="neg">${sym}${s.open_at_risk.toFixed(2)}</span></div>
      <div class="row"><span class="k">max win (open)</span><span class="pos">${sym}${s.open_max_win.toFixed(2)}</span></div>
      <div class="row"><span class="k">settled W/L/V</span><span>${s.won}/${s.lost}/${s.void}</span></div>
      <div class="row"><span class="k">settled P&amp;L</span>${money(sym,s.settled_pl)}</div>
      <div class="row"><span class="k">ROI</span><span class="${s.roi>=0?'pos':'neg'}">${s.roi==null?'—':s.roi.toFixed(2)+'%'}</span></div>
    </div>`; }
  document.getElementById('cards').innerHTML=h;
}
let view='open', sortKey='id', sortDir=1;
function rowsFor(){ return D.bets.filter(b=> view==='open' ? b.status==='open' : b.status!=='open'); }
function render(){
  let rows=rowsFor().slice().sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(typeof x==='string'){x=x||'';y=y||''}return (x>y?1:x<y?-1:0)*sortDir});
  const open=view==='open';
  const cols = open
   ? [['id','#'],['ts','placed'],['match','match'],['sel','selection'],['platform','venue'],['acct','a/c'],['odds','odds'],['stake','stake'],['max_win','max win'],['max_loss','max loss']]
   : [['id','#'],['settled_ts','settled'],['match','match'],['sel','selection'],['platform','venue'],['odds','odds'],['stake','stake'],['status','result'],['pl','p&l'],['clv','clv%']];
  const numc=new Set(['id','odds','stake','max_win','max_loss','pl','clv']);
  let h='<table><thead><tr>'+cols.map(c=>`<th class="${numc.has(c[0])?'num':''}" data-k="${c[0]}">${c[1]}</th>`).join('')+'</tr></thead><tbody>';
  for(const b of rows){
    h+='<tr>'+cols.map(([k])=>{
      let v=b[k];
      if(k==='status'){return `<td><span class="badge b-${v}">${v.toUpperCase()}</span></td>`}
      if(k==='pl'){return `<td class="num">${money(b.ccy,b.pl)}</td>`}
      if(k==='max_loss'||k==='max_win'||k==='stake'){return `<td class="num">${b.ccy}${(v||0).toFixed(2)}</td>`}
      if(k==='odds'){return `<td class="num">${(v||0).toFixed(2)}</td>`}
      if(k==='clv'){return `<td class="num muted">${v==null?'—':v.toFixed(2)}</td>`}
      if(k==='platform'){return `<td class="ven">${v}</td>`}
      if(k==='sel'){return `<td class="sel">${v}${b.free?' <span class="badge b-void">FREE</span>':''}</td>`}
      if(k==='match'){return `<td class="match">${v}</td>`}
      return `<td class="${numc.has(k)?'num':''}">${v==null?'':v}</td>`;
    }).join('')+'</tr>';
  }
  if(!rows.length) h+=`<tr><td colspan="${cols.length}" class="muted" style="padding:18px">no ${view} bets</td></tr>`;
  h+='</tbody></table>';
  document.getElementById('table').innerHTML=h;
  document.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=1}render()});
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));t.classList.add('on');view=t.dataset.v;sortKey=view==='open'?'id':'settled_ts';sortDir=view==='open'?1:-1;render()});
cards();render();
</script></body></html>"""


def main():
    bets = load()
    summary = summarise(bets)
    gen = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = render(bets, summary, gen)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write(html)
    nopen = sum(1 for b in bets if b["status"] == "open")
    print(f"wrote {OUT}: {len(bets)} bets ({nopen} open) | currencies {list(summary)}")
    for sym, s in summary.items():
        print(f"  {sym}: open {s['open_n']} @risk {sym}{s['open_at_risk']:.2f} | "
              f"settled P&L {sym}{s['settled_pl']:+.2f} ROI {s['roi']}")


if __name__ == "__main__":
    main()
