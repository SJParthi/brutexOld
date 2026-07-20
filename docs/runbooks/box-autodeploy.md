# Runbook — the nightly box auto-deploy leg (`brutex box-update`)

> DEC-BOX-AUTODEPLOY-LANE-001 (MIGRATION.md #1939). The nightly chain's
> PRE-CHAIN deploy leg: fast-forward the operator box's checkout to
> `origin/main` under FAIL-SAFE guards, rebuild the maturin wheels when the
> delta touched Rust/build config, assert the doctor wheel-freshness law
> GREEN in a fresh scrubbed subprocess, then re-exec ONCE so the whole night
> runs the deployed code. The leg is FAIL-OPEN outside the chain break law —
> it NEVER blocks the nightly (a GitHub outage means one more night on old
> code, never a missed pull). That law reaches CODING BUGS too: a defect in
> the leg itself (or a raising `os.execv`) folds into ONE structlog ERROR
> `box_update.leg_error` + a null `box_update` block, and the chain proceeds
> on the existing checkout (§25 deviation 12).

## 1. What runs, when

- **Automatic:** every `brutex nightly run` executes the leg PRE-CHAIN (before
  the six R42 links), under the SAME `nightly_singleflight_lock`, on the
  existing 01:30 launchd schedule. **No new launchd agent** — the plist argv
  stays the plain `brutex nightly run` (if a dedicated agent is ever adopted
  its install must delegate to `schedule.install` INCLUDING the mandatory
  `launchctl list` verification — the #1612 verified-install law).
- **Manual:** `brutex box-update [--lake-root PATH]` runs the leg once and
  prints the outcome JSON. Exit law: `0` on updated/up_to_date/disabled
  (an honored kill switch is not an error), `1` on a guard skip / junk
  kill-switch value / flock contention (loud, zero git commands run), `3` on
  rolled_back/error.
- **Dry-run:** `brutex nightly run --dry-run` SKIPS the leg entirely (the
  zero-mutation contract) — the report's `box_update` block is honestly null.

## 2. Env knobs

| Var | Law | Default |
|---|---|---|
| `BRUTEX_BOX_UPDATE` | the ONE fail-loud knob: unset/`1/true/yes/on` = enabled; `0/false/no/off` = disabled (honest `status=disabled` block still written); anything else = clean `EXIT_USER_ERROR` before any git command | enabled |
| `BRUTEX_BOX_UPDATE_FETCH_TIMEOUT_S` | FAIL-SAFE parse (junk → default, never blocks) | 60 (×2 attempts) |
| `BRUTEX_BOX_UPDATE_BUILD_TIMEOUT_S` | FAIL-SAFE, per crate | 2700 |
| `BRUTEX_BOX_UPDATE_PIP_TIMEOUT_S` | FAIL-SAFE; ONE knob for BOTH pip mouths (forward refresh + post-rollback restore) | 600 |
| `BRUTEX_BOX_UPDATE_STATE` | INTERNAL — the re-exec loop-proof sentinel; process-env-only, never persist it anywhere | — |

Network envelope (coordinator-approved, verbatim): **no network beyond the
git remote + the canonical wheel-build fetches** (`git fetch origin main`,
`pip install -e ".[data,ui,groww]"` on a pyproject delta, cargo's registry
fetches during the bare maturin builds).

## 3. Verify — what a healthy morning looks like

- The nightly `report.json` carries a top-level `box_update` block every
  night: `status` ∈ `updated | up_to_date | skipped | rolled_back | error |
  disabled`, `running_sha` (the SHA the night actually executed), per-wheel
  `result`/`digest_ok`, `gate` (`"skipped"` ⇔ the gate subprocess never ran),
  `repair`, `pip_restore`, `lock_restored`/`quarantined`,
  `alarm_signature`/`alarm_repeats`.
- The Telegram summary carries one `📦 box-update: …` line for every
  non-quiet status (silent on up_to_date/disabled — the block still records).
- An `updated` night re-sweeps EVERYTHING (`code_commit_sha` is in the sweep
  run identity — the documented #1612 re-key). `skipped_cache == 0` the
  morning after an upstream-change night is EXPECTED, not a regression.
- The `env_preflight` block lists any retired env flag still set in the
  process env or a dotenv file (e.g. `BRUTEX_FILL_MODE`), each with its
  remediation text — unset the variable / delete the dotenv line.

## 4. Failure states + remediation

| Block reads | Meaning | Remediation |
|---|---|---|
| `status=skipped`, `skip_reason=dirty_tree` | tracked local changes on the box checkout (incl. an operator-edited `Cargo.lock` — NEVER clobbered) | commit/stash/revert the local change; the leg resumes next night |
| `skip_reason=diverged` | local commits on `main` | rebase/remove them (the leg never merges/rebases for you) |
| `skip_reason=fetch_failed` | network outage (FAIL-OPEN, ≤ ~125 s spent) | nothing — self-heals when the network returns |
| `status=rolled_back`, `repair=not_needed/rebuilt` | the new tree failed the gate (or a required build failed); the box rolled back and is COHERENT on old code | nothing urgent — fix the upstream breakage; the leg retries next night |
| `repair=failed_stale` (WARN) | the torn-version residual: tree=from_sha but a required wheel is still to_sha-built (`digest_ok=false`) | usually self-heals next night; manual: the wedge-rescue command below, per stale crate (the bare `maturin develop --release` needs the venv maturin + cargo PATH — a fresh shell hits maturin-not-found) |
| `pip_restore=failed` (WARN) | deps may sit at to_sha against the from_sha tree | `pip install -e ".[data,ui,groww]"` from the repo root |
| `status=error`, `repair=failed` (CRITICAL) | HARD CLI-DOWN: a required wheel is unloadable and the repair failed — the NEXT invocation cannot import | the wedge-rescue command below, NOW |
| `status=error`, `skip_reason=rollback_failed` (CRITICAL) | `checkout -B` refused (a concurrent writer dirtied a tracked file mid-run) | inspect `git status`; restore the file; `git checkout -B main <from_sha>` manually |
| nightly reports STOP arriving at all | the §14 WEDGE shape — a process death inside a `trading_core` install window (or a breaking dep skew) crashes `python -m brutex` PRE-CLI: NO lane alarm can fire | the wedge-rescue command below; watch the #1747 health hub's nightly lane for stale/overdue |

**The manual wedge-rescue command** (the DEC-BOX-WEDGE-RESCUE-001 follow-up
will mechanise this; until then it is the operator's job whenever the CLI
itself will not start):

```bash
cd <repo> && cd rust/trading-core && \
  PATH="$HOME/.cargo/bin:$PATH" VIRTUAL_ENV="<venv>" <venv>/bin/maturin develop --release
# repeat for rust/fno-math if `python -c "import fno_math"` still fails;
# a broken dep set instead needs: <venv>/bin/pip install -e ".[data,ui,groww]"
```

**Quarantine recovery** — every lane lock-restore preserves the pre-restore
bytes FIRST: if the lane restored a `rust/*/Cargo.lock` you had deliberately
edited (only possible inside the one-night crash-marker window), the original
bytes are at `<lake_root>/.box_update/quarantine/<UTC-ts>/rust__<crate>__Cargo.lock`
(newest 10 run dirs retained) — copy the file back. A `dirty_tree` stall after
a crash whose marker write had FAILED (one `build_marker_write_failed` WARN
that night) is cleared with one manual
`git checkout -f -- rust/<crate>/Cargo.lock`.

## 5. Honest limits

- Worst-case leg wall time ≈ **4.3 h** (the mixed gate-rollback ceiling; the
  build-timeout arm ≈ 3.6 h) — bounded, eats into the nightly slot, never
  blocks the chain. Typical rust-delta night: minutes; python-only: seconds.
- The self-heal/repair reach is ASYMMETRIC: it repairs a broken `fno_math`
  and a stale-but-loadable `trading_core`, but an UNLOADABLE `trading_core`
  crashes the CLI pre-leg (the wedge row above) — external signals only.
- brutex-lake IS in the wheel set (REQUIRED) since DEC-RUST-LAKE-WAVED-001
  (W-6a, MIGRATION.md #1954) landed its required Makefile block — the designed
  drift-pin pickup, superseding the pending-exclusion clause of
  DEC-BRUTEX-LAKE-BOX-WHEEL-001. Residual: the gate/rollback import smokes +
  doctor coherence checks still cover trading_core + fno_math only —
  brutex_lake gets build + required-failure coverage; its import-smoke/doctor
  stages remain the (narrowed) DEC-BRUTEX-LAKE-BOX-WHEEL-001 follow-up scope.
- Repeated same-signature failures demote to WARN after night 1 (the alarm
  dedupe); the report block stays fully forensic every night.
