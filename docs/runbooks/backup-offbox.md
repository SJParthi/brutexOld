# Runbook — Automated Off-Box Backup (restic) · Install / Operate / Restore

> **Decision:** DEC-BACKUP-OFFBOX-001 (MIGRATION.md #1527; ADR
> `docs/adr/0017-offbox-backup-restic.md`). **Blueprint authority:**
> `docs/REMOTE_CONTROL_AND_ZERO_TOUCH_COMPARISON.md:304` ("restic/rclone to
> S3 Object-Lock (or B2 immutable) + local snapshots"), matrix rows 28
> (bit-rot, `:156`) + 30 (ransomware, `:158`), dead-man keystone
> (`:27-31`, `:287`).
> **Supersedes** the manual tar recipe in
> `docs/runbooks/lake-backup-restore.md` §2 (which stays as the manual
> fallback); that runbook's restore-ORDER law + consistency argument
> ("the ledger reconciles against the lake — but expect re-pull work
> proportional to the mismatch") stand verbatim and are cross-linked from
> §"Restore" below.
> **Target audience:** the operator (sjparthi) arming the nightly off-box
> backup on the §8 Mac, and any Claude session asked "is the lake backed
> up / how do I restore?".

The system is three one-shot scheduled jobs plus a CLI:

| Job | Schedule | Command | What it does |
|---|---|---|---|
| `com.brutex.backup` | nightly 03:35 IST | `brutex backup run` | preflight → restic backup → verify (structure + rotating read-data subset) → stamp success → retention (`forget`, Sunday `--prune`) |
| `com.brutex.backup-deadman` | hourly | `brutex backup check` | O(1) dead-man freshness check; CRITICAL Telegram on breach; healthchecks ping every tick; daily deep remote cross-check self-gated inside the hourly tick |
| `com.brutex.backup-drill` | monthly (Day 1, 13:00) | `brutex backup restore-drill` | 1 MiB known-bytes canary roundtrip + stratified per-category sample restore + byte verification (audit files: byte-PREFIX law) |

Fire time 03:35 IST: after the 02:30 sweep-daemon default, before the
~06:05 IST token-minter window. Quiescence is NOT guaranteed and is not
required — a backup during a live pull is tagged `pull-active` and tolerated
by design (every lake writer is atomic tmp→fsync→`os.replace`; set-level skew
heals via the ledger-reconcile design of
`docs/runbooks/lake-backup-restore.md` §4).

---

## 1. Install procedure (12 steps — each tagged [AUTO] or [MANUAL-ADR-0017 M#])

1. **[MANUAL M1]** Install restic: `brew install restic` (pin ≥ 0.16).
2. **[MANUAL M1]** **Probe the INSTALLED binary** (closes the external-tool
   fact gate): run `restic version`; confirm against the ADR-0017
   "External-tool facts" citations that the installed version has
   exit-code-3 partial-backup semantics, supports
   `check --read-data-subset=n/t`, and that `unlock` removes only stale
   locks (restic docs, https://restic.readthedocs.io/ — verified
   2026-07-06; re-run this probe step on every restic upgrade).
3. **[MANUAL M2]** Provision the bucket: S3 Object-Lock (compliance mode) or
   Backblaze B2 with object lock enabled; create a scoped key WITHOUT
   delete / bypass-governance permission. Put the S3/B2 credentials into
   `~/.brutex/.env` (0600) — `bootstrap_env()` loads it at CLI dispatch, so
   the launchd jobs inherit them without any secret in a plist.
4. **[MANUAL M3]** Create the repo password:
   `security add-generic-password -s brutex-restic -w` (macOS keychain, then
   set `BRUTEX_BACKUP_PASSWORD_COMMAND="security find-generic-password -s brutex-restic -w"`)
   or a 0600 password file (`BRUTEX_BACKUP_PASSWORD_FILE=...`). EXACTLY ONE
   of the two may be set. Copy the password to an OFF-BOX password manager
   AND a printed/offline copy — **password loss = total loss; restic has no
   recovery** (ADR-0017 M3).
5. **[AUTO]** Add `BRUTEX_BACKUP_REPO=s3:s3.ap-south-1.amazonaws.com/<bucket>/brutex`
   (or the B2 equivalent) to `~/.brutex/.env`; run
   `brutex backup run --dry-run` and review the printed include/exclude
   plan + WOULD-run argv (zero restic calls, zero state writes).
6. **[MANUAL M4]** **First seed:** run `brutex backup run` manually under
   `screen`/`tmux` — an 8 TB lake over WAN takes days (~1 TB/day at
   ~100 Mbit/s sustained — honest envelope). For any seed expected to exceed
   the default, raise `BRUTEX_BACKUP_MAX_RUNTIME_HOURS` (default 48) for the
   seed window — it is BOTH the restic runtime deadline and the RUNNING→HUNG
   bound; the dead-man reads **RUNNING** while the run is alive (no false
   page) and HUNG only past that bound. **Set the raise in `~/.brutex/.env`
   — NOT only the seed shell:** the hourly dead-man checker is a SEPARATE
   launchd process that resolves the SAME bound from its own environment
   (`bootstrap_env()` loads `~/.brutex/.env` at every CLI dispatch; the
   dead-man plist deliberately carries no backup env), so a shell-only
   export on the seed command leaves the checker at 48 h — false HUNG
   CRITICALs from hour 48 of the seed, and a false forgery-divergence page
   after it. **Keep the raised value in place
   until the NEXT nightly backup stamps a fresh success** (then remove it
   from `.env`): the daily deep
   cross-check also uses it as the stamp-PLAUSIBILITY bound (a success stamp
   landing more than one run-duration after the remote snapshot's own start
   time would otherwise
   page as a forgery). A Ctrl-C abort of the foreground seed clears the
   RUNNING marker before propagating, so the dead-man honestly reads
   NEVER_SUCCEEDED/OVERDUE — never a stale quiet RUNNING. The single-flight
   lock means overlapping launchd fire times exit busy (code 5, no alert)
   while the seed runs.
7. **[MANUAL M4]** Fill the `{{PLACEHOLDER}}`s in the three templates under
   `docs/runbooks/backup-templates/` (`com.brutex.backup.plist`,
   `com.brutex.backup-deadman.plist`, `com.brutex.backup-drill.plist` —
   GENERATED from `brutex/backup/schedule_render.py`, golden-pinned
   byte-equal; edit the renderer, never the copies). **Keychain
   (`_COMMAND`) route from step 4:** set the
   `{{BRUTEX_BACKUP_PASSWORD_FILE}}` placeholder in the backup + drill
   plists to an EMPTY string (blank reads as unset) or delete that key —
   any non-blank value (including the literal placeholder) combines with
   the `.env` `_COMMAND` into the ambiguous-source `EXIT_CONFIG` refusal
   every night. **Dead-man plist healthcheck key:** the
   `{{BRUTEX_BACKUP_HEALTHCHECK_URL}}` value does not exist yet (step 10
   creates the healthchecks.io check) — set it to an EMPTY string for now
   (blank reads as unset; any non-URL value, including the literal
   placeholder, is refused with a WARN every hourly tick while
   `backup status` reads SET) and fill it at step 10. Then `cp` them to
   `~/Library/LaunchAgents/`; then
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.brutex.backup*.plist`;
   verify with `launchctl print gui/$(id -u)/com.brutex.backup`.
8. **[MANUAL M5]** Enable Telegram: `BRUTEX_TELEGRAM_ENABLED=true` in
   `~/.brutex/.env`, then `brutex notify test`. Until enabled (the repo
   default is OFF), backup alerts degrade to exit codes + structlog + the
   healthchecks lane, and `brutex backup status` says so ("Telegram
   alerts: DISABLED" — plain text).
9. **[MANUAL — mandatory live NEGATIVE dead-man drill]** Prove the alarm
   path end-to-end before trusting it:

   ```bash
   BRUTEX_BACKUP_DEADMAN_HOURS=0.0001 brutex backup check
   ```

   EXPECT: exit code 1, a `backup.deadman.*` breach event in the log, and
   the CRITICAL "BACKUP DEAD-MAN" Telegram push on your phone. An untested
   recovery path is unverified (blueprint §C.7 step 10,
   `docs/REMOTE_CONTROL_AND_ZERO_TOUCH_COMPARISON.md`). The re-alert
   throttle stamps `deadman.json` only after a SUCCESSFUL send, so the
   drill costs at most one page; the next normal hourly tick reads FRESH
   again with the real threshold.
10. **[MANUAL M6]** Create the healthchecks.io check (period 1 h, grace
    ≥ 2 h) and set `BRUTEX_BACKUP_HEALTHCHECK_URL=<ping url>` **in the
    dead-man plist** (`com.brutex.backup-deadman.plist` — the hourly
    launchd checker reads THAT value: launchd env OVERRIDES
    `~/.brutex/.env` because `bootstrap_env` never overrides an
    already-set var), then re-install the agent
    (`launchctl bootout gui/$(id -u)/com.brutex.backup-deadman` +
    `launchctl bootstrap …`); a `.env`-only URL takes effect only if the
    plist key stays blank/deleted. Watch one
    hourly ping arrive. The checker pings EVERY tick — plain URL when
    FRESH/RUNNING, `<url>/fail` on any breach — so the EXTERNAL service
    alarms when pings STOP: the one lane that catches checker-dead,
    launchd-dead, box-dead and Telegram-dead (the blueprint's missing
    keystone, `:27-31`). **Without M6 a dead box is silent.**
11. **[AUTO]** `brutex backup restore-drill` → expect exit 0 (canary
    verified, zero corrupt); then `brutex backup status --json` → green
    (FRESH, `maintenance_ok: true`, Telegram ENABLED, healthcheck SET).
12. **Reference — restore procedures:** see §4 below (single file /
    directory / full lake), the restore-ORDER law + post-restore mop-up
    (ledger-driven idempotent re-pull + catalog reconcile) kept from
    `docs/runbooks/lake-backup-restore.md` §3-§4; the verify-egress cost
    table (§5); seed-window threshold guidance (step 6); and the
    Object-Lock-vs-prune interplay (`maintenance_ok=false` is EXPECTED
    under Object-Lock retention — repo growth is a cost issue, not a data
    risk; review quarterly, ADR-0017 M8).

## 2. cron fallback (non-macOS boxes / launchd-averse installs)

Emitted by `schedule_render.render_cron_lines()` — same three jobs,
`crontab -e`:

```cron
35 3 * * *  BRUTEX_LAKE_ROOT=/path/lake /path/venv/bin/brutex backup run           >> ~/.brutex/logs/backup.log 2>&1
0  * * * *  /path/venv/bin/brutex backup check                                     >> ~/.brutex/logs/backup.log 2>&1
0 13 1 * *  BRUTEX_LAKE_ROOT=/path/lake /path/venv/bin/brutex backup restore-drill >> ~/.brutex/logs/backup.log 2>&1
```

Note cron does not load `~/.brutex/.env` itself — `bootstrap_env()` does at
CLI dispatch, so the lines need only the lake root. Tighter RPO = edit the
first line's schedule (the RPO IS the schedule interval — honest envelope
§6).

## 3. Operating: exit codes, alerts, status

- `brutex backup status [--json]` — O(1), zero restic calls: prints
  `state.json` + `deadman.json` + resolved config (repo URL redacted to
  scheme+host; the password VALUE is never read), plus "Telegram alerts:
  ENABLED/DISABLED" and "healthcheck ping: SET/UNSET". Exit mirrors the
  shallow dead-man verdict (0/1) with NO alert/ping/sidecar side effects.
- `brutex backup check [--deep] [--dry-run]` — the dead-man tick. `--deep`
  forces the remote cross-check (`restic snapshots --no-lock --json
  --latest 1 --tag brutex` compared against the local stamp) this tick;
  `--no-lock` is load-bearing (round 6) — the read-only metadata call must
  never collide with the nightly `forget`/Sunday `--prune` (or drill
  canary forget) EXCLUSIVE repo locks, which would read as a false
  `state_remote_divergence` CRITICAL + `/fail` pings every tick until the
  prune ended (verified on restic 0.16.4: `snapshots --no-lock` succeeds
  under a held exclusive lock). THREE tiers: (1) a
  stamp-PLAUSIBILITY bound is screened FIRST — a `last_success_at` landing
  more than `BRUTEX_BACKUP_MAX_RUNTIME_HOURS` after the REMOTE latest
  snapshot's own time is CRITICAL divergence. The anchor is the remote time
  ALONE — the one stamp no on-box `state.json` write can move — so a stamp
  forgery that keeps the real snapshot id is caught WHEN it moves the
  success stamp more than `max_runtime_hours` past the remote snapshot's
  own time (bump-only OR success+attempt bumped together; anchoring on the
  local attempt stamp would let the two-field bump read as agreement) —
  worst-case detection latency ≈ `max_runtime_hours` + 24 h under
  defaults; a SUB-bound inflation is inherently indistinguishable from a
  slow legit run and stays bounded only by the shallow 30 h threshold, and
  the deep tier is FRESH-gated so a forged `running_since` bypasses it
  entirely — honest residuals, §6 item 8
  (keep a seed-window `MAX_RUNTIME` raise in place — in `~/.brutex/.env` so
  the hourly checker resolves the same bound — until the next nightly
  success restamps, step 6); (2) agreement = the remote latest snapshot id
  matching the stamped `last_snapshot_id`; (3) the time-gap fallback
  (id-less state) is judged against the NEARER of the attempt/success
  stamps — restic stamps a snapshot's time at backup START, so a run longer
  than the dead-man threshold (the multi-day seed) never reads as
  divergence.
- Exit codes (from `brutex/backup/errors.py`): 0 OK · 1 dead-man breach ·
  2 config · 3 preflight (unmounted/phantom/empty lake) · 4 restic/state
  failure · 5 busy (run-vs-run overlap benign, no alert; a busy-skipped
  DRILL emits one WARN — round 6) · 6 corruption
  (verify/drill) · 7 maintenance failed (data-success stamp stands).
- A DIVERGED deep check advances its daily gate ONLY when the CRITICAL was
  actually delivered — an undelivered divergence (Telegram outage/OFF)
  retries the bounded metadata call every hourly tick (exit 1 + `/fail`
  ping each tick) instead of going quiet for 24 h.
- The monthly drill takes the SAME single-flight lock as the nightly run:
  if the nightly run still holds it at the drill's fire time, the drill
  exits busy (5) rather than colliding on the restic repo lock (which
  would read as a false `drill_restic` failure or a false corruption
  page). Round 6: the default fire time is Day-1 **13:00 IST** —
  deliberately OUTSIDE the nightly run's window (by the §5 math the 03:35
  run's default `auto` subset read ≈ 3.2 h at the step-6 ~100 Mbit/s
  envelope holds the lock to ~07:00+, so the previous 04:30 default made
  the busy skip the deterministic monthly NORM, and launchd one-shots
  never retry) — and a busy-skipped drill now emits ONE WARN
  `backup_degraded(aspect="drill_skipped")` (a skipped drill is a month of
  LOST verification, unlike the benign run-vs-run overlap, which stays
  no-alert): re-run `brutex backup restore-drill` manually; a recurring
  skip means the fire time needs moving again.
- Every failure maps to a deterministic triple (exit code + `backup.*`
  structlog event + alert-or-explicit-no-alert) — the full 23-row
  failure-mode matrix lives in the DEC-BACKUP-OFFBOX-001 PR body and
  MIGRATION.md #1527.
- The nightly run REFUSES an unmounted `/Volumes/<name>` phantom dir and an
  empty backup set BEFORE any restic verb — critically before
  `forget --prune`, the one path by which a backup system could age real
  snapshots out against a skeleton lake. A genuinely brand-new box seeds
  once with `brutex backup run --allow-empty` (loud, logged).
- Partial backups (restic exit 3 — some source files unreadable, normal on
  a LIVE lake) still stamp success; a streak ≥ 3 escalates CRITICAL.

## 4. Restore

Single file / directory:

```bash
restic -r "$BRUTEX_BACKUP_REPO" snapshots --tag brutex          # pick <id>
restic -r "$BRUTEX_BACKUP_REPO" restore <id> --target /tmp/r --include '/path/inside/lake'
```

Full lake: `restic restore <id> --target <new_lake_root_parent>` then
follow the restore-ORDER law of `docs/runbooks/lake-backup-restore.md` §3
VERBATIM: stop the dashboard + every pull first; restore `bars/`, the three
`*.duckdb` masters, `sweeps/`, `audit/`; restore
`window_ledger/ledger.parquet` LAST; restart the dashboard. Post-restore
mop-up: the DuckDB masters may have been snapshotted torn during a live
pull — they are REBUILDABLE (instrument re-load / registry metadata re-pull
/ catalog reconcile); the ledger reconciles against the lake — expect
re-pull work proportional to the mismatch (idempotent keep-first sink), per
`docs/runbooks/lake-backup-restore.md` §4.

Repo-corruption remediation (`backup_repo_corrupt` alert): human-only —
restore from an OLDER snapshot or re-seed; NEVER run automated "repair" (an
automated repair of a corrupt repo can destroy the last good copy) —
ADR-0017.

## 5. Verify-egress cost table (the `BRUTEX_BACKUP_VERIFY_SUBSET` trade)

The nightly `restic check` verifies repo STRUCTURE (metadata) every run;
the read-data tier is the knob:

| `BRUTEX_BACKUP_VERIFY_SUBSET` | Nightly read | Full-repo cycle | Egress on ~8 TB |
|---|---|---|---|
| `auto` (default) | rotating `k/56` pack bucket (state cursor; advances only on a PASSING check — no bucket ever skipped) | every 56 successful nights ≈ 8 weeks | S3: ~8 TB/56 ≈ **143 GB/night** (billed egress); B2's free-egress tier changes the math (pricing: https://www.backblaze.com/cloud-storage/pricing — verified 2026-07-06) |
| `none` | structure only | never (bit-rot detection OFF) | ~0 |
| passthrough (e.g. `2%`, `1/10`) | verbatim to restic | your arithmetic | proportional |

## 6. Honest envelope (Golden Rule #10 — what this does NOT guarantee)

1. **A dead box is silent on-box** — every alert pushes FROM the Mac; true
   box-death detection REQUIRES the M6 external healthchecks watcher.
2. **RPO = the schedule interval** (nightly ⇒ up to ~24 h lost). Bars are
   re-pullable from Groww within vendor retention (CLAUDE.md §6 historical
   depth row); **`sweeps/` + the hash-chained audit logs are NOT
   re-pullable — their loss window IS the RPO.**
3. **No cross-file transactional snapshot** — file-level consistency only
   (atomic writers); DuckDB masters may snapshot torn mid-pull
   (rebuildable); set-level skew heals via ledger reconcile.
4. **Subset verification is sampling** — bit-rot at the remote can live up
   to ~8 weeks before the rotating read-data cycle touches its pack; the
   drill samples N files (default 16) + a canary, not 8 TB; the quarterly
   FULL restore rehearsal is manual (ADR-0017 M7).
5. **Immutability is provisioned remote-side** (M2) — a stolen key with
   delete rights on a NON-locked bucket can still destroy history.
6. **Password loss = total loss** (M3).
7. Secrets never leave the box: the password exists only as a FILE path or
   COMMAND in the child env; any inherited `RESTIC_PASSWORD` is stripped;
   argv is secret-free; `~/.brutex/cache/`, `~/.brutex/.env`, `quota/`,
   `runtime.json` and every credential path are NOT in the backup set
   (creds are AWS-SSM-only, DEC-AWS-ONLY-CREDS-001).
8. **The deep forgery tier has honest bounds (round 6).** (a) It catches a
   stamp forgery that keeps the real snapshot id ONLY when the forged
   `last_success_at` lands more than `BRUTEX_BACKUP_MAX_RUNTIME_HOURS`
   past the remote latest snapshot's own time — a SUB-bound inflation
   (≤ 48 h under defaults) is inherently indistinguishable from a slow
   legit run (worst-case total quiet ≈ the shallow 30 h threshold ON TOP
   of the inflated stamp); a persistent re-bump is detected 48–72 h after
   the last real snapshot (bound + 24 h deep cadence), not "~24 h". (b)
   The deep tier runs only on a FRESH shallow verdict: a forgery that
   plants/re-bumps `running_since` bypasses it entirely — a ONE-TIME
   `running_since` poison is caught by the HUNG bound at
   `max_runtime_hours` (48 h default, via HUNG — not the forgery
   mechanism), and a PERSISTENT `running_since` re-bump (any cadence
   < 48 h) is an acknowledged residual: it needs on-box write access to
   `state.json`, at which point the box is already hostile territory.

## Decision trail

- DEC-BACKUP-OFFBOX-001 (MIGRATION.md #1527) — this system.
- ADR-0017 (`docs/adr/0017-offbox-backup-restic.md`) — the
  manual-acceptance register M1-M8 + external-tool facts.
- `docs/runbooks/lake-backup-restore.md` — the manual tar fallback + the
  restore-ORDER law + consistency argument this runbook inherits.
- DEC-LAKE-HEAL-001 (#1175) — why `.quarantine/` is INCLUDED in the backup
  set (post-heal it is the ONLY record of removed poison rows).
- DEC-ATOMIC-WRITE-CONSOLIDATE-001 (#890) — why the state files ride
  `atomic_write_simple` (a state ENOSPC must never mis-escalate to
  `LakeWriteHalt`).
