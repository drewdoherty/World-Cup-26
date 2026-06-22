#!/usr/bin/env python
"""One-time ledger audit & repair — run on the host with the CANONICAL ledger.

Three independent, conservative passes over the ``bets`` ledger:

1. SETTLE concluded matches still marked ``open``. Only **1X2 / match-result**
   markets are graded automatically (from ``data/raw/results.csv``); every
   other market (bet-builders, accas, correct-score, props, FGS, corners,
   2-Up, Polymarket advancement) is left open and printed as a MANUAL worklist
   with the final score, so a human can settle it via ``scripts/wca_settle.py``.
2. BACKFILL blank ``match_desc`` from the ``odds_snapshots`` fixture metadata.
3. BACKFILL ``closing_odds`` + ``clv`` where missing — open 1X2 bets via the
   supported ``closecapture`` path, settled 1X2 bets best-effort from the same
   de-vigged consensus close. (Only touches the CLV analytic field, never P&L.)

SAFETY
------
* **Dry-run by default** — prints what it WOULD do and changes nothing.
* ``--apply`` performs the writes, after taking a consistent ``.db`` backup.
* Every bet is handled in its own try/except — one bad row never aborts the run.
* Auto-settlement is restricted to unambiguous 1X2 results; ambiguous pairings
  (rematches with two results) are skipped to the manual worklist.

Reuses ``wca.ledger.store`` (settle_bet/void_bet/set_closing_odds),
``wca.closecapture`` (consensus close) and ``wca.data.results`` — no bespoke
P&L or de-vig maths.
"""

from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wca import closecapture, tracking  # noqa: E402
from wca.data.results import add_outcome_column, load_results  # noqa: E402
from wca.ledger import store  # noqa: E402

# Markets we will NOT auto-settle (kept here only for the report label).
_AUTO_NOTE = "1X2/match-result only; all other markets -> manual"


def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


# -- results lookup -------------------------------------------------------


def build_results_lookup(
    results_path: str, since: str = "2026-06-01"
) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
    """``(home_canon, away_canon) -> [(date_iso, "H-A"), ...]`` for concluded games.

    ``since`` (ISO date) excludes pre-tournament history. results.csv holds 100+
    years of internationals, so without this an unplayed WC fixture (e.g.
    England vs Ghana, 2026-06-23) would false-match a historical friendly
    (England 1-1 Ghana, 2011) and wrongly settle the bet. Pass since="" to
    disable (not recommended).
    """
    import pandas as pd

    df = add_outcome_column(load_results(results_path))
    df = df[df["outcome"].notna()]
    if since:
        df = df[df["date"] >= pd.Timestamp(since)]
    lut: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for _, r in df.iterrows():
        key = (str(r["home_team"]).strip().casefold(), str(r["away_team"]).strip().casefold())
        score = "%d-%d" % (int(r["home_score"]), int(r["away_score"]))
        date = str(r["date"])[:10]
        lut.setdefault(key, []).append((date, score))
    return lut


def result_for(match_desc: Optional[str], lut: Dict[Tuple[str, str], List[Tuple[str, str]]]
               ) -> Optional[Tuple[str, str, str]]:
    """Return ``(home, away, score)`` for a fixture, or None if absent/ambiguous."""
    pair = tracking.split_fixture(match_desc or "")
    if not pair:
        return None
    home, away = pair
    hits = lut.get((home.strip().casefold(), away.strip().casefold()))
    if not hits:
        return None
    if len({s for _, s in hits}) > 1:
        return None  # rematch with differing scores -> ambiguous, settle manually
    return home, away, hits[0][1]


def grade_1x2(selection: str, home: str, away: str, score: str) -> Optional[str]:
    """'won'/'lost' for a 1X2-style selection given the final score, else None."""
    leg_isno = closecapture.selection_leg(selection, home, away)
    outcome = tracking.outcome_from_score(score)
    if leg_isno is None or outcome is None:
        return None
    leg, is_no = leg_isno
    won = (outcome == leg) ^ is_no
    return "won" if won else "lost"


# -- backup ---------------------------------------------------------------


