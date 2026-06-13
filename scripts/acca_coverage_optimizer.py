"""Outcome-coverage optimizer for 5x free-bet accumulators (account 2 / Betfair SB).

Enumerates all 1X2 result scenarios across tonight's 4-match slate, scores the
EXISTING open book's result-level return in each scenario, then selects 5 free-bet
accumulators (stake-not-returned) to maximise outcome coverage + model EV.

All probabilities are the WCA model's 1X2 probs (data/model_predictions.json).
All acca-leg odds are live Betfair Sportsbook (betfair_sb_uk), since free-bet
accas must be placed on the sportsbook, not the exchange.

Run:  python3 scripts/acca_coverage_optimizer.py
"""
from __future__ import annotations
import itertools, json, sys

# --------------------------------------------------------------------------
# 1. The 4-match slate: model probs + live Betfair SPORTSBOOK odds
#    outcome key -> (model_prob, betfair_sb_odds)
# --------------------------------------------------------------------------
SLATE = {
    "QAT": {  # Qatar vs Switzerland, KO 19:00
        "label": "Qatar v Switzerland",
        "outcomes": {
            "Qatar":       (0.07649, 12.0),
            "Draw_Q":      (0.156077, 5.5),
            "Switzerland": (0.767433, 1.17),
        },
    },
    "BRA": {  # Brazil vs Morocco, KO 22:00
        "label": "Brazil v Morocco",
        "outcomes": {
            "Brazil":  (0.556568, 1.57),
            "Draw_B":  (0.264724, 3.2),
            "Morocco": (0.178709, 5.0),
        },
    },
    "HAI": {  # Haiti vs Scotland, KO 01:00
        "label": "Haiti v Scotland",
        "outcomes": {
            "Haiti":    (0.168147, 5.0),
            "Draw_H":   (0.229761, 3.6),
            "Scotland": (0.602092, 1.44),
        },
    },
    "AUS": {  # Australia vs Turkey, KO 04:00
        "label": "Australia v Turkey",
        "outcomes": {
            "Australia": (0.244, 4.5),
            "Draw_A":    (0.274, 3.25),
            "Turkey":    (0.482, 1.62),
        },
    },
}
ORDER = ["QAT", "BRA", "HAI", "AUS"]

# --------------------------------------------------------------------------
# 2. Existing open book — result-level returns on the 4 slate matches.
#    (profit returned to bankroll if that outcome hits; stake already spent)
#    Exact-score polymarket punts are listed separately as WEAK coverage.
# --------------------------------------------------------------------------
# (match, winning_outcome, stake, odds)  -> profit = stake*(odds-1)
EXISTING_SINGLES = [
    ("QAT", "Qatar",     4.42, 17.0),
    ("QAT", "Draw_Q",    5.11, 7.2),
    ("BRA", "Morocco",   0.30, 6.2),
    ("BRA", "Morocco",   3.47, 6.0),
    ("BRA", "Draw_B",    3.23, 4.0),
    ("HAI", "Draw_H",    3.92, 4.4),
    ("AUS", "Australia", 18.06, 5.5),
    ("AUS", "Draw_A",    5.80, 3.9),
]
# exact-score punts: (match, {outcome_it_implies}, note) — only pay on exact score,
# so they DON'T reliably cover a result branch. Tracked for reporting only.
SCORELINE_PUNTS = [
    ("QAT", "Switzerland", "Qatar 0-2 (£1.9)"),
    ("BRA", "Brazil",      "Brazil 1-0 (£1.7)"),
    ("HAI", "Scotland",    "Haiti 0-1, 0-2 (£2.9)"),
    ("HAI", "Draw_H",      "Haiti 1-1 (£0.77)"),
    ("AUS", "Turkey",      "Aus 0-1 Tur (£1.5)"),
]

FREE_BET = 10.0  # £ per acca, stake NOT returned

# --------------------------------------------------------------------------
# Scenario enumeration
# --------------------------------------------------------------------------
def scenarios():
    keys = [list(SLATE[m]["outcomes"].keys()) for m in ORDER]
    for combo in itertools.product(*keys):
        sel = dict(zip(ORDER, combo))
        p = 1.0
        for m, o in sel.items():
            p *= SLATE[m]["outcomes"][o][0]
        yield sel, p

