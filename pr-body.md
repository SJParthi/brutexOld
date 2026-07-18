> **Merge-Readiness Charter (DEC-MERGE-GUARANTEE-001):** this PR obeys
> `docs/MERGE_READINESS_GUARANTEE.md` — §3 gate matrix + §4 Definition of
> Done. Never merge over a red or pending gate.

**Before:** the four observability detector/stamp laws — the event-loop
stall tiers, the worker-heartbeat hung/recovered decision, the SLA
breach/recovery edge, and the stage-stamp shape + verdict matrix — live
only as Python, so the Rust-uniformity program (ADR-0020's posture,
extended to this package by the coordinator lane brief) had no observability
lane at all. **After:** the same four laws also exist as a Rust decision
kernel (`rust/brutex-observability`) behind ONE default-OFF env flag
(`BRUTEX_OBSERVABILITY_RUST`) and ONE facade
(`brutex/observability/observability_rust.py`); with the flag unset —
today's production posture — every byte of behavior is IDENTICAL (the
Python law bodies were moved verbatim and the routers are branch-free), and
with the flag on, Rust decides while Python still materializes every
object, error and log, with a 16-sample burn-in bit-compare that
auto-reverts to the Python oracle on any divergence. **How:** the
window_ledger_rust facade template (handshake + burn-in + degrade +
refuse-poison gates), the feed-style crate shape, and a panic-containment
posture in which the crate exports its OWN `PanicException` class so the
facade's belt can actually catch it (measured: pyo3 panic classes are
per-extension statics a name-based belt can never resolve).

This lane touches NO Groww/NSE/BSE surface — zero vendor calls, zero
calendar/market-hours edits, zero rate/scope change (nothing to cite
beyond the standard trailer; the market-facing DEC citation rule is
therefore vacuously satisfied).

Branch `claude/rust-observability-lane` — 8 commits
(`8a6ccb9` crate · `05b9cd9` facade+seams · `ac72dab` tests · `d87d449`
CI wiring · `02be837` panic containment · `b323be2` fix-round-1
de-survivor + pins · `0426771` DEC-id citation sweep · `b201095`
ledger entry + blurbs), HEAD
`b201095891faca89c05b2da81615c51dc9d5a879`, base
`415e863ec98da3285304db1f82106b10d689a9bb` (== origin/main tip at branch
time). Diff: 31 files, +6,023 / −56.

## Definition of Ready (plan-first gate — DEC-DEFINITION-OF-READY-001)

### Scenarios covered

- **Happy:** flag unset (default) — all four consumer modules serve the
  verbatim-moved Python oracles through branch-free routers; flag ON with
  the wheel — burn-in compares 16 samples per law, then Rust serves
  (engaged-INFO once per process; parity suite drives all four PUBLIC
  entry points with the oracles BOMBED — correct results prove the crate
  served).
- **Edge:** NaN/±inf/−0.0/2^53/subnormal floats, threshold-boundary-exact
  values (`>=` law A vs strict-`>` law B vs `1e-9`-epsilon law C — the
  deliberate differences preserved, never harmonized), junk tier tokens,
  unknown SLA predicate fallthrough, D1 all-64 bool combinations, D2
  option-arm truth table (60 combos), lone-surrogate strings
  (UnicodeEncodeError ⊂ ValueError → degrade → oracle),
  `episodes == u64::MAX` (checked_add → ValueError degrade → the oracle's
  unbounded-int value; u64::MAX−1 increments through the crate).
- **Empty/zero:** wheel ABSENT (lazy import; `crate_unavailable` latch;
  the module imports clean wheel-free — doctest-walker safe); a wheel
  missing ANY handshake symbol (`crate_stale` — incl. a stale
  pre-containment wheel lacking the exported PanicException); blank/unset
  flag = OFF default; `remaining=0` reset = post-burn-in steady state.
- **Failure:** per-member degrade set (ValueError/RuntimeError/TypeError/
  OverflowError/AttributeError → warn-once + oracle); out-of-set KeyError
  and genuine panic classes → `crate_panic` latch + oracle (injected
  fake-crate raisers INCLUDING an instance of the real wheel's own
  PanicException — the exact class that escaped pre-fix); burn-in
  mismatch → ships the ORACLE value + ONE `autoreverted` ERROR; poisoned
  crate RETURN shapes/vocabularies refused → oracle;
  MemoryError/KeyboardInterrupt/SystemExit re-raise RAW (escape clause
  above the belt).