def backup_db(db_path: str, backup_dir: str) -> Path:
    """Consistent online backup of the SQLite ledger via the backup API."""
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    dst = Path(backup_dir) / ("wca_audit_%s.db" % datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"))
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(str(dst))
    try:
        with out:
            src.backup(out)
    finally:
        src.close()
        out.close()
    return dst


# -- passes ---------------------------------------------------------------


def pass_settle(con, db_path, lut, apply, log):
    """Settle concluded open 1X2 bets; collect a manual worklist for the rest."""
    settled, manual = [], []
    rows = con.execute(
        "SELECT id, match_desc, market, selection, decimal_odds, stake, status "
        "FROM bets WHERE status='open' "
        "AND (manual_override IS NULL OR TRIM(manual_override)='')"
    ).fetchall()
    for r in rows:
        res = result_for(r["match_desc"], lut)
        if res is None:
            continue  # match not concluded (or unknown / ambiguous) -> leave open
        home, away, score = res
        if closecapture.is_1x2_market(r["market"]):
            graded = grade_1x2(r["selection"], home, away, score)
            if graded is not None:
                log("  settle #%d %s | %s -> %s (%s %s)" % (
                    r["id"], r["match_desc"], r["selection"], graded.upper(), score,
                    "(APPLY)" if apply else "(dry-run)"))
                if apply:
                    store.settle_bet(r["id"], graded, db_path=db_path)
                settled.append((r["id"], graded))
                continue
        manual.append((r["id"], r["match_desc"], r["market"], r["selection"], score))
    return settled, manual


def pass_match_desc(con, apply, log):
    """Backfill blank match_desc from odds_snapshots fixture metadata."""
    index = closecapture.match_index(con)
    fixed = []
    rows = con.execute(
        "SELECT id, match_id FROM bets "
        "WHERE (match_desc IS NULL OR TRIM(match_desc)='') "
        "AND (manual_override IS NULL OR TRIM(manual_override)='')"
    ).fetchall()
    for r in rows:
        meta = index.get(str(r["match_id"]))
        if not meta:
            continue
        desc = "%s vs %s" % (meta["home"], meta["away"])
        log("  match_desc #%d -> %r %s" % (r["id"], desc, "(APPLY)" if apply else "(dry-run)"))
        if apply:
            con.execute("UPDATE bets SET match_desc=? WHERE id=?", (desc, r["id"]))
        fixed.append((r["id"], desc))
    if apply and fixed:
        con.commit()
    return fixed


def pass_closes(con, db_path, lut, apply, log):
    """Backfill closing_odds+clv: open 1X2 via closecapture, settled 1X2 best-effort."""
    # Open 1X2 bets — supported path.
    open_done = closecapture.capture_closes_db(db_path, now_utc=_now_iso(), dry_run=not apply)
    for rec in open_done:
        log("  close(open) #%s %s clv=%.4f %s" % (
            rec.get("bet_id"), rec.get("selection"), rec.get("clv", 0.0),
            "(APPLY)" if apply else "(dry-run)"))

    # Settled 1X2 bets missing closing_odds — best-effort from the same consensus.
    index = closecapture.match_index(con)
    by_pair = {}
    for sid, m in index.items():
        by_pair[(closecapture._canon(m["home"]), closecapture._canon(m["away"]))] = (sid, m)
    settled_done = []
    rows = con.execute(
        "SELECT id, match_desc, market, selection FROM bets "
        "WHERE status IN ('won','lost') AND closing_odds IS NULL "
        "AND (manual_override IS NULL OR TRIM(manual_override)='')"
    ).fetchall()
    for r in rows:
        if not closecapture.is_1x2_market(r["market"]):
            continue
        pair = tracking.split_fixture(r["match_desc"] or "")
        if not pair:
            continue
        hit = by_pair.get((closecapture._canon(pair[0]), closecapture._canon(pair[1])))
        if not hit:
            continue
        sid, meta = hit
        cc = closecapture.consensus_close(con, sid, meta["home"], meta["away"], meta["kickoff"])
        if not cc:
            continue
        leg_isno = closecapture.selection_leg(r["selection"], meta["home"], meta["away"])
        if leg_isno is None:
            continue
        close = closecapture.fair_closing_odds(cc["triple"], leg_isno[0], leg_isno[1])
        if close is None:
            continue
        log("  close(settled) #%d %s -> %.3f %s" % (
            r["id"], r["selection"], close, "(APPLY)" if apply else "(dry-run)"))
        if apply:
            store.set_closing_odds(r["id"], close, db_path=db_path)
        settled_done.append((r["id"], close))
    return open_done, settled_done


# -- main -----------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="One-time ledger audit & repair (dry-run by default)")
    p.add_argument("--db", default="data/wca.db", help="canonical ledger (run on the mini)")
    p.add_argument("--results", default="data/raw/results.csv", help="concluded-match results CSV")
    p.add_argument("--apply", action="store_true", help="write changes (backs up the DB first)")
    p.add_argument("--backup-dir", default="data/backups", help="where to write the pre-apply backup")
    p.add_argument("--skip-closes", action="store_true", help="skip the closing_odds/clv backfill pass")
    p.add_argument("--since", default="2026-06-01",
                   help="ignore results before this date (excludes pre-tournament history)")
    args = p.parse_args(argv)

    def log(msg: str) -> None:
        print(msg, flush=True)

    mode = "APPLY (writing)" if args.apply else "DRY-RUN (no changes)"
    log("== WCA ledger audit — %s ==" % mode)
    log("db=%s  results=%s" % (args.db, args.results))

    if args.apply:
        bak = backup_db(args.db, args.backup_dir)
        log("backup -> %s" % bak)

    log("results window: matches on/after %s only" % (args.since or "(all history)"))
    lut = build_results_lookup(args.results, since=args.since)
    con = store._connect(args.db)
    try:
        log("\n[1] settle concluded open bets (%s)" % _AUTO_NOTE)
        settled, manual = pass_settle(con, args.db, lut, args.apply, log)

        log("\n[2] backfill blank match_desc")
        fixed = pass_match_desc(con, args.apply, log)

        open_closes, settled_closes = ([], [])
        if not args.skip_closes:
            log("\n[3] backfill closing_odds/clv")
            open_closes, settled_closes = pass_closes(con, args.db, lut, args.apply, log)
    finally:
        con.close()

    log("\n== summary ==")
    log("  auto-settled 1X2 bets : %d" % len(settled))
    log("  match_desc backfilled : %d" % len(fixed))
    log("  closes backfilled     : %d open + %d settled" % (len(open_closes), len(settled_closes)))
    log("  MANUAL settle worklist: %d" % len(manual))
    for bid, desc, market, sel, score in manual:
        log("    - #%d  %s  [%s]  %r  final=%s  -> wca_settle.py --bet-id %d --outcome won|lost|void"
            % (bid, desc, market, sel, score, bid))
    if not args.apply:
        log("\nDRY-RUN only. Re-run with --apply to write (a backup is taken first).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
