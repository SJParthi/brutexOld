# Track 2 — Observability Specification (Tracking, Logging, Dashboard, Alerts)

**Status:** DRAFT 2026-05-16 (operator-requested consolidation).
This document consolidates observability requirements scattered across
`TRACK1_RUNTIME_ARCHITECTURE.md`, `TRACK2_OPERATIONAL_GUARANTEES.md`,
and `CLAUDE.md` into a single reference.

**Purpose:** Specify what BruteX captures, where it logs, how it
visualizes, when it alerts, and what guarantees the operator gets.

**Scope:** Track 1 + Track 2 combined — observability is cross-cutting.

---

## 1. The 4 pillars

| Pillar | What it does | Primary tool | Doc |
|---|---|---|---|
| **TRACKING** | every material event recorded | Audit JSONL hash-chain | `TRACK2_OPERATIONAL_GUARANTEES §5` |
| **LOGGING** | structured logs from every module | `structlog` → file + stdout | `TRACK1_RUNTIME_ARCHITECTURE §6.1` |
| **DASHBOARD** | live view of state + history | FastAPI on port 9080 | `TRACK1_RUNTIME_ARCHITECTURE §6.2` |
| **ALERTING** | operator notified on RED events | Telegram + dashboard banner | `TRACK2_OPERATIONAL_GUARANTEES §6` |

All four are MANDATORY. Each has a guarantee SLA documented below.

---

## 2. Tracking — what gets captured

Every material action emits one line to `audit/runs/<run_id>.jsonl`.
"Material" = any state change that affects correctness or reproducibility.

### 2.1 Event taxonomy

| Event class | Examples | Frequency |
|---|---|---|
| `ingest.chunk` | one Groww chunk fetched + persisted | ~92/day initial, ~4/day incremental |
| `ingest.error` | Groww API error or quarantine | rare |
| `combo.run.start` | brute-force combo evaluation begins | ~33B over a sweep |
| `combo.run.complete` | combo evaluation finishes with metrics | ~33B over a sweep |
| `combo.skip` | combo deduplicated by Bloom or LMDB | varies |
| `stage.transition` | funnel stage 1A → 1B → 2 etc | a few per run |
| `lockbox.access` | last-6-month data accessed (CRITICAL audit) | only in Stage 4.7 |
| `audit.chain.break` | hash chain integrity violation detected | NEVER (fatal) |
| `watchdog.restart` | engine auto-restarted after crash | rare |
| `operator.kill` | operator halted the sweep | rare |
| `cost.calc` | per-trade cost computed (Track 2) | ~440K per execution-sim batch |
| `liquidity.reject` | trade candidate rejected by OI/spread filter | varies |
| `tax.year.computed` | annual tax aggregation produced | once per FY |

### 2.2 Audit row schema

```python
@dataclass(frozen=True)
class AuditRow:
    ts_utc:        str          # RFC3339 with µs precision
    run_id:        str          # ULID of the sweep run
    event_class:   str          # from §2.1 taxonomy
    severity:      Literal["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]
    actor:         str          # module name emitting the row
    payload:       dict         # event-specific structured payload
    prev_hash:     str          # blake3 of previous row (chain)
    row_hash:      str          # blake3 of this row excluding row_hash
```

### 2.3 Hash chain guarantee

Per `TRACK2_OPERATIONAL_GUARANTEES §5`: any tampering with a past audit
row breaks the chain at that point. `brutex doctor` check C7 verifies
the chain on every workday boot. CI also runs a daily chain-verify on
the last 30 days of audit rows.

### 2.4 Storage

- File: `audit/runs/<run_id>.jsonl` (one file per sweep run)
- Compressed daily: `audit/daily/<YYYY-MM-DD>.jsonl.zst`
- Retention: forever (audit rows are small, ~200 bytes each)
- Estimated annual size: ~10 GB per active sweep day, archived = ~50 GB/yr

---

## 3. Logging — structured app logs

Every Python module uses `structlog` exclusively. NO `print()` statements
(enforced by ruff rule).

### 3.1 Log levels + destinations

| Level | Stdout | File `logs/brutex.jsonl` | Prometheus counter |
|---|---|---|---|
| DEBUG | only if `BRUTEX_LOG=DEBUG` | always | no |
| INFO | always | always | no |
| WARN | always | always | `brutex_warns_total` |
| ERROR | always | always | `brutex_errors_total` |
| CRITICAL | always (red) | always | `brutex_criticals_total` + alert fires |

