"""Top price-movers across Polymarket WC26 market categories.

Reads the Polymarket price-history trajectory captured by :mod:`wca.pmhistory`
(the versioned JSONL dataset or the ``pm_snapshots`` table), buckets each market
into one of three categories —

* ``prop``        — single-match player / exact-score props,
* ``futures``     — tournament outrights (champion, golden boot, group winner),
* ``advancement`` — knockout progression (reach R16/QF/SF/Final),

— and ranks the biggest *share-price* moves over several look-back windows.

A Polymarket share trades in cents that equal the implied probability, so a move
of ``+0.08`` in ``pm_mid`` is an **+8¢** (== +8 percentage-point) move; that cent
delta is the primary mover metric (percentage change is reported too but is noisy
on longshots).

The compute path is pure and network-free so it is fully unit-testable; chart
rendering is matplotlib-optional and degrades to a text summary when matplotlib
is unavailable.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

# Category identity + display.
CATEGORIES: Tuple[str, str, str] = ("prop", "futures", "advancement")
CATEGORY_LABELS = {
    "prop": "Player / Exact-Score Props",
    "futures": "Tournament Futures",
    "advancement": "Advancement / Knockout",
}
# Stage -> category for the advancement feed's stage vocabulary.
_FUTURES_STAGES = {"win", "winner", "group_winner", "golden_boot", "top_scorer"}
_ADV_STAGES = {"R32", "R16", "QF", "SF", "Final"}

# Bar palette per window (consistent across the three category charts).
_WINDOW_COLORS = ["#6D4AD0", "#2563eb", "#0891b2", "#d97706"]


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------


def categorize(rec: Dict[str, object]) -> Optional[str]:
    """Bucket one snapshot record into ``prop`` / ``futures`` / ``advancement``.

    Resolution order: an explicit ``kind`` wins, then the advancement ``stage``
    vocabulary, then free-text on the market slug / title (so the richer universe
    captured by :func:`wca.data.polymarket.find_world_cup_markets` — which carries
    no ``stage`` — still buckets correctly). Returns ``None`` when nothing matches.
    """
    kind = str(rec.get("kind") or "").strip().lower()
    if kind in ("prop", "player_prop", "exact_score", "scorer", "goalscorer", "booking", "cards", "shots"):
        return "prop"
    if kind in ("futures", "outright", "winner", "golden_boot", "top_scorer", "group_winner"):
        return "futures"
    # Archive backfill: match-winner (1X2) of a knockout tie == "advance"; the
    # stage-of-elimination ladder is also advancement.
    if kind == "match":
        return "advancement"

    stage = str(rec.get("stage") or "").strip()
    if stage in _FUTURES_STAGES:
        return "futures"
    if stage in _ADV_STAGES or stage == "win_match" or stage.startswith("elim:"):
        return "advancement"

    text = " ".join(str(rec.get(f) or "") for f in ("market_slug", "title", "event_title", "question")).lower()
    if any(k in text for k in (
        "player prop", "exact score", "exact-score", "anytime", "to score",
        "first goal", "shots on target", "booking", " card", "assist", "corner",
    )):
        return "prop"
    if any(k in text for k in (
        "win the world cup", "to win outright", "champion", "winner",
        "golden boot", "top scorer", "top goalscorer", "group winner",
    )):
        return "futures"
    if any(k in text for k in ("to reach", "advance", "qualify for", "make the")):
        return "advancement"
    # The advancement feed's records are all kind='advancement' with a knockout
    # stage; an unrecognised one defaults to advancement rather than dropping it.
    if kind == "advancement":
        return "advancement"
    return None


# ---------------------------------------------------------------------------
# Record cleaning / keys / labels
# ---------------------------------------------------------------------------


def _to_dt(ts: object) -> Optional[datetime]:
    """Parse the two timestamp shapes the store emits, as tz-aware UTC.

    Handles ``'2026-06-29 07:15 UTC'`` (JSONL) and ISO ``'2026-06-29T07:15:00Z'``.
    """
    s = str(ts or "").strip()
    if not s:
        return None
    s2 = s.replace(" UTC", "").replace("Z", "+00:00").replace(" ", "T", 1) if " UTC" in s else s.replace("Z", "+00:00")
    for cand in (s2, s2 + ":00"):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _market_key(rec: Dict[str, object]) -> str:
    """Stable identity of a priced market across snapshots."""
    slug = rec.get("market_slug")
    if slug:
        return str(slug)
    return "|".join(str(rec.get(k, "")) for k in ("kind", "team", "stage"))


def market_label(rec: Dict[str, object]) -> str:
    """Short human label for a market (chart row / table)."""
    team = str(rec.get("team") or "").strip()
    stage = str(rec.get("stage") or "").strip()
    if team and stage:
        pretty = {"win": "Champion", "group_winner": "Group Winner"}.get(stage, stage)
        return "%s · %s" % (team, pretty)
    if team:
        # Full-universe rows pack the whole human label into ``team`` (no stage).
        return team
    for f in ("market_slug", "title", "event_title", "question"):
        v = rec.get(f)
        if v:
            return str(v)
    return _market_key(rec)


def clean_records(records: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    """Keep records with a valid timestamp and a probability ``pm_mid`` in (0,1).

    Returns shallow copies annotated with ``_dt`` (datetime) and ``_cat``
    (category), sorted by time. Records that fail to parse are dropped.
    """
    out: List[Dict[str, object]] = []
    seen: set = set()  # (market_key, ts) — dedup overlap when JSONL + DB are merged
    for r in records:
        dt = _to_dt(r.get("ts_utc"))
        if dt is None:
            continue
        try:
            mid = float(r.get("pm_mid"))
        except (TypeError, ValueError):
            continue
        if not (0.0 <= mid <= 1.0):
            continue
        cat = categorize(r)
        if cat is None:
            continue
        dedup = (_market_key(r), str(r.get("ts_utc")))
        if dedup in seen:
            continue
        seen.add(dedup)
        rr = dict(r)
        rr["_dt"] = dt
        rr["pm_mid"] = mid
        rr["_cat"] = cat
        out.append(rr)
    out.sort(key=lambda x: x["_dt"])
    return out


def _group_by_market(records: Sequence[Dict[str, object]]) -> Dict[str, List[Dict[str, object]]]:
    groups: Dict[str, List[Dict[str, object]]] = {}
    for r in records:
        groups.setdefault(_market_key(r), []).append(r)
    for snaps in groups.values():
        snaps.sort(key=lambda x: x["_dt"])
    return groups


def _nearest_at_or_before(snaps: Sequence[Dict[str, object]], target: datetime) -> Optional[Dict[str, object]]:
    """The latest snapshot whose timestamp is <= ``target`` (None if none)."""
    chosen = None
    for s in snaps:
        if s["_dt"] <= target:
            chosen = s
        else:
            break
    return chosen


# ---------------------------------------------------------------------------
# Window selection + mover computation
# ---------------------------------------------------------------------------

Window = Tuple[str, Optional[float]]  # (label, hours)  hours=None -> earliest snapshot


def anchor_time(records: Sequence[Dict[str, object]]) -> Optional[datetime]:
    """The most recent snapshot time across all records ('now' for windows)."""
    dts = [r["_dt"] for r in records if r.get("_dt") is not None]
    return max(dts) if dts else None


def default_windows(records: Sequence[Dict[str, object]]) -> List[Window]:
    """Pick three honest look-back windows from the data's actual span."""
    dts = [r["_dt"] for r in records if r.get("_dt") is not None]
    if len(dts) < 2:
        return [("all", None)]
    span_h = (max(dts) - min(dts)).total_seconds() / 3600.0
    if span_h >= 24:
        return [("6h", 6.0), ("24h", 24.0), ("all", None)]
    if span_h >= 12:
        return [("3h", 3.0), ("12h", 12.0), ("all", None)]
    if span_h >= 6:
        return [("3h", 3.0), ("6h", 6.0), ("all", None)]
    if span_h >= 2:
        return [("1h", 1.0), (("%.0fh" % span_h), span_h), ("all", None)]
    return [("all", None)]


