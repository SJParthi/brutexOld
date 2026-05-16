# BruteX Track 2 — Data Ingest Design

> Storage layout, state machine, error map, and pseudocode for the
> Dhan options backfill pipeline. Mirrors `TRACK1_DATA_INGEST_DESIGN.md`
> with options-specific extensions.

_Last updated: 2026-05-15._

---

## 1. Inputs

| Input | Source | Cardinality |
|---|---|---|
| Underlying | Track 2 universe (NIFTY, BANKNIFTY, SENSEX) | 3 |
| Expiry cycle | WEEK + MONTH per underlying with per-regime gating (per §2 of TRACK2_OPTIONS_SPEC.md) | **6 active streams** |
| Strike | `ATM`, `ATM+1..+10`, `ATM-1..-10` (string sent to API) | 21 per stream |
| Option type | `CALL` + `PUT` (sent as `drvOptionType`) | 2 per strike |
| Timeframe | 1 minute ONLY (`interval: 1`) | 1 |
| Date range | 2021-01-01 → today, per-stream date gating | rolling |

Per-stream active window (from `TRACK2_OPTIONS_SPEC.md §2`):
- NIFTY-WEEK + NIFTY-MONTH: 2021-01-01 → today
- BANKNIFTY-WEEK: 2021-01-01 → **2024-11-13** (SEBI killed; last expiry Wed)
- BANKNIFTY-MONTH: 2021-01-01 → today (day-of-week varies — see SPEC §2)
- SENSEX-WEEK + SENSEX-MONTH: **2021-01-01** → today (Thursday cycle to 2023-05-12 per BSE Notice 20230327-65 — thin liquidity; Friday 2023-05-15 → 2024-12-31; Tuesday 2025-01-01 → 2025-08-31 with one-off Friday carve-out 2025-01-03; Thursday 2025-09-04 → today)

Total active backfill series = 6 × 21 × 2 = **252**.

---

## 2. Hierarchical lake layout

```
$BRUTEX_LAKE/
├── data/
│   ├── spot/                                       ← Track 1 (existing)
│   │   ├── IDX_I/13/INDEX/1m/2024/05.parquet      (NIFTY 50)
│   │   └── …
│   │
│   ├── options/                                    ← Track 2 (NEW)
│   │   ├── NSE_FNO/OPTIDX/NIFTY/
│   │   │   ├── WEEK/
│   │   │   │   ├── ATM-10/CALL/2024/05.parquet
│   │   │   │   ├── ATM-10/PUT/2024/05.parquet
│   │   │   │   ├── ATM-9/CALL/2024/05.parquet
│   │   │   │   ├── …  (21 strikes × 2 types = 42 partitions)
│   │   │   └── MONTH/
│   │   │       └── (same shape as WEEK)
│   │   ├── NSE_FNO/OPTIDX/BANKNIFTY/
│   │   │   ├── WEEK/  (populated 2021-01-01 → 2024-11-13; dormant thereafter)
│   │   │   └── MONTH/ (continuous)
│   │   └── BSE_FNO/OPTIDX/SENSEX/
│   │       ├── WEEK/  (populated 2021-01-01 onwards; thin pre-2023-05-15)
│   │       └── MONTH/ (populated 2021-01-01 onwards; thin pre-2023-05-15)
│   │
│   └── reference/                                  ← shared
│       ├── risk_free_rate.parquet                 (60-row 5y monthly)
│       ├── expiry_calendar_2021_2026.parquet      (per-underlying expiry days)
│       └── strike_grid.parquet                    (per-underlying strike step)
│
├── audit/audit.jsonl                              ← shared with Track 1
├── watermark/                                     ← shared
│   ├── spot_ingest.json
│   └── options_ingest.json                        (NEW per stream/strike/type)
└── locks/
    └── options_lock.json                          (NEW; prevents 2 parallel options ingests)
```

### Parquet schema (per partition: 1 month × 1 series)

