# docs/soak/ — the flip-soak COMMITTED EVIDENCE home

> DEC-FLIP-SOAK-HARNESS-001 (MIGRATION.md #1894).
> This directory ships **EMPTY of evidence by design** — no PASS file is
> ever fabricated by a build session. Evidence lands ONLY via an operator
> evidence PR after a real run on the §8 M4-Pro box (the
> `docs/groww/probe/settle_evidence.json` / `tests/bench/baselines/`
> governance precedents).

## Why a committed home (the #1479 gap)

PR #1479's evidence protocol was "paste the PASS line + hash manifests into
the PR body" — the slot still reads *pending*; PR bodies are mutable,
unvalidated and un-citable by a later ADR in any machine-checkable way, and
workflow artifacts expire (14–90 day retention). A default-ON flip ADR
needs evidence that is (a) reviewed, (b) schema-validated in CI, and
(c) citable by path + git history — i.e. a committed file.

## Path convention

```
docs/soak/<FLAG>/<YYYY-MM-DD>-PASS.json     # the machine-readable verdict
docs/soak/<FLAG>/<YYYY-MM-DD>-PASS.log.txt  # optional bounded log tail (companion)
```

`<FLAG>` is the registry env-var name verbatim (e.g.
`BRUTEX_SWEEP_RUST_ENUM`). One file per qualifying run; a re-run on a later
date is a NEW file, never an edit (edits are diff-visible governance
events, the bench-baselines law).

## Schema v1 (validated by `scripts/soak/emit_pass.py --validate` + the repo test)

Top-level required keys: `schema_version` (=1) · `flag` · `verdict` ·
`date_utc` · `git_sha` · `branch` · `runner_name` · `hardware` (object with
`model`) · `run_url` (an actions run URL, or the literal `"terminal"` for
the operator one-liner path) · `grid_scale` · `engagement`
(`engaged_count` ≥ 1, `bad_markers_found` == 0 on a PASS) · `soak`
(`windows` ≥ 1, `hard_gate_breaches` == 0 on a PASS) · `wall_clock` ·
`ambient_flags` (EVERY registry flag recorded — the one-flag-at-a-time
proof), plus EXACTLY ONE of:

* `twin` — SWEEP_TWIN / SURFACE_GOLDEN classes: `off_manifest_sha256` /
  `on_manifest_sha256` / `files_compared` (≥ 1 — the vacuity guard) /
  `byte_identical` / `data_snapshot_hash_off` / `data_snapshot_hash_on`.
* `differential` — DIFFERENTIAL class (byte identity is trivially true BY
  DESIGN — the Rust side computes, the Python oracle's value ships):
  `divergence_count` (== 0 on a PASS). This deliberately replaces the
  design sketch's `comparisons` field — engagement counts prove the
  differential ran; the divergence counter is the load-bearing oracle.

## Governance laws

1. **PASS-only.** `verdict` must be `"PASS"` for any file under this
   directory — `emit_pass.py` refuses to write FAIL/RETRY_ENV_MOVED here,
   and the repo test refuses a committed non-PASS file. A red soak lives in
   the issue tracker (the workflow's failure watchdog files it).
2. **Provenance mandatory.** A venue-less PASS is invalid: `run_url` (or
   `"terminal"` + the runner identity), `git_sha`, `branch`, `runner_name`
   and the `hardware` block are all required — the honest-venue discipline
   (a PASS quoted without venue is the exact drift Golden Rule #10 forbids).
3. **Reviewed-PR landing.** Evidence is committed by the operator via a
   normal reviewed PR (the settle-evidence flow); a bench-m4pro-style
   `bless-baseline` bot-PR automation is a possible later iteration once
   the schema is stable (design §6 open question 2).
4. **One flag at a time.** `ambient_flags` records every registry flag;
   only the flag under test may be non-null. Stacked-flag soaks are a
   SEPARATE, later evidence class requiring their own registry mode + ADR.
5. **The flip ADR cites BOTH** the PASS.json path AND the run/transcript
   (`run_url`), plus the weekly mutation.yml green run ID for the flag's
   kernel scope — the harness does not re-run mutants.

Honest envelope (Golden Rule #10): a determined operator can still
fabricate a PASS file — the control is review + this governance law +
schema validation, stated honestly, the same posture as
`tests/bench/baselines/`.
