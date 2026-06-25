#!/usr/bin/env python
"""Generate the model-vs-market scores feed (``site/scores_data.json``).

Pulls live World Cup h2h odds from The Odds API, opportunistically fetches
Polymarket implied 1X2 quotes for the same fixtures, reads the cached matchday
card, and writes the structured JSON that ``site/scores.html`` renders.

Unlike the deterministic library in :mod:`wca.scorespage`, this CLI is allowed
to read the wall clock and the network.  Every network call is guarded: a
failed odds pull is fatal (there'd be nothing to compare), but a failed
Polymarket fetch simply omits the polymarket venue.

Usage
-----
    python scripts/wca_scores_data.py [--card data/card_latest.md] \
        [--out site/scores_data.json] [--hours-ahead 48] [--no-polymarket]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make ``src`` importable when run directly (python scripts/wca_scores_data.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import scorespage  # noqa: E402
from wca.data import odds_source, polymarket, teamnames  # noqa: E402


_SPORT_KEY = "soccer_fifa_world_cup"


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so we don't add a python-dotenv dependency."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _now_utc_str() -> str:
    """Return the current UTC time as an ISO-ish display string."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")


def _filter_next_hours(odds_df: Any, hours_ahead: float) -> Any:
    """Restrict ``odds_df`` to events kicking off within ``hours_ahead`` hours.

    Events with an unparseable / missing commence_time are kept (we'd rather
    show a fixture than silently drop it).  Returns the DataFrame unchanged if
    it has no ``commence_time`` column.
    """
    if odds_df is None or "commence_time" not in getattr(odds_df, "columns", []):
        return odds_df
    import pandas as pd  # local import: only the CLI needs pandas directly.

    now = pd.Timestamp.now(tz="UTC")
    horizon = now + pd.Timedelta(hours=hours_ahead)
    ct = pd.to_datetime(odds_df["commence_time"], utc=True, errors="coerce")
    keep = ct.isna() | ((ct >= now) & (ct <= horizon))
    return odds_df[keep]


def _fixture_pairs(odds_df: Any) -> List[Tuple[str, str]]:
    """Distinct (home_team, away_team) pairs present in ``odds_df`` (raw feed
    spelling, de-duplicated)."""
    pairs: List[Tuple[str, str]] = []
    seen = set()
    if odds_df is None:
        return pairs
    cols = getattr(odds_df, "columns", [])
    if "home_team" not in cols or "away_team" not in cols:
        return pairs
    for _, row in odds_df[["home_team", "away_team"]].iterrows():
        home = row.get("home_team")
        away = row.get("away_team")
        if home is None or away is None:
            continue
        key = (str(home), str(away))
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def _pm_quote_for(home: str, away: str) -> Optional[Dict[str, float]]:
    """Best-effort Polymarket 1X2 quote for one fixture.

    Searches Polymarket for the fixture and parses its three markets ("Will
    HOME win...", "Will HOME vs. AWAY end in a draw?", "Will AWAY win...") into
    home/draw/away probabilities in 0..1.  Returns ``None`` on any failure or
    when fewer than two legs resolve (a single leg can't be trusted).
    """
    home_c = teamnames.canonical(home)
    away_c = teamnames.canonical(away)
    try:
        events = polymarket.search_events("%s vs %s" % (home, away), closed=False)
    except Exception:  # noqa: BLE001 — network/parse failures are non-fatal.
        return None

    target_event = _best_pm_event(events, home, away)
    if target_event is None:
        return None

    legs: Dict[str, float] = {}
    for market in target_event.get("markets") or []:
        question = (market.get("question") or "")
        price = _pm_yes_price(market)
        if price is None:
            continue
        leg = _classify_pm_question(question, home, home_c, away, away_c)
        if leg is not None and leg not in legs:
            legs[leg] = price

    if len(legs) < 2:
        return None
    return {
        "home": legs.get("home"),
        "draw": legs.get("draw"),
        "away": legs.get("away"),
    }


