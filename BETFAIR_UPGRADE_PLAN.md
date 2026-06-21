# Betfair Exchange Upgrade Plan
## World Cup 2026 Odds Architecture Migration

**Objective**: Upgrade from TheOddsAPI-only → Betfair (primary) + TheOddsAPI (fallback/validation)

**Timeline**: 3–4 calendar days | **Effort**: 14 hours | **Cost**: Commission-only + $30/mo TheOddsAPI

---

## Current State

**TheOddsAPI (existing)**:
- Latency: 200–500ms (polling only)
- Coverage: ~20 books aggregated
- Cost: $30/mo
- Auth: ODDS_API_KEY env var
- Used by: snapshot.py, card.py, nextmatch.py, scorespage.py

**Problem**: 
- Slow for live in-play updates (need <50ms)
- Small book coverage (20 vs 100+ available)
- Limited market breadth (missing some prop bets)

---

## Optimal Architecture

### Primary Feed: Betfair Exchange
- **REST polling**: Every 5s, all 104 WC matches, 100+ markets/match
- **WSS streaming**: Live matches only, <50ms latency updates
- **Cost**: Commission-only (aligned profit incentive)
- **Auth**: OAuth via betfairlightweight library (BETFAIR_API_KEY)

### Fallback: TheOddsAPI
- **Kept as-is**: No breaking changes
- **Role**: Fallback on Betfair timeout/auth failure; validation cross-check
- **Trigger**: After 3 consecutive Betfair failures

### Aggregator Layer
- **Single entry point**: `OddsAggregator.get_odds()`
- **Routing**: Primary/fallback selection via env var or parameter
- **Caching**: 30s TTL local cache
- **Merging**: Optional WSS price updates into REST snapshots

---

## Implementation Phases

### **Phase 1: REST Polling (Week 1, June 4–6)**
Execute PROMPT 1 to create `betfair.py`
- Synchronous REST client matching theodossapi schema exactly
- 20s timeout, auth via betfairlightweight, all 104 WC matches
- Tests: schema, timeout, empty response, auth failure
- **Gate**: Can poll all 104 matches in <30s? ✓

### **Phase 2: WSS Streaming + Aggregator (Week 2, June 7–10)**
Execute PROMPT 2 + PROMPT 3
- Async WebSocket client (isolated, no blocking)
- Exponential backoff reconnection (1s, 2s, 4s, ..., max 30s)
- Central aggregator with fallback routing & caching
- Integration tests: failover, cache hit/miss/expiry, both sources fail
- **Gate**: Does fallback work? Latency <100ms p90? ✓

### **Phase 3: Rollout (June 11–17)**
- **Day 1–2**: Shadow mode (Betfair logs only, no bets)
- **Day 3–4**: Validation mode (Betfair primary, TheOddsAPI validates)
- **Day 5+**: Live (Betfair primary, fallback on error)
- **Monitoring**: Latency, uptime, fallback rate, CLV delta

---

## Three Opus 4.8 Ultracode Prompts

See separate files below. Copy-paste each into `/ultracode`:

1. **PROMPT_1_BETFAIR_REST.md** — REST polling module
2. **PROMPT_2_BETFAIR_WSS.md** — WebSocket streaming module
3. **PROMPT_3_ODDS_AGGREGATOR.md** — Orchestration layer

---

## Configuration

Add to `.env.example`:

```bash
# Betfair (new)
BETFAIR_API_KEY=your_api_key_here
BETFAIR_USERNAME=your_username
BETFAIR_PASSWORD=your_password

# Aggregator (new)
ODDS_SOURCE_PRIMARY=THEODDSAPI  # Stage 1: proven baseline
ODDS_CACHE_TTL_SECONDS=30
ODDS_REST_TIMEOUT_MS=5000
ODDS_FALLBACK_CONSECUTIVE_FAILURES=3

# TheOddsAPI (existing, no change)
ODDS_API_KEY=your_existing_key
```