def _mover_for_window(snaps: Sequence[Dict[str, object]], now: datetime, hours: Optional[float]
                      ) -> Optional[Dict[str, object]]:
    """Entry-vs-latest move for one market over one window, or None if N/A."""
    if len(snaps) < 2:
        return None
    latest = snaps[-1]
    if hours is None:
        then = snaps[0]
    else:
        then = _nearest_at_or_before(snaps, now - timedelta(hours=hours))
    if then is None or then["_dt"] >= latest["_dt"]:
        return None
    then_pm = float(then["pm_mid"])
    latest_pm = float(latest["pm_mid"])
    delta = latest_pm - then_pm
    actual_h = (latest["_dt"] - then["_dt"]).total_seconds() / 3600.0
    return {
        "key": _market_key(latest),
        "label": market_label(latest),
        "team": latest.get("team"),
        "stage": latest.get("stage"),
        "then_pm": then_pm,
        "latest_pm": latest_pm,
        "delta_pp": delta * 100.0,            # cents == probability points
        "pct": (delta / then_pm * 100.0) if then_pm > 0 else None,
        "actual_hours": actual_h,
        "n_snaps": len(snaps),
    }


def compute_movers(records: Sequence[Dict[str, object]], *, category: str,
                   windows: Optional[Sequence[Window]] = None,
                   anchor_ts: Optional[datetime] = None, top_n: int = 8,
                   ) -> Dict[str, List[Dict[str, object]]]:
    """Top ``top_n`` movers (by |Δ cents|) per window for one category.

    Returns ``{window_label: [mover, ...]}`` with movers sorted by absolute
    share-price move descending.
    """
    recs = [r for r in records if r.get("_cat") == category]
    if windows is None:
        windows = default_windows(recs) or default_windows(records)
    now = anchor_ts or anchor_time(recs) or anchor_time(records)
    out: Dict[str, List[Dict[str, object]]] = {}
    if now is None:
        return {label: [] for label, _ in windows}
    groups = _group_by_market(recs)
    for label, hours in windows:
        movers = []
        for snaps in groups.values():
            m = _mover_for_window(snaps, now, hours)
            if m is not None:
                m = dict(m)
                m["window"] = label
                movers.append(m)
        movers.sort(key=lambda x: abs(x["delta_pp"]), reverse=True)
        out[label] = movers[:top_n]
    return out


