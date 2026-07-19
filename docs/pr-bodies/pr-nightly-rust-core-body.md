# Pull Request — BruteX

> **Merge-Readiness Charter (DEC-MERGE-GUARANTEE-001):** this PR obeys
> `docs/MERGE_READINESS_GUARANTEE.md` — the §3 gate matrix + §4 Definition of
> Done. **DRAFT** — opened for review; never merged over a red/pending gate.

## Summary

- **FIRST CUT of the `brutex/nightly/` Rust conversion** (DEC-NIGHTLY-RUST-CORE-001,
  MIGRATION.md #1957): the TEN named pure decision kernels — chain
  (`first_breaker` · `exit_code_for` · `audit_reconcile_tripwire`), report
  (`summary_lines` + its `_verdict`/`_count_text` sub-laws), schedule
  (`render_launchd_plist` · `parse_at` · `pmset_wake_command` ·
  `seconds_since_scheduled_fire`) — re-hosted on the NEW standalone crate
  **`rust/brutex-ops`** behind the default-OFF **`BRUTEX_NIGHTLY_RUST`** facade
  `brutex/nightly/nightly_rust.py`.
- Every verbatim Python body is RETAINED as the **live parity oracle**
  (`*_python`, the #1913 extraction shape); public signatures are byte-unchanged
  (`brutex/crossverify/schedule.py`'s imports untouched). Flag OFF (the default)
  is byte-identical AND crate-untouched (bomb-spy-pinned).
- Ships **INERT**. Expected payoff ≈ 0 — conversion is for uniformity, not speed
  (the ADR-0020 banner verbatim). A default flip needs its own DEC.

## Decision ID

- **Decision ID(s):** DEC-NIGHTLY-RUST-CORE-001 (this PR) ·
  DEC-NIGHTLY-RUST-CORE-002 (the NAMED follow-up — backup/restore ordering,
  `deadman_verdict`, the probes floor law, run.py orchestration + bindings:
  deliberately stay Python).
- **MIGRATION.md row(s):** #1957 (provisional — renumber-at-landing per
  DEC-LEDGER-RENUMBER-001).
- **Spec section(s) touched:** none (ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  no lake/schema/scope/vocab/rate change; run identity untouched).

## Type of change

- [x] feat — new feature (a default-OFF Rust seam; behavior-identical by default)

---

## Definition of Ready

### Scenarios covered

- **Happy:** flag unset/falsy → every kernel runs the verbatim retained Python
  oracle, byte-identical (`_BombModule` spy proves the crate is never touched).
- **Flag ON + real wheel:** steady-state (`remaining=0`) parity goldens — 21
  tests drive the COMPILED `brutex_ops` through the real routed seams and pin
  crate == oracle, the launchd plist byte-for-byte.
- **Flag ON + wheel absent:** `crate_unavailable` latch → oracle, one WARN.
- **Edge:** empty outcome runs · blocked/refused/failed breaker orderings ·
  exit_hint pass-through (6) · tripwire severity upgrade · None counts → "?" ·
  negative-duration clamp · midnight-wrapping pmset · `" 9:05 "` strip parse ·
  XML-metachar label escape · empty env dict · custom log stem.
- **Empty/zero:** empty statuses / empty events / zero-length program args all
  parity-pinned on both arms.
- **Failure:** crate fault (TypeError/ValueError/…) → degrade to oracle;
  poisoned return shapes → refuse-poison gates; wrong VALUES → 16-sample
  burn-in autorevert (whole-facade disable, ship-the-oracle); pyo3 panic →
  belt latch; junk flag value → one warn-once then OFF.
- **Un-mirrorable inputs (refuse-to-guess):** non-ASCII `--at` (CPython's
  un-flagged `\d` matches Unicode digits — `"0٣:05"` genuinely parses (3, 5);
  the crate refuses, the oracle decides — pinned EQUAL) · out-of-range datetime
  components (the oracle raises Python's own `hour must be in 0..23`).
- **Concurrent:** the facade is process-local module state mutated only by
  latches/burn-in decrements (idempotent, monotone toward OFF); the nightly
  chain itself is single-flight by the existing G6/nightly flock (unchanged).

### Failure modes

| Failure | System behavior |
|---|---|
| wheel missing / stale surface | `crate_unavailable`/`crate_surface` latch → oracle, one WARN |
| crate raises (degrade set) | oracle serves; one `nightly_rust.fallback` warn-once per reason |
| crate return wrong SHAPE | refuse-poison gate → oracle (`*_result_refused`) |
| crate return wrong VALUE | burn-in bit-compare → `kernel_autoreverted` ERROR, facade OFF, oracle value shipped |
| pyo3 panic escapes | panic belt → `crate_panic` latch, oracle serves |
| MemoryError / KeyboardInterrupt / SystemExit | re-raised RAW — never laundered into a fallback |
| junk `BRUTEX_NIGHTLY_RUST` | one warn-once, treated OFF |
| post-burn-in wrong-but-well-shaped value | DISCLOSED envelope: served un-oracled (the documented facade limit, same as every house facade) |

### Real-run proof

```
$ cd rust/brutex-ops && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test --release
    → 24 passed (incl. plist_full_golden_with_env_vars)
$ .venv/bin/maturin develop --release -m rust/brutex-ops/Cargo.toml   # wheel built + import-smoked
$ .venv/bin/python -m pytest tests/unit/test_nightly_rust.py tests/unit/test_nightly_{chain,report,schedule}_rust_seam.py tests/golden/test_nightly_ops_rust_parity.py -q
    → 126 passed (45 + 21 + 10 + 29 + 21; the 21 goldens drive the REAL wheel steady-state)
$ .venv/bin/python -m pytest tests/unit/test_nightly_*.py tests/integration/test_nightly*.py -q   # existing suites UNMODIFIED
    → 466 pre-existing test_nightly* unit+integration tests green
$ coverage (the ci.yml way): nightly_rust.py 116/38 · chain.py 234/66 · report.py 128/28 · schedule.py 198/54 — ALL 100.00% line+branch
$ .venv/bin/mypy --strict brutex → clean (496 files; the single stage3_refine `cma` import-not-found is the env-only baseline — module untouched by this PR)
```

### Rollback plan

- The seam ships **default-OFF**: production behavior is already the rollback
  state. To hard-rollback: revert the PR commit (one commit); the crate dir +
  facade are additive, the three seam files regain their pre-extraction bodies
  via the revert. No data / lake / schema migration involved.
- Runtime kill: leave `BRUTEX_NIGHTLY_RUST` unset (the default) — byte-identical.

### Out of scope

- backup/restore ordering laws, `deadman_verdict`, the probes floor law,
  run.py orchestration, production bindings → **DEC-NIGHTLY-RUST-CORE-002**.
- No CI parity lane for the wheel in this first cut (the wheel-free faithful
  fake + the importorskip goldens carry the load — disclosed).
- No default flip; no engine/Groww/rate/scope change of any kind.

---

## Z+ 15-row matrix

| # | Row | Status |
|---|-----|--------|
| 1 | Coverage line 100% | ✅ 100.00% on all four touched modules (ci.yml way) |
| 2 | Coverage branch 100% | ✅ 100.00% (116/38 · 234/66 · 128/28 · 198/54) |
| 3 | Mutation ≥ 85% | ✅ pair wired into mutation.yml weekly loop (`nightly_rust.py:test_nightly_rust.py`); wiring test-pinned |
| 4 | mypy --strict | ✅ clean (496 files; env-only `cma` baseline untouched) |
| 5 | pyright | ✅ 0 errors on touched files |
| 6 | ruff + format | ✅ clean on every touched file |
| 7 | Zero duplication | ✅ `env_flag_tristate` imported, never copied; ONE `route_kernel` chokepoint; oracles extracted not duplicated (the crate port is the sanctioned twin-implementation, parity-pinned) |
| 8 | Functionality completeness | ✅ all TEN named kernels routed; nothing else touched |
| 9 | Regression coverage | ✅ flag-OFF byte-identity spies + 466 pre-existing tests unmodified |
| 10 | 7 test types | ✅ unit + integration-shaped seam suites + golden (plist byte + real-wheel) + property-style parametrized matrices + cargo tests; bench N/A (once-per-night path, no O(1) SLA claimed); mutation wired weekly |
| 11 | CI every push | ✅ discovery coverage gate folds the new stem-matched suites automatically (zero ci.yml edits) |
| 12 | O(1) hot path | N/A — nightly orchestration, not a hot path (honest) |
| 13 | Audit chain | ✅ untouched (direct file appends, not structlog; no audit surface changed) |
| 14 | Observability | ✅ structlog events: `nightly_rust.engaged` / `.fallback` / `.disabled` / `.kernel_autoreverted` / `.burn_in_complete` |
| 15 | Decision ID ledgered | ✅ DEC-NIGHTLY-RUST-CORE-001 → MIGRATION.md #1957 |

## Z+ 7-row resilience matrix

| # | Threat | Defense |
|---|--------|---------|
| 1 | wheel absent/stale | import + 11-name API handshake, latch OFF |
| 2 | crate exception | narrow degrade set → oracle, warn-once |
| 3 | crate panic | pyo3 PanicException belt → latch OFF |
| 4 | wrong-shape return | refuse-poison gates (bool-is-not-int included) |
| 5 | wrong-value return | per-kernel 16-sample burn-in → whole-facade autorevert, ship-the-oracle |
| 6 | un-mirrorable input | crate refuses-to-guess (ValueError) → oracle decides the genuine Python law |
| 7 | OOM / operator interrupt | MemoryError/KeyboardInterrupt/SystemExit re-raise RAW — never swallowed |

## Z+ 7-layer audit

- **DETECT:** burn-in bit-compare + refuse-poison shape gates + API handshake.
- **VERIFY:** 126 new tests (flag-OFF spies · faithful-fake parity · real-wheel
  steady-state goldens) + 24 cargo tests + 466 unmodified pre-existing tests.
- **RECONCILE:** crate value vs live Python oracle per call during burn-in; the
  faithful fake is itself burn-in-reconciled against the oracle.
- **PREVENT:** default-OFF flag; latches are monotone toward OFF; escape set
  re-raises raw; `lib.rs` append-only registration law.
- **AUDIT:** structlog events for every engage/degrade/refuse/autorevert;
  ledger entry MIGRATION.md #1957; this PR body.
- **RECOVER:** every failure path serves the oracle — the nightly chain never
  gains a new crash class; runtime recovery = unset the flag.
- **COOLDOWN:** warn-once registry (no log storms); process-local latches reset
  only via the test seam.

## 4-agent hostile review

**COMPLETE — verdict SHIP (zero blockers/majors).** The independent hostile
review's evidence included a **300k+ input differential fuzz of the compiled
crate vs the Python oracles — 0 value mismatches**, including the Unicode
refuse-to-guess classes and a refuted DST hypothesis, and confirmed the
oracle extraction is byte-identical. Findings folded on this branch:

- **1 MINOR:** the no-CI-parity-lane gap — already disclosed in the Honest
  envelope; follow-up material under DEC-NIGHTLY-RUST-CORE-002.
- **2 NITs (both fixed):** the MIGRATION.md #1957 wording on the sub-law FFI
  faces (pinned DIRECTLY by cargo, TRANSITIVELY by the Python nets — reworded
  to that truth) + a thread-safety design note beside the facade's
  process-local module state (single-threaded orchestration; a race could
  only mis-account burn-in, never return a wrong value).

## Honest envelope

- Expected payoff ≈ 0 — **uniformity, not speed** (ADR-0020 banner verbatim).
  These kernels run once per nightly; the FFI hop is a per-call slowdown in
  absolute nanoseconds, accepted. NO performance claim is made.
- The post-burn-in steady state ships a well-shaped wrong crate value
  un-oracled — the documented envelope of every house facade (the refuse-poison
  gates bound the SHAPE only).
- The main CI test job never installs the `brutex_ops` wheel; the real-crate
  goldens run only where the wheel is built (locally proven here). No dedicated
  parity CI lane in this first cut — disclosed, follow-up material.
- NO version guard on the facade — the laws are stdlib-pure with no
  wheel↔runtime lockstep to attest (the #1914 documented deviation).
- `mypy --strict` shows the pre-existing env-only `stage3_refine`/`cma`
  import-not-found (this venv installs `[dev]` without the `optimize` extra);
  that module is untouched by this PR.

## Operator action required (post-merge)

- None. The seam is default-OFF and inert.
