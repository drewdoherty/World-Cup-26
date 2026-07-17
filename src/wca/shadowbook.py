"""Research-grade multi-venue shadow prediction-market book.

The book is intentionally isolated from the live ledger.  It records every
market observation, every enter/abstain decision, simulated fills, and eventual
outcomes.  Model-backed forecasts and controlled market-only exploration are
kept distinct so exploration can improve coverage without being mislabeled as
alpha.

Polymarket event rows come from ``site/forest_data.json``.  Settlement-matched
Hyperliquid/Polymarket pairs come from ``site/hl_xvenue.json``.  Cross-venue
positions fail closed on stale data or divergent settlement rules.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 1
POLICY_VERSION = "shadow-v1"


@dataclass(frozen=True)
class ShadowPolicy:
    bankroll_usd: float = 3227.0
    kelly_fraction: float = 0.25
    min_edge: float = 0.01
    max_position_usd: float = 40.0
    model_fixture_cap_usd: float = 160.0
    exploration_stake_usd: float = 1.0
    exploration_fixture_cap_usd: float = 50.0
    min_price: float = 0.02
    max_price: float = 0.98
    calibration_prior: float = 20.0
    venue_stale_seconds: int = 900
    cross_max_cost_usd: float = 100.0


DDL = """
CREATE TABLE IF NOT EXISTS shadow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    forest_generated TEXT,
    hl_generated TEXT,
    source_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts_utc TEXT NOT NULL,
    venue TEXT NOT NULL,
    fixture TEXT,
    market_key TEXT NOT NULL,
    family TEXT NOT NULL,
    selection TEXT NOT NULL,
    settlement_basis TEXT NOT NULL,
    instrument_id TEXT,
    yes_price REAL NOT NULL,
    bid REAL,
    ask REAL,
    spread REAL,
    depth REAL,
    raw_forecast REAL,
    calibrated_forecast REAL,
    forecast_source TEXT NOT NULL,
    calibration_n INTEGER NOT NULL DEFAULT 0,
    market_status TEXT NOT NULL DEFAULT 'open',
    outcome REAL,
    settled_ts TEXT
);

CREATE TABLE IF NOT EXISTS shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL,
    ts_utc TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT,
    reason TEXT NOT NULL,
    edge REAL,
    stake_usd REAL NOT NULL DEFAULT 0,
    exploration INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    observation_id INTEGER NOT NULL,
    venue TEXT NOT NULL,
    fixture TEXT,
    market_key TEXT NOT NULL,
    family TEXT NOT NULL,
    selection TEXT NOT NULL,
    settlement_basis TEXT NOT NULL,
    instrument_id TEXT,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stake_usd REAL NOT NULL,
    shares REAL NOT NULL,
    forecast_prob REAL,
    status TEXT NOT NULL DEFAULT 'open',
    settlement_value REAL,
    settled_pl REAL,
    settled_ts TEXT
);

CREATE TABLE IF NOT EXISTS shadow_cross_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts_utc TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    edge_per_share REAL,
    shares REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    expected_profit_usd REAL NOT NULL DEFAULT 0,
    settlement_tail TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    settled_pl REAL
);

