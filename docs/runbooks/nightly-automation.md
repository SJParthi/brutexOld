# Runbook — Nightly zero-click chain (pull → prebuild → sweep) + backup/restore

**DEC-SWEEP-NIGHTLY-AUTOMATION-001 (MIGRATION.md #1541).** The R9 nightly
chain: ONE `brutex nightly run` executes COLLISION → DISK → BACKUP → PULL →
PREBUILD → SWEEP over EXISTING public entry points, under the R42 break law,
with the R50 sealed-snapshot backup/restore and launchd scheduling.

> **Audience:** the operator (one Mac, one lake). **Time:** ~10 min one-time
> bootstrap; ~0 clicks/day thereafter (the morning Telegram is the interface).
> **Golden Rule #1 pledge:** every path below traces to the cited file:line on
> this build (2026-07-07).

## 0. What runs when

| # | Link | What it does | Source |
|---|------|--------------|--------|
| 1 | COLLISION | acquires the nightly + account + lake pull locks (acquire-and-HOLD — the probe IS the acquisition, zero TOCTOU) + the sweep-liveness heuristic | `brutex/nightly/run.py` (`collision_fn`), `brutex/adapters/groww/pull_singleflight.py` |
| 2 | DISK | fail-CLOSED free-space floors, backup requirement INVENTORY-sized ×1.2 | `brutex/nightly/probes.py::check_disk_floors` |
| 3 | BACKUP | seals `<backup_root>/<nightly_id>` (pre-mutation state — the rollback point) + retention prune | `brutex/nightly/backup.py::create_snapshot` |
| 4-5 | PULL + PREBUILD | ONE headless `run_full_pipeline` execution (spot → metadata → historical → prebuild), decomposed into two reported links | `brutex/nightly/pipeline_binding.py` |
| 6 | SWEEP | `cmd_sweep_all(**kwargs)` on the daemon key-list precedent; counts from the audit tail | `brutex/nightly/sweep_binding.py` |

Break law (R42): the FIRST `REFUSED`/`FAILED` link stops the chain; every
later link is recorded `BLOCKED` with `blocked_by` naming the breaker — a
break at link N never corrupts links 1..N-1, recovery starts at N, never zero
(`brutex/nightly/chain.py::run_chain`; property-pinned over every failure
position × mode in `tests/property/test_nightly_chain_property.py`).

| Link fails | Status | Later links | Exit | Telegram |
|---|---|---|---|---|
| second nightly / live pull / live sweep | REFUSED | BLOCKED | 1 | `nightly_refused` CRITICAL (instant) |
| disk floor breached | REFUSED (exact bytes named) | BLOCKED | 1 | `nightly_refused` CRITICAL |
| backup copy/hash/ENOSPC | FAILED (`.partial` left, never sealed) | BLOCKED | 2 | summary CRITICAL |
| pull halt / pipeline failure | FAILED (halt alert fires first) | BLOCKED | 2 | halt CRITICAL + summary CRITICAL |
| prebuild failure | FAILED (quarantined symbols named) | SWEEP BLOCKED | 2 | summary CRITICAL |
| sweep preflight refusal (exit 1) | REFUSED | — | 1 | summary CRITICAL |
| sweep failed tuples (exit 6) | FAILED | — | **6 pass-through** | summary CRITICAL |
| ANY tripwire | (links may be green) | — | **2 — SUSPECT is cron-RED** | summary WARN/CRITICAL "⚠️ SUSPECT" |
| all green | OK/SKIPPED | — | 0 | summary INFO |

## 1. One-time bootstrap (macOS)

```
brutex nightly install                      # default 01:30 local; --at HH:MM to change
brutex nightly run --dry-run                # the post-install smoke: probes only, NO locks, ZERO mutation
brutex notify test                          # prove the Telegram channel BEFORE trusting silence
sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00   # REQUIRED, run yourself — see §7
```

The `pmset` scheduled wake is **MANDATED** (gap-register 2026-07-08 row 20):
launchd cannot wake a sleeping Mac, so without it the nightly silently does
not run on any night the machine sleeps. `nightly install` prints the exact
command computed for YOUR `--at` (5 minutes before the trigger —
`schedule.pmset_wake_command`); BruteX documents + prints it and NEVER runs
it (sudo, a system-wide power setting).

`install` writes `~/Library/LaunchAgents/com.brutex.nightly.plist`, loads it,
and then MANDATORILY verifies `launchctl list com.brutex.nightly` succeeds —
**installed-but-unverified is a FAILURE (exit 2)**: a plist that launchctl
does not list will NEVER fire (the TickVault never-installed lesson,
mechanized in `brutex/nightly/schedule.py::install`). No secrets enter the
plist — `bootstrap_env()` loads `~/.brutex/.env` in-process.

Linux: no launchd — schedule via cron (plain cron misses = skipped, no
catch-up; anacron is the named alternative):

```
30 1 * * * BRUTEX_LAKE_ROOT=<abs lake root> /abs/path/to/brutex nightly run >> ~/.brutex/logs/nightly-cron.log 2>&1
```

## 2. The morning Telegram

```
🌙 Nightly nightly-20260708T013000Z-a1b2c3 — ✅ OK in 1:07.
collision ok · disk ok · backup ok (2481 files) · pull ok (52340 bars) · prebuild ok (214 built) · sweep ok (12 run · 40 skipped-cache · 0 failed)
```

- Unknown counts read a literal `?` — NEVER a fabricated 0 (`bars_net_new`
  precedent; `brutex/notifications/sweep_alerts.py::nightly_summary`).
- A refusal ALSO fires the instant `nightly_refused` at the gate itself, so a
  01:30 collision is known at 01:30, not 06:00. Dedupe keys are PER-RUN — a
  same-night manual retry is never swallowed.
- `--skip-backup` nights append: "⚠️ backup SKIPPED by operator flag — stores
  unprotected tonight."
- **No message ≠ no run**: Telegram is best-effort by contract (disabled =
  silent). The independent surfaces are §3.

## 3. 6 a.m. triage

```
brutex nightly status
```

Five honest lines: schedule (plist present? launchctl listed? next fire),
last run (`nightly_id`, exit, blocker, age), latest sealed snapshot, the
**row-20 dead-man verdict** (`OK` / `MISSED` / `UNKNOWN`), and the lock-file
caveat (existence ≠ held — a flock cannot be observed without acquiring it).

The dead-man (`schedule.deadman_verdict`, pure + fully tested) is the ONLY
signal for the perfectly-silent staleness of a Mac asleep at the trigger —
no process starts, so no in-run alerter can fire. It compares the newest
per-run dir under `<results_root>/nightly/` (created at chain START, so a
long-running sweep leg never reads stale; the shared `audit/` dir is
excluded) against the most recent expected fire + a 3 h grace. A `MISSED`
verdict also fires `SweepAlerter.nightly_deadman` (CRITICAL, distinct
`nightly-deadman` dedupe key) best-effort from `nightly status`. The
dashboard-BANNER half of row 20 is **P4's zone** — the banner wires this
same `deadman_verdict` seam at merge-integration. Deeper forensics, per run:

| Artifact | Path |
|---|---|
| report (frozen `brutex-nightly-report-v1`) | `<results_root>/nightly/<id>/report.json` |
| diagnostic bundle (report copy · audit tails · progress · allowlisted env · versions · timings) | `<results_root>/nightly/<id>/bundle/` |
| hash-chained nightly audit (one chain across runs) | `<results_root>/nightly/audit/nightly.jsonl` |
| launchd stdout/stderr | `~/.brutex/logs/nightly-launchd.{out,err}.log` |

Exit codes: `0` clean · `1` refused/user error · `2` failed OR **SUSPECT**
(any tripwire — count reconcile, audit reconcile, snapshot re-read, sweep
count identity — marks the run untrustworthy and cron-RED) · `3` restore
verification findings · `6` sweep failed-tuples pass-through.

## 4. Backup operations (R50)

Scope (fixed inventory, `brutex/nightly/backup.py::backup_inventory`):

| In | Out (by law, reason recorded in the manifest) |
|---|---|
| `window_ledger/` (ledger.parquet + deltas), `registry.duckdb`, `instruments.duckdb`, `catalog.duckdb`, lake `audit/` | `bars/` — re-pullable; full-lake protection stays the manual path (`lake-backup-restore.md`) |
| the whole `sweeps/` tree: tuple parquets, `.complete.json` sentinels, trial stats, `audit/*.jsonl`, `checkpoint/`, `dedup-cache/` LMDB, `cascade/`, PRIOR `nightly/<id>/` dirs — incl., when present, the in-tree `<results_root>/sweep_runs/` run artifacts (Ruling 4) + `<results_root>/estimator_calibration.json` (Ruling 13, the R20/R23 calibration store) | `.locks/` (kernel-released flocks), `.rate/` (transient), `.quarantine/` (heal forensics), `sweeps/progress/` (transient — restoring a stale "running" would false-positive the liveness probe), the CURRENT `nightly/<id>/`, the SHARED cross-run `nightly/audit/` chain (live-appended by the very command that snapshots/restores — a restored copy would `os.replace` the writer's open inode, rolling the forensic chain back to snapshot time and losing RESTORE_START/RESTORE_DONE to a dead inode; each run's audit tail is durably captured in its prior `nightly/<id>/bundle/`) |
| the SIBLING run-artifact root `<results_root parent>/sweep_runs/` — the SHIPPED P3 `trade_store.sweep_run_dir` layout (top100.json, baselines, results-depth sidecars), a third `sweep_runs` manifest namespace, include-if-present | `~/.brutex/runs/` — the Ruling-15 INTERNAL-disk forensics mirror (flight recorder / diagnostic bundle). It exists precisely to survive the external volume this backup lives on, so it is NEVER dragged into the backup; protect it yourself with e.g. `rsync -a ~/.brutex/runs/ <second-volume>/brutex-runs-mirror/` (or Time Machine) |

Consistency: everything in scope is quiescent — the nightly holds the pull
locks (no ledger/registry/audit writer can run) and the liveness probe
passed; every producer is atomic tmp→fsync→replace, so file-level copies are
internally consistent. The dedup-cache LMDB is copied cold; on restore the
Bloom rebuilds from LMDB on open, and a stale cache is results-safe —
sentinels are the redo authority.

Atomicity: staged under `<id>.partial/`, per-file tmp→`os.replace`, manifest
(hash-of-DESTINATION-bytes) written LAST, FULL pre-seal re-verify
(`BRUTEX_NIGHTLY_BACKUP_VERIFY=0` documented escape), ONE atomic dir rename.
**A non-`.partial` snapshot dir is by construction complete + verified.**
Retention keeps the newest `--keep` (default 7, `BRUTEX_NIGHTLY_BACKUP_KEEP`),
never the just-written snapshot, and GCs `.partial` orphans older than 24 h.

Boot-disk caveat: the default `~/.brutex/backups` may share the boot disk —
NOT disaster protection. Point `--backup-root` /
`BRUTEX_NIGHTLY_BACKUP_ROOT` at a second volume for that.

## 5. Restore drill (operator-runnable)

```
brutex nightly restore --snapshot <backup_root>/<id> --dry-run   # ordered plan + verify verdict
brutex nightly restore --snapshot <backup_root>/<id> --yes       # the live restore
brutex doctor                                                    # post-restore health
```

Laws (`brutex/nightly/restore.py`): Phase A re-hashes EVERY entry BEFORE a
single destination byte (mismatch → exit 3, lake byte-untouched); Phase C
copies in `restore_order` with the destination `window_ledger/deltas/`
CLEARED of manifest-extraneous `*.parquet` first (a leftover post-snapshot
delta would fold latest-wins OVER the restored base — the #1533 QUALIFY law)
and `ledger.parquet` STRICTLY LAST (its stat token is every dashboard cache's
cross-process refresh signal); Phase D re-hashes the destination. A LEGACY
snapshot whose manifest still carries the shared `nightly/audit/` chain has
those entries SKIPPED (named `SKIP …` in the printed plan) — restore appends
RESTORE_START/RESTORE_DONE to that very chain and must never copy the
snapshot's stale copy over the writer's open inode.

| Refusal | Exit |
|---|---|
| `.partial` dir / missing / junk manifest | 1 |
| verification mismatch (Phase A or D) | 3 (`EXIT_VERIFY_FINDINGS` — the `audit verify` precedent) |
| nightly/pull lock held; live sweep | 1 ("stop the dashboard/pull/sweep first") |
| missing `--yes` | 1 (the plan is printed first) |

CI exercises this drill on every push
(`tests/integration/test_nightly_restore_drill.py`: real-writer lake →
snapshot → mutilate + extra delta → restore → byte-parity + deltas-clear +
ledger-last + audit chain green; `tests/unit/test_cli_nightly.py` drives the
same via `cli.main`). Post-restore, bars↔ledger drift since the snapshot
reconciles via `reconcile_ledger_with_lake` / `seed_lake_success`
(`brutex/data/window_ledger_store.py`).

## 6. Collision + disk laws

| Lock | Order | Held until | Contention answer |
|---|---|---|---|
| `nightly_singleflight.lock` (holder note `nightly:<id>`) | 1 | end of chain | REFUSED `nightly_already_running` |
| account pull lock (`~/.brutex/locks/pull_account*.flock`) | 2 | released after PREBUILD (no vendor calls during the sweep) | REFUSED `pull_in_flight` |
| `pull_singleflight.lock` (holder note `nightly:<id>`) | 3 | **EX through PULL+PREBUILD, then EX → SH for the whole SWEEP leg** (DEC-NIGHTLY-SWEEP-LOCK-POSTURE-001, MIGRATION.md #1772) | REFUSED `pull_in_flight` — never queued |
| sweep-liveness probe (NOT a lock) | 4 | n/a | REFUSED `sweep_alive(<evidence>)` |

The R4/G10 sweep-vs-pull exclusion stays mechanical for the WHOLE sweep leg,
now delivered by a SHARED hold: the original "EX held through SWEEP" law was
written pre-G10 ("no sweep-level flock exists"); once `cmd_sweep_all` grew its
own G10 `lake_read_guard` `LOCK_SH|LOCK_NB` on the same lock file, the
nightly's held EX self-conflicted with that second-fd SH and the SWEEP link
died with `LakeBusyError` every night. The nightly now releases its EX at the
SWEEP link and holds the G10 shared read-guard for the entire leg — a manual
pull attempted mid-nightly is still refused with `(holder: nightly:<id>)`
(the note bytes survive the transition) — correct, because its bars would
re-key the data under the sweep's feet; `cmd_sweep_all`'s own SH composes. A
peer pull landing in the few-syscall release→reacquire window makes the leg
REFUSE loudly (`pull_in_flight`, log `nightly.sweep_lock_downgrade_lost_race`)
— fail-safe, next nightly retries; never a silent exclusion gap.
try-acquire-and-release was rejected as a TOCTOU. A missing AWS credential
skips the account lock with one loud `nightly.account_lock_skipped` WARN.
A long nightly crossing 06:00 IST is safe — the token re-mint self-heals
mid-pull (the reauth re-read).

The sweep-liveness signal is **honestly a HEURISTIC** (dual mtime:
`progress/sweep-progress.json` `state=="running"` + file age, and
`checkpoint/sweep-all.json` age; window `BRUTEX_NIGHTLY_SWEEP_FRESH_SECONDS`,
default 900): the snapshot's `updated_at` is process-local `time.monotonic()`
— unusable cross-process. False-negative = wasted compute, never corruption
(checkpoint/sentinel/cache laws); false-positive = refuse-on-doubt. The probe
PREFERS the mechanical `<results_root>/.locks/sweep_singleflight.lock` the
moment P2 lands it.

Disk floors (fail-CLOSED — contrast `make_disk_guard`, which fails OPEN
because it protects a RUNNING pipeline): `BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES`
(default 20 GiB) and `BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES` (default 10 GiB,
raised to `1.2 × inventory` when larger). The refusal Telegram carries the
exact free/required bytes. The inventory walk is honestly O(files).

Never run the sweep-daemon (02:30 precedent) AND the nightly against one
lake — the liveness probe refuses when they overlap.

## 7. Scheduling honesty (sleep/wake)

launchd `StartCalendarInterval` does NOT wake a sleeping Mac; a fire missed
while ASLEEP runs ONCE at next wake (coalesced); missed while POWERED OFF is
skipped entirely. `keep_awake` (caffeinate) holds the machine awake WHILE the
chain runs but cannot wake it at 01:30. On-time fires therefore REQUIRE the
operator's one-time scheduled wake, MANDATED by §1 (gap-register row 20) —
`sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00` for the default 01:30
trigger; `nightly install` prints the exact command for your `--at`.
Documented + printed, NEVER run by BruteX (a system-wide power setting; we
do not sudo on the operator's behalf). A late (next-wake) run that collides
with a morning manual pull is a LOUD Telegram'd refusal — designed behavior.
The dead-man verdict on `nightly status` (§3) is the detection net for the
silent non-run; the P4 dashboard banner wires the same seam at
merge-integration.

## 8. Crash windows (crash-at-any-point recovery)

| Crash point | State left | Recovery |
|---|---|---|
| before/at lock acquire | nothing written | none needed; the kernel released any half-taken flock on fd close/death |
| mid-backup | `<id>.partial/` dir | never consumed (restore/retention see only sealed dirs); pruned by a later run's retention pass (>24 h) |
| mid-pull | partial lake writes | existing law: atomic writers + keep-first sink + durable ledger (mid-pull flush INSIDE `pull_all_spot`) ⇒ the next pull resumes |
| mid-prebuild | partial derived TFs | per-symbol quarantine; the sweep's derived-freshness gate refuses stale TFs regardless |
| mid-sweep | checkpoint/sentinels | native sweep resume law; the next nightly's sweep leg resumes/cache-skips (R6) |
| mid-audit-append | torn JSONL tail | `SweepAuditWriter.open(resume=True)` truncates to the last committed entry |
| before the report write | no report.json | the report is written in a `finally`; a SIGKILL before it leaves the audit chain naming the last completed link + the launchd nonzero exit as the forensic signal |
| SIGKILL anywhere | all of the above | all flocks kernel-released; NO pid files anywhere in this design (flock-only, deliberate) |

## Honest envelope (Golden Rule #10)

1. **R48 decay diff NOT built** — deferred to v1.1 by the explicit operator
   phase directive. This is a recorded DIVERGENCE from the living
   requirements, which home R48 (A-approved) in P6 — the file's v1.1 deferral
   is R49. The P3 `top100.json` presence probe is the documented v1.1 seam;
   every `NightlyReport.honesty_notes` carries the divergence sentence.
2. Manual-sweep detection is a HEURISTIC until P2's results-root flock lands
   (§6) — it can false-negative a wedged sweep (wasted compute, never
   corruption) and false-positive a just-killed one (refuse-on-doubt).
3. `bars/` is NOT in the automated backup — re-pullable by design; full-lake
   (incl. `bars/`) protection stays the manual rsync/Time-Machine guidance in
   `lake-backup-restore.md`. The default backup root may share the boot disk.
4. Sleep/wake limits per §7; cron misses are skipped (anacron named).
5. **`code_commit_sha` re-key caveat:** the sweep tuple identity folds the
   commit SHA, so the FIRST nightly after ANY deploy re-runs the whole grid
   over byte-identical data — reproducibility-correct (Golden Rule #4),
   expensive, and out of this phase's zone to change. The Telegram skip
   counts make it visible; a `skipped_cache == 0` night (with a prior sweep
   audit) carries a "possible commit re-key" honesty note.
6. **≤24 h backup exposure:** night-N sweep results are captured night N+1
   (backup runs PRE-pull by design — the snapshot is the last-known-good
   rollback point); a post-sweep second snapshot is a named v1.1 lever.
7. Sweep counts are best-effort (the `SUPERVISOR_ACTION_COMPLETE` audit-tail
   payload; absent ⇒ `?`, never 0); an SLA-overrun-sized grid makes the SWEEP
   link REFUSED every night until the operator shrinks the grid or runs a
   supervised `sweep-all --acknowledge-sla-overrun` — the nightly
   hard-refuses all three escape flags (STRICTER than the daemon precedent,
   which refuses only `--allow-out-of-scope`).
8. Restore is file-copy semantics, not transactional; a mid-restore crash
   leaves a mixed old/new set — re-run the restore (idempotent).
   Manifest-extraneous files OUTSIDE `window_ledger/deltas/` are not deleted
   (documented hazard; a stray tuple parquet is inert without its sentinel).
9. The nightly holds the lake pull lock through its sweep leg — manual pulls
   on this lake are refused for the duration (deliberate, loudly attributed
   via the holder note).
10. Backup/restore are honestly O(files + bytes) — never claimed O(1); the
    probes are O(1)-per-call and bench-pinned flat
    (`tests/bench/test_nightly_probe_o1.py`).
11. Telegram disabled = silent by contract; exit codes + `report.json` +
    the audit chain + launchd logs + `nightly status` are the independent
    surfaces. The bootstrap prescribes `brutex notify test`.
12. Two OS users / cross-host pulls on one account remain uncovered (the
    existing documented residual of the account lock).
13. A nightly `--dry-run` does NOT execute the pipeline (even in the spines'
    own dry-run mode) — the dashboard redirects dry-run pulls to an EPHEMERAL
    lake because they still write synthetic bars; the nightly smoke instead
    reads probes + inventory only and mutates NOTHING (tree-oracle pinned).
14. A concurrently-open dashboard's rate widget reads unwired/idle during a
    nightly (the observe-only sampler is dashboard-owned); pacing itself
    binds identically via the shared `SessionRateController` + 500/min
    governor.
15. **Daemon-side SLA ruling PENDING (gap-register row 2):** the nightly
    HARD-refuses `--acknowledge-sla-overrun`, so an institutional-scale grid
    that trips `check_sweep_sla` is LOUDLY REFUSED every night (exit 1,
    CRITICAL summary) until P1 lands the acknowledge/calibrate path — never
    a silent overrun, but also never an unattended institutional run today.
16. **Incremental nightly = FULL recompute until P2's per-slice re-keying
    (row 16):** every tuple key folds the GRID-wide `data_snapshot_hash`, so
    the nightly's fresh pull re-keys EVERY tuple and the sweep leg re-runs
    the whole grid each night — composing with the §5 `code_commit_sha`
    caveat (a deploy alone forces the same full re-run). The Telegram
    `skipped-cache` count is the visibility; P2's per-slice snapshot
    component is the fix.
17. **`keep_awake` on the SWEEP leg is PENDING P2 (row 19):** today it wraps
    only fetch/enrich/pull — the sweep entry points (incl. `cmd_sweep_all`,
    which the nightly drives) carry no caffeinate assertion. Mitigation, not
    a fix: the launchd-started nightly holds `keep_awake` around ITS pull
    leg, and the mandated §7 `pmset` wake gets the machine up — but a
    display-sleep policy can still sleep the box mid-sweep until P2 wires
    all six entry points.
18. **No free-disk preflight inside `cmd_sweep_all` until P1 (row 22):** the
    sweep itself can still die resumably on ENOSPC hours in. The nightly's
    OWN fail-closed DISK link floors (§6) mitigate at chain level — they
    refuse the whole night BEFORE mutation when free space is below the
    floors — but they are static floors, not the est_bytes arithmetic P1
    will add at click/launch time.
19. **SENSEX / multi-symbol fan-out PENDING P1 (Ruling 9):** the nightly
    forwards ONE `cmd_sweep_all` invocation (one exchange/segment); the
    one-logical-`sweep_run_id`-wrapping-N-engine-runs fan-out that brings
    SENSEX (BSE) into the same nightly is P1's entry wiring — until it
    lands, a default nightly sweeps the NSE selection only.

## Decision trail

- **DEC-SWEEP-NIGHTLY-AUTOMATION-001** (this runbook, MIGRATION.md #1541)
- Builds on: DEC-CLI-GREEKS-ENRICH-001 #1536 (lock/keep-awake/cron-visible
  exit idioms) · DEC-PULL-SIX-STEP-ORDER-001 #1527 (strict-gate seam) ·
  DEC-SWEEP-SCHEDULER-001 (the sweep-daemon precedent) ·
  DEC-CLI-LEDGER-CHECKPOINT-001 #884 (mid-pull durable ledger) ·
  DEC-LAKE-PARTITION-FLOCK-001 #887 + DEC-ACCOUNT-KEY-SINGLEFLIGHT-001 #1285
  (the flock family) · DEC-RUNBOOK-REFRESH-002 #1305 (house runbook format) ·
  DEC-LAKE-HEAL-001 #1175 (heal-before-derive ordering, inherited via the
  pipeline spines) · DEC-LEDGER-DELTA-O-PAGE-001 #1533 (the deltas
  latest-wins fold the restore's deltas-clear law exists for).