def _best_pm_event(
    events: List[Dict[str, Any]], home: str, away: str
) -> Optional[Dict[str, Any]]:
    """Pick the event whose title mentions both teams, else the first result."""
    home_l = teamnames.canonical(home).casefold()
    away_l = teamnames.canonical(away).casefold()
    for ev in events or []:
        title = (ev.get("title") or "").casefold()
        if home_l in title and away_l in title:
            return ev
    return (events or [None])[0]


def _pm_yes_price(market: Dict[str, Any]) -> Optional[float]:
    """Extract the 'Yes' probability (0..1) from a Polymarket market dict.

    Prefers a midpoint of bestBid/bestAsk when present, else the priceMap /
    outcomePrices 'Yes' entry.  Returns ``None`` when nothing usable is found.
    """
    bid = _opt_float(market.get("bestBid"))
    ask = _opt_float(market.get("bestAsk"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0

    price_map = market.get("priceMap")
    if isinstance(price_map, dict):
        for key in ("Yes", "yes", "YES"):
            val = _opt_float(price_map.get(key))
            if val is not None:
                return val
    return None


def _classify_pm_question(
    question: str, home: str, home_c: str, away: str, away_c: str
) -> Optional[str]:
    """Map a Polymarket question to home/draw/away.

    Questions look like "Will Mexico win on 2026-06-12?" or "Will Mexico vs.
    South Africa end in a draw?".
    """
    q = question.casefold()
    if "draw" in q:
        return "draw"
    # Win questions: match the first team name appearing after "will".
    for label, canon in (("home", home), ("away", away)):
        for name in (canon, teamnames.canonical(canon)):
            if name and name.casefold() in q and "win" in q:
                return label
    return None


def _opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _collect_pm_quotes(odds_df: Any) -> Dict[str, Dict[str, float]]:
    """Fetch Polymarket quotes for every fixture in ``odds_df`` (best-effort).

    Keyed by ``"Home vs Away"`` (raw feed spelling) so
    :func:`wca.scorespage.build_scores_data` can match them.
    """
    quotes: Dict[str, Dict[str, float]] = {}
    for home, away in _fixture_pairs(odds_df):
        quote = _pm_quote_for(home, away)
        if quote is not None:
            quotes["%s vs %s" % (home, away)] = quote
    return quotes


_PM_SCORES_CACHE = "data/pm_exactscore_cache.json"


def _kickoff_for(odds_df: Any, home: str, away: str) -> Optional[datetime.datetime]:
    """Kickoff (UTC datetime) for a fixture from the odds frame, or ``None``."""
    try:
        import pandas as pd

        if odds_df is None or getattr(odds_df, "empty", True):
            return None
        if "commence_time" not in odds_df.columns:
            return None
        m = odds_df[(odds_df["home_team"] == home) & (odds_df["away_team"] == away)]
        if m.empty:
            return None
        dt = pd.to_datetime(m["commence_time"].iloc[0], utc=True, errors="coerce")
        return None if pd.isna(dt) else dt.to_pydatetime()
    except Exception:  # noqa: BLE001
        return None


def _collect_pm_scores(
    odds_df: Any, cache_path: str = _PM_SCORES_CACHE
) -> Dict[str, Dict[str, float]]:
    """Per-fixture Polymarket exact-score probabilities, on a refresh cadence.

    Each fixture's correct-score quotes are re-pulled **once per day** while its
    kickoff is far off, stepping up to **once per hour once kickoff is < 4h away**
    (publish runs hourly, so the <4h tier resolves to every run). A small JSON
    cache (``data/pm_exactscore_cache.json``, host-local, not committed) records
    the last pull per fixture so far-off fixtures aren't re-fetched every hour.
    Polymarket's Gamma API is free, so this never costs Odds-API credits.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    except (OSError, json.JSONDecodeError):
        cache = {}

    fixtures = [
        (home, away, "%s vs %s" % (home, away), _kickoff_for(odds_df, home, away))
        for home, away in _fixture_pairs(odds_df)
    ]

    due = []
    for home, away, fx, ko in fixtures:
        ttl = 86400.0
        if ko is not None and now < ko and (ko - now).total_seconds() <= 4 * 3600:
            ttl = 3600.0
        ts = (cache.get(fx) or {}).get("ts")
        fresh = False
        if ts:
            try:
                fresh = (now - datetime.datetime.fromisoformat(ts)).total_seconds() < ttl
            except ValueError:
                fresh = False
        if not fresh:
            due.append((home, away, fx))

    if due:
        try:
            events = polymarket.find_world_cup_markets(include_closed=False)
        except Exception:  # noqa: BLE001 — never let PM break the feed.
            events = None
        if events is not None:
            for home, away, fx in due:
                try:
                    scores = polymarket.resolve_exact_scores(home, away, events=events)
                except Exception:  # noqa: BLE001
                    scores = {}
                cache[fx] = {"ts": now.isoformat(), "scores": scores}
            try:
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh, indent=2)
            except OSError:
                pass

    out: Dict[str, Dict[str, float]] = {}
    for _h, _a, fx, _k in fixtures:
        sc = (cache.get(fx) or {}).get("scores") or {}
        if sc:
            out[fx] = sc
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the World Cup Alpha model-vs-market scores feed.",
    )
    parser.add_argument(
        "--card",
        default="data/card_latest.md",
        help="Path to the cached matchday card (default: data/card_latest.md).",
    )
    parser.add_argument(
        "--out",
        default="site/scores_data.json",
        help="Destination JSON file (default: site/scores_data.json).",
    )
    parser.add_argument(
        "--hours-ahead",
        type=float,
        default=48.0,
        help="Only include fixtures kicking off within this many hours.",
    )
    parser.add_argument(
        "--no-polymarket",
        action="store_true",
        help="Skip the Polymarket enrichment fetch entirely.",
    )
    parser.add_argument("--env", default=".env", help="dotenv file to load.")
    args = parser.parse_args(argv)

    _load_dotenv(args.env)

    # --- Odds pull (Betfair -> Odds API -> Polymarket; never fatal) ---------
    # An empty frame degrades the scores feed to "data-pending" fixtures rather
    # than crashing when the upstream odds source is down.
    odds_df, quota = odds_source.get_odds(
        _SPORT_KEY, regions="uk", markets="h2h"
    )
    odds_df = _filter_next_hours(odds_df, args.hours_ahead)

    # --- Polymarket enrichment (best-effort) --------------------------------
    pm_quotes: Dict[str, Dict[str, float]] = {}
    pm_scores: Dict[str, Dict[str, float]] = {}
    if not args.no_polymarket:
        try:
            pm_quotes = _collect_pm_quotes(odds_df)
        except Exception as exc:  # noqa: BLE001 — never let PM break the feed.
            print("polymarket 1X2 enrichment failed (%s); continuing" % exc)
        try:
            pm_scores = _collect_pm_scores(odds_df)
        except Exception as exc:  # noqa: BLE001
            print("polymarket exact-score enrichment failed (%s); continuing" % exc)

    now_utc = _now_utc_str()
    out_path = scorespage.write_scores_data(
        card_path=args.card,
        out_path=args.out,
        odds_df=odds_df,
        pm_quotes=pm_quotes,
        pm_scores=pm_scores,
        now_utc=now_utc,
    )

    data = scorespage.build_scores_data(
        args.card, odds_df=odds_df, pm_quotes=pm_quotes, pm_scores=pm_scores,
        now_utc=now_utc,
    )
    fixtures = data["fixtures"]
    n_with_venues = sum(1 for f in fixtures if f.get("venues"))
    n_with_pmscores = sum(
        1 for f in fixtures if any(s.get("pm_prob") is not None for s in f.get("scores") or [])
    )

    print(out_path)
    print(
        "fixtures=%d  with_venues=%d  pm_quotes=%d  pm_scores_fixtures=%d  quota_remaining=%s"
        % (
            len(fixtures),
            n_with_venues,
            len(pm_quotes),
            n_with_pmscores,
            "?" if quota is None else quota.remaining,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
