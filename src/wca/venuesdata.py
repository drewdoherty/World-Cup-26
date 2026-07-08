"""Data access + feed assembly for the Model-vs-Venue benchmark.

Reads the EXISTING stores read-only and joins them into the panel the pure
engine (:mod:`wca.venuesbench`) ranks:

* model fair 1X2 probabilities + their Elo/DC/market components, from the
  ``predictions`` ledger (one row per fixture-leg per build);
* per-bookmaker venue quotes, from ``odds_snapshots`` (OddsAPI ``raw`` JSON);
* placed model bets (``bets.source = 'model'``), linked to the EXACT preceding
  build/leg — never "this leg was ever placed".

Everything here is deterministic and side-effect-free except the explicit
read-only DB queries. The no-lookahead matcher and the bet linkage are written
as pure functions over plain rows so they can be tested without a database.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from wca import venuesbench as vb
from wca import venues
from wca.markets import devig


# --------------------------------------------------------------------------- #
# Time parsing
# --------------------------------------------------------------------------- #


def parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse a stored timestamp to a timezone-aware UTC datetime.

    Handles the two formats in the stores: naive ``2026-06-13T00:09:50`` (treated
    as UTC) and tz-aware ``2026-06-11T13:27:30.716212+00:00`` (and the space
    variant ``2026-06-13 01:00:00+00:00``). Returns ``None`` on junk.
    """
    if not s:
        return None
    txt = str(s).strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        # Last-ditch: trim fractional seconds / trailing tokens.
        try:
            dt = datetime.fromisoformat(txt[:19])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Team / outcome canonicalisation (for the Arm-B match_id bridge)
# --------------------------------------------------------------------------- #

#: Common short/alt spellings -> the canonical team name used in predictions.
_TEAM_ALIASES = {
    "usa": "united states", "us": "united states", "united states of america": "united states",
    "czechia": "czech republic", "czech rep": "czech republic", "czech": "czech republic",
    "korea republic": "south korea", "korea": "south korea", "south korea": "south korea",
    "bosnia": "bosnia and herzegovina", "bih": "bosnia and herzegovina",
    "turkiye": "turkey", "türkiye": "turkey",
    "ivory coast": "cote d'ivoire", "côte d'ivoire": "cote d'ivoire",
    "iran": "iran", "ir iran": "iran",
    # knockout-stage spellings that differ between OddsAPI and Polymarket
    "bosnia & herzegovina": "bosnia and herzegovina",
    "cabo verde": "cape verde", "cape verde islands": "cape verde",
    "congo dr": "dr congo",
    "democratic republic of congo": "dr congo", "congo democratic republic": "dr congo",
}


def canon_team(name: str) -> str:
    """Normalise a team name to lowercase canonical form for fixture matching."""
    t = (name or "").strip().lower()
    return _TEAM_ALIASES.get(t, t)


def pair_key(a: str, b: str) -> frozenset:
    """Order-independent fixture key from two team names."""
    return frozenset({canon_team(a), canon_team(b)})


def split_fixture(fixture: str) -> Tuple[str, str]:
    """Split a 'A vs B' / 'A v B' fixture string into (home, away)."""
    txt = (fixture or "").replace(" vs ", " v ").strip()
    parts = txt.split(" v ", 1)
    if len(parts) != 2:
        return (fixture or "", "")
    return (parts[0].strip(), parts[1].strip())


def map_outcome_to_leg(outcome: str, home: str, away: str) -> Optional[str]:
    """Map an OddsAPI ``outcome_name`` to a 1X2 leg using the fixture's teams."""
    o = canon_team(outcome)
    if o in ("draw", "tie"):
        return "Draw"
    if o == canon_team(home):
        return "Home"
    if o == canon_team(away):
        return "Away"
    return None


# --------------------------------------------------------------------------- #
# Per-book quotes (no-lookahead, freshness-capped, incomplete-omitted)
# --------------------------------------------------------------------------- #

