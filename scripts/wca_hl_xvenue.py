#!/usr/bin/env python
"""Cross-venue HL<->Polymarket snapshot -> SHADOW-only ``site/hl_xvenue.json``.

Snapshots both venues for the 16 settlement-matched 2026-World-Cup pairs
(8 champion + 8 QF team-sides — map pinned in ``wca.hl.xvenue``), computes
fee-adjusted cross-venue gaps + executable size for both directions of each
pair, and writes the monitor-only feed. Labels are XV_WATCH /
XV_ARB_CANDIDATE / XV_MISMATCHED_SETTLEMENT / XV_NO_DATA — NEVER a trade
instruction. This script never touches the ledger, Telegram, or any
execution path; there is no execution scaffold for Hyperliquid at all
(watcher-only verdict — go/no-go criteria for ever building one:
``docs/research/hl_venue_recon_2026-07-09.md``).

Hard rules encoded here (do not regress):

* Settlement-basis gating: a positive fee-adjusted edge whose direction
  carries a divergent settlement tail (QF dir2 cancellation-toxic; champion
  dir1 co-champion tail) can NEVER be XV_ARB_CANDIDATE — it caps at
  XV_MISMATCHED_SETTLEMENT (`wca.hl.xvenue.TAILS`).
* PM per-match 1X2 never pairs with HL QF markets (3-way / 90-min vs 2-way /
  ET+pens + 0.5-void tail) — structurally excluded from the pair map.
* Fee stack: PM taker 0.03*p*(1-p) per share; HL trading fee 0 (docs +
  497/502 empirical fills); HL SETTLEMENT fee UNVERIFIED, assumed 0 with a
  mandatory feed caveat.
* Fail-closed: any missing/unfetchable book, or a PM token id that no longer
  matches the live gamma mapping, drops the pair to XV_NO_DATA. An SSL
  WRONG_VERSION_NUMBER means the NordVPN tunnel dropped (both venues are
  VPN-only from the dev box): the run ABORTS with exit code 2 and says so —
  it never retries blindly and never writes a partial feed.
* Every reported figure comes from a fetched (or replayed) book; aggregates
  state n (``n_snapshots`` from the local history file). Nothing is invented.

Network cadence: 24 HL l2Book POSTs + 32 PM CLOB book GETs (+2 gamma GETs
for mapping verification) per run — REST-polling scale, fine for the
10-30s-cadence monitoring recommended by the recon. The mini is PM-blind
(and HL rides the same VPN-only route), so this runs from the MacBook over
NordVPN; the publish-loop hook is gated behind ``WCA_HL_XVENUE=1``
(default OFF).

Usage
-----
    python scripts/wca_hl_xvenue.py                       # live snapshot
    python scripts/wca_hl_xvenue.py --offline-dir DIR     # replay dumped books
        [--out site/hl_xvenue.json] [--history data/hl_xvenue_history.jsonl]
        [--generated 2026-07-09T18:16:00Z] [--dump-dir DIR]
        [--skip-gamma-verify]

Offline replay reads the recon capture's file naming:
``l2book_<outcome_id>_side<0|1>.json`` and
``book_<win_wc|reach_sf>_<Team>_<Yes|No>.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import requests  # noqa: E402

from wca.hl import client as hl_client  # noqa: E402
from wca.hl import xvenue  # noqa: E402

PM_BOOK_URL = "https://clob.polymarket.com/book"
PM_GAMMA_URL = "https://gamma-api.polymarket.com/events"
_HEADERS = {"User-Agent": "WorldCupAlpha/0.1 (read-only monitor)"}
_TIMEOUT = 20

_PM_GROUP_BY_KIND = {"champion": "win_wc", "qf": "reach_sf"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_generated(raw: Optional[str]) -> str:
    """Injectable clock; an unparseable stamp is a HARD error (edge-desk
    convention — never a silent wall-clock fallback)."""
    if raw is None:
        return _utcnow_iso()
    try:
        datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SystemExit("--generated %r is not ISO-8601 Z (YYYY-MM-DDTHH:MM:SSZ): %s" % (raw, exc))
    return raw


def _abort_if_vpn_drop(exc: BaseException) -> None:
    if hl_client.is_vpn_drop(exc):
        print(
            "FATAL: SSL WRONG_VERSION_NUMBER — the NordVPN tunnel has "
            "dropped (both api.hyperliquid.xyz and Polymarket are VPN-only "
            "from this box). Not retrying; no feed written.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _dump(dump_dir: Optional[str], name: str, obj: Any) -> None:
    if not dump_dir:
        return
    os.makedirs(dump_dir, exist_ok=True)
    with open(os.path.join(dump_dir, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)


# ---------------------------------------------------------------------------
# Book sources: live network vs offline replay
# ---------------------------------------------------------------------------

class LiveSource:
    """Fetch books over the network (read-only GET/POST info calls)."""

    def __init__(self, dump_dir: Optional[str] = None):
        self._hl = hl_client.HLInfoClient()
        self._dump_dir = dump_dir
        self._hl_cache: Dict[Any, Optional[Dict[str, Any]]] = {}
        self.failures: Dict[str, str] = {}

    def hl_book(self, outcome_id: int, side: int) -> Optional[Dict[str, Any]]:
        key = (outcome_id, side)
        if key in self._hl_cache:
            return self._hl_cache[key]
        try:
            raw = self._hl.l2_book(outcome_id, side)
            _dump(self._dump_dir, "l2book_%d_side%d.json" % (outcome_id, side), raw)
            book = hl_client.parse_l2_book(raw)
        except Exception as exc:  # noqa: BLE001 — fail the pair closed
            _abort_if_vpn_drop(exc)
            self.failures["hl:%d/%d" % (outcome_id, side)] = repr(exc)
            book = None
        self._hl_cache[key] = book
        return book

    def pm_book(self, group: str, team: str, outcome: str, token_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = requests.get(
                PM_BOOK_URL, params={"token_id": token_id},
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            raw = resp.json()
            _dump(self._dump_dir, "book_%s_%s_%s.json" % (group, team, outcome), raw)
            return xvenue.parse_pm_book(raw)
        except Exception as exc:  # noqa: BLE001
            _abort_if_vpn_drop(exc)
            self.failures["pm:%s/%s/%s" % (group, team, outcome)] = repr(exc)
            return None

    def gamma_event(self, slug: str) -> Optional[Any]:
        try:
            resp = requests.get(
                PM_GAMMA_URL, params={"slug": slug},
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            raw = resp.json()
            _dump(self._dump_dir, "gamma_event_%s.json" % slug.replace("-", "_"), raw)
            return raw
        except Exception as exc:  # noqa: BLE001
            _abort_if_vpn_drop(exc)
            self.failures["gamma:%s" % slug] = repr(exc)
            return None


class OfflineSource:
    """Replay a directory of dumped raw books (recon naming). Deterministic —
    used for tests, fixture replays, and generating the initial committed
    feed from the 2026-07-09 capture."""

    def __init__(self, directory: str):
        self._dir = directory
        self.failures: Dict[str, str] = {}

    def _load(self, name: str, key: str) -> Optional[Any]:
        path = os.path.join(self._dir, name)
        if not os.path.exists(path):
            self.failures[key] = "missing file %s" % name
            return None
        return xvenue.load_json(path)

    def hl_book(self, outcome_id: int, side: int) -> Optional[Dict[str, Any]]:
        raw = self._load("l2book_%d_side%d.json" % (outcome_id, side), "hl:%d/%d" % (outcome_id, side))
        return hl_client.parse_l2_book(raw) if raw is not None else None

    def pm_book(self, group: str, team: str, outcome: str, token_id: str) -> Optional[Dict[str, Any]]:
        raw = self._load(
            "book_%s_%s_%s.json" % (group, team, outcome),
            "pm:%s/%s/%s" % (group, team, outcome),
        )
        if raw is None:
            return None
        parsed = xvenue.parse_pm_book(raw)
        # Same fail-closed identity check as the live gamma verify: the dump
        # must actually be the pinned token's book.
        if parsed.get("asset_id") not in (None, token_id):
            self.failures["pm:%s/%s/%s" % (group, team, outcome)] = (
                "asset_id mismatch: dump %r != pinned %r" % (parsed.get("asset_id"), token_id)
            )
            return None
        return parsed

    def gamma_event(self, slug: str) -> Optional[Any]:
        return None  # offline replay trusts the asset_id identity check above


# ---------------------------------------------------------------------------
# PM mapping verification (fail-closed per pair)
# ---------------------------------------------------------------------------

def verify_pm_mapping(gamma_payload: Any, expected: Dict[str, Any]) -> Dict[str, str]:
    """Cross-check pinned (market_id, token_yes, token_no) per team against a
    live gamma ``/events?slug=`` payload. Returns {team: problem} for every
    team that no longer matches (missing market, changed tokens, market
    closed+archived, ...). Pure — unit-tested offline."""
    problems: Dict[str, str] = {}
    events = gamma_payload if isinstance(gamma_payload, list) else (gamma_payload or {}).get("data", [])
    if not events:
        return {team: "gamma event payload empty" for team in expected}
    markets = events[0].get("markets") or []
    by_group_title = {m.get("groupItemTitle"): m for m in markets}
    for team, exp in expected.items():
        m = by_group_title.get(team)
        if m is None:
            problems[team] = "no gamma market with groupItemTitle=%r" % team
            continue
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            try:
                toks = json.loads(toks)
            except json.JSONDecodeError:
                toks = None
        if not toks or len(toks) < 2:
            problems[team] = "gamma market %r has no clobTokenIds" % m.get("id")
        elif str(m.get("id")) != exp["market_id"] or toks[0] != exp["token_yes"] or toks[1] != exp["token_no"]:
            problems[team] = (
                "pinned mapping stale: gamma id=%r tokens=%r..." % (m.get("id"), str(toks[0])[:16])
            )
    return problems


# ---------------------------------------------------------------------------
# Snapshot run
# ---------------------------------------------------------------------------

def run_snapshot(source: Any, skip_gamma_verify: bool = False) -> Dict[str, Any]:
    """Fetch/replay all books, evaluate the 16 pairs, return rows + telemetry."""
    configs = xvenue.pair_configs()

    mapping_problems: Dict[str, Dict[str, str]] = {}
    if not skip_gamma_verify:
        for group, slug in xvenue.PM_GAMMA_SLUGS.items():
            payload = source.gamma_event(slug)
            if payload is None:
                continue  # unreachable gamma degrades to a caveat, not a wipe
            table = xvenue.PM_WIN_WC if group == "win_wc" else xvenue.PM_REACH_SF
            expected = {
                team: {"market_id": mid, "token_yes": ty, "token_no": tn}
                for team, (mid, ty, tn) in table.items()
            }
            probs = verify_pm_mapping(payload, expected)
            if probs:
                mapping_problems[group] = probs

    rows = []
    for cfg in configs:
        group = _PM_GROUP_BY_KIND[cfg["kind"]]
        problem = (mapping_problems.get(group) or {}).get(cfg["team"])
        if problem:
            row = xvenue.evaluate_pair(cfg, None, None, None, None)
            row["status_reason"] = "pm mapping verification failed (fail-closed): %s" % problem
            rows.append(row)
            continue
        yes_side = cfg["hl_yes_side"]
        hl_yes = source.hl_book(cfg["hl_outcome_id"], yes_side)
        hl_no = source.hl_book(cfg["hl_outcome_id"], 1 - yes_side)
        pm_yes = source.pm_book(group, cfg["team"], "Yes", cfg["pm_token_yes"])
        pm_no = source.pm_book(group, cfg["team"], "No", cfg["pm_token_no"])
        rows.append(xvenue.evaluate_pair(cfg, hl_yes, hl_no, pm_yes, pm_no))

    return {
        "rows": rows,
        "fetch_failures": dict(getattr(source, "failures", {})),
        "mapping_problems": mapping_problems,
    }


def _append_history(path: str, record: Dict[str, Any]) -> int:
    """Append one line to the local snapshot history; returns the line count
    AFTER the append (= n_snapshots including this run). The history file is
    machine-local (untracked) — it exists to make n honest, not to sync."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def _write_atomic(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".hl_xvenue_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(_ROOT, "site", "hl_xvenue.json"))
    ap.add_argument("--history", default=os.path.join(_ROOT, "data", "hl_xvenue_history.jsonl"))
    ap.add_argument("--offline-dir", default=None,
                    help="replay dumped raw books from this directory instead of the network")
    ap.add_argument("--dump-dir", default=None,
                    help="(live mode) dump every raw API response here")
    ap.add_argument("--generated", default=None,
                    help="injectable ISO-8601Z clock; unparseable = hard error")
    ap.add_argument("--skip-gamma-verify", action="store_true",
                    help="skip the live PM mapping cross-check (2 gamma GETs)")
    args = ap.parse_args(argv)

    generated_at = _parse_generated(args.generated)
    if args.offline_dir:
        source: Any = OfflineSource(args.offline_dir)
        mode = "offline-replay:%s" % os.path.abspath(args.offline_dir)
    else:
        source = LiveSource(dump_dir=args.dump_dir)
        mode = "live"

    result = run_snapshot(source, skip_gamma_verify=args.skip_gamma_verify)
    rows = result["rows"]

    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    open_edges = [
        d["edge_per_share_at_best"]
        for r in rows if r.get("directions")
        for d in r["directions"].values()
        if not d["settlement_tail"]["gated"] and d["edge_per_share_at_best"] is not None
    ]
    n_snapshots = _append_history(args.history, {
        "generated_at": generated_at,
        "mode": mode,
        "by_status": by_status,
        "best_open_edge_per_share": max(open_edges) if open_edges else None,
    })

    extra_caveats = []
    if result["fetch_failures"]:
        extra_caveats.append(
            "fetch failures this run (pairs failed closed to XV_NO_DATA): %s"
            % json.dumps(result["fetch_failures"], sort_keys=True)
        )
    if result["mapping_problems"]:
        extra_caveats.append(
            "PM mapping verification problems (fail-closed): %s"
            % json.dumps(result["mapping_problems"], sort_keys=True)
        )
    if args.offline_dir:
        extra_caveats.append(
            "offline replay of dumped books — book timestamps inside rows are "
            "the CAPTURE times, not generated_at"
        )

    feed = xvenue.build_feed(
        rows,
        generated_at=generated_at,
        n_snapshots=n_snapshots,
        sources={
            "mode": mode,
            "hl_info": hl_client.HL_INFO_URL,
            "pm_clob_book": PM_BOOK_URL,
            "pm_gamma": PM_GAMMA_URL + "?slug=" + "|".join(xvenue.PM_GAMMA_SLUGS.values()),
            "pair_map": "wca.hl.xvenue (pinned 2026-07-09 recon; raw capture "
                        "18:13-18:15 UTC; load-bearing books preserved in "
                        "tests/fixtures/hl_xvenue/)",
            "history_file": os.path.relpath(os.path.abspath(args.history), _ROOT),
        },
        extra_caveats=extra_caveats,
    )
    _write_atomic(args.out, feed)
    print(
        "hl_xvenue: wrote %s (n_snapshots=%d, %s)"
        % (args.out, n_snapshots, ", ".join("%s=%d" % kv for kv in sorted(by_status.items())))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
