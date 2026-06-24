"""Pure cash-out logic: classify a Polymarket position, decide whether a live
match event has invalidated it, and build the SELL proposal that exits it.

No network, no signing, no state — every function here is a pure transform so
the kill predicates (the part that decides whether to dump real money) are
exhaustively unit-testable. The watcher / CLI wire these to live data:

  position (wca.pm.positions.Position)        ── classify_market ──▶ kind
  live score {team: goals}  ── orient_score ──▶ (home_goals, away_goals)
  (kind, outcome, title, home_goals, away_goals) ── evaluate ──▶ killed?
  position + best_bid ── build_sell_proposal ──▶ parked SELL proposal

Kill semantics (the bet is DEAD — fair value has gone to ~0):

* Exact score ``a-b`` (a "Yes" on one scoreline): dead once the score can no
  longer be exactly ``a-b`` — i.e. ``home_goals > a`` OR ``away_goals > b``.
* Totals "Under L" (or "Over L" held as "No"): dead once ``total ≥ ceil(L)``
  (Under 2.5 dies on the 3rd goal). "Over" held as "Yes" is *helped* by a goal,
  never event-killed — we don't cash those out here.
* BTTS "No": dead once both teams have scored. "Yes" is helped, never killed.

VAR note: these predicates are monotonic in goals — they only ever flip
alive→dead. The watcher is responsible for the disallowed-goal cooldown
(a chalked-off goal makes the feed's score tick back down); see the daemon.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from wca.data.teamnames import canonical

# Market kinds we understand. Only the first three are "binary kills" a single
# goal can decide; the rest are gradient/футures and out of cash-out scope.
KIND_EXACT = "exact_score"
KIND_TOTALS = "totals"
KIND_BTTS = "btts"
KIND_TEAM_WIN = "team_win"
KIND_ADVANCEMENT = "advancement"
KIND_OTHER = "other"

KILLABLE_KINDS = (KIND_EXACT, KIND_TOTALS, KIND_BTTS)


# ---------------------------------------------------------------------------
# Classification + title parsing
# ---------------------------------------------------------------------------

_EXACT_RE = re.compile(
    r"exact\s*score\s*:\s*(.+?)\s+(\d+)\s*-\s*(\d+)\s+(.+?)\s*\??\s*$",
    re.IGNORECASE,
)
# "...over 2.5 goals...", "Total goals Under 3.5", "O 2.5", etc.
_TOTALS_RE = re.compile(r"\b(over|under|o|u)\b[^\d]*(\d+(?:\.\d+)?)", re.IGNORECASE)
# "vs" is the strong separator: greedy left so the LAST " vs " splits home/away
# (handles a hyphenated home name like "Bosnia-Herzegovina vs Serbia"). The dash
# fallback requires SPACES around the dash so it never splits inside a name.
_VS_RE = re.compile(r"^(.+)\s+(?:vs\.?|v\.?)\s+(.+)$", re.IGNORECASE)
_DASH_RE = re.compile(r"^(.+?)\s+[-–]\s+(.+)$")


def classify_market(title: str, outcome: str = "") -> str:
    """Best-effort classification of a Polymarket market from its title."""
    t = (title or "").lower()
    if _EXACT_RE.search(title or ""):
        return KIND_EXACT
    if any(
        kw in t
        for kw in ("reach the round", "win the group", "advance", "reach the",
                   "to win the 2026", "win the 2026", "winner", "top scorer",
                   "golden boot", "reach the final", "win their group")
    ):
        return KIND_ADVANCEMENT
    if "both teams" in t or "btts" in t or "both to score" in t:
        return KIND_BTTS
    if ("goals" in t or "total" in t) and _TOTALS_RE.search(t):
        return KIND_TOTALS
    if "draw" in t or re.search(r"\bwin\b", t):
        return KIND_TEAM_WIN
    return KIND_OTHER


@dataclass
class ExactScore:
    home: str
    home_goals: int
    away: str
    away_goals: int


def parse_exact_score(title: str) -> Optional[ExactScore]:
    """'Exact Score: Qatar 0 - 2 Switzerland?' -> ExactScore(Qatar,0,Switzerland,2)."""
    m = _EXACT_RE.search(title or "")
    if not m:
        return None
    return ExactScore(
        home=m.group(1).strip(),
        home_goals=int(m.group(2)),
        away=m.group(4).strip(),
        away_goals=int(m.group(3)),
    )


def parse_totals_line(title: str) -> Optional[Tuple[str, float]]:
    """Return ('over'|'under', line) parsed from a totals title, else None."""
    m = _TOTALS_RE.search(title or "")
    if not m:
        return None
    word = m.group(1).lower()
    phrase = "over" if word in ("over", "o") else "under"
    try:
        line = float(m.group(2))
    except ValueError:
        return None
    return phrase, line


def parse_match_teams(title: str) -> Optional[Tuple[str, str]]:
    """Best-effort (home, away) for a totals/BTTS title.

    Exact-score titles should use :func:`parse_exact_score` (it carries the
    explicit order). For other markets we look for an 'A vs B' / 'A - B' span,
    preferring text after 'in '.
    """
    ex = parse_exact_score(title)
    if ex is not None:
        return ex.home, ex.away
    text = title or ""
    # Prefer the segment after "in " ("...goals in Brazil vs Morocco?").
    idx = text.lower().rfind(" in ")
    cand = text[idx + 4:] if idx >= 0 else text
    cand = cand.strip().rstrip("?").strip()
    m = _VS_RE.search(cand) or _DASH_RE.search(cand)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


# ---------------------------------------------------------------------------
# Effective bet direction (combine title phrasing with the Yes/No we hold)
# ---------------------------------------------------------------------------


def _is_yes(outcome: str) -> bool:
    return (outcome or "").strip().lower() in ("yes", "y", "true")


def effective_totals_direction(phrase: str, outcome: str) -> str:
    """Net over/under we are exposed to, given the title phrasing + our side.

    Holding "No" on an "Over 2.5?" market == betting the Under, and vice-versa.
    """
    yes = _is_yes(outcome)
    if phrase == "over":
        return "over" if yes else "under"
    return "under" if yes else "over"


# ---------------------------------------------------------------------------
# Kill predicates
# ---------------------------------------------------------------------------


@dataclass
class KillVerdict:
    kind: str
    killed: bool
    reason: str


def evaluate_position(
    *,
    title: str,
    outcome: str,
    home_goals: int,
    away_goals: int,
) -> KillVerdict:
    """Decide whether the current score has invalidated this position.

    ``home_goals`` / ``away_goals`` must already be oriented to the title's
    home/away order (use :func:`orient_score`). Returns a verdict; ``killed`` is
    only ever ``True`` for the three binary-kill kinds.
    """
    kind = classify_market(title, outcome)
    total = int(home_goals) + int(away_goals)

    if kind == KIND_EXACT:
        ex = parse_exact_score(title)
        if ex is None:
            return KillVerdict(kind, False, "unparseable exact-score title")
        # Held as a "Yes" on the scoreline: dead once it's unreachable.
        if home_goals > ex.home_goals or away_goals > ex.away_goals:
            return KillVerdict(
                kind, True,
                "score %d-%d can no longer be %d-%d"
                % (home_goals, away_goals, ex.home_goals, ex.away_goals),
            )
        return KillVerdict(kind, False, "scoreline still reachable")

    if kind == KIND_TOTALS:
        parsed = parse_totals_line(title)
        if parsed is None:
            return KillVerdict(kind, False, "unparseable totals title")
        phrase, line = parsed
        direction = effective_totals_direction(phrase, outcome)
        if direction == "under":
            need = math.ceil(line)  # Under 2.5 dies when total >= 3
            if total >= need:
                return KillVerdict(
                    kind, True,
                    "total %d reached the %s %.1f kill (>= %d)"
                    % (total, direction, line, need),
                )
            return KillVerdict(kind, False, "under still alive (total %d < %d)" % (total, need))
        # We are net "over": a goal helps us; never an event-kill.
        return KillVerdict(kind, False, "over is helped by goals, not killed")

    if kind == KIND_BTTS:
        yes = _is_yes(outcome)
        if not yes:  # BTTS-No
            if home_goals >= 1 and away_goals >= 1:
                return KillVerdict(kind, True, "both teams have scored (BTTS-No dead)")
            return KillVerdict(kind, False, "both teams have not yet scored")
        return KillVerdict(kind, False, "BTTS-Yes is helped by goals, not killed")

    return KillVerdict(kind, False, "kind %r is not a binary-kill market" % kind)


# ---------------------------------------------------------------------------
# Orientation: live scores -> (home_goals, away_goals) in the title's order
# ---------------------------------------------------------------------------


def orient_score(
    home: str,
    away: str,
    scores: List[Dict[str, Any]],
) -> Optional[Tuple[int, int]]:
    """Map a scores-feed score list to (home_goals, away_goals) for (home, away).

    ``scores`` is the Odds-API ``/scores`` shape: a list of
    ``{"name": <team>, "score": <int|str>}``. Names are compared on their
    canonical spelling so feed/Polymarket/results spellings line up. Returns
    ``None`` (caller must SKIP — never guess) when either team can't be matched
    or a score is missing/garbage — guessing here risks selling the WRONG
    position, the worst failure mode.
    """
    if not scores or len(scores) < 2:
        return None
    by_canon: Dict[str, Any] = {}
    for s in scores:
        name = canonical(str(s.get("name") or ""))
        if not name:
            return None
        by_canon[name] = s.get("score")

    h, a = canonical(home), canonical(away)
    if h not in by_canon or a not in by_canon:
        return None
    try:
        hg = int(by_canon[h])
        ag = int(by_canon[a])
    except (TypeError, ValueError):
        return None
    if hg < 0 or ag < 0:
        return None
    return hg, ag


# ---------------------------------------------------------------------------
# Order-book parsing (CLOB /book) — what we can actually sell into right now
# ---------------------------------------------------------------------------


def _book_bids(book: Dict[str, Any]) -> List[Tuple[float, float]]:
    """Return [(price, size), ...] bids, highest price first."""
    out: List[Tuple[float, float]] = []
    for b in (book or {}).get("bids") or []:
        try:
            price = float(b["price"]) if isinstance(b, dict) else float(b[0])
            size = float(b["size"]) if isinstance(b, dict) else float(b[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            out.append((price, size))
    out.sort(key=lambda x: -x[0])
    return out


def best_bid_from_book(book: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """(price, size) of the highest resting bid, or ``None`` if the book is empty."""
    bids = _book_bids(book)
    return bids[0] if bids else None


def marketable_sell_plan(
    book: Dict[str, Any],
    shares: float,
    *,
    min_price: float = 0.0,
) -> Optional[Tuple[float, float, float]]:
    """Walk the bid side to price a market-ish exit of up to ``shares``.

    Sells into resting bids from the top down, stopping when ``shares`` is filled
    or the next bid is below ``min_price`` (the residual-value floor — don't dump
    into bids worth ~nothing). Returns ``(limit_price, fillable_shares,
    proceeds)`` where ``limit_price`` is the lowest level hit (a single FAK SELL
    limit at that price fills every level above it), or ``None`` if nothing
    fillable at/above the floor.
    """
    bids = _book_bids(book)
    if not bids:
        return None
    remaining = float(shares)
    proceeds = 0.0
    taken = 0.0
    limit_price: Optional[float] = None
    for price, size in bids:
        if price < min_price or remaining <= 1e-9:
            break
        take = min(size, remaining)
        proceeds += price * take
        taken += take
        limit_price = price
        remaining -= take
    if taken <= 0 or limit_price is None:
        return None
    return round(limit_price, 6), round(taken, 6), round(proceeds, 6)


# ---------------------------------------------------------------------------
# SELL proposal construction
# ---------------------------------------------------------------------------


def build_sell_proposal(
    position,
    best_bid: Optional[float],
    *,
    shares: Optional[float] = None,
    bid_size: Optional[float] = None,
    min_proceeds: float = 1.0,
    undercut_ticks: int = 0,
    tick: float = 0.01,
    reason: str = "",
) -> Optional[Dict[str, Any]]:
    """Build a parked SELL proposal that cashes a position out at the book.

    Returns ``None`` (don't trade) when there is no usable bid or the estimated
    proceeds fall below ``min_proceeds`` — a sell that nets ~nothing is pure
    downside given VAR risk and gas, so the floor is a hard gate even with no
    arming price floor on the market.

    Parameters
    ----------
    position:
        A :class:`wca.pm.positions.Position`.
    best_bid:
        Current best bid for the token (per-share USDC). ``None``/``<=0`` => no
        liquidity => return ``None``.
    shares:
        Shares to sell; defaults to the whole held size, clamped to ``bid_size``
        when that is given (don't quote more than the book can absorb).
    bid_size:
        Shares available at/under the bid (book depth), if known.
    min_proceeds:
        Minimum acceptable gross proceeds (price × shares) to bother selling.
    undercut_ticks:
        Place the limit this many ticks BELOW the best bid to guarantee the FAK
        crosses (0 = sell exactly at the bid).
    """
    if best_bid is None or best_bid <= 0:
        return None
    price = round(best_bid - undercut_ticks * tick, 6)
    if price <= 0:
        return None

    size = float(shares) if shares is not None else float(position.size)
    if bid_size is not None:
        size = min(size, float(bid_size))
    if size <= 0:
        return None

    proceeds = price * size
    if proceeds < min_proceeds:
        return None

    teams = parse_match_teams(position.title)
    match_desc = "%s vs %s" % teams if teams else position.title

    label = position.title
    if len(label) > 48:
        label = label[:45] + "..."

    return {
        "token_id": position.asset,
        "side": "SELL",
        "price": price,
        "size": round(size, 6),
        "neg_risk": bool(position.neg_risk),
        # FOK (fill-or-kill, all-or-nothing): either the whole sized slice fills
        # or nothing does. This makes booking deterministic — no partial fill to
        # reconcile — and the size is already clamped to book depth by
        # marketable_sell_plan, so it should fill unless the book just moved.
        "order_type": "FOK",
        "outcome": position.outcome,
        "selection": position.title,
        "label": label,
        "match_desc": match_desc,
        "market_question": position.title,
        "event_slug": position.event_slug,
        "source": "model",
        "cashout_reason": reason,
        "est_proceeds": round(proceeds, 4),
        "avg_price": position.avg_price,
        "cur_price": position.cur_price,
    }


# ---------------------------------------------------------------------------
# End-to-end decision (pure): position + live scores + book -> action
# ---------------------------------------------------------------------------


def find_scores_event(
    home: str, away: str, events: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Find the scores-feed event whose two teams canonically match (home, away).

    Matches order-independently against the event's ``home_team``/``away_team``
    and its ``scores`` names. Returns ``None`` — the caller must SKIP, never guess
    (a wrong match sells the wrong token) — if NO event matches both teams, OR if
    MORE THAN ONE does (an ambiguous/duplicate feed row; true WC rematches can't
    occur, but stale API rows can).
    """
    hc, ac = canonical(home), canonical(away)
    matches: List[Dict[str, Any]] = []
    for ev in events or []:
        teamset = {
            canonical(str(ev.get("home_team") or "")),
            canonical(str(ev.get("away_team") or "")),
        }
        for s in ev.get("scores") or []:
            teamset.add(canonical(str(s.get("name") or "")))
        teamset.discard("")
        if hc in teamset and ac in teamset:
            matches.append(ev)
    return matches[0] if len(matches) == 1 else None


@dataclass
class CashoutDecision:
    """Outcome of evaluating one held position against the live book + score.

    ``action`` is one of:
      * ``'sell'``        — killed and there is real value to capture (proposal set)
      * ``'not_killed'``  — still alive (or a gradient market that goals help)
      * ``'no_match'``    — couldn't map to a live score (SKIP, never guess)
      * ``'no_value'``    — killed but the book has nothing worth selling into
      * ``'unsupported'`` — not a binary-kill market kind
    """

    action: str
    reason: str
    verdict: Optional[KillVerdict] = None
    proposal: Optional[Dict[str, Any]] = None
    plan: Optional[Tuple[float, float, float]] = None


def decide_cashout(
    position,
    scores_events: List[Dict[str, Any]],
    book: Optional[Dict[str, Any]],
    *,
    min_proceeds: float = 1.0,
    price_floor: float = 0.0,
    undercut_ticks: int = 0,
    tick: float = 0.01,
) -> CashoutDecision:
    """Decide what to do with one held position, purely from inputs.

    ``book`` may be ``None`` (don't fetch it until we know the position is
    killed); pass it only when you want a sell priced. The daemon calls this
    twice if it likes: once with ``book=None`` to test the kill cheaply, then
    again with the live book once a kill persists past the VAR cooldown.
    """
    kind = classify_market(position.title, position.outcome)
    if kind not in KILLABLE_KINDS:
        return CashoutDecision("unsupported", "kind %r not a binary kill" % kind)

    teams = parse_match_teams(position.title)
    if not teams:
        return CashoutDecision("no_match", "cannot parse teams from title")
    home, away = teams

    ev = find_scores_event(home, away, scores_events)
    if ev is None:
        return CashoutDecision("no_match", "no live score for %s vs %s" % (home, away))

    oriented = orient_score(home, away, ev.get("scores") or [])
    if oriented is None:
        return CashoutDecision("no_match", "could not orient score for %s vs %s" % (home, away))
    home_goals, away_goals = oriented

    verdict = evaluate_position(
        title=position.title, outcome=position.outcome,
        home_goals=home_goals, away_goals=away_goals,
    )
    if not verdict.killed:
        return CashoutDecision("not_killed", verdict.reason, verdict=verdict)

    if book is None:
        # Killed, but caller didn't supply a book to price the exit yet.
        return CashoutDecision("sell", verdict.reason, verdict=verdict)

    plan = marketable_sell_plan(book, position.size, min_price=price_floor)
    if plan is None:
        return CashoutDecision("no_value", "no fillable bids at/above floor", verdict=verdict)
    price, shares, proceeds = plan
    if proceeds < min_proceeds:
        return CashoutDecision(
            "no_value", "proceeds %.2f < min %.2f" % (proceeds, min_proceeds),
            verdict=verdict, plan=plan,
        )
    proposal = build_sell_proposal(
        position, best_bid=price, shares=shares, min_proceeds=min_proceeds,
        undercut_ticks=undercut_ticks, tick=tick, reason=verdict.reason,
    )
    if proposal is None:
        return CashoutDecision("no_value", "proposal below min proceeds", verdict=verdict, plan=plan)
    proposal["est_proceeds"] = round(proceeds, 4)
    return CashoutDecision("sell", verdict.reason, verdict=verdict, proposal=proposal, plan=plan)
