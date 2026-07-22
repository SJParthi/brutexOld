# Pull Request — BruteX

> **Merge-Readiness Charter (DEC-MERGE-GUARANTEE-001):** this PR obeys
> `docs/MERGE_READINESS_GUARANTEE.md` — the §3 gate matrix + §4 Definition of
> Done. Never merge over a red or pending gate. Per Golden Rule #10, partial /
> roadmap / out-of-scope rows are disclosed in the Honest envelope below.

**Status: DRAFT** — awaiting this PR's own real GitHub CI run (every gate below
was executed executor-local; the review protocol's standing recommendation is a
green real-CI run before ready/merge).

## Before / After (plain language)

**Before:** the Stage-3 refinement optimizer (the between-stage CMA-ES that
polishes each finalist's (sl, tp) cell) ran on the third-party Python `pycma`
package — with a MEASURED budget overshoot (121 unique evaluations on a
documented 60×2 budget) and a 32-bit seed cap imposed by numpy's MT19937.

**After:** the optimizer is a PURE-RUST, dependency-free crate
(`rust/brutex-refine`: splitmix64-seeded xoshiro256++ + cached Box-Muller,
hand-rolled cyclic-Jacobi eigensolver, Hansen arXiv:1604.00772 CMA-ES,
IPOP-lite restarts, grid-snapped asks, streaming bounded top-K select) behind
one ask/tell session FFI — DEFAULT-ON via `BRUTEX_REFINE_RUST` with the Python
optimizer math DELETED in the same lane (greenfield-sanctioned: no deployment
and no stored stage-3 backtests exist, so no pycma bit-match is owed). The
strict budget law closes the overshoot; the seed widens to ADR-0007's original
low 64 bits; refinement run identity re-keys to v2 with the crate-source
digest folded in (any crate edit re-keys automatically).

**How:** `brutex/engine/refine_rust.py` is a loud-refusal facade (wheel-absent
= typed `RefineRustUnavailableError` naming `maturin develop --release` at
CALL time only — the dashboard boots wheel-free, subprocess-proven); all
bookkeeping (cache/evals/best-key/audit) stays Python; grid error strings are
byte-identical crate↔Python (parity-pinned); select preserves the
DEC-SWEEP-STREAMING-RANK-002 bounded-memory law end-to-end.

## Definition of Ready (plan-first gate — DEC-DEFINITION-OF-READY-001)

### Scenarios covered

- Happy: a seeded 60×2-budget refinement through the REAL `refine_mask` →
  117 evals, best ((100, 260), 0.01) — frozen as the Python golden
  (`tests/golden/stage3_refine_rust_golden.json`) + the Rust trajectory golden
  (120 asks, head/tail literals, FNV-1a full-sequence pin); refuter round
  re-reproduced both on an independently built wheel.
- Edge: budgets landing mid-population (truncated final generation, NO
  update); single-point grid axes; a 10¹²-step axis; exact-half round-even
  denormalize; restart k seeding `seed.wrapping_add(k)`; heavy sharpe ties
  (±0.0 / 1e308 / 5e-324) + duplicate run_ids in select.
- Empty/zero: zero-candidate select boards; `None` sharpe mapped to 0.0;
  top_k ∈ {1..10000} incl. top_k ≫ stream.
- Failure: NaN/inf tell rejected with the ask left retryable (state provably
  uncorrupted — retry trajectory byte-identical to a never-poisoned run);
  hi<lo / tick>range / lo≤0 / off-grid x0 → typed refusals with byte-exact
  messages; wheel absent → typed call-time refusal; falsy/junk
  `BRUTEX_REFINE_RUST` → loud typed refusal (a fallback that does not exist is
  never silently pretended); a mid-iteration evaluator raise propagates loudly
  after exactly the asked evals with a deterministic re-run.
- Concurrent: cross-process determinism proven (two fresh processes +
  interleaved host-RNG activity → full trajectory + best + run_id
  byte-identical; the in-crate PRNG is immune to host-RNG interference);
  sessions are per-refinement objects — no shared mutable state crosses the
  FFI.

### Failure modes

- Wheel missing / stale (EXPECTED_BUILD handshake mismatch) → typed
  `RefineRustUnavailableError` at CALL time naming the build step; import +
  dashboard boot stay wheel-free (101 routes, subprocess-proven).
- `BRUTEX_REFINE_RUST` falsy or junk → loud typed refusal (no silent
  fallback exists by design).
- NaN/inf tell → crate rejects (`ValueError`), the outstanding ask stays
  retryable, optimizer state uncorrupted (byte-identical-retry proven).
- Invalid grid / out-of-box x0 / non-finite sigma0 → typed refusal BEFORE any
  optimizer state is built; under the house `panic="abort"` profile the FFI
  input validation is process-survival load-bearing — every reachable crate
  assert was traced and confirmed guarded (round-1 review).
- Budget exhaustion mid-generation → truncated final ask batch, no update,
  deterministic termination (44-combo sweep, 0 violations).
- A crate source edit → the baked source digest moves → every refinement
  run_id re-keys automatically (identity v2); the stale-wheel handshake
  refuses a mismatched wheel.

### Real-run proof

```
.venv/bin/python -m pytest tests/golden/test_stage3_refine_rust_golden.py \
  tests/golden/test_engine_run_identity_golden.py tests/unit/test_engine_refine_rust.py -q
# → 28 passed against the REAL built wheel (117 evals through the real
#   refine_mask; four v2 identity digests re-derived bit-for-bit)
cargo test --release --locked --manifest-path rust/brutex-refine/Cargo.toml
# → 58 green (48 lib + 2 golden + 5 proptest + 3 digest-parity)
```

(The refuter round additionally re-reproduced the full trajectory
byte-identically across two fresh processes on an independent wheel rebuild,
ran the 44-combo budget sweep with 0 violations, and ran the OLD-vs-NEW
`group_survivors` differential over 720 adversarial ResultRow streams with 0
mismatches.)

### Rollback plan

- Rollback = `git revert` of this branch's commits — stated plainly: the
  `BRUTEX_REFINE_RUST` flag exists but has NO Python fallback by design (the
  pycma math is deleted), so the flag is a loud-refusal switch, not a
  rollback lever.
- Greenfield: no deployment and no stored stage-3 backtests exist — there is
  no data or deployment state to unwind (MIGRATION.md #2110 greenfield
  ruling).
- The deleted pycma path is restorable from git history (merge-base
  `4c11f7a`) if a future decision ever re-opens it (its own DEC required).

### Out of scope

- `DEC-REFINE-RUST-DOCTOR-STAGE-001` — the doctor wheel-freshness stage for
  this crate (named follow-up).
- Cross-platform golden portability — the trajectory goldens are
  platform-scoped (glibc-2.39 x86-64, the pinned runner); a future
  runner-image bump re-locks via `BRUTEX_REGEN_STAGE3_GOLDEN=1` (disclosed
  residual, not solved here).
- The full 468-mutant cargo-mutants pass — the wired weekly
  `rust-mutation-brutex-refine` job is the enforcing zero-survivor gate.
- Any engine-scope / vocabulary / rate / lake-schema change — none
  (ENGINE result UNCHANGED, NIFTY/BANKNIFTY/SENSEX per
  DEC-ENGINE-SPOT-ONLY-001; stage-3 is a between-stage refinement layer).

---

## Summary

- The Stage-3 CMA-ES refinement path converted to PURE RUST
  (`rust/brutex-refine`, dependency-free: in-crate PRNG + Jacobi eigensolver
  + Hansen-parametrized CMA-ES + IPOP-lite restarts + tick-grid axis +
  streaming bounded top-K select), flipped DEFAULT-ON, with the Python
  `pycma` optimizer math DELETED in the same lane (greenfield-sanctioned).
- Refinement run identity re-keyed to v2 (crate-source digest folded in);
  the strict budget law closes pycma's measured 121-eval overshoot on a
  60×2 budget; seed widened 32→64 bits (ADR-0007's original law).
- CI: build-wheels 4→5, the refine wheel at every stage3-running consumer
  job, NEW `rust-brutex-refine` crate job in ci-ok.needs, NEW per-module
  100% gate, mutation.yml sibling mirror.

## Spec / decision links

- Decision ID(s): DEC-RUST-STAGE3-REFINE-001 (supersedes
  DEC-STAGE3-OPTIMIZE-EXTRA-001 #626 + the pycma arm of ADR-0007
  condition 4); named follow-up `DEC-REFINE-RUST-DOCTOR-STAGE-001`.
- MIGRATION.md row(s): #2110 (renumbered #1957 → #1960 → #2110 — the
  2026-07-19 main merge-forward hop (main's club-O fold took #1957), then
  the 2026-07-19 queue-master ledger-census collision resolution; per
  DEC-LEDGER-RENUMBER-001 the DEC-ID is the stable handle).
- Spec section(s) touched: ADR-0007 condition-4 identity formula (v2
  successor recorded in #2110); no TRACK1 spec text change.

## Type of change

- [x] feat — new feature
- [ ] fix / docs / refactor / test / chore / perf

## Golden Rule #12 — 100% standards self-check

### Coverage + tests

- 100.00% line+branch on every touched module the ci.yml way: refine_rust
  43/6 (20 tests) · stage3_refine 94/18 (18) · stage3_grid 44/8 (46) ·
  stage3_select 36/12 (28) · stage3_wire 218/64 (59) · box_update 711/274
  (151); whole-engine gate 100.00% (11319 stmts / 3258 branches, 0 missed).
- Property: 5 proptest invariants in-crate (grid round-trip/totality/
  monotonicity, driver budget+grid containment+bit-identical determinism,
  select vs a verbatim two-pass reference).
- Golden: the frozen Rust trajectory golden + the Python frozen golden
  through the REAL `refine_mask` + the re-locked v2 run-identity golden
  (four digests, hand-assembled payload bytes, 3 independent hashers).
- Bench: N/A — stage-3 is a between-stage layer off the Golden-Rule-#5 hot
  paths; the mask/bar hot loop receives ZERO diffs (frozen sweep-run goldens
  pass UNMODIFIED).

### Bug fixes

- N/A — feat, not fix.

### Type / lint / security

- `mypy --strict brutex` — Success: no issues found in 496 source files.
- `pyright brutex tests` — 0 errors (warnings at the house baseline).
- `ruff check` + `ruff format --check` clean (brutex + tests + crate shim).
- cargo fmt --check clean; clippy `--all-targets --locked -D warnings` clean;
  `cargo tree --locked` clean vs the committed Cargo.lock (runtime deps =
  only optional pyo3 — genuinely dependency-free).
- No new copy-paste: the crate follows the `rust/brutex-feed` template; the
  one duplicated `(hi-lo)//tick+1` steps formula (Python + Rust) is a
  drift-pinned ACCEPTED deviation (collapsing it would put the wheel on the
  wheel-free import path — recorded in #2110).

### Performance

- No new O(n) where O(1) was achievable; the optimizer is O(evals) by
  definition and off the hot paths; no pandas; no new global mutable state
  (sessions are per-refinement objects).

### Observability

- The stage-3 audit payload carries optimizer_version + optimizer_build;
  structlog throughout, no `print`; no new metric / CRITICAL condition.

### Documentation

- NumPy-style docstrings on public symbols; MIGRATION.md #2110 + the
  CLAUDE.md header/§1/§3 blurbs updated (incl. both review-round records);
  no silent scope expansion (Golden Rule #3).

### No hallucination (Golden Rule #1)

- The CMA-ES parametrization is sourced from Hansen arXiv:1604.00772 and was
  verified constant-by-constant by the round-1 review; the crates.io evidence
  for rejecting the third-party `cmaes` crate is dated + recorded in #2110;
  no invented rate/threshold/constant. No Groww/NSE/BSE market fact is
  touched by this PR (pull scope, rate caps, calendars all byte-untouched —
  MIGRATION.md #2110).

---

## Z+ 15-row matrix (100% everything — charter §C)

| Demand | This PR's proof | Real-time check |
|---|---|---|
| 100% code coverage | 100.00% line+branch on all six touched/gated modules (refine_rust 43/6 · stage3_refine 94/18 · stage3_grid 44/8 · stage3_select 36/12 · stage3_wire 218/64 · box_update 711/274) + the whole-engine gate 100.00% (11319/3258, 0 missed, 3843 passed) — re-measured independently by the refuter round | ci.yml per-module + whole-engine gates |
| 100% audit coverage | the stage-3 audit payload carries optimizer_version + optimizer_build; the durable decision record is MIGRATION.md #2110; no audit-JSONL schema change | audit-chain CI gate unchanged |
| 100% testing coverage | unit + integration (rehearsal + vix 23 passed) + property (5 proptest invariants) + golden (Rust trajectory + Python refine_mask + v2 identity digests) + cargo 58 `--release --locked` + the 55-file workflow-pin battery 2925 passed; bench N/A (off-hot-path, stated); mutation via the wired weekly `rust-mutation-brutex-refine` zero-survivor job | pytest + cargo + mutation.yml |
| 100% code checks | ruff + ruff-format clean; `mypy --strict` clean (496 files); pyright 0 errors; cargo fmt/clippy `--locked -D warnings` clean; bandit rides the standing pre-push/CI gates | pre-push hooks + ci.yml |
| 100% code performance | N/A hot-path — stage-3 is a between-stage refinement layer; the mask/bar hot loop receives ZERO diffs (frozen sweep-run goldens UNMODIFIED); the strict budget law makes eval cost ≤ documented budget (pycma overshot 121 on 60×2 — closed) | frozen sweep goldens + budget proptest |
| 100% monitoring | typed loud refusals + structlog events on the facade paths; the audit payload carries the optimizer provenance | grep brutex/engine/refine_rust.py |
| 100% logging | every refusal/engagement path emits structlog, no `print` | grep -r structlog |
| 100% alerting | N/A — no new CRITICAL condition; a stale box wheel refuses stage-3 sweeps loudly via the nightly `WHEEL_SET` required=True entry | brutex/nightly/box_update.py |
| 100% security | no secret/credential/network surface; zero new runtime deps (dependency-free crate; Cargo.lock committed, `--locked` everywhere); deny.toml in-crate | pre-push gate + deny.toml |
| 100% security hardening | every `# pragma: no cover` is a structural Protocol stub (refuter-verified, no logic-hiding); FFI input validation is process-survival load-bearing under `panic="abort"` — traced total by round 1, maintainer notes at python.rs + the facade | grep noqa / pragma |
| 100% bug fixing | round-1 findings folded at `25c2bf4` (MINOR-2 ledger count, NIT-1 docstring label, NIT-2 maintainer notes; MINOR-1 + NIT-3 recorded as disclosed residual / accepted deviation); round-2 refuter CLEAN | review record below |
| 100% scenarios covering | the Definition-of-Ready scenario battery above — hostile grids, NaN-tell retry, mid-population budgets, cross-process determinism, wheel-free boot, falsy/junk flags — all EXECUTED (round-2 evidence) | pytest -q battery |
| 100% functionalities covering | every new public symbol driven by the facade matrix + the stem-matched suites; the OLD-vs-NEW `group_survivors` caller-contract differential read 0 mismatches over 720 adversarial streams | pre-push gate |
| 100% code review | two independent adversarial rounds (SHIP then CLEAN) — evidence below | this section |
| 100% extreme check | the frozen trajectory goldens + the FNV-1a full-sequence pin + the source-digest identity re-key + the EXPECTED_BUILD stale-wheel handshake are the mechanical anti-regression rails | every commit |

## Z+ 7-row resilience matrix (charter §C)

| Demand | Honest envelope | This PR's proof |
|---|---|---|
| Zero ticks lost | N/A — BruteX is backtest-only (Track 1); no feed/ingest surface touched | confirmed (diff inspection, round 1) |
| WS never disconnects | N/A — no WebSocket in Track 1 | confirmed |
| Never slow/locked/hanged | the optimizer is single-threaded, allocation-bounded, budget-capped; a wedged evaluator propagates loudly (never swallowed); no I/O in the crate | 44-combo budget sweep 0 violations; mid-iteration-raise test |
| QuestDB never fails | N/A — BruteX uses DuckDB + LMDB; neither touched | confirmed |
| O(1) latency | N/A — stage-3 is off the Golden-Rule-#5 hot paths; the mask/bar hot loop receives ZERO diffs | frozen sweep-run goldens pass UNMODIFIED |
| Uniqueness + dedup | refinement run identity v2 is collision-hardened (blake3 over the labeled payload; the `\x1f` reject guard covers the injectable tokens; no v1 aliasing — refuter-verified) | run-identity golden (4 digests, 3 independent hashers) |
| Real-time proof | the Real-run proof commands above, executed against the REAL built wheel; refuter re-reproduced on an independent rebuild | this PR body (Real-run proof section) |

## Z+ 7-layer audit

| Layer | This PR's status |
|---|---|
| DETECT | the frozen Rust trajectory golden + FNV-1a full-sequence pin + the Python refine_mask golden detect ANY behavioral drift; the EXPECTED_BUILD handshake detects a stale wheel; the source-digest identity re-key detects a crate edit at run-identity level |
| VERIFY | cargo 58 `--release --locked` + the golden/property/caller battery (271 passed) + the refuter's independent-rebuild reproduction (trajectories, digests, 720-stream select differential) verify end-to-end |
| RECONCILE | greenfield — no stored stage-3 state to reconcile; the caller contract for stage3_wire/cascade consumers is byte-identical (OLD-vs-NEW differential, 0 mismatches) |
| PREVENT | the wired weekly `rust-mutation-brutex-refine` zero-survivor job + the `rust-brutex-refine` crate job in ci-ok.needs + the per-module 100% gate + the workflow-pin battery prevent silent regression |
| AUDIT | MIGRATION.md #2110 (incl. both review-round records) is the durable decision record; the stage-3 audit payload carries optimizer_version + optimizer_build per refinement |
| RECOVER | rollback = git revert (greenfield — nothing to unwind; the deleted pycma path is restorable from git history at `4c11f7a`); the flag is a loud-refusal switch, NOT a fallback (stated plainly) |
| COOLDOWN | the standing recommendation: this PR's own real GitHub CI run before ready/merge (all gate evidence is executor-local); post-merge the weekly mutation job completes the 468-mutant scope |

## 4-agent hostile review

Honest framing (Golden Rule #10): the review protocol run for this lane is
TWO independent adversarial rounds — a round-1 hostile review (verdict SHIP)
+ a round-2 refuter (verdict CLEAN) — each carrying the canonical lenses
(correctness/math, determinism, FFI safety/security, O-complexity + coverage
honesty, cite/path — `docs/zplus/ZPLUS_PR_DISCIPLINE.md`) jointly rather
than as four single-lens agents.

**Round 1 (hostile review — verdict SHIP), per-hunt table:**

| Hunt | Lens | Result |
|---|---|---|
| Correctness / math | every CMA-ES strategy constant vs Hansen arXiv:1604.00772 (weights, μ_eff, c_σ/d_σ/c_c/c_1/c_μ, χ_n, h_σ gate, rank-1+rank-μ update with the c1a correction, CSA, unit-box repair) | CLEAN — all match; the classic non-active (μ/μ_w, λ) form, correctly implemented |
| Determinism | in-crate PRNG, BTreeMap ordering, to_bits compares; goldens rebuilt on an independent glibc-2.39 host | CLEAN — Rust golden + all 5 proptests reproduced bit-for-bit |
| FFI safety | every reachable crate assert traced vs the RefineDriver/SelectCore validation | CLEAN — validation-total; rejected tell retryable |
| Fail-loud contract | flag semantics + wheel-free import + pycma deletion | CLEAN — subprocess-proven; grep-confirmed deletion |
| Run-identity v2 | digest derivation via hand-assembled payload bytes + 3 independent hashers; no v1 aliasing | CLEAN — WF/SCHED digests untouched |
| Blast radius | hot-path identity modules byte-untouched; zero frozen sweep goldens modified | CLEAN — substantiated |

Findings: 0 BLOCKER, 0 MAJOR; MINOR-1 (platform-fragile trajectory goldens in
required jobs — disclosed residual + re-lock lever + run-real-CI
recommendation), MINOR-2 (stale 57→58 cargo count — corrected), NIT-1/2/3
(docstring label, panic="abort" maintainer notes, steps-formula duplication)
— all folded/recorded at commit `25c2bf4`.

**Round 2 (refuter — verdict CLEAN, no MINOR+), executed-evidence summary:**
independent wheel rebuild; all NINE load-bearing claim families survived
executed refutation — cross-process seeded bit-identity incl. host-RNG
interference (117-eval trajectory + best + run_id byte-identical); NaN-tell
retry byte-identical to an unpoisoned run; the 44-combo strict-budget sweep
0 violations; the hostile-grid battery 0 off-grid/OOB asks; the OLD-vs-NEW
`group_survivors` differential over 720 adversarial ResultRow streams 0
mismatches; wheel-free boot of 101 routes + typed call-time refusal;
100.00% re-measured on all five modules; CI/mutation wiring re-verified
incl. all 10 wheel-install sites + ci-ok.needs; the four ledger digests
reproduced bit-for-bit on a fresh build. One cosmetic NIT (the
production-unreachable fail-loud `_sharpe_of` NaN pass-through) recorded as
a named residual in MIGRATION.md #2110.

Review evidence: MIGRATION.md #2110 (both per-round records) + the CLAUDE.md
§1 review-rounds clause.

---

## Honest envelope (Golden Rule #10)

- ✅ REAL: a correct, deterministic, dependency-free CMA-ES verified against
  frozen goldens + property invariants + an independent-rebuild refuter
  round; 100.00% line+branch on every touched module + the whole engine;
  the caller contract byte-identical (720-stream differential); the mask/bar
  hot loop byte-untouched.
- ✅ REAL (disclosed deltas, greenfield-sanctioned per the #2110 ruling):
  the strict budget law CHANGES eval counts vs the deleted pycma path
  (which overshot — measured 121 on a 60×2 budget); the seed widens 32→64
  bits. No pycma bit-match is owed — none is claimed.
- 🟡 PARTIAL: the determinism claim is PLATFORM-SCOPED — glibc-2.39 x86-64
  (the pinned ubuntu-24.04 runner class); cross-platform trajectory
  bit-identity is NOT claimed (libm-dependent; `BRUTEX_REGEN_STAGE3_GOLDEN=1`
  is the re-lock lever for a future runner-image glibc/arch bump — the
  round-1 MINOR-1 disclosed residual). The 468-mutant cargo-mutants full
  pass is DEFERRED to the wired weekly zero-survivor job (2 in-session
  survivors found + already pinned; partial in-session evidence recorded in
  #2110). ALL gate evidence is executor-local until this PR's own GitHub CI
  runs (the standing pre-merge recommendation).
- 📋 ROADMAP: `DEC-REFINE-RUST-DOCTOR-STAGE-001` (doctor wheel-freshness
  stage); the fuzz lane (skipped per the brutex-strategies no-fuzz
  precedent — proptest never-panics invariants stand in-crate); any future
  re-opening of a Python fallback (its own DEC).

Anti-pattern check: every claim above cites the real gate evidence (Test
plan below) or the ledger entry (MIGRATION.md #2110); no claim is based on a
hypothetical artifact.

## Test plan — gate evidence (all executed, executor-local; verbatim tallies)

- [x] cargo: **58 green** `--release --locked` (48 lib + 2 golden + 5
      proptest + 3 digest-parity); fmt + clippy `-D warnings` clean
- [x] Whole-engine 100% gate: **100.00%** (11319 stmts / 3258 branches,
      0 missed), **3843 passed**, exit 0
- [x] Per-module 100.00% line+branch: refine_rust 43/6 · stage3_refine
      94/18 · stage3_grid 44/8 · stage3_select 36/12 · stage3_wire 218/64 ·
      box_update 711/274
- [x] Golden/property/caller battery **271 passed** (+1 sanctioned skip);
      55-file workflow-pin battery **2925 passed**; integration rehearsal +
      vix **23 passed**
- [x] `mypy --strict brutex` — **Success: no issues found in 496 source
      files**; `pyright brutex tests` — **0 errors**
- [x] Run-identity v2 digests (re-derived bit-for-bit, refuter-confirmed):
      default `c66e6c6cf100d308cce20b3ed998d4d75f8879550ed54ee22dd07b4809584e3a` ·
      varied_budget `0843a7f101e3f167ebf59e81db74061d7249faf2f477b7d9b778de02cde1f013` ·
      varied_seed `2b956f34316e87d89151044ead5d8d58bd54a2db975fa6ca275d1a88e066a1d3` ·
      varied_versions `a30b656e4b823572c492160aa54153c61e7a3493252213de9e290ed55e05f2c4`
- [x] `scripts/ci/validate_ledger_numbering.py` + `validate_citation_accuracy.py`
      green; `tests/unit/test_claude_md_branch_consistency.py` 10 passed
- [x] `scripts/ci/zplus-pr-guard.sh` + `definition-of-ready-guard.sh` +
      `validate_no_hallucination.py` + `validate_dec_ids.py` — exit 0 on this
      body

## Operator action required (post-merge)

- None at merge. Boxes rebuild the refine wheel via the REQUIRED
  bootstrap/Makefile step (abort-on-failure); the nightly box-update
  `WHEEL_SET` carries brutex_refine required=True.

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01VpcBQxBBvReHHx83bVLJYX
