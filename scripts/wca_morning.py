"""Morning briefing: settle overnight results, report movers, build the day.

Designed to run unattended after the overnight matches finish:

1. Pull final scores (Odds API /scores, 2 credits) and AUTO-SETTLE any open
   1X2 / BTTS / moneyline bets whose result is now determined. Player props
   and accas are flagged for manual confirmation, never guessed.
2. Set closing odds (match-filtered consensus at the last pre-KO snapshot)
   on newly settled bets -> CLV.
3. Overnight line movers: consensus implied-probability change per fixture
   between the latest snapshot and ~N hours earlier (default 9h).
4. Build today's card (full model fit) for the next 30h of fixtures.
5. Compose a markdown briefing: P&L by pool, settled results, movers,
   today's recommended bets with venues. Save to docs/reports/, push the
   site, and send the briefing to the admin Telegram chat.

Usage:  wca_morning.py [--hours-back 9] [--no-card] [--no-telegram] [--dry]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))


def _load_dotenv(path: str = ".env") -> None:
    p = _REPO / path
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def fetch_scores(days_from: int = 1) -> List[Dict[str, Any]]:
    """Completed games with final scores from the Odds API (2 credits)."""
    import requests

    url = ("https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/scores"
           "?daysFrom=%d&apiKey=%s" % (days_from, os.environ["ODDS_API_KEY"]))
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    out = []
    for g in resp.json():
        if not g.get("completed"):
            continue
        scores = {s["name"]: int(s["score"]) for s in (g.get("scores") or [])}
        if len(scores) == 2:
            out.append({"home": g["home_team"], "away": g["away_team"],
                        "scores": scores, "id": g.get("id")})
    return out


def _teams_in(text: str, home: str, away: str) -> bool:
    t = (text or "").lower()
    def hit(name: str) -> bool:
        parts = [w for w in name.lower().split() if len(w) > 3] or [name.lower()]
        return any(w in t for w in parts)
    return hit(home) and hit(away)


def settle_from_scores(db_path: str, games: List[Dict[str, Any]],
                       dry: bool = False) -> Tuple[List[str], List[str]]:
    """Settle determinable open bets; return (settled_lines, manual_flags)."""
    import sqlite3

    from wca.ledger.store import settle_bet

    settled: List[str] = []
    manual: List[str] = []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM bets WHERE status='open'").fetchall()
    con.close()
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    for g in games:
        home, away = g["home"], g["away"]
        hs, as_ = g["scores"][home], g["scores"][away]
        outcome = "home" if hs > as_ else ("away" if as_ > hs else "draw")
        winner = home if outcome == "home" else (away if outcome == "away" else None)
        btts_yes = hs > 0 and as_ > 0

        for r in rows:
            if r["status"] != "open" or not _teams_in(r["match_desc"], home, away):
                continue
            market = (r["market"] or "").lower()
            sel = (r["selection"] or "").lower()
            result: Optional[str] = None

            if "btts" in market or "btts" in sel:
                want_no = "no" in sel
                result = "won" if (btts_yes != want_no) else "lost"
            elif market in ("h2h", "pm_moneyline") or "moneyline" in market:
                if "draw" in sel:
                    result = "won" if outcome == "draw" else "lost"
                elif winner and _teams_in(sel, winner, winner):
                    result = "won"
                elif any(w in sel for w in (home.lower().split() + away.lower().split()) if len(w) > 3):
                    result = "lost"
            elif "acca" in market or "treble" in (r["match_desc"] or "").lower():
                # A losing leg settles the whole acca; a winning leg leaves it open.
                leg_won = winner is not None and _teams_in(sel, winner, winner)
                if not leg_won and any(
                        w in sel for w in (home.lower() + " " + away.lower()).split() if len(w) > 3):
                    result = "lost"
            else:
                manual.append("#%d %s (%s) — player/prop market, confirm manually"
                              % (r["id"], r["selection"], r["match_desc"][:30]))
                continue

            if result:
                line = "#%d %s — %s %d-%d -> %s" % (
                    r["id"], r["selection"][:30], home, hs, as_, result.upper())
                if not dry:
                    try:
                        settle_bet(r["id"], result, db_path=db_path, settled_ts_utc=now)
                    except Exception as exc:  # already settled / race
                        line += " (skipped: %s)" % exc
                settled.append(line)
    return settled, manual


def set_closing_for_match(db_path: str, home_kw: str, ko_iso: str,
                          bet_ids: List[int], selection: str) -> Optional[str]:
    """Match-filtered consensus close for a selection -> set on bet ids."""
    import sqlite3

    from wca.ledger.store import set_closing_odds
    from wca.linemove import robust_event_meta

    meta = robust_event_meta(str(_REPO / "data" / "raw" / "snapshots"))
    mid = next((m for m, v in meta.items() if home_kw.lower() in v["fixture"].lower()), None)
    if not mid:
        return None
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "select ts_utc from odds_snapshots where market='h2h' and match_id=? "
        "and ts_utc < ? order by ts_utc desc limit 1", (mid, ko_iso)).fetchone()
    if not row:
        con.close()
        return None
    prices = sorted(r["decimal_odds"] for r in con.execute(
        "select decimal_odds from odds_snapshots where ts_utc=? and match_id=? "
        "and market='h2h' and selection=?", (row["ts_utc"], mid, selection)))
    con.close()
    if not prices:
        return None
    close = prices[len(prices) // 2]
    for b in bet_ids:
        try:
            set_closing_odds(b, close, db_path=db_path)
        except Exception:
            pass
    return "%s close %.3f (ts %s, %d books)" % (selection, close, row["ts_utc"][:16], len(prices))


def overnight_movers(db_path: str, hours_back: float = 9.0,
                     top_n: int = 10) -> List[str]:
    """Largest consensus implied-prob moves per fixture over the window."""
    import sqlite3

    from wca.linemove import robust_event_meta

    meta = robust_event_meta(str(_REPO / "data" / "raw" / "snapshots"))
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    latest = con.execute("select max(ts_utc) m from odds_snapshots").fetchone()["m"]
    if not latest:
        con.close()
        return []
    cutoff = (dt.datetime.fromisoformat(latest[:19]) -
              dt.timedelta(hours=hours_back)).isoformat()
    early = con.execute(
        "select min(ts_utc) m from odds_snapshots where ts_utc >= ?", (cutoff,)
    ).fetchone()["m"]

    def consensus(ts: str) -> Dict[Tuple[str, str], float]:
        acc: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        for r in con.execute(
                "select match_id, selection, decimal_odds from odds_snapshots "
                "where ts_utc=? and market='h2h'", (ts,)):
            if r["decimal_odds"] and r["decimal_odds"] > 1:
                acc[(r["match_id"], r["selection"])].append(1.0 / r["decimal_odds"])
        return {k: sorted(v)[len(v) // 2] for k, v in acc.items() if v}

    c_now, c_then = consensus(latest), consensus(early)
    con.close()
    moves = []
    for key, now_p in c_now.items():
        then_p = c_then.get(key)
        if then_p is None:
            continue
        delta = now_p - then_p
        if abs(delta) >= 0.005:
            mid, sel = key
            fixture = meta.get(mid, {}).get("fixture", mid[:12])
            moves.append((abs(delta), "%-34s %-18s %5.1f%% -> %5.1f%% (%+.1fpp)"
                          % (fixture[:34], sel[:18], then_p * 100, now_p * 100, delta * 100)))
    moves.sort(reverse=True)
    return [m[1] for m in moves[:top_n]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours-back", type=float, default=9.0)
    ap.add_argument("--no-card", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    _load_dotenv()
    os.chdir(_REPO)
    db = "data/wca.db"

    lines: List[str] = []
    lines.append("☀️ *WCA morning briefing* — %s UTC"
                 % dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"))

    # 1. Scores + settlement
    try:
        games = fetch_scores()
        if games:
            lines.append("")
            lines.append("*Final scores (last 24h)*")
            for g in games:
                lines.append("  %s %d-%d %s" % (
                    g["home"], g["scores"][g["home"]],
                    g["scores"][g["away"]], g["away"]))
            settled, manual = settle_from_scores(db, games, dry=args.dry)
            if settled:
                lines.append("")
                lines.append("*Auto-settled*")
                lines.extend("  " + s for s in settled)
            if manual:
                lines.append("")
                lines.append("*Needs your confirmation (props/accas)*")
                lines.extend("  " + m for m in manual)
            # CLV close for the Korea fixture if present
            note = set_closing_for_match(db, "South Korea vs Czech", "2026-06-12T02:00",
                                         [8, 10], "South Korea")
            if note:
                lines.append("  closing: " + note)
    except Exception as exc:
        lines.append("(scores/settlement failed: %s)" % exc)

    # 2. P&L by pool
    try:
        import wca.bot.app as app
        lines.append("")
        lines.append(app.handle_summary(db))
    except Exception as exc:
        lines.append("(summary failed: %s)" % exc)

    # 3. Movers
    try:
        movers = overnight_movers(db, hours_back=args.hours_back)
        lines.append("")
        lines.append("*Overnight line movers* (last %.0fh)" % args.hours_back)
        if movers:
            lines.append("```")
            lines.extend(movers)
            lines.append("```")
        else:
            lines.append("  (no meaningful moves)")
    except Exception as exc:
        lines.append("(movers failed: %s)" % exc)

    # 4. Today's card
    if not args.no_card:
        try:
            import subprocess
            r = subprocess.run(
                [str(_REPO / ".venv/bin/python"), "scripts/wca_build_card.py",
                 "--hours-ahead", "30"],
                capture_output=True, text=True, timeout=900)
            from wca import cardcache
            cached = cardcache.read_card("data/card_latest.md")
            if cached and cached.get("text"):
                lines.append("")
                lines.append("*Today's card*")
                lines.append(cached["text"])
        except Exception as exc:
            lines.append("(card build failed: %s)" % exc)

    report = "\n".join(lines)

    # Persist + publish
    out_dir = _REPO / "docs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ("morning_%s.md" % dt.datetime.utcnow().strftime("%Y%m%d"))
    out_path.write_text(report)
    print(report)

    if not args.dry:
        try:
            from wca import sync
            sync.push_site(reason="morning briefing", db_path=db)
            import subprocess
            subprocess.run(["git", "-C", str(_REPO), "add", "docs/reports", "data/card_latest.md"],
                           capture_output=True, timeout=30)
            subprocess.run(["git", "-C", str(_REPO), "commit", "-q", "-m",
                            "Morning briefing %s" % dt.date.today().isoformat(),
                            "--no-verify"], capture_output=True, timeout=30)
            subprocess.run(["git", "-C", str(_REPO), "push", "-q"],
                           capture_output=True, timeout=60)
        except Exception as exc:
            print("(publish failed: %s)" % exc, file=sys.stderr)

    if not args.no_telegram:
        try:
            from wca.bot.telegram import TelegramClient
            chat = (os.environ.get("TELEGRAM_ADMIN_USER_ID")
                    or (os.environ.get("TELEGRAM_CHAT_ID") or "").split(",")[0])
            if chat:
                TelegramClient().send_message(chat, report)
                print("briefing sent to telegram")
        except Exception as exc:
            print("(telegram send failed: %s)" % exc, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
