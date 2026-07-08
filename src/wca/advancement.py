"""Tournament-advancement edges: Monte-Carlo sim vs Polymarket.

This module is the deterministic core behind ``scripts/wca_advancement.py``. It
runs the project's 2026 World Cup Monte-Carlo simulator
(:mod:`wca.sim.tournament2026`) to obtain, per team, the probability of reaching
each tournament stage, then compares those simulated probabilities to the
matching Polymarket advancement / group-winner markets and computes a
fee-adjusted edge and a quarter-Kelly stake for the Polymarket pool.

Honesty caveats (read these)
----------------------------
* The simulator drives every future match through ``prob_fn`` (see
  :func:`make_prob_fn`). Fixtures with a complete tradable 1X2 book are anchored
  to the same de-vigged market-consensus blend as the match card; generated
  knockout fixtures with no market fall back to an Elo + Dixon-Coles model
  blend. Treat the edges as model-vs-market disagreements to be sized
  conservatively, not as ground truth.
* Knockout ties (including the 90-minute-draw -> extra-time / penalties path)
  are resolved entirely inside the simulator's ET model. "Advancing" therefore
  *includes* winning on penalties, which matches how Polymarket resolves these
  markets ("reach stage X" = the team is among the teams in stage X, however it
  got there).
* Host advantage (United States, Mexico, Canada) is applied only on those three
  teams' own group fixtures, derived from the scheduled-fixture ``neutral``
  flag exactly as :mod:`wca.card` does. Every other match is neutral.

Stage <-> Polymarket mapping
----------------------------
The 2026 format is: group stage -> Round of 32 (first knockout round) -> Round
of 16 -> quarter-finals -> semi-finals -> final. The simulator's ``reach``
labels line up with the Polymarket questions as follows (and the *resolution
semantics must match exactly*):

==========================================  =================  =====================
Polymarket event                            sim quantity       meaning
==========================================  =================  =====================
Team to advance to Knockout Stages          reach["R32"]       top-2 or best-8 third
Nation To Reach Round of 16                 reach["R16"]        won the R32 tie
Nation To Reach Quarterfinals               reach["QF"]         won the R16 tie
Nation To Reach Semifinals                  reach["SF"]         won the QF tie
Nation to Reach Final                       reach["F"]          won the SF tie
World Cup Winner                            win                won the final
World Cup Group <X> Winner                  group_position 1st  finished 1st in group
==========================================  =================  =====================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from wca.card import BlendWeights, FittedModels, dc_probs, elo_probs, market_consensus
from wca.data.teamnames import canonical
from wca.markets import bankroll as pm_rule
from wca.markets import kelly as kelly_mod
from wca.models import venues as venues_mod
from wca.models.structural import load_country_factors
from wca.selection import bucket_rank, longshot_no_cash, prob_bucket
from wca.sim.tournament2026 import GROUP_LETTERS, Result, TournamentSimulator

# ---------------------------------------------------------------------------
# Official 2026 FIFA World Cup groups.
# ---------------------------------------------------------------------------
# Verified 2026-06-11 against the FIFA final-draw results (5 Dec 2025) and the
# Wikipedia draw page, AND cross-checked to be internally consistent with the
# 72 scheduled FIFA-World-Cup fixtures in data/raw/results.csv: every scheduled
# group-stage match is intra-group and each group has exactly 6 fixtures (the
# fixtures themselves therefore confirm the draw). Team names use the martj42
# results-dataset spelling so every name is a key in the model ratings.
#
# Sources (accessed 2026-06-11):
#   https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/articles/final-draw-results
#   https://en.wikipedia.org/wiki/2026_FIFA_World_Cup_draw
WC2026_GROUPS: Dict[str, List[str]] = {
    "A": ["Mexico", "South Africa", "South Korea", "Czech Republic"],
    "B": ["Canada", "Bosnia and Herzegovina", "Qatar", "Switzerland"],
    "C": ["Brazil", "Morocco", "Haiti", "Scotland"],
    "D": ["United States", "Paraguay", "Australia", "Turkey"],
    "E": ["Germany", "Curaçao", "Ivory Coast", "Ecuador"],
    "F": ["Netherlands", "Japan", "Sweden", "Tunisia"],
    "G": ["Belgium", "Egypt", "Iran", "New Zealand"],
    "H": ["Spain", "Cape Verde", "Saudi Arabia", "Uruguay"],
    "I": ["France", "Senegal", "Iraq", "Norway"],
    "J": ["Argentina", "Algeria", "Austria", "Jordan"],
    "K": ["Portugal", "DR Congo", "Uzbekistan", "Colombia"],
    "L": ["England", "Croatia", "Ghana", "Panama"],
}

# The three host nations receive home advantage on their own group fixtures.
HOST_NATIONS: Tuple[str, ...] = ("United States", "Mexico", "Canada")

# Stage ordering used for reporting / monotonicity, best (easiest) first.
STAGE_ORDER: Tuple[str, ...] = ("R32", "R16", "QF", "SF", "F", "win")

# "Further-out" ordering for the canonical selection rule's SECONDARY key
# (wca.selection): a deeper knockout stage is the analogue of a further-out
# fixture (Final/Win > SF > QF > R16 > R32 > group-winner). Higher = further
# out; the sort uses ``-stage_further_out`` so deeper stages rank first within
# a model-prob bucket. GW (group winner) is the nearest-term market.
_STAGE_FURTHER_OUT: Dict[str, int] = {
    "GW": 0, "R32": 1, "R16": 2, "QF": 3, "SF": 4, "F": 5, "win": 6,
}


def stage_further_out(stage: str) -> int:
    """Deeper-stage rank for the selection rule's further-out secondary key."""
    return _STAGE_FURTHER_OUT.get(str(stage), 0)