| Column | Type | Notes |
|---|---|---|
| `bar_ts` | `int64` (microseconds UTC) | Aligned to 1-minute boundary (× 60_000_000) |
| `open` | `int64` (paisa) | Option premium open |
| `high` | `int64` (paisa) | |
| `low` | `int64` (paisa) | |
| `close` | `int64` (paisa) | |
| `volume` | `int64` (contracts) | Stored as **number of contracts**, NOT lots — see lot-size hostile-review fix in §13. Lot size at the date is keyed from `reference/lot_size_history.parquet` and applied in the engine. |
| `open_interest` | `int64` (contracts) | Same lot-size policy as volume. |
| `iv` | `float64` | Implied volatility from Dhan, fraction (0.2 = 20%) |
| `delta` | `float64` | Computed BSM |
| `gamma` | `float64` | Computed BSM |
| `theta` | `float64` | Computed BSM (per-year; convert to per-day in engine) |
| `vega` | `float64` | Computed BSM |
| `spot_at_bar` | `int64` (paisa) | NIFTY/BANKNIFTY/SENSEX spot price at same `bar_ts` (foreign-keyed from Track 1 spot lake at ingest time) |
| `strike_api` | `int64` (paisa) | Absolute strike as **returned by Dhan** in the rollingoption response — the contract that backs this bar. Rolls when ATM shifts. |
| `strike_computed` | `int64` (paisa) | Engine-recomputed strike = `compute_absolute_strike(spot_at_bar, strikePosition, strike_step)`. **Invariant P-O12**: `strike_api == strike_computed` modulo Dhan's tie-break at midpoints (deviation must match `expected_tiebreak_table`). |
| `time_to_expiry_us` | `int64` (microseconds) | (expiry_ts_at_15_30 − bar_ts) |
| `raw_bar_hash` | `bytes(8)` | SHA256-low-64-bits of (bar_ts ‖ ohlc ‖ iv ‖ spot_at_bar ‖ strike_api). Covers Dhan-returned raw data. Used in audit log for ingest replay. |
| `cooked_bar_hash` | `bytes(8)` | SHA256-low-64-bits of (raw_bar_hash ‖ delta ‖ gamma ‖ theta ‖ vega ‖ bsm_kernel_version). Covers BSM-derived Greeks. Used in audit log for full-replay (oracle O-O1 checks BOTH hashes). |

Compressed with ZSTD at level 3. Per-row ≈ 50 bytes after compression.

> **Why two hashes?** Hostile review caught that a single hash over raw
> data alone leaves replay oracle O-O1 blind to BSM kernel regressions
> (a Greek-only refactor would silently change Parquet bytes while
> `bar_hash` stays identical). Separating raw and cooked hashes lets the
> oracle check both: raw-ingest determinism AND Greeks-compute
> determinism, independently.

---

## 3. Watermark / state machine

### Per-(stream, strike, type) watermark

`$BRUTEX_LAKE/watermark/options_ingest.json` is a single file with **252 rows**, one per series.

Each row also carries the `active_window` (start_date, end_date) so the
state machine can determine when the series is "permanently dormant"
(e.g., BANKNIFTY-WEEK after 2024-11-13) vs simply "no fresh data yet
today" (transient). Dormant series do not trigger doctor RED:

```json
{
  "schema_version": 1,
  "series": {
    "NIFTY:WEEK:-10:CE": {
      "last_ok_to_date": "2024-05-30",
      "last_ok_at_utc": "2024-05-30T15:35:12Z",
      "next_chunk_from": "2024-05-31",
      "state": "OK",
      "consecutive_errors": 0,
      "last_error": null,
      "last_audit_offset": 1456,
      "bytes_written_total": 21847362
    },
    "NIFTY:WEEK:-10:PE": { … },
    "NIFTY:WEEK:-9:CE":  { … },
    …
  }
}
```

### State machine per series

