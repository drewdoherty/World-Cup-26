"""Historical match-event data pipeline (corners / SoT / fouls / cards / shots).

This module is the *additive* loader that unifies two free historical sources
into one match-event table, then derives empirical-Bayes per-team priors for
the match-event prop models (:mod:`wca.models.props`).

Sources & attribution
----------------------
1. **football-data.co.uk** (compiled by Joseph Buchdahl).  Club-league CSVs at
   ``https://www.football-data.co.uk/mmz4281/<season>/<code>.csv`` carry the
   shot/corner/foul/card columns at scale (~50k+ rows once enough seasons are
   pulled).  Free for personal/non-commercial use; attribution requested.
   Mirrors (used as fallback when the main host is blocked):
   ``footballcsv`` (github.com/footballcsv) and ``jokecamp/FootballData``.
2. **StatsBomb open data** (StatsBomb, free for research with attribution).
   International tournaments (World Cup 2018+2022 cached on disk) — the only
   source with xG and an *international* context.  Parsed by
   :mod:`wca.data.statsbomb`; this module reuses it rather than re-parsing.

football-data.co.uk column map (one CSV row per match)
------------------------------------------------------
``FTHG``/``FTAG`` -> goals          ``HS``/``AS``   -> shots
``HST``/``AST``   -> shots_on_target ``HC``/``AC``   -> corners
``HF``/``AF``     -> fouls           ``HY``/``AY``   -> yellows
``HR``/``AR``     -> reds            ``Div``         -> competition
``Date``          -> date (parsed ``%d/%m/%y`` then ``%d/%m/%Y``)
``HomeTeam``/``AwayTeam`` -> team / opponent

Missing fields are mapped to **NaN, never 0** — a missing column must not
masquerade as a real zero count (that would corrupt every variance / EB prior).
football-data club CSVs carry no possession or xg, so those columns are NaN.

StatsBomb shots-on-target derivation (FROZEN convention)
--------------------------------------------------------
A Shot event is "on target" iff its outcome is in
``{"Goal", "Saved", "Saved To Post"}`` (see
:data:`wca.data.statsbomb.SOT_OUTCOMES`).  ``Blocked``, ``Post`` (woodwork, no
save), ``Off T`` and ``Wayward`` are NOT on target.  This mapping is frozen and
must match ``statsbomb.py``.

Unified schema (TWO rows per match — one per team)
--------------------------------------------------
``match_id, source, competition, season, date, team, opponent, is_home,
neutral, goals, shots, shots_on_target, corners, fouls, yellows, reds,
possession, xg``

Card convention: ``yellows`` and ``reds`` are stored directly; a second-yellow
sending-off is already ONE red in both sources (football-data's ``HR`` and the
StatsBomb parser).  ``card_points = yellows + 2*reds`` is derived downstream.

Backward-compatibility contract
-------------------------------
:func:`load_priors` returns the *hard-coded* model defaults (the same constants
baked into :mod:`wca.models.props`) whenever ``prop_priors.csv`` is absent or
malformed, so a fresh checkout / the live card never breaks.  ``prop_priors.csv``
lives under ``data/processed/`` which is gitignored — it is a local artifact.
"""
from __future__ import annotations

import io
import logging
import math
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .teamnames import canonical

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & source registry
# ---------------------------------------------------------------------------

FOOTBALL_DATA_BASE = "https://www.football-data.co.uk/mmz4281"

# League division codes that carry the HS/HST/HC/HF/HY/HR stat columns.  The
# brief's set; "extra-league" country files (single-file leagues) lack the
# stat columns and are intentionally excluded.
FOOTBALL_DATA_CODES = (
    "E0", "E1", "E2", "E3", "EC",
    "SC0", "SC1", "SC2", "SC3",
    "D1", "D2", "I1", "I2", "SP1", "SP2",
    "F1", "F2", "N1", "B1", "P1", "T1", "G1",
)

# Mirror hosts tried (in order) when the main football-data.co.uk host fails.
FOOTBALL_DATA_MIRRORS = (
    "https://www.football-data.co.uk/mmz4281",
    # footballcsv cache mirror (season/code layout differs; kept for docs):
    "https://raw.githubusercontent.com/footballcsv/cache.footballdata/master/mmz4281",
)

