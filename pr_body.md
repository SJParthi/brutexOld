## Before / After (plain language)

**Before:** the installed `fno_math` kernel reports crate 0.3.0; every new
sweep's run-identity fingerprint folds `fno_math-0.3.0` into
`indicator_version`. The orchestrator's dead scaffolding
(state/pipeline/retry/store/adaptive_rate_limiter) is still in the tree, and
the audit-chain / calendar-verdict / rate-math laws exist only in Python.

**After:** the fno-math crate reports **0.4.0** (the ONE slice-9
wheel-surface bump) carrying the new `audit_chain` / `calendar_verdict` /
`rate_math` Rust kernels, parity-gated by two frozen goldens. Because
current main already folds `fno_math.__version__` into `indicator_version`
(DEC-SWEEP-RUN-IDENTITY-COMPLETE-001), **landing this moves the engine
run-identity fingerprint: every new sweep re-keys its run identity** —
announced, never silent (the §2 Rule 4 run-identity law of the root
instructions file; MIGRATION.md entry #204). Dead orchestrator
scaffolding is deleted (live shapes preserved in `orchestrator/events.py`;
`quota_tracker` trimmed to its live exceptions + the `QuotaGate` Protocol).

## ⚠️ SERIALIZED RUN-IDENTITY RE-KEY — DO NOT MERGE

Stays DRAFT. The merge-driver lands this on the coordinator's explicit
flag, **4th** in the A → B → Vocab → **#1466** → Sweeper order.
**Run-identity goldens are intentionally HELD** (folded byte-exact from
PR #1466) and MUST be re-derived fresh on the post-Vocab tree at the land
turn — HARD gate.

## How (fold mechanics)

- Re-forward of PR #1466 (head `dc2b01f7`, merge-base `c22495cb`) onto main
  tip `0a673f18a` as ONE squash-fold commit (squash chosen by the
  subject-grep law — 5 provenance subjects carry banned tokens; provenance
  = the head sha, recorded in the fold commit body).
- 18 conflicted files resolved by STANZA-UNION, never last-wins — full
  per-file record in the fold commit body. Highlights: fno_math surface
  lists = sorted union of main's u128/stats symbols + the PR's
  audit/calendar/rate symbols (`symbols == _EXPECTED_COMPILED_SYMBOLS`
  cross-checked); `nse.py` keeps main's EXTENDED_SESSIONS arm + the PR's
  `_py_` oracle-routed predicates; ci.yml keeps main's prebuilt-wheel
  architecture + the PR's mutmut retarget; the mutants.toml
  `examine_globs` is the 9-entry union.
- Byte-verify zero-loss: **12491/12520** PR-added lines present verbatim;
  the fold's 29 exceptions individually enumerated in the fold commit
  body, EXTENDED by 7 in the cycle-2 lap below (items 30-36, the second
  mutmut retarget — see `fb4d58bf` commit body for the full list).

## Fix-forward lap (cycle 2, commits `a854d0f` + `fb4d58bf`)

Four cycle-1 CI reds + two review-confirmed reds-in-waiting, each root-caused
and fixed with measured evidence (MIGRATION.md #1727a/#1727b remain the
ledger record):

1. **Rust (fno-math build + clippy + test)** — the job's FIRST step
   `cargo fmt --check` failed: rustfmt's default module reordering wants the
   union-edited `lib.rs` mod stanza alphabetical (`audit_chain` before
   `stats`). Fixed by `cargo fmt` scoped to the crate (order-only move; every
   PR-added line stays byte-verbatim). Full job sequence re-run green
   locally: fmt --check 0 · check --all-targets 0 ·
   clippy --all-targets -D warnings 0 · test --release 656 pass 0 fail.
2. **Stranded IST pin** — `tests/unit/test_timestamp_ist_singlesource.py`
   (a main-side file, post-merge-base, not in the PR's 107 files) asserted
   `quota_tracker._IST is IST`; the fold's DEC-ORCH-DEAD-SCAFFOLD-DELETE-001
   trim (correct) deleted `_IST` → deterministic AttributeError. The import +
   assertion removed with an in-place note (the
   DEC-TIMESTAMP-IST-SINGLESOURCE-001 invariant is satisfied by removal —
   the module constructs no IST at all now); file re-run green: 24 passed.
3. **Dangling mutmut target** — the REQUIRED `mutation-py` step still ran
   `--paths-to-mutate brutex/data/parser.py`, but main's lap-8 fold DELETED
   that module + its test (DEC-DATA-PARSER-DEAD-DELETE-001) after the PR was
   cut; the score parser exits 1 on a 0-mutant set → deterministic red.
   Retargeted to the LIVE vendor parser (`brutex/adapters/groww/parser.py`,
   runner `tests/unit/test_groww_parser.py`). Measured honestly: the bare
   retarget scored 54/70 = 77.14% < the 98% floor, so the lap ALSO landed
   11 exact-value mutation-hardening tests + ONE documented
   equivalent-mutant pragma → re-measured **69/69 = 100.00%**, 135 s wall
   (inside the 300 s step budget; ~150 s projected at the CI ~2.2 s/mutant
   pace). ci.yml re-validated: YAML parse green, 47 jobs, 0 duplicate ids,
   0 dangling `needs:`, ci-ok needs UNCHANGED at 44.
4. **Two false certifications in the fold commit's record CORRECTED** (in
   the `fb4d58bf` commit body): (i) its post-merge import grep missed the
   `quota_tracker._IST` attribute access; (ii) its no-dangling-mutmut check
   was validated only against the PR-side deletions and missed main's
   independent parser delete.
5. **The three PR-body conformance reds** (Definition of Ready · Z+ marker ·
   DEC-ID validator) are fixed by THIS body revision: the plan-first
   sections + Z+ matrices below, and the DEC-ID citation form corrected —
   the two golden-fixture decision IDs (`DEC-AUDIT-CHAIN-PARITY-GOLDEN-001`,
   `DEC-CALENDAR-VERDICT-GOLDEN-001`) live inside the SHARED ledger header
   of entry #1727a, whose first-ID-only header capture means only
   DEC-ORCH-CALENDAR-RUST-CONVERT-001 resolves mechanically; they are
   therefore cited in code-span form here (the validator's documented
   non-live-citation mechanism), with entry #1727a as their ledger home.

| Gate | Result |
|---|---|
| byte-presence | PASS — 12491/12520 + 29 fold exceptions + 7 cycle-2 exceptions (all enumerated in commit bodies) |
| required-names (ci-ok needs) | PASS — set == main's 44, nothing lost; branch-protection-drift untouched |
| needs-integrity (ci.yml/mutation.yml/bench-m4pro) | PASS — every `needs:` target exists; re-verified after the cycle-2 ci.yml edit |
| crate-version | PASS — Cargo.toml == pyproject == EXPECTED_BUILD == Cargo.lock == 0.4.0 |
| fno-math CI job sequence (fmt/check/clippy/test) | PASS local — the full 4-step sequence on the fixed tree, 656 tests 0 failed |
| mutmut retarget (mutation-py step semantics) | PASS local — 69/69 = 100.00% ≥ 0.98 floor, 135 s < 300 s budget |
| fixture integrity | PASS — 3 frozen fixtures byte-identical to PR head + SHA-256 manifests match |
| ruff check / format (touched .py) | PASS |
| ledger-numbering / DEC-id / citation-accuracy validators | PASS — DEC-id re-run against THIS body revision |
| banned-token subject grep (branch commits) | PASS — 0 matches |
| python golden/rehearsal suites, mypy, pyright | DEFERRED to CI (no full dep env in the sandbox; targeted suites run — 24 + 63 tests green in a minimal py3.12 env) |

## Finalize-at-weave re-lock manifest (HARD gate at the land turn)

1. `tests/integration/test_sweep_full_chain_rehearsal.py` — re-derive
   `_PINNED_CODE_COMMIT_SHA` + re-measure the cascade-coverage assertions
   on the post-Vocab tree (the rehearsal uses the LIVE
   `cli._indicator_version()`, which this bump moves).
2. Verify ALL frozen run-identity golden suites green on the woven tree
   (`_GOLDEN_RUN_ID` `715212…62fc9` / `_GOLDEN_CONTENT_HASH`
   `ee8743…0789` / temporal-gate store hash `8341bed7…` pin the literal
   `"ind-v1"` — hermetic to this bump alone, but the Vocab event's
   vocab-66 reshape moves them).
3. `tests/golden/fixtures/vocab_golden_vectors.json` — NOT touched by this
   PR (verified); must stay byte-untouched through this event.
4. The PR's own parity fixtures (audit-chain jsonl/scalars + the
   2,557-day calendar verdict table) are fingerprint-independent — folded
   byte-exact, SHA-manifest-verified; never regenerate.
5. ADR-number note for the census: `docs/adr/0019-slice9-…` coexists with
   main's `0019-sweep-unkillable-core.md` (the repo already carries two
   0016s + two 0017s — kept; coordinator may rule otherwise at land).

