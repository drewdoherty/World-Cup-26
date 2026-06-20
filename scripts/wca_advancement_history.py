#!/usr/bin/env python
"""Aggregate advancement-probability snapshots into site/advancement_history.json.

The Visuals tab's "Model outputs over time" exhibit (site/visuals.js) needs >= 2
time points of per-team tournament probabilities to render. This builds them from
what the repo already has:

  * a **pre-tournament baseline** — recovered from the current file's recorded
    ``P(Final)_delta`` (pre = now - delta);
  * any dated **checkpoints** under
    ``data/snapshots/<YYYYMMDD-...>/advancement_current_vs_pretournament.json``;
  * the live ``data/advancement_current_vs_pretournament.json`` (now).

Metric per team = ``P(Final)`` (probability of reaching the final), 0..1 — the
quantity the exhibit ranks teams by. Output shape (consumed by visuals.js):

    {"meta": {...}, "snapshots": [{"label", "date", "probs": {team: 0..1}}, ...]}

Idempotent and cheap (pure file IO, no network); safe to run each publish.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sys

CURRENT = "data/advancement_current_vs_pretournament.json"
OUT = "site/advancement_history.json"
METRIC = "P(Final)"
PRETOURNAMENT_DATE = "2026-06-11"  # WC2026 kickoff; the delta baseline


def _probs(rows):
    return {
        r["team"]: round(float(r.get(METRIC) or 0.0), 4)
        for r in rows
        if r.get("team")
    }


def _pretournament(rows):
    """Pre-tournament P(Final) recovered as (current - recorded delta)."""
    out = {}
    for r in rows:
        if not r.get("team"):
            continue
        now = float(r.get(METRIC) or 0.0)
        delta = float(r.get(METRIC + "_delta") or 0.0)
        out[r["team"]] = round(now - delta, 4)
    return out


def _date_from_dir(path):
    m = re.search(r"(\d{8})", os.path.basename(os.path.dirname(path)))
    if not m:
        return None
    s = m.group(1)
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])


def main(argv=None) -> int:
    if not os.path.exists(CURRENT):
        print("no current advancement file (%s); nothing to do" % CURRENT,
              file=sys.stderr)
        return 0
    cur = json.load(open(CURRENT))

    snapshots = [
        {"label": "Pre-tournament", "date": PRETOURNAMENT_DATE,
         "probs": _pretournament(cur)},
    ]

    seen_dates = {PRETOURNAMENT_DATE}
    for p in sorted(glob.glob(
            "data/snapshots/*/advancement_current_vs_pretournament.json")):
        try:
            rows = json.load(open(p))
        except (OSError, json.JSONDecodeError):
            continue
        dt = _date_from_dir(p)
        if not dt or dt in seen_dates:
            continue
        seen_dates.add(dt)
        snapshots.append({"label": dt, "date": dt, "probs": _probs(rows)})

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if today not in seen_dates:
        snapshots.append({"label": "Now (%s)" % today, "date": today,
                          "probs": _probs(cur)})

    snapshots.sort(key=lambda s: s.get("date") or "")

    out = {
        "meta": {
            "generated": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC"),
            "metric": METRIC,
        },
        "snapshots": snapshots,
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print("%s: %d snapshots (%s)"
          % (OUT, len(snapshots), ", ".join(s["label"] for s in snapshots)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
