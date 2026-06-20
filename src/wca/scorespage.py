"""Model-vs-market scoreline page data builder for World Cup Alpha.

This module produces the structured JSON that the ``site/scores.html`` page
renders.  Two things are surfaced per fixture:

1. **Predicted scorelines** — the top-k correct-score ladder (score, model
   probability, fair decimal odds), taken verbatim from the cached matchday
   card (re-using :func:`wca.sitedata.parse_scorelines`).

2. **Model vs priced odds across venues** — for each fixture matched (by team
   name) to the supplied odds DataFrame, the per-bookmaker 1X2 (h2h) prices
   plus, optionally, a Polymarket-implied 1X2, compared against a *model*
   fair 1X2 derived from the scoreline matrix.

Important caveat
----------------
The card lists only the *top-k* scorelines, not the full Poisson matrix, so the
model 1X2 we reconstruct here (summing parsed-score probabilities into
home-win / draw / away-win buckets and renormalising) is **approximate**.  Every
fixture's venue block therefore carries ``"approx_1x2": True`` and the
front-end labels it accordingly.

Design notes
------------
* **Deterministic.**  :func:`build_scores_data` never reads the wall clock or
  the network; the caller supplies ``now_utc`` and any odds / market data.  The
  thin CLI (``scripts/wca_scores_data.py``) is the only place allowed to touch
  the clock and the network.
* **Tolerant.**  A missing card, a missing / empty ``odds_df`` and missing
  ``pm_quotes`` must never raise — fixtures are still emitted (with empty
  ``venues``) so the page shows a clean state.
* **Team-name aware.**  Card fixtures spell teams one way and the odds feed
  another; both sides are normalised through
  :func:`wca.data.teamnames.canonical` before matching.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from wca import modelpreds, sitedata
from wca.data import teamnames


# ---------------------------------------------------------------------------
# Fixture / team-name helpers.
# ---------------------------------------------------------------------------


def _canon(name: Any) -> str:
    """Canonicalise *name* for matching: alias-resolved and casefolded.

    Returns ``""`` for missing / non-string inputs (``None`` and, crucially,
    pandas float ``NaN`` cells from a malformed odds feed) so a missing team
    name never collides and never raises — preserving the module's "tolerant"
    contract when ``.map(_canon)`` runs over a DataFrame column.
    """
    if not isinstance(name, str):
        return ""
    canon = teamnames.canonical(name)
    if not isinstance(canon, str):
        return ""
    return canon.strip().casefold()


def _split_fixture(fixture: str) -> Optional[Tuple[str, str]]:
    """Split a card fixture string ``"Home vs Away"`` into ``(home, away)``.

    Accepts the common separators used in the card ("vs", "v", "vs.").  Returns
    ``None`` when no recognisable separator is present.
    """
    if not fixture:
        return None
    text = fixture.strip()
    lowered = text.lower()
    # Try the separators in order of specificity so "vs." wins over "v".
    for sep in (" vs. ", " vs ", " v. ", " v "):
        idx = lowered.find(sep)
        if idx != -1:
            home = text[:idx].strip()
            away = text[idx + len(sep):].strip()
            if home and away:
                return home, away
    return None


def _fixture_key(home: str, away: str) -> Tuple[str, str]:
    """Canonical, order-sensitive key for a (home, away) pair."""
    return (_canon(home), _canon(away))


# ---------------------------------------------------------------------------
# Model 1X2 reconstruction from the (top-k) scoreline ladder.
# ---------------------------------------------------------------------------


def _model_1x2_from_scores(scores: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Approximate a home/draw/away probability triple from parsed scorelines.

    Each parsed score looks like ``{"score": "2-1", "prob": 10.2, ...}`` where
    ``prob`` is a *percentage* (0..100).  We bucket by sign of (home - away),
    sum the probabilities, then renormalise to 1.0 across the three buckets so
    the triple is a proper distribution despite the card only listing top-k
    scores.

    Returns ``None`` when no usable score rows are present.
    """
    home = 0.0
    draw = 0.0
    away = 0.0
    total = 0.0
    for row in scores or []:
        score = (row.get("score") or "").strip()
        prob = row.get("prob")
        if prob is None:
            continue
        try:
            p = float(prob)
        except (TypeError, ValueError):
            continue
        if "-" not in score:
            continue
        h_str, _, a_str = score.partition("-")
        try:
            h_goals = int(h_str.strip())
            a_goals = int(a_str.strip())
        except (TypeError, ValueError):
            continue
        if h_goals > a_goals:
            home += p
        elif h_goals == a_goals:
            draw += p
        else:
            away += p
        total += p

    if total <= 0.0:
        return None
    return {"home": home / total, "draw": draw / total, "away": away / total}


