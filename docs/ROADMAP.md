# BruteX — Roadmap & Storage Guarantees

> Living status of the 4-phase Groww ingest pipeline, the O(1) + uniqueness
> + dedup guarantees of the persistence layer, and the work still open.
>
> Complements `docs/REQUIREMENTS.md` (the frozen functional spec). This
> file is the operational tracker; `REQUIREMENTS.md` is the contract.

Last updated: 2026-04-15

## 1. What is implemented — end-to-end Groww ingest

The Groww 4-phase pipeline is implemented and wired into the CLI. Every
phase persists into a primary-keyed store so reruns are idempotent and
every lookup is O(1)/O(log n) on an index.

| Phase | Pulls                              | Persists to                                | Primary key                                                              | Code path                                                                |
|-------|------------------------------------|--------------------------------------------|--------------------------------------------------------------------------|--------------------------------------------------------------------------|
| A     | Instrument master CSV (~100k rows) | `instruments.duckdb` → `instruments`       | `(groww_symbol, snapshot_date)`                                          | `brutex/adapters/groww/instruments.py` → `InstrumentStore.upsert_many`   |
| B     | Expiry dates per underlying        | `instruments.duckdb` → `expiries`          | `(exchange, underlying_symbol, expiry_date, snapshot_date)`              | `brutex/adapters/groww/expiries.py` → `InstrumentStore.upsert_expiries`  |
| C     | Legs per (underlying, expiry)      | `instruments.duckdb` → `contracts`         | `(contract_symbol, snapshot_date)`                                       | `brutex/adapters/groww/contracts.py` → `InstrumentStore.upsert_contracts`|
| D     | Historical OHLC candles per leg    | `lake/<partitioned Parquet>` + `catalog.duckdb` → `partitions` | `(exchange, segment, symbol, interval, year, month)` | `brutex/adapters/groww/candles.py` → `ParquetBarSink` + `Catalog.upsert` |

Plus a fifth metadata table in `instruments.duckdb`:

| Table             | PK                | Purpose                                            |
|-------------------|-------------------|----------------------------------------------------|
| `instrument_runs` | `run_id`          | One row per Phase A run — audit trail + counts     |
| `schema_meta`     | `key`             | Schema version + migration metadata                |

## 2. CLI surface (all shipped)

```
brutex instruments --lake-root DIR    # Phase A          → instruments.duckdb
brutex expiries    --lake-root DIR    # Phase A → B      → instruments.duckdb
brutex contracts   --lake-root DIR    # Phase A → B → C  → instruments.duckdb
brutex candles     --lake-root DIR    # Phase A → B → C → D  → Parquet + catalog.duckdb
brutex ingest all  --lake-root DIR    # alias for `candles` (full pipeline)
brutex dashboard   --lake-root DIR    # FastAPI UI on top of instruments.duckdb
```

## 3. Storage guarantees — O(1) + uniqueness + dedup

Every persistence surface uses a **composite primary key**. DuckDB
implements PKs with an **ART (Adaptive Radix Tree)** index, which gives
effective O(1) point lookup on fixed-size keys and O(k) on variable-size
keys where `k` is the key length — not O(n), not a table scan.

### 3.1 Uniqueness
The PK is a hard uniqueness constraint. Any second insert with the same
PK fails the `INSERT` at the SQL layer. We never check uniqueness in
Python code — the DB is the single source of truth.

### 3.2 Dedup / idempotent upsert
Every `upsert_*` method uses a single transaction:

```sql
BEGIN;
DELETE FROM <table> WHERE <pk_columns> IN (?, ?, …);
INSERT INTO <table> VALUES …;
COMMIT;
```

So rerunning the same phase with the same snapshot_date is a **no-op in
effect**: the DELETE removes the old row, the INSERT re-adds an
identical row, and the PK stays unique. No duplicates can sneak in.

### 3.3 O(1) / O(log n) lookup
Beyond the PK ART, we also create 8 secondary indexes on the hot query
paths:

```
idx_instr_segment        (segment,    snapshot_date)
idx_instr_underlying     (underlying_symbol, snapshot_date)
idx_instr_type           (instrument_type,   snapshot_date)
idx_instr_expiry         (expiry_date,       snapshot_date)
idx_instr_in_scope       (in_scope,          snapshot_date)
idx_exp_underlying       (underlying_symbol, snapshot_date)
idx_con_underlying_expiry(underlying_symbol, expiry_date, snapshot_date)
idx_con_expiry           (expiry_date,       snapshot_date)
```

Every dashboard filter path (by underlying, by type, by segment, by
expiry) hits one of these.

### 3.4 Catalog PK (for Phase D Parquet partitions)
```
partitions(exchange, segment, symbol, interval, year, month) PRIMARY KEY
```
One row per month-partition file on disk. Prevents a partition from ever
being registered twice; the `raw_hash` column is an extra drift guard.

## 4. Mapping — how the four phases chain

```
A: CSV → Instrument rows
            │
            │ underlying_symbol, segment=FNO
            ▼
B: Instrument (index) → Expiry dates
            │
            │ (underlying, expiry_date)
            ▼
C: (underlying, expiry) → Contract legs (FUT / CE / PE @ strikes)
            │
            │ contract_symbol, interval, [start, end]
            ▼
D: Contract → OHLC candles → Parquet partitions + catalog row
```

The mapping is **one-directional and exact** — Phase D's work list is
produced by iterating `contracts` filtered by in-scope underlyings.
Nothing is made up, nothing is inferred.

## 5. Still open (tracked here, not in REQUIREMENTS.md)

### 5.1 Correctness / drift
- CSV sha256 drift detection — cross-check Groww CSV vs NSE `fomon.zip`
  so a silent upstream schema change is caught before ingest
- Golden-file test pinning `(csv_sha256 → expected filtered row count)`
  per underlying — prevents silent universe drift
- `brutex audit verify` auto-run after each phase so the hash-chained
  audit log is checked on every ingest (CLAUDE.md §2.9)

### 5.2 Docs
- `docs/REQUIREMENTS.md` needs a new section for Phase B/C/D +
  InstrumentStore schema + catalog PK (the spec is frozen but should be
  amended — this is a material addition)

### 5.3 Test coverage — lift modules back out of the omit list
- `brutex/cli.py` — needs a CLI snapshot test harness
- `brutex/dashboard/app.py` — has integration tests; needs gate reruns
  with `.[data]` extras installed
- `brutex/data/instrument_store.py` — same
- `brutex/feed/replay.py`, `brutex/strategies/runner.py`,
  `brutex/strategies/sweep.py`, `brutex/execution/driver.py` — same

### 5.4 Blocked (need operator action)
- Real Groww API contract test — needs `GROWW_API_AUTH_TOKEN` GH secret
- Mac M4 benchmark suite — needs a self-hosted runner
- `fno-math-py` in the CI Rust gate — needs a workflow change to
  `maturin build` and `pip install` the wheel before pytest

## 6. Proof

`scripts/proof_storage_guarantees.py` runs the following live
assertions against a throwaway on-disk store:

1. Open an empty `InstrumentStore` + `Catalog`
2. Insert 1k instruments, 1k expiries, 1k contracts, 1k catalog entries
3. Verify row counts via `SELECT COUNT(*)`
4. **Uniqueness** — attempt to insert a duplicate PK, assert IT FAILS
5. **Dedup** — rerun the exact same upsert, assert row count is unchanged
6. **O(1)** — time a PK-bound `SELECT` at 1k vs 10k rows; assert the
   latency ratio is < 3× (sublinear → effectively constant)
7. Print the 4 PK definitions from `information_schema`

Run it with:
```
python scripts/proof_storage_guarantees.py
```

See `scripts/proof_storage_guarantees.py` for the full script.
