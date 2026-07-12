"""HL<->Polymarket cross-venue pair map + fee-adjusted gap/arb math.

SHADOW / MONITOR-ONLY. Nothing in this module (or anything that imports it)
places, sizes, parks, or recommends a trade. The only outputs are the
watch-labels ``XV_WATCH`` / ``XV_ARB_CANDIDATE`` / ``XV_MISMATCHED_SETTLEMENT``
(+ ``XV_NO_DATA`` when a book is missing) — never PLACE/FIRE. Hyperliquid is a
NEW venue: per the CLAUDE.md live-money gate it needs price capture + CLV
stamping + settlement automation (plus the go/no-go criteria in
``docs/research/hl_venue_recon_2026-07-09.md``) before real money.

Everything below is pinned to raw API evidence captured 2026-07-09 ~18:15 UTC
(recon dump directory; load-bearing books preserved under
``tests/fixtures/hl_xvenue/``). Key citations:

* Pair universe: HL ``outcomeMeta`` (12 WC markets) x PM gamma events
  ``world-cup-winner`` (30615, negRisk) + ``world-cup-nation-to-reach-
  semifinals`` (551781) -> exactly 16 settlement-matched pairs (8 champion +
  8 QF team-sides). PM per-match 1X2 NEVER pairs with the HL QF markets:
  PM 1X2 is 3-way and settles on the FIRST 90 MINUTES ONLY, while HL QF
  markets are 2-way, ET+pens-inclusive, with a 0.5-void tail — different
  contracts (HL France 77.5c vs PM France-90min 60.4c is basis, not edge).
* Fee model: PM sports taker fee = ``0.03 * p * (1-p)`` per share, taker-only,
  makers 0 (PM fee docs; same constant as ``wca.advancement.PM_TAKER_FEE_COEF``
  — parity is asserted in tests). HL TRADING fee = 0 ("Fees are currently
  zero for outcome markets for initial testing", HIP-4 docs; empirically 497
  of 502 outcome fills on an active wallet had fee=0.0, the 5 non-zero were
  1.5bp BUILDER-code fees on crossed sells, not protocol fees). HL SETTLEMENT
  fee is UNVERIFIED (outcome.xyz fee docs reserve one; no market spec or fill
  evidence proves it zero) — exposed as an explicit parameter + caveat, and a
  blocking item on the pre-money gate list.
* Settlement bases + divergence tails: verbatim market descriptions in the
  HL ``outcomeMeta`` dump and PM gamma dumps; see ``CHAMPION_*``/``QF_*``
  basis strings and the per-direction tail table below.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Fee model
# ---------------------------------------------------------------------------

# Polymarket sports taker-fee coefficient: fee per share = COEF * p * (1-p),
# charged on the TAKER leg only (makers pay 0). Same value as
# ``wca.advancement.PM_TAKER_FEE_COEF`` — kept as a local constant so this
# module stays import-light, with test-enforced parity
# (tests/test_hl_xvenue.py::test_pm_fee_parity_with_advancement).
PM_TAKER_FEE_COEF = 0.03

# HL trading fee per share — zero per HIP-4 docs + 497/502 empirical fills
# (see module docstring). The 5 exceptions were 1.5bp builder-code fees:
# orders sent WITHOUT a builder code pay 0. Re-check userFills before sizing
# any assumption into real math: "currently zero" has no announced end date.
HL_TRADING_FEE_PER_SHARE = 0.0

# HL settlement fee: RESERVED in outcome.xyz docs ("a settlement fee is
# deducted from the payout ... shown in the market spec") but absent from
# every captured market spec and untested by the trading-fill evidence.
# Unverified => modelled explicitly so the feed can carry the caveat; a
# nonzero value here consumes edge on every branch that collects via HL
# settlement (i.e. nearly all of them).
HL_SETTLEMENT_FEE_ASSUMED = 0.0
HL_SETTLEMENT_FEE_VERIFIED = False


def pm_taker_fee(price: float) -> float:
    """PM sports taker fee per share at fill *price* (clamped to [0, 1])."""
    p = min(max(float(price), 0.0), 1.0)
    return PM_TAKER_FEE_COEF * p * (1.0 - p)


# ---------------------------------------------------------------------------
# Statuses (monitor-only; the full enum — consumers must reject others)
# ---------------------------------------------------------------------------

STATUS_WATCH = "XV_WATCH"
STATUS_ARB_CANDIDATE = "XV_ARB_CANDIDATE"
STATUS_MISMATCHED_SETTLEMENT = "XV_MISMATCHED_SETTLEMENT"
STATUS_NO_DATA = "XV_NO_DATA"
ALLOWED_STATUSES = (
    STATUS_WATCH,
    STATUS_ARB_CANDIDATE,
    STATUS_MISMATCHED_SETTLEMENT,
    STATUS_NO_DATA,
)

# Per-direction settlement-divergence tails. dir1 = buy HL Yes + buy PM No;
# dir2 = buy PM Yes + buy HL No. "Gated" = a positive edge in that direction
# may NEVER be labelled XV_ARB_CANDIDATE, because a settlement-divergence tail
# collects less than the ~$1 cost basis:
#
#   champion dir1  GATED   co-champions: HL question 32 resolves ALL-No
#                          (explicit) while PM (UMA, silent on co-champions)
#                          may resolve the team Yes -> both legs pay 0.
#   champion dir2  open    residual tail = champion declared inside the
#                          ~19-24h PM(Oct 13, no TZ)/HL(Oct 14 23:59 UTC)
#                          deadline gap -> collects 0; needs a ~3-month
#                          postponement landing in a <24h window (caveat,
#                          not gate).
#   qf dir1        open    cancellation/no-result: HL pays 0.5 + PM No pays
#                          1.0 -> collects 1.5 (windfall, safe).
#   qf dir2        GATED   same tail: PM Yes pays 0 + HL pays 0.5 ->
#                          collects 0.5 on ~1.0 cost; and a winner declared
#                          inside the ~20h HL/PM deadline gap collects 0.
TAILS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "champion": {
        "dir1_buy_hl_yes_buy_pm_no": {
            "gated": True,
            "tail": "co_champion_both_legs_zero",
            "detail": (
                "Co-champions: HL resolves all-No (explicit in question-32 "
                "description) while PM is silent (UMA) and may resolve the "
                "team Yes -> HL Yes pays 0 AND PM No pays 0."
            ),
        },
        "dir2_buy_pm_yes_buy_hl_no": {
            "gated": False,
            "tail": "deadline_gap_sliver_negligible",
            "detail": (
                "Champion declared between PM's 2026-10-13 deadline (no TZ "
                "stated) and HL's 2026-10-14 23:59 UTC -> collects 0. Remote "
                "(<24h window after a ~3-month postponement); caveat only."
            ),
        },
    },
    "qf": {
        "dir1_buy_hl_yes_buy_pm_no": {
            "gated": False,
            "tail": "cancellation_windfall_1_5",
            "detail": (
                "Cancellation/no result by the deadlines: HL pays 0.5/share "
                "+ PM reach-SF No pays 1.0 -> collects 1.5 (windfall)."
            ),
        },
        "dir2_buy_pm_yes_buy_hl_no": {
            "gated": True,
            "tail": "cancellation_toxic_0_5",
            "detail": (
                "Cancellation/no result: PM reach-SF Yes pays 0 + HL pays "
                "0.5 -> collects 0.5 on ~1.0 cost. Also a winner declared "
                "inside the ~20h HL(Jul 26 23:59 UTC)/PM(Jul 25 23:59 ET) "
                "deadline gap collects 0."
            ),
        },
    },
}

# ---------------------------------------------------------------------------
# Settlement bases (verbatim-grounded summaries; both legs carried per pair)
# ---------------------------------------------------------------------------

HL_CHAMPION_BASIS = (
    "HL question 32 '2026 World Cup Champion': Yes iff FIFA officially "
    "declares the team champion; ET+pens valid; No settles EARLY on "
    "mathematical elimination (observed live: outcome 172 Algeria); ALL "
    "outcomes No on cancellation/co-champions/no champion declared by "
    "2026-10-14 23:59 UTC. Deadline exists only in description text (no "
    "machine-readable expiry field)."
)
PM_WIN_WC_BASIS = (
    "PM 'world-cup-winner' (event 30615, negRisk): Yes iff the team wins "
    "the 2026 FIFA World Cup; immediate No on elimination; resolves to "
    "'Other' if cancelled/not completed by October 13, 2026 11:59 PM (no "
    "timezone stated in description)."
)
HL_QF_BASIS = (
    "HL QF match winner: resolves to the team FIFA declares winner of the "
    "quarterfinal; 'Game results after regular time, extra time, and "
    "penalties, if applicable, are all valid' (verbatim); walkover/forfeit/"
    "admin decision valid; resolves 0.5/share to BOTH sides if cancelled/"
    "no winner/no result by 2026-07-26 23:59 UTC. No Draw side."
)
PM_REACH_SF_BASIS = (
    "PM 'world-cup-nation-to-reach-semifinals' (event 551781, separate "
    "binary markets, NOT negRisk): Yes iff the team reaches the semifinals "
    "(advancement basis, ET+pens inclusive — winning the QF == reaching SF); "
    "No if cancelled/postponed past July 25, 2026 11:59 PM ET."
)

# ---------------------------------------------------------------------------
# Pair map (pinned 2026-07-09 from raw dumps; PM token ids are immutable per
# market — the runtime gamma cross-check in scripts/wca_hl_xvenue.py fails a
# pair CLOSED to XV_NO_DATA on any mismatch)
# ---------------------------------------------------------------------------

PM_GAMMA_SLUGS = {
    "win_wc": "world-cup-winner",
    "reach_sf": "world-cup-nation-to-reach-semifinals",
}

# team -> (pm_market_id, token_yes, token_no); win-WC (all 16 asset ids
# cross-checked against the captured CLOB book dumps' ``asset_id`` fields).
PM_WIN_WC: "OrderedDict[str, Tuple[str, str, str]]" = OrderedDict(
    [
        ("Argentina", ("558938",
                       "18812649149814341758733697580460697418474693998558159483117100240528657629879",
                       "115428153746996892211798999366308897078723117634059783423375188043903703749062")),
        ("Belgium", ("558946",
                     "30815807067456631524510535002617106205417832891402132396713720656146245200000",
                     "71145888994888153292442623019750517622535407476309461406574461229898137896934")),
        ("England", ("558935",
                     "115556263888245616435851357148058235707004733438163639091106356867234218207169",
                     "77121637225348873006259930776623502125079210522997384841464684944292365296940")),
        ("France", ("558936",
                    "108233603819467706476318984012158651931658302669301887462181073562758483842092",
                    "32270411694523539495262303868629477861017829722282576458031815333486368239544")),
        ("Morocco", ("558963",
                     "69910730841487615802736046038473620030754616421912831175284551372639933569112",
                     "64291832879722161879651094688874074984529456778901604558632306686248535158725")),
        ("Norway", ("558951",
                    "60447443643099453130956385288904175887233107411078568881602330835010340506057",
                    "111538579557239934343870815626480092245052857494675784434731223739153238373070")),
        ("Spain", ("558934",
                   "4394372887385518214471608448209527405727552777602031099972143344338178308080",
                   "112680630004798425069810935278212000865453267506345451433803052322987302357330")),
        ("Switzerland", ("558974",
                         "62131913648515148266463816694306031394539656598501514114816028349608560215534",
                         "45315272750116791836504013666029583517532908319286234834610455739871173419179")),
    ]
)

PM_REACH_SF: "OrderedDict[str, Tuple[str, str, str]]" = OrderedDict(
    [
        ("Argentina", ("2419361",
                       "89536067333293473447901433413782685510126736303748247526034263106383356680639",
                       "15579232773398251307282915471792114310844694494654532312907387009654692860923")),
        ("Belgium", ("2419349",
                     "69308688490171395576850307939004739703133311275309901259098144580114964835504",
                     "27465305808830027478748264338548566113121667931960602026884841681314319912671")),
        ("England", ("2419369",
                     "55294405439268844550383434100266469978217752464671073044130093830763750964730",
                     "43180050312134890928476684764665203164471123431180683307338020007956622756559")),
        ("France", ("2419357",
                    "100129780550616595145553750912141386610721021591799769049531023014005791181112",
                    "11833328785598556491835740107930911499139397624320250850272538944203363112935")),
        ("Morocco", ("2419334",
                     "47854818125142579705136730484306012843275003274878888606842114188142707548341",
                     "49255480439312539444144499647289034096071265497094624211295651547224031778875")),
        ("Norway", ("2419359",
                    "62948040058811530738851582948100508585678711039782457351344064638643339400332",
                    "94507404587911363782429002685350140819700646734600625387531572421599373278903")),
        ("Spain", ("2419353",
                   "94114201224211049031571044475406189237583412588859764322595606591995897942500",
                   "100938009931263211789902925967068115839559588497892683689528715549774223922226")),
        ("Switzerland", ("2419328",
                         "103493863332754371201759692475592619819800488748318590025773806305338466147803",
                         "4143069696891676731364438152594194190773290479243220062827397216866200244953")),
    ]
)

# HL champion outcome ids (question 32; sideSpecs = ["Yes", "No"]).
HL_CHAMPION_IDS: "OrderedDict[str, int]" = OrderedDict(
    [
        ("Argentina", 173), ("Belgium", 176), ("England", 188), ("France", 189),
        ("Morocco", 199), ("Norway", 202), ("Spain", 212), ("Switzerland", 214),
    ]
)

# HL QF match markets: (hl outcome id, side-0 team, side-1 team, game date) —
# side order verified against outcomeMeta sideSpecs (recon + fixture).
HL_QF_MARKETS: List[Tuple[int, str, str, str]] = [
    (761, "France", "Morocco", "2026-07-09"),
    (778, "Norway", "England", "2026-07-11"),
    (779, "Spain", "Belgium", "2026-07-10"),
    (788, "Argentina", "Switzerland", "2026-07-11"),
]


def pair_configs() -> List[Dict[str, Any]]:
    """The 16 settlement-matched pair configs (8 champion + 8 QF team-sides).

    Each config carries everything needed to fetch + evaluate one pair:
    HL outcome id and the side index whose price is the team's "Yes" space,
    PM market id + Yes/No token ids, and the settlement basis of BOTH legs.
    """
    pairs: List[Dict[str, Any]] = []
    for team, hl_id in HL_CHAMPION_IDS.items():
        mid, tok_y, tok_n = PM_WIN_WC[team]
        pairs.append({
            "pair_id": "champion:%s" % team,
            "kind": "champion",
            "team": team,
            "hl_outcome_id": hl_id,
            "hl_yes_side": 0,  # sideSpecs ["Yes","No"]
            "hl_settlement_basis": HL_CHAMPION_BASIS,
            "pm_market_id": mid,
            "pm_token_yes": tok_y,
            "pm_token_no": tok_n,
            "pm_slug": PM_GAMMA_SLUGS["win_wc"],
            "pm_settlement_basis": PM_WIN_WC_BASIS,
            "event_date": None,
        })
    for hl_id, t0, t1, date in HL_QF_MARKETS:
        for side, team in ((0, t0), (1, t1)):
            mid, tok_y, tok_n = PM_REACH_SF[team]
            pairs.append({
                "pair_id": "qf:%s" % team,
                "kind": "qf",
                "team": team,
                "hl_outcome_id": hl_id,
                "hl_yes_side": side,  # sideSpecs [teamA, teamB]; team side == Yes space
                "hl_settlement_basis": HL_QF_BASIS,
                "pm_market_id": mid,
                "pm_token_yes": tok_y,
                "pm_token_no": tok_n,
                "pm_slug": PM_GAMMA_SLUGS["reach_sf"],
                "pm_settlement_basis": PM_REACH_SF_BASIS,
                "event_date": date,
            })
    return pairs


# ---------------------------------------------------------------------------
# Book parsing (PM CLOB) + two-leg walk
# ---------------------------------------------------------------------------

def parse_pm_book(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a CLOB ``GET /book?token_id=…`` payload to sorted floats.

    Shape (verified, recon ``book_*.json``): ``bids``/``asks`` lists of
    ``{"price": str, "size": str}`` (sizes in shares), ``timestamp`` in ms
    (string), plus ``asset_id``/``tick_size``/``min_order_size``/``neg_risk``.
    """
    bids = sorted(
        ((float(x["price"]), float(x["size"])) for x in payload.get("bids", [])),
        key=lambda x: -x[0],
    )
    asks = sorted(
        ((float(x["price"]), float(x["size"])) for x in payload.get("asks", [])),
        key=lambda x: x[0],
    )
    ts_raw = payload.get("timestamp")
    try:
        ts_ms = int(ts_raw)
    except (TypeError, ValueError):
        ts_ms = None
    return {
        "asset_id": payload.get("asset_id"),
        "timestamp_ms": ts_ms,
        "bids": bids,
        "asks": asks,
        "tick_size": payload.get("tick_size"),
        "min_order_size": payload.get("min_order_size"),
        "neg_risk": payload.get("neg_risk"),
    }


