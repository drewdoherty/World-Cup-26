"""Render the test-book equity/P&L curve to a PNG for the @worldcupdevbot ping.

Pure + best-effort: takes the plain series from :func:`wca.testbook.store.equity_series`
and returns PNG bytes (or ``None`` if matplotlib is unavailable or there is
nothing to plot). Network-free and unit-testable.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Dict, List, Optional, Sequence


def _parse_ts(ts: object):
    if not isinstance(ts, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def render_equity_png(series: Sequence[Dict[str, object]], *, seed: float,
                      title: Optional[str] = None) -> Optional[bytes]:
    """Equity curve PNG. Green fill above seed, red below; dashed seed baseline.

    Returns ``None`` (never raises) if matplotlib is missing or ``series`` is empty.
    """
    if not series:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")  # headless — no display needed
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception:
        return None

    eq = [float(p.get("equity", seed)) for p in series]
    dts = [_parse_ts(p.get("ts")) for p in series]
    use_dates = all(d is not None for d in dts) and len(dts) >= 2
    xs: List = dts if use_dates else list(range(len(eq)))
    # A single point plots as a flat baseline so the chart is never blank.
    if len(eq) == 1:
        xs = [0, 1]
        eq = [eq[0], eq[0]]

    final = eq[-1]
    up = final >= seed
    line_c = "#1a9e5f" if up else "#d1435b"

    try:
        fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=130)
        ax.plot(xs, eq, color=line_c, linewidth=2.0, zorder=3)
        ax.axhline(seed, color="#8a8f98", linewidth=1.0, linestyle="--", zorder=1)
        ax.fill_between(xs, eq, seed, where=[e >= seed for e in eq],
                        color="#1a9e5f", alpha=0.14, zorder=2, interpolate=True)
        ax.fill_between(xs, eq, seed, where=[e < seed for e in eq],
                        color="#d1435b", alpha=0.14, zorder=2, interpolate=True)
        roi = (100.0 * (final - seed) / seed) if seed else 0.0
        ax.set_title(title or ("Test book equity  $%.0f  (ROI %+.1f%%)" % (final, roi)),
                     fontsize=11, fontweight="bold")
        ax.set_ylabel("equity ($)", fontsize=9)
        ax.grid(True, alpha=0.18)
        ax.margins(x=0.02)
        if use_dates:
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            fig.autofmt_xdate(rotation=30, ha="right")
        else:
            ax.set_xticks([])
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return None
