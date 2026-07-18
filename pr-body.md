# Pull Request — BruteX

> **Merge-Readiness Charter (DEC-MERGE-GUARANTEE-001):** this PR obeys
> `docs/MERGE_READINESS_GUARANTEE.md` — the §3 gate matrix + §4 Definition of
> Done. Never merge over a red or pending gate. Per Golden Rule #10, partial /
> roadmap / out-of-scope rows are disclosed in the Honest envelope below.

## Definition of Ready (plan-first gate — DEC-DEFINITION-OF-READY-001)

### Scenarios covered

- **Happy:** flag ON + healthy wheel — all four kernels engage
  (`rust_stats.engaged` per fn), results bit-equal to the Python tails
  (goldens + hypothesis differential, `struct.pack('<d', …)` compares).
- **Default:** flag unset/OFF — byte-identical to merge-base behavior; the
  crate is never invoked (bomb-kernel spy pins on every module).
- **Edge:** ±0.0, subnormals (5e-324), 1e±300 magnitudes, erf branch-point
  ±3-ULP brackets, `n_trials` up to 2^62 (the `StatisticsError` parity arm),
  PSR radicand-zero/negative raise parity, WFO 1e300 overflow → the
  fail-safe-None Sharpe on BOTH arms, PBO odd-`n_splits`/short-rows raises
  BEFORE the seam.
- **Empty/zero:** `n_trials == 1` (returns before the seam — engagement
  correctly not expected), zero variance, zero-length walks (`no walks`
  raise on both arms), PBO all-equal logits.
- **Failure:** junk env token (warn-once, OFF), missing/stale wheel (import
  guard → oracle), poisoned wheel returning wrong types / wrong-arity tuples /
  float subclasses (exact-type result gates refuse → oracle), burn-in
  bit-mismatch (autorevert latch + `rust_stats.autoreverted`, oracle value
  served), kernel raising (per-call refusal → oracle), MemoryError re-raises
  raw.
- **Concurrent:** the seam's pre-existing process-local latch/memo model is
  unchanged (PR-1 contract); no new shared mutable state beyond the
  registered budgets.

### Failure modes

| Failure | System behavior |
| --- | --- |
| Wheel absent / stale handshake | Import guard refuses; oracle serves; warn-once (`wheel_missing` / `wheel_stale`) |
| Junk `BRUTEX_STATS_RUST` value | Treated OFF + one `flag_unrecognised` warn — never a crash |
| Kernel raises (any exception) | Per-call refusal → oracle; `MemoryError` re-raises RAW |
| Burn-in bit mismatch | The ORACLE value ships; per-function autorevert latch; `rust_stats.autoreverted` ERROR |
| Byzantine result (wrong type / arity / float subclass / out-of-band count) | Exact-type result gates (`_valid_float_result` / `_valid_pbo_result`) refuse → oracle |
| Hostile args (NaN threshold, int subclass, non-finite, out-of-i64 count) | Exact-type arg gates refuse BEFORE the FFI → oracle owns the call on both arms |
| PBO combination count > 2,000,000 | Kernel `Err` → oracle (never an unbounded alloc under `panic = "abort"`) |
| numpy changes its internal pairwise order (future release) | The WFO burn-in bit-compare fails → autorevert to the oracle — drift is structurally impossible to ship |

### Real-run proof

Verbatim gate outputs (this workspace, wheel built via maturin):