def window_matrix(records: Sequence[Dict[str, object]], *, category: str,
                  windows: Sequence[Window], anchor_ts: Optional[datetime] = None,
                  top_n: int = 8) -> List[Dict[str, object]]:
    """Per-market Δ across every window, for the grouped-bar chart.

    Returns up to ``top_n`` markets (those with the largest single-window move),
    each ``{key, label, latest_pm, deltas:{window:pp|None}, max_abs}``, ordered by
    ``max_abs`` ascending (so the biggest mover plots at the top of a barh chart).
    """
    recs = [r for r in records if r.get("_cat") == category]
    now = anchor_ts or anchor_time(recs) or anchor_time(records)
    groups = _group_by_market(recs)
    rows: List[Dict[str, object]] = []
    if now is None:
        return rows
    for snaps in groups.values():
        deltas: Dict[str, Optional[float]] = {}
        latest_pm = float(snaps[-1]["pm_mid"]) if snaps else None
        present = False
        for label, hours in windows:
            m = _mover_for_window(snaps, now, hours)
            if m is None:
                deltas[label] = None
            else:
                deltas[label] = m["delta_pp"]
                present = True
        if not present:
            continue
        max_abs = max(abs(v) for v in deltas.values() if v is not None)
        rows.append({
            "key": _market_key(snaps[-1]),
            "label": market_label(snaps[-1]),
            "latest_pm": latest_pm,
            "deltas": deltas,
            "max_abs": max_abs,
        })
    rows.sort(key=lambda r: r["max_abs"], reverse=True)
    rows = rows[:top_n]
    rows.sort(key=lambda r: r["max_abs"])  # ascending for barh (top = biggest)
    return rows