### 3.2 Structured fields (mandatory on every log line)

```python
{
    "ts_utc":     "2026-05-16T18:24:51.123456Z",
    "level":      "INFO",
    "module":     "brutex.orchestrator.scheduler",
    "run_id":     "01HXYZ...",
    "trace_id":   "...",
    "msg":        "human readable summary",
    # ... event-specific fields ...
}
```

### 3.3 Rotation + retention

- `logs/brutex.jsonl` rotates at 100 MB, compressed with Zstd
- Active logs: keep last 30 days uncompressed
- Archive logs: keep forever, compressed
- Annual log storage: ~5-20 GB

### 3.4 Forbidden patterns

| Forbidden | Use instead |
|---|---|
| `print(x)` | `log.info("x", value=x)` |
| `logging.info(...)` (stdlib) | `structlog.get_logger().info(...)` |
| string-format messages | structured kwargs |
| bare `except:` | named exception types |

---

## 4. Dashboard — FastAPI on port 9080

### 4.1 Pages

| Path | What it shows |
|---|---|
| `/` | live system status — green/yellow/red per component |
| `/run/{run_id}` | per-sweep run detail — progress bar, ETA, throughput |
| `/results` | searchable leaderboard of strategies |
| `/results/{combo_hash}` | drill-down into one combo — trades, equity curve |
| `/audit` | audit chain viewer with hash-chain verification status |
| `/doctor` | rendered output of `brutex doctor` 10 C-checks |
| `/data` | data lake explorer — Parquet partitions, watermarks |
| `/costs` | cost-model breakdown for a sample trade (interactive) |
| `/tax` | annual tax aggregator (interactive — enter capital, regime) |

### 4.2 Visualization

Per `TRACK2_OPERATIONAL_GUARANTEES §8`:

- **Sankey-style ingest flow** showing daily completion per
  (instrument × timeframe × pipeline stage)
- **Equity curve** per strategy with drawdown shading
- **Trade ledger heatmap** — bars colored by win/loss
- **Strike-sweep distribution** — for each combo, post-cost PnL across
  21 strike offsets as a horizontal bar chart
- **Strategy comparison view** — pick 2-5 strategies, see equity curves
  overlaid + metric table side-by-side
- **Audit chain visualizer** — Merkle-tree-style display, click to see
  the row and its hash inputs
- **Calendar heatmap** — daily PnL by date (5-year view)

### 4.3 Real-time updates

- WebSocket endpoint `/ws/status` pushes status updates every 5 seconds
- WebSocket endpoint `/ws/progress/{run_id}` pushes progress during a sweep
- Plain HTTP `GET /api/v1/...` for snapshot queries

### 4.4 Authentication

- Bound to `127.0.0.1` only (env `BRUTEX_HOST_BIND`)
- No auth required for localhost (single-operator design)
- If exposed via SSH tunnel, operator-managed credentials

### 4.5 Performance budget

- All pages render in < 500 ms (P99) on a 5-year-old MacBook
- API queries < 100 ms (P99) for snapshots
- DuckDB-backed leaderboard query < 2 s for top-100 (across 2.5 TB Parquet)

---

## 5. Prometheus + Metrics

### 5.1 Endpoint

`http://127.0.0.1:9081/metrics` — Prometheus exposition format.

### 5.2 Core counters / gauges

| Metric | Type | Description |
|---|---|---|
| `brutex_combos_evaluated_total` | counter | total brute-force combos processed |
| `brutex_combos_skipped_total{reason=...}` | counter | combos dedup-skipped, by reason |
| `brutex_ingest_chunks_total{instrument,tf}` | counter | chunks fetched per series |
| `brutex_ingest_errors_total{kind}` | counter | ingest errors by kind |
| `brutex_throughput_trials_per_sec` | gauge | current sweep throughput |
| `brutex_rate_limiter_tokens_available` | gauge | Groww rate-limit headroom |
| `brutex_audit_chain_intact` | gauge (0/1) | chain integrity check result |
| `brutex_watermark_seconds_behind{instrument,tf}` | gauge | staleness per series |
| `brutex_storage_bytes_used` | gauge | Parquet lake size |
| `brutex_lockbox_accessed_total` | counter | NEVER incremented outside Stage 4.7 |

