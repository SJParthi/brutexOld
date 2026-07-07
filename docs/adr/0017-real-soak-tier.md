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
   requested duration** (`soak_minutes + 30`, applied at BOTH the job level and
   the pytest step) so it can never outrun its own clock. The derivation runs as
   SHELL arithmetic in a tiny GitHub-hosted `plan` job whose digits-validated
   outputs feed `fromJSON(needs.plan.outputs.…)` — NOT as expression arithmetic,
   because GitHub Actions expressions have NO arithmetic operators (an
   in-expression `+ 30` fails workflow-file validation and no job, watchdog
   included, ever runs — the SOAK-R6-1/OH6-1 BLOCKER this replaced). It runs
   `pytest tests/stress -m 'stress or soak'` with `STRESS_DURATION_S =
   soak_minutes × 60` (same plan-job derivation), driving the DURATION-based
   `test_soak_sustained`, under `--basetemp "$RUNNER_TEMP/soak-tmp"` so the
   drift-log artifact is JOB-scoped (the default macOS basetemp lives under
   `/var/folders/.../T/`, where a `/tmp/pytest-*` glob matches nothing — and an
   unscoped glob on a PERSISTENT runner could upload unrelated local pytest
   temp trees to a public-repo artifact).

The drift detector reuses the EXISTING rails (`SLADetector` hysteresis,
`MetricRegistry` gauges, guarded callbacks → `SweepAlerter.soak_drift`), mirroring
`pull_health.py`. RSS + fd are read through ONE COMMON cross-platform probe
(`process_probe.py`). The p99 uses the ONE COMMON `metrics.compute.percentile`.

The failure signal is AUTOMATIC. Every per-window MONOTONE RSS-ceiling / fd-growth
breach (a) fires a CRITICAL `SweepAlerter.soak_drift` Telegram alert in
real time — the driver's `on_breach` is WIRED to it (DEC-PULL-HEALTH-WIRING-001
precedent, which bound the originally-dormant pull-health monitor), a
trigger-independent push that is a silent no-op when Telegram is disabled.
HONEST prerequisite (OH7-1): `soak-selfhosted.yml` wires NO Telegram
secrets/env, so on the soak tier this push is live ONLY if the self-hosted
runner daemon's own environment exports `BRUTEX_TELEGRAM_BOT_TOKEN` /
`BRUTEX_TELEGRAM_CHAT_ID` / `BRUTEX_TELEGRAM_ENABLED=true` — an operator
runner-service configuration, out-of-band of the workflow; without it the
auto-opened issue is the ONLY automatic failure signal.
`soak.p99_drift` is OBSERVE-ONLY — it fires NEITHER this push NOR the hard gate
(see below). The
end-of-run HARD gate (b) is the DETERMINISTIC oracles ONLY — the RSS ceiling, the
fd-growth count, and the audit-chain integrity are asserted absent so a leak
fails CI, never a chart read. `soak.p99_drift` is deliberately **observe-only — neither a real-time
Telegram push NOR a hard CI gate**: a sub-second window's p99 is dominated by
transient fsync/GC/scheduler spikes (`audit.append` fsyncs per record), so
comparing a later window's p99 to a single warm first-window baseline at ratio
2.0 fires on NOISE, not drift (reproduced as a ~15% false-fail rate on an idle
box, ratios up to 8–9×), and the detector re-fires a fresh OK→BREACH edge per
up-crossing so pushing each would storm the operator on non-regression noise. It
is visible ONLY via the monitor's structlog breach warning + the
`brutex_soak_p99_ratio` /metrics gauge, so the nightly cron cannot cry-wolf and
open false CRITICAL issues (Golden Rule #10). HONEST GAP: a genuine multi-hour
p99 climb is therefore NOT auto-alerted — a smoothed/sustained-p99 push rule is a
named follow-up.

## 3. Consequences

**Positive**

* The 24h/multi-hour soak is now genuinely runnable to completion (on the
  self-hosted tier, for the dispatched duration) — the fixed-60 trap is closed by
  a duration-derived timeout.
* Whole-process RSS + fd are now HARD-gated automatically (deterministic); the
  drift LOGIC is 100%-covered on synthetic series. p99-latency-drift is
  OBSERVE-ONLY (structlog + the `brutex_soak_p99_ratio` gauge) — neither a
  real-time Telegram push NOR a hard CI gate — see Honest limits.
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
  RSS rule is an absolute CEILING backstop, not a growth slope. The fd-growth
  rule is the hard-gated slope signal; `soak.p99_drift` is the drift signal but
  is OBSERVE-ONLY — visible via structlog + the `brutex_soak_p99_ratio` gauge,
  and fires NEITHER a real-time Telegram push NOR a hard CI gate — a sub-second
  window's p99 is fsync/GC/scheduler-jitter-bound and asserting on it false-fails
  ~15% of runs (reproduced), so a hard gate on it would train the operator to
  ignore soak alerts (the opposite of §11), and pushing each oscillating
  up-crossing would storm the operator on non-regression noise. It is filtered
  out of BOTH the end-of-run assertion and the real-time push. HONEST GAP (Golden
  Rule #10): a genuine multi-hour p99 climb is therefore NOT auto-alerted — a
  smoothed/sustained-p99 push rule is a named follow-up.

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
