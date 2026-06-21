# Betfair Upgrade — Execution Checklist

## Files Generated (Copy-Paste Ready)

✅ **BETFAIR_UPGRADE_PLAN.md** — Full plan overview  
✅ **PROMPT_1_BETFAIR_REST.md** — REST polling prompt  
✅ **PROMPT_2_BETFAIR_WSS.md** — WebSocket streaming prompt  
✅ **PROMPT_3_ODDS_AGGREGATOR.md** — Aggregator orchestration prompt  

---

## Execution Timeline: 3–4 Days

### **Week 1: June 4–6 (REST Polling)**

- [ ] **Day 1 (June 4)**
  - [ ] Open PROMPT_1_BETFAIR_REST.md
  - [ ] Copy-paste entire prompt → `/ultracode` in Claude Opus 4.8
  - [ ] Wait for output (5–10 min)
  - [ ] Review generated files:
    - `src/wca/data/betfair.py` ← Main REST module
    - `src/wca/data/betfair_mapping.py` ← Mapping stubs (OK if empty)
    - `tests/test_betfair.py` ← Unit tests
  - [ ] Merge into your repo

- [ ] **Day 2 (June 5)**
  - [ ] Run tests: `pytest tests/test_betfair.py -v`
  - [ ] Fix any failures (likely mapping table population)
  - [ ] Populate `betfair_mapping.py`:
    - Get Betfair event IDs for 104 WC2026 matches
    - Get market IDs (h2h, totals, btts)
    - Get selection IDs (Home, Draw, Away, etc.)
  - [ ] Re-run tests → all green ✓

- [ ] **Gate 1: Can poll 104 WC matches in <30s?**
  - [ ] Manual test: `python -c "from wca.data.betfair import get_odds_rest; df, m = get_odds_rest(); print(len(df))"`
  - [ ] Should show 100+ rows (all markets from all matches)
  - [ ] Latency ~200–400ms per call (acceptable baseline)
  - [ ] **PASS?** → Continue to Week 2 ✓

---

### **Week 2: June 7–10 (WSS + Aggregator)**

- [ ] **Day 1 (June 7)**
  - [ ] Open PROMPT_2_BETFAIR_WSS.md
  - [ ] Copy-paste entire prompt → `/ultracode`
  - [ ] Review output:
    - `src/wca/data/betfair_stream.py` ← WebSocket client
    - `tests/test_betfair_stream.py` ← Async tests
  - [ ] Merge into repo
  - [ ] Run tests: `pytest tests/test_betfair_stream.py -v`
  - [ ] Note: Some tests may require mock setup; fix as needed

- [ ] **Day 2 (June 8)**
  - [ ] Open PROMPT_3_ODDS_AGGREGATOR.md
  - [ ] Copy-paste entire prompt → `/ultracode`
  - [ ] Review output:
    - `src/wca/data/odds_aggregator.py` ← Orchestration layer
    - `tests/test_odds_aggregator.py` ← Unit + integration tests
  - [ ] Merge into repo
  - [ ] Run tests: `pytest tests/test_odds_aggregator.py -v`

- [ ] **Day 3 (June 9)**
  - [ ] Integration testing:
    - [ ] Test fallback: Mock Betfair timeout → verify fallback to TheOddsAPI
    - [ ] Test cache: Call twice within 30s → verify cache hit on second call
    - [ ] Test both fail: Mock both sources fail → verify RuntimeError
  - [ ] Update scripts:
    - [ ] `scripts/wca_snapshot_odds.py` — Add `--source` flag
    - [ ] `scripts/wca_snapshotd.py` — Add `--source` flag
    - [ ] `src/wca/data/__init__.py` — Export `OddsAggregator`
  - [ ] Update `.env.example`:
    ```bash
    BETFAIR_API_KEY=your_api_key
    BETFAIR_USERNAME=your_username
    BETFAIR_PASSWORD=your_password
    ODDS_SOURCE_PRIMARY=THEODDSAPI
    ODDS_CACHE_TTL_SECONDS=30
    ODDS_REST_TIMEOUT_MS=5000
    ```

- [ ] **Gate 2: Does fallback work? Latency <100ms p90?**
  - [ ] Manual test:
    ```python
    from wca.data.odds_aggregator import OddsAggregator
    agg = OddsAggregator()
    df, meta = agg.get_odds()
    print(meta["sources_used"])  # Should be ['betfair']
    print(meta["cache_hit"])  # Should be False (first call)
    ```
  - [ ] Measure latency: `time python ...` → should be ~300ms
  - [ ] **PASS?** → Continue to Week 3 ✓

---

### **Week 3: June 11–17 (Rollout)**