### 5.3 Histograms

| Metric | Description |
|---|---|
| `brutex_combo_eval_duration_seconds` | per-combo eval time |
| `brutex_ingest_chunk_duration_seconds` | per-Groww-chunk fetch time |
| `brutex_dashboard_request_duration_seconds` | per-API-request latency |
| `brutex_cost_calc_duration_seconds` | per-trade cost-stack compute time |

### 5.4 Alerts (Prometheus AlertManager-style rules)

Defined in `prometheus/alerts.yml`. Each rule has `severity` + `for` (sustain duration):

| Alert | Severity | Sustain | Channel |
|---|---|---|---|
| `BruteXAuditChainBroken` | critical | 0s | Telegram immediate + page |
| `BruteXIngestStale` | high | 60s | Telegram + dashboard red |
| `BruteXThroughputLow` | medium | 10m | Dashboard yellow |
| `BruteXLockboxAccessed` | critical | 0s | Telegram immediate + audit |
| `BruteXRateLimitNearCap` | medium | 5m | Dashboard yellow |
| `BruteXStorageNearFull` | high | 1h | Telegram + dashboard banner |

---

## 6. Alerting — Telegram + dashboard + email digest

Per `TRACK2_OPERATIONAL_GUARANTEES §6`. Re-confirmed by operator 2026-05-15
to override the older "no Telegram for now" decision.

### 6.1 Channels

| Channel | Latency target | Use case |
|---|---|---|
| Telegram bot | < 60 sec | CRITICAL + HIGH events |
| Dashboard banner | < 5 sec (WebSocket) | all RED/YELLOW visible to operator |
| Email digest | 07:35 IST daily | overnight summary + audit-chain status |
| macOS notification | < 5 sec | operator-side fallback if Telegram down |

### 6.2 Alert matrix

Locked at `TRACK2_OPERATIONAL_GUARANTEES §6.1`. Reproduced here:

| Event | Severity | Telegram | Dashboard | Doctor exit |
|---|---|---|---|---|
| Audit chain broken | CRITICAL | immediate | red banner | 1 |
| Watermark stale > 1 day on active series | HIGH | within 60s | red row | 1 |
| Groww API > 5% error rate over 1h | HIGH | within 60s | yellow row | 0 (warn) |
| Backfill rate < 2 r/s for 10 min | MEDIUM | daily digest | yellow row | 0 |
| Put-call parity violation (P-O10) | HIGH | within 60s | red row | 1 |
| Doctor C12 OPTIONS_FRESH red | HIGH | within 60s | red row | 1 |
| Greek NaN / inf detected | MEDIUM | daily digest | yellow row | 0 |
| Bloom false-positive rate > 1% | LOW | daily digest | informational | 0 |
| Storage > 80% full | MEDIUM | once/day | yellow banner | 0 |
| Backfill complete | INFO | success ping | green banner | 0 |
| Lockbox accessed | CRITICAL | immediate | red banner | 1 |
| Stage 5 execution-sim done | INFO | success ping | green banner | 0 |
| Throughput < 30K trials/sec | HIGH (SLA at risk) | within 60s | red row | 1 |

### 6.3 Telegram message format

```
🔴 [CRITICAL] BruteX audit-chain broken

run_id:    01HXYZ...
ts:        2026-05-16T18:24:51 UTC
broke at:  audit/daily/2026-05-15.jsonl row 87234
prev_hash: 7f3e...
expected:  a9b1...
got:       c4d2...

actions:
  • Check disk integrity (smartctl)
  • Re-verify last clean snapshot
  • Run: brutex audit verify --since 2026-05-14
```

Long messages get truncated; full payload always in dashboard.

### 6.4 Operator action contract

When operator receives a Telegram alert, the dashboard must already
have:
1. The exact audit row(s) related to the alert
2. A "click to acknowledge" button
3. A "click to halt sweep" button (for CRITICAL only)
4. The relevant `brutex doctor` C-check output rendered

Maximum operator-side clicks to triage: 3 (open Telegram, tap link, see context).

---

## 7. `brutex doctor` — the daily integrity check

The 10 (now 12) C-checks every workday morning at 07:30 IST.

