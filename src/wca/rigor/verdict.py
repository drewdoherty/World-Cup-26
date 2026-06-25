"""Gate G7 (multiple-testing on segments) and the final VERDICT assembly.

G7 — Benjamini-Hochberg over segments
-------------------------------------
We slice the book into segments (market type, source, platform-currency, ...)
and test each for a positive edge.  Testing many segments and reporting the
best one is the classic garden-of-forking-paths inflation.  G7 applies the
Benjamini-Hochberg (1995) FDR procedure to the per-segment p-values and reports
which segments *survive* at FDR 5%.  At small N the honest result is that **no**
segment survives — which is what the verdict must reflect.

VERDICT
-------
The level is assembled from the gate flags with one inviolable rule:

    *Base green is never granted on CLV alone.*

Concretely::

    green (EDGE_LIKELY)  requires  (G1 & G2 & G3)            # CLV passes ...
                          AND       (G4 OR G5)               # ... and skill/calib
                          AND        G6                      # ... and it persists

If the CLV gates pass but no outcome-anchored gate (G4/G5) does, the verdict is
PROMISING (amber), not green — a best-price-only artifact tops out here.  If
CLV is significantly *negative* the verdict is NO_EDGE (red).  A detected break
with positive history is EDGE_DECAYING (amber).  Anything below the power
floors, or with no usable sample, defaults to INSUFFICIENT_SAMPLE (grey).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from wca.rigor.clv import N_EFF_CLV_MIN


# ---------------------------------------------------------------------------
# Gate G7: Benjamini-Hochberg FDR over segment p-values.
# ---------------------------------------------------------------------------


def benjamini_hochberg(
    p_values: Sequence[Optional[float]], alpha: float = 0.05
) -> List[Optional[bool]]:
    """BH-adjusted survival flags at FDR ``alpha``.

    ``None`` p-values (segments with no testable sample) pass through as
    ``None`` and are excluded from the multiplicity count.  Returns a list of
    ``True`` / ``False`` / ``None`` aligned with the input order.
    """
    indexed = [(i, p) for i, p in enumerate(p_values) if p is not None]
    m = len(indexed)
    survive = [None] * len(p_values)  # type: List[Optional[bool]]
    if m == 0:
        return survive
    ordered = sorted(indexed, key=lambda t: t[1])
    # Largest rank k with p_(k) <= (k/m) * alpha.
    max_k = 0
    for rank, (_, p) in enumerate(ordered, start=1):
        if p <= (rank / m) * alpha:
            max_k = rank
    survivors = set()
    if max_k > 0:
        for rank, (idx, _) in enumerate(ordered, start=1):
            if rank <= max_k:
                survivors.add(idx)
    for i, _ in indexed:
        survive[i] = i in survivors
    return survive


def adjusted_pvalues(
    p_values: Sequence[Optional[float]]
) -> List[Optional[float]]:
    """BH step-up adjusted p-values (``None`` -> ``None``)."""
    indexed = [(i, p) for i, p in enumerate(p_values) if p is not None]
    m = len(indexed)
    adj = [None] * len(p_values)  # type: List[Optional[float]]
    if m == 0:
        return adj
    ordered = sorted(indexed, key=lambda t: t[1])
    prev = 1.0
    # Step-up from largest p to smallest, enforcing monotonicity.
    for rank in range(m, 0, -1):
        idx, p = ordered[rank - 1]
        val = min(prev, p * m / rank)
        adj[idx] = val
        prev = val
    return adj


def segments_block(
    segments: Sequence[Dict[str, object]], alpha: float = 0.05
) -> List[Dict[str, object]]:
    """Attach BH-adjusted p-values + FDR survival to per-segment results.

    Each input segment dict must carry ``key``, ``p_raw`` (one-sided p of a
    positive edge, or ``None``) and ``coverage`` (fraction of the book the
    segment covers).  Returns the segment list with ``p_adj`` and
    ``survives_fdr`` filled in.
    """
    p_raw = [seg.get("p_raw") for seg in segments]
    p_adj = adjusted_pvalues(p_raw)
    survive = benjamini_hochberg(p_raw, alpha=alpha)
    out: List[Dict[str, object]] = []
    for seg, pa, sv in zip(segments, p_adj, survive):
        out.append(
            {
                "key": seg.get("key"),
                "p_raw": seg.get("p_raw"),
                "p_adj": pa,
                "survives_fdr": sv,
                "coverage": seg.get("coverage"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# VERDICT assembly.
# ---------------------------------------------------------------------------

# Human-readable verdict levels and their colour.
_LEVELS = {
    "INSUFFICIENT_SAMPLE": ("grey", "Insufficient sample"),
    "PROMISING": ("amber", "Promising — CLV only"),
    "EDGE_LIKELY": ("green", "Edge likely"),
    "NO_EDGE": ("red", "No edge"),
    "EDGE_DECAYING": ("amber", "Edge decaying"),
    "INCONCLUSIVE": ("amber", "Inconclusive"),
}


def assemble_verdict(
    gate_flags: Dict[str, Optional[bool]],
    clv_block: Dict[str, object],
    n_eff: float,
    *,
    futures_only: bool = False,
) -> Dict[str, str]:
    """Map gate flags -> ``{level, label, reason, color}``.

    ``gate_flags`` keys are ``G0``..``G7`` (``None`` = insufficient/untestable).
    The CLV block supplies the mean/lower so we can tell a negative edge
    (NO_EDGE / red) from merely-insufficient (grey).
    """
    g = gate_flags

    # Futures are permanently insufficient by construction (N ~= 1).
    if futures_only:
        return {
            "level": "INSUFFICIENT_SAMPLE",
            "label": _LEVELS["INSUFFICIENT_SAMPLE"][1],
            "reason": "futures resolve once; effective sample is permanently N~=1",
            "color": "grey",
        }

    mean_clv = clv_block.get("mean")
    clv_lower = clv_block.get("lower")

    # Hard power floor: not enough effective sample to say anything.
    if n_eff < N_EFF_CLV_MIN or g.get("G0") is not True:
        return {
            "level": "INSUFFICIENT_SAMPLE",
            "label": _LEVELS["INSUFFICIENT_SAMPLE"][1],
            "reason": (
                "n_eff=%.1f below the %d-effective-bet CLV floor; "
                "default verdict is insufficient sample"
                % (n_eff, int(N_EFF_CLV_MIN))
            ),
            "color": "grey",
        }

    clv_gates_pass = (g.get("G1") is True and g.get("G2") is True
                      and g.get("G3") is True)
    skill_pass = (g.get("G4") is True) or (g.get("G5") is True)
    stable_pass = g.get("G6") is True

    # Decaying edge: history was positive but a break is detected.
    if g.get("G6") is False and clv_gates_pass and (mean_clv or 0) > 0:
        return {
            "level": "EDGE_DECAYING",
            "label": _LEVELS["EDGE_DECAYING"][1],
            "reason": "CLV gates pass but a structural break / OOS decay is detected",
            "color": "amber",
        }

    # Base green requires CLV gates AND an outcome-anchored gate AND stability.
    if clv_gates_pass and skill_pass and stable_pass:
        return {
            "level": "EDGE_LIKELY",
            "label": _LEVELS["EDGE_LIKELY"][1],
            "reason": (
                "CLV gates (G1-G3) pass, an outcome-anchored gate (G4/G5) "
                "confirms skill, and the edge is stable (G6)"
            ),
            "color": "green",
        }

    # CLV passes but no skill confirmation -> best-price-only artifact ceiling.
    if clv_gates_pass and not skill_pass:
        return {
            "level": "PROMISING",
            "label": _LEVELS["PROMISING"][1],
            "reason": (
                "CLV gates pass but no outcome-anchored skill gate (G4/G5) "
                "confirms it — consistent with best-price selection, not edge"
            ),
            "color": "amber",
        }

    # Significantly negative CLV -> active no-edge / losing to the close.
    if clv_lower is not None and clv_lower < 0 and (mean_clv or 0) < 0 \
            and g.get("G2") is False:
        return {
            "level": "NO_EDGE",
            "label": _LEVELS["NO_EDGE"][1],
            "reason": "mean CLV is negative with the upper region below zero — "
                      "currently losing to the closing line",
            "color": "red",
        }

    # Enough sample, but gates split -> genuinely inconclusive.
    return {
        "level": "INCONCLUSIVE",
        "label": _LEVELS["INCONCLUSIVE"][1],
        "reason": "sufficient sample but gates disagree; no clear edge or no-edge call",
        "color": "amber",
    }
