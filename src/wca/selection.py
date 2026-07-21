# src/wca/selection.py — SINGLE source of truth for the desk selection rule.
# Extracted from scripts/wca_pm_propose.py (2026-07-07). Treat as a
# human-approved-change file (like the execution caps): editing PROB_BUCKETS,
# LONGSHOT_PROB, or preference_sort_key moves ALL real-money orderings at once.
"""Canonical desk selection rule — the ONE place the rule lives.

Every bet-ranking / selection / sizing surface in the codebase imports from
this module so the rule can never drift between surfaces again. See
``docs/SELECTION_RULES.md`` for the full spec and the per-surface compliance
table.

The rule (canonical, user-confirmed 2026-07-07; refined 2026-07-09)
------------------------------------------------------------------
1. **Bucket by MODEL probability (PRIMARY sort).** ``moneyline`` = model
   ``>= 0.50``; ``mid`` = ``0.25 <= model < 0.50``; ``longshot`` = model
   ``< 0.25``. A higher bucket ALWAYS ranks above a lower bucket, regardless
   of EV.
2. **Further-out fixtures first (SECONDARY) — CATEGORY-CONDITIONAL (2026-07-09).**
   Raw continuous hours-to-kickoff, descending — but ONLY for multi-week
   futures / advancement markets. For 90-minute MATCH markets the hours-out
   term is NEUTRALISED (contributes 0) so EV breaks ties within the bucket.
   The hours value, when it applies, is a continuous float, never bucketed.
3. **EV breaks ties** — within the same bucket + further-out tier for futures;
   within the bucket directly for match markets (where hours-out is neutral).
4. **No cash on longshots (model < 0.25).** Free-bet / lottery only: stake is
   forced to 0 and the side is flagged (it may still be DISPLAYED, dimmed).

The 2026-07-09 category-conditional refinement
----------------------------------------------
Backtest (2026-07-09, n=1,046 resolved PM markets, composition-controlled,
look-ahead-guarded) found:

* **Match (90-min) markets** — PM efficiency is FLAT from 168h out to kickoff
  (fixed-cohort Brier 0.131 -> 0.124, CIs overlap); early entry earns ~0 after
  fees inside 72h and a small PENALTY inside 12h. The apparent early premium is
  a favourite-firms / longshot-bleeds drift already captured by BUCKETING plus
  ``longshot_no_cash``. => "further-out-first" has NO basis for match markets;
  the hours-out term is neutralised there and EV breaks ties within the bucket.
* **Multi-week futures / advancement** — a REAL tradeable early edge (+6-7% at
  24-72h, n=60, truncation-caveated). => further-out-first is KEPT there.

So the ``-hours_out`` secondary term is now conditional on the candidate's
market category (:data:`MARKET_MATCH` vs :data:`MARKET_FUTURES`). Futures /
advancement surfaces order by DEEPER STAGE FIRST via their own ``stage_further_out``
depth map (``wca.advancement.stage_further_out`` etc.) — a separate secondary
key that was never routed through :func:`hours_out`, so those surfaces are
already correct and unchanged. ``preference_sort_key`` keeps a
``market_kind`` opt-in so any hours-driven futures surface stays further-out-first.

Default (documented): a candidate that does not declare a category is treated
as :data:`MARKET_MATCH` (hours-out NEUTRAL). Match markets are the bulk and the
evidence says neutral is correct for them; futures must OPT IN to
further-out-first (pass ``market_kind=MARKET_FUTURES`` or set a futures
settlement/category on the candidate).

The 2026-07-07 REPLACE ruling
-----------------------------
"Longshot" is now defined PURELY by model prob ``< 0.25``. This RETIRES the
older 2026-06-29 "cut all market outright-underdogs regardless of prob"
decision. A market outsider the model rates 25-49% is now a STAKEABLE MID.
The market-relative FAV / 2ND-FAV / longshot categories
(``card.classify_outcome`` / ``card._CATEGORY_PRIORITY``) survive ONLY as
cosmetic display labels — they must NOT feed the sort key or the cash-cut
predicate anymore.

Design invariants (do NOT "improve")
------------------------------------
* ``hours_out`` stays a continuous raw-float value — never bucketed. It is
  the raw hours-to-kickoff; whether it FEEDS the sort is decided by
  :func:`hours_out_term` from the candidate's ``market_kind`` (2026-07-09).
* The category-conditional hours term lives in ONE place —
  :func:`hours_out_term`. Every surface that builds its own inline sort key
  (``card.rank_card``, ``accas.rank_key``, ``wca_betrecs._singles_sort_key``)
  MUST call it rather than hardcoding ``-hours``; never re-derive the
  match/futures conditional at a call site.
* ``preference_sort_key`` ONLY deprioritises longshots (rank 2); it does NOT
  enforce the cash ban. The cash ban is ``longshot_no_cash()``, applied at the
  SIZING step and kept SEPARATE so a surface can display a longshot dimmed
  while sizing it at zero.
* Boundaries: ``>= 0.50`` moneyline, ``>= 0.25`` mid, ``< 0.25`` longshot
  (inclusive lower bounds). The cash floor is a strict ``< 0.25``.
"""
from typing import Any, Mapping, Optional
import datetime as _dt

