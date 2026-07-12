# Cross-verify producer — operator runbook

> DEC-CROSSVERIFY-PRODUCER-001 (MIGRATION.md #1757) ·
> DEC-CROSSVERIFY-S3-WRITE-POSTURE-001 (#1758) ·
> DEC-CROSSVERIFY-DAY-DERIVE-001 (#1759)
>
> The daily TickVault cross-verification producer: post-close priority pull
> (ALL spot + NIFTY/BANKNIFTY futures — options EXCLUDED) → completeness
> gate → day-scoped prebuild → pinned per-symbol CSV export → idempotent S3
> publish to `s3://tv-prod-cold/crossverify/groww/<day>/<segment>/<symbol>.csv`
> (ap-south-1). Correct-or-absent, never wrong bytes: an incomplete file is
> WITHHELD (Telegram CRITICAL), never published short.

---

## 0. One-time install checklist (in order)

1. **IAM grant (BruteX's FIRST AWS write surface — #1758).** Attach this
   policy to the producer box's AWS principal (the boto3 default credential
   chain resolves it — env / shared credentials file / instance profile).
   `s3:PutObject` on the `crossverify/*` prefix ONLY — NO Get/Head/List/
   Delete (idempotency is journal-side by design):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "BrutexCrossverifyPutOnly",
         "Effect": "Allow",
         "Action": "s3:PutObject",
         "Resource": "arn:aws:s3:::tv-prod-cold/crossverify/*"
       }
     ]
   }
   ```

   Until the grant lands the producer aborts LOUDLY (`AccessDenied` →
   whole-publish abort, `publish_failed` exit 2, CRITICAL Telegram naming
   this runbook) — staged bytes survive for the next run's recovery scan.

2. **boto3 (the ONE new runtime dep — optional extra, never in CI):**

   ```bash
   pip install -e ".[crossverify]"
   ```

   Missing boto3 → `S3BackendUnavailable` naming this exact command.

3. **Run the 5-day post-close settle probe** (sibling #1756,
   `docs/groww/probe/postclose_settle_probe.py`) and choose the fire time:
   the earliest time the probe shows 5/5 settled days, **plus a 15-minute
   margin**. NO default time ships anywhere (Golden Rule #1) — vendor
   same-day availability is UNVERIFIED until the probe says otherwise; a
   too-early run WITHHOLDS (settle-retry belt + the withhold law), never
   publishes wrong bytes.

4. **Bring-up rehearsal (no S3):**

   ```bash
   brutex crossverify run --lake-root /data/lake --dry-run    # render only — no locks/pulls/writes
   brutex crossverify run --lake-root /data/lake --no-publish # full chain, staged only
   ```

5. **Install the LaunchAgent (macOS; `--at` REQUIRED):**

   > **HARD GATE — do NOT install until the completeness-gate PR (#1755,
   > `brutex/data/today_completeness.py`) has landed.** The producer is
   > FAIL-CLOSED without it: every scheduled fire exits 1 `refused … gate
   > module absent` and pages CRITICAL — a paging loop, not a producer.
   >
   > **`--at` must be POST-CLOSE** (after 15:30 IST, probe-decided per
   > step 3). A PRE-OPEN (<08:45 IST) fire resolves the NOT-YET-TRADED
   > day: it pulls, the gate withholds everything →
   > `withheld_partial`/`blocked` exit 2 → CRITICAL on every fire. A
   > MID-DAY (08:45–15:35 IST) fire never reaches the vendor: the step-E
   > market-hours gate defers — `deferred_market_hours` exit 0, NO alert,
   > NO journal — a SILENT mis-schedule, backstopped only by the
   > trading-day-aware journal-mtime deadman (`brutex crossverify status`
   > reads MISSED once past grace).

   ```bash
   brutex crossverify install --at HH:MM --lake-root /data/lake
   ```

   The install VERIFIES `launchctl list com.brutex.crossverify` —
   installed-but-unlisted FAILS loudly (the TickVault lesson). It also
   prints the one-time `sudo pmset repeat wakeorpoweron …` scheduled-wake
   command — launchd cannot wake a sleeping Mac; run it yourself (BruteX
   never runs pmset). Linux: cron guidance is printed instead
   (`brutex crossverify run` at the chosen time).

6. **First live run + acceptance:** after the first scheduled fire, verify
   one object end-to-end (operator acceptance — CI never touches real S3):

   ```bash
   aws s3api head-object --bucket tv-prod-cold \
     --key "crossverify/groww/$(date +%F)/IDX_I/NIFTY.csv"   # needs YOUR creds; the producer itself cannot Head
   brutex crossverify status --lake-root /data/lake
   ```

---

## 1. The daily run (zero-touch)

`brutex crossverify run` executes: kill switch → prior-day pending-publish
recovery scan (BEFORE the session gate, deliberately: a weekend/holiday run
still republishes a prior trading day's pending staged keys — recovery is
lock-free and vendor-free; a fully-recovered day's staging dir is cleaned,
a still-failing day's is kept and the skip verdict upgrades honestly to
`publish_failed` exit 2) → session gate (non-trading + **Muhurat days skip
entirely — no files, both sides no-op**) → fail-closed completeness-gate
presence → bounded pull-lock wait (45 min default; a peer pull holding the
locks past the budget DEFERS the day with a CRITICAL `crossverify_refused`
— no preemption in v1) → priority pull (spot whole-universe to TODAY,
scoped metadata, `expiry>=day` NIFTY/BANKNIFTY futures) → per-symbol
completeness gate + export decisions (INDEX missing its day = BLOCKED exit
2; a settling future / short index gets ONE bounded settle-retry) →
day-scoped derive (#1759; never blocks the export) → stage + journal →
**locks released** → publish frozen staged bytes → exactly ONE Telegram
summary.

Exit codes (cron-red discipline): `0` published / staged-only / honest
skips / disabled / market-hours deferral · `1` junk config / lock refusal /
gate module absent · `2` halts / blocked index / withheld-final / publish
failed / 4 h deadline / calendar abstain.

The dashboard twin: the **Cross-verify → TickVault** button on `/universe`
(same runner, dedicated progress feed at `/api/v1/crossverify/progress`).
GREEKS N/A for this trigger — the Greeks stage is options-only and this
pull excludes options; nothing to enrich.

## 2. Recovery playbook

| Symptom | What happened | Action |
|---|---|---|
| `publish_failed` exit 2, keys named | S3 throttle exhausted retries, or the grant is broken (`aborted_nonretryable`) | Fix the grant/backend; the NEXT run's recovery scan re-publishes the FROZEN staged bytes (no re-render, no re-gate) — or re-run manually |
| `refused … locks still held` | A pull/backfill held the lake locks past the 45-min wait | Nothing lost — the day deferred; re-run after the peer finishes (`brutex crossverify run --day YYYY-MM-DD`) |
| `withheld_partial` exit 2 | The gate withheld incomplete files (settling OI / short index / render refusal) | TickVault sees honest absence. Re-pull later and re-run `--day YYYY-MM-DD` (manual backfill — auto re-gate is the named follow-up DEC-CROSSVERIFY-BACKFILL-001) |
| `blocked` exit 2 | A brute-force INDEX has NO bars for the day — a pull failure | Investigate the pull (vendor outage / halt); re-run the day once healed |
| `refused … gate module absent` | The sibling completeness gate (#1755) is not merged/installed | The producer is FAIL-CLOSED by design — never publishes ungated |
| Corrupt journal WARN | `.crossverify/<day>.json` unreadable (junk JSON, invalid UTF-8, a directory) | Self-heals: the run re-renders + re-puts identical bytes (convergent); a corrupt PRIOR-day journal never blocks later runs |
| `crossverify.staging_orphaned` WARN | A staging dir exists for a day whose journal is wiped/corrupt — invisible to the pending scan | Bytes are left in place; `brutex crossverify run --day YYYY-MM-DD` re-runs that day idempotently |
| `crossverify.staged_bytes_corrupt` WARN | A staged file's bytes no longer match the journal's recorded sha256/size (bit rot / tamper) | Never published; stays pending. A same-day re-run re-renders + heals; a prior day needs `--day YYYY-MM-DD` |
| `refused … concurrent same-day run` | A peer producer run holds the day's journal lock past the bound | Nothing lost — let the peer finish; re-run if it died (the flock dies with its process) |
| dead-man `MISSED` on `status` | The Mac slept through the fire | Run the printed `pmset` scheduled wake; re-run the missed day manually |

## 3. Ops surface

- Config: the `BRUTEX_CROSSVERIFY_*` env surface — `docs/runbooks/ENV_VARS.md` §8.
- Kill switch: `BRUTEX_CROSSVERIFY=0` (a scheduled fire exits 0 `disabled`);
  `brutex crossverify uninstall` removes the LaunchAgent. No lake/schema
  state to unwind — the `.crossverify/` journal + staging sidecars are
  inert, S3 objects stay as-is.
- Status + dead-man: `brutex crossverify status` (plist presence, launchctl
  listing, newest journal, trading-day-aware dead-man verdict; MISSED fires
  a best-effort CRITICAL push). Honest limit: a dead box is silent on-box
  (the #1596 posture). A `nothing_to_do` day DOES write its (empty-entries)
  journal, so the dead-man reads OK for it; the journal-less exits are the
  pre-staging ones — lock refusal / gate absent / pull halt each fired
  their own CRITICAL/refusal alert, but a MID-DAY (08:45–15:35 IST)
  vendor-calling fire's `deferred_market_hours` exit 0 is SILENT (no
  alert, no journal) — for that mis-schedule this dead-man is the ONLY
  signal.
- Locks: the producer holds the pull account→lake→lease trio ONLY through
  staging; the S3 publish (and the recovery scan) never hold the PULL
  locks, so the 01:30 nightly can never meet a producer-held pull flock
  during publish. Per-DAY journal+staging+publish sections additionally
  serialise on a day-journal flock (`<lake>/.locks/…`, the COMMON
  partition-lock sidecar) so two concurrent runs converge instead of racing
  the staging cleanup — that flock is a leaf, never held while acquiring
  the pull locks.

## 4. Honest limits

- **The 4 h hard deadline is checked at STEP BOUNDARIES only** — a
  pathologically long pull leg can hold the flocks past the wall until its
  own step completes (in practice bounded by the 08:45 market-hours halt,
  which stops vendor pulls mid-run; a nightly that then fires at 01:30
  against still-held locks refuses LOUDLY after its own wait budget — never
  silently, never corrupting).
- **Box-local time is assumed IST** (the nightly/launchd precedent — NSE
  market hours, the deadman clock and the `--at` fire time all read the
  box clock; a non-IST box needs its system timezone set to Asia/Kolkata).
- **IAM — not the code — is the write-scope guardrail:** the
  `BRUTEX_CROSSVERIFY_S3_BUCKET`/`_S3_PREFIX` env vars are bring-up
  configuration, not a security boundary; the PutObject-only grant on
  `arn:aws:s3:::tv-prod-cold/crossverify/*` is what actually bounds where
  the producer can write.
- **A legacy out-of-session bar in an un-healed month partition withholds
  that symbol's file** (`render_refused:` — the renderer refuses any row
  outside the session window; correct-or-absent, never a dropped row).
  Remediation: run the lake heal / re-pull the affected month, then re-run
  `--day YYYY-MM-DD`.
