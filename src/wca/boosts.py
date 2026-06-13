"""Price a bookmaker price-boost / enhanced-odds offer against our model.

A "boost" is a sportsbook promo: a selection offered at an enhanced decimal
price (e.g. "Brazil to beat Morocco, was 1.80, BOOSTED to 2.50"). Unlike a
normal line, a boost is *given* to us — there is no margin to overcome — so the
only question is whether the boosted price beats the *fair* price our model
implies. If ``boosted_odds * model_prob - 1 > 0`` the boost is genuinely +EV
and worth taking; otherwise the book has boosted something that is still a
loser at the enhanced price (boosts are almost always offered on outcomes the
book *wants* you to back).

This module is a thin, **pure** pricing layer: given a :class:`Boost` and the
already-computed model feed (``site/scores_data.json``), it maps the boost's
market+selection onto the matching model probability and returns a
:class:`BoostEval`. It never touches the network, the wall clock, or the
ledger — the daemon and the bot are the I/O layers around it.

Honesty about coverage (v1 limitations — read before trusting a verdict)
------------------------------------------------------------------------
* The model feed only carries **1X2**, **over/under** (one primary line plus a
  correct-score grid we can re-aggregate from), **BTTS**, and **correct score**
  for the top handful of scorelines. Anything outside that — anytime/first
  goalscorer, player props, cards, corners, exotic combos — is reported
  ``priceable=False`` with an explicit reason. We do **not** guess a price we
  cannot derive from the feed; a missing price is stated, never faked.
* **In-play boosts are not priced.** The feed is a pre-match model; once a game
  is live the true probabilities have moved and our number is stale, so any
  boost flagged in-play (or whose text mentions "live"/"in-play") is returned
  unpriceable rather than scored against a pre-match prior.
* Over/under on a **non-primary line** is re-derived by summing the
  correct-score grid strictly over / under the line. That grid is truncated to
  the top scorelines, so for a line where meaningful probability mass sits
  outside the listed scores we report unpriceable rather than a number we know
  is biased low.

**UNITS (critical).** In the feed, ``model_1x2`` values are probabilities in
``[0, 1]``, but ``over_under.over``/``under``, ``btts`` and ``scores[].prob``
are **percentages (0–100)**. This module divides those by 100 before using them
as probabilities; the unit handling is the single most error-prone part and is
covered directly by the tests.

Shared contract (the daemon imports these names — keep them stable):
``Boost``, ``BoostEval``, ``MIN_EDGE``, ``load_scores_feed``, ``evaluate_boost``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from wca.data.teamnames import canonical

# Boosts are enhanced odds, so there is no bookmaker-margin buffer to clear:
# any genuinely positive edge is worth flagging. (A normal-line filter would
# demand a few points of EV to cover devig noise; an explicit boost does not.)
MIN_EDGE = 0.0


@dataclass
class Boost:
    """One bookmaker price-boost / enhanced-odds offer to be priced.

    ``fixture`` is free-text in any team-name spelling (e.g.
    "Brazil vs Morocco", "USA v Mexico"); :func:`evaluate_boost` splits and
    canonicalises it to match the model feed. ``market`` and ``selection`` are
    also free text as the book labels them.
    """

    site: str
    fixture: str            # e.g. "Brazil vs Morocco" (any team-name spelling)
    market: str             # e.g. "Match Result", "Over 2.5 Goals", "BTTS", "Correct Score"
    selection: str          # e.g. "Brazil", "Draw", "Over", "Yes", "2-1"
    boosted_odds: float     # decimal
    was_odds: Optional[float] = None
    is_inplay: bool = False


@dataclass
class BoostEval:
    """Verdict for a :class:`Boost` priced against the model feed.

    ``priceable`` is False when we cannot honestly derive a model probability
    (fixture not modelled, in-play, unsupported market, score off the grid);
    ``reason`` always explains the outcome. When priced, ``edge`` is
    ``boosted_odds * model_prob - 1`` and ``is_plus_ev`` is ``edge > MIN_EDGE``.
    """

    model_prob: Optional[float]
    fair_odds: Optional[float]
    edge: Optional[float]       # boosted_odds*model_prob - 1
    is_plus_ev: bool
    priceable: bool
    reason: str                 # human-readable; populated esp. when not priceable


# ---------------------------------------------------------------------------
# Feed loading.
# ---------------------------------------------------------------------------


def load_scores_feed(path: str = "site/scores_data.json") -> dict:
    """Load the model scores feed, tolerantly.

    Returns the parsed top-level dict (``{"meta":..., "fixtures":[...]}``). A
    missing file, unreadable bytes, or invalid JSON yields ``{}`` rather than
    raising — the caller (bot / daemon) treats an empty feed as "nothing
    priceable" and says so, never crashing on a stale or absent file.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


