# Runbook — Critical Alerts: what fired, why, and what to do

> **Target audience:** the operator (sjparthi) receiving a BruteX Telegram
> push (or reading the pull/sweep logs), and any Claude session triaging one.
> **Time required:** minutes per alert. **Scope:** every CRITICAL/WARN alert
> the pull + sweep surfaces emit today, each traced to its defining
> `file:line` on this build (2026-07-03). Golden Rule #1: every claim below
> cites the code.

## 0. Channel + configuration (read once)

All pushes go through the Telegram notifier (`brutex/notifications/telegram.py`).
Three env vars, ALL required when enabled (`telegram.py:91-94`):

```
BRUTEX_TELEGRAM_ENABLED=true          # strict decoder — only literal "true"
BRUTEX_TELEGRAM_BOT_TOKEN=<bot token>
BRUTEX_TELEGRAM_CHAT_ID=<numeric chat id>
```

Setup walkthrough: `docs/runbooks/telegram-setup.md`. Behavioural contract:

- **Best-effort / fire-and-forget** — an alert failure is swallowed + logged
  and can never block, slow, or crash a pull or sweep.
- **Disabled = silent no-op** (the default). Alerts add signal; the logs
  remain the durable record.
- **Per-kind dedupe windows** (`telegram.py:152-167`): e.g.
  `RATE_LIMIT_DOWNSHIFT` coalesces within 60 s, `AUTH_RENEW_FAILED` within
  300 s. The CRITICAL kinds (`PHASE_BLOCKED`, `WORKER_HUNG`,
  `SYMBOL_WINDOW_FAILURE`) have a **0 s window — the notifier never drops
  them** (`telegram.py:283-285`); their once-per-cause behaviour comes from
  the FIRING side instead (event-driven halts; the retry-storm watchdog
  fires once per symbol).

---

## 1. `pull_halted` — 🛑 CRITICAL (`PHASE_BLOCKED`)

- **Definition:** `SweepAlerter.pull_halted(*, reason)` —
  `notifications/sweep_alerts.py:388-416`; dedupe key `pull-halt:<reason>`.
- **Message:** "🛑 Pull HALTED: {reason}. The pull stopped — no further bars
  will be fetched until you resolve it (e.g. free disk space) and re-run.
  Bars already written are safe (atomic + idempotent); a re-pull resumes."
- **Trigger:** a pull-wide global stop propagating out of the ingest spine,
  fired via the COMMON `emit_pull_halt_alert(exc)` (`sweep_alerts.py:532-568`;
  `reason = exc.reason` for `LakeWriteHalt` — `disk_full` / `out_of_memory` —
  else the class name). Call sites: CLI fetch (`cli.py:3253` via the
  delegating `_emit_pull_halt_alert` at `cli.py:2573-2593`) and the dashboard
  background pull (`dashboard/pull_jobs.py:443,499`).
- **The halt set:** `LakeWriteHalt`, `DailyQuotaExceeded`,
  `QuotaInvariantError`, `MassEmptySettleHalt` (CLI `_pull_halt_errors`,
  `cli.py:2872-2878`) **plus a TERMINAL Groww auth failure** — a dead
  credential / an exhausted reauth budget (DEC-GROWW-AUTH-HALT-001; the
  dashboard predicate `pull_jobs.is_pull_halt` at `pull_jobs.py:140-169`
  consults `is_terminal_auth_failure`, so RETRYABLE auth blips never alert).
- **Threshold:** none — event-driven; fires once per distinct reason.
- **First diagnostic:**
  `df -h $(dirname "${BRUTEX_LAKE_ROOT:-$HOME/.brutex}")` for `disk_full`,
  then `.venv/bin/python -m brutex status`. Resolve the cause and re-run —
  the idempotent keep-first sink + the durable window ledger resume the pull.

## 2. `pull_degraded` — ⚠️ WARN push, 5 SLA rules

- **Definition:** `SweepAlerter.pull_degraded(*, rule, achieved, allowed,
  detail)` — `sweep_alerts.py:338-374`; dedupe key `pull-degraded:<rule>`.
  Message: "⚠️ Pull health degraded ({rule}): achieved {a:.2f} r/s vs allowed
  {b:.2f} r/s. {detail}."
- **Fired by:** `PullHealthMonitor`
  (`brutex/observability/pull_health.py`) on a rule OK→BREACH edge; log
  events `pull_health.breach` / `pull_health.recovery`. Built per pull by
  `pull_controller._build_pull_health_monitor` (`pull_controller.py:1138`,
  bound at `:1562` + `:2280`) and on the CLI at `cli.py:3192`.
