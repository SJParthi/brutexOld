# Track 1 — Groww Adapter Specification

**Status:** PROPOSED (docs-only PR; awaiting Phase 0 probe + operator approval before any adapter code lands)
**Decision ID:** `DEC-VENDOR-PIVOT-001` (ADR-0001)
**Date:** 2026-05-26
**Replaces (when merged):** the prior broker adapter as the spec-locked backtesting data source

---

## 1. Scope

This document specifies the Groww Trade API adapter for BruteX Track 1 backtesting. It covers:

1. Authentication (TOTP only)
2. Instrument addressing (`groww_symbol` strings vs prior-broker int securityIds)
3. Historical OHLC fetching (1m, 1d, 1w, 1mo intervals)
4. Adaptive rate-limiter auto-tuning algorithm
5. Price-unit normalization (rupees → int64 paisa)
6. Error taxonomy (11 Groww exception classes mapped to BruteX retry/quarantine/alert policy)
7. Audit log `vendor: groww` stamp
8. Z+ defensive layers (DETECT / VERIFY / RECONCILE / PREVENT / AUDIT / RECOVER / COOLDOWN)
9. Universe constants (200 stocks + 4 indexes + ~46 futures = ~250 instruments)
10. Phased rollout sequence

**Out of scope (this spec):** Options (Track 2 / future), live WebSocket feed (Track 1 §1 explicit OUT-OF-SCOPE per CLAUDE.md), order placement (signal-only research).

---

## 2. Authentication — TOTP only

Per operator decree 2026-05-26 + CLAUDE.md §11 AUTOMATED rule ("ZERO clicks per day"), only the TOTP flow is accepted. The API-Key+Secret flow (Groww Approach 1, requires daily approval) is REFUSED.

### 2.1 Required env vars

| Env var | Type | Default | Purpose |
|---|---|---|---|
| `BRUTEX_GROWW_API_KEY` | string | — | TOTP token (16-byte base32 secret) |
| `BRUTEX_GROWW_TOTP_SECRET` | string | — | TOTP secret (32-char base32) |
| `BRUTEX_GROWW_TOKEN_PATH` | path | `~/.brutex/groww/access_token.json` | Cached access_token file (mode 0600, mirrors prior-broker pattern) |
| `BRUTEX_GROWW_SMOKE` | flag | unset (off) | Set to `1` to enable smoke tests against real Groww API |

### 2.2 Auth lifecycle

```
on first call → use API_KEY + TOTP_SECRET → generate TOTP → groww.get_access_token() → cache to TOKEN_PATH
on subsequent calls → load cached access_token from TOKEN_PATH
on 401 / GrowwAPIAuthenticationException → regenerate TOTP → refresh access_token → retry (max 3)
on 3rd consecutive auth failure → CRITICAL Telegram alert + halt fetcher
```