# Polymarket advancement-event title -> sim stage label.
# Group-winner events are handled separately (one event per group letter).
PM_STAGE_EVENTS: Dict[str, str] = {
    "World Cup: Team to advance to Knockout Stages": "R32",
    "World Cup: Nation To Reach Round of 16": "R16",
    "World Cup: Nation To Reach Quarterfinals": "QF",
    "World Cup: Nation To Reach Semifinals": "SF",
    "World Cup: Nation to Reach Final": "F",
    "World Cup Winner": "win",
}

# Human-readable stage labels for the report.
STAGE_LABEL: Dict[str, str] = {
    "R32": "Reach R32 (knockout)",
    "R16": "Reach Round of 16",
    "QF": "Reach Quarterfinals",
    "SF": "Reach Semifinals",
    "F": "Reach Final",
    "win": "Win the World Cup",
    "GW": "Win group",
}

# Polymarket sports taker-fee coefficient: fee per share = COEF * p * (1 - p).
PM_TAKER_FEE_COEF: float = 0.03

# A Polymarket binary quoting YES at/beyond these bounds is effectively RESOLVED
# (the earlier-round tie is decided); there is no tradable edge left, only a
# phantom "sim vs 0.99" disagreement. Such rows are dropped from the comparison
# and withheld from the Action Desk feed (fix 2026-07-08).
PM_RESOLVED_HI: float = 0.98
PM_RESOLVED_LO: float = 0.02

# Freshest played-results feed. ``data/raw`` results CSVs lag 1-3 days
# (documented in CLAUDE.md); this derived JSON (scripts/wca_build_wc2026_results
# .py) is refreshed continuously and carries fixture/score/outcome/kickoff_utc.
# Knockout pinning unions BOTH sources, preferring whichever has the result —
# a lagging CSV alone published phantom edges (USA P(QF)=0.317 after its Jul-6
# elimination).
RESULTS_JSON_PATH: str = "data/processed/wc2026_results.json"

# Polymarket pool sizing — from the project-wide GLOBAL RULE
# (``wca.markets.bankroll``): ¼-Kelly of £3,000 ± realised P&L at $1.33/£,
# 4% per-bet cap. Never re-hardcode a pool figure here (the old hardcoded
# $1,310 silently overrode the rule). This module-level bankroll is the BASE
# pool (realised P&L unknown at import time); callers with ledger access pass
# the P&L-adjusted figure explicitly.
PM_POOL_BANKROLL: float = pm_rule.pm_bankroll_usd()
PM_KELLY_FRACTION: float = pm_rule.PM_KELLY_FRACTION
PM_PER_BET_CAP: float = pm_rule.PM_MAX_STAKE_FRAC

# Default dilution of each co-host's home bonus on the venue-aware path. The
# legacy path (venue_aware=False) ignores this and uses the full bonus.
DEFAULT_HOST_FACTOR: float = 0.5

# Representative group-stage home-venue altitude per host nation (metres). The
# Mexican hosts play their group games at altitude (Estadio Azteca, Mexico City);
# the US/Canadian host venues are effectively sea level.
HOST_VENUE_ALTITUDE_M: Dict[str, float] = {
    "Mexico": 2240.0,
    "United States": 30.0,
    "Canada": 50.0,
}


# ---------------------------------------------------------------------------
# prob_fn: card-style Elo + Dixon-Coles + market blend when market exists.
# ---------------------------------------------------------------------------


def _host_for(home: str, away: str) -> Optional[str]:
    """Return the host nation in this pairing, if either side is a host.

    Only matters for the three hosts' own group games; the simulator queries
    ``prob_fn`` with ``knockout`` to drive every match, and we treat all
    knockout matches and all non-host group matches as neutral.
    """
    if home in HOST_NATIONS:
        return home
    if away in HOST_NATIONS:
        return away
    return None


