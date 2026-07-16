# Runbook — the M4-Pro flip-soak harness (default-ON flip evidence)

> DEC-FLIP-SOAK-HARNESS-001 (number pending coordinator reservation).
> ONE parameterized harness qualifies EVERY Rust-conversion default-ON flip:
> a flag's flip ADR may open only after this harness produces — and the
> operator commits — the standard evidence bundle (twin identity · proven
> engagement · soak-drift cleanliness · honest wall-clock A/B · the
> schema-validated `docs/soak/<FLAG>/<YYYY-MM-DD>-PASS.json`), plus a green
> weekly `mutation.yml` run covering the flag's kernel scope (cited by run
> ID in the ADR — the harness does not re-run mutants).

## 0. Prerequisites

* The registered self-hosted M4-Pro runner (labels `self-hosted, macos,
  m4pro` — LIVE per bench-m4pro.yml's header) with the real lake on the
  box and `BRUTEX_LAKE_ROOT` exported in the runner daemon's environment
  (dispatch path) or passed on the command line (terminal path).
* The flag under test must be a row in `scripts/soak/flag_registry.py`
  (each slice PR adds its row) and its wrapper must emit the naming-law
  structlog tokens `<surface>.engaged` / `<surface>.fallback` /
  `<surface>.kernel_autoreverted` (design §1(b) — the parameterized
  engagement gate keys on them).
* HONEST: as of this harness's landing, both registry flags live on their
  slice branches, NOT on main — always dispatch with `ref` = the slice
  branch (a main checkout refuses at the engagement gate, correctly).

## 1. The dispatch path (preferred)

GitHub → Actions → **Flip soak (self-hosted M4-Pro)** → Run workflow:

| Input | Value |
|---|---|
| `flag` | the ONE flag under test (choice = the registry) |
| `ref` | the slice branch (or SHA) whose code is being qualified |
| `soak_minutes` | drift-leg budget, 1..1100 (default 360 = 6 h; the job timeout derives as `m + 90 + m/6`, bounded by the 24-h GITHUB_TOKEN law — SEC9-1) |
| `grid_scale` | `default` or `full` for flip evidence; `smoke` is the #1479 minutes-scale shape and is NOT flip-qualifying |

The workflow is MAIN-RESIDENT by design — `workflow_dispatch` only surfaces
workflows on the default branch (the #1479 lesson: `rust-enum-twin-run.yml`
lived only on its slice branch and was never dispatchable). The `ref` input
checks out the slice code; the workflow definition itself never leaves main.

### Scheduling window (runner contention law)

The single M4-Pro runner also serves bench-m4pro (daily 11:00 UTC + pushes),
verify-m4pro (daily 12:00 UTC) and soak-selfhosted (Sun 02:00 UTC). A
self-hosted runner executes ONE job at a time — queueing IS the serializer
(GHA has no cross-workflow lock; the workflow's `m4pro-flip-soak`
concurrency group only serializes flip-soaks against each other). Operator
law:

* Dispatch in the **12:30 UTC → 01:30 UTC window** (post verify-m4pro, pre
  bench-m4pro).
* **Never Sunday** (soak-selfhosted's weekly cron).
* Avoid the **01:30 IST nightly-chain window** (lake writers — see §4).
* A queued job on an offline Mac is AUTO-CANCELLED by GitHub after 24 h
  with NO step running — the in-job failure watchdog cannot fire for a
  never-started run (SOAK-R8-2; the ADR-0018 out-of-job watchdog remains
  the named follow-up).

## 2. The terminal one-liner (fallback / local path)

```bash
bash scripts/soak/flip_soak.sh BRUTEX_SWEEP_RUST_ENUM /path/to/lake \
    --minutes 360 --scale default --out-dir /tmp/flip-soak
```

Same legs, same verdict line, `run_url` recorded as `"terminal"` (the
schema accepts it — provenance then leans on runner identity + git SHA +
the operator's PR review). Useful flags: `--skip-drift` (twin leg only,
NOT flip-qualifying alone), `--drift-dry` (the CI fake-lake drift mode —
never flip-qualifying), `--run-url` (the dispatch path sets it).

## 3. One-flag-at-a-time law (mechanical)

The harness REFUSES to start if ANY registry flag is pre-set in the ambient
environment — not just the flag under test (soaking X with Y ambiently ON
qualifies NOTHING; a stacked flip could pass alone yet interact). PASS.json
records the full `ambient_flags` map as proof. Stacked-flag soaks are a
separate, later evidence class requiring their own registry mode + ADR —
deliberately out of v1.

## 4. RETRY_ENV_MOVED semantics (lake mutation mid-soak)

The box's own automation (01:30 IST nightly chain, the ▶ Pull Spot
auto-pull scheduler, crossverify) legitimately WRITES the lake while a
sweep computes (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001), so the harness never
assumes an exclusive lake. Each arm's `data_snapshot_hash` is harvested
from the run's persisted JSON sidecars; if the two arms disagree — or ONE
arm holds two distinct hashes (moved MID-arm) — the verdict is
**RETRY_ENV_MOVED** (exit 3), NEVER a kernel FAIL:

* It is NOT evidence against the kernel — the input moved, nothing else is
  knowable from the run.
* Remediation: re-dispatch outside the nightly window; the grids pin CLOSED
  historical quarters (keep-first lake writes never restate stored history;
  only a heal/quarantine can move a closed quarter — if RETRY repeats,
  check `<lake>/.quarantine/` before suspecting the harness).
* A RETRY artifact is NOT committable under `docs/soak/` (PASS-only law).

## 5. Evidence commit ritual (after a PASS)

1. Fetch `PASS.json` from the run's `flip-soak-evidence` artifact (or the
   `--out-dir` on the terminal path).