# Ordered high->low; inclusive lower bounds.
PROB_BUCKETS = ((0.50, "moneyline"), (0.25, "mid"), (0.0, "longshot"))
_BUCKET_RANK = {"moneyline": 0, "mid": 1, "longshot": 2}
LONGSHOT_PROB = 0.25  # cash floor: sides below this are free-bet/lottery only, NEVER cash

# --- Market category (2026-07-09) -------------------------------------------
# The "further-out-first" secondary key is CONDITIONAL on category:
#   * MARKET_MATCH   — 90-minute match markets (1X2, totals, BTTS, spreads,
#                      event-props that settle at 90'). Hours-out is NEUTRAL:
#                      EV breaks ties within the bucket.
#   * MARKET_FUTURES — multi-week futures / advancement (settlement ET+pens,
#                      advancement / group_winner / outright / "win"). Keep
#                      further-out-first (hours-out retained where it drives
#                      the sort; stage-depth surfaces use their own depth map).
# SAFE DEFAULT: an undeclared candidate is treated as MARKET_MATCH (hours-out
# neutral) — match is the bulk and the backtest says neutral is correct there;
# futures must OPT IN to further-out-first.
MARKET_MATCH = "match"
MARKET_FUTURES = "futures"

# Settlement / category tokens that mark a candidate as multi-week futures.
# Everything else (incl. "90min", 1X2, totals, btts, spreads, unknown) is
# treated as a MATCH market. Compared case-insensitively as substrings on the
# candidate's settlement / market / family / kind fields. NOTE the bare
# tournament-winner token "win" is intentionally NOT a loose substring (it
# would false-match "winner", "winning margin", etc.); the winner-futures case
# is caught by "outright" / "to_win" / "tournament" / an explicit
# market_kind=MARKET_FUTURES instead. A single-match "Team to Advance"
# (settlement ET+pens but resolved within a match) is deliberately still a
# MATCH market — the ruling is about MULTI-WEEK futures, keyed off tournament
# advancement/outright, not any ET+pens leg.
_FUTURES_MARKERS = (
    "advancement", "group_winner", "group winner", "outright",
    "to_win", "tournament", "futures", "reach_",
)


def prob_bucket(model_prob):
    """Bucket a MODEL probability into ``moneyline`` / ``mid`` / ``longshot``.

    * ``moneyline`` — model ``>= 0.50``.
    * ``mid``       — ``0.25 <= model < 0.50``.
    * ``longshot``  — model ``< 0.25``.

    Boundaries are inclusive lower bounds (0.50 -> moneyline, 0.25 -> mid,
    0.2499 -> longshot). ``None`` / falsy inputs are treated as 0.0.
    """
    prob = float(model_prob or 0.0)
    for lo, name in PROB_BUCKETS:
        if prob >= lo:
            return name
    return "longshot"