- **Concurrent:** thread-fuzz pins on the latch + warn-once memo (8×25
  degrade storm never latches; a panic storm latches exactly once; values
  always correct — the documented unlocked-memo exception; the
  empty-list `_DISABLED_REASON` latch keeps the FIRST racing winner).

### Failure modes

- Wheel missing/stale → loud warn-once latch, oracle serves — behavior
  identical to today, never a crash.
- Crate divergence during burn-in → oracle value ships + `autoreverted`
  ERROR — a wrong Rust value can never be served during burn-in; past
  burn-in, the parity lane's differentials are the drift net.
- Crate panic → contained by the belt (the crate's own exported class
  resolves FIRST) → `crate_panic` latch → oracle. No natural panic path
  remains post-audit (the one overflow-capable op is checked); containment
  proven by injection — disclosed.
- Poisoned crate return (wrong type/shape/token) → refused, oracle serves —
  a junk verdict token can never crash `StampVerdict(token)`.
- OOM → re-raises RAW from either arm, landing in the caller exactly as
  the pure-Python path does today (stated + pinned).
- Junk flag value → OFF + ONE `flag_unrecognised` WARN (never silently
  armed).

### Real-run proof

Executed on this branch (verbatim tails recorded in the lane handoff):

    cd rust/brutex-observability && cargo test --release --locked
    # → 52 green (edge_matrix 42 · proptest_detector 4 · proptest_stamp 3
    #    · source_digest_parity 3); identical 52 with --features python
    .venv/bin/maturin develop --release
    # → 📦 Built wheel for abi3 Python ≥ 3.12 · 🛠 Installed brutex-observability-0.1.0
    .venv/bin/python -m pytest tests/unit/test_observability_rust.py \
        tests/integration/test_observability_rust_parity.py -q
    # → 171 passed (unit 161 + integration 10; wheel present;
    #    triple-run random-seed stable)
    BRUTEX_OBSERVABILITY_RUST=1 .venv/bin/python -m pytest \
        tests/unit/test_observability_rust.py \
        tests/integration/test_observability_rust_parity.py \
        <the four consumer-module suites> -q
    # → flag-ON: 302 passed — the parity suite drives the four PUBLIC
    #    entry points with the Python oracles monkeypatched to BOMBS —
    #    correct results = the crate served; observability_rust.engaged
    #    INFO witnessed

Plus the wheel-ABSENT arm (meta_path blocker plugin): 148 passed,
14 skipped — facade coverage 283/92, 0 missed, 100.00% on BOTH arms; the
recorded ad-hoc 20k-sample-per-law differentials (laws A/B/C vs the
oracle at `remaining=0` — 0 bitwise mismatches); and the CI-replica
mutmut run over the wired pair: 270/270 killed, 0 survived, cache tally
`{'ok_killed': 270}`, `mutmut results` EMPTY, the workflow's own results
grep GREEN.

### Rollback plan

- **Lever 0 (already engaged):** the flag defaults OFF — production
  behavior is byte-identical with this PR merged and nothing set.
- **Lever 1 (in-process):** any burn-in mismatch / panic / guard failure
  auto-reverts to the Python oracle for the process (ships the oracle
  value, never the suspect one).
- **Lever 2 (operator):** ensure `BRUTEX_OBSERVABILITY_RUST` is unset/0 —
  per-call read, effective immediately incl. spawn workers.
- **Lever 3 (full):** `git revert` of the 8 commits — the crate + facade +
  workflow are additive files; the four seam edits are verbatim-move
  extractions, so reverting restores the pre-seam law bodies exactly; no
  data/schema/state migration exists to unwind.

### Out of scope

- `run_tripwires` (assessed, deferred — DEC-RUST-OBSERVABILITY-TRIPWIRES-002,
  ~1.5–2 sessions on its own).
- A CPython-`json.loads` parse twin (DEC-RUST-OBSERVABILITY-STAMP-PARSE-003);
  `json.loads` + the canonical stamp dump + `content_hash_of` (hashlib —
  I/O not decision, a negative-payoff port) stay Python.