```
$ cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings   (rust/fno-math)
fmt: clean
    Finished `dev` profile [unoptimized + debuginfo] target(s)   (clippy -D warnings: zero warnings)

$ cargo test --release --locked   (rust/fno-math)
test result: ok. 636 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out

$ pytest tests/unit/test_rust_stats.py --cov=brutex --cov-branch -q && coverage report --include="*/brutex/metrics/rust_stats.py" --fail-under=100
210 passed
brutex/metrics/rust_stats.py     348      0    138      0   100.00%

$ (same ci.yml form) deflated_sharpe / pbo / wfo
39 passed  → brutex/metrics/deflated_sharpe.py   56  0  20  0  100.00%
20 passed  → brutex/metrics/pbo.py              115  0  46  0  100.00%
37 passed  → brutex/metrics/wfo.py               84  0  26  0  100.00%

$ pytest goldens + property + facade + neighbor suites (14 files)
399 passed

$ ruff check … && ruff format --check …   (all 12 touched Python files)
All checks passed!
12 files already formatted

$ mypy --strict brutex
Found 10 errors in 7 files → stash-diffed vs merge-base 98b9a732: 0 NEW
(9 pre-existing error lines identical; the 10th line is the count header)

$ pyright   (all 11 touched Python files)
0 errors, 0 warnings, 0 informations

$ BRUTEX_STATS_RUST=1 python  (end-to-end, real wheel — engagement + values)
[info] rust_stats.engaged fn=sr_max
[info] rust_stats.engaged fn=psr
DSR chain (kernels engaged): 9.216985297211266e-08 0.5215199719891647 0.9404331722600482
[info] rust_stats.engaged fn=pbo
PBO: 0.9
[info] rust_stats.engaged fn=wfo
WFO: 0.5 0.5 0.4
   → every value bit-equal to the merge-base-frozen goldens.

$ scripts/ci/validate_citation_accuracy.py
✅ every extracted citation is accurate

$ pytest tests/unit/test_claude_md_branch_consistency.py -q
10 passed
```

### Rollback plan

- The flag ships **OFF** — production behavior is already the rollback state.
  `BRUTEX_STATS_RUST=0` (or unset) is byte-identical to merge-base
  (bomb-kernel-pinned).
- Runtime self-rollback: any burn-in bit mismatch autoreverts the affected
  function to its Python oracle for the process, shipping the oracle value.
- Full revert: `git revert 12ecea58` — one atomic commit; the Python tails
  were never deleted, so no data/artifact migration exists to unwind. Run
  identity is untouched (no `canonical_trade_params` key), so no re-sweep.

### Out of scope