def make_prob_fn(
    models: FittedModels,
    *,
    odds_df: Optional[pd.DataFrame] = None,
    weights: Optional[BlendWeights] = None,
    venue_aware: bool = False,
    host_factor: float = DEFAULT_HOST_FACTOR,
    altitude_coef: float = venues_mod.DEFAULT_ALTITUDE_COEF,
):
    """Build ``prob_fn(team_a, team_b, knockout) -> (p_a, p_draw, p_b)``.

    For fixtures with a complete tradable 1X2 market, uses the same convex
    Elo/Dixon-Coles/market blend as :mod:`wca.card`: each book is de-vigged with
    Shin, the median fair probability is taken as market consensus, then the
    components are blended with ``weights``. Fixtures without market consensus
    (normally generated knockout ties) fall back to an Elo + Dixon-Coles blend,
    using the relative Elo/DC weights when possible and 50/50 if the model
    weights are zero.

    Venue handling
    --------------
    * Group matches involving a host (United States, Mexico, Canada) are played
      at home for that host: ``neutral=False`` is *not* used directly because
      the simulator does not pass venue; instead we pass ``host=<host>`` and
      ``neutral=True`` to the Elo rating-diff helper, which grants the host the
      home-advantage bonus in the correct direction. Dixon-Coles is queried at
      ``neutral=True`` (its home-advantage term is symmetric and venue-specific
      host handling is not modelled there — a small, documented approximation).
    * Every other group match and **all** knockout matches are neutral.

    Venue/geography awareness (opt-in)
    ----------------------------------
    With ``venue_aware=True`` the host bonus is **diluted** by ``host_factor``
    (the legacy single-host full bonus is mis-specified for three co-hosts who
    are only at home in the group stage) and an **altitude** term is added that
    taxes a sea-level visitor at a high-altitude venue — chiefly Mexico's group
    games at Estadio Azteca. With ``venue_aware=False`` (default) the full,
    undiluted host bonus is used exactly as before.

    The returned probabilities are ``(p_a, p_draw, p_b)`` for the *ordered* pair
    ``(team_a, team_b)``, i.e. ``team_a`` is treated as the nominal home side.
    The simulator normalises the triple, so only the ratio matters.
    """
    base_adv = models.rater.home_advantage
    factors = load_country_factors() if venue_aware else {}
    w = (weights or BlendWeights()).normalised()
    market_lookup = _market_consensus_lookup(odds_df)
    model_weight_sum = w.elo + w.dc
    if model_weight_sum > 0:
        fallback_elo = w.elo / model_weight_sum
        fallback_dc = w.dc / model_weight_sum
    else:
        fallback_elo = fallback_dc = 0.5

    def _host_points(host: Optional[str], opponent: Optional[str]) -> Optional[float]:
        """Diluted, altitude-adjusted host bonus, or ``None`` for legacy behaviour."""
        if not venue_aware or host is None:
            return None
        venue_alt = HOST_VENUE_ALTITUDE_M.get(host)
        opp = factors.get(opponent) if opponent is not None else None
        opp_alt = opp.home_altitude_m if opp is not None else None
        return venues_mod.host_advantage_points(
            base_adv,
            factor=host_factor,
            venue_altitude_m=venue_alt,
            visitor_home_altitude_m=opp_alt,
            altitude_coef=altitude_coef,
        )

    def prob_fn(team_a: str, team_b: str, knockout: bool) -> Tuple[float, float, float]:
        a = canonical(team_a)
        b = canonical(team_b)
        host = None if knockout else _host_for(a, b)
        # The visiting (non-host) side bears any altitude tax.
        opponent = b if host == a else (a if host == b else None)
        host_points = _host_points(host, opponent)
        # Elo: pass host on a neutral venue so the host bonus is applied.
        e_h, e_d, e_a = elo_probs(
            models, a, b, neutral=True, host=host, host_points=host_points
        )
        # Dixon-Coles: neutral (no per-host venue term available).
        d_h, d_d, d_a = dc_probs(models, a, b, neutral=True)
        # Market anchor applies to KNOCKOUT fixtures too (fix 2026-07-08): once
        # the bracket is set, R16/QF/... pairings are FIXED fixtures with live
        # 1X2 books, and the 90-minute 1X2 is exactly what the simulator needs
        # here (the ET/pens path is resolved downstream by the sim's ET model).
        # The old ``None if knockout`` guard silently ran the ENTIRE post-group
        # tournament on the Elo/DC-only fallback — measured worse on the 22
        # played KO ties (log-loss 0.7465 model-only vs 0.7190 market, n=22).
        # Both pair orientations are stored in the lookup.
        mkt = market_lookup.get((a, b))
        if mkt is None:
            p_a = fallback_elo * e_h + fallback_dc * d_h
            p_d = fallback_elo * e_d + fallback_dc * d_d
            p_b = fallback_elo * e_a + fallback_dc * d_a
        else:
            m_h, m_d, m_a = mkt
            p_a = w.elo * e_h + w.dc * d_h + w.market * m_h
            p_d = w.elo * e_d + w.dc * d_d + w.market * m_d
            p_b = w.elo * e_a + w.dc * d_a + w.market * m_a
        s = p_a + p_d + p_b
        if s <= 0:
            return (1 / 3, 1 / 3, 1 / 3)
        return (p_a / s, p_d / s, p_b / s)

    return prob_fn


def _market_consensus_lookup(
    odds_df: Optional[pd.DataFrame],
) -> Dict[Tuple[str, str], Tuple[float, float, float]]:
    """Return ordered-pair -> de-vigged market 1X2 consensus.

    Keys use canonical team names. Both directions are stored so simulator
    calls in the reverse nominal order still pick up the tradable market, with
    home/away probabilities swapped.
    """
    if odds_df is None or odds_df.empty or "market" not in odds_df.columns:
        return {}

    required = {
        "event_id",
        "home_team",
        "away_team",
        "commence_time",
        "bookmaker_key",
        "market",
        "outcome_name",
        "decimal_odds",
    }
    if not required.issubset(set(odds_df.columns)):
        return {}

    out: Dict[Tuple[str, str], Tuple[float, float, float]] = {}
    h2h = odds_df[odds_df["market"] == "h2h"]
    if h2h.empty:
        return out

    for (_eid, home_raw, away_raw, _commence), grp in h2h.groupby(
        ["event_id", "home_team", "away_team", "commence_time"], sort=False
    ):
        home_disp, away_disp = str(home_raw), str(away_raw)
        home, away = canonical(home_disp), canonical(away_disp)
        books: Dict[str, Dict[str, float]] = {}
        for book, bgrp in grp.groupby("bookmaker_key"):
            prices: Dict[str, float] = {}
            for _, r in bgrp.iterrows():
                name = str(r["outcome_name"])
                try:
                    odd = float(r["decimal_odds"])
                except (TypeError, ValueError):
                    continue
                if name == home_disp:
                    prices["home"] = odd
                elif name == away_disp:
                    prices["away"] = odd
                elif name.lower() == "draw":
                    prices["draw"] = odd
            if prices:
                books[str(book)] = prices
        mkt = market_consensus(books)
        if mkt is None:
            continue
        h, d, a = float(mkt[0]), float(mkt[1]), float(mkt[2])
        out[(home, away)] = (h, d, a)
        out[(away, home)] = (a, d, h)
    return out


# ---------------------------------------------------------------------------
# Run the advancement simulation.
# ---------------------------------------------------------------------------