Mirrors the prior-broker `auth.py:60` `REQUIRED_FILE_MODE = 0o600` pattern (file removed in DEC-VENDOR-PIVOT-001 wipe; see MIGRATION.md entry #247).

---

## 3. Instrument addressing

Groww uses `groww_symbol` STRING tokens, not integer securityIds. The adapter MUST map BruteX logical instrument names to Groww symbols at the request boundary.

### 3.1 Track 1 brute-force universe (4 instruments, hardcoded)

| Logical name | Prior broker (legacy) | Groww (proposed; pending Phase 0 verification) |
|---|---|---|
| NIFTY 50 | securityId=13, IDX_I | `groww_symbol="NSE-NIFTY"`, segment=CASH |
| BANKNIFTY | securityId=25, IDX_I | `groww_symbol="NSE-BANKNIFTY"`, segment=CASH |
| SENSEX | securityId=51, IDX_I | `groww_symbol="BSE-SENSEX"`, segment=CASH |
| INDIA VIX | securityId=21, IDX_I | `groww_symbol="NSE-INDIAVIX"` (UNVERIFIED — Phase 0 gate) |

### 3.2 LAKE-only universe (250 instruments per DEC-P0-063)

200 NSE F&O eligible stocks + 4 indexes + ~46 futures contracts. Loaded from `growwapi-assets.groww.in/instruments/instrument.csv` at startup. Cached locally with 24-hour TTL.

---

## 4. Historical OHLC fetching

### 4.1 Endpoint mapping

| BruteX call | Groww SDK call |
|---|---|
| `fetch_intraday(instrument, start, end, interval="1minute")` | `groww.get_historical_candles(exchange=NSE, segment=CASH, groww_symbol=..., start_time=..., end_time=..., candle_interval=CANDLE_INTERVAL_MIN_1)` |
| `fetch_daily(instrument, start, end)` | `groww.get_historical_candles(..., candle_interval=CANDLE_INTERVAL_DAY)` |

### 4.2 Chunk-window limits (verified from operator's paste of Groww docs)

| Interval | Max range per single call | BruteX chunk planner setting |
|---|---|---|
| 1m / 2m / 3m / 5m | 30 days | 30 days |
| 10m / 15m / 30m | 90 days | 90 days |
| 1h / 4h / 1d / 1w / 1mo | 180 days | 180 days |

Note: this REPLACES the prior-broker `INTRADAY_MAX_WINDOW_DAYS = 90` constant.

### 4.3 Response shape

```python
{
  "candles": [
    [timestamp_str, open, high, low, close, volume, oi_or_null],
    ...
  ],
  "closing_price": float,
  "start_time": "yyyy-MM-dd HH:mm:ss",
  "end_time": "yyyy-MM-dd HH:mm:ss",
  "interval_in_minutes": int,
}
```

**CRITICAL: prices are in RUPEES (float), not paisa.** Parser MUST apply `int(round(price_rupees * 100))` at the boundary to preserve BruteX's int64 paisa invariant (CLAUDE.md §4 mandate).

---

## 5. Adaptive Rate-Limiter Auto-Tuning Algorithm

**Operator decree 2026-05-26:** the rate limiter MUST auto-discover the highest sustainable rate within [4, 8] r/s, stepping down on rate-limit observations and stepping up after sustained clean operation.

### 5.1 Parameters

| Parameter | Default | Env-overridable |
|---|---|---|
| `MIN_RATE_RPS` | 4.0 | `BRUTEX_GROWW_MIN_RPS` |
| `MAX_RATE_RPS` | 8.0 | `BRUTEX_GROWW_MAX_RPS` |
| `INITIAL_RATE_RPS` | 8.0 | `BRUTEX_GROWW_INITIAL_RPS` |
| `STEP_DOWN_HITS` | 3 | `BRUTEX_GROWW_STEP_DOWN_HITS` |
| `STEP_DOWN_WINDOW_SEC` | 60.0 | `BRUTEX_GROWW_STEP_DOWN_WINDOW` |
| `STEP_UP_CLEAN_SEC` | 300.0 (5 min) | `BRUTEX_GROWW_STEP_UP_CLEAN_WINDOW` |
| `RECHECK_INTERVAL_SEC` | 3600.0 (1 hr) | `BRUTEX_GROWW_RECHECK_INTERVAL` |

### 5.2 State machine

```
States: PROBING_DOWN | LOCKED | PROBING_UP | FLOOR_BREACHED

initial state = PROBING_DOWN
initial current_rate = INITIAL_RATE_RPS (8.0)

Transitions:

  PROBING_DOWN
    on (rate_limit_hits >= STEP_DOWN_HITS within STEP_DOWN_WINDOW_SEC):
      → current_rate -= 1.0
      → reset hit counter
      → if current_rate < MIN_RATE_RPS: FLOOR_BREACHED
      → else: PROBING_DOWN (continue)

    on (clean_duration >= STEP_UP_CLEAN_SEC):
      → LOCKED at current_rate

  LOCKED
    on (rate_limit_hits >= STEP_DOWN_HITS within STEP_DOWN_WINDOW_SEC):
      → PROBING_DOWN
      → step down by 1.0

    on (RECHECK_INTERVAL_SEC elapsed since lock):
      → if current_rate < MAX_RATE_RPS:
          → PROBING_UP (attempt current_rate + 1.0)

  PROBING_UP
    on (rate_limit_hits >= 1 within STEP_DOWN_WINDOW_SEC):
      → rollback to previous rate
      → LOCKED (mark as stable; do not retry up for 24 hours)

    on (clean_duration >= STEP_UP_CLEAN_SEC):
      → LOCKED at higher rate

  FLOOR_BREACHED
    → CRITICAL Telegram alert
    → halt fetcher (no new requests)
    → operator intervention required (manual unblock via env var bump or Groww support)
```

### 5.3 Observability

Every state transition appends an audit JSONL entry:

```json
{"seq": N, "ts_utc": ..., "actor": "groww_rate_limiter", "action": "step_down",
 "subject": "rate_limiter", "payload": {"from_rps": 8.0, "to_rps": 7.0,
   "trigger": "3_hits_in_60s", "hits_count": 3, "window_sec": 60.0}}
```

Each transition also fires a Telegram event:
- `step_down` → INFO
- `step_up` → INFO
- `lock` → INFO
- `floor_breached` → CRITICAL

### 5.4 Why not start at 4 r/s and probe up?

The operator's directive is **"start from 8 r/s and low-grade to 4 r/s"**. Starting high is correct because:
1. Groww docs publish 10 r/s for Live Data bucket (8 is 80% of that — within safety margin)
2. Sustained 1.5-hour bulk download benefits from highest possible rate
3. Step-down is fast (1 second window of 3 errors → 1 s/s slower); step-up is slow (5 min clean) → asymmetric optimism with downside protection

### 5.5 Worst-case timing

| Scenario | Effective rate | First-run (~20K calls) |
|---|---|---|
| 8 r/s sustained (no errors ever) | 8 r/s | ~42 min |
| Probe 8→7→6→5 stable (3 step-downs in first 4 min) | 5 r/s | ~67 min |
| 4 r/s sustained (worst case before alert) | 4 r/s | ~83 min |
| Floor breached at 4 r/s | halted | operator unblock needed |

---

## 6. Error taxonomy mapping

Groww SDK exposes 11 exception classes (`brutex/adapters/groww/groww_errors.py` will mirror this).

| Groww exception | BruteX policy | Telegram |
|---|---|---|
| `GrowwAPIRateLimitException` | Backoff (2^n, max 60s) + retry; increment rate-limiter hit counter | INFO after 3 retries |
| `GrowwAPITimeoutException` | Backoff + retry up to 10× | WARN after 3 |
| `GrowwAPIAuthenticationException` | Halt; refresh TOTP; retry max 3; then CRITICAL | CRITICAL |
| `GrowwAPIAuthorisationException` | Halt — account suspended or scope error | CRITICAL |
| `GrowwAPIBadRequestException` | Quarantine chunk; do not retry (request shape bug) | WARN |
| `GrowwAPINotFoundException` | Suspicious — instrument symbol may have changed; quarantine + manual review | WARN |
| `GrowwFeedException` | n/a (live feed out of scope) | — |
| `GrowwFeedConnectionException` | n/a | — |
| `GrowwFeedNotSubscribedException` | n/a | — |
| `GrowwAPIException` (base) | Catch-all; quarantine + retry once | WARN |
| `GrowwBaseException` (root) | Should never reach this; if hit → CRITICAL bug | CRITICAL |

---

## 7. Z+ Defensive Layers — zero-data-loss guarantee

| Layer | Mechanism | File / Module |
|---|---|---|
| **L1 DETECT** | Rate-limiter probes + per-call metrics + chunk completeness verifier | `brutex/adapters/groww/client.py` + `brutex/observability/metrics.py` |
| **L2 VERIFY** | Bit-exact golden fixtures + protocol-conformance contract test | `tests/golden/test_groww_constants_frozen.py` + `tests/integration/test_protocol_conformance.py` |
| **L3 RECONCILE** | Daily reconciliation: for each (instrument, trading_day), compare expected bar count vs lake. Gap → re-queue + Telegram WARN | `brutex/orchestrator/replay.py` (existing oracle, extended) |
| **L4 PREVENT** | Bloom + LMDB + audit JSONL hash-chain — already in place, broker-agnostic | `brutex/orchestrator/{bloom, store, audit}.py` |
| **L5 AUDIT** | Every chunk fetch = 1 audit JSONL line with `vendor: groww` stamp; replay oracle verifies lake↔audit on each MonitorLoop tick | `brutex/orchestrator/audit.py` + `replay.py` |
| **L6 RECOVER** | Process crash → audit log replay on startup → resume from last completed WorkItem (idempotent re-fetch) | `brutex/orchestrator/pipeline.py` (existing) |
| **L7 COOLDOWN** | Auto-resume after rate-limit recovery + OPERATOR_OVERRIDE rate-limited per DEC-P0-008 pattern | `brutex/orchestrator/rate_limiter.py` (existing + adaptive ext) |

**Key invariant:** even with 4 r/s throttle, account suspension, network down, or process crash — every (instrument, ts) bar tuple is FETCHED and STORED at least once. Idempotent re-fetch is safe because content_hash + Bloom + LMDB collapse duplicates.

---

## 8. Daily reconciliation pass (gap detection)

Runs every day at 09:00 IST via cron / `brutex monitor`:

```
for instrument in universe (250):
  for day in last_30_trading_days:
    expected = session_minutes(instrument, day)   # 375 for indexes, 375 for stocks
    actual = lake.count_bars(instrument, day)

    if actual == expected:
      OK
    elif actual == 0 and day in known_holidays:
      OK (no trading)
    elif actual < expected:
      gap_minutes = expected - actual
      # Sub-check: was there a market halt that minute? (NSE halt log lookup)
      if gap matches known halt:
        OK
      else:
        queue_gap_fill(instrument, day, gap_minutes)
        Telegram WARN
```

Output → `~/.brutex/reconciliation/<date>.json` + Telegram daily summary.

---

## 9. Smoke test gates

| Test | When | Triggered by |
|---|---|---|
| `tests/smoke/test_groww_auth_smoke.py` | `BRUTEX_GROWW_SMOKE=1` | Manual + CI nightly |
| `tests/smoke/test_groww_intraday_real.py` | `BRUTEX_GROWW_SMOKE=1` | Manual + CI nightly |
| `tests/smoke/test_groww_securityid_verification.py` | `BRUTEX_GROWW_SMOKE=1` | Quarterly cron |
| `tests/smoke/test_groww_vix_present.py` | `BRUTEX_GROWW_SMOKE=1` | **Phase 0 gate; nightly** |

---

## 10. Phased rollout

(Mirrors ADR-0001 §5 phased PR sequence.)

| Phase | PR scope | Lines |
|---|---|---|
| 0a | This doc + ADR + MIGRATION entry | docs-only |
| 0b | `scripts/groww_probe.py` + transcripts | ~250 + docs |
| 1 | Auth + constants + types | ~480 |
| 2 | Body + parser + client + adaptive rate limiter | ~600 |
| 3 | SSRF guard + timeconv + GrowwMarketAdapter (protocol impl) | ~470 |
| 4 | Worker + errors taxonomy | ~700 |
| 5 | `brutex/adapters/_common/` chunk-helper extract | ~300 (refactor) |
| 6 | CLI flip + `BRUTEX_BROKER` dispatcher | ~250 |
| 7 | Spec doc amendments + CLAUDE.md §6 replaced | ~600 docs |
| 8 | Delete prior broker adapter (5,659 LoC) — gated on git tag `v-pre-groww-pivot` | -5,659 |

Total new code: ~3,200 LoC (Groww adapter + tests + workers). Total deleted: 5,659 LoC (prior broker).

---

## 11. Open questions resolved by operator on 2026-05-26

| # | Question | Operator's answer |
|---|---|---|
| 1 | Full pivot (Option C) or multi-source (Option D)? | **Full pivot C — only Groww, only backtesting** |
| 2 | Send Groww support email to confirm bucket / VIX / fair-use? | **No** — empirical probe only |
| 3 | Rate-limiter range? | **[4, 8] r/s, adaptive auto-tune** |
| 4 | "Multiple hits" threshold for step-down? | **3 hits within 60s** (default) |
| 5 | "Clean window" for step-up? | **5 minutes of zero errors** (default) |
| 6 | Major design changes + Z+ defensive layers + all checks? | **Yes, mandatory** |
| 7 | Universe scope? | **250 instruments (200 stocks + 4 indexes + ~46 futures, NO options yet)** |
| 8 | Intervals? | **1m, 1d, 1w, 1mo** |
| 9 | Date range? | **2020-01-01 → today** (Groww's documented depth) |
| 10 | Reproducibility lineage break? | **Accepted** — git tag `v-pre-groww-pivot` precedes deletion |

---

## 12. Honest envelope (Golden Rule #10)

- ✅ REAL: 4-agent hostile review completed (Plan + Explore + Red-team + Compliance); transcripts archived; operator override per DEC-P0-005 precedent
- ✅ REAL: Adaptive rate-limiter algorithm fully specified with state machine + Telegram observability
- 🟡 PARTIAL: INDIA VIX presence on Groww UNVERIFIED — Phase 0b probe is the gate
- 🟡 PARTIAL: Backtesting endpoint rate-limit bucket assignment UNVERIFIED — adaptive limiter mitigates
- 🟡 PARTIAL: Data drift magnitude UNQUANTIFIED — Phase 0b bar-by-bar diff is the gate
- ❌ CERTAIN: Reproducibility lineage break (vendor swap = new `data_snapshot_hash`). Mitigated by git tag + archived audit chain
- 📋 ROADMAP: Options support deferred (Track 2 future ADR); live WebSocket out of scope (CLAUDE.md §1)
