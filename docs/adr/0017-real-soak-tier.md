# ADR-0017 — A real self-hosted multi-hour soak tier + a pure drift detector

**Decision ID:** `DEC-SOAK-REAL-TIER-001` (MIGRATION.md entry #1512)
**Date:** 2026-07-06
**Status:** **ACCEPTED** — ships in the owning PR (CI + observability + tests +
docs only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no Groww vendor/rate/scope/vocab change).
**Author:** the soak-tier implementation session (operator-directed).
**Builds on:** CLAUDE.md §2 Golden Rule #10 (honest limits) / #11 (COMMON) / §11
(AUTOMATED — zero-click failure signal); `DEC-STRESS-001` (MIGRATION.md #208 — the
seed stress suite this corrects); `DEC-PULL-HEALTH-MONITOR-001` (#932 — the
`SLADetector` + `MetricRegistry` + guarded-callback precedent `soak_drift.py`
mirrors); `DEC-MEMLEAK-GATE-001` (#616 — the tracemalloc memory-growth harness
whose RSS read is now the ONE COMMON probe); `DEC-M4PRO-VERIFY-001` /
`bench-m4pro.yml` (the registered self-hosted M4-Pro runner precedent).

---

## 1. Context — the "soak" was a fiction gated by a fixed 60-min timeout

`DEC-STRESS-001` (#208) shipped `tests/stress/` + `stress-nightly.yml` and claimed
the 24h soak SLA from CLAUDE.md §2 Rule #12 was "REACHABLE via `workflow_dispatch`
with `STRESS_EVENTS=10_000_000`." A live-repo audit disproved that:

* `stress-nightly.yml` runs on GitHub-hosted `ubuntu-24.04` with a fixed
  `timeout-minutes: 60`.
* The 10M knob is threaded into ONE sequential `pytest tests/stress -m stress`.
* `test_audit_chain_sustained` at 5000 events ≈ 30 s (the test's own docstring),
  so 10M/5000 = 2000× ≈ **16.7 h** for that ONE test — the workflow header
  elsewhere called the same value a "24h soak."
* GitHub Actions hard-kills the job at 60 min → the soak self-kills at ~1/17th
  complete. It was **never reachable-to-completion.**
* The failure-alert step was `if: failure() && github.event_name == 'schedule'`,
  so a dispatched soak's self-kill opened NO issue — eyeball-only.
* `tests/stress/__init__.py` documented `STRESS_DURATION_S=86400` as the soak
  knob, but NO test read it — a dead lever; the only working lever was an
  un-completable event COUNT.

A real soak must ALSO watch signals a one-shot test cannot: whole-process RSS
growth, fd-count growth, and p99-latency drift OVER TIME — none of which the
existing tracemalloc gate (single-point, Python-heap only) or the bench O(1)
`p99(1M)/p99(1K)` scale-invariance gate can see.

## 2. Decision

Split verification into three honest tiers and factor the drift LOGIC out of any
wall-clock runner:

1. **PR / unit tier** — the pure `brutex/observability/soak_drift.py` drift
   detector is unit-tested to 100% line+branch on synthetic RSS/fd/latency
   series (rising / flat / spiky / leak). No wall-clock.
2. **ubuntu nightly tier** — `stress-nightly.yml`, honestly reclassified as a
   ~5-min regression gate, runs `pytest tests/stress -m stress` (including the
   SHORT real soak driver `test_soak_short`, so no long-only path is dishonestly
   `pragma`'d). A guard step REFUSES `STRESS_EVENTS > 200000` and points at the
   real soak tier, so the un-completable 10M path can never launch here.
3. **self-hosted soak tier** — NEW `.github/workflows/soak-selfhosted.yml` on the
   registered M4-Pro (`runs-on: [self-hosted, macos, m4pro]`), weekly cron +
   `workflow_dispatch(soak_minutes)`, with **`timeout-minutes` DERIVED from the
   requested duration** (`fromJSON(soak_minutes || '360') + 30`, applied at BOTH
   the job level and the pytest step) so it can never outrun its own clock. It
   runs `pytest tests/stress -m 'stress or soak'` with `STRESS_DURATION_S =
   soak_minutes × 60`, driving the DURATION-based `test_soak_sustained`.

The drift detector reuses the EXISTING rails (`SLADetector` hysteresis,
`MetricRegistry` gauges, guarded callbacks → `SweepAlerter.soak_drift`), mirroring
`pull_health.py`. RSS + fd are read through ONE COMMON cross-platform probe
(`process_probe.py`). The p99 uses the ONE COMMON `metrics.compute.percentile`.

The failure signal is AUTOMATIC: a per-window RSS-ceiling / fd-growth /
p99-drift breach (a) fires a CRITICAL `SweepAlerter.soak_drift` Telegram alert in
real time (trigger-independent) and (b) is asserted absent at end-of-run, so a
leak fails CI — never a chart read.

## 3. Consequences

**Positive**

* The 24h/multi-hour soak is now genuinely runnable to completion (on the
  self-hosted tier, for the dispatched duration) — the fixed-60 trap is closed by
  a duration-derived timeout.
* Whole-process RSS / fd / p99-latency-drift are now gated automatically; the
  drift LOGIC is deterministic and 100%-covered on synthetic series.
* The #208 "REACHABLE" over-claim is corrected in place (Golden Rule #10).

**Operational dependency (accepted)**

* The real soak requires the operator's registered self-hosted M4-Pro to be
  online. Like `verify-m4pro.yml` / `bench-m4pro.yml`, the job queues (does not
  fail) when the runner is offline, and there is NO `pull_request` trigger, so an
  offline runner never blocks a PR's mergeable state.

**Honest limits (Golden Rule #10)**

* This is NOT a claim that a 24h run happens in PR CI. The PR tier proves the
  drift LOGIC; the ubuntu tier is a ~5-min regression gate; only the scheduled
  self-hosted job runs the multi-hour wall-clock, and only for the dispatched
  `soak_minutes` (default weekly = 6 h).
* On macOS the RSS fallback reads `ru_maxrss` (a process-lifetime PEAK), so the
  RSS rule is an absolute CEILING backstop, not a growth slope. The fd-growth and
  p99-drift rules are the slope/drift signals.

**Named follow-ups (NOT built here)**

* A queue-age watchdog that alerts when the self-hosted soak sits queued too long
  (an unregistered/offline runner) — so a silently-never-running soak is visible.
* A Linux self-hosted soak variant (so `/proc` VmRSS current-slope RSS gating is
  available on a dedicated box, not just the macOS PEAK backstop).

## 4. Alternatives rejected

* **Flip the existing ubuntu cron to weekly with `STRESS_EVENTS=10_000_000`**
  (the #208 ROADMAP proposal) — still self-kills at the 60-min timeout; does not
  solve the structural problem.
* **Raise the ubuntu `timeout-minutes` to ~20 h** — GitHub-hosted runners cap job
  time (and burn metered minutes); a 20-h ubuntu job is neither reliable nor
  economical, and the operator's real hardware (CLAUDE.md §8) is the M4-Pro.
* **A fourth percentile copy in `soak_drift.py`** — rejected for Golden Rule #11
  (COMMON); `metrics.compute._percentile` was promoted to public `percentile`
  and shared, so the drift ratio uses ONE linear-interp convention for both
  windows.
