#!/usr/bin/env python
"""Build the full PM event-markets feeds (forest + trade recs).

Writes TWO artefacts from one live snapshot:

* ``site/forest_data.json``   — the Event-Markets forest (model vs market per
  outcome, EVERY market family Polymarket lists for each upcoming fixture:
  1X2, goals O/U at every listed line, BTTS, exact score, spreads / winning
  margins, team totals, extra time, Team to Advance, plus honest market-only
  rows for families the model cannot price yet — halves, first-to-score,
  corners, penalty shootout).
* ``site/event_market_recs.json`` — governed trade recs over those markets
  (wca.selection ordering, longshot cash floor, PM fee 0.03·p·(1−p), 2pp net
  edge floor, quarter-Kelly on the PM pool, $160/order cap, same-fixture
  correlation cap, kill-list). See :func:`wca.eventmarkets.build_event_market_recs`.

Model pricing (nothing is invented)
-----------------------------------
* 1X2 comes verbatim from the persisted card blend
  (``data/model_predictions.json``, written at card-build time).
* The scoreline grid is rebuilt EXACTLY as the card builds it:
  ``wca.card.fit_models`` on the full results history with the production
  ``DEFAULT_DC_LEVEL_TARGET`` anchor, ``dc.predict`` per fixture, reconciled to
  the persisted blended 1X2 via ``wca.models.scores.reconcile_scoreline_matrix``.
  The fitted lambdas are cross-checked against the lambdas persisted in the
  predictions snapshot and the check result is stamped into the feed meta.
* Totals / BTTS / exact-score fair values are market-blended
  (60% de-vigged market reference) per the 2026-07-08 calibration study — the
  DC grid ties the market on Brier, so it anchors, never overrides. Raw DC is
  only shown where no market reference exists, and is labelled as such.
* Anytime-scorer props are priced by ``wca.models.playerprops`` (rates fall
  back to structural priors when players.db is absent; the rate source is
  stamped per row so the mini build upgrades them).
* "Team to Advance" model probabilities come from the advancement MC feed
  (``site/advancement_data.json``) and settle ET+pens — they sit in their own
  clearly-labelled section, never mixed into 90-minute rows.

Market prices: CLOB top-of-book mid per token
(``wca.data.pm_clob_history.top_of_book``), falling back to the Gamma
bestBid/bestAsk mid and then Gamma outcomePrices. Every row records
``price_source`` and ``captured_utc``. Where PM lists no market for an
outcome the row says ``"no PM market"`` — never a bare "model" tag.

Usage::

    PYTHONPATH=src python3 scripts/wca_event_markets.py \
        [--days-ahead 7] [--out-forest site/forest_data.json] \
        [--out-recs site/event_market_recs.json] [--no-fit] [--db data/wca.db]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import warnings
from typing import Any, Dict, List, Optional, Tuple

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import eventmarkets as EM  # noqa: E402
from wca.data import polymarket as PM  # noqa: E402
from wca.data.teamnames import canonical  # noqa: E402

_PREDS_PATH = os.path.join(_ROOT, "data", "model_predictions.json")
_ADV_PATH = os.path.join(_ROOT, "site", "advancement_data.json")
_PLAYERS_JSON = os.path.join(_ROOT, "data", "players.json")
_PLAYERS_DB = os.path.join(_ROOT, "data", "players.db")

#: Stage order used to resolve "the tie being decided" from the advancement MC
#: feed (feed meta carries the same list; this is the fallback).
_ADV_STAGES = ("R32", "R16", "QF", "SF", "Final", "win")


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fixtures from the persisted card predictions.
# ---------------------------------------------------------------------------


def load_upcoming_fixtures(preds_path: str, days_ahead: float,
                           now: Optional[dt.datetime] = None) -> List[Dict[str, Any]]:
    """Upcoming fixtures (with exact blended 1X2 + DC lambdas) from the card
    predictions snapshot. Past fixtures (>3h ago) are dropped."""
    now = now or _now_utc()
    try:
        data = json.load(open(preds_path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: List[Dict[str, Any]] = []
    for fx in data.get("fixtures") or []:
        fixture = fx.get("fixture") or ""
        if " vs " not in fixture:
            continue
        ko_raw = fx.get("kickoff")
        ko = None
        if ko_raw:
            try:
                ko = dt.datetime.fromisoformat(str(ko_raw))
                if ko.tzinfo is None:
                    ko = ko.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                ko = None
        if ko is not None:
            if ko < now - dt.timedelta(hours=3):
                continue
            if ko > now + dt.timedelta(hours=24 * days_ahead):
                continue
        home, _, away = fixture.partition(" vs ")
        out.append({
            "fixture": fixture,
            "home": home.strip(),
            "away": away.strip(),
            "kickoff": ko.isoformat() if ko else "",
            "model_1x2": fx.get("model") or {},
            "lambda_home": fx.get("lambda_home"),
            "lambda_away": fx.get("lambda_away"),
            "preds_generated": fx.get("generated"),
        })
    return out


# ---------------------------------------------------------------------------
# Production model grid (fit exactly as the card does).
# ---------------------------------------------------------------------------


def fit_production_dc():
    """Fit the production Dixon-Coles (card path, level-anchored). ~2.5 min."""
    from wca.data.results import load_results
    from wca.data.cleaning import resolve_results_path
    from wca.card import fit_models, DEFAULT_DC_LEVEL_TARGET

    results = load_results(resolve_results_path())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        models = fit_models(results, dc_level_target=DEFAULT_DC_LEVEL_TARGET)
    return models.dc


def reconciled_matrix(dc, fx: Dict[str, Any]) -> Tuple[Optional[Any], Dict[str, Any]]:
    """Card-consistent reconciled scoreline grid + lambda cross-check.

    ``dc.predict(home, away, neutral=True)`` (all remaining WC fixtures are
    neutral-venue — no host nation left in the bracket is asserted by the
    lambda check), reconciled to the persisted blended 1X2. Returns
    ``(matrix, check)`` where ``check`` records the fitted-vs-persisted lambda
    agreement; on any failure returns ``(None, check_with_reason)``.
    """
    from wca.models.scores import reconcile_scoreline_matrix

    home, away = fx["home"], fx["away"]
    trip = fx.get("model_1x2") or {}
    target = (trip.get("home"), trip.get("draw"), trip.get("away"))
    if any(v is None for v in target):
        return None, {"ok": False, "reason": "no persisted blended 1X2"}
    try:
        pred = dc.predict(home, away, neutral=True, warn=False)
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "reason": "dc.predict failed: %s" % exc}

    check: Dict[str, Any] = {"ok": True}
    lam_h, lam_a = fx.get("lambda_home"), fx.get("lambda_away")
    if lam_h is not None and lam_a is not None:
        dh = abs(pred.lambda_home - float(lam_h))
        da = abs(pred.lambda_away - float(lam_a))
        check = {
            "ok": bool(dh < 5e-3 and da < 5e-3),
            "fitted": [round(pred.lambda_home, 6), round(pred.lambda_away, 6)],
            "persisted": [float(lam_h), float(lam_a)],
        }
        if not check["ok"]:
            check["reason"] = ("fitted lambdas diverge from the card snapshot "
                               "— results history or neutral flag drifted")
    try:
        matrix = reconcile_scoreline_matrix(
            pred.matrix, tuple(float(v) for v in target),
            lambdas=(pred.lambda_home, pred.lambda_away))
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "reason": "reconcile failed: %s" % exc}
    return matrix, check


# ---------------------------------------------------------------------------
# Live PM enumeration.
# ---------------------------------------------------------------------------


def fetch_soccer_events() -> List[Dict[str, Any]]:
    """All open Gamma events tagged soccer (paginated), markets decoded."""
    events: List[Dict[str, Any]] = []
    offset, limit = 0, 100
    while True:
        try:
            page = PM._get("/events", params={
                "limit": limit, "offset": offset,
                "tag_slug": "soccer", "closed": "false",
            })
        except requests.HTTPError as exc:
            # Gamma's /events paginator has an undocumented offset ceiling
            # (observed: offset=2000 -> 200, offset=2100 -> 422, stable/
            # reproducible, 2026-07-13) — same class of limit already known
            # on the data-api /trades endpoint (caps at offset 3000). Past
            # it every subsequent page 422s too, so read it as "no more
            # results" and keep what's already collected, matching the fix
            # in wca.data.polymarket.find_world_cup_markets.
            status = exc.response.status_code if exc.response is not None else None
            if status == 422:
                break
            raise
        if not isinstance(page, list):
            page = page.get("data", page.get("events", [])) or []
        if not page:
            break
        for ev in page:
            e = dict(ev)
            e["markets"] = [PM._parse_market_prices(m) for m in (e.get("markets") or [])]
            events.append(e)
        offset += limit
        if len(page) < limit:
            break
    return events


def fetch_fixture_search_events(home: str, away: str) -> List[Dict[str, Any]]:
    """Fetch newly-created fixture events missed by Gamma's offset ceiling.

    The soccer catalogue is capped before some newly listed World Cup
    fixtures.  Gamma's public-search endpoint indexes those events directly,
    so use it as a targeted supplement rather than treating an empty match as
    evidence that Polymarket has no market.
    """
    query = "%s %s" % (home, away)
    result = PM._get("/public-search", params={"q": query})
    candidates = result.get("events", []) if isinstance(result, dict) else []
    out: List[Dict[str, Any]] = []
    want = {canonical(home), canonical(away)}
    seen = set()
    main_slugs: List[str] = []
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        ev = dict(raw)
        if not (ev.get("slug") or "").startswith("fifwc-"):
            continue
        teams = _title_teams(ev.get("title") or "")
        if not teams or {canonical(teams[0]), canonical(teams[1])} != want:
            continue
        key = ev.get("id") or ev.get("slug")
        if key in seen:
            continue
        seen.add(key)
        ev["markets"] = [PM._parse_market_prices(m)
                         for m in (ev.get("markets") or [])]
        out.append(ev)
        if not (ev.get("slug") or "").endswith(tuple(s for s, _ in EM.EVENT_KIND_SUFFIXES)):
            main_slugs.append(ev["slug"])

    # public-search often returns only the main event even though the related
    # halftime, exact-score, corners, and player-props slugs already exist.
    # Fetch those deterministic siblings directly.
    for stem in main_slugs:
        for suffix, _kind in EM.EVENT_KIND_SUFFIXES:
            slug = stem + suffix
            if slug in seen:
                continue
            try:
                ev = PM._get("/events/slug/%s" % slug)
            except Exception:
                continue
            if not isinstance(ev, dict):
                continue
            teams = _title_teams(ev.get("title") or "")
            if not teams or {canonical(teams[0]), canonical(teams[1])} != want:
                continue
            ev["markets"] = [PM._parse_market_prices(m)
                             for m in (ev.get("markets") or [])]
            seen.add(slug)
            out.append(ev)
    return out


def _title_teams(title: str) -> Optional[Tuple[str, str]]:
    """Parse "<Home> vs. <Away>[ - Suffix]" into the two team names."""
    head = (title or "").split(" - ")[0]
    head = head.replace(" vs. ", " vs ")
    if " vs " not in head:
        return None
    a, _, b = head.partition(" vs ")
    a, b = a.strip(), b.strip()
    return (a, b) if a and b else None


def events_for_fixture(events: List[Dict[str, Any]], home: str, away: str
                       ) -> Dict[str, List[Dict[str, Any]]]:
    """Group a fixture's PM events by market-family kind (slug suffix)."""
    want = {canonical(home), canonical(away)}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        slug = (ev.get("slug") or "")
        if not slug.startswith("fifwc-"):
            continue
        teams = _title_teams(ev.get("title") or "")
        if teams is None:
            continue
        if {canonical(teams[0]), canonical(teams[1])} != want:
            continue
        kind = EM.event_kind_from_slug(slug)
        out.setdefault(kind, []).append(ev)
    return out


