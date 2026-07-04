"""The Odds API v4 — complete read-endpoint registry with quota guarding.

Design:
* :data:`ENDPOINTS` documents EVERY relevant v4 read endpoint (featured odds,
  per-event odds incl. props, available-markets, scores, participants, the
  three historical endpoints) with its documented credit-cost formula.
* :class:`QuotaGuard` enforces a per-run credit budget. Costs are ESTIMATED
  up-front from the documented formulas (labelled estimates) and TRUED-UP
  from the ``x-requests-used`` header delta after each call — the observed
  number is what counts against the budget.
* :func:`fetch` is the single door: offline mode (or cache hit within
  ``cache_max_age_s``) reads the newest raw snapshot; live mode performs the
  HTTP GET with retries + exponential backoff, then persists the payload to
  the raw layer BEFORE returning it. Every call — success, skip, or error —
  is appended to the guard's call log so the notebook can print a full
  run/skip table with reasons.

The production client (``wca.data.theoddsapi``) is reused for its response
parsing (``_parse_events``) so bronze tables here are shaped exactly like
production's; requests go through :func:`fetch` for uniform raw capture.

The World Cup sport key is DISCOVERED from ``/sports`` and validated, never
hardcoded (production evidence: ``soccer_fifa_world_cup`` — verified at run
time each pull).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import lib.bootstrap as bt
import lib.storage as st

BASE = "https://api.the-odds-api.com/v4"
SOURCE = "theoddsapi"

# Featured markets served by the bulk /odds endpoint; everything else
# (btts, DNB, spreads variants, player props, alternates…) is per-event only.
FEATURED_MARKETS = ("h2h", "spreads", "totals", "outrights")

# --------------------------------------------------------------------------
# Endpoint registry. cost: documented formula, evaluated per-call. All costs
# are ESTIMATES from https://the-odds-api.com/liveapi/guides/v4/ — the guard
# trues them up from response headers.
# --------------------------------------------------------------------------


def _n(csv: Optional[str]) -> int:
    return len([x for x in (csv or "").split(",") if x.strip()]) or 1


@dataclass
class Endpoint:
    key: str
    path: str                       # format template
    cost: Callable[[Dict[str, Any]], int]
    cost_note: str
    doc: str
    needs: Tuple[str, ...] = ()     # required template fields


ENDPOINTS: Dict[str, Endpoint] = {e.key: e for e in [
    Endpoint("sports", "/sports", lambda p: 0, "free",
             "All in-season sports (all=true adds out-of-season)."),
    Endpoint("events", "/sports/{sport_key}/events", lambda p: 0, "free",
             "Upcoming/live events for a sport, no odds.", ("sport_key",)),
    Endpoint("odds", "/sports/{sport_key}/odds",
             lambda p: _n(p.get("regions")) * _n(p.get("markets")),
             "regions × markets (featured markets only)",
             "Bulk featured odds for every upcoming event.", ("sport_key",)),
    Endpoint("event_markets", "/sports/{sport_key}/events/{event_id}/markets",
             lambda p: _n(p.get("regions")),
             "regions (documented as counted like one market per region)",
             "Which markets each bookmaker currently offers for ONE event.",
             ("sport_key", "event_id")),
    Endpoint("event_odds", "/sports/{sport_key}/events/{event_id}/odds",
             lambda p: _n(p.get("regions")) * _n(p.get("markets")),
             "regions × unique markets — the only route to props/non-featured",
             "Odds for ONE event; supports btts, alternates, player props…",
             ("sport_key", "event_id")),
    Endpoint("scores", "/sports/{sport_key}/scores",
             lambda p: 2 if p.get("daysFrom") else 1,
             "1 live-only; 2 with daysFrom (adds recently completed)",
             "Live + recently completed scores.", ("sport_key",)),
    Endpoint("participants", "/sports/{sport_key}/participants",
             lambda p: 1, "1", "Team list for a sport.", ("sport_key",)),
    Endpoint("historical_events", "/historical/sports/{sport_key}/events",
             lambda p: 1, "1 (metadata snapshot, no odds)",
             "Event list as of a past `date` (ISO).", ("sport_key",)),
    Endpoint("historical_odds", "/historical/sports/{sport_key}/odds",
             lambda p: 10 * _n(p.get("regions")) * _n(p.get("markets")),
             "10 × regions × markets — EXPENSIVE",
             "Bulk featured odds snapshot as of a past `date`.", ("sport_key",)),
    Endpoint("historical_event_odds",
             "/historical/sports/{sport_key}/events/{event_id}/odds",
             lambda p: 10 * _n(p.get("regions")) * _n(p.get("markets")),
             "10 × regions × unique markets — EXPENSIVE",
             "One event's odds (incl. props) as of a past `date`.",
             ("sport_key", "event_id")),
]}


@dataclass
class QuotaGuard:
    """Per-run credit budget with observed-usage true-up and a full call log."""
    max_credits: int
    dry_run: bool = False
    spent_estimated: int = 0
    spent_observed: int = 0
    remaining_reported: Optional[int] = None
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def estimate(self, key: str, params: Dict[str, Any]) -> int:
        return ENDPOINTS[key].cost(params)

    def can_afford(self, est: int) -> bool:
        return self.spent_estimated + est <= self.max_credits

    def log(self, **row: Any) -> None:
        row.setdefault("utc", bt.utcnow_iso())
        self.calls.append(row)

    def to_frame(self):
        import pandas as pd
        return pd.DataFrame(self.calls)


class SkippedCall(Exception):
    """Raised when the guard (budget/offline/dry-run) skips a live call."""


def fetch(key: str, guard: QuotaGuard, *, offline: bool = False,
          cache_max_age_s: Optional[int] = None,
          retries: int = 3, backoff_s: float = 2.0,
          **params: Any) -> Tuple[Any, str, Dict[str, Any]]:
    """One guarded, cached, raw-captured GET.

    Returns (payload, snapshot_id, meta). `params` mixes path fields
    (sport_key, event_id) and query params. Raises :class:`SkippedCall`
    with a precise reason when no data is available under the constraints.
    """
    ep = ENDPOINTS[key]
    missing = [f for f in ep.needs if f not in params]
    if missing:
        raise ValueError(f"{key} needs {missing}")
    path_fields = {f: params.pop(f) for f in ep.needs}
    endpoint_path = ep.path.format(**path_fields)
    cache_params = {**path_fields, **params}
    est = ep.cost(params)

    # 1) cache / offline
    snap = st.latest_raw(SOURCE, endpoint_path, cache_params)
    if snap:
        meta = st.raw_meta(snap)
        age = _age_s(meta.get("retrieved_utc"))
        if offline or (cache_max_age_s is not None and age is not None
                       and age <= cache_max_age_s):
            guard.log(endpoint=key, mode="cache", est_credits=0,
                      snapshot=snap, cache_age_s=age, status=meta.get("status"))
            return st.read_raw(snap), snap, meta
    if offline:
        guard.log(endpoint=key, mode="skip", est_credits=est,
                  reason="offline mode and no cached snapshot")
        raise SkippedCall(f"{key}: offline and never pulled")

    # 2) guard rails
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        guard.log(endpoint=key, mode="skip", est_credits=est,
                  reason="ODDS_API_KEY not configured")
        raise SkippedCall(f"{key}: no API key")
    if guard.dry_run:
        guard.log(endpoint=key, mode="dry_run", est_credits=est,
                  reason=f"dry-run; would cost ~{est} ({ep.cost_note})")
        raise SkippedCall(f"{key}: dry-run (est {est} credits)")
    if not guard.can_afford(est):
        guard.log(endpoint=key, mode="skip", est_credits=est,
                  reason=f"budget: {guard.spent_estimated}+{est} > {guard.max_credits}")
        raise SkippedCall(f"{key}: over credit budget")

    # 3) live call with retries + backoff
    q = {"apiKey": api_key, "dateFormat": "iso", **params}
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(BASE + endpoint_path, params=q, timeout=30)
            if resp.status_code == 429 and attempt < retries - 1:
                time.sleep(backoff_s * (2 ** attempt))
                continue
            used_before = guard.spent_observed
            payload = resp.json() if resp.content else None
            headers = dict(resp.headers)
            snap = st.write_raw(SOURCE, endpoint_path, payload,
                                params={**cache_params, "apiKey": "***"},
                                status=resp.status_code, headers=headers,
                                url=resp.url.split("apiKey=")[0] + "apiKey=***")
            guard.spent_estimated += est
            used = headers.get("x-requests-used")
            rem = headers.get("x-requests-remaining")
            if rem is not None:
                prev = guard.remaining_reported
                guard.remaining_reported = int(float(rem))
                if prev is not None:
                    guard.spent_observed += max(0, prev - guard.remaining_reported)
            guard.log(endpoint=key, mode="live", est_credits=est,
                      status=resp.status_code, snapshot=snap,
                      quota_used_hdr=used, quota_remaining_hdr=rem)
            if resp.status_code >= 400:
                raise SkippedCall(
                    f"{key}: HTTP {resp.status_code} — {str(payload)[:200]}")
            return payload, snap, st.raw_meta(snap)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            time.sleep(backoff_s * (2 ** attempt))
    guard.log(endpoint=key, mode="error", est_credits=est,
              reason=f"network error after {retries} tries: {last_err}")
    raise SkippedCall(f"{key}: network failure ({last_err})")


def _age_s(retrieved_utc: Optional[str]) -> Optional[float]:
    if not retrieved_utc:
        return None
    import datetime as dt
    try:
        t = dt.datetime.strptime(retrieved_utc, "%Y-%m-%dT%H:%M:%SZ")
        return (dt.datetime.utcnow() - t).total_seconds()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# WC sport-key discovery (never hardcode)
# --------------------------------------------------------------------------

def discover_wc_sport_key(guard: QuotaGuard, *, offline: bool = False) -> Dict[str, Any]:
    """Find + validate the FIFA World Cup sport key from /sports (free)."""
    payload, snap, _ = fetch("sports", guard, offline=offline,
                             cache_max_age_s=6 * 3600, all="true")
    hits = [s for s in (payload or [])
            if "world cup" in (s.get("title") or "").lower()
            and (s.get("group") or "").lower() == "soccer"
            and "winner" not in (s.get("title") or "").lower()]
    active = [s for s in hits if s.get("active")]
    chosen = (active or hits)
    if not chosen:
        raise SkippedCall("no soccer 'World Cup' sport in /sports response")
    return {"sport_key": chosen[0]["key"], "candidates": hits,
            "snapshot": snap}


def parse_odds_payload(payload: Any):
    """Bronze-shape an /odds or /event-odds payload EXACTLY like production
    (reuses wca.data.theoddsapi._parse_events)."""
    import pandas as pd
    from wca.data import theoddsapi as toa
    events = payload if isinstance(payload, list) else [payload]
    rows = toa._parse_events([e for e in events if isinstance(e, dict)])
    df = pd.DataFrame(rows)
    if not df.empty:
        df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True, errors="coerce")
        df["retrieved_at"] = pd.to_datetime(df["retrieved_at"], utc=True, errors="coerce")
    return df
