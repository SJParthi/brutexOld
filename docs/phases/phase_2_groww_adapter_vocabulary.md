# Phase 2 — Groww Adapter + Spot-Index Ingest + Vocabulary

## Status: ⏸️ Blocked (needs user-provided Groww token)

## Effort: 3-4 weeks
## Owner: Claude (entirely — user provides only token + sign-off)

## Goal

Stand up the Groww data path (1m + 1d for 4 instruments), the
52-condition vocabulary engine, and the indicator precompute pipeline.
At end of Phase 2, BruteX can pull 5 years of OHLC for all 4 instruments,
resample to 21 TFs, compute 9 indicators, and evaluate all 52 atomic
conditions per bar.

## Prerequisites

- [x] Phase 0 complete (3 PRs on main)
- [ ] Phase 1 complete (verification scaffolding)
- [ ] **USER: provide Groww access token** in `~/.brutex/groww/access_token.json`:
  ```json
  {
    "client_id": "<your-client-id>",
    "access_token": "<token-from-web.groww.co>",
    "expiry_time": "<ISO 8601 expiry>",
    "last_renewed_at": "<ISO 8601 now>"
  }
  ```
  Get token from `web.groww.co` → My Profile → Access Groww APIs.
- [ ] **USER: ensure** `/Volumes/QuantData` SSD is mounted (or set `BRUTEX_LAKE_ROOT`)
- [ ] Read `docs/TRACK1_DATA_INGEST_DESIGN.md` in full

## Checklist

### Week 4 — Groww adapter

#### Constants module

- [ ] `brutex/adapters/groww/__init__.py`
- [ ] `brutex/adapters/groww/constants.py` — the 4 hardcoded tuples per decision #87:
  - [ ] `NIFTY = (securityId=13, exchangeSegment="IDX_I", instrument="INDEX", symbol="NIFTY 50")`
  - [ ] `BANKNIFTY = (25, "IDX_I", "INDEX", "NIFTY BANK")`
  - [ ] `SENSEX = (51, "IDX_I", "INDEX", "SENSEX")`
  - [ ] `INDIA_VIX = (21, "IDX_I", "INDEX", "INDIA VIX")` (reference-only)
  - [ ] `RATE_LIMIT_PER_SEC = 5`, `DAILY_QUOTA = 100_000`, `SAFETY_FACTOR = 0.6`
  - [ ] `BASE_URL = "https://api.groww.co/v2"`
  - [ ] `RENEW_URL = "https://api.groww.co/v2/RenewToken"`

#### Authentication (decision #86)

- [ ] `brutex/adapters/groww/auth.py`:
  - [ ] `GrowwCredentials` frozen dataclass
  - [ ] `load_credentials(path: Path)` — read JSON, raise if missing
  - [ ] `needs_renewal(creds, threshold_minutes=25)` — boolean
  - [ ] `renew_token(creds)` — call `POST /v2/RenewToken`, return new creds
  - [ ] `persist_atomically(creds, path)` — tempfile + rename
  - [ ] `get_or_renew(path)` — hot-path helper
- [ ] Unit tests with mocked HTTP responses
- [ ] `brutex auth setup` CLI subcommand — prints instructions
- [ ] `brutex auth status` CLI subcommand — shows current expiry

#### HTTP client

- [ ] `brutex/adapters/groww/client.py`:
  - [ ] HTTPX async client with token from `auth.get_or_renew()`
  - [ ] Adaptive token bucket at 3 r/s effective (5 × 0.6)
  - [ ] 5xx retry with exponential backoff (3 attempts)
  - [ ] Map Groww error codes per `TRACK1_DATA_INGEST_DESIGN.md §10A`:
    - [ ] 805 / DH-904 → RATE_LIMIT → downshift bucket
    - [ ] 807/808/809/810 / DH-901/903 → AUTH_FAILED → quarantine + alert
    - [ ] 811/812/813/814 / DH-905/906 → BAD_REQUEST → quarantine
    - [ ] 804/806 → FATAL → halt
    - [ ] 800 / DH-908/910 → GENERIC_API_ERROR → retry
    - [ ] DH-907 → NO_DATA → advance watermark
    - [ ] DH-909 → TIMEOUT → retry
  - [ ] Daily quota tracker (refuse if projected > 80%)
