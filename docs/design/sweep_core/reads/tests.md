# AREA MAP: TESTS + GATES (key: tests)

Everything a Rust sweep-core conversion (moving the per-mask orchestration loop from
Python into a PyO3 kernel) must satisfy. All anchors verified 2026-07-08 on branch
`claude/data-edge-guards` at /home/user/bruteX.

---

## 1. GOLDEN SUITES (frozen content hashes) — the strongest oracle

### tests/golden/test_engine_sweep_run_golden.py (451 lines)
- `_GOLDEN_RUN_ID = "f588194f…d2d5921a"` (line 142) pins
  `mask_run_id(mask=0b101, params=_PARAMS, **_CTX)` (line 145-146). BLAKE3 of the 9
  canonical run-identity inputs; mask encoded `%032x` (u128, 32 hex — re-locked by
  DEC-VOCAB-U128-CODEGEN-001); params layout carries `fib_anchor` + `start_equity_paisa`
  unconditionally (lines 64-74). Any change to the canonical-params serialization in a
  Rust port moves this digest → hard fail.
- `_GOLDEN_CONTENT_HASH = "2387cc88…4bd4d147"` (line 170): the persisted store hash of
  `run_persisted_sweep` over the fixed 12-day synthetic fixture (`_bars()` lines 77-101,
  `_config()` lines 104-118: instrument NIFTY, tf 1m, BUY, mode A, condition_bits [0..4],
  k=1, sl 80 / tp 50). Pinned summary tallies (lines 173-181): enumerated=5,
  conflict_pruned=0, skipped_dedup=0, evaluated=5, ranked=5, persisted=5.
- Determinism gate (lines 184-189): two independent runs → identical
  `store_content_hash` AND **byte-identical Parquet files**.
- ARM MATRIX all landing on the SAME hash:
  - Wired Rust trade-sim arm asserts kernel genuinely engaged: exactly ONE structlog
    event `sweep_run.trade_sim_path` with `rust_active is True` (lines 224-240) — the
    anti-vacuous-gate teeth.
  - Forced-Python fallback (lines 243-253): monkeypatches the **module attribute**
    `brutex.engine.sweep_run.ResidentTradeSim` with a factory that sets instance
    `sim._rust = None` (lines 209-221, "the DEC-P0-017 friend seam"). ⇒ The sweep MUST
    keep constructing its trade sim via that module-level name, and the `_rust=None`
    instance seam must keep forcing the Python oracle.
  - SELL-direction wired == fallback byte equality (lines 256-284).
  - Rust-METRICS default arm (lines 299-323): with NO env set, exactly ONE
    `sweep_rust_metrics.engaged` event + zero `kernel_autoreverted` + the frozen hash
    (ADR-0017 flip licence). Explicit `BRUTEX_SWEEP_RUST_METRICS=0` is the Python
    reference arm; `=1` byte-identical + engaged (lines 326-360; tallies compared too).
  - Poison tests monkeypatch `fno_math.sweep_drawdown_stats` directly (lines 363-451):
    domain-INVALID result → one `sweep_rust_metrics.kernel_result_refused`, pinned hash;
    domain-VALID poison past burn-in (`reset_self_check_for_tests(remaining=0)`) →
    hash MOVES (proves the ON arm consumes kernel output end-to-end); same poison
    DURING burn-in → one `kernel_autoreverted` with `reason == "self_check_mismatch"`,
    oracle bytes ship, kernel latched off. ⇒ Rust conversion must (a) keep consuming
    `fno_math.sweep_drawdown_stats` late-bound/monkeypatchable, (b) preserve the
    16-mask burn-in bit-compare vs the Python `_drawdown_stats` oracle, (c) keep test
    seams `reset_observability_memo_for_tests()` / `reset_self_check_for_tests(remaining=)`
    in `brutex.engine.rust_metrics`.
- PINNED LOG-EVENT SURFACE (names are load-bearing): `sweep_run.trade_sim_path`
  (`rust_active` field), `sweep_rust_metrics.engaged` (once per process — memoized),
  `sweep_rust_metrics.kernel_autoreverted` (`reason`), `sweep_rust_metrics.kernel_result_refused`,
  `sweep_rust_metrics.fallback` (`reason="exit_indices_demoted"` — bench file).

