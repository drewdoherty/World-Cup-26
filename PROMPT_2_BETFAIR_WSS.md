# PROMPT 2: Implement Betfair WebSocket Streaming Module

**Copy-paste this entire prompt into `/ultracode`**

---

## CONTEXT

You are implementing live Betfair WebSocket streaming for World Cup 2026 matches. The REST polling module (betfair.py) provides baseline odds every 5 seconds; WebSocket streaming complements it by delivering price updates in near-real-time (<50ms latency) when subscriptions are active.

The streaming layer must:
1. Connect to Betfair ESA (Exchange Streaming API) via WSS (WebSocket Secure)
2. Subscribe to live matches (within 24h of kickoff)
3. Yield DataFrame updates as prices move
4. Auto-reconnect on disconnect with exponential backoff (1s, 2s, 4s, ..., max 30s)
5. Handle message parsing errors gracefully (log, skip malformed message, continue)
6. Clean shutdown (context manager with __aenter__ / __aexit__)

The async architecture is isolated: this module uses aiohttp + asyncio, but the main polling loop remains synchronous (in the aggregator layer, coming in PROMPT 3). This WSS stream is a *supplement* to REST polling, not a replacement.

WebSocket is only enabled for matches within 24 hours of kickoff. Outside that window, REST is sufficient.

---

## TASK

Create **src/wca/data/betfair_stream.py** with:

```python
class BetfairStreamClient:
    """Async WebSocket client for Betfair ESA (Exchange Streaming API).
    
    Usage (async context):
        async with BetfairStreamClient(api_key, market_ids) as client:
            async for odds_df in client.stream():
                # Process price update as DataFrame
                print(odds_df)
    """
    
    def __init__(
        self,
        api_key: str,
        market_ids: List[str],  # Betfair market IDs (e.g., ["1.123...", "1.456..."])
        currency: str = "GBP",
    ):
        """Initialize WebSocket client (does not connect yet).
        
        Args:
            api_key: Betfair API key (from BETFAIR_API_KEY environment variable)
            market_ids: List of Betfair market IDs to subscribe to
            currency: Betting currency (default GBP)
        """
    
    async def __aenter__(self) -> "BetfairStreamClient":
        """Async context entry: connect and authenticate."""
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context exit: unsubscribe and close."""
    
    async def stream(
        self,
        max_reconnect_attempts: int = 3,
    ) -> AsyncGenerator[pd.DataFrame, None]:
        """Yield DataFrame updates as prices move on subscribed markets.
        
        Yields:
            DataFrame with columns: [event_id, market, outcome_name, decimal_odds, retrieved_at, source]
            (subset of the full schema; only changed prices)
        
        Raises:
            RuntimeError: If max reconnect attempts exceeded
        """

async def stream_live_odds(
    market_ids: List[str],
    api_key: Optional[str] = None,
) -> AsyncGenerator[pd.DataFrame, None]:
    """Convenience function: stream live odds from Betfair ESA.
    
    Usage:
        async for df in stream_live_odds(["1.123", "1.456"]):
            print(df)  # Price update
    
    Args:
        market_ids: Betfair market IDs (from betfair_mapping.py)
        api_key: Betfair API key (default: from BETFAIR_API_KEY env var)
    
    Yields:
        DataFrame with price updates
    """
```

### Implementation Details

**Connection & Auth**:
- Use `aiohttp.ClientSession` for WebSocket connection
- Endpoint: `wss://stream-api.betfair.com/exchange`
- Auth: Include `API-NG-TOKEN` header with API key
- Heartbeat: Betfair sends periodic heartbeats; respond with PING frame

**Subscription**:
- On connection, send subscription message for each market_id:
  ```json
  {"op": "marketSubscription", "marketIds": ["1.123456789"], "fields": ["EX", "AV"]}
  ```
  - EX = exchange prices (back/lay ladder)
  - AV = available liquidity (volume at each price)
- Subscribe to 1–10 markets per WebSocket connection (Betfair soft limit: 100 concurrent subscriptions per session)

