"""Static HTML dashboard for the World Cup Alpha bet ledger.

This module renders a single, fully self-contained HTML5 page summarising the
state of the bet ledger (``data/wca.db``).  The page is designed to be dropped
straight onto GitHub Pages: it has no external assets, no CDN links, and no
network requests of any kind — all CSS, JavaScript (none is actually needed),
and chart graphics are inlined, with charts drawn as inline SVG.

Design notes
------------
* **Pure functions.** :func:`gather_stats` and :func:`render_html` never read
  the wall clock.  The caller (the CLI) is responsible for stamping the
  generation time and passing it in as ``now_utc``.  This keeps rendering
  deterministic and trivially testable.
* **Venue rollup.** Individual platforms are collapsed into three *venues* for
  the headline by-venue chart, per the project convention:

  =========================================  ==========
  platform                                   venue
  =========================================  ==========
  polymarket                                 polymarket
  kalshi                                     kalshi
  everything else (virginbet, paddypower,    sportsbook
  bet365, betfair_*, skybet, williamhill,
  unknown, ...)
  =========================================  ==========

* **Money convention.** ``wagered`` is the gross stake placed across *all*
  bets (open + settled + void).  ``open_stake`` is the stake still at risk
  (status ``open``).  ``settled_pl`` is the realised profit/loss from settled
  bets (won/lost); void bets contribute zero P&L.

* **Safety.** Every string sourced from the database (match descriptions,
  selections, platform names, ...) is passed through :func:`html.escape`
  before being placed in the page, so a hostile ``match_desc`` cannot inject
  markup or script.
"""

from __future__ import annotations

import html
import os
import sqlite3
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Venue mapping.
# ---------------------------------------------------------------------------

# Canonical venue ordering for the by-venue chart / table.
VENUES = ("sportsbook", "polymarket", "kalshi")

# Platforms that map to their own dedicated venue.  Everything not listed here
# folds into "sportsbook".
_DEDICATED_VENUES = {
    "polymarket": "polymarket",
    "kalshi": "kalshi",
}


def venue_for_platform(platform: Optional[str]) -> str:
    """Map a raw platform string to one of the three rollup venues.

    ``polymarket`` -> ``polymarket``; ``kalshi`` -> ``kalshi``; everything
    else (sportsbooks and exchanges such as ``virginbet``, ``paddypower``,
    ``bet365``, ``betfair_ex``, ``skybet``, ``williamhill``, ``unknown`` and
    ``None``) -> ``sportsbook``.  Matching is case-insensitive and tolerant of
    surrounding whitespace.
    """
    key = (platform or "").strip().lower()
    return _DEDICATED_VENUES.get(key, "sportsbook")


# ---------------------------------------------------------------------------
# Stats gathering.
# ---------------------------------------------------------------------------

# Columns we read out of the bets table, in a stable order.
_BET_COLUMNS = (
    "id", "ts_utc", "match_id", "match_desc", "market", "selection",
    "platform", "decimal_odds", "stake", "model_prob", "market_prob_devig",
    "ev", "kelly_fraction", "status", "settled_pl", "settled_ts",
    "closing_odds", "clv", "notes", "account", "source", "manual_override",
)


def _empty_venue_block() -> Dict[str, float]:
    return {"wagered": 0.0, "open_stake": 0.0, "settled_pl": 0.0, "n_bets": 0}


