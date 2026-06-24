"""Reporting and analytics for the World Cup Alpha bet ledger.

All functions take a ``db_path`` argument and return either a
:class:`pandas.DataFrame` or a plain :class:`dict`.  They are deliberately
stateless: call them whenever you want a fresh snapshot.

Metrics implemented
-------------------
Bankroll curve
    Cumulative P&L over time, starting from the sum of all bankroll deposits.

Open exposure
    Unsettled bets and their total stake-at-risk.

CLV report
    Per-bet closing-line value and aggregate statistics.  The *closing-line
    value* (CLV) is the primary quality KPI for this project: consistently
    positive CLV indicates that bets are placed at prices better than the
    efficient market consensus at kick-off.

Calibration report
    Bets binned by model probability; observed win rate per bin; Brier score
    for *both* the model probability and the de-vigged market probability.
    This lets us compare model accuracy against the bookmaker's own (margined)
    implied forecast.

    Brier score (Brier, 1950): ``BS = (1/N) sum_i (p_i - o_i)^2`` where
    ``o_i in {0, 1}`` is the outcome.  Lower is better; a perfect forecaster
    scores 0.  A random forecast of 0.5 on binary outcomes scores 0.25.

References
----------
- Brier, G. W. (1950). "Verification of forecasts expressed in terms of
  probability". *Monthly Weather Review* 78(1):1-3.
- Good, I. J. (1952). "Rational decisions". *Journal of the Royal Statistical
  Society B* 14(1):107-114.  (Log-loss / cross-entropy scoring rule.)
"""

from __future__ import annotations

from typing import Dict, Any

import numpy as np
import pandas as pd

from wca.ledger.store import all_bets, all_bankroll_events, REALIZED_STATUSES