# ---------------------------------------------------------------------------
# Text summary (chart fallback / CLI)
# ---------------------------------------------------------------------------


def _fmt_mover(m: Dict[str, object]) -> str:
    arrow = "▲" if m["delta_pp"] >= 0 else "▼"
    pct = ("  (%+.0f%%)" % m["pct"]) if m.get("pct") is not None else ""
    return "  %s %-22s %4.0f¢ → %3.0f¢  %s%+.1f pp%s" % (
        arrow, str(m["label"])[:22], m["then_pm"] * 100, m["latest_pm"] * 100,
        arrow, m["delta_pp"], pct,
    )


def text_summary(records: Sequence[Dict[str, object]], *,
                 windows: Optional[Sequence[Window]] = None, top_n: int = 5) -> str:
    """Plain-text movers digest for all three categories (chart fallback)."""
    recs = clean_records(records) if (records and "_cat" not in (records[0] or {})) else list(records)
    if windows is None:
        windows = default_windows(recs)
    now = anchor_time(recs)
    lines: List[str] = ["*Polymarket top movers*"]
    if now is not None:
        lines.append("_as of %s · windows %s_" % (
            now.strftime("%Y-%m-%d %H:%M UTC"), ", ".join(l for l, _ in windows)))
    for cat in CATEGORIES:
        lines.append("\n*%s*" % CATEGORY_LABELS[cat])
        per = compute_movers(recs, category=cat, windows=windows, top_n=top_n)
        # Show the widest informative window that has movers.
        shown = None
        for label, _ in reversed(list(windows)):
            if per.get(label):
                shown = label
                break
        if shown is None:
            lines.append("  _no price history captured yet (COLLECTING)_")
            continue
        lines.append("  _window: %s_" % shown)
        for m in per[shown]:
            lines.append(_fmt_mover(m))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Charts (matplotlib-optional)
# ---------------------------------------------------------------------------


def _new_figure(figsize):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt, plt.subplots(figsize=figsize)


