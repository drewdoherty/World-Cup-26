"""Structured JSON feed for the World Cup Alpha trading-terminal site.

This module turns the live bet ledger plus the cached matchday card into a
single, flat ``data.json`` document that the static front-end (``site/``)
renders with a tiny vanilla-JS app.  Unlike :mod:`wca.dashboard`, which emits a
fully self-contained HTML page, here we emit *data only* — the look-and-feel
lives in ``site/index.html`` / ``app.js`` / ``style.css``.

Design notes
------------
* **Deterministic.** :func:`build_site_data` never reads the wall clock; the
  caller passes ``now_utc`` (the CLI is allowed to stamp it).  This keeps the
  output reproducible and trivially testable.
* **Reuse, don't duplicate.** Venue rollups, totals, CLV and the raw bet list
  all come straight from :func:`wca.dashboard.gather_stats`, so the site and
  the legacy dashboard can never drift apart.
* **Tolerant parsing.** A missing card file or a missing / empty database must
  never raise — the corresponding sections come back empty so the site simply
  shows a clean "no data" state.

The cached card (``data/card_latest.md``) has a *scorelines* section that looks
like::

    *World Cup Alpha — scorelines* (2 fixtures)

    *Mexico vs South Africa*
        1-0  16.9%  fair 5.91  back >= 6.03
        ...
        O/U 2.5: over 45.8% / under 54.2%   BTTS 39.0%

We parse that into structured rows of ``{score, prob, fair, back}`` plus an
over/under + BTTS summary line per fixture.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from wca import dashboard


# ---------------------------------------------------------------------------
# Scoreline-section parsing.
# ---------------------------------------------------------------------------

# A fixture heading inside the scorelines section, e.g. "*Mexico vs South
# Africa*".  We only treat a "*...*" line as a fixture once we are *inside* the
# scorelines section (see _parse_scorelines), so the bet-card section's own
# numbered "*1. A vs B* ..." headings are never mistaken for fixtures.
_FIXTURE_RE = re.compile(r"^\*(?P<name>.+?)\*\s*$")

# A single scoreline row: "1-0  16.9%  fair 5.91  back >= 6.03".  The "fair"
# and "back" columns are optional so we tolerate slimmer card variants.
_SCORE_RE = re.compile(
    r"^(?P<score>\d+\s*-\s*\d+)\s+"
    r"(?P<prob>\d+(?:\.\d+)?)%"
    # The fair / back prices may be followed by an implied-probability note in
    # parentheses, e.g. "fair 5.63 (17.8%)" — tolerate it (and the older
    # note-free format) so the site feed keeps parsing either card variant.
    r"(?:\s+fair\s+(?P<fair>\d+(?:\.\d+)?)(?:\s*\(\d+(?:\.\d+)?%\))?)?"
    r"(?:\s+back\s*>=\s*(?P<back>\d+(?:\.\d+)?)(?:\s*\(\d+(?:\.\d+)?%\))?)?"
    r"\s*$"
)

# The over/under + BTTS summary line:
# "O/U 2.5: over 45.8% / under 54.2%   BTTS 39.0%".
_OU_RE = re.compile(
    r"^O/U\s+(?P<line>\d+(?:\.\d+)?)\s*:\s*"
    r"over\s+(?P<over>\d+(?:\.\d+)?)%\s*/\s*"
    r"under\s+(?P<under>\d+(?:\.\d+)?)%"
    r"(?:\s+BTTS\s+(?P<btts>\d+(?:\.\d+)?)%)?"
    r"\s*$"
)

# Expected-goals line: "xG: 1.47-0.89" (home - away), emitted by format_scores.
_XG_RE = re.compile(
    r"^xG:\s*(?P<home>\d+(?:\.\d+)?)\s*-\s*(?P<away>\d+(?:\.\d+)?)\s*$"
)

# Header of the scorelines section itself.
_SCORELINES_HEADER_RE = re.compile(r"^\*World Cup Alpha\s*[—-]\s*scorelines\*")


def _to_opt_float(text: Optional[str]) -> Optional[float]:
    """Parse a numeric string to float, returning None on failure / None."""
    if text is None:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_scorelines(card_text: str) -> List[Dict[str, Any]]:
    """Parse the *scorelines* section of a card body into structured fixtures.

    Parameters
    ----------
    card_text:
        The raw card body (header comment already stripped, or not — leading
        lines before the scorelines header are ignored either way).

    Returns
    -------
    list of dict
        One entry per fixture::

            {
              "fixture": "Mexico vs South Africa",
              "scores": [
                  {"score": "1-0", "prob": 16.9, "fair": 5.91, "back": 6.03},
                  ...
              ],
              "over_under": {"line": 2.5, "over": 45.8, "under": 54.2},
              "btts": 39.0,
            }

        ``over_under`` is ``None`` when no O/U line was present; ``btts`` is
        ``None`` when not stated.  An empty list is returned when there is no
        scorelines section at all.
    """
    if not card_text:
        return []

    lines = card_text.splitlines()

    # Locate the scorelines section start.
    start = None
    for i, line in enumerate(lines):
        if _SCORELINES_HEADER_RE.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return []

    fixtures: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for raw in lines[start:]:
        line = raw.strip()
        if not line:
            continue

        # A new top-level "*World Cup Alpha — ...*" header ends the section.
        if line.startswith("*World Cup Alpha"):
            break

        # Expected-goals line: "xG: 1.47-0.89".
        xg_match = _XG_RE.match(line)
        if xg_match and current is not None:
            current["xg_home"] = _to_opt_float(xg_match.group("home"))
            current["xg_away"] = _to_opt_float(xg_match.group("away"))
            continue

        # Over/under + BTTS summary (check before the generic fixture regex,
        # which would not match anyway, but order keeps intent clear).
        ou_match = _OU_RE.match(line)
        if ou_match and current is not None:
            current["over_under"] = {
                "line": _to_opt_float(ou_match.group("line")),
                "over": _to_opt_float(ou_match.group("over")),
                "under": _to_opt_float(ou_match.group("under")),
            }
            current["btts"] = _to_opt_float(ou_match.group("btts"))
            continue

        # Scoreline row.
        score_match = _SCORE_RE.match(line)
        if score_match and current is not None:
            current["scores"].append({
                "score": score_match.group("score").replace(" ", ""),
                "prob": _to_opt_float(score_match.group("prob")),
                "fair": _to_opt_float(score_match.group("fair")),
                "back": _to_opt_float(score_match.group("back")),
            })
            continue

        # Otherwise: a fixture heading "*Fixture name*".
        fx_match = _FIXTURE_RE.match(line)
        if fx_match:
            current = {
                "fixture": fx_match.group("name").strip(),
                "scores": [],
                "over_under": None,
                "btts": None,
                "xg_home": None,
                "xg_away": None,
            }
            fixtures.append(current)
            continue

        # Unrecognised line inside the section — ignore it gracefully.

    return fixtures


def _read_card_body(card_path: str) -> str:
    """Return the card body with any ``<!-- generated: ... -->`` header line
    stripped, or ``""`` when the file is missing / unreadable."""
    if not card_path or not os.path.exists(card_path):
        return ""
    try:
        with open(card_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return ""

    # Strip a leading generated-timestamp comment header, matching cardcache.
    first, _, rest = raw.partition("\n")
    if first.startswith("<!-- generated:") and first.rstrip().endswith("-->"):
        return rest
    return raw


# ---------------------------------------------------------------------------
# Positions (open bets) extraction.
# ---------------------------------------------------------------------------


# Venue -> currency of money actually held there. Pools are PER-CURRENCY and
# must never be summed across currencies (GBP + USD is not a number).
VENUE_CURRENCY = {"sportsbook": "GBP", "polymarket": "USD", "kalshi": "USD"}
CURRENCY_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€"}

# Statuses that mean the bet is still live. The ledger stores "open" but
# external ingestion paths (Betfair, third-party feeds) may log aliases.
_OPEN_STATUSES = frozenset({"open", "matched", "pending", "active", "unsettled"})

# Terminal statuses — these bets are correctly excluded from the open-positions
# view. Anything else is reported in dropped_open_bets (never silent).
_TERMINAL_STATUSES = frozenset({"won", "lost", "void"})


def _positions_from_bets(bets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Project the open bets into the compact terminal "positions" shape.

    Recognises every status in :data:`_OPEN_STATUSES` as "live" so that
    external ingestion paths that log "matched"/"pending"/"active"/"unsettled"
    are not silently dropped.  Terminal statuses are correctly excluded.  Any
    unrecognised status string is also excluded here; callers compare the
    result against ``dropped_open_bets`` so nothing is silent.

    Ordering follows ``gather_stats`` (newest-first by id).  No fixture
    mapping is applied — outrights (``match_id="outright_*"``) and
    multi-fixture accumulators pass through unchanged.
    """
    positions: List[Dict[str, Any]] = []
    for b in bets:
        status = (b.get("status") or "").strip().lower()
        if status in _TERMINAL_STATUSES:
            continue
        if status not in _OPEN_STATUSES:
            # Unknown — not terminal and not a recognised live alias.
            # build_site_data captures this in dropped_open_bets.
            continue
        venue = dashboard.venue_for_platform(b.get("platform"))
        positions.append({
            "id": b.get("id"),
            "ts_utc": b.get("ts_utc"),
            "match": b.get("match_desc"),
            "match_id": b.get("match_id"),
            "market": b.get("market"),
            "selection": b.get("selection"),
            "platform": b.get("platform"),
            "venue": venue,
            "account": str(b.get("account") or "1"),
            "source": str(b.get("source") or "model"),
            "currency": VENUE_CURRENCY.get(venue, "GBP"),
            "decimal_odds": _opt_num(b.get("decimal_odds")),
            "stake": _opt_num(b.get("stake")),
            "model_prob": _opt_num(b.get("model_prob")),
            "market_prob_devig": _opt_num(b.get("market_prob_devig")),
            "ev": _opt_num(b.get("ev")),
            "kelly_fraction": _opt_num(b.get("kelly_fraction")),
            "notes": b.get("notes"),
            "manual_override": b.get("manual_override"),
        })
    return positions


