"""Generate ``site/bet_recs.json`` for the Bet Recs page (formerly Arbitrage).

Two products, both *monitoring only* (no execution):

1. **Best Bets** — model-backed prop recommendations. For every market where the
   project has a calibrated model probability we evaluate **both sides** of the
   Polymarket market (back YES vs back NO), fee-adjust, pick the positive-EV
   side and size it with **fractional Kelly** (``--fraction``) on a fixed
   ``--bankroll``, hard-capped at ``--cap`` of the bankroll.

2. **Prop Arbs** — Polymarket-vs-sportsbook prop arbitrage monitor. v1 surfaces
   *PM-internal* arbs (buying YES and NO on the same market costs < 1 after fee
   — always settlement-safe because both legs are the same market). The
   book-vs-PM scorer family is structurally present but inert until a live
   sportsbook prop feed is wired (it needs the settlement-key vocabulary in
   ``wca.arb`` extended to scorer markets — see the Arbitrage blind-spot note).

Coverage (v1): the **group-winner** family is priced **live** off Polymarket.
The model probabilities come from ``site/advancement_data.json``; if that feed
is older than the latest result the panel is flagged STALE, and any single rec
whose live price has drifted more than ``--drift`` from the model's baseline
price is withheld from the actionable list (the model hasn't seen that news).

Usage:
    PYTHONPATH=src python scripts/wca_betrecs.py \
        [--bankroll 250] [--fraction 0.5] [--cap 0.05] \
        [--min-edge 0.02] [--drift 0.10] [--out site/bet_recs.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ---------------------------------------------------------------------------
# Both-sides Kelly evaluation
# ---------------------------------------------------------------------------

def _eval_side(p: float, buy_price: float, bankroll: float, fraction: float,
               cap: float) -> Dict[str, Any]:
    """Evaluate backing one Polymarket YES-share with win prob *p* at *buy_price*.

    Returns the fee-adjusted $-EV per $1 staked and the fractional-Kelly stake.
    """
    from wca.advancement import pm_taker_fee
    from wca.markets import kelly

    if buy_price is None or buy_price <= 0.0 or buy_price >= 1.0:
        return {"price": buy_price, "ev": None, "stake": 0.0}
    fee = pm_taker_fee(buy_price)
    net_cost = buy_price + fee
    ev = p - net_cost                      # share pays 1 if it resolves YES
    odds = 1.0 / net_cost if net_cost > 0 else 0.0
    stake = kelly.stake(p, odds, bankroll, fraction=fraction, cap=cap) if odds > 1.0 else 0.0
    return {"price": round(buy_price, 4), "ev": round(ev, 4), "stake": round(stake, 2)}


def _rec_row(market: str, label: str, team: str, group: Optional[str],
             p_yes: float, yes_ask: float, no_ask: float, live_mid: float,
             baseline_pm: Optional[float], bankroll: float, fraction: float,
             cap: float) -> Dict[str, Any]:
    """Build a both-sides rec row for a binary YES/NO Polymarket market."""
    yes = _eval_side(p_yes, yes_ask, bankroll, fraction, cap)        # back YES
    no = _eval_side(1.0 - p_yes, no_ask, bankroll, fraction, cap)    # back NO
    yes["side"], no["side"] = "YES", "NO"
    yes["desc"], no["desc"] = label, _negate_label(label)
    best = yes if (yes["ev"] or -9) >= (no["ev"] or -9) else no
    drift = None
    if baseline_pm is not None and live_mid is not None:
        drift = round(abs(live_mid - baseline_pm), 4)
    return {
        "market": market,
        "team": team,
        "group": group,
        "model_prob": round(p_yes, 4),
        "live_mid": round(live_mid, 4) if live_mid is not None else None,
        "baseline_pm": round(baseline_pm, 4) if baseline_pm is not None else None,
        "drift": drift,
        "best_side": best["side"],
        "edge": best["ev"],
        "stake": best["stake"],
        "yes": yes,
        "no": no,
    }


def _negate_label(label: str) -> str:
    if label.lower().startswith("win "):
        return "NOT " + label[0].lower() + label[1:]
    return "NOT (" + label + ")"


# ---------------------------------------------------------------------------
# Live Polymarket group-winner prices (hardened, bounded)
# ---------------------------------------------------------------------------

def _live_group_winner_prices(timeout: float = 8.0) -> Tuple[Dict[str, Dict[str, float]], Optional[str]]:
    """Fetch live group-winner YES/NO/mid per team. Returns ({team: prices}, err)."""
    import wca.data.polymarket as poly
    poly._TIMEOUT = timeout
    from wca.advancement import (
        _group_winner_event_letter, _team_markets, _yes_mid, _yes_ask, _no_ask,
    )

    out: Dict[str, Dict[str, float]] = {}
    try:
        evs = poly.find_world_cup_markets(include_closed=False)
    except Exception as ex:  # one hung pull must not kill the whole product
        return out, type(ex).__name__
    for e in evs:
        letter = _group_winner_event_letter(str(e.get("title") or ""))
        if not letter:
            continue
        for team, m in _team_markets(e):
            try:
                mid = _yes_mid(m)
                if mid is None:
                    continue
                out[team] = {
                    "yes_ask": _yes_ask(m, mid),
                    "no_ask": _no_ask(m, mid),
                    "mid": mid,
                    "group": letter,
                }
            except Exception:
                continue                       # skip one bad market, keep going
    return out, None


# ---------------------------------------------------------------------------
# Prop-arb monitor (PM-internal; settlement-safe)
# ---------------------------------------------------------------------------

def _pm_internal_arbs(live: Dict[str, Dict[str, float]], min_profit: float = 0.005,
                      bankroll: float = 250.0) -> List[Dict[str, Any]]:
    """Buy YES and NO on the SAME PM market for < 1 total cost after fee."""
    from wca import arb

    found: List[Dict[str, Any]] = []
    for team, px in live.items():
        yes, no = px.get("yes_ask"), px.get("no_ask")
        if yes is None or no is None:
            continue
        ny = arb.pm_yes_to_decimal(yes)
        nn = arb.pm_yes_to_decimal(no)
        res = arb.two_way_arb(ny, nn, "polymarket_yes", "polymarket_no", net=True)
        if res is None or res["profit_pct"] < min_profit:
            continue
        legs = [
            {"venue": "Polymarket", "side": "YES", "desc": "win Group %s" % px.get("group"),
             "price": round(yes, 4), "stake": round(res["stake_fractions"][0] * bankroll, 2),
             "currency": "USD"},
            {"venue": "Polymarket", "side": "NO", "desc": "NOT win Group %s" % px.get("group"),
             "price": round(no, 4), "stake": round(res["stake_fractions"][1] * bankroll, 2),
             "currency": "USD"},
        ]
        found.append({
            "kind": "pm_internal", "market": "group_winner", "team": team,
            "group": px.get("group"), "guaranteed_pct": round(res["profit_pct"], 4),
            "legs": legs,
        })
    return sorted(found, key=lambda a: a["guaranteed_pct"], reverse=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=250.0)
    ap.add_argument("--fraction", type=float, default=0.5)   # 1/2 Kelly
    ap.add_argument("--cap", type=float, default=0.05)       # 5% hard cap
    ap.add_argument("--min-edge", type=float, default=0.02)  # 2% gross edge gate
    ap.add_argument("--drift", type=float, default=0.10)     # stale-model guard
    ap.add_argument("--adv", default="site/advancement_data.json")
    ap.add_argument("--out", default="site/bet_recs.json")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    adv = json.loads(Path(args.adv).read_text())
    model_generated = adv.get("model_generated") or adv.get("meta", {}).get("generated")
    model: Dict[str, Dict[str, Any]] = {}
    for t in adv.get("teams", []):
        gw = t.get("model", {}).get("group_winner")
        if gw is None:
            continue
        base = (t.get("pm", {}).get("group_winner") or {}).get("pm")
        model[t["team"]] = {"p": gw, "group": t.get("group"), "baseline_pm": base}

    t0 = time.time()
    live, fetch_err = _live_group_winner_prices(timeout=args.timeout)
    fetch_secs = round(time.time() - t0, 1)

    actionable: List[Dict[str, Any]] = []
    flagged: List[Dict[str, Any]] = []          # withheld: stale-drift or no model/price
    for team, mv in model.items():
        px = live.get(team)
        if not px:
            continue
        row = _rec_row(
            market="group_winner", label="win Group %s" % mv["group"], team=team,
            group=mv["group"], p_yes=mv["p"], yes_ask=px["yes_ask"], no_ask=px["no_ask"],
            live_mid=px["mid"], baseline_pm=mv["baseline_pm"], bankroll=args.bankroll,
            fraction=args.fraction, cap=args.cap,
        )
        if row["edge"] is None or row["edge"] < args.min_edge or row["stake"] <= 0:
            continue
        if row["drift"] is not None and row["drift"] > args.drift:
            row["flag"] = "model stale vs market (drift %.0f%%) — review before staking" % (row["drift"] * 100)
            flagged.append(row)
        else:
            actionable.append(row)
    actionable.sort(key=lambda r: r["edge"], reverse=True)
    flagged.sort(key=lambda r: r["drift"] or 0, reverse=True)

    prop_arbs = _pm_internal_arbs(live, bankroll=args.bankroll)

    # Staleness: model older than ~18h is suspect mid-tournament (one matchday).
    model_stale = False
    if model_generated:
        try:
            gen = dt.datetime.fromisoformat(str(model_generated).replace("Z", "").replace(" UTC", "").strip())
            model_stale = (dt.datetime.utcnow() - gen) > dt.timedelta(hours=18)
        except Exception:
            model_stale = False

    payload = {
        "meta": {
            "generated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "bankroll": args.bankroll,
            "currency": "USD",
            "kelly_fraction": args.fraction,
            "per_bet_cap": args.cap,
            "max_stake": round(args.bankroll * args.cap, 2),
            "min_edge": args.min_edge,
            "drift_guard": args.drift,
            "model_generated": model_generated,
            "model_stale": model_stale,
            "coverage": "group_winner (live PM). Model-backed; reach-stage / anytime-scorer / exact-score families wired next.",
            "live_fetch_secs": fetch_secs,
            "live_fetch_error": fetch_err,
            "monitoring_only": True,
            "note": "Read-only monitoring. No execution. Both sides analysed; "
                    "positive-EV side sized at fractional Kelly. Stakes hard-capped.",
        },
        "best_bets": actionable,
        "flagged": flagged,
        "prop_arbs": {
            "pm_internal": prop_arbs,
            "pm_vs_book": [],
            "pm_vs_book_note": "Inert until a live sportsbook scorer feed + prop "
                               "settlement keys are wired in wca.arb (Arbitrage blind-spot fix).",
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print("wrote %s — %d best bets, %d flagged, %d pm-internal arbs (live fetch %ss%s)" % (
        out, len(actionable), len(flagged), len(prop_arbs), fetch_secs,
        ", err=%s" % fetch_err if fetch_err else "",
    ))
    if model_stale:
        print("  WARNING: model feed (%s) looks stale — recs gated by drift guard." % model_generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
