"""Portfolio P&L distribution for the OPEN WCA book (Module A flagship).

The open book is valued against many simulated outcomes to produce a P&L
*distribution* rather than a single point EV.  Everything here is pure: no
wall-clock and no network so the unit tests run offline and deterministically
(numpy ``Generator(seed=42)``).  The caller (``scripts/wca_risk_pnl_data.py``)
supplies the ``generated``/``fx_ts`` timestamps and reads the production
ledger strictly read-only.

Design / honesty notes
----------------------
* Each open position is modelled as a **binary win/lose** with a per-position
  win probability ``p_win``.  The current open book is dominated by
  bet-builders, accumulators, outrights and tournament-advancement binaries —
  *none* are clean single-fixture 1X2 selections, so there is no 1X2 leg to
  read off the model triple (``selection_leg`` returns ``None`` for every one
  of them).  ``p_win`` is therefore sourced, in order of preference:

    1. the stored ``model_prob`` on the bet row (a genuine model estimate), or
    2. the model-triple-derived fair probability when the selection *does* map
       to a 1X2 leg of a fixture present in ``model_predictions_log.jsonl``
       (kept for forward-compatibility — fires for plain "Team"/"Draw"/"Team
       Yes|No" selections), or
    3. the de-vigged-free implied probability ``1/decimal_odds`` as a last
       resort, flagged ``p_source="implied"``.  This is an honest fallback,
       not a model edge: it makes such a position EV-neutral by construction.

* Positions are drawn **independently** across the book (v1 simplification —
  documented in the feed ``note``).  Within a single multi-leg bet the legs are
  already collapsed into one binary, so intra-bet correlation is captured; only
  *cross-bet* correlation (e.g. two England bets on the same match) is ignored.

* **Settlement** mirrors :func:`wca.ledger.store.settle_bet` conventions:
    - back bet, win  -> ``+stake*(odds-1)``
    - back bet, lose -> ``-stake``
    - free bet (stake-not-returned), win  -> ``+stake*(odds-1)``
    - free bet, lose -> ``0`` (own cash is never at risk)
    - lay bet, win (selection loses) -> ``+stake`` (backer's stake won)
    - lay bet, lose (selection wins) -> ``-stake*(odds-1)`` (liability)

* **Currencies are faceted.**  GBP and USD are never naively summed.  The one
  sanctioned cross-currency number is the ``distribution_gbp`` view, where USD
  legs are converted at a fixed placeholder ``fx_rate`` recorded in ``meta``.
  This is disclosed as "distribution view only".
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from wca.closecapture import fair_closing_odds, selection_leg
from wca.data.teamnames import canonical
from wca.ledger.reports import _platform_currency

# Placeholder FX. Disclosed in meta; NOT a market rate. USD -> GBP.
DEFAULT_FX_RATE = 0.79
DEFAULT_N_SIMS = 20_000
DEFAULT_SEED = 42
N_HIST_BINS = 40

# Power threshold below which an aggregate rate is band-only / insufficient.
INSUFFICIENT_SAMPLE_N = 30


# --------------------------------------------------------------------------- #
# Wilson interval (shared statistical-honesty helper)
# --------------------------------------------------------------------------- #
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for ``k`` successes in ``n`` trials.

    Handles ``n == 0`` (returns ``(0.0, 1.0)`` — total ignorance), ``k == 0``
    and ``k == n`` (the bound is pinned but the interval still has width).
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / d
    half = (z / d) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return (lo, hi)


# --------------------------------------------------------------------------- #
# Position model
# --------------------------------------------------------------------------- #
@dataclass
class OpenPosition:
    """A single open bet, reduced to a binary win/lose for simulation."""

    bet_id: int
    match_desc: str
    selection: str
    platform: str
    currency: str  # "GBP" | "USD"
    decimal_odds: float
    stake: float
    p_win: float
    p_source: str  # "model" | "triple" | "implied"
    is_free: bool
    is_lay: bool
    teams: list[str] = field(default_factory=list)

    def payoff(self, won: np.ndarray) -> np.ndarray:
        """Vectorised P&L (native currency) given a boolean win mask.

        ``won`` is the event *the bet is graded a win*.  For a lay that already
        means the backed selection lost, so the caller passes the lay-graded
        mask; we just apply the lay payout schedule when ``is_lay``.
        """
        won = np.asarray(won, dtype=bool)
        profit = self.stake * (self.decimal_odds - 1.0)
        if self.is_lay:
            # win -> keep backer stake (+stake); lose -> pay liability
            liability = self.stake * (self.decimal_odds - 1.0)
            return np.where(won, self.stake, -liability)
        if self.is_free:
            # stake-not-returned: win pays winnings, lose costs nothing
            return np.where(won, profit, 0.0)
        return np.where(won, profit, -self.stake)

    def worst_case(self) -> float:
        """Deterministic worst-case loss (<= 0) for the hard floor."""
        if self.is_lay:
            return -self.stake * (self.decimal_odds - 1.0)
        if self.is_free:
            return 0.0
        return -self.stake


# --------------------------------------------------------------------------- #
# Helpers: currency, free-bet, lay detection, win-prob sourcing
# --------------------------------------------------------------------------- #
def _currency_for(platform: str, notes: Optional[str]) -> str:
    """Resolve GBP/USD. ``notes`` may carry an explicit ``currency=XXX``."""
    if notes:
        m = re.search(r"currency=([A-Za-z]{3})", notes)
        if m:
            cur = m.group(1).upper()
            if cur in ("GBP", "USD"):
                return cur
    return _platform_currency(platform)


def _is_free_bet(notes: Optional[str]) -> bool:
    if not notes:
        return False
    nl = notes.lower()
    return ("free bet" in nl) or ("free-bet" in nl)


def _is_lay(market: Optional[str], selection: Optional[str], notes: Optional[str]) -> bool:
    blob = " ".join(x for x in (market, selection, notes) if x).lower()
    # Exchange lay bets are tagged explicitly; the current book has none.
    return bool(re.search(r"\blay\b", blob)) and "betfair_exchange" in blob


def _split_fixture_teams(match_desc: str) -> Optional[tuple[str, str]]:
    """Return raw (home, away) if ``match_desc`` is a simple ``A vs B``.

    Only the " vs "/" v " separators are honoured: every real fixture in the
    predictions log uses " vs ", whereas " - " appears inside outright /
    advancement descriptions (e.g. "2026 FIFA World Cup - Japan Round of 16")
    and would split spuriously.
    """
    if not match_desc:
        return None
    for sep in (" vs ", " v "):
        if sep in match_desc:
            parts = match_desc.split(sep)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return (parts[0].strip(), parts[1].strip())
    return None


def _model_index(log_path: Optional[str]) -> dict[tuple[str, str], dict]:
    """Latest model triple per canonical (home, away) from the predictions log."""
    index: dict[tuple[str, str], dict] = {}
    if not log_path:
        return index
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fx = rec.get("fixture")
                model = rec.get("model")
                if not isinstance(fx, str) or not isinstance(model, dict):
                    continue
                teams = _split_fixture_teams(fx)
                if not teams:
                    continue
                key = (canonical(teams[0]), canonical(teams[1]))
                index[key] = model  # later lines overwrite -> latest wins
    except FileNotFoundError:
        return index
    return index


def _team_tokens(match_desc: str, selection: str) -> list[str]:
    """Best-effort set of canonical team names this position touches."""
    teams: list[str] = []
    pair = _split_fixture_teams(match_desc)
    if pair:
        teams = [canonical(pair[0]), canonical(pair[1])]
    return teams


def _resolve_p_win(
    selection: str,
    match_desc: str,
    decimal_odds: float,
    model_prob,
    model_index: dict[tuple[str, str], dict],
) -> tuple[float, str]:
    """Pick a win probability and record its provenance.

    Order: stored ``model_prob`` -> 1X2 triple (if selection maps) -> implied.
    """
    if model_prob is not None:
        try:
            p = float(model_prob)
            if 0.0 < p < 1.0:
                return p, "model"
        except (TypeError, ValueError):
            pass

    pair = _split_fixture_teams(match_desc)
    if pair:
        triple = model_index.get((canonical(pair[0]), canonical(pair[1])))
        if triple:
            leg = selection_leg(selection, pair[0], pair[1])
            if leg is not None:
                fair = fair_closing_odds(triple, leg[0], leg[1])
                if fair and fair > 1.0:
                    return 1.0 / fair, "triple"

    implied = 1.0 / decimal_odds if decimal_odds > 1.0 else 0.0
    implied = min(max(implied, 0.0), 1.0)
    return implied, "implied"


# --------------------------------------------------------------------------- #
# Load the open book (read-only)
# --------------------------------------------------------------------------- #
def load_open_positions(
    db_path: str,
    model_log_path: Optional[str] = None,
    *,
    read_only: bool = True,
) -> list[OpenPosition]:
    """Read ``status='open'`` bets from the ledger, strictly read-only.

    ``db_path`` may be a path or a full sqlite URI.  When ``read_only`` the
    connection is opened ``mode=ro&immutable=1`` so the production ledger can
    never be mutated from the dev box.
    """
    if "://" in db_path or db_path.startswith("file:"):
        uri = db_path
        con = sqlite3.connect(uri, uri=True)
    elif read_only:
        uri = f"file:{db_path}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True)
    else:
        con = sqlite3.connect(db_path)

    model_index = _model_index(model_log_path)
    positions: list[OpenPosition] = []
    try:
        rows = con.execute(
            "SELECT id, match_desc, market, selection, platform, "
            "decimal_odds, stake, model_prob, notes "
            "FROM bets WHERE status='open' ORDER BY id"
        ).fetchall()
    finally:
        con.close()

    for (
        bet_id,
        match_desc,
        market,
        selection,
        platform,
        decimal_odds,
        stake,
        model_prob,
        notes,
    ) in rows:
        if decimal_odds is None or stake is None:
            continue
        decimal_odds = float(decimal_odds)
        stake = float(stake)
        currency = _currency_for(platform or "", notes)
        is_free = _is_free_bet(notes)
        is_lay = _is_lay(market, selection, notes)
        p_win, p_source = _resolve_p_win(
            selection or "", match_desc or "", decimal_odds, model_prob, model_index
        )
        positions.append(
            OpenPosition(
                bet_id=int(bet_id),
                match_desc=match_desc or "",
                selection=selection or "",
                platform=platform or "",
                currency=currency,
                decimal_odds=decimal_odds,
                stake=stake,
                p_win=p_win,
                p_source=p_source,
                is_free=is_free,
                is_lay=is_lay,
                teams=_team_tokens(match_desc or "", selection or ""),
            )
        )
    return positions


# --------------------------------------------------------------------------- #
# Vectorised settlement & simulation
# --------------------------------------------------------------------------- #
def settle_vectorised(
    positions: list[OpenPosition], wins: np.ndarray
) -> np.ndarray:
    """Per-position P&L matrix in native currency.

    ``wins`` is a boolean ``(n_sims, n_positions)`` array (a draw is graded a
    win).  Returns a float ``(n_sims, n_positions)`` P&L matrix.
    """
    n_sims = wins.shape[0]
    out = np.empty((n_sims, len(positions)), dtype=float)
    for j, pos in enumerate(positions):
        out[:, j] = pos.payoff(wins[:, j])
    return out


def simulate_book(
    positions: list[OpenPosition],
    *,
    n_sims: int = DEFAULT_N_SIMS,
    seed: int = DEFAULT_SEED,
    fx_rate: float = DEFAULT_FX_RATE,
) -> dict:
    """Simulate the open book; return per-position pnl + GBP-view totals.

    Returns a dict with:
      ``wins``            (n_sims, n) boolean draws,
      ``pnl_native``      (n_sims, n) native-currency P&L,
      ``pnl_gbp``         (n_sims, n) P&L converted to GBP (USD * fx_rate),
      ``book_gbp``        (n_sims,) book-level GBP P&L (sanctioned FX view),
      ``rng``             the Generator used.
    """
    rng = np.random.default_rng(seed)
    n = len(positions)
    if n == 0:
        empty = np.zeros((n_sims, 0))
        return {
            "wins": np.zeros((n_sims, 0), dtype=bool),
            "pnl_native": empty,
            "pnl_gbp": empty,
            "book_gbp": np.zeros(n_sims),
            "rng": rng,
        }

    probs = np.array([p.p_win for p in positions], dtype=float)
    # Independent draws (v1). One uniform per (sim, position).
    u = rng.random((n_sims, n))
    wins = u < probs[None, :]

    pnl_native = settle_vectorised(positions, wins)

    fx = np.array(
        [fx_rate if p.currency == "USD" else 1.0 for p in positions], dtype=float
    )
    pnl_gbp = pnl_native * fx[None, :]
    book_gbp = pnl_gbp.sum(axis=1)

    return {
        "wins": wins,
        "pnl_native": pnl_native,
        "pnl_gbp": pnl_gbp,
        "book_gbp": book_gbp,
        "rng": rng,
    }


# --------------------------------------------------------------------------- #
# Distribution statistics
# --------------------------------------------------------------------------- #
def _pctile(a: np.ndarray, q: float) -> float:
    return float(np.percentile(a, q)) if a.size else 0.0


def distribution_stats(book_gbp: np.ndarray) -> dict:
    """mean/median/percentiles, VaR95, CVaR95, P(book down), from a GBP draw."""
    if book_gbp.size == 0:
        return {
            "mean": 0.0,
            "median": 0.0,
            "p5": 0.0,
            "p25": 0.0,
            "p75": 0.0,
            "p95": 0.0,
            "var95": 0.0,
            "cvar95": 0.0,
            "p_book_down": 0.0,
        }
    p5 = _pctile(book_gbp, 5)
    mean = float(book_gbp.mean())
    # VaR95 = the loss not exceeded with 95% confidence (a magnitude, >=0).
    var95 = float(max(0.0, -p5))
    # CVaR95 = mean of the worst 5% tail (expected shortfall), reported as a
    # magnitude. Never *better* (smaller) than VaR95.
    tail = book_gbp[book_gbp <= p5]
    cvar95 = float(max(0.0, -tail.mean())) if tail.size else var95
    p_down = float((book_gbp < 0).mean())
    return {
        "mean": mean,
        "median": float(np.median(book_gbp)),
        "p5": p5,
        "p25": _pctile(book_gbp, 25),
        "p75": _pctile(book_gbp, 75),
        "p95": _pctile(book_gbp, 95),
        "var95": var95,
        "cvar95": cvar95,
        "p_book_down": p_down,
    }


def histogram(book_gbp: np.ndarray, bins: int = N_HIST_BINS) -> list[dict]:
    if book_gbp.size == 0:
        return []
    lo, hi = float(book_gbp.min()), float(book_gbp.max())
    if hi <= lo:
        hi = lo + 1.0
    counts, edges = np.histogram(book_gbp, bins=bins, range=(lo, hi))
    out = []
    for i, c in enumerate(counts):
        out.append(
            {
                "bin_lo": round(float(edges[i]), 4),
                "bin_hi": round(float(edges[i + 1]), 4),
                "count": int(c),
            }
        )
    return out


def hard_floor(positions: list[OpenPosition], fx_rate: float) -> float:
    """Deterministic worst case: every position loses simultaneously (GBP)."""
    total = 0.0
    for p in positions:
        wc = p.worst_case()
        total += wc * (fx_rate if p.currency == "USD" else 1.0)
    return float(total)


def by_currency(positions: list[OpenPosition], sims: dict) -> dict:
    """Faceted per-currency facts. EV is the *native-currency* mean P&L.

    Currencies are reported side by side and are NEVER summed here.
    """
    out: dict[str, dict] = {}
    pnl_native = sims["pnl_native"]
    for cur in ("GBP", "USD"):
        idx = [j for j, p in enumerate(positions) if p.currency == cur]
        n = len(idx)
        open_stake = float(sum(positions[j].stake for j in idx))
        if n and pnl_native.size:
            ev = float(pnl_native[:, idx].sum(axis=1).mean())
        else:
            ev = 0.0
        out[cur] = {
            "n": n,
            "open_stake": round(open_stake, 4),
            "ev": round(ev, 4),
        }
    return out


def per_team(positions: list[OpenPosition], sims: dict, fx_rate: float) -> list[dict]:
    """Mean GBP-view P&L contribution attributed to each team.

    A position's contribution is split evenly across the teams it touches; an
    unteamed position (outright/advancement) is bucketed under its
    ``match_desc`` so nothing is silently dropped.
    """
    pnl_gbp = sims["pnl_gbp"]
    contrib: dict[str, float] = {}
    for j, pos in enumerate(positions):
        ev_j = float(pnl_gbp[:, j].mean()) if pnl_gbp.size else 0.0
        labels = pos.teams if pos.teams else [pos.match_desc or f"bet#{pos.bet_id}"]
        share = ev_j / len(labels)
        for lab in labels:
            contrib[lab] = contrib.get(lab, 0.0) + share
    rows = [
        {"team": k, "ev_contribution": round(v, 4)}
        for k, v in sorted(contrib.items(), key=lambda kv: kv[1])
    ]
    return rows


# --------------------------------------------------------------------------- #
# Feed assembly
# --------------------------------------------------------------------------- #
@dataclass
class PnlResult:
    feed: dict
    positions: list[OpenPosition]
    book_gbp: np.ndarray


def build_risk_pnl(
    positions: list[OpenPosition],
    *,
    generated: str,
    fx_ts: str,
    n_sims: int = DEFAULT_N_SIMS,
    seed: int = DEFAULT_SEED,
    fx_rate: float = DEFAULT_FX_RATE,
) -> PnlResult:
    """Assemble the exact ``risk_pnl.json`` payload.

    ``generated`` and ``fx_ts`` are injected by the caller (no wall-clock in
    library code).
    """
    sims = simulate_book(positions, n_sims=n_sims, seed=seed, fx_rate=fx_rate)
    book_gbp = sims["book_gbp"]

    dist = distribution_stats(book_gbp)
    dist["hard_floor"] = round(hard_floor(positions, fx_rate), 4)
    dist = {k: round(v, 4) if isinstance(v, float) else v for k, v in dist.items()}

    n_open = len(positions)
    insufficient = n_open < INSUFFICIENT_SAMPLE_N
    sources = sorted({p.p_source for p in positions})
    note = (
        f"Open book of {n_open} positions valued over {n_sims} independent "
        "simulations (numpy Generator seed=42). v1 simplification: positions "
        "are drawn INDEPENDENTLY (cross-bet correlation ignored; intra-bet "
        "legs already collapsed to one binary). Win probabilities sourced "
        f"{sources} (implied=de-vigged-free 1/odds fallback, EV-neutral by "
        "construction — not an edge). Currencies are faceted; distribution_gbp "
        "is the ONLY cross-currency view and uses a placeholder FX rate. "
    )
    if insufficient:
        note += (
            f"INSUFFICIENT SAMPLE: only {n_open} open positions "
            f"(< {INSUFFICIENT_SAMPLE_N}) — treat the distribution as a band, "
            "not a precise forecast."
        )

    feed = {
        "meta": {
            "generated": generated,
            "n_sims": int(n_sims),
            "n_open_positions": int(n_open),
            "fx_rate": float(fx_rate),
            "fx_ts": fx_ts,
            "fx_note": (
                "USD converted to GBP for distribution view only — per-venue "
                "tables remain faceted"
            ),
        },
        "distribution_gbp": dist,
        "by_currency": by_currency(positions, sims),
        "histogram": histogram(book_gbp),
        "per_team": per_team(positions, sims, fx_rate),
        "note": note,
    }
    return PnlResult(feed=feed, positions=positions, book_gbp=book_gbp)