def _edge(model_prob: Optional[float], decimal: Optional[float]) -> Optional[float]:
    """Expected-value edge of a priced bet vs the model: ``p*odds - 1``.

    Returns ``None`` when either input is missing / non-positive so the
    front-end renders a neutral cell rather than a misleading number.
    """
    if model_prob is None or decimal is None:
        return None
    try:
        p = float(model_prob)
        d = float(decimal)
    except (TypeError, ValueError):
        return None
    if d <= 0.0:
        return None
    return p * d - 1.0


def _implied_from_decimal(decimal: Optional[float]) -> Optional[float]:
    """Implied probability ``1/odds`` (un-de-vigged) for a decimal price."""
    if decimal is None:
        return None
    try:
        d = float(decimal)
    except (TypeError, ValueError):
        return None
    if d <= 0.0:
        return None
    return 1.0 / d


def _decimal_from_prob(prob: Optional[float]) -> Optional[float]:
    """Convert a 0..1 probability to a decimal price ``1/p``."""
    if prob is None:
        return None
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if p <= 0.0:
        return None
    return 1.0 / p


# ---------------------------------------------------------------------------
# Venue extraction from the odds DataFrame.
# ---------------------------------------------------------------------------


def _venue_block(
    selection_prices: Dict[str, Optional[float]],
    model_1x2: Optional[Dict[str, float]],
    venue: str,
) -> Dict[str, Any]:
    """Assemble one venue row: prices, implied probs and edge-vs-model."""
    implied: Dict[str, Optional[float]] = {}
    edge: Dict[str, Optional[float]] = {}
    for leg in ("home", "draw", "away"):
        dec = selection_prices.get(leg)
        implied[leg] = _implied_from_decimal(dec)
        model_prob = None if model_1x2 is None else model_1x2.get(leg)
        edge[leg] = _edge(model_prob, dec)
    return {
        "venue": venue,
        "selection_prices": dict(selection_prices),
        "implied": implied,
        "edge_vs_model": edge,
    }


