#!/usr/bin/env python
"""Reconcile the bets ledger against the LIVE Polymarket account(s).

The ``bets`` ledger drifts from reality: positions placed manually in the PM app
(or that miss a write) never land as rows, and resolved markets linger as
``status='open'``. This tool diffs the live Polymarket **Data API** positions
(public, read-only — no keys, no fund movement) for the configured proxy wallets
against ``bets WHERE platform='polymarket' AND status='open'`` and proposes:

* **inserts** — positions live on-chain but missing from the ledger. Recorded
  with ``stake = current value`` and ``decimal_odds = 1/current_price`` so the
  whole-book *exposure* engine counts them immediately; the ``notes`` flag the
  entry cost as APPROXIMATE (real cost pending) and the ``token_id`` is stored
  for reliable future matching. P&L on these stays approximate until corrected.
* **closes** — ledger rows marked open but NOT live on-chain. Settled by a
  general rule: in the redeemable set -> **won**; an unparseable/malformed
  selection -> **void**; otherwise resolved worthless -> **lost**.

Modes (NEVER places or moves funds):
    (default)   dry-run: print the plan, write nothing.
    --apply     write the plan to the DB (use --db for the canonical ledger).
    --check     drift check only: exit 2 if live != ledger (for an hourly alert).

Usage:
    PYTHONPATH=src python3 scripts/wca_pm_reconcile.py            # dry-run
    PYTHONPATH=src python3 scripts/wca_pm_reconcile.py --apply --db data/wca.db
    PYTHONPATH=src python3 scripts/wca_pm_reconcile.py --check    # for cron/launchd
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

# Configured Polymarket proxy wallets (read-only Data API lookups).
WALLETS = {
    "PM1": "0x86b4c55a4df1fbea0f325e842434e0a537caa549",  # vl880 (primary)
    "PM2": "0xd42e35059b0615c4c7a9cf7db5427b313ebb7b31",  # World-Cup-26
}
DATA_API = "https://data-api.polymarket.com/positions"
_SIZE_FLOOR = 1.0   # ignore sub-1-share dust positions

_TEAM_FIX = {"united states": "usa"}


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_env(path: str) -> None:
    """Minimal KEY=VALUE .env loader into os.environ (no overwrite of set vars)."""
    if not path or not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _notify_drift(plan: dict) -> None:
    """Best-effort Telegram DM to the admin when drift is detected (for --check)."""
    try:
        from wca.bot.telegram import TelegramClient
        admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
        if not admin:
            return
        body = ("⚠️ *PM ledger drift* — live=%d vs ledger_open=%d\n"
                "%d missing (insert), %d stale (close). Run `wca_pm_reconcile.py --apply` "
                "on the mini after a backup." % (
                    plan["n_live"], plan["n_ledger_open"],
                    len(plan["inserts"]), len(plan["closes"])))
        TelegramClient().send_message(admin, body)
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        print("notify failed: %s" % exc, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Normalisation (pure — unit-tested)
# --------------------------------------------------------------------------- #


def norm_type(text: str) -> str:
    t = (text or "").lower()
    if "reach the round of 16" in t or "_reach_r16" in t or "reach the r16" in t:
        return "r16"
    if "reach the quarterfinal" in t or "_reach_qf" in t:
        return "qf"
    if "reach the semifinal" in t or "_reach_sf" in t:
        return "sf"
    if "advance to the knockout" in t or "_advancement" in t:
        return "advance"
    if "win the 2026 fifa world cup" in t or "win the world cup" in t or "_outright_winner" in t:
        return "win_wc"
    if "be eliminated" in t or "_elimination" in t:
        return "elim"
    if "end in a draw" in t or "_match_draw" in t:
        return "draw"
    if "group" in t and "winner" in t:
        return "group_winner"
    if re.search(r"win on \d{4}-\d\d-\d\d", t) or "_match_winner" in t or "moneyline" in t:
        return "match_win"
    return "other"


def norm_subject(text: str) -> str:
    """Lowercase team/subject key from a market title or ledger selection."""
    t = (text or "").lower().strip()
    t = re.sub(r"^will\s+", "", t)
    for marker in (" reach the", " advance to", " win the 2026", " win the world",
                   " be eliminated", " win on", " vs.", " vs ", " - ", "?"):
        i = t.find(marker)
        if i > 0:
            t = t[:i]
            break
    t = t.strip()
    return _TEAM_FIX.get(t, t)


def is_malformed(selection: str) -> bool:
    """A selection with no recognisable subject (e.g. bare 'Yes'/'No')."""
    return norm_subject(selection) in ("", "yes", "no")


def key_of(subject_text: str, mtype: str, outcome: str) -> str:
    return "%s|%s|%s" % (norm_subject(subject_text), mtype, (outcome or "").strip().lower())


# --------------------------------------------------------------------------- #
# Live account + ledger
# --------------------------------------------------------------------------- #


def _fetch(wallet: str, redeemable) -> list:
    url = "%s?user=%s&limit=500&sizeThreshold=0.1" % (DATA_API, wallet)
    if redeemable is not None:
        url += "&redeemable=%s" % ("true" if redeemable else "false")
    req = urllib.request.Request(url, headers={"User-Agent": "wca-pm-reconcile"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def load_live(wallets: dict):
    """Return (open_by_key, redeemable_keys). open excludes resolved/redeemable."""
    open_by_key, redeem_keys = {}, set()
    for name, w in wallets.items():
        for p in _fetch(w, redeemable=False):
            title, outcome = p.get("title") or "", p.get("outcome") or ""
            size = float(p.get("size") or 0)
            price = float(p.get("curPrice") or p.get("price") or 0)
            if not title or size < _SIZE_FLOOR:
                continue
            mtype = norm_type(title)
            open_by_key[key_of(title, mtype, outcome)] = {
                "wallet": name, "title": title, "outcome": outcome, "type": mtype,
                "shares": round(size, 4), "price": round(price, 4),
                "value": round(size * price, 2),
                "token_id": p.get("asset") or p.get("conditionId") or "",
            }
        for p in _fetch(w, redeemable=True):
            title, outcome = p.get("title") or "", p.get("outcome") or ""
            if title:
                redeem_keys.add(key_of(title, norm_type(title), outcome))
    return open_by_key, redeem_keys


def load_ledger_open(con: sqlite3.Connection) -> dict:
    out = {}
    for bid, market, sel, odds, stake in con.execute(
            "SELECT id, market, selection, decimal_odds, stake FROM bets "
            "WHERE platform='polymarket' AND status='open'"):
        outcome = "no" if (sel or "").strip().lower().endswith("- no") else "yes"
        k = key_of(sel, norm_type(market + " " + sel), outcome)
        out[k] = {"id": bid, "market": market, "sel": sel, "odds": odds, "stake": stake, "key": k}
    return out


def build_plan(live_open: dict, redeem_keys: set, ledger_open: dict) -> dict:
    live_k, led_k = set(live_open), set(ledger_open)
    inserts = [live_open[k] for k in sorted(live_k - led_k)]
    closes = []
    for k in sorted(led_k - live_k):
        b = ledger_open[k]
        if is_malformed(b["sel"]):
            action, pl = "void", 0.0
        elif k in redeem_keys:
            action, pl = "won", round(b["stake"] * (b["odds"] - 1.0), 2)
        else:
            action, pl = "lost", round(-b["stake"], 2)
        closes.append({**b, "action": action, "settled_pl": pl})
    matched = sorted(live_k & led_k)
    return {"inserts": inserts, "closes": closes, "n_matched": len(matched),
            "n_live": len(live_open), "n_ledger_open": len(ledger_open)}


# --------------------------------------------------------------------------- #
# Render + apply
# --------------------------------------------------------------------------- #


def render(plan: dict) -> None:
    print("=" * 74)
    print("PM RECONCILE  live=%d  ledger_open=%d  matched=%d  ->  insert=%d  close=%d"
          % (plan["n_live"], plan["n_ledger_open"], plan["n_matched"],
             len(plan["inserts"]), len(plan["closes"])))
    print("=" * 74)
    print("\nINSERTS (live on-chain, missing from ledger) — stake = CURRENT VALUE (entry cost APPROX):")
    tot = 0.0
    for p in plan["inserts"]:
        tot += p["value"]
        print("  %-4s %-10s %-46s %-3s shares=%8.1f price=%.3f value=$%.2f"
              % (p["wallet"], p["type"], p["title"][:46], p["outcome"], p["shares"], p["price"], p["value"]))
    print("  => %d inserts, ~$%.2f exposure" % (len(plan["inserts"]), tot))
    print("\nCLOSES (ledger 'open' but not live on-chain):")
    for c in plan["closes"]:
        print("  id=%-5s %-16s %-40s -> %-4s settled_pl=%+.2f"
              % (c["id"], c["market"], c["sel"][:40], c["action"].upper(), c["settled_pl"]))


def apply_plan(con: sqlite3.Connection, plan: dict, now: str) -> dict:
    cur = con.cursor()
    cur.execute("BEGIN IMMEDIATE")
    ins_ids, closed = [], []
    for p in plan["inserts"]:
        odds = round(1.0 / p["price"], 4) if p["price"] > 0 else 0.0
        note = ("RECONCILED on-chain %s; wallet=%s shares=%s cur_price=%s cur_value=%s; "
                "ENTRY COST APPROX (=cur_value), real cost PENDING"
                % (now[:10], p["wallet"], p["shares"], p["price"], p["value"]))
        cur.execute(
            "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, platform, "
            "decimal_odds, stake, status, source, account, token_id, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, "pm_recon", p["title"], "pm_" + p["type"], p["title"] + " - " + p["outcome"],
             "polymarket", odds, p["value"], "open", "punt", "1", p["token_id"], note))
        ins_ids.append(cur.lastrowid)
    for c in plan["closes"]:
        tag = "[%s via pm-reconcile %s: %s]" % (
            c["action"], now[:10],
            "on-chain redeemable" if c["action"] == "won"
            else ("malformed dup" if c["action"] == "void" else "resolved $0 on-chain"))
        cur.execute(
            "UPDATE bets SET status=?, settled_pl=?, settled_ts=?, "
            "notes=COALESCE(notes,'')||' '||? WHERE id=?",
            (c["action"], c["settled_pl"], now, tag, c["id"]))
        closed.append((c["id"], c["action"], c["settled_pl"]))
    con.commit()
    return {"inserted_ids": ins_ids, "closed": closed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/wca.db", help="ledger DB")
    ap.add_argument("--apply", action="store_true", help="write the plan (default: dry-run)")
    ap.add_argument("--check", action="store_true", help="drift check only; exit 2 on drift")
    ap.add_argument("--env", default=None, help="env file to load (for --notify creds)")
    ap.add_argument("--notify", action="store_true", help="DM the admin on drift (with --check)")
    args = ap.parse_args(argv)

    if args.env:
        _load_env(args.env)
    live_open, redeem = load_live(WALLETS)
    mode = "ro" if not args.apply else "rw"
    uri = "file:%s?mode=%s" % (os.path.abspath(args.db), "ro" if mode == "ro" else "rwc")
    con = sqlite3.connect(uri if mode == "ro" else os.path.abspath(args.db), uri=(mode == "ro"))
    try:
        ledger_open = load_ledger_open(con)
        plan = build_plan(live_open, redeem, ledger_open)

        if args.check:
            drift = len(plan["inserts"]) + len(plan["closes"])
            print("DRIFT: %d (inserts=%d closes=%d) | live=%d ledger_open=%d | %s"
                  % (drift, len(plan["inserts"]), len(plan["closes"]),
                     plan["n_live"], plan["n_ledger_open"], _now_z()))
            if drift and args.notify:
                _notify_drift(plan)
            return 2 if drift else 0

        render(plan)
        if not args.apply:
            print("\nDRY-RUN — nothing written. Re-run with --apply --db <canonical> to commit.")
            return 0
        res = apply_plan(con, plan, _now_z())
        print("\nAPPLIED: inserted ids %s" % res["inserted_ids"])
        for cid, act, pl in res["closed"]:
            print("  closed id=%s -> %s (pl=%+.2f)" % (cid, act, pl))
        print("post-state open PM:",
              con.execute("SELECT count(*) FROM bets WHERE platform='polymarket' AND status='open'").fetchone()[0])
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