| ID | Check | Severity |
|---|---|---|
| C1 | `data.watermark` — every active series has fresh data | HIGH |
| C2 | `audit.chain.intact` — hash chain unbroken last 30 days | CRITICAL |
| C3 | `bloom.fp_rate` — Bloom false-positive rate ≤ 1% | LOW |
| C4 | `lmdb.consistency` — work_items table consistent | HIGH |
| C5 | `parquet.partitions` — no missing partitions | HIGH |
| C6 | `storage.headroom` — free disk ≥ 200 GB | MEDIUM |
| C7 | `groww.rate_limit_headroom` — token bucket ≥ 50% | LOW |
| C8 | `combo_hash.invariant` — content-hash spot-check | HIGH |
| C9 | `telegram.delivery` — Telegram bot reachable | MEDIUM |
| C10 | `code_commit_match` — running version matches last released | LOW |
| C11 | `throughput.canary_fresh` — canary measured ≤ 14 days ago | HIGH |
| C12 | `options_fresh` — Track 2 options data current (Track 2 only) | HIGH |

`brutex doctor` exits 0 (all green), 1 (any RED), 2 (only YELLOW).

---

## 8. Guarantees + SLAs (the contract)

| Guarantee | SLA | Verified by |
|---|---|---|
| Every material action audited | within 100 ms of action | unit test on audit emitter |
| Hash chain intact | always | `brutex doctor C2` daily |
| CRITICAL alert reaches operator | within 60 sec | Telegram bot heartbeat + delivery receipt |
| Dashboard accurate | within 5 sec of underlying state | WebSocket push test |
| `brutex doctor` runs every workday | 07:30 IST cron | GHA cron doctor-daily.yml (CI surface) + verify-m4pro.yml (production surface, M4 Pro); "alert if missed" still open — DEC-DOCTOR-DAILY-CRON-001 |
| Backtest reproducible | bit-for-bit identical | `audit replay` CLI |
| No data loss on crash | guaranteed via checkpointing | property test P-AUDIT-1 |

If any SLA is violated, the operator gets a `CRITICAL` Telegram alert.

---

## 9. Property invariants (Phase 1 tests)

| ID | Invariant | Failure mode caught |
|---|---|---|
| P-OBS-1 | every code path that mutates state emits an audit row | silent state change |
| P-OBS-2 | audit row `row_hash` recomputes correctly on replay | hash chain corruption |
| P-OBS-3 | `print()` statements absent from `brutex/` package | ruff rule |
| P-OBS-4 | every CRITICAL log triggers a Telegram alert | alert wiring broken |
| P-OBS-5 | dashboard `/` returns 200 within 500ms (P99) | dashboard regression |
| P-OBS-6 | `brutex doctor` exit code matches RED/YELLOW worst severity | doctor incorrectness |
| P-OBS-7 | Prometheus `/metrics` endpoint always reachable | exporter broken |

---

## 10. Decision IDs (logged in MIGRATION.md)

| ID | Decision | Rationale |
|---|---|---|
| DEC-OBS-001 | 4 pillars: tracking + logging + dashboard + alerts (NONE optional) | operator decree "extreme comprehensive automation monitoring alerting notifying" |
| DEC-OBS-002 | Audit JSONL hash-chain is the source of truth for tracking | tamper-evident; replay-oracle for backtests |
| DEC-OBS-003 | `structlog` exclusively; no `print()` ever | uniform structured fields enable querying |
| DEC-OBS-004 | FastAPI dashboard on `127.0.0.1:9080` | single-operator localhost-only |
| DEC-OBS-005 | Telegram re-enabled for Track 2+ (override of Decision #78) | operator decree 2026-05-15 |
| DEC-OBS-006 | Max 3 operator-side clicks from alert to context | usability commitment |
| DEC-OBS-007 | All audit rows kept FOREVER (compressed) | reproducibility forever |
| DEC-OBS-008 | `brutex doctor` daily at 07:30 IST mandatory | proactive health checks |

---

## 11. Sources

- `docs/TRACK1_RUNTIME_ARCHITECTURE.md §6` — dashboard + metrics architecture
- `docs/track2/TRACK2_OPERATIONAL_GUARANTEES.md §5, §6, §8` — audit chain + alerting + visualization
- `docs/CLAUDE.md §10` — observability rules
- `docs/MIGRATION.md #78, #99-bis` — Telegram decision history

---

## 12. Amendment log

| Date | Change | Reason |
|---|---|---|
| 2026-05-16 | Initial draft | Operator request to consolidate observability into single ref doc with explicit guarantees + SLAs |
