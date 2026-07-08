"""Live portfolio metrics for the Risk & Blind Spots dashboard.

Computes EV, best/worst-case scenarios and win probabilities from the canonical
ledger, published to ``site/exposure_dashboard.json`` for on-load rendering.

Honesty rules (F5)
------------------
* ``p_profit`` / ``p_loss`` / ``p_win_50`` are derived from the **real
  model-conditional result-scenario distribution** (the same engine that powers
  the full exposure feed, :func:`wca.exposure.build_exposure_data`), NOT from
  hardcoded constants.  When the model's 1X2 slate isn't available these fields
  are reported as ``None`` (unavailable) rather than fabricated.
* ``best_case`` / ``worst_case`` are **currency-coherent**: GBP (sportsbook) and
  USD (Polymarket) legs are reported separately, never summed into one number.
  The legacy single ``best_case`` / ``worst_case`` keys are retained for
  backward compatibility and carry the **GBP** total only (the sportsbook book),
  with the USD figures exposed under ``best_case_usd`` / ``worst_case_usd`` and
  the full split under ``by_currency``.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any, Dict, List, Optional

from wca import exposure
from wca.ledger import store

logger = logging.getLogger(__name__)

#: Big-win threshold used for the ``p_win_50`` metric (P&L >= this, GBP book).
WIN_50_THRESHOLD: float = 50.0

#: Platforms whose stakes/returns are denominated in USD; everything else GBP.
_USD_PLATFORMS = {"polymarket"}

_DEFAULT_PREDS_PATH = "data/model_predictions.json"

# Knockout stage NESTING order (earliest/shallowest first) — mirrors
# wca.advancement.STAGE_ORDER / NESTED_PATH_STAGES (the sizing-side fix, #183:
# apply_path_exposure_caps, which caps PRE-bet PM stakes at feed-build time).
# Reaching a later stage is a strict subset of reaching every earlier one, so
# a team's ledger rows across these stages are NOT independently losable —
# see _advancement_team_stage below. This module is the LEDGER-side analogue:
# #183 fixes sizing for candidate recs before a bet is placed; once a bet is
# open in data/wca.db this dashboard's worst_case independently summed every
# nested rung's stake as if it were separately losable — the same correlation
# bug, one layer later. #183 does not touch this file.
_KO_STAGE_NEST_ORDER: tuple = ("R32", "R16", "QF", "SF", "FINAL", "WIN")
_KO_STAGE_RANK: Dict[str, int] = {s: i for i, s in enumerate(_KO_STAGE_NEST_ORDER)}

# ledger selection strings for advancement bets are "reach_<STAGE>" (see
# scripts/wca_betrecs.py build_advancement_futures / wca_pm_fire.py); match_desc
# is "<Team> <Stage> (advancement)" for Action-Desk-fired orders. Both are
# parsed defensively — if either doesn't match, the bet is left OUT of the
# nested-path grouping and falls back to being treated independently.
_SELECTION_STAGE_RE = re.compile(r"reach_([A-Za-z0-9]+)", re.IGNORECASE)
_MATCH_DESC_TEAM_STAGE_RE = re.compile(
    r"^(?P<team>.+?)\s+(?P<stage>R32|R16|QF|SF|Final|Win)\b", re.IGNORECASE
)


def _currency_for(platform: Any) -> str:
    return "USD" if str(platform or "").strip().lower() in _USD_PLATFORMS else "GBP"


def _is_free(source: Any) -> bool:
    """Promo / free-bet: a loss costs nothing (stake not returned)."""
    return str(source or "").strip().lower() == "offer"


def _advancement_team_stage(bet: Dict[str, Any]) -> Optional[tuple]:
    """Return ``(team, stage_rank)`` for an advancement bet, or ``None``.

    Only ``market == "advancement"`` rows are candidates. The stage comes from
    ``selection`` (``"reach_<STAGE>"``, the canonical form written by
    build_advancement_futures / the PM fire path); the team comes from
    ``match_desc`` (``"<Team> <Stage> (advancement)"``, written by
    wca_pm_fire.py / wca.bot.app for Action-Desk-fired orders). Returns
    ``None`` (never grouped) whenever either can't be confidently parsed —
    the caller then treats the bet independently, exactly as before this fix,
    which is the SAFE default (never overstates nor understates a bet the
    aggregator can't identify).
    """
    if str(bet.get("market") or "").strip().lower() != "advancement":
        return None
    sel = str(bet.get("selection") or "")
    m_stage = _SELECTION_STAGE_RE.search(sel)
    if not m_stage:
        return None
    stage = m_stage.group(1).strip().upper()
    if stage not in _KO_STAGE_RANK:
        return None
    match_desc = str(bet.get("match_desc") or "")
    m_team = _MATCH_DESC_TEAM_STAGE_RE.match(match_desc.strip())
    if not m_team:
        return None
    team = m_team.group("team").strip()
    if not team:
        return None
    return (team, _KO_STAGE_RANK[stage])


def _currency_best_worst(bets: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Per-currency best/worst-case bounds, never mixing GBP and USD.

    best_case: every favourite (model prob > 0.5) wins → sum of profits.
    worst_case: every real-money underdog (prob <= 0.5) loses → sum of stakes
    (free bets lose £0), EXCEPT that advancement bets on the SAME team across
    NESTED knockout stages (reach SF ⊂ reach Final ⊂ ... — see
    _advancement_team_stage / _KO_STAGE_NEST_ORDER) are NOT independently
    losable: a team eliminated before its earliest recommended rung loses
    every one of those rungs TOGETHER, on the same result. Their joint
    worst-case contribution is therefore the MAX single stake among that
    team's nested rungs, not the SUM (summing double/triple-counts one
    correlated elimination as if it were several independent ones — the same
    correlation bug fixed pre-bet, at PM-sizing time, by #183
    (wca.advancement.apply_path_exposure_caps); this is the LEDGER-side
    analogue for bets already open in data/wca.db).

    These are still loose per-bet(-group) bounds (the honest joint
    distribution lives in ``p_profit``/``p_loss``), but each is computed within
    a single currency so the number is coherent.
    """
    out: Dict[str, Dict[str, float]] = {
        "GBP": {"best_case": 0.0, "worst_case": 0.0},
        "USD": {"best_case": 0.0, "worst_case": 0.0},
    }
    # Nested advancement underdog stakes, grouped by (currency, team); each
    # value collects every nested-stage stake for that team so the group's
    # worst-case contribution can be capped at the max rung afterwards instead
    # of summed rung-by-rung.
    nested_group_stakes: Dict[tuple, List[float]] = {}

    for b in bets:
        cur = _currency_for(b.get("platform"))
        stake = float(b.get("stake") or 0.0)
        odds = float(b.get("decimal_odds") or 0.0)
        prob = b.get("model_prob")
        prob = float(prob) if prob is not None else 0.5
        if prob > 0.5:
            out[cur]["best_case"] += stake * (odds - 1.0)
        elif not _is_free(b.get("source")):
            team_stage = _advancement_team_stage(b)
            if team_stage is not None:
                team, _stage_rank = team_stage
                nested_group_stakes.setdefault((cur, team), []).append(stake)
            else:
                out[cur]["worst_case"] -= stake

    # Nested groups: the joint worst case for perfectly-correlated nested
    # rungs is the largest single rung's stake, not the sum across rungs. A
    # team with only ONE advancement bet open (len == 1) is unaffected —
    # max() of a single-element list equals that element's own stake, so this
    # is a strict superset of the old per-bet behaviour (invariance for
    # single-rung teams).
    for (cur, _team), stakes in nested_group_stakes.items():
        out[cur]["worst_case"] -= max(stakes)

    for cur in out:
        out[cur]["best_case"] = round(out[cur]["best_case"], 2)
        out[cur]["worst_case"] = round(out[cur]["worst_case"], 2)
    return out


def _has_modelable_exposure(data: Dict[str, Any]) -> bool:
    """True iff at least one open bet maps onto the model's 1X2 slate.

    The scenario engine can only score result singles and accas whose legs are
    on the upcoming slate. When every open bet is off-slate (outrights, futures,
    bet-builders, props) the scenario P&L distribution carries no exposure and
    its win-probabilities are meaningless — this lets the caller mark them
    unavailable instead of shipping a hollow 0.

    Detected from the engine's own output: a fixture carries modelable exposure
    iff it has stake at risk or at least one live (single/acca) leg on a result
    outcome.
    """
    for fx in data.get("fixtures") or []:
        summary = fx.get("summary") or {}
        if float(summary.get("stake_at_risk") or 0.0) > 0.0:
            return True
        for row in fx.get("results") or []:
            if row.get("live"):
                return True
    return False


def _load_model_fixtures(preds_path: str) -> List[Dict[str, Any]]:
    """Model 1X2 slate from ``data/model_predictions.json`` (``[]`` on failure)."""
    try:
        with open(preds_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    fixtures = (data.get("fixtures") if isinstance(data, dict) else data) or []
    out: List[Dict[str, Any]] = []
    for f in fixtures:
        if f.get("fixture") and (f.get("model") or {}):
            out.append(
                {"fixture": f["fixture"], "kickoff": f.get("kickoff"),
                 "model": f.get("model")}
            )
    return out


def _empty_metrics() -> Dict[str, Any]:
    return {
        "ev": 0.0,
        "best_case": 0.0,
        "worst_case": 0.0,
        "best_case_usd": 0.0,
        "worst_case_usd": 0.0,
        "by_currency": {
            "GBP": {"best_case": 0.0, "worst_case": 0.0},
            "USD": {"best_case": 0.0, "worst_case": 0.0},
        },
        "p_profit": None,
        "p_loss": None,
        "p_win_50": None,
        "p_metrics_available": False,
    }


def compute_dashboard_metrics(
    db_path: str,
    preds_path: str = _DEFAULT_PREDS_PATH,
    now_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute portfolio metrics from open bets.

    ``p_profit`` / ``p_loss`` / ``p_win_50`` come from the honest scenario
    distribution; they are ``None`` (with ``p_metrics_available=False``) when the
    model slate can't be resolved.  ``best_case``/``worst_case`` are the GBP
    book; USD is exposed separately (see :data:`by_currency`).
    """
    updated_at = now_utc or (datetime.datetime.utcnow().isoformat() + "Z")
    try:
        conn = store._connect(db_path)
        cur = conn.execute(
            "SELECT id, match_id, match_desc, selection, market, platform, "
            "source, stake, decimal_odds, model_prob, ev "
            "FROM bets WHERE status = 'open' ORDER BY match_id"
        )
        cols = [c[0] for c in cur.description]
        bets = [dict(zip(cols, row)) for row in cur.fetchall()]
        conn.close()

        if not bets:
            return {
                "metrics": _empty_metrics(),
                "blind_spots": [],
                "worst_result_states": [],
                "updated_at": updated_at,
                "n_open_bets": 0,
            }

        total_ev = sum(float(b.get("ev") or 0.0) for b in bets)
        by_currency = _currency_best_worst(bets)

        # Honest win probabilities from the model-conditional scenario engine.
        model_fixtures = _load_model_fixtures(preds_path)
        p_profit: Optional[float] = None
        p_loss: Optional[float] = None
        p_win_50: Optional[float] = None
        p_available = False
        worst_states: List[Dict[str, Any]] = []
        if model_fixtures:
            try:
                data = exposure.build_exposure_data(
                    bets=bets, model_fixtures=model_fixtures, now_utc=now_utc,
                )
                pf = data.get("portfolio") or {}
                # The scenario engine only models 1X2 singles / accas on the
                # upcoming slate. If none of the open bets map to it (all are
                # outrights / futures / props — "off-slate"), the win-prob
                # metrics are trivially zero and uninformative: report them
                # unavailable rather than as a fabricated 0.
                mappable = pf.get("n_scenarios", 0) > 1 and _has_modelable_exposure(data)
                if mappable:
                    p_profit = pf.get("p_profit")
                    p_loss = pf.get("p_loss")
                    p_win_50 = pf.get("p_big_win")  # P(P&L >= £50)
                    p_available = True
                    corr = data.get("correlation") or {}
                    worst_states = [
                        {"scenario": " / ".join(s.get("results", [])),
                         "loss": s.get("pnl"), "prob": s.get("prob")}
                        for s in (corr.get("worst_states") or [])[:5]
                    ]
            except Exception as exc:  # noqa: BLE001 — never fabricate on failure
                logger.warning("scenario metrics unavailable: %s", exc)

        metrics = {
            "ev": round(total_ev, 2),
            # Legacy single-currency keys carry the GBP book only.
            "best_case": by_currency["GBP"]["best_case"],
            "worst_case": by_currency["GBP"]["worst_case"],
            "best_case_usd": by_currency["USD"]["best_case"],
            "worst_case_usd": by_currency["USD"]["worst_case"],
            "by_currency": by_currency,
            "p_profit": (round(p_profit, 2) if p_profit is not None else None),
            "p_loss": (round(p_loss, 2) if p_loss is not None else None),
            "p_win_50": (round(p_win_50, 2) if p_win_50 is not None else None),
            "p_metrics_available": p_available,
        }

        return {
            "metrics": metrics,
            "blind_spots": [],  # detailed blind-spot analysis lives in exposure_data.json
            "worst_result_states": worst_states,
            "updated_at": updated_at,
            "n_open_bets": len(bets),
        }
    except Exception as exc:
        logger.warning("compute_dashboard_metrics failed: %s", exc)
        metrics = _empty_metrics()
        metrics.update({"ev": None, "best_case": None, "worst_case": None,
                        "best_case_usd": None, "worst_case_usd": None})
        return {
            "metrics": metrics,
            "blind_spots": [],
            "worst_result_states": [],
            "updated_at": updated_at,
            "n_open_bets": 0,
        }


def publish_dashboard_json(db_path: str, output_path: str = "site/exposure_dashboard.json") -> None:
    """Compute metrics and publish to site/exposure_dashboard.json."""
    metrics = compute_dashboard_metrics(db_path)
    try:
        import pathlib
        pathlib.Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(metrics, f, indent=2)
        logger.info("published exposure_dashboard.json: %s", output_path)
    except Exception as exc:
        logger.error("failed to publish exposure_dashboard.json: %s", exc)
