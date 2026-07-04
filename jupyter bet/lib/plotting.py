"""Matplotlib chart helpers — consistent style, always saved to outputs/charts.

Seaborn is NOT used (not in the repo venv — spec: optional, only if already
present). Charts are publication-lean: title, labelled axes, source note
with n, UTC timestamps.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless-safe; notebooks still render via inline hooks
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import lib.bootstrap as bt

plt.rcParams.update({
    "figure.figsize": (10, 5), "figure.dpi": 110,
    "axes.grid": True, "grid.alpha": 0.25,
    "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 10,
})


def save_fig(fig, name: str, *, note: str = "") -> Path:
    """Save to outputs/charts/<name>.png with an optional source footnote."""
    if note:
        fig.text(0.01, 0.005, note, fontsize=7, color="#555")
    path = bt.CHART_DIR / f"{name}.png"
    fig.savefig(path, bbox_inches="tight")
    return path


def ts_axis(ax) -> None:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(30)
        lbl.set_ha("right")


def save_table(df, name: str, *, index: bool = False) -> Path:
    """Persist a result table (pandas or polars) to outputs/tables as CSV."""
    path = bt.TABLE_DIR / f"{name}.csv"
    if hasattr(df, "write_csv"):          # polars
        df.write_csv(path)
    else:                                  # pandas
        df.to_csv(path, index=index)
    return path
