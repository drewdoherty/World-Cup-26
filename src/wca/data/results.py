"""Download and load the martj42 international football results dataset.

Reference dataset: https://github.com/martj42/international_results
CSV fields: date, home_team, away_team, home_score, away_score,
            tournament, city, country, neutral
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import requests

_DEFAULT_URL = (
    "https://raw.githubusercontent.com/martj42/international_results"
    "/master/results.csv"
)
_DEFAULT_DEST = "data/raw/results.csv"
# Penalty-shootout outcomes live in a SEPARATE martj42 file — results.csv only
# carries the 90-minute score, so a knockout tie that finished level and went to
# pens looks like an unresolved draw without this. Columns: date, home_team,
# away_team, winner, first_shooter.
_SHOOTOUTS_URL = (
    "https://raw.githubusercontent.com/martj42/international_results"
    "/master/shootouts.csv"
)
_SHOOTOUTS_DEST = "data/raw/shootouts.csv"
_TIMEOUT = 30
_HEADERS = {
    "User-Agent": "WorldCupAlpha/0.1 (research; contact via GitHub)",
}

logger = logging.getLogger(__name__)


def download_results(
    dest: str = _DEFAULT_DEST,
    url: str = _DEFAULT_URL,
    force: bool = False,
) -> Path:
    """Download the martj42 international results CSV with a freshness check.

    Skips the download if the destination file already exists *and* was last
    modified today (UTC), unless *force* is *True*.

    Parameters
    ----------
    dest:
        Local path to write the CSV.  Parent directories are created if absent.
    url:
        Source URL; defaults to the master branch on GitHub.
    force:
        If *True*, always download even if the file is fresh.

    Returns
    -------
    Path object pointing at the downloaded file.
    """
    dest_path = Path(dest)
    if not dest_path.is_absolute():
        # Resolve relative to cwd at call time
        dest_path = Path(os.getcwd()) / dest_path

    # Compare in UTC on BOTH sides: date.today() is the machine's LOCAL date,
    # which disagrees with the UTC mtime for a few hours around local
    # midnight (e.g. Bahrain UTC+3) and made fresh files look stale.
    today_str = datetime.utcnow().date().isoformat()

    if not force and dest_path.exists():
        mtime = datetime.utcfromtimestamp(dest_path.stat().st_mtime).date()
        if mtime.isoformat() == today_str:
            logger.info("results.csv is fresh (mtime=%s), skipping download.", mtime)
            return dest_path

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading results.csv from %s …", url)
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    logger.info("Saved %d bytes to %s", len(resp.content), dest_path)
    return dest_path


def download_shootouts(
    dest: str = _SHOOTOUTS_DEST,
    url: str = _SHOOTOUTS_URL,
    force: bool = False,
) -> Path:
    """Download the martj42 penalty-shootouts CSV with a freshness check.

    Mirrors :func:`download_results`: skips the download if the destination
    already exists *and* was last modified today (UTC), unless *force* is
    *True*. This file records the winner of any knockout tie that finished
    level and went to a penalty shootout — information that ``results.csv``
    (90-minute score only) does not carry.

    Parameters
    ----------
    dest:
        Local path to write the CSV. Parent directories are created if absent.
    url:
        Source URL; defaults to the master branch on GitHub.
    force:
        If *True*, always download even if the file is fresh.

    Returns
    -------
    Path object pointing at the downloaded file.
    """
    dest_path = Path(dest)
    if not dest_path.is_absolute():
        dest_path = Path(os.getcwd()) / dest_path

    # UTC on both sides — see download_results for the local-vs-UTC rationale.
    today_str = datetime.utcnow().date().isoformat()

    if not force and dest_path.exists():
        mtime = datetime.utcfromtimestamp(dest_path.stat().st_mtime).date()
        if mtime.isoformat() == today_str:
            logger.info(
                "shootouts.csv is fresh (mtime=%s), skipping download.", mtime
            )
            return dest_path

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading shootouts.csv from %s …", url)
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    logger.info("Saved %d bytes to %s", len(resp.content), dest_path)
    return dest_path


def load_shootouts(path: Union[str, Path]) -> pd.DataFrame:
    """Load the martj42 shootouts CSV into a typed DataFrame.

    Columns: ``date`` (parsed to ``datetime64[ns]``), ``home_team``,
    ``away_team``, ``winner``, ``first_shooter``. Unparseable dates coerce to
    ``NaT`` so downstream date filters stay well-typed (see :func:`load_results`
    for the same coercion rationale).
    """
    df = pd.read_csv(path, parse_dates=["date"])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def shootout_winner(
    shootouts_df: Optional[pd.DataFrame],
    home: str,
    away: str,
    when: Optional[Union[str, date, datetime, pd.Timestamp]] = None,
) -> Optional[str]:
    """Return the penalty-shootout winner for an *unordered* team pair, or None.

    Matches on the ``{home, away}`` pair in either order. When *when* is given,
    the match is additionally restricted to the same calendar date if the
    timestamp parses cleanly; a year-only value restricts to that year. Purely
    defensive: an empty/None frame, a missing pair, or missing columns all
    yield ``None`` rather than raising.
    """
    if shootouts_df is None or len(shootouts_df) == 0:
        return None
    cols = shootouts_df.columns
    if not {"home_team", "away_team", "winner"}.issubset(cols):
        return None

    df = shootouts_df
    pair_mask = (
        (df["home_team"] == home) & (df["away_team"] == away)
    ) | (
        (df["home_team"] == away) & (df["away_team"] == home)
    )
    cand = df[pair_mask]
    if cand.empty:
        return None

    if when is not None and "date" in cols:
        ts = pd.to_datetime(when, errors="coerce")
        if pd.notna(ts):
            cand_dates = pd.to_datetime(cand["date"], errors="coerce")
            same_day = cand[cand_dates.dt.date == ts.date()]
            if not same_day.empty:
                cand = same_day
            else:
                # Same calendar date failed; accept a same-year match only (the
                # year is the reliably-trustworthy field). If neither matches,
                # the date restriction eliminated every row -> no result, rather
                # than returning an unrelated historical shootout for this pair.
                cand = cand[cand_dates.dt.year == ts.year]
                if cand.empty:
                    return None

    winner = cand.iloc[0]["winner"]
    if pd.isna(winner):
        return None
    return str(winner)


def load_results(path: Union[str, Path]) -> pd.DataFrame:
    """Load the martj42 results CSV into a typed DataFrame.

    The returned DataFrame has these dtypes:
    - ``date``: ``datetime64[ns]``
    - ``home_score``, ``away_score``: ``Int64`` (nullable integer)
    - ``neutral``: ``bool``
    - Everything else: ``object`` (string)

    Parameters
    ----------
    path:
        Path to the CSV file (or a file-like object).

    Returns
    -------
    ``pd.DataFrame`` with columns: date, home_team, away_team, home_score,
    away_score, tournament, city, country, neutral.
    """
    df = pd.read_csv(
        path,
        parse_dates=["date"],
        dtype={
            "home_team": "object",
            "away_team": "object",
            "tournament": "object",
            "city": "object",
            "country": "object",
            "neutral": "object",  # parse manually below
        },
    )
    # ``parse_dates`` silently leaves the column as object *strings* if ANY
    # value fails to parse — e.g. the cleaned dataset carries future fixtures
    # with blank/placeholder dates. That later breaks every ``date >=`` filter
    # downstream with "'>=' not supported between str and Timestamp", which
    # silently stalled settlement of in-window games. Coerce explicitly so the
    # documented datetime64 contract always holds; unparseable rows become NaT
    # and are dropped by the usual ``.notna()`` / ``>=`` guards.
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    # Normalise boolean column (stored as True/False strings in CSV)
    if "neutral" in df.columns:
        df["neutral"] = df["neutral"].map(
            lambda v: str(v).strip().lower() in ("true", "1", "yes")
        )
    # Use nullable integer type so NaN rows don't force float
    for col in ("home_score", "away_score"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    return df


def filter_since(df: pd.DataFrame, since: Union[str, date, datetime]) -> pd.DataFrame:
    """Return rows where ``date >= since``.

    Parameters
    ----------
    df:
        DataFrame returned by :func:`load_results`.
    since:
        ISO date string, ``datetime.date``, or ``datetime.datetime``.
    """
    cutoff = pd.Timestamp(since)
    return df[df["date"] >= cutoff].reset_index(drop=True)


def add_outcome_column(df: pd.DataFrame) -> pd.DataFrame:
    """Append an ``outcome`` column with values ``"H"`` / ``"D"`` / ``"A"``.

    Null scores produce a null outcome.  The input DataFrame is not mutated;
    a copy is returned.

    Reference: standard 3-way match-result encoding used throughout the
    sports-modelling literature (Dixon & Coles, 1997).
    """
    df = df.copy()

    def _outcome(row: pd.Series) -> str:
        hs = row.get("home_score")
        as_ = row.get("away_score")
        if pd.isna(hs) or pd.isna(as_):
            return None  # type: ignore[return-value]
        if int(hs) > int(as_):
            return "H"
        if int(hs) < int(as_):
            return "A"
        return "D"

    df["outcome"] = df.apply(_outcome, axis=1)
    return df