def _opt_num(value: Any) -> Optional[float]:
    """Coerce a DB numeric to float, preserving None (so the front-end can
    render an em-dash rather than a misleading 0)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Polymarket proxy wallet(s) holding live positions. These are placed manually
# in the PM UI and live ONLY on-chain (not in the SQLite ledger), so we read
# them from Polymarket's public data-API at site-build time — the source of
# truth, independent of which ledger fork ran the build. Overridable via the
# WCA_PM_PROXIES env (comma-separated) or POLYMARKET_FUNDER.
_PM_PROXIES_DEFAULT = ("0x86b4c55a4df1fbea0f325e842434e0a537caa549",)


def live_pm_positions(
    proxies: Optional[List[str]] = None, min_value: float = 0.1
) -> Optional[List[Dict[str, Any]]]:
    """Fetch live Polymarket positions and project them into the positions
    shape. Returns ``None`` on total failure so callers fall back to the
    ledger-only positions (the function never raises)."""
    import json as _json
    import os as _os
    import urllib.request as _u

    if proxies is None:
        env = (
            _os.environ.get("WCA_PM_PROXIES")
            or _os.environ.get("POLYMARKET_FUNDER")
            or ""
        )
        proxies = [p.strip() for p in env.split(",") if p.strip()] or list(
            _PM_PROXIES_DEFAULT
        )

    out: List[Dict[str, Any]] = []
    for i, proxy in enumerate(proxies):
        acct = "1" if i == 0 else "2"
        url = (
            "https://data-api.polymarket.com/positions?user=%s"
            "&sizeThreshold=0.1&limit=200" % proxy
        )
        try:
            req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            rows = _json.load(_u.urlopen(req, timeout=20))
        except Exception:
            continue
        for p in rows or []:
            try:
                cv = float(p.get("currentValue") or 0.0)
            except (TypeError, ValueError):
                cv = 0.0
            if cv < min_value:
                continue  # resolved / dust — not an open holding
            avg = _opt_num(p.get("avgPrice")) or 0.0
            iv = _opt_num(p.get("initialValue")) or 0.0
            # The API's percentPnl field is unreliable; derive P&L from cost vs
            # current value (cashPnl == currentValue - initialValue).
            cash_pnl = round(cv - iv, 2)
            out.append({
                "id": "pm-" + str(p.get("conditionId") or "")[:12],
                "ts_utc": "",
                "match": p.get("title"),
                "match_id": p.get("conditionId"),
                "market": p.get("title"),
                "selection": p.get("outcome"),
                "platform": "polymarket",
                "venue": "polymarket",
                "account": acct,
                "source": "manual",
                "currency": "USD",
                "decimal_odds": (1.0 / avg) if avg > 0 else None,
                "stake": _opt_num(p.get("initialValue")),
                "model_prob": None,
                "market_prob_devig": None,
                "ev": None,
                "kelly_fraction": None,
                "cur_value": round(cv, 2),
                "cur_price": _opt_num(p.get("curPrice")),
                "avg_price": avg,
                "shares": _opt_num(p.get("size")),
                "cash_pnl": cash_pnl,
                "pct_pnl": (cash_pnl / iv) if iv else None,
                "notes": "live PM position (data-API)",
            })
    return out or None


def settled_pm_positions(
    proxies: Optional[List[str]] = None,
    *,
    since_hours: int = 24,
    min_value: float = 0.1,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch RESOLVED Polymarket positions and project them into the settled
    shape. Returns ``None`` on total failure (never raises).

    A Polymarket position is treated as SETTLED when its market has resolved:
    ``currentValue`` has collapsed to ~0 (below ``min_value``) AND the data-API
    reports a realised P&L (``realizedPnl``) and/or a ``redeemable`` flag. We
    require an unambiguous realised P&L; positions still carrying live value go
    to :func:`live_pm_positions` instead.

    INFERRED SHAPE CAVEAT: the data-API does not expose a settled timestamp, so
    the 24h window CANNOT be applied server-side here. ``settled_ts`` is left
    empty and the caller's window filter keeps PM settles in the snapshot (the
    conservative confident-match guard, not the clock, is what gates the
    auto-settle). If a settled timestamp becomes available, populate
    ``settled_ts`` and the shared window filter will apply.
    """
    import json as _json
    import os as _os
    import urllib.request as _u

    if proxies is None:
        env = (
            _os.environ.get("WCA_PM_PROXIES")
            or _os.environ.get("POLYMARKET_FUNDER")
            or ""
        )
        proxies = [p.strip() for p in env.split(",") if p.strip()] or list(
            _PM_PROXIES_DEFAULT
        )

    out: List[Dict[str, Any]] = []
    any_ok = False
    for i, proxy in enumerate(proxies):
        acct = "1" if i == 0 else "2"
        url = (
            "https://data-api.polymarket.com/positions?user=%s"
            "&sizeThreshold=0.1&limit=200&redeemable=true" % proxy
        )
        try:
            req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            rows = _json.load(_u.urlopen(req, timeout=20))
            any_ok = True
        except Exception:
            continue
        for p in rows or []:
            try:
                cv = float(p.get("currentValue") or 0.0)
            except (TypeError, ValueError):
                cv = 0.0
            redeemable = bool(p.get("redeemable"))
            realized = _opt_num(p.get("realizedPnl"))
            # SETTLED = resolved market: value collapsed AND a realised P&L /
            # redeemable flag is present. Anything still holding live value is an
            # OPEN position, handled by live_pm_positions.
            is_settled = (cv < min_value) and (redeemable or realized is not None)
            if not is_settled:
                continue
            avg = _opt_num(p.get("avgPrice")) or 0.0
            iv = _opt_num(p.get("initialValue")) or 0.0
            if realized is None:
                # Fall back to cash P&L = current value − cost (cv≈0 ⇒ −cost).
                realized = round(cv - iv, 2)
            result = "won" if (realized is not None and realized > 0) else "lost"
            out.append({
                "venue": "polymarket",
                "market": p.get("title"),
                "selection": p.get("outcome"),
                "fixture_or_event": p.get("title"),
                "stake": iv or None,
                "size": _opt_num(p.get("size")),
                "avg_price": avg or None,
                "odds": (1.0 / avg) if avg > 0 else None,
                "settled_pnl": round(float(realized), 2),
                "result": result,
                "settled_ts": "",
                "external_id": p.get("conditionId"),
                "account": acct,
                "token_id": p.get("conditionId"),
            })
    if not any_ok:
        return None
    return out


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def build_site_data(
    db_path: str,
    card_path: str = "data/card_latest.md",
    now_utc: str = "",
    pm_positions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the full ``data.json`` payload for the static site.

    Parameters
    ----------
    db_path:
        Path to the SQLite ledger.  Missing / empty databases yield zeroed
        rollups and empty positions (never raises).
    card_path:
        Path to the cached matchday card.  Missing files yield an empty
        ``predictions`` list.
    now_utc:
        Pre-formatted generation timestamp (the caller stamps the clock; this
        function never reads it).  May be empty.

    Returns
    -------
    dict
        ::

            {
              "meta": {"generated": now_utc},
              "totals": {wagered, open_stake, settled_pl, n_bets},
              "venues": {
                  "sportsbook": {wagered, open_stake, settled_pl, n_bets},
                  "polymarket": {...},
                  "kalshi": {...},
              },
              "clv": {avg_clv, pct_beat_close, n_with_close},
              "positions": [ {...open bet...}, ... ],
              "predictions": [ {...fixture...}, ... ],
            }
    """
    stats = dashboard.gather_stats(db_path)

    by_venue = stats.get("by_venue") or {}
    # Normalise to plain dicts in canonical venue order.
    venues: Dict[str, Any] = {}
    for v in dashboard.VENUES:
        block = by_venue.get(v) or {}
        venues[v] = {
            "wagered": float(block.get("wagered", 0.0)),
            "open_stake": float(block.get("open_stake", 0.0)),
            "settled_pl": float(block.get("settled_pl", 0.0)),
            "n_bets": int(block.get("n_bets", 0)),
            "currency": VENUE_CURRENCY.get(v, "GBP"),
        }

    # Split the sportsbook venue (GBP) by physical account -> "sportsbook_1" /
    # "sportsbook_2". The legacy combined "sportsbook" key above is retained so
    # old front-ends keep working. polymarket/kalshi are not split (single
    # account each for now). source_summary aggregates per source x currency.
    def _acct_block() -> Dict[str, Any]:
        return {"wagered": 0.0, "open_stake": 0.0, "settled_pl": 0.0,
                "n_bets": 0, "currency": "GBP"}

    sb_by_account: Dict[str, Dict[str, Any]] = {"1": _acct_block(), "2": _acct_block()}
    source_summary: Dict[str, Any] = {}
    for b in stats.get("bets") or []:
        stake = float(b.get("stake") or 0.0)
        status = (b.get("status") or "").strip().lower()
        venue = dashboard.venue_for_platform(b.get("platform"))
        ccy = VENUE_CURRENCY.get(venue, "GBP")
        is_open = status == "open"
        pl = float(b.get("settled_pl") or 0.0) if status in ("won", "lost", "cashed") else 0.0

        if venue == "sportsbook":
            acct = str(b.get("account") or "1")
            blk = sb_by_account.setdefault(acct, _acct_block())
            blk["wagered"] += stake
            blk["n_bets"] += 1
            if is_open:
                blk["open_stake"] += stake
            blk["settled_pl"] += pl

        src = str(b.get("source") or "model")
        sblk = source_summary.setdefault(src, {}).setdefault(
            ccy, {"wagered": 0.0, "open_stake": 0.0, "settled_pl": 0.0, "n_bets": 0}
        )
        sblk["wagered"] += stake
        sblk["n_bets"] += 1
        if is_open:
            sblk["open_stake"] += stake
        sblk["settled_pl"] += pl

    for acct, label in (("1", "Sportsbook 1"), ("2", "Sportsbook 2")):
        blk = sb_by_account.get(acct) or _acct_block()
        blk["label"] = label
        venues["sportsbook_%s" % acct] = blk

    # Totals PER CURRENCY — £ and $ are never added together. The legacy
    # single-number "totals" block is kept for backward compatibility but the
    # front-end should prefer totals_by_currency.
    totals_by_currency: Dict[str, Any] = {}
    for v in dashboard.VENUES:
        block = venues[v]
        ccy = block["currency"]
        agg = totals_by_currency.setdefault(
            ccy, {"wagered": 0.0, "open_stake": 0.0, "settled_pl": 0.0, "n_bets": 0}
        )
        agg["wagered"] += block["wagered"]
        agg["open_stake"] += block["open_stake"]
        agg["settled_pl"] += block["settled_pl"]
        agg["n_bets"] += block["n_bets"]

    totals_in = stats.get("totals") or {}
    totals = {
        "wagered": float(totals_in.get("wagered", 0.0)),
        "open_stake": float(totals_in.get("open_stake", 0.0)),
        "settled_pl": float(totals_in.get("settled_pl", 0.0)),
        "n_bets": int(totals_in.get("n_bets", 0)),
    }

    clv_in = stats.get("clv") or {}
    clv = {
        "avg_clv": clv_in.get("avg_clv"),
        "pct_beat_close": clv_in.get("pct_beat_close"),
        "n_with_close": int(clv_in.get("n_with_close") or 0),
    }

    all_bets = stats.get("bets") or []
    positions = _positions_from_bets(all_bets)

    # Invariant diagnostic: every non-terminal bet must appear in positions.
    # A non-empty list means a bet is live but got silently excluded — that is
    # always a bug and must never be silent.
    _position_ids = {p["id"] for p in positions}
    dropped_open_bets: List[Dict[str, Any]] = []
    for b in all_bets:
        raw = (b.get("status") or "").strip().lower()
        if raw in _TERMINAL_STATUSES:
            continue
        if b.get("id") not in _position_ids:
            dropped_open_bets.append({
                "id": b.get("id"),
                "match": b.get("match_desc"),
                "status": b.get("status"),
                "reason": "unrecognized_status",
            })

    # Live Polymarket holdings are the source of truth for the PM venue (placed
    # manually in the UI, not in the ledger). When provided, they REPLACE the
    # ledger-derived PM rows in the positions list and the polymarket venue /
    # USD-total open_stake + count, so the terminal shows the real book.
    if pm_positions is not None:
        positions = [p for p in positions if p.get("venue") != "polymarket"]
        positions = list(pm_positions) + positions
        pm_stake = sum(float(p.get("stake") or 0.0) for p in pm_positions)
        pm_n = len(pm_positions)
        pm_blk = venues.get("polymarket")
        old_stake = float(pm_blk.get("open_stake") or 0.0) if pm_blk else 0.0
        old_n = int(pm_blk.get("n_bets") or 0) if pm_blk else 0
        if pm_blk is not None:
            pm_blk["open_stake"] = pm_stake
            pm_blk["n_bets"] = pm_n
        usd = totals_by_currency.get("USD")
        if usd is not None:
            usd["open_stake"] += pm_stake - old_stake
            usd["n_bets"] += pm_n - old_n

    # Closed (settled/void) positions with realized P&L per bet.
    closed_positions: List[Dict[str, Any]] = []
    for b in stats.get("bets") or []:
        status = (b.get("status") or "").strip().lower()
        if status not in ("won", "lost", "void", "cashed"):
            continue
        venue = dashboard.venue_for_platform(b.get("platform"))
        closed_positions.append({
            "id": b.get("id"),
            "ts_utc": b.get("ts_utc"),
            "settled_ts": b.get("settled_ts"),
            "match": b.get("match_desc"),
            "match_id": b.get("match_id"),
            "market": b.get("market"),
            "selection": b.get("selection"),
            "platform": b.get("platform"),
            "venue": venue,
            "account": str(b.get("account") or "1"),
            "source": str(b.get("source") or "model"),
            "currency": VENUE_CURRENCY.get(venue, "GBP"),
            "decimal_odds": _opt_num(b.get("decimal_odds")),
            "stake": _opt_num(b.get("stake")),
            "model_prob": _opt_num(b.get("model_prob")),
            "market_prob_devig": _opt_num(b.get("market_prob_devig")),
            "ev": _opt_num(b.get("ev")),
            "kelly_fraction": _opt_num(b.get("kelly_fraction")),
            "status": status,
            "pl": _opt_num(b.get("settled_pl")),
            "closing_odds": _opt_num(b.get("closing_odds")),
            "clv": _opt_num(b.get("clv")),
            "notes": b.get("notes"),
            "manual_override": b.get("manual_override"),
        })

    # Realized P&L curves: cumulative settled P&L over settlement time, one
    # series for the sportsbook pool (GBP) and one for prediction markets
    # combined (polymarket + kalshi, USD). Currencies are separate lines —
    # never summed.
    def _pnl_series(rows: List[Dict[str, Any]]) -> List[List[Any]]:
        pts = [(r.get("settled_ts") or r.get("ts_utc"), r.get("pl") or 0.0)
               for r in rows if r.get("pl") is not None]
        pts.sort(key=lambda x: str(x[0]))
        cum, out = 0.0, []
        for ts, pl in pts:
            cum += float(pl)
            out.append([ts, round(cum, 2)])
        return out

    pnl_series = {
        "sportsbook": {
            "currency": "GBP",
            "points": _pnl_series([r for r in closed_positions
                                   if r["venue"] == "sportsbook"]),
        },
        "prediction_markets": {
            "currency": "USD",
            "points": _pnl_series([r for r in closed_positions
                                   if r["venue"] in ("polymarket", "kalshi")]),
        },
    }

    # Per-bookmaker breakdown within each venue (all bets, not just open),
    # so the site can show which books the money actually sits at.
    platforms: Dict[str, Any] = {}
    # Same per-book breakdown, but ALSO split by physical account so the venue
    # panel can show each sportsbook account's own book split (a1 AND a2 — the
    # two accounts share books like Betfair, so the venue-level split alone can't
    # attribute the per-book money).
    platforms_by_account: Dict[str, Dict[str, Any]] = {}

    def _plat_block(venue: str) -> Dict[str, Any]:
        return {
            "venue": venue,
            "currency": VENUE_CURRENCY.get(venue, "GBP"),
            "wagered": 0.0, "open_stake": 0.0, "settled_pl": 0.0, "n_bets": 0,
        }

    for b in stats.get("bets") or []:
        plat = (b.get("platform") or "unknown").strip()
        venue = dashboard.venue_for_platform(plat)
        stake = float(b.get("stake") or 0.0)
        status = (b.get("status") or "").lower()
        pl = float(b.get("settled_pl") or 0.0) if status in ("won", "lost", "cashed") else 0.0
        acct = str(b.get("account") or "1")

        for blk in (
            platforms.setdefault(plat, _plat_block(venue)),
            platforms_by_account.setdefault(acct, {}).setdefault(plat, _plat_block(venue)),
        ):
            blk["wagered"] += stake
            blk["n_bets"] += 1
            if status == "open":
                blk["open_stake"] += stake
            blk["settled_pl"] += pl

    predictions = parse_scorelines(_read_card_body(card_path))

    return {
        "meta": {"generated": now_utc},
        "totals": totals,
        "totals_by_currency": totals_by_currency,
        "venues": venues,
        "source_summary": source_summary,
        "platforms": platforms,
        "platforms_by_account": platforms_by_account,
        "closed_positions": closed_positions,
        "pnl_series": pnl_series,
        "clv": clv,
        "positions": positions,
        "dropped_open_bets": dropped_open_bets,
        "predictions": predictions,
    }


def write_site_data(
    db_path: str,
    out_path: str = "site/data.json",
    card_path: str = "data/card_latest.md",
    now_utc: str = "",
    include_pm_live: bool = True,
) -> str:
    """Build the site payload and write it to ``out_path`` as JSON.

    Parent directories are created as needed.  Returns ``out_path``.

    When ``include_pm_live`` (the default for site generation) live Polymarket
    positions are pulled from the data-API and merged in; failures fall back to
    ledger-only positions silently.
    """
    pm = live_pm_positions() if include_pm_live else None
    data = build_site_data(
        db_path, card_path=card_path, now_utc=now_utc, pm_positions=pm
    )

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    return out_path