def _h2h_venues_for_fixture(
    odds_df: Any,
    home: str,
    away: str,
    model_1x2: Optional[Dict[str, float]],
) -> Tuple[List[Dict[str, Any]], str]:
    """Build per-bookmaker venue blocks for one fixture from ``odds_df``.

    Matching is by canonicalised home/away team names against the DataFrame's
    ``home_team`` / ``away_team`` columns.  Only ``market == "h2h"`` rows are
    used.  The h2h outcome names are mapped to home/draw/away by comparing the
    outcome name against the event's home/away team (canonicalised); the
    remaining outcome ("Draw") is the draw leg.

    Returns ``(venues, kickoff)`` where ``kickoff`` is the first non-empty
    ``commence_time`` seen for the fixture (ISO string), or ``""``.
    """
    venues: List[Dict[str, Any]] = []
    kickoff = ""
    if odds_df is None:
        return venues, kickoff

    # Guard against a DataFrame that lacks the expected columns.
    required = {"home_team", "away_team", "market", "bookmaker_key",
                "outcome_name", "decimal_odds"}
    try:
        columns = set(odds_df.columns)
    except AttributeError:
        return venues, kickoff
    if not required.issubset(columns):
        return venues, kickoff

    home_c = _canon(home)
    away_c = _canon(away)

    # Filter to this fixture's h2h rows. We compare canonicalised names so the
    # card's "Bosnia and Herzegovina" matches the feed's "Bosnia & Herzegovina".
    mask = (
        (odds_df["market"] == "h2h")
        & odds_df["home_team"].map(_canon).eq(home_c)
        & odds_df["away_team"].map(_canon).eq(away_c)
    )
    sub = odds_df[mask]
    if sub.empty:
        return venues, kickoff

    # Kickoff: first non-empty commence_time.
    if "commence_time" in columns:
        for raw in sub["commence_time"].tolist():
            text = _iso_str(raw)
            if text:
                kickoff = text
                break

    # Group rows by bookmaker_key, preserving first-seen order.
    order: List[str] = []
    by_bookie: Dict[str, Dict[str, Optional[float]]] = {}
    for _, row in sub.iterrows():
        bookie = row.get("bookmaker_key")
        if bookie is None:
            continue
        bookie = str(bookie)
        leg = _leg_for_outcome(row.get("outcome_name"), home_c, away_c)
        if leg is None:
            continue
        price = _to_opt_float(row.get("decimal_odds"))
        if bookie not in by_bookie:
            by_bookie[bookie] = {"home": None, "draw": None, "away": None}
            order.append(bookie)
        # Keep the first non-null price per (bookie, leg) for determinism.
        if by_bookie[bookie].get(leg) is None:
            by_bookie[bookie][leg] = price

    for bookie in order:
        venues.append(_venue_block(by_bookie[bookie], model_1x2, bookie))

    return venues, kickoff


def _leg_for_outcome(
    outcome_name: Optional[str], home_c: str, away_c: str
) -> Optional[str]:
    """Map an h2h outcome name to ``"home"`` / ``"draw"`` / ``"away"``.

    Soccer h2h outcomes are the two team names plus the literal ``"Draw"``.
    """
    if outcome_name is None:
        return None
    name_c = _canon(outcome_name)
    if not name_c:
        return None
    if name_c == home_c:
        return "home"
    if name_c == away_c:
        return "away"
    if name_c == "draw":
        return "draw"
    return None


# ---------------------------------------------------------------------------
# Misc parsing helpers.
# ---------------------------------------------------------------------------