```
                  ┌──────────────┐
                  │     IDLE     │  (initial / after success)
                  └──────┬───────┘
                         │ schedule_chunk(from, to)
                         ▼
                  ┌──────────────┐
                  │   ACQUIRING  │  (in-flight HTTP call)
                  └──────┬───────┘
                  ok ┌───┴───┐ err
                     ▼       ▼
              ┌──────────┐ ┌─────────────┐
              │ VALIDATING│ │  RETRYING  │ (exponential backoff)
              └─────┬─────┘ └──┬──────┬──┘
                ok  │          │      │ max_retries
                    ▼          │      ▼
              ┌──────────┐     │  ┌──────────┐
              │ WRITING  │     │  │  FROZEN  │ (human-intervention required)
              └────┬─────┘     │  └──────────┘
                ok │           │
                   ▼           │
              ┌──────────┐     │
              │  COMMIT  │←────┘ (back to IDLE after audit append)
              └────┬─────┘
                   │
                   ▼
              ┌──────────┐
              │   IDLE   │
              └──────────┘
```

Transitions are atomic — `WRITING → COMMIT` is `fsync` + `rename` of
the Parquet partition AND `append` to audit AND `update` to watermark
in that order. Failure between any two steps is recoverable (Phase 1
recovery test exercises this).

---

## 4. Bandwidth + quota math

### Per-call

| Quantity | Value |
|---|---|
| Bars per call (max) | 30 days × 375 bars = 11,250 |
| Response bytes (JSON, uncompressed) | ~1 MB (8 arrays × 11,250 × ~10 bytes) |
| Compressed (gzip in-flight) | ~250 KB |

### 5-year backfill (with per-stream date gating)

> **Chunk count uses `ceil(window_days / 30)`**. Leftover partial windows
> need a final call. Reality-check agent caught the prior floor-based
> off-by-one (15,624 → 15,876).

| Stream | Window | Cal days | Chunks/series (ceil) | Calls (× 42 series) |
|---|---|---|---|---|
| NIFTY-WEEK    | 2021-01-01 → today | ~1,960 | 66 | 2,772 |
| NIFTY-MONTH   | 2021-01-01 → today | ~1,960 | 66 | 2,772 |
| BANKNIFTY-WEEK| 2021-01-01 → 2024-11-13 | ~1,412 | 48 | 2,016 |
| BANKNIFTY-MONTH| 2021-01-01 → today | ~1,960 | 66 | 2,772 |
| SENSEX-WEEK   | **2021-01-01** → today | ~1,960 | 66 | 2,772 |
| SENSEX-MONTH  | **2021-01-01** → today | ~1,960 | 66 | 2,772 |
| | | | **Total calls** | **~15,876** |

| Quantity | Value |
|---|---|
| Effective rate (3 r/s cap) | 3 calls/sec |
| Backfill wall time | 15,876 / 3 = 5,292 sec = **~88 minutes** |
| Daily quota used | 15,876 / 100,000 = **15.9%** (one-time; below cap) |

### Daily incremental

| Quantity | Value |
|---|---|
| Active series on a typical post-Nov-2024 day | 210 (252 total − 42 BN-WEEK dormant) |
| Calls per day | 210 |
| Daily quota used | 210 / 100,000 = **0.21%** |
| Daily wall time | 210 / 3 = 70 sec |

Well below quota in both modes.

---

## 5. Error handling

Same error map as Track 1 (`TRACK1_DATA_INGEST_DESIGN.md §10A`),
plus these options-specific cases:

| Error code | Meaning | Strategy |
|---|---|---|
| `806` | Invalid expiry | Log + skip; bump series to `FROZEN`; alert operator |
| `807` | strikePosition out of range | Should never happen (we clamp to [-10, +10]); fail loud as a bug, not a transient error |
| `808` | No data (e.g., expiry in the future) | Treat as empty success; advance watermark to `to_date` |
| `809` | Underlying not tradeable on date | Treat as empty success; advance watermark |
| `810` | rolling-option series discontinuity | Phase 2 quantifies; default = NaN row at discontinuity; downstream Greek calc skips NaN bars |