### tests/golden/test_engine_sweep_orchestrator_golden.py
- Determinism across independent grid runs (byte-identical per-tuple Parquet);
  idempotent re-run against SAME roots + populated dedup → `evaluated` drops to 0,
  files NEVER overwritten with an empty store; audit framing exactly
  `2 + 3·tuples_run` lines (`GRID_SWEEP_START` + per-tuple `SWEEP_*` ×3 + `GRID_SWEEP_COMPLETE`)
  (module docstring lines 1-14).

### tests/golden/test_vocab_golden.py + fixtures/vocab_golden_vectors.json
- Frozen language-neutral input→mask answer key (u128 mask as hex STRING). Python
  `evaluate_atoms` AND `rust/trading-core/tests/golden_vectors.rs` must satisfy it
  bit-for-bit; fixture byte-untouched is a standing PR expectation (docstring lines 1-18).
- Other sweep-adjacent goldens: test_engine_run_identity_golden.py,
  test_engine_stage1a_golden.py, test_engine_trade_sim_golden.py,
  test_engine_{nifty_day,banknifty_week,sensex_forced_exit}_golden.py,
  test_engine_paisa_accounting_golden.py, test_engine_walk_forward_golden.py.

Run locally: `pip install -e ".[dev,data,ui]" && pip install ./rust/fno-math ./rust/trading-core`
then `pytest tests/golden -x` (ci.yml golden job, lines 2423-2485).

---

## 2. PROPERTY SUITES that must pass UNMODIFIED

### tests/property/test_sweep_suffix_independence.py (485 lines, DEC-SWEEP-LOOKAHEAD-GUARD-001)
CLAUDE.md pins that this file passes UNMODIFIED. Gates:
1. `test_stage1a_prefix_projection_suffix_absent` (176-186):
   `build_bar_bits(bars)[:k+1] == build_bar_bits(bars[:k+1])` bit-identical.
2. `test_kernel_bar_bits_immune_to_extreme_suffix_mutation` (191-204):
   `ResidentSweepKernel(bars).bar_bits` prefix identical under extreme (₹1 / ₹1e6)
   suffix mutation — pins the kernel arm the grid orchestrator reuses across tuples.
3. Four resampler gates (feed `resample_bars` + canonical `resample_1m_to_tf`;
   suffix-day mutation, appended suffix, mid-session cut — lines 212-335; the
   mid-session gate was verified to FAIL against a planted off-by-one).
4. `test_full_spine_trades_closed_before_mutation_point_are_unchanged` (343-386):
   bars → `build_bar_bits` → `mask_to_entry_signals` → `simulate_trades`; a trade closed
   at bar K is bit-identical under any mutation of bars strictly after K.
5. `test_prefix_sweep_is_bit_identical_before_and_after_a_full_series_run` (437-485):
   ENGINE STATELESSNESS — `metrics_for_mask` + full `run_persisted_sweep` over a prefix
   produce identical metrics/hash/Parquet BYTES whether or not a full-series sweep ran
   first in the same process. ⇒ a Rust kernel may hold NO cross-run global mutable
   state (statics, lazy caches keyed on anything reusable) that changes results.

### P-06 look-ahead
- tests/property/test_lookahead_properties.py (105 lines): `LookAheadBarrier`
  never-false-negative (`access(i>N)` raises `LookAheadError`), never-false-positive,
  rejected index never enters the recorded trail, forward-replay + monotonic cursor.
  Runs on a bare checkout (pure Python).
- tests/unit/test_engine_sweep_lookahead.py: `maybe_guard_bars(bars) is bars` when the
  opt-in sweep guard is OFF (lines 105, 117) — the OFF hot path must return the SAME
  object (identity contract, byte-identical + cost-unchanged). The opt-in
  `BRUTEX_SWEEP_LOOKAHEAD_GUARD=1` wraps bars in an index-guarded sequence — a Rust
  core taking raw buffers must still honor/wrap-around this path.
- tests/property/test_engine_trade_sim_properties.py (walker-local no-look-ahead),
  tests/property/test_market_structure_lookahead.py (BOS/CHoCH).
- Even the bench canary asserts P-06: `evaluate_atoms` must not mutate
  `prior_history` (test_throughput_canary.py:399-428).

Run locally: `HYPOTHESIS_PROFILE=ci pytest tests/property -x --hypothesis-show-statistics`
(ci.yml property job lines 2288-2294; env.HYPOTHESIS_PROFILE="ci" at ci.yml:99 — the
fully-explicit deterministic profile, DEC-TEST-APPARATUS-DEBTS-001(b)).

---

## 3. BENCH GATES