- [ ] Property test P-18 (rate limiter timing) green

#### Intraday endpoint

- [ ] `brutex/adapters/groww/intraday.py`:
  - [ ] `fetch_intraday(security_id, from_dt, to_dt, interval=1)` returning list[Bar]
  - [ ] Verify 90-day chunk limit per call
  - [ ] Validate response: timestamps sorted, no duplicates
  - [ ] Convert UTC epoch → store as UTC (per decision #16)
  - [ ] Drop pre-open bars (< 09:15:00 IST)
  - [ ] Drop holiday dates (per `TRACK1_MICROSTRUCTURE.md §4`)

#### Daily endpoint

- [ ] `brutex/adapters/groww/historical.py`:
  - [ ] `fetch_daily(security_id, from_date, to_date)` returning list[Bar]
  - [ ] Same validation as intraday

#### First live verification (USER PRESENT)

- [ ] Run `brutex doctor C11` — should be GREEN
- [ ] `brutex auth status` — shows valid token
- [ ] Fetch 1 day of NIFTY 1m — verify timestamps in 09:15-15:30 IST range
- [ ] **Verify all 4 securityIds resolve** via web.groww.co/Charts URL smoke test
- [ ] Verify rate limit isn't tripped at 3 r/s

### Week 5 — Data lake plumbing

#### Parser

- [ ] `brutex/data/parser_groww.py`:
  - [ ] Parse Groww intraday response → `CandleBar`
  - [ ] Parse Groww historical response → `CandleBar` (daily)
  - [ ] Replace `parse_candle_row` calls in lake pipeline (or keep both)

#### Watermark table (decision #76, gap G1)

- [ ] `brutex/data/watermark.py`:
  - [ ] DDL: `watermarks(instrument_id, timeframe, last_bar_ts, last_bar_hash, chunk_id, bars_written, updated_at)`
  - [ ] PK on `(instrument_id, timeframe)`
  - [ ] `WatermarkStore.get(inst, tf)` — O(1) read
  - [ ] `WatermarkStore.advance(inst, tf, bar)` — atomic upsert
  - [ ] Property test: cardinality at most 4 × 21 = 84 rows

#### bar_hash function (decision #77, gap G3)

- [ ] `brutex/data/bar_hash.py`:
  - [ ] `compute_bar_hash(bar, instrument_id, tf, source_version) -> bytes`
  - [ ] Formula per `TRACK1_RUNTIME_ARCHITECTURE.md §3.6`:
    `sha256(inst ‖ tf ‖ ts_us ‖ OHLC_paisa ‖ vol ‖ source_version)`
  - [ ] Use int64 paisa form (NOT float)
  - [ ] Property test: same bar → same hash; 1-byte change → different hash

#### Int64 paisa columns (decision Phase 1 gap G2)

- [ ] Add `open_paisa, high_paisa, low_paisa, close_paisa` int64 columns to `CandleBar`
- [ ] Validate `Decimal(str(price)) * 100` is integer-valued (reject sub-paise)
- [ ] Property test P-21 (multiple of 5 paise) green

#### Partition / segment whitelist extension (decisions Phase 1 gaps G4, G5)

- [ ] `brutex/data/partition.py`:
  - [ ] `VALID_INTERVALS` extended to 21 Track 1 TFs:
    - `{1m, 2m, 3m, 4m, 5m, 6m, 7m, 8m, 9m, 10m, 11m, 12m, 13m, 14m, 15m, 30m, 60m, 120m, 180m, 240m, 1d}`
  - [ ] `VALID_SEGMENTS` add `IDX_I` (per decision #80)

#### Resampler — 09:15 IST anchor (decision Phase 1 gap critical #4)

- [ ] `brutex/feed/resampler.py`:
  - [ ] Replace UTC-epoch-floor bucketing with **09:15 IST anchor**
  - [ ] Single-pass: 1m → 19 derived intraday TFs in one walk
  - [ ] Drop partial bars at 15:30 session close
  - [ ] Property test P-08 (associativity) green
  - [ ] Property test P-23 (session window) green

#### ParquetWriter current-month grow (decision Phase 1 critical)

- [ ] `brutex/data/parquet_writer.py`:
  - [ ] Allow current-month partition to grow (append + merge by ts)
  - [ ] Keep `WriterError` raise for stale (older) partitions
  - [ ] Property test P-19 (deterministic Parquet) green

#### 9 indicators in Rust

- [ ] `rust/fno-math/src/indicators/ema.rs` (EMA 20, EMA 200)
- [ ] `rust/fno-math/src/indicators/day_extremes.rs` (running day_high, day_low)
- [ ] `rust/fno-math/src/indicators/pdh_pdl.rs` (previous trading day)
- [ ] `rust/fno-math/src/indicators/daily_pivots.rs` (P, R1, R2, R3, S1, S2, S3)
- [ ] `rust/fno-math/src/indicators/daily_fib.rs` (12 computed, 11 in vocab)
- [ ] `rust/fno-math/src/indicators/candle_meta.rs` (color, body, wicks)
- [ ] Cache key: `sha256(data_version ‖ indicator_version)`
- [ ] PyO3 bindings in `rust/fno-math-py/src/lib.rs`
- [ ] Property tests P-13, P-14 green

#### Golden fixtures #1-5 (decision Pyramid §4)

- [ ] `groww_intraday_2024-01-02_NIFTY.json` (post-budget gap day)
- [ ] `groww_intraday_2024-04-12_NIFTY.json` (Ram Navami half-session)
- [ ] `groww_intraday_2024-11-01_NIFTY.json` (Diwali Muhurat)
- [ ] `groww_intraday_2024-05-20_NIFTY.json` (election holiday)
- [ ] `groww_intraday_2025-03-14_NIFTY.json` (Holi)

### Week 6 — 52-condition vocabulary

- [ ] `rust/fno-math/src/vocabulary/atoms.rs` — all 52 atomic evaluators
  - [ ] Group 1: EMA position (bits 0-5)
  - [ ] Group 2: Pivot proximity (bits 6-12)
  - [ ] Group 3: PDH/PDL relationships (bits 13-18)
  - [ ] Group 4: Fib proximity (bits 19-29) — 11 levels (12th reserved)
  - [ ] Group 5: Candle shape (bits 30-36)
  - [ ] Group 6: Prior bar pattern (bits 37-39)
  - [ ] Group 7: Day structure (bits 40-43)
  - [ ] Group 8: Time of day (bits 44-47)
  - [ ] Group 9: Day type (bits 48-51)
- [ ] `evaluate_all_atoms(bar, indicators, proximity_bps) -> u64`
- [ ] Property test P-11 (mask commutativity) green
- [ ] **`tests/golden/condition_truth_table.csv` fully populated (≥200 rows)**
- [ ] **`tests/golden/test_condition_vocabulary.py`** — 100% match required

#### Conflict graph + pattern generator

- [ ] `brutex/strategies/vocabulary/conflict_graph.py`
- [ ] 52×52 boolean conflict matrix (e.g., bit 0 + bit 1 are mutually exclusive)
- [ ] `brutex/strategies/pattern_generator.py`
- [ ] Generates ~1.2-1.5M survivors after pruning
- [ ] Property tests for conflict-pair detection

### Week 7 — Phase 2 sign-off

- [ ] First incremental pull on Day 1 — verify all 4 instruments + 21 TFs
- [ ] Day 5 incremental run — only ~4 chunks fetched, ~5 sec wall
- [ ] Audit chain verifies clean
- [ ] All Phase 2 property tests green
- [ ] All Phase 2 golden tests green
- [ ] **Spawn 4-agent hostile review** on Phase 2 deliverables
- [ ] Hostile review findings documented + fixed
- [ ] **User signs off** → ready for Phase 2.5

## Verification gates

- [ ] `pytest tests/property` — all Phase 2 invariants green (no longer skipping)
- [ ] `pytest tests/golden` — all 5 fixtures match expected SHA256
- [ ] `pytest tests/golden/test_condition_vocabulary.py` — 100% truth table match
- [ ] `pytest tests/bench/test_hotpath_o1.py` — bar lookup, indicator get, mask AND all O(1)
- [ ] `brutex pull` — completes for 5y × 4 inst × 21 TFs
- [ ] `brutex doctor` — all GREEN
- [ ] 22 CI gates green
- [ ] 4-agent hostile review report

## Exit criteria

- [ ] Groww ingest of 5y × 4 instruments × 21 TFs working end-to-end
- [ ] 9 indicators precomputed and cached
- [ ] 52 atomic conditions evaluable on any bar
- [ ] Pattern generator emits the conflict-pruned set
- [ ] Day 5 incremental run < 5 sec wall (proves watermark works)
- [ ] Bar identity hash deterministic across runs
- [ ] CI 22 gates green
- [ ] Phase 2 hostile review report green
- [ ] User signs off

## Files to create / modify

```
brutex/adapters/groww/__init__.py                   (new)
brutex/adapters/groww/constants.py                  (new, 4 hardcoded tuples)
brutex/adapters/groww/auth.py                       (new)
brutex/adapters/groww/client.py                     (new)
brutex/adapters/groww/intraday.py                   (new)
brutex/adapters/groww/historical.py                 (new)
brutex/data/parser_groww.py                         (new)
brutex/data/watermark.py                           (new)
brutex/data/bar_hash.py                            (new)
brutex/data/partition.py                           (extend VALID_INTERVALS + VALID_SEGMENTS)
brutex/data/parquet_writer.py                      (allow current-month grow)
brutex/data/models.py                              (add int64 paisa columns to CandleBar)
brutex/feed/resampler.py                           (09:15 IST anchor, 21-TF cascade)
brutex/strategies/vocabulary/conflict_graph.py     (new)
brutex/strategies/pattern_generator.py             (new)
rust/fno-math/src/indicators/ema.rs                (new)
rust/fno-math/src/indicators/day_extremes.rs       (new)
rust/fno-math/src/indicators/pdh_pdl.rs            (new)
rust/fno-math/src/indicators/daily_pivots.rs       (new)
rust/fno-math/src/indicators/daily_fib.rs          (new)
rust/fno-math/src/indicators/candle_meta.rs        (new)
rust/fno-math/src/vocabulary/atoms.rs              (new, 52 evaluators)
tests/golden/fixtures/*.json                       (5 real fixtures filled in)
tests/golden/condition_truth_table.csv             (≥200 rows)
tests/golden/test_condition_vocabulary.py          (full impl)
```

## Risk register

| Risk | Mitigation |
|---|---|
| Groww API returns IST-shifted epoch (not UTC) | First call validates with timestamp decode test — see decision #16 |
| Groww rate limit lower than 5 r/s in practice | Adapter cap at 3 r/s effective; live verification first call |
| First-call security ID 13/25/51/21 broken | Quarterly smoke test (4 URLs); manual escalation via constants update |
| Master CSV needed for instrument validation | NOT NEEDED — universe is fixed per decision #87 |
| Indicator math differs from TradingView pin | Golden fixture has TradingView-derived expected values within 0.5% |
| Resampler 09:15 IST anchor edge cases (e.g., 25min spec interval that's not used) | Restrict source to 1-minute base only; whitelist 21 derived targets |
| pyarrow version drift breaks Parquet determinism | Pin to exact polars version + golden test on byte hashes |

## Decisions to lock (estimated #111-#125)

- #111: Groww adapter shipped (4-tuple constants, auth, client, intraday, historical)
- #112: Authentication manual-paste + auto-renew live
- #113: Watermark table in DuckDB
- #114: bar_hash function in code
- #115: Int64 paisa columns in lake
- #116: VALID_INTERVALS extended to 21 names
- #117: VALID_SEGMENTS adds IDX_I
- #118: Resampler 09:15 IST anchor
- #119: 9 indicators in Rust
- #120: 52-condition vocabulary in Rust
- #121: Conflict graph pruning
- #122: Pattern generator
- #123: 5 golden fixtures populated
- #124: Condition truth table (200 rows)
- #125: Phase 2 4-agent hostile review pass

## Phase 2 entry / exit summary

| | Before | After |
|---|---|---|
| Groww adapter | none | 5 modules, working |
| Lake data | empty | 5y × 4 inst × 21 TFs (~80 MB Parquet) |
| Watermark table | absent | 84 rows live |
| 9 indicators | absent | all in Rust, cached |
| 52 vocabulary atoms | spec only | all in Rust + 200-row truth table |
| Pattern generator | absent | emits 1.2-1.5M survivors |
| MIGRATION decisions | ~110 | ~125 |
| Phase 3 readiness | n/a | READY |
