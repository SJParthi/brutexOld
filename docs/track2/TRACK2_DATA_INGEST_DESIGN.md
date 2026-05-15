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
| Expiry cycle | WEEK + MONTH per underlying (per §2 of TRACK2_OPTIONS_SPEC.md) | 4 active streams |
| Strike position | ATM-10 .. ATM+10 | 21 per stream |
| Option type | CE + PE | 2 per strike |
| Timeframe | 1 minute ONLY | 1 |
| Date range | 2021-01-01 → today | rolling |

Total active backfill series = 4 × 21 × 2 = **168**.

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
│   │   │   │   ├── ATM-10/CE/2024/05.parquet
│   │   │   │   ├── ATM-10/PE/2024/05.parquet
│   │   │   │   ├── ATM-9/CE/2024/05.parquet
│   │   │   │   ├── …  (21 strikes × 2 types = 42 partitions)
│   │   │   └── MONTH/
│   │   │       └── (same shape as WEEK)
│   │   ├── NSE_FNO/OPTIDX/BANKNIFTY/
│   │   │   └── MONTH/(same shape; NO WEEK — SEBI killed it)
│   │   └── BSE_FNO/OPTIDX/SENSEX/
│   │       └── WEEK/(same shape; MONTH derived from last-Thursday-of-month)
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
| `volume` | `int64` (lots) | Lot size per underlying (NIFTY 25, BANKNIFTY 15, SENSEX 10 — verified Phase 2) |
| `open_interest` | `int64` (lots) | |
| `iv` | `float64` | Implied volatility from Dhan, fraction (0.2 = 20%) |
| `delta` | `float64` | Computed BSM |
| `gamma` | `float64` | Computed BSM |
| `theta` | `float64` | Computed BSM (per-year; convert to per-day in engine) |
| `vega` | `float64` | Computed BSM |
| `spot_at_bar` | `int64` (paisa) | NIFTY/BANKNIFTY/SENSEX spot price at same `bar_ts` (foreign-keyed from Track 1 spot lake at ingest time) |
| `strike` | `int64` (paisa) | Absolute strike for this bar (e.g., 25,000 NIFTY = 2,500,000 paisa) |
| `time_to_expiry_us` | `int64` (microseconds) | (expiry_ts_at_15_30 − bar_ts) |
| `bar_hash` | `bytes(8)` | SHA256-low-64-bits of (bar_ts ‖ ohlc ‖ iv ‖ spot ‖ strike); used in audit log |

Compressed with ZSTD at level 3. Per-row ≈ 50 bytes after compression.

---

## 3. Watermark / state machine

### Per-(stream, strike, type) watermark

`$BRUTEX_LAKE/watermark/options_ingest.json` is a single file with 168 rows, one per series:

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

### 5-year backfill

| Quantity | Value |
|---|---|
| Days in 5y (calendar) | ~1,825 |
| Chunks per series (30-day chunks) | 1,825 / 30 = ~61 |
| Series total | 168 |
| **Total backfill calls** | **168 × 61 = 10,248** |
| Effective rate (3 r/s cap) | 3 calls/sec |
| Backfill wall time | 10,248 / 3 = 3,416 sec = **~57 minutes** |
| Daily quota used | 10,248 / 100,000 = **10.2%** (one-time; below cap) |

### Daily incremental

| Quantity | Value |
|---|---|
| Calls per day per series | 1 (yesterday only) |
| Series total | 168 |
| Daily call count | 168 |
| Daily quota used | 168 / 100,000 = **0.17%** |
| Daily wall time | 168 / 3 = 56 sec |

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
    spot_lake = read_track1_spot(df['bar_ts'])           # foreign-key join
    df = df.join(spot_lake, on='bar_ts', how='left')
    df['strike'] = compute_absolute_strike(
        df['spot_at_bar'],
        underlying=…,
        strike_position=…,
        strike_step=STRIKE_STEPS[underlying],
    )
    df['time_to_expiry_us'] = expiry_ts(…) - df['bar_ts']
    rf_rate = lookup_risk_free_rate(df['bar_ts'])         # 60-row monthly table
    df['delta'], df['gamma'], df['theta'], df['vega'] = fno_math.bsm.greeks(
        S=df['spot_at_bar'] / 100.0,                      # paisa → rupees
        K=df['strike'] / 100.0,
        T=df['time_to_expiry_us'] / (365.25 * 86400 * 1e6),
        r=rf_rate,
        sigma=df['iv'],
        q=0.0,
        option_type=…,
    )
    df['bar_hash'] = sha256_low64(df['bar_ts'] ‖ df['open'] ‖ … ‖ df['strike'])
    df.write_parquet(chunk_parquet_path, compression='zstd')