def load_played_group_results(
    groups: Optional[Dict[str, List[str]]] = None,
    results_path: Optional[str] = None,
) -> List[Result]:
    """Return the already-played 2026 World Cup *group-stage* results.

    Reads the cleaned results dataset (the same source the models are fit on),
    keeps only ``FIFA World Cup`` fixtures dated 2026 with a non-NA scoreline
    where **both** teams sit in the same group (which guarantees the row is a
    group match, never a knockout tie), and returns them as
    :class:`~wca.sim.tournament2026.Result` objects.

    These are passed to :class:`TournamentSimulator` so that completed group
    matches are *fixed* (not re-simulated) — the advancement probabilities then
    reflect the actual results so far, not a from-scratch pre-tournament sim.

    Team names are mapped through :func:`wca.data.teamnames.canonical` so they
    line up with the group definitions (and therefore the model ratings).
    """
    grp = groups if groups is not None else WC2026_GROUPS
    team_to_group = {t: g for g, ts in grp.items() for t in ts}

    if results_path is None:
        from wca.data.cleaning import resolve_results_path

        results_path = resolve_results_path()

    df = pd.read_csv(results_path)
    df = df[df["tournament"] == "FIFA World Cup"].copy()
    dates = pd.to_datetime(df["date"], errors="coerce")
    df = df[dates.dt.year == 2026]
    df = df.dropna(subset=["home_score", "away_score"])

    out: List[Result] = []
    for _, r in df.iterrows():
        home = canonical(str(r["home_team"]))
        away = canonical(str(r["away_team"]))
        gh, ga = team_to_group.get(home), team_to_group.get(away)
        # Same-group => a group-stage fixture; skip anything else (knockouts,
        # or a name that does not resolve to a 2026 group team).
        if gh is None or gh != ga:
            continue
        out.append(
            Result(
                home=home,
                away=away,
                home_goals=int(r["home_score"]),
                away_goals=int(r["away_score"]),
            )
        )
    return out


