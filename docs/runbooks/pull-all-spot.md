# Runbook — the scoped-ALL spot pull (`brutex pull-all-spot` / the ▶ Pull Spot button)

> DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779). ONE dashboard button +
> ONE CLI subcommand pulling the ENTIRE spot universe — nothing else.
> Round-6 TWO-BUTTON split: the button is relabeled **▶ Pull Spot** (formerly
> "▶ Pull EVERYTHING — spot only") and /universe gains its manual sibling
> **▶ Pull F&O** (`scope=fno-only` — see §8); the combined "Pull · full
> pipeline" button is removed (the route's full-chain API default survives).

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
  a broken verify config/tail NEVER fails the pull);
- **(dashboard button, round-5 fold)** ARMS the daily post-market auto-pull
  on the first click — see §2 (disarm via the chip; the CLI never touches
  the armed state).

WILL NEVER (the PULL COMMAND ITSELF — see the honest tail note below):

- pull options — and the pull itself never pulls futures metadata/candles
  either (the note says `F&O skipped by design (spot-only scope)` — never a
  fake "0 expiries"). Tail scope (the refuter-round-3 F-D1 caveat, since
  CLOSED): the cross-verify tail (default ON; `BRUTEX_CROSSVERIFY=off`
  disables it) runs the #1776 producer, whose pull leg is now SPOT-ONLY
  too — PR #1515 (DEC-CROSSVERIFY-SPOT-ONLY-001, MIGRATION.md #1788, landed
  2026-07-13) deleted the producer's NIFTY/BANKNIFTY futures metadata +
  candle legs, so the WHOLE chain (pull + tail) is spot-only;
- run Greeks enrichment (the note says
  `Greeks: not run — no options in this pull`);