#### **Phase 0: Shadow Mode (June 11–12)**
- [ ] Set env: `ODDS_SOURCE_PRIMARY=THEODDSAPI` (baseline, no risk)
- [ ] Deploy to staging
- [ ] Start `wca_snapshotd.py --source THEODDSAPI`
- [ ] Monitor logs: should see "Using primary: theodossapi" every 5s
- [ ] No betting yet (shadow mode = logging only)

#### **Phase 1: Validation Mode (June 13–14)**
- [ ] Set env: `ODDS_SOURCE_PRIMARY=BETFAIR`
- [ ] Deploy to staging
- [ ] Start `wca_snapshotd.py --source BETFAIR`
- [ ] Monitor logs:
  - [ ] Should see "Using primary: betfair"
  - [ ] Latency should drop to <100ms p90
  - [ ] Fallback rate should be <5% (alert if >10%)
- [ ] Watch CLV tracking:
  - [ ] Compare Betfair prices vs Pinnacle (sharp baseline)
  - [ ] Deltas should be <1% (alert if >5%)
- [ ] Run 1–2 WC matches (friendlies or qualifiers if any)

#### **Phase 2: Live (June 15+)**
- [ ] **June 15 (WC2026 Group Stage Kickoff)**
  - [ ] Confirm `ODDS_SOURCE_PRIMARY=BETFAIR` in production .env
  - [ ] Confirm `ODDS_CACHE_TTL_SECONDS=30` (tunable, start here)
  - [ ] Deploy to production
  - [ ] Start `wca_snapshotd.py` (auto-reads BETFAIR from env)

- [ ] **Monitoring Dashboard** (hourly during tournaments):
  - [ ] **Latency**: p50, p90, p99 (target: <100ms p90)
  - [ ] **Uptime**: Betfair availability, WSS reconnect rate
  - [ ] **Fallback rate**: % of calls that triggered fallback (target: <5%)
  - [ ] **CLV tracking**: Betfair vs market (validate fair prices)
  - [ ] **Cache hit rate**: Should be ~75% (good sign)

- [ ] **Daily Post-Match**:
  - [ ] Review CLV on settled matches
  - [ ] Check logs for any errors/fallbacks
  - [ ] Spot-check Betfair prices vs live Pinnacle

---

## Rollback Plan (If Betfair Fails)

**Time to rollback: <5 minutes**

```bash
# In .env (production):
ODDS_SOURCE_PRIMARY=THEODDSAPI

# Restart:
pkill wca_snapshotd.py
python scripts/wca_snapshotd.py --source THEODDSAPI
```

All subsequent `get_odds()` calls automatically revert to TheOddsAPI (no data loss, no bets affected).

---

## Success Metrics

| Metric | Target | Check |
|--------|--------|-------|
| Latency p90 | <100ms | Monitor during matches |
| Betfair latency p90 | <50ms | WSS should be <50ms, REST 200–400ms |
| Uptime | 99.5% | <2.5 hours downtime per week |
| Fallback rate | <5% | Log "fallback_triggered" events |
| CLV delta | <1% | Betfair vs Pinnacle comparison |
| Cache hit rate | 75% | Monitor aggregator.get_odds() calls |
| Test coverage | 95% | `pytest --cov` on test_* files |

---

## Common Issues & Fixes

| Issue | Fix |
|-------|-----|
| "BETFAIR_API_KEY not set" | Add to `.env`: `BETFAIR_API_KEY=...` |
| "Betfair auth failed" | Check API key is valid (test on sandbox first) |
| "Max reconnect attempts exceeded" | WSS network issue; will fallback to REST |
| High fallback rate (>10%) | Check Betfair API status; may be under load |
| Latency >500ms | REST polling suffering; check network, API rate limits |
| Cache hit rate <50% | Odds changing faster than cache TTL; increase TTL or reduce interval |

---

## Contact & Questions

- **Betfair Sandbox**: Create account at https://sandbox.betfair.com (test before prod)
- **betfair_mapping.py**: Betfair event/market IDs for WC2026 (may need manual lookup)
- **Rate limits**: Betfair ~100 req/sec soft limit; TheOddsAPI ~500 req/month
- **Cost**: Commission-only (Betfair) + $30/mo (TheOddsAPI)

---

## Final Checklist Before June 11

- [ ] PROMPT 1 executed & tests passing
- [ ] PROMPT 2 executed & tests passing
- [ ] PROMPT 3 executed & tests passing
- [ ] betfair_mapping.py populated with WC2026 IDs
- [ ] .env configured with BETFAIR_* env vars
- [ ] --source flag added to snapshot scripts
- [ ] Integration tests pass
- [ ] Monitoring dashboard set up
- [ ] Rollback plan documented & tested
- [ ] Team briefed on ODDS_SOURCE_PRIMARY env var (how to toggle)

**Ready for June 11 group stage kickoff!** 🎉
