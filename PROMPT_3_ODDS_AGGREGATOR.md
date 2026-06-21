# PROMPT 3: Implement Odds Aggregator with Fallback Logic

**Copy-paste this entire prompt into `/ultracode`**

---

## CONTEXT

You are implementing the central orchestration layer for dual-feed odds polling (Betfair REST + TheOddsAPI fallback). This aggregator:

1. Routes `get_odds()` calls to the primary source (configurable: Betfair or TheOddsAPI)
2. Falls back automatically if primary fails (timeout, network error, auth failure)
3. Normalizes both sources to a unified schema with "source" column
4. Optionally merges live WSS updates (from betfair_stream.py) into REST snapshots
5. Caches results locally (30s TTL by default) to avoid hammering APIs
6. Logs source selection, fallbacks, and cache hits

The aggregator is the *single point of entry* for all odds data in the system. Existing code (scripts/wca_snapshot_odds.py, scripts/wca_snapshotd.py) will import from odds_aggregator and select source via environment variable or CLI flag.

Backward compatibility: theodossapi.py remains unchanged and importable, but new code should use `odds_aggregator.get_odds()`.

---

## TASK

Create **src/wca/data/odds_aggregator.py** with:

```python
class OddsAggregator:
    """Routes odds requests to primary/fallback sources with caching.
    
    Usage (synchronous):
        agg = OddsAggregator()
        df, meta = agg.get_odds(use_betfair=True, use_fallback=True)
        print(df["source"].unique())  # ['betfair'] or ['betfair', 'theoddsapi'] if merged
    """
    
    def __init__(
        self,
        cache_ttl_seconds: int = 30,
        rest_timeout_seconds: int = 5,
        fallback_after_failures: int = 3,
    ):
        """Initialize aggregator with cache and fallback config.
        
        Args:
            cache_ttl_seconds: Time-to-live for cached odds (default 30s)
            rest_timeout_seconds: REST API timeout (default 5s)
            fallback_after_failures: Activate fallback after N consecutive primary failures (default 3)
        """
    
    def get_odds(
        self,
        sport_key: str = "soccer_fifa_world_cup",
        regions: str = "uk",
        markets: str = "h2h,totals",
        odds_format: str = "decimal",
        event_ids: Optional[List[str]] = None,
        use_betfair: bool = True,  # Can be overridden by ODDS_SOURCE_PRIMARY env var
        use_fallback: bool = True,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Fetch odds from primary source with fallback.
        
        Args:
            sport_key: Sport key (e.g. "soccer_fifa_world_cup")
            regions: Bookmaker regions (e.g. "uk")
            markets: Markets to fetch (e.g. "h2h,totals")
            odds_format: Odds format ("decimal" or "american")
            event_ids: Filter to specific event IDs (optional)
            use_betfair: If True, try Betfair first; if False, use TheOddsAPI
            use_fallback: If True, fall back to secondary source on primary failure
        
        Returns:
            Tuple of (DataFrame, metadata_dict)
            DataFrame has "source" column: "betfair", "theodossapi", or "hybrid" (if merged WSS)
            metadata_dict includes: {
                "sources_used": ["betfair"],
                "fallback_triggered": False,
                "cache_hit": False,
                "merge_wss": False,
            }
        
        Raises:
            RuntimeError: If both primary and fallback fail
        """

def _normalize_betfair_frame(df: pd.DataFrame, source: str = "betfair") -> pd.DataFrame:
    """Add source column to Betfair DataFrame (for compatibility)."""

def _normalize_theoddsapi_frame(df: pd.DataFrame, source: str = "theoddsapi") -> pd.DataFrame:
    """Add source column to TheOddsAPI DataFrame (for compatibility)."""

def _merge_frames(
    primary_df: pd.DataFrame,
    fallback_df: pd.DataFrame,
    on: List[str] = ["event_id", "market", "outcome_name"],
) -> pd.DataFrame:
    """Merge primary and fallback DataFrames.
    
    Uses primary's prices where available; fills missing markets/outcomes from fallback.
    Source column reflects which source each row came from.
    """

def merge_wss_updates(
    rest_df: pd.DataFrame,
    wss_prices: Dict[Tuple[str, str, str], float],  # (event_id, market, outcome) → price
    wss_timestamp: datetime,
) -> pd.DataFrame:
    """Merge live WSS prices into REST snapshot.
    
    If a (event_id, market, outcome) exists in both, prefer WSS (fresher).
    """
```

