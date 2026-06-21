# PROMPT 1: Implement Betfair REST Polling Module

**Copy-paste this entire prompt into `/ultracode`**

---

## CONTEXT

You are implementing the primary odds feed for World Cup 2026 live betting. The project uses a synchronous polling architecture (theodossapi.py via requests) that must remain unchanged. You are adding a new Betfair REST module that matches the existing theodossapi.get_odds() contract exactly, so it can be swapped in as a primary source or fallback.

Current baseline: theodossapi.py returns a pandas DataFrame with columns:
```
[event_id, commence_time, home_team, away_team, bookmaker_key, bookmaker_title, market, 
 outcome_name, outcome_description, outcome_point, decimal_odds, retrieved_at]
```

Your module must:
1. Query Betfair REST API for all 104 WC 2026 matches
2. Normalize responses to the EXACT same schema as theodossapi
3. Support all Betfair-available markets (match odds, goals, corners, etc.)
4. Authenticate via betfairlightweight library (OAuth token managed by library)
5. Handle timeouts and errors cleanly (20s timeout, log, re-raise)
6. Return a DataFrame with source='betfair' added as metadata column

Your module must NOT attempt WebSocket streaming in this prompt. REST polling only.

---

## TASK

Create **src/wca/data/betfair.py** with the following public interface:

```python
def get_odds_rest(
    sport_key: str = "soccer_fifa_world_cup",
    regions: str = "uk",
    markets: str = "h2h,totals",
    odds_format: str = "decimal",
    event_ids: Optional[List[str]] = None,
    timeout_seconds: int = 20,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Fetch odds from Betfair REST API matching theodossapi signature.
    
    Returns:
        Tuple of (DataFrame, metadata_dict).
        DataFrame has columns: [event_id, commence_time, home_team, away_team,
                               bookmaker_key, bookmaker_title, market,
                               outcome_name, outcome_description, outcome_point,
                               decimal_odds, retrieved_at]
        metadata_dict holds: {"source": "betfair", "rate_limit_remaining": int, ...}
    """
```

### Implementation Details

**Connection & Auth**:
- Use betfairlightweight.APIClient for REST calls
- Authenticate via BETFAIR_API_KEY environment variable
- Load API key with error if missing: `raise EnvironmentError("BETFAIR_API_KEY not set")`

**Data Fetching**:
- Query Betfair API for all WC 2026 events (104 matches total)
- If event_ids parameter provided, filter to those only
- Fetch markets: h2h (Match Odds), totals (Over/Under 2.5), btts (Both Teams To Score)
- Use betfair_mapping.py to translate Betfair IDs → FIFA event IDs

