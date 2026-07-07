# src/wca/selection.py — SINGLE source of truth for the desk selection rule.
# Extracted from scripts/wca_pm_propose.py (2026-07-07). Treat as a
# human-approved-change file (like the execution caps): editing PROB_BUCKETS,
# LONGSHOT_PROB, or preference_sort_key moves ALL real-money orderings at once.
"""Canonical desk selection rule — the ONE place the rule lives.

Every bet-ranking / selection / sizing surface in the codebase imports from
this module so the rule can never drift between surfaces again. See
``docs/SELECTION_RULES.md`` for the full spec and the per-surface compliance
table.

The rule (canonical, user-confirmed 2026-07-07)
-----------------------------------------------
1. **Bucket by MODEL probability (PRIMARY sort).** ``moneyline`` = model
   ``>= 0.50``; ``mid`` = ``0.25 <= model < 0.50``; ``longshot`` = model
   ``< 0.25``. A higher bucket ALWAYS ranks above a lower bucket, regardless
   of EV.
2. **Further-out fixtures first (SECONDARY).** Raw continuous hours-to-kickoff,
   descending — thin/soft early markets are more likely mispriced. This is a
   continuous float; it is never bucketed into day-tiers.
3. **EV breaks ties ONLY (tertiary)** — within the same bucket + further-out
   tier.
4. **No cash on longshots (model < 0.25).** Free-bet / lottery only: stake is
   forced to 0 and the side is flagged (it may still be DISPLAYED, dimmed).

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
* ``hours_out`` stays a continuous raw-float secondary key — never bucketed.
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


def hours_out(p, kick_by_match=None, now_dt=None):
    """Continuous hours until the proposal's fixture kicks off (0.0 when unknown).

    ``p`` is a mapping-like proposal with a ``match_desc`` key; ``kick_by_match``
    maps ``match_desc`` -> kickoff timestamp. Returns a raw float (never
    bucketed): the SECONDARY sort key, used descending so further-out fixtures
    rank first. Unknown / unparseable kickoffs return 0.0 (sorts as imminent).
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


def preference_sort_key(p, kick_by_match=None, now_dt=None):
    """Canonical desk ordering: ``(bucket_rank, -hours_out, -ev)``.

    1. ``bucket_rank`` (model-prob bucket) — moneyline over mid over longshot,
       ALWAYS, regardless of EV.
    2. ``-hours_out`` — further-out fixtures first (thin/soft early markets are
       more likely mispriced). Continuous raw float, never bucketed.
    3. ``-ev`` — EV descending, breaking ties ONLY within the same bucket +
       further-out tier.

    NOTE: this key ONLY deprioritises longshots (they sort last). It does NOT
    enforce the cash ban — that is :func:`longshot_no_cash`, applied at the
    sizing step.
    """
    return (
        bucket_rank(p.get("model_prob")),
        -hours_out(p, kick_by_match, now_dt),
        -float(p.get("ev") or 0.0),
    )
