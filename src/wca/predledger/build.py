"""Flatten a card build into prediction-ledger rows.

:func:`flatten_card` is the bridge between the live card's per-fixture model
output and the prediction ledger's flat row schema.  It is *standalone*: it
takes plain dict/list inputs (not live card objects), so it can be unit-tested
on synthetic fixtures and wired into the real build later without coupling.

Per-market column-population matrix
-----------------------------------
Different markets carry different evidence.  We only populate the market-price
columns (``market_devig_prob`` / ``market_best_odds`` / ``edge`` /
``ev_per_unit``) where a real market price is supplied:

    market        model_prob  market_devig  edge/ev   notes
    1X2           yes         if priced     if priced  3 legs/fixture
    scoreline     yes         NULL          NULL       no per-score market here
    ou            yes         if priced     if priced  Over/Under <line>
    btts          yes         if priced     if priced  Yes/No
    advancement   yes         if priced     if priced  reach-stage / winner

A scoreline / O-U / BTTS row therefore has NULL market columns *unless* the
caller passes a market price for it — never a fabricated de-vig.

Inputs
------
recs:
    list of fixture dicts, each::

        {
          "match_id": str, "fixture": "Home vs Away",
          "kickoff_utc": iso, "stage": "group"|"R16"|...,
          "model": {"home":p,"draw":p,"away":p},      # 1X2 triple (required)
          "elo":   {...}, "dc": {...},                # optional components
          "market":{"home":p,...},                    # optional de-vig triple
          "best_odds": {"home":dec,...},              # optional best book price
          "book": "bet365",                           # optional book label
          "devig_method": "proportional",             # optional
          "ou": [{"line":2.5,"side":"Over","model_prob":p,
                  "market_devig_prob":p?,"best_odds":dec?,"book":str?}],
          "btts": [{"side":"Yes","model_prob":p, ...}],
          "scoreline": [{"score":"1-0","model_prob":p}],
        }
score_cards:
    optional list of scoreline dicts keyed by match_id::

        {"match_id": str, "scores": [{"score":"1-0","model_prob":p}, ...]}

    (merged with any per-fixture ``scoreline`` in *recs*).
advancement_df:
    optional list of advancement dicts::

        {"match_id":str?, "fixture":str?, "team":str, "stage":"R16"|"win"|...,
         "model_prob":p, "market_devig_prob":p?, "best_odds":dec?, "book":str?}
now:
    ISO-8601 UTC timestamp (caller-supplied; no clock read here).

Returns ``(rows, accas)`` — ``rows`` is the flat prediction list, ``accas`` is
a list of ``{"acca_id":str, "pred_ids":[...], "model_prob":p, "fixture":str}``
(empty unless a fixture carries an ``"acca"`` key with leg selections).
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from wca.predledger import store

_LEGS = ("home", "draw", "away")
_LEG_LABEL = {"home": "Home", "draw": "Draw", "away": "Away"}


def _build_id(now: str, recs: Sequence[Dict[str, Any]]) -> str:
    """Deterministic build id from the build timestamp + fixture set.

    Stable for a given ``now`` + fixture list so re-flattening the same build
    upserts (does not duplicate) prediction rows.
    """
    mids = "|".join(sorted(str(r.get("match_id", "")) for r in recs))
    raw = "%s||%s" % (now, mids)
    return "b_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _fair_odds(prob: Optional[float]) -> Optional[float]:
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    return 1.0 / p if 0.0 < p < 1.0 else None


def _edge_ev(
    model_prob: Optional[float],
    market_devig_prob: Optional[float],
    best_odds: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    """``(edge, ev_per_unit)`` only when a market price exists, else ``(None,None)``.

    ``edge = model_prob - market_devig_prob`` (model's probability advantage);
    ``ev_per_unit = model_prob * best_odds - 1`` (expected return per unit
    staked at the best available price).  Both NULL without a market price.
    """
    if model_prob is None or market_devig_prob is None:
        edge = None
    else:
        edge = float(model_prob) - float(market_devig_prob)
    ev = None
    if model_prob is not None and best_odds is not None:
        try:
            ev = float(model_prob) * float(best_odds) - 1.0
        except (TypeError, ValueError):
            ev = None
    return edge, ev


def _base_row(rec: Dict[str, Any], build_id: str, now: str) -> Dict[str, Any]:
    return {
        "build_id": build_id,
        "ts_utc": now,
        "match_id": str(rec.get("match_id", "")),
        "fixture": str(rec.get("fixture", "")),
        "kickoff_utc": str(rec.get("kickoff_utc", "")),
        "stage": str(rec.get("stage", "") or ""),
        "status": "open",
        "model_source": rec.get("model_source", "card"),
    }


def _emit_market_row(
    base: Dict[str, Any],
    market: str,
    selection: str,
    line: float,
    n_outcomes: int,
    model_prob: Optional[float],
    *,
    market_devig_prob: Optional[float] = None,
    best_odds: Optional[float] = None,
    book: Optional[str] = None,
    devig_method: Optional[str] = None,
    elo_prob: Optional[float] = None,
    dc_prob: Optional[float] = None,
) -> Dict[str, Any]:
    edge, ev = _edge_ev(model_prob, market_devig_prob, best_odds)
    row = dict(base)
    row.update(
        {
            "market": market,
            "selection": selection,
            "line": line,
            "n_outcomes": n_outcomes,
            "model_prob": model_prob,
            "model_fair_odds": _fair_odds(model_prob),
            "elo_prob": elo_prob,
            "dc_prob": dc_prob,
            "market_devig_prob": market_devig_prob,
            "market_best_odds": best_odds,
            "market_book": book,
            "devig_method": devig_method,
            "edge": edge,
            "ev_per_unit": ev,
        }
    )
    row["prediction_id"] = store._row_prediction_id(row)
    return row


def _flatten_1x2(rec: Dict[str, Any], base: Dict[str, Any]) -> List[Dict[str, Any]]:
    model = rec.get("model") or {}
    elo = rec.get("elo") or {}
    dc = rec.get("dc") or {}
    mkt = rec.get("market") or {}
    best = rec.get("best_odds") or {}
    book = rec.get("book")
    devig_method = rec.get("devig_method")
    rows: List[Dict[str, Any]] = []
    for leg in _LEGS:
        mp = model.get(leg)
        if mp is None:
            continue
        rows.append(
            _emit_market_row(
                base,
                "1X2",
                _LEG_LABEL[leg],
                -1.0,
                3,
                float(mp),
                market_devig_prob=(float(mkt[leg]) if leg in mkt else None),
                best_odds=(float(best[leg]) if leg in best else None),
                book=book,
                devig_method=devig_method,
                elo_prob=(float(elo[leg]) if leg in elo else None),
                dc_prob=(float(dc[leg]) if leg in dc else None),
            )
        )
    return rows


def _flatten_scoreline(
    rec: Dict[str, Any], base: Dict[str, Any], extra_scores: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    scores = list(rec.get("scoreline") or []) + list(extra_scores or [])
    rows: List[Dict[str, Any]] = []
    seen = set()
    for s in scores:
        score = str(s.get("score", "")).strip()
        if not score or score in seen:
            continue
        seen.add(score)
        mp = s.get("model_prob")
        # Scoreline carries NO market de-vig unless the caller supplies one.
        rows.append(
            _emit_market_row(
                base,
                "scoreline",
                score,
                -1.0,
                len(scores),
                (float(mp) if mp is not None else None),
                market_devig_prob=(
                    float(s["market_devig_prob"])
                    if s.get("market_devig_prob") is not None
                    else None
                ),
                best_odds=(
                    float(s["best_odds"]) if s.get("best_odds") is not None else None
                ),
                book=s.get("book"),
            )
        )
    return rows


def _flatten_ou(rec: Dict[str, Any], base: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for o in rec.get("ou") or []:
        side = str(o.get("side", "")).strip()
        line = o.get("line")
        if not side or line is None:
            continue
        mp = o.get("model_prob")
        rows.append(
            _emit_market_row(
                base,
                "ou",
                "%s %s" % (side, line),
                float(line),
                2,
                (float(mp) if mp is not None else None),
                market_devig_prob=(
                    float(o["market_devig_prob"])
                    if o.get("market_devig_prob") is not None
                    else None
                ),
                best_odds=(
                    float(o["best_odds"]) if o.get("best_odds") is not None else None
                ),
                book=o.get("book"),
                devig_method=o.get("devig_method"),
            )
        )
    return rows


def _flatten_btts(rec: Dict[str, Any], base: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for b in rec.get("btts") or []:
        side = str(b.get("side", "")).strip()
        if not side:
            continue
        mp = b.get("model_prob")
        rows.append(
            _emit_market_row(
                base,
                "btts",
                side,
                -1.0,
                2,
                (float(mp) if mp is not None else None),
                market_devig_prob=(
                    float(b["market_devig_prob"])
                    if b.get("market_devig_prob") is not None
                    else None
                ),
                best_odds=(
                    float(b["best_odds"]) if b.get("best_odds") is not None else None
                ),
                book=b.get("book"),
                devig_method=b.get("devig_method"),
            )
        )
    return rows


def _flatten_advancement(
    adv: Dict[str, Any], build_id: str, now: str
) -> Optional[Dict[str, Any]]:
    team = str(adv.get("team", "")).strip()
    stage = str(adv.get("stage", "")).strip()
    if not team or not stage:
        return None
    base = {
        "build_id": build_id,
        "ts_utc": now,
        "match_id": str(adv.get("match_id", "") or ""),
        "fixture": str(adv.get("fixture", "") or ""),
        "kickoff_utc": str(adv.get("kickoff_utc", "") or ""),
        "stage": stage,
        "status": "open",
        "model_source": adv.get("model_source", "card"),
    }
    mp = adv.get("model_prob")
    return _emit_market_row(
        base,
        "advancement",
        team,
        -1.0,
        2,
        (float(mp) if mp is not None else None),
        market_devig_prob=(
            float(adv["market_devig_prob"])
            if adv.get("market_devig_prob") is not None
            else None
        ),
        best_odds=(
            float(adv["best_odds"]) if adv.get("best_odds") is not None else None
        ),
        book=adv.get("book"),
        devig_method=adv.get("devig_method"),
    )


def flatten_card(
    recs: Sequence[Dict[str, Any]],
    score_cards: Optional[Sequence[Dict[str, Any]]],
    advancement_df: Optional[Sequence[Dict[str, Any]]],
    now: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Flatten a card build into ``(prediction_rows, accas)``.

    See module docstring for the input contract.  Deterministic: the same
    inputs + ``now`` always produce the same ``build_id`` and ``prediction_id``
    values, so re-running upserts rather than duplicates.
    """
    recs = list(recs or [])
    build_id = _build_id(now, recs)
    score_by_mid: Dict[str, List[Dict[str, Any]]] = {}
    for sc in score_cards or []:
        score_by_mid.setdefault(str(sc.get("match_id", "")), []).extend(
            sc.get("scores") or []
        )

    rows: List[Dict[str, Any]] = []
    accas: List[Dict[str, Any]] = []
    for rec in recs:
        base = _base_row(rec, build_id, now)
        rows.extend(_flatten_1x2(rec, base))
        rows.extend(
            _flatten_scoreline(
                rec, base, score_by_mid.get(str(rec.get("match_id", "")), [])
            )
        )
        rows.extend(_flatten_ou(rec, base))
        rows.extend(_flatten_btts(rec, base))

        acca = rec.get("acca")
        if acca and acca.get("pred_ids"):
            aid = store.acca_id(build_id, acca["pred_ids"])
            accas.append(
                {
                    "acca_id": aid,
                    "pred_ids": list(acca["pred_ids"]),
                    "model_prob": acca.get("model_prob"),
                    "fixture": rec.get("fixture", ""),
                    "build_id": build_id,
                }
            )

    for adv in advancement_df or []:
        row = _flatten_advancement(adv, build_id, now)
        if row is not None:
            rows.append(row)

    return rows, accas