def _played_wc2026_json_rows(
    results_json_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Parsed rows of the freshest played-results feed (best-effort, never raises).

    Reads ``data/processed/wc2026_results.json`` (schema: ``{"results":
    [{date, fixture, score, outcome, kickoff_utc?}]}``, one row per PLAYED 2026
    WC fixture) and returns ``{home, away, home_goals, away_goals, outcome,
    when}`` dicts with canonical team names. Unparseable rows are skipped; a
    missing/corrupt file yields ``[]`` so the CSV path keeps working alone.
    """
    import json

    path = results_json_path if results_json_path is not None else RESULTS_JSON_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            results = json.load(fh).get("results", [])
    except (OSError, ValueError):
        return []

    out: List[Dict[str, Any]] = []
    for r in results:
        fixture = str(r.get("fixture") or "")
        score = str(r.get("score") or "")
        if " vs " not in fixture or "-" not in score:
            continue
        home_raw, away_raw = fixture.split(" vs ", 1)
        try:
            hg_s, ag_s = score.split("-", 1)
            hg, ag = int(hg_s), int(ag_s)
        except ValueError:
            continue
        when = pd.to_datetime(
            r.get("kickoff_utc") or r.get("date"), errors="coerce", utc=True
        )
        if pd.notna(when):
            when = when.tz_localize(None)
        out.append(
            {
                "home": canonical(home_raw.strip()),
                "away": canonical(away_raw.strip()),
                "home_goals": hg,
                "away_goals": ag,
                "outcome": (str(r["outcome"]) if r.get("outcome") else None),
                "when": when,
            }
        )
    return out


def load_played_knockout_results(
    groups: Optional[Dict[str, List[str]]] = None,
    results_path: Optional[str] = None,
    shootouts_df: Optional[pd.DataFrame] = None,
    results_json_path: Optional[str] = None,
) -> Dict[FrozenSet[str], str]:
    """Return already-played 2026 World Cup *knockout* ties with a resolved winner.

    Mirrors :func:`load_played_group_results` but selects the CROSS-group
    fixtures (a knockout tie is between two teams that are **not** in the same
    2026 group) that have a final scoreline, and resolves each tie to a winner
    *name*. Rows are pinned from the **union** of two sources, preferring
    whichever has the result (fix 2026-07-08 — the CSV alone lags 1-3 days and
    published phantom edges for already-eliminated teams):

    * the cleaned results CSV (``resolve_results_path()``), as before;
    * the freshest derived feed ``data/processed/wc2026_results.json``
      (:data:`RESULTS_JSON_PATH`), which is refreshed continuously.

    Per candidate row the winner is resolved as:

    * a decisive 90-minute result -> the higher-scoring side;
    * a drawn 90-minute result -> the penalty-shootout winner via
      :func:`wca.data.results.shootout_winner` (matched on the unordered pair and
      the fixture date);
    * a drawn 90-minute result with **no** shootout record but whose
      results-JSON ``outcome`` field names a side (``home``/``away`` despite the
      drawn scoreline) -> that side. NOTE: the current builder
      (``scripts/wca_build_wc2026_results.py``) computes ``outcome`` from the
      score, so drawn ties carry ``"draw"`` today and this fallback is dormant —
      it exists so a feed that marks the advancing side pins without a code
      change; a drawn tie with neither source stays unpinned;
    * otherwise the tie is *skipped* (it cannot be pinned — leaving it unfixed
      keeps the sim from inventing a winner it has no basis for; the
      state-freshness gate, :func:`knockout_state_staleness`, then withholds the
      affected teams instead of publishing phantom edges).

    These are passed to :class:`TournamentSimulator` so completed knockout ties
    are *fixed* rather than re-simulated from scratch — otherwise an eliminated
    team still shows a large survival probability and an advanced team a tiny
    one, because the simulator would replay every knockout round.

    Returns
    -------
    ``{frozenset((home, away)): winner_name}`` keyed by the unordered team pair
    (canonical names). The R32-slot participants are constant across sims once
    the groups are finished, so a pinned tie applies uniformly.
    """
    grp = groups if groups is not None else WC2026_GROUPS
    team_to_group = {t: g for g, ts in grp.items() for t in ts}

    if results_path is None:
        from wca.data.cleaning import resolve_results_path

        results_path = resolve_results_path()

    df = pd.read_csv(results_path)
    df = df[df["tournament"] == "FIFA World Cup"].copy()
    dates = pd.to_datetime(df["date"], errors="coerce")
    df["_date"] = dates
    df = df[dates.dt.year == 2026]
    df = df.dropna(subset=["home_score", "away_score"])

    # Shootouts are optional: a missing/absent file simply means drawn ties can
    # not be pinned (they are skipped), never an error.
    if shootouts_df is None:
        try:
            from wca.data.results import load_shootouts

            shootouts_df = load_shootouts("data/raw/shootouts.csv")
        except Exception:  # noqa: BLE001 - shootouts are a best-effort anchor.
            shootouts_df = None

    from wca.data.results import shootout_winner

    # Candidate rows from BOTH sources, CSV first (the two agree on the 90'
    # scoreline when both have the row; the JSON adds fresher rows the CSV
    # lacks, plus the ``outcome`` advancing-side marker for drawn ties).
    candidates: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        candidates.append(
            {
                "home": canonical(str(r["home_team"])),
                "away": canonical(str(r["away_team"])),
                "home_goals": int(r["home_score"]),
                "away_goals": int(r["away_score"]),
                "outcome": None,
                "when": r.get("_date"),
            }
        )
    candidates.extend(_played_wc2026_json_rows(results_json_path))

    out: Dict[FrozenSet[str], str] = {}
    for c in candidates:
        home, away = c["home"], c["away"]
        gh, ga = team_to_group.get(home), team_to_group.get(away)
        # A knockout tie is CROSS-group. Skip intra-group (group-stage) rows and
        # any team that does not resolve to a 2026 group.
        if gh is None or ga is None or gh == ga:
            continue
        pair = frozenset((home, away))
        if pair in out:
            continue  # already pinned from an earlier (resolved) candidate
        hg, ag = c["home_goals"], c["away_goals"]
        if hg > ag:
            winner = home
        elif ag > hg:
            winner = away
        else:
            # Drawn after 90' -> decided in ET/pens. Match on the pair and the
            # fixture date; fall back to the results-JSON advancing-side marker.
            winner = shootout_winner(shootouts_df, home, away, when=c.get("when"))
            if winner is not None:
                winner = canonical(str(winner))
            elif c.get("outcome") == "home":
                winner = home
            elif c.get("outcome") == "away":
                winner = away
            else:
                continue  # cannot pin — never invent a winner
        out[pair] = winner
    return out


def knockout_state_staleness(
    pinned: Optional[Mapping[FrozenSet[str], str]],
    groups: Optional[Dict[str, List[str]]] = None,
    results_path: Optional[str] = None,
    results_json_path: Optional[str] = None,
    now: Optional[pd.Timestamp] = None,
) -> Dict[str, str]:
    """Map team -> withheld-reason for teams whose KO state the sim has NOT settled.

    State-freshness gate (fix 2026-07-08). A knockout tie that has *kicked off*
    but is not pinned in the sim's conditioning set (``pinned``, from
    :func:`load_played_knockout_results` at sim time) means every advancement
    probability for BOTH participants is phantom: the sim re-simulates a tie
    reality has already decided (USA showed P(QF)=0.317 after its Jul-6
    elimination; Egypt P(R16)=0.4708 after winning its Jul-3 shootout). Such
    teams must be WITHHELD from actionable surfaces, not repriced.

    Kicked-off detection (no fabrication, evidence only):

    * a played row (numeric scoreline) in either the results CSV or the freshest
      ``wc2026_results.json`` feed — the fixture certainly kicked off;
    * a *scheduled* CSV row (NA scoreline) whose date is strictly before today
      (UTC) — the CSV carries the full KO bracket ahead of time, so a past-dated
      NA row is a played-but-not-yet-recorded tie (the 1-3 day CSV lag). Same-day
      NA rows are NOT flagged (date-only granularity: kickoff may still be
      ahead).

    ``pinned=None`` means the sim's conditioning set is UNKNOWN (e.g. a legacy
    cache without pin metadata) and is treated as empty — fail closed.

    Returns
    -------
    ``{team: reason}`` for every affected team (canonical names); empty when the
    sim's state is complete.
    """
    grp = groups if groups is not None else WC2026_GROUPS
    team_to_group = {t: g for g, ts in grp.items() for t in ts}
    pinned_pairs = set(pinned.keys()) if pinned else set()
    ts_now = (
        pd.Timestamp.now(tz="UTC").tz_localize(None)
        if now is None
        else pd.Timestamp(now)
    )

    if results_path is None:
        from wca.data.cleaning import resolve_results_path

        results_path = resolve_results_path()

    # Candidate (home, away, when, played) knockout fixtures with evidence of
    # having kicked off.
    kicked_off: List[Tuple[str, str, Optional[pd.Timestamp]]] = []

    try:
        df = pd.read_csv(results_path)
    except OSError:
        df = pd.DataFrame()
    if not df.empty and "tournament" in df.columns:
        df = df[df["tournament"] == "FIFA World Cup"].copy()
        dates = pd.to_datetime(df["date"], errors="coerce")
        df["_date"] = dates
        df = df[dates.dt.year == 2026]
        scores = pd.to_numeric(df["home_score"], errors="coerce")
        played = scores.notna() & pd.to_numeric(df["away_score"], errors="coerce").notna()
        for _, r in df.iterrows():
            when = r.get("_date")
            is_played = bool(played.loc[r.name])
            # NA-score rows: kicked off only when dated strictly before today.
            if not is_played and not (pd.notna(when) and when.date() < ts_now.date()):
                continue
            kicked_off.append(
                (canonical(str(r["home_team"])), canonical(str(r["away_team"])), when)
            )

    for c in _played_wc2026_json_rows(results_json_path):
        kicked_off.append((c["home"], c["away"], c.get("when")))

    out: Dict[str, str] = {}
    for home, away, when in kicked_off:
        gh, ga = team_to_group.get(home), team_to_group.get(away)
        if gh is None or ga is None or gh == ga:
            continue  # not a knockout tie between 2026 teams
        if frozenset((home, away)) in pinned_pairs:
            continue  # settled in the sim's conditioning set
        date_s = when.strftime("%Y-%m-%d") if pd.notna(when) else "date unknown"
        reason = (
            "state-stale: earlier-round tie unsettled in sim "
            "(%s vs %s, kicked off %s)" % (home, away, date_s)
        )
        out.setdefault(home, reason)
        out.setdefault(away, reason)
    return out


def run_advancement(
    models: FittedModels,
    n_sims: int = 20000,
    seed: int = 42,
    groups: Optional[Dict[str, List[str]]] = None,
    odds_df: Optional[pd.DataFrame] = None,
    weights: Optional[BlendWeights] = None,
    venue_aware: bool = False,
    results: Optional[Sequence[Result]] = None,
    ko_results: Optional[Mapping[FrozenSet[str], str]] = None,
) -> pd.DataFrame:
    """Simulate the tournament and return per-team stage probabilities.

    Parameters
    ----------
    models:
        Fitted Elo + Dixon-Coles models (from :func:`wca.card.fit_models`).
    n_sims:
        Number of Monte-Carlo tournaments.
    seed:
        RNG seed for reproducibility.
    groups:
        Group assignment; defaults to :data:`WC2026_GROUPS`.
    odds_df:
        Optional flat TheOddsAPI-style 1X2 odds frame. When supplied, fixtures
        with a complete de-vigged market consensus use the market-anchored
        blend; fixtures without one fall back to the Elo + Dixon-Coles blend.
    weights:
        Convex Elo/DC/market weights. Defaults to :class:`wca.card.BlendWeights`.
    results:
        Already-played group matches to fix (not re-simulate). Defaults to
        auto-loading them via :func:`load_played_group_results`; pass an empty
        list to force a from-scratch pre-tournament simulation.
    ko_results:
        Already-played *knockout* ties to fix, as
        ``{frozenset((team_a, team_b)): winner_name}`` (see
        :func:`load_played_knockout_results`). ``None`` (default) re-simulates
        every knockout round, which is only correct *before* the knockouts
        start; once R32 is played, pass the pinned ties so eliminated teams no
        longer show a survival probability.

    Returns
    -------
    pandas.DataFrame indexed by team with columns
    ``P(R32) P(R16) P(QF) P(SF) P(Final) P(win) P(group_winner)`` plus the
    team's ``group`` letter. One row per team, 48 rows.
    """
    grp = groups if groups is not None else WC2026_GROUPS
    if set(grp) != set(GROUP_LETTERS):
        raise ValueError("groups must contain exactly the 12 letters A-L")

    # Fix already-played group matches so the simulation is conditioned on the
    # actual results so far rather than replaying the whole tournament. Pass an
    # explicit empty list to force a pre-tournament (from-scratch) sim.
    if results is None:
        results = load_played_group_results(grp)

    prob_fn = make_prob_fn(
        models, odds_df=odds_df, weights=weights, venue_aware=venue_aware
    )
    sim = TournamentSimulator(
        grp, prob_fn, results=results, fixed_knockouts=ko_results
    )
    res = sim.simulate(n_sims=n_sims, rng_seed=seed)

    team_to_group = {t: g for g, ts in grp.items() for t in ts}

    rows: List[Dict[str, Any]] = []
    for team in res.teams:
        gp = res.group_position[team]  # [P1st, P2nd, P3rd, P4th]
        rows.append(
            {
                "team": team,
                "group": team_to_group[team],
                "P(R32)": float(res.reach["R32"].get(team, 0.0)),
                "P(R16)": float(res.reach["R16"].get(team, 0.0)),
                "P(QF)": float(res.reach["QF"].get(team, 0.0)),
                "P(SF)": float(res.reach["SF"].get(team, 0.0)),
                "P(Final)": float(res.reach["F"].get(team, 0.0)),
                "P(win)": float(res.win.get(team, 0.0)),
                "P(group_winner)": float(gp[0]),
            }
        )
    df = pd.DataFrame(rows).set_index("team")
    return df


# ---------------------------------------------------------------------------
# Polymarket comparison.
# ---------------------------------------------------------------------------

# sim DataFrame column for each stage label.
_STAGE_COL: Dict[str, str] = {
    "R32": "P(R32)",
    "R16": "P(R16)",
    "QF": "P(QF)",
    "SF": "P(SF)",
    "F": "P(Final)",
    "win": "P(win)",
    "GW": "P(group_winner)",
}


def pm_taker_fee(price: float) -> float:
    """Polymarket sports taker fee *per share* at a fill price ``price``.

    ``fee = 0.03 * price * (1 - price)`` (maker fee is zero). This is charged on
    the winnings side; we fold it into the edge as a haircut per dollar at risk.
    """
    p = float(price)
    return PM_TAKER_FEE_COEF * p * (1.0 - p)


def _yes_mid(market: Dict[str, Any]) -> Optional[float]:
    """Best estimate of the YES fair price for a Polymarket binary market.

    Prefers the mid of ``bestBid``/``bestAsk`` when both are present and sane;
    falls back to the ``priceMap['Yes']`` (last/AMM price) or the first
    ``outcomePrices`` entry. Returns ``None`` if nothing usable is found.
    """
    bid = market.get("bestBid")
    ask = market.get("bestAsk")
    try:
        b = float(bid) if bid is not None else None
        a = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        b = a = None
    if b is not None and a is not None and 0.0 < b <= a < 1.0:
        return 0.5 * (b + a)
    pm = market.get("priceMap") or {}
    y = pm.get("Yes")
    try:
        if y is not None:
            yv = float(y)
            if 0.0 < yv < 1.0:
                return yv
    except (TypeError, ValueError):
        pass
    return None


def _yes_ask(market: Dict[str, Any], mid: float) -> float:
    """Effective YES *buy* price (what you pay to take YES)."""
    ask = market.get("bestAsk")
    try:
        a = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        a = None
    if a is not None and 0.0 < a < 1.0:
        return a
    return mid


def _no_ask(market: Dict[str, Any], yes_mid: float) -> float:
    """Effective NO *buy* price (what you pay to take NO).

    NO ask = 1 - YES bid. When the YES bid is unavailable we approximate the NO
    ask as ``1 - yes_mid`` (i.e. the symmetric mid), which is a slightly
    optimistic stand-in flagged in the report's caveats.
    """
    bid = market.get("bestBid")
    try:
        b = float(bid) if bid is not None else None
    except (TypeError, ValueError):
        b = None
    if b is not None and 0.0 < b < 1.0:
        return 1.0 - b
    return 1.0 - yes_mid


@dataclass
class AdvancementEdge:
    """One side (YES or NO) of one team-stage market vs the simulation."""

    team: str
    group: str
    stage: str
    stage_label: str
    market_title: str
    side: str  # "YES" or "NO"
    sim_prob: float  # simulated probability the SIDE pays out
    pm_price: float  # price paid for the SIDE (buy price)
    pm_yes_mid: float  # the YES mid, for reference
    fee: float  # per-share taker fee at the buy price
    raw_edge: float  # sim_prob - pm_price
    fee_adj_edge: float  # sim_prob - pm_price - fee (per $ at risk)
    fee_adj_ev_per_dollar: float  # same as fee_adj_edge here (binary $1 payout)
    stake: float  # quarter-Kelly stake on the PM pool, capped

    @property
    def edge_pct(self) -> float:
        return self.fee_adj_edge * 100.0

    @property
    def bucket(self) -> str:
        """Model-prob bucket of the SIDE (wca.selection): moneyline/mid/longshot."""
        return prob_bucket(self.sim_prob)

    @property
    def no_cash(self) -> bool:
        """True when the SIDE is a <25c longshot — free-bet/lottery only, no cash."""
        return longshot_no_cash(self.sim_prob)


def _fee_adjusted_kelly_stake(
    sim_prob: float,
    price: float,
    fee: float,
    bankroll: float = PM_POOL_BANKROLL,
    fraction: float = PM_KELLY_FRACTION,
    cap: float = PM_PER_BET_CAP,
) -> float:
    """Quarter-Kelly stake for a binary Polymarket position, fee-aware.

    A Polymarket binary pays $1 per share if the side resolves YES. Buying at
    ``price`` with taker ``fee`` per share is equivalent to a fixed-odds bet
    with net win ``(1 - price - fee)`` and loss ``(price + fee)`` per share, so
    the effective decimal odds are ``1 / (price + fee)``. We size with the
    project's standard fractional-Kelly + cap on those effective odds at the
    *fee-adjusted* win probability.

    Canonical cash floor (:func:`wca.selection.longshot_no_cash`; user
    2026-07-07): a side the model rates ``< 0.25`` is a longshot — NO cash,
    free-bet / lottery only — so the stake is forced to 0.0 here regardless of
    the fee-adjusted edge.
    """
    if longshot_no_cash(sim_prob):
        # <25c model prob: free-bet/lottery only, never cash — stake 0.
        return 0.0
    cost = float(price) + float(fee)
    if cost <= 0.0 or cost >= 1.0:
        return 0.0
    decimal_odds = 1.0 / cost
    # Win probability already nets the fee out of the payout; use sim_prob.
    return kelly_mod.stake(
        float(sim_prob), decimal_odds, bankroll, fraction=fraction, cap=cap
    )


def _team_markets(event: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Yield ``(canonical_team, market)`` for every team market in an event.

    Skips non-team noise markets ("Other", "Team AM", "Country E", placeholder
    nations not in the tournament) by requiring the canonical name to be one of
    the 48 entered teams.
    """
    entered = {t for ts in WC2026_GROUPS.values() for t in ts}
    out: List[Tuple[str, Dict[str, Any]]] = []
    for m in event.get("markets") or []:
        raw = m.get("groupItemTitle")
        if not raw:
            continue
        team = canonical(str(raw))
        if team in entered:
            out.append((team, m))
    return out