- The ci.yml `rust-brutex-observability` crate job + `ci-ok.needs`
  membership and the weekly `rust-mutation-brutex-observability`
  cargo-mutants job + mutation path triggers (required-checks / weekly
  job-graph surface — deferred wiring, deliberate; the in-crate
  `.cargo/mutants.toml` scope, the python mutmut pair and its verified
  270/270 zero-survivor evidence ship HERE — the deferral is wiring-only,
  never a coverage hole).
- The sibling facades' name-based-only panic belts (documented, not
  edited — a house-wide fix breaks their mirror pins, own DEC needed) and
  the sibling pairs' latent PEP-563 annotation-mutant exposure
  (executed-proven in lake_rust_stats.py:72 / today_completeness.py:373;
  candidates in lake_rust_locks / lake_rust_resample / feed replay —
  flagged to the coordinator, other lanes' files).
- Any default flip of the flag (own DEC + soak; oracle deletion only via
  the H.2 rail, DEC-OBSERVABILITY-ORACLE-DELETE-001: parity → flip DEC →
  soak → delete).
- Deleting the census-verified dead files `pull_eta.py` / `throttle.py`
  (the removal track — own DEC, never a rider).
- The structlog→tracing endgame lane (the coordinator H.1 map).

---

## Summary

- NEW standalone crate `rust/brutex-observability` hosting the four pure
  detector/stamp laws (A loop_watchdog tiered stall machine · B watchdog
  heartbeat decide · C sla predicate+hysteresis edge · D stage_stamp
  shape-index + verdict matrix) as scalars-only pyfunctions.
- ONE default-OFF per-call flag `BRUTEX_OBSERVABILITY_RUST` + ONE facade
  `brutex/observability/observability_rust.py` (window_ledger_rust
  template; local tristate/panic-belt twins equality-pinned vs
  lake_rust_render; all-or-nothing handshake incl. the crate-exported
  PanicException; NO polars version guard — documented polars-free
  deviation; per-law 16-sample burn-in + ship-the-oracle autorevert).
- Verbatim-move oracles + branch-free routers in the four consumer
  modules — flag OFF byte-identical (bomb-spy pinned); the four existing
  ci.yml 100% stanzas green over UNMODIFIED test lists.
- Panic containment: the crate exports its OWN PanicException (the belt
  resolves it first); the one overflow-capable op is `checked_add` →
  ValueError degrade → the oracle's unbounded-int value.
- Fix round 1 folded: the mutation pair de-survivored to a verified
  270/270 killed / 0 survivors (the PEP-563 annotation-mutant class + the
  novel phantom-cache-row CI-gate-breaker class closed by RESTRUCTURE,
  never a pragma); round 2 CLEAN.
- CI: mutation.yml mutmut pair + the NEW non-required path-triggered
  `observability-rust-parity.yml` lane.
- DEC-id citations swept to the resolving handle
  DEC-RUST-OBSERVABILITY-001; MIGRATION.md #1954 (provisional) +
  CLAUDE.md blurbs recorded.

## Spec / decision links

- Decision ID(s): DEC-RUST-OBSERVABILITY-001 (this PR) · named follow-ups
  DEC-RUST-OBSERVABILITY-TRIPWIRES-002, DEC-RUST-OBSERVABILITY-STAMP-PARSE-003
- MIGRATION.md row(s): #1954 (PROVISIONAL — coordinator-reservation-map
  number; renumber-at-landing per DEC-LEDGER-RENUMBER-001)
- Spec section(s) touched: none (ADR-0020 posture extended by the
  coordinator lane brief; no TRACK1 spec surface)

## Gate table (exact counts, all executed on this branch)