def _read_bets(db_path: str) -> List[Dict[str, Any]]:
    """Read every bet row as a list of plain dicts, newest-first.

    Returns an empty list (rather than raising) when the database file does
    not exist or the ``bets`` table is absent.  We deliberately open the
    database read-only and never create tables here: ``gather_stats`` must be a
    side-effect-free read of whatever already exists.
    """
    if not db_path or not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        # Is there a bets table at all?
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bets'"
        ).fetchone()
        if has_table is None:
            return []
        rows = conn.execute(
            "SELECT * FROM bets ORDER BY id DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    bets: List[Dict[str, Any]] = []
    for r in rows:
        keys = r.keys()
        bets.append({col: (r[col] if col in keys else None) for col in _BET_COLUMNS})
    return bets


def _to_float(value: Any) -> float:
    """Coerce a possibly-None / possibly-string numeric DB value to float."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def gather_stats(db_path: str) -> Dict[str, Any]:
    """Read the ledger and return a dashboard-ready statistics dict.

    Parameters
    ----------
    db_path:
        Path to the SQLite ledger file.  A missing file (or a file without a
        ``bets`` table) is handled gracefully and yields all-zero rollups and
        empty lists.

    Returns
    -------
    dict with keys
        ``by_venue``
            ``{venue: {wagered, open_stake, settled_pl, n_bets}}`` for each of
            the three canonical venues (always present, zeroed if unused).
        ``totals``
            Same four keys, summed across all venues.
        ``bets``
            List of bet row dicts, newest-first.
        ``clv``
            ``{avg_clv, pct_beat_close, n_with_close}`` over bets that have a
            recorded ``closing_odds``.  ``avg_clv`` / ``pct_beat_close`` are
            ``None`` when no bet has a closing line yet.
        ``generated_inputs_ok``
            ``True`` when the database existed and was read successfully (even
            if empty); ``False`` when no readable ledger was found.
    """
    inputs_ok = bool(db_path) and os.path.exists(db_path)
    bets = _read_bets(db_path)

    by_venue: Dict[str, Dict[str, float]] = {v: _empty_venue_block() for v in VENUES}
    totals = _empty_venue_block()

    clv_sum = 0.0
    clv_beat = 0
    clv_n = 0

    for bet in bets:
        venue = venue_for_platform(bet.get("platform"))
        block = by_venue[venue]

        stake = _to_float(bet.get("stake"))
        status = (bet.get("status") or "").strip().lower()
        source = str(bet.get("source") or "model")
        # Free bets (source='offer') are stake-not-returned: the stake is never
        # at risk, so it contributes £0 to open exposure (only the potential
        # profit is at stake, and that is upside, not downside).
        exposed = 0.0 if source == "offer" else stake

        block["wagered"] += stake
        block["n_bets"] += 1
        totals["wagered"] += stake
        totals["n_bets"] += 1

        if status == "open":
            block["open_stake"] += exposed
            totals["open_stake"] += exposed

        if status in ("won", "lost", "cashed"):
            pl = _to_float(bet.get("settled_pl"))
            block["settled_pl"] += pl
            totals["settled_pl"] += pl

        closing = bet.get("closing_odds")
        if closing is not None:
            clv_val = _to_float(bet.get("clv"))
            clv_sum += clv_val
            clv_n += 1
            if clv_val > 0:
                clv_beat += 1

    if clv_n > 0:
        clv = {
            "avg_clv": clv_sum / clv_n,
            "pct_beat_close": clv_beat / clv_n,
            "n_with_close": clv_n,
        }
    else:
        clv = {"avg_clv": None, "pct_beat_close": None, "n_with_close": 0}

    # Per-currency rollup — GBP (sportsbook) and USD (polymarket/kalshi) must
    # NEVER be summed. Consumers should prefer this over the legacy ``totals``
    # (which sums across currencies and is kept only for backward-compat).
    _VENUE_CCY = {"sportsbook": "GBP", "polymarket": "USD", "kalshi": "USD"}
    totals_by_currency: Dict[str, Dict[str, float]] = {}
    for v in VENUES:
        ccy = _VENUE_CCY.get(v, "GBP")
        agg = totals_by_currency.setdefault(
            ccy, {"wagered": 0.0, "open_stake": 0.0, "settled_pl": 0.0, "n_bets": 0}
        )
        for k in ("wagered", "open_stake", "settled_pl", "n_bets"):
            agg[k] += by_venue[v][k]

    return {
        "by_venue": by_venue,
        "totals": totals,
        "totals_by_currency": totals_by_currency,
        "bets": bets,
        "clv": clv,
        "generated_inputs_ok": inputs_ok,
    }


# ---------------------------------------------------------------------------
# HTML rendering.
# ---------------------------------------------------------------------------


def _fmt_money(value: Any) -> str:
    """Format a number as a currency-style string, e.g. ``£1,234.56``."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    sign = "-" if num < 0 else ""
    return "%s£%s" % (sign, "{:,.2f}".format(abs(num)))


def _fmt_signed_money(value: Any) -> str:
    """Format P&L with an explicit ``+`` for non-negative values."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    sign = "-" if num < 0 else "+"
    return "%s£%s" % (sign, "{:,.2f}".format(abs(num)))


def _esc(value: Any) -> str:
    """HTML-escape any value, coercing None to an empty string."""
    if value is None:
        return ""
    return html.escape(str(value))


def _venue_bar_chart(by_venue: Dict[str, Dict[str, float]]) -> str:
    """Render an inline-SVG horizontal bar chart of wagered-by-venue.

    One bar per canonical venue.  Bars are scaled to the maximum wagered
    amount; each bar is labelled with the venue name and the wagered amount.
    All venues are shown even when their wagered total is zero (a zero-width
    bar plus a £0.00 label), so the chart shape is stable.
    """
    amounts = {v: float(by_venue.get(v, {}).get("wagered", 0.0)) for v in VENUES}
    max_amt = max(amounts.values()) if amounts else 0.0

    # Geometry.
    row_h = 46          # vertical pitch per bar
    bar_h = 26          # bar thickness
    top_pad = 16
    label_w = 110       # left gutter for venue names
    track_x = label_w + 10
    track_w = 430       # full-scale bar width
    width = track_x + track_w + 110  # room for value label on the right
    height = top_pad * 2 + row_h * len(VENUES)

    parts: List[str] = []
    parts.append(
        '<svg viewBox="0 0 %d %d" width="100%%" '
        'preserveAspectRatio="xMinYMin meet" '
        'role="img" aria-label="Wagered by venue" '
        'xmlns="http://www.w3.org/2000/svg" class="venue-chart">' % (width, height)
    )

    for i, venue in enumerate(VENUES):
        amt = amounts[venue]
        y = top_pad + i * row_h
        bar_y = y + (row_h - bar_h) / 2.0
        frac = (amt / max_amt) if max_amt > 0 else 0.0
        w = track_w * frac

        # Background track.
        parts.append(
            '<rect x="%d" y="%.1f" width="%d" height="%d" rx="4" '
            'class="bar-track" />' % (track_x, bar_y, track_w, bar_h)
        )
        # Value bar (only draw a visible rect when there is something to show).
        if w > 0:
            parts.append(
                '<rect x="%d" y="%.1f" width="%.1f" height="%d" rx="4" '
                'class="bar bar-%s" />' % (track_x, bar_y, w, bar_h, _esc(venue))
            )
        # Venue label (left).
        parts.append(
            '<text x="%d" y="%.1f" class="bar-label" '
            'text-anchor="end" dominant-baseline="middle">%s</text>'
            % (label_w, y + row_h / 2.0, _esc(venue))
        )
        # Amount label (right of the bar).
        parts.append(
            '<text x="%.1f" y="%.1f" class="bar-value" '
            'dominant-baseline="middle">%s</text>'
            % (track_x + max(w, 0.0) + 8, y + row_h / 2.0, _esc(_fmt_money(amt)))
        )

    parts.append("</svg>")
    return "".join(parts)


def _tile(label: str, value: str, *, cls: str = "") -> str:
    """Render a single headline tile."""
    extra = (" " + cls) if cls else ""
    return (
        '<div class="tile%s">'
        '<div class="tile-value">%s</div>'
        '<div class="tile-label">%s</div>'
        "</div>" % (extra, value, _esc(label))
    )


def _open_bets_rows(bets: List[Dict[str, Any]]) -> str:
    """Render ``<tr>`` rows for every open bet (newest-first order preserved)."""
    open_bets = [b for b in bets if (b.get("status") or "").strip().lower() == "open"]
    if not open_bets:
        return (
            '<tr><td colspan="6" class="empty-row">'
            "No open bets.</td></tr>"
        )

    rows: List[str] = []
    for b in open_bets:
        odds = b.get("decimal_odds")
        try:
            odds_str = "{:.2f}".format(float(odds))
        except (TypeError, ValueError):
            odds_str = _esc(odds)
        rows.append(
            "<tr>"
            "<td>%s</td>"
            "<td>%s</td>"
            '<td class="num">%s</td>'
            '<td class="num">%s</td>'
            "<td>%s</td>"
            '<td><span class="status status-%s">%s</span></td>'
            "</tr>"
            % (
                _esc(b.get("match_desc")),
                _esc(b.get("selection")),
                _esc(odds_str),
                _esc(_fmt_money(b.get("stake"))),
                _esc(b.get("platform")),
                _esc((b.get("status") or "").strip().lower()),
                _esc(b.get("status")),
            )
        )
    return "".join(rows)


def _clv_tile(clv: Dict[str, Any]) -> str:
    """Render the CLV tile, showing a clean ``N/A`` before any closing lines."""
    n = int(clv.get("n_with_close") or 0)
    if n == 0 or clv.get("avg_clv") is None:
        value = "N/A"
        sub = "no closing lines yet"
    else:
        avg = float(clv["avg_clv"])
        pct = float(clv.get("pct_beat_close") or 0.0)
        value = "{:+.2%}".format(avg)
        sub = "{:.0%} beat close · n={}".format(pct, n)
    return (
        '<div class="tile clv-tile">'
        '<div class="tile-value">%s</div>'
        '<div class="tile-label">Avg CLV</div>'
        '<div class="tile-sub">%s</div>'
        "</div>" % (_esc(value), _esc(sub))
    )


_STYLE = """
:root {
  --bg: #0b0f17;
  --panel: #151c28;
  --panel-2: #1c2535;
  --border: #27313f;
  --text: #e6edf3;
  --muted: #8b97a7;
  --accent: #4fd1c5;
  --pos: #46d18a;
  --neg: #f2738c;
  --sportsbook: #4f8ff7;
  --polymarket: #9d7bff;
  --kalshi: #4fd1c5;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    Helvetica, Arial, sans-serif;
  line-height: 1.45;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 28px 20px 60px; }
header.app { display: flex; flex-wrap: wrap; align-items: baseline;
  justify-content: space-between; gap: 8px; margin-bottom: 6px; }
header.app h1 { font-size: 1.7rem; margin: 0; letter-spacing: 0.3px; }
header.app h1 .dot { color: var(--accent); }
.generated { color: var(--muted); font-size: 0.82rem; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 14px; margin: 22px 0 10px; }
.tile { background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 16px 18px; }
.tile-value { font-size: 1.5rem; font-weight: 650; }
.tile-label { color: var(--muted); font-size: 0.8rem; margin-top: 4px;
  text-transform: uppercase; letter-spacing: 0.6px; }
.tile-sub { color: var(--muted); font-size: 0.78rem; margin-top: 6px; }
.tile.pl-pos .tile-value { color: var(--pos); }
.tile.pl-neg .tile-value { color: var(--neg); }
.clv-tile .tile-value { color: var(--accent); }
section.card { background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 18px 20px; margin-top: 22px; }
section.card > h2 { margin: 0 0 14px; font-size: 1.05rem;
  font-weight: 600; }
.venue-chart { display: block; }
.venue-chart .bar-track { fill: var(--panel-2); }
.venue-chart .bar-sportsbook { fill: var(--sportsbook); }
.venue-chart .bar-polymarket { fill: var(--polymarket); }
.venue-chart .bar-kalshi { fill: var(--kalshi); }
.venue-chart .bar-label { fill: var(--text); font-size: 14px;
  font-weight: 600; }
.venue-chart .bar-value { fill: var(--muted); font-size: 13px; }
table.bets { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
table.bets th, table.bets td { text-align: left; padding: 9px 10px;
  border-bottom: 1px solid var(--border); }
table.bets th { color: var(--muted); font-weight: 600; font-size: 0.76rem;
  text-transform: uppercase; letter-spacing: 0.5px; }
table.bets td.num, table.bets th.num { text-align: right;
  font-variant-numeric: tabular-nums; }
table.bets tr:last-child td { border-bottom: none; }
.empty-row { color: var(--muted); text-align: center; padding: 18px 10px; }
.status { display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 0.74rem; text-transform: capitalize; border: 1px solid var(--border); }
.status-open { color: var(--accent); border-color: var(--accent); }
.banner { background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 14px; color: var(--muted);
  font-size: 0.85rem; margin-top: 18px; }
footer.app { color: var(--muted); font-size: 0.78rem; margin-top: 34px;
  text-align: center; }
"""


def render_html(stats: Dict[str, Any], now_utc: str = "") -> str:
    """Render the complete standalone dashboard HTML page.

    Parameters
    ----------
    stats:
        The dict produced by :func:`gather_stats`.
    now_utc:
        Pre-formatted generation timestamp string (the caller stamps the
        clock; this function never reads it).  May be empty.

    Returns
    -------
    str
        A complete, self-contained HTML5 document with inlined CSS and inline
        SVG charts and *no* external requests of any kind.
    """
    totals = stats.get("totals") or _empty_venue_block()
    by_venue = stats.get("by_venue") or {v: _empty_venue_block() for v in VENUES}
    bets = stats.get("bets") or []
    clv = stats.get("clv") or {"avg_clv": None, "pct_beat_close": None, "n_with_close": 0}
    inputs_ok = bool(stats.get("generated_inputs_ok"))

    settled_pl = float(totals.get("settled_pl", 0.0))
    pl_cls = "pl-pos" if settled_pl >= 0 else "pl-neg"

    gen_line = ""
    if now_utc:
        gen_line = '<div class="generated">Generated %s</div>' % _esc(now_utc)

    tiles = "".join([
        _tile("Total Wagered", _esc(_fmt_money(totals.get("wagered", 0.0)))),
        _tile("Open Exposure", _esc(_fmt_money(totals.get("open_stake", 0.0)))),
        _tile("Settled P&L", _esc(_fmt_signed_money(settled_pl)), cls=pl_cls),
        _tile("Bets Placed", _esc(str(int(totals.get("n_bets", 0))))),
        _clv_tile(clv),
    ])

    chart = _venue_bar_chart(by_venue)
    open_rows = _open_bets_rows(bets)

    banner = ""
    if not inputs_ok:
        banner = (
            '<div class="banner">No ledger database found yet &mdash; '
            "showing zeros. Place a bet to populate the dashboard.</div>"
        )

    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        "<title>World Cup Alpha &mdash; Dashboard</title>\n"
        "<style>%s</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="wrap">\n'
        '<header class="app">\n'
        '<h1>World Cup Alpha <span class="dot">&bull;</span> '
        "Betting Dashboard</h1>\n"
        "%s\n"
        "</header>\n"
        "%s\n"
        '<div class="tiles">%s</div>\n'
        '<section class="card">\n'
        "<h2>Wagered by venue</h2>\n"
        "%s\n"
        "</section>\n"
        '<section class="card">\n'
        "<h2>Open bets</h2>\n"
        '<table class="bets">\n'
        "<thead><tr>"
        "<th>Match</th><th>Selection</th>"
        '<th class="num">Odds</th><th class="num">Stake</th>'
        "<th>Platform</th><th>Status</th>"
        "</tr></thead>\n"
        "<tbody>%s</tbody>\n"
        "</table>\n"
        "</section>\n"
        '<footer class="app">World Cup Alpha &mdash; static ledger snapshot. '
        "No external assets, no tracking.</footer>\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    ) % (_STYLE, gen_line, banner, tiles, chart, open_rows)

    return doc


# ---------------------------------------------------------------------------
# Write helper.
# ---------------------------------------------------------------------------


def write_dashboard(db_path: str, out_path: str, now_utc: str) -> str:
    """Gather stats, render the page, and write it to ``out_path``.

    Parent directories of ``out_path`` are created as needed.

    Parameters
    ----------
    db_path:
        Path to the SQLite ledger.
    out_path:
        Destination HTML file path.
    now_utc:
        Pre-formatted generation timestamp (the caller stamps the clock).

    Returns
    -------
    str
        ``out_path`` (echoed back for convenience).
    """
    stats = gather_stats(db_path)
    page = render_html(stats, now_utc=now_utc)

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)

    return out_path
