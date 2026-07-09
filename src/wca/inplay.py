"""In-play Polymarket monitor core — state reconciliation, settlement-lag
detectors, sizing, dedupe, relays and the session log.

THE MONITOR NEVER PLACES OR FIRES ORDERS. This module is structurally
incapable of order placement: it never imports ``wca.pm.trader``,
``wca.pm.signing`` or ``wca.pm.relayer`` and contains no order-POST path.
Everything it produces is either a Telegram ping or a *parked* ``PM-<n>``
proposal that only the existing human ``Y PM-<n>`` reply flow (behind
``PM_DRY_RUN``) can fire, on the mini.

Relay design (chosen after investigating the real topology, 2026-07-09)
-----------------------------------------------------------------------
The bot + ``pm_parked`` + trader live on the Mac mini (``data/wca.db``);
Polymarket is reachable ONLY from the MacBook and only while NordVPN is up —
and while the VPN is up the MacBook cannot SSH the mini (full tunnel). So the
relay is pluggable, probed at runtime, best-first:

1. **SSH relay** (lowest latency, seconds): if
   ``ssh -o BatchMode=yes -o ConnectTimeout=3 <mini> true`` succeeds (VPN off /
   split-tunnel), the proposal is parked by executing the mini's own
   ``scripts/wca_pm_inplay_ingest.py --park-json`` over SSH. The mini allocates
   the ``PM-<n>`` and the number is returned synchronously, so the immediate
   ping already carries the fireable tag.
2. **Git artifact relay** (fallback, ~≤6 min to fireable): append the proposal
   to the git-committed ``data/pm_inplay_proposals.json`` via a dedicated
   detached worktree pinned to ``origin/main`` (never touches the operator's
   checkout or branch), commit + push ``HEAD:main``. The mini autopulls within
   5 min and the mini-side ingest (``pminplayingest`` launchd job, 60 s; the
   ``pmpropose`` cycle is a backstop) parks it and DMs the fireable ``PM-<n>``.
   The MacBook ping still goes out IMMEDIATELY, with the caveat line
   "fireable after mini sync (~≤6min)".

Either way the Telegram ping is sent from the MacBook the moment the
opportunity is detected; only the *park* travels via the relay.

Match state (two sources, reconciled, never fabricated)
-------------------------------------------------------
* **Scores feed** — ``data/live_scores.json`` (schema in
  :func:`load_feed_scores`), freshness-gated. The repo has no automated
  in-game score feed today (``site/scores_data.json`` is a pre-game
  model-vs-market feed; ``results.csv`` lags days), so this file is written by
  whatever scorer is available on the day (manual/analyst entry counts).
* **PM-implied** — quotes near 0/1 on BTTS / totals / exact-score markets
  imply propositions ("both scored", "3+ goals"); a >=10c 1X2 jump implies
  *an event happened* (reusing PR #179's ``detect_jump``). PM-implied state is
  a set of labelled propositions, NEVER a fabricated scoreline.

On conflict the scores feed wins, the conflict is logged and shown on the
ping. Opportunities are only *parked* when the scores feed (co-)confirms the
proposition — a PM-only-implied proposition means the quote already moved and
there is nothing stale to buy.

Sizing: ¼-Kelly of the PM pool via :mod:`wca.markets.bankroll`, hard-capped at
``min(existing per-order caps, INPLAY_SAFETY_CAP_USD)`` and by walked book
depth. Settlement basis is flagged on every surface (1X2/BTTS/totals/exact =
90-minute; advancement rungs = ET+pens).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants (documented knobs — the safety cap is a HUMAN-APPROVED constant)
# ---------------------------------------------------------------------------

#: HARD in-play per-order safety cap (USD). New constant for the in-play
#: monitor: in-game books are thin and fast; a fat-fingered size must not be
#: able to ride a settlement-lag ping. The effective cap is
#: ``min(INPLAY_SAFETY_CAP_USD, trader per-order cap $160)`` — the trader's own
#: static fail-closed caps ($160/order, $1,000/day) are STILL enforced at fire
#: time on the mini; this is an additional, tighter ceiling applied at
#: proposal build time. Changing it is a human-approved code change.
INPLAY_SAFETY_CAP_USD = 100.0

#: Below this walked-executable notional an opportunity is not worth a ping.
MIN_EXECUTABLE_USD = 25.0

#: Worst-case Polymarket fee rate: 0.03 * p * (1-p) of notional, where charged.
FEE_RATE = 0.03

#: Minimum edge AFTER fee (per $1 of settlement value) to surface a
#: settlement-lagged quote. 91c ask on a settled-$1 BTTS clears this easily.
MIN_EDGE_AFTER_FEE = 0.02

#: A live window runs from scheduled kickoff to +130 min wall-clock (mirrors
#: wca.pollsched.PollPolicy.match_duration_minutes).
LIVE_WINDOW_MINUTES = 130

#: Scores-feed entries older than this are stale — treated as absent.
FEED_FRESH_SECS = 300.0

#: PM mid >= this implies a proposition is priced TRUE; <= (1-this) FALSE.
PM_IMPLIED_TRUE = 0.97

#: Ladder-lag trigger: >=10c move on a 1X2 token (PR #179 harness constant).
LADDER_JUMP_THRESHOLD = 0.10

#: A rung quote within this band of its pre-jump reference is "stale".
LADDER_STALE_BAND = 0.02

#: Historical ladder-lag drift estimate used ONLY as a labelled estimate on
#: ladder-lag pings: +1.9c mean drift after a >=10c 1X2 jump (measured on the
#: historical tape, n=302 — see scripts/wca_ladderlag_papertest.py). It is an
#: estimate from a prior study, not a live computation; every surface that
#: shows it labels it "hist est (n=302)".
LADDER_HIST_DRIFT = 0.019
LADDER_HIST_N = 302

#: Fixed stake for ladder-lag parks (no state-determined fair value exists, so
#: no Kelly sizing; the class is paper-tested, not settled-certain).
LADDER_STAKE_USD = 25.0

#: Goal-lag informational ping: a goal in the feed with the scoring team's 1X2
#: mid having moved less than this since pre-goal is flagged (ping only —
#: fair value in-play is model-dependent, so nothing is parked).
GOAL_LAG_MIN_MOVE = 0.03

MINI_SSH_HOST = "andrewdoherty@Drews-Mac-mini.local"
MINI_REPO = "~/World-Cup-26"

PROPOSALS_PATH = "data/pm_inplay_proposals.json"
SESSION_LOG_PATH = "data/pm_inplay_log.jsonl"
FEED_SCORES_PATH = "data/live_scores.json"

#: Settlement basis per market kind — NEVER visually confusable (charter):
#: 1X2/BTTS/totals/exact settle at 90 minutes; advancement includes ET+pens.
SETTLEMENT_BASIS = {
    "1x2": "90-min",
    "btts": "90-min",
    "total": "90-min",
    "exact": "90-min",
    "ladder": "ET+pens",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fee / edge / depth math (pure — unit tested)
# ---------------------------------------------------------------------------


def fee(price: float) -> float:
    """Worst-case fee as a fraction of $1 settlement: ``0.03 * p * (1-p)``."""
    p = max(0.0, min(1.0, float(price)))
    return FEE_RATE * p * (1.0 - p)


def edge_after_fee(fair: float, price: float) -> float:
    """Executable edge per share after the worst-case fee, for a BUY at
    ``price`` of an outcome whose state-determined fair value is ``fair``."""
    return float(fair) - float(price) - fee(price)


@dataclass
class BookLevel:
    price: float
    size: float


def parse_book(payload: Optional[dict]) -> Tuple[List[BookLevel], List[BookLevel]]:
    """Raw CLOB ``/book`` payload -> (bids desc, asks asc). Tolerant of junk."""
    if not payload:
        return [], []

    def _side(side: str) -> List[BookLevel]:
        out = []
        for lv in payload.get(side) or []:
            try:
                out.append(BookLevel(price=float(lv["price"]), size=float(lv["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    bids = sorted(_side("bids"), key=lambda l: l.price, reverse=True)
    asks = sorted(_side("asks"), key=lambda l: l.price)
    return bids, asks


def walk_executable(
    asks: Sequence[BookLevel],
    fair: float,
    *,
    min_edge: float = MIN_EDGE_AFTER_FEE,
    cap_usd: float = INPLAY_SAFETY_CAP_USD,
) -> Dict[str, float]:
    """Walk ``asks`` (ascending), taking ONLY levels that still clear
    ``min_edge`` after fee vs ``fair``, up to ``cap_usd`` notional.

    Never assumes size: the executable quantity is what the actual book shows.
    Returns ``{avg_price, shares, notional, edge}`` where ``edge`` is the
    after-fee edge at the volume-weighted fill price (0-all when nothing
    executable).
    """
    remaining = float(cap_usd)
    shares = 0.0
    cost = 0.0
    for lvl in asks:
        if remaining <= 1e-9:
            break
        if lvl.price is None or lvl.price <= 0.0 or lvl.price >= 1.0:
            continue
        if edge_after_fee(fair, lvl.price) < min_edge:
            break  # asks are ascending — deeper levels are worse
        take_notional = min(remaining, lvl.price * lvl.size)
        shares += take_notional / lvl.price
        cost += take_notional
        remaining -= take_notional
    if shares <= 0.0:
        return {"avg_price": 0.0, "shares": 0.0, "notional": 0.0, "edge": 0.0}
    avg = cost / shares
    return {
        "avg_price": avg,
        "shares": shares,
        "notional": cost,
        "edge": edge_after_fee(fair, avg),
    }


# ---------------------------------------------------------------------------
# Sizing (¼-Kelly of the PM pool, capped)
# ---------------------------------------------------------------------------


def size_stake_usd(fair: float, price: float, *, bankroll_usd: Optional[float] = None) -> float:
    """¼-Kelly stake for belief ``fair`` at ``price``, hard-capped in-play.

    Uses :mod:`wca.markets.bankroll` (the single source of truth). Realised
    P&L is NOT readable from the MacBook mid-match (canonical ledger lives on
    the mini), so the base bankroll is used — conservative when the book is up.
    The result is additionally capped at :data:`INPLAY_SAFETY_CAP_USD`; the
    trader's own $160/order + $1,000/day caps still apply at fire time.
    """
    from wca.markets import bankroll as bk

    br = bk.pm_bankroll_usd(0.0) if bankroll_usd is None else float(bankroll_usd)
    placement = bk.size_placement(fair, price, br)
    return min(float(placement["stake"]), INPLAY_SAFETY_CAP_USD)


# ---------------------------------------------------------------------------
# Match state — scores feed + PM-implied, reconciled
# ---------------------------------------------------------------------------


@dataclass
class FeedScore:
    """One entry of the external live-scores feed (source of truth when fresh)."""

    home_goals: int
    away_goals: int
    minute: Optional[int] = None
    status: str = "live"  # live | ht | ft
    ts_utc: str = ""
    red_cards: Dict[str, int] = field(default_factory=dict)  # side -> count

    @property
    def total_goals(self) -> int:
        return self.home_goals + self.away_goals

    @property
    def scoreline(self) -> str:
        return "%d-%d" % (self.home_goals, self.away_goals)


def load_feed_scores(
    path: str = FEED_SCORES_PATH,
    *,
    now_ts: Optional[float] = None,
    fresh_secs: float = FEED_FRESH_SECS,
) -> Dict[str, FeedScore]:
    """Load ``data/live_scores.json`` -> ``{"Home vs Away": FeedScore}``.

    Schema (one object per fixture, any writer — script or human — may
    maintain it during a match)::

        {"France vs Morocco": {"home_goals": 2, "away_goals": 1,
                               "minute": 62, "status": "live",
                               "ts_utc": "2026-07-09T20:58:00Z",
                               "red_cards": {"home": 0, "away": 1}}}

    Entries with ``ts_utc`` older than ``fresh_secs`` are DROPPED (stale feed
    is treated as no feed — never act on an old scoreline). A missing file
    returns ``{}``.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    now = time.time() if now_ts is None else now_ts
    out: Dict[str, FeedScore] = {}
    for match_key, d in (raw or {}).items():
        if not isinstance(d, dict):
            continue
        ts = str(d.get("ts_utc") or "")
        try:
            age = now - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            age = float("inf")
        if age > fresh_secs:
            continue
        try:
            out[str(match_key)] = FeedScore(
                home_goals=int(d["home_goals"]),
                away_goals=int(d["away_goals"]),
                minute=(int(d["minute"]) if d.get("minute") is not None else None),
                status=str(d.get("status") or "live").lower(),
                ts_utc=ts,
                red_cards=dict(d.get("red_cards") or {}),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


@dataclass
class Proposition:
    """One reconciled boolean proposition with per-source attribution."""

    value: bool
    source: str  # "feed" | "pm" | "feed+pm" | "conflict"
    detail: str = ""


@dataclass
class MatchState:
    """Reconciled per-match in-play state. ``feed`` is None when the scores
    feed is absent/stale; ``score`` is NEVER synthesised from prices."""

    match_key: str
    feed: Optional[FeedScore]
    pm_mids: Dict[str, Optional[float]] = field(default_factory=dict)
    both_scored: Optional[Proposition] = None
    goals_ge: Dict[float, Proposition] = field(default_factory=dict)
    conflicts: List[str] = field(default_factory=list)

    def state_sig(self) -> str:
        """Signature of the observable state — the dedupe unit. Changes when
        the feed scoreline/status changes (one ping per state-change)."""
        if self.feed is not None:
            return "%s@%s" % (self.feed.scoreline, self.feed.status)
        pm = ",".join(
            "%s=%s" % (k, ("T" if p.value else "F"))
            for k, p in sorted(self.goals_ge.items())
        )
        return "pm[%s|%s]" % (
            "T" if (self.both_scored and self.both_scored.value) else "F", pm,
        )


def _implied(mid: Optional[float]) -> Optional[bool]:
    """PM mid -> implied truth of the proposition (None in the grey zone)."""
    if mid is None:
        return None
    if mid >= PM_IMPLIED_TRUE:
        return True
    if mid <= 1.0 - PM_IMPLIED_TRUE:
        return False
    return None


def _reconcile(feed_val: Optional[bool], pm_val: Optional[bool], label: str,
               conflicts: List[str]) -> Optional[Proposition]:
    """Merge one proposition from the two sources; feed wins on conflict."""
    if feed_val is None and pm_val is None:
        return None
    if feed_val is None:
        return Proposition(bool(pm_val), "pm")
    if pm_val is None or pm_val == feed_val:
        src = "feed" if pm_val is None else "feed+pm"
        return Proposition(bool(feed_val), src)
    conflicts.append(
        "%s: feed says %s but PM prices imply %s — using feed" % (label, feed_val, pm_val)
    )
    return Proposition(bool(feed_val), "conflict")


def reconcile_state(
    match_key: str,
    feed: Optional[FeedScore],
    *,
    btts_yes_mid: Optional[float] = None,
    over_mids: Optional[Dict[float, Optional[float]]] = None,
) -> MatchState:
    """Build the reconciled :class:`MatchState` from the scores feed and
    PM-implied signals. Labels which source said what; on conflict the feed is
    preferred and the conflict is recorded. NEVER fabricates a scoreline —
    PM-implied signals stay boolean propositions.
    """
    conflicts: List[str] = []
    st = MatchState(match_key=match_key, feed=feed)
    st.pm_mids["btts_yes"] = btts_yes_mid

    feed_both = None
    if feed is not None:
        feed_both = feed.home_goals >= 1 and feed.away_goals >= 1
    st.both_scored = _reconcile(feed_both, _implied(btts_yes_mid), "both_scored", conflicts)

    for line, mid in sorted((over_mids or {}).items()):
        st.pm_mids["over_%.1f" % line] = mid
        feed_over = None if feed is None else (feed.total_goals > line)
        prop = _reconcile(feed_over, _implied(mid), "goals>%.1f" % line, conflicts)
        if prop is not None:
            st.goals_ge[line] = prop

    st.conflicts = conflicts
    return st


def feed_confirmed(prop: Optional[Proposition]) -> bool:
    """True when the proposition is TRUE and the scores feed (co-)confirms it.

    Parks require this: a PM-only-implied TRUE means the quote already moved
    (nothing stale to buy) and acting on it alone would be circular.
    """
    return bool(prop and prop.value and prop.source in ("feed", "feed+pm", "conflict"))


# ---------------------------------------------------------------------------
# Market/token map
# ---------------------------------------------------------------------------


@dataclass
class MarketToken:
    """One tradeable in-play market for a live match.

    ``kind``: ``1x2`` (team win / draw), ``btts``, ``total`` (over line),
    ``exact`` (one scoreline), ``ladder`` (same-team advancement rung).
    ``yes_token`` / ``no_token`` are CLOB token ids (``no_token`` may be "").
    ``line`` is the totals line; ``score`` the (home, away) of an exact row;
    ``team`` the backing team for 1x2/ladder.
    """

    kind: str
    question: str
    yes_token: str
    no_token: str = ""
    line: Optional[float] = None
    score: Optional[Tuple[int, int]] = None
    team: Optional[str] = None
    neg_risk: bool = False
    rung: str = ""  # ladder rung category, e.g. advancement_sf

    @property
    def settlement(self) -> str:
        return SETTLEMENT_BASIS.get(self.kind, "90-min")


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------


@dataclass
class Opportunity:
    """One actionable settlement-lag (or ladder-lag) trade idea."""

    uid: str
    match_key: str
    detector: str
    market: MarketToken
    token_id: str          # the token actually bought (YES, or NO for exact-impossible)
    outcome: str           # "Yes" / "No"
    fair: Optional[float]  # state-determined fair value (None for ladder-lag)
    price: float           # walked average fill price
    best_ask: Optional[float]
    shares: float
    notional_usd: float    # walked executable notional at/under the cap
    stake_usd: float       # final sized stake (Kelly/cap/depth)
    edge: float            # after-fee edge per share (est for ladder-lag)
    edge_is_estimate: bool
    reason: str            # human trigger line, e.g. "2-1 62' — BTTS-Yes ask 91c"
    state_sources: str
    created_utc: str = field(default_factory=now_iso)

    def dedupe_key(self, state_sig: str) -> str:
        return "%s:%s:%s:%s" % (self.detector, self.match_key, self.token_id, state_sig)


def _state_reason(state: MatchState) -> str:
    if state.feed is not None:
        minute = ("%d'" % state.feed.minute) if state.feed.minute is not None else state.feed.status.upper()
        return "%s %s [feed]" % (state.feed.scoreline, minute)
    return "PM-implied state (no fresh scores feed)"


def _mk_opportunity(
    *,
    match_key: str,
    detector: str,
    market: MarketToken,
    token_id: str,
    outcome: str,
    fair: float,
    fill: Dict[str, float],
    best_ask: Optional[float],
    state: MatchState,
    reason_market: str,
    bankroll_usd: Optional[float] = None,
) -> Optional[Opportunity]:
    """Common tail of the settled-fair detectors: threshold, size, package."""
    if fill["shares"] <= 0.0 or fill["notional"] < MIN_EXECUTABLE_USD - 1e-6:
        return None
    stake = size_stake_usd(fair, fill["avg_price"], bankroll_usd=bankroll_usd)
    stake = min(stake, fill["notional"])
    if stake < MIN_EXECUTABLE_USD - 1e-6:
        return None
    src = "feed"
    if detector == "btts_settled" and state.both_scored is not None:
        src = state.both_scored.source
    elif detector == "ou_over_settled" and market.line in state.goals_ge:
        src = state.goals_ge[market.line].source
    return Opportunity(
        uid=uuid.uuid4().hex[:12],
        match_key=match_key,
        detector=detector,
        market=market,
        token_id=token_id,
        outcome=outcome,
        fair=fair,
        price=fill["avg_price"],
        best_ask=best_ask,
        shares=fill["shares"],
        notional_usd=fill["notional"],
        stake_usd=stake,
        edge=fill["edge"],
        edge_is_estimate=False,
        reason="%s — %s" % (_state_reason(state), reason_market),
        state_sources=src,
    )


def detect_btts(
    state: MatchState,
    market: MarketToken,
    book: Optional[dict],
    *,
    bankroll_usd: Optional[float] = None,
) -> Optional[Opportunity]:
    """Both teams have scored (feed-confirmed) -> BTTS-Yes settles $1; buy any
    ask that still clears the after-fee edge threshold."""
    if market.kind != "btts" or not feed_confirmed(state.both_scored):
        return None
    _, asks = parse_book(book)
    fill = walk_executable(asks, 1.0)
    best = asks[0].price if asks else None
    reason = "BTTS-Yes ask %s, settles $1 (both scored)" % _cents(best)
    return _mk_opportunity(
        match_key=state.match_key, detector="btts_settled", market=market,
        token_id=market.yes_token, outcome="Yes", fair=1.0, fill=fill,
        best_ask=best, state=state, reason_market=reason,
        bankroll_usd=bankroll_usd,
    )


def detect_ou_over(
    state: MatchState,
    market: MarketToken,
    book: Optional[dict],
    *,
    bankroll_usd: Optional[float] = None,
) -> Optional[Opportunity]:
    """Goals already exceed the line (feed-confirmed) -> Over settles $1."""
    if market.kind != "total" or market.line is None:
        return None
    if not feed_confirmed(state.goals_ge.get(market.line)):
        return None
    _, asks = parse_book(book)
    fill = walk_executable(asks, 1.0)
    best = asks[0].price if asks else None
    goals = state.feed.total_goals if state.feed else "?"
    reason = "Over %.1f ask %s, settles $1 (%s goals scored)" % (
        market.line, _cents(best), goals,
    )
    return _mk_opportunity(
        match_key=state.match_key, detector="ou_over_settled", market=market,
        token_id=market.yes_token, outcome="Yes", fair=1.0, fill=fill,
        best_ask=best, state=state, reason_market=reason,
        bankroll_usd=bankroll_usd,
    )


def exact_impossible(score: Tuple[int, int], feed: FeedScore) -> bool:
    """A final exact score (h, a) is impossible once either team already has
    more goals than that scoreline allows (goals never come off the board)."""
    h, a = score
    return h < feed.home_goals or a < feed.away_goals


def detect_exact_impossible(
    state: MatchState,
    market: MarketToken,
    no_book: Optional[dict],
    *,
    yes_bid: Optional[float] = None,
    bankroll_usd: Optional[float] = None,
) -> Optional[Opportunity]:
    """An exact-score row that the current (feed) score has made impossible ->
    its NO settles $1; buy the NO ask while the row is still bid.

    ``yes_bid`` (when known) gates on the row actually still being bid — a
    dead row with an empty book is not an opportunity.
    """
    if market.kind != "exact" or market.score is None or state.feed is None:
        return None
    if not exact_impossible(market.score, state.feed):
        return None
    if yes_bid is not None and yes_bid < 0.02:
        return None  # already dead — nothing stale left
    if not market.no_token:
        return None
    _, asks = parse_book(no_book)
    fill = walk_executable(asks, 1.0)
    best = asks[0].price if asks else None
    reason = "exact %d-%d impossible at %s — NO ask %s, settles $1" % (
        market.score[0], market.score[1], state.feed.scoreline, _cents(best),
    )
    return _mk_opportunity(
        match_key=state.match_key, detector="exact_impossible", market=market,
        token_id=market.no_token, outcome="No", fair=1.0, fill=fill,
        best_ask=best, state=state, reason_market=reason,
        bankroll_usd=bankroll_usd,
    )


def detect_ft_winner(
    state: MatchState,
    market: MarketToken,
    book: Optional[dict],
    *,
    bankroll_usd: Optional[float] = None,
) -> Optional[Opportunity]:
    """Feed says full-time -> the 90-min 1X2 outcome is settled; buy the
    winner's (or draw's) YES if an ask still clears the threshold."""
    if market.kind != "1x2" or state.feed is None or state.feed.status != "ft":
        return None
    f = state.feed
    if f.home_goals == f.away_goals:
        won = market.team is not None and market.team.lower() == "draw"
    else:
        leader = "home" if f.home_goals > f.away_goals else "away"
        won = (market.team or "") == leader
    if not won:
        return None
    _, asks = parse_book(book)
    fill = walk_executable(asks, 1.0)
    best = asks[0].price if asks else None
    reason = "FT %s — %s settled, ask %s, settles $1 (90-min basis)" % (
        f.scoreline, market.question or market.team, _cents(best),
    )
    return _mk_opportunity(
        match_key=state.match_key, detector="ft_winner", market=market,
        token_id=market.yes_token, outcome="Yes", fair=1.0, fill=fill,
        best_ask=best, state=state, reason_market=reason,
        bankroll_usd=bankroll_usd,
    )


def detect_ladder_lag(
    state: MatchState,
    rung: MarketToken,
    rung_book: Optional[dict],
    *,
    trigger_team: str,
    jump_pre: float,
    jump_post: float,
    rung_pre_ref: Optional[float],
) -> Optional[Opportunity]:
    """Same-team advancement rung lagging a decisive (>=10c UP) 1X2 move.

    Detection logic per PR #179's paper harness (which measured the class:
    +1.9c mean drift, n=302 historical): after the jump, if the rung's best
    ask still sits within :data:`LADDER_STALE_BAND` of its pre-jump reference,
    the quote is stale. There is NO state-determined fair value here, so the
    edge shown is the labelled historical estimate and the stake is the fixed
    :data:`LADDER_STAKE_USD` minimum (never Kelly-sized).
    """
    if rung.kind != "ladder" or rung_pre_ref is None:
        return None
    if (jump_post - jump_pre) < LADDER_JUMP_THRESHOLD:
        return None  # BUY side only: only an UP move makes the rung cheap
    _, asks = parse_book(rung_book)
    if not asks:
        return None
    best = asks[0]
    if best.price > rung_pre_ref + LADDER_STALE_BAND:
        return None  # rung already repriced
    fill = walk_executable(asks, best.price + LADDER_STALE_BAND,
                           min_edge=-1.0, cap_usd=LADDER_STAKE_USD)
    if fill["shares"] <= 0.0 or fill["notional"] < MIN_EXECUTABLE_USD - 1e-6:
        return None
    reason = (
        "ladder-lag: %s 1X2 %s->%s (+%dc) but %s ask %s ~ pre-jump %s; "
        "hist est +%.1fc drift (n=%d)"
        % (
            trigger_team, _cents(jump_pre), _cents(jump_post),
            round((jump_post - jump_pre) * 100), rung.rung or "rung",
            _cents(best.price), _cents(rung_pre_ref),
            LADDER_HIST_DRIFT * 100, LADDER_HIST_N,
        )
    )
    return Opportunity(
        uid=uuid.uuid4().hex[:12],
        match_key=state.match_key,
        detector="ladder_lag",
        market=rung,
        token_id=rung.yes_token,
        outcome="Yes",
        fair=None,
        price=fill["avg_price"],
        best_ask=best.price,
        shares=fill["shares"],
        notional_usd=fill["notional"],
        stake_usd=min(LADDER_STAKE_USD, fill["notional"]),
        edge=max(0.0, LADDER_HIST_DRIFT - fee(fill["avg_price"])),
        edge_is_estimate=True,
        reason=reason,
        state_sources="pm",
    )


def _cents(p: Optional[float]) -> str:
    return "--" if p is None else ("%dc" % round(float(p) * 100))


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


class DedupeRegistry:
    """One ping per opportunity per state-change; survives restarts by
    replaying the session log (see :func:`replay_pinged_keys`)."""

    def __init__(self, seen: Optional[set] = None):
        self._seen = set(seen or ())

    def should_ping(self, opp: Opportunity, state_sig: str) -> bool:
        return opp.dedupe_key(state_sig) not in self._seen

    def mark(self, opp: Opportunity, state_sig: str) -> None:
        self._seen.add(opp.dedupe_key(state_sig))


def replay_pinged_keys(log_path: str = SESSION_LOG_PATH) -> set:
    """Rebuild the pinged-key set from the session log (idempotent restarts)."""
    p = Path(log_path)
    if not p.exists():
        return set()
    keys = set()
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "ping" and rec.get("dedupe_key"):
                keys.add(rec["dedupe_key"])
    except OSError:
        return keys
    return keys


# ---------------------------------------------------------------------------
# Session log
# ---------------------------------------------------------------------------


def append_log(record: Dict[str, Any], log_path: str = SESSION_LOG_PATH) -> None:
    """Append one audit record to ``data/pm_inplay_log.jsonl`` (best-effort)."""
    rec = dict(record)
    rec.setdefault("ts_utc", now_iso())
    p = Path(log_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Ping formatting (/pm conventions: PM cents, $ stakes, settlement flagged)
# ---------------------------------------------------------------------------


def format_opportunity_ping(
    opp: Opportunity,
    *,
    relay_name: str,
    pm_token: Optional[str] = None,
    conflicts: Optional[List[str]] = None,
) -> str:
    """Telegram ping for one opportunity, /pm-style: cents, $ stake, basis."""
    lines = ["🚨 *IN-PLAY: %s* — %s" % (opp.match_key, opp.detector.replace("_", " "))]
    lines.append("    %s" % opp.reason)
    edge_pct = opp.edge * 100.0
    edge_lbl = ("edge ~%.1fc/sh (hist est, n=%d)" % (edge_pct, LADDER_HIST_N)
                if opp.edge_is_estimate else "edge +%.1fc/sh after fee" % edge_pct)
    lines.append(
        "    BUY %s @ %s | $%.0f (%.0f sh, book-walked depth $%.0f) | %s"
        % (opp.outcome, _cents(opp.price), opp.stake_usd, opp.shares,
           opp.notional_usd, edge_lbl)
    )
    lines.append("    settles: %s basis | source: %s" % (opp.market.settlement, opp.state_sources))
    for c in conflicts or []:
        lines.append("    ⚠ %s" % c)
    if pm_token:
        lines.append("    → parked as `%s` — reply `Y %s` to fire (PM_DRY_RUN gates)" % (pm_token, pm_token))
    elif relay_name == "git":
        lines.append("    → parked via git relay — fireable after mini sync (~≤6min); "
                     "the mini will DM the `Y PM-<n>` prompt")
    else:
        lines.append("    → park FAILED (%s) — manual only" % relay_name)
    return "\n".join(lines)


def format_exposure_ping(
    match_key: str,
    state: MatchState,
    positions: List[Dict[str, Any]],
) -> str:
    """Open-exposure awareness ping on a material development.

    Each ``positions`` item: ``{title, outcome, size, avg_price, cur_price,
    impact}`` (impact one of helps/hurts/unclear, derived by the caller from
    the scoring side — labelled heuristic, never a model claim).
    """
    lines = ["📌 *OPEN PM EXPOSURE — %s* (%s)" % (match_key, _state_reason(state))]
    for p in positions:
        mtm = (float(p["cur_price"]) - float(p["avg_price"])) * float(p["size"])
        lines.append(
            "    %s %s — %.0f sh @ %s, mark %s (MTM %+.0f$) — %s"
            % (
                p.get("title", "?"), p.get("outcome", ""), float(p["size"]),
                _cents(p.get("avg_price")), _cents(p.get("cur_price")), mtm,
                p.get("impact", "impact unclear"),
            )
        )
    for c in state.conflicts:
        lines.append("    ⚠ %s" % c)
    return "\n".join(lines)


def classify_impact(title: str, outcome: str, scoring_side_team: str,
                    other_team: str) -> str:
    """Heuristic helps/hurts for a position given which team just scored.

    Pure string heuristic (win-market titles carry the team name); anything
    ambiguous is labelled "impact unclear" rather than guessed.
    """
    t = (title or "").lower()
    yes = (outcome or "").strip().lower() in ("yes", "y")
    scorer = (scoring_side_team or "").lower()
    other = (other_team or "").lower()
    if "draw" in t:
        return "hurts (draw pos)" if yes else "helps (no-draw pos)"
    if scorer and scorer in t and "win" in t:
        return "helps" if yes else "hurts"
    if other and other in t and "win" in t:
        return "hurts" if yes else "helps"
    return "impact unclear"


# ---------------------------------------------------------------------------
# Parked-proposal packaging (gate-compatible; the mini allocates PM-<n>)
# ---------------------------------------------------------------------------


def to_parked_proposal(opp: Opportunity) -> Dict[str, Any]:
    """Package an opportunity as a ``pm_parked`` proposal the existing
    ``Y PM-<n>`` flow can fire, mirroring ``wca_pm_propose._augment_for_gate``:
    ``size`` = SHARES (the gate computes notional as price*size), plus the
    display keys. In-play extras (uid/detector/reason/...) ride along for the
    audit trail — the gate ignores unknown keys."""
    shares = round(opp.stake_usd / opp.price, 2) if opp.price > 0 else 0.0
    return {
        "uid": opp.uid,
        "created_utc": opp.created_utc,
        "inplay": True,
        "detector": opp.detector,
        "reason": opp.reason,
        "settlement_basis": opp.market.settlement,
        "token_id": opp.token_id,
        "side": "BUY",
        "price": round(opp.price, 3),
        "size": shares,             # gate sizes in SHARES
        "shares": shares,
        "size_usd": round(opp.stake_usd, 2),
        "market_question": opp.market.question,
        "outcome": opp.outcome,
        "match_desc": opp.match_key,
        "model_prob": (opp.fair if opp.fair is not None else 0.0),
        "ev": (opp.edge / opp.price if opp.price > 0 else 0.0),
        "neg_risk": bool(opp.market.neg_risk),
        "label": opp.market.question or opp.match_key,
    }


# ---------------------------------------------------------------------------
# Relays — pluggable, probed at runtime, never place orders
# ---------------------------------------------------------------------------


@dataclass
class RelayResult:
    ok: bool
    relay: str            # "ssh" | "git" | "none"
    pm_token: Optional[str] = None  # "PM-<n>" when known synchronously (ssh)
    detail: str = ""


def _default_runner(cmd: List[str], *, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class SshRelay:
    """Park via the mini's own ingest CLI over SSH (instant PM-<n>).

    Only usable when the LAN route to the mini is up (VPN off / split-tunnel);
    :meth:`available` probes this at call time with a short timeout.
    """

    name = "ssh"

    def __init__(self, host: str = MINI_SSH_HOST, repo: str = MINI_REPO,
                 runner: Callable = _default_runner):
        self.host = host
        self.repo = repo
        self._run = runner

    def available(self) -> bool:
        try:
            proc = self._run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                 self.host, "true"],
                timeout=8.0,
            )
            return proc.returncode == 0
        except Exception:  # noqa: BLE001 — probe failure just means unavailable
            return False

    def park(self, proposal: Dict[str, Any]) -> RelayResult:
        import base64

        payload = base64.b64encode(
            json.dumps(proposal, sort_keys=True).encode("utf-8")
        ).decode("ascii")
        try:
            proc = self._run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.host,
                 "cd %s && PYTHONPATH=src ./.venv/bin/python "
                 "scripts/wca_pm_inplay_ingest.py --park-b64 %s" % (self.repo, payload)],
                timeout=60.0,
            )
        except Exception as exc:  # noqa: BLE001
            return RelayResult(False, self.name, detail="ssh error: %s" % exc)
        if proc.returncode != 0:
            return RelayResult(False, self.name, detail=(proc.stderr or "")[:200])
        tok = None
        for word in (proc.stdout or "").split():
            if word.startswith("PM-"):
                tok = word.strip("`.,")
                break
        return RelayResult(True, self.name, pm_token=tok, detail="parked over ssh")