CREATE INDEX IF NOT EXISTS ix_shadow_obs_family
ON shadow_observations(venue, family, forecast_source, outcome);
CREATE INDEX IF NOT EXISTS ix_shadow_pos_status
ON shadow_positions(status, fixture, family);
"""


def connect(path: str = "data/shadow_book.db") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    con.commit()
    return con


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pm_fee(price: float) -> float:
    p = min(max(float(price), 0.0), 1.0)
    return 0.03 * p * (1.0 - p)


def _source_hash(forest: Dict[str, Any], hl_feed: Dict[str, Any]) -> str:
    raw = json.dumps({"forest": forest, "hl": hl_feed}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _is_stale(value: Optional[str], now_utc: str, max_age: int) -> bool:
    stamp, now = _parse_ts(value), _parse_ts(now_utc)
    if stamp is None or now is None:
        return True
    return (now - stamp).total_seconds() > max_age


def calibrated_probability(
    con: sqlite3.Connection,
    *,
    venue: str,
    family: str,
    source: str,
    raw: float,
    prior: float,
) -> Tuple[float, int]:
    """Reliability-bin calibration learned only from already settled forecasts."""
    lo = math.floor(max(0.0, min(0.999999, raw)) * 10.0) / 10.0
    hi = lo + 0.1
    row = con.execute(
        """SELECT COUNT(*) n, SUM(outcome) wins
           FROM shadow_observations
           WHERE venue=? AND family=? AND forecast_source=?
             AND outcome IS NOT NULL AND raw_forecast>=? AND raw_forecast<?""",
        (venue, family, source, lo, hi),
    ).fetchone()
    n = int(row["n"] or 0)
    wins = float(row["wins"] or 0.0)
    calibrated = (wins + prior * raw) / (n + prior)
    return min(max(calibrated, 1e-6), 1.0 - 1e-6), n


def _quarter_kelly(bankroll: float, fraction: float, q: float, cost: float) -> float:
    if not (0.0 < cost < 1.0) or q <= cost:
        return 0.0
    raw = (q - cost) / (1.0 - cost)
    return bankroll * fraction * raw


def _deterministic_side(key: str) -> str:
    return "YES" if int(hashlib.sha256(key.encode("utf-8")).hexdigest()[-1], 16) % 2 == 0 else "NO"


def _already_open(con: sqlite3.Connection, market_key: str, side: str) -> bool:
    return con.execute(
        "SELECT 1 FROM shadow_positions WHERE market_key=? AND side=? AND status='open' LIMIT 1",
        (market_key, side),
    ).fetchone() is not None


def _insert_observation(
    con: sqlite3.Connection,
    *,
    run_id: int,
    ts_utc: str,
    venue: str,
    fixture: Optional[str],
    market_key: str,
    family: str,
    selection: str,
    settlement: str,
    instrument_id: Optional[str],
    yes_price: float,
    raw: Optional[float],
    calibrated: Optional[float],
    source: str,
    calibration_n: int,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    spread: Optional[float] = None,
    depth: Optional[float] = None,
) -> int:
    cur = con.execute(
        """INSERT INTO shadow_observations(
               run_id,ts_utc,venue,fixture,market_key,family,selection,
               settlement_basis,instrument_id,yes_price,bid,ask,spread,depth,
               raw_forecast,calibrated_forecast,forecast_source,calibration_n)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, ts_utc, venue, fixture, market_key, family, selection,
         settlement, instrument_id, yes_price, bid, ask, spread, depth,
         raw, calibrated, source, calibration_n),
    )
    return int(cur.lastrowid)


