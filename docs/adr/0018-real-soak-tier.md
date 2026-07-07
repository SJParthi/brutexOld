# ADR-0018 — A real self-hosted multi-hour soak tier + a pure drift detector

**Decision ID:** `DEC-SOAK-REAL-TIER-001` (MIGRATION.md entry #1537)
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
   requested duration** (`soak_minutes + 30 + soak_minutes/6` — fixed
   install/rebuild slack plus the duration-LINEAR teardown cost, per SOAK-R7-4;
   default 360 → 450; applied at BOTH the job level and the pytest step) so it
   can never outrun its own clock. `soak_minutes` is fenced to `1..1200`
   (20 h): a zero dispatch would fake-green a ~1 s "multi-hour" soak
   (SOAK-R8-3), and on a self-hosted runner the job's `GITHUB_TOKEN` can only
   be refreshed for up to **24 hours** (documented) — every step past the 24-h
   mark holds a dead token, so the "open issue on failure" watchdog would 401
   and file nothing; the derived timeout `m + 30 + m/6` must therefore fit
   under 1440 min, giving `m <= 1208` → ceiling 1200, derived timeout 1430
   (SEC9-1 — this token bound is TIGHTER than, and supersedes as the binding
   bound, the 5-day/7200-min platform execution kill of SOAK-R8-1/SEC8-2,
   which had set the previous 5760 ceiling). A disk PREFLIGHT step refuses a
   dispatch the runner disk cannot hold: the fsync'd audit JSONL grows
   ~1.5 GiB per soak-hour (the SOAK-R7-4 measurement), so a long soak on a
   near-full disk would burn hours then die at ENOSPC mid-run — the same
   burn-then-die shape as the clock traps (SOAK-R9-3). The derivation runs as
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

* A genuine multi-hour soak (up to 20 h per dispatch) is now runnable to
  completion on the self-hosted tier — the fixed-60 trap is closed by a
  duration-derived timeout. HONEST bound (SEC9-1): a single-dispatch soak
  **longer than 20 h — including a literal 24 h run — is NOT yet possible**,
  because the job's `GITHUB_TOKEN` expires at 24 h on self-hosted runners and
  the end-of-job issue watchdog must still be able to authenticate; the >20 h
  tier is blocked on the out-of-job watchdog follow-up below (which carries
  its own fresh token and would let the fence rise back toward the 4-day
  platform bound).
* Whole-process RSS + fd are now HARD-gated automatically (deterministic); the
  drift LOGIC is 100%-covered on synthetic series. p99-latency-drift is
  OBSERVE-ONLY (structlog + the `brutex_soak_p99_ratio` gauge) — neither a
  real-time Telegram push NOR a hard CI gate — see Honest limits.
* The #208 "REACHABLE" over-claim is corrected in place (Golden Rule #10).

**Operational dependency (accepted)**

* The real soak requires the operator's registered self-hosted M4-Pro to be
  online. There is NO `pull_request` trigger, so an offline runner never blocks
  a PR's mergeable state — but an offline runner is NOT benign (SOAK-R8-2):
  GitHub AUTO-CANCELS a job that has been queued for 24 hours (the documented
  self-hosted job-queue limit) WITHOUT any step executing, so the in-job issue
  watchdog structurally cannot fire for a never-started run and a weekly cron
  soak on an asleep Mac dies SILENTLY, week after week — the exact §11
  silent-death pathology, on a path no in-job step can cover.
* A run that STARTED and then LOST its runner is equally in-job-uncoverable
  (SOAK-R9-1): if the M4-Pro sleeps, reboots or loses power mid-soak — at
  least as plausible unattended as the never-started case — GitHub marks the
  job failed after losing runner communication, but NO further step can
  execute on the dead runner, so the `failure() || cancelled()` watchdog never
  runs, no issue is filed and no Telegram fires. The out-of-job watchdog named
  below is the follow-up that closes BOTH gaps.
* The job's `GITHUB_TOKEN` can only be refreshed for up to 24 hours on a
  self-hosted runner (SEC9-1) — the binding bound behind the 1200-min fence
  (see §2); GitHub additionally terminates ANY self-hosted job after 5 days
  (7200 min) of execution (SOAK-R8-1/SEC8-2), the outer platform bound a
  future >20 h tier must still respect.
* **Supply chain — GitHub-owned actions ONLY on the self-hosted Mac
  (SEC12-1):** the workflow runs zero third-party actions on the persistent
  M4-Pro, matching the deliberate `verify-m4pro.yml` / `bench-m4pro.yml`
  invariant. An early draft used `dtolnay/rust-toolchain@stable` — a MUTABLE
  third-party tag that, if ever moved maliciously (account/repo compromise),
  would execute unattended, weekly, on the operator's personal machine with
  state persisting across runs (and the runner daemon's environment may
  export the Telegram token). It was replaced with the precedent workflows'
  local-toolchain contract: the Rust wheels build with the runner's OWN
  rustup/cargo, and a missing toolchain fails LOUDLY with the remediation
  named. The identical `dtolnay/rust-toolchain@stable` usage in
  `stress-nightly.yml` / `ci.yml` is materially different (disposable
  GitHub-hosted VMs) and is deliberately left as-is.

**Honest limits (Golden Rule #10)**

* This is NOT a claim that a 24h run happens in PR CI. The PR tier proves the
  drift LOGIC; the ubuntu tier is a ~5-min regression gate; only the scheduled
  self-hosted job runs the multi-hour wall-clock, and only for the dispatched
  `soak_minutes` (default weekly = 6 h; max 20 h per dispatch — the SEC9-1
  token bound, see §2 and Consequences).
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

* An OUT-OF-JOB watchdog — a cheap GitHub-hosted cron (with its own fresh
  `GITHUB_TOKEN`) that **opens an issue whenever the last `soak-selfhosted`
  run concluded anything but success and no tracking issue exists** — REQUIRED
  for full §11 coverage, because THREE failure paths are structurally
  invisible to the in-job watchdog: (a) a never-started run (GitHub
  auto-cancels a >24h-queued job with no step executing, SOAK-R8-2); (b) a
  started run whose runner died mid-soak (sleep/reboot/power loss — the job
  concludes `failure` with no further step able to execute, SOAK-R9-1; note a
  "cancelled/never-ran"-only check would MISS this path); and (c) any future
  >20 h tier whose in-job token has expired (SEC9-1). This watchdog is ALSO
  the prerequisite for raising the `soak_minutes` fence above 1200 back
  toward the 4-day platform bound.
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