def market_quote(market: Dict[str, Any], outcome_index: int,
                 *, use_clob: bool = True) -> Optional[Dict[str, Any]]:
    """Best available quote for one outcome of a PM market.

    Preference: CLOB top-of-book (mid + bid/ask), then the Gamma
    bestBid/bestAsk mid, then the Gamma outcomePrices entry (last-trade-ish).
    Gamma bestBid/bestAsk refer to outcome[0]; for outcome[1] they are
    mirrored (1-ask, 1-bid). Returns ``None`` when nothing usable exists.
    """
    token_ids = PM._parse_json_array(market.get("clobTokenIds")) or []
    outcomes = PM._parse_json_array(market.get("outcomes")) or []
    if outcome_index >= len(token_ids):
        return None
    token = str(token_ids[outcome_index])
    name = str(outcomes[outcome_index]) if outcome_index < len(outcomes) else ""

    if use_clob:
        try:
            from wca.data.pm_clob_history import top_of_book

            book = top_of_book(token)
        except Exception:  # noqa: BLE001
            book = None
        if book and book.get("mid") is not None:
            return {
                "token_id": token, "outcome": name,
                "mid": float(book["mid"]),
                "bid": book.get("bid"), "ask": book.get("ask"),
                "price_source": "clob_mid",
            }

    def _f(v):
        try:
            out = float(v)
        except (TypeError, ValueError):
            return None
        return out if out == out else None

    bb, ba = _f(market.get("bestBid")), _f(market.get("bestAsk"))
    if bb is not None and ba is not None and 0.0 < bb and ba < 1.0:
        if outcome_index == 1:  # gamma bid/ask quote outcome[0]; mirror.
            bb, ba = 1.0 - ba, 1.0 - bb
        return {
            "token_id": token, "outcome": name,
            "mid": (bb + ba) / 2.0, "bid": bb, "ask": ba,
            "price_source": "gamma_mid",
        }
    prices = PM._parse_json_array(market.get("outcomePrices")) or []
    if outcome_index < len(prices):
        p = _f(prices[outcome_index])
        if p is not None and 0.0 < p < 1.0:
            return {
                "token_id": token, "outcome": name,
                "mid": p, "bid": None, "ask": None,
                "price_source": "gamma_outcome_price",
            }
    return None