| Gate | Result |
|---|---|
| cargo fmt --check / clippy `-D warnings` (default + `--features python`) | clean |
| `cargo test --release --locked` (default AND `--features python`) | 52 green ×2 |
| maturin abi3-py312 wheel | built + installed |
| new suites, wheel PRESENT (unit + integration) | 171 passed (161 + 10); triple-run seed-stable |
| new suites, wheel ABSENT (meta_path blocker) | 148 passed, 14 skipped |
| facade coverage (both arms) | 283 stmts / 92 branches, 0 missed, 100.00% |
| flag-ON parity pass (both new suites + the four consumer suites) | 302 passed |
| ci.yml stanzas (UNMODIFIED test lists) | sla 92/16 (32) · watchdog 71/8 (25) · loop_watchdog 190/46 (52) · stage_stamp 152/42 (22) — all 100.00% |
| 28 protected consumer/law files, one run, zero edits | 673 passed, 1 skipped (pre-existing env-only `cma`) |
| `coverage_gate_discovery.py --base origin/main` | 5 touched module(s) at 100% |
| ruff / ruff-format | clean |
| `mypy --strict brutex` | 0 new (1 pre-existing env-only `cma` error, baseline-identical) |
| pyright (touched files) | 0 errors (62 warnings, all house `reportPrivateUsage`) |
| conventional commits | all 8 green |
| mutation | pair wired in mutation.yml AND de-survivored: 270/270 killed / 0 survived / 0 suspicious / 0 untested on the CI-replica shape (results grep GREEN) |
| differentials | hypothesis at `remaining=0` + 20k/law ad-hoc (A/B/C, 0 bitwise mismatches) + D1 all-64 + D2 truth table, oracles BOMBED |
| ledger + doc pins | validate_ledger_numbering.py green · test_claude_md_branch_consistency.py 10 passed · validate_citation_accuracy.py green |

## Type of change

- [x] feat — new feature (default-OFF; flag-unset behavior byte-identical)

---

## Z+ 15-row matrix (100% everything — charter §C)

| Demand | This PR's proof | Real-time check |
|---|---|---|
| 100% code coverage | facade 283/92, 0 missed, 100.00% BOTH arms; 4 consumer stanzas 100.00% over UNMODIFIED lists; discovery gate 5/5 modules at 100% | coverage_gate_discovery.py + ci.yml stanzas |
| 100% audit coverage | N/A — no material state change; the facade's literal event vocabulary (fallback/rust_path_disabled/autoreverted/engaged/burn_in_complete) is the observability record; no audit-JSONL surface touched | grep observability_rust. events |
| 100% testing coverage | unit (161) + integration parity (10) + property (cargo proptest ×7 + hypothesis differentials) + golden (source-digest parity + exact-value edge matrix) + thread-fuzz; stress/bench N/A — no hot path (FFI-hop cost disclosed); mutation: 270/270 zero-survivor on the wired pair | pytest + cargo test + mutation.yml |
| 100% code checks | ruff + ruff-format clean · mypy --strict 0 new · pyright 0 errors · cargo fmt + clippy -D warnings (both feature sets) · deny.toml ships | pre-push + CI |
| 100% code performance | every law O(1) per check on BOTH arms; the ON arm ADDS an FFI hop — disclosed, default OFF; no O(1) gate touched | ADR-0020 banner honesty |
| 100% monitoring | the five literal facade events; no metric surface changed | grep brutex/observability/observability_rust.py |
| 100% logging | structlog throughout; warn-once memo prevents log storms (thread-fuzz pinned) | grep _warn_once |
| 100% alerting | N/A — no new CRITICAL condition; alert paths byte-identical flag-OFF | flag-OFF bomb-spy pins |
| 100% security | no secrets; pyo3 0.29 abi3-py312 sibling-identical pin; Cargo.lock committed; deny.toml; no new network/exec surface | gitleaks + cargo-deny |
| 100% security hardening | zero new noqa; crate `overflow-checks = true`; the one overflow-capable op checked_add-guarded | grep noqa + Cargo.toml |
| 100% bug fixing | the IMPL-2 material finding (panic-belt escape — per-extension PanicException, measured) fixed STRUCTURALLY in 02be837 with the escape pin FLIPPED to a containment pin; the round-1 confirmed MAJOR (mutation-gate survivors) fixed in b323be2 (270/270, restructures never pragmas) | tests/unit/test_observability_rust.py |
| 100% scenarios covering | degrade/panic/escape/poison/burn-in/thread-fuzz matrices × {wheel-free, wheel-present} × {flag OFF, flag ON} | the DoR Scenarios section |
| 100% functionalities covering | every new public fn (5 crate laws + 5 routed fns + reset seam + shim handshake) direct-tested; contract pins (shim ≡ EXPECTED_BUILD.json ≡ `_CRATE_API` ⊆ shim; signature(check_once) ≡ signature(oracle)) | AST/JSON-parsed pins |
| 100% code review | the staged adversarial chain + the two review rounds in the 4-agent section below (round 2 CLEAN) | this body |
| 100% extreme check | mirror pins (tristate sets + panic-belt fold vs lake_rust_render) + the crate_stale symbol-handshake teeth prevent silent drift/regression | tests + handshake |