def render_category_chart(records: Sequence[Dict[str, object]], *, category: str,
                          windows: Sequence[Window], anchor_ts: Optional[datetime] = None,
                          top_n: int = 8) -> Optional[bytes]:
    """Grouped horizontal bar chart of top movers for one category, as PNG bytes.

    One bar group per market (top ``top_n`` by largest single-window move), one
    bar per look-back window. Returns ``None`` if matplotlib is unavailable;
    renders an honest placeholder when the category has no captured history.
    """
    try:
        rows = window_matrix(records, category=category, windows=windows,
                             anchor_ts=anchor_ts, top_n=top_n)
        title = "%s — Polymarket share-price movers" % CATEGORY_LABELS[category]
        if not rows:
            plt, (fig, ax) = _new_figure((8.0, 3.0))
            ax.axis("off")
            ax.text(0.5, 0.62, title, ha="center", va="center", fontsize=12, weight="bold")
            ax.text(0.5, 0.36,
                    "No price history captured yet — COLLECTING.\n"
                    "Movers populate once ≥ 2 snapshots exist for this category.",
                    ha="center", va="center", fontsize=10, color="#6b7280")
            buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=130, bbox_inches="tight"); plt.close(fig)
            return buf.getvalue()

        labels = ["%s  (%.0f¢)" % (str(r["label"])[:30], (r["latest_pm"] or 0) * 100) for r in rows]
        n_rows = len(rows)
        wlabels = [w[0] for w in windows]
        n_win = len(wlabels)
        # Scale label offsets / axis padding to the data so labels never collide
        # with the y-axis text or run off the right edge (futures move <1¢; a
        # knockout resolving moves ~25¢ — both must read cleanly).
        peak = max((abs(v) for r in rows for v in r["deltas"].values() if v is not None), default=1.0)
        peak = max(peak, 0.5)
        lab_off = 0.018 * peak
        lab_floor = max(0.05, 0.012 * peak)
        plt, (fig, ax) = _new_figure((8.6, max(2.6, 0.62 * n_rows + 1.0)))
        group_h = 0.8
        bar_h = group_h / max(1, n_win)
        ys = list(range(n_rows))
        for wi, wl in enumerate(wlabels):
            offs = [y + (wi - (n_win - 1) / 2.0) * bar_h for y in ys]
            vals = [(r["deltas"].get(wl) or 0.0) for r in rows]
            ax.barh(offs, vals, height=bar_h * 0.92,
                    color=_WINDOW_COLORS[wi % len(_WINDOW_COLORS)], label=wl, zorder=3)
            for yo, v, r in zip(offs, vals, rows):
                if r["deltas"].get(wl) is None or abs(v) < lab_floor:
                    continue
                ax.text(v + (lab_off if v >= 0 else -lab_off), yo, "%+.1f" % v,
                        va="center", ha="left" if v >= 0 else "right", fontsize=7,
                        color="#374151", zorder=4)
        ax.set_xlim(-1.28 * peak, 1.28 * peak)
        ax.axvline(0, color="#9ca3af", lw=0.8, zorder=2)
        ax.set_yticks(ys)
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel("Δ share price (cents / probability points)  ·  ▶ up   ◀ down")
        ax.set_title(title, fontsize=12, weight="bold")
        ax.legend(title="window", fontsize=8, title_fontsize=8, loc="lower right", framealpha=0.9)
        ax.grid(axis="x", color="#e5e7eb", lw=0.6, zorder=0)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        fig.tight_layout()
        buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=130, bbox_inches="tight"); plt.close(fig)
        return buf.getvalue()
    except Exception:
        try:
            import matplotlib.pyplot as plt  # noqa
            plt.close("all")
        except Exception:
            pass
        return None


def build_charts(records: Sequence[Dict[str, object]], *,
                 windows: Optional[Sequence[Window]] = None,
                 anchor_ts: Optional[datetime] = None, top_n: int = 8,
                 ) -> List[Dict[str, object]]:
    """The three category charts (the insightful set), one per category.

    Returns ``[{category, label, caption, png(bytes|None), n_markets, top_window_movers}]``
    in a fixed prop/futures/advancement order.
    """
    recs = clean_records(records) if (not records or "_cat" not in (records[0] or {})) else list(records)
    if windows is None:
        windows = default_windows(recs)
    now = anchor_ts or anchor_time(recs)
    asof = now.strftime("%Y-%m-%d %H:%M UTC") if now else "n/a"
    out: List[Dict[str, object]] = []
    for cat in CATEGORIES:
        cat_recs = [r for r in recs if r.get("_cat") == cat]
        n_markets = len({_market_key(r) for r in cat_recs})
        per = compute_movers(recs, category=cat, windows=windows, anchor_ts=now, top_n=top_n)
        top_movers: List[Dict[str, object]] = []
        for label, _ in reversed(list(windows)):
            if per.get(label):
                top_movers = per[label]
                break
        png = render_category_chart(recs, category=cat, windows=windows, anchor_ts=now, top_n=top_n)
        caption = "%s — biggest PM share-price moves (as of %s)" % (CATEGORY_LABELS[cat], asof)
        if not cat_recs:
            caption = "%s — COLLECTING (no PM price history captured yet)" % CATEGORY_LABELS[cat]
        out.append({
            "category": cat, "label": CATEGORY_LABELS[cat], "caption": caption,
            "png": png, "n_markets": n_markets, "top_window_movers": top_movers,
        })
    return out