def _record_decision(
    con: sqlite3.Connection,
    *,
    observation_id: int,
    ts_utc: str,
    action: str,
    side: Optional[str],
    reason: str,
    edge: Optional[float],
    stake: float,
    exploration: bool,
    policy: ShadowPolicy,
    position: Optional[Dict[str, Any]] = None,
) -> int:
    cur = con.execute(
        """INSERT INTO shadow_decisions(
               observation_id,ts_utc,action,side,reason,edge,stake_usd,
               exploration,policy_version) VALUES(?,?,?,?,?,?,?,?,?)""",
        (observation_id, ts_utc, action, side, reason, edge, stake,
         int(exploration), POLICY_VERSION),
    )
    decision_id = int(cur.lastrowid)
    if action == "enter" and position is not None and stake > 0:
        price = float(position["price"])
        con.execute(
            """INSERT INTO shadow_positions(
                   decision_id,observation_id,venue,fixture,market_key,family,
                   selection,settlement_basis,instrument_id,side,entry_price,
                   stake_usd,shares,forecast_prob)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decision_id, observation_id, position["venue"], position.get("fixture"),
             position["market_key"], position["family"], position["selection"],
             position["settlement"], position.get("instrument_id"), side, price,
             stake, stake / price, position.get("forecast")),
        )
    return decision_id


def ingest_forest(
    con: sqlite3.Connection,
    run_id: int,
    forest: Dict[str, Any],
    ts_utc: str,
    policy: ShadowPolicy,
) -> Dict[str, int]:
    counts = {"observed": 0, "entered": 0, "explored": 0, "abstained": 0}
    for fixture in forest.get("fixtures", []):
        fixture_name = fixture.get("fixture") or "unknown fixture"
        model_used = 0.0
        exploration_used = 0.0
        for row in fixture.get("rows", []):
            if "section" in row or row.get("market") is None:
                continue
            market = float(row["market"])
            if not (0.0 < market < 1.0):
                continue
            family = str(row.get("family") or "other")
            selection = str(row.get("label") or "unknown")
            settlement = str(row.get("settlement") or "other")
            key = "%s|%s|%s|%s" % (fixture_name, family, selection, settlement)
            raw_model = row.get("model")
            source = "production_model" if raw_model is not None else "market_prior_exploration"
            raw = float(raw_model) if raw_model is not None else market
            calibrated, calib_n = calibrated_probability(
                con, venue="polymarket", family=family, source=source,
                raw=raw, prior=policy.calibration_prior)
            obs_id = _insert_observation(
                con, run_id=run_id, ts_utc=ts_utc, venue="polymarket",
                fixture=fixture_name, market_key=key, family=family,
                selection=selection, settlement=settlement,
                instrument_id=row.get("token_id"), yes_price=market,
                raw=raw, calibrated=calibrated, source=source,
                calibration_n=calib_n, spread=row.get("spread"),
            )
            counts["observed"] += 1

            if raw_model is not None:
                yes_cost = market + pm_fee(market)
                no_market = 1.0 - market
                no_cost = no_market + pm_fee(no_market)
                yes_edge = calibrated - yes_cost
                no_edge = (1.0 - calibrated) - no_cost
                side, price, q, edge = (
                    ("YES", yes_cost, calibrated, yes_edge)
                    if yes_edge >= no_edge else
                    ("NO", no_cost, 1.0 - calibrated, no_edge)
                )
                raw_stake = _quarter_kelly(
                    policy.bankroll_usd, policy.kelly_fraction, q, price)
                stake = min(
                    raw_stake,
                    policy.max_position_usd,
                    max(0.0, policy.model_fixture_cap_usd - model_used),
                )
                if edge >= policy.min_edge and stake >= 0.25 and not _already_open(con, key, side):
                    reason = "model_edge_after_fee"
                    action = "enter"
                    model_used += stake
                    counts["entered"] += 1
                elif edge >= policy.min_edge and _already_open(con, key, side):
                    reason = "already_open"
                    action, stake = "abstain", 0.0
                    counts["abstained"] += 1
                else:
                    reason = "edge_below_threshold_or_cap"
                    action, stake = "abstain", 0.0
                    counts["abstained"] += 1
                _record_decision(
                    con, observation_id=obs_id, ts_utc=ts_utc, action=action,
                    side=side, reason=reason, edge=edge, stake=stake,
                    exploration=False, policy=policy,
                    position={
                        "venue": "polymarket", "fixture": fixture_name,
                        "market_key": key, "family": family,
                        "selection": selection, "settlement": settlement,
                        "instrument_id": row.get("token_id"), "price": price,
                        "forecast": q,
                    },
                )
                continue

            # Controlled coverage experiment: every otherwise-priceable family
            # is sampled, but the decision is explicitly tagged exploration.
            if not (policy.min_price <= market <= policy.max_price):
                action, reason, stake, side = "abstain", "extreme_price", 0.0, None
                counts["abstained"] += 1
            elif exploration_used + policy.exploration_stake_usd <= policy.exploration_fixture_cap_usd:
                action, reason = "enter", "coverage_exploration_no_model"
                stake = policy.exploration_stake_usd
                side = _deterministic_side(key)
                if _already_open(con, key, side):
                    action, reason, stake = "abstain", "already_open", 0.0
                    counts["abstained"] += 1
                else:
                    exploration_used += stake
                    counts["entered"] += 1
                    counts["explored"] += 1
            else:
                action, reason, stake, side = "abstain", "exploration_fixture_cap", 0.0, None
                counts["abstained"] += 1
            chosen_price = market if side != "NO" else 1.0 - market
            chosen_q = calibrated if side != "NO" else 1.0 - calibrated
            _record_decision(
                con, observation_id=obs_id, ts_utc=ts_utc, action=action,
                side=side, reason=reason, edge=None, stake=stake,
                exploration=True, policy=policy,
                position={
                    "venue": "polymarket", "fixture": fixture_name,
                    "market_key": key, "family": family,
                    "selection": selection, "settlement": settlement,
                    "instrument_id": row.get("token_id"),
                    "price": chosen_price + pm_fee(chosen_price),
                    "forecast": chosen_q,
                },
            )
    return counts


def ingest_hyperliquid(
    con: sqlite3.Connection,
    run_id: int,
    hl_feed: Dict[str, Any],
    ts_utc: str,
    policy: ShadowPolicy,
) -> Dict[str, int]:
    counts = {"pairs": 0, "cross_entered": 0, "independent_entered": 0, "abstained": 0}
    generated = hl_feed.get("generated_at")
    stale = _is_stale(generated, ts_utc, policy.venue_stale_seconds)
    for pair in hl_feed.get("pairs", []):
        counts["pairs"] += 1
        pair_id = str(pair.get("pair_id") or "unknown")
        for direction, detail in (pair.get("directions") or {}).items():
            tail = detail.get("settlement_tail") or {}
            executable = detail.get("executable") or {}
            edge = detail.get("edge_per_share_at_best")
            shares = float(executable.get("shares") or 0.0)
            cost = float(executable.get("cost_usd") or 0.0)
            profit = float(executable.get("profit_usd") or 0.0)
            if stale:
                action, reason = "abstain", "stale_cross_venue_snapshot"
            elif tail.get("gated"):
                action, reason = "abstain", "settlement_tail_gated"
            elif con.execute(
                    "SELECT 1 FROM shadow_cross_decisions WHERE pair_id=? AND direction=? AND action='enter' AND status='open' LIMIT 1",
                    (pair_id, direction)).fetchone() is not None:
                action, reason = "abstain", "already_open"
            elif edge is not None and float(edge) > 0 and shares > 0 and cost > 0:
                action, reason = "enter", "fee_surviving_cross_venue"
                if cost > policy.cross_max_cost_usd:
                    scale = policy.cross_max_cost_usd / cost
                    shares, profit, cost = shares * scale, profit * scale, policy.cross_max_cost_usd
                counts["cross_entered"] += 1
            else:
                action, reason = "abstain", "no_executable_cross_edge"
            if action == "abstain":
                counts["abstained"] += 1
            con.execute(
                """INSERT INTO shadow_cross_decisions(
                       run_id,ts_utc,pair_id,direction,action,reason,
                       edge_per_share,shares,cost_usd,expected_profit_usd,
                       settlement_tail) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, ts_utc, pair_id, direction, action, reason, edge,
                 shares if action == "enter" else 0.0,
                 cost if action == "enter" else 0.0,
                 profit if action == "enter" else 0.0,
                 tail.get("tail")),
            )

        # Independent relative-value experiments: use the opposite venue's
        # midpoint as a forecast, but only when the snapshot is fresh.
        hl, pm = pair.get("hl") or {}, pair.get("pm") or {}
        hb, ha, pb, pa = hl.get("yes_bid"), hl.get("yes_ask"), pm.get("yes_bid"), pm.get("yes_ask")
        if stale or any(v is None for v in (hb, ha, pb, pa)):
            continue
        hl_mid, pm_mid = (float(hb) + float(ha)) / 2.0, (float(pb) + float(pa)) / 2.0
        for venue, price, forecast, source, instrument in (
            ("hyperliquid", float(ha), pm_mid, "cross_venue_pm", str(hl.get("outcome_id") or "")),
            ("polymarket", float(pa), hl_mid, "cross_venue_hl", str(pm.get("token_yes") or "")),
        ):
            family = "hl_%s" % str(pair.get("kind") or "outcome")
            calibrated, calib_n = calibrated_probability(
                con, venue=venue, family=family, source=source,
                raw=forecast, prior=policy.calibration_prior)
            key = "%s|%s|YES" % (pair_id, venue)
            obs_id = _insert_observation(
                con, run_id=run_id, ts_utc=ts_utc, venue=venue, fixture=None,
                market_key=key, family=family, selection=pair_id,
                settlement="cross_venue_basis", instrument_id=instrument,
                yes_price=price, raw=forecast, calibrated=calibrated,
                source=source, calibration_n=calib_n,
                bid=float(hb) if venue == "hyperliquid" else float(pb),
                ask=price, spread=(float(ha) - float(hb)) if venue == "hyperliquid" else (float(pa) - float(pb)),
            )
            edge = calibrated - price
            stake = min(policy.exploration_stake_usd, policy.max_position_usd)
            action = "enter" if edge >= policy.min_edge and not _already_open(con, key, "YES") else "abstain"
            if edge >= policy.min_edge and action == "abstain":
                reason = "already_open"
            else:
                reason = "independent_cross_venue_signal" if action == "enter" else "relative_edge_below_threshold"
            if action == "enter":
                counts["independent_entered"] += 1
            else:
                counts["abstained"] += 1
                stake = 0.0
            _record_decision(
                con, observation_id=obs_id, ts_utc=ts_utc, action=action,
                side="YES", reason=reason, edge=edge, stake=stake,
                exploration=True, policy=policy,
                position={
                    "venue": venue, "fixture": None, "market_key": key,
                    "family": family, "selection": pair_id,
                    "settlement": "cross_venue_basis", "instrument_id": instrument,
                    "price": price, "forecast": calibrated,
                },
            )
    return counts