Backoff schedule for retryable errors (HTTP 429, 5xx, network):
- Attempt 1: 0 sec
- Attempt 2: 1 sec
- Attempt 3: 4 sec
- Attempt 4: 16 sec
- Attempt 5: 64 sec
- After 5: `FROZEN`, alert operator via `brutex doctor` C12

---

## 6. Greeks computation pipeline

After each chunk WRITE succeeds for raw OHLC+IV bars, a deterministic
post-process computes Greeks per bar:

```python
def compute_greeks_for_chunk(chunk_parquet_path):
    df = pl.read_parquet(chunk_parquet_path)
    # df already contains strike_api (Dhan-returned per-bar strike).
    spot_lake = read_track1_spot(df['bar_ts'])           # foreign-key join
    df = df.join(spot_lake, on='bar_ts', how='left')

    # Compute engine's own strike from spot + strikePosition + step.
    df['strike_computed'] = compute_absolute_strike(
        df['spot_at_bar'],
        underlying=…,
        strike_position=…,
        strike_step=STRIKE_STEPS[underlying],
    )
    # P-O12 invariant — strike_api should match strike_computed
    # modulo Dhan tie-break at exact midpoints. Phase 2 builds the
    # tie-break exception table from observed midpoint cases.
    assert_strike_invariant(df['strike_api'], df['strike_computed'],
                            tiebreak_table=EXPECTED_TIEBREAKS[underlying])

    df['time_to_expiry_us'] = expiry_ts(…) - df['bar_ts']
    rf_rate = lookup_risk_free_rate(df['bar_ts'])         # 60-row monthly table

    # Use strike_api (the contract that actually traded) for BSM,
    # NOT strike_computed (engine's idea).
    df['delta'], df['gamma'], df['theta'], df['vega'] = fno_math.bsm.greeks(
        S=df['spot_at_bar'] / 100.0,                      # paisa → rupees
        K=df['strike_api'] / 100.0,
        T=df['time_to_expiry_us'] / (365.25 * 86400 * 1e6),
        r=rf_rate,
        sigma=df['iv'],
        q=0.0,
        option_type=…,                                    # 'CALL' or 'PUT'
    )

    # Two hashes — raw covers Dhan-returned bytes, cooked covers Greeks.
    BSM_KERNEL_VERSION = fno_math.bsm.VERSION             # int, bumped on kernel change
    df['raw_bar_hash'] = sha256_low64(
        df['bar_ts'] ‖ df['open'] ‖ df['high'] ‖ df['low'] ‖ df['close']
        ‖ df['volume'] ‖ df['open_interest'] ‖ df['iv']
        ‖ df['spot_at_bar'] ‖ df['strike_api']
    )
    df['cooked_bar_hash'] = sha256_low64(
        df['raw_bar_hash'] ‖ df['delta'] ‖ df['gamma']
        ‖ df['theta'] ‖ df['vega'] ‖ BSM_KERNEL_VERSION
    )
    df.write_parquet(chunk_parquet_path, compression='zstd')
```

Audit log carries BOTH hashes. Replay oracle O-O1:
- Re-runs ingest from same Dhan input + spot lake → `raw_bar_hash` must match
- Re-runs Greeks compute with same BSM kernel version → `cooked_bar_hash` must match
- If BSM kernel version is bumped, replay only re-validates raw; cooked is
  reproducible-only-against-the-pinned-version (audit row records the version).

---

## 7. Concurrency model

### Process-level
- Single Python process. asyncio with httpx (same as Track 1).
- One asyncio task per series; up to 3 r/s aggregate via a shared token bucket.

### File-system
- Locking via `$BRUTEX_LAKE/locks/options_lock.json` (advisory flock).
- Track 1 spot ingest and Track 2 options ingest can run concurrently
  (they touch different lake subtrees + different rate-limit budget,
  but share the daily 100K quota). The scheduler enforces "spot
  finishes incremental, then options runs" if both are scheduled at
  the same start time.

