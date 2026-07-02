#!/usr/bin/env python
"""Build the Lilac-Ledger terminal feed from the LIVE site-analytics feeds and
bake it into a served copy of lilac-ledger.html (localhost:8002).

The lilac-ledger.html artifact embeds a single ``const DATA = {...}`` blob. This
script reads the live feeds (the same ones 8001 serves), maps them into that
exact consolidated shape, and re-injects the blob into a copy of the template —
so 8002 shows live data instead of the baked-in snapshot. Honest by design:
missing sub-feeds render as empty, never faked.

    python3 scripts/wca_lilac_ledger.py \
        --feeds site-analytics/data \
        --template "<lilac-ledger.html template>" \
        --out site-lilac
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone


def _scrub_nan(o):
    """Recursively replace NaN/Inf with None so json.dumps(allow_nan=False)
    (used by inject) never raises on an upstream feed that baked a bare NaN."""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {k: _scrub_nan(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_scrub_nan(v) for v in o]
    return o


def _load(d, name, default=None):
    p = os.path.join(d, name)
    try:
        with open(p, encoding="utf-8") as fh:
            return _scrub_nan(json.load(fh))
    except (OSError, ValueError):
        return default if default is not None else {}


_SYM = {"GBP": "£", "USD": "$"}

#: Source rows the lilac template references by name (model/offer/punt drill).
#: The template reads sources.<name>.GBP.* directly, so guarantee the shape with
#: honest zeros when the live source_summary omits a source (never faked totals).
_SRC_KEYS = ("model", "offer", "punt", "hedge")
_CUR_ZERO = {"wagered": 0.0, "open_stake": 0.0, "settled_pl": 0.0, "n_bets": 0}


def _norm_sources(src):
    """Ensure every template-referenced source has GBP/USD currency objects."""
    src = dict(src or {})
    for name in _SRC_KEYS:
        row = dict(src.get(name) or {})
        for cur in ("GBP", "USD"):
            row[cur] = {**_CUR_ZERO, **(row.get(cur) or {})}
        src[name] = row
    return src


def _map_position(p, status):
    """Map a data.json position to the lilac position shape."""
    cur = p.get("currency") or ("USD" if p.get("venue") == "polymarket" else "GBP")
    return {
        "date": (p.get("ts_utc") or "")[:10],
        "match": p.get("match"),
        "market": p.get("market"),
        "selection": p.get("selection"),
        "platform": p.get("platform"),
        "venue": p.get("venue"),
        "source": p.get("source"),
        "sym": _SYM.get(cur, cur),
        "odds": p.get("decimal_odds"),
        "stake": p.get("stake"),
        "ev": p.get("ev"),
        "model_prob": p.get("model_prob"),
        "status": p.get("status") or status,
        "pl": p.get("cash_pnl") if p.get("cash_pnl") is not None else p.get("settled_pl"),
        "clv": p.get("clv"),
        "close": p.get("closing_odds") if p.get("closing_odds") is not None else p.get("cur_price"),
    }


def _heatmap_from_adv(adv):
    """Build the heatmap shape the lilac template consumes: ``cols`` (stage
    headers), and per team ``{team, group, model_win, cells:[edge per col]}``
    sorted by model win prob. Only stages with at least one PM edge become
    columns (others would be all-null). Honest: missing edges stay None."""
    teams = adv.get("teams") or []
    if not teams:
        return {"cols": [], "teams": [], "n_pm": 0}
    stages = (adv.get("meta") or {}).get("stages") or ["R32", "R16", "QF", "SF", "Final", "win"]
    cols = [s for s in stages
            if any(((t.get("pm") or {}).get(s) or {}).get("edge_adj") is not None for t in teams)]
    if not cols:
        cols = stages
    rows = []
    for t in teams:
        pm = t.get("pm") or {}
        rows.append({
            "team": t.get("team"),
            "group": t.get("group"),
            "model_win": (t.get("model") or {}).get("win"),
            "cells": [((pm.get(c) or {}).get("edge_adj")) for c in cols],
        })
    rows.sort(key=lambda r: (r["model_win"] or 0), reverse=True)
    return {"cols": cols, "teams": rows, "n_pm": (adv.get("meta") or {}).get("n_pm_markets", 0)}


def _standings_from_by_team(bt):
    """Compute group league tables from the live ``by_team`` feed (keyed by
    team -> {group, games:[{ft, home, away}]}). The lilac template expects
    ``standings[group] = [{pos,team,p,w,d,l,gd,pts}]`` sorted by pts/gd."""
    if not isinstance(bt, dict) or not bt:
        return {}
    groups = {}
    for team, rec in bt.items():
        g = (rec or {}).get("group")
        if not g:
            continue
        p = w = d = l = gf = ga = 0
        for gm in (rec.get("games") or []):
            ft = gm.get("ft")
            if not ft or "-" not in str(ft):
                continue
            try:
                hs, as_ = (int(x) for x in str(ft).split("-")[:2])
            except (ValueError, TypeError):
                continue
            tf, ta = (hs, as_) if gm.get("home") == team else (as_, hs)
            p += 1
            gf += tf
            ga += ta
            if tf > ta:
                w += 1
            elif tf == ta:
                d += 1
            else:
                l += 1
        groups.setdefault(g, []).append({
            "team": team, "p": p, "w": w, "d": d, "l": l,
            "gd": gf - ga, "pts": w * 3 + d,
        })
    out = {}
    for g in sorted(groups):
        rows = sorted(groups[g], key=lambda r: (r["pts"], r["gd"], r["w"]), reverse=True)
        for i, r in enumerate(rows):
            r["pos"] = i + 1
        out[g] = rows
    return out


def _rnorm(s):
    t = (s or "").strip().lower()
    return {"côte d'ivoire": "ivory coast", "cote d'ivoire": "ivory coast",
            "ir iran": "iran", "usa": "united states"}.get(t, t)


def _reconcile_1x2_mc(adv, scores):
    """Live 1X2-forecast vs Monte-Carlo advancement consistency (network-free).

    The MC 'to advance' probability is the SAME engine as the 1X2 forecast (1X2
    home-win + the ET/penalties share of the draw), so the two must agree. This
    recomputes that check from the advancement + scores feeds each build, so the
    Under-The-Hood panel is always current to the latest model."""
    mc = {_rnorm(t.get("team")): (t.get("model") or {}) for t in (adv or {}).get("teams", [])}
    rows = []
    for f in (scores or {}).get("fixtures", []):
        fx = f.get("fixture") or ""
        m = f.get("model_1x2") or {}
        if " vs " not in fx or not m:
            continue
        home = fx.split(" vs ", 1)[0].strip()
        reach = (mc.get(_rnorm(home)) or {}).get("R16")
        if reach is None:
            continue
        imp = float(m.get("home", 0)) + 0.5 * float(m.get("draw", 0))  # 1X2 -> advance
        rows.append({"tie": fx, "home": home,
                     "h": round(float(m.get("home", 0)), 3), "d": round(float(m.get("draw", 0)), 3),
                     "a": round(float(m.get("away", 0)), 3),
                     "imp": round(imp, 3), "mc": round(float(reach), 3),
                     "gap": round(imp - float(reach), 3)})
    rows.sort(key=lambda r: -r["mc"])
    mean_abs_gap = round(sum(abs(r["gap"]) for r in rows) / len(rows), 3) if rows else None
    return {
        "rows": rows,
        "mean_abs_gap": mean_abs_gap,
        "n": len(rows),
        "verdict": ("The 1X2 forecast and the Monte-Carlo advancement model are ONE engine "
                    "(MC advance = 1X2 home-win + ET/penalty share of the draw), so they agree "
                    "within ~%s pp. A small negative 1X2 edge therefore implies a small negative "
                    "advancement edge — the profitable knockout book so far is favourite beta + "
                    "mark-to-market convergence at n_eff~1 tournament, not demonstrated alpha. "
                    "Judge on CLV vs the close, not MTM P&L."
                    % (int(mean_abs_gap * 100) if mean_abs_gap is not None else "?")),
    }


def build_data(feeds_dir):
    data = _load(feeds_dir, "data.json")
    scores = _load(feeds_dir, "scores_data.json")
    smk = _load(feeds_dir, "scores_markets.json")
    adv = _load(feeds_dir, "advancement_data.json")
    exposure = _load(feeds_dir, "exposure_data.json")
    mc = _load(feeds_dir, "mc_futures.json")
    tracking = _load(feeds_dir, "tracking_data.json")
    # The lilac template reads DATA.tracking.buckets.* (Exhibit 3 series), but
    # buckets ship as a separate feed — nest it so the panel finds it.
    if isinstance(tracking, dict):
        tracking = {**tracking, "buckets": _load(feeds_dir, "tracking_buckets.json")}
        # template's scoreboard panel reads TR.scoreboard; live feed calls it fixtures
        tracking.setdefault("scoreboard", tracking.get("fixtures") or [])
    promos = _load(feeds_dir, "promos_data.json", default={})
    # --- merged panels from 8001 (analytics) — same live feeds 8001 serves ---
    venues_bench = _load(feeds_dir, "venues_benchmark.json")
    market_intel = _load(feeds_dir, "market_intel.json")
    rigor = _load(feeds_dir, "rigor.json")
    risk_pnl = _load(feeds_dir, "risk_pnl.json")
    winrate = _load(feeds_dir, "winrate.json")
    predledger = _load(feeds_dir, "predledger.json")
    clv_bench = _load(feeds_dir, "tracking_clv_benchmark.json")
    bet_recs = _load(feeds_dir, "bet_recs.json")

    def gen(o):
        return ((o or {}).get("meta") or {}).get("generated")

    positions = data.get("positions") or []
    closed = data.get("closed_positions") or []

    out = {
        "meta": {
            "terminal": gen(data), "scores": gen(scores) or gen(smk), "adv": gen(adv),
            "adv_model": (adv.get("meta") or {}).get("model_generated"),
            "exposure": gen(exposure), "mc": gen(mc), "tracking": gen(tracking),
            "promos": gen(promos),
            "venues_bench": gen(venues_bench), "market_intel": gen(market_intel),
            "rigor": gen(rigor), "risk_pnl": gen(risk_pnl), "winrate": gen(winrate),
            "predledger": gen(predledger), "clv_bench": gen(clv_bench),
            "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "lilac_built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "by_currency": data.get("totals_by_currency") or {},
        "venues": data.get("venues") or {},
        "sources": _norm_sources(data.get("source_summary")),
        "clv": data.get("clv") or {},
        "positions_open": [_map_position(p, "open") for p in positions],
        "positions_closed": [_map_position(p, "settled") for p in closed],
        "pnl_series": data.get("pnl_series") or {},
        "predictions": data.get("predictions") or [],
        "scores_fixtures": (scores.get("fixtures") or [])[:24],
        "heatmap": _heatmap_from_adv(adv),
        "standings": _standings_from_by_team(smk.get("by_team")) or smk.get("standings") or {},
        "exposure": exposure or {"empty": True},
        "mc": mc or {"meta": {}, "markets": {}},
        "tracking": tracking or {},
        "promos": promos or {"signup_offers": [], "watchlist": [], "boost_evals": [],
                             "scrape_health": [], "ongoing": []},
        # merged 8001 analytics panels (honest empties when a feed is absent)
        "venues_bench": venues_bench or {},
        "market_intel": market_intel or {},
        "rigor": rigor or {},
        "risk_pnl": risk_pnl or {},
        "winrate": winrate or {},
        "predledger": predledger or {},
        "clv_bench": clv_bench or {},
        "bet_recs": bet_recs or {},
        "reconcile": _reconcile_1x2_mc(adv, scores),
    }
    return out


def inject(template_html, data):
    blob = json.dumps(data, separators=(",", ":"), allow_nan=False)
    # Replace the first `const DATA = {...};` (greedy-safe: match up to the
    # `;` that precedes a newline + non-data line). The template has exactly one.
    pat = re.compile(r"const DATA = \{.*?\};\n", re.S)
    if not pat.search(template_html):
        raise SystemExit("template has no `const DATA = {...};` blob to replace")
    repl = "const DATA = " + blob + ";\n"
    # Function replacement so backslash escapes in the JSON (£ etc.) are NOT
    # interpreted as regex replacement templates.
    return pat.sub(lambda _m: repl, template_html, count=1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feeds", default="site-analytics/data")
    ap.add_argument("--template", required=True, help="path to the lilac-ledger.html template")
    ap.add_argument("--out", default="site-lilac", help="output dir for the served HTML")
    args = ap.parse_args(argv)

    data = build_data(args.feeds)
    with open(args.template, encoding="utf-8") as fh:
        template = fh.read()
    html = inject(template, data)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    with open(os.path.join(args.out, "lilac_ledger.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, allow_nan=False)

    print("lilac 8002 built: %d open / %d closed positions | terminal=%s | built=%s"
          % (len(data["positions_open"]), len(data["positions_closed"]),
             data["meta"]["terminal"], data["meta"]["lilac_built"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