# ---------------------------------------------------------------------------
# Fixture matching.
# ---------------------------------------------------------------------------

# Split a "Home vs Away" fixture string. We accept "vs", "v", and a bare "-"
# (some books render boosts as "Brazil - Morocco"). The dash split is only used
# when no word separator is present, so a hyphenated team name is not mangled.
_VS_RE = re.compile(r"\bvs?\b", re.IGNORECASE)


def _split_fixture(fixture: str) -> Optional[Tuple[str, str]]:
    """Split "Home vs Away" into ``(home, away)`` or ``None`` if not two sides.

    Tries the word separators "vs"/"v" first, then a bare "-" as a fallback.
    Each side is canonicalised by the caller; here we only split.
    """
    if not fixture:
        return None
    parts = _VS_RE.split(fixture, maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        # Fall back to a dash separator ("Brazil - Morocco").
        if "-" in fixture:
            left, _, right = fixture.partition("-")
            if left.strip() and right.strip():
                return left.strip(), right.strip()
        return None
    return parts[0].strip(), parts[1].strip()


def _canon_pair(fixture: str) -> Optional[Tuple[str, str]]:
    """Return the canonicalised ``(home, away)`` teams of a fixture string."""
    sides = _split_fixture(fixture)
    if sides is None:
        return None
    return canonical(sides[0]), canonical(sides[1])


def _find_fixture(boost_fixture: str, feed: dict) -> Optional[dict]:
    """Locate the feed fixture matching ``boost_fixture`` (canonical team set).

    Matches by the *set* of canonical team names (order-independent), so a boost
    written "Morocco vs Brazil" still finds the feed's "Brazil vs Morocco".
    Falls back to a case-insensitive substring test on the raw fixture strings
    for spellings we do not yet have aliases for.
    """
    fixtures = feed.get("fixtures") if isinstance(feed, dict) else None
    if not isinstance(fixtures, list):
        return None

    boost_pair = _canon_pair(boost_fixture)
    boost_set = {boost_pair[0], boost_pair[1]} if boost_pair else None

    for fx in fixtures:
        if not isinstance(fx, dict):
            continue
        feed_fixture = fx.get("fixture") or ""
        if boost_set is not None:
            feed_pair = _canon_pair(feed_fixture)
            if feed_pair is not None and {feed_pair[0], feed_pair[1]} == boost_set:
                return fx
    # Substring fallback (no canonical-set hit): both boost teams appear in the
    # feed fixture text, or vice-versa.
    if boost_pair is not None:
        bl = [boost_pair[0].lower(), boost_pair[1].lower()]
        for fx in fixtures:
            if not isinstance(fx, dict):
                continue
            low = (fx.get("fixture") or "").lower()
            if all(t in low for t in bl):
                return fx
    return None


def _side_of_team(team: str, fixture_str: str) -> Optional[str]:
    """Return "home"/"away" for ``team`` within the feed fixture, else None."""
    pair = _canon_pair(fixture_str)
    if pair is None:
        return None
    t = canonical(team)
    if t == pair[0]:
        return "home"
    if t == pair[1]:
        return "away"
    # Loose containment for spellings that escape the alias table.
    tl = t.lower()
    if tl and tl in pair[0].lower():
        return "home"
    if tl and tl in pair[1].lower():
        return "away"
    return None


# ---------------------------------------------------------------------------
# Market classification helpers.
# ---------------------------------------------------------------------------

_INPLAY_RE = re.compile(r"\b(in-?play|live)\b", re.IGNORECASE)
_DRAW_RE = re.compile(r"\b(draw|tie|x)\b", re.IGNORECASE)
_OVER_RE = re.compile(r"\bover\b", re.IGNORECASE)
_UNDER_RE = re.compile(r"\bunder\b", re.IGNORECASE)
_YES_RE = re.compile(r"\byes\b", re.IGNORECASE)
_NO_RE = re.compile(r"\bno\b", re.IGNORECASE)
# A correct-score selection like "2-1", "2 - 1", "2:1".
_SCORE_RE = re.compile(r"^\s*(\d+)\s*[-:]\s*(\d+)\s*$")
# A goals line embedded in market/selection text, e.g. "Over 2.5 Goals".
_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _is_match_result(market: str) -> bool:
    m = market.lower()
    return any(
        kw in m
        for kw in (
            "match result",
            "match odds",
            "1x2",
            "full-time result",
            "full time result",
            "fulltime result",
            "match winner",
            "to win",
            "win the match",
            "result",
            "moneyline",
            "money line",
        )
    )


def _is_over_under(market: str, selection: str) -> bool:
    text = ("%s %s" % (market, selection)).lower()
    # "total corners"/"total cards"/"player shots" are props, not a goals line —
    # only treat a total/over-under as the *goals* market the feed can price.
    if any(prop in text for prop in ("corner", "card", "booking", "shot", "player")):
        return False
    if "over/under" in text or "over / under" in text or "total goals" in text:
        return True
    if "total" in text and "goal" in text:
        return True
    return "goals" in text and (
        bool(_OVER_RE.search(text)) or bool(_UNDER_RE.search(text))
    )


def _is_btts(market: str, selection: str) -> bool:
    text = ("%s %s" % (market, selection)).lower()
    return (
        "btts" in text
        or "both teams to score" in text
        or "both teams score" in text
    )


def _is_correct_score(market: str, selection: str) -> bool:
    if "correct score" in market.lower():
        return True
    return bool(_SCORE_RE.match(selection or ""))


def _is_player_prop(market: str, selection: str) -> bool:
    text = ("%s %s" % (market, selection)).lower()
    return any(
        kw in text
        for kw in (
            "goalscorer",
            "scorer",
            "to score",
            "assist",
            "card",
            "booking",
            "shots",
            "shot on target",
            "corner",
            "player",
            "first goal",
            "last goal",
            "hat-trick",
            "hat trick",
        )
    )


def _extract_line(market: str, selection: str) -> Optional[float]:
    """Pull a goals line (e.g. 2.5) out of the market/selection text."""
    for text in (selection, market):
        m = _LINE_RE.search(text or "")
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _grid_over_under(scores: List[dict], line: float) -> Tuple[Optional[float], Optional[float], float]:
    """Aggregate the correct-score grid into (over_prob, under_prob, total_mass).

    Sums ``scores[].prob`` (percentages) for scorelines strictly over / under
    ``line`` and divides by 100. Returns probabilities plus the total grid mass
    (also a probability) so the caller can judge whether the truncated grid
    covers enough mass to trust the re-aggregation.
    """
    over = 0.0
    under = 0.0
    total = 0.0
    for s in scores:
        if not isinstance(s, dict):
            continue
        m = _SCORE_RE.match(str(s.get("score", "")))
        prob = s.get("prob")
        if m is None or not isinstance(prob, (int, float)):
            continue
        goals = int(m.group(1)) + int(m.group(2))
        p = float(prob) / 100.0
        total += p
        if goals > line:
            over += p
        elif goals < line:
            under += p
        # goals == line is impossible for the .5 lines books boost on; an
        # integer line (push) is left out of both, matching exchange rules.
    return (over, under, total)


# ---------------------------------------------------------------------------
# Public pricing entry point.
# ---------------------------------------------------------------------------


def _verdict(model_prob: float, boosted_odds: float, reason: str) -> BoostEval:
    """Assemble a priced :class:`BoostEval` from a model probability."""
    fair = (1.0 / model_prob) if model_prob > 0 else None
    edge = boosted_odds * model_prob - 1.0
    return BoostEval(
        model_prob=model_prob,
        fair_odds=fair,
        edge=edge,
        is_plus_ev=edge > MIN_EDGE,
        priceable=True,
        reason=reason,
    )


def _unpriceable(reason: str) -> BoostEval:
    """Assemble an unpriceable verdict with an explanatory reason."""
    return BoostEval(
        model_prob=None,
        fair_odds=None,
        edge=None,
        is_plus_ev=False,
        priceable=False,
        reason=reason,
    )


def evaluate_boost(boost: Boost, scores_feed: dict) -> BoostEval:
    """Price a :class:`Boost` against the model feed. Pure and deterministic.

    Resolution order:

    1. **In-play** (``boost.is_inplay`` or "live"/"in-play" in the text) →
       unpriceable: the pre-match model is stale once a game is live.
    2. **Fixture match** — split ``boost.fixture`` on "vs"/"v"/"-", canonicalise
       each side, and match the feed by canonical team *set* (then substring).
       No match → unpriceable.
    3. **Market mapping**:
       - match result / 1X2 / "to win" → ``model_1x2[home|away|draw]``;
       - over/under / total goals → primary ``over_under`` line (÷100) if it
         matches, else re-derive from the correct-score grid;
       - BTTS → ``btts/100`` ("yes") or ``1 - btts/100`` ("no");
       - correct score "a-b" → matching ``scores`` entry (÷100), else
         unpriceable ("outside model grid");
       - goalscorer / player props → unpriceable (not in the feed).

    When priced: ``fair_odds = 1/model_prob``,
    ``edge = boosted_odds*model_prob - 1``, ``is_plus_ev = edge > MIN_EDGE``.
    """
    # 1) In-play — the model feed is pre-match only.
    text_blob = "%s %s" % (boost.market or "", boost.selection or "")
    if boost.is_inplay or _INPLAY_RE.search(text_blob):
        return _unpriceable("needs live model (in-play not priced)")

    # 2) Fixture.
    fx = _find_fixture(boost.fixture, scores_feed)
    if fx is None:
        return _unpriceable("fixture not in model feed")
    fixture_str = fx.get("fixture") or ""

    market = boost.market or ""
    selection = boost.selection or ""

    # Supported markets are matched FIRST (BTTS's "both teams to score" and a
    # correct-score "2-1" both look player-prop-ish via "to score"/score-pattern
    # heuristics, so claim them before the player-prop gate below).
    # 3a) Correct score (check before over/under so "2-1" isn't read as a line).
    if _is_correct_score(market, selection):
        m = _SCORE_RE.match(selection)
        if m is None:
            return _unpriceable("correct score selection unreadable")
        want = "%d-%d" % (int(m.group(1)), int(m.group(2)))
        for s in fx.get("scores") or []:
            if not isinstance(s, dict):
                continue
            sm = _SCORE_RE.match(str(s.get("score", "")))
            prob = s.get("prob")
            if sm is None or not isinstance(prob, (int, float)):
                continue
            if "%d-%d" % (int(sm.group(1)), int(sm.group(2))) == want:
                return _verdict(float(prob) / 100.0, boost.boosted_odds, "priced via correct-score grid")
        return _unpriceable("correct score outside model grid")

    # 3b) BTTS.
    if _is_btts(market, selection):
        btts = fx.get("btts")
        if not isinstance(btts, (int, float)):
            return _unpriceable("BTTS not modelled for this fixture")
        p_yes = float(btts) / 100.0
        if _NO_RE.search(selection) and not _YES_RE.search(selection):
            return _verdict(1.0 - p_yes, boost.boosted_odds, "priced via model BTTS (no = 1 - btts)")
        return _verdict(p_yes, boost.boosted_odds, "priced via model BTTS")

    # 3c) Over/Under / total goals.
    if _is_over_under(market, selection):
        is_over = bool(_OVER_RE.search(text_blob))
        is_under = bool(_UNDER_RE.search(text_blob))
        if not (is_over or is_under):
            return _unpriceable("over/under side not stated (need 'over' or 'under')")
        line = _extract_line(market, selection)
        ou = fx.get("over_under") if isinstance(fx.get("over_under"), dict) else {}
        feed_line = ou.get("line")
        # Primary line: use the model's over/under directly.
        if line is not None and isinstance(feed_line, (int, float)) and abs(float(feed_line) - line) < 1e-9:
            side_pct = ou.get("over") if is_over else ou.get("under")
            if not isinstance(side_pct, (int, float)):
                return _unpriceable("over/under not modelled for this fixture")
            return _verdict(float(side_pct) / 100.0, boost.boosted_odds, "priced via model over/under (primary line)")
        # If no line was stated, default to the model's primary line.
        if line is None and isinstance(feed_line, (int, float)):
            side_pct = ou.get("over") if is_over else ou.get("under")
            if isinstance(side_pct, (int, float)):
                return _verdict(
                    float(side_pct) / 100.0,
                    boost.boosted_odds,
                    "priced via model over/under (assumed primary line %.1f)" % float(feed_line),
                )
        # Non-primary line: re-aggregate from the correct-score grid.
        if line is None:
            return _unpriceable("over/under line not stated and no model line available")
        over_p, under_p, total = _grid_over_under(fx.get("scores") or [], line)
        # The grid is truncated to top scorelines; only trust the re-aggregation
        # when it captures most of the probability mass.
        if total < 0.80:
            return _unpriceable(
                "over/under line %.1f off the primary line and model grid too "
                "sparse to re-derive" % line
            )
        side_p = over_p if is_over else under_p
        return _verdict(side_p, boost.boosted_odds, "priced via correct-score grid for line %.1f" % line)

    # Player props (goalscorer, cards, shots, corners…) read like a result
    # market ("anytime goalscorer" + a name) but are not derivable from the
    # scores feed — gate them here, after the supported markets, before 1X2.
    if _is_player_prop(market, selection):
        return _unpriceable("player props not priced from scores feed")

    # 3d) Match result / 1X2.
    if _is_match_result(market) or _DRAW_RE.fullmatch(selection.strip()) or _side_of_team(selection, fixture_str):
        m1x2 = fx.get("model_1x2") if isinstance(fx.get("model_1x2"), dict) else None
        if not m1x2:
            return _unpriceable("1X2 not modelled for this fixture")
        if _DRAW_RE.fullmatch(selection.strip()):
            p = m1x2.get("draw")
            if not isinstance(p, (int, float)):
                return _unpriceable("draw probability missing in model feed")
            return _verdict(float(p), boost.boosted_odds, "priced via model_1x2 (draw)")
        side = _side_of_team(selection, fixture_str)
        if side is None:
            return _unpriceable("could not match boost selection to a team in the fixture")
        p = m1x2.get(side)
        if not isinstance(p, (int, float)):
            return _unpriceable("model_1x2 missing the %s probability" % side)
        return _verdict(float(p), boost.boosted_odds, "priced via model_1x2 (%s)" % side)

    # Anything else: unknown market we cannot map onto the feed.
    return _unpriceable("market not supported by the scores feed (v1)")
