"""Profit / ROI block (drives the ROI half of gate G0 and reports Sharpe).

ROI is realised P&L over realised stake on *settled* bets (won / lost only;
pushes and voids are excluded from both the numerator and the denominator).
Because ROI is dominated by a handful of large-odds wins at small N, the point
estimate is meaningless on its own — we always report a lower confidence bound
and the effective sample size next to it, and the ROI gate threshold is a large
n (3860, the textbook sample to detect a ~2% edge at typical variance) so the
default at the current N is plainly INSUFFICIENT.

Returns are clustered by fixture for the lower bound and Sharpe, so two legs of
one match do not count as two independent profit observations.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np

from wca.rigor.clv import n_eff_clusters

# Pre-registered sample size to *detect* a small ROI edge at conventional
# power.  ~2% edge, per-bet return sd ~1.0 (mix of evens and longshots),
# 80% power, 5% two-sided  ->  n ~= (2.8 / 0.02)^2 ~= 3860.
ROI_SAMPLE_TO_SIG = 3860


def profit_block(
    returns: Sequence[float],
    stakes: Sequence[float],
    cluster_ids: Sequence,
    *,
    seed: int = 20260625,
) -> Dict[str, object]:
    """Compute ROI, a clustered ROI lower bound, Sharpe, and the G0-ROI flag.

    ``returns[i]`` is the settled P&L of bet ``i`` (negative for a loss);
    ``stakes[i]`` its stake; ``cluster_ids[i]`` its fixture / acca id.  Pushes
    and voids must already be excluded by the caller.

    Returns ``{roi, roi_lo, sharpe, n, n_eff, gate}`` where ``gate`` is the
    G0-ROI pass (``n_eff >= ROI floor``) — almost always ``False`` at current
    scale, by design.
    """
    r = np.asarray(list(returns), dtype=float)
    s = np.asarray(list(stakes), dtype=float)
    n = int(len(r))
    if n == 0 or s.sum() <= 0:
        return {
            "roi": None, "roi_lo": None, "sharpe": None,
            "n": n, "n_eff": 0.0, "gate": False,
        }

    roi = float(r.sum() / s.sum())
    # Per-unit return for variance / Sharpe (return per pound staked).
    per_unit = np.divide(r, s, out=np.zeros_like(r), where=s > 0)
    n_eff = n_eff_clusters(per_unit, cluster_ids, seed=seed)

    if n_eff >= 2 and per_unit.std(ddof=1) > 0:
        mu = float(per_unit.mean())
        sd = float(per_unit.std(ddof=1))
        se = sd / math.sqrt(n_eff)
        roi_lo = mu - 1.96 * se
        sharpe = mu / sd if sd > 0 else None
    else:
        roi_lo = None
        sharpe = None

    return {
        "roi": roi,
        "roi_lo": roi_lo,
        "sharpe": sharpe,
        "n": n,
        "n_eff": n_eff,
        # G0-ROI: enough *effective* settled bets to even attempt an ROI claim.
        "gate": bool(n_eff >= 100.0),
    }