def run_cycle(
    con: sqlite3.Connection,
    *,
    forest: Dict[str, Any],
    hl_feed: Optional[Dict[str, Any]] = None,
    ts_utc: Optional[str] = None,
    policy: Optional[ShadowPolicy] = None,
) -> Dict[str, Any]:
    policy = policy or ShadowPolicy()
    ts_utc = ts_utc or utc_now()
    hl_feed = hl_feed or {}
    source_hash = _source_hash(forest, hl_feed)
    cur = con.execute(
        """INSERT INTO shadow_runs(ts_utc,policy_version,policy_json,
               forest_generated,hl_generated,source_hash) VALUES(?,?,?,?,?,?)""",
        (ts_utc, POLICY_VERSION, json.dumps(asdict(policy), sort_keys=True),
         (forest.get("meta") or {}).get("generated"), hl_feed.get("generated_at"), source_hash),
    )
    run_id = int(cur.lastrowid)
    pm_counts = ingest_forest(con, run_id, forest, ts_utc, policy)
    hl_counts = ingest_hyperliquid(con, run_id, hl_feed, ts_utc, policy)
    con.commit()
    return {"run_id": run_id, "ts_utc": ts_utc, "polymarket": pm_counts, "hyperliquid": hl_counts}


def settle_market(
    con: sqlite3.Connection,
    market_key: str,
    outcome: float,
    *,
    ts_utc: Optional[str] = None,
) -> int:
    """Settle every observation/position for a canonical binary market key."""
    ts_utc = ts_utc or utc_now()
    value = float(outcome)
    if value not in (0.0, 0.5, 1.0):
        raise ValueError("outcome must be 0, 0.5, or 1")
    obs = con.execute(
        "SELECT id FROM shadow_observations WHERE market_key=? AND outcome IS NULL",
        (market_key,),
    ).fetchall()
    con.execute(
        "UPDATE shadow_observations SET outcome=?,settled_ts=?,market_status='settled' "
        "WHERE market_key=? AND outcome IS NULL",
        (value, ts_utc, market_key),
    )
    positions = con.execute(
        "SELECT * FROM shadow_positions WHERE market_key=? AND status='open'",
        (market_key,),
    ).fetchall()
    for p in positions:
        side_value = value if p["side"] == "YES" else 1.0 - value
        pl = float(p["shares"]) * side_value - float(p["stake_usd"])
        con.execute(
            "UPDATE shadow_positions SET status='settled',settlement_value=?,settled_pl=?,settled_ts=? WHERE id=?",
            (side_value, pl, ts_utc, p["id"]),
        )
    con.commit()
    return len(obs)