# football-data column -> (unified_field, side)
_FD_COLMAP = {
    "FTHG": ("goals", "home"), "FTAG": ("goals", "away"),
    "HS": ("shots", "home"), "AS": ("shots", "away"),
    "HST": ("shots_on_target", "home"), "AST": ("shots_on_target", "away"),
    "HC": ("corners", "home"), "AC": ("corners", "away"),
    "HF": ("fouls", "home"), "AF": ("fouls", "away"),
    "HY": ("yellows", "home"), "AY": ("yellows", "away"),
    "HR": ("reds", "home"), "AR": ("reds", "away"),
}

# Unified per-match schema (wide; one row per match before the team-row split).
_MATCH_FIELDS = (
    "goals", "shots", "shots_on_target", "corners", "fouls",
    "yellows", "reds", "possession", "xg",
)

UNIFIED_COLUMNS = (
    "match_id", "source", "competition", "season", "date",
    "team", "opponent", "is_home", "neutral",
    "goals", "shots", "shots_on_target", "corners", "fouls",
    "yellows", "reds", "possession", "xg",
)

# ---------------------------------------------------------------------------
# Hard-coded model fallbacks (must mirror wca.models.props defaults).  These
# are the *method-of-moments* fits on the 128-match StatsBomb WC18+22 sample;
# they are what every model uses when prop_priors.csv is absent.
# ---------------------------------------------------------------------------

# Per-MATCH (total) global baselines: (mean, dispersion_k).
GLOBAL_FALLBACK = {
    "corners": {"mean": 8.97, "dispersion_k": 157.5},
    "sot": {"mean": 8.32, "dispersion_k": 11.0},
    "fouls": {"mean": 28.523, "dispersion_k": 32.3},
    "shots": {"mean": 25.0, "dispersion_k": 22.9},
    "cards": {"mean": 3.41, "dispersion_k": 6.9},
    "yellows": {"mean": 3.352, "dispersion_k": 7.5},
    "reds": {"mean": 0.062, "dispersion_k": 1e6},
}

# Per-TEAM league means (one team's count per match), used as the EB shrinkage
# target when a team has few/zero matches.
LEAGUE_TEAM_FALLBACK = {
    "corners": 4.484,
    "sot": 4.16,
    "fouls": 14.262,
    "shots": 12.5,
    "cards": 1.707,
    "yellows": 1.676,
    "reds": 0.031,
}

# Default EB shrinkage strength (in "pseudo-matches").  A team with eb_tau
# matches is shrunk 50% toward the league mean.
DEFAULT_EB_TAU = 4.0

# Markets emitted as per-team priors in prop_priors.csv.
PRIOR_MARKETS = ("corners", "sot", "fouls", "cards", "yellows", "reds")

DEFAULT_PRIORS_PATH = "data/processed/prop_priors.csv"


# ---------------------------------------------------------------------------
# football-data.co.uk loader
# ---------------------------------------------------------------------------

def _parse_fd_date(series: pd.Series) -> pd.Series:
    """Parse football-data dates (``%d/%m/%y`` then ``%d/%m/%Y``)."""
    out = pd.to_datetime(series, format="%d/%m/%y", errors="coerce")
    mask = out.isna()
    if mask.any():
        alt = pd.to_datetime(series[mask], format="%d/%m/%Y", errors="coerce")
        out.loc[mask] = alt
    return out


