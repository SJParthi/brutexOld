# ADR-0017: Off-box incremental backup via restic (DEC-BACKUP-OFFBOX-001)

## Status
Accepted (2026-07-06). Implements `docs/REMOTE_CONTROL_AND_ZERO_TOUCH_COMPARISON.md:304`
(checklist item 8), matrix rows 28/30 (`:156,:158`), dead-man keystone (`:27-31,:287`).

## Context
Sweeps + the hash-chained audit logs are NOT re-pullable (Golden Rules #4/#8); the ~8 TB lake
is re-pullable only at multi-day vendor cost. Ransomware / `rm -rf` / SSD death currently
loses everything. The manual tar runbook (`docs/runbooks/lake-backup-restore.md`) exists but
is unscheduled, unverified, and unalerted.

## Decision
restic (content-addressed incremental, encrypted, `check --read-data-subset` native) to an
immutable remote (S3 Object-Lock compliance / B2 object-lock). Nightly launchd run with
preflight mount gate + verify tiers; O(1) state-file dead-man with CRITICAL Telegram +
durable re-alert throttle + daily remote cross-check; monthly canary+sample restore drill
with byte verification and the audit byte-PREFIX law. rclone rejected: sync ≠ snapshots
(no point-in-time restore, no dedup, no verify subset). Full design: DEC-BACKUP-OFFBOX-001,
MIGRATION.md #1596.

## External-tool facts relied on (cited; PROBED against the installed binary — runbook step 2)
- restic backup exit code 3 = snapshot CREATED, some source files unreadable (restic docs,
  "Exit codes", https://restic.readthedocs.io/en/stable/040_backup.html — verified 2026-07-06).
- `restic check --read-data-subset=n/t` reads a deterministic pack bucket (restic docs,
  https://restic.readthedocs.io/en/stable/045_working_with_repos.html — verified 2026-07-06).
- `restic unlock` removes ONLY stale locks of dead processes (restic docs — verified 2026-07-06).
- restic registers a snapshot only on completion — a killed upload never becomes a torn
  "latest" (restic docs, architecture — verified 2026-07-06).

## Manual-acceptance register (each row is an operator commitment, per Golden Rule #10 — these steps are ONE-TIME or physically irreducible, never silently omitted)
| # | Manual step | Why not automatable | Frequency | Residual risk accepted |
|---|---|---|---|---|
| M1 | Install restic (`brew install restic`, pin ≥ 0.16) + run the runbook fact-probe | third-party binary provisioning | once | version drift → probe step re-run on upgrade |
| M2 | Provision S3 Object-Lock / B2 bucket + scoped key (no delete / no bypass-governance) | cloud account + billing + IAM are human trust decisions | once | mis-provisioned immutability weakens ransomware defense — runbook verify step |
| M3 | Create repo password; store in keychain AND an offline copy | secret genesis; software cannot verify the offline copy | once | lost password = ALL snapshots unrecoverable — named, not hidden |
| M4 | Fill plist placeholders; `launchctl bootstrap` the 3 jobs; run the first 8 TB seed manually | operator paths/TCC consent; multi-day seed is human-priced | once | until the seed completes, RPO = ∞ (dead-man reads RUNNING/NEVER_SUCCEEDED honestly) |
| M5 | Enable Telegram (`BRUTEX_TELEGRAM_ENABLED=true` + `brutex notify test`) + the live negative dead-man drill | repo-wide default-OFF is a deliberate consent gate (telegram.py:238) | once | with Telegram off, alerts = exit codes + logs + the ping lane only |
| M6 | Provision healthchecks.io; set `BRUTEX_BACKUP_HEALTHCHECK_URL` | the external watcher account IS the keystone (blueprint :303) | once | without it, box-death is silent (honest envelope §2.1) |
| M7 | Quarterly FULL restore DR exercise to a spare disk | 8 TB egress/wall-time is a priced human decision | quarterly | drill sampling honesty note stands |
| M8 | Remote capacity/retention review | paying for storage is a human decision | quarterly | Object-Lock blocks prune until retention lapses — `maintenance_ok=false` expected |

## Consequences
+ Ransomware/deletion/SSD-death recovery becomes snapshot-restore + ledger-driven re-pull
  mop-up (the prior runbook's restore-ORDER law).
− A new external binary + remote spend; prune-vs-Object-Lock interplay (documented).
Until M6, box-death is silent. Until M5, alerts are logs + exit codes only.

## Rollback
Remove the three LaunchAgents (`launchctl bootout` + delete); the package is inert without
`BRUTEX_BACKUP_REPO`. No engine surface touched.