---

## Definition of Ready (plan-first gate — DEC-DEFINITION-OF-READY-001)

### Scenarios covered
Fold-assembly: clean auto-merge files; 18 stanza-union conflicts
(both-sides-appended lists, deletion-vs-edit in `quota_tracker`, main-moved
mechanisms in ci.yml/rehearsal); ledger append with free numbers
(1727a/1727b per DEC-LEDGER-RENUMBER-001). Cycle-2 fix-forward: fmt drift
on the union stanza; a main-side test pinning a PR-deleted attribute; a
CI step whose BOTH paths main deleted independently (0-mutant red); a
survivor-score red hiding BEHIND the 0-mutant red (measured, then killed);
the body-gate trio (plan-first sections, Z+ markers, DEC-ID resolution
against a shared multi-ID ledger header).

### Failure modes
A lost stanza → the byte-verify gate (every exception enumerated, all else
verbatim); a lost required check → the required-names superset gate; a
dangling `needs:` → the needs-integrity parse; a wrong crate surface → the
`symbols == _EXPECTED_COMPILED_SYMBOLS` cross-check + the crate test suite
on the merged tree; a mutmut retarget that under-kills → the step's own
junitxml score gate at the 0.98 floor (measured 100.00% before push); a
premature land → the DRAFT + banner + held-goldens law (merge-driver only,
coordinator flag only); a stale body snapshot → the body-reading gates
re-run on the next pull_request event after the body update.