def _to_opt_float(value: Any) -> Optional[float]:
    """Coerce a value to float, returning ``None`` on failure / None / NaN."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN check without importing math.
        return None
    return result


def _iso_str(value: Any) -> str:
    """Render a commence_time cell (pandas Timestamp / str / None) to an ISO
    string, or ``""`` when missing / NaT.

    Note: ``pandas.NaT`` is missing-but-truthy and, in pandas 2.x,
    ``NaT.isoformat()`` returns the literal string ``"NaT"`` (it does not
    raise).  A float ``NaN`` is also missing.  We therefore test for
    missing-ness with ``value != value`` (true only for NaT / NaN) *before*
    the ``isoformat`` branch, otherwise a coerced-to-NaT commence_time would
    leak ``"NaT"`` onto the page as a bogus kickoff."""
    if value is None:
        return ""
    # NaT and NaN are the only values not equal to themselves.
    try:
        if value != value:
            return ""
    except (TypeError, ValueError):  # pragma: no cover - exotic cell types
        pass
    if hasattr(value, "isoformat"):
        try:
            return str(value.isoformat())
        except (ValueError, AttributeError):
            return ""
    text = str(value).strip()
    if text.lower() in ("nat", "nan", "none"):
        return ""
    return text


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def build_scores_data(
    card_path: str,
    odds_df: Any = None,
    pm_quotes: Optional[Dict[str, Dict[str, float]]] = None,
    pm_scores: Optional[Dict[str, Dict[str, float]]] = None,
    now_utc: str = "",
    model_preds_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the scores-page payload.

    Parameters
    ----------
    card_path:
        Path to the cached matchday card; its scorelines section provides the
        predicted scoreline ladders.  Missing / empty cards yield an empty
        ``fixtures`` list (never raises).
    odds_df:
        Optional pandas DataFrame of odds with at least the columns
        ``home_team, away_team, market, bookmaker_key, outcome_name,
        decimal_odds`` (and optionally ``commence_time``) — the exact shape
        returned by :func:`wca.data.theoddsapi.get_odds`.  ``None`` (or a frame
        that does not match a fixture) simply yields empty ``venues``.
    pm_quotes:
        Optional mapping of fixture string -> ``{"home": p, "draw": p,
        "away": p}`` where each ``p`` is a 0..1 probability (e.g. Polymarket
        mid-price).  Converted to a decimal venue named ``"polymarket"``.  Keys
        are matched both verbatim and after team-name canonicalisation.
    now_utc:
        Pre-formatted generation timestamp (the caller stamps the clock).
    model_preds_path:
        Path to the persisted model-predictions snapshot written at card-build
        time (``data/model_predictions.json``).  When a fixture matches, its
        exact blended 1X2 replaces the top-k scoreline reconstruction and
        ``approx_1x2`` is reported as ``False``.  ``None`` uses the default
        location; a missing/malformed file silently falls back.

    Returns
    -------
    dict
        ::

            {
              "meta": {"generated": now_utc},
              "fixtures": [
                {
                  "fixture": "Mexico vs South Africa",
                  "kickoff": "2026-06-12T18:00:00+00:00",  # if derivable
                  "scores": [{"score","prob","fair"}, ...],
                  "over_under": {...} | None,
                  "btts": float | None,
                  "model_1x2": {"home","draw","away"} | None,
                  "approx_1x2": True,
                  "venues": [
                    {
                      "venue": "betfair_ex_uk",
                      "selection_prices": {"home","draw","away"},
                      "implied": {"home","draw","away"},
                      "edge_vs_model": {"home","draw","away"},
                    },
                    ...
                  ],
                },
                ...
              ],
            }
    """
    pm_quotes = pm_quotes or {}

    # Pre-index the pm_quotes by canonical fixture key for tolerant matching.
    pm_by_key: Dict[Tuple[str, str], Dict[str, float]] = {}
    for fx_str, quote in pm_quotes.items():
        if not isinstance(quote, dict):
            continue
        pair = _split_fixture(fx_str)
        if pair is None:
            continue
        pm_by_key[_fixture_key(pair[0], pair[1])] = quote

    # Per-scoreline Polymarket exact-score probabilities (fixture -> {"H-A": p}),
    # indexed by canonical fixture key like the 1X2 quotes above.
    pm_scores = pm_scores or {}
    pm_scores_by_key: Dict[Tuple[str, str], Dict[str, float]] = {}
    for fx_str, smap in pm_scores.items():
        if not isinstance(smap, dict):
            continue
        pair = _split_fixture(fx_str)
        if pair is None:
            continue
        pm_scores_by_key[_fixture_key(pair[0], pair[1])] = smap

    # Exact blended 1X2 persisted at card-build time, indexed verbatim and by
    # canonical fixture key for tolerant matching.
    exact_preds = modelpreds.load_latest(
        model_preds_path if model_preds_path is not None else modelpreds.LATEST_PATH
    )
    exact_by_key: Dict[Tuple[str, str], Dict[str, float]] = {}
    for fx_str, triple in exact_preds.items():
        pair = _split_fixture(fx_str)
        if pair is not None:
            exact_by_key[_fixture_key(pair[0], pair[1])] = triple

    card_body = _read_card_body(card_path)
    parsed = sitedata.parse_scorelines(card_body)

    fixtures: List[Dict[str, Any]] = []
    for fx in parsed:
        fixture_str = fx.get("fixture") or ""
        scores_in = fx.get("scores") or []
        # Drop the per-score "back" column from the public payload — the scores
        # page shows score / prob / fair, plus the Polymarket exact-score
        # probability (pm_prob, percent) when a PM correct-score market exists.
        pm_sc = pm_scores.get(fixture_str)
        if pm_sc is None:
            pair_ps = _split_fixture(fixture_str)
            if pair_ps is not None:
                pm_sc = pm_scores_by_key.get(_fixture_key(pair_ps[0], pair_ps[1]))
        pm_sc = pm_sc or {}
        scores = []
        for s in scores_in:
            row = {"score": s.get("score"), "prob": s.get("prob"), "fair": s.get("fair")}
            pmp = pm_sc.get(str(s.get("score")))
            if pmp is not None:
                row["pm_prob"] = round(float(pmp) * 100.0, 1)
            scores.append(row)

        exact = exact_preds.get(fixture_str)
        if exact is None:
            pair_for_exact = _split_fixture(fixture_str)
            if pair_for_exact is not None:
                exact = exact_by_key.get(
                    _fixture_key(pair_for_exact[0], pair_for_exact[1])
                )
        model_1x2 = exact if exact is not None else _model_1x2_from_scores(scores_in)

        venues: List[Dict[str, Any]] = []
        kickoff = ""

        pair = _split_fixture(fixture_str)
        if pair is not None:
            home, away = pair
            venues, kickoff = _h2h_venues_for_fixture(
                odds_df, home, away, model_1x2
            )

            # Polymarket venue (if a quote exists for this fixture).
            quote = pm_by_key.get(_fixture_key(home, away))
            if quote is None:
                quote = pm_quotes.get(fixture_str)  # verbatim fallback
            if isinstance(quote, dict):
                pm_prices = {
                    "home": _decimal_from_prob(quote.get("home")),
                    "draw": _decimal_from_prob(quote.get("draw")),
                    "away": _decimal_from_prob(quote.get("away")),
                }
                venues.append(_venue_block(pm_prices, model_1x2, "polymarket"))

        entry: Dict[str, Any] = {
            "fixture": fixture_str,
            "scores": scores,
            "over_under": fx.get("over_under"),
            "btts": fx.get("btts"),
            "model_1x2": model_1x2,
            "approx_1x2": exact is None,
            "venues": venues,
        }
        if kickoff:
            entry["kickoff"] = kickoff
        fixtures.append(entry)

    return {"meta": {"generated": now_utc}, "fixtures": fixtures}