### tests/bench/_o1_tiers.py — the ONE ceiling source (67 lines)
- `ratio_ceiling(shared_ci_default)` → `1.1` iff `BRUTEX_M4PRO_STRICT=1`
  (`STRICT_RATIO_CEILING`, line 28), else the caller's robust 3–10× shared-CI default
  (lines 59-66). `cache_tier_ceiling(default)` → `min(3.0, default)` on the strict tier
  (`CACHE_TIER_STRICT_CEILING=3.0`, line 45) for workloads crossing a CPU cache tier.
  Every O(1) ratio assertion must route through these functions (COMMON rule).

### 50 M/s throughput canary
- tests/bench/test_throughput_absolute.py: `M4PRO_ABSOLUTE_FLOOR_OPS_PER_SEC = 50_000_000`
  (line 72); `test_numpy_mask_and_meets_50m_ops_per_sec` (line 135) is a HARD gate only
  under `BRUTEX_M4PRO_ABS=1` (self-hosted M4-Pro, bench-m4pro.yml); constant pinned to
  STANDARDS row 14 (line 209-210).
- tests/bench/test_throughput_canary.py: shared-CI RATIO-only gates (mask-AND ratio
  with `cache_tier_ceiling(8.0)`, dict-lookup `cache_tier_ceiling(10.0)`,
  `evaluate_atoms` ratio `ratio_ceiling(8.0)`), best-of-15 min timing
  (`_TIMING_REPEATS=15`, DEC-CANARY-MINTIME-001). Absolute ops/s printed, never gated
  on shared CI.

### Exit-index fast-path gate (#1531, DEC-SWEEP-EXIT-INDEX-FASTPATH-001)
- tests/bench/test_sweep_exit_index_fast_path.py: `_FAST_PATH_FLOOR = 1.15` (line 53).
  With `BRUTEX_SWEEP_RUST_METRICS=1` + burn-in exhausted
  (`reset_self_check_for_tests(remaining=0)`), on 20 000 bars / 200 trades:
  (a) `metrics_from_trades(..., exit_indices=…)` must equal the dict arm's result
  EXACTLY (line 124), then (b) dict-arm median ≥ 1.15× fast-arm median (lines 125-133).
  ANTI-VACUOUS TWIN (lines 136-188): out-of-range indices must be REFUSED by
  `exit_indices_consistent` → per-call demotion to `build_ts_index`, exactly one
  `sweep_rust_metrics.fallback` event with `reason == "exit_indices_demoted"`, and the
  demoted arm's ratio must sit BELOW the floor. ⇒ the conversion must preserve the
  `metrics_from_trades(bars, trades, start_equity_paisa=, tf_minutes=, exit_indices=)`
  seam, the consistency belt, and the demotion path.

### Sweep O(1)/SLA pins (all in tests/bench/)
- test_engine_sweep_run_o1.py: `ComboDedup.seen` latency independent of marked count;
  audit-append count independent of combo count — a k=1 and a k=2 persisted sweep both
  emit EXACTLY 3 audit lines (docstring lines 3-16). ⇒ per-mask work in Rust must not
  add per-mask audit lines.
- test_engine_sweep_driver_o1.py: per-mask `walk_forward_sweep` cost independent of
  grid size — bar_bits + fold schedule built ONCE, `metrics_for_mask` shared core.
- test_trade_sim_throughput.py: `fno_math.ResidentSweepSim` per-(mask·bar) flat ratio +
  per-combo regression floor (every runner) + absolute combos/s printed
  (M4-gated assertion).
- test_full_sweep_extrapolation.py: 33B-trial projection (`_FULL_SWEEP_TRIALS` COMMON);
  hard ≤4-day SLA assert only under `BRUTEX_M4PRO_ABS=1`.
- test_m4pro_sweep_walltime.py: real trade-walk (`count_trades`) projection, M4-gated
  timing body, ungated pure projection-math test.
- test_supermask_kernel_bench.py: Rust `count_supermasks_u64s` must not be SLOWER than
  the numpy reference (best-of-N min), identical output asserted.
- test_engine_combo_mask_u128_o1.py (u128 two-limb mask pin), test_engine_stage1a_o1.py,
  test_engine_trade_sim_o1.py, test_engine_sweep_{orchestrator,scheduler,supervisor,cache}_o1.py.

