## 8. Hardware Envelope (for honest SLA claims)

**Track 1 reference:** Apple M4 Pro, 14-core CPU (10P + 4E), 48 GB RAM,
8 TB external SSD. Daily apps run concurrently (10 cores used by sweep,
2 reserved).

This envelope is now **auto-detected at RUNTIME**, never hardcoded — the ONE
COMMON config leaf `brutex/config/hardware_envelope.py`
(DEC-HARDWARE-ENVELOPE-001 #1401 + DEC-RAM-BUDGETS-001 #1360, consolidated by
DEC-HW-ENVELOPE-CONSOLIDATE-001 #1490) probes cores (macOS `sysctl` P/E
split, Linux `sched_getaffinity` cgroup/CI-aware, fallback `os.cpu_count()`)
AND total physical RAM (sysconf-first, the honest `0` "unknown" sentinel) and
sizes every CPU-bound stage off it (`cpu_pool_workers(reserve=2,
prefer_performance=True)` → 8 on this machine; `memory_budget_bytes` for the
RAM-scaled byte budgets). The engine sweep pool (`auto_sweep_workers`), the
Greeks enrichment pool (`greeks_pool_workers`), and the pull pipeline's
timeframe PREBUILD (parallelised across cores via a `ThreadPoolExecutor`,
byte-identical lake output) all size off it; the polars global pool (pinned to FULL
physical width at the CLI entrypoint) + every concurrent-reader DuckDB
connection (`physical_cores // 2` per connection — the contention fix) are
bounded off it too (DEC-THREAD-BUDGET-BOUNDS-001 #1491, kill switches
`BRUTEX_POLARS_THREADS`/`BRUTEX_DUCKDB_THREADS`).
`BRUTEX_CPU_WORKERS` / `BRUTEX_PREBUILD_WORKERS` / `BRUTEX_SWEEP_WORKERS=1`
override. The Groww FETCH pool + the per-second rate ceiling + the 500/min
governor are a REAL vendor limit (a single TOTP token; §2 Rule 6) and are
deliberately NOT hardware-scaled.

**Production sweep SLA:** **2-4 days wall time** for ~33 billion trials
on this envelope, after Stage 1A → 1B → 2 → 3 → 4 pipeline. Public
benchmarks (vectorbt, NautilusTrader, Jesse) constrain the upper bound;
see `docs/TRACK1_PERFORMANCE_BUDGET.md` for the full memory-bandwidth
analysis.

Larger machines (64-128 core EPYC / Threadripper) scale linearly and
bring sweep to 12-24 hours; the spec MUST state SLA per hardware
envelope.

---

