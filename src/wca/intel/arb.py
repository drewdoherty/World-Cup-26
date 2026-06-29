"""Cross-venue arbitrage scanner for the Market Intelligence subsystem.

Detects three families of risk-free lock across the venues we track:

a. **cross_book** — back the BEST decimal odds for every selection of a
   complete market across all venues; an arb exists iff Σ(1/best) < 1.
b. **back_lay**   — back a selection at a sportsbook and lay it on an exchange
   (Betfair / Smarkets), net of commission.
c. **pm_book**    — a Polymarket YES that maps to a real-world selection paired
   against the complementary sportsbook outcome(s).

ALL commission / FX / lay / PM math is delegated to :mod:`wca.arbfx` /
:mod:`wca.arb` — this module only orchestrates (best-price selection, grouping,
staleness gating, formatting). Nothing here re-derives a net price.

HONEST LIMITS (why the `actionable` gate exists)
------------------------------------------------
Our exchange and Polymarket quotes arrive via the **OddsAPI relay**: there is no
order-book depth (``has_liquidity`` is False for relay venues) and the quotes can
be minutes-to-days stale.  A real, *executable* arb needs direct exchange/PM APIs
to confirm price AND size at the moment of execution.  So every opportunity is
marked ``actionable=False`` ("indicative — verify live") whenever any leg is
older than ``staleness_s`` OR an exchange/PM leg has unknown liquidity.  This is
a *screening* tool, not an execution tool — it NEVER places bets.

Pure / deterministic / network-free: ``now`` is injected so the staleness gate is
testable; only the bot handler (in ``wca.bot.app``) does any IO.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from wca import arb as _arb
from wca import arbfx as _arbfx
from wca.intel.registry import (
    EXCHANGE,
    PREDICTION_MARKET,
    commission_for,
    venue_for,
)

#: Default total stake we split across the arb legs when reporting absolute
#: sizes. Books are sized in GBP, PM in USD via ``DEFAULT_FX_USD_PER_GBP``.
DEFAULT_TOTAL_STAKE_GBP = 100.0

#: USD per GBP. The PM pool is sized in dollars (£1 ≈ $1.33 per the dual-pool
#: convention); the caller may override.
DEFAULT_FX_USD_PER_GBP = 1.33

#: A quote older than this (seconds) makes any opportunity it touches indicative.
#: Relay odds drift fast, so the default is deliberately tight (5 minutes).
DEFAULT_STALENESS_S = 300.0

#: Minimum guaranteed return to report (fraction). Below this the "arb" is noise
#: well inside relay quote error, so we suppress it.
DEFAULT_MIN_RETURN = 0.002

#: PM YES net-decimal converter and taker fee live in wca.arb.
_PM_TAKER_FEE_RATE = _arb.PM_TAKER_FEE_RATE


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArbLeg:
    """One side of a lock.

    ``side`` is ``"back"`` (back the selection), ``"lay"`` (lay it on an
    exchange) or ``"buy_yes"`` (buy a Polymarket YES share). ``currency`` is the
    native currency the stake is denominated in (GBP for books/exchanges, USD for
    Polymarket).
    """

    venue: str
    side: str
    selection: str
    odds: float              # raw quoted decimal odds (or PM YES price for buy_yes)
    net_odds: float          # fee/commission-adjusted decimal (from arbfx/arb)
    stake: float             # native-currency stake for the reported total
    currency: str
    quote_age_secs: Optional[float] = None
    stale: bool = False
    liquidity_known: bool = False


@dataclass(frozen=True)
class ArbOpportunity:
    """A detected cross-venue lock.

    ``actionable`` is True only when NO leg is stale and every exchange/PM leg has
    known liquidity — i.e. the opportunity could (in principle) be executed right
    now.  With relay odds it is almost always False; treat such rows as a
    watch-list, not a trade ticket.
    """

    fixture: str
    market: str
    arb_type: str            # "cross_book" | "back_lay" | "pm_book"
    legs: List[ArbLeg]
    guaranteed_return_pct: float
    total_stake: float
    total_stake_currency: str
    quote_age_secs: Optional[float]   # max leg age (overall)
    stale: bool                       # any leg stale
    liquidity_known: bool             # all exchange/PM legs have known depth
    actionable: bool
    confidence: str          # "indicative" | "executable"
    note: str = ""
    meta: Dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _age_secs(ts: Optional[str], now: datetime) -> Optional[float]:
    """Seconds between a quote ``ts`` and ``now`` (None if unparseable)."""
    d = _parse_ts(ts)
    if d is None:
        return None
    return (now - d).total_seconds()


def _venue_kind(row: Dict[str, object]) -> Optional[str]:
    """Kind for a snapshot row: prefer the stored ``venue_kind``, else registry."""
    kind = row.get("venue_kind")
    if kind:
        return str(kind)
    v = venue_for(str(row.get("venue", "")))
    return v.kind if v else None


def _liquidity_known(venue: str, row: Dict[str, object]) -> bool:
    """Whether this venue exposes real order-book depth right now.

    True only when the registry marks the venue ``has_liquidity`` AND the row
    actually carries a liquidity figure (relay rows never do).
    """
    v = venue_for(venue)
    if not (v and v.has_liquidity):
        return False
    return row.get("liquidity") is not None


def _net_back(venue: str, odds: float) -> float:
    """Commission-adjusted decimal for BACKING ``odds`` at ``venue``.

    Reuses :func:`wca.arb.effective_back` with the registry's commission so a
    plain sportsbook is unchanged and an exchange has its winnings haircut.
    """
    canon = venue_for(venue)
    book_key = canon.canon if canon else venue
    return _arb.effective_back(odds, book_key, {book_key: commission_for(venue)})


def _round_legs_stake(legs: Sequence[ArbLeg]) -> List[ArbLeg]:
    """Round stake to pennies/cents for display (immutable dataclass → rebuild)."""
    return [
        ArbLeg(
            venue=l.venue, side=l.side, selection=l.selection, odds=round(l.odds, 4),
            net_odds=round(l.net_odds, 4), stake=round(l.stake, 2), currency=l.currency,
            quote_age_secs=(round(l.quote_age_secs, 1) if l.quote_age_secs is not None else None),
            stale=l.stale, liquidity_known=l.liquidity_known,
        )
        for l in legs
    ]


def _assemble(*, fixture, market, arb_type, legs, return_pct, total_stake,
              total_stake_currency, staleness_s, note, meta):
    """Build an ArbOpportunity, computing the overall staleness / actionable gate."""
    legs = _round_legs_stake(legs)
    ages = [l.quote_age_secs for l in legs if l.quote_age_secs is not None]
    overall_age = max(ages) if ages else None
    any_stale = any(l.stale for l in legs)
    # Liquidity matters only for legs that actually hit an exchange or PM (a
    # sportsbook back is taken at the quoted price — depth is not our gate there).
    risk_legs = [l for l in legs if l.side in ("lay", "buy_yes")]
    liq_known = all(l.liquidity_known for l in risk_legs) if risk_legs else True
    actionable = (not any_stale) and liq_known
    return ArbOpportunity(
        fixture=fixture, market=market, arb_type=arb_type, legs=legs,
        guaranteed_return_pct=round(return_pct, 5),
        total_stake=round(total_stake, 2), total_stake_currency=total_stake_currency,
        quote_age_secs=(round(overall_age, 1) if overall_age is not None else None),
        stale=any_stale, liquidity_known=liq_known, actionable=actionable,
        confidence=("executable" if actionable else "indicative"),
        note=note, meta=meta,
    )


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #


def _best_back_per_selection(latest, now, staleness_s):
    """Best (highest) *raw* decimal back odds per selection, with the winning row.

    Returns ``{selection: {venue, odds, net, age, stale, liq, kind}}``.  "Best"
    ranks on the *raw* decimal so a backer's headline price wins; the net price is
    carried alongside for the arb test (commission only bites exchanges).
    """
    best: Dict[str, Dict[str, object]] = {}
    for sel, rows in latest.items():
        for r in rows:
            odds = r.get("decimal_odds")
            try:
                odds = float(odds)
            except (TypeError, ValueError):
                continue
            if odds <= 1.0:
                continue
            venue = str(r.get("venue", ""))
            cur = best.get(sel)
            if cur is None or odds > cur["odds"]:
                age = _age_secs(r.get("ts_utc"), now)
                best[sel] = {
                    "venue": venue, "odds": odds, "net": _net_back(venue, odds),
                    "age": age, "stale": (age is not None and age > staleness_s),
                    "liq": _liquidity_known(venue, r), "kind": _venue_kind(r),
                    "currency": "USD" if _venue_kind(r) == PREDICTION_MARKET else "GBP",
                }
    return best


def scan_cross_book(latest, *, market_type, fixture, now, staleness_s,
                    total_stake_gbp, fx_usd_per_gbp, min_return):
    """Family (a): best-price-per-selection back arb across all venues.

    A complete market (≥2 selections, every selection priced) where Σ(1/best
    net) < 1 is a back-only arb. Stake fractions equalise payout (delegated to
    :func:`wca.arb._arb_from_net`). Returns 0 or 1 opportunities for this market.
    """
    best = _best_back_per_selection(latest, now, staleness_s)
    if len(best) < 2:
        return []
    sels = list(best.keys())
    nets = [best[s]["net"] for s in sels]
    res = _arb._arb_from_net(nets)
    if res is None or res["profit_pct"] < min_return:
        return []

    fractions = res["stake_fractions"]
    legs: List[ArbLeg] = []
    for sel, frac in zip(sels, fractions):
        b = best[sel]
        gbp_stake = frac * total_stake_gbp
        cur = b["currency"]
        stake = gbp_stake * fx_usd_per_gbp if cur == "USD" else gbp_stake
        legs.append(ArbLeg(
            venue=b["venue"], side="back", selection=sel,
            odds=b["odds"], net_odds=b["net"], stake=stake, currency=cur,
            quote_age_secs=b["age"], stale=b["stale"],
            liquidity_known=b["liq"],
        ))
    return [_assemble(
        fixture=fixture, market=market_type, arb_type="cross_book", legs=legs,
        return_pct=res["profit_pct"], total_stake=total_stake_gbp,
        total_stake_currency="GBP", staleness_s=staleness_s,
        note="back best price per selection across venues; net of commission",
        meta={"n_legs": len(legs), "fx_usd_per_gbp": fx_usd_per_gbp},
    )]


def scan_back_lay(latest, lay_latest, *, market_type, fixture, now, staleness_s,
                  total_stake_gbp, min_return):
    """Family (b): back at a sportsbook, lay the SAME selection on an exchange.

    ``lay_latest`` is the ``latest_per_selection`` output for the *lay* market
    (relay stores it as ``moneyline_lay``). Lay net is from
    :func:`wca.arbfx.exchange_lay_net`; the pair is evaluated by
    :func:`wca.arbfx.evaluate_lock` (same-currency, no FX). One opp per selection
    that locks; the best (back venue × lay venue) pair wins.
    """
    if not lay_latest:
        return []
    opps: List[ArbOpportunity] = []
    for sel, back_rows in latest.items():
        lay_rows = lay_latest.get(sel) or []
        if not lay_rows:
            continue
        best = None
        for br in back_rows:
            try:
                back_odds = float(br.get("decimal_odds"))
            except (TypeError, ValueError):
                continue
            if back_odds <= 1.0:
                continue
            b_venue = str(br.get("venue", ""))
            # Backing on an exchange is allowed, but the lay leg must be a
            # *different* exchange venue; skip same-venue self-pairs.
            b_net = _net_back(b_venue, back_odds)
            for lr in lay_rows:
                l_venue = str(lr.get("venue", ""))
                lv = venue_for(l_venue)
                if not (lv and lv.kind == EXCHANGE):
                    continue  # only exchanges can be layed
                if l_venue == b_venue:
                    continue
                try:
                    lay_odds = float(lr.get("decimal_odds"))
                except (TypeError, ValueError):
                    continue
                if lay_odds <= 1.0:
                    continue
                lay_book = lv.canon
                lay_net = _arbfx.exchange_lay_net(lay_odds, _arbfx_venue_key(lay_book))
                if lay_net <= 1.0:
                    continue
                lock = _arbfx.evaluate_lock(
                    {"venue": b_venue, "currency": "GBP", "net": b_net,
                     "desc": "back %s" % sel, "fixture": fixture,
                     "market": market_type, "outcome": sel,
                     "confidence": "monitoring-grade"},
                    {"venue": lay_book, "currency": "GBP", "net": lay_net,
                     "desc": "lay %s" % sel, "confidence": "monitoring-grade"},
                    fx_usd_per_gbp=1.0,  # same currency → FX is a no-op
                    total_outlay_gbp=total_stake_gbp,
                )
                if lock is None or lock.guaranteed_pct < min_return:
                    continue
                if best is None or lock.guaranteed_pct > best[0].guaranteed_pct:
                    best = (lock, br, lr, back_odds, b_net, b_venue,
                            lay_odds, lay_net, lay_book)
        if best is None:
            continue
        lock, br, lr, back_odds, b_net, b_venue, lay_odds, lay_net, lay_book = best
        b_age = _age_secs(br.get("ts_utc"), now)
        l_age = _age_secs(lr.get("ts_utc"), now)
        legs = [
            ArbLeg(venue=b_venue, side="back", selection=sel, odds=back_odds,
                   net_odds=b_net, stake=lock.legs[0].stake, currency="GBP",
                   quote_age_secs=b_age, stale=(b_age is not None and b_age > staleness_s),
                   liquidity_known=_liquidity_known(b_venue, br)),
            ArbLeg(venue=lay_book, side="lay", selection=sel, odds=lay_odds,
                   net_odds=lay_net, stake=lock.legs[1].stake, currency="GBP",
                   quote_age_secs=l_age, stale=(l_age is not None and l_age > staleness_s),
                   liquidity_known=_liquidity_known(lay_book, lr)),
        ]
        opps.append(_assemble(
            fixture=fixture, market=market_type, arb_type="back_lay", legs=legs,
            return_pct=lock.guaranteed_pct, total_stake=total_stake_gbp,
            total_stake_currency="GBP", staleness_s=staleness_s,
            note="back at sportsbook, lay on exchange (net of commission); "
                 "exchange depth unknown on relay odds",
            meta={"selection": sel},
        ))
    return opps


def _arbfx_venue_key(canon: str) -> str:
    """Map a registry canon name to the lowercase key arbfx's commission map uses."""
    return {"Betfair": "betfair", "Smarkets": "smarkets"}.get(canon, canon.lower())