def _bets_df(db_path: str) -> pd.DataFrame:
    """Load all bets as a DataFrame with proper dtypes."""
    rows = all_bets(db_path)
    if not rows:
        return pd.DataFrame(
            columns=[
                "id", "ts_utc", "match_id", "match_desc", "market",
                "selection", "platform", "decimal_odds", "stake",
                "model_prob", "market_prob_devig", "ev", "kelly_fraction",
                "status", "settled_pl", "closing_odds", "clv", "notes",
            ]
        )
    df = pd.DataFrame([dict(r) for r in rows])
    for col in ("decimal_odds", "stake", "model_prob", "market_prob_devig",
                "ev", "kelly_fraction", "settled_pl", "closing_odds", "clv"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def bankroll_curve(db_path: str) -> pd.DataFrame:
    """Compute the bankroll curve from deposits and settled bet P&L.

    The curve is built chronologically:

    1. All bankroll-event deposits/withdrawals contribute their ``amount``.
    2. All settled (won/lost) bets contribute their ``settled_pl`` at
       ``ts_utc``.
    3. Events are sorted by timestamp and the ``bankroll`` column is the
       running sum.

    Returns
    -------
    pandas.DataFrame
        Columns: ``ts_utc``, ``event_type`` (``"deposit"``/``"withdrawal"``/
        ``"won"``/``"lost"``), ``delta``, ``bankroll``.
    """
    events = all_bankroll_events(db_path)
    bets = all_bets(db_path)

    records = []
    for e in events:
        amt = float(e["amount"])
        records.append(
            {
                "ts_utc": e["ts_utc"],
                "event_type": "deposit" if amt >= 0 else "withdrawal",
                "delta": amt,
            }
        )

    for b in bets:
        # ``cashed`` (a Polymarket cash-out) realises P&L just like won/lost.
        if b["status"] in REALIZED_STATUSES and b["settled_pl"] is not None:
            records.append(
                {
                    "ts_utc": b["ts_utc"],
                    "event_type": b["status"],
                    "delta": float(b["settled_pl"]),
                }
            )

    if not records:
        return pd.DataFrame(columns=["ts_utc", "event_type", "delta", "bankroll"])

    df = pd.DataFrame(records).sort_values("ts_utc", kind="stable").reset_index(drop=True)
    df["bankroll"] = df["delta"].cumsum()
    return df


def open_exposure(db_path: str) -> pd.DataFrame:
    """Return all open (unsettled) bets and a summary of total stake at risk.

    Returns
    -------
    pandas.DataFrame
        All open bets, sorted by ``ts_utc``.  An extra summary row with
        ``id == None`` and ``match_id == "TOTAL"`` is appended showing the
        summed stake.
    """
    df = _bets_df(db_path)
    open_df = df[df["status"] == "open"].copy()
    if open_df.empty:
        return open_df

    open_df = open_df.sort_values("ts_utc").reset_index(drop=True)

    total_stake = float(open_df["stake"].sum())
    total_ev = float(open_df["ev"].sum()) if open_df["ev"].notna().any() else None

    # Build summary row with dtypes matching open_df to avoid FutureWarning in
    # pandas 2.x about concatenation with empty/NA-only columns.
    summary_data: Dict[str, Any] = {col: [None] for col in open_df.columns}
    summary_data["match_id"] = ["TOTAL"]
    summary_data["match_desc"] = [""]
    summary_data["market"] = [""]
    summary_data["selection"] = [""]
    summary_data["platform"] = [""]
    summary_data["stake"] = [total_stake]
    summary_data["ev"] = [total_ev]
    summary_data["status"] = ["open"]
    summary = pd.DataFrame(summary_data)
    return pd.concat([open_df, summary], ignore_index=True)


# Sources whose open stake counts as "our" sportsbook exposure to hedge. A
# 'punt' is a manual non-model bet; 'model' / 'offer' (free bet / promo) are the
# positions the Polymarket loop should consider hedging.
HEDGEABLE_SOURCES = ("model", "offer")


def _match_team_key(match_desc: str):
    """Frozenset of canonical team names for a single-match ``"A vs B"`` desc.

    Returns ``None`` for accumulators (multi-match, contain ``" | "``) or any
    string that is not a single ``"<home> vs <away>"`` fixture, so callers can
    skip exposure they cannot attribute to one fixture/outcome.
    """
    import re

    from wca.data.teamnames import canonical

    if not match_desc or "|" in match_desc:
        return None
    # Ledger match_desc separators vary across logging paths: "A vs B", "A v B",
    # "A - B". Split on the first of these (spaces required, so hyphenated team
    # names like "Bosnia-Herzegovina" are not split) so result bets logged with
    # " v " (e.g. "Scotland v Morocco") are still attributed to their fixture.
    parts = [
        p.strip()
        for p in re.split(r"\s+(?:vs?|-)\s+", match_desc, maxsplit=1, flags=re.IGNORECASE)
    ]
    if len(parts) != 2 or not all(parts):
        return None
    return frozenset(canonical(p) for p in parts)


# Venues whose money is denominated in USD ($); everything else is GBP (£).
_USD_VENUES = ("polymarket", "kalshi")

# Selection keywords that mean a leg is NOT a clean 1X2 result (so a Polymarket
# 1X2/result bet cannot hedge it): handicaps, totals, scorers, props, etc.
_NONRESULT_KW = (
    "goal", "score", "corner", "card", "shot", "assist", "handicap",
    "btts", "both teams", "2up", "2 up", "minus", "over ", "under ",
    "double chance", "half", "-1", "-2", "-3", "+1", "+2", "+3", "clean sheet",
)
# Keywords that positively mark a clean match-result pick.
_RESULT_KW = ("match odds", "match result", "full time result", "ft result", "to win")


def _platform_currency(platform: str) -> str:
    """``"USD"`` for prediction-market venues, else ``"GBP"``."""
    p = (platform or "").lower()
    return "USD" if any(v in p for v in _USD_VENUES) else "GBP"


def currency_symbol(currency: str) -> str:
    return {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency or "", "£")


def _leg_team(leg: str) -> str:
    """Extract the team a leg is about: handles ``"Team (Match Odds 90)"`` and
    ``"Match Odds -> Team"`` (and falls back to the leftmost token)."""
    t = leg
    if "->" in t:
        t = t.split("->")[-1]
    t = t.split("(")[0]
    # Trim a trailing market suffix like "Morocco - 2UP" → "Morocco".
    t = t.split(" - ")[0]
    return t.strip()


def _leg_is_result(leg: str) -> bool:
    """True only for an unambiguous 1X2 match-result pick (conservative: any
    handicap/total/prop keyword disqualifies it, so we never claim a false
    hedge)."""
    l = leg.lower()
    if any(k in l for k in _NONRESULT_KW):
        return False
    return any(k in l for k in _RESULT_KW)


def _decompose_legs(match_desc: str, selection: str):
    """Split a multi-leg bet into ``(fixture_key, leg_selection, team, is_result)``.

    ``match_desc`` may list several fixtures (``"A vs B | C vs D"``); ``selection``
    lists legs joined by ``" + "``.  Each leg is attributed to the fixture whose
    teams contain the leg's team; a single-fixture bet-builder attributes every
    leg to that one fixture.  Returns ``[]`` for a plain single-leg straight bet.
    """
    import re

    from wca.data.teamnames import canonical

    matches = [m.strip() for m in match_desc.split("|") if m.strip()]
    legs = [x.strip() for x in re.split(r"\s+\+\s+", selection) if x.strip()]
    if len(matches) <= 1 and len(legs) <= 1:
        return []

    fixtures = []  # (key, {canonical teams}, desc)
    for m in matches:
        parts = [p.strip() for p in re.split(r"\s+(?:vs?|-)\s+", m, maxsplit=1, flags=re.IGNORECASE)]
        if len(parts) == 2 and all(parts):
            fixtures.append((frozenset(canonical(p) for p in parts),
                             {canonical(parts[0]), canonical(parts[1])}, m))
    if not fixtures:
        return []

    out = []
    for leg in legs:
        team = _leg_team(leg)
        team_c = canonical(team)
        assigned = next((f for f in fixtures if team_c in f[1]), None)
        if assigned is None and len(fixtures) == 1:
            assigned = fixtures[0]  # single-match builder: all legs → that match
        if assigned is not None:
            out.append((assigned[0], leg, team, _leg_is_result(leg)))
    return out


def sportsbook_open_exposure_by_match(
    db_path: str, sources=HEDGEABLE_SOURCES, *, decompose_multileg: bool = False
) -> Dict[frozenset, Dict[str, Any]]:
    """Open sportsbook exposure, keyed by canonical team pair.

    Only open bets whose ``source`` is in *sources* (default model + offer/free
    bets; pass ``sources=None`` to include **every** source / book / venue) are
    counted.  Straight ``"<home> vs <away>"`` bets accrue under ``outcomes``.
    Free bets (``source == 'offer'``) are stake-not-returned, so their **profit
    at risk** ``stake*(odds-1)`` is what's exposed, not stake.

    With ``decompose_multileg=True``, accumulators / bet-builders (previously
    skipped) are split into per-fixture legs collected under ``legs`` — so no
    open bet is "missed out". A leg is flagged ``is_result`` only when it is an
    unambiguous 1X2 pick (the only thing a Polymarket result bet can hedge).

    Returns ``frozenset({home, away}) -> {match_desc, total_stake, outcomes, legs}``
    where each outcome / leg carries its ``currency`` ("USD"/"GBP"/"MIXED").
    """
    df = _bets_df(db_path)
    if df.empty:
        return {}
    open_df = df[df["status"] == "open"].copy()
    if sources is not None and "source" in open_df.columns:
        open_df = open_df[open_df["source"].isin(set(sources))]
    if open_df.empty:
        return {}

    out: Dict[frozenset, Dict[str, Any]] = {}

    def _entry(key, match_desc):
        return out.setdefault(
            key,
            {"match_desc": match_desc, "outcomes": {}, "legs": [], "total_stake": 0.0},
        )

    def _accrue_currency(oc, cur):
        seen = oc.setdefault("_cur", set())
        seen.add(cur)
        oc["currency"] = next(iter(seen)) if len(seen) == 1 else "MIXED"

    for _, b in open_df.iterrows():
        match_desc = str(b.get("match_desc") or "")
        sel = str(b.get("selection") or "").strip()
        stake = float(b.get("stake") or 0.0)
        odds = float(b.get("decimal_odds") or 0.0)
        platform = str(b.get("platform") or "")
        source = str(b.get("source") or "")
        cur = _platform_currency(platform)
        risk = stake * (odds - 1.0) if source == "offer" else stake

        key = _match_team_key(match_desc)
        is_multileg = (" + " in sel) or ("|" in match_desc)

        # Multi-leg (acca / single-match bet-builder): surface each leg under
        # its fixture rather than treating the whole "A + B + C" string as one
        # straight outcome. Only when the caller opts in.
        if decompose_multileg and is_multileg:
            legs = _decompose_legs(match_desc, sel)
            if legs:
                for leg_key, leg_sel, team, is_result in legs:
                    entry = _entry(leg_key, match_desc)
                    entry["legs"].append({
                        "bet_type": "acca" if "|" in match_desc else "builder",
                        "selection": leg_sel,
                        "team": team,
                        "is_result": is_result,
                        "stake": stake,    # whole-bet stake at risk (all-or-nothing)
                        "risk": risk,
                        "source": source,
                        "platform": platform,
                        "currency": cur,
                        "full_desc": match_desc,
                    })
                continue
            # decomposition found no attributable leg → fall through to straight.

        if key is not None:
            # Straight single-match bet → clean, fully hedgeable outcome.
            entry = _entry(key, match_desc)
            oc = entry["outcomes"].setdefault(
                sel, {"stake": 0.0, "risk": 0.0, "n": 0, "sources": set(),
                      "platforms": set(), "currency": cur},
            )
            oc["stake"] += stake
            oc["risk"] += risk
            oc["n"] += 1
            oc["sources"].add(source)
            oc["platforms"].add(platform)
            _accrue_currency(oc, cur)
            entry["total_stake"] += stake

    # Drop the private currency-tracking set before returning.
    for entry in out.values():
        for oc in entry["outcomes"].values():
            oc.pop("_cur", None)
    return out


def clv_report(db_path: str) -> Dict[str, Any]:
    """Per-bet CLV table and aggregate CLV statistics.

    Only bets with a non-null ``closing_odds`` are included.

    Returns
    -------
    dict with keys:
        ``"per_bet"``
            DataFrame with columns ``id``, ``match_desc``, ``selection``,
            ``decimal_odds``, ``closing_odds``, ``clv``, ``status``.
        ``"avg_clv"``
            Mean CLV across all bets with closing odds recorded (float).
        ``"pct_beat_close"``
            Fraction of bets with CLV > 0 (float in [0, 1]).
        ``"n_bets"``
            Number of bets with closing odds recorded (int).
    """
    df = _bets_df(db_path)
    clv_df = df[df["closing_odds"].notna()].copy()

    if clv_df.empty:
        return {
            "per_bet": pd.DataFrame(
                columns=["id", "match_desc", "selection", "decimal_odds",
                         "closing_odds", "clv", "status"]
            ),
            "avg_clv": float("nan"),
            "pct_beat_close": float("nan"),
            "n_bets": 0,
        }

    per_bet = clv_df[
        ["id", "match_desc", "selection", "decimal_odds", "closing_odds", "clv", "status"]
    ].copy().reset_index(drop=True)

    avg_clv = float(clv_df["clv"].mean())
    pct_beat = float((clv_df["clv"] > 0).mean())

    return {
        "per_bet": per_bet,
        "avg_clv": avg_clv,
        "pct_beat_close": pct_beat,
        "n_bets": len(clv_df),
    }


def staking_stats(db_path: str) -> Dict[str, Any]:
    """Evidence inputs for the pre-registered Kelly ladder (KellyPolicy).

    Counts only *settled* bets (won/lost) that also have closing odds
    recorded — the ladder promotes on demonstrated CLV, and CLV requires a
    closing line. Bets are ordered by id (insertion order) for the rolling
    window.

    Returns
    -------
    dict with keys:
        ``"n_settled"``     settled bets with closing odds (int)
        ``"clv_to_date"``   mean CLV over those bets, or ``None`` if zero bets
        ``"rolling50_clv"`` mean CLV over the most recent 50, or ``None`` if
                            fewer than 50 exist
    """
    df = _bets_df(db_path)
    eligible = df[
        df["status"].str.lower().isin(["won", "lost"]) & df["clv"].notna()
    ].sort_values("id")

    n = len(eligible)
    if n == 0:
        return {"n_settled": 0, "clv_to_date": None, "rolling50_clv": None}
    clv_to_date = float(eligible["clv"].mean())
    rolling50 = float(eligible["clv"].tail(50).mean()) if n >= 50 else None
    return {"n_settled": n, "clv_to_date": clv_to_date, "rolling50_clv": rolling50}


def calibration_report(db_path: str, n_bins: int = 5) -> Dict[str, Any]:
    """Calibration analysis comparing model probability to market probability.

    Only settled bets (won/lost) with a non-null ``model_prob`` are included.
    Bets are binned by ``model_prob`` into ``n_bins`` equal-width bins in
    [0, 1].  For each bin the observed win rate is computed.

    Two Brier scores are computed over the *full settled sample* (not per-bin):

    * **model Brier score**: ``BS_model = mean((model_prob - outcome)^2)``
    * **market Brier score**: ``BS_market = mean((market_prob_devig - outcome)^2)``
      (only for bets where ``market_prob_devig`` is not null)

    A lower Brier score indicates better calibration.  If
    ``BS_model < BS_market`` the model is better-calibrated than the
    bookmaker's own de-vigged odds.

    Parameters
    ----------
    db_path:
        Path to the SQLite database.
    n_bins:
        Number of equal-width probability bins in [0, 1].

    Returns
    -------
    dict with keys:
        ``"calibration_bins"``
            DataFrame with columns ``bin_low``, ``bin_high``, ``n_bets``,
            ``observed_win_rate``, ``mean_model_prob``.
        ``"brier_model"``
            Brier score for ``model_prob`` vs outcomes (float or NaN).
        ``"brier_market"``
            Brier score for ``market_prob_devig`` vs outcomes (float or NaN).
        ``"n_settled"``
            Number of settled bets used (int).
    """
    df = _bets_df(db_path)
    settled = df[df["status"].isin(("won", "lost")) & df["model_prob"].notna()].copy()

    if settled.empty:
        empty_bins = pd.DataFrame(
            columns=["bin_low", "bin_high", "n_bets", "observed_win_rate", "mean_model_prob"]
        )
        return {
            "calibration_bins": empty_bins,
            "brier_model": float("nan"),
            "brier_market": float("nan"),
            "n_settled": 0,
        }

    # Binary outcome: 1 for won, 0 for lost.
    settled["outcome"] = (settled["status"] == "won").astype(float)

    # Brier score: model.
    model_p = settled["model_prob"].to_numpy(dtype=float)
    outcomes = settled["outcome"].to_numpy(dtype=float)
    brier_model = float(np.mean((model_p - outcomes) ** 2))

    # Brier score: market (only bets that have market_prob_devig).
    mkt_mask = settled["market_prob_devig"].notna()
    if mkt_mask.sum() >= 1:
        mkt_p = settled.loc[mkt_mask, "market_prob_devig"].to_numpy(dtype=float)
        mkt_o = settled.loc[mkt_mask, "outcome"].to_numpy(dtype=float)
        brier_market = float(np.mean((mkt_p - mkt_o) ** 2))
    else:
        brier_market = float("nan")

    # Calibration bins.
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_records = []
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        # Include upper edge only in the last bin to capture p == 1.0.
        if i < n_bins - 1:
            mask = (settled["model_prob"] >= lo) & (settled["model_prob"] < hi)
        else:
            mask = (settled["model_prob"] >= lo) & (settled["model_prob"] <= hi)
        subset = settled[mask]
        n = int(len(subset))
        obs_wr = float(subset["outcome"].mean()) if n > 0 else float("nan")
        mean_mp = float(subset["model_prob"].mean()) if n > 0 else float("nan")
        bin_records.append(
            {
                "bin_low": round(float(lo), 4),
                "bin_high": round(float(hi), 4),
                "n_bets": n,
                "observed_win_rate": obs_wr,
                "mean_model_prob": mean_mp,
            }
        )

    return {
        "calibration_bins": pd.DataFrame(bin_records),
        "brier_model": brier_model,
        "brier_market": brier_market,
        "n_settled": len(settled),
    }


def summary(db_path: str) -> Dict[str, Any]:
    """High-level summary dict suitable for printing to the terminal.

    Returns
    -------
    dict
        Keys include ``total_bets``, ``open_bets``, ``won_bets``,
        ``lost_bets``, ``void_bets``, ``total_staked``, ``total_pl``,
        ``roi``, ``avg_clv``, ``pct_beat_close``, ``brier_model``,
        ``brier_market``, ``total_deposited``, ``current_bankroll``.
    """
    df = _bets_df(db_path)

    total_bets = len(df)
    open_bets = int((df["status"] == "open").sum())
    won_bets = int((df["status"] == "won").sum())
    lost_bets = int((df["status"] == "lost").sum())
    void_bets = int((df["status"] == "void").sum())
    cashed_bets = int((df["status"] == "cashed").sum())

    # Realised set includes cash-outs: a cashed row's settled_pl is realised
    # money and its stake was real money put at risk, so both belong in P&L/ROI.
    settled = df[df["status"].isin(REALIZED_STATUSES)]
    total_staked = float(settled["stake"].sum()) if not settled.empty else 0.0
    total_pl = float(settled["settled_pl"].sum()) if not settled.empty else 0.0
    roi = (total_pl / total_staked) if total_staked > 0 else float("nan")

    # Money currently at risk: stakes of open (unsettled) bets. total_staked
    # deliberately stays settled-only so ROI is realised-return over realised
    # stakes; the bot surfaces both.
    open_df = df[df["status"] == "open"]
    open_staked = float(open_df["stake"].sum()) if not open_df.empty else 0.0

    clv_data = clv_report(db_path)
    avg_clv = clv_data["avg_clv"]
    pct_beat = clv_data["pct_beat_close"]

    cal = calibration_report(db_path)
    brier_model = cal["brier_model"]
    brier_market = cal["brier_market"]

    # Bankroll: sum of deposits + total settled P&L.
    events = all_bankroll_events(db_path)
    total_deposited = float(sum(float(e["amount"]) for e in events))
    current_bankroll = total_deposited + total_pl

    # Per-source breakdown: n (all bets), staked (all bets), settled_pl
    # (settled won/lost only). The three canonical sources are always present
    # so downstream consumers can index them unconditionally. This is purely
    # additive and does not touch the CLV / calibration keys above.
    by_source: Dict[str, Any] = {
        s: {"n": 0, "staked": 0.0, "settled_pl": 0.0}
        for s in ("model", "offer", "punt")
    }
    if "source" in df.columns:
        src_series = df["source"].fillna("model").astype(str)
    else:
        src_series = pd.Series(["model"] * len(df), index=df.index)
    for src, grp in df.groupby(src_series):
        blk = by_source.setdefault(
            str(src), {"n": 0, "staked": 0.0, "settled_pl": 0.0}
        )
        blk["n"] = int(len(grp))
        blk["staked"] = float(grp["stake"].sum()) if not grp.empty else 0.0
        settled_grp = grp[grp["status"].isin(REALIZED_STATUSES)]
        blk["settled_pl"] = (
            float(settled_grp["settled_pl"].sum()) if not settled_grp.empty else 0.0
        )

    return {
        "by_source": by_source,
        "total_bets": total_bets,
        "open_bets": open_bets,
        "won_bets": won_bets,
        "lost_bets": lost_bets,
        "void_bets": void_bets,
        "cashed_bets": cashed_bets,
        "total_staked": total_staked,
        "open_staked": open_staked,
        "total_pl": total_pl,
        "roi": roi,
        "avg_clv": avg_clv,
        "pct_beat_close": pct_beat,
        "brier_model": brier_model,
        "brier_market": brier_market,
        "total_deposited": total_deposited,
        "current_bankroll": current_bankroll,
    }
