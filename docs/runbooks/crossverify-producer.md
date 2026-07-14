# Cross-verify producer — operator runbook

> DEC-CROSSVERIFY-PRODUCER-001 (MIGRATION.md #1776) ·
> DEC-CROSSVERIFY-S3-WRITE-POSTURE-001 (#1777) ·
> DEC-CROSSVERIFY-DAY-DERIVE-001 (#1778) ·
> **DEC-CROSSVERIFY-SPOT-ONLY-001 (#1788 — the operator contract amendment:
> SPOT ONLY, whole spot universe)**
>
> The daily TickVault cross-verification producer: post-close priority pull
> (the WHOLE spot universe — ALL indices (~25, incl. INDIAVIX/sectorals) +
> ALL stocks (~751, F&O-eligible AND reference-only NTM names); futures AND
> options EXCLUDED — TickVault must NOT expect futures files, their absence
> is BY DESIGN) → completeness gate → day-scoped prebuild → pinned
> per-symbol CSV export → idempotent S3 publish to
> `s3://tv-prod-cold/crossverify/groww/<day>/<segment>/<symbol>.csv`
> (ap-south-1). Correct-or-absent, never wrong bytes: an incomplete file is
> WITHHELD (Telegram CRITICAL), never published short. The CSV schema is
> UNCHANGED — the header keeps its `oi` column; every spot row renders the
> empty `oi` cell (the pinned byte contract).

---

## 0. One-time install checklist (in order)

1. **IAM grant (BruteX's FIRST AWS write surface — #1777).** Attach this
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

3. **Run the 5-day post-close settle probe** (sibling #1775,
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

   > **HARD GATE — do NOT install until the completeness-gate PR (#1774,
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
`publish_failed` exit 2) → **withheld-day AUTO-RE-GATE**
(DEC-CROSSVERIFY-WITHHELD-REGATE-001; also before the session gate): every
prior day inside the catchup window whose journal still names WITHHELD
symbols has exactly those symbols re-gated from the (since-healed) lake —
re-export → stage → publish the now-complete ones, journal `withheld` map
rewritten to the honest remainder; already-published keys are NEVER re-put
(journal idempotency), the gate binds fail-closed first (gate absent ⇒ the
re-gate skips with one WARN), a transient store fault in the re-gate's own
target enumeration skips the scan FAIL-OPEN with one
`crossverify.regate_scan_failed` WARN (the main day never sinks; the next
run re-finds the withheld maps), and each re-gated symbol pays one
month-partition read (O(one month of 1-minute bars) per symbol·day).
Attribution honesty: a prior PENDING key published by the re-gate pass
rather than the recovery scan counts as `regated`, not `recovered_prior` —
attribution only, both roads end published →
session gate (non-trading + **Muhurat days skip
entirely — no files, both sides no-op**) → fail-closed completeness-gate
presence → bounded pull-lock wait (45 min default; a peer pull holding the
locks past the budget DEFERS the day with a CRITICAL `crossverify_refused`
— no preemption in v1) → priority pull (the WHOLE spot universe to TODAY —
the ONE spot leg; no metadata leg, no futures leg, zero F&O vendor calls —
#1788) → per-symbol completeness gate + export decisions (a short/withheld
index gets ONE bounded settle-retry; per-index tier law below) → day-scoped
derive (#1778; never blocks the export) → stage
+ journal → **locks released** → publish frozen staged bytes → exactly ONE
Telegram summary.

Gate-class table at ~776 spot symbols (what ships vs what withholds —
the APPROVED index-policy ruling, #1788 fix round):

| Class | Publishes when | Withholds when | Zero-bar day |
|---|---|---|---|
| ENGINE-TRIO INDEX (`index_spot`, exactly NIFTY/BANKNIFTY/SENSEX — `day_export.STRICT_INDEX_COORDS`, the brute-force engine surface) | STRICT: distinct minutes == the session's expected count AND the last bar is the close edge | `count_short`/`count_excess`/`last_bar_incomplete` → withheld exit 2 (ONE settle-retry first) | the gate's count branch reads it `count_short` (0 < expected) → **withheld exit 2, CRITICAL** (a trio index always prints — absence is a pull failure) |
| OBSERVE INDEX (`index_spot`, the other ~22 incl. INDIAVIX + sectorals — print behavior UNVERIFIED, CLAUDE.md §6) | bars exist AND the last bar is the close edge — a short/excess COUNT still publishes, COUNTED (`crossverify.observe_count_anomaly` INFO + the summary's `observe-published` counter), never a count-withhold | a FORMING last bar → withheld `last_bar_incomplete` (genuinely unsettled data; settle-retried) | honest `nothing` — never a CRITICAL |
| CASH (`cash_spot`, ALL ~751 stocks) | any bars whose volumes are not ALL zero — **NO minute-count demand** (the #1031 illiquid lesson: sparsity ≠ incompleteness) | ALL-zero-volume shape → withheld (`volume_all_zero`, exit 2) | honest `nothing` (exit unaffected); a legitimately thin/sparse day SHIPS with exactly its stored minutes |

Promotion path: an observe index graduates to the strict tier via a small
follow-up DEC carrying probe evidence of its full-session print behavior
(`DEC-CROSSVERIFY-INDEX-STRICT-PROMOTE-001`, named in MIGRATION.md #1788).
The `blocked` verdict survives only as a DEFENSIVE-TOTALITY arm on the trio
(production-dead: the real gate's index count branch runs first, so
`no_today_bars` is never emitted for an index — F2 of the #1788 fix round).

HONEST LIMIT (named, not hidden): the gate cannot distinguish a genuinely
settled all-zero-volume CASH day from a still-settling one — such a day
(if the vendor ever serves one) is withheld, never published. The per-day
counts ARE operator-visible three ways: the ONE Telegram summary carries
`strict-published N · observe-published N · withheld N · blocked N ·
nothing N` verbatim (pinned in
`tests/unit/test_notifications_sweep_alerts_crossverify.py` — nothing ships
invisibly), and the journal's `withheld` map names each symbol + reason.

Exit codes (cron-red discipline): `0` published / staged-only / honest
skips / disabled / market-hours deferral · `1` junk config / lock refusal /
gate module absent · `2` halts / blocked index / withheld-final / publish
failed / 4 h deadline / calendar abstain.

The dashboard twin: the **Cross-verify → TickVault** button on `/universe`
(same runner, dedicated progress feed at `/api/v1/crossverify/progress`).
GREEKS N/A for this trigger — the Greeks stage is options-only and this
pull excludes ALL F&O (futures AND options — #1788); nothing to enrich.

## 2. Recovery playbook

| Symptom | What happened | Action |
|---|---|---|
| `publish_failed` exit 2, keys named | S3 throttle exhausted retries, or the grant is broken (`aborted_nonretryable`) | Fix the grant/backend; the NEXT run's recovery scan re-publishes the FROZEN staged bytes (no re-render, no re-gate) — or re-run manually |
| `refused … locks still held` | A pull/backfill held the lake locks past the 45-min wait | Nothing lost — the day deferred; re-run after the peer finishes (`brutex crossverify run --day YYYY-MM-DD`) |
| `withheld_partial` exit 2 | The gate withheld incomplete files (short/zero-bar TRIO index — the gate reads a zero-bar index `count_short` / forming last bar on ANY index / all-zero-volume stock / render refusal) | TickVault sees honest absence. Once the lake heals (the nightly re-pull), the NEXT daily run within `BRUTEX_CROSSVERIFY_CATCHUP_DAYS` (default 5) AUTO-RE-GATES + publishes the day — zero manual steps (DEC-CROSSVERIFY-WITHHELD-REGATE-001, which closes the follow-up #1776 named DEC-CROSSVERIFY-BACKFILL-001). Only an OLDER-than-catchup day, or a day whose symbol read honest `nothing` (zero bars — never in the withheld map), still needs the manual `--day YYYY-MM-DD` re-run |
| `blocked` exit 2 | Production-dead defensive arm (a zero-bar trio index reads `withheld_partial` via `count_short`, never this) — fires only if a FUTURE gate emitted `no_today_bars` for a trio index | Investigate the gate/pull; re-run the day once healed |
| `refused … gate module absent` | The sibling completeness gate (#1774) is not merged/installed | The producer is FAIL-CLOSED by design — never publishes ungated |
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
- Last-run surface: the runner persists `<lake>/.crossverify/last_run.json`
  at every MATERIAL run end (summary verdicts + refusals; never dry-runs).
  The honest skips follow the material-actions-only law: a TRUE no-op skip
  writes nothing and emits no summary — a weekend skip cannot clobber
  Friday's failure record — but a skip-day run that re-gated or recovered
  prior days' keys (real S3 objects shipped) DOES write the record AND emit
  the one Telegram summary (WARN, the catch-up tier): nothing ships
  invisibly, and the record it may overwrite was resolved by that very
  re-gate. `GET /api/v1/crossverify/progress` serves it as the additive
  `last_run` field and the `/universe` panel renders it on page load
  (✓/✕ + day + verdict + publish-failed count + capped failing keys +
  detail), so a finished failure survives page reloads AND dashboard
  restarts. The per-day journal stays the authoritative record — this file
  is a view convenience (corrupt/absent ⇒ `null`, never load-bearing).
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