def bucket_rank(model_prob):
    """Sortable integer bucket rank (lower ranks higher).

    ``moneyline`` -> 0, ``mid`` -> 1, ``longshot`` -> 2. This is the PRIMARY
    sort key everywhere the desk selection rule applies.
    """
    return _BUCKET_RANK[prob_bucket(model_prob)]


def longshot_no_cash(model_prob):
    """True when this side is a <25c longshot -> no cash (free-bet/lottery only).

    This is the cash-floor predicate, kept SEPARATE from
    :func:`preference_sort_key` so a surface can display a longshot (dimmed)
    while sizing it at zero. The floor is a strict ``< 0.25`` (a model prob of
    exactly 0.25 is a stakeable ``mid``, NOT a longshot).
    """
    return float(model_prob or 0.0) < LONGSHOT_PROB


# Sides whose position pays out WITH the quoted outcome vs AGAINST it.
_POSITION_SIDE_SAME = frozenset({"yes", "back"})
_POSITION_SIDE_FLIP = frozenset({"no", "lay"})


def position_prob(model_prob, side):
    """Model probability that the POSITION ACTUALLY HELD pays out (2026-07-14).

    THE RULE: the bucket (:func:`prob_bucket` / :func:`bucket_rank`) and the
    cash floor (:func:`longshot_no_cash`) key on the probability of the
    POSITION HELD, not on the market's headline/YES outcome. Buying NO on a
    market whose YES outcome the model rates 0.2256 is a 0.7744 position —
    moneyline bucket, cash-eligible; a YES/back position at 0.17 is still a
    longshot (no cash). Every surface that buckets or cash-gates a SIDED
    position (Polymarket YES/NO, exchange back/lay) must route the probability
    through this helper before calling the bucket / cash-floor predicates.

    * ``side`` in {"YES", "back"} (case-insensitive), or ``None``/"" (no side
      concept — the outcome itself is the position) -> ``model_prob``.
    * ``side`` in {"NO", "lay"} (case-insensitive) -> ``1 - model_prob``.
    * any other side raises ``ValueError`` — a mistyped side silently treated
      as YES/back would mislabel a real-money position.
    * ``model_prob=None`` returns ``None``, which FAILS SAFE downstream
      (``prob_bucket(None)`` -> longshot; ``longshot_no_cash(None)`` -> True)
      instead of inventing a 1.0 NO-side probability from a missing input.
    """
    if model_prob is None:
        return None
    s = "" if side is None else str(side).strip().lower()
    if not s or s in _POSITION_SIDE_SAME:
        return float(model_prob)
    if s in _POSITION_SIDE_FLIP:
        return 1.0 - float(model_prob)
    raise ValueError(
        "position_prob: unknown side %r (expected YES/NO/back/lay)" % (side,))


def resolve_market_kind(*hints):
    """Resolve a candidate's market category -> ``MARKET_MATCH`` / ``MARKET_FUTURES``.

    Pass any category hints in priority order: an explicit ``market_kind``, a
    ``settlement`` basis, a ``market`` / ``family`` label, a ``stage`` token,
    etc. The FIRST hint that is an explicit ``MARKET_MATCH`` / ``MARKET_FUTURES``
    wins verbatim; otherwise a hint is scanned (case-insensitive substring) for
    a multi-week-futures marker (:data:`_FUTURES_MARKERS`).

    SAFE DEFAULT: if nothing declares futures, return :data:`MARKET_MATCH`
    (hours-out NEUTRAL). Match is the bulk of the book and the 2026-07-09
    backtest says neutral is correct there; futures must OPT IN. See the module
    docstring for the ruling.
    """
    for h in hints:
        if h is None:
            continue
        s = str(h).strip().lower()
        if not s:
            continue
        if s == MARKET_MATCH:
            return MARKET_MATCH
        if s == MARKET_FUTURES:
            return MARKET_FUTURES
        if any(mark in s for mark in _FUTURES_MARKERS):
            return MARKET_FUTURES
    return MARKET_MATCH