### Audit log
- All writers append to the SAME `audit/audit.jsonl`.
- File-lock around append (`fcntl.flock`), same as Track 1.
- SHA chain link from prior row remains valid across both Track 1 and
  Track 2 audit kinds.

---

## 8. Pseudocode

### High-level driver

```python
# All 6 streams with per-regime date gating (see §1).
# Vocabulary: optionType ∈ {'CALL','PUT'} per Dhan SDK validator
# (drvOptionType field) — NOT 'CE'/'PE'.
async def backfill_options(global_start, global_end):
    streams = [
        # (underlying, securityId, exchange_segment, cycle, start, end)
        ('NIFTY',     '13', 'NSE_FNO', 'WEEK',  '2021-01-01', None),
        ('NIFTY',     '13', 'NSE_FNO', 'MONTH', '2021-01-01', None),
        ('BANKNIFTY', '25', 'NSE_FNO', 'WEEK',  '2021-01-01', '2024-11-13'),
        ('BANKNIFTY', '25', 'NSE_FNO', 'MONTH', '2021-01-01', None),
        ('SENSEX',    '51', 'BSE_FNO', 'WEEK',  '2021-01-01', None),
        ('SENSEX',    '51', 'BSE_FNO', 'MONTH', '2021-01-01', None),
    ]
    tasks = []
    for underlying, sec_id, segment, cycle, w_start, w_end in streams:
        stream_end = w_end or global_end
        for sp in range(-10, 11):                        # 21 strikes
            for ot in ('CALL', 'PUT'):                   # SDK-validated vocab
                tasks.append(asyncio.create_task(
                    backfill_series(underlying, sec_id, segment, cycle, sp, ot,
                                    max(w_start, global_start), stream_end)
                ))
    await asyncio.gather(*tasks)

def strike_label(sp: int) -> str:
    """API takes strike as string 'ATM', 'ATM+1', …, 'ATM-10'."""
    if sp == 0: return 'ATM'
    return f"ATM{'+' if sp > 0 else ''}{sp}"

async def backfill_series(underlying, sec_id, segment, cycle, sp, ot,
                          start_date, end_date):
    key = f"{underlying}:{cycle}:{sp:+d}:{ot}"          # e.g. "NIFTY:WEEK:+5:CALL"
    wm = load_watermark(key)
    cursor = wm['next_chunk_from'] or start_date
    while cursor < end_date:
        chunk_to = min(cursor + timedelta(days=30), end_date)
        try:
            async with rate_limiter:
                resp = await dhan_client.expired_options_data(
                    securityId=sec_id,
                    exchangeSegment=segment,
                    instrument='OPTIDX',
                    expiryFlag=cycle,                   # 'WEEK' or 'MONTH'
                    expiryCode=0,                       # current cycle as of fromDate
                    strike=strike_label(sp),            # 'ATM' / 'ATM+5' / 'ATM-3'
                    drvOptionType=ot,                   # 'CALL' / 'PUT'
                    requiredData=['open','high','low','close','volume','iv','oi','spot','strike'],
                    fromDate=cursor.isoformat(),
                    toDate=chunk_to.isoformat(),
                    interval=1,                         # int per SDK
                )
            df = parse_response(resp)
            df = validate_ohlc(df)              # P-O2, P-O3 invariants
            df = enrich_with_spot(df)           # foreign-key join Track 1
            df = compute_greeks(df)             # BSM
            df.write_parquet(partition_path(key, cursor))
            audit_append({
                'kind': 'OPT_INGEST_CHUNK_OK',
                'series': key,
                'from': cursor.isoformat(),
                'to':   chunk_to.isoformat(),
                'n_bars': len(df),
                'sha256': df['bar_hash'].sha256_aggregate(),
            })
            wm['last_ok_to_date'] = chunk_to.isoformat()
            wm['next_chunk_from'] = (chunk_to + timedelta(days=1)).isoformat()
            wm['consecutive_errors'] = 0
            save_watermark(key, wm)
            cursor = chunk_to + timedelta(days=1)
        except RetryableError as e:
            wm['consecutive_errors'] += 1
            if wm['consecutive_errors'] >= 5:
                wm['state'] = 'FROZEN'
                save_watermark(key, wm)
                raise
            await asyncio.sleep(backoff(wm['consecutive_errors']))
        except FatalError as e:
            wm['state'] = 'FROZEN'
            save_watermark(key, wm)
            audit_append({'kind': 'OPT_INGEST_CHUNK_ERR', 'series': key,
                          'from': cursor, 'to': chunk_to, 'error': str(e)})
            raise

async def daily_incremental():
    yesterday_start = today_ist().replace(hour=9, minute=15) - timedelta(days=1)
    yesterday_end   = yesterday_start.replace(hour=15, minute=30)
    await backfill_options(yesterday_start, yesterday_end)
```

