#!/usr/bin/env python
"""One-off: apply the paper-book decision-quality lens to the REAL ledger.

The live book has NO trim/close events (it holds every bet to settlement), so the
trim/close track is structurally empty — that absence is itself the headline
risk-management finding. Every bet is an ADD, and a decimal-odds bet maps onto the
same binary-YES math the paper framework uses (price p = 1/odds; identical Kelly).
So we score each entry on the SAME process metric (sizing vs capped-Kelly) and the
SAME quarantined outcome metric (edge realisation = stake-weighted v - q).

Self-contained (stdlib only) so it can be copied to the mini and run against the
canonical ledger with no project on PYTHONPATH. The Kelly math mirrors
wca.testbook.store so the live-book and paper-book numbers are computed identically.

    python3 scripts/wca_ledger_decision_analysis.py [--db data/wca.db]
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


# Pure decision math — mirrors wca.testbook.store so this stays a SELF-CONTAINED,
# stdlib-only script you can scp to the mini and run without the project on PYTHONPATH.
def kelly_fraction(q: float, p: float) -> float:
    if not (0.0 < p < 1.0):
        return 0.0
    return max(0.0, (q - p) / (1.0 - p))


def f_target(q: float, p: float, kelly_mult: float, max_stake_frac: float) -> float:
    return min(kelly_mult * kelly_fraction(q, p), max_stake_frac)


def g_logwealth(f: float, q: float, p: float) -> float:
    if not (0.0 < p < 1.0) or f >= 1.0:
        return float("-inf") if f >= 1.0 else 0.0
    win, lose = 1.0 + f * (1.0 - p) / p, 1.0 - f
    if win <= 0 or lose <= 0:
        return float("-inf")
    return q * math.log(win) + (1.0 - q) * math.log(lose)


# Sizing reference (documented assumptions — the only inputs not in the ledger).
KELLY_MULT = 0.5            # half-Kelly (user's chosen fraction)
MAX_STAKE_FRAC = 0.05       # 5% per-bet cap (whole-book coverage policy)
BANKROLL = {"gbp": 1500.0, "usd": 1995.0}   # dual-pool sizing base


def _currency(platform: str) -> str:
    p = (platform or "").lower()
    return "usd" if ("poly" in p or "kalshi" in p) else "gbp"


def _bucket(market: str, selection: str) -> str:
    t = ((market or "") + " " + (selection or "")).lower()
    if any(k in t for k in ("golden boot", "outright", "winner", "to win")):
        return "outright"
    if any(k in t for k in ("reach", "advance", "eliminated", "round of", "qualif")):
        return "advancement"
    if any(k in t for k in ("acca", "treble", "2up", "bet builder", "betbuilder", "combo", "fold")):
        return "acca/builder"
    if any(k in t for k in ("sot", "shots", "scorer", "goalscorer", "to score", "assist", "card")):
        return "player_prop"
    if any(k in t for k in ("draw", "win", "1x2", "result", "ht", "double chance")):
        return "match_result"
    return "other"


def _agg():
    return {"n": 0, "edge": 0.0, "stake": 0.0, "h": 0.0, "fk": 0.0, "ft": 0.0,
            "gog": 0.0, "dg": 0.0, "capbind": 0, "n_set": 0, "shares": 0.0,
            "gap": 0.0, "q_set": 0.0, "v_set": 0.0, "pl": 0.0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "wca.db"))
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT * FROM bets"))

    # Composition.
    by_status, by_source, by_platform = {}, {}, {}
    cashed = 0
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_source[r["source"] or "?"] = by_source.get(r["source"] or "?", 0) + 1
        by_platform[r["platform"]] = by_platform.get(r["platform"], 0) + 1
        if (r["status"] or "").lower() == "cashed" or r["cashout_proceeds"] is not None:
            cashed += 1

    print("=" * 74)
    print("REAL-LEDGER DECISION ANALYSIS  (db=%s)" % args.db)
    print("=" * 74)
    print("Bets: %d   |   status %s" % (len(rows), dict(sorted(by_status.items()))))
    print("source %s" % dict(sorted(by_source.items())))
    canonical = "World-Cup-26" in os.path.abspath(args.db)
    print("Source: %s" % ("CANONICAL mini ledger ✅" if canonical
                          else "⚠ non-canonical copy (the canonical ledger is the mini: ~/World-Cup-26/data/wca.db)"))
    print("Sizing assumes half-Kelly, 5%% cap, bankroll £1500 / $1995 (dual pool).")

    # --- TRIM / CLOSE track --------------------------------------------------
    print("\n" + "-" * 74)
    print("TRIM / CLOSE DECISIONS")
    print("-" * 74)
    print("  trims: 0    early-closes/cash-outs: %d" % cashed)
    print("  → The live book is HOLD-TO-SETTLEMENT only. No early risk management is")
    print("    exercised: no variance-reducing trims, no edge-decay or stop exits.")
    print("    So there is no trim/close decision quality to score — the *absence* is")
    print("    the finding (all variance is carried to the final whistle).")

    # --- ADD decisions -------------------------------------------------------
    overall = _agg()
    by_src, by_bkt = {}, {}
    n_priced = n_unpriced = 0
    for r in rows:
        d = r["decimal_odds"]
        q = r["model_prob"]
        if q is None or d is None or d <= 1.0:
            n_unpriced += 1
            continue
        n_priced += 1
        q = max(1e-6, min(1.0 - 1e-6, float(q)))
        p = 1.0 / float(d)
        stake = float(r["stake"] or 0.0)
        bank = BANKROLL[_currency(r["platform"])]
        fk = kelly_fraction(q, p)
        ft = f_target(q, p, KELLY_MULT, MAX_STAKE_FRAC)
        h = stake / bank
        gog = h - ft
        dg = g_logwealth(h, q, p) - g_logwealth(ft, q, p)
        capbind = 1 if (KELLY_MULT * fk > MAX_STAKE_FRAC + 1e-12) else 0
        status = (r["status"] or "").lower()
        v = 1.0 if status == "won" else (0.0 if status == "lost" else None)
        shares = stake * float(d)   # = stake / p

        for agg in (overall, by_src.setdefault(r["source"] or "?", _agg()),
                    by_bkt.setdefault(_bucket(r["market"], r["selection"]), _agg())):
            agg["n"] += 1
            agg["edge"] += (q - p)
            agg["stake"] += stake
            agg["h"] += h
            agg["fk"] += fk
            agg["ft"] += ft
            agg["gog"] += gog
            agg["dg"] += dg
            agg["capbind"] += capbind
            if v is not None:
                agg["n_set"] += 1
                agg["shares"] += shares
                agg["gap"] += shares * (v - q)
                agg["q_set"] += q
                agg["v_set"] += v
                agg["pl"] += float(r["settled_pl"] or 0.0)

    def line(name, a):
        if a["n"] == 0:
            return
        n = a["n"]
        meanedge = a["edge"] / n
        gog = a["gog"] / n
        cap = 100.0 * a["capbind"] / n
        gapcell = ("%+.3f" % (a["gap"] / a["shares"])) if a["shares"] else "  n/a"
        wr = ("%.0f%%/%.0f%%" % (100 * a["v_set"] / a["n_set"], 100 * a["q_set"] / a["n_set"])) if a["n_set"] else "  -"
        print("  %-14s n=%-3d edge%+.3f  meanGOG%+.3f  cap%3.0f%%  | settled n=%-2d  hit/model %-9s evgap %s  P/L %+.0f"
              % (name[:14], n, meanedge, gog, cap, a["n_set"], wr, gapcell, a["pl"]))

    print("\n" + "-" * 74)
    print("ADD DECISIONS — process (sizing) + outcome (edge realisation)")
    print("-" * 74)
    print("  priced (model_prob + odds present): %d   |   unpriced (offer/punt/no-q): %d"
          % (n_priced, n_unpriced))
    print("\n  meanGOG<0 ⇒ staked BELOW half-Kelly (conservative);  >0 ⇒ above.")
    print("  evgap = stake-weighted (realised − model q):  >0 model UNDER-rated, <0 OVER-rated.\n")
    line("OVERALL", overall)
    print("\n  by source:")
    for k in sorted(by_src):
        line("  " + k, by_src[k])
    print("\n  by market bucket:")
    for k in sorted(by_bkt):
        line("  " + k, by_bkt[k])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