## Z+ 7-row resilience matrix (charter §C)

| Demand | Honest envelope | This PR's proof |
|---|---|---|
| Zero ticks lost | N/A — BruteX is backtest-only (Track 1) | confirmed — no feed surface touched |
| WS never disconnects | N/A — no WebSocket in Track 1 | confirmed |
| Never slow/locked/hanged | no locks added; the facade's unlocked memo is the documented exception (worst case: duplicate log / extra burn-in compares — never a wrong value; the empty-list latch keeps the first racing winner) | thread-fuzz pins |
| QuestDB never fails | N/A — BruteX uses DuckDB + LMDB; neither touched | confirmed |
| O(1) latency | per-check O(1) both arms; ON arm's FFI hop disclosed (no speedup claimed — ADR-0020 banner) | banner + design |
| Uniqueness + dedup | N/A — no dedup surface touched | confirmed |
| Real-time proof | the gate table above + verbatim tails in the lane handoff (scratchpad, quoted in the ledger entry) | this PR body |

## Z+ 7-layer audit

| Layer | This PR's status |
|---|---|
| DETECT | per-law 16-sample burn-in bit-compare + the parity lane's hypothesis differentials + the two lake_rust_render mirror pins detect any crate/oracle/vocabulary drift |
| VERIFY | the flag-ON parity suite (302 passed) drives the four PUBLIC entry points with the Python oracles BOMBED — correct results prove the crate served end-to-end; engaged-INFO witnessed; round-2 independent claims verification (5/5 mutant spot-kills, coverage re-measured, OFF-arm bomb inertness anti-vacuously proven) |
| RECONCILE | additive only — flag-OFF byte-identical (bomb-crate spy + law-A OK→OK object-identity pins); zero edits to the 28 protected test files |
| PREVENT | refuse-poison input/output gates + the crate_stale all-or-nothing symbol handshake (a stale wheel can never engage) + the flipped escape→containment pin (any future belt/crate change is a conscious flip) + the 270/270 zero-survivor mutmut pair (phantom-row + annotation-mutant classes structurally closed) |
| AUDIT | MIGRATION.md #1954 (provisional) + the literal greppable event vocabulary + this PR body's marker sections |
| RECOVER | default-OFF flag (per-call read) · in-process ship-the-oracle autorevert latch · `git revert` of 8 additive/verbatim-move commits |
| COOLDOWN | ships INERT — a default flip requires its own DEC + a soak window; oracle deletion only via the H.2 rail (DEC-OBSERVABILITY-ORACLE-DELETE-001: parity → flip DEC → soak → delete) |

## 4-agent hostile review

Actual adversarial rounds run in this lane (staged multi-worker chain +
two formal review rounds + the independent workspace verifier):

