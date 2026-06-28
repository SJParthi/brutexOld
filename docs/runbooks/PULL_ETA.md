# Pull wall-clock ETA — honest runbook (DEC-ENV-PERSIST-SLA-001)

> **Golden Rule #10 (honest limits).** This runbook gives a STRAIGHT-DIVISION
> estimate (`windows / achieved_rps`) and clearly labels every number as
> **MEASURED** (taken from a real run / live snapshot) or **PROJECTED**
> (arithmetic from a measured input). No fake O(1), no "minutes" magic.

## What the estimator computes

`brutex.observability.pull_eta.estimate_pull_eta(total_windows, achieved_rps)`:

```
seconds = total_windows / max(achieved_rps, EPSILON_RPS)   # EPSILON_RPS = 1e-3
```

* `total_windows` — the number of `(contract, interval)` fetch WINDOWS the pull
  must issue. A pull plans these up front (the `/coverage/windows` "planned"
  total + the F&O window plan). Negative → clamped to 0.
* `achieved_rps` — the **MEASURED** achieved request rate. This is an INPUT, not
  a constant baked into the code: read it from the live rate-controller snapshot
  / the `/metrics` gauges (`pull.achieved_rps`) or from a finished run's log
  (e.g. `cf1afd97-brutex.log` reported a time-weighted **achieved 3.20 r/s**, see
  MIGRATION.md #921). A zero/stalled rate is floored at `EPSILON_RPS` so the
  estimate is finite, never `inf`.

The estimate is deliberately OPTIMISTIC: it ignores vendor 429 back-off, the
06:00 IST token re-mint pause, and lake-write contention, so the REAL pull is
generally **slower** than the number below. Treat it as a floor, not a promise.

## Hardware envelope

**Apple M4 Pro** — 14-core CPU (10P + 4E), 48 GB RAM, 8 TB external SSD (the
operator's overnight MacBook; CLAUDE.md §8). The achieved rate is bounded by the
vendor + the pull's connection pool, NOT by this CPU.

## Rate inputs (cite the source, never invent)

| Rate | Value | Status | Source |
|------|-------|--------|--------|
| Controller per-second CEILING | 8 r/s | configured (operator-directed) | `GROWW_RATE_LIMIT_RPS_CEILING=8.0`, MIGRATION.md #922 |
| Per-minute HARD governor | 500/min | operator-confirmed | `GROWW_RATE_LIMIT_PER_MIN=500`, MIGRATION.md #6 / Rule 6 |
| Achieved on a real run | ~3.2 r/s | **MEASURED** | `cf1afd97-brutex.log`, MIGRATION.md #921 |

> The ceiling only ALLOWS a rate; the vendor's 429 ladder + the pool size set
> what is actually ACHIEVED. The last measured achieved rate was ~3.2 r/s; 8 r/s
> is the configured ceiling, NOT a measured throughput (SUSTAINED 8 is UNVERIFIED
> — the live log showed the vendor 429ing at 5, MIGRATION.md #922).

## Worked numbers — NIFTY first, then the full universe

**Approach:** prove NIFTY end-to-end first (its spot 1-minute history + its F&O),
THEN scale to the full universe (all symbols' spot + futures + options). The spot
window count below (~67k) is a worked PLACEHOLDER you replace with the pull's own
planned total — the arithmetic is what matters, not the exact count.

### A. NIFTY spot, ~67,000 1-minute windows (PROJECTED from a MEASURED rate)

| achieved_rps | source | seconds | format |
|--------------|--------|---------|--------|
| 3.0 r/s | **MEASURED** (~3.2 rounded down, MIGRATION.md #921) | 22,333 s | **~6h 12m** |
| 5.0 r/s | PROJECTED (the 429-observed plateau) | 13,400 s | **~3h 43m** |
| 8.0 r/s | PROJECTED (the configured ceiling — UNVERIFIED) | 8,375 s | **~2h 19m** |

So NIFTY spot alone is an **honest several HOURS** at the measured rate — NOT
minutes. (Numbers via `estimate_pull_eta` / `format_eta`; verify with the unit
test `test_worked_example_67k_at_3rps`.)

### B. Full universe (PROJECTED — scales linearly with the window count)

The full lake is ~750 underlyings (CLAUDE.md §3) × (spot + the planned F&O
contract windows). The window count is orders of magnitude larger than NIFTY's,
so at the same measured ~3 r/s the full-universe spot+F&O backfill is an honest
**MULTI-DAY** job — consistent with the §8 "2–4 days" sweep envelope class. Plug
the pull's actual planned-window total into `estimate_pull_eta(total, 3.0)` for
the live figure; do NOT quote a fixed number here (it depends on how many
contracts the operator selects).

## How to read it live

1. Open `/coverage/windows` → note the **planned** total (`total_windows`).
2. Read the **achieved** rate from `/metrics` (`pull.achieved_rps`) or the live
   rate panel.
3. `estimate_pull_eta(total_windows, achieved_rps)` → `.hours` / `format_eta(...)`.

The estimate updates as the achieved rate moves (a 429 storm lowers it →
the ETA widens honestly).
