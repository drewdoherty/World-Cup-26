#!/usr/bin/env python
"""Generate the World Cup 2026 calendar (.ics) with the FULL knockout bracket,
auto-filling teams as rounds resolve.

Design (so re-runs UPDATE the calendar in place, never duplicate):
* Every knockout slot has a STABLE UID keyed on its FIFA match number
  (``wc2026-m73@worldcupalpha.com`` … ``wc2026-m104``). Apple Calendar matches on
  UID, so regenerating after each result updates the same event.
* Knockout date/time/venue are the FIXED published FIFA slots (embedded, verified
  three ways: Wikipedia + ESPN + openfootball/worldcup.json, cross-checked vs the
  codebase bracket sim ``src/wca/sim/tournament2026.py``). Only the TEAM NAMES
  change as the tournament progresses.
* R32 teams are the now-determined matchups (group stage complete). R16→Final
  show descriptive placeholders ("Winner M74 vs Winner M77") until each feeding
  match is played; pass --results to auto-fill winners from a results CSV.
* The existing group-stage events are preserved verbatim; only the broken
  knockout section (the 2 bogus R32 rows + the wrong-time Final) is replaced.

Times are Asia/Bahrain (UTC+3, no DST). Usage:
    python3 scripts/gen_wc_calendar.py \
        [--ics World_Cup_2026_Calendar_Bahrain.ics] \
        [--results data/raw/martj42_cleaned.csv]   # optional, fills R16+ winners
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from datetime import datetime, timedelta

# --- Knockout slot grid: M# -> (round, "YYYYMMDDTHHMMSS" Bahrain, venue, city) ---
# FIFA-fixed slots. M92 kickoff flagged for confirmation (sources split 01:00 vs
# 03:00 Bahrain); using 03:00 and noting it in the event body.
SCHEDULE = {
    73: ("R32", "20260628T220000", "SoFi Stadium", "Inglewood, Los Angeles"),
    76: ("R32", "20260629T200000", "NRG Stadium", "Houston"),
    74: ("R32", "20260629T233000", "Gillette Stadium", "Foxborough"),
    75: ("R32", "20260630T040000", "Estadio BBVA", "Guadalupe, Monterrey"),
    78: ("R32", "20260630T200000", "AT&T Stadium", "Arlington, Dallas"),
    77: ("R32", "20260701T000000", "MetLife Stadium", "East Rutherford, NJ"),
    79: ("R32", "20260701T040000", "Estadio Azteca", "Mexico City"),
    80: ("R32", "20260701T190000", "Mercedes-Benz Stadium", "Atlanta"),
    82: ("R32", "20260701T230000", "Lumen Field", "Seattle"),
    81: ("R32", "20260702T030000", "Levi's Stadium", "Santa Clara, SF Bay"),
    84: ("R32", "20260702T220000", "SoFi Stadium", "Inglewood, Los Angeles"),
    83: ("R32", "20260703T020000", "BMO Field", "Toronto"),
    85: ("R32", "20260703T060000", "BC Place", "Vancouver"),
    88: ("R32", "20260703T210000", "AT&T Stadium", "Arlington, Dallas"),
    86: ("R32", "20260704T010000", "Hard Rock Stadium", "Miami Gardens"),
    87: ("R32", "20260704T043000", "Arrowhead Stadium", "Kansas City"),
    90: ("R16", "20260704T200000", "NRG Stadium", "Houston"),
    89: ("R16", "20260705T000000", "Lincoln Financial Field", "Philadelphia"),
    91: ("R16", "20260705T230000", "MetLife Stadium", "East Rutherford, NJ"),
    92: ("R16", "20260706T030000", "Estadio Azteca", "Mexico City"),
    93: ("R16", "20260706T220000", "AT&T Stadium", "Arlington, Dallas"),
    94: ("R16", "20260707T030000", "Lumen Field", "Seattle"),
    95: ("R16", "20260707T190000", "Mercedes-Benz Stadium", "Atlanta"),
    96: ("R16", "20260707T230000", "BC Place", "Vancouver"),
    97: ("QF", "20260709T230000", "Gillette Stadium", "Foxborough"),
    98: ("QF", "20260710T220000", "SoFi Stadium", "Inglewood, Los Angeles"),
    99: ("QF", "20260712T000000", "Hard Rock Stadium", "Miami Gardens"),
    100: ("QF", "20260712T040000", "Arrowhead Stadium", "Kansas City"),
    101: ("SF", "20260714T220000", "AT&T Stadium", "Arlington, Dallas"),
    102: ("SF", "20260715T220000", "Mercedes-Benz Stadium", "Atlanta"),
    103: ("3P", "20260719T000000", "Hard Rock Stadium", "Miami Gardens"),
    104: ("F", "20260719T220000", "MetLife Stadium", "East Rutherford, NJ"),
}

# R32 matchups — determined (group stage complete; verified vs codebase sim).
R32_TEAMS = {
    73: ("South Africa", "Canada"), 74: ("Germany", "Paraguay"),
    75: ("Netherlands", "Morocco"), 76: ("Brazil", "Japan"),
    77: ("France", "Sweden"), 78: ("Ivory Coast", "Norway"),
    79: ("Mexico", "Ecuador"), 80: ("England", "DR Congo"),
    81: ("USA", "Bosnia & Herzegovina"), 82: ("Belgium", "Senegal"),
    83: ("Portugal", "Croatia"), 84: ("Spain", "Austria"),
    85: ("Switzerland", "Algeria"), 86: ("Argentina", "Cape Verde"),
    87: ("Colombia", "Ghana"), 88: ("Australia", "Egypt"),
}

# Knockout feed tree: M# -> (("W"|"L", src_M), ("W"|"L", src_M)).
FEED = {
    89: (("W", 74), ("W", 77)), 90: (("W", 73), ("W", 75)),
    91: (("W", 76), ("W", 78)), 92: (("W", 79), ("W", 80)),
    93: (("W", 83), ("W", 84)), 94: (("W", 81), ("W", 82)),
    95: (("W", 86), ("W", 88)), 96: (("W", 85), ("W", 87)),
    97: (("W", 89), ("W", 90)), 98: (("W", 93), ("W", 94)),
    99: (("W", 91), ("W", 92)), 100: (("W", 95), ("W", 96)),
    101: (("W", 97), ("W", 98)), 102: (("W", 99), ("W", 100)),
    103: (("L", 101), ("L", 102)), 104: (("W", 101), ("W", 102)),
}

ROUND_LABEL = {"R32": "Round of 32", "R16": "Round of 16", "QF": "Quarter-final",
               "SF": "Semi-final", "3P": "Third-place Play-off", "F": "FINAL"}


def _canon(name: str) -> str:
    n = (name or "").strip().lower()
    aliases = {"united states": "usa", "usa": "usa", "bosnia and herzegovina": "bosnia & herzegovina",
               "korea republic": "south korea", "czechia": "czech republic",
               "cote d'ivoire": "ivory coast", "côte d'ivoire": "ivory coast"}
    return aliases.get(n, n)


def load_shootouts(path):
    """Map frozenset{canon teamA, canon teamB} -> pens winner, knockout era.

    A 90-minute draw in a knockout tie goes to extra time then penalties;
    the base results CSV only carries the 90-min score, so a drawn knockout
    match is otherwise unresolved. This is a SEPARATE source (martj42
    shootouts.csv, fetched via wca.data.results.download_shootouts) — never
    fabricated. Graceful if absent (script still runs, those ties just stay
    as "Winner M##" placeholders until the shootout data is available).
    """
    out = {}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                d = (r.get("date") or "")[:10]
                if d < "2026-06-28":
                    continue
                h, a, w = r.get("home_team"), r.get("away_team"), r.get("winner")
                if not (h and a and w):
                    continue
                out[frozenset((_canon(h), _canon(a)))] = w
    except OSError:
        pass
    return out


def load_results(path, shootouts_path=None):
    """Map frozenset{canon teamA, canon teamB} -> winner team name, for knockout-
    era matches (>= 2026-06-28) that have a decided score. Graceful if absent.

    A 90-minute draw defers to ``shootouts_path`` (see :func:`load_shootouts`)
    for the real pens winner; if that source doesn't have the match either,
    the tie is left unresolved rather than guessed.
    """
    out = {}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                d = (r.get("date") or "")[:10]
                if d < "2026-06-28":
                    continue
                h, a = r.get("home_team"), r.get("away_team")
                hs, as_ = r.get("home_score"), r.get("away_score")
                if not (h and a) or hs in (None, "") or as_ in (None, ""):
                    continue
                try:
                    hs, as_ = int(float(hs)), int(float(as_))
                except (TypeError, ValueError):
                    continue
                key = frozenset((_canon(h), _canon(a)))
                if hs == as_:
                    continue  # draw: resolved below via shootouts, if available
                out[key] = h if hs > as_ else a
    except OSError:
        pass
    if shootouts_path:
        shootouts = load_shootouts(shootouts_path)
        for key, winner in shootouts.items():
            out.setdefault(key, winner)  # 90-min score (if any) always wins first
    return out


def resolve_teams(m, results, _seen=None):
    """Return (teamA_label, teamB_label) for match m — real names where known,
    else descriptive placeholders. Recurses through the feed tree."""
    if m in R32_TEAMS:
        return R32_TEAMS[m]
    out = []
    for kind, src in FEED[m]:
        w = winner_of(src, results)
        if w is None:
            out.append("%s M%d" % ("Winner" if kind == "W" else "Loser", src))
        elif kind == "W":
            out.append(w)
        else:  # loser of a played match
            a, b = resolve_teams(src, results)
            out.append(a if w == b else b)
    return tuple(out)


def winner_of(m, results):
    a, b = resolve_teams(m, results)
    if a.startswith(("Winner ", "Loser ")) or b.startswith(("Winner ", "Loser ")):
        return None
    return results.get(frozenset((_canon(a), _canon(b))))


def _end(dtstart):
    dt = datetime.strptime(dtstart, "%Y%m%dT%H%M%S") + timedelta(hours=2)
    return dt.strftime("%Y%m%dT%H%M%S")


def knockout_vevents(results, stamp):
    blocks = []
    for m in sorted(SCHEDULE):
        rnd, dtstart, venue, city = SCHEDULE[m]
        a, b = resolve_teams(m, results)
        title = "%s vs %s" % (a, b)
        rlabel = ROUND_LABEL[rnd]
        summ = title if rnd != "F" else "FIFA World Cup 2026 Final: %s" % title
        feed_note = ""
        if m in FEED:
            (k1, s1), (k2, s2) = FEED[m]
            feed_note = "\\n%s of Match %d / %s of Match %d" % (
                "Winner" if k1 == "W" else "Loser", s1,
                "Winner" if k2 == "W" else "Loser", s2)
        m92note = "\\nKickoff time to confirm" if m == 92 else ""
        desc = ("%s (Match %d)\\n%s\\nStadium: %s\\nCity: %s\\nBahrain Time: %s%s%s"
                % (rlabel, m, title, venue, city, dtstart[9:11] + ":" + dtstart[11:13],
                   feed_note, m92note))
        blocks.append("\n".join([
            "BEGIN:VEVENT",
            "DTSTART;TZID=Asia/Bahrain:%s" % dtstart,
            "DTEND;TZID=Asia/Bahrain:%s" % _end(dtstart),
            "DTSTAMP:%s" % stamp,
            "UID:wc2026-m%d@worldcupalpha.com" % m,
            "SUMMARY:%s" % summ,
            "LOCATION:%s, %s" % (venue, city),
            "DESCRIPTION:%s" % desc,
            "BEGIN:VALARM", "TRIGGER:-PT1H",
            "DESCRIPTION:World Cup %s in 1 hour: %s" % (rlabel, title),
            "ACTION:DISPLAY", "END:VALARM", "END:VEVENT",
        ]))
    return blocks


def keep_group_events(ics_text):
    """Return the group-stage VEVENT blocks (UID wc2026-0NN) verbatim; drop the
    broken knockout rows (067, 068, final)."""
    blocks = re.findall(r"BEGIN:VEVENT.*?END:VEVENT", ics_text, re.S)
    kept = []
    for b in blocks:
        uid = re.search(r"UID:(\S+)", b)
        if not uid:
            continue
        u = uid.group(1)
        mnum = re.search(r"wc2026-(\d+)@", u)
        if mnum and int(mnum.group(1)) <= 66:   # group stage = 001..066
            kept.append(b)
    return kept


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ics", default=os.path.join(here, "World_Cup_2026_Calendar_Bahrain.ics"))
    ap.add_argument("--results", default=os.path.join(here, "data/raw/martj42_cleaned.csv"),
                    help="results CSV to auto-fill R16+ winners (optional)")
    ap.add_argument("--shootouts", default=os.path.join(here, "data/raw/shootouts.csv"),
                    help="penalty-shootout winners for knockout draws (optional; "
                         "fetch via `python -c \"from wca.data.results import "
                         "download_shootouts; download_shootouts()\"`)")
    ap.add_argument("--stamp", default="20260628T120000Z", help="DTSTAMP (UTC) for this revision")
    args = ap.parse_args(argv)

    existing = open(args.ics, encoding="utf-8").read() if os.path.exists(args.ics) else ""
    group = keep_group_events(existing)
    results = load_results(args.results, shootouts_path=args.shootouts)
    ko = knockout_vevents(results, args.stamp)

    header = "\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//World Cup Alpha//World Cup 2026 Schedule//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:FIFA World Cup 2026 (Bahrain Time)",
        "X-WR-TIMEZONE:Asia/Bahrain",
        "X-WR-CALDESC:Full FIFA World Cup 2026 fixtures incl. knockout bracket "
        "(auto-fills as teams advance). Kickoffs in Bahrain time (UTC+3) with 1h alerts.",
        "BEGIN:VTIMEZONE", "TZID:Asia/Bahrain", "BEGIN:STANDARD",
        "DTSTART:19700101T000000", "TZOFFSETFROM:+0300", "TZOFFSETTO:+0300",
        "TZNAME:GST", "END:STANDARD", "END:VTIMEZONE",
    ])
    body = "\n".join([header] + group + ko + ["END:VCALENDAR", ""])
    with open(args.ics, "w", encoding="utf-8") as fh:
        fh.write(body)

    resolved = sum(1 for m in SCHEDULE if not resolve_teams(m, results)[0].startswith(("Winner", "Loser")))
    print("wrote %s: %d group + %d knockout events; %d/%d knockout slots have real teams"
          % (args.ics, len(group), len(ko), resolved, len(SCHEDULE)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