- **The 5 rules + default thresholds** (`pull_health.py:85-101`, rule
  construction `:308-353` — operator-tunable HEURISTICS, not vendor limits):

  | Rule | Trigger | Default threshold | SLA severity |
  |---|---|---|---|
  | `pull.achieved_below_allowed` | `effective_rps / current_rps <` ratio | 0.6 | WARN |
  | `pull.pool_idle_high` | `1 − in_flight/pool_size >` fraction | 0.5 | WARN |
  | `pull.write_latency_spike` | `latency_ms_p95 >` budget | 1500 ms | WARN |
  | `pull.rate_storm` | controller `recent_hits >` N | 20 (VENDOR-gated detail) | WARN |
  | `pull.stalled` | `effective_rps <` floor for K consecutive samples | 0.5 r/s × 5 samples | **CRITICAL** |

- **Honesty note (state it, don't paper over it):** `pull.stalled` is
  CRITICAL in the `SLARule` taxonomy (`pull_health.py:350`) but the Telegram
  push it routes through (`pull_degraded`) always publishes severity **WARN**
  (`sweep_alerts.py:369-374`) — the "CRITICAL" lives in the log/rule
  taxonomy, NOT the push severity. Do not expect a red-styled push for a
  stalled pull.
- **Triage via the `detail` string** (`pull_health.py:409-416`):
  `pull.rate_storm` → "VENDOR 429-gated — expected…" (no action; the AIMD
  controller already throttled); every other rule → "OUR pipeline is the
  limiter — check write latency / pool width".
- **Prometheus mirror:** `brutex_pull_achieved_rps` / `brutex_pull_allowed_rps`
  / `brutex_pull_in_flight` / `brutex_pull_pool_idle_fraction` /
  `brutex_pull_latency_ms_p95` / `brutex_pull_recent_hits` on `/metrics`
  (`pull_health.py:104-109`).
- **First diagnostic:** the dashboard rate widget / `GET /api/v1/pull/rate`;
  or `curl :<port>/metrics | grep brutex_pull_`.

## 3. `pull.underlying_all_empty` — structlog WARNING (NOT a Telegram push)

- **What it is:** the whole-underlying "silent-vanish" detector
  (DEC-SUSPICIOUS-EMPTY-FLAG-001 #919, production-wired by
  DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001 #1014). **It is a log line only —
  there is NO Telegram push for it today**; you find it in the pull log, not
  on your phone.
- **Event names:** `pull.underlying_all_empty`
  (`adapters/groww/option_ingest.py:873`) + error twin
  `pull.underlying_all_empty_error` (`:875`).
- **Trigger:** a whole F&O underlying whose every CLEANLY-fetched contract
  stored ZERO bars — flags iff `attempted >= 3` clean fetches
  (`_ALL_EMPTY_MIN_CONTRACTS`, `:879`) AND `stored_bars == 0`; clean =
  `windows≥1 ∧ skipped==0 ∧ still_failed==0` (`_is_clean_fetch`, `:920`).
  Warnings capped at 20 underlyings (`_ALL_EMPTY_SAMPLE_CAP`, `:882`).
  Observe-only — no ledger/engine change, no auto-recovery.
- **First diagnostic:** `grep "pull.underlying_all_empty" <pull log>` for the
  named underlying (carried as `repr()`), then check `/coverage/windows` for
  it and probe one contract on the `/db` F&O candle view. Known root-cause
  class: `&`-symbol encoding (M&M — DEC-GROWW-AMPERSAND-FALLBACK-001 #898).
  Recovery path once fixed: `docs/runbooks/option-ledger-gap-unsettle.md`.

## 4. `rate_breach` — ⚠️ WARN (`RATE_LIMIT_DOWNSHIFT`, 60 s dedupe)

- **Definition:** `SweepAlerter.rate_breach(*, current_rps, recent_hits)` —
  `sweep_alerts.py:325-336`; dedupe key `"rate-breach"`. Message: "⚠️ Vendor
  rate breach: {n} recent 429(s); throttled to {r:.0f} r/s. The pull
  continues, slower."
- **Trigger:** a vendor 429, via `rate_breach_callback(controller)`
  (`sweep_alerts.py:506-529`) wrapping the controller's real AIMD step-down;
  wired e.g. on the CLI fetch at `cli.py:3083`. The 60 s
  `RATE_LIMIT_DOWNSHIFT` dedupe window coalesces a storm into one push.
- **First diagnostic:** usually none needed — the AIMD ladder self-heals
  (steps down toward soft floor 3 / deep floor 1, recovers to the 8 r/s
  ceiling once the storm clears — CLAUDE.md §2 Rule 6). Confirm recovery on
  `GET /api/v1/pull/rate` (`current_rps` climbing back).

## 5. `watchdog_hung` — 🛑 CRITICAL (`WORKER_HUNG`) — sweep, not pull

- **Definition:** `SweepAlerter.watchdog_hung(*, worker_name,
  last_beat_seconds_ago, timeout_seconds)` — `sweep_alerts.py:305-323`;
  dedupe key = the worker name. Message: "🛑 Sweep watchdog: worker {name}
  HUNG (last beat {x}s ago, timeout {t}s). The sweep may be stalled."
- **Trigger:** a sweep worker heartbeat older than the timeout — default
  300 s (`engine/sweep_watchdog.py:44` `DEFAULT_TIMEOUT_SECONDS`); wired at
  `cli.py:3886` `SweepWatchdog(on_hung=alerter.watchdog_hung_callback())`.
- **First diagnostic:** `.venv/bin/python -m brutex status`; then the sweep
  log + checkpoint (`brutex sweep-verify` / `brutex results show` after the
  run).

## 6. Retry-storm give-up → `symbol_window_failure` — 🛑 CRITICAL

- **Definition:** `SweepAlerter.symbol_window_failure(*, symbol,
  failed_windows, total_windows)` — `sweep_alerts.py:417-441`; dedupe key =
  the symbol. Message: "🛑 Retry storm: {symbol} hit 100% window-failure
  ({failed}/{total} windows gave up). Bars for {symbol} are NOT in the lake —
  re-pull after the 429 storm clears."
- **Trigger:** the `RetryStormWatchdog` (`orchestrator/watchdog.py:162`) — a
  `(symbol, window)` gives up after `max_retry_seconds` of CONTINUOUS failure
  or on terminal retry exhaustion; when a symbol's give-ups reach its window
  total (100%), the alert fires ONCE per symbol.
- **Arming (opt-in):** `brutex fetch --max-retry-minutes N` (omit = disabled;
  must be ≥ 1 — `cli.py:2816`).
- **Audit trail:** hash-chained rows `RETRY_GIVE_UP` per give-up +
  `SYMBOL_WINDOW_FAILURE` per symbol (`orchestrator/watchdog.py:77,80`) under
  `<lake>/audit/`.
- **First diagnostic:** `/coverage/windows` FAILED tab for the symbol;
  `.venv/bin/python -m brutex audit verify`; after the storm clears,
  `brutex fetch --retry-failed` (or `--until-clean`).

---

## Related (mentioned for completeness, not headline alerts)

- **`auth_renew_failed`** — 🔑 ERROR (`AUTH_RENEW_FAILED`,
  `sweep_alerts.py:375-387`, 300 s dedupe): a refused vendor token renew.
  The 06:00 IST daily token reset itself normally self-heals mid-pull by
  RE-READING the Lambda-minted token from SSM and adopting it — production
  resolves the READ-ONLY `ParameterStoreTokenStore`, so `SdkRefresher.reauth`
  takes the `readonly_reread` branch, logging `groww.token.reauth` then
  `groww.token.reread`; **BruteX never mints on the read-only broker**
  (DEC-BROKER-READONLY-REAUTH-REREAD-001 #1072 /
  DEC-REAUTH-COLDSTART-READONLY-001 #1092, over the #909 reauth seam). A
  persistently-stale token is fixed by re-deploying the Lambda minter
  (`deploy/aws_token_minter/deploy.sh`) — no other operator action unless a
  TERMINAL auth failure escalates to the §1 `pull_halted` push.
- **Sweep lifecycle pushes** (`sweep_complete` INFO/⚠️, phase events) are
  informational — see `docs/runbooks/pilot-sweep-runday.md`.

## Decision trail

- DEC-TELEGRAM-PULL-HALT-001 (#888) + DEC-TELEGRAM-PULL-HALT-DASHBOARD-001
  (#889) — the `pull_halted` wiring (CLI + dashboard).
- DEC-PULL-HEALTH-MONITOR-001 (#932) + DEC-PULL-HEALTH-WIRING-001 (#935) —
  the 5-rule health monitor + its production binding.
- DEC-SUSPICIOUS-EMPTY-FLAG-001 (#919) +
  DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001 (#1014) — the all-empty detector.
- DEC-RATE-STORM-WATCHDOG-001 — the retry-storm give-up guard.
- DEC-GROWW-TOKEN-REAUTH-001 (#909) — the self-healing reauth seam; on the
  production read-only broker the heal is an SSM RE-READ, not a mint
  (DEC-BROKER-READONLY-REAUTH-REREAD-001 #1072).
