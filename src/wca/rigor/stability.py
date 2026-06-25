"""Stability gate G6 — is the edge persistent, or a decaying / broken signal?

An edge that only existed in the first half of the sample is not a repeatable
edge; it is a regime that has since closed.  G6 combines two checks on the CLV
(or per-unit return) series ordered in time:

1. **No structural break.**  We run a simple max-CUSUM / split-mean scan: for
   every interior split point we compare the mean before and after, and flag a
   break if the largest standardised mean shift exceeds a conservative
   threshold (scaled so it does not trip on pure noise at small N).

2. **Out-of-sample holds up.**  Split the (time-ordered) series into an
   in-sample first half and an out-of-sample second half; the gate requires
   ``mean_OOS / mean_IS > 0.5`` — the edge in the held-out tail retains at
   least half its in-sample size.  If the in-sample mean is non-positive the
   ratio is undefined and the gate cannot pass (there was no edge to retain).

The gate passes only when there is **no break** *and* the OOS/IS ratio clears
0.5.  Below a minimum length it returns ``None`` (insufficient), never a pass.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Sequence

import numpy as np

MIN_LEN = 8  # below this the split is meaningless.
OOS_IS_FLOOR = 0.5
BREAK_Z = 3.0  # conservative: ~0.3% two-sided per single look.


def stability_block(series: Sequence[float]) -> Dict[str, object]:
    """Gate G6 inputs from a time-ordered value series (CLV or per-unit P&L).

    Returns ``{break_detected, oos_is_ratio, pass}``.  ``pass`` is ``None``
    when the series is too short to judge.
    """
    x = np.asarray(list(series), dtype=float)
    n = len(x)
    if n < MIN_LEN:
        return {"break_detected": None, "oos_is_ratio": None, "pass": None}

    # --- structural break: max standardised split-mean shift -------------
    grand_sd = float(np.std(x, ddof=1))
    break_detected = False
    max_z = 0.0
    if grand_sd > 0:
        for k in range(2, n - 1):
            a = x[:k]
            b = x[k:]
            ma, mb = float(a.mean()), float(b.mean())
            # Pooled SE of the difference in means at this split.
            se = grand_sd * math.sqrt(1.0 / len(a) + 1.0 / len(b))
            if se <= 0:
                continue
            z = abs(ma - mb) / se
            if z > max_z:
                max_z = z
        break_detected = bool(max_z > BREAK_Z)

    # --- OOS / IS ratio --------------------------------------------------
    half = n // 2
    is_mean = float(x[:half].mean())
    oos_mean = float(x[half:].mean())
    if is_mean > 0:
        ratio = oos_mean / is_mean
    else:
        ratio = None  # no in-sample edge -> ratio undefined.

    if ratio is None:
        passed = False  # nothing to retain.
    else:
        passed = bool((not break_detected) and ratio > OOS_IS_FLOOR)

    return {
        "break_detected": break_detected,
        "oos_is_ratio": ratio,
        "max_break_z": max_z,
        "pass": passed,
    }
