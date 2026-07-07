# Absolute benchmark baselines — per-hardware, measured-only

**DEC-BENCH-ABS-BASELINE-001 (MIGRATION.md #1528).**

This directory holds the committed ABSOLUTE hot-path benchmark baselines the
`bench-m4pro.yml` compare gate enforces. One file per machine:
`<hardware_key>.json`, where the filename stem MUST equal the embedded
`hardware_key` (schema law), and the key MUST re-derive from the embedded
fingerprint fields — `{model_slug}-{p}c{pp}p{e}e-{ram}g-{arch}`, e.g.
`mac16-8-14c10p4e-48g-arm64`. Numbers are NEVER compared across keys.

> **This directory ships with ZERO `.json` files — no number in the
> bootstrap PR is fabricated; the first baseline is measured by the M4 Pro
> runner itself** (Golden Rules #1/#10).

## Schema (v1)

- ONE unit: `ns_per_op` (lower is better; ops/s is display-derived).
- ONE statistic: `min` (best-of-N, DEC-CANARY-MINTIME-001 idiom); ALL raw
  round samples are recorded so any other stat is re-derivable forever.
- Per entry: `value_ns_per_op` (== `min(samples)`), `samples_ns_per_op`,
  `spread_pct`, `tolerance_pct` (**strictly < 100 AND failure multiplier
  `1 + t/100` float-exactly below 2.0** — the raw range alone admits the
  top ULP below 100, whose multiplier rounds to exactly 2.0 in IEEE
  double; the structural cap that makes a 2x slowdown fail under EVERY
  governable config),
  `improvement_warn_ratio` (default 2.0 → WARN "baseline stale" when MORE
  than 2x faster, strict),
  `suspicious_speedup_ratio` (default 4.0 → FAIL when MORE than 4x faster,
  strict — exactly 4.0x passes with the staleness WARN; forces a ledgered
  re-bless), `workload_hash` (of the declared `ENTRY_WORKLOADS` params),
  `decision_id`, and live-verifiable `provenance` (run URL + commit +
  captured timestamp + runner name).
- Boundary law: FAIL iff `measured > value x (1 + tolerance_pct/100)`
  (strict `>` — equality passes).
- The one source of truth is `brutex/observability/bench_baseline.py`
  (pure stdlib; the CI steps load it standalone with zero pip installs).

## Lifecycle

```
measure (bench-m4pro, 6 fragments)      no <key>.json?
        │                                    │
        ▼                                    ▼
   compare gate ──── OK/WARN ──► attestation "BENCH-ABS GATE RAN: 6/6 …"
        │                                    │
        ├─ REGRESSION (rc 1) → ONE fresh-process re-measure → still red → job FAILS
        ├─ STRUCTURAL (rc 2) → job FAILS (never retried)
        └─ ABSTAIN (rc 0, LOUD) → capture 3 process-fresh runs
                                       │ (refused if cross-run spread > tolerance/2)
                                       ▼
                        bless-baseline job (GitHub-hosted, the ONLY
                        contents/PR-write holder; the runner's token is
                        issues:write only) commits the candidate VERBATIM
                        and opens a bot PR → operator nudges CI + merges
```

## Governance (mechanical — no silent re-baseline)

- A FIRST capture per cell is pre-authorized by `DEC-BENCH-ABS-BASELINE-001`
  (this standing entry) — additive blesses need no new ledger entry.
- ANY modification/removal of a blessed cell requires, in the SAME PR: a
  NEW human-written `docs/MIGRATION.md` entry whose ADDED lines quote the
  new decision id, the literal entry key, AND the literal new value token
  (e.g. `55.0`); plus a FRESH provenance run that the gate LIVE-verifies
  via the GitHub API (exists on THIS repository — the `$GITHUB_REPOSITORY`
  repo pin, so a completed run on a fork/foreign repo is a breach and is
  never queried; is bench-m4pro.yml; completed with conclusion `success`;
  head_sha == the embedded commit). Enforced by
  `scripts/ci/bench_baseline_gate.py govern` inside the required `bench`
  CI job; a missing `$GITHUB_REPOSITORY` fails CLOSED. There is NO
  override env.
- Workload-param change recipe (S11): the SAME PR that edits
  `ENTRY_WORKLOADS` (and the hash golden in
  `tests/bench/test_absolute_baseline.py`) REMOVES the affected baseline
  entries with a ledgered note; the next scheduled run abstains on them and
  auto-blesses additively. Forgetting this leaves an honest STRUCTURAL red
  on the next m4pro compare — see `docs/runbooks/bench-baseline.md`.

## Bootstrap nudge (stated openly)

GITHUB_TOKEN-created PRs do not trigger CI: close/reopen the bot bless PR
(or push an empty commit) so the required `CI OK (aggregate gate)` context
reports, then review + merge. One nudge + one click; deliberately NO PAT
(a PAT on operator hardware would reopen the credential surface
DEC-CI-FORK-PR-HARDEN-001 closed).
