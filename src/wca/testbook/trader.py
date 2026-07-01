"""Automated paper-trader for the isolated test book.

Joins our model (the FT family in ``site/scores_data.json`` + the advance family
in ``site/advancement_data.json``) to live Polymarket prices, finds +EV YES
positions, sizes them with fractional Kelly off the test-book bankroll, and logs
paper fills to :mod:`wca.testbook.store`.

The FT-result vs ADVANCE distinction is first-class:

* ``match_result`` (resolution_basis ``FT``) — the bare "Will X win on <date>?"
  market: 90' + stoppage, a draw is a separate outcome. Priced off ``model_1x2``.
* ``advance`` (resolution_basis ``advance``) — "World Cup: Nation To Reach
  Round of N": progression after extra-time + penalties. Priced off the
  advancement sim's reach probabilities. These are DIFFERENT markets and are
  never conflated.

Other priced families: ``totals_ou25`` (totals), ``btts`` (btts),
``exact_score`` (exact) — the exotic/thin end this book is meant to probe.

The pure logic (candidate building, edge, Kelly) takes plain data so it is fully
unit-testable; the live fetch lives in ``scripts/wca_test_book.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from wca.data.polymarket import _parse_json_array
from wca.data.teamnames import canonical
from wca.testbook import store
# Reuse the store's pure decision math so the sizing and the decision scorer can
# never diverge (spec INV-1). kelly_fraction stays importable as trader.kelly_fraction.
from wca.testbook.store import kelly_fraction, f_target, g_logwealth  # noqa: F401

# Reach-market title -> advancement stage key.
_REACH_TITLE_RE = re.compile(r"nation to reach (round of 16|quarterfinals?|semifinals?|final)", re.I)
_REACH_STAGE = {"round of 16": "R16", "quarterfinal": "QF", "quarterfinals": "QF",
                "semifinal": "SF", "semifinals": "SF", "final": "Final"}


@dataclass
class Candidate:
    fixture: str
    market_type: str
    selection: str
    resolution_basis: str
    token_id: Optional[str]
    price: float          # PM YES ask (the paper fill price)
    model_prob: float
    edge: float           # model_prob - price
    volume: float
    spread: Optional[float]


def _f(x):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def eval_exit_rules(*, q_t: float, p_bid: Optional[float], p_mid: Optional[float],
                    spread: Optional[float], depth: Optional[float], shares: float,
                    equity: float, kelly_mult: float, max_stake_frac: float,
                    over_kelly_band: float = 0.5, spread_cap: float = 0.10,
                    min_depth: float = 0.0):
    """Decide a TRIM/CLOSE for one open position from decision-time state only.

    Returns ``(rule, action, shares_to_sell, threshold)`` or ``None``. Implements
    R1 edge-flip-close (model edge gone: q ≤ bid → growth-optimal stake is 0),
    R3 liquidity-exit (spread too wide / depth too thin — an exit-cost emergency),
    R2 over-Kelly-trim (marked exposure above the capped-Kelly target → trim back).
    R4 (cap_release) is portfolio-level and handled by the caller.
    """
    if p_bid is None or shares <= 0 or equity <= 0 or not (0.0 < p_bid < 1.0):
        return None
    ft = f_target(q_t, p_bid, kelly_mult, max_stake_frac)
    h = shares * p_bid / equity                       # marked exposure fraction
    if q_t <= p_bid:                                   # R1: edge flipped non-positive
        return ("edge_flip_close", "close", shares, 0.0)
    if (spread is not None and spread > spread_cap) or (min_depth and depth is not None and depth < min_depth):
        return ("liquidity_exit", "close", shares, spread_cap)   # R3
    if ft > 0 and h > ft * (1.0 + over_kelly_band):    # R2: over-Kelly
        target_shares = ft * equity / p_bid
        sell = max(0.0, shares - target_shares)
        if sell > 1e-9:
            return ("over_kelly_trim", "trim", sell, over_kelly_band)
    return None


def _market_tradeable(market: Dict[str, object]) -> bool:
    """False if a PM market is resolved / not accepting orders (a dead fixture).

    Once a match kicks off, its pre-match markets stop accepting orders (and later
    close). Taking a paper position on those is the leak that produced the early
    player-prop losses — guard every entry through here."""
    if market.get("closed") is True or market.get("active") is False:
        return False
    if market.get("acceptingOrders") is False:
        return False
    return True


def yes_quote(market: Dict[str, object]) -> Optional[Dict[str, object]]:
    """YES-side quote for a TRUE Yes/No binary market: token, ask, bid, mid, vol, spread.

    Returns ``None`` unless the market literally has a "Yes" outcome — this is the
    key guard against silently mispricing 2-outcome markets like [Over, Under] or
    [Team1, Team2] (where index 0 is NOT "Yes"). Those markets need their own
    outcome-aware pricing, not this helper.
    """
    if not _market_tradeable(market):
        return None
    toks = _parse_json_array(market.get("clobTokenIds"))
    if not toks:
        return None
    outs = _parse_json_array(market.get("outcomes")) or []
    yi = None
    for i, o in enumerate(outs):
        if str(o).strip().lower() == "yes":
            yi = i
            break
    if yi is None or yi >= len(toks):
        return None
    bid, ask = _f(market.get("bestBid")), _f(market.get("bestAsk"))
    prices = _parse_json_array(market.get("outcomePrices")) or []
    op = _f(prices[yi]) if yi < len(prices) else None
    if not (ask and 0.0 < ask < 1.0):
        ask = op
    if not (bid and 0.0 < bid < 1.0):
        bid = op
    if ask is None or not (0.0 < ask < 1.0):
        return None
    mid = (bid + ask) / 2.0 if (bid and ask) else op
    return {"token": str(toks[yi]), "ask": ask, "bid": bid, "mid": mid,
            "vol": _f(market.get("volumeNum")) or 0.0,
            "spread": (ask - bid) if (bid and ask) else None}


def outcome_quote(market: Dict[str, object], want: str) -> Optional[Dict[str, object]]:
    """Quote for a NAMED outcome of a 2+-outcome market (e.g. 'Over'/'Under' or a
    team), matched case-insensitively by substring. For [Over,Under] /
    [Team1,Team2] markets where there is no 'Yes' outcome. Returns token + ask
    (the YES-equivalent buy price of that outcome) + bid/mid/vol/spread.
    """
    if not _market_tradeable(market):
        return None
    toks = _parse_json_array(market.get("clobTokenIds"))
    outs = _parse_json_array(market.get("outcomes")) or []
    prices = _parse_json_array(market.get("outcomePrices")) or []
    if not toks or not outs:
        return None
    wl = want.strip().lower()
    idx = None
    for i, o in enumerate(outs):
        if wl in str(o).strip().lower():
            idx = i
            break
    if idx is None or idx >= len(toks):
        return None
    p = _f(prices[idx]) if idx < len(prices) else None
    if p is None or not (0.0 < p < 1.0):
        return None
    # Two-outcome books: spread approximated from the complementary outcome.
    comp = _f(prices[1 - idx]) if len(prices) == 2 and idx < 2 else None
    implied_bid = (1.0 - comp) if comp is not None else None
    spread = abs(p - implied_bid) if implied_bid is not None else None
    return {"token": str(toks[idx]), "ask": p, "bid": implied_bid, "mid": p,
            "vol": _f(market.get("volumeNum")) or 0.0, "spread": spread}


def _score_dist(scores) -> Dict[tuple, float]:
    """Model scoreline distribution {(h,a): prob} from scores_data 'scores'."""
    out: Dict[tuple, float] = {}
    for s in scores or []:
        m = re.match(r"\s*(\d+)\s*[-–]\s*(\d+)", str(s.get("score") or ""))
        if m and s.get("prob") is not None:
            out[(int(m.group(1)), int(m.group(2)))] = float(s["prob"]) / 100.0
    return out


def _teams_from_title(title: str):
    head = title.split(" - ")[0]
    parts = [p.strip() for p in head.replace(" vs. ", " vs ").split(" vs ")]
    return (canonical(parts[0]), canonical(parts[1])) if len(parts) == 2 else (None, None)


def _fixture_lambdas(fx: Dict[str, object]):
    """Absolute expected goals (λ_home, λ_away) from the model scoreline dist."""
    dist = _score_dist(fx.get("scores"))
    tot = sum(dist.values())
    if not dist or tot <= 0:
        return None
    lh = sum(h * p for (h, a), p in dist.items()) / tot
    la = sum(a * p for (h, a), p in dist.items()) / tot
    return lh, la


_OVERRIDES_CACHE = {"loaded": False, "data": {}}


def _load_overrides():
    """{canonical_team: [PlayerParams]} scorer overrides (cached, best-effort)."""
    if not _OVERRIDES_CACHE["loaded"]:
        _OVERRIDES_CACHE["loaded"] = True
        try:
            from wca.models.scorers import load_player_overrides
            _OVERRIDES_CACHE["data"] = {canonical(k): v for k, v in load_player_overrides().items()}
        except Exception:
            _OVERRIDES_CACHE["data"] = {}
    return _OVERRIDES_CACHE["data"]


def _price_player_props(fx: Dict[str, object], ev: Dict[str, object]) -> List["Candidate"]:
    """Model-vs-PM player-prop candidates for a '<H> vs <A> - Player Props' event.

    Prices anytime/2+/3+ goals, shots, shots-on-target, assists via the Poisson
    player-prop model (λ from the model scoreline dist) and joins to the live PM
    labels. resolution_basis='prop'."""
    lam = _fixture_lambdas(fx)
    if lam is None:
        return []
    overrides = _load_overrides()
    home, away = fx["home"], fx["away"]
    scorers = {home: overrides.get(canonical(home), []), away: overrides.get(canonical(away), [])}
    if not (scorers[home] or scorers[away]):
        return []
    try:
        from wca.models import playerprops as PPM
        priced = PPM.price_fixture_props_detailed(
            home, away, lambda_home=lam[0], lambda_away=lam[1], scorers_by_team=scorers)
        rows = PPM.join_fixture_to_pm(priced, ev, yes_quote_fn=yes_quote)
    except Exception:
        return []
    out: List[Candidate] = []
    for r in rows:
        out.append(Candidate(
            fixture=fx["raw"], market_type="player_%s" % r.market_type,
            selection="%s %d+ %s" % (r.player, r.threshold, r.market_type.replace("_", " ")),
            resolution_basis="prop", token_id=r.token_id, price=r.pm_price,
            model_prob=r.model_prob, edge=r.edge, volume=0.0, spread=None))
    return out


# --------------------------------------------------------------------------- model side


def load_model(scores: Dict, advancement: Dict) -> Dict[str, object]:
    """Index model probabilities by canonical fixture / team for fast lookup."""
    fixtures: Dict[frozenset, Dict[str, object]] = {}
    for f in (scores or {}).get("fixtures", []):
        fx = f.get("fixture") or ""
        if " vs " not in fx:
            continue
        h, a = [s.strip() for s in fx.split(" vs ", 1)]
        key = frozenset({canonical(h), canonical(a)})
        fixtures[key] = {"home": canonical(h), "away": canonical(a), "raw": fx,
                         "model_1x2": f.get("model_1x2") or {}, "over_under": f.get("over_under") or {},
                         "btts": f.get("btts"), "scores": f.get("scores") or []}
    advance: Dict[str, Dict[str, float]] = {}
    for t in (advancement or {}).get("teams", []):
        advance[canonical(t.get("team") or "")] = t.get("model") or {}
    return {"fixtures": fixtures, "advance": advance}


# --------------------------------------------------------------------------- candidate building


def build_candidates(model: Dict[str, object], pm_events: Sequence[Dict[str, object]],
                     *, min_volume: float = 0.0) -> List[Candidate]:
    """All model-vs-PM YES candidates across the priced market families."""
    fixtures = model["fixtures"]
    advance = model["advance"]
    out: List[Candidate] = []

    def add(fixture, mtype, sel, basis, q, quote):
        if quote is None or q is None:
            return
        if quote["vol"] < min_volume:
            return
        out.append(Candidate(fixture, mtype, sel, basis, quote["token"], quote["ask"],
                             float(q), float(q) - quote["ask"], quote["vol"], quote.get("spread")))

    for ev in pm_events:
        title = (ev.get("title") or "").strip()
        tl = title.lower()
        markets = ev.get("markets") or []

        # 1) ADVANCE — "World Cup: Nation To Reach Round of N"
        rm = _REACH_TITLE_RE.search(title)
        if rm:
            stage = _REACH_STAGE.get(rm.group(1).lower())
            for m in markets:
                team = canonical((m.get("groupItemTitle") or "").strip())
                q = (advance.get(team) or {}).get(stage) if (team and stage) else None
                add("%s" % team, "advance", "%s to reach %s" % (team, stage),
                    "advance", q, yes_quote(m))
            continue

        # Fixture-scoped families need a model fixture match.
        if " vs. " in title or " vs " in title:
            ch, ca = _teams_from_title(title)
            if not (ch and ca):
                continue
            fx = fixtures.get(frozenset({ch, ca}))
            if not fx:
                continue
            fixture = fx["raw"]
            home, away = fx["home"], fx["away"]
            is_bare = " - " not in title
            suffix = title.split(" - ", 1)[1].strip().lower() if " - " in title else ""

            # Player-props event: price the whole event via the Poisson model.
            if suffix == "player props":
                out.extend(_price_player_props(fx, ev))
                continue

            for m in markets:
                git = (m.get("groupItemTitle") or "").strip()
                q_text = (m.get("question") or "")
                ql = q_text.lower()

                # 2) FT match result (bare event): "Will X win on <date>?" / draw
                if is_bare:
                    quote = yes_quote(m)
                    if "end in a draw" in ql or git.lower().startswith("draw"):
                        add(fixture, "match_result", "Draw (FT)", "FT",
                            fx["model_1x2"].get("draw"), quote)
                    else:
                        team = canonical(git) or (ch if ch in ql else ca)
                        side = "home" if team == home else ("away" if team == away else None)
                        if side:
                            add(fixture, "match_result", "%s win (FT 90')" % team, "FT",
                                fx["model_1x2"].get(side), quote)
                    continue

                # 3) Totals O/U 2.5 — a 2-outcome [Over,Under] market. Match ONLY the
                #    MATCH total-goals market, whose PM label is EXACTLY "O/U 2.5".
                #    Team totals ("Argentina O/U 2.5"), half-totals ("1st Half O/U
                #    2.5") and corners are prefixed/foreign and must NOT be priced
                #    against the match over-2.5 prob (that was the fake-edge bug).
                blob = (git + " " + q_text).lower()
                if git.strip().lower() == "o/u 2.5":
                    ou = fx["over_under"] or {}
                    if str(ou.get("line")) == "2.5":
                        over = (ou.get("over") or 0) / 100.0
                        under = (ou.get("under") or 0) / 100.0
                        add(fixture, "totals_ou25", "Over 2.5", "totals", over, outcome_quote(m, "Over"))
                        add(fixture, "totals_ou25", "Under 2.5", "totals", under, outcome_quote(m, "Under"))
                    continue

                # 4) Handicap "Spread: <Team> (-1.5)" — derive margin prob from the
                #    model scoreline distribution. 2-outcome [Team1, Team2].
                hm = re.search(r"spread:\s*(.+?)\s*\(\s*-1\.5\s*\)", (git or q_text), re.I)
                if hm:
                    team_name = hm.group(1).strip()
                    fav = canonical(team_name)
                    dist = _score_dist(fx["scores"])
                    if dist and fav in (home, away):
                        q = (sum(p for (h, a), p in dist.items() if h - a >= 2) if fav == home
                             else sum(p for (h, a), p in dist.items() if a - h >= 2))
                        add(fixture, "handicap_15", "%s -1.5" % fav, "handicap",
                            q, outcome_quote(m, team_name))
                    continue

                # 4) BTTS
                if "both teams to score" in ql or "both teams to score" in git.lower():
                    b = fx["btts"]
                    if b is not None:
                        yes = ("yes" in git.lower()) or ("yes" in ql) or True  # YES side of binary
                        add(fixture, "btts", "BTTS Yes", "btts", b / 100.0, yes_quote(m))
                    continue

                # 5) Exact score: "Exact Score: H N - M A?"
                if "exact score" in suffix or "exact score" in ql:
                    mm = re.search(r"(\d+)\s*[-–]\s*(\d+)", git or q_text)
                    if not mm:
                        continue
                    aa, bb = int(mm.group(1)), int(mm.group(2))
                    # Orient to model's home-away convention.
                    pm_home = ch  # title order
                    flip = pm_home != home
                    sk = "%d-%d" % ((bb, aa) if flip else (aa, bb))
                    q = None
                    for s in fx["scores"]:
                        if s.get("score") == sk:
                            q = (s.get("prob") or 0) / 100.0
                            break
                    add(fixture, "exact_score", "Exact %s" % sk, "exact", q, yes_quote(m))
    return out


# --------------------------------------------------------------------------- paper pass


def run_paper_pass(con, model: Dict[str, object], pm_events: Sequence[Dict[str, object]], *,
                   ts_utc: str, edge_threshold: float = 0.04, kelly_mult: float = 0.5,
                   max_stake_frac: float = 0.02, min_price: float = 0.03, max_price: float = 0.97,
                   min_volume: float = 0.0, max_edge: float = 0.15,
                   max_open_per_token: int = 1) -> Dict[str, object]:
    """One scan: log paper fills for every +EV candidate not already held.

    Sizing = ``kelly_mult`` × Kelly × bankroll, capped at ``max_stake_frac`` of
    bankroll and at available cash. Candidates with edge above ``max_edge`` are
    QUARANTINED as probable model-vs-market mismatches / miscalibration / stale-PM
    artifacts — on a market we can actually price, a >~15pp "edge" is almost always
    a bug or a wrong model, not alpha (the ev_calibration_gap validator confirms
    which as fixtures settle). Returns a summary dict.
    """
    bankroll = store.realized_balance(con) + store.deployed_capital(con)  # equity base for sizing
    held = {r["token_id"] for r in store.open_bets(con) if r.get("token_id")}
    cands = build_candidates(model, pm_events, min_volume=min_volume)
    suspicious = [c for c in cands if c.edge > max_edge]
    cands = [c for c in cands if edge_threshold <= c.edge <= max_edge
             and min_price <= c.price <= max_price]
    cands.sort(key=lambda c: c.edge, reverse=True)

    placed, skipped = [], 0
    for c in cands:
        if c.token_id in held:
            skipped += 1
            continue
        f = kelly_fraction(c.model_prob, c.price) * kelly_mult
        stake = min(f * bankroll, max_stake_frac * bankroll, store.realized_balance(con))
        if stake < 1.0:
            skipped += 1
            continue
        bid = store.log_paper_bet(
            con, ts_utc=ts_utc, fixture=c.fixture, market_type=c.market_type,
            selection=c.selection, resolution_basis=c.resolution_basis, token_id=c.token_id,
            entry_price=c.price, stake_usd=round(stake, 2), model_prob=c.model_prob,
            edge=c.edge, kelly_fraction=f,
            notes="vol=%.0f spread=%s" % (c.volume, ("%.3f" % c.spread) if c.spread else "na"))
        # Log the ADD as a decision event (process scores computed inline).
        store.log_decision(
            con, action="add", rule="auto_scan", bet_id=bid, token_id=c.token_id,
            fixture=c.fixture, resolution_basis=c.resolution_basis, q_t=c.model_prob,
            q_source="entry_static", p_t=c.price, p_mid_t=c.price, spread_t=c.spread,
            vol_t=c.volume, equity_t=bankroll, kelly_mult=kelly_mult, max_stake_frac=max_stake_frac,
            entry_price=c.price, stake_before=0.0, stake_after=round(stake, 2),
            shares_delta=(round(stake, 2) / c.price if c.price > 0 else 0.0), ts_utc=ts_utc)
        held.add(c.token_id)
        placed.append({"id": bid, "fixture": c.fixture, "market": c.market_type,
                       "selection": c.selection, "basis": c.resolution_basis,
                       "price": round(c.price, 3), "model": round(c.model_prob, 3),
                       "edge": round(c.edge, 3), "stake": round(stake, 2)})

    return {"ts": ts_utc, "candidates": len(cands), "placed": placed,
            "n_placed": len(placed), "skipped": skipped,
            "suspicious": len(suspicious),
            "balance": round(store.realized_balance(con), 2),
            "deployed": round(store.deployed_capital(con), 2)}