def _group_winner_event_letter(title: str) -> Optional[str]:
    """Extract the group letter from a 'World Cup Group X Winner' title."""
    t = (title or "").strip()
    low = t.lower()
    if "group" not in low or "winner" not in low:
        return None
    # Pattern: "World Cup Group <X> Winner"
    tokens = t.replace("Group", "Group ").split()
    for i, tok in enumerate(tokens):
        if tok.lower() == "group" and i + 1 < len(tokens):
            cand = tokens[i + 1].strip().upper()
            if len(cand) == 1 and cand in GROUP_LETTERS:
                return cand
    return None


def compare_to_polymarket(
    sim_df: pd.DataFrame,
    pm_events: Sequence[Dict[str, Any]],
    bankroll: float = PM_POOL_BANKROLL,
    fraction: float = PM_KELLY_FRACTION,
    cap: float = PM_PER_BET_CAP,
) -> pd.DataFrame:
    """Match Polymarket advancement / group-winner markets to the simulation.

    For every team market in every recognised event this computes BOTH a YES and
    a NO position, their fee-adjusted edges and a quarter-Kelly stake, and
    returns the row for whichever side has the larger fee-adjusted edge (so each
    team-stage market contributes at most one actionable row, in the direction
    the simulation favours).

    Parameters
    ----------
    sim_df:
        Output of :func:`run_advancement` (indexed by team).
    pm_events:
        Polymarket event dicts (from
        :func:`wca.data.polymarket.find_world_cup_markets`).
    bankroll, fraction, cap:
        Polymarket pool sizing parameters.

    Returns
    -------
    pandas.DataFrame, one row per matched team-stage market, sorted by
    fee-adjusted edge descending. Columns mirror :class:`AdvancementEdge`.
    """
    edges: List[AdvancementEdge] = []

    for event in pm_events:
        title = str(event.get("title") or "").strip()
        stage = PM_STAGE_EVENTS.get(title)
        group_letter = None
        if stage is None:
            group_letter = _group_winner_event_letter(title)
            if group_letter is None:
                continue
            stage = "GW"

        col = _STAGE_COL[stage]
        for team, market in _team_markets(event):
            if team not in sim_df.index:
                continue
            # For group-winner markets only credit teams actually in that group.
            if stage == "GW":
                if str(sim_df.loc[team, "group"]) != group_letter:
                    continue
            sim_p = float(sim_df.loc[team, col])
            yes_mid = _yes_mid(market)
            if yes_mid is None:
                continue
            # Resolved/decided markets (fix 2026-07-08): a YES quote pinned at
            # ≥0.98 or ≤0.02 means the market has effectively settled (the
            # team's tie is decided). There is no tradable edge — only a
            # phantom sim-vs-0.99 disagreement when the sim's conditioning
            # lags reality — so the row is dropped, never sized.
            if yes_mid >= PM_RESOLVED_HI or yes_mid <= PM_RESOLVED_LO:
                continue

            yes_buy = _yes_ask(market, yes_mid)
            no_buy = _no_ask(market, yes_mid)

            # YES side: pays out with prob sim_p.
            yes_fee = pm_taker_fee(yes_buy)
            yes_edge = sim_p - yes_buy - yes_fee
            yes_stake = _fee_adjusted_kelly_stake(
                sim_p, yes_buy, yes_fee, bankroll, fraction, cap
            )

            # NO side: pays out with prob (1 - sim_p).
            no_fee = pm_taker_fee(no_buy)
            no_edge = (1.0 - sim_p) - no_buy - no_fee
            no_stake = _fee_adjusted_kelly_stake(
                1.0 - sim_p, no_buy, no_fee, bankroll, fraction, cap
            )

            label = STAGE_LABEL[stage]
            if stage == "GW":
                label = "Win Group %s" % group_letter

            if yes_edge >= no_edge:
                side, side_p, side_price, side_fee, side_edge, side_stake = (
                    "YES", sim_p, yes_buy, yes_fee, yes_edge, yes_stake,
                )
            else:
                side, side_p, side_price, side_fee, side_edge, side_stake = (
                    "NO", 1.0 - sim_p, no_buy, no_fee, no_edge, no_stake,
                )

            edges.append(
                AdvancementEdge(
                    team=team,
                    group=str(sim_df.loc[team, "group"]),
                    stage=stage,
                    stage_label=label,
                    market_title=title,
                    side=side,
                    sim_prob=side_p,
                    pm_price=side_price,
                    pm_yes_mid=yes_mid,
                    fee=side_fee,
                    raw_edge=side_p - side_price,
                    fee_adj_edge=side_edge,
                    fee_adj_ev_per_dollar=side_edge,
                    stake=side_stake,
                )
            )

    rows = [
        {
            "team": e.team,
            "group": e.group,
            "stage": e.stage,
            "stage_label": e.stage_label,
            "market_title": e.market_title,
            "side": e.side,
            "sim_prob": e.sim_prob,
            "pm_price": e.pm_price,
            "pm_yes_mid": e.pm_yes_mid,
            "fee": e.fee,
            "raw_edge": e.raw_edge,
            "fee_adj_edge": e.fee_adj_edge,
            "fee_adj_ev_per_dollar": e.fee_adj_ev_per_dollar,
            "stake": e.stake,
            # Canonical selection tags (wca.selection): the SIDE's model-prob
            # bucket and the <25c no-cash flag, so downstream (site tables,
            # adv_edge_matrix.js) can group / grey by bucket without recomputing.
            "bucket": e.bucket,
            "no_cash": e.no_cash,
        }
        for e in edges
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        # Canonical desk ordering (wca.selection; user 2026-07-07):
        #   1. model-prob bucket (moneyline > mid > longshot), ALWAYS;
        #   2. further-out first — deeper knockout stage (stage_further_out desc);
        #   3. fee-adjusted edge breaks ties within a bucket + stage tier.
        df["_bucket_rank"] = df["sim_prob"].map(bucket_rank)
        df["_stage_out"] = df["stage"].map(stage_further_out)
        df = df.sort_values(
            by=["_bucket_rank", "_stage_out", "fee_adj_edge"],
            ascending=[True, False, False],
        ).drop(columns=["_bucket_rank", "_stage_out"]).reset_index(drop=True)
    return df