**Message Parsing**:
- Betfair sends zlib-compressed JSON updates
- Each message has: `{"op": "mcm" (market change), "clk": <clock>, "pt": [price updates], ...}`
- Extract from `pt` array: market_id, selection_id (runner), back/lay prices
- Map selection_id → outcome_name using `betfair_mapping.BETFAIR_SELECTION_MAPPING`
- Timestamp each price update with `retrieved_at = datetime.now(timezone.utc)`

**Reconnection Logic**:
- On disconnect (lost connection, timeout, remote close):
  1. Log warning with reason
  2. Wait exponential backoff: attempt_1 → 1s, attempt_2 → 2s, attempt_3 → 4s, attempt_4 → 8s, ..., max 30s
  3. Retry connection up to `max_reconnect_attempts` (default 3)
  4. If all attempts fail, raise `RuntimeError("Max reconnect attempts exceeded")`
- On successful reconnection, resubscribe to all markets

**Error Handling**:
- Malformed JSON: log error, skip message, continue streaming
- Subscription error (market not found, auth fail): log and raise RuntimeError
- Network timeout (no message for 60s): trigger reconnect
- Graceful shutdown: close WebSocket, cancel all pending tasks

**DataFrame Schema**:
Each yielded DataFrame has columns:
- `event_id`: string (from market metadata, or mapped from market_id via betfair_mapping)
- `market`: string ("h2h", "totals", etc. from betfair_mapping)
- `outcome_name`: string ("Home", "Draw", "Away", "Over 2.5", etc.)
- `decimal_odds`: float (best available price: max(back_prices) or similar)
- `retrieved_at`: pandas.Timestamp (UTC)
- `source`: "betfair" (literal string)

**Logging**:
- Debug: subscription acks, heartbeats, message counts
- Info: connection established, disconnected, reconnect attempts
- Warning: subscription failures, malformed messages, timeouts
- Error: unrecoverable failures

---

## SUCCESS CRITERIA

1. **Module imports without error**: `from wca.data.betfair_stream import BetfairStreamClient, stream_live_odds`
2. **BetfairStreamClient is an async context manager**: has `__aenter__`, `__aexit__`
3. **stream() is an async generator**: yields DataFrames
4. **DataFrame schema validation**:
   - Columns: [event_id, market, outcome_name, decimal_odds, retrieved_at, source]
   - All timestamps are pandas.Timestamp, UTC
5. **Reconnection logic**: exponential backoff (1s, 2s, 4s, ..., max 30s)
6. **Malformed JSON handling**: logged and skipped, stream continues
7. **Connection timeout**: (no message for 60s) triggers reconnect attempt
8. **Max reconnect attempts exceeded**: raises RuntimeError with clear message
9. **Graceful shutdown**: closes WebSocket and cancels pending tasks
10. **Unit tests pass**: tests/test_betfair_stream.py with mocked aiohttp.ClientSession

---

## TESTING

Create **tests/test_betfair_stream.py**:

```python
import pytest
import asyncio
from unittest.mock import AsyncMock, Mock, patch
import pandas as pd
from datetime import datetime, timezone
from wca.data.betfair_stream import BetfairStreamClient, stream_live_odds

@pytest.mark.asyncio
async def test_betfair_stream_client_context_manager():
    """Verify context manager enters/exits cleanly."""
    with patch("aiohttp.ClientSession") as mock_session:
        mock_ws = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        
        async with BetfairStreamClient("test_key", ["1.123"]) as client:
            assert client is not None
        
        # WebSocket should be closed
        mock_ws.close.assert_called()

@pytest.mark.asyncio
async def test_stream_live_odds_yields_dataframe():
    """Verify stream yields DataFrames with correct schema."""
    with patch("aiohttp.ClientSession") as mock_session:
        mock_ws = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        
        # Mock a single price update message
        sample_message = {
            "op": "mcm",
            "clk": "123456",
            "pt": [
                {
                    "id": 1.123456789,
                    "rc": [
                        {"id": 12345, "ex": {"b": [[1.5, 10]], "l": [[1.51, 5]]}}
                    ]
                }
            ]
        }
        mock_ws.__aiter__.return_value = [sample_message]
        
        async with BetfairStreamClient("test_key", ["1.123456789"]) as client:
            async for df in client.stream():
                assert isinstance(df, pd.DataFrame)
                assert all(col in df.columns for col in ["event_id", "market", "outcome_name", "decimal_odds", "retrieved_at", "source"])
                break

@pytest.mark.asyncio
async def test_stream_reconnect_exponential_backoff():
    """Verify reconnection uses exponential backoff (1s, 2s, 4s, ...)."""
    with patch("aiohttp.ClientSession") as mock_session:
        with patch("asyncio.sleep") as mock_sleep:
            mock_ws = AsyncMock()
            mock_session.ws_connect = AsyncMock(return_value=mock_ws)
            
            # Simulate connection failure on first attempt
            mock_ws.__aiter__.return_value = []
            mock_ws.close.side_effect = Exception("Connection lost")
            
            async with BetfairStreamClient("test_key", ["1.123"]) as client:
                try:
                    async for _ in client.stream(max_reconnect_attempts=2):
                        pass
                except RuntimeError:
                    pass
            
            # Verify sleep was called with exponential backoff values
            sleep_calls = mock_sleep.call_args_list
            # Should have called sleep(1) and sleep(2)

@pytest.mark.asyncio
async def test_stream_malformed_json_skipped():
    """Verify malformed JSON is logged and skipped (stream continues)."""
    with patch("aiohttp.ClientSession") as mock_session:
        mock_ws = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        
        # Return malformed message then valid message
        messages = [
            None,  # Malformed
            {"op": "mcm", "pt": [{"id": 1.123, "rc": [{"id": 1, "ex": {"b": [[1.5, 10]]}}]}]}  # Valid
        ]
        mock_ws.__aiter__.return_value = messages
        
        async with BetfairStreamClient("test_key", ["1.123"]) as client:
            count = 0
            async for df in client.stream():
                count += 1
                break
            assert count == 1  # Should have gotten 1 valid DataFrame despite malformed message

@pytest.mark.asyncio
async def test_stream_timeout_triggers_reconnect():
    """Verify 60s timeout without message triggers reconnect."""
    with patch("aiohttp.ClientSession") as mock_session:
        with patch("asyncio.sleep") as mock_sleep:
            mock_ws = AsyncMock()
            mock_session.ws_connect = AsyncMock(return_value=mock_ws)
            
            # Simulate long silence
            async def slow_iter():
                await asyncio.sleep(61)
                yield {"op": "heartbeat"}
            
            mock_ws.__aiter__.return_value = slow_iter()
            
            async with BetfairStreamClient("test_key", ["1.123"]) as client:
                try:
                    async for _ in client.stream():
                        pass
                except asyncio.TimeoutError:
                    pass

@pytest.mark.asyncio
async def test_stream_max_reconnect_attempts_exceeded():
    """Verify max reconnect attempts raises RuntimeError."""
    with patch("aiohttp.ClientSession") as mock_session:
        mock_ws = AsyncMock()
        mock_session.ws_connect = AsyncMock(side_effect=Exception("Connection refused"))
        
        async with BetfairStreamClient("test_key", ["1.123"]) as client:
            with pytest.raises(RuntimeError, match="Max reconnect attempts exceeded"):
                async for _ in client.stream(max_reconnect_attempts=1):
                    pass
```

---

## CONFIGURATION

Environment variables:
- `BETFAIR_API_KEY`: (reuse from betfair.py)
- `BETFAIR_WSS_ENABLED`: "true" | "false" (default: "true" if within 24h of match kickoff)

---

## NOTES

- WSS is async-only; main polling loop is synchronous (aggregator handles coordination)
- Do NOT block the event loop; use asyncio.Task and .gather() for concurrent subscriptions
- Subscription limit: ~100 per session (Betfair soft limit); span multiple clients if needed
- No REST fallback in this module; REST is handled by betfair.py (REST-only) and aggregator (fallback logic)
- All timestamps must be pandas.Timestamp, UTC
- Mapping tables (betfair_mapping.py) must be populated from Betfair API introspection (dependency)

---

## DELIVERABLES

- [ ] src/wca/data/betfair_stream.py (WebSocket streaming module)
- [ ] tests/test_betfair_stream.py (async unit tests)

---

## INTEGRATION NOTE

This module is called by the aggregator (PROMPT 3) optionally when live match prices are needed. It runs in a separate async context and is not required for basic polling to work.