class GitArtifactRelay:
    """Park by committing the proposal to ``data/pm_inplay_proposals.json`` on
    ``origin/main`` via a dedicated detached worktree.

    The worktree is reset to ``origin/main`` before every append, so the
    operator's checkout/branch is never touched and stale branch data can
    never be committed over CI-fresh data. The mini autopulls main (5-min
    cycle) and its ingest job parks + DMs the fireable ``PM-<n>``.
    """

    name = "git"

    def __init__(self, repo_root: str, worktree: Optional[str] = None,
                 runner: Callable = _default_runner):
        self.repo_root = str(repo_root)
        self.worktree = worktree or os.path.join(self.repo_root, "worktrees", "pm-inplay-relay")
        self._run = runner

    def _git(self, args: List[str], cwd: str, timeout: float = 60.0):
        return self._run(["git", "-C", cwd] + args, timeout=timeout)

    def _ensure_worktree(self) -> Optional[str]:
        if self._git(["fetch", "origin", "main"], self.repo_root, 120.0).returncode != 0:
            return "git fetch failed"
        if not Path(self.worktree, ".git").exists():
            proc = self._git(
                ["worktree", "add", "--detach", self.worktree, "origin/main"],
                self.repo_root, 120.0,
            )
            if proc.returncode != 0:
                return "worktree add failed: %s" % (proc.stderr or "")[:160]
        proc = self._git(["reset", "--hard", "origin/main"], self.worktree)
        if proc.returncode != 0:
            return "reset failed: %s" % (proc.stderr or "")[:160]
        return None

    #: _append sentinel: the uid is already in the artifact (relayed earlier).
    _DUPLICATE = "__duplicate__"

    def _append(self, proposal: Dict[str, Any]) -> Optional[str]:
        path = Path(self.worktree) / PROPOSALS_PATH
        try:
            doc = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            doc = {}
        proposals = [p for p in (doc.get("proposals") or []) if isinstance(p, dict)]
        if any(p.get("uid") == proposal.get("uid") for p in proposals):
            return self._DUPLICATE
        proposals.append(proposal)
        doc = {"meta": {"updated_utc": now_iso(),
                        "writer": "wca_pm_inplay_monitor (MacBook)"},
               "proposals": proposals}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        except OSError as exc:
            return "write failed: %s" % exc
        return None

    def park(self, proposal: Dict[str, Any]) -> RelayResult:
        for attempt in (1, 2):  # one retry on a push race
            err = self._ensure_worktree()
            if err:
                return RelayResult(False, self.name, detail=err)
            err = self._append(proposal)
            if err == self._DUPLICATE:
                # Already committed on origin/main by an earlier park — the
                # mini will (or did) ingest it; nothing new to push.
                return RelayResult(True, self.name,
                                   detail="already relayed (uid dedupe)")
            if err:
                return RelayResult(False, self.name, detail=err)
            if self._git(["add", PROPOSALS_PATH], self.worktree).returncode != 0:
                return RelayResult(False, self.name, detail="git add failed")
            msg = "inplay: park proposal %s (%s)" % (proposal.get("uid"), proposal.get("detector"))
            if self._git(["commit", "-m", msg], self.worktree).returncode != 0:
                return RelayResult(False, self.name, detail="git commit failed")
            push = self._git(["push", "origin", "HEAD:main"], self.worktree, 120.0)
            if push.returncode == 0:
                return RelayResult(True, self.name,
                                   detail="pushed to main; fireable after mini sync (~≤6min)")
            if attempt == 1:
                continue  # re-fetch/reset and retry once
            return RelayResult(False, self.name,
                               detail="git push rejected: %s" % (push.stderr or "")[:160])
        return RelayResult(False, self.name, detail="unreachable")


class PaperRelay:
    """--paper mode: log the park intent, touch nothing."""

    name = "paper"

    def available(self) -> bool:  # pragma: no cover - trivial
        return True

    def park(self, proposal: Dict[str, Any]) -> RelayResult:
        return RelayResult(True, self.name, detail="paper mode — not relayed")


def select_relay(ssh_relay: SshRelay, git_relay: GitArtifactRelay):
    """Best-first runtime selection: SSH when the mini answers, else git."""
    if ssh_relay.available():
        return ssh_relay
    return git_relay