#: A raw quote row: (book_key, outcome_name, home_team, away_team, dt, decimal_odds).
QuoteRow = Tuple[str, str, str, str, datetime, float]


def per_book_quotes_from_rows(
    rows: Sequence[QuoteRow],
    as_of: datetime,
    freshness_s: float,
    *,
    method: str = "shin",
) -> Dict[str, Dict[str, object]]:
    """Latest complete fresh 1X2 per book at-or-before ``as_of``, de-vigged.

    For each canonical book we keep, per leg, the newest quote with
    ``dt <= as_of`` (NO lookahead). A book is included only if all three legs are
    present AND the freshest of its three legs is within ``freshness_s`` of
    ``as_of`` (stale or incomplete books are OMITTED, never imputed).

    Returns ``{book: {"fair": triple, "age_s": float, "odds": {leg: odds}}}``.
    """
    # book -> leg -> (dt, odds)
    latest: Dict[str, Dict[str, Tuple[datetime, float]]] = {}
    for book_key, outcome, home, away, dt, odds in rows:
        if dt is None or dt > as_of:           # strict no-lookahead
            continue
        leg = map_outcome_to_leg(outcome, home, away)
        if leg is None:
            continue
        try:
            odds_f = float(odds)
        except (TypeError, ValueError):
            continue
        if odds_f <= 1.0:
            continue
        book = venues.canon_book(book_key)
        legmap = latest.setdefault(book, {})
        prev = legmap.get(leg)
        if prev is None or dt > prev[0]:
            legmap[leg] = (dt, odds_f)

    out: Dict[str, Dict[str, object]] = {}
    for book, legmap in latest.items():
        if any(leg not in legmap for leg in vb.LEGS):
            continue  # incomplete 1X2 -> omit
        newest = max(legmap[leg][0] for leg in vb.LEGS)
        age = (as_of - newest).total_seconds()
        if age > freshness_s:
            continue  # stale -> omit
        odds_triple = {leg: legmap[leg][1] for leg in vb.LEGS}
        fair = vb.book_fair_triple(odds_triple, method=method)
        if fair is None:
            continue
        out[book] = {"fair": fair, "age_s": float(age), "odds": odds_triple}
    return out


def load_match_quote_rows(con: sqlite3.Connection, match_id: str) -> List[QuoteRow]:
    """All h2h quote rows for a fixture from ``odds_snapshots`` (parsed once)."""
    cur = con.execute(
        "SELECT ts_utc, decimal_odds, "
        "json_extract(raw,'$.bookmaker_key'), json_extract(raw,'$.outcome_name'), "
        "json_extract(raw,'$.home_team'), json_extract(raw,'$.away_team') "
        "FROM odds_snapshots WHERE match_id = ? AND market = 'h2h'",
        (match_id,),
    )
    rows: List[QuoteRow] = []
    for ts, odds, book, outcome, home, away in cur:
        dt = parse_ts(ts)
        if dt is None or book is None or outcome is None:
            continue
        rows.append((book, outcome, home or "", away or "", dt, odds))
    return rows


# --------------------------------------------------------------------------- #
# Model records from the predictions ledger
# --------------------------------------------------------------------------- #


