"""Project-wide Polymarket bankroll & sizing rule — the SINGLE SOURCE OF TRUTH.

GLOBAL RULE (applies everywhere Polymarket is sized, not just the test book):

* Polymarket accounting is done in **USD** (``$``).
* The staking bankroll is **£3,000 ± realised P&L**, converted to USD at the
  project FX rate **$1.33 = £1**.
* Position size is **¼-Kelly** of that USD bankroll.

Because £3,000 → $3,990 and realised P&L is already booked in USD, the bankroll
in USD is simply ``3000·1.33 + realised_pnl_usd`` (the £-first and $-first
readings are algebraically identical). Import from here — never re-hardcode the
base, the FX rate, or the Kelly fraction anywhere else.
"""

from __future__ import annotations

import os

# --- The rule (env-overridable so ops can retune without a code change). ------
GBP_PM_BANKROLL_BASE = float(os.environ.get("WCA_PM_BANKROLL_GBP", "3000"))  # £
GBP_USD = float(os.environ.get("WCA_GBP_USD", "1.33"))                        # $ per £
PM_KELLY_FRACTION = float(os.environ.get("WCA_PM_KELLY", "0.25"))            # ¼-Kelly
PM_MAX_STAKE_FRAC = float(os.environ.get("WCA_PM_MAXFRAC", "0.04"))          # per-bet cap: 4% of bankroll
PM_BOOK_CAP_FRAC = float(os.environ.get("WCA_PM_BOOKCAP", "0.75"))           # whole-book cap: 75% of bankroll
PM_CCY = "$"


def gbp_to_usd(gbp: float) -> float:
    """Convert £ to $ at the project FX rate ($1.33 = £1)."""
    return float(gbp) * GBP_USD


def pm_bankroll_usd(realized_pnl_usd: float = 0.0) -> float:
    """Live Polymarket bankroll in USD = £3,000 ± realised P&L, at $1.33 = £1.

    ``realized_pnl_usd`` is the strategy's realised P&L to date, already in USD.
    """
    return gbp_to_usd(GBP_PM_BANKROLL_BASE) + float(realized_pnl_usd or 0.0)


def size_placement(q, p, bankroll: float, *, kelly_frac: float = None,
                   max_frac: float = None) -> dict:
    """Per-bet fractional-Kelly stake on a binary-YES at price ``p``, belief ``q``.

    ``f* = (q-p)/(1-p)`` floored at 0; fraction = ``kelly_frac·f*`` capped at
    ``max_frac`` (the per-bet ceiling). Defaults come from the global rule
    (¼-Kelly, 4% cap). Returns ``{frac, stake, f_star, capped}``.
    """
    kf = PM_KELLY_FRACTION if kelly_frac is None else kelly_frac
    mf = PM_MAX_STAKE_FRAC if max_frac is None else max_frac
    try:
        q, p, bk = float(q), float(p), float(bankroll)
    except (TypeError, ValueError):
        return {"frac": 0.0, "stake": 0.0, "f_star": 0.0, "capped": False}
    f_star = max(0.0, (q - p) / (1.0 - p)) if 0.0 < p < 1.0 else 0.0
    frac = kf * f_star
    capped = bool(mf and frac > mf)
    if capped:
        frac = mf
    return {"frac": frac, "stake": frac * bk, "f_star": f_star, "capped": capped}


def book_scale(new_total: float, existing_exposure: float, bankroll: float, *,
               book_cap_frac: float = None) -> float:
    """Proportional scale (≤1) so existing + new exposure fits the whole-book cap.

    The book (open positions + this pass's new stakes) is capped at
    ``book_cap_frac`` of bankroll (75% by default). Returns the factor to apply to
    the NEW stakes: 1.0 if they fit, <1.0 to shrink them, 0.0 if the book is
    already at/over the cap.
    """
    bcf = PM_BOOK_CAP_FRAC if book_cap_frac is None else book_cap_frac
    cap = bcf * float(bankroll)
    avail = max(0.0, cap - float(existing_exposure or 0.0))
    nt = float(new_total or 0.0)
    if nt <= 0.0 or nt <= avail:
        return 1.0
    return avail / nt
