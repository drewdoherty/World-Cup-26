#!/usr/bin/env python
"""Run / report the isolated paper-trading test book.

    PYTHONPATH=src python3 scripts/wca_test_book.py trade     # one live paper pass
    PYTHONPATH=src python3 scripts/wca_test_book.py mark      # mark open positions to CLOB
    PYTHONPATH=src python3 scripts/wca_test_book.py report    # headline P&L / exposure

Fully isolated: writes only to data/test_book.db. No real money, ever.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.testbook import store, trader  # noqa: E402
from wca.data import pm_clob_history as CH  # noqa: E402

_DB = os.path.join(_ROOT, "data", "test_book.db")
_SCORES = os.path.join(_ROOT, "site", "scores_data.json")
_ADV = os.path.join(_ROOT, "site", "advancement_data.json")

# Sizing + trim/close rule config (frozen per evaluation window — no tuning on
# the certification set; matches the trade-pass defaults).
KELLY_MULT = 0.5
MAX_STAKE_FRAC = 0.02
OVER_KELLY_BAND = 0.5     # R2: trim when marked exposure > f_target*(1+band)
SPREAD_CAP = 0.10         # R3: exit when bid-ask spread exceeds this
MIN_DEPTH = 0.0           # R3: exit when best-bid depth below this (0 = off)

# LIVE-money sizing shown on the @worldcupdevbot BUY lines (for manual A1 bets).
# Bankroll = base ± the book's realised P&L; stake = LIVE_KELLY × Kelly × bankroll.
# All overridable via env so the operator can retune without a code change.
LIVE_BANKROLL_BASE = float(os.environ.get("WCA_TESTBOOK_LIVE_BANKROLL", "3000"))
LIVE_KELLY = float(os.environ.get("WCA_TESTBOOK_LIVE_KELLY", "0.25"))   # ¼-Kelly
LIVE_MAX_FRAC = float(os.environ.get("WCA_TESTBOOK_LIVE_MAXFRAC", "0"))  # 0 = uncapped
LIVE_CCY = os.environ.get("WCA_TESTBOOK_LIVE_CCY", "£")


def _live_bankroll(report):
    """Latest live bankroll = base ± realised P&L to date."""
    return LIVE_BANKROLL_BASE + float(report.get("realized_pl", 0.0) or 0.0)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _send_chart(con, report=None):
    """Best-effort: render the equity/P&L curve and post it to @worldcupdevbot."""
    from wca.testbook import chart, notify
    rep = report if report is not None else store.report(con)
    png = chart.render_equity_png(store.equity_series(con), seed=rep.get("seed", 0.0))
    return notify.send_photo(png, notify.chart_caption(rep))


def _load_model():
    with open(_SCORES) as fh:
        scores = json.load(fh)
    with open(_ADV) as fh:
        adv = json.load(fh)
    return trader.load_model(scores, adv)


def cmd_trade(args):
    from wca.data import polymarket as P
    con = store.connect(_DB)
    store.seed_bankroll(con, store.DEFAULT_SEED_USD, ts_utc=_now())
    model = _load_model()
    print("Fetching live PM markets …")
    events = P.find_world_cup_markets(include_closed=False)
    res = trader.run_paper_pass(
        con, model, events, ts_utc=_now(),
        edge_threshold=args.edge, kelly_mult=args.kelly,
        max_stake_frac=args.max_stake, min_volume=args.min_volume)
    from wca.testbook import notify
    rep = store.report(con)
    bankroll = _live_bankroll(rep)
    print("Pass @ %s: %d candidates, placed %d, skipped %d | cash $%.2f deployed $%.2f"
          % (res["ts"], res["candidates"], res["n_placed"], res["skipped"],
             res["balance"], res["deployed"]))
    print("Live bankroll %s%.0f (base %s%.0f %+0.0f realised) · %g×Kelly"
          % (LIVE_CCY, bankroll, LIVE_CCY, LIVE_BANKROLL_BASE,
             rep.get("realized_pl", 0.0), LIVE_KELLY))
    for p in res["placed"]:
        s = notify.live_sizing(p["model"], p["price"], bankroll,
                               kelly_frac=LIVE_KELLY, max_frac=LIVE_MAX_FRAC)
        print("  +[%s/%s] %-34s @ %.0f¢  fair %.0f%%  edge %+.0f%%  -> stake %s%.0f (%.1f%%)"
              % (p["basis"], p["market"], p["selection"][:34], p["price"] * 100,
                 p["model"] * 100, p["edge"] * 100, LIVE_CCY, s["stake"], 100 * s["frac"]))
    # Ping the dev chat (@worldcupdevbot) with this pass's activity + P&L chart.
    if notify.send(notify.format_activity(
            res, rep, live_bankroll=bankroll, kelly_frac=LIVE_KELLY,
            max_frac=LIVE_MAX_FRAC, currency=LIVE_CCY)):
        print("  (pinged @worldcupdevbot)")
        if _send_chart(con, rep):
            print("  (posted P&L chart)")
    return 0


def cmd_mark(args):
    con = store.connect(_DB)
    ts = _now()
    equity = store.report(con)["equity"]
    n = 0
    actions = []
    for b in store.open_bets(con):
        tok = b.get("token_id")
        if not tok:
            continue
        # Real transactable book (INV-2); fall back to mid-only price history.
        top = CH.top_of_book(tok)
        if top and top.get("mid") is not None:
            mid, bid, ask = top["mid"], top.get("bid"), top.get("ask")
            spread, depth = top.get("spread"), top.get("bid_size")
        else:
            hist = CH.price_history(tok, interval="1d", fidelity=60)
            if not hist:
                continue
            mid, bid, ask, spread, depth = hist[-1][1], None, None, None, None
        q_t = b.get("model_prob")
        store.record_mark(con, b["id"], mid, ts, bid_price=bid, ask_price=ask, spread=spread,
                          depth_bid=depth, q_at_mark=q_t, q_source="entry_static")
        n += 1
        # Exit rules need a transactable bid AND a model belief.
        if bid is None or q_t is None:
            continue
        entry, stake = float(b["entry_price"]), float(b["stake_usd"])
        shares = stake / entry if entry > 0 else 0.0
        decision = trader.eval_exit_rules(
            q_t=float(q_t), p_bid=bid, p_mid=mid, spread=spread, depth=depth, shares=shares,
            equity=equity, kelly_mult=KELLY_MULT, max_stake_frac=MAX_STAKE_FRAC,
            over_kelly_band=OVER_KELLY_BAND, spread_cap=SPREAD_CAP, min_depth=MIN_DEPTH)
        if not decision:
            continue
        rule, action, sell_shares, threshold = decision
        if action == "close":
            realized_pl = store.close(con, b["id"], bid, ts)
            sold, stake_after = shares, 0.0
        else:
            realized_pl = store.trim(con, b["id"], sell_shares, bid, ts)
            sold, stake_after = sell_shares, max(0.0, (shares - sell_shares)) * entry
        store.log_decision(
            con, action=action, rule=rule, bet_id=b["id"], token_id=tok, fixture=b.get("fixture"),
            resolution_basis=b["resolution_basis"], q_t=float(q_t), q_source="entry_static",
            p_t=bid, p_mid_t=mid, spread_t=spread, depth_t=depth, equity_t=equity,
            kelly_mult=KELLY_MULT, max_stake_frac=MAX_STAKE_FRAC, entry_price=entry,
            stake_before=stake, stake_after=stake_after, shares_delta=sold,
            rule_threshold=threshold, ts_utc=ts)
        actions.append({
            "id": b["id"], "action": action, "rule": rule,
            "fixture": b.get("fixture"), "selection": b.get("selection"),
            "basis": b["resolution_basis"], "market": b.get("market_type"),
            "entry_price": entry, "exit_price": bid, "shares_sold": sold,
            "realized_pl": realized_pl, "stake_after": stake_after,
            "q": float(q_t), "spread": spread})
    print("Marked %d open positions." % n)
    for a in actions:
        verb = "EXIT" if a["action"] == "close" else "TRIM"
        print("  %-4s #%-4d %-30s sold %.1fsh @ %.0f¢ (entry %.0f¢) realised $%+.2f · %s"
              % (verb, a["id"], str(a["selection"])[:30], a["shares_sold"],
                 a["exit_price"] * 100, a["entry_price"] * 100, a["realized_pl"], a["rule"]))
    if actions:
        from wca.testbook import notify
        rep = store.report(con)
        if notify.send(notify.format_exits(actions, rep)):
            _send_chart(con, rep)
    return cmd_report(args)


def cmd_decisions(args):
    from wca.testbook import settle as S
    con = store.connect(_DB)
    counts = {r[0]: r[1] for r in con.execute(
        "SELECT action, COUNT(*) FROM decision_events GROUP BY action")}
    total = sum(counts.values())
    print("\n=== PAPER DECISION QUALITY ===")
    print("%d decisions (%d add / %d trim / %d close)"
          % (total, counts.get("add", 0), counts.get("trim", 0), counts.get("close", 0)))
    proc = S.process_rollup(con)
    print("\nPROCESS (leading · decision-time only · model-q)")
    print("  %-10s %-13s %7s %9s %8s %8s %4s" % ("basis", "q_source", "meanGOG", "meanΔg", "spread$", "capbind", "n"))
    for basis in sorted(proc):
        for qs, d in sorted(proc[basis].items()):
            print("  %-10s %-13s %+7.3f %+9.4f %8s %7.0f%% %4d"
                  % (basis, qs, d["mean_gog"] or 0, d["mean_delta_g"] or 0,
                     ("%.3f" % d["mean_exit_spread_cost"]) if d["mean_exit_spread_cost"] is not None else "  -",
                     100 * (d["cap_binding_rate"] or 0), d["n"]))
    calib = S.calibration_rollup(con)
    print("\nOUTCOME (lagging · quarantined · does NOT grade decisions)")
    if calib["by_basis"]:
        for basis, d in sorted(calib["by_basis"].items()):
            gap = d["ev_calibration_gap"]
            print("  ev_calibration_gap %-9s %s  [n=%d%s]"
                  % (basis, ("%+.3f" % gap) if gap is not None else " n/a",
                     d["n"], " COLLECTING" if d["collecting"] else ""))
    else:
        print("  ev_calibration_gap: COLLECTING (no settled add decisions yet)")
    print("  exit value vs hold: $%+.2f over %d exits%s"
          % (calib["exit_value_vs_hold"], calib["n_exits"],
             " [INSUFFICIENT]" if calib["n_exits"] < 10 else ""))
    print("\n_process columns drive KEEP/KILL; outcome block is validation-only (INV-5)._")
    return 0


def cmd_report(args):
    con = store.connect(_DB)
    rep = store.report(con)
    print("\n=== TEST BOOK (paper, $%.0f seed) ===" % rep["seed"])
    print("equity      $%.2f   (ROI %+.2f%%)" % (rep["equity"], rep["roi_pct"]))
    print("cash        $%.2f" % rep["realized_balance"])
    print("deployed    $%.2f across %d open" % (rep["deployed"], rep["n_open"]))
    print("realised    $%+.2f over %d settled" % (rep["realized_pl"], rep["n_settled"]))
    print("unrealised  $%+.2f (MTM)" % rep["unrealized_pl"])
    if rep["by_basis"]:
        print("by basis:")
        for basis, d in sorted(rep["by_basis"].items()):
            print("  %-9s n=%-3d  pl=$%+.2f" % (basis, d["n"], d["pl"]))
    ob = store.open_bets(con)
    if ob:
        print("open positions:")
        for b in ob[:30]:
            print("  #%-4d [%s/%s] %-30s @ %.0f¢  $%.2f"
                  % (b["id"], b["resolution_basis"], b["market_type"],
                     str(b["selection"])[:30], b["entry_price"] * 100, b["stake_usd"]))
    return 0


def cmd_settle(args):
    from wca.testbook import settle as S
    con = store.connect(_DB)
    results = S.load_wc_results(os.path.join(_ROOT, "data", "raw", "martj42_cleaned.csv"))
    # reached=None: advance bets stay open until a stage-reached mapping is wired.
    summ = S.settle_open(con, results, reached=None, ts_utc=_now())
    print("Settled: %s | P&L $%+.2f | %d still unresolved"
          % (summ["settled"], summ["pl"], summ["unresolved"]))
    from wca.testbook import notify
    rep = store.report(con)
    if notify.send(notify.format_settlement(summ, rep)):
        _send_chart(con, rep)
    return cmd_report(args)


def cmd_chart(args):
    """Render the equity/P&L curve and post it to @worldcupdevbot on demand."""
    con = store.connect(_DB)
    ok = _send_chart(con)
    print("Posted P&L chart to @worldcupdevbot." if ok
          else "Chart not sent (no matplotlib, empty book, or no bot credentials).")
    return 0


def cmd_equity(args):
    con = store.connect(_DB)
    print("ts_utc                 kind      balance")
    for r in con.execute("SELECT ts_utc, kind, balance FROM bankroll_events ORDER BY id"):
        print("%-22s %-8s $%.2f" % (r["ts_utc"], r["kind"], r["balance"]))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trade"); t.set_defaults(fn=cmd_trade)
    t.add_argument("--edge", type=float, default=0.04)
    t.add_argument("--kelly", type=float, default=0.5)
    t.add_argument("--max-stake", type=float, default=0.02, dest="max_stake")
    t.add_argument("--min-volume", type=float, default=0.0, dest="min_volume")
    m = sub.add_parser("mark"); m.set_defaults(fn=cmd_mark)
    r = sub.add_parser("report"); r.set_defaults(fn=cmd_report)
    s = sub.add_parser("settle"); s.set_defaults(fn=cmd_settle)
    e = sub.add_parser("equity"); e.set_defaults(fn=cmd_equity)
    d = sub.add_parser("decisions"); d.set_defaults(fn=cmd_decisions)
    c = sub.add_parser("chart"); c.set_defaults(fn=cmd_chart)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