### Bench job mechanics (ci.yml bench job, lines 2320-2418)
- Steps: `bench_baseline_gate.py validate --baselines-dir tests/bench/baselines`
  (every run, line 2338) → `govern --base origin/$BASE_REF` (PRs only, line 2346) →
  `pip install -e ".[dev,data]"` + both wheels →
  `pytest tests/bench -W default::DeprecationWarning -W default::PendingDeprecationWarning
  --benchmark-min-rounds=3 --benchmark-disable-gc --benchmark-json=bench-results.json`.
- Absolute-baseline registry: tests/bench/_abs_baseline_registry.py (6 entries: dict
  probe, scalar u128 evaluate_mask, vectorised mask-AND, bloom probe, LMDB probe,
  result-row append); baselines dir currently bootstrap-empty (README only) — compare
  ABSTAINS loudly; governance forbids silent re-baseline (needs a new DEC + literal
  value token in the ledger).

Run locally: `pytest tests/bench -W default::DeprecationWarning --benchmark-min-rounds=3 --benchmark-disable-gc`
(strict tier: prefix `BRUTEX_M4PRO_STRICT=1`, abs floors: `BRUTEX_M4PRO_ABS=1` — M4 only).

---

## 4. COVERAGE GATE MECHANICS (ci.yml `test` job)

1. **Whole-package floor** (lines 369-376):
   `pytest --cov=brutex --cov-branch --cov-report=term-missing --cov-report=xml
   --cov-fail-under=100 --ignore=tests/bench tests/unit tests/integration tests/property`
   — 100.00% line+branch on the ENTIRE brutex package; the coverage omit list is EMPTY.
2. **Smoke + redteam** (line 387): `pytest -q tests/smoke tests/redteam` (redteam =
   gate-integrity meta-tests asserting each CI gate catches a planted bug).
3. **Static per-module gates** (lines 388-…, `set -x`, fail-on-first): notably
   `pytest --cov=brutex.engine --cov-branch --cov-fail-under=100 tests/unit/test_engine_*.py`
   (lines 427-433) — the WHOLE engine package must reach 100% line+branch from the
   `tests/unit/test_engine_*.py` glob ALONE. ⇒ any new Python orchestration module for
   the Rust core needs `tests/unit/test_engine_<name>.py`-stemmed unit tests reaching
   100% inside that glob (incl. every fallback/error branch — hence the forcing seams).
4. **Discovery gate** (line 2110): `python scripts/ci/coverage_gate_discovery.py --base "$BASE"`
   — diffs `brutex/**/*.py` vs origin/main; a NEWLY ADDED module with no
   import-verified stem-matched test = exit 1; each touched module with a verified test
   is re-run standalone at `--cov-fail-under=100` line+branch. Exclusion allowlist
   (`COVERAGE_DISCOVERY_EXCLUDE`, script lines 86-…) covers only app.py /
   pull_controller.py / db_view.py-class multi-suite modules. Local dry-run: add `--list`.
- Rust source is NOT pytest-cov'd; it is gated by `rust` / `rust-trading-core` jobs
  (cargo test: fno-math 565 / trading-core 162 suites) + cargo-mutants (below).
- Python fallback paths must stay REACHABLE in tests (the `_rust=None` seam and env
  kill switches exist precisely so coverage of both arms is achievable).

---

## 5. MUTATION WORKFLOW

### Per-PR (ci.yml `mutation-py`, lines 3213-…; 20-min cap)
mutmut==2.5.1 on EXACTLY TWO modules: `brutex/orchestrator/state.py` (300 s budget,
survivor gate) + `brutex/money.py` (240 s). Do not add modules without proving fit.

### Weekly deep gate (.github/workflows/mutation.yml, 895 lines; cron `0 4 * * 1` +
push-to-main + dispatch; DEC-MUTATION-RUNTIME-003, zero-survivor policy)
- **python-mutation** (120-min cap): pass 1
  `--paths-to-mutate brutex/metrics/deflated_sharpe.py,brutex/ranking/__init__.py,brutex/ranking/overfit.py`
  (line 255); then a 10-pair per-module loop (lines 390-399): metrics/compute,
  orchestrator/{state,retry,audit,rate_limiter,store}, data/{partition,parser},
  strategies/spec, adapters/rate_common (timeout-wrapped: `sh -c 'timeout -k 5 $wrap_s
  pytest <tests> -x -q || exit 1'`, `--test-time-base 45`, preflight min-baseline
  refusal band [1200, 20000] ms). ANY surviving mutant (`mutmut results` numeric line)
  = exit 1. `HYPOTHESIS_PROFILE=ci` (line ~109) for mutant-vs-seed determinism.
