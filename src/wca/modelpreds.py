"""Persist the blended model 1X2 per fixture at card-build time.

The card markdown only records model probabilities for *picked* selections,
and the scores feed reconstructs an approximate 1X2 from the top-k scoreline
ladder (which clips outcomes that never crack the top six).  This module dumps
the exact blended triple — plus the Elo / DC / market components — so the site
and the prediction-tracking pipeline can read what the model actually said
before kickoff.

Two artefacts, both git-tracked so history is preserved by commits:

* ``data/model_predictions.json`` — latest snapshot, overwritten each build.
* ``data/model_predictions_log.jsonl`` — append-only log, one line per
  fixture per build.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

LATEST_PATH = "data/model_predictions.json"
LOG_PATH = "data/model_predictions_log.jsonl"

# Optional archival TEE: additive, never changes behavior (guarded).
try:
    from wca.archive import tee as _archive_tee
except Exception:  # pragma: no cover - archive is optional
    _archive_tee = None

_LEGS = ("home", "draw", "away")

# 1X2 shadow-variant tuning (SHADOW-ONLY — see build_predictions docstring).
# The residual model that mw90_* blends the market against: the deployed
# 0.25*Elo + 0.75*DC split (matches the card's model residual, not the final
# 0.90-market blend which would be circular). Changing these is a shadow-config
# tweak only — nothing here touches live pricing/sizing/selection.
_MW90_W_MARKET = 0.90
_MW90_ELO_WEIGHT = 0.25
_MW90_DC_WEIGHT = 0.75
_SHRINK_K_MID = 0.5      # k for legs the model rates >= 0.25
_SHRINK_K_LONGSHOT = 0.25  # k for legs the model rates < 0.25
_SHRINK_LONGSHOT_PROB = 0.25
_DISAGREE_PP = 0.03      # |model - market| >= 3pp flags a leg as a disagreement


# ---------------------------------------------------------------------------
# LIVE shrink-to-market kill-switch (graduation of the `shrink` shadow).
# ---------------------------------------------------------------------------
#
# The `shrink` shadow family (``p' = p_mkt + k*(p_model - p_mkt)``, ``k=0.5``
# for legs the model rates >=0.25 and ``k=0.25`` below) was promoted to the LIVE
# model line (user-approved 2026-07-09). Evidence: backtest n=383 model-vs-PM
# observations — when the model disagrees with PM by >=2pp, resolution moves
# TOWARD the model only 24.8% [21.1, 28.5] of the time (7.7% on favourites);
# per-leg calibration n=99, model <0.25 legs predict 15.8% vs realize 10.1%; the
# `shrink` shadow trended positive vs live (brierΔ −0.0046 at n=75, same
# direction as `market`). Conclusion: shrink the model prob TOWARD the de-vigged
# market reference BEFORE it drives edge/EV/sizing.
#
# The application layer is :func:`wca.card._iter_fixture_blends` (the single
# blend origin shared by the card, ``nextmatch`` and the persisted
# ``model_predictions.json`` that ``betrecs`` / ``eventmarkets`` read). This flag
# gates ONLY the LIVE application; the SHADOW recompute in the scorer and the
# ``shrink`` field written here are untouched by it. Default ON — flip
# ``WCA_SHRINK_LIVE=0`` to restore the EXACT pre-promotion behaviour (raw blend
# drives everything). Reversible, no data migration.
_SHRINK_LIVE_ENV = "WCA_SHRINK_LIVE"


def shrink_live_enabled() -> bool:
    """True unless ``WCA_SHRINK_LIVE`` is explicitly a falsey value.

    Default ON (per the 2026-07-09 approval). ``0`` / ``false`` / ``no`` / ``off``
    (case-insensitive) turns the LIVE shrink OFF so the raw blend drives
    edge/EV/sizing exactly as it did before promotion. Any other value — or the
    variable being unset — leaves it ON.
    """
    raw = os.environ.get(_SHRINK_LIVE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def shrink_triple(
    model: Mapping[str, float],
    market: Mapping[str, float],
) -> Optional[Dict[str, float]]:
    """Public alias for the promoted shrink-to-market transform.

    This is the EXACT function the ``shrink`` shadow family has always used
    (:func:`_shrink_triple`); it was promoted verbatim to the LIVE model line so
    the live behaviour can never drift from what the scoreboard scored. Returns
    ``None`` (caller keeps the raw blend) when either triple is missing.
    """
    return _shrink_triple(model, market)


def _triple(probs: Dict[str, float]) -> Dict[str, float]:
    return {leg: round(float(probs[leg]), 6) for leg in _LEGS}


def _renorm(triple: Dict[str, float]) -> Optional[Dict[str, float]]:
    """Non-negative-clamp then sum-to-one renormalise, or ``None`` if degenerate.

    Legs are floored at 0 (a blend can never legitimately go negative here, but
    float noise / a stray input could); ``None`` is returned when the total is
    non-positive so a caller omits the shadow rather than emitting a fabricated
    uniform triple.
    """
    clamped = {leg: max(0.0, float(triple.get(leg, 0.0))) for leg in _LEGS}
    total = sum(clamped.values())
    if total <= 0.0:
        return None
    return {leg: clamped[leg] / total for leg in _LEGS}


def _valid_triple(probs: Any) -> bool:
    """True iff *probs* carries a finite number for every 1X2 leg."""
    if not isinstance(probs, Mapping):
        return False
    return all(isinstance(probs.get(leg), (int, float)) for leg in _LEGS)


def _mw90_triple(
    elo: Mapping[str, float],
    dc: Mapping[str, float],
    market: Mapping[str, float],
) -> Optional[Dict[str, float]]:
    """``0.9*market + 0.1*(0.25*elo + 0.75*dc)`` renormalised, or ``None``.

    SHADOW-ONLY 1X2 variant (n=73 evidence: in-sample optimal market weight was
    100%; this parks a 90/10 residual blend so it can be CLV/Brier-scored before
    any move toward the market touches live pricing). Returns ``None`` when any
    of the three component triples is missing/malformed — never fabricated.
    """
    if not (_valid_triple(elo) and _valid_triple(dc) and _valid_triple(market)):
        return None
    residual = {
        leg: _MW90_ELO_WEIGHT * float(elo[leg]) + _MW90_DC_WEIGHT * float(dc[leg])
        for leg in _LEGS
    }
    blended = {
        leg: _MW90_W_MARKET * float(market[leg]) + (1.0 - _MW90_W_MARKET) * residual[leg]
        for leg in _LEGS
    }
    return _renorm(blended)


def _shrink_triple(
    model: Mapping[str, float],
    market: Mapping[str, float],
) -> Optional[Dict[str, float]]:
    """Shrink-to-market ``p' = p_mkt + k*(p_model - p_mkt)`` renormalised.

    SHADOW-ONLY 1X2 variant. ``k = 0.5`` for legs the model rates ``>= 0.25``
    and ``k = 0.25`` for legs it rates ``< 0.25`` (pulls the anti-signal model
    longshots harder toward the market — n=99 legs ran 15.8% predicted vs 10.1%
    realized). The per-leg shrink is applied *before* renormalisation across the
    three legs. Returns ``None`` when either input triple is missing.
    """
    if not (_valid_triple(model) and _valid_triple(market)):
        return None
    shrunk = {}
    for leg in _LEGS:
        p_model = float(model[leg])
        p_mkt = float(market[leg])
        k = _SHRINK_K_MID if p_model >= _SHRINK_LONGSHOT_PROB else _SHRINK_K_LONGSHOT
        shrunk[leg] = p_mkt + k * (p_model - p_mkt)
    return _renorm(shrunk)


def _disagree3pp(
    model: Mapping[str, float],
    market: Mapping[str, float],
) -> Optional[Dict[str, bool]]:
    """Per-leg ``|model - market| >= 3pp`` flags, or ``None`` if inputs missing.

    SHADOW-ONLY diagnostic: disagreement legs were anti-signal *both* ways at
    n=73 (model>=mkt+3pp legs hit 17.9% vs market 20.6%; mkt>=model+3pp legs hit
    65.5% vs model 57.7%), so the scorer can slice paired diffs on this flag.
    """
    if not (_valid_triple(model) and _valid_triple(market)):
        return None
    return {
        leg: abs(float(model[leg]) - float(market[leg])) >= _DISAGREE_PP
        for leg in _LEGS
    }


def _onex2_shadows(
    model: Mapping[str, float],
    elo: Mapping[str, float],
    dc: Mapping[str, float],
    market: Mapping[str, float],
) -> Dict[str, Any]:
    """All 1X2 shadow fields present for a row, guarded on the market triple.

    Emits nothing (``{}``) when the fixture has no usable market triple — the
    hard guard from the spec: shadows are omitted for a row rather than
    fabricated. When present, keys are ``mw90`` (triple), ``shrink`` (triple)
    and ``disagree3pp`` (per-leg bools).
    """
    if not _valid_triple(market):
        return {}
    out: Dict[str, Any] = {}
    mw90 = _mw90_triple(elo, dc, market)
    if mw90 is not None:
        out["mw90"] = _triple(mw90)
    shrink = _shrink_triple(model, market)
    if shrink is not None:
        out["shrink"] = _triple(shrink)
    flags = _disagree3pp(model, market)
    if flags is not None:
        out["disagree3pp"] = flags
    return out


def _lambdas_for(dc_model: Any, fb: Any) -> Optional[Dict[str, float]]:
    """``{lambda_home, lambda_away}`` goal means for a blend, or ``None``.

    Uses the *same* fitted DC model and neutral-venue flag that produced the
    persisted DC 1X2, so the goal expectations are leakage-free (lagged fit, no
    future info) and consistent with the 1X2 already in the row. Any failure
    (unseen team, missing attribute) returns ``None`` so the entry is still
    written without lambdas and older readers never crash.
    """
    if dc_model is None:
        return None
    expected = getattr(dc_model, "expected_lambdas", None)
    if expected is None:
        return None
    try:
        lam_h, lam_a = expected(
            fb.home, fb.away, neutral=bool(getattr(fb, "neutral", True)), warn=False
        )
    except Exception:
        return None
    if lam_h is None or lam_a is None:
        return None
    return {
        "lambda_home": round(float(lam_h), 6),
        "lambda_away": round(float(lam_a), 6),
    }


def _totals_prior_for(
    lambdas: Optional[Dict[str, float]],
    match_id: str,
    totals_quotes_by_match: Optional[Mapping[str, Sequence[Any]]],
) -> Optional[Dict[str, Any]]:
    """Totals-market-implied lambda + blend for one fixture, or ``None``.

    SHADOW-ONLY (P6 quant-ladder item #1, ``docs/HANDOFF_2026-07-03.md`` §4):
    de-vigs the fixture's Over/Under totals ladder into an implied total-goals
    lambda (:mod:`wca.models.totals_prior`) and blends it with the deployed DC
    lambda. Returns ``None`` whenever there is no DC lambda to blend against or
    no quotes lookup was supplied at all, so older callers / entries are
    completely unaffected (mirrors the ``gb_lambda_*`` additive pattern).
    """
    if lambdas is None or not totals_quotes_by_match:
        return None
    quotes = totals_quotes_by_match.get(match_id)
    if not quotes:
        return None
    try:
        from wca.models.totals_prior import compute_totals_prior

        result = compute_totals_prior(
            lambdas["lambda_home"], lambdas["lambda_away"], quotes
        )
    except Exception:
        return None
    return {
        "tl_lambda_market_total": (
            round(result.lambda_market_total, 6)
            if result.lambda_market_total is not None
            else None
        ),
        "tl_n_market_quotes": result.n_market_quotes,
        "tl_weight_market": round(result.weight_market, 6),
        "tl_lambda_blend_home": round(result.lambda_blend_home, 6),
        "tl_lambda_blend_away": round(result.lambda_blend_away, 6),
    }


def build_predictions(
    blends: List[Any],
    now_utc: str,
    dc_model: Any = None,
    gb_model: Any = None,
    totals_quotes_by_match: Optional[Mapping[str, Sequence[Any]]] = None,
) -> Dict[str, Any]:
    """JSON-ready payload from ``card._iter_fixture_blends`` output.

    ``now_utc`` is supplied by the caller (no clock reads here) and stamps both
    the meta block and each fixture row so log lines are self-contained.

    ``dc_model`` (the fitted :class:`~wca.models.dixon_coles.DixonColesModel`)
    is optional. When supplied, each fixture row additionally carries
    ``lambda_home`` / ``lambda_away`` — the per-fixture Poisson goal means from
    the *same* lagged fit that produced the DC 1X2, honouring the neutral-venue
    flag. These are the compact sufficient statistic the correlated-exposure
    model reconstructs the full scoreline matrix from (so the 49/121-cell matrix
    is never persisted). Older entries without lambdas stay valid.

    ``gb_model`` (the F7 goal-blend, a drop-in DC with ``expected_lambdas``)
    is optional and SHADOW-ONLY: when supplied, rows additionally carry
    ``gb_lambda_home`` / ``gb_lambda_away`` so the blend's out-of-sample CLV
    can be compared against the deployed DC before it ever drives sizing.

    ``totals_quotes_by_match`` (``{match_id: [TotalsQuote, ...]}``) is optional
    and SHADOW-ONLY (P6 totals-lambda-prior): when supplied and a DC lambda is
    available for the fixture, rows additionally carry
    ``tl_lambda_market_total`` (the de-vigged totals-market-implied total-goals
    lambda; ``None`` if the ladder had no usable complete O/U pair),
    ``tl_n_market_quotes`` (how many quotes fed it), ``tl_weight_market`` (the
    credibility weight actually applied) and ``tl_lambda_blend_home`` /
    ``tl_lambda_blend_away`` (the blended lambda, model shrunk toward the
    market). See :mod:`wca.models.totals_prior` for the exact de-vig/blend
    method. NOT consumed by any pricing or sizing path in this change.

    Each row carries BOTH ``model`` and ``model_raw``:

    * ``model`` — the LIVE model line the desk bets against. Since the 2026-07-09
      shrink promotion (:func:`shrink_live_enabled`, ``WCA_SHRINK_LIVE``) this is
      the shrink-to-market blend when the flag is on, and the RAW blend when it
      is off. The value comes from ``fb.blended``, which
      :func:`wca.card._iter_fixture_blends` already shrinks upstream, so
      ``betrecs`` / ``eventmarkets`` read the shrunk value with no change.
    * ``model_raw`` — the RAW (pre-shrink) Elo/DC/market blend, from
      ``fb.blended_raw``. Preserved so the shadow scoreboard can keep comparing
      RAW -> shrunk -> market after promotion (the ``shrink`` shadow becomes the
      live line and ``raw`` becomes the family scored against it).

    1X2 SHADOW variants are additionally emitted for every row that carries a
    usable de-vigged market triple (all rows in practice), computed from the
    ``model_raw`` / ``elo`` / ``dc`` / ``market`` triples in the row (the RAW
    model, so the shadow comparison is never blinded by the live shrink):
    ``mw90`` (``0.9*market + 0.1*(0.25*elo + 0.75*dc)``, renormalised — the
    near-market blend the n=73 evidence favoured), ``shrink`` (shrink-to-market
    ``p' = p_mkt + k*(p_model - p_mkt)`` with ``k=0.5`` for model legs ``>=0.25``
    and ``k=0.25`` below, renormalised — the family PROMOTED to the live line)
    and ``disagree3pp`` (per-leg ``|model - market| >= 3pp`` booleans). The
    shadow fields themselves never touch live pricing/sizing/selection and are
    scored by ``scripts/wca_shadow_score.py``. A fixture with no market triple
    carries none of them (guarded — never fabricated).
    """
    fixtures: List[Dict[str, Any]] = []
    for fb in blends:
        match_id = str(fb.fx.get("event_id", ""))
        # ``fb.blended`` is the LIVE model line: the shrunk blend when the
        # WCA_SHRINK_LIVE promotion is on, the raw blend when it is off. The RAW
        # (pre-shrink) blend is ``fb.blended_raw`` when the producer supplies it
        # (``_iter_fixture_blends`` always does); older callers / test stubs that
        # only carry ``blended`` fall back to it (there the two coincide).
        raw_blend = getattr(fb, "blended_raw", None)
        if raw_blend is None:
            raw_blend = fb.blended
        row: Dict[str, Any] = {
            "generated": now_utc,
            "fixture": "%s vs %s" % (fb.home, fb.away),
            "match_id": match_id,
            "kickoff": str(fb.fx.get("commence_time", "")),
            "model": _triple(fb.blended),       # LIVE line (shrunk when on)
            "model_raw": _triple(raw_blend),    # RAW pre-shrink blend (shadow baseline)
            "elo": _triple(fb.elo_map),
            "dc": _triple(fb.dc_map),
            "market": _triple(fb.mkt_map),
        }
        # 1X2 SHADOW variants (mw90 / shrink / disagree3pp): recomputed from the
        # RAW model + market so the scoreboard keeps comparing raw -> shrunk ->
        # market honestly even AFTER ``model`` becomes the shrunk live line.
        # Guarded — a fixture with no market triple carries none of them.
        row.update(_onex2_shadows(row["model_raw"], row["elo"], row["dc"], row["market"]))
        lambdas = _lambdas_for(dc_model, fb)
        if lambdas is not None:
            row.update(lambdas)
        gb_lambdas = _lambdas_for(gb_model, fb)
        if gb_lambdas is not None:
            row["gb_lambda_home"] = gb_lambdas["lambda_home"]
            row["gb_lambda_away"] = gb_lambdas["lambda_away"]
        totals_prior = _totals_prior_for(lambdas, match_id, totals_quotes_by_match)
        if totals_prior is not None:
            row.update(totals_prior)
        fixtures.append(row)
    fixtures.sort(key=lambda f: (f["kickoff"], f["fixture"]))
    return {"meta": {"generated": now_utc}, "fixtures": fixtures}


def write_predictions(
    payload: Dict[str, Any],
    latest_path: str = LATEST_PATH,
    log_path: str = LOG_PATH,
) -> None:
    """Overwrite the latest snapshot and append every fixture to the log.

    An empty fixture list never clobbers a populated latest file (mirrors the
    linemove transient-failure guard).
    """
    latest = Path(latest_path)
    log = Path(log_path)
    latest.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)

    if payload["fixtures"] or not _has_fixtures(latest):
        latest.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if payload["fixtures"]:
        with log.open("a", encoding="utf-8") as fh:
            for row in payload["fixtures"]:
                fh.write(json.dumps(row, sort_keys=True) + "\n")

    # Point-in-time copy of this build into the parquet archive (best-effort).
    if _archive_tee is not None and payload["fixtures"]:
        _archive_tee.model_payload(payload)


def _has_fixtures(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("fixtures"))


def load_latest(path: str = LATEST_PATH) -> Dict[str, Dict[str, float]]:
    """Map fixture string -> exact blended 1X2 triple from the latest snapshot.

    Returns an empty dict when the file is missing or malformed so callers can
    fall back to their existing approximations.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for fx in data.get("fixtures", []):
        model = fx.get("model")
        name = fx.get("fixture")
        if not name or not isinstance(model, dict):
            continue
        if all(isinstance(model.get(leg), (int, float)) for leg in _LEGS):
            out[str(name)] = {leg: float(model[leg]) for leg in _LEGS}
    return out


def load_lambdas(path: str = LATEST_PATH) -> Dict[str, Dict[str, float]]:
    """Map fixture string -> ``{lambda_home, lambda_away}`` from the snapshot.

    Only fixtures whose row carries both finite lambdas are returned, so a caller
    can cleanly fall back to its legacy behaviour for older entries that predate
    lambda persistence. Missing / malformed file -> empty dict.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for fx in data.get("fixtures", []):
        name = fx.get("fixture")
        lam_h = fx.get("lambda_home")
        lam_a = fx.get("lambda_away")
        if not name:
            continue
        if isinstance(lam_h, (int, float)) and isinstance(lam_a, (int, float)):
            out[str(name)] = {
                "lambda_home": float(lam_h),
                "lambda_away": float(lam_a),
            }
    return out


def load_totals_prior(path: str = LATEST_PATH) -> Dict[str, Dict[str, Optional[float]]]:
    """Map fixture string -> the totals-lambda-prior shadow fields, if present.

    SHADOW-ONLY reader (mirrors :func:`load_lambdas`): only fixtures whose row
    actually carries ``tl_lambda_blend_home`` / ``tl_lambda_blend_away`` are
    returned (older entries, or entries built without a totals-quotes lookup,
    are simply absent — no fabrication, no crash). Intended for CLV/OOS
    comparison tooling, not for any live pricing or sizing path.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for fx in data.get("fixtures", []):
        name = fx.get("fixture")
        lam_h = fx.get("tl_lambda_blend_home")
        lam_a = fx.get("tl_lambda_blend_away")
        if not name:
            continue
        if isinstance(lam_h, (int, float)) and isinstance(lam_a, (int, float)):
            out[str(name)] = {
                "tl_lambda_market_total": fx.get("tl_lambda_market_total"),
                "tl_n_market_quotes": fx.get("tl_n_market_quotes"),
                "tl_weight_market": fx.get("tl_weight_market"),
                "tl_lambda_blend_home": float(lam_h),
                "tl_lambda_blend_away": float(lam_a),
            }
    return out