### Validation predicates (Phase 1 property tests)

```python
def validate_ohlc(df):
    assert (df['low'] <= df['open']).all()
    assert (df['low'] <= df['close']).all()
    assert (df['high'] >= df['open']).all()
    assert (df['high'] >= df['close']).all()
    assert (df['open']  >= 0).all()
    assert (df['close'] >= 0).all()
    assert (df['volume'] >= 0).all()
    assert (df['open_interest'] >= 0).all()
    assert (df['iv'] >= 0).all() & (df['iv'] <= 5.0).all()    # IV cap 500%
    return df
```

---

## 9. Recovery scenarios

| Scenario | Detection | Recovery |
|---|---|---|
| Crash mid-chunk WRITE | Partition has temp file but no rename | Phase 1 test: on startup, delete temp; restart chunk from watermark |
| Crash after WRITE but before audit append | Audit chain missing latest hash; partition exists | Phase 1 test: detect by chain audit; re-append audit row with replay hash |
| Crash after audit but before watermark update | Watermark says next chunk = N; partition for N+1 exists | Phase 1 test: scan partitions; reconcile watermark to highest partition + 1 |
| Disk full | Partition WRITE fails | Audit error row + FROZEN; operator clears space + sets state IDLE |
| Dhan 429 (rate limit) | HTTP 429 | Backoff per §5 schedule |
| Dhan 5xx | HTTP 5xx | Backoff per §5 schedule |
| Network timeout | httpx timeout | Treat as transient; backoff |
| Token expired mid-chunk | HTTP 401 from rollingoption | Trigger Track 1 auto-renew loop (shared); retry chunk with fresh token |

---

## 10. Doctor check (C12 — to be added in Phase 2)

`brutex doctor` adds a 12th check on top of Track 1's 11:

```
C12 OPTIONS_FRESH: every active series has watermark within 1 day of today (IST)
```

Output format (5 active streams + 1 dormant after 2024-11-13):
```
C12 OPTIONS_FRESH:
  NIFTY:WEEK       CALL×21  PUT×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
  NIFTY:MONTH      CALL×21  PUT×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
  BANKNIFTY:WEEK   CALL×21  PUT×21   last_ok=2024-11-13  DORMANT (SEBI killed)  ◌ SKIP
  BANKNIFTY:MONTH  CALL×21  PUT×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
  SENSEX:WEEK      CALL×21  PUT×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
  SENSEX:MONTH     CALL×21  PUT×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
```

RED if any non-dormant series has `last_ok_to_date < today − 1 day`
OR state ≠ IDLE. DORMANT series (BN-WEEK post-2024-11-13) are SKIP,
not RED.

---

## 11. Smoke test (sub-30-second, Phase 1)

`tests/smoke/test_options_minimal.py`:

```python
def test_options_smoke():
    """≤30s smoke: fake adapter returns 1 day × 1 series; verify pipeline."""
    fake = FakeRollingOptionAdapter(
        series={('NIFTY','WEEK',0,'CALL'): synthetic_bars(date='2024-05-13')}
    )
    asyncio.run(backfill_options(
        adapter=fake,
        start_date=date(2024,5,13),
        end_date=date(2024,5,13),
        streams=[('NIFTY','13','NSE_FNO','WEEK',0,'CALL')],
    ))
    # Assert partition exists, audit row appended, watermark updated.
    assert Path(LAKE / 'data/options/NSE_FNO/OPTIDX/NIFTY/WEEK/ATM/CALL/2024/05.parquet').exists()
    assert audit_chain_intact()
    assert load_watermark('NIFTY:WEEK:+0:CALL')['last_ok_to_date'] == '2024-05-13'
```

---

## 12. Out of scope (Phase 0)

- ✗ Live option chain WebSocket subscription
- ✗ Order placement / portfolio
- ✗ Multi-leg structures (straddles, condors)
- ✗ Margin calculation
- ✗ Greeks beyond delta/gamma/theta/vega
- ✗ Per-contract (non-rollingoption) backfill — only added if Dhan removes rolling endpoint

---

## 13. Decisions to lock (in addition to spec §17)

These are ingest-design decisions specifically (will append to MIGRATION.md):

- Storage layout: `options/<exchange>/<segment>/<underlying>/<cycle>/ATM<offset>/<type>/<YYYY>/<MM>.parquet`
- Strike folder name: `ATM` for ATM, `ATM+N` / `ATM-N` for offsets (matches API `strike` string)
- Option type folder name: `CALL` / `PUT` (matches API `drvOptionType`)
- Watermark file: single JSON with 252 rows (one per series); watermark key format `"<underlying>:<cycle>:<+N|-N|+0>:<CALL|PUT>"`
- Audit kinds: `OPT_INGEST_CHUNK_OK/ERR`, `OPT_GREEK_COMPUTED`, `OPT_BACKTEST_ENTRY/EXIT` — payload uses CALL/PUT (NOT CE/PE)
- Concurrency: per-series asyncio task; 3 r/s shared token bucket
- **Audit log append serialization (hostile-review fix):** all audit appends pass through a single asyncio writer task — `audit_writer_task` consumes from an `asyncio.Queue` and writes serially. flock is for cross-process protection only. Replay oracle O-O1 sorts replayed rows by `(stream, chunk_from)` to remove asyncio-scheduler nondeterminism.
- Greeks computed inline at chunk-write time (not lazy); two-hash design (`raw_bar_hash` + `cooked_bar_hash`) ensures replay catches BSM kernel regressions
- Risk-free rate table: 60-row 5y monthly snapshot in `reference/risk_free_rate.parquet`. **Lookup is step-function (no interpolation)** — bar at `bar_ts` uses the row for `floor(bar_ts, month)`. Replay oracle locks this policy.
- Strike grid: hardcoded per-underlying in `brutex/options/strike_grid.py` (NIFTY 50, BANKNIFTY 100, SENSEX 100), **with date-keyed override support** (Phase 2 verifies historical stability against Dhan master CSV snapshots; emits ADR if any step changed in 2021-2026)
- **Lot size: date-keyed table `reference/lot_size_history.parquet`** — NOT hardcoded per-underlying. SEBI revised NIFTY lot size 50→75 Nov 2024 and BANKNIFTY lot size multiple times in 2022-2024; per-bar `volume`/`open_interest` are stored as **contract count** (independent of lot size), engine looks up lot size for P&L conversion. (Hostile review caught: hardcoded once would silently corrupt P&L reconstruction across lot-size revision dates.)
- **Expiry-day theta policy:** bars with `time_to_expiry_us < 1 minute` (i.e., the 15:29:00 → 15:30:00 bar on expiry day) **clamp T at 1 minute** for Greeks computation to avoid divergence. Bars themselves are stored (not NaN). This policy is recorded in the audit chain via `BSM_KERNEL_VERSION`.
- Doctor adds C12 OPTIONS_FRESH check (5 active + 1 dormant for BN-WEEK)
- Smoke test against FakeRollingOptionAdapter < 30 sec
