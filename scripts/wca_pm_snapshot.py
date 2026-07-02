#!/usr/bin/env python
"""Append a Polymarket price snapshot to the historical dataset.

Outright / advancement / knockout markets have no fixed close, so the only
*leading* edge signal is whether the PM price drifts toward the model over time —
which needs a captured price trajectory. This snapshotter builds that trajectory.

It reuses the model-vs-PM pairing the advancement feed already computes every
build (``site/advancement_data.json``: per team x stage, ``model`` prob and PM
``pm`` mid), so it costs NO extra Polymarket calls and runs wherever that feed
is fresh (CI cloud build or the mini). Each run appends one timestamped record
per market to ``data/pm_price_history.jsonl`` (a versioned dataset) and, when a
DB is given, to the ``pm_snapshots`` table.

With ``--full`` it ALSO captures the categories the advancement feed never had —
single-match **player / exact-score / corners props** and the **wider tournament
futures** (Golden Boot, Furthest-Advancing nation, etc.) — by walking the live
World-Cup market universe via :func:`wca.data.polymarket.find_world_cup_markets`.
The feed already owns advancement + champion + group-winner with model
probabilities and an unbroken history, so ``--full`` deliberately does NOT
re-capture those (no duplicate series).

Usage
-----
    PYTHONPATH=src python3 scripts/wca_pm_snapshot.py \
        [--adv site/advancement_data.json] \
        [--jsonl data/pm_price_history.jsonl] \
        [--db data/wca.db] [--ts 2026-06-28T06:00:00Z] [--full]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmhistory  # noqa: E402

_DEF_ADV = os.path.join(_ROOT, "site", "advancement_data.json")
_DEF_JSONL = os.path.join(_ROOT, "data", "pm_price_history.jsonl")
_ADV_STAGES = ("R32", "R16", "QF", "SF", "Final", "win", "group_winner")

# --- Full-universe classification (props + wider futures) --------------------
# Skip novelty / non-sporting markets entirely.
_NOVELTY_KW = (
    "trump", "ronaldo cry", "shake hands", "halftime show", "boot brand",
    "boot sponsor", "glove brand", "afa president", "penalty shootouts",
    "fastest goal", "goalkeeper to score", "record broken", "perform at",
    "never won the world cup", "advance furthest in",
)
# Tournament-future keywords (non-match events).
_FUTURES_KW = (
    "world cup winner", "golden boot", "silver boot", "bronze boot",
    "golden ball", "silver ball", "bronze ball", "golden glove",
    "fair play award", "furthest advancing", "which continent", "continent to score",
)
# Match-level event suffixes that are betting props (not 1X2 / halftime).
_PROP_MATCH_SUFFIX = ("exact score", "player props", "total corners", "more markets")
# Events the advancement feed already owns (champion + group winner): skip in --full.
_FEED_OWNED_RE = re.compile(r"^world cup winner\s*$|^world cup group [a-z0-9] winner\s*$", re.I)


def classify_event(title: str) -> Optional[str]:
    """Bucket a Polymarket WC *event* title into prop / futures / advancement.

    Returns ``None`` for novelty markets, bare 1X2 / halftime match markets, and
    anything unrecognised (so we never mis-capture)."""
    t = (title or "").strip()
    tl = t.lower()
    if any(k in tl for k in _NOVELTY_KW):
        return None
    if " vs. " in t or " vs " in t:
        if " - " not in t:
            return None  # bare "X vs. Y" == 1X2 match winner
        suffix = t.split(" - ", 1)[1].strip().lower()
        return "prop" if suffix in _PROP_MATCH_SUFFIX else None
    if "stage of elimination" in tl:
        return "advancement"
    if tl.startswith("world cup:") and tl.endswith(" goals"):
        return "prop"  # "World Cup: <Player> Goals" — tournament-long player prop
    if any(k in tl for k in _FUTURES_KW):
        return "futures"
    return None


def _label_for(title: str, git: str) -> str:
    """A concise human label for a full-universe market row."""
    t = (title or "").strip()
    tl = t.lower()
    g = (git or "").strip()
    if "stage of elimination" in tl:
        subj = t.split(":", 1)[1] if ":" in t else t
        subj = subj.lower().replace("stage of elimination", "").strip().title()
        return "%s → %s" % (subj, g)
    if tl.startswith("world cup:") and tl.endswith(" goals"):
        player = t.split(":", 1)[1].rsplit("Goals", 1)[0].strip()
        return "%s %s goals" % (player, g)
    if " vs. " in t or " vs " in t:
        return g  # player props / corners / exact score are self-describing
    ctx = re.sub(r"(?i)^world cup:?\s*", "", t).strip()
    return ("%s — %s" % (g, ctx)) if ctx else g


def rows_from_wc_markets(events, *, only=None, skip_feed_owned: bool = True) -> List[Dict[str, object]]:
    """Snapshot rows from the live WC market universe (props + wider futures).

    ``only`` optionally restricts to a set of categories (e.g. ``{"prop",
    "futures"}``). When ``skip_feed_owned`` is true, champion / group-winner
    events are dropped (the advancement feed already captures those)."""
    from wca.data import polymarket as _pm

    rows: List[Dict[str, object]] = []
    for ev in events or []:
        title = ev.get("title") or ""
        cat = classify_event(title)
        if cat is None or (only is not None and cat not in only):
            continue
        if skip_feed_owned and _FEED_OWNED_RE.match(title.strip()):
            continue
        slug = ev.get("slug") or title
        for m in ev.get("markets") or []:
            res = _pm._yes_token_and_price(m, ev)
            if not res:
                continue  # unpriced / illiquid -> skipped (no usable mid)
            git = m.get("groupItemTitle") or m.get("question") or ""
            if not git:
                continue
            rows.append({
                "kind": cat,
                "team": _label_for(title, git),
                "stage": None,
                "market_slug": "%s::%s" % (slug, git),
                "token_id": res.get("token_id"),
                "pm_mid": res.get("price"),
                "model_prob": None,
            })
    return rows


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_from_advancement(adv: Dict[str, object]) -> List[Dict[str, object]]:
    """Snapshot rows from an advancement feed: one per (team, stage) priced market."""
    rows: List[Dict[str, object]] = []
    for t in adv.get("teams", []):
        team = t.get("team")
        model = t.get("model", {}) or {}
        pm = t.get("pm", {}) or {}
        for stage in _ADV_STAGES:
            cell = pm.get(stage)
            if not isinstance(cell, dict) or cell.get("pm") is None:
                continue
            rows.append({
                "kind": "advancement", "team": team, "stage": stage,
                "market_slug": "%s:%s" % (team, stage),
                "token_id": cell.get("token_id"),
                "pm_mid": cell.get("pm"),
                "model_prob": (model.get(stage) if model.get(stage) is not None else None),
            })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adv", default=_DEF_ADV, help="advancement feed (model+pm per team/stage)")
    ap.add_argument("--jsonl", default=_DEF_JSONL, help="append-only JSONL history dataset")
    ap.add_argument("--db", default=None, help="optional sqlite DB for the pm_snapshots table")
    ap.add_argument("--ts", default=None, help="capture timestamp (default: advancement model_generated, else now)")
    ap.add_argument("--full", action="store_true",
                    help="also capture props + wider futures via find_world_cup_markets()")
    args = ap.parse_args(argv)

    with open(args.adv, "r", encoding="utf-8") as fh:
        adv = json.load(fh)
    rows = rows_from_advancement(adv)
    # Prefer the model's own generation time so re-running on the same feed is idempotent-ish.
    meta = adv.get("meta", {}) or {}
    ts = args.ts or meta.get("model_generated") or meta.get("generated") or _now_iso_z()

    n_full = 0
    if args.full:
        from wca.data import polymarket as _pm
        events = _pm.find_world_cup_markets(include_closed=False)
        # Props + wider futures only; advancement / champion / group-winner stay
        # with the feed (model probs + unbroken history), so no duplicate series.
        full_rows = rows_from_wc_markets(events, only={"prop", "futures"}, skip_feed_owned=True)
        n_full = len(full_rows)
        rows = rows + full_rows

    n_jsonl = pmhistory.append_jsonl(args.jsonl, rows, ts)
    n_db = 0
    if args.db:
        con = sqlite3.connect(args.db)
        try:
            n_db = pmhistory.append_snapshots(con, rows, ts)
        finally:
            con.close()
    print("pm snapshot @ %s: %d markets (+%d full) -> jsonl(+%d) db(+%d) [%s]"
          % (ts, len(rows), n_full, n_jsonl, n_db, args.jsonl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