# ---------------------------------------------------------------------------
# Advancement (Team to Advance) model probabilities.
# ---------------------------------------------------------------------------


def load_advancement_model(adv_path: str) -> Tuple[Dict[str, Dict[str, float]], str]:
    """``{canonical team -> {stage -> model prob}}`` + the sim stamp."""
    try:
        data = json.load(open(adv_path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}, ""
    stamp = (data.get("meta") or {}).get("model_generated") or ""
    out: Dict[str, Dict[str, float]] = {}
    for t in data.get("teams") or []:
        name = t.get("team")
        model = t.get("model") or {}
        if name:
            out[canonical(name)] = {k: v for k, v in model.items()
                                    if isinstance(v, (int, float))}
    return out, stamp


def advance_model_probs(adv: Dict[str, Dict[str, float]], home: str, away: str
                        ) -> Tuple[Optional[float], Optional[float], str]:
    """Model P(advance) for the two teams of one KO tie from the MC feed.

    The tie being decided is the first stage (in bracket order) where BOTH
    teams' reach-probability is < 1.0; the two must complement to ~1 (same
    tie). Returns ``(p_home, p_away, note)``; ``(None, None, reason)`` when the
    pairing cannot be honestly resolved (e.g. stale sim after a bracket flip).
    """
    th = adv.get(canonical(home))
    ta = adv.get(canonical(away))
    if not th or not ta:
        return None, None, "team missing from advancement MC feed"
    for stage in _ADV_STAGES:
        ph, pa = th.get(stage), ta.get(stage)
        if ph is None or pa is None:
            continue
        if ph >= 1.0 and pa >= 1.0:
            continue
        if abs((ph + pa) - 1.0) <= 0.03:
            return float(ph), float(pa), "stage=%s" % stage
        return None, None, ("advancement sim pairing mismatch at %s "
                            "(p_home+p_away=%.3f) — re-run the sim"
                            % (stage, ph + pa))
    return None, None, "no undecided stage found in advancement MC feed"


# ---------------------------------------------------------------------------
# Row construction.
# ---------------------------------------------------------------------------


def _row(label: str, model: Optional[float], market: Optional[float],
         *, family: str, settlement: str, model_source: str = "",
         model_null_reason: Optional[str] = None,
         market_null_reason: Optional[str] = None,
         quote: Optional[Dict[str, Any]] = None,
         captured_utc: str = "", components: Optional[Dict[str, Any]] = None,
         dimmed: bool = False, warning: Optional[str] = None) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "label": label,
        "model": None if model is None else round(float(model), 6),
        "market": None if market is None else round(float(market), 6),
        "family": family,
        "settlement": settlement,
    }
    e = EM.edge_pp(model, market)
    row["edge_pp"] = None if e is None else round(e, 2)
    row["signal"] = EM.signal_for(model, market)
    if model_source:
        row["model_source"] = model_source
    if model is None:
        row["model_null_reason"] = model_null_reason or "not priced"
    if market is None:
        row["market_null_reason"] = market_null_reason or "no PM market"
    if quote:
        row["token_id"] = quote.get("token_id")
        row["price_source"] = quote.get("price_source")
        if quote.get("bid") is not None:
            row["bid"] = round(float(quote["bid"]), 4)
        if quote.get("ask") is not None:
            row["ask"] = round(float(quote["ask"]), 4)
        row["captured_utc"] = captured_utc
    if components:
        row["model_components"] = components
    if dimmed or (model is not None and EM.longshot_no_cash(model)):
        row["dimmed"] = True
    # totals-family under/lay warning travels with the row.
    w = warning or EM.totals_under_warning(family, row.get("signal") == "lay")
    if w:
        row["warning"] = w
    return row


def _candidate(fx: Dict[str, Any], row: Dict[str, Any], side: str,
               selection: str, q: float, price: float,
               price_source: str, token_id: Optional[str]) -> Dict[str, Any]:
    return {
        "fixture": fx["fixture"],
        "kickoff": fx.get("kickoff") or "",
        "family": row["family"],
        "label": row["label"],
        "side": side,
        "selection": selection,
        "settlement": row["settlement"],
        "model_prob": q,
        "price": price,
        "token_id": token_id,
        "price_source": price_source,
        "captured_utc": row.get("captured_utc") or "",
        "model_source": row.get("model_source") or "",
    }