def hours_out_term(hours, market_kind=MARKET_MATCH):
    """The CATEGORY-CONDITIONAL secondary sort contribution (2026-07-09).

    Returns the value to place in the sort key's secondary slot:

    * ``MARKET_FUTURES`` -> ``-float(hours)`` (further-out-first KEPT — deeper /
      later-resolving markets rank first, the proven multi-week early edge).
    * ``MARKET_MATCH`` (default, incl. unknown) -> ``0.0`` (hours-out
      NEUTRALISED — EV breaks ties within the bucket for 90-min match markets).

    This is the ONE place the match/futures conditional lives. Every surface —
    including those that build their own inline sort key (``card.rank_card``,
    ``accas.rank_key``, ``wca_betrecs._singles_sort_key``) — MUST route its
    secondary term through here rather than hardcoding ``-hours``, so the
    conditional can never drift between surfaces.
    """
    if market_kind == MARKET_FUTURES:
        return -float(hours or 0.0)
    return 0.0


def hours_out(p, kick_by_match=None, now_dt=None):
    """Continuous hours until the proposal's fixture kicks off (0.0 when unknown).

    ``p`` is a mapping-like proposal with a ``match_desc`` key; ``kick_by_match``
    maps ``match_desc`` -> kickoff timestamp. Returns a raw float (never
    bucketed). Whether this value FEEDS the sort is decided by
    :func:`hours_out_term` from the candidate's category (2026-07-09): for
    ``MARKET_FUTURES`` further-out fixtures rank first; for ``MARKET_MATCH`` the
    term is neutral. Unknown / unparseable kickoffs return 0.0.
    """
    import pandas as _pd

    ts = (kick_by_match or {}).get(str(p.get("match_desc") or ""))
    if not ts:
        return 0.0
    try:
        k = _pd.to_datetime(ts, utc=True).tz_convert(None)
        ref = now_dt or _dt.datetime.utcnow()
        return max(0.0, (k - ref).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def preference_sort_key(p, kick_by_match=None, now_dt=None, market_kind=None):
    """Canonical desk ordering: ``(bucket_rank, hours_term, -ev)`` (2026-07-09).

    1. ``bucket_rank`` (model-prob bucket) — moneyline over mid over longshot,
       ALWAYS, regardless of EV.
    2. ``hours_term`` — CATEGORY-CONDITIONAL (:func:`hours_out_term`):
       * multi-week FUTURES / advancement -> ``-hours_out`` (further-out
         fixtures first — the proven early edge; continuous raw float);
       * 90-min MATCH markets (default) -> ``0.0`` (NEUTRAL — the backtest
         found no early premium after fees, so EV breaks ties within the
         bucket directly).
    3. ``-ev`` — EV descending. For match markets this is the effective
       secondary key (hours neutral); for futures it breaks ties within the
       same bucket + further-out tier.

    ``market_kind`` selects the branch. Resolution order:
      * the explicit ``market_kind`` argument, else
      * the candidate's ``market_kind`` / ``settlement`` / ``market`` /
        ``family`` / ``stage`` fields (via :func:`resolve_market_kind`), else
      * the SAFE DEFAULT :data:`MARKET_MATCH` (hours NEUTRAL). Futures surfaces
        must OPT IN. See the module docstring for the 2026-07-09 ruling.

    NOTE: this key ONLY deprioritises longshots (they sort last). It does NOT
    enforce the cash ban — that is :func:`longshot_no_cash`, applied at the
    sizing step.
    """
    kind = market_kind or resolve_market_kind(
        p.get("market_kind"), p.get("settlement"), p.get("market"),
        p.get("family"), p.get("stage"),
    )
    return (
        bucket_rank(p.get("model_prob")),
        hours_out_term(hours_out(p, kick_by_match, now_dt), kind),
        -float(p.get("ev") or 0.0),
    )