def load_model_records(con: sqlite3.Connection, market: str = "1X2") -> List[Dict[str, object]]:
    """One record per (build, fixture): the model 1X2 triple + components.

    Each record carries the model / Elo / DC / market / closing triples, the
    build timestamp, kickoff, the realised outcome (the leg whose status is
    'won', else ``None``), and the set of placed legs.
    """
    cur = con.execute(
        "SELECT build_id, match_id, fixture, kickoff_utc, ts_utc, selection, "
        "model_prob, elo_prob, dc_prob, market_devig_prob, closing_devig_prob, "
        "placed, status, stage "
        "FROM predictions WHERE market = ? ORDER BY ts_utc",
        (market,),
    )
    recs: Dict[Tuple[str, str], Dict[str, object]] = {}
    for (build_id, match_id, fixture, kickoff, ts, sel, mp, elo, dc, mkt, cl,
         placed, status, stage) in cur:
        leg = sel if sel in vb.LEGS else None
        if leg is None:
            continue
        key = (build_id, match_id)
        rec = recs.get(key)
        if rec is None:
            rec = {
                "build_id": build_id, "match_id": match_id, "fixture": fixture,
                "kickoff": parse_ts(kickoff), "ts": parse_ts(ts),
                "stage": stage or "", "legs": {},
                "placed": set(), "outcome": None,
            }
            recs[key] = rec
        rec["legs"][leg] = {
            "model": mp, "elo": elo, "dc": dc, "market": mkt, "closing": cl,
        }
        if placed:
            rec["placed"].add(leg)
        if (status or "").lower() == "won":
            rec["outcome"] = leg

    # Keep only complete 1X2 records (all three legs with model+elo+dc present).
    out = []
    for rec in recs.values():
        legs = rec["legs"]
        if all(l in legs and legs[l]["model"] is not None
               and legs[l]["elo"] is not None and legs[l]["dc"] is not None
               for l in vb.LEGS):
            out.append(rec)
    out.sort(key=lambda r: (r["ts"] or datetime.min.replace(tzinfo=timezone.utc), r["match_id"]))
    return out


def _triple(legs: Dict[str, Dict[str, object]], field: str) -> Optional[vb.Triple]:
    try:
        vals = [float(legs[l][field]) for l in vb.LEGS]
    except (KeyError, TypeError, ValueError):
        return None
    if any(v is None for v in vals) or sum(vals) <= 0:
        return None
    return vb.as_triple(tuple(vals))


#: Model-side comparators. "model" contains market consensus (circular — display
#: only); the rest are independent of any single book.
COMPARATORS = ("ex_market", "elo", "dc", "model")


def model_side_triples(legs: Dict[str, Dict[str, object]]) -> Dict[str, Optional[vb.Triple]]:
    """The model-side comparator triples for one fixture's legs."""
    elo = _triple(legs, "elo")
    dc = _triple(legs, "dc")
    return {
        "model": _triple(legs, "model"),
        "elo": elo,
        "dc": dc,
        "ex_market": vb.ex_market_triple(elo, dc) if (elo and dc) else None,
    }


# --------------------------------------------------------------------------- #
# Arm A: paper-book panel (every model-priced fixture vs every venue)
# --------------------------------------------------------------------------- #