- accept a date range (`--start`/`--end` deliberately do not exist — full-range
  convergence IS the promise; date-scoped pulls stay on `brutex fetch` / the
  full-pipeline button's pickers). The underlying `/pull-pipeline` ROUTE, by
  contrast, MAY combine `scope=spot-only` with `symbols=`/`from_date=`/
  `to_date=` — a supported NARROWING (a smaller spot-only pull; the token only
  swaps the F&O phases, so the combination can never widen — refuter-round-3
  F-D2, route-pinned);
- touch the ENGINE (NIFTY/BANKNIFTY/SENSEX), the vocabulary, the rate caps, or
  the lake schema.

## 2. Fully automatic daily — the SELF-ARMING button (round-5 fold)

**One click does everything.** The FIRST click of ▶ Pull Spot runs the
pull now (if allowed) AND arms the daily post-market
auto-pull: from then on, while the dashboard process is up, a background
ticker (`brutex/dashboard/auto_pull_scheduler.py`, 60 s cadence —
`BRUTEX_AUTO_PULL_TICK_S`) fires the SAME button launch path once per
trading day when the `--auto` gate opens (post-close settle time + trading
day + Muhurat deferral — the same gate table below). Zero manual steps
after the first click.

- **Armed state is durable** — `<lake_root>/.pull_all_spot_auto.json`
  survives dashboard restarts; the boot lifespan resumes ticking when armed
  (and if today's window is open and today's pull has not yet succeeded, an
  early tick catches up). A corrupt/unreadable state file reads DISARMED
  with one WARN — never a crash and never a HANG, for EVERY corruption
  shape, inode-TYPE shapes included (junk JSON, wrong shape, invalid UTF-8,
  a nesting bomb, an oversized file — the round-6 N-1 hardening — plus a
  non-regular inode planted at the path: a FIFO / device symlink / socket /
  directory, which pre-round-7 blocked the read forever and silently wedged
  dashboard boot; the round-7 P-1 re-hardening reads both sidecars through
  the shared `O_NONBLOCK` + `S_ISREG` regular-file-only loader, with the
  16 MiB cap checked on the `fstat` of the SAME open descriptor — the
  round-6 stat→read TOCTOU window is closed by the fstat-on-fd cap. The
  same loader hardens `.pull_intent.json`).
- **One dashboard process per lake** (round-6 M-3 disclosure): the
  once-per-day law is per DASHBOARD PROCESS — two armed dashboards over ONE
  lake each hold their own in-memory mirror of the state file, so both may
  fire once on the same day (flock-serialized + ledger-idempotent — bounded
  waste, never wrong data) and a disarm from a stale-mirror process can
  clobber the other's day-marks on disk. Run ONE dashboard per lake.
- **Once per day**: a STARTED run marks the day attempted (a FAILED run does
  NOT retry the same day — it retries the next eligible day, and the
  incremental ledger heals whatever it missed); a run that completes ok
  marks the day done. A SUCCESSFUL MANUAL click also counts as the day's
  pull — the auto never redundantly re-fires behind it.
- **Contention defers, never breaks** (the coexistence law): a running pull
  — or a multi-day dashboard SWEEP, which holds the lake flock + job slot
  for its whole run — refuses the auto launch; the scheduler logs one
  `auto_pull.refused_retry` per day per reason and retries on later ticks
  until the day's window closes. Days fully blocked by a long sweep are
  caught up automatically by the next successful run (auto-incremental).
  No preemption — the sweep is never interrupted.
- **A click during market hours still arms**: the immediate run is refused
  by the untouched market-hours guard, the response says both
  (`started: false` + the armed `auto_pull` block), and the first auto run
  fires at/after the settle time the same day.
- **Disarm / re-arm**: the `auto: ON — disarm` chip under the button
  (`POST /api/v1/universe/pull-all-spot-auto?armed=0|1`). The armed state,
  last outcome and the honest next-eligible text ride the
  `/api/v1/universe/pull-progress` payload's additive `auto_pull` object.
- **Honest limit**: the scheduler lives in the dashboard PROCESS — it fires
  nothing while the dashboard is down (the state survives; the next boot
  catches up if the day's window is still open). For a dashboard-less box,
  use the headless lane below.

### `--auto` (cron / launchd) — the HEADLESS alternative

```
30 17 * * *  /path/to/brutex pull-all-spot --auto --lake-root /Volumes/lake
```

The crontab time is HOST-LOCAL (refuter-round-3 F-D3): schedule it so it
lands AT/AFTER the settle time **in IST** — run the cron on an IST host, or
shift the line (e.g. 17:30 IST = 12:00 UTC). A TZ-misplaced line lands
`before_settle` every day: one printed deferral line, exit 0, FOREVER — a
silent permanent no-pull. That silence is BY DESIGN (deferrals are
cron-green) and this lane has NO dead-man/freshness check (unlike the
nightly chain); an operator who wants "no pull landed today" alerting should
wire the existing healthcheck patterns (the nightly chain's dead-man lane is
the precedent) — deliberately not built into this command. (The dashboard's
self-arming button above and this cron lane share the durable window ledger,
so running both is safe — whichever fires second converges to a zero-fetch
no-op; the single-flight refuses a genuine overlap.)

`--auto` gates BEFORE any lock/vendor work; every deferral is ONE
`pull-all-spot deferred: …` line + **exit 0** (cron-green). The SAME gate
(one module, `calendars/auto_pull_gate.py`) drives the dashboard scheduler's
ticks — its deferrals log `auto_pull.deferred` once per (day, reason):

| reason token          | when                                             | detail text (exact family)                                        |
|-----------------------|--------------------------------------------------|-------------------------------------------------------------------|
| `non_trading_day`     | weekend / listed NSE holiday (incl. Muhurat days) | `<date> is not an NSE trading day — nothing new to pull`           |
| `muhurat_deferral`    | a (hypothetical) trading-day Muhurat listing      | `<date> has a Muhurat session — deferred to the next regular trading day (evening-session settle behavior unverified)` |
| `before_settle`       | trading day, before the settle time               | `waiting for post-close settle time HH:MM IST (…)` — HH:MM is the ACTUAL resolved settle and the parenthetical names the resolved TIER (DEC-SETTLE-EVIDENCE-ADOPTION-001): default → `(conservative — the vendor's true settle time is UNMEASURED; see docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md; override BRUTEX_PULL_ALL_SETTLE_IST)` (byte-identical to pre-#1793); env/flag → `(operator override BRUTEX_PULL_ALL_SETTLE_IST)`; committed evidence → `(measured — probe evidence: earliest all-pass HH:MM IST + N min margin over D trading days; override BRUTEX_PULL_ALL_SETTLE_IST)` |
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

## 3. The settle time — measured when evidence exists, a guess until then

The settle time resolves through the THREE-TIER law
(DEC-SETTLE-EVIDENCE-ADOPTION-001, MIGRATION.md #1793):

1. **Explicit `BRUTEX_PULL_ALL_SETTLE_IST`** (or `--settle-ist HH:MM`, which
   rides the same strict resolver) — ALWAYS wins, evidence never read. Strict
   `HH:MM`, fail-loud on junk, structurally refused below 15:35 (the
   market-hours guard's BLOCK_END — an earlier "settle" would just bounce off
   the untouched guard).
2. **Validated committed probe evidence** —
   `docs/groww/probe/settle_evidence.json` (schema 1; override the location
   with `BRUTEX_SETTLE_EVIDENCE_PATH`; `off` disables the channel). A junk
   EXPLICIT path fails loud at boot / pre-lock; a corrupt file at the DEFAULT
   repo path degrades to tier 3 with one `settle_evidence.invalid` WARN (a
   bad commit must die in CI's committed-file test, never brick fleet boots).
3. **The conservative 17:30 IST default** — a NAMED CONSERVATIVE guess: until
   the evidence file is committed the vendor's post-close settle time stays
   **UNMEASURED** (17:30 is the #1775 probe's last slot).

**The operator flow that replaces the guess** (all of it reviewed, none of it
code): run the probe on ≥5 REGULAR trading days per
`docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md`, then

```bash
python docs/groww/probe/postclose_settle_probe.py \
  --summarize docs/groww/probe/ \
  --emit-evidence docs/groww/probe/settle_evidence.json --margin-minutes 15
```

review the emitted per-day matrix + margin, commit the evidence + its
transcripts in ONE PR (CI's committed-file test validates it), then `git pull`
+ restart (or wait one scheduler tick on a running dashboard) and verify the
`settle_evidence.valid` INFO plus the "measured — probe evidence" deferral /
`next_eligible` text. A >180-day-old evidence file still ADOPTS with a
`settle_evidence.stale` WARN — re-probe and re-commit when it fires.

Why the probe and not production self-learning (the censoring note):
production "settled-by-T" observations are CENSORED at the very observation
schedule being tuned — every gate-pass is seen at/after the current settle
time, so passive evidence can never legitimately LOWER it (and the #1774
gate-pass time is parse-blind to the raw 6-vs-7 row shape only the probe's
`rows_all_len7` measures).

Data safety either way: a too-early pull's still-settling TODAY bars are
withheld by the today-completeness gate (#1774) and the end bound is the
previous day — a mis-set settle time costs bounded deferral churn, never wrong
data.

## 4. Verification loop (after every run)

1. The finish note / stdout `spot:` line — `N window(s) FAILED (will retry)`
   must match the **/coverage/windows** FAILED tab count. (Historical note:
   before PR #1515 landed — DEC-CROSSVERIFY-SPOT-ONLY-001, #1788 — the
   cross-verify tail's producer pull leg could ALSO write `groww-future`
   (FUT) windows there; that leg is deleted, so a fresh run's ledger rows
   are spot-only end-to-end. Rows from pre-#1515 runs may persist in an old
   ledger.)
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
it never widens into an F&O pull). A concurrently-REFUSED click cannot
clobber the running pull's intent either: the launcher SERIALIZES its whole
intent transaction (snapshot → persist → start → restore) under a per-store
process-local mutex and restores the prior on-disk intent on a refused start
(refuter-round-3 F-1 + round-4 G-1/R4-1 — pre-fix a refused full-scope click
during a live spot-only pull left a full-scope intent on disk, and raced
sibling clicks could orphan-clear or re-clobber it even with the restore).
Honest envelope: the mutex serializes launcher threads IN one dashboard
process; cross-PROCESS launch exclusion stays the account-wide flock (a
second dashboard process on the same lake is off-design — its refused
clicks' snapshot/restore do not serialize against this process's). The one
in-process residual is the running pull's own completion clear landing
inside a refused click's restore window: the restore then either leaves the
file honestly absent or resurrects the just-finished pull's OWN intent —
worst case one idempotent boot re-pull of an already-complete pull (zero
re-fetched spot windows; the metadata phase and the cross-verify tail's
producer still issue their per-run vendor calls), never a wrong scope.

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
- `--strict-exit` (CLI) escalates an alert-status-family verdict
  (`refused`/`withheld_partial`/`blocked`/`publish_failed`/`suspect`/`failed`
  — the `TAIL_ALERT_STATUSES` set; named by alert STATUS, not by exit code:
  `refused` carries a variable exit code on the direct `brutex crossverify
  run` surface, 1 on a lock refusal, so it is not uniformly exit-2 —
  escalating it here is the conservative choice)
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

## 8. ▶ Pull F&O — the manual fno-only sibling (round-6 two-button split)

The round-6 coordinator ruling replaced the combined "Pull · full pipeline"
button with TWO buttons on `/universe`:

- **▶ Pull Spot** — the spot-only auto-arming button this runbook describes
  (`scope=spot-only`, `auto_arm=1` on first click).
- **▶ Pull F&O** — the NEW manual sibling: the SAME
  `POST /api/v1/universe/pull-pipeline` launcher with `scope=fno-only`.

`fno-only` is the exact structural mirror of `spot-only`:

- **SPOT phase = honest no-op.** An all-zeros `BulkSpotReport` (empty
  dates/symbols; verdict reads `spot clean (0 pair(s))`) passes the
  sequencer's `spot_phase_ok` gate vacuously; the note surgery replaces that
  clause with **"spot skipped by design (F&O-only scope)"** — a designed
  no-op must never read like a real empty spot pull (negative-pinned).
- **METADATA + F&O HISTORICAL run for REAL** over the whole universe (or
  the ticked selection — the selection checkboxes + the from/to date
  pickers scope the ▶ Pull F&O button exactly as they scoped the removed
  combined button; dates additionally scope which expiries are pulled per
  DEC-FNO-EXPIRY-SCOPE-001). The fenced Greeks pass runs as on any real
  F&O pull.
- **Prebuild = the F&O contract cascade ONLY** (spot materialisation not
  injected — reported as a skipped-by-design phase, the mirror of
  spot-only's empty cascade); the movers warm covers futures + options
  segments only (the spot warm is honestly absent).
- **All rails inherited**: market-hours start gate + mid-run guard, the
  composed Muhurat belt (armed for every non-full scope), the
  today-completeness gate, the intent scope field — a crashed fno-only
  pull resumes FNO-ONLY, never a silently-widened full pull.

**Ruled exclusivities (pinned):**

- The cross-verify tail NEVER fires on an fno-only pull — the tail is
  Spot-exclusive (its contract is the spot CSV byte contract).
- The scheduler NEVER arms or fires fno-only — `auto_arm=1` on any
  non-spot-only scope stays a clean 400. ▶ Pull F&O is manual-only.
- The bare route (no `scope=`) REMAINS the full-chain API default,
  byte-identical (exact-payload-pinned) — nothing existing re-routes.

**CLI:** there is deliberately NO `brutex pull-fno` subcommand in this
change; the headless binding REFUSES `scope=fno-only` loudly. The CLI twin
is the named one-line follow-up in MIGRATION.md #1779 (round-6 record).