def existing_return(sel):
    """Profit from existing RESULT-LEVEL singles in this scenario."""
    tot = 0.0
    for m, o, stake, odds in EXISTING_SINGLES:
        if sel[m] == o:
            tot += stake * (odds - 1.0)
    return tot

# --------------------------------------------------------------------------
# Acca model: a tuple of (match, outcome) legs
# --------------------------------------------------------------------------
def acca_odds(acca):
    o = 1.0
    for m, out in acca:
        o *= SLATE[m]["outcomes"][out][1]
    return o

def acca_prob(acca):
    p = 1.0
    for m, out in acca:
        p *= SLATE[m]["outcomes"][out][0]
    return p

def acca_wins(acca, sel):
    return all(sel[m] == out for m, out in acca)

def acca_profit_if_win(acca):
    return FREE_BET * (acca_odds(acca) - 1.0)

def gen_accas(min_legs=3, max_legs=4, min_leg_odds=1.0):
    """All feasible accas: choose min..max matches, one outcome each, each leg>=min_leg_odds."""
    out = []
    for nlegs in range(min_legs, max_legs + 1):
        for matches in itertools.combinations(ORDER, nlegs):
            per_match_choices = []
            for m in matches:
                opts = [(m, o) for o, (p, od) in SLATE[m]["outcomes"].items() if od >= min_leg_odds]
                per_match_choices.append(opts)
            for legs in itertools.product(*per_match_choices):
                out.append(tuple(legs))
    return out

# --------------------------------------------------------------------------
# Portfolio evaluation
# --------------------------------------------------------------------------
def evaluate(portfolio, hole_threshold=5.0):
    """Return dict of stats for a portfolio (list of accas)."""
    scns = list(scenarios())
    ev_new = 0.0           # EV of the 5 accas alone
    ev_total = 0.0         # EV of existing singles + accas
    p_any_acca = 0.0       # prob >=1 acca wins
    p_slate_hole = 0.0     # prob total slate return < threshold
    covered_mass = 0.0     # prob total slate return > 0
    worst = 1e9
    for sel, p in scns:
        new_ret = sum(acca_profit_if_win(a) for a in portfolio if acca_wins(a, sel))
        old_ret = existing_return(sel)
        tot = new_ret + old_ret
        ev_new += p * new_ret
        ev_total += p * tot
        if new_ret > 0:
            p_any_acca += p
        if tot > 0:
            covered_mass += p
        if tot < hole_threshold:
            p_slate_hole += p
        worst = min(worst, tot)
    return {
        "ev_new_accas": ev_new,
        "ev_total_slate": ev_total,
        "p_any_acca_wins": p_any_acca,
        "p_slate_covered": covered_mass,
        "p_slate_hole_below_%g" % hole_threshold: p_slate_hole,
        "worst_case_slate_return": worst,
        "n_accas": len(portfolio),
    }

def fmt_acca(a):
    parts = []
    for m, out in a:
        lbl = out.replace("Draw_Q", "Draw").replace("Draw_B", "Draw").replace("Draw_H", "Draw").replace("Draw_A", "Draw")
        parts.append(f"{lbl}({SLATE[m]['outcomes'][out][1]:g})")
    return " × ".join(parts) + f"  = {acca_odds(a):.2f}  | model P(hit)={acca_prob(a)*100:.1f}%  £10→£{acca_profit_if_win(a):.2f}"