def resolve_fixture_observation(row: Dict[str, Any], result: Dict[str, Any]) -> Optional[float]:
    """Resolve one forest observation from a structured match-event result.

    Returns the YES/outcome value (0/1), or ``None`` when the supplied result
    lacks the event needed to settle that family. Ambiguous labels remain open.
    """
    family = str(row.get("family") or "")
    label = str(row.get("selection") or "")
    fixture = str(row.get("fixture") or "")
    if " vs " not in fixture:
        return None
    home, away = fixture.split(" vs ", 1)
    try:
        hg, ag = int(result["home_goals_90"]), int(result["away_goals_90"])
    except (KeyError, TypeError, ValueError):
        return None

    def over_line(text: str, value: float) -> Optional[float]:
        m = re.search(r"(?:O/U|Over)\s+(\d+(?:\.\d+)?)", text, re.I)
        return None if not m else float(value > float(m.group(1)))

    if family == "1x2":
        if label.lower() == "draw":
            return float(hg == ag)
        if label == home:
            return float(hg > ag)
        if label == away:
            return float(ag > hg)
    if family == "total_goals":
        return over_line(label, hg + ag)
    if family == "btts":
        return float(hg > 0 and ag > 0)
    if family == "spread":
        m = re.search(r"^(.*?)\s+-([\d.]+)", label)
        if not m:
            return None
        margin = (hg - ag) if m.group(1).strip() == home else (ag - hg)
        return float(margin > float(m.group(2)))
    if family == "team_total":
        team_goals = hg if label.startswith(home + " ") else ag if label.startswith(away + " ") else None
        return None if team_goals is None else over_line(label, team_goals)
    if family == "exact_score":
        if label == "Any Other Score":
            return None
        m = re.fullmatch(r"(\d+)-(\d+)", label)
        return None if not m else float((hg, ag) == (int(m.group(1)), int(m.group(2))))
    if family == "scorer_prop":
        player = re.sub(r"\s+anytime$", "", label, flags=re.I)
        scorers = result.get("scorers") or {}
        return float(int(scorers.get(player, 0)) > 0) if player in scorers else None
    if family == "extra_time":
        value = result.get("went_extra_time")
        return None if value is None else float(bool(value))
    if family == "advance":
        advanced = result.get("advanced")
        if advanced is None:
            return None
        return float(str(advanced).lower() in label.lower())
    if family == "penalty_shootout":
        value = result.get("penalty_shootout")
        return None if value is None else float(bool(value))
    if family in ("halftime_result", "second_half_result"):
        key = "first_half_score" if family == "halftime_result" else "second_half_score"
        score = result.get(key)
        if not isinstance(score, (list, tuple)) or len(score) != 2:
            return None
        h, a = int(score[0]), int(score[1])
        if label.lower() == "draw":
            return float(h == a)
        return float(h > a) if label == home else float(a > h) if label == away else None
    if family == "first_to_score":
        first = result.get("first_team_to_score")
        if first is None:
            return None
        return float(str(first).lower() == label.lower())
    if family == "corners":
        if "Odd or Even" in label or "Team to Take First Corner" in label:
            return None  # the forest label omits which outcome token this row represents
        if label.startswith("1st Half"):
            value = result.get("first_half_corners")
        elif label.startswith("2nd Half"):
            value = result.get("second_half_corners")
        else:
            value = result.get("total_corners")
        return None if value is None else over_line(label, float(value))
    if family == "half_market":
        first = label.startswith("1st Half") or " 1st Half " in label
        second = label.startswith("2nd Half") or " 2nd Half " in label
        score = result.get("first_half_score" if first else "second_half_score" if second else "")
        if not isinstance(score, (list, tuple)) or len(score) != 2:
            return None
        h, a = int(score[0]), int(score[1])
        if "Both Teams to Score" in label:
            return float(h > 0 and a > 0)
        if label.startswith(home + " ") or ("%s " % home) in label:
            return over_line(label, h)
        if label.startswith(away + " ") or ("%s " % away) in label:
            return over_line(label, a)
        return over_line(label, h + a)
    return None


