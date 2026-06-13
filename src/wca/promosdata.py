"""Deterministic JSON feed for the promo-operations panel of the site.

This is the promo sibling of :mod:`wca.sitedata`. It turns the
:mod:`wca.promos` catalog (the ``promotions`` / ``promo_snapshots`` /
``boost_evals`` tables) into the single flat ``promos_data.json`` document that
the static front-end renders.

Design notes (identical discipline to :mod:`wca.sitedata`)
---------------------------------------------------------
* **Deterministic.** :func:`build_promos_data` NEVER reads the wall clock; the
  caller (``scripts/wca_promos_data.py``) stamps ``now_utc`` and passes it in.
  Given the same DB rows and the same ``now_utc`` the output is byte-identical,
  which is what makes it testable.
* **Reuse, don't duplicate.** All catalog access goes through the reader views
  in :mod:`wca.promos` (``active_promotions`` / ``latest_snapshot_per_site`` /
  ``recent_boost_evals`` / ``signup_offers``), so the feed and the DB can never
  drift apart.
* **Tolerant.** An empty / brand-new DB yields empty sections (never raises) so
  the site shows a clean "nothing yet" state.

``scores_feed`` is accepted for parity with :mod:`wca.sitedata` and for future
use (e.g. annotating boost evals with live fixture state). It is not consumed
today; passing ``None`` or ``{}`` is fine.

Output shape (consumed by the front-end JS)::

    { "meta": {"generated": now_utc},
      "sites": [{name, kind, scrape:{status,last_seen}, ongoing:[...], boosts:[...]}],
      "signup_offers": [{site, offer, min_odds, min_stake, free_bet_value,
                         expiry, promo_code, url}],
      "watchlist":     [{site, title, description, why}],
      "boost_evals":   [{ts, site, fixture, market, selection, boosted_odds,
                         model_prob, fair_odds, edge, is_plus_ev, source}],
      "scrape_health": [{site, status, http_status, last_ok_utc}] }
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from wca import promos


def _promo_view(row: sqlite3.Row) -> Dict[str, Any]:
    """Project a ``promotions`` row into the compact ``{title,description,url}``."""
    return {
        "title": row["title"] or "",
        "description": row["description"] or "",
        "url": row["url"] or "",
    }


def build_promos_data(
    conn: sqlite3.Connection,
    scores_feed: Optional[Dict[str, Any]],
    now_utc: str,
) -> Dict[str, Any]:
    """Assemble the ``promos_data.json`` payload from the promo catalog.

    Deterministic: every value comes from the DB or from ``now_utc`` — the wall
    clock is never read here. ``scores_feed`` is accepted for parity / future use
    and may be ``None``.

    Parameters
    ----------
    conn:
        Open connection to a DB whose promo tables exist. (The caller calls
        :func:`wca.promos.init_db` first; we also tolerate missing tables by
        catching the lookup errors into empty sections.)
    scores_feed:
        The parsed ``site/scores_data.json`` document (or ``None``). Unused today.
    now_utc:
        Pre-formatted generation timestamp; stamped verbatim into ``meta.generated``.
    """
    try:
        active = promos.active_promotions(conn)
    except sqlite3.Error:
        active = []
    try:
        snaps = promos.latest_snapshot_per_site(conn)
    except sqlite3.Error:
        snaps = {}
    try:
        last_ok = promos.latest_ok_snapshot_per_site(conn)
    except sqlite3.Error:
        last_ok = {}
    try:
        signups = promos.signup_offers(conn)
    except sqlite3.Error:
        signups = []
    try:
        bevals = promos.recent_boost_evals(conn, limit=50)
    except sqlite3.Error:
        bevals = []

    # Group active ongoing/boost promotions by site (signups + watchlist are
    # surfaced in their own top-level sections, not under each site card).
    ongoing_by_site: Dict[str, List[Dict[str, Any]]] = {}
    boosts_by_site: Dict[str, List[Dict[str, Any]]] = {}
    watchlist: List[Dict[str, Any]] = []
    for row in active:
        ptype = row["promo_type"]
        site = row["site"]
        if ptype == "ongoing":
            ongoing_by_site.setdefault(site, []).append(_promo_view(row))
        elif ptype == "boost":
            boosts_by_site.setdefault(site, []).append(_promo_view(row))
        elif ptype == "watchlist":
            watchlist.append(
                {
                    "site": site,
                    "title": row["title"] or "",
                    "description": row["description"] or "",
                    "why": "flagged in recon as a check-the-app / no-standing-promo note",
                }
            )
        # 'signup' rows are emitted via signup_offers(), not here.

    # Build one card per registry site so the panel always shows every monitored
    # book/exchange (even ones with no active promos / never-fetched), in the
    # registry's stable order. Any site that appears only in the DB (e.g. a recon
    # book name not in SITES) is appended afterwards, sorted, for completeness.
    sites_out: List[Dict[str, Any]] = []
    emitted: set = set()

    def _scrape_block(site_name: str) -> Dict[str, Any]:
        snap = snaps.get(site_name)
        return {
            "status": snap["fetch_status"] if snap else "never",
            "last_seen": snap["ts_utc"] if snap else "",
        }

    for entry in promos.SITES:
        name = entry["name"]
        emitted.add(name)
        sites_out.append(
            {
                "name": name,
                "kind": entry.get("kind", ""),
                "scrape": _scrape_block(name),
                "ongoing": ongoing_by_site.get(name, []),
                "boosts": boosts_by_site.get(name, []),
            }
        )

    extra_sites = sorted(
        (set(ongoing_by_site) | set(boosts_by_site) | set(snaps)) - emitted
    )
    for name in extra_sites:
        if name in emitted:
            continue
        emitted.add(name)
        sites_out.append(
            {
                "name": name,
                "kind": "",
                "scrape": _scrape_block(name),
                "ongoing": ongoing_by_site.get(name, []),
                "boosts": boosts_by_site.get(name, []),
            }
        )

    # Boost-eval stream (newest first; recent_boost_evals already ordered DESC).
    boost_evals_out: List[Dict[str, Any]] = []
    for r in bevals:
        boost_evals_out.append(
            {
                "ts": r["ts_utc"],
                "site": r["site"],
                "fixture": r["fixture"],
                "market": r["market"],
                "selection": r["selection"],
                "boosted_odds": r["boosted_odds"],
                "model_prob": r["model_prob"],
                "fair_odds": r["fair_odds"],
                "edge": r["edge"],
                "is_plus_ev": bool(r["is_plus_ev"]),
                "source": r["source"],
            }
        )

    # Scrape-health table: one row per registry site, honest status + last OK.
    scrape_health: List[Dict[str, Any]] = []
    for entry in promos.SITES:
        name = entry["name"]
        snap = snaps.get(name)
        scrape_health.append(
            {
                "site": name,
                "status": snap["fetch_status"] if snap else "never",
                "http_status": snap["http_status"] if snap else None,
                "last_ok_utc": last_ok.get(name, ""),
            }
        )

    return {
        "meta": {"generated": now_utc},
        "sites": sites_out,
        "signup_offers": signups,
        "watchlist": watchlist,
        "boost_evals": boost_evals_out,
        "scrape_health": scrape_health,
    }
