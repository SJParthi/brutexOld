# Runbook — the scoped-ALL spot pull (`brutex pull-all-spot` / the ▶ Pull EVERYTHING — spot only button)

> DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779). ONE dashboard button +
> ONE CLI subcommand pulling the ENTIRE spot universe — nothing else.

## 1. What it does — and will NEVER do

DOES (every invocation, identically):

- pulls **all admitted indices + all ~750 stocks**, **1-minute only**,
  **2020-01-01 → the previous calendar day**, auto-incrementally (the durable
  window ledger skips every settled window — re-running is always safe and
  converges to a zero-fetch no-op);
- runs the SAME gated sequencer as the full pipeline (`run_full_pipeline`) with
  the two F&O phases structurally replaced by no-op reports — then the
  derived-timeframe **prebuild** over the clean symbol set (quarantined
  symbols excluded + named in the headline) and — dashboard only — the **spot**
  movers warm;
- fires the **TickVault cross-verify producer** as the LAST step (fail-soft:
  a broken verify config/tail NEVER fails the pull).

WILL NEVER:

- pull options or futures metadata/candles (the note says
  `F&O skipped by design (spot-only scope)` — never a fake "0 expiries");
- run Greeks enrichment (the note says
  `Greeks: not run — no options in this pull`);
- accept a date range (`--start`/`--end` deliberately do not exist — full-range
  convergence IS the promise; date-scoped pulls stay on `brutex fetch` / the
  full-pipeline button's pickers);
- touch the ENGINE (NIFTY/BANKNIFTY/SENSEX), the vocabulary, the rate caps, or
  the lake schema.

## 2. `--auto` (cron / launchd)

```
30 17 * * *  /path/to/brutex pull-all-spot --auto --lake-root /Volumes/lake
```

`--auto` gates BEFORE any lock/vendor work; every deferral is ONE
`pull-all-spot deferred: …` line + **exit 0** (cron-green):

| reason token          | when                                             | detail text (exact family)                                        |
|-----------------------|--------------------------------------------------|-------------------------------------------------------------------|
| `non_trading_day`     | weekend / listed NSE holiday (incl. Muhurat days) | `<date> is not an NSE trading day — nothing new to pull`           |
| `muhurat_deferral`    | a (hypothetical) trading-day Muhurat listing      | `<date> has a Muhurat session — deferred to the next regular trading day (evening-session settle behavior unverified)` |
| `before_settle`       | trading day, before the settle time               | `waiting for post-close settle time HH:MM IST (conservative — the vendor's true settle time is UNMEASURED; …)` — HH:MM is the ACTUAL resolved settle (default or override) |
| `market_hours_blocked`| structural belt (unreachable while settle ≥ 15:35)| `inside the NSE market-hours pull window — deferred`               |

Contention under `--auto` (another pull holds the lake single-flight) also
exits 0 with one line — no cron retry storm. A manual run refuses contention
with exit 1, naming the holder.

Kill-switch coupling (hostile-review r1 ruling): the spot-only arm's
Muhurat MID-RUN belt (the composed `market_closed` predicate) honors the
SAME `BRUTEX_MARKET_HOURS_GUARD` kill switch as the market-hours guard it
composes over — `off` disables BOTH (ONE switch governs the shared-account
protection family; there is no separate Muhurat switch). The `--auto` gate's
whole-day Muhurat/holiday deferrals are calendar-keyed and independent of
that switch.

Mid-run / whole-chain deferral wording (refuter-round-1 r2 MINOR-1): the
composed belt can also defer during an EVENING Muhurat session, so on
spot-only surfaces the deferral text reads the both-arms
`market-session protection window active (NSE hours 08:45-15:35 IST or a
Muhurat session)` — never the bare market-hours clock claim — and the logs
carry one `auto_pull_gate.muhurat_belt_halt` WARN when the Muhurat arm (not
the guard) fired. `MarketHoursHalt.reason` stays `market_hours` (shared
contract).

CLI stdout honesty: the completion headline is ALWAYS the full pipeline note
(through the spot-only surgery), so warning clauses — `window ledger reload
FAILED …`, `prebuild CRASHED (…)`, `post-steps SKIPPED (strict gate: …)` —
are never dropped on an exit-0 run; a crashed prebuild also gets its own
`prebuild: CRASHED (ErrorType)` line. Junk `BRUTEX_PIPELINE_STRICT_GATE` is
a clean exit-1 refusal before any lock (like the settle/market-hours knobs).

## 3. The settle time is a GUESS — measure it, then tighten

`BRUTEX_PULL_ALL_SETTLE_IST` defaults to **17:30 IST** — a NAMED CONSERVATIVE
guess: the vendor's post-close settle time is **UNMEASURED** (the #1775 probe
was never run; 17:30 is the probe's last slot). Run
`docs/groww/probe/postclose_settle_probe.py` per
`docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md`, then set the env (or
`--settle-ist HH:MM`) to the measured slot + margin. Strict `HH:MM`, fail-loud
on junk, structurally refused below 15:35 (the market-hours guard's BLOCK_END —
an earlier "settle" would just bounce off the untouched guard).

Data safety either way: a too-early pull's still-settling TODAY bars are
withheld by the today-completeness gate (#1774) and the end bound is the
previous day — a mis-set settle time costs bounded deferral churn, never wrong
data.

## 4. Verification loop (after every run)

1. The finish note / stdout `spot:` line — `N window(s) FAILED (will retry)`
   must match the **/coverage/windows** FAILED tab count.
2. **/completeness** — spot coverage per symbol (a late-listed symbol reads
   honest `partial`).
3. **/coverage** — the per-symbol rollup. The dashboard button's finish detail
   carries the same note (500-char cap).
4. Cross-verify: `cross-verify: producer started (poll
   /api/v1/crossverify/progress)` on the dashboard;
   `<verdict> (published … · withheld … · failed …)` on the CLI.

## 5. Crash / interrupt / resume

Every processed window is checkpointed to the durable ledger MID-pull
(throttled). A crash, SIGKILL, or the 08:45 market-hours halt loses at most
the un-flushed tail; the next run re-pulls ONLY missing/FAILED windows. A
dashboard-launched real pull additionally persists a crash-recovery intent —
the boot-time resume re-fires it **spot-only** (the scope rides the intent;
it never widens into an F&O pull).

NAMED RESIDUAL (refuter-round-1 r2 MINOR/NIT-4 — the ROLLBACK direction):
that scope law holds only on scope-aware code. If you roll the DEPLOYED code
back to a pre-scope build while a crashed spot-only pull's intent file is
pending, the old code ignores the `scope` key and the boot resume re-fires a
FULL-scope F&O pull (hours of vendor calls; all rails hold, but the scope
widening is operator-unauthorized). Remediation: do not roll back across a
crashed spot-only pull, or delete the intent file first
(`<lake>/.pull_intent.json` — see `brutex/dashboard/pull_intent.py`).

Mass-empty / zero-data settles: windows settled SKIPPED by the circuit
breakers are re-verified via `brutex reopen-skipped` (see that command's
help) — never an automatic re-pull storm.

## 6. Cross-verify tail semantics

- Kill switch: `BRUTEX_CROSSVERIFY=off` → `cross-verify: disabled (…)`.
- Junk `BRUTEX_CROSSVERIFY_*` env → `cross-verify: not configured
  (ValueError: …)` — fail-SOFT here (the spot pull is never blocked); the
  direct `brutex crossverify run` / `POST /api/v1/crossverify/run` surfaces
  stay fail-LOUD.
- The dashboard tail is the SAME producer as `brutex crossverify run` (one
  shared trigger); it acquires its OWN lock trio with a bounded wait-retry —
  it may idle up to one 60 s retry while the pull job's finally releases the
  flock (bounded by the runner's 45-min budget).
- `--strict-exit` (CLI) escalates an exit-2-family verdict
  (`refused`/`withheld_partial`/`blocked`/`publish_failed`/`suspect`/`failed`)
  to exit 2; the default prints it and stays 0.

## 7. First-run expectation + known shapes

The first-ever full run is LONG (~750 symbols × 6+ years of 1-minute; the
08:45 guard will halt + defer it repeatedly). Progress is monotone — every
session's settled windows persist, and each subsequent run shrinks. NO
wall-clock SLA is claimed (Golden Rule #10).

**Vendor-EMPTY symbol ⇒ movers warm withheld (NAMED residual, refuter-round-1
r3 F-1 — inherited #1145/#1221 pipeline law, real-prebuild-e2e-pinned):** a
symbol the vendor cleanly returns NO data for (settled SKIPPED, zero stored
bars — NOT a quarantine) has nothing to derive, so the prebuild reports it a
failure (`prebuild failures: <SYM>`), `PrebuildOutcome.ok` reads False, and
the sequencer withholds the DASHBOARD spot-movers warm for the whole run
(the warm clause is honestly absent from the note — never fabricated). The
healthy symbols' timeframes ARE still built. Impact is display-cache warmth
only: the movers caches re-key on the revision token at the next poll. The
scoped exclusion fix is the named follow-up
DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001 (the spot report's per-symbol
`no_data` provably conflates vendor-empty with an already-settled
incremental re-run, so the exclusion needs a zero-stored-bars lake probe).

**Dry-run reports prebuild failures BY CONSTRUCTION (refuter-round-1 r3
F-3):** a `--dry-run` / `dry_run=true` run writes no bars, so on a fresh
lake the (real) prebuild fails every target and the note honestly reads
`prebuild failures: …` — the inherited full-pipeline dry-run shape, not a
defect of this button/command.