**Schema Mapping** (CRITICAL - must match theodossapi exactly):
- `event_id`: Betfair event_id (string)
- `commence_time`: Match kickoff time (pandas.Timestamp, UTC)
- `home_team`, `away_team`: Team names (string)
- `bookmaker_key`: "betfair" (literal string)
- `bookmaker_title`: "Betfair" (literal string)
- `market`: "h2h" | "totals" | "btts" (standardized, not Betfair's native names)
- `outcome_name`: 
  - For h2h: Actual team names ("Brazil", "Ecuador", etc.)
  - For totals: "Over" or "Under"
  - For btts: "Yes" or "No"
- `outcome_description`: Null or detailed prop descriptor (optional, may be null)
- `outcome_point`: 
  - For h2h: None (null)
  - For totals: Float (e.g., 2.5)
  - For btts: None (null)
- `decimal_odds`: Decimal odds (float >= 1.0)
- `retrieved_at`: Timestamp when odds were fetched (pandas.Timestamp, UTC, use datetime.datetime.now(timezone.utc))

**Error Handling**:
- Timeout (no response within timeout_seconds): raise `requests.Timeout`
- Auth failure (invalid key, expired token): raise `RuntimeError("Betfair auth failed: ...")`
- Rate limit (HTTP 429): log warning, sleep 30 seconds, retry up to 2 times
- Network error (connection refused, DNS): raise `requests.RequestException`
- Empty response (no events): return empty DataFrame with correct columns (no rows, all columns present)

**Logging**:
- Log successful connection and event count at INFO level
- Log auth failures and timeouts at ERROR level
- Log rate limit hits at WARNING level

**Also Create**: `src/wca/data/betfair_mapping.py` with stubs:

```python
"""Betfair ID mappings to FIFA event IDs and market/outcome names."""

# Populate from Betfair API introspection (dependency task)
# Format: Betfair event_id (string) → FIFA event_id (string)
# Example: {"30123456": "ev_qat_ecua"}
BETFAIR_TO_FIFA_EVENT_MAPPING = {
    # TODO: Populate with all 104 WC2026 events
}

# Betfair market ID (string) → standardized market key
# Example: {"1.123456789": "h2h"}
BETFAIR_MARKET_MAPPING = {
    # TODO: Populate with h2h, totals, btts market IDs
}

# Betfair selection ID (runner ID) → outcome name
# Example: {"123456": "Home", "456789": "Draw", "789012": "Away"}
BETFAIR_SELECTION_MAPPING = {
    # TODO: Populate with all possible runners
}
```

---

## SUCCESS CRITERIA

1. **Module imports without error**: `from wca.data.betfair import get_odds_rest`
2. **Calling get_odds_rest() with no args returns (DataFrame, dict)**
3. **DataFrame schema validation**:
   - Has exactly 12 columns (no extras): event_id, commence_time, home_team, away_team, bookmaker_key, bookmaker_title, market, outcome_name, outcome_description, outcome_point, decimal_odds, retrieved_at
   - All columns present even if data is empty
4. **Empty response handling**: If Betfair returns no events, return (empty DataFrame with correct schema, metadata)
5. **Data quality for populated rows**:
   - event_id is non-null string
   - commence_time is pandas.Timestamp, UTC, non-null
   - home_team, away_team are non-null strings (actual team names)
   - bookmaker_key == "betfair", bookmaker_title == "Betfair"
   - market in {"h2h", "totals", "btts"}
   - outcome_name is string (team name for h2h, "Over"/"Under" for totals)
   - decimal_odds is float >= 1.0
   - retrieved_at is pandas.Timestamp, UTC, non-null
6. **Metadata dict structure**: includes at least `{"source": "betfair", "rate_limit_remaining": ...}`
7. **Timeout behavior**: Respects timeout_seconds parameter (default 20s)
8. **Auth failure**: Raises RuntimeError with "Betfair auth failed" message
9. **Network timeout**: Raises requests.Timeout
10. **All timestamps are timezone-aware (UTC)**
11. **Unit tests pass**: tests/test_betfair.py with mocked APIClient
12. **betfair_mapping.py exists** with correct structure (stubs OK for now)

---

## TESTING

Create **tests/test_betfair.py**:

```python
import pytest
from unittest.mock import Mock, patch
import pandas as pd
import pytz
from datetime import datetime
from wca.data.betfair import get_odds_rest

def test_get_odds_rest_returns_tuple():
    """Verify get_odds_rest returns (DataFrame, dict)."""
    with patch("wca.data.betfair.APIClient") as mock_api:
        mock_client = Mock()
        mock_api.return_value = mock_client
        # Mock minimal response
        mock_client.list_events.return_value = []
        df, meta = get_odds_rest()
        assert isinstance(df, pd.DataFrame)
        assert isinstance(meta, dict)

def test_get_odds_rest_schema():
    """Verify DataFrame has all 12 required columns."""
    with patch("wca.data.betfair.APIClient"):
        df, _ = get_odds_rest()
        expected_cols = [
            'event_id', 'commence_time', 'home_team', 'away_team',
            'bookmaker_key', 'bookmaker_title', 'market',
            'outcome_name', 'outcome_description', 'outcome_point',
            'decimal_odds', 'retrieved_at'
        ]
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"

def test_get_odds_rest_empty_response():
    """Verify empty Betfair response returns empty DataFrame with correct schema."""
    with patch("wca.data.betfair.APIClient") as mock_api:
        mock_client = Mock()
        mock_api.return_value = mock_client
        mock_client.list_events.return_value = []
        df, meta = get_odds_rest()
        assert df.empty
        assert len(df.columns) == 12

def test_get_odds_rest_timeout():
    """Verify timeout after timeout_seconds raises requests.Timeout."""
    import requests
    with patch("wca.data.betfair.APIClient") as mock_api:
        mock_client = Mock()
        mock_api.return_value = mock_client
        mock_client.list_events.side_effect = requests.Timeout("Connection timeout")
        with pytest.raises(requests.Timeout):
            get_odds_rest(timeout_seconds=5)

def test_get_odds_rest_auth_failure():
    """Verify auth failure raises RuntimeError with 'Betfair auth failed'."""
    with patch("wca.data.betfair.APIClient") as mock_api:
        mock_api.side_effect = Exception("Invalid API key")
        with pytest.raises(RuntimeError, match="Betfair auth failed"):
            get_odds_rest()

def test_betfair_mapping_structure():
    """Verify betfair_mapping.py exists with correct structure."""
    from wca.data import betfair_mapping
    assert hasattr(betfair_mapping, 'BETFAIR_TO_FIFA_EVENT_MAPPING')
    assert isinstance(betfair_mapping.BETFAIR_TO_FIFA_EVENT_MAPPING, dict)
    assert hasattr(betfair_mapping, 'BETFAIR_MARKET_MAPPING')
    assert hasattr(betfair_mapping, 'BETFAIR_SELECTION_MAPPING')

def test_get_odds_rest_timestamps_are_utc():
    """Verify all timestamps are UTC and timezone-aware."""
    with patch("wca.data.betfair.APIClient") as mock_api:
        mock_client = Mock()
        mock_api.return_value = mock_client
        mock_client.list_events.return_value = []
        df, _ = get_odds_rest()
        if not df.empty:
            assert df['commence_time'].dt.tz is not None, "commence_time not timezone-aware"
            assert df['retrieved_at'].dt.tz is not None, "retrieved_at not timezone-aware"
```

---

## CONFIGURATION

Add to **.env.example**:

```bash
# Betfair REST API
BETFAIR_API_KEY=your_betfair_api_key_here
BETFAIR_USERNAME=your_betfair_username
BETFAIR_PASSWORD=your_betfair_password
```

---

## DELIVERABLES

- [ ] src/wca/data/betfair.py (REST polling module)
- [ ] src/wca/data/betfair_mapping.py (static lookup stubs)
- [ ] tests/test_betfair.py (unit tests)
- [ ] .env.example updated

---

## NOTES

- Do NOT implement WebSocket streaming in this prompt (REST only)
- Do NOT modify theodossapi.py (backward compatibility)
- Keep this module synchronous (no async/await)
- betfair_mapping.py stubs can be empty dicts for now; populate from Betfair API docs before June 11
- All timestamps must be pandas.Timestamp with UTC timezone
- Return schema must match theodossapi exactly; tests compare frames directly