---

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Latency (p90) | <100ms | Per-API latency tracking in aggregator |
| Betfair latency (p90) | <50ms | WSS: <50ms, REST: 200–400ms |
| Uptime | 99.5% | Monitor fallback activation rate |
| Fallback rate | <5% | Fallback triggered < 5% of calls |
| CLV delta vs market | <1% | Betfair vs Pinnacle comparison |
| Cache hit rate | 75% | Repeated calls within 30s |
| Test coverage | 95% | Unit + integration tests |

---

## Files to Create/Modify

**Create** (4 new):
- `src/wca/data/betfair.py` — REST polling
- `src/wca/data/betfair_stream.py` — WSS streaming
- `src/wca/data/odds_aggregator.py` — Aggregator
- `src/wca/data/betfair_mapping.py` — Static lookups (Betfair IDs → FIFA IDs)

**Modify** (6 files, no breaking changes):
- `src/wca/data/theodossapi.py` — KEEP UNCHANGED (fallback)
- `src/wca/data/__init__.py` — Export aggregator
- `scripts/wca_snapshot_odds.py` — Add --source flag
- `scripts/wca_snapshotd.py` — Add --source flag
- `.env.example` — New env vars
- `tests/test_*.py` — Add unit + integration tests

---

## Execution Checklist

- [ ] **Week 1 (June 4–6)**
  - [ ] Copy PROMPT 1 → `/ultracode` → merge betfair.py
  - [ ] Create betfair_mapping.py stubs (4 empty dicts)
  - [ ] Run tests/test_betfair.py
  - [ ] **GATE**: All 104 WC matches fetch in <30s? 

- [ ] **Week 2 (June 7–10)**
  - [ ] Copy PROMPT 2 → `/ultracode` → merge betfair_stream.py
  - [ ] Copy PROMPT 3 → `/ultracode` → merge odds_aggregator.py
  - [ ] Create tests/test_betfair_stream.py + test_odds_aggregator.py
  - [ ] Run integration tests (snapshot daemon with aggregator)
  - [ ] **GATE**: Failover works? Latency <100ms p90?

- [ ] **Week 3 (June 11–17)**
  - [ ] Deploy to staging, shadow mode (June 11–12)
  - [ ] Switch to validation mode (June 13–14)
  - [ ] Go live: ODDS_SOURCE_PRIMARY=BETFAIR (June 15+)
  - [ ] Monitor: latency, uptime, fallback rate, CLV

---

## Rollback Plan

If Betfair fails:
1. Set `ODDS_SOURCE_PRIMARY=THEODDSAPI` (env var)
2. Restart `wca_snapshotd.py`
3. All bets automatically revert to TheOddsAPI (no data loss)
4. Estimated time: <5 minutes

---

## Risk Mitigation

**Shadow Mode (Day 1–2)**
- Betfair runs in parallel, metrics logged, no odds changes
- Validate Betfair polling works at scale

**Validation Mode (Day 3–4)**
- Betfair becomes primary, TheOddsAPI validates (alerts if delta >5%)
- Monitor CLV tracking vs traditional books
- Detect any data quality issues early

**Live Mode (Day 5+)**
- Betfair primary, fallback on error
- Monitoring dashboard: latency, fallback rate, uptime
- Daily CLV reports (Betfair vs Pinnacle)

---

## Dependencies

- **betfairlightweight** library (Python) — Already in/add to requirements.txt
- **aiohttp** library (Python) — For WSS (async HTTP client)
- **pandas** (existing)
- **requests** (existing)

---

## Next Steps

1. **Read PROMPT 1** (REST polling design)
2. **Read PROMPT 2** (WSS streaming design)
3. **Read PROMPT 3** (Aggregator orchestration design)
4. **Copy PROMPT 1** → `/ultracode` → review output → merge
5. **Copy PROMPT 2** → `/ultracode` → review output → merge
6. **Copy PROMPT 3** → `/ultracode` → review output → merge
7. **Run tests** (should all pass)
8. **Deploy** (shadow → validation → live)

---

## Contact & Support

- **Prompts location**: This directory
- **Questions**: See inline comments in generated code
- **Blockers**: Check mapping tables (betfair_mapping.py) — may need manual population from Betfair docs

**Estimated delivery**: June 11, 2026 (WC2026 group stage kickoff)