# --------------------------------------------------------------------------
# Greedy selection: pick 5 accas maximising an objective, given existing book
# --------------------------------------------------------------------------
def greedy_select(objective, n=5, min_leg_odds=1.0, candidates=None):
    cand = candidates if candidates is not None else gen_accas(3, 4, min_leg_odds)
    scns = list(scenarios())
    chosen = []
    def portfolio_score(port):
        if objective == "ev":
            return sum(p * sum(acca_profit_if_win(a) for a in port if acca_wins(a, sel)) for sel, p in scns)
        if objective == "coverage":
            # prob that (existing + accas) returns > hole floor; tie-break by EV
            floor = 5.0
            cov = 0.0; ev = 0.0
            for sel, p in scns:
                nr = sum(acca_profit_if_win(a) for a in port if acca_wins(a, sel))
                tot = nr + existing_return(sel)
                if tot >= floor: cov += p
                ev += p * nr
            return cov * 1000 + ev * 0.01
        if objective == "logutil":
            BR = 38.78 + 200.0  # notional bankroll incl. open stakes
            u = 0.0
            for sel, p in scns:
                nr = sum(acca_profit_if_win(a) for a in port if acca_wins(a, sel))
                tot = nr + existing_return(sel)
                import math
                u += p * math.log(BR + tot)
            return u
        raise ValueError(objective)
    for _ in range(n):
        best = None; best_s = -1e18
        for a in cand:
            if a in chosen:
                continue
            s = portfolio_score(chosen + [a])
            if s > best_s:
                best_s = s; best = a
        chosen.append(best)
    return chosen

# --------------------------------------------------------------------------
def report_existing_hole():
    print("=" * 78)
    print("EXISTING BOOK — result-level coverage of tonight's 4-match slate")
    print("=" * 78)
    hole_mass = 0.0
    worst_scns = []
    for sel, p in scenarios():
        r = existing_return(sel)
        if r < 5.0:
            hole_mass += p
            worst_scns.append((p, sel, r))
    worst_scns.sort(reverse=True)
    print(f"P(existing result-book returns < £5 on the slate) = {hole_mass*100:.1f}%")
    print("Biggest uncovered scenarios (prob, result, existing return):")
    for p, sel, r in worst_scns[:8]:
        s = ", ".join(f"{SLATE[m]['label'].split(' v ')[0] if sel[m] not in ('Draw_Q','Draw_B','Draw_H','Draw_A') else ''}{sel[m]}" for m in ORDER)
        desc = " / ".join(sel[m].replace('Draw_Q','Draw').replace('Draw_B','Draw').replace('Draw_H','Draw').replace('Draw_A','Draw') for m in ORDER)
        print(f"  {p*100:5.2f}%  [{desc}]  -> £{r:.2f}")
    print()

def main():
    report_existing_hole()
    for obj in ["coverage", "ev", "logutil"]:
        for mlo, tag in [(1.0, "any-leg"), (1.5, "legs>=1.5")]:
            port = greedy_select(obj, n=5, min_leg_odds=mlo)
            stats = evaluate(port)
            print("-" * 78)
            print(f"OBJECTIVE = {obj.upper()}   ({tag})")
            for i, a in enumerate(port, 1):
                print(f"  {i}. {fmt_acca(a)}")
            print(f"   EV(accas alone) = £{stats['ev_new_accas']:.2f}  | "
                  f"P(>=1 acca wins) = {stats['p_any_acca_wins']*100:.1f}%  | "
                  f"P(slate covered) = {stats['p_slate_covered']*100:.1f}%")
            print()

def score_portfolio_json(port_json):
    """Score a hand-designed portfolio. Input: list of accas; each acca is a
    list of [match_key, outcome_key]. Returns full stats + per-acca detail."""
    portfolio = [tuple((m, o) for m, o in acca) for acca in port_json]
    # validate
    for a in portfolio:
        for m, o in a:
            if m not in SLATE or o not in SLATE[m]["outcomes"]:
                raise ValueError(f"bad leg {m}/{o}")
    stats = evaluate(portfolio)
    detail = [{"acca": [list(l) for l in a], "odds": round(acca_odds(a), 2),
               "model_p_hit": round(acca_prob(a), 4),
               "profit_if_win": round(acca_profit_if_win(a), 2),
               "pretty": fmt_acca(a)} for a in portfolio]
    return {"stats": stats, "accas": detail}

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--score":
        data = json.load(sys.stdin)
        print(json.dumps(score_portfolio_json(data), indent=2))
    else:
        main()


