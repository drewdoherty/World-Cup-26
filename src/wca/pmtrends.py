"""Polymarket price-trajectory line charts (multi-team, faceted).

Companion to :mod:`wca.pmmovers`. Where movers rank single-window deltas, this
renders the *trajectory*: for one market (e.g. "reach the Round of 16", "win the
World Cup") it plots one line per team over a chosen time window, in two stacked
facets — raw share price (cents) on top, % change from the window start below.

Teams are ordered by soonest kickoff so the most imminent fixtures lead, and an
"exposure" variant restricts to the teams we currently hold open bets on.

The data/selection paths are pure and testable; rendering is matplotlib-optional.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from wca import pmmovers

# Distinct, high-contrast line palette (lilac-family lead, then categorical).
_LINE_COLORS = [
    "#6D4AD0", "#2563eb", "#0891b2", "#d97706", "#dc2626", "#059669",
    "#db2777", "#65a30d", "#7c3aed", "#0d9488",
]
_FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Stage -> human market name (advancement feed vocabulary).
STAGE_MARKET = {
    "R32": "Reach Round of 32", "R16": "Reach Round of 16", "QF": "Reach Quarter-Final",
    "SF": "Reach Semi-Final", "Final": "Reach Final", "win": "Win the World Cup",
    "group_winner": "Win the Group",
    # IMPORTANT: the bare "X vs Y" PM market is the FT 1X2 result (90' + stoppage,
    # a draw is possible) — NOT advancement. "To advance" (ET + penalties) is a
    # separate market; do not conflate them.
    "win_match": "Win in 90' (FT result)",
    "advance": "To Advance (incl. ET/pens)",
}


def resample_series(pts: Sequence[Tuple[datetime, float]], *, bin_minutes: Optional[float]
                    ) -> List[Tuple[datetime, float]]:
    """Down-sample a point series to one (last) point per ``bin_minutes`` bucket.

    ``bin_minutes=None`` returns the points unchanged (native cadence). The last
    observation in each fixed-width bucket represents that bucket (last-known
    price), which is the right convention for a price line.
    """
    if not bin_minutes:
        return list(pts)
    width = bin_minutes * 60.0
    buckets: Dict[int, Tuple[datetime, float]] = {}
    for dt, price in pts:
        b = int((dt - _EPOCH).total_seconds() // width)
        if b not in buckets or dt >= buckets[b][0]:
            buckets[b] = (dt, price)
    return [buckets[b] for b in sorted(buckets)]


# ---------------------------------------------------------------------------
# Context loaders (kickoffs, exposure)
# ---------------------------------------------------------------------------


def load_kickoffs(scores_path: str) -> Dict[str, datetime]:
    """``{canonical_team: earliest upcoming kickoff}`` from the scores feed."""
    import json

    from wca.data.teamnames import canonical

    out: Dict[str, datetime] = {}
    try:
        with open(scores_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return out
    for f in data.get("fixtures", []) or []:
        fx = f.get("fixture") or ""
        ko = f.get("kickoff")
        if not ko or " vs " not in fx:
            continue
        dt = pmmovers._to_dt(ko)
        if dt is None:
            continue
        for side in fx.split(" vs "):
            t = canonical(side.strip())
            if t and (t not in out or dt < out[t]):
                out[t] = dt
    return out


def exposure_teams(db_path: str, known_teams: Sequence[str]) -> set:
    """Canonical team names appearing in our OPEN bets (match_desc + selection).

    Matched by canonical-name substring against ``known_teams`` (the teams that
    actually have PM trajectories), so only plottable exposures are returned.
    """
    import sqlite3

    from wca.data.teamnames import canonical

    text = ""
    try:
        con = sqlite3.connect(db_path)
        try:
            for md, sel in con.execute(
                "SELECT match_desc, selection FROM bets WHERE lower(status)='open'"
            ):
                text += " %s %s" % ((md or ""), (sel or ""))
        finally:
            con.close()
    except Exception:
        return set()
    text_l = text.lower()
    out = set()
    for t in known_teams:
        ct = canonical(t)
        if ct and ct.lower() in text_l:
            out.add(t)
    return out


# ---------------------------------------------------------------------------
# Trajectory extraction
# ---------------------------------------------------------------------------


def trajectories(records: Sequence[Dict[str, object]], *, stage: Optional[str] = None,
                 category: Optional[str] = None, teams: Optional[Sequence[str]] = None,
                 ) -> Dict[str, List[Tuple[datetime, float]]]:
    """``{team: [(dt, pm_mid), ...]}`` for a fixed market (stage/category).

    Records may be raw or already cleaned. Series are time-sorted; only markets
    keyed to a single ``team`` are returned (one line == one team).
    """
    recs = records
    if not recs or "_cat" not in (recs[0] or {}):
        recs = pmmovers.clean_records(records)
    team_set = {str(t) for t in teams} if teams else None
    out: Dict[str, List[Tuple[datetime, float]]] = {}
    for r in recs:
        if category is not None and r.get("_cat") != category:
            continue
        if stage is not None and str(r.get("stage") or "") != stage:
            continue
        team = r.get("team")
        if not team:
            continue
        if team_set is not None and str(team) not in team_set:
            continue
        out.setdefault(str(team), []).append((r["_dt"], float(r["pm_mid"])))
    for pts in out.values():
        pts.sort(key=lambda p: p[0])
    return out


def _window_filter(series: Dict[str, List[Tuple[datetime, float]]], *,
                   anchor: datetime, hours: Optional[float]
                   ) -> Dict[str, List[Tuple[datetime, float]]]:
    """Clip each series to the window ``[anchor-hours, anchor]`` (hours=None=all)."""
    if hours is None:
        return {k: list(v) for k, v in series.items() if v}
    start = anchor - timedelta(hours=hours)
    out: Dict[str, List[Tuple[datetime, float]]] = {}
    for k, pts in series.items():
        kept = [p for p in pts if p[0] >= start]
        if kept:
            out[k] = kept
    return out


def select_teams(series: Dict[str, List[Tuple[datetime, float]]], *,
                 kickoffs: Optional[Dict[str, datetime]] = None, top_n: int = 7,
                 require_live: bool = True) -> List[str]:
    """Order teams by soonest kickoff (then latest price desc) and take ``top_n``.

    ``require_live`` drops markets already resolved to 0/100¢ (no live signal).
    """
    from wca.data.teamnames import canonical

    teams = []
    for t, pts in series.items():
        if not pts:
            continue
        last = pts[-1][1]
        if require_live and (last <= 0.005 or last >= 0.995):
            continue
        teams.append(t)
    ko = kickoffs or {}

    def key(t):
        k = ko.get(canonical(t), _FAR_FUTURE)
        return (k, -series[t][-1][1])

    teams.sort(key=key)
    return teams[:top_n]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_trend_figure(series: Dict[str, List[Tuple[datetime, float]]], *,
                        title: str, subtitle: str = "",
                        kickoffs: Optional[Dict[str, datetime]] = None,
                        order: Optional[Sequence[str]] = None) -> Optional[bytes]:
    """Two-facet line chart (raw ¢ on top, % change below) as PNG bytes.

    One line per team. ``order`` fixes the team order/selection; otherwise all
    series are drawn. Returns ``None`` if matplotlib is unavailable or there is
    nothing to plot.
    """
    try:
        teams = list(order) if order is not None else list(series.keys())
        teams = [t for t in teams if series.get(t)]
        if not teams:
            return None
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from wca.data.teamnames import canonical

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(11.0, 7.0), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
        ko = kickoffs or {}
        for i, t in enumerate(teams):
            pts = series[t]
            xs = [p[0].replace(tzinfo=None) for p in pts]
            ys = [p[1] * 100.0 for p in pts]
            color = _LINE_COLORS[i % len(_LINE_COLORS)]
            kdt = ko.get(canonical(t))
            lbl = "%s · %.0f¢" % (t, ys[-1])
            if kdt is not None:
                lbl += " · KO %s" % kdt.strftime("%b %d %H:%M")
            ax1.plot(xs, ys, marker="o", ms=3.2, lw=1.8, color=color, label=lbl, zorder=3)
            base = ys[0]
            if base and base > 0:
                pct = [(y / base - 1.0) * 100.0 for y in ys]
                ax2.plot(xs, pct, marker="o", ms=3.2, lw=1.8, color=color, zorder=3)

        ax1.set_ylabel("share price (¢)")
        ax1.set_title("%s\n%s" % (title, subtitle) if subtitle else title,
                      fontsize=12, weight="bold")
        ax1.grid(color="#e5e7eb", lw=0.6, zorder=0)
        # Legend to the right, outside the axes, so it never overlaps the lines.
        ax1.legend(fontsize=8, loc="center left", bbox_to_anchor=(1.01, 0.5),
                   framealpha=0.9, borderaxespad=0.0, title="team · now · kickoff",
                   title_fontsize=8)
        ax2.set_ylabel("% change from\nwindow start")
        ax2.axhline(0, color="#9ca3af", lw=0.9, zorder=2)
        ax2.grid(color="#e5e7eb", lw=0.6, zorder=0)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
        for ax in (ax1, ax2):
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# High-level builders
# ---------------------------------------------------------------------------

# Default look-back periods: (label, hours, bin_minutes). hours=None -> all;
# bin_minutes=None -> native cadence.
DEFAULT_PERIODS: Tuple[Tuple[str, Optional[float], Optional[float]], ...] = (
    ("Last 24h · 30-min", 24.0, 30.0),
    ("Last week · hourly", 168.0, 60.0),
    ("Full history · native", None, None),
)


def _norm_period(p):
    """Accept (label, hours) or (label, hours, bin_minutes)."""
    if len(p) >= 3:
        return p[0], p[1], p[2]
    return p[0], p[1], None


def build_market_figures(records: Sequence[Dict[str, object]], *, stage: str,
                         category: Optional[str] = None,
                         periods: Sequence[tuple] = DEFAULT_PERIODS,
                         kickoffs: Optional[Dict[str, datetime]] = None,
                         teams: Optional[Sequence[str]] = None,
                         top_n: int = 7, require_live: bool = True,
                         scope_label: str = "") -> List[Dict[str, object]]:
    """One faceted line figure per period for a single market (stage).

    Each period is ``(label, hours, bin_minutes)``: the series is clipped to the
    window then down-sampled to the bin (30-min, hourly, …) before plotting.
    Returns ``[{stage, market, period, png, teams}]``. ``teams`` restricts to a
    set (e.g. our exposure); otherwise the soonest-kickoff top ``top_n`` are used.
    """
    recs = pmmovers.clean_records(records) if (not records or "_cat" not in (records[0] or {})) else list(records)
    base = trajectories(recs, stage=stage, category=category, teams=teams)
    anchor = pmmovers.anchor_time(recs)
    market = STAGE_MARKET.get(stage, stage)
    out: List[Dict[str, object]] = []
    if anchor is None or not base:
        return out
    for period in periods:
        plabel, hours, bin_min = _norm_period(period)
        win = _window_filter(base, anchor=anchor, hours=hours)
        if bin_min:
            win = {k: resample_series(v, bin_minutes=bin_min) for k, v in win.items()}
            win = {k: v for k, v in win.items() if v}
        order = select_teams(win, kickoffs=kickoffs, top_n=top_n, require_live=require_live)
        if not order:
            continue
        scope = (" · %s" % scope_label) if scope_label else ""
        subtitle = "%s%s · as of %s" % (plabel, scope, anchor.strftime("%Y-%m-%d %H:%M UTC"))
        png = render_trend_figure(win, title="%s — Polymarket trajectory" % market,
                                  subtitle=subtitle, kickoffs=kickoffs, order=order)
        out.append({"stage": stage, "market": market, "period": plabel,
                    "png": png, "teams": order})
    return out
