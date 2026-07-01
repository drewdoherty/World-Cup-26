#!/usr/bin/env python
"""Generate the Event-Markets forest feed (``site/forest_data.json``).

This is the per-fixture "MODEL vs MARKET" dot-plot feed that ``site/forest.html``
renders. It is a thin projection of the *same* real pipeline the scores feed
uses (:func:`wca.scorespage.build_scores_data`): live World Cup h2h odds from
The Odds API / Betfair, opportunistic Polymarket 1X2 + exact-score quotes, and
the model's reconciled scoreline / 1X2 / O-U / BTTS probabilities from the
cached matchday card. Nothing here invents a probability or a price — every
number originates in a real producer.

For each fixture we emit rows grouped by market (1X2, O/U 2.5, BTTS, top
correct-scores). Each row carries:

    model   float 0..1   — model implied probability for the outcome
    market  float 0..1   — best available market-implied probability, or null
                           when no live market price exists for that outcome

``market`` uses the same rule the reference forest used: prefer the
Polymarket mid (vig-free) when present, else the best (lowest-implied = best
back price) across the live venues. Where no market exists for an outcome
(e.g. O/U or BTTS, for which we have no live venue feed), ``market`` is ``null``
and the site renders the model dot only, labelled "model".

Usage
-----
    python scripts/wca_forest_data.py [--card data/card_latest.md] \
        [--out site/forest_data.json] [--hours-ahead 48] [--no-polymarket]

Reuses the odds/Polymarket collection helpers from ``wca_scores_data`` so the
two feeds are always built from an identical source snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Make ``src`` importable when run directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import scorespage  # noqa: E402
from wca.data import odds_source  # noqa: E402

# Reuse the *exact* real collectors the scores feed uses so the two feeds are
# built from one odds/Polymarket snapshot (no divergent, re-fabricated numbers).
import wca_scores_data as scores_cli  # noqa: E402

_SPORT_KEY = "soccer_fifa_world_cup"


def _best_market_implied(
    venues: List[Dict[str, Any]], outcome: str
) -> Optional[float]:
    """Best available market-implied probability (0..1) for a 1X2 outcome.

    Prefers the Polymarket mid (vig-free) when present; otherwise the best
    (lowest-implied = best back price) across live venues. Returns ``None`` when
    no venue prices this outcome — the site then renders the model dot only.
    Mirrors the reference forest's ``_emBestImplied``.
    """
    if not venues:
        return None
    pm: Optional[float] = None
    best: Optional[float] = None
    for v in venues:
        implied = (v or {}).get("implied") or {}
        imp = implied.get(outcome)
        if imp is None or not (imp > 0):
            continue
        if v.get("venue") == "polymarket":
            pm = imp
        if best is None or imp < best:
            best = imp
    return pm if pm is not None else best


def _fixture_rows(fx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the ordered forest rows for one fixture from real feed values.

    ``model`` / ``market`` are 0..1 probabilities. Section rows carry only a
    ``section`` key. A missing market yields ``market: None`` (site shows the
    model dot + "model" label).
    """
    rows: List[Dict[str, Any]] = []
    mx = fx.get("model_1x2") or {}
    venues = fx.get("venues") or []

    # --- 1X2 (model vs best live venue implied) -----------------------------
    rows.append({"section": "1X2"})
    rows.append(
        {"label": "Home", "model": mx.get("home"),
         "market": _best_market_implied(venues, "home")}
    )
    rows.append(
        {"label": "Draw", "model": mx.get("draw"),
         "market": _best_market_implied(venues, "draw")}
    )
    rows.append(
        {"label": "Away", "model": mx.get("away"),
         "market": _best_market_implied(venues, "away")}
    )

    # --- O/U goals (model only — no live O/U venue feed) --------------------
    ou = fx.get("over_under")
    if ou:
        line = ou.get("line")
        over = ou.get("over")
        under = ou.get("under")
        rows.append({"section": "O/U %s Goals" % (line if line is not None else "2.5")})
        rows.append({"label": "Over",
                     "model": (over / 100.0) if over is not None else None,
                     "market": None})
        rows.append({"label": "Under",
                     "model": (under / 100.0) if under is not None else None,
                     "market": None})

    # --- BTTS (model only) --------------------------------------------------
    btts = fx.get("btts")
    if btts is not None:
        rows.append({"section": "BTTS"})
        rows.append({"label": "Yes", "model": btts / 100.0, "market": None})
        rows.append({"label": "No", "model": (100.0 - btts) / 100.0, "market": None})

    # --- Top correct-scores (model vs Polymarket exact-score, where present) -
    scores = (fx.get("scores") or [])[:6]
    if scores:
        rows.append({"section": "Top Scorelines (model & market)"})
        for sc in scores:
            prob = sc.get("prob")
            pm_prob = sc.get("pm_prob")
            rows.append({
                "label": sc.get("score"),
                "model": (prob / 100.0) if prob is not None else None,
                "market": (pm_prob / 100.0) if pm_prob is not None else None,
            })

    return rows


