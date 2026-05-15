# Track 1 — Performance Budget & Throughput Math

> **Status:** Locked 2026-05-15 by Agent 4 (Throughput Reality Check).
> Companion to `TRACK1_BACKTEST_SPEC.md §2, §12`.

This document captures the honest hardware/throughput analysis that
sized the Track 1 SLA. The 10-13 hour claim in the pre-2026-05-15 spec
was 5-15x optimistic. This doc shows why.

---

## 1. The Claim Being Sized

```
Engine:       Rust + std::simd + Rayon parallel, Apple M4 Pro
              (14-core: 10 P-cores + 4 E-cores; 10 P-cores allocated to sweep)
Workload:     ~33 billion backtest trials per sweep
              (Stage 1A 5.8M + Stage 1B 2.25M + Stage 2 ~33B)
Per-trial:    Walk 5y OHLC at one timeframe; eval 52 atoms once per
              bar (precomputed); test bitmask AND vs N strategy masks;
              simulate forward to SL/TP/15:20 exit; compute metrics
SLA claim:    First production sweep finishes in 2-4 days wall time
```

That's a sustained throughput of **80,000 - 150,000 trials/sec** on
the 10-core M4 Pro after 2-stage smart filtering reduces effective
work.

---

## 2. Public Benchmark References

No public engine (open-source or commercial) is documented at the
700K+ trials/sec range the prior spec claimed. The realistic ceiling
is set by:

| Engine | Reported speed | Source |
|---|---|---|
| vectorbt PRO | ~667 configs/sec | vectorbt.pro/features/performance |
| NautilusTrader (Rust) | 5M rows/sec (ingestion, not trials) | nautilustrader.io |
| Jesse-AI | 16x speedup over baseline | Medium article 2024 |
| Backtesting.py | ~1K trials/sec | github |
| QuantConnect Lean | hundreds trials/sec | QC blog |

The Track 1 SLA of 80-150K trials/sec **already requires 100-200x
better than the best public Python engine**. Achievable in Rust with
careful SIMD tiling, but no margin.

---

## 3. Hardware Budget — M4 Pro

| Resource | M4 Pro 14-core (10P + 4E) | Source |
|---|---|---|
| P-core clock | up to 4.512 GHz | eclecticlight.co M4 P-cores |
| L1 per P-core | 192 KB I + 128 KB D | same |
| L2 (P cluster) | 32 MB shared | same |
| Theoretical DRAM BW | 273 GB/s (LPDDR5X 8533 MT/s) | Wikipedia M4 |
| Sustained STREAM BW | ~205-253 GB/s (~85% of peak) | arxiv:2502.05317 |
| Internal SSD write sustained | ~4.2-7.5 GB/s | Apple discussions |

### 3.1 Compute budget

```
10 P-cores × 4.5 GHz × ~4 µops/cycle (NEON 128-bit, optimistic)
  = ~180 billion µops/sec available
Per-bar evaluation (load + AND + CMP + cond-store):
  ≈ 4 µops × 2 cycles (pipelining) = ~2 cycles per bar
Compute ceiling: ~22 billion bar-evals/sec
```

For 80K trials/sec × 94K bars/trial (5y at 5m) = 7.5B bar-evals/sec.
**Compute alone has 3x headroom.** Not the bottleneck.

### 3.2 Memory bandwidth budget (the real ceiling)

```
Per bar-eval: ~50 bytes loaded (OHLC + 9 indicators × ~5 bytes each)
Required BW: 65B bar-evals × 50 bytes = 3.25 TB/sec  (naive impl)
Available  : 205-253 GB/sec sustained

GAP: 13x SHORT on memory bandwidth at naive implementation.
```

### 3.3 Mitigation — memory tiling

Process all trials sharing a `(window, instrument, TF)` bucket
consecutively, holding the bar mask in registers/L1 while testing many
strategy masks. With aggressive 8-way SIMD bitmask AND on resident
data:

```
Effective load count drops ~8x: 50B → 6 B/bar-eval
Required BW: 65B × 6 = 390 GB/sec  (with tiling)
Available  : 250 GB/sec sustained

GAP: 1.6x SHORT even with aggressive tiling.
```

This is why **the realistic throughput is 80-150K trials/sec, not
700K**. The architecture must do all of:

1. Tile by (instrument, TF, day-window) bucket
2. Hold the bar mask in registers across many strategy mask tests
3. 8-way SIMD bitmask AND
4. Result row append to streaming Parquet writer (not random LMDB writes)

---

## 4. Bottleneck Audit

| Bottleneck | Estimate | Severity |
|---|---|---|
| Memory bandwidth | 1.6-13x gap | DOMINANT |
| Parquet/Zstd I/O write | 6.6 TB / 13 h = 141 MB/s — well under 4-7 GB/s SSD ceiling | OK |
| Bloom filter dedup | 33B × 5 ns = 2.7 min total | OK |
| LMDB inserts | 50 µs × billion = 14 hours sequential | **NOT OK** — fix by storing only survivors |
| Branch mispredict on SL/TP loops | ~10-20 cycles per simulated entry | MINOR (~5-10% overhead) |