def _push_candidates(cands: List[Dict[str, Any]], fx: Dict[str, Any],
                     row: Dict[str, Any], quote: Optional[Dict[str, Any]],
                     complement_label: str, *, include_lay: bool = True) -> None:
    """BACK + LAY candidates for a priced row (executable side prices).

    BACK buys the row's outcome at its ask (fallback mid); LAY buys the
    complement at ``1 - bid`` (the mirrored book price on a PM binary),
    labelled with its own price source. Rows missing model or market are
    skipped — no invented numbers. ``include_lay=False`` suppresses the lay
    side where the complement is itself a listed outcome (Team to Advance —
    the other team's BACK row already covers it) or the family is killed for
    cash anyway (exact score / scorer props — lay clutter with zero stake).
    """
    if row.get("model") is None or row.get("market") is None or not quote:
        return
    q = float(row["model"])
    ask = quote.get("ask")
    bid = quote.get("bid")
    # EXECUTABLE prices only — no mid fallbacks in the recs. A back needs a
    # real ask to lift; a lay needs a real bid to mirror (1-bid is the
    # complement's executable cost on a PM binary). One-sided junk books
    # (ask-only dust quotes) produced phantom "edges" otherwise; those rows
    # stay on the forest for display but never become trade candidates.
    if ask is not None and 0.0 < float(ask) < 1.0:
        cands.append(_candidate(
            fx, row, "back", row["label"], q, float(ask),
            "clob_ask" if quote.get("price_source") == "clob_mid" else "gamma_ask",
            quote.get("token_id")))
    if not include_lay:
        return
    if bid is not None and 0.0 < float(bid) < 1.0:
        cands.append(_candidate(
            fx, row, "lay", complement_label, 1.0 - q, 1.0 - float(bid),
            "clob_mirror(1-bid)" if quote.get("price_source") == "clob_mid"
            else "gamma_mirror(1-bid)",
            None))


# ---------------------------------------------------------------------------
# Per-fixture build.
# ---------------------------------------------------------------------------

_UNPRICEABLE_SECTIONS = (
    ("halftime_result", "Halftime Result"),
    ("second_half_result", "Second Half Result"),
    ("first_to_score", "First Team to Score"),
    ("total_corners", "Corners"),
)