### Real-run proof
`cd rust/fno-math && cargo fmt --check && cargo check --all-targets &&
cargo clippy --all-targets -- -D warnings && cargo test --release` → all
green, 656 tests 0 failed on the fixed tree. `timeout 300 mutmut run
--paths-to-mutate brutex/adapters/groww/parser.py --tests-dir tests/unit
--runner "pytest -x tests/unit/test_groww_parser.py"` + the step's own
junitxml score parser → killed=69 / total=69 = 100.00%.
`pytest tests/unit/test_timestamp_ist_singlesource.py` → 24 passed.
`pytest tests/unit/test_groww_parser.py` → 63 passed.
`python3 scripts/ci/validate_dec_ids.py <this-body>` → green.

### Rollback plan
Delete the integration branch / close this draft — main is untouched
(nothing lands until the merge-driver acts on the coordinator's flag).
Post-land rollback = revert the fold commit + the two cycle-2 fix commits
(append-only history; no force-push anywhere).

### Out of scope
No golden re-lock in this prep (HELD for finalize-at-weave); no default
behavior change beyond the PR's own ratified content; no vocab change; no
rate/scope/lake change; no engine change (NIFTY/BANKNIFTY/SENSEX
unchanged); the ADR-number ruling is the census's call; the mutation.yml
weekly loop is untouched (the retarget is the per-PR ci.yml step only).

---

## Z+ 15-row matrix (100% everything)

| Demand | This PR's proof |
|---|---|
| 100% coverage on touched gates | `brutex.adapters.groww.parser` keeps its 100% line+branch ci.yml gate (63 tests incl. the 11 new); the singlesource file re-runs 24/24 |
| 100% mutation on the retargeted module | 69/69 killed = 100.00% ≥ the 98% floor, measured through the step's own junitxml parser |
| 100% audit trail | fold commit + both cycle-2 commit bodies carry root cause verbatim, fix rationale, re-run evidence, the exception ledger, and the two false-certification corrections |
| 100% byte accountability | 12491/12520 PR-added lines verbatim; 29 + 7 enumerated exceptions, zero silent drift |
| 100% required-check preservation | ci-ok needs == main's 44; 47 jobs, 0 duplicate ids, 0 dangling needs |
| crate surface integrity | `symbols == _EXPECTED_COMPILED_SYMBOLS` cross-check + 656 crate tests green |
| fixture integrity | 3 frozen fixtures byte-identical to PR head, SHA-256 manifests match |
| run-identity honesty | the re-key is ANNOUNCED (banner + land order + held-goldens law), never silent |
| lint/type on touched files | ruff check + format clean; mypy/pyright deferred to CI (no full env) — disclosed |
| YAML integrity | ci.yml re-parsed green after every edit |
| no banned tokens | 0 matches over `git log origin/main..HEAD` subjects+bodies and this body |
| append-only history | no amend, no force-push; fix-forward commits only |
| draft discipline | DRAFT + DO-NOT-MERGE banner; merge-driver lands on the coordinator's flag only |
| equivalent-mutant honesty | ONE pragma, documented in-module with the equivalence proof (`is_absent` collapses `None`/`""`) |
| measured-not-assumed budgets | mutmut 135 s measured vs the 300 s budget; CI pace projection stated with its basis |

## Z+ 7-row resilience matrix

| Failure injected | Behavior |
|---|---|
| fmt drift re-introduced | `cargo fmt --check` is the job's first step — fails closed |
| mutmut target deleted again | the score parser's 0-mutant arm exits 1 — fails closed, never silent-green |
| survivor regression on the parser | the junitxml 0.98 floor gate exits 1 |
| a body edit reverting the plan sections | DoR guard greps the five sections — fails closed |
| a body edit dropping the Z+ markers | zplus guard greps the six markers — fails closed |
| a bogus DEC-ID added to this body | the validator's existence check exits 1 against the ledger |
| premature merge attempt | DRAFT state + the DO-NOT-MERGE banner + the held-goldens HARD gate at the land turn |

## Z+ 7-layer audit
DETECT (cycle-1 CI board + the adversarial review's two reds-in-waiting) /
VERIFY (every red reproduced locally before any fix — fmt exit 1, the
seeded-body gate exits, the 0-mutant semantics, the AttributeError proven
by grep + module read) / RECONCILE (stale ci.yml prose + the two false
certifications corrected in the durable record) / PREVENT (exact-message
mutation pins + the documented pragma + the enumerated exception ledger) /
AUDIT (commit bodies carry verbatim first-error lines + re-run evidence) /
RECOVER (append-only fix-forward commits; rollback = revert, main
untouched) / COOLDOWN (DRAFT until the coordinator's land flag; goldens
re-derived at the weave).

## 4-agent hostile review
The fold ran the campaign's adversarial review lap on the merged tree; its
two confirmed BLOCKERs (the stranded `quota_tracker._IST` pin + the
dangling mutmut target) are exactly the reds-in-waiting fixed in cycle 2
above — both reproduced before fixing, both now carry regression evidence.
The body-gate trio was additionally reproduced locally against the three
gate scripts themselves (exit codes captured) before this body was written.
Honest scope: this lap's review evidence is the cycle-1 board + that
adversarial lap + the local gate reproduction — no fresh 4-agent panel was
convened for the cycle-2 fixes themselves.

## Honest envelope
Golden Rule #10: local Rust toolchain is 1.94.1 vs the CI pin 1.93.1 —
clippy 1.94 lints are a superset-class, so local-clean is the conservative
direction, but the CI re-run is authoritative. The mutmut 69/69 was
measured in a minimal py3.12 env (structlog/hypothesis/pytest + local
wheels), not the full `.[dev]` env — the mutant set is source-derived and
identical, but the first full-env CI run is the authoritative confirmation.
mypy/pyright/full golden suites are deferred to CI (no dep env in the
sandbox). A green stamp on this body means the gates' sections are present
and the cited IDs resolve — plan wisdom stays human judgement.

## Summary

- PR #1466 (slice-9 orchestrator/calendar Rust conversion + fno-math
  0.3.0 → 0.4.0) re-forwarded onto main `0a673f18a` as Event D of the
  merge campaign — a serialized run-identity re-key.
- One squash-fold commit carries the full census/conflict/gate record; two
  cycle-2 fix-forward commits green the CI board and correct the record.

## Spec / decision links

- Decision ID(s): DEC-ORCH-CALENDAR-RUST-CONVERT-001 and
  DEC-ORCH-DEAD-SCAFFOLD-DELETE-001 (carried; ledger entries #1727a /
  #1727b), plus — in code-span form because they share entry #1727a's
  header (first-ID-only mechanical capture) —
  `DEC-AUDIT-CHAIN-PARITY-GOLDEN-001` and
  `DEC-CALENDAR-VERDICT-GOLDEN-001`. Cycle-2 executes
  DEC-DATA-PARSER-DEAD-DELETE-001 (the mutmut retarget consequence) and
  DEC-TIMESTAMP-IST-SINGLESOURCE-001 (the pin removal);
  DEC-SWEEP-RUN-IDENTITY-COMPLETE-001 is the landed fold that makes this
  bump a live re-key.
- MIGRATION.md row(s): #1727a, #1727b (this PR); #1941, #1929 (executed).
- Spec section(s) touched: ADR-0019 (slice-9); the §2 Rule 4 run-identity
  law of the root instructions file (re-keyed at land, announced;
  MIGRATION.md entry #204).

## Type of change

- [x] feat — new feature (Rust kernels + wheel-surface bump; re-key at land)
- [x] fix — cycle-2 CI greening (fmt drift, mutmut retarget, stranded pin)
- [x] chore — tooling / CI / build (gate unions, prose reconciliation)

## Test plan

- CI runs the full battery on this draft; the python-side golden /
  rehearsal / type gates were deferred from the sandbox to CI (targeted
  suites — 24 + 63 tests — run green locally).
- At the land turn: the finalize-at-weave manifest above is the HARD gate.

## Operator action required (post-merge)

- Rebuild local wheels (`maturin develop --release` for fno-math) — the
  doctor wheel-freshness gate will demand 0.4.0.
- Expect the first post-land sweep to re-key (full re-sweep — the
  documented behavior at an announced re-key).
