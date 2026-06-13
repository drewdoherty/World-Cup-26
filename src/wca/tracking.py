"""Prediction-tracking data builder for World Cup Alpha.

This module produces the structured JSON that ``site/tracking.html`` renders:
how the model's *pre-match* predictions fared against actual results, fixture
by fixture, plus bet-level aggregates (P/L, CLV) from the ledger.

Where the predictions come from
-------------------------------
The platform never persisted a full pre-match 1X2 per fixture in V1, so the
pre-match view is reconstructed from history:

* **Card snapshots** — every historical version of ``data/card_latest.md``
  (recovered from git by the CLI).  Picks carry exact ``model X% / mkt Y%``
  probabilities for the picked selection; the scorelines section carries the
  top-k correct-score ladder plus O/U 2.5 and BTTS percentages.
* **Scores-feed snapshots** — historical ``site/scores_data.json`` versions,
  which carry the same scoreline ladder per fixture.
* **Closing odds** — the ledger DB's ``odds_snapshots`` table, from which a
  de-vigged consensus 1X2 is computed at the last capture before kickoff.

For each completed fixture we use the **latest snapshot generated before
kickoff** as the pre-match prediction.

Model 1X2 reconstruction
------------------------
The card lists only the top-k scorelines, so the model 1X2 triple is
**approximate**: scoreline probabilities are bucketed into home/draw/away and
renormalised (the same convention :mod:`wca.scorespage` uses for the scores
page), then floored at a small epsilon so a bucket that never cracked the
top-k cannot produce an infinite log-loss.  The exact pick-level
``model %`` / ``mkt %`` values are preserved separately in each fixture's
``picks`` list.

Design notes
------------
* **Deterministic.**  :func:`build_tracking_data` never reads the wall clock,
  the network, git, or the filesystem; the CLI
  (``scripts/wca_tracking_data.py``) gathers all inputs and passes them in.
* **Tolerant.**  Missing snapshots, missing market closes and unsettled
  fixtures must never raise — fixtures degrade to ``pending`` / partial rows
  so the page can show a clean state.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from wca import sitedata
from wca.data import teamnames

_LEGS = ("home", "draw", "away")

# Floor applied to a reconstructed-1X2 leg so truncated buckets (a leg with no
# scoreline in the top-k) never carry exactly zero probability.
_PROB_FLOOR = 0.005

# Log-loss clamp: never take log of anything smaller than this.
_LOGLOSS_EPS = 1e-9


# ---------------------------------------------------------------------------
# Small parsing helpers.
# ---------------------------------------------------------------------------

_GENERATED_RE = re.compile(r"<!--\s*generated:\s*(?P<ts>[0-9T:\-\. ]+?)\s*-->")

# A pick header inside the bet-card section, e.g.
# "*1. Australia vs Turkey* — Australia @ *5.50* (betfair_ex_uk)".
_PICK_HEADER_RE = re.compile(
    r"^\*\d+\.\s*(?P<fixture>.+?)\*\s*[—-]\s*(?P<selection>.+?)\s*@\s*"
    r"\*(?P<odds>[0-9.]+)\*(?:\s*\((?P<venue>[^)]+)\))?"
)

# The model/market line under a pick header:
# "model 24.5% / mkt 18.1%  edge *+35.0%*  [elo 31% dc 31%]".
_PICK_MODEL_RE = re.compile(r"model\s+(?P<model>[0-9.]+)%")
_PICK_MKT_RE = re.compile(r"mkt\s+(?P<mkt>[0-9.]+)%")


def card_generated(card_text: str) -> Optional[str]:
    """Extract the ``<!-- generated: ... -->`` timestamp from a card body.

    Returns the raw timestamp string (UTC by convention of
    ``wca.cardcache.write_card``) or ``None`` when absent.
    """
    if not card_text:
        return None
    match = _GENERATED_RE.search(card_text)
    return match.group("ts") if match else None


def parse_card_picks(card_text: str) -> List[Dict[str, Any]]:
    """Parse the bet-card picks with their exact model / market probabilities.

    Returns one dict per pick::

        {"fixture": "Mexico vs South Africa", "selection": "Mexico",
         "odds": 1.44, "venue": "betfair_ex_uk",
         "model_prob": 0.713, "market_prob": 0.693}

    ``model_prob`` / ``market_prob`` are 0..1 fractions (``None`` when the
    model line is missing or malformed).
    """
    picks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw in (card_text or "").splitlines():
        line = raw.strip()
        header = _PICK_HEADER_RE.match(line)
        if header:
            current = {
                "fixture": header.group("fixture").strip(),
                "selection": header.group("selection").strip(),
                "odds": float(header.group("odds")),
                "venue": (header.group("venue") or "").strip() or None,
                "model_prob": None,
                "market_prob": None,
            }
            picks.append(current)
            continue
        if current is not None and line.startswith("model"):
            model = _PICK_MODEL_RE.search(line)
            mkt = _PICK_MKT_RE.search(line)
            if model:
                current["model_prob"] = float(model.group("model")) / 100.0
            if mkt:
                current["market_prob"] = float(mkt.group("mkt")) / 100.0
            current = None
    return picks


def _canon(name: Any) -> str:
    """Alias-resolved, casefolded team name ('' for non-strings)."""
    if not isinstance(name, str):
        return ""
    canon = teamnames.canonical(name)
    if not isinstance(canon, str):
        return ""
    return canon.strip().casefold()


def split_fixture(fixture: str) -> Optional[Tuple[str, str]]:
    """Split ``"Home vs Away"`` into ``(home, away)``; ``None`` if unsplittable."""
    if not fixture:
        return None
    text = fixture.strip()
    lowered = text.lower()
    for sep in (" vs. ", " vs ", " v. ", " v "):
        idx = lowered.find(sep)
        if idx != -1:
            home = text[:idx].strip()
            away = text[idx + len(sep):].strip()
            if home and away:
                return home, away
    return None


def fixture_key(fixture: str) -> Optional[Tuple[str, str]]:
    """Canonical order-sensitive key for a fixture string, or ``None``."""
    pair = split_fixture(fixture)
    if pair is None:
        return None
    return (_canon(pair[0]), _canon(pair[1]))


def leg_for_selection(fixture: str, selection: str) -> Optional[str]:
    """Map a pick selection to ``home`` / ``draw`` / ``away`` for *fixture*."""
    sel = (selection or "").strip()
    if not sel:
        return None
    if "draw" in sel.casefold():
        return "draw"
    pair = split_fixture(fixture)
    if pair is None:
        return None
    sel_c = _canon(sel)
    if sel_c and sel_c == _canon(pair[0]):
        return "home"
    if sel_c and sel_c == _canon(pair[1]):
        return "away"
    return None


# ---------------------------------------------------------------------------
# Outcomes and scores.
# ---------------------------------------------------------------------------


def parse_score(score: Any) -> Optional[Tuple[int, int]]:
    """Parse ``"2-1"`` / ``"2 - 1"`` into ``(2, 1)``; ``None`` on failure."""
    if not isinstance(score, str):
        return None
    match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", score)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def outcome_from_score(score: Any) -> Optional[str]:
    """``home`` / ``draw`` / ``away`` for a final score string, else ``None``."""
    parsed = parse_score(score)
    if parsed is None:
        return None
    home_goals, away_goals = parsed
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


# ---------------------------------------------------------------------------
# 1X2 triples: reconstruction, de-vig, scoring rules.
# ---------------------------------------------------------------------------


def model_1x2_from_scorelines(
    scores: List[Dict[str, Any]],
    floor: float = _PROB_FLOOR,
) -> Optional[Dict[str, float]]:
    """Approximate model 1X2 from a top-k scoreline ladder.

    Buckets scoreline probabilities (percentages, 0..100) by sign of
    ``home - away`` and renormalises — the :mod:`wca.scorespage` convention —
    then floors each leg at *floor* and renormalises again so truncated
    buckets never carry exactly zero.  Returns ``None`` when no usable rows.
    """
    buckets = {"home": 0.0, "draw": 0.0, "away": 0.0}
    total = 0.0
    for row in scores or []:
        parsed = parse_score(row.get("score"))
        prob = row.get("prob")
        if parsed is None or prob is None:
            continue
        try:
            p = float(prob)
        except (TypeError, ValueError):
            continue
        if p < 0:
            continue
        home_goals, away_goals = parsed
        if home_goals > away_goals:
            buckets["home"] += p
        elif home_goals < away_goals:
            buckets["away"] += p
        else:
            buckets["draw"] += p
        total += p
    if total <= 0:
        return None
    triple = {leg: buckets[leg] / total for leg in _LEGS}
    return _floor_and_renorm(triple, floor)


def _floor_and_renorm(
    triple: Dict[str, float], floor: float = _PROB_FLOOR
) -> Dict[str, float]:
    floored = {leg: max(float(triple.get(leg) or 0.0), floor) for leg in _LEGS}
    total = sum(floored.values())
    return {leg: floored[leg] / total for leg in _LEGS}


def devig_consensus(
    book_prices: List[Dict[str, Any]],
) -> Optional[Dict[str, float]]:
    """De-vigged consensus 1X2 from raw bookmaker prices.

    *book_prices* is a list of ``{"book": str, "home": dec, "draw": dec,
    "away": dec}`` rows (decimal odds).  Each complete book triple is
    de-vigged by proportional normalisation of implied probabilities; the
    consensus is the mean across books, renormalised.  Returns ``None`` when
    no book has a complete triple.
    """
    devigged: List[Dict[str, float]] = []
    for row in book_prices or []:
        implied = {}
        ok = True
        for leg in _LEGS:
            try:
                dec = float(row.get(leg))
            except (TypeError, ValueError):
                ok = False
                break
            if dec <= 1.0:
                ok = False
                break
            implied[leg] = 1.0 / dec
        if not ok:
            continue
        total = sum(implied.values())
        if total <= 0:
            continue
        devigged.append({leg: implied[leg] / total for leg in _LEGS})
    if not devigged:
        return None
    mean = {leg: sum(d[leg] for d in devigged) / len(devigged) for leg in _LEGS}
    total = sum(mean.values())
    return {leg: mean[leg] / total for leg in _LEGS}


def brier_1x2(triple: Optional[Dict[str, float]], outcome: str) -> Optional[float]:
    """Multiclass Brier score of a 1X2 triple against the actual *outcome*.

    ``sum over legs of (p_leg - 1{leg == outcome})^2`` — 0 is perfect, 2 is
    maximally wrong.  ``None`` when the triple is missing.
    """
    if not triple or outcome not in _LEGS:
        return None
    return sum(
        (float(triple.get(leg) or 0.0) - (1.0 if leg == outcome else 0.0)) ** 2
        for leg in _LEGS
    )


def log_loss_1x2(triple: Optional[Dict[str, float]], outcome: str) -> Optional[float]:
    """Negative log-likelihood of the actual outcome under the triple."""
    if not triple or outcome not in _LEGS:
        return None
    return -math.log(max(float(triple.get(outcome) or 0.0), _LOGLOSS_EPS))


def modal_pick(triple: Optional[Dict[str, float]]) -> Optional[str]:
    """The leg with the highest probability (``None`` for a missing triple)."""
    if not triple:
        return None
    return max(_LEGS, key=lambda leg: float(triple.get(leg) or 0.0))


# ---------------------------------------------------------------------------
# Snapshot selection.
# ---------------------------------------------------------------------------


def _parse_ts(ts: Any) -> Optional[str]:
    """Normalise a timestamp-ish string to sortable ``YYYY-MM-DDTHH:MM:SS``.

    Accepts ``2026-06-11T15:25:01``, ``2026-06-11 15:25:01 UTC`` and
    ``...Z`` / ``...+00:00`` suffixes (all are UTC by repo convention).
    Returns ``None`` when unparseable.
    """
    if not isinstance(ts, str):
        return None
    text = ts.strip().replace(" UTC", "").replace("Z", "")
    text = re.sub(r"\+00:00$", "", text)
    text = text.replace(" ", "T")
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", text)
    if not match:
        # Date-only fallback.
        match = re.match(r"^(\d{4}-\d{2}-\d{2})$", text)
        if not match:
            return None
        return match.group(1) + "T00:00:00"
    return match.group(1)


def exact_model_before(
    rows: Optional[List[Dict[str, Any]]],
    key: Optional[Tuple[str, str]],
    kickoff_utc: Any,
) -> Optional[Dict[str, Any]]:
    """Latest exact card-build prediction for *key* generated before kickoff.

    *rows* are entries from the model-predictions log (see
    :mod:`wca.modelpreds`): ``{"generated", "fixture", "model": {home, draw,
    away}}``.  Returns the winning row or ``None``.  Like
    :func:`latest_snapshot_before`, an unparseable kickoff yields ``None`` —
    better no prediction than a post-hoc one.
    """
    if not rows or key is None:
        return None
    kick = _parse_ts(kickoff_utc)
    if kick is None:
        return None
    best = None
    best_ts = None
    for row in rows:
        if fixture_key(str(row.get("fixture") or "")) != key:
            continue
        model = row.get("model")
        if not isinstance(model, dict):
            continue
        if not all(isinstance(model.get(leg), (int, float)) for leg in _LEGS):
            continue
        ts = _parse_ts(row.get("generated"))
        if ts is None or ts >= kick:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = row, ts
    return best


def latest_snapshot_before(
    snapshots: List[Dict[str, Any]],
    kickoff_utc: Any,
) -> Optional[Dict[str, Any]]:
    """Latest snapshot whose ``generated`` precedes *kickoff_utc*.

    *snapshots* entries carry a ``generated`` timestamp string.  Snapshots
    with an unparseable timestamp are ignored.  When *kickoff_utc* is
    unparseable, returns ``None`` (we'd rather show no prediction than a
    post-hoc one).
    """
    kick = _parse_ts(kickoff_utc)
    if kick is None:
        return None
    best = None
    best_ts = None
    for snap in snapshots or []:
        ts = _parse_ts(snap.get("generated"))
        if ts is None or ts >= kick:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = snap, ts
    return best


# ---------------------------------------------------------------------------
# The builder.
# ---------------------------------------------------------------------------


def _prediction_views(snapshot: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Index a snapshot's per-fixture predictions by canonical fixture key.

    A snapshot is either a card (``text``) or a scores feed (``fixtures``
    already structured).  Both collapse to::

        {"fixture", "scores", "over_under", "btts", "picks"}
    """
    views: Dict[Tuple[str, str], Dict[str, Any]] = {}

    if "text" in snapshot:  # card snapshot
        text = snapshot.get("text") or ""
        picks = parse_card_picks(text)
        for fx in sitedata.parse_scorelines(text):
            key = fixture_key(fx.get("fixture") or "")
            if key is None:
                continue
            views[key] = {
                "fixture": fx.get("fixture"),
                "scores": fx.get("scores") or [],
                "over_under": fx.get("over_under"),
                "btts": fx.get("btts"),
                "picks": [],
            }
        for pick in picks:
            key = fixture_key(pick.get("fixture") or "")
            if key is None:
                continue
            view = views.setdefault(
                key,
                {
                    "fixture": pick.get("fixture"),
                    "scores": [],
                    "over_under": None,
                    "btts": None,
                    "picks": [],
                },
            )
            view["picks"].append(pick)
        return views

    for fx in snapshot.get("fixtures") or []:  # scores-feed snapshot
        key = fixture_key(fx.get("fixture") or "")
        if key is None:
            continue
        views[key] = {
            "fixture": fx.get("fixture"),
            "scores": fx.get("scores") or [],
            "over_under": fx.get("over_under"),
            "btts": fx.get("btts"),
            "picks": [],
            "kickoff": fx.get("kickoff"),
        }
    return views