The LMDB-billion-inserts issue is addressed in
`TRACK1_BACKTEST_SPEC.md §14, §16`: we store only **survivors above a
result-threshold** in LMDB, not every trial. Failures go to
`failures.parquet` for audit completeness, not LMDB.

---

## 5. Realistic Throughput Envelope

| Scenario | trials/sec | 33B wall time |
|---|---|---|
| Optimistic (perfect tiling + 8-way SIMD + hot L2) | 80-150K | 2.5-4.5 days |
| Realistic (Rust + Rayon, no exotic SIMD) | 20-50K | 7-20 days |
| Conservative (matches no public engine within 100x) | 5-15K | 25-75 days |

**Track 1 SLA: 80-150K trials/sec, 2-4 days per sweep.** Phase 3
micro-benchmark must hit ≥80K sustained or escalate to bigger hardware
before Phase 4.

---

## 6. Alternative Hardware Envelopes

| Hardware | Cores | DRAM BW | trials/sec est | 33B wall time |
|---|---|---|---|---|
| Apple M4 Pro (current) | 12 (10 used) | 250 GB/s | 80-150K | 2-4 days |
| AMD Threadripper 7980X | 64 | 500+ GB/s | 400-800K | 12-24 hours |
| AMD EPYC 9654 (single socket) | 96 | 460 GB/s | 600-1.2M | 8-16 hours |
| Dual-socket EPYC | 192 | 920 GB/s | 1.2-2.4M | 4-8 hours |

Phase 3 will benchmark on M4 Pro and quote actual numbers; this table
is the design envelope.

---

## 6A. Day vs Night Envelope Split (M4 Pro reference)

The M4 Pro is a personal machine, not a dedicated server. During the
day (07:00-22:00 IST) the user runs IntelliJ + Docker + Chrome + Claude
Code, leaving BruteX a much smaller envelope.

| Mode | Cores for BruteX | RAM for BruteX | GPU for BruteX | Throughput target |
|---|---|---|---|---|
| ☀️ Day | 5-6 P-cores | 16-20 GB | 20 cores (idle, free) | 40-90K trials/sec |
| 🌙 Night | 10 P-cores | 40-44 GB | 20 cores (full Metal) | 80-150K trials/sec |
| 🌙 Night + Metal GPU | 10 P-cores + 20 GPU | 40-44 GB | full | 160-400K trials/sec |

Wall time for 33B-trial sweep:

| Mode | Wall time |
|---|---|
| Day-only (impossible — no one runs 100h sequentially during work) | ~100 hours |
| Night-only at 9h/night | **~7 nights** (CPU only) or **~3 nights** (with GPU) |
| Mixed daytime probes + nighttime sweep | **3-4 days realistic** |

For 1B-trial probes during day work: **~3 hours**. For 10B-trial deep
probes: **night-only** is the right tool.

The auto-throttle implementation lives in
`brutex/orchestrator/scheduler.py` (Phase 2 deliverable) per
`TRACK1_RUNTIME_ARCHITECTURE.md §5A.5`. Resumable checkpoints (every
100M trials) ensure no work is lost on a clean morning stop
(`§5A.7`).

---

## 7. Storage I/O Budget

```
Result Parquet rows per sweep: ~33B trials
With 95% drop via threshold filter: ~1.65B kept rows
Per row: ~200 bytes after Zstd-9
Total storage per sweep: ~330 GB raw → ~50-100 GB after compression
Daily iteration: ~3-5 sweeps before SSD pressure (8 TB → ~80-160 sweeps)
```

Streaming Parquet write rate needed: 100 GB / (4 days × 86400 sec) =
~300 KB/sec sustained. **Trivially under SSD ceiling.** Not a
bottleneck.

---

## 8. CI Gate

The throughput claim is gated by:

```python
# tests/bench/test_full_sweep_extrapolation.py (nightly)
def test_projected_33b_within_sla(benchmark):
    # Run 100K trials end-to-end, multiply, assert
    measured = benchmark(run_micro_sweep, num_trials=100_000)
    trials_per_sec = 100_000 / measured.elapsed
    projected_33b_seconds = 33_000_000_000 / trials_per_sec
    assert projected_33b_seconds <= 4 * 86400, \
        f"Projected sweep: {projected_33b_seconds/86400:.1f} days > 4 day SLA"
```

If this gate fails, the SLA gets re-stated in the spec — we don't lie
to the user about wall time.

---

## 9. Lessons Locked In

From the 2026-05-15 hostile review:

1. **Always benchmark against public engines first.** No public engine
   at 700K trials/sec → the claim was a fantasy.
2. **Memory bandwidth dominates, not compute.** A 14-core M4 Pro has
   compute headroom but is BW-bound for this workload.
3. **State the SLA per hardware envelope** in the spec, every time.
4. **Tiling is not optional.** Naive per-trial-per-bar loops will be
   13x too slow.
5. **The CI bench gate is the canary.** If `bench_full_sweep_extrapolation`
   fails, the spec lies. Do not ship.