### Implementation Details

**Caching**:
- Local cache (dict-based) with TTL: keyed by `(sport_key, regions, markets, frozenset(event_ids))`
- Check cache before calling any API
- If hit and not stale, return cached frame + `{"cache_hit": True}`
- On cache miss or stale, fetch from API and update cache
- TTL check: `cache_timestamp + cache_ttl_seconds > current_time`
- No distributed cache (local-only, fine for single-threaded polling daemon)

**Primary vs. Fallback Selection**:
- Default primary: `ODDS_SOURCE_PRIMARY` environment variable ("BETFAIR" | "THEODDSAPI")
- Fallback: whichever source is NOT primary
- `use_betfair` parameter can override env var (env var wins if set)
- Default env var if not set: "THEODDSAPI" (stage 1: proven baseline)

**Fallback Routing Logic**:
1. Try primary source for `rest_timeout_seconds` (default 5s)
2. On any error (timeout, auth, network, exception):
   - Log warning: "Primary <source> failed, trying fallback: <error>"
   - Increment consecutive failure counter for this source
   - Try fallback with same config
3. If fallback succeeds:
   - Reset consecutive failure counter
   - Return result + `{"fallback_triggered": True, "sources_used": ["primary", "fallback"]}`
4. If both fail:
   - Raise `RuntimeError(f"Both primary ({primary_error}) and fallback ({fallback_error}) failed")`
5. **Adaptive routing** (optional): After `fallback_after_failures` consecutive primary failures, demote primary for next N calls

**Schema Normalization**:
Both sources return DataFrames with these columns (already matching):
- event_id, commence_time, home_team, away_team (match metadata)
- bookmaker_key, bookmaker_title (exchange identity)
- market, outcome_name, outcome_description, outcome_point (market & outcome)
- decimal_odds, retrieved_at (price snapshot)
- **source** (NEW: "betfair" | "theodossapi" | "hybrid")

If either source is missing columns, pad with NaN or defaults (use pd.concat with join='outer').

**WSS Merging** (optional, advanced):
- Only if `betfair_stream.py` has live WebSocket prices available
- Live prices supersede REST prices by latency (WSS <50ms vs REST 200–500ms)
- Mark merged rows with `source="hybrid"` or `source="betfair_wss"`
- Only merge if within 5 minutes of kickoff (when prices are volatile)
- If WSS timestamp is older than REST timestamp, ignore WSS (data freshness check)

**Logging**:
- Debug: cache hit/miss, API call details (latency, bytes received)
- Info: source selection, fallback triggers, merged frames
- Warning: API timeouts, transient errors, adaptive routing changes
- Error: unrecoverable failures

---

## SUCCESS CRITERIA

1. **Module imports**: `from wca.data.odds_aggregator import OddsAggregator`
2. **OddsAggregator() instantiates** with default config
3. **get_odds() returns (DataFrame, dict)** tuple
4. **DataFrame has "source" column** with values "betfair" | "theoddsapi" | "hybrid"
5. **All 12 required columns present** (same as theodossapi)
6. **Cache working**: repeated calls within TTL return same frame + `{"cache_hit": True}`
7. **Cache expiry**: calls after TTL re-fetch from API (no cache hit)
8. **Fallback logic**: if primary fails, fallback is attempted and result returned
9. **Timeout respected**: REST calls timeout after `rest_timeout_seconds` (default 5s)
10. **Env var ODDS_SOURCE_PRIMARY overrides use_betfair parameter** (if set)
11. **Metadata dict** includes: sources_used, fallback_triggered, cache_hit, merge_wss
12. **Normalized frames** (both sources) have identical column order and types
13. **Unit tests pass**: test_odds_aggregator.py with mocked betfair.get_odds_rest() and theodossapi.get_odds()
14. **Integration test**: snapshot daemon can call aggregator and insert rows correctly

---

## TESTING

Create **tests/test_odds_aggregator.py**:

```python
import pytest
import os
from unittest.mock import Mock, patch, MagicMock
import pandas as pd
from datetime import datetime, timezone
from wca.data.odds_aggregator import (
    OddsAggregator,
    _merge_frames,
    merge_wss_updates,
)

def _sample_df(source: str, outcomes=None, odds=None) -> pd.DataFrame:
    """Helper: create synthetic odds DataFrame for testing."""
    outcomes = outcomes or ["Home", "Draw", "Away"]
    odds = odds or 1.50
    
    rows = []
    for outcome in outcomes:
        rows.append({
            "event_id": "ev123",
            "commence_time": pd.Timestamp("2026-06-11 12:00", tz="UTC"),
            "home_team": "Brazil",
            "away_team": "Ecuador",
            "bookmaker_key": "betfair" if source == "betfair" else "pinnacle",
            "bookmaker_title": "Betfair" if source == "betfair" else "Pinnacle",
            "market": "h2h",
            "outcome_name": outcome,
            "outcome_description": None,
            "outcome_point": None,
            "decimal_odds": odds,
            "retrieved_at": pd.Timestamp.now(tz="UTC"),
        })
    
    return pd.DataFrame(rows)

def test_odds_aggregator_primary_betfair():
    """Verify aggregator calls Betfair first if use_betfair=True."""
    with patch("wca.data.betfair.get_odds_rest") as mock_betfair:
        with patch("wca.data.theodossapi.get_odds") as mock_theodoss:
            mock_betfair.return_value = (_sample_df("betfair"), {"rate_limit_remaining": 100})
            agg = OddsAggregator()
            df, meta = agg.get_odds(use_betfair=True)
            assert meta["sources_used"] == ["betfair"]
            assert (df["source"] == "betfair").all()
            mock_betfair.assert_called_once()
            mock_theodoss.assert_not_called()

def test_odds_aggregator_fallback_on_primary_failure():
    """Verify fallback is attempted if primary fails."""
    with patch("wca.data.betfair.get_odds_rest") as mock_betfair:
        with patch("wca.data.theodossapi.get_odds") as mock_theodoss:
            mock_betfair.side_effect = TimeoutError("Betfair timeout")
            mock_theodoss.return_value = (_sample_df("theodossapi"), {"remaining": 50})
            agg = OddsAggregator()
            df, meta = agg.get_odds(use_betfair=True, use_fallback=True)
            assert meta["sources_used"] == ["betfair", "theodossapi"]
            assert meta["fallback_triggered"] == True
            assert (df["source"] == "theodossapi").all()
            mock_betfair.assert_called_once()
            mock_theodoss.assert_called_once()

def test_odds_aggregator_cache_hit():
    """Verify cache returns same frame + cache_hit=True on second call."""
    with patch("wca.data.betfair.get_odds_rest") as mock_betfair:
        mock_betfair.return_value = (_sample_df("betfair"), {"rate_limit_remaining": 100})
        agg = OddsAggregator(cache_ttl_seconds=30)
        df1, meta1 = agg.get_odds()
        df2, meta2 = agg.get_odds()  # Same call, within TTL
        assert meta1["cache_hit"] == False
        assert meta2["cache_hit"] == True
        assert df1.equals(df2)
        mock_betfair.assert_called_once()  # Only called once due to cache

def test_odds_aggregator_cache_expiry():
    """Verify cache is re-fetched after TTL."""
    with patch("wca.data.betfair.get_odds_rest") as mock_betfair:
        mock_betfair.return_value = (_sample_df("betfair"), {"rate_limit_remaining": 100})
        with patch("time.time") as mock_time:
            agg = OddsAggregator(cache_ttl_seconds=30)
            mock_time.return_value = 1000.0
            df1, meta1 = agg.get_odds()
            mock_time.return_value = 1031.0  # +31s, past TTL
            df2, meta2 = agg.get_odds()
            assert meta1["cache_hit"] == False
            assert meta2["cache_hit"] == False
            assert mock_betfair.call_count == 2  # Called twice

def test_odds_aggregator_env_var_primary():
    """Verify ODDS_SOURCE_PRIMARY env var overrides use_betfair parameter."""
    with patch.dict(os.environ, {"ODDS_SOURCE_PRIMARY": "THEODDSAPI"}):
        with patch("wca.data.theodossapi.get_odds") as mock_theodoss:
            mock_theodoss.return_value = (_sample_df("theodossapi"), {"remaining": 50})
            agg = OddsAggregator()
            df, meta = agg.get_odds(use_betfair=True)  # Should ignore this, use THEODDSAPI
            assert meta["sources_used"] == ["theodossapi"]

def test_odds_aggregator_both_fail():
    """Verify RuntimeError if both primary and fallback fail."""
    with patch("wca.data.betfair.get_odds_rest") as mock_betfair:
        with patch("wca.data.theodossapi.get_odds") as mock_theodoss:
            mock_betfair.side_effect = TimeoutError("Betfair timeout")
            mock_theodoss.side_effect = TimeoutError("TheOddsAPI timeout")
            agg = OddsAggregator()
            with pytest.raises(RuntimeError, match="Both primary and fallback failed"):
                agg.get_odds(use_betfair=True, use_fallback=True)

def test_odds_aggregator_no_fallback():
    """Verify failure if use_fallback=False and primary fails."""
    with patch("wca.data.betfair.get_odds_rest") as mock_betfair:
        mock_betfair.side_effect = RuntimeError("Betfair auth failed")
        agg = OddsAggregator()
        with pytest.raises(RuntimeError):
            agg.get_odds(use_betfair=True, use_fallback=False)

def test_merge_frames():
    """Verify _merge_frames fills missing outcomes from fallback."""
    primary = _sample_df("betfair", outcomes=["Home", "Draw"])
    fallback = _sample_df("theodossapi", outcomes=["Home", "Draw", "Away"])
    merged = _merge_frames(primary, fallback)
    assert "Away" in merged["outcome_name"].values
    assert merged[merged["outcome_name"] == "Home"]["source"].iloc[0] == "betfair"
    assert merged[merged["outcome_name"] == "Away"]["source"].iloc[0] == "theodossapi"

def test_merge_wss_updates():
    """Verify live WSS prices supersede REST prices."""
    rest_df = _sample_df("betfair", odds=1.50)  # Stale REST price
    wss_prices = {("ev123", "h2h", "Home"): 1.55}  # Fresher WSS price
    wss_ts = datetime.now(timezone.utc)
    merged = merge_wss_updates(rest_df, wss_prices, wss_ts)
    home_row = merged[merged["outcome_name"] == "Home"].iloc[0]
    assert home_row["decimal_odds"] == 1.55
    assert home_row["source"] == "hybrid"
```