def _round_triple(triple: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not triple:
        return None
    return {leg: round(float(triple.get(leg) or 0.0), 4) for leg in _LEGS}


def _opt_round(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _mean(values: List[float]) -> Optional[float]:
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    return sum(cleaned) / len(cleaned)


def payload_degraded(new: Dict[str, Any], old: Dict[str, Any]) -> bool:
    """True when *new* is a strictly poorer feed than *old*.

    Guards the public tracking feed against environment-starved rebuilds
    (e.g. a CI runner with no ledger DB and a shallow git clone) wholesale
    replacing a populated feed with nulls — the same never-clobber contract
    as ``linemove.write_linemove`` and ``modelpreds.write_predictions``.
    """
    try:
        ns, os_ = new["summary"], old["summary"]
    except (KeyError, TypeError):
        return False
    if not (old.get("fixtures") or old.get("pending")):
        return False  # old feed empty — anything new is an improvement
    if int(ns.get("fixtures_complete") or 0) < int(os_.get("fixtures_complete") or 0):
        return True
    if (
        int((ns.get("bets") or {}).get("settled") or 0) == 0
        and int((os_.get("bets") or {}).get("settled") or 0) > 0
    ):
        return True
    if ns.get("model_brier") is None and os_.get("model_brier") is not None:
        return True
    return False


def build_tracking_data(
    results: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
    market_closes: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    bets: Optional[List[Dict[str, Any]]] = None,
    now_utc: str = "",
    exact_models: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Assemble the tracking payload.

    Parameters
    ----------
    results:
        Rows from the manually-maintained results file: ``{"date", "fixture",
        "kickoff_utc", "score" ("2-0" or None), "outcome" ("home" / "draw" /
        "away" / "pending")}``.
    snapshots:
        Historical prediction snapshots, newest or oldest first (order does
        not matter).  Card snapshots are ``{"generated", "text"}``; scores
        feed snapshots are ``{"generated", "fixtures": [...]}``.
    market_closes:
        Optional map of canonical fixture key -> ``{"triple": {home, draw,
        away}, "ts": str, "books": int}`` — the de-vigged consensus at the
        last odds capture before kickoff (see :func:`devig_consensus`).
    bets:
        Ledger bet rows (dicts with at least ``status``, ``settled_pl``,
        ``clv``; extra keys are passed through to the scatter payload).
    now_utc:
        Display timestamp for ``meta.generated`` (caller-supplied; this
        function never reads the clock).
    exact_models:
        Optional rows from the model-predictions log (:mod:`wca.modelpreds`).
        When a fixture has an exact pre-kickoff blended triple here, it
        replaces the top-k scoreline reconstruction (``model_source`` flips
        from ``"scoreline_approx"`` to ``"card_build"``).
    """
    market_closes = market_closes or {}
    bets = bets or []

    fixtures_out: List[Dict[str, Any]] = []
    pending_out: List[Dict[str, Any]] = []

    for row in results or []:
        fixture = row.get("fixture") or ""
        key = fixture_key(fixture)
        outcome = (row.get("outcome") or "").strip().lower()
        score = row.get("score")
        kickoff = row.get("kickoff_utc") or row.get("date")
        is_pending = outcome not in ("home", "draw", "away")

        # Pre-match snapshot view for this fixture.
        view = None
        snap_used = None
        candidates = [
            s for s in snapshots or []
            if key is not None and key in _prediction_views(s)
        ]
        snap_used = latest_snapshot_before(candidates, kickoff)
        if snap_used is not None:
            view = _prediction_views(snap_used).get(key)

        scores = (view or {}).get("scores") or []
        picks = (view or {}).get("picks") or []
        model_triple = model_1x2_from_scorelines(scores)
        model_source = "scoreline_approx" if model_triple else None

        # Exact blended triple persisted at card-build time beats the
        # scoreline reconstruction whenever a pre-kickoff row exists.
        exact = exact_model_before(exact_models, key, kickoff)
        if exact is not None:
            model_triple = {
                leg: float(exact["model"][leg]) for leg in _LEGS
            }
            total = sum(model_triple.values())
            if total > 0:
                model_triple = {
                    leg: model_triple[leg] / total for leg in _LEGS
                }
                model_source = "card_build"
            else:
                model_triple = model_1x2_from_scorelines(scores)
                model_source = "scoreline_approx" if model_triple else None

        close = market_closes.get(key) if key is not None else None
        market_triple = (close or {}).get("triple")
        market_source = "closing_devig" if market_triple else None

        if not market_triple and picks:
            # Fallback: shape the market triple like the model triple but
            # anchored on the exact de-vigged pick probabilities from the card.
            anchored: Dict[str, float] = {}
            for pick in picks:
                leg = leg_for_selection(fixture, pick.get("selection") or "")
                if leg and pick.get("market_prob") is not None:
                    anchored[leg] = float(pick["market_prob"])
            if anchored and model_triple:
                remaining = max(1.0 - sum(anchored.values()), 0.0)
                rest_legs = [leg for leg in _LEGS if leg not in anchored]
                rest_weight = sum(model_triple[leg] for leg in rest_legs)
                triple = dict(anchored)
                for leg in rest_legs:
                    share = (
                        model_triple[leg] / rest_weight
                        if rest_weight > 0
                        else 1.0 / len(rest_legs)
                    )
                    triple[leg] = remaining * share
                market_triple = _floor_and_renorm(triple)
                market_source = "card_devig"

        if is_pending:
            pending_out.append(
                {
                    "fixture": fixture,
                    "date": row.get("date"),
                    "kickoff": kickoff,
                    "model_1x2": _round_triple(model_triple),
                    "model_source": model_source,
                    "model_pick": modal_pick(model_triple),
                    "top_scoreline": (
                        {
                            "score": scores[0].get("score"),
                            "prob": scores[0].get("prob"),
                        }
                        if scores
                        else None
                    ),
                    "picks": picks,
                    "pending": True,
                }
            )
            continue

        # ---- completed fixture scoring ---------------------------------
        model_pick = modal_pick(model_triple)
        market_pick = modal_pick(market_triple)

        parsed_score = parse_score(score)
        top_scoreline = None
        top6_hit = None
        if scores:
            top = scores[0]
            top_scoreline = {
                "score": top.get("score"),
                "prob": top.get("prob"),
                "hit": (
                    None
                    if parsed_score is None
                    else parse_score(top.get("score")) == parsed_score
                ),
            }
            if parsed_score is not None:
                top6_hit = any(
                    parse_score(s.get("score")) == parsed_score
                    for s in scores[:6]
                )

        ou25 = None
        over_under = (view or {}).get("over_under")
        if over_under and over_under.get("over") is not None:
            model_over = float(over_under["over"]) / 100.0
            actual_over = (
                None if parsed_score is None else sum(parsed_score) > 2.5
            )
            hit = None
            if actual_over is not None:
                hit = (model_over >= 0.5) == actual_over
            ou25 = {
                "model_over": round(model_over, 4),
                "actual_over": actual_over,
                "hit": hit,
            }

        btts = None
        btts_model = (view or {}).get("btts")
        if btts_model is not None:
            model_yes = float(btts_model) / 100.0
            actual_yes = (
                None
                if parsed_score is None
                else (parsed_score[0] > 0 and parsed_score[1] > 0)
            )
            hit = None
            if actual_yes is not None:
                hit = (model_yes >= 0.5) == actual_yes
            btts = {"model": round(model_yes, 4), "actual": actual_yes, "hit": hit}

        fixtures_out.append(
            {
                "fixture": fixture,
                "date": row.get("date"),
                "kickoff": kickoff,
                "score": score,
                "outcome": outcome,
                "card_generated": (snap_used or {}).get("generated"),
                "model_1x2": _round_triple(model_triple),
                "model_source": model_source,
                "market_1x2": _round_triple(market_triple),
                "market_source": market_source,
                "market_books": (close or {}).get("books"),
                "model_pick": model_pick,
                "market_pick": market_pick,
                "model_correct": (
                    None if model_pick is None else model_pick == outcome
                ),
                "market_correct": (
                    None if market_pick is None else market_pick == outcome
                ),
                "model_prob_outcome": _opt_round(
                    (model_triple or {}).get(outcome)
                ),
                "market_prob_outcome": _opt_round(
                    (market_triple or {}).get(outcome)
                ),
                "brier_model": _opt_round(brier_1x2(model_triple, outcome)),
                "brier_market": _opt_round(brier_1x2(market_triple, outcome)),
                "logloss_model": _opt_round(log_loss_1x2(model_triple, outcome)),
                "logloss_market": _opt_round(log_loss_1x2(market_triple, outcome)),
                "top_scoreline": top_scoreline,
                "top6_hit": top6_hit,
                "ou25": ou25,
                "btts": btts,
                "picks": picks,
                "pending": False,
            }
        )

    # ---- upcoming fixtures from the latest snapshot (not in results) -----
    latest_snap = None
    latest_ts = None
    for snap in snapshots or []:
        ts = _parse_ts(snap.get("generated"))
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_snap, latest_ts = snap, ts
    if latest_snap is not None:
        result_keys = {
            fixture_key(r.get("fixture") or "") for r in results or []
        }
        for key, view in _prediction_views(latest_snap).items():
            if key in result_keys:
                continue
            scores = view.get("scores") or []
            model_triple = model_1x2_from_scorelines(scores)
            model_source = "scoreline_approx" if model_triple else None
            # Upcoming fixtures can't be predicted post-hoc, so the freshest
            # exact triple wins regardless of the (possibly absent) kickoff.
            exact = exact_model_before(exact_models, key, "9999-12-31T23:59:59")
            if exact is not None:
                total = sum(float(exact["model"][leg]) for leg in _LEGS)
                if total > 0:
                    model_triple = {
                        leg: float(exact["model"][leg]) / total for leg in _LEGS
                    }
                    model_source = "card_build"
            pending_out.append(
                {
                    "fixture": view.get("fixture"),
                    "date": None,
                    "kickoff": view.get("kickoff"),
                    "model_1x2": _round_triple(model_triple),
                    "model_source": model_source,
                    "model_pick": modal_pick(model_triple),
                    "top_scoreline": (
                        {
                            "score": scores[0].get("score"),
                            "prob": scores[0].get("prob"),
                        }
                        if scores
                        else None
                    ),
                    "picks": view.get("picks") or [],
                    "pending": True,
                }
            )
        pending_out.sort(key=lambda p: (p.get("kickoff") or "9999", p.get("fixture") or ""))

    # ---- bet-level aggregates -------------------------------------------
    settled = [
        b for b in bets if (b.get("status") or "").lower() in ("won", "lost", "void")
    ]
    won = sum(1 for b in settled if (b.get("status") or "").lower() == "won")
    lost = sum(1 for b in settled if (b.get("status") or "").lower() == "lost")
    pl = sum(float(b.get("settled_pl") or 0.0) for b in settled)
    # CLV is locked in at the close (kickoff) and is independent of settlement,
    # so it counts every bet with a captured closing line — including still-open
    # in-play bets. This matches dashboard.py's terminal CLV; restricting to
    # settled bets (the old behaviour) froze the metric until results landed,
    # so a kicked-off fixture's CLV never showed up here.
    clvs = [
        float(b["clv"]) for b in bets if b.get("clv") is not None
    ]
    avg_clv = (sum(clvs) / len(clvs)) if clvs else None

    bets_out = [
        {
            "id": b.get("id"),
            "match": b.get("match_desc"),
            "selection": b.get("selection"),
            "odds": b.get("decimal_odds"),
            "stake": b.get("stake"),
            "status": (b.get("status") or "").lower(),
            "pl": b.get("settled_pl"),
            "clv": b.get("clv"),
        }
        for b in settled
    ]

    summary = {
        "fixtures_complete": len(fixtures_out),
        "model_1x2_correct": sum(1 for f in fixtures_out if f.get("model_correct")),
        "market_1x2_correct": sum(
            1 for f in fixtures_out if f.get("market_correct")
        ),
        "model_brier": _opt_round(
            _mean([f.get("brier_model") for f in fixtures_out])
        ),
        "market_brier": _opt_round(
            _mean([f.get("brier_market") for f in fixtures_out])
        ),
        "model_logloss": _opt_round(
            _mean([f.get("logloss_model") for f in fixtures_out])
        ),
        "market_logloss": _opt_round(
            _mean([f.get("logloss_market") for f in fixtures_out])
        ),
        "top6_hits": sum(1 for f in fixtures_out if f.get("top6_hit")),
        "bets": {
            "settled": len(settled),
            "won": won,
            "lost": lost,
            "pl": round(pl, 2),
            "avg_clv": _opt_round(avg_clv),
            "clv_count": len(clvs),
        },
    }

    return {
        "meta": {"generated": now_utc},
        "summary": summary,
        "fixtures": fixtures_out,
        "pending": pending_out,
        "bets": bets_out,
    }