def settle_fixture(
    con: sqlite3.Connection,
    result: Dict[str, Any],
    *,
    ts_utc: Optional[str] = None,
) -> Dict[str, int]:
    """Resolve every supported observation for one structured fixture result."""
    fixture = str(result.get("fixture") or "")
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM shadow_observations WHERE fixture=? AND outcome IS NULL",
        (fixture,),
    )]
    keys: Dict[str, float] = {}
    unresolved = 0
    for row in rows:
        outcome = resolve_fixture_observation(row, result)
        if outcome is None:
            unresolved += 1
        else:
            keys[row["market_key"]] = outcome
    settled = 0
    for key, outcome in keys.items():
        settled += settle_market(con, key, outcome, ts_utc=ts_utc)
    return {"settled_observations": settled, "unresolved_observations": unresolved,
            "settled_markets": len(keys)}


def _metric_rows(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = con.execute(
        """SELECT venue,family,forecast_source,COUNT(*) n,
                  AVG((calibrated_forecast-outcome)*(calibrated_forecast-outcome)) brier,
                  AVG(-(outcome*LOG(MAX(MIN(calibrated_forecast,0.999999),0.000001))
                        +(1-outcome)*LOG(MAX(MIN(1-calibrated_forecast,0.999999),0.000001)))) log_loss,
                  AVG(outcome) actual_rate,AVG(calibrated_forecast) forecast_rate
           FROM shadow_observations
           WHERE outcome IS NOT NULL AND calibrated_forecast IS NOT NULL
           GROUP BY venue,family,forecast_source
           ORDER BY venue,family,forecast_source"""
    ).fetchall()
    return [dict(r) for r in rows]


def _decision_metrics(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [dict(r) for r in con.execute(
        """SELECT d.action,d.reason,o.venue,o.family,COUNT(*) n,
                  AVG((o.calibrated_forecast-o.outcome)*(o.calibrated_forecast-o.outcome)) brier,
                  AVG(o.outcome) actual_rate,AVG(o.calibrated_forecast) forecast_rate
           FROM shadow_decisions d JOIN shadow_observations o ON o.id=d.observation_id
           WHERE o.outcome IS NOT NULL AND o.calibrated_forecast IS NOT NULL
           GROUP BY d.action,d.reason,o.venue,o.family
           ORDER BY d.action,d.reason,o.venue,o.family"""
    )]


def report(con: sqlite3.Connection) -> Dict[str, Any]:
    counts = dict(con.execute(
        "SELECT action,COUNT(*) n FROM shadow_decisions GROUP BY action"
    ).fetchall())
    pos = con.execute(
        """SELECT COUNT(*) n, SUM(CASE WHEN status='open' THEN stake_usd ELSE 0 END) open_stake,
                  SUM(COALESCE(settled_pl,0)) settled_pl,
                  SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) n_settled
           FROM shadow_positions"""
    ).fetchone()
    by_family = [dict(r) for r in con.execute(
        """SELECT family,venue,COUNT(*) n,
                  SUM(CASE WHEN status='open' THEN stake_usd ELSE 0 END) open_stake,
                  SUM(COALESCE(settled_pl,0)) settled_pl
           FROM shadow_positions GROUP BY family,venue ORDER BY family,venue"""
    )]
    latest = con.execute("SELECT * FROM shadow_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "latest_run": dict(latest) if latest else None,
        "summary": {
            "observations": con.execute("SELECT COUNT(*) FROM shadow_observations").fetchone()[0],
            "decisions": sum(int(v) for v in counts.values()),
            "entered": int(counts.get("enter", 0)),
            "abstained": int(counts.get("abstain", 0)),
            "positions": int(pos["n"] or 0),
            "open_stake_usd": float(pos["open_stake"] or 0.0),
            "settled_pl_usd": float(pos["settled_pl"] or 0.0),
            "settled_positions": int(pos["n_settled"] or 0),
        },
        "by_family": by_family,
        "calibration": _metric_rows(con),
        "decision_metrics": _decision_metrics(con),
        "open_positions": [dict(r) for r in con.execute(
            "SELECT * FROM shadow_positions WHERE status='open' ORDER BY fixture,family,id LIMIT 500"
        )],
        "recent_decisions": [dict(r) for r in con.execute(
            """SELECT d.*,o.venue,o.fixture,o.family,o.selection,o.yes_price,
                      o.calibrated_forecast,o.forecast_source
               FROM shadow_decisions d JOIN shadow_observations o ON o.id=d.observation_id
               ORDER BY d.id DESC LIMIT 500"""
        )],
        "cross_venue": [dict(r) for r in con.execute(
            "SELECT * FROM shadow_cross_decisions ORDER BY id DESC LIMIT 200"
        )],
    }
