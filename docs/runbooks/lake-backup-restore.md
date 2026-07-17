# Runbook — Lake Backup + Restore

> **Target audience:** the operator (sjparthi) backing up the multi-year
> Parquet lake (or migrating it to a new box / SSD), and any Claude session
> asked "how do I back the lake up safely?". **Time required:** minutes plus
> copy time. **Golden Rule #1:** every path below traces to the cited
> `file:line` on this build (2026-07-03).

> **Amendment (2026-07-07, DEC-SWEEP-NIGHTLY-AUTOMATION-001, MIGRATION.md
> #1541):** the MECHANIZED nightly path is `brutex nightly backup` /
> `brutex nightly restore` (`docs/runbooks/nightly-automation.md` §4-5) for
> the stores it covers — the window ledger (+deltas), `instruments.duckdb`,
> `registry.duckdb`, `catalog.duckdb`, the lake `audit/` and the whole
> `sweeps/` results tree — with sealed hash-verified snapshots, a
> verify-everything-then-write restore (destination `window_ledger/deltas/`
> cleared of manifest-extraneous files; `ledger.parquet` strictly LAST, the
> law §4 below states), and a CI restore drill on every push. **`bars/` is
> deliberately NOT in the automated set** (re-pullable) — the tar/rsync/
> Time-Machine recipe below REMAINS the full-lake (incl. `bars/`) manual
> path. No law below changes.

## 0. Where the lake is

The lake root resolves as `$BRUTEX_LAKE_ROOT`, else `~/.brutex/lake`
(`brutex/config/paths.py:19` `ENV_LAKE_ROOT`; `resolve_lake_root` `:50-69`,
`~`-expanded). BruteX home state lives beside it under `~/.brutex`.
Note for macOS external volumes: a `/Volumes/<name>` lake root has a
dead-mount identity check (`paths.py:72-75`) — restore to the SAME volume
name or re-point `BRUTEX_LAKE_ROOT`.

## 1. The backup set (everything under `<lake_root>`)

| Path | What it is | Source |
|---|---|---|
| `bars/<EXCH>/<SEGMENT>/<symbol>/<interval>/<yyyy>/<mm>.parquet` | spot + F&O candle partitions (F&O under `FNO/`) | CLAUDE.md §6 F&O-lake row; `data/fno_candle_lake.py` |
| `window_ledger/ledger.parquet` | THE durable pull ledger — every production pull checkpoints it per window | `data/window_ledger_store.py:80-81,117-120` |
| `instruments.duckdb` | lean instrument master | `data/instrument_store.py:153` (`DEFAULT_DB_NAME`) |
| `registry.duckdb` | expiry/contract registry cache (sibling of the master) | `dashboard/db_view.py:211` (`REGISTRY_DB_NAME`) |
| `catalog.duckdb` | Parquet-partition catalog | `cli.py:1379` (`Catalog.open(resolved / "catalog.duckdb")`); the "default `<lake-root>/catalog.duckdb`" phrase is the `--catalog` flag help of `brutex run --help` etc. |
| `sweeps/<instrument>/<tf>/*.parquet` | per-tuple sweep-results tree | `dashboard/lake_revision.py:22-31` |
| `audit/` | hash-chained audit JSONL (retry give-ups etc.) | `brutex fetch --help` (`--max-retry-minutes`: "audit rows under `<lake>/audit/`") |

**EXCLUDE `.locks/`** — transient advisory `flock` sidecars only: the pull
single-flight lock (`adapters/groww/pull_singleflight.py:89`
`pull_singleflight.lock`) and the per-partition `<sha256(dest)>.lock` files
(`data/partition_lock.py`, DEC-LAKE-PARTITION-FLOCK-001 #887). The kernel
releases flocks on process death — there is no stale-lock state worth
backing up, and restoring old lock files is pure noise.

**Outside the lake (also back up when migrating a box):**
`~/.brutex/runtime.json` (status store), the stored auth-token state saved by
`brutex auth` (0600 atomic save — DEC-ATOMIC-WRITE-FINISH-001 #891; the Groww
credentials themselves live in AWS Parameter Store, NOT on the box —
DEC-AWS-ONLY-CREDS-001), and `~/.brutex/cache/ntm_constituents.csv` (the NTM
last-good cache, DEC-NTM-CACHE-FIRST-001 #892).

## 2. Backup (suggested shape)

> **AUTOMATED by `brutex backup` — DEC-BACKUP-OFFBOX-001 (MIGRATION.md
> #1596), see `docs/runbooks/backup-offbox.md`; the tar recipe below
> remains the manual fallback.** The restore-ORDER law (§3) and the
> consistency argument (§4) below stand verbatim and are what the
> automated system's restore procedure follows.

Never back up during a running pull — take (or wait on) the single-flight
lock semantics of `data-fetch-runday.md` §6a first (a pull holds
`<lake_root>/.locks/pull_singleflight.lock` for its whole duration; a quiet
lake is a consistent lake because every writer is atomic
tmp→fsync→`os.replace`).

```bash
LAKE_ROOT="${BRUTEX_LAKE_ROOT:-$HOME/.brutex/lake}"
tar -C "$LAKE_ROOT" --exclude='.locks' -czf "brutex-lake-$(date +%F).tgz" \
  bars window_ledger sweeps audit \
  instruments.duckdb registry.duckdb catalog.duckdb
```

(Any of the members may be absent on a young lake — drop absent names from
the list, or add `--ignore-failed-read`.)

## 3. Restore — the ORDER law

1. **Stop the dashboard and every pull first.** Never restore while a pull
   runs (the single-flight lock exists precisely because two writers on one
   lake corrupt each other's read-modify-write cycles).
2. Untar `bars/`, the three `*.duckdb` files, `sweeps/`, `audit/`.
3. Restore `window_ledger/ledger.parquet` **LAST** — then restart the
   dashboard (or wait one poll).

**Why the ledger goes last:** the dashboard's generation-keyed caches
(`/db` totals, dataset discovery, `/coverage/windows` counts, `/movers`,
completeness, `/results`) refresh off a composite O(1) revision token —
one `os.stat` of `window_ledger/ledger.parquet` plus a bounded
directory-mtime digest of `sweeps/` (`dashboard/lake_revision.py:89-216`
`lake_revision`, wired into `SummaryCache(revision_probe=…)` in
`dashboard/app.py:1102`). Restoring the ledger last makes its mtime/size
change the FINAL signal, so a running dashboard invalidates every cache
after the bars are already in place. Restarting the dashboard achieves the
same thing unconditionally.

## 4. Honest envelope (Golden Rule #10 — say the limits)

- **Out-of-band `bars/` writes are NOT detected.** The revision token moves
  only when a writer touches the window ledger or the sweeps tree; a raw
  partition file hand-copied into `bars/` that bypasses the ledger is
  invisible to a RUNNING dashboard (`lake_revision.py:39-47` docstring — the
  write-time lake manifest remains named future work, #867/#1025). After any
  manual surgery inside `bars/`: **restart the dashboard.**
- **Deleting a `bars/` partition while its ledger row stays `SUCCESS` is
  SILENT data loss — a later incremental pull does NOT restore it.** The
  window ledger is the SOLE skip authority (`data/window_ledger.py`
  `is_skippable`): a settled `SUCCESS`/`SKIPPED` window is skipped on ledger
  authority, never re-verified against lake presence, so the deleted
  partition's window is never re-fetched. Neither remediation lever covers
  this case: `brutex reopen-skipped` reopens only `SKIPPED` rows, and
  `record(degraded=...)` is a DURING-pull demotion the spine applies to a
  window it just fetched — nothing watches for a vanished partition behind a
  `SUCCESS` row. A naive in-memory `record(error_type=...)` FAILED demotion
  is also silently REVERTED by the save-merge's more-settled-wins rule
  (`window_ledger_store.py` `merge_ledger_rows`). Real recovery: delete (or
  reopen via a `record(..., degraded=True)` + save, which registers the
  tombstone the merge honours) the window's ledger row so the next pull
  re-fetches it, or restore the partition from backup. Pinned end-to-end by
  `tests/integration/test_incremental_pull_cycle.py`
  (`test_deleted_partition_with_success_ledger_is_not_repulled`).
- **A lost/absent ledger is recoverable, but expensively.** The next pull
  re-seeds settled windows from the lake bars it finds
  (`reconcile_ledger_with_lake` / the cold-start `seed_lake_success` bridge,
  `data/window_ledger_store.py:891`), and genuinely-empty windows re-earn
  their `SKIPPED` status over re-pulls — safe (idempotent keep-first sink),
  but the first pull after losing the ledger does real re-verification work.
  Backing the ledger up with the bars avoids that cost.
- **This is a file copy, not a transactional snapshot.** Consistency comes
  from backing up a QUIET lake (step-2 precondition), not from tar. Partial
  restores (bars without their ledger rows, or vice versa) are tolerated by
  design — the ledger reconciles against the lake — but expect re-pull work
  proportional to the mismatch.

## Decision trail

- DEC-CLI-LEDGER-CHECKPOINT-001 (#884) — every production pull checkpoints
  the durable ledger per window (what makes the ledger worth backing up).
- DEC-LAKE-PARTITION-FLOCK-001 (#887) + DEC-PULL-HARDENING-FOLLOWUP-001
  (#996) — the `.locks/` sidecars this runbook excludes.
- DEC-HARDENING-LEFTOVERS-001 (#1149) — the ledger-stat revision probe the
  restore-order law rides on.
- DEC-WINDOW-LEDGER-RESCUE-001 — merge-on-save; two writers never clobber
  each other's ledger rows.