---

## CONFIGURATION

Environment variables:
- `ODDS_SOURCE_PRIMARY`: "BETFAIR" | "THEODDSAPI" (default: "THEODDSAPI" for stage 1)
- `ODDS_CACHE_TTL_SECONDS`: int (default: 30)
- `ODDS_REST_TIMEOUT_MS`: int (default: 5000, converted to seconds)
- `ODDS_FALLBACK_CONSECUTIVE_FAILURES`: int (default: 3)

Add to **.env.example**:

```bash
ODDS_SOURCE_PRIMARY=THEODDSAPI
ODDS_CACHE_TTL_SECONDS=30
ODDS_REST_TIMEOUT_MS=5000
ODDS_FALLBACK_CONSECUTIVE_FAILURES=3
```

---

## INTEGRATION WITH EXISTING CODE

Modify (DO NOT break existing calls):

**scripts/wca_snapshot_odds.py**:
- Add `--source` flag (default: THEODDSAPI for backward compat)
  ```bash
  python scripts/wca_snapshot_odds.py --source THEODDSAPI  # Legacy
  python scripts/wca_snapshot_odds.py --source BETFAIR     # New primary
  ```

**scripts/wca_snapshotd.py**:
- Add `--source` flag, route snapshot calls via `odds_aggregator.get_odds()`

**src/wca/data/__init__.py**:
- Export OddsAggregator
- Add deprecation note to direct theodossapi imports (inline comment)

**src/wca/data/snapshot.py**:
- `rows_from_odds_frame()` already handles `source` column
- No changes needed

---

## NOTES

- No async/await in this module (synchronous polling driver)
- WSS merging is optional (advanced feature, can be disabled with env var)
- Cache is single-process local (fine for snapshotd daemon)
- Fallback promotion (adaptive routing after N failures) is optional
- Mapping tables (betfair_mapping.py) must be populated from Betfair introspection (dependency)
- All timestamps must be pandas.Timestamp with UTC timezone

---

## DELIVERABLES

- [ ] src/wca/data/odds_aggregator.py (OddsAggregator class + helper functions)
- [ ] tests/test_odds_aggregator.py (unit tests)
- [ ] Integration test update to snapshot daemon (verify it can use aggregator)
- [ ] .env.example updated with new config vars
- [ ] scripts/wca_snapshot_odds.py updated with --source flag
- [ ] scripts/wca_snapshotd.py updated with --source flag