def build_forest_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Project a ``build_scores_data`` payload into the forest feed shape."""
    out_fixtures: List[Dict[str, Any]] = []
    for fx in data.get("fixtures") or []:
        rows = _fixture_rows(fx)
        entry: Dict[str, Any] = {
            "fixture": fx.get("fixture") or "",
            "rows": rows,
        }
        if fx.get("kickoff"):
            entry["kickoff"] = fx["kickoff"]
        # honest flag: did any outcome in this fixture get a live market price?
        entry["has_market"] = any(
            r.get("market") is not None for r in rows if "section" not in r
        )
        out_fixtures.append(entry)
    return {"meta": dict(data.get("meta") or {}), "fixtures": out_fixtures}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the World Cup Alpha event-markets forest feed.",
    )
    parser.add_argument("--card", default="data/card_latest.md",
                        help="Cached matchday card (default: data/card_latest.md).")
    parser.add_argument("--out", default="site/forest_data.json",
                        help="Destination JSON (default: site/forest_data.json).")
    parser.add_argument("--hours-ahead", type=float, default=48.0,
                        help="Only include fixtures kicking off within N hours.")
    parser.add_argument("--no-polymarket", action="store_true",
                        help="Skip Polymarket enrichment entirely.")
    parser.add_argument("--env", default=".env", help="dotenv file to load.")
    args = parser.parse_args(argv)

    scores_cli._load_dotenv(args.env)

    # --- Odds pull (Betfair -> Odds API -> Polymarket; never fatal) ---------
    odds_df, _quota = odds_source.get_odds(_SPORT_KEY, regions="uk", markets="h2h")
    odds_df = scores_cli._filter_next_hours(odds_df, args.hours_ahead)

    # --- Polymarket enrichment (best-effort; identical to scores feed) ------
    pm_quotes: Dict[str, Dict[str, float]] = {}
    pm_scores: Dict[str, Dict[str, float]] = {}
    if not args.no_polymarket:
        try:
            pm_quotes = scores_cli._collect_pm_quotes(odds_df)
        except Exception as exc:  # noqa: BLE001 — never let PM break the feed.
            print("polymarket 1X2 enrichment failed (%s); continuing" % exc)
        try:
            pm_scores = scores_cli._collect_pm_scores(odds_df)
        except Exception as exc:  # noqa: BLE001
            print("polymarket exact-score enrichment failed (%s); continuing" % exc)

    now_utc = scores_cli._now_utc_str()
    data = scorespage.build_scores_data(
        args.card, odds_df=odds_df, pm_quotes=pm_quotes, pm_scores=pm_scores,
        now_utc=now_utc,
    )
    forest = build_forest_data(data)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(forest, fh, indent=2)
        fh.write("\n")

    fixtures = forest["fixtures"]
    n_with_market = sum(1 for f in fixtures if f.get("has_market"))
    print(args.out)
    print("fixtures=%d  with_live_market=%d  pm_quotes=%d"
          % (len(fixtures), n_with_market, len(pm_quotes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