```

Output bar_hash is what goes into the audit chain. Replay oracle
recomputes Greeks from raw input + spot lake; if anything is
non-deterministic, replay diverges and CI fails.

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
async def backfill_options(start_date, end_date):
    streams = [
        ('NIFTY',     'NSE_FNO', 'WEEK'),
        ('NIFTY',     'NSE_FNO', 'MONTH'),
        ('BANKNIFTY', 'NSE_FNO', 'MONTH'),
        ('SENSEX',    'BSE_FNO', 'WEEK'),
    ]
    tasks = []
    for underlying, segment, cycle in streams:
        for sp in range(-10, 11):                        # 21 strikes
            for ot in ('CE', 'PE'):                      # 2 types
                tasks.append(asyncio.create_task(
                    backfill_series(underlying, segment, cycle, sp, ot,
                                    start_date, end_date)
                ))
    await asyncio.gather(*tasks)

async def backfill_series(underlying, segment, cycle, sp, ot,
                          start_date, end_date):
    key = f"{underlying}:{cycle}:{sp}:{ot}"
    wm = load_watermark(key)
    cursor = wm['next_chunk_from'] or start_date
    while cursor < end_date:
        chunk_to = min(cursor + timedelta(days=30), end_date)
        try:
            async with rate_limiter:
                resp = await dhan_client.charts_rollingoption(
                    underlying=underlying,
                    exchange_segment=segment,
                    instrument='OPTIDX',
                    expiry_cycle=cycle,
                    option_type=ot,
                    strike_position=sp,
                    from_date=cursor,
                    to_date=chunk_to,
                    interval='1',
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

Output format:
```
C12 OPTIONS_FRESH:
  NIFTY:WEEK     CE×21  PE×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
  NIFTY:MONTH    CE×21  PE×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
  BANKNIFTY:MONTH CE×21 PE×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
  SENSEX:WEEK    CE×21  PE×21   last_ok=2026-05-14  (≤1d)  ✓ GREEN
```

RED if any series has `last_ok_to_date < today − 1 day` OR state ≠ IDLE.

---

## 11. Smoke test (sub-30-second, Phase 1)

`tests/smoke/test_options_minimal.py`:

```python
def test_options_smoke():
    """≤30s smoke: fake adapter returns 1 day × 1 series; verify pipeline."""
    fake = FakeRollingOptionAdapter(
        series={('NIFTY','WEEK',0,'CE'): synthetic_bars(date='2024-05-13')}
    )
    asyncio.run(backfill_options(
        adapter=fake,
        start_date=date(2024,5,13),
        end_date=date(2024,5,13),
        streams=[('NIFTY','NSE_FNO','WEEK',0,'CE')],
    ))
    # Assert partition exists, audit row appended, watermark updated.
    assert Path(LAKE / 'data/options/NSE_FNO/OPTIDX/NIFTY/WEEK/ATM-0/CE/2024/05.parquet').exists()
    assert audit_chain_intact()
    assert load_watermark('NIFTY:WEEK:0:CE')['last_ok_to_date'] == '2024-05-13'
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
- Watermark file: single JSON with 168 rows (one per series)
- Audit kinds: `OPT_INGEST_CHUNK_OK/ERR`, `OPT_GREEK_COMPUTED`, `OPT_BACKTEST_ENTRY/EXIT`
- Concurrency: per-series asyncio task; 3 r/s shared token bucket
- Greeks computed inline at chunk-write time (not lazy)
- Risk-free rate table: 60-row 5y monthly snapshot in `reference/risk_free_rate.parquet`
- Strike grid: hardcoded per-underlying in `brutex/options/strike_grid.py` (NIFTY 50, BANKNIFTY 100, SENSEX 100)
- Lot size: hardcoded per-underlying in `brutex/options/lot_size.py` (verified Phase 2 against Dhan master CSV)
- Doctor adds C12 OPTIONS_FRESH check
- Smoke test against FakeRollingOptionAdapter < 30 sec
