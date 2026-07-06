# Track 1 — Groww Adapter Specification

> **📜 HISTORICAL DESIGN RECORD (banner refreshed 2026-07-03).** The live
> Track 1 vendor is **Groww** again per **DEC-DHAN-GROWW-REMIGRATE-001 /
> ADR-0009 (2026-06-12)**, which supersedes the ADR-0002 Dhan revert this
> banner previously announced (`docs/TRACK1_DHAN_ADAPTER.md` was REMOVED in
> the ADR-0009 §5 cutover). This document remains the ORIGINAL design spec,
> kept for history — it predates the remigration, so its quotas/flows are
> NOT the live contract. Do NOT build against this document: the live
> adapter contract is **CLAUDE.md §6 + `brutex/adapters/groww/`** (e.g. the
> "100K/day quota" below is superseded — operator-confirmed **NO daily
> quota**, 500 req/min hard governor, DEC-GROWW-RATE-PERMIN-001).

**Status:** HISTORICAL record (was: PROPOSED → superseded by ADR-0002 → vendor re-migrated per ADR-0009). Live vendor is **Groww**; live contract is CLAUDE.md §6.
**Decision ID:** `DEC-VENDOR-PIVOT-001` (ADR-0001, SUPERSEDED by ADR-0002)
**Date:** 2026-05-26 (superseded 2026-05-31)
**Replaces (when merged):** the prior broker adapter as the spec-locked backtesting data source