# ==========================================================================
# CO-HITTABLE MODE: 5 accas that can ALL win together (stacked upside).
# Given a target scenario (one outcome per match), the unique set of 5
# distinct 3-or-4-leg accas is the "drop-one" bundle:
#   - 1x 4-leg (all four target outcomes)
#   - 4x 3-leg (each drops one match -> also pays if that match deviates)
# We rank all 81 target scenarios by stacked upside + EV + book-complement.
# ==========================================================================
def dropone_bundle(target):
    """target: dict match->outcome. Returns list of 5 accas (drop-one + full)."""
    legs_all = [(m, target[m]) for m in ORDER]
    accas = [tuple(legs_all)]  # 4-leg
    for drop in ORDER:         # 4x 3-leg
        accas.append(tuple((m, target[m]) for m in ORDER if m != drop))
    return accas

def rank_cohittable():
    scns = list(scenarios())
    rows = []
    for combo in itertools.product(*[list(SLATE[m]["outcomes"].keys()) for m in ORDER]):
        target = dict(zip(ORDER, combo))
        bundle = dropone_bundle(target)
        # best-case stacked payout (all 5 hit, when target occurs)
        stacked = sum(acca_profit_if_win(a) for a in bundle)
        p_target = 1.0
        for m, o in target.items():
            p_target *= SLATE[m]["outcomes"][o][0]
        # EV of the acca bundle alone across all scenarios
        ev_accas = 0.0
        ev_total = 0.0
        p_any = 0.0
        worst_combined = 1e9
        for sel, p in scns:
            nr = sum(acca_profit_if_win(a) for a in bundle if acca_wins(a, sel))
            tot = nr + existing_return(sel)
            ev_accas += p * nr
            ev_total += p * tot
            if nr > 0: p_any += p
            worst_combined = min(worst_combined, tot)
        rows.append({
            "target": {m: target[m].replace('Draw_Q','Draw').replace('Draw_B','Draw').replace('Draw_H','Draw').replace('Draw_A','Draw') for m in ORDER},
            "p_target_all5_hit": p_target,
            "stacked_payout_all5": stacked,
            "ev_accas": ev_accas,
            "ev_total_combined": ev_total,
            "p_any_acca_pays": p_any,
            "worst_combined": worst_combined,
            "ev_weighted_upside": ev_accas,  # alias
        })
    return rows

def report_cohittable():
    rows = rank_cohittable()
    def show(rows, key, title, n=6, reverse=True):
        print(f"\n### Top {n} targets by {title}")
        for r in sorted(rows, key=lambda x: x[key], reverse=reverse)[:n]:
            t = r["target"]
            tgt = f"{t['QAT']}/{t['BRA']}/{t['HAI']}/{t['AUS']}"
            print(f"  {tgt:42s} P(all5)={r['p_target_all5_hit']*100:5.2f}%  "
                  f"stack=£{r['stacked_payout_all5']:7.2f}  EV(accas)=£{r['ev_accas']:6.2f}  "
                  f"P(any pays)={r['p_any_acca_pays']*100:4.1f}%  EVtot=£{r['ev_total_combined']:6.2f}")
    print("="*100)
    print("CO-HITTABLE (drop-one) BUNDLES — ranked over all 81 target scenarios")
    print("="*100)
    show(rows, "ev_accas", "EV of acca bundle (expected upside)")
    show(rows, "stacked_payout_all5", "best-case stacked payout (jackpot if target hits)")
    show(rows, "ev_total_combined", "EV of COMBINED book (accas + existing)")
    # explicit favourite target
    fav = next(r for r in rows if r["target"]=={'QAT':'Switzerland','BRA':'Brazil','HAI':'Scotland','AUS':'Turkey'})
    print("\n### FAVOURITE-SWEEP target (Switzerland/Brazil/Scotland/Turkey):")
    print(f"  P(all 5 hit)={fav['p_target_all5_hit']*100:.2f}%  stacked=£{fav['stacked_payout_all5']:.2f}  "
          f"EV(accas)=£{fav['ev_accas']:.2f}  P(any pays)={fav['p_any_acca_pays']*100:.1f}%  EVtot=£{fav['ev_total_combined']:.2f}")

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--cohit":
    report_cohittable()