2. Validate locally:
   `python3 scripts/soak/emit_pass.py --validate PASS.json`
3. Commit it via a NORMAL reviewed PR to
   `docs/soak/<FLAG>/<YYYY-MM-DD>-PASS.json` (+ optionally the bounded
   `.log.txt` tail beside it). CI re-validates the schema + the PASS-only
   law (`tests/unit/test_flip_soak_harness.py`).
4. The flip ADR cites: the PASS.json path, the `run_url`, AND the weekly
   mutation run ID. No PASS.json → no flip ADR.

## 6. Failure triage

| Verdict / exit | Meaning | Action |
|---|---|---|
| exit 2 refusal | preset registry flag / missing lake / bad args | clean the env, rerun |
| exit 4 engagement | ON arm never engaged, or a bad marker matched (fallback / autorevert — the masked-PASS class) | the wheel is stale or the slice's wrapper mis-emits the naming-law tokens; rebuild from the ref, check the slice PR |
| exit 5 vacuous | OFF arm produced ZERO artifacts | the grid/scale produced nothing — wrong window or lake coordinates |
| exit 6 byte diff | manifests differ | a REAL kernel divergence — file the bug on the slice PR; diff the two manifests printed in the log |
| exit 7 divergence | DIFFERENTIAL class counter non-zero | same — the R45-style differential caught the Rust math out |
| exit 3 RETRY_ENV_MOVED | lake moved | §4 |
| exit 8 drift FAIL | RSS/fd hard-gate breach or a window run error | a leak class — the exact regression the soak exists to catch; the real-time Telegram push fired IF the runner daemon exports BRUTEX_TELEGRAM_* (otherwise silent no-op — the OH7-1 honest disclosure) |

A failed/cancelled dispatched run auto-files a `bug,soak,critical` issue
(the `failure() || cancelled()` watchdog — with the two structurally
uncoverable paths disclosed in the workflow header).

## 7. Honest envelope

* The harness itself has NOT yet executed on the real box — its own logic
  is CI-proven in dry/fake-lake mode (`tests/unit/test_flip_soak_harness.py`);
  the first real dispatch is operator work.
* Grid scales are PROVISIONAL pending a wall-time probe on the box (design
  §6 open question 1); SENSEX (BSE exchange) is out of the v1 grid.
* `soak.p99_drift` is OBSERVE-ONLY end-to-end (the DEC-SOAK-REAL-TIER-001
  ~15% false-fail rationale) — RSS ceiling + fd growth are the hard gates.
* macOS `ru_maxrss` is a process-lifetime PEAK — the RSS rule is a ceiling
  backstop, not a growth slope (`process_probe.py`).