- **Flag default flip** (needs its own DEC with measured evidence — the
  #1532 pattern). No speedup is claimed in this PR.
- **`metrics/lockbox.py`** (the stretch) — DROPPED + disclosed: its
  equity/drawdown path rides `np.cumsum` / `np.maximum.accumulate`
  (further numpy-INTERNAL element orders) on a cold once-per-run path —
  per the never-ship-drift law it stays pure Python.
- **`metrics/spa.py` + `metrics/monte_carlo.py`** — seeded numpy PCG64
  streams; bit-replication of the RNG consumption order is out of this
  lane (pre-declared OUT).
- **`metrics/cpcv.py` remainder** — the fold kernel shipped in PR-1; the
  split/purge machinery stays Python.
- **Open-PR-owned files, ZERO edits:** `metrics/compute.py` (#1605),
  `metrics/cascade.py` (#1554/#1580), `metrics/types.py` +
  `ranking/phrases.py` (#1574), `ranking/__init__.py` (#1478/#1544),
  `ranking/tiers.py` / `ranking/temporal.py`. `ranking/overfit.py` is
  byte-untouched (transitive DSR consumption — golden-pinned).

---

## Summary

- Slice 6 PR-2 (DEC-METRICS-RUST-STATS-002): the metrics **tails** — PSR +
  expected-max Sharpe (SR_0), the PBO/CSCV fold, and the WFO walk pass-count —
  are re-hosted on `rust/fno-math` (NEW `src/stats/{norm,deflated_sharpe,pbo,wfo}.rs`)
  behind the SAME landed default-OFF `BRUTEX_STATS_RUST` seam
  (per-function burn-in bit-compare + autorevert; the Python tails retained
  forever as the live oracles). `brutex/ranking/overfit.py` is served
  TRANSITIVELY with zero edits.
- **Bit-exact by proof, not assertion:** the ONE sanctioned same-symbol libm
  `erf` FFI (fdlibm transcription measurement-REFUTED; CPython `math.erf`
  proven the platform libm symbol — 0 mismatches over 3.5M points), the AS241
  `inv_cdf` transcription (0 mismatches / 2.5M points), the numpy-pairwise-
  REPLICATED WFO sum (0 mismatches / 4,000 trials; burn-in is the fence), and
  STRUCTURAL op-for-op PSR/SR_0/PBO transcriptions.
- Handshake extended the same-commit way: fenced `lib.rs` appends,
  `EXPECTED_BUILD.json` 17 → 21 symbols, `.pyi`, the frozen `brutex.math`
  facade golden +4 (the documented deliberate-surface-change path).

## Spec / decision links

- Decision ID(s): **DEC-METRICS-RUST-STATS-002** (extends
  DEC-METRICS-RUST-STATS-001)
- MIGRATION.md row(s): **#1977** (PR-1: #1928)
- Spec section(s) touched: none (Stage-3/4 statistics implementation detail;
  outputs bit-identical — the frozen goldens are the contract)

## Decision ID

**DEC-METRICS-RUST-STATS-002** — MIGRATION.md **#1977** (2026-07-18; entry
number = the live ceiling #1953 + clear of the in-flight #1974–#1976 band;
the renumber-at-landing law stands per DEC-LEDGER-RENUMBER-001).

**Disclosed deviation from the lane brief:** the brief named the flag
`BRUTEX_METRICS_TAILS_RUST`; this PR extends the ALREADY-LANDED
`BRUTEX_STATS_RUST` seam instead (Golden Rule #11 COMMON — one flag, one
burn-in law, one latch registry for every slice-6 function). A rename is a
one-constant change if the coordinator overrides.

## Type of change

- [x] feat — new feature (default-OFF; flag-OFF byte-identical)

## Z+ 15-row matrix

| # | Row | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | Correctness (bit-exact vs oracle) | ✅ | goldens (merge-base-frozen literals, both flags) + hypothesis differential (bit + exception parity) + burn-in bit-compare at runtime |
| 2 | Flag-OFF byte-identity | ✅ | bomb-kernel spy pins per module; extracted oracles are verbatim token-for-token moves; existing suites pass UNMODIFIED |
| 3 | Reliability (kernel fault → oracle) | ✅ | poisoned-wheel / raising-kernel / wrong-type result tests; MemoryError re-raises raw |
| 4 | Recoverability (autorevert) | ✅ | burn-in-mismatch tests assert the ORACLE value ships + the latch short-circuits later calls |
| 5 | Observability | ✅ | `rust_stats.engaged` / `rust_stats.fallback` (warn-once, bounded contexts) / `rust_stats.autoreverted`; AST structural pin over every log call |
| 6 | Coverage 100% line+branch | ✅ | 348/138 · 56/20 · 115/46 · 84/26 — all 0 missed, the ci.yml way |
| 7 | Type safety | ✅ | mypy --strict 0 new; pyright 0 errors on touched files |
| 8 | Lint/format | ✅ | ruff check + ruff format --check clean (12 files) |
| 9 | Rust gates | ✅ | fmt --check · clippy --all-targets -D warnings · test --release --locked 636 green |
| 10 | Mutation scope | ✅ | `src/stats/*.rs` already inside the fno-math `examine_globs`; `rust_stats.py` + `deflated_sharpe.py` already in the weekly mutmut pairs (runners now execute the new tests) |
| 11 | No-drift law | ✅ | any unprovable function EXCLUDED (lockbox dropped); the WFO REPLICATED claim is burn-in-fenced |
| 12 | Scope lock | ✅ | zero edits to the open-PR-owned exclusion files; `ranking/overfit.py` byte-untouched |
| 13 | Run identity | ✅ | untouched — no `canonical_trade_params` key; frozen run-identity goldens unaffected (flag OFF default) |
| 14 | Security | ✅ | ONE scoped `unsafe extern "C"` (libm erf) under crate-level `deny(unsafe_code)` + SAFETY argument; zero Cargo.toml/Cargo.lock changes; no secrets; no `print` |
| 15 | Docs/ledger | ✅ | MIGRATION.md #1977 + CLAUDE.md header/§1/§3; citation-accuracy scanner green; branch-consistency pins green |

## Z+ 7-row resilience matrix

| # | Fault injected | Result |
| --- | --- | --- |
| 1 | Wheel absent (sys.modules poison) | Oracle serves; warn-once; no crash — pinned per binding |
| 2 | Wheel stale (handshake mismatch) | Import guard refuses; oracle serves — PR-1 machinery, re-exercised |
| 3 | Kernel raises mid-call | Per-call refusal → oracle; the call still returns the oracle value |
| 4 | Kernel returns bit-different value (burn-in window) | Oracle value ships; autorevert latch; ERROR logged — pinned for psr/sr_max/pbo/wfo |
| 5 | Kernel returns wrong type / wrong arity / float subclass | Exact-type result gates refuse → oracle — parametrized per shape |
| 6 | Hostile args (NaN / int subclass / 2^63 / Decimal / np.float64) | Exact-type arg gates refuse BEFORE the FFI; the oracle's own semantics own both arms — the wfo↔cpcv gate cross-pin proves gate-law agreement |
| 7 | PBO combination explosion (n_total > 2,000,000) | Kernel Err → oracle; no unbounded alloc under `panic = "abort"` |

## Z+ 7-layer audit

| Layer | Status |
| --- | --- |
| 1 · Unit | 210 (rust_stats) + 39 + 20 + 37 (module dispatch arms) — every binding arm ON/OFF/junk/burn-in/poison/latch |
| 2 · Integration | end-to-end flag-ON smoke through the PUBLIC functions with real-wheel engagement (verbatim output above); neighbor suites 383+ unmodified-green |
| 3 · Property | NEW hypothesis differential (10) — ON-vs-OFF bit parity + exception parity over hostile corpora; in-crate proptest differentials vs naive twins |
| 4 · Golden | NEW merge-base-frozen goldens (12) — literals captured from `git show 98b9a732:` sources, both flags, anti-vacuous engagement asserted; the facade surface golden +4 |
| 5 · Bench | pre-existing `test_rust_stats_speedup.py` passes; NO speedup claimed (flag OFF — a flip is its own DEC) |
| 6 · Stress | the property corpora carry the hostile-magnitude load (1e±300, subnormals, 2^62 counts); burn-in exercised per example |
| 7 · Mutation | Rust: `src/stats/*.rs` in the cargo-mutants examine_globs (weekly matrix); Python: rust_stats + deflated_sharpe mutmut pairs pre-wired, runners now include the new tests (pbo/wfo not in the loop — pre-existing scope, disclosed) |

## 4-agent hostile review

Four independent hostile-reviewer subagents were run over the committed diff
(12ecea58 vs merge-base 98b9a732), one adversarial lens each. Verdicts:

| Lens | Verdict | Findings + disposition |
| --- | --- | --- |
| 1 · Rust numerical correctness / bit-exactness | **SHIP** | Every bit-exactness claim UPHELD by executed differentials against the real oracles (>1M hostile samples total, 0 mismatches): the wheel's `erf` is `UND erf@GLIBC_2.2.5` resolved from the same `libm.so.6` CPython uses (500k-sample identity; LTO cannot inline a dynamic extern; single symbol version); AS241 coefficients byte-identical to statistics.py with the C-accelerator FMA concern REFUTED empirically (200k samples incl. 1e-300 tails); PSR/SR_0 term-for-term with constant bit-pins; PBO enumerator == itertools.combinations (3,000 configs, per-logit bits) with `binomial_checked` proven panic-free under overflow-checks; WFO pairwise replica == numpy 2.5.1 over 6,000 hostile trials + 20,000 nextafter boundary stresses; NO reachable panic path under `panic=abort` (guard-by-construction + hostile probing); astronomical-`n_trials` exception parity confirmed end-to-end. 2 NITs: the REPLICATED-claim K=16 burn-in bound on a hypothetical divergent-SIMD platform (already the disclosed Honest-envelope residual — flagged for the record) and one pbo.rs comment over-claim ("exact for any real N") — FOLDED to "exact for n_strat < 2^53" in the review-fold commit. |
| 2 · Python seam + dispatch wiring | **SHIP** | All 8 hard laws PASS (flag-OFF byte-identity — verbatim token-for-token oracle moves confirmed line-for-line vs `git show` merge-base; oracle == burn-in comparandum proven bit-identical; append-only law — cpcv binding + shared helpers byte-untouched; MemoryError raw; no import cycle; PBO logit ORDER burn-in-fenced positionally). 1 non-live MINOR: WFO `test_starts`/`test_stops` bypass the `_count_as_i64` gate (internal, non-negative, narrow-catch-covered; unreachable-negative residual disclosed in the binding docstring) — accepted as defense-in-depth debt, documented. 2 cosmetic NITs (a marginally-over-strong "by construction" comment clause; the deliberate NaN-admitting result gate is documented-correct) — accepted. |
| 3 · Test + coverage honesty | **SHIP** | 0 BLOCKER/MAJOR/MINOR. Golden provenance PROVEN by hermetic merge-base execution — all 16 literals reproduce bit-for-bit from `git show 98b9a732:` sources (not a same-tree echo); engagement anti-vacuous per example; no new pragmas/suppressions; the four modules re-measured 100.00% by the reviewer independently; zero pre-existing test deletions beyond the four sanctioned reconciliations. 3 NITs, all pre-existing/cosmetic: (1) deflated_sharpe/pbo have no DEDICATED ci.yml stanza (their 100% rides the discovery gate + the exact command form — disclosed here as a follow-up candidate), (2) the structural-pin census floor stays >= 12 while the census is 37 (per-call teeth carry the load), (3) one golden asserts values without its own engagement capture (covered by the transitive golden + unit dispatch tests). |
| 4 · Scope compliance / unsafe / ledger accuracy | **SHIP** | Hard exclusions verified byte-untouched (incl. `ranking/overfit.py`); the one `unsafe extern "C"` scoped + SAFETY-documented under crate-level `deny(unsafe_code)`; zero Cargo.toml/Cargo.lock changes; registrations append-only; ledger figures spot-checked against the tree. 1 MINOR: the 2.5M inv_cdf probe-corpus figure lived only in the ledger — FOLDED: the figure + method are now committed in norm.rs's `std_norm_inv_cdf_checked` docs and the ledger's erf/inv_cdf provenance wording was anchored to the committed docs (the review-fold commit). |

Review-fold commit: two doc-precision folds — the scope-docs MINOR
(inv_cdf probe-corpus provenance anchored into norm.rs + the ledger) and the
lens-1 pbo.rs comment NIT ("exact for any real N" → "exact for n_strat <
2^53"). Every other finding was accepted-as-disclosed (NITs / pre-existing
items), recorded above and in the Honest envelope. Cargo + citation gates
re-run green after the fold.

## Honest envelope

- **The same-symbol erf law is PER-PLATFORM.** Linux glibc and macOS
  libsystem_m each bind their own libm; the ±3-ULP branch-boundary golden
  pins + the runtime burn-in re-prove/fence the equality wherever the suite
  runs. A platform whose CPython does NOT use libm `erf` would fail the
  goldens LOUDLY (and autorevert at runtime) — never drift silently.
- **The WFO claim class is REPLICATED, not STRUCTURAL** — it mirrors
  numpy's internal pairwise reduction order. A future numpy release changing
  that order is caught by the burn-in (autorevert), not by a compile break.
- **Post-burn-in residual:** after 16 (psr/sr_max/wfo) / 4 (pbo) verified
  calls, a hypothetically drifting kernel would serve unverified values —
  the same disclosed PR-1 envelope; the exact-type gates still refuse
  structural poison per call.
- **cargo-mutants was NOT re-measured in this sandbox** (binary not
  installed). `src/stats/*.rs` already sits in the fno-math
  `examine_globs` allowlist, so the weekly mutation.yml matrix covers the
  four new modules automatically; the shard-budget re-measure rides the
  next scheduled run.
- **`pbo.py`/`wfo.py` are not in the python mutmut loop** (pre-existing
  scope — `rust_stats.py` + `deflated_sharpe.py` were already paired and
  their runners now execute the new dispatch tests).
- **The PBO binding engages only on the default `sharpe_from_moments`
  evaluator** — a custom evaluator honestly stays Python on both arms
  (pinned).
- **NO performance claim is made** — the flag ships OFF; a default flip
  needs its own DEC with measured evidence.
- **Groww/NSE/BSE:** this PR touches no vendor surface; the engine scope
  stays NIFTY/BANKNIFTY/SENSEX per DEC-ENGINE-SPOT-ONLY-001
  (MIGRATION.md #1790); no rate/scope/vocab change (DEC-GROWW-RATE-CEILING-8-002
  and the §2 Rule 6 envelope untouched).

## Golden Rule #12 — 100% standards self-check

### Coverage + tests
- [x] Coverage line 100% on changed `brutex/` files
- [x] Coverage branch 100% on changed files
- [x] Property tests added (hypothesis differential + in-crate proptest)
- [x] Golden fixtures added (merge-base-frozen literals, both flags)
- [x] Benchmark: pre-existing seam bench passes; no new hot path (flag OFF)

### Bug fixes
- [x] N/A — feat (no `fix:` title; the frozen goldens are the reversion net)
- [x] DEC-ID referenced above

### Type / lint / security
- [x] `mypy --strict` passes locally (0 new vs merge base)
- [x] `pyright` passes locally (0 errors on touched files)
- [x] `ruff check` passes locally
- [x] `bandit`: no new findings surface — the one `unsafe extern "C"` is
      Rust-side, scoped + SAFETY-documented under `deny(unsafe_code)`
- [x] No new copy-paste (the tails were MOVED into oracles, not duplicated;
      the Rust mirrors are the sanctioned conversion itself)

### Performance
- [x] No new O(n) where O(1) was achievable (kernels are the same O as their
      oracles; validation gates are O(args))
- [x] No new pandas
- [x] No new global mutable state (the seam's registered budgets/latches are
      the PR-1 contract, extended per-function)

### Observability
- [x] No new material state change class (flag-gated compute dispatch;
      engaged/fallback/autorevert structlog events — AST-pinned)
- [x] structlog only, no `print`
- [x] No new metric / no new CRITICAL condition

### Documentation
- [x] NumPy-style docstrings on every public symbol (bindings + Rust docs)
- [x] Specs: no behavior contract changed (bit-identical outputs)
- [x] MIGRATION.md #1977 + CLAUDE.md header/§1/§3 updated
- [x] No silent scope expansion

### No hallucination (Golden Rule #1)
- [x] Every numeric claim (mismatch counts, corpus sizes, coverage figures,
      test counts) is measured in this workspace or pinned in-crate; the
      probe-corpus figures are documented in the module docstrings beside
      their methods
- [x] No invented constants — AS241 coefficients transcribed from CPython
      `statistics.py`; EULER_MASCHERONI/E bit-pattern-pinned

## Test plan

- [x] `pytest tests/unit tests/property` (touched-scope battery) — 499
      passed across the new/touched files + neighbors
- [x] `cargo test --release --locked` — 636 passed
- [x] The four ci.yml per-module coverage gates — 100.00% each
- [x] Flag-ON manual smoke (verbatim output in Real-run proof)

## Operator action required (post-merge)

- None (the flag ships OFF; nothing to install/enable).

---

CI will enforce:
- 100% pass rate on unit + integration + property + golden + bench
- Coverage gate (per pyproject.toml fail_under)
- mypy --strict + pyright + ruff + bandit + vulture + gitleaks
- Mutation score on critical modules (weekly + nightly)
- Conventional commit message format
- No banned patterns (print, bare except, pandas-in-hot-path)
- O(1) NFR gates on declared hot paths

Any red gate BLOCKS this PR. Per Golden Rule #12, operator override requires ADR.