def edge_at_best(
    hl_ask: Optional[float], pm_ask: Optional[float], pm_fee_price: Optional[float]
) -> Optional[float]:
    """Per-share edge of buying both complementary legs at best ask.

    Payout is $1/share on the matched branch; fee applies to the PM leg only
    (HL trading fee 0 — see module docstring; HL settlement fee assumed 0,
    UNVERIFIED, carried as an explicit feed caveat).
    """
    if hl_ask is None or pm_ask is None or pm_fee_price is None:
        return None
    return 1.0 - (
        hl_ask + pm_ask + pm_taker_fee(pm_fee_price) + HL_SETTLEMENT_FEE_ASSUMED
    )


def walk_two_leg(
    asks_hl: List[Tuple[float, float]],
    asks_pm: List[Tuple[float, float]],
) -> Dict[str, Any]:
    """Greedy walk of both ask ladders while the fee-adjusted pair cost < $1.

    Buys equal share counts of an HL leg and a PM leg whose payouts are
    complementary ($1 total on the matched branch). Marginal pair cost =
    hl_px + pm_px + pm_taker_fee(pm_px). Returns executable size/cost/fees/
    profit + the per-level fill trace. Same method as the recon's
    ``arb_compute.py`` (whose outputs were independently re-derived); the
    Norway/Belgium regression tests pin exact hand-checked values.
    """
    ih = ip = 0
    rh = asks_hl[0][1] if asks_hl else 0.0
    rp = asks_pm[0][1] if asks_pm else 0.0
    tot_sh = tot_cost = tot_fee = 0.0
    levels: List[Dict[str, Any]] = []
    while ih < len(asks_hl) and ip < len(asks_pm):
        ph, pp = asks_hl[ih][0], asks_pm[ip][0]
        fee = pm_taker_fee(pp)
        cost = ph + pp + fee + HL_SETTLEMENT_FEE_ASSUMED
        if cost >= 1.0:
            break
        q = min(rh, rp)
        tot_sh += q
        tot_cost += q * cost
        tot_fee += q * fee
        levels.append({
            "hl_px": ph, "pm_px": pp, "pm_fee": round(fee, 6),
            "shares": round(q, 2), "margin_per_share": round(1.0 - cost, 6),
        })
        rh -= q
        rp -= q
        if rh <= 1e-9:
            ih += 1
            rh = asks_hl[ih][1] if ih < len(asks_hl) else 0.0
        if rp <= 1e-9:
            ip += 1
            rp = asks_pm[ip][1] if ip < len(asks_pm) else 0.0
    profit = tot_sh - tot_cost
    return {
        "shares": round(tot_sh, 2),
        "cost_usd": round(tot_cost, 2),
        "pm_fees_usd": round(tot_fee, 2),
        "profit_usd": round(profit, 4),
        "ret_on_cost_pct": round(100.0 * profit / tot_cost, 4) if tot_cost else None,
        "levels": levels,
    }