def parse_football_data_csv(text_or_buffer, season: Optional[str] = None) -> pd.DataFrame:
    """Parse one football-data.co.uk CSV into the wide unified match schema.

    Maps the football-data columns to unified fields; any stat column absent
    from the CSV is filled with **NaN** (never 0).  ``possession`` and ``xg``
    are always NaN (football-data carries neither).  Team names are passed
    through :func:`wca.data.teamnames.canonical`.

    Returns one row per match with ``home``/``away`` split done later by
    :func:`to_team_rows`.
    """
    if isinstance(text_or_buffer, (str, bytes)):
        buf = io.StringIO(text_or_buffer.decode() if isinstance(text_or_buffer, bytes)
                          else text_or_buffer)
    else:
        buf = text_or_buffer
    raw = pd.read_csv(buf, encoding="latin-1", on_bad_lines="skip")
    # Drop fully-empty trailing rows football-data sometimes appends.
    raw = raw.dropna(how="all")
    if "HomeTeam" not in raw.columns or "AwayTeam" not in raw.columns:
        return pd.DataFrame()
    raw = raw[raw["HomeTeam"].notna() & raw["AwayTeam"].notna()].copy()

    out = pd.DataFrame()
    out["competition"] = raw.get("Div")
    out["season"] = season
    out["date"] = _parse_fd_date(raw["Date"]) if "Date" in raw.columns else pd.NaT
    out["home"] = raw["HomeTeam"].astype(str).map(canonical)
    out["away"] = raw["AwayTeam"].astype(str).map(canonical)

    for field in _MATCH_FIELDS:
        out[field + "_home"] = np.nan
        out[field + "_away"] = np.nan
    for col, (field, side) in _FD_COLMAP.items():
        if col in raw.columns:
            out[field + "_" + side] = pd.to_numeric(raw[col], errors="coerce")
    # possession / xg are unavailable in football-data -> stay NaN.

    out["source"] = "football-data"
    out["neutral"] = False
    # match_id: stable per row from competition/season/teams/date.
    out = out.reset_index(drop=True)
    out["match_id"] = [
        "fd:%s:%s:%s:%s" % (
            r.competition, r.season,
            (r.date.strftime("%Y%m%d") if pd.notna(r.date) else "NA"),
            ("%s_v_%s" % (r.home, r.away)),
        )
        for r in out.itertuples()
    ]
    return out


def fetch_football_data(codes=FOOTBALL_DATA_CODES, seasons=("2223", "2324"),
                        cache_dir=None, session=None) -> pd.DataFrame:
    """Download & parse football-data.co.uk CSVs into the wide match schema.

    Network is attempted against :data:`FOOTBALL_DATA_MIRRORS` in order; a
    code/season that 404s or errors on every mirror is skipped (logged).  If
    ``cache_dir`` is given, raw CSVs are cached there and re-used.

    This performs network IO and is **not** exercised by the unit tests (which
    parse fixtures directly via :func:`parse_football_data_csv`).
    """
    import requests  # local import: keeps the module import network-free

    sess = session or requests.Session()
    frames: List[pd.DataFrame] = []
    cache = Path(cache_dir) if cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    for season in seasons:
        for code in codes:
            text = None
            if cache and (cache / ("%s_%s.csv" % (code, season))).exists():
                text = (cache / ("%s_%s.csv" % (code, season))).read_text(
                    encoding="latin-1")
            else:
                for base in FOOTBALL_DATA_MIRRORS:
                    url = "%s/%s/%s.csv" % (base, season, code)
                    try:
                        resp = sess.get(url, timeout=60)
                    except Exception as exc:  # pragma: no cover - network
                        logger.warning("fetch error %s: %s", url, exc)
                        continue
                    if resp.status_code == 200 and resp.content:
                        text = resp.content.decode("latin-1")
                        if cache:
                            (cache / ("%s_%s.csv" % (code, season))).write_text(
                                text, encoding="latin-1")
                        break
                if text is None:
                    logger.info("no data for %s/%s on any mirror", season, code)
                    continue
            try:
                df = parse_football_data_csv(text, season=season)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("parse failed for %s/%s: %s", season, code, exc)
                continue
            if len(df):
                frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# StatsBomb internationals (reuse statsbomb.py; normalise to wide schema)
# ---------------------------------------------------------------------------