def build_fixture(fx: Dict[str, Any], matrix, lam_check: Dict[str, Any],
                  pm_by_kind: Dict[str, List[Dict[str, Any]]],
                  adv_model: Dict[str, Dict[str, float]], adv_stamp: str,
                  captured_utc: str, *, use_clob: bool = True,
                  scorer_pricer=None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """One fixture's forest entry + its trade-rec candidates."""
    home, away = fx["home"], fx["away"]
    rows: List[Dict[str, Any]] = []
    cands: List[Dict[str, Any]] = []
    grid_src = "dc_grid_reconciled(card 1X2 blend)"

    def quote_for(market, idx):
        return market_quote(market, idx, use_clob=use_clob)

    # ---- 1X2 (main event) ---------------------------------------------------
    rows.append({"section": "1X2", "settlement": EM.SETTLE_90MIN,
                 "note": "settles on the 90'+stoppage score"})
    trip = fx.get("model_1x2") or {}
    main_events = pm_by_kind.get("main") or []
    legs_done = set()
    for ev in main_events:
        for m in ev.get("markets") or []:
            desc = EM.classify_pm_market("main", m.get("groupItemTitle") or "",
                                         m.get("question") or "", home, away)
            if desc.get("family") != "1x2" or not desc.get("priceable"):
                continue
            leg = desc["leg"]
            legs_done.add(leg)
            outcomes = PM._parse_json_array(m.get("outcomes")) or []
            yes_idx = 0
            for i, o in enumerate(outcomes):
                if str(o).strip().lower() == "yes":
                    yes_idx = i
                    break
            quote = quote_for(m, yes_idx)
            label = {"home": home, "away": away, "draw": "Draw"}[leg]
            model = trip.get(leg)
            row = _row(label, model, quote and quote.get("mid"),
                       family="1x2", settlement=EM.SETTLE_90MIN,
                       model_source="card blend (Elo/DC/market, persisted at card build)",
                       quote=quote, captured_utc=captured_utc,
                       market_null_reason="no PM market")
            rows.append(row)
            _push_candidates(cands, fx, row, quote, "%s — No" % label)
    for leg in ("home", "draw", "away"):
        if leg in legs_done:
            continue
        label = {"home": home, "away": away, "draw": "Draw"}[leg]
        rows.append(_row(label, trip.get(leg), None, family="1x2",
                         settlement=EM.SETTLE_90MIN,
                         model_source="card blend (Elo/DC/market, persisted at card build)",
                         market_null_reason="no PM market"))

    # ---- goals O/U (more-markets) -------------------------------------------
    ou_markets: List[Tuple[float, Dict[str, Any]]] = []
    btts_market = None
    spread_markets: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    team_total_markets: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    advance_market = None
    extra_time_market = None
    other_unpriced: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for ev in pm_by_kind.get("more_markets") or []:
        for m in ev.get("markets") or []:
            desc = EM.classify_pm_market("more_markets",
                                         m.get("groupItemTitle") or "",
                                         m.get("question") or "", home, away)
            fam = desc.get("family")
            if fam == "total_goals":
                ou_markets.append((desc["line"], m))
            elif fam == "btts":
                btts_market = m
            elif fam == "spread" and desc.get("priceable"):
                spread_markets.append((desc, m))
            elif fam == "team_total" and desc.get("priceable"):
                team_total_markets.append((desc, m))
            elif fam == "advance":
                advance_market = m
            elif fam == "extra_time":
                extra_time_market = m
            else:
                other_unpriced.append((desc, m))

    rows.append({"section": "Goals O/U", "settlement": EM.SETTLE_90MIN,
                 "note": ("fair value = 40% DC grid + 60% market "
                          "(2026-07-08 calibration: grid ties market on Brier); "
                          "under-side signals are display-only")})
    ou_lines_done = set()
    for line, m in sorted(ou_markets, key=lambda t: t[0]):
        quote = quote_for(m, 0)  # outcome[0] = Over
        mid = quote and quote.get("mid")
        grid = None if matrix is None else EM.prob_over(matrix, line)
        blend = EM.blend_with_market(grid, mid)
        row = _row("Over %.1f" % line, blend["prob"], mid,
                   family="total_goals", settlement=EM.SETTLE_90MIN,
                   model_source=blend["source"],
                   model_null_reason=(None if grid is not None
                                      else "model fit unavailable"),
                   quote=quote, captured_utc=captured_utc,
                   components=blend["components"])
        rows.append(row)
        ou_lines_done.add(line)
        _push_candidates(cands, fx, row, quote, "Under %.1f" % line)
    if 2.5 not in ou_lines_done:
        grid = None if matrix is None else EM.prob_over(matrix, 2.5)
        blend = EM.blend_with_market(grid, None)
        rows.append(_row("Over 2.5", blend["prob"], None, family="total_goals",
                         settlement=EM.SETTLE_90MIN, model_source=blend["source"],
                         model_null_reason=(None if grid is not None
                                            else "model fit unavailable"),
                         market_null_reason="no PM market",
                         components=blend["components"]))

    # ---- BTTS ----------------------------------------------------------------
    rows.append({"section": "BTTS", "settlement": EM.SETTLE_90MIN})
    grid_btts = None if matrix is None else EM.prob_btts(matrix)
    if btts_market is not None:
        quote = quote_for(btts_market, 0)  # outcome[0] = Yes
        mid = quote and quote.get("mid")
        blend = EM.blend_with_market(grid_btts, mid)
        row = _row("BTTS — Yes", blend["prob"], mid, family="btts",
                   settlement=EM.SETTLE_90MIN, model_source=blend["source"],
                   model_null_reason=(None if grid_btts is not None
                                      else "model fit unavailable"),
                   quote=quote, captured_utc=captured_utc,
                   components=blend["components"])
        rows.append(row)
        _push_candidates(cands, fx, row, quote, "BTTS — No")
    else:
        blend = EM.blend_with_market(grid_btts, None)
        rows.append(_row("BTTS — Yes", blend["prob"], None, family="btts",
                         settlement=EM.SETTLE_90MIN, model_source=blend["source"],
                         model_null_reason=(None if grid_btts is not None
                                            else "model fit unavailable"),
                         market_null_reason="no PM market",
                         components=blend["components"]))

    # ---- spreads / winning margins -------------------------------------------
    if spread_markets:
        rows.append({"section": "Spreads / Winning Margin",
                     "settlement": EM.SETTLE_90MIN,
                     "note": "grid-priced off the card-reconciled matrix"})
        for desc, m in spread_markets:
            quote = quote_for(m, 0)
            mid = quote and quote.get("mid")
            model = (None if matrix is None else
                     EM.prob_margin_at_least(matrix, desc["margin"], desc["side"]))
            row = _row(desc["label"], model, mid, family="spread",
                       settlement=EM.SETTLE_90MIN, model_source=grid_src,
                       model_null_reason=(None if model is not None
                                          else "model fit unavailable"),
                       quote=quote, captured_utc=captured_utc)
            rows.append(row)
            _push_candidates(cands, fx, row, quote, "%s — other side" % desc["label"])

    # ---- team totals -----------------------------------------------------------
    if team_total_markets:
        rows.append({"section": "Team Totals", "settlement": EM.SETTLE_90MIN,
                     "note": ("fair value = 40% DC grid + 60% market; "
                              "under-side signals are display-only")})
        for desc, m in sorted(team_total_markets,
                              key=lambda t: (t[0]["side"], t[0]["line"])):
            quote = quote_for(m, 0)  # outcome[0] = Over
            mid = quote and quote.get("mid")
            grid = (None if matrix is None else
                    EM.prob_team_over(matrix, desc["line"], desc["side"]))
            blend = EM.blend_with_market(grid, mid)
            row = _row(desc["label"], blend["prob"], mid, family="team_total",
                       settlement=EM.SETTLE_90MIN, model_source=blend["source"],
                       model_null_reason=(None if grid is not None
                                          else "model fit unavailable"),
                       quote=quote, captured_utc=captured_utc,
                       components=blend["components"])
            rows.append(row)
            _push_candidates(cands, fx, row, quote,
                             desc["label"].replace("Over", "Under"))

    # ---- exact score (killed for cash — display only) --------------------------
    exact_events = pm_by_kind.get("exact_score") or []
    if exact_events or matrix is not None:
        rows.append({"section": "Exact Score", "settlement": EM.SETTLE_90MIN,
                     "note": ("display only — correct score is a KILLED market "
                              "family (never cash); fair value = 40% grid + "
                              "60% de-vigged PM partition")})
    for ev in exact_events:
        listed: List[Tuple[int, int]] = []
        legs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for m in ev.get("markets") or []:
            desc = EM.classify_pm_market("exact_score",
                                         m.get("groupItemTitle") or "",
                                         m.get("question") or "", home, away)
            if not desc.get("priceable"):
                continue
            if desc.get("score"):
                # PM lists scores in ITS title order; re-orient to our home/away.
                t = _title_teams(ev.get("title") or "")
                if t is not None and canonical(t[0]) != canonical(home):
                    h, a = desc["score"]
                    desc = dict(desc, score=(a, h))
                listed.append(desc["score"])
            legs.append((desc, m))
        # De-vig reference: the exact-score legs form a full partition.
        mids: Dict[int, Optional[float]] = {}
        quotes: Dict[int, Optional[Dict[str, Any]]] = {}
        for i, (desc, m) in enumerate(legs):
            q = quote_for(m, 0)
            quotes[i] = q
            mids[i] = q.get("mid") if q else None
        total_mid = sum(v for v in mids.values() if v is not None)
        devig_ok = 0.85 <= total_mid <= 1.25 and all(
            v is not None for v in mids.values())
        for i, (desc, m) in enumerate(legs):
            if desc.get("any_other"):
                desc = dict(desc, listed_scores=listed)
                label = "Any Other Score"
            else:
                h, a = desc["score"]
                label = "%d-%d" % (h, a)
            grid = None if matrix is None else EM.grid_prob_for(desc, matrix)
            mid = mids[i]
            ref = (mid / total_mid) if (devig_ok and mid is not None) else None
            blend = EM.blend_with_market(grid, ref)
            row = _row(label, blend["prob"], mid, family="exact_score",
                       settlement=EM.SETTLE_90MIN, model_source=blend["source"],
                       model_null_reason=(None if grid is not None
                                          else "model fit unavailable"),
                       quote=quotes[i], captured_utc=captured_utc,
                       components=blend["components"], dimmed=True)
            rows.append(row)
            _push_candidates(cands, fx, row, quotes[i], "Not %s" % label,
                             include_lay=False)  # killed family

    # ---- anytime scorer props (killed for cash — display only) -----------------
    rows.append({"section": "Anytime Scorer (props)",
                 "settlement": EM.SETTLE_90MIN,
                 "note": ("display only — scorer props are a KILLED market "
                          "family (never cash); rate source stamped per row")})
    props_events = pm_by_kind.get("player_props") or []
    if not props_events:
        rows.append(_row("Anytime scorer", None, None, family="scorer_prop",
                         settlement=EM.SETTLE_90MIN,
                         model_null_reason=("not priced — no PM Player-Props "
                                            "event listed for this fixture"),
                         market_null_reason=("no PM market — no Player-Props "
                                             "event listed for this fixture")))
    elif scorer_pricer is not None:
        for prop_row in scorer_pricer(fx, props_events[0], captured_utc):
            rows.append(prop_row)
            # killed family: display only — candidates still recorded so the
            # recs feed shows them dimmed at stake 0.
            if prop_row.get("model") is not None and prop_row.get("market") is not None:
                cands.append(_candidate(
                    fx, prop_row, "back", prop_row["label"],
                    float(prop_row["model"]), float(prop_row["market"]),
                    prop_row.get("price_source") or "", prop_row.get("token_id")))

    # ---- extra time (90-min-derived: ET occurs iff the 90' score is level) -----
    if extra_time_market is not None:
        rows.append({"section": "Extra Time", "settlement": EM.SETTLE_90MIN,
                     "note": "KO tie goes to ET iff the 90' score is level — "
                             "model = blended 90' draw probability"})
        quote = quote_for(extra_time_market, 0)  # outcome[0] = Yes
        mid = quote and quote.get("mid")
        model = trip.get("draw")
        row = _row("Goes to Extra Time — Yes", model, mid, family="extra_time",
                   settlement=EM.SETTLE_90MIN,
                   model_source="card blend draw probability",
                   quote=quote, captured_utc=captured_utc,
                   model_null_reason=(None if model is not None
                                      else "no persisted blended 1X2"))
        rows.append(row)
        _push_candidates(cands, fx, row, quote, "Goes to Extra Time — No")

    # ---- advancement (ET+pens — OWN section, never mixed with 90-min) ----------
    if advance_market is not None:
        rows.append({"section": "Team to Advance", "settlement": EM.SETTLE_ADVANCE,
                     "note": ("ADVANCEMENT settlement (includes ET + penalties) "
                              "— NOT a 90-minute market; model = advancement MC sim")})
        ph, pa, note = advance_model_probs(adv_model, home, away)
        outcomes = PM._parse_json_array(advance_market.get("outcomes")) or []
        for idx, oname in enumerate(outcomes[:2]):
            side = "home" if canonical(str(oname)) == canonical(home) else "away"
            model = ph if side == "home" else pa
            quote = quote_for(advance_market, idx)
            mid = quote and quote.get("mid")
            row = _row("%s to advance" % oname, model, mid, family="advance",
                       settlement=EM.SETTLE_ADVANCE,
                       model_source=("advancement MC sim (%s, %s)"
                                     % (adv_stamp or "stamp missing", note)),
                       model_null_reason=(None if model is not None else note),
                       quote=quote, captured_utc=captured_utc)
            rows.append(row)
            if model is not None and quote:
                # No lay side: the complement IS the other team's BACK row.
                _push_candidates(cands, fx, row, quote,
                                 "%s NOT to advance" % oname, include_lay=False)

    # ---- families with no production model (market only) ------------------------
    for kind, section in _UNPRICEABLE_SECTIONS:
        evs = pm_by_kind.get(kind) or []
        if not evs:
            continue
        first = True
        for ev in evs:
            for m in ev.get("markets") or []:
                desc = EM.classify_pm_market(kind, m.get("groupItemTitle") or "",
                                             m.get("question") or "", home, away)
                quote = quote_for(m, 0)
                mid = quote and quote.get("mid")
                if mid is None:
                    continue
                if first:
                    rows.append({"section": "%s — market only" % section,
                                 "settlement": desc["settlement"],
                                 "note": desc.get("model_null_reason") or
                                 "no production model for this family"})
                    first = False
                rows.append(_row(desc["label"], None, mid,
                                 family=desc["family"],
                                 settlement=desc["settlement"],
                                 model_null_reason=desc.get("model_null_reason"),
                                 quote=quote, captured_utc=captured_utc))
    # remaining unpriceable more-markets legs (halves, pens, odd/even ...)
    first = True
    for desc, m in other_unpriced:
        quote = quote_for(m, 0)
        mid = quote and quote.get("mid")
        if mid is None:
            continue
        if first:
            rows.append({"section": "Other markets — market only",
                         "settlement": EM.SETTLE_90MIN,
                         "note": "families the production model cannot price "
                                 "fairly yet — market price shown, no model, "
                                 "no trade signal"})
            first = False
        rows.append(_row(desc["label"], None, mid, family=desc["family"],
                         settlement=desc["settlement"],
                         model_null_reason=desc.get("model_null_reason"),
                         quote=quote, captured_utc=captured_utc))

    entry: Dict[str, Any] = {
        "fixture": fx["fixture"],
        "rows": rows,
        "has_market": any(r.get("market") is not None
                          for r in rows if "section" not in r),
        "lambda_check": lam_check,
    }
    if fx.get("kickoff"):
        entry["kickoff"] = fx["kickoff"]
    return entry, cands


# ---------------------------------------------------------------------------
# Scorer-prop pricer (playerprops machinery; provenance stamped).
# ---------------------------------------------------------------------------


def make_scorer_pricer(matrix_by_fixture: Dict[str, Any]):
    """Build the per-fixture anytime-scorer pricer (players.json + players.db).

    Returns a callable ``(fx, pm_event, captured_utc) -> [rows]`` or ``None``
    when players.json is unavailable (no analyst player params — the model
    cannot price scorers at all, so no numbers are emitted).
    """
    try:
        from wca.models.scorers import load_player_overrides, PlayerParams
        from wca.models import playerprops as PPM
    except Exception:  # noqa: BLE001
        return None
    if not os.path.exists(_PLAYERS_JSON):
        return None
    overrides = load_player_overrides(_PLAYERS_JSON)
    by_canon = {canonical(k): v for k, v in overrides.items()}
    store = None
    if os.path.exists(_PLAYERS_DB):
        try:
            from wca.models.betbuilder import RateStore

            store = RateStore(_PLAYERS_DB)
        except Exception:  # noqa: BLE001
            store = None

    def _pricer(fx: Dict[str, Any], pm_event: Dict[str, Any],
                captured_utc: str) -> List[Dict[str, Any]]:
        import numpy as np

        home, away = fx["home"], fx["away"]
        matrix = matrix_by_fixture.get(fx["fixture"])
        lam_h, lam_a = fx.get("lambda_home"), fx.get("lambda_away")
        if matrix is not None:
            m = np.asarray(matrix, dtype=float)
            lam_h = float((np.arange(m.shape[0]) * m.sum(axis=1)).sum())
            lam_a = float((np.arange(m.shape[1]) * m.sum(axis=0)).sum())
        if lam_h is None or lam_a is None:
            return []
        scorers: Dict[str, List[PlayerParams]] = {}
        for team in (home, away):
            plist = by_canon.get(canonical(team))
            if plist:
                scorers[team] = [PlayerParams(
                    name=p.name, team=team, npxg_share=p.npxg_share,
                    penalty_taker=p.penalty_taker,
                    expected_minutes=p.expected_minutes, source=p.source)
                    for p in plist]
        if not scorers:
            return []
        rates = {}
        if store is not None:
            for team, plist in scorers.items():
                for pp in plist:
                    r = PPM.rates_from_players_db(
                        team, pp.name, store=store,
                        expected_minutes=pp.expected_minutes)
                    if r is not None:
                        rates[(team, pp.name)] = r
        priced = PPM.price_fixture_props_detailed(
            home, away, lambda_home=float(lam_h), lambda_away=float(lam_a),
            scorers_by_team=scorers, rates_by_player=rates,
            markets=(PPM.MK_GOALS,), thresholds={PPM.MK_GOALS: (1,)})

        def _yes_quote(market):
            q = market_quote(market, 0)
            if not q:
                return None
            return {"token": q["token_id"],
                    "ask": q.get("ask") if q.get("ask") is not None else q.get("mid"),
                    "mid": q.get("mid"), "bid": q.get("bid"),
                    "price_source": q.get("price_source")}

        joined = PPM.join_fixture_to_pm(priced, pm_event, yes_quote_fn=_yes_quote)
        out: List[Dict[str, Any]] = []
        for r in sorted(joined, key=lambda r: -r.model_prob):
            if r.market_type != PPM.MK_GOALS or r.threshold != 1:
                continue
            out.append(_row(
                "%s anytime" % r.player, r.model_prob, r.pm_price,
                family="scorer_prop", settlement=EM.SETTLE_90MIN,
                model_source=("playerprops (rate=%s, minutes=%s%s)"
                              % (r.rate_source, r.minutes_source,
                                 "" if os.path.exists(_PLAYERS_DB)
                                 else "; players.db absent on this box — "
                                      "structural priors, mini build upgrades")),
                quote={"token_id": r.token_id, "mid": r.pm_price,
                       "bid": None, "ask": r.pm_price,
                       "price_source": "clob_ask"},
                captured_utc=captured_utc, dimmed=True))
        return out

    return _pricer


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def _count_priced_rows(fixtures_list) -> int:
    return sum(1 for entry in fixtures_list or []
               for r in entry.get("rows", [])
               if "section" not in r and r.get("market") is not None)


def pm_blind_guard_blocks(out_fixtures, out_forest_path, *,
                          force: bool = False) -> bool:
    """PM-BLIND guard (same class as the advancement #161 fix): a run with
    the PM route down produces model-only rows and must NEVER clobber a feed
    carrying real market prices. Observed 2026-07-09: a VPN drop + rerun
    overwrote 319 priced rows with 0. Returns True when the write must be
    blocked."""
    if force or _count_priced_rows(out_fixtures) > 0:
        return False
    try:
        with open(out_forest_path, encoding="utf-8") as fh:
            existing_mkt = _count_priced_rows(json.load(fh).get("fixtures"))
    except Exception:  # noqa: BLE001 - no existing feed, nothing to protect
        return False
    if existing_mkt > 0:
        print("PM-BLIND GUARD: this run captured 0 market prices but the "
              "existing feed has %d priced rows - refusing to overwrite "
              "(reconnect the PM route / VPN and rerun, or pass "
              "--force-blind)." % existing_mkt)
        return True
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the PM event-markets forest + trade recs feeds.")
    ap.add_argument("--preds", default=_PREDS_PATH,
                    help="model predictions snapshot (card build artefact)")
    ap.add_argument("--out-forest", default=os.path.join(_ROOT, "site", "forest_data.json"))
    ap.add_argument("--out-recs", default=os.path.join(_ROOT, "site", "event_market_recs.json"))
    ap.add_argument("--days-ahead", type=float, default=7.0)
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "wca.db"),
                    help="ledger (read-only, for realised PM P&L; base bankroll "
                         "when absent)")
    ap.add_argument("--no-fit", action="store_true",
                    help="skip the DC fit (grid families become model:null)")
    ap.add_argument("--no-clob", action="store_true",
                    help="skip CLOB top-of-book calls (gamma prices only)")
    ap.add_argument("--force-blind", action="store_true",
                    help="overwrite the feeds even when this run captured "
                         "ZERO market prices (default: refuse if the "
                         "existing feed has priced rows)")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args(argv)

    _load_dotenv(args.env)
    now = _now_utc()
    captured_utc = _iso(now)

    fixtures = load_upcoming_fixtures(args.preds, args.days_ahead, now)
    if not fixtures:
        print("no upcoming fixtures in %s — nothing to build" % args.preds)
        return 1
    print("fixtures: %s" % ", ".join(f["fixture"] for f in fixtures))

    # --- model grid (production fit) ----------------------------------------
    dc = None
    if not args.no_fit:
        print("fitting production Dixon-Coles (card path, ~2.5 min) ...")
        try:
            dc = fit_production_dc()
        except Exception as exc:  # noqa: BLE001
            print("WARNING: model fit failed (%s) — grid families will be "
                  "model:null" % exc)
    matrices: Dict[str, Any] = {}
    checks: Dict[str, Dict[str, Any]] = {}
    for fx in fixtures:
        if dc is None:
            matrices[fx["fixture"]] = None
            checks[fx["fixture"]] = {"ok": False, "reason": "model fit unavailable"}
            continue
        matrix, check = reconciled_matrix(dc, fx)
        matrices[fx["fixture"]] = matrix
        checks[fx["fixture"]] = check
        print("  %-28s lambda_check=%s" % (fx["fixture"], check))

    # --- live PM enumeration --------------------------------------------------
    try:
        events = fetch_soccer_events()
        print("gamma soccer events fetched: %d" % len(events))
    except Exception as exc:  # noqa: BLE001
        print("WARNING: Gamma fetch failed (%s) — market side will be "
              "'no PM market' everywhere" % exc)
        events = []

    # Gamma's bulk /events pagination can stop before newly-created fixture
    # slugs.  Search each requested fixture directly before concluding that it
    # has no Polymarket markets.
    existing_pairs = set()
    for ev in events:
        teams = _title_teams(ev.get("title") or "")
        if teams:
            existing_pairs.add(frozenset((canonical(teams[0]), canonical(teams[1]))))
    for fx in fixtures:
        pair = frozenset((canonical(fx["home"]), canonical(fx["away"])))
        if pair in existing_pairs:
            continue
        try:
            supplement = fetch_fixture_search_events(fx["home"], fx["away"])
            events.extend(supplement)
            if supplement:
                print("  public-search %s vs %s: %d event(s)"
                      % (fx["home"], fx["away"], len(supplement)))
        except Exception as exc:  # noqa: BLE001
            print("WARNING: fixture search failed for %s vs %s (%s)"
                  % (fx["home"], fx["away"], exc))

    adv_model, adv_stamp = load_advancement_model(_ADV_PATH)

    scorer_pricer = make_scorer_pricer(matrices)

    out_fixtures: List[Dict[str, Any]] = []
    all_cands: List[Dict[str, Any]] = []
    coverage: List[str] = []
    for fx in fixtures:
        pm_by_kind = events_for_fixture(events, fx["home"], fx["away"])
        entry, cands = build_fixture(
            fx, matrices[fx["fixture"]], checks[fx["fixture"]], pm_by_kind,
            adv_model, adv_stamp, captured_utc,
            use_clob=not args.no_clob, scorer_pricer=scorer_pricer)
        out_fixtures.append(entry)
        all_cands.extend(cands)
        n_rows = sum(1 for r in entry["rows"] if "section" not in r)
        n_mkt = sum(1 for r in entry["rows"]
                    if "section" not in r and r.get("market") is not None)
        cov = "%s: pm_event_kinds=%s rows=%d with_market=%d" % (
            fx["fixture"], sorted(pm_by_kind.keys()), n_rows, n_mkt)
        coverage.append(cov)
        print("  " + cov)

    if pm_blind_guard_blocks(out_fixtures, args.out_forest,
                             force=args.force_blind):
        return 1

    forest = {
        "meta": {
            "generated": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "source": "scripts/wca_event_markets.py (live Gamma+CLOB snapshot)",
            "signal_rule": ("|model - market| >= 2pp: green = model above "
                            "market (BACK), red = market above model (LAY / "
                            "back the complement)"),
            "settlement_note": ("90-min rows settle on the 90'+stoppage "
                                "score; 'Team to Advance' rows settle "
                                "ET+pens and sit in their own section"),
        },
        "fixtures": out_fixtures,
    }
    os.makedirs(os.path.dirname(args.out_forest) or ".", exist_ok=True)
    with open(args.out_forest, "w", encoding="utf-8") as fh:
        json.dump(forest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(args.out_forest)

    # --- trade recs -------------------------------------------------------------
    from wca.markets import bankroll as pm_rule

    pnl = None
    try:
        import sqlite3

        if os.path.exists(args.db):
            con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
            try:
                row = con.execute(
                    "SELECT COALESCE(SUM(settled_pl), 0.0) FROM bets "
                    "WHERE platform='polymarket' AND settled_pl IS NOT NULL"
                ).fetchone()
                pnl = float(row[0]) if row else None
            finally:
                con.close()
    except Exception:  # noqa: BLE001
        pnl = None
    bankroll = pm_rule.pm_bankroll_usd(pnl or 0.0)

    recs = EM.build_event_market_recs(all_cands, bankroll_usd=bankroll,
                                      now_dt=now.replace(tzinfo=None))
    recs["meta"]["generated"] = forest["meta"]["generated"]
    recs["meta"]["bankroll_source"] = (
        "ledger realised PM P&L $%.2f applied" % pnl if pnl is not None
        else "base (ledger unavailable on this box)")
    recs["meta"]["n_candidates"] = len(all_cands)
    with open(args.out_recs, "w", encoding="utf-8") as fh:
        json.dump(recs, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(args.out_recs)

    n_cash = sum(1 for r in recs["recs"] if r.get("stake_usd", 0) > 0)
    print("forest fixtures=%d  rec rows=%d (cash-sized=%d, display-only=%d)"
          % (len(out_fixtures), len(recs["recs"]), n_cash,
             len(recs["recs"]) - n_cash))
    for c in coverage:
        print(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
