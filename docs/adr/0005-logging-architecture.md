# ADR 0005 — Ingest logging architecture (event taxonomy, levels, pull_id)

- **Status:** Accepted (operator directive 2026-06-03: "proper professional
  in-depth detailed logging architecture … log everything … only then we
  will know every nook and corner")
- **Decision id:** DEC-DHAN-INGEST-LOGGING-001 (MIGRATION.md #426)
- **Builds on:** DEC-DHAN-CALL-LATENCY-LOGGING-001 (#419, the transport layer)
  · DEC-DHAN-RATE-LOGGING-001 (#420, the rate controller) · ADR 0002 (the live
  vendor is Dhan Data API v2)
- **Scope:** the **observability** of the spot + options ingest path
  (`brutex/adapters/dhan/{ingest,option_ingest,run_ingest,option_universe_ingest}.py`
  + the dashboard pull controllers). **No ingest behaviour changes** — outputs
  are byte-identical; this ADR only fixes the event vocabulary that makes the
  existing flow traceable. The BRUTE-FORCE ENGINE is UNCHANGED (CLAUDE.md §2
  Rule 3).

## Operator directive

> "proper professional in-depth detailed logging architecture … log
> everything … only then we will know every nook and corner" (2026-06-03).

## Context — the "what's lit vs dark" roadmap

DEC-DHAN-RATE-LOGGING-001 (#420) opened a staged roadmap to light every dark
layer of the adapter / ingest path. Two layers were already lit:

- **#419 — transport.** `DhanClient._observe` emits `dhan.call` (DEBUG, per
  HTTP round-trip: endpoint · status · latency_ms) + `dhan.throughput` (INFO,
  every 25th call: effective r/s + latency avg/min/max/p95).
- **#420 — rate controller.** `DhanRateController` emits `rate.429` (WARNING),
  `rate.stepdown` (WARNING), `rate.stepup` (INFO).

Everything **between** the pull button and the HTTP call was DARK. An operator
who saw "NIFTY pulled 0 bars" could not tell whether a window was skipped (the
coverage ledger), the vendor returned nothing (parse), the session filter
dropped every bar (parse), the sink rejected them (lake-write), or the series
was quarantined on a terminal error (quarantine-cause) — and there was **no
correlation id** to follow one pull across the 16k-call options fan-out.

## Decision

### 1. Event taxonomy (the `ingest.*` namespace)

A new `ingest.*` namespace lights the spine, sitting between the pull
controllers' `universe.*` events and the transport's `dhan.*` events:

| Event | Level | Emitted by | Granularity | Key fields |
|-------|-------|-----------|-------------|-----------|
| `ingest.window` | DEBUG | `ingest_spot`, `ingest_rolling_option_series` | per fetch window | `pull_id`, series coords, `window_start`, `window_end`, `action` (`fetched`/`skipped`) |
| `ingest.parse` | DEBUG | `ingest_spot`, `ingest_rolling_option_series` | per fetched window | `pull_id`, series coords, `raw_bars`, `session_bars` |
| `ingest.lake_write` | DEBUG | `ingest_spot`, `ingest_rolling_option_series` | per fetched window | `pull_id`, series coords, `bars_appended` |
| `ingest.series` | DEBUG | `ingest_spot`, `ingest_rolling_option_series` | per series `(symbol,interval)` / `(sid,flag,code,strike,type)` | `pull_id`, series coords, `windows`, `bars_appended`, `transient_failures`, `skipped_windows` |
| `ingest.underlying` | INFO | `run_dhan_spot_ingest`, `run_dhan_options_ingest` | per underlying (rollup) | `pull_id`, `symbol`/`security_id`, `series_completed`, `series_failed`, `bars_appended`, `windows` |
| `ingest.quarantine` | WARNING | `run_dhan_spot_ingest`, `run_dhan_options_ingest` | per terminal failure | `pull_id`, series coords, `error` |

"Series coords" = `{symbol, interval}` for spot;
`{security_id, expiry_flag, expiry_code, strike, drv_option_type}` for options.

A **fetched** window emits three DEBUG lines (`ingest.window` + `ingest.parse`
+ `ingest.lake_write`); a **skipped** window emits one (`ingest.window`,
`action="skipped"`) and nothing else, because nothing was fetched, parsed, or
written. Every series closes with one `ingest.series` summary.

### 2. Levels — why DEBUG vs INFO vs WARNING

The level law follows CLAUDE.md §2 Rule 6's "never flood": a full options pull
fans out to **~16k series** (≈252 series/underlying × the F&O universe), each
tiling into 30-day windows. Per-window / per-series detail at INFO would bury
the signal.

- **DEBUG** — per-window + per-series detail (`ingest.window`, `ingest.parse`,
  `ingest.lake_write`, `ingest.series`) and the transport's `dhan.call`. Off by
  default; turned on (`configure_logging(level="DEBUG")`) for a deep dive.
- **INFO** — rollups only: the pull boundaries (`universe.*.start/done`), the
  per-underlying `ingest.underlying`, the transport's periodic
  `dhan.throughput`, and `rate.stepup`. One INFO line per underlying keeps a
  full-universe pull readable (~241 lines, not millions).
- **WARNING** — anything an operator must notice: `ingest.quarantine` (a series
  dropped on a terminal Dhan error), `rate.429`, `rate.stepdown`, and the
  pull-level `universe.pull_options.failures` digest.

### 3. The `pull_id` contract

Every pull is correlated by one **`pull_id`** so an operator can grep a single
pull end-to-end across all three layers.

- **Generation + binding.** Each pull core (`pull_candles`, `pull_all_spot`,
  `pull_options`) opens `brutex.observability.correlation.pull_id_scope(pull_id)`.
  A `None` `pull_id` (the default) mints a fresh 12-hex id via `new_pull_id()`;
  a caller MAY pass an explicit id to correlate with an external job. The scope
  **binds** the id into the structlog context-vars
  (`structlog.contextvars.bound_contextvars`) for the whole pull and **always
  unbinds on exit** (nesting-safe restore), so an id never leaks into a later
  pull or an unrelated request on the same thread.
- **Free propagation to the lit layers.** `merge_contextvars` is first in the
  configured processor chain (`brutex.observability.logging._shared_processors`),
  so the bound `pull_id` is merged into EVERY downstream event — including the
  already-lit `dhan.call` / `dhan.throughput` / `rate.*` events — with **zero
  signature plumbing**. This is the entire value of a context-var correlation
  id: light the spine once, correlate everything.
- **Explicit re-stamp on the spine events.** The spine functions read the id
  back via `current_pull_id()` and stamp it as an **explicit** field on each
  `ingest.*` event. This is required by the test oracle:
  `structlog.testing.capture_logs` deliberately strips the contextvars
  processor, so a context-var-only id would be invisible to `capture_logs`
  assertions. Re-stamping makes each event's `pull_id` directly assertable
  (and is a harmless no-op against `merge_contextvars` in production — the
  explicit field wins, the value is identical).

This is **observability only** (Golden Rule #10): `pull_id_scope` binds a log
field and changes no ingest behaviour. Outputs are byte-identical with or
without a scope (a spine function called outside any scope logs a benign
`pull_id=""`).

## Lit vs dark after this ADR

| Layer | Status | Events |
|-------|--------|--------|
| Pull boundary (controllers) | ✅ LIT | `universe.pull[_all\|_options].{start,done,failures}` |
| Per-underlying rollup | ✅ LIT (this ADR) | `ingest.underlying` |
| Per-series | ✅ LIT (this ADR) | `ingest.series` |
| Per-window (fetch/skip) | ✅ LIT (this ADR) | `ingest.window` |
| Parse (decode + session gate) | ✅ LIT (this ADR) | `ingest.parse` |
| Lake-write (rows persisted) | ✅ LIT (this ADR) | `ingest.lake_write` |
| Quarantine cause | ✅ LIT (this ADR) | `ingest.quarantine` |
| Rate controller | ✅ LIT (#420) | `rate.429`, `rate.stepdown`, `rate.stepup` |
| Transport (HTTP) | ✅ LIT (#419) | `dhan.call`, `dhan.throughput` |
| **Resampler (1m → 19 TFs)** | ⬛ DARK | — (tracked follow-up) |
| **Instrument master load / refresh** | ⬛ DARK | — (tracked follow-up) |
| **Bloom + LMDB dedup** | ⬛ DARK | — (tracked follow-up) |
| **Parquet writer internals** | ⬛ DARK | — (`ingest.lake_write` reports the count at the boundary; the writer's partition/row-group detail is still dark) |
| **DuckDB catalog / audit hash-chain** | ⬛ DARK | — (separate observability slice) |

The ingest **pull path** an operator drives from the dashboard is now fully lit:
window → parse → lake-write → series → underlying, plus quarantine, rate, and
transport — all under one `pull_id`. The lake's downstream transforms
(resample, dedup, catalog) remain dark and are the next roadmap slices.

## Consequences

- **Diagnosable in one grep.** `pull_id=<id>` returns the full story of a pull:
  which windows were skipped vs fetched, how many bars the vendor returned vs
  how many survived the session gate, how many reached Parquet, which series
  were quarantined and why, how the rate controller behaved, and the per-call
  latency.
- **Strict gate held.** Every new event sits inside an existing branch, so the
  per-module 100% line+branch gate stands; each event + its fields is asserted
  via `capture_logs` (no network).
- **No new dependency / no behaviour change.** Reuses the configured structlog
  chain; the one new module (`correlation.py`) imports only structlog + stdlib.