- **rust-mutation**: cargo-mutants over rust/fno-math on a 6-shard matrix
  (`shard: [0..5]`, line 588-589) and rust/trading-core on a 7-shard matrix
  (`shard: [0..6]`, line 689-690; scope evaluator+python+spec, `--features python,serde`).
  Worst shard ~65 min vs 120-min cap; fan-in verdict job with honest
  timeout-vs-survivor alert titles. ⇒ a new Rust sweep kernel in fno-math JOINS these
  shards under the zero-survivor policy; the ledgered dial for overrun is "+1 shard".
- Sweep modules (sweep_run/sweep_driver/rust_metrics) are NOT mutmut-gated today;
  their teeth are the goldens + engine coverage gate + the anti-vacuous bench twins.

---

## 6. PR-BODY / MERGE GATE SCRIPTS (scripts/ci/) — full inventory

_textscan.py · bench_baseline_gate.py · check-conventional-commits.sh ·
coverage_gate_discovery.py · definition-of-ready-guard.sh · no-dhan-guard.sh ·
pre-merge-check.sh · scan-banned-patterns.sh · spec_test_crosswalk.py ·
validate_bugfix_regression.py · validate_bugfix_reversion.py ·
validate_citation_accuracy.py · validate_dec_ids.py · validate_ledger_numbering.py ·
validate_no_hallucination.py · vulture_whitelist.py · zplus-pr-guard.sh

The gates a PR must pass (each is in ci-ok.needs, ci.yml lines 3784-3886) + local runs:

1. **zplus-pr-guard.sh** — PR body must contain ALL 6 literal markers: "Z+ 15-row
   matrix", "Z+ 7-row resilience matrix", "Z+ 7-layer audit", "4-agent hostile review",
   "Honest envelope", "Decision ID" (script lines 10-17). Local:
   `bash scripts/ci/zplus-pr-guard.sh pr_body.md`.
2. **definition-of-ready-guard.sh** — 5 non-blank sections: "Scenarios covered",
   "Failure modes", "Real-run proof", "Rollback plan", "Out of scope" (lines 14-20;
   evaluated against the LIVE PR body). Local:
   `bash scripts/ci/definition-of-ready-guard.sh pr_body.md`.
3. **validate_dec_ids.py** — every `DEC-*` cited in the body must resolve in
   docs/MIGRATION.md; PLUS appropriateness: a non-exempt-titled PR touching
   brutex/**/*.py, rust/**/*.rs or workflows must cite a resolving DEC-ID or touch the
   ledger. Local: `PR_FILES="$(git diff --name-only origin/main...HEAD)"
   PR_TITLE='feat(engine): …' PR_AUTHOR=me python3 scripts/ci/validate_dec_ids.py pr_body.md`.
4. **validate_bugfix_reversion.py** (fix:-titled PRs) — shipped tests must FAIL with
   the PR's MODIFIED brutex sources reverted to merge-base in a scratch worktree
   (import-resolution probe guards against editable-install shadowing). Local:
   `PR_TITLE='fix(engine): …' BASE_REF=main python3 scripts/ci/validate_bugfix_reversion.py`.
   (Sibling presence check: validate_bugfix_regression.py, same env contract.)
5. **validate_citation_accuracy.py** — runs on push AND PR (never skipped): file:line
   cites in CLAUDE.md / docs/TRACK1_*.md / STANDARDS_100PCT.md must resolve Tier 1
   (file) / Tier 2 (line range) / Tier 3 (anchored symbol ±5), and `MIGRATION.md #NNN`
   refs must exist. Local: `python3 scripts/ci/validate_citation_accuracy.py`.
6. **bench_baseline_gate.py** — subcommands key/validate/compare/capture/govern; CI runs
   `validate` (every run) + `govern` (PR). Local:
   `python3 scripts/ci/bench_baseline_gate.py validate --baselines-dir tests/bench/baselines`
   and `python3 scripts/ci/bench_baseline_gate.py govern --base origin/main`.
7. **jscpd** (duplication job, ci.yml lines 3161-3194): pinned `jscpd@3.5.10`,
   `jscpd brutex --min-lines 10 --min-tokens 70 --threshold 1` (grand-total LINES % ≤1;
   baseline 0.37%). Copy-pasting the Python per-mask loop shape into a new module can
   trip this — share helpers instead.