def scan_pm_book(latest, *, market_type, fixture, now, staleness_s,
                 total_stake_gbp, fx_usd_per_gbp, min_return):
    """Family (c): a Polymarket YES paired against the sportsbook side(s).

    Where a market has a Polymarket selection (PM YES, priced as a decimal via
    :func:`wca.arb.pm_yes_to_decimal`) AND the *other* selections are available
    at books, take the best book net for each of the others + the PM net and test
    Σ(1/net) < 1. This is the cross-venue 3-way (1X2) lock; for a complementary
    2-way market it degenerates to YES-vs-other naturally.
    """
    # Split PM rows from book rows per selection.
    pm_best: Dict[str, Dict[str, object]] = {}
    book_best: Dict[str, Dict[str, object]] = {}
    for sel, rows in latest.items():
        for r in rows:
            venue = str(r.get("venue", ""))
            kind = _venue_kind(r)
            try:
                odds = float(r.get("decimal_odds"))
            except (TypeError, ValueError):
                continue
            if kind == PREDICTION_MARKET:
                # PM rows are stored as decimal odds; recover the YES price.
                yes_price = 1.0 / odds if odds and odds > 1.0 else None
                if yes_price is None or not (0.0 < yes_price < 1.0):
                    continue
                net = _arb.pm_yes_to_decimal(yes_price, _PM_TAKER_FEE_RATE)
                age = _age_secs(r.get("ts_utc"), now)
                cur = pm_best.get(sel)
                if cur is None or net > cur["net"]:
                    pm_best[sel] = {"venue": venue, "yes_price": yes_price,
                                    "net": net, "age": age,
                                    "stale": (age is not None and age > staleness_s),
                                    "liq": _liquidity_known(venue, r)}
            else:
                if odds <= 1.0:
                    continue
                net = _net_back(venue, odds)
                cur = book_best.get(sel)
                if cur is None or net > cur["net"]:
                    age = _age_secs(r.get("ts_utc"), now)
                    book_best[sel] = {"venue": venue, "odds": odds, "net": net,
                                      "age": age,
                                      "stale": (age is not None and age > staleness_s),
                                      "liq": _liquidity_known(venue, r)}
    if not pm_best or not book_best:
        return []

    opps: List[ArbOpportunity] = []
    for pm_sel, pm in pm_best.items():
        others = [s for s in book_best if s != pm_sel]
        if not others:
            continue
        nets = [pm["net"]] + [book_best[s]["net"] for s in others]
        res = _arb._arb_from_net(nets)
        if res is None or res["profit_pct"] < min_return:
            continue
        fracs = res["stake_fractions"]
        legs: List[ArbLeg] = []
        pm_gbp = fracs[0] * total_stake_gbp
        legs.append(ArbLeg(
            venue=pm["venue"], side="buy_yes", selection=pm_sel,
            odds=pm["yes_price"], net_odds=pm["net"],
            stake=pm_gbp * fx_usd_per_gbp, currency="USD",
            quote_age_secs=pm["age"], stale=pm["stale"], liquidity_known=pm["liq"],
        ))
        for s, frac in zip(others, fracs[1:]):
            bb = book_best[s]
            legs.append(ArbLeg(
                venue=bb["venue"], side="back", selection=s, odds=bb["odds"],
                net_odds=bb["net"], stake=frac * total_stake_gbp, currency="GBP",
                quote_age_secs=bb["age"], stale=bb["stale"], liquidity_known=bb["liq"],
            ))
        opps.append(_assemble(
            fixture=fixture, market=market_type, arb_type="pm_book", legs=legs,
            return_pct=res["profit_pct"], total_stake=total_stake_gbp,
            total_stake_currency="GBP", staleness_s=staleness_s,
            note="buy Polymarket YES + back complementary outcome(s) at books; "
                 "FX + PM-depth risk → indicative",
            meta={"pm_selection": pm_sel, "fx_usd_per_gbp": fx_usd_per_gbp},
        ))
    return opps


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def scan_market(latest, *, market_type, fixture, now,
                lay_latest=None, staleness_s=DEFAULT_STALENESS_S,
                total_stake_gbp=DEFAULT_TOTAL_STAKE_GBP,
                fx_usd_per_gbp=DEFAULT_FX_USD_PER_GBP,
                min_return=DEFAULT_MIN_RETURN) -> List[ArbOpportunity]:
    """Scan one fixture×market for all three arb families.

    ``latest`` is :func:`wca.intel.store.latest_per_selection` output for the
    BACK market (``{selection: [rows]}``). ``lay_latest`` (optional) is the same
    for the matching lay market (enables back-vs-lay). ``now`` is a tz-aware
    datetime (injected — keeps the staleness gate deterministic/testable).

    Returns opportunities sorted by guaranteed return desc. Each is honestly
    gated: ``actionable=False`` whenever a leg is stale or exchange/PM depth is
    unknown (the norm on relay odds — see module docstring).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    opps: List[ArbOpportunity] = []
    opps += scan_cross_book(
        latest, market_type=market_type, fixture=fixture, now=now,
        staleness_s=staleness_s, total_stake_gbp=total_stake_gbp,
        fx_usd_per_gbp=fx_usd_per_gbp, min_return=min_return)
    opps += scan_back_lay(
        latest, lay_latest or {}, market_type=market_type, fixture=fixture,
        now=now, staleness_s=staleness_s, total_stake_gbp=total_stake_gbp,
        min_return=min_return)
    opps += scan_pm_book(
        latest, market_type=market_type, fixture=fixture, now=now,
        staleness_s=staleness_s, total_stake_gbp=total_stake_gbp,
        fx_usd_per_gbp=fx_usd_per_gbp, min_return=min_return)
    opps.sort(key=lambda o: o.guaranteed_return_pct, reverse=True)
    return opps


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

_TYPE_LABEL = {
    "cross_book": "cross-book back",
    "back_lay": "back/lay",
    "pm_book": "PM↔book",
}
_SIDE_LABEL = {"back": "back", "lay": "lay", "buy_yes": "buy YES"}
_CUR_SYM = {"GBP": "£", "USD": "$"}


def _fmt_age(secs: Optional[float]) -> str:
    if secs is None:
        return "age?"
    if secs < 90:
        return "%ds" % int(secs)
    if secs < 5400:
        return "%dm" % int(secs / 60)
    if secs < 36 * 3600:
        return "%.1fh" % (secs / 3600.0)
    return "%.0fd" % (secs / 86400.0)


def format_arb_report(opps: Sequence[ArbOpportunity], *, limit: int = 8,
                      now: Optional[datetime] = None) -> str:
    """Telegram-Markdown summary of detected arbs (most profitable first).

    Mirrors the bot's report style (bold title, emoji, per-leg breakdown). If
    nothing locks — or everything that does is stale/indicative — it says so
    honestly rather than implying a free lunch.
    """
    if not opps:
        return (
            "\U0001f50d *Arbitrage scan*\n"
            "No arbs found (Σ implied ≥ 1 on every market after fees).\n"
            "_Relay odds only — exchange/PM depth unknown; this is indicative._"
        )

    actionable = [o for o in opps if o.actionable]
    header_note = ""
    if not actionable:
        header_note = (
            "⚠️ *All indicative* — every candidate has a stale or "
            "depth-unknown leg (relay odds). Verify live before trusting.\n"
        )

    lines = ["\U0001f50d *Arbitrage scan* — %d candidate%s" %
             (len(opps), "" if len(opps) == 1 else "s")]
    if header_note:
        lines.append(header_note.rstrip())
    lines.append("")

    for o in opps[:limit]:
        flag = "✅ executable" if o.actionable else "\U0001f7e1 indicative"
        lines.append(
            "*%s* — %s [%s]" % (o.fixture or "?", _TYPE_LABEL.get(o.arb_type, o.arb_type), o.market)
        )
        lines.append(
            "  return *%.2f%%* on %s%.0f | %s | oldest %s" % (
                o.guaranteed_return_pct * 100.0,
                _CUR_SYM.get(o.total_stake_currency, ""), o.total_stake,
                flag, _fmt_age(o.quote_age_secs),
            )
        )
        for l in o.legs:
            sym = _CUR_SYM.get(l.currency, "")
            stale_mark = " ⚠️" if l.stale else ""
            lines.append(
                "    %s %s @ %.3f → %s%.2f (%s, %s)%s" % (
                    _SIDE_LABEL.get(l.side, l.side), l.selection, l.odds,
                    sym, l.stake, l.venue, _fmt_age(l.quote_age_secs), stale_mark,
                )
            )
        lines.append("")

    if len(opps) > limit:
        lines.append("_…and %d more (showing top %d by return)._" % (len(opps) - limit, limit))
    lines.append("_Indicative only; never auto-bet. Executable arb needs direct "
                 "exchange/PM APIs (live price + depth)._")
    return "\n".join(lines).rstrip()
