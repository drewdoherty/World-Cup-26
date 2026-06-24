"""Watch Polymarket limit orders for fills and sync them to the ledger.

Usage::

    python scripts/wca_pm_watch.py [--until 2026-06-13T04:00:00Z] [--interval 900]

Polls the Polymarket data API for the account-1 wallet's exact-score
positions on USA vs Paraguay. When a watched position grows beyond what the
ledger records, the incremental fill is inserted as a new bet row (stake =
incremental cost, odds = 1/fill price) and the site feed is regenerated,
committed, and pushed. Exits at --until (default: well after full time).

Designed to run unattended in the background; progress goes to stdout.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

WALLET = "0x86b4c55a4df1fbea0f325e842434e0a537caa549"
API = "https://data-api.polymarket.com/positions?user=%s&limit=100" % WALLET

# title fragment -> (ledger selection, match_desc, model_prob)
WATCH = {
    "United States 1 - 1 Paraguay": (
        "United States 1-1 Paraguay", "United States vs Paraguay", 0.138),
    "United States 0 - 0 Paraguay": (
        "United States 0-0 Paraguay", "United States vs Paraguay", 0.106),
}


def fetch_positions():
    req = urllib.request.Request(API, headers={"User-Agent": "wca-watch/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ledger_state(con, selection):
    """Return (shares, cost) already recorded for *selection*."""
    rows = con.execute(
        "SELECT stake, decimal_odds FROM bets WHERE platform='polymarket' "
        "AND selection=? AND status NOT IN ('void','cashed')", (selection,)
    ).fetchall()
    shares = sum(r[0] * r[1] for r in rows)
    cost = sum(r[0] for r in rows)
    return shares, cost


def sync_site():
    py = os.path.join(ROOT, ".venv", "bin", "python")
    subprocess.run([py, os.path.join(HERE, "wca_site.py")], cwd=ROOT,
                   check=True, capture_output=True)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    subprocess.run(["git", "add", "site/data.json", "site/linemove.json"],
                   cwd=ROOT, env=env, capture_output=True)
    r = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit",
         "-m", "Auto-sync site: pm_watch fill",
         "--", "site/data.json", "site/linemove.json"],
        cwd=ROOT, env=env, capture_output=True)
    if r.returncode == 0:
        subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       cwd=ROOT, env=env, capture_output=True)
        subprocess.run(["git", "push"], cwd=ROOT, env=env, capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="2026-06-13T04:00:00Z")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "wca.db"))
    args = ap.parse_args()

    until = dt.datetime.strptime(
        args.until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

    while True:
        now = dt.datetime.now(dt.timezone.utc)
        if now >= until:
            print("[%s] deadline reached, exiting" % now.isoformat(timespec="seconds"))
            return

        try:
            positions = fetch_positions()
        except Exception as exc:
            print("[%s] fetch failed: %s" % (now.isoformat(timespec="seconds"), exc))
            positions = []

        con = sqlite3.connect(args.db)
        try:
            for pos in positions:
                title = pos.get("title") or ""
                for frag, (selection, match_desc, model_prob) in WATCH.items():
                    if frag not in title:
                        continue
                    size = float(pos.get("size") or 0.0)
                    avg = float(pos.get("avgPrice") or 0.0)
                    have_shares, have_cost = ledger_state(con, selection)
                    new_shares = size - have_shares
                    if new_shares < 0.5:  # nothing new (allow rounding dust)
                        print("[%s] %s: size %.2f, no new fill"
                              % (now.isoformat(timespec="seconds"), selection, size))
                        continue
                    new_cost = round(size * avg - have_cost, 4)
                    if new_cost <= 0:
                        continue
                    fill_price = new_cost / new_shares
                    odds = 1.0 / fill_price
                    ev = model_prob * odds - 1
                    con.execute(
                        """INSERT INTO bets
                           (ts_utc, match_id, match_desc, market, selection,
                            platform, decimal_odds, stake, status, account,
                            source, model_prob, ev)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (now.strftime("%Y-%m-%dT%H:%M:%S"),
                         "UNITEDSTATES_PARAGUAY", match_desc, "Exact Score",
                         selection, "polymarket", round(odds, 4),
                         round(new_cost, 2), "open", "1", "model",
                         model_prob, round(ev, 4)))
                    con.commit()
                    print("[%s] FILL %s: +%.1f shares @ %.3f ($%.2f) ev %+.1f%%"
                          % (now.isoformat(timespec="seconds"), selection,
                             new_shares, fill_price, new_cost, ev * 100))
                    sync_site()
        finally:
            con.close()

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