8. **pre-merge-check.sh** — MUST be run before any merge:
   `./scripts/ci/pre-merge-check.sh <PR#> [SJParthi/bruteX]`; refuses on <20 registered
   checks (`EXPECTED_MIN_CHECKS`), any FAILURE/CANCELLED/TIMED_OUT, any PENDING, or
   CONFLICTING mergeable state; override = OPERATOR_OVERRIDE + reason + rate-limited log.
9. Also in ci-ok: validate_no_hallucination.py (uncited external claims in body),
   validate_ledger_numbering.py, check-conventional-commits.sh (title/commits),
   no-dhan-guard.sh, scan-banned-patterns.sh, vulture, chaos-race, browser, run-smoke.
- **ci-ok aggregate** (lines 3765-3972): 35 needs-jobs; result must be success or
  skipped for each; required branch-protection context is the PR-event-only name
  "CI OK (aggregate gate)".

---

## 7. GOTCHAS FOR THE RUST SWEEP-CORE CONVERSION (do-not-break list)

1. **Byte-frozen hashes**: `_GOLDEN_CONTENT_HASH` + `_GOLDEN_RUN_ID` must reproduce
   byte-for-byte, including Parquet file bytes across two runs. Any change in float
   arithmetic/rounding of per-mask metrics (bit-compared via `struct.pack`, signed
   zeros included, in the burn-in self-check) or in run-identity serialization fails.
2. **Module-attribute seams**: sweep must instantiate `ResidentTradeSim` via
   `brutex.engine.sweep_run.ResidentTradeSim` (monkeypatched by goldens) and call
   `fno_math.sweep_drawdown_stats` late-bound (poison tests monkeypatch it). A Rust
   loop that internalizes these calls breaks four golden tests.
3. **Instance `_rust = None`** must keep forcing the pure-Python oracle for the WHOLE
   persisted sweep (fallback arm), and `BRUTEX_SWEEP_RUST_METRICS` tristate semantics
   ("0" off / "1" on / unset default-ON via `SWEEP_RUST_METRICS_DEFAULT`) must survive.
4. **Burn-in self-check**: first 16 kernel-served masks bit-compared vs the Python
   oracle; mismatch ships the ORACLE value + one `kernel_autoreverted` + process latch.
   Seams `reset_self_check_for_tests(remaining=)` / `reset_observability_memo_for_tests()`
   are called by goldens AND benches.
5. **Log-event names/fields are pinned** (see §1) — including exactly-once semantics.
6. **Audit framing**: exactly 3 audit lines per persisted sweep run regardless of mask
   count; grid = 2 + 3·tuples. Per-mask Rust work must emit ZERO extra audit lines.
7. **Statelessness + suffix independence**: no cross-run static state; prefix-of-full ==
   full-of-prefix for `build_bar_bits` and `ResidentSweepKernel.bar_bits`; trades closed
   at K immune to later-bar mutation through the full spine.
8. **Exit-index contract**: keep `metrics_from_trades(..., exit_indices=)`,
   `exit_indices_consistent` refusal → per-call dict demotion (never to a wrong result,
   never silently), ≥1.15× dict-vs-fast measured ratio.
9. **Coverage**: 100.00% line+branch on the whole package (unit+integration+property,
   bench excluded) AND `--cov=brutex.engine` from `tests/unit/test_engine_*.py` alone AND
   the discovery gate for every touched/added module. New module ⇒ new stem-matched
   test file or hard fail.
10. **Summary tallies** (enumerated/conflict_pruned/skipped_dedup/evaluated/ranked/
    persisted) must stay exact across arms; dedup skip counting (`ComboDedup`) and the
    idempotent re-run (`evaluated == 0`, files preserved) are golden-pinned.
11. **O(1) ceilings**: route any new ratio assertion through `_o1_tiers.ratio_ceiling` /
    `cache_tier_ceiling`; per-mask cost must remain grid-size-independent; dedup probe
    O(1); `maybe_guard_bars` identity when OFF.
12. **Rust side**: new fno-math code joins cargo test + the 6-shard cargo-mutants
    zero-survivor matrix (65/120-min headroom today; "+1 shard" is the sanctioned dial);
    trading-core untouched unless the vocab moves (frozen 400-case fixture byte-locked).
13. **Hypothesis determinism**: CI runs `HYPOTHESIS_PROFILE=ci`; property job runs
    `pytest tests/property -x --hypothesis-show-statistics` then
    `pytest tests/stress -m 'not soak' -q`.
