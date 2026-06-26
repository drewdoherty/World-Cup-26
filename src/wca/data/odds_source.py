"""Odds-source orchestrator with graceful degradation.

One seam in front of every odds provider. It selects a source at runtime and
**never raises** on a provider failure: a dead, absent or unauthenticated
provider yields an empty (correctly-shaped) frame, so the card build degrades
to a fresh "data-pending" card (timestamp still advances) instead of crashing
with ``sys.exit(1)`` and freezing /card, /next and /scores.

Priority order (override with the ``WCA_ODDS_SOURCES`` env var, comma-separated):

    betfair     live Betfair Exchange API   (needs creds — see betfair_exchange)
    theoddsapi  The Odds API                (needs ODDS_API_KEY)
    polymarket  Polymarket share prices     (no creds — public Gamma API)

Default ``betfair,theoddsapi,polymarket``: Betfair wins the moment its creds are
added; until then The Odds API is tried (works again the moment the key is
re-issued); and Polymarket is the always-on floor that keeps the build live.
The same flat DataFrame shape is returned regardless of which source answered.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

import pandas as pd

from wca.data import betfair_exchange, polymarket_odds, theoddsapi

logger = logging.getLogger(__name__)

_DEFAULT_ORDER = ("betfair", "theoddsapi", "polymarket")

_COLUMNS = (
    "event_id",
    "commence_time",
    "home_team",
    "away_team",
    "bookmaker_key",
    "bookmaker_title",
    "market",
    "outcome_name",
    "outcome_description",
    "outcome_point",
    "decimal_odds",
    "retrieved_at",
)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_COLUMNS))


def _scrub(exc: object) -> str:
    """Render an exception for logs with any ``apiKey=...`` secret redacted.

    A requests HTTPError stringifies the full URL, which for The Odds API
    includes the live key. Never let that reach a log file.
    """
    import re

    return re.sub(r"(apiKey=)[^&\s]+", r"\1<redacted>", str(exc))


def _order() -> List[str]:
    raw = os.environ.get("WCA_ODDS_SOURCES", "").strip()
    if not raw:
        return list(_DEFAULT_ORDER)
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def get_odds(
    sport_key: str,
    regions: str = "uk",
    markets: str = "h2h",
    odds_format: str = "decimal",
    event_ids: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, object]:
    """Return ``(odds_df, quota)`` from the first source that yields rows.

    Tries each configured source in priority order. A source that raises or
    returns no rows is skipped. If every source is empty/unavailable, returns
    an empty (correctly-shaped) frame and ``None`` quota — the caller must treat
    an empty frame as "data-pending", not an error.
    """
    _merge_raw = os.environ.get("WCA_ODDS_MERGE", "").strip().lower()
    # "bestprice"/"union": keep EVERY source's rows so downstream best_price can
    # pick the better venue per market/outcome (Betfair vs Polymarket, labelled,
    # fee-adjusted). "1"/"gapfill": legacy gap-fill (sharper source wins a
    # fixture; later sources only fill fixtures it lacks).
    union = _merge_raw in ("bestprice", "union", "all", "both")
    merge = union or _merge_raw in ("1", "true", "yes", "gapfill")
    last_quota: object = None
    kept_quota: object = None
    frames: List[pd.DataFrame] = []
    for name in _order():
        try:
            if name == "betfair":
                df, q = betfair_exchange.get_odds(
                    sport_key, regions=regions, markets=markets,
                    odds_format=odds_format, event_ids=event_ids,
                )
            elif name == "theoddsapi":
                df, q = theoddsapi.get_odds(
                    sport_key, regions=regions, markets=markets,
                    odds_format=odds_format, event_ids=event_ids,
                )
            elif name == "polymarket":
                df, q = polymarket_odds.get_odds(
                    sport_key, regions=regions, markets=markets,
                    odds_format=odds_format, event_ids=event_ids,
                )
            else:
                logger.warning("unknown odds source %r (skipping)", name)
                continue
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the build.
            logger.warning("odds source %s failed: %s", name, _scrub(exc))
            continue
        if df is not None and not df.empty:
            if not merge:
                logger.info("odds source %s -> %d rows", name, len(df))
                return df, q
            if union:
                # Best-price mode: keep ALL of this source's rows so both venues
                # coexist on a fixture and best_price chooses the better (fee-
                # adjusted) line per outcome, labelling its venue.
                added = df
            else:
                # Gap-fill: keep earlier (sharper) source's fixtures; only add a
                # later source's rows for fixtures not already covered.
                added = _gap_fill(frames, df)
            frames.append(added)
            if kept_quota is None:
                kept_quota = q
            logger.info("odds merge(%s): %s added %d rows",
                        "union" if union else "gapfill", name, len(added))
        if q is not None:
            last_quota = q
    if merge and frames:
        combined = pd.concat(frames, ignore_index=True)
        logger.info("odds merge -> %d rows across %d sources", len(combined), len(frames))
        return combined, kept_quota
    logger.warning(
        "all odds sources empty/unavailable (%s); returning empty frame "
        "(card will be data-pending)", ",".join(_order()),
    )
    return _empty_frame(), last_quota


def _fixture_key(home: object, away: object) -> frozenset:
    """Order-independent fixture key, tolerant of cross-source name spellings."""
    from wca.data.teamnames import canonical

    return frozenset({canonical(str(home or "")), canonical(str(away or ""))})


def _gap_fill(existing: List[pd.DataFrame], new: pd.DataFrame) -> pd.DataFrame:
    """Return the subset of *new* whose fixtures are absent from *existing*."""
    seen = set()
    for f in existing:
        for _, r in f[["home_team", "away_team"]].drop_duplicates().iterrows():
            seen.add(_fixture_key(r["home_team"], r["away_team"]))
    if not seen:
        return new
    mask = new.apply(
        lambda r: _fixture_key(r["home_team"], r["away_team"]) not in seen, axis=1
    )
    return new[mask].copy()


def get_event_odds(
    sport_key: str,
    event_id: str,
    regions: str = "uk",
    markets: str = "btts",
    odds_format: str = "decimal",
) -> Tuple[pd.DataFrame, object]:
    """Per-event markets (player props/btts) from the first source with rows.

    Polymarket has no equivalent per-event frame here (its scorer enrichment is
    wired separately downstream), so this falls through to an empty frame when
    only Polymarket is available — scorer sections then render "data-pending".
    """
    for name in _order():
        try:
            if name == "betfair":
                df, q = betfair_exchange.get_event_odds(
                    sport_key, event_id, regions=regions, markets=markets,
                    odds_format=odds_format,
                )
            elif name == "theoddsapi":
                df, q = theoddsapi.get_event_odds(
                    sport_key, event_id, regions=regions, markets=markets,
                    odds_format=odds_format,
                )
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("event-odds source %s failed: %s", name, _scrub(exc))
            continue
        if df is not None and not df.empty:
            return df, q
    return _empty_frame(), None