def statsbomb_wide(matches_csv: Optional[str] = None,
                   matches_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Normalise StatsBomb props rows into the wide unified match schema.

    Accepts either a path to a ``props_matches.csv`` (as produced by
    :func:`wca.data.statsbomb.build_props_dataset`) or a pre-loaded DataFrame.
    SoT (``sot_home``/``sot_away``) is carried through if present; older caches
    that lack it leave ``shots_on_target`` NaN.

    Team names go through :func:`canonical`.  ``possession`` is NaN (not in the
    cached props csv); ``xg`` is carried through.  Internationals are flagged
    ``neutral=True`` (World Cup hosts aside, treated as neutral context for the
    intl-vs-domestic adjustment).
    """
    if matches_df is None:
        if matches_csv is None:
            raise ValueError("provide matches_csv or matches_df")
        matches_df = pd.read_csv(matches_csv)
    sb = matches_df

    out = pd.DataFrame()
    out["competition"] = "WC"
    out["season"] = sb.get("season")
    out["date"] = pd.to_datetime(sb.get("date"), errors="coerce")
    out["home"] = sb["home"].astype(str).map(canonical)
    out["away"] = sb["away"].astype(str).map(canonical)

    # statsbomb field -> unified field
    sb_map = {
        "goals": "goals", "shots": "shots", "sot": "shots_on_target",
        "corners": "corners", "fouls": "fouls", "yellows": "yellows",
        "reds": "reds", "xg": "xg",
    }
    for field in _MATCH_FIELDS:
        out[field + "_home"] = np.nan
        out[field + "_away"] = np.nan
    for sbf, field in sb_map.items():
        for side in ("home", "away"):
            col = sbf + "_" + side
            if col in sb.columns:
                out[field + "_" + side] = pd.to_numeric(sb[col], errors="coerce")
    # possession not in cached props csv -> NaN.

    out["source"] = "statsbomb"
    out["neutral"] = True
    out = out.reset_index(drop=True)
    if "match_id" in sb.columns:
        out["match_id"] = ["sb:%s" % m for m in sb["match_id"]]
    else:
        out["match_id"] = ["sb:%d" % i for i in range(len(out))]
    return out


# ---------------------------------------------------------------------------
# Wide -> two-team-rows
# ---------------------------------------------------------------------------

def to_team_rows(wide: pd.DataFrame) -> pd.DataFrame:
    """Explode a wide (one-row-per-match) frame into TWO rows per match.

    Each match yields a home row (``is_home=True``) and an away row
    (``is_home=False``) with ``team``/``opponent`` set and the per-side stat
    columns renamed to the unified field names.  NaNs are preserved (no
    zero-filling).
    """
    if wide is None or len(wide) == 0:
        return pd.DataFrame(columns=list(UNIFIED_COLUMNS))

    def _side_rows(side: str, is_home: bool) -> pd.DataFrame:
        other = "away" if side == "home" else "home"
        df = pd.DataFrame()
        df["match_id"] = wide["match_id"]
        df["source"] = wide["source"]
        df["competition"] = wide.get("competition")
        df["season"] = wide.get("season")
        df["date"] = wide.get("date")
        df["team"] = wide[side] if side in wide else wide[side + "_team"]
        df["opponent"] = wide[other] if other in wide else wide[other + "_team"]
        df["is_home"] = is_home
        df["neutral"] = wide.get("neutral", False)
        for field in _MATCH_FIELDS:
            df[field] = wide.get(field + "_" + side, np.nan)
        return df

    home = _side_rows("home", True)
    away = _side_rows("away", False)
    out = pd.concat([home, away], ignore_index=True)
    return out[list(UNIFIED_COLUMNS)]


def load_matchevents(football_data: Optional[pd.DataFrame] = None,
                     statsbomb: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Combine football-data + StatsBomb wide frames into the team-row table.

    Either input may be None.  Returns the unified two-rows-per-match table.
    """
    frames = []
    if football_data is not None and len(football_data):
        frames.append(to_team_rows(football_data))
    if statsbomb is not None and len(statsbomb):
        frames.append(to_team_rows(statsbomb))
    if not frames:
        return pd.DataFrame(columns=list(UNIFIED_COLUMNS))
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Baselines, intl-vs-domestic adjustment, empirical-Bayes priors
# ---------------------------------------------------------------------------

# Map a prior-market name to the team-row column it aggregates.
_MARKET_COLUMN = {
    "corners": "corners",
    "sot": "shots_on_target",
    "fouls": "fouls",
    "cards": None,          # derived: yellows + reds
    "yellows": "yellows",
    "reds": "reds",
    "shots": "shots",
}


def _market_series(rows: pd.DataFrame, market: str) -> pd.Series:
    """Per-team-row value for a market (cards = yellows + reds)."""
    if market == "cards":
        return rows["yellows"].astype(float) + rows["reds"].astype(float)
    return rows[_MARKET_COLUMN[market]].astype(float)


def _mom_dispersion(mean: float, var: float) -> float:
    """Method-of-moments NB dispersion k from mean & variance.

    Var = mu + mu^2/k  =>  k = mu^2 / (var - mu).  Near/under-Poisson
    (var <= mean) returns a large k (effectively Poisson).
    """
    if mean <= 0:
        return 1e6
    excess = var - mean
    if excess <= 1e-9:
        return 1e6
    return float(mean * mean / excess)


def team_baselines(rows: pd.DataFrame, market: str) -> pd.DataFrame:
    """Per-team mean / count for one market from the team-row table.

    Returns a DataFrame indexed by team with columns ``mean`` and ``n``
    (matches with a non-null value — NaNs are dropped, never zero-filled).
    """
    s = _market_series(rows, market)
    df = pd.DataFrame({"team": rows["team"], "val": s}).dropna(subset=["val"])
    g = df.groupby("team")["val"]
    return pd.DataFrame({"mean": g.mean(), "n": g.count()})


def global_baseline(rows: pd.DataFrame, market: str) -> Dict[str, float]:
    """Global per-team (one team's count) mean / var / k / n for a market."""
    s = _market_series(rows, market).dropna()
    if len(s) == 0:
        return {"mean": float("nan"), "var": float("nan"),
                "dispersion_k": float("nan"), "n": 0}
    mean = float(s.mean())
    var = float(s.var(ddof=1)) if len(s) > 1 else 0.0
    return {"mean": mean, "var": var,
            "dispersion_k": _mom_dispersion(mean, var), "n": int(len(s))}


def intl_domestic_adjustment(rows: pd.DataFrame, market: str) -> float:
    """International ÷ domestic per-team mean ratio for a market.

    Compares StatsBomb (international) team-rows to football-data (domestic).
    Returns 1.0 if either side is empty (no adjustment, back-compat safe).
    """
    s = _market_series(rows, market)
    df = pd.DataFrame({"source": rows["source"], "val": s}).dropna(subset=["val"])
    intl = df.loc[df["source"] == "statsbomb", "val"]
    dom = df.loc[df["source"] == "football-data", "val"]
    if len(intl) == 0 or len(dom) == 0 or dom.mean() == 0:
        return 1.0
    return float(intl.mean() / dom.mean())


def empirical_bayes_priors(rows: pd.DataFrame, market: str,
                           eb_tau: float = DEFAULT_EB_TAU) -> pd.DataFrame:
    """Empirical-Bayes shrunk per-team prior means for a market.

    ``prior_team = (n*rate_team + tau*league_mean) / (n + tau)`` — a team with
    ``eb_tau`` matches is pulled 50% toward the league mean; a team with zero
    matches returns the league mean exactly.  ``shrinkage_weight = n/(n+tau)``
    is the weight on the team's own data.
    """
    gb = global_baseline(rows, market)
    league_mean = gb["mean"]
    if not (league_mean == league_mean):  # NaN -> use fallback table
        league_mean = LEAGUE_TEAM_FALLBACK.get(market, 1.0)
    tb = team_baselines(rows, market)
    out_rows = []
    for team, rec in tb.iterrows():
        n = float(rec["n"])
        rate = float(rec["mean"])
        shrunk = (n * rate + eb_tau * league_mean) / (n + eb_tau)
        out_rows.append({
            "entity": team,
            "market": market,
            "mean": shrunk,
            "dispersion_k": gb["dispersion_k"],
            "n_matches": int(n),
            "shrinkage_weight": n / (n + eb_tau),
        })
    return pd.DataFrame(out_rows)


def build_prop_priors(rows: pd.DataFrame, eb_tau: float = DEFAULT_EB_TAU,
                      markets=PRIOR_MARKETS) -> pd.DataFrame:
    """Build the full prop_priors table: GLOBAL baselines + per-team EB priors.

    Schema: ``entity (GLOBAL|<team>), market, mean, dispersion_k, n_matches,
    shrinkage_weight``.  GLOBAL rows carry the per-team league mean and the
    method-of-moments dispersion; per-team rows carry EB-shrunk means.

    Every figure traces to ``rows`` (a real fetched table) — no placeholders.
    """
    out_frames = []
    for market in markets:
        gb = global_baseline(rows, market)
        out_frames.append(pd.DataFrame([{
            "entity": "GLOBAL",
            "market": market,
            "mean": gb["mean"],
            "dispersion_k": gb["dispersion_k"],
            "n_matches": gb["n"],
            "shrinkage_weight": 1.0,
        }]))
        eb = empirical_bayes_priors(rows, market, eb_tau=eb_tau)
        if len(eb):
            out_frames.append(eb)
    out = pd.concat(out_frames, ignore_index=True)
    return out


def write_prop_priors(rows: pd.DataFrame, path: str = DEFAULT_PRIORS_PATH,
                      eb_tau: float = DEFAULT_EB_TAU) -> pd.DataFrame:
    """Build and write prop_priors.csv; returns the table."""
    table = build_prop_priors(rows, eb_tau=eb_tau)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(p, index=False)
    return table


# ---------------------------------------------------------------------------
# Loader with hard fallback (backward-compatibility contract)
# ---------------------------------------------------------------------------

def _fallback_priors() -> Dict[str, dict]:
    """The hard-coded defaults, in the same nested shape as load_priors()."""
    out = {"GLOBAL": {}}
    for market in PRIOR_MARKETS:
        gmean = (GLOBAL_FALLBACK.get(market) or {}).get(
            "mean", LEAGUE_TEAM_FALLBACK.get(market))
        gk = (GLOBAL_FALLBACK.get(market) or {}).get("dispersion_k", 1e6)
        out["GLOBAL"][market] = {
            "mean": LEAGUE_TEAM_FALLBACK.get(market, gmean),
            "dispersion_k": gk,
            "n_matches": 0,
            "shrinkage_weight": 0.0,
        }
    return out


def load_priors(path: str = DEFAULT_PRIORS_PATH) -> Dict[str, dict]:
    """Load prop_priors.csv into a nested dict, FALLING BACK to hard-coded
    defaults when the file is missing or malformed.

    Returns ``{entity: {market: {mean, dispersion_k, n_matches,
    shrinkage_weight}}}`` with a ``"GLOBAL"`` entity always present.  A fresh
    checkout (no prop_priors.csv, which is gitignored) gets the
    :data:`LEAGUE_TEAM_FALLBACK` / :data:`GLOBAL_FALLBACK` constants, so the
    live card never breaks on a missing artifact.
    """
    p = Path(path)
    if not p.exists():
        return _fallback_priors()
    try:
        df = pd.read_csv(p)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("malformed prop_priors at %s: %s; using fallback", p, exc)
        return _fallback_priors()
    required = {"entity", "market", "mean", "dispersion_k"}
    if not required.issubset(df.columns) or len(df) == 0:
        logger.warning("prop_priors at %s missing columns; using fallback", p)
        return _fallback_priors()

    out: Dict[str, dict] = {}
    for r in df.itertuples():
        ent = str(r.entity)
        out.setdefault(ent, {})[str(r.market)] = {
            "mean": float(r.mean),
            "dispersion_k": float(r.dispersion_k),
            "n_matches": int(getattr(r, "n_matches", 0) or 0),
            "shrinkage_weight": float(getattr(r, "shrinkage_weight", 0.0) or 0.0),
        }
    if "GLOBAL" not in out:
        out["GLOBAL"] = _fallback_priors()["GLOBAL"]
    return out


def team_prior(priors: Dict[str, dict], team: str, market: str) -> float:
    """Look up a team's EB mean for a market, falling back GLOBAL -> hard-coded.

    Team name is canonicalised first.  Never raises; always returns a float.
    """
    tname = canonical(team) if team else team
    ent = priors.get(tname)
    if ent and market in ent:
        return float(ent[market]["mean"])
    glob = priors.get("GLOBAL", {})
    if market in glob:
        return float(glob[market]["mean"])
    return float(LEAGUE_TEAM_FALLBACK.get(market, float("nan")))