def _best(levels: List[Tuple[float, float]]) -> Tuple[Optional[float], Optional[float]]:
    return (levels[0][0], levels[0][1]) if levels else (None, None)


def _skew_seconds(a_ms: Optional[int], b_ms: Optional[int]) -> Optional[float]:
    if a_ms is None or b_ms is None:
        return None
    return round(abs(int(a_ms) - int(b_ms)) / 1000.0, 1)


# ---------------------------------------------------------------------------
# Pair evaluation
# ---------------------------------------------------------------------------

def evaluate_pair(
    cfg: Dict[str, Any],
    hl_yes_book: Optional[Dict[str, Any]],
    hl_no_book: Optional[Dict[str, Any]],
    pm_yes_book: Optional[Dict[str, Any]],
    pm_no_book: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Evaluate one settlement-matched pair -> one monitor-only feed row.

    Books are the parsed forms (:func:`wca.hl.client.parse_l2_book` /
    :func:`parse_pm_book`); any ``None`` book fails the row CLOSED to
    ``XV_NO_DATA``. Both directions are computed with the PM taker fee on the
    PM leg; a positive-edge direction whose settlement-divergence tail is
    gated (see :data:`TAILS`) can at most yield ``XV_MISMATCHED_SETTLEMENT``.
    """
    row: Dict[str, Any] = {
        "pair_id": cfg["pair_id"],
        "kind": cfg["kind"],
        "team": cfg["team"],
        "hl": {
            "outcome_id": cfg["hl_outcome_id"],
            "yes_side": cfg["hl_yes_side"],
            "settlement_basis": cfg["hl_settlement_basis"],
        },
        "pm": {
            "market_id": cfg["pm_market_id"],
            "token_yes": cfg["pm_token_yes"],
            "token_no": cfg["pm_token_no"],
            "slug": cfg["pm_slug"],
            "settlement_basis": cfg["pm_settlement_basis"],
        },
        "event_date": cfg.get("event_date"),
    }
    missing = [
        name
        for name, book in (
            ("hl_yes", hl_yes_book), ("hl_no", hl_no_book),
            ("pm_yes", pm_yes_book), ("pm_no", pm_no_book),
        )
        if book is None
    ]
    if missing:
        row["status"] = STATUS_NO_DATA
        row["status_reason"] = "missing books: %s" % ", ".join(missing)
        row["directions"] = None
        return row

    hl_bid, hl_bid_sz = _best(hl_yes_book["bids"])
    hl_ask, hl_ask_sz = _best(hl_yes_book["asks"])
    hl_no_ask, hl_no_ask_sz = _best(hl_no_book["asks"])
    pm_bid, pm_bid_sz = _best(pm_yes_book["bids"])
    pm_ask, pm_ask_sz = _best(pm_yes_book["asks"])
    pm_no_ask, pm_no_ask_sz = _best(pm_no_book["asks"])

    row["hl"].update({
        "yes_bid": hl_bid, "yes_bid_sz": hl_bid_sz,
        "yes_ask": hl_ask, "yes_ask_sz": hl_ask_sz,
        "no_ask": hl_no_ask, "no_ask_sz": hl_no_ask_sz,
        "book_time_ms": hl_yes_book.get("time_ms"),
    })
    row["pm"].update({
        "yes_bid": pm_bid, "yes_bid_sz": pm_bid_sz,
        "yes_ask": pm_ask, "yes_ask_sz": pm_ask_sz,
        "no_ask": pm_no_ask, "no_ask_sz": pm_no_ask_sz,
        "book_timestamp_ms": pm_yes_book.get("timestamp_ms"),
    })
    # Pre-fee crossedness (diagnostic only)
    row["raw_cross_pre_fee"] = {
        "buy_hl_sell_pm": round(pm_bid - hl_ask, 5) if (pm_bid is not None and hl_ask is not None) else None,
        "buy_pm_sell_hl": round(hl_bid - pm_ask, 5) if (hl_bid is not None and pm_ask is not None) else None,
    }
    row["mid_gap_hl_minus_pm"] = (
        round((hl_bid + hl_ask) / 2.0 - (pm_bid + pm_ask) / 2.0, 5)
        if all(v is not None for v in (hl_bid, hl_ask, pm_bid, pm_ask))
        else None
    )

    tails = TAILS[cfg["kind"]]
    directions: Dict[str, Any] = {}
    # dir1: buy HL Yes @ ask + buy PM No @ ask (fee on the PM leg).
    d1_walk = walk_two_leg(hl_yes_book["asks"], pm_no_book["asks"])
    directions["dir1_buy_hl_yes_buy_pm_no"] = {
        "edge_per_share_at_best": (
            round(edge_at_best(hl_ask, pm_no_ask, pm_no_ask), 5)
            if edge_at_best(hl_ask, pm_no_ask, pm_no_ask) is not None else None
        ),
        "executable": d1_walk,
        "leg_skew_seconds": _skew_seconds(
            hl_yes_book.get("time_ms"), pm_no_book.get("timestamp_ms")
        ),
        "settlement_tail": tails["dir1_buy_hl_yes_buy_pm_no"],
    }
    # dir2: buy PM Yes @ ask + buy HL No @ ask (fee on the PM leg).
    d2_walk = walk_two_leg(hl_no_book["asks"], pm_yes_book["asks"])
    directions["dir2_buy_pm_yes_buy_hl_no"] = {
        "edge_per_share_at_best": (
            round(edge_at_best(hl_no_ask, pm_ask, pm_ask), 5)
            if edge_at_best(hl_no_ask, pm_ask, pm_ask) is not None else None
        ),
        "executable": d2_walk,
        "leg_skew_seconds": _skew_seconds(
            hl_no_book.get("time_ms"), pm_yes_book.get("timestamp_ms")
        ),
        "settlement_tail": tails["dir2_buy_pm_yes_buy_hl_no"],
    }
    row["directions"] = directions

    # Status: fail-closed ordering. A direction "hits" when its fee-adjusted
    # edge at best is positive AND the walk fills > 0 shares.
    open_hits: List[str] = []
    gated_hits: List[str] = []
    for name, d in directions.items():
        edge = d["edge_per_share_at_best"]
        if edge is not None and edge > 0.0 and d["executable"]["shares"] > 0:
            (gated_hits if d["settlement_tail"]["gated"] else open_hits).append(name)
    if open_hits:
        row["status"] = STATUS_ARB_CANDIDATE
        best_dir = max(
            open_hits, key=lambda n: directions[n]["edge_per_share_at_best"]
        )
        d = directions[best_dir]
        row["status_reason"] = (
            "%s: +%.5f/share after PM taker fee (HL fee 0, settlement fee "
            "UNVERIFIED assumed 0), executable %.2f shares for $%.4f profit. "
            "Monitor-only: legs captured %.1fs apart — simultaneous fill "
            "unproven."
            % (
                best_dir,
                d["edge_per_share_at_best"],
                d["executable"]["shares"],
                d["executable"]["profit_usd"],
                d["leg_skew_seconds"] if d["leg_skew_seconds"] is not None else -1.0,
            )
        )
    elif gated_hits:
        row["status"] = STATUS_MISMATCHED_SETTLEMENT
        best_dir = max(
            gated_hits, key=lambda n: directions[n]["edge_per_share_at_best"]
        )
        row["status_reason"] = (
            "%s shows +%.5f/share but its settlement-divergence tail is gated "
            "(%s) — never an arb candidate."
            % (
                best_dir,
                directions[best_dir]["edge_per_share_at_best"],
                directions[best_dir]["settlement_tail"]["tail"],
            )
        )
    else:
        row["status"] = STATUS_WATCH
        edges = [
            d["edge_per_share_at_best"]
            for d in directions.values()
            if d["edge_per_share_at_best"] is not None
        ]
        row["status_reason"] = (
            "no fee-surviving cross; best direction %.5f/share" % max(edges)
            if edges else "no quotes on at least one leg"
        )
    return row


# ---------------------------------------------------------------------------
# Feed assembly
# ---------------------------------------------------------------------------

STANDING_CAVEATS = [
    "MONITOR-ONLY feed. Statuses are watch-labels, never trade instructions; "
    "no execution scaffold exists for Hyperliquid (watcher-only verdict, "
    "2026-07-09 recon) and the venue has no price-capture/CLV/settlement "
    "automation yet — the CLAUDE.md live-money gate is NOT cleared.",
    "HL settlement fee UNVERIFIED: outcome.xyz docs reserve a fee deducted "
    "from settlement payouts; no captured market spec or fill proves it zero "
    "for these markets. Edges assume 0 — confirm before believing any edge "
    "(nearly every branch collects via HL settlement).",
    "HL trading fees are 'currently zero for initial testing' with no "
    "announced end date; 5 of 502 observed fills carried 1.5bp BUILDER-code "
    "fees (direct API orders without a builder code pay 0). Re-check "
    "userFills each session.",
    "HL REST l2Book truncates at 20 levels/side; deep-book sizes are lower "
    "bounds when a ladder extends past the snapshot window.",
    "Cross-venue legs are captured sequentially over REST (tens of seconds "
    "of skew); a positive edge here proves touching/crossing quotes existed, "
    "NOT that both legs were simultaneously fillable.",
    "PM per-match 1X2 markets are structurally excluded: 3-way, 90-minute "
    "settlement — they NEVER pair with HL's 2-way ET+pens QF markets.",
    "HL QF books are merged dual books (side1 == 1 - side0 structurally): "
    "no intra-HL arb can exist (mint/merge at $1, fee zero).",
]


def build_feed(
    rows: List[Dict[str, Any]],
    generated_at: str,
    n_snapshots: int,
    sources: Dict[str, Any],
    extra_caveats: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Assemble the ``site/hl_xvenue.json`` payload (schema v1).

    ``n_snapshots`` = paired-snapshot count INCLUDING this run (from the
    history file) — every aggregate consumer must treat the feed as n=that,
    not as an established distribution.
    """
    statuses = [r.get("status") for r in rows]
    bad = [s for s in statuses if s not in ALLOWED_STATUSES]
    if bad:
        raise ValueError("disallowed status(es) %r — monitor-only enum is %r" % (bad, ALLOWED_STATUSES))
    caveats = ["n=%d cross-venue snapshot(s) so far — existence evidence only, "
               "not frequency/persistence/fillability" % int(n_snapshots)]
    caveats += STANDING_CAVEATS
    if extra_caveats:
        caveats += list(extra_caveats)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "monitor_only": True,
        "execution": (
            "NONE — no execution scaffold this cycle (watcher-only verdict); "
            "go/no-go criteria for ever adding one: "
            "docs/research/hl_venue_recon_2026-07-09.md"
        ),
        "allowed_statuses": list(ALLOWED_STATUSES),
        "n_snapshots": int(n_snapshots),
        "sources": sources,
        "fee_model": {
            "pm_taker_fee": "0.03 * p * (1-p) per share, taker-only (PM sports fee; "
                            "parity with wca.advancement.PM_TAKER_FEE_COEF)",
            "hl_trading_fee_per_share": HL_TRADING_FEE_PER_SHARE,
            "hl_settlement_fee_assumed": HL_SETTLEMENT_FEE_ASSUMED,
            "hl_settlement_fee_verified": HL_SETTLEMENT_FEE_VERIFIED,
        },
        "summary": {
            "n_pairs": len(rows),
            "by_status": {s: statuses.count(s) for s in ALLOWED_STATUSES},
        },
        "pairs": rows,
        "caveats": caveats,
    }


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