| Agent / round | Lens | Findings | Severity |
|---|---|---|---|
| Census + pattern-contract workers (read-only, pre-design) | cite/path — every law line, consumer, ci stanza and facade-template clause verified file:line against the tree | 0 defects; produced the verified import-cycle evidence that forced the local-twin decision | — |
| Extraction-spec design pass | correctness-by-design — bit-for-bit edge semantics per law (`>=` vs strict-`>` vs epsilon; NaN/±0.0/clamp argument order; unknown-token fallthrough), refuse-poison domains, the D1 droppability clause | design-level constraints, all carried into implementation | — |
| IMPL-2 test worker (hostile differential battery) | correctness/race — 20k-per-law differentials, thread-fuzz, escape pins | **1 MATERIAL finding: the panic belt could NEVER catch this crate's own PanicException (per-extension static — measured, pinned as an escape)** | MAJOR-class — fixed (02be837) |
| IMPL-3 containment worker | fix + re-verify — crate exports its own class, belt resolves it first, checked_add kills the panic source; escape pin flipped to containment; full gate battery re-run | finding closed structurally; 0 residual (siblings' narrowing documented, out of scope) | closed |
| **Review round 1** — 4 hostile lenses + refuters (13 agents) | correctness / claims / teeth / mutation-gate | verdicts SHIP / FIX-FIRST / SHIP / SHIP; **1 confirmed MAJOR: the enrolled mutmut pair carried real survivors on the CI-replica shape**; 1 finding set refuted; MINOR/NIT folds (poison-matrix gate pins, honest-limit comments, docstring self-contradiction) | MAJOR — fixed (b323be2: 270/270 killed / 0 survivors; PEP-563 annotation-mutant + phantom-cache-row classes closed by RESTRUCTURE, never a pragma) |
| **Review round 2** — 2 lenses + independent claims verification | re-review of the fix commit + claims audit | CLEAN — SHIP / SHIP, zero confirmed findings; 5/5 mutant spot-kills reproduced, coverage re-measured 100.00%, OFF-arm bomb inertness anti-vacuously proven | — |
| **workspace-health** (independent verifier) | whole-repo gates | HEALTHY — obs family 714 · engine 3682 · dashboard 3010 · consumer suites 382 · chaos 23 · property 18; the full ~25k-unit sweep time-boxed at ~8% sampled clean (disclosed as a sample) | — |

This PR opens as a DRAFT; marking ready follows the ship-pr flow once CI
is green on the pushed head.

## Honest envelope (Golden Rule #10)

- ✅ REAL: everything in the gate table (executed on this branch, tails
  recorded); flag-OFF byte-identity (bomb-spy + object-identity pins);
  the panic containment (measured per-extension class + injected
  containment proofs); the 270/270 zero-survivor mutmut evidence; the
  20k/law differentials; the two review rounds + workspace-health.
- 🟡 PARTIAL: genuine-panic containment is proven by INJECTION — no
  natural panic path remains post-audit (an honest substitution,
  disclosed); crate-side mutation coverage is the in-crate
  `.cargo/mutants.toml` scope — the weekly cargo-mutants JOB and the
  required ci.yml crate job are deliberately deferred wiring (the
  required-checks surface is untouched in this PR); the untimed mutmut
  pair carries a killed-but-slow OK_SUSPICIOUS flake class on a loaded
  runner (observed only under deliberate concurrent load; quiet-box
  requalification killed both rows fast — disclosed for the weekly gate);
  ONE lane flag means a poisoned crate latches all five laws together;
  flag-ON law-A verdicts carry an EQUAL-not-identical recomposed state
  (no consumer relies on identity — verified); the exact-type gates make
  the routed domain deliberately narrower than the Python domain (the
  oracle serves the rest); entry #1954 is PROVISIONAL
  (coordinator-reservation-map assignment pending; renumber-at-landing
  law, chain to be recorded in-entry); two T27 partition deviations
  recorded for coordinator reconcile (standalone crate not
  `rust/brutex-core`; flag `BRUTEX_OBSERVABILITY_RUST` not
  `BRUTEX_OBS_RUST`).
- 📋 ROADMAP: DEC-RUST-OBSERVABILITY-TRIPWIRES-002 ·
  DEC-RUST-OBSERVABILITY-STAMP-PARSE-003 · ci.yml crate job +
  ci-ok.needs + the weekly cargo-mutants job + path triggers · the
  structlog→tracing endgame lane (H.1) · the pull_eta/throttle dead-file
  removal track · the sibling annotation-mutant exposure (coordinator) ·
  a default flip + oracle deletion strictly via the H.2 rail
  (DEC-OBSERVABILITY-ORACLE-DELETE-001).
- Payoff statement (ADR-0020 banner, verbatim): **expected payoff ≈ 0 —
  the conversion is for uniformity, not speed.** The ON arm adds an FFI
  hop; no speedup is claimed anywhere.

## Decision ID

DEC-RUST-OBSERVABILITY-001 — MIGRATION.md #1954 (PROVISIONAL;
coordinator-reservation-map-assigned, renumber-at-landing per
DEC-LEDGER-RENUMBER-001; the id adopted from the coordinator partition's
T27 row, with the in-code citations swept to it in `0426771` — recorded
T27 deviations: standalone crate not `rust/brutex-core`; flag
`BRUTEX_OBSERVABILITY_RUST` not `BRUTEX_OBS_RUST`).

## Operator action required (post-merge)

- None (the lane ships INERT; nothing to install, nothing to set).