> **Design update — 2026-05-28 (DEC-GROWW-ADAPTER-DESIGN-001):** §1–§12 are the
> original PROPOSED spec. The **candle surface has since shipped** (the resilient
> fetch skeleton — watermark, loop, chunk planner, parser, client, persist;
> MIGRATION.md entries #282–#287). The operator then asked for a precise,
> reviewable design covering **all five surfaces** (instruments · auth · expiries
> · contracts · candles) with **every worst-case / error / exception** handled and
> Z+ layers per surface, **before** the remaining code lands. That design is
> **§13** below — read + approve §13, then implementation proceeds in the §13.9
> sequence. §13 supersedes §10's rollout where they differ.

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

> **⚠️ RETIRED (DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065):** the first three rows below
> are the ORIGINAL spec's env-var/file design and are NO LONGER the credential contract —
> the api-key, TOTP secret AND the daily access token now live ONLY in AWS Parameter
> Store (SecureStrings `/tickvault/prod/groww/{api-key,totp-secret,access-token}`, region
> `ap-south-1`; fail-fast, no env/file fallback). Live reference:
> `docs/runbooks/ENV_VARS.md` §5 + `docs/runbooks/aws-token-broker.md`. The table is
> kept as the historical spec record.

| Env var | Type | Default | Purpose |
|---|---|---|---|
| `BRUTEX_GROWW_API_KEY` (RETIRED) | string | — | TOTP token (16-byte base32 secret) |
| `BRUTEX_GROWW_TOTP_SECRET` (RETIRED) | string | — | TOTP secret (32-char base32) |
| `BRUTEX_GROWW_TOKEN_PATH` (RETIRED) | path | `~/.brutex/groww/access_token.json` | Cached access_token file (mode 0600, mirrors prior-broker pattern) |
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
| `GrowwAPIAuthorisationException` | Quarantine the ONE symbol (terminal `GrowwAuthorisationError` — an entitlement restriction is per-instrument, never a pull-wide stop); ≥ threshold (default 5, `BRUTEX_GROWW_AUTHZ_STORM_MIN_SYMBOLS`) DISTINCT authz-rejected symbols with ZERO successes escalates fleet-wide → `GrowwAuthorisationStormError` HALT (a real account suspension hits every symbol) — DEC-GROWW-AUTHZ-QUARANTINE-001 | WARN per quarantined symbol; CRITICAL on the storm HALT |
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

---

## 13. Comprehensive Per-Surface Fetcher Design (DEC-GROWW-ADAPTER-DESIGN-001)

**Status:** PROPOSED for operator review (2026-05-28). No code lands from §13 until approved.
**Why:** the operator's directive — *"instruments, authentication, contracts, expiries, and candle historical fetch entirely … worst-case scenarios, errors, exceptions, situations, bugs, cases … everything should be taken care of, only then go ahead with precise design and implementation."* §13 is that precise design.

### 13.0 Design principle — ONE resilient spine, five thin fetchers

The single most important design decision: **we do NOT write five separate error-handling stacks.** Every Groww surface is a *thin fetcher* that rides the **same spine** the candle surface already proved. "Handle every worst-case for expiries/contracts/instruments" reduces to *"wrap the Groww call and route it through the spine."*

| Spine component | Guarantee it provides | Module | Status |
|---|---|---|---|
| **Pacer** (token bucket) | never exceed the rate; auto down/up-shift on 429s | `orchestrator/rate_limiter.py::AdaptiveTokenBucket` + `adaptive_rate_limiter.py` | ✅ built |
| **Retry + backoff** | transient errors retry on an exponential ladder, then quarantine — never an infinite hammer, never a silent give-up | `orchestrator/retry.py::backoff_delay` + `fetch_loop.run_fetch` | ✅ built |
| **Error translator** | all 11 `Groww*Exception` → neutral `Vendor*` → `Outcome` (retry / re-auth / quarantine / settle) | `adapters/groww/client.py` + `orchestrator/retry.py::classify_exception` | ✅ built |
| **Watermark** | crash-safe resume; settled work (COMPLETED / NO_DATA) never re-pulled | `data/watermark.py::FetchWatermark` | ✅ built |
| **Audit** | every action = one hash-chained JSONL line (`vendor: groww`) | `orchestrator/audit.py` | ✅ built |
| **Quota** | the 100K/day ceiling halts cleanly, resumes next IST day *(HISTORICAL — operator-confirmed 2026-06-13: backtesting has **NO daily quota**, DEC-GROWW-RATE-PERMIN-001; the live build sites wire `quota_tracker=None`)* | `orchestrator/quota_tracker.py` | ✅ built (wiring pending) |
| **Sink / Store** | parsed records land idempotently (Bloom + content-hash dedup) | `data/sink.py` (bars) · `data/instrument_store.py` (instruments/expiries/contracts) | ✅ built |

**The template is `GrowwCandleFetcher`** (`adapters/groww/client.py`): pace → call SDK → extract → translate errors → return. Each new surface is the same five steps against a different SDK method, feeding a different store. This is CLAUDE.md §11 **COMMON** (one spine, no copy-paste) + **DYNAMIC** (add a surface without re-architecting).

### 13.1 Implementation status — what the five surfaces look like TODAY (post-#287)

| # | Surface | SDK method / source | Storage target | Fetcher module | Status |
|---|---|---|---|---|---|
| A | **Authentication** | `GrowwAPI.get_access_token(api_key, totp)` | token file (mode 0600) | *(inline in `client.from_env`)* | 🟡 thin — works, but not hardened/extracted |
| B | **Instrument master** | CSV @ `growwapi-assets.groww.in/instruments/instrument.csv` | `InstrumentStore.upsert_many` | — | 🔴 store built; **fetcher missing** |
| C | **Expiries** | `groww.get_expiries(exchange, underlying, year, month)` | `InstrumentStore.upsert_expiries` | — | 🔴 `ExpiryRecord` + upsert built; **fetcher missing** |
| D | **Contracts** | `groww.get_contracts(exchange, underlying, expiry_date)` | `InstrumentStore.upsert_contracts` | — | 🔴 `ContractRecord` + upsert built; **fetcher missing** |
| E | **Historical candles** | `groww.get_historical_candles(...)` | `ParquetBarSink` + catalog | `client.py` + `parser` + `chunks` + `fetch_loop` + `persist` | ✅ **DONE** (#282–#287) |

**So the remaining build is 4 thin fetchers (A harden, B/C/D new) + one `brutex fetch` orchestration that wires them.** No spine work remains.

### 13.2 Surface A — Authentication (TOTP), hardened

**Target module:** `brutex/adapters/groww/auth.py` (extract from `client.from_env`).

**Design — `GrowwAuth`:**

```
GrowwAuth(api_key, totp_secret, token_path, *, clock=time.time, now_factory=...)
  .session() -> GrowwAPI         # cached token if fresh; else mint + cache
  .refresh() -> GrowwAPI         # force re-mint (the loop's reauth hook)
```

| Concern | Design decision |
|---|---|
| Token caching | Mint via `pyotp.TOTP(secret).now()` → `get_access_token` → write `token_path` (mode **0600**, dir 0700; verify mode on read, refuse world-readable). |
| Refresh-on-401 | The fetch loop already calls `reauth` on `AUTH_FAILED`; `GrowwAuth.refresh` re-mints. Max 3 consecutive (§2.2); 3rd → CRITICAL + halt. |
| 6 AM / token-expiry | **UNVERIFIED** (docs say "No Expiry"). Design copes *reactively*: any 401 → refresh. A proactive pre-6 AM refresh is added ONLY if the Phase 0b probe observes expiry. |
| Clock skew (TOTP) | TOTP is time-based; on `AUTH_FAILED` immediately after mint, retry once after a 1-step (30 s) wait before counting it against the 3-retry budget. |
| Secret hygiene | Secrets only from env; never logged (use `BruteXError(safe_message=...)`); token file is the only at-rest artifact. |
| Concurrency | One `GrowwAuth` per process; `refresh()` guarded by a lock so parallel workers don't mint duplicate tokens (thundering herd). |

**Spine wiring:** auth is upstream of the spine (every surface needs a session). `refresh` is the loop's `reauth=` callback.
**Tests (proof obligations):** token-file create/read/round-trip · mode-0600 enforced (refuse 0644) · refresh re-mints · 3-fail→halt · clock-skew single-retry · secret never in `str(exc)`. Live mint is `BRUTEX_GROWW_SMOKE`-gated (pragma no-cover).

### 13.3 Surface B — Instrument master (CSV fetch)

**Target module:** `brutex/adapters/groww/instruments.py` (the store at `data/instrument_store.py` already persists; only the **fetch + parse + normalise** is missing).

**Design — `InstrumentMasterFetcher`:**

```
InstrumentMasterFetcher(http_get, store, *, ttl_hours=24, csv_url=DEFAULT)
  .refresh(*, force=False) -> InstrumentRefreshResult
     # if cache fresh (<ttl) and not force → no-op (return cached count)
     # else: GET csv_url → parse rows → normalise to Instrument → upsert_many → stamp fetched_at
```

| Concern | Design decision |
|---|---|
| Download | `http_get` injected (real = `urllib`/`httpx` with **SSRF guard**: only the documented `growwapi-assets.groww.in` host, https, no redirects to other hosts). Tests inject a fake returning canned CSV bytes. |
| Schema validation | Validate the header row against the documented columns (§02-instruments). Missing/renamed column → `InstrumentFormatError` → **abort refresh, keep last good cache** (never half-load). |
| Row normalisation | Reuse `models.normalise_tick_size` + `Instrument`; a bad row → quarantine that row (count + log), don't fail the whole file (one corrupt row ≠ no instruments). |
| Scope tagging | Set `in_scope` via `universe.is_in_fetch_scope` (two-tier: all NSE F&O + indices + F&O-eligible equities + SENSEX). |
| 24 h TTL + fallback | Fresh cache → skip network. On any network/parse failure with a cache present → **serve stale + Telegram WARN** (never block fetches on a transient CSV hiccup). No cache + failure → CRITICAL + halt (can't address symbols). |
| Atomicity | `upsert_many` is transactional (instrument_store); a failed parse never commits a partial master. |

**Spine wiring:** one paced `http_get` (counts against quota); retried/translated like any call; the refresh is itself a single "work item" in the audit log.
**Tests:** fresh-cache no-op · forced refresh parses+upserts · schema-drift aborts+keeps-cache · one-bad-row quarantined+rest-loaded · network-fail-with-cache serves stale+WARN · network-fail-no-cache halts · SSRF guard rejects off-host URL.

### 13.4 Surface C — Expiries discovery (F&O lake only)

**Target module:** `brutex/adapters/groww/expiries.py`. **Only needed for the 200-stock F&O lake — the 3 brute-force indices are CASH and need NO expiries.**

**Design — `ExpiryFetcher`:**

```
ExpiryFetcher(sdk, bucket, base_exception, store)
  .fetch(underlying, exchange, *, year, month=None) -> list[ExpiryRecord]
     # pace → sdk.get_expiries(...) → validate ISO dates → ExpiryRecord[] → store.upsert_expiries
```

| Concern | Design decision |
|---|---|
| Mirrors the candle client | Same pace → call → translate pattern; reuses `client._neutral_for` (same `Groww`→`Vendor` translation) so the loop classifies expiry errors identically. |
| Year-range guard | Clamp/validate `year ∈ [2020, current+1]` (docs: FNO from 2020) **before** the call → an out-of-range request becomes a local `bad_request`, no wasted quota. |
| Empty list = OK | `{"expiries": []}` is **not an error** — a symbol may have no live contracts. Record NO_DATA, settle, move on. |
| Date-format check | Each returned string must parse as `YYYY-MM-DD`; a malformed date → quarantine that symbol-year (surfaced), don't poison the store. |
| Historical expiries | **UNVERIFIED** whether `get_expiries` returns *past* expiries (Phase 0b gate). Design backfills year-by-year from 2020; if past years 404, the probe tells us and we switch to "current + forward only." |

**Tests:** happy path upserts records · empty list → NO_DATA · year<2020 → bad_request (no SDK call) · malformed date → quarantine · rate-limit → translated+retried (integration).

### 13.5 Surface D — Contracts discovery (F&O lake only)

**Target module:** `brutex/adapters/groww/contracts.py`. Depends on Surface C (needs an expiry date).

**Design — `ContractFetcher`:**

```
ContractFetcher(sdk, bucket, base_exception, store)
  .fetch(underlying, exchange, expiry_date) -> list[ContractRecord]
     # pace → sdk.get_contracts(...) → validate symbols → ContractRecord[] → store.upsert_contracts
```

| Concern | Design decision |
|---|---|
| Pre-validation | The `expiry_date` should come from a stored `ExpiryRecord` (Surface C), so we never request a non-existent expiry; a stray 404 still → quarantine + WARN. |
| Empty list = OK | A valid expiry with no contracts (e.g. an index with no options) → NO_DATA, settle. |
| Symbol shape guard | Each contract symbol must match `EXCHANGE-SYMBOL-EXPIRY[-STRIKE]-(CE\|PE\|FUT)` (§04-annexures); a malformed symbol → quarantine that contract, keep the rest. |
| Permissions | If the instrument master flags `buy_allowed/sell_allowed = false`, the contract is recorded but flagged (suspended) so the candle backfill can skip it cleanly. |
| Feeds candle fetch | The resulting contract `groww_symbol`s are exactly the inputs to the existing `GrowwCandleFetcher` (segment auto-derives to FNO via `symbol_to_exchange_segment`). |

**Tests:** happy path upserts · empty → NO_DATA · expiry-not-found → quarantine+WARN · malformed symbol → quarantine+rest-kept · suspended flag carried.

### 13.6 Surface E — Historical candles (✅ DONE, for reference)

Shipped in #282–#287. The design is the live template for A–D:
`chunks.plan_chunks` (30/90/180-day windows) → `GrowwCandleFetcher.fetch_chunk` (pace + call + translate) → `PersistingFetcher` (parse + OHLC-guard + sink) → `run_fetch` (retry + watermark + reauth) → `ParquetBarSink` (idempotent Parquet + catalog). Price-unit (rupees→paisa, §4.3) is the one open follow-up flagged in #285.

### 13.7 The full failure-mode matrix (34 modes — "everything taken care of")

Every row's **spine outcome** is one of: `RETRY` (backoff ladder) · `QUARANTINE` (terminal, surfaced for review, no retry) · `NO_DATA` (legitimately empty, settle, never re-pull) · `REAUTH` (refresh token, retry once) · `HALT` (CRITICAL alert, stop — operator) · `PREVENT` (caught locally before the call, no quota spent).

**Surface A — Authentication (5)**

| # | Failure mode | Trigger | Spine outcome | Z+ |
|---|---|---|---|---|
| A1 | Invalid / corrupt TOTP secret | `GrowwAPIAuthenticationException` | REAUTH → 3× → HALT+CRITICAL | L7 |
| A2 | Token file corrupt / unreadable | file I/O / parse error | re-mint via TOTP | L6 |
| A3 | 6 AM / token rollover (UNVERIFIED) | mid-run 401 | REAUTH (reactive); proactive only if probe confirms | L7 |
| A4 | Network down during auth | `GrowwAPITimeoutException` | RETRY 3× / use cache | L6 |
| A5 | Entitlement-restricted instrument / account suspension | `GrowwAPIAuthorisationException` | QUARANTINE (single symbol — terminal, WARN, re-pullable) → fleet HALT+CRITICAL only at ≥ 5 distinct authz-rejected symbols with zero successes (`GrowwAuthorisationStormError`, DEC-GROWW-AUTHZ-QUARANTINE-001) | L7 |

**Surface B — Instrument master (6)**

| # | Failure mode | Trigger | Spine outcome | Z+ |
|---|---|---|---|---|
| B1 | CSV fetch timeout / network | `GrowwAPITimeoutException` | RETRY; fallback to cache | L6 |
| B2 | CSV schema drift (col renamed/missing) | `InstrumentFormatError` | abort refresh, KEEP cache, WARN | L2/L3 |
| B3 | Empty CSV (0 rows) | data-level | fallback cache; no-cache→HALT | L3 |
| B4 | Symbol absent at request time | `GrowwAPINotFoundException` | pre-flight lookup; QUARANTINE unknown | L1 |
| B5 | Stale cache + new contracts listed | staleness | 24 h TTL refresh + age-check | L3 |
| B6 | Price unit rupees-vs-paisa | silent scale error | ×100 at parse boundary (mandatory) | L2 |

**Surface C — Expiries (6)**

| # | Failure mode | Trigger | Spine outcome | Z+ |
|---|---|---|---|---|
| C1 | Empty expiries list | data-level | NO_DATA (OK) | L1 |
| C2 | Year out of `[2020, current+1]` | local guard / `GrowwAPIBadRequestException` | PREVENT (local) | L4 |
| C3 | Malformed expiry date string | data-level | QUARANTINE symbol-year | L1/L2 |
| C4 | Underlying not found | `GrowwAPINotFoundException` | pre-check master; QUARANTINE | L1 |
| C5 | Incomplete list (fewer than expected) | data-level | accept (no completeness contract); WARN if anomalous | L3 |
| C6 | Past expiries unavailable (UNVERIFIED) | `GrowwAPINotFoundException` | Phase 0b gate → forward-only fallback | L1 |

**Surface D — Contracts (5)**

| # | Failure mode | Trigger | Spine outcome | Z+ |
|---|---|---|---|---|
| D1 | Expiry not found for symbol | `GrowwAPINotFoundException` | pre-validate from `ExpiryRecord`; QUARANTINE+WARN | L1 |
| D2 | Empty contracts list | data-level | NO_DATA (OK) | L1 |
| D3 | Malformed contract symbol | data-level | QUARANTINE that contract, keep rest | L2 |
| D4 | buy/sell disallowed (suspended) | data-level (permissions) | record + flag; candle backfill skips | L1 |
| D5 | Rate-limit mid-enumeration | `GrowwAPIRateLimitException` | pace-down + RETRY | L7 |

**Surface E — Historical candles (12)**

| # | Failure mode | Trigger | Spine outcome | Z+ |
|---|---|---|---|---|
| E1 | Range exceeds 30/90/180-day window | `GrowwAPIBadRequestException` | PREVENT (chunk planner) | L4 |
| E2 | start ≥ end | `GrowwAPIBadRequestException` | PREVENT (planner yields []) | L4 |
| E3 | Bad timestamp format | `GrowwAPIBadRequestException` | PREVENT (epoch-seconds, tz-unambiguous) | L4 |
| E4 | Holiday / no trading | empty `candles` | NO_DATA (OK) | L3 |
| E5 | Partial bars (market halt) | data-level | RECONCILE vs NSE halt log; gap-fill queue | L3 |
| E6 | Frozen candle (halt, vol=0) | data-level anomaly | allow (valid), WARN | L1 |
| E7 | Price in rupees | scale error | ×100 mandatory + golden test | L2 |
| E8 | OI null (cash segment) | schema | expected; allow null | L2 |
| E9 | `closing_price` ≠ last candle close | consistency | use last candle; audit discrepancy | L5 |
| E10 | Rate-limit 429 | `GrowwAPIRateLimitException` | pace-down + backoff RETRY | L7 |
| E11 | Timeout / disconnect (no partial commit) | `GrowwAPITimeoutException` | RETRY (idempotent re-fetch) | L6 |
| E12 | Groww 5xx / API down | `GrowwAPIException` (base) | RETRY once → QUARANTINE; CRITICAL after 3 | L1/L7 |

**Total: 5 + 6 + 6 + 5 + 12 = 34 distinct failure modes**, each with a defined spine outcome and Z+ layer. The `Vendor*` translation + `classify_exception` table (§6) already routes every `Groww*Exception`; the data-level rows are guarded in each fetcher's parse/validate step (proven by that fetcher's tests).

### 13.8 Z+ 7-layer mapping per surface

**L4 PREVENT, L5 AUDIT, L6 RECOVER, L7 COOLDOWN are inherited identically from the spine** (§13.0 + §7) for every surface — no per-surface work. Only the *data-aware* layers differ:

| Layer | Auth | Instruments | Expiries | Contracts | Candles |
|---|---|---|---|---|---|
| **L1 DETECT** | 401 streak counter | row-parse error count | malformed-date count | malformed-symbol count | bar-count vs expected |
| **L2 VERIFY** | token-format check | header-schema golden test | ISO-date assert | symbol-regex assert | OHLC-sanity + ×100 golden |
| **L3 RECONCILE** | — (stateless) | 24 h age-check vs TTL | year-coverage 2020→now | contracts-per-expiry sanity | daily bar-count vs NSE calendar (§8) |

### 13.9 Build sequence (dependency order — what I implement after you approve)

1. **Surface A — `auth.py`** (extract + harden). Everything needs a session; smallest, unblocks all.
2. **Surface B — `instruments.py`** (CSV fetch). Everything addresses symbols from the master.
3. **`brutex fetch` CLI (indices)** — wire auth + master + the existing candle stack for the **3 brute-force indices** end-to-end → first real (or `--dry-run` fake) bars + logs you can verify. *(Indices need no C/D.)*
4. **Surface C — `expiries.py`** (F&O lake).
5. **Surface D — `contracts.py`** (F&O lake) → extend `brutex fetch` to the 200-stock F&O universe.
6. **Quota-tracker wiring** into the loop (stop-at-100K, resume next IST day) + price-unit (#285) resolution. *(HISTORICAL plan step — never wired: backtesting has NO daily quota, DEC-GROWW-RATE-PERMIN-001.)*

Each step: its own PR, 100% standalone-gated tests covering its slice of the 34-matrix, MIGRATION entry, ruff+mypy+pyright green.

### 13.10 Honest hard-gates — what design CANNOT close without your live login (Golden Rule #10)

| Gate | Design's reactive coping | What the Phase 0b probe must confirm |
|---|---|---|
| Backtesting rate-limit bucket | adaptive limiter discovers it; conservative default | the real sustained r/s |
| INDIA VIX present? | 3-index fallback if absent | `NSE-INDIAVIX` returns candles |
| Token "no expiry" / 6 AM? | reactive REAUTH on any 401 | whether a proactive refresh is needed |
| `get_contracts`/`get_expiries` for PAST dates? | year-by-year backfill; forward-only fallback | past-expiry queries return data |

These four are PROPOSED-to-be-resolved by `scripts/groww_probe.py` (§9 smoke gates) once you provide TOTP creds — **no amount of design removes them; the design only makes the system survive whichever way they resolve.**

### 13.11 Decision record

**Decision ID:** `DEC-GROWW-ADAPTER-DESIGN-001` (2026-05-28). **Builds on:** DEC-VENDOR-PIVOT-001 (this doc §1–§12) · DEC-FETCH-{WATERMARK,LOOP,CHUNKS,PARSER,CLIENT,PERSIST}-001 (#282–#287, the shipped spine + candle surface) · DEC-FETCH-ALL-001 (#296, two-tier universe). **Closes:** the precise per-surface design + the full 34-failure-mode matrix + per-surface Z+ mapping the operator required before further code. **Does NOT close:** the implementations themselves (gated on operator approval of §13) + the four Phase-0b hard-gates (need live creds). **Operator review status:** PENDING.
