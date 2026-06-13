"""Exposure-aware card sizing.

The card sizes every pick with independent Kelly — blind to what you already
hold. This nets each pick's Kelly target against EXISTING open exposure to the
same 1X2 outcome, so the recommended *additional* stake never piles you on past
the target, even when the pick is +EV.

    recommended_add = max(0, kelly_target - existing_real_money_exposure)

Real-money exposure (has downside) is netted; free-bet/offer legs (stake-not-
returned, upside-only) are reported separately as correlation context, not netted.

Run:  python3 scripts/wca_exposure_sizer.py
"""
from __future__ import annotations
import re
import sqlite3
import sys

DB = "data/wca.db"
CARD = "data/card_latest.md"

# canonical team -> (match_key, outcome) so a single bet's selection maps to a 1X2 leg
TEAM_OUTCOME = {
    "australia": ("AUS_TUR", "Australia"), "turkey": ("AUS_TUR", "Turkey"), "türkiye": ("AUS_TUR", "Turkey"),
    "haiti": ("HAI_SCO", "Haiti"), "scotland": ("HAI_SCO", "Scotland"),
    "brazil": ("BRA_MOR", "Brazil"), "morocco": ("BRA_MOR", "Morocco"),
    "qatar": ("QAT_SUI", "Qatar"), "switzerland": ("QAT_SUI", "Switzerland"),
    "germany": ("GER_CUR", "Germany"), "curacao": ("GER_CUR", "Curaçao"), "curaçao": ("GER_CUR", "Curaçao"),
    "ecuador": ("IVO_ECU", "Ecuador"), "ivory coast": ("IVO_ECU", "Ivory Coast"),
    "tunisia": ("SWE_TUN", "Tunisia"), "sweden": ("SWE_TUN", "Sweden"),
}
MATCH_OF = {
    "australia v turkey": "AUS_TUR", "australia vs turkey": "AUS_TUR", "australia vs türkiye": "AUS_TUR",
    "haiti vs scotland": "HAI_SCO", "haiti v scotland": "HAI_SCO",
    "brazil vs morocco": "BRA_MOR", "qatar vs switzerland": "QAT_SUI",
    "germany vs curaçao": "GER_CUR", "ivory coast vs ecuador": "IVO_ECU", "sweden vs tunisia": "SWE_TUN",
}


def classify(match_desc, selection):
    """Map a bet to (match_key, outcome) where outcome in {team, 'Draw', None}.

    Returns (None, None) for non-1X2 selections (scorelines, player props,
    accumulators, outright markets) — they're not directional 1X2 exposure.
    """
    sel = (selection or "").strip().lower()
    md = (match_desc or "").strip().lower()
    mk = None
    for name, key in MATCH_OF.items():
        if name in md:
            mk = key
            break
    # draw
    if sel in ("draw", "the draw") and mk:
        return mk, "Draw"
    # straight team win (selection is exactly a team name)
    if sel in TEAM_OUTCOME:
        return TEAM_OUTCOME[sel]
    return None, None


def is_free_bet(source, notes):
    """True only for genuine stake-not-returned free bets (no downside).

    Source alone is too crude: many ``offer`` bets are REAL-money qualifiers
    (the Betfair-exchange hedge, the Paddy/Unibet/Betfred qualifying bets) whose
    stake IS at risk. Only the SNR free bets (flagged in notes) are upside-only.
    """
    # Genuine SNR free bets are logged with notes that BEGIN "FREE BET ...".
    # A substring match is unsafe: a real-money qualifier's notes can mention
    # the "SNR free bet" it unlocks (e.g. the exchange hedge id72) without its
    # own stake being free.
    n = (notes or "").strip().lower()
    return n.startswith("free bet") or n.startswith("free-bet")


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT match_desc, selection, decimal_odds, stake, source, notes "
        "FROM bets WHERE status='open'"
    ).fetchall()

    real = {}   # (match_key, outcome) -> real-money stake at risk
    free = {}   # (match_key, outcome) -> free-bet notional (upside only)
    for r in rows:
        mk, oc = classify(r["match_desc"], r["selection"])
        if mk is None:
            continue
        key = (mk, oc)
        if is_free_bet(r["source"], r["notes"]):
            free[key] = free.get(key, 0.0) + float(r["stake"])
        else:
            real[key] = real.get(key, 0.0) + float(r["stake"])

    # parse card picks
    picks = []
    txt = open(CARD).read().splitlines()
    for i, line in enumerate(txt):
        m = re.match(r"\*\d+\.\s+(.+?)\*\s+—\s+(.+?)\s+@\s+\*([\d.]+)\*", line)
        if not m:
            continue
        match_disp, sel, odds = m.group(1), m.group(2), float(m.group(3))
        edge = ""
        stake = None
        for j in (i + 1, i + 2):
            if j < len(txt):
                em = re.search(r"edge \*([+\-][\d.]+%)\*", txt[j])
                if em:
                    edge = em.group(1)
                sm = re.search(r"stake: main ([\d.]+)", txt[j])
                if sm:
                    stake = float(sm.group(1))
        mk, oc = classify(match_disp, sel)
        picks.append((match_disp, sel, odds, edge, stake, mk, oc))

    print("EXPOSURE-AWARE CARD  (recommended_add = max(0, kelly_target - existing real-money))")
    print("=" * 96)
    hdr = f"{'#':>2} {'pick':34} {'odds':>5} {'edge':>7} {'Kelly':>6} {'have£':>6} {'+free':>6} {'ADD£':>6}  note"
    print(hdr); print("-" * len(hdr))
    total_add = 0.0
    for n, (md, sel, odds, edge, stake, mk, oc) in enumerate(picks, 1):
        have = real.get((mk, oc), 0.0)
        fb = free.get((mk, oc), 0.0)
        tgt = stake or 0.0
        add = max(0.0, tgt - have)
        total_add += add
        note = ""
        if have >= tgt and tgt > 0:
            note = f"ALREADY AT/OVER target (long £{have:.0f}) -> skip"
        elif have > 0:
            note = f"top-up only (already £{have:.2f})"
        elif fb > 0:
            note = f"new real money; £{fb:.0f} free-bet upside already on"
        label = f"{md.split(' vs ')[0][:12]}/{sel}"[:34]
        print(f"{n:>2} {label:34} {odds:>5.2f} {edge:>7} {tgt:>6.2f} {have:>6.2f} {fb:>6.0f} {add:>6.2f}  {note}")
    print("-" * len(hdr))
    print(f"   Naive card total: £{sum((p[4] or 0) for p in picks):.2f}   |   Exposure-aware ADD total: £{total_add:.2f}")
    con.close()


if __name__ == "__main__":
    main()