def write_scores_data(
    card_path: str = "data/card_latest.md",
    out_path: str = "site/scores_data.json",
    odds_df: Any = None,
    pm_quotes: Optional[Dict[str, Dict[str, float]]] = None,
    pm_scores: Optional[Dict[str, Dict[str, float]]] = None,
    now_utc: str = "",
    model_preds_path: Optional[str] = None,
) -> str:
    """Build the scores payload and write it to ``out_path`` as JSON.

    Parent directories are created as needed.  Returns ``out_path``.
    """
    data = build_scores_data(
        card_path,
        odds_df=odds_df,
        pm_quotes=pm_quotes,
        pm_scores=pm_scores,
        now_utc=now_utc,
        model_preds_path=model_preds_path,
    )

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    return out_path


# ---------------------------------------------------------------------------
# Internal: card reading (mirrors wca.sitedata behaviour).
# ---------------------------------------------------------------------------


def _read_card_body(card_path: str) -> str:
    """Return the card body with any ``<!-- generated: ... -->`` header line
    stripped, or ``""`` when the file is missing / unreadable."""
    if not card_path or not os.path.exists(card_path):
        return ""
    try:
        with open(card_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return ""
    first, _, rest = raw.partition("\n")
    if first.startswith("<!-- generated:") and first.rstrip().endswith("-->"):
        return rest
    return raw