def build_arm_a(
    records: Sequence[Dict[str, object]],
    con_odds: sqlite3.Connection,
    *,
    freshness_s: float = 6 * 3600.0,
    method: str = "shin",
) -> Dict[str, object]:
    """Assemble the Arm-A panels + accuracy + coverage from records and odds.

    Returns a dict with: ``panels`` (``{comparator: {metric: {obs: {venue: dist}}}}``),
    ``lobo`` (``{metric: {obs: {venue: dist}}}`` — venue vs leave-it-out consensus),
    ``accuracy`` (per venue + model, paired Brier/log-loss on settled fixtures),
    ``coverage`` and ``obs_meta`` (per-obs book count, median quote age, outcome).
    """
    panels: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = {
        c: {m: {} for m in vb.DISTANCE_METRICS} for c in COMPARATORS
    }
    lobo: Dict[str, Dict[str, Dict[str, float]]] = {m: {} for m in vb.DISTANCE_METRICS}
    obs_meta: Dict[str, Dict[str, object]] = {}
    quote_cache: Dict[str, List[QuoteRow]] = {}
    venues_seen = set()

    # Accuracy is one observation per FIXTURE (dedup builds): use the latest
    # pre-kickoff build that has a realised outcome.
    acc_pick: Dict[str, Dict[str, object]] = {}

    for rec in records:
        match_id = rec["match_id"]
        as_of = rec["ts"]
        if as_of is None:
            continue
        obs_id = "%s|%s" % (match_id, rec["build_id"])
        rows = quote_cache.get(match_id)
        if rows is None:
            rows = load_match_quote_rows(con_odds, match_id)
            quote_cache[match_id] = rows
        books = per_book_quotes_from_rows(rows, as_of, freshness_s, method=method)
        if not books:
            continue
        book_fairs = {b: books[b]["fair"] for b in books}
        sides = model_side_triples(rec["legs"])
        venues_seen.update(book_fairs.keys())

        for comp in COMPARATORS:
            side = sides.get(comp)
            if side is None:
                continue
            for metric, fn in vb.DISTANCE_METRICS.items():
                row = panels[comp][metric].setdefault(obs_id, {})
                for book, fair in book_fairs.items():
                    row[book] = fn(side, fair)

        # LOBO: each venue compared to the consensus of the OTHER books.
        for metric, fn in vb.DISTANCE_METRICS.items():
            row = lobo[metric].setdefault(obs_id, {})
            for book, fair in book_fairs.items():
                ind = vb.lobo_consensus(book_fairs, exclude=book)
                if ind is not None:
                    row[book] = fn(ind, fair)
            if not row:
                lobo[metric].pop(obs_id, None)

        ages = sorted(books[b]["age_s"] for b in books)
        model_side = sides.get("model") or sides.get("ex_market")
        fav_prob = max(model_side) if model_side else None
        ttk_h = None
        if rec["kickoff"] is not None:
            ttk_h = (rec["kickoff"] - as_of).total_seconds() / 3600.0
        obs_meta[obs_id] = {
            "match_id": match_id, "fixture": rec["fixture"], "build_id": rec["build_id"],
            "n_books": len(books), "median_age_s": ages[len(ages) // 2],
            "outcome": rec["outcome"], "fav_prob": fav_prob, "ttk_h": ttk_h,
        }

        # Accuracy candidate: settled fixture, pre-kickoff build.
        if rec["outcome"] is not None and rec["kickoff"] is not None and as_of <= rec["kickoff"]:
            prev = acc_pick.get(match_id)
            if prev is None or as_of > prev["as_of"]:
                acc_pick[match_id] = {
                    "as_of": as_of, "outcome": rec["outcome"], "sides": sides,
                    "book_fairs": book_fairs,
                }

    accuracy = _accuracy_block(acc_pick)
    coverage = {
        "n_obs": len(obs_meta),
        "n_fixtures": len({m["match_id"] for m in obs_meta.values()}),
        "n_venues": len(venues_seen),
        "venues": sorted(venues_seen),
    }
    return {"panels": panels, "lobo": lobo, "accuracy": accuracy,
            "coverage": coverage, "obs_meta": obs_meta}


def _accuracy_block(acc_pick: Dict[str, Dict[str, object]]) -> Dict[str, object]:
    """Paired Brier / log-loss for each venue and the model comparators.

    One observation per settled fixture (dedup builds). Agreement with the model
    is NOT accuracy — this scores every side against the realised outcome.
    """
    venue_scores: Dict[str, Dict[str, List[float]]] = {}
    model_scores: Dict[str, Dict[str, List[float]]] = {c: {"brier": [], "log_loss": []} for c in COMPARATORS}
    n = 0
    for match_id, pick in acc_pick.items():
        outcome = pick["outcome"]
        n += 1
        for comp, side in pick["sides"].items():
            if side is None:
                continue
            model_scores[comp]["brier"].append(vb.brier(side, outcome))
            model_scores[comp]["log_loss"].append(vb.log_loss(side, outcome))
        for book, fair in pick["book_fairs"].items():
            d = venue_scores.setdefault(book, {"brier": [], "log_loss": []})
            d["brier"].append(vb.brier(fair, outcome))
            d["log_loss"].append(vb.log_loss(fair, outcome))

    def _summ(d):
        nb = len(d["brier"])
        if nb == 0:
            return {"n": 0, "brier": None, "log_loss": None}
        return {"n": nb,
                "brier": round(sum(d["brier"]) / nb, 6),
                "log_loss": round(sum(d["log_loss"]) / nb, 6)}

    return {
        "n_fixtures": n,
        "venues": {b: _summ(s) for b, s in sorted(venue_scores.items())},
        "model": {c: _summ(s) for c, s in model_scores.items()},
        "note": ("accuracy is scored on settled fixtures only; agreement with the "
                 "model is not accuracy"),
    }


# --------------------------------------------------------------------------- #
# Arm B: link placed model bets to the exact preceding build/leg
# --------------------------------------------------------------------------- #

#: bet ``market`` strings that map to a 1X2 leg (others are out of scope here).
_1X2_MARKETS = {"h2h", "full time result", "full-time result", "match odds", "1x2"}


def link_model_bets(
    con_pred: sqlite3.Connection,
    con_bets: sqlite3.Connection,
) -> Dict[str, object]:
    """Link ``source='model'`` bets to the exact preceding prediction build/leg.

    A bet links to the latest prediction for the SAME fixture+leg whose
    ``ts_utc <= bet.ts_utc`` (the build that was live when the bet was placed) —
    never a fuzzy "ever placed" match. Returns linked rows plus an audit of every
    unmatched bet with a reason, so the thinness is visible, not hidden.
    """
    # Index predictions by fixture pair + leg, sorted by time.
    preds: Dict[Tuple[frozenset, str], List[Tuple[datetime, str]]] = {}
    for fixture, sel, ts in con_pred.execute(
        "SELECT fixture, selection, ts_utc FROM predictions WHERE market='1X2'"
    ):
        if sel not in vb.LEGS:
            continue
        h, a = split_fixture(fixture)
        dt = parse_ts(ts)
        if dt is None:
            continue
        preds.setdefault((pair_key(h, a), sel), []).append((dt, fixture))
    for k in preds:
        preds[k].sort()

    linked: List[Dict[str, object]] = []
    unmatched: List[Dict[str, object]] = []
    for bet_id, ts, match_desc, market, selection in con_bets.execute(
        "SELECT id, ts_utc, match_desc, market, selection FROM bets WHERE source='model'"
    ):
        mkt = (market or "").strip().lower()
        if mkt not in _1X2_MARKETS:
            unmatched.append({"bet_id": bet_id, "market": market, "reason": "non_1x2_market"})
            continue
        h, a = split_fixture(match_desc or "")
        leg = map_outcome_to_leg(selection or "", h, a)
        if leg is None:
            unmatched.append({"bet_id": bet_id, "market": market, "reason": "selection_not_a_team_leg"})
            continue
        bet_dt = parse_ts(ts)
        cands = preds.get((pair_key(h, a), leg))
        if not cands:
            unmatched.append({"bet_id": bet_id, "market": market, "reason": "no_matching_fixture_build"})
            continue
        prior = [c for c in cands if bet_dt is not None and c[0] <= bet_dt]
        if not prior:
            unmatched.append({"bet_id": bet_id, "market": market, "reason": "bet_predates_first_build"})
            continue
        chosen = prior[-1]
        linked.append({"bet_id": bet_id, "leg": leg, "fixture": chosen[1],
                       "build_ts": chosen[0].isoformat()})

    return {
        "n_model_bets": len(linked) + len(unmatched),
        "n_linked": len(linked),
        "n_unmatched": len(unmatched),
        "linked": linked,
        "unmatched_audit": unmatched,
        "insufficient": len(linked) < int(rigor_clv_MIN),
        "note": ("placed-bet arm links each source=model bet to the exact "
                 "preceding build; unmatched bets are audited, not dropped"),
    }


# Local alias so the linkage threshold lives next to the engine's gate floor.
rigor_clv_MIN = vb.rigor_clv.N_EFF_CLV_MIN  # 25


# --------------------------------------------------------------------------- #
# Feed assembly (deterministic given its inputs)
# --------------------------------------------------------------------------- #

#: The primary, circularity-safe headline pairing.
PRIMARY_COMPARATOR = "ex_market"
PRIMARY_METRIC = "mae"


def _segment_panel(panel_metric: Dict[str, Dict[str, float]],
                   obs_meta: Dict[str, Dict[str, object]],
                   keep) -> Dict[str, Dict[str, float]]:
    """Sub-panel of obs whose meta satisfies ``keep(meta)``."""
    return {o: row for o, row in panel_metric.items()
            if o in obs_meta and keep(obs_meta[o])}


def _fav_bucket(meta) -> Optional[str]:
    fp = meta.get("fav_prob")
    if fp is None:
        return None
    if fp >= 0.60:
        return "strong_fav (>=60%)"
    if fp >= 0.45:
        return "moderate_fav (45-60%)"
    return "open (<45%)"


def assemble_feed(
    arm_a: Dict[str, object],
    placed: Dict[str, object],
    *,
    generated: str,
    window: str,
    model_variant: str,
    freshness_s: float,
    n_boot: int = 2000,
    seed: int = vb._DEFAULT_SEED,
    history: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, object]:
    """Build the full ``venues_benchmark.json`` payload from Arm-A + Arm-B inputs.

    Pure given its inputs (``generated`` is caller-supplied so the output is
    byte-deterministic across runs). Honest states: an empty Arm A renders
    ``insufficient``; Polymarket always renders ``COLLECTING`` (no price series).
    """
    panels = arm_a["panels"]
    obs_meta = arm_a["obs_meta"]
    coverage = arm_a["coverage"]
    venues_all = coverage["venues"]

    # allow_relaxed_support: a single chronically-thin venue (a fresh addition,
    # or Polymarket before it had its own captured price series) can collapse
    # the strict all-venues intersection to zero even when the rest of the
    # field shares deep common support. Relaxing to the largest subset of
    # >=MIN_VENUES_FOR_RANKING venues with real common support (see
    # wca.venuesbench.best_common_support_subset) lets a ranking actually
    # emit instead of "insufficient" every run; the dropped venues are always
    # reported (leaderboard["venues_dropped"]) — never a silent swap — and the
    # strict "insufficient" verdict still applies if even a relaxed search
    # can't clear the minimums.
    primary_panel = panels[PRIMARY_COMPARATOR][PRIMARY_METRIC]
    leaderboard = vb.rank_venues(primary_panel, venues_all, metric=PRIMARY_METRIC,
                                 n_boot=n_boot, seed=seed, allow_relaxed_support=True)
    lobo_board = vb.rank_venues(arm_a["lobo"][PRIMARY_METRIC], venues_all,
                                metric=PRIMARY_METRIC, n_boot=n_boot, seed=seed,
                                allow_relaxed_support=True)

    # Secondary ALL-AVAILABLE ranking: each venue scored over whatever obs it has
    # a fresh quote for. NOT directly comparable across venues (different obs) —
    # it exists to show coverage, never to crown a winner.
    coverage_ranking = []
    for v in venues_all:
        vals = [row[v] for row in primary_panel.values() if v in row]
        if not vals:
            continue
        fixtures = {o.split("|", 1)[0] for o, row in primary_panel.items() if v in row}
        coverage_ranking.append({
            "venue": v, "n_obs": len(vals), "n_fixtures": len(fixtures),
            "mean_distance": round(sum(vals) / len(vals), 6),
        })
    coverage_ranking.sort(key=lambda r: r["mean_distance"])

    # Multi-metric / multi-comparator omnibus p-values (for the hypothesis table).
    hypotheses: List[Dict[str, object]] = []
    raw_p: List[Optional[float]] = []
    for comp in COMPARATORS:
        for metric in ("mae", "js"):
            sup = vb.common_support(panels[comp][metric], venues_all)
            stat, p = vb.friedman_test(panels[comp][metric], venues_all, sup)
            circular = comp == "model"
            hypotheses.append({
                "name": "venues differ vs %s (%s)" % (comp, metric),
                "comparator": comp, "metric": metric,
                "independent": not circular,
                "n_fixtures": len({o.split('|', 1)[0] for o in sup}),
                "friedman_stat": None if stat is None else round(stat, 4),
                "p": None if p is None else round(p, 6),
            })
            raw_p.append(p)
    for q, h in zip(vb.bh_fdr(raw_p), hypotheses):
        h["q_bh"] = None if q is None else round(q, 6)

    # Pre-registered segments (fixture-level cuts), BH-FDR over their Friedman p.
    seg_defs = []
    for leg in vb.LEGS:
        seg_defs.append(("outcome=%s won" % leg, lambda m, _l=leg: m.get("outcome") == _l))
    for label in ("strong_fav (>=60%)", "moderate_fav (45-60%)", "open (<45%)"):
        seg_defs.append(("favourite:%s" % label, lambda m, _lab=label: _fav_bucket(m) == _lab))
    segments = []
    seg_p: List[Optional[float]] = []
    for name, keep in seg_defs:
        sub = _segment_panel(primary_panel, obs_meta, keep)
        sup = vb.common_support(sub, venues_all)
        nfix = len({o.split('|', 1)[0] for o in sup})
        stat, p = vb.friedman_test(sub, venues_all, sup)
        leader = None
        if sup and nfix >= 1:
            board = vb.rank_venues(sub, venues_all, metric=PRIMARY_METRIC, n_boot=max(500, n_boot // 4), seed=seed)
            leader = board["venues"][0]["venue"] if board["venues"] else None
        segments.append({"segment": name, "n_obs": len(sup), "n_fixtures": nfix,
                         "friedman_p": None if p is None else round(p, 6),
                         "closest": leader,
                         "state": "ok" if nfix >= vb.MIN_COMMON_FIXTURES else "insufficient"})
        seg_p.append(p)
    for q, s in zip(vb.bh_fdr(seg_p), segments):
        s["q_bh"] = None if q is None else round(q, 6)

    feed = {
        "meta": {
            "generated": generated,
            "window": window,
            "model_variant": model_variant,
            "primary": "%s vs %s" % (PRIMARY_COMPARATOR, PRIMARY_METRIC),
            "freshness_limit_h": round(freshness_s / 3600.0, 2),
            "seed": seed,
            "n_boot": n_boot,
        },
        "coverage": coverage,
        "leaderboard": leaderboard,
        "lobo_leaderboard": lobo_board,
        "coverage_ranking": {
            "venues": coverage_ranking,
            "note": ("secondary, all-available ranking — each venue uses its own "
                     "set of fixtures, so these are NOT directly comparable; the "
                     "common-support leaderboard above is the only fair ranking"),
        },
        "accuracy": arm_a["accuracy"],
        "segments": segments,
        "hypotheses": hypotheses,
        "placed_arm": placed,
        "polymarket": {
            "state": "COLLECTING",
            "note": ("Polymarket has no captured 1X2 price series in odds_snapshots "
                     "(only dry-run orders exist), so it cannot yet be ranked as a "
                     "venue. This section fills once a prospective PM snapshotter "
                     "accrues matched-time H/D/A partitions."),
        },
        "exit_test": {
            "state": "insufficient",
            "note": ("exploratory exit rule (divergence from the closest independent "
                     "venue -> adverse executable move) requires a held-out shadow "
                     "ledger; not run live, no promotion without held-out evidence"),
        },
        "history": history or [],
    }
    if coverage["n_obs"] == 0:
        feed["leaderboard"]["verdict"] = "insufficient — no model/venue overlap in window"
    return feed

