# Z+ Retroactive Audit — PRs #33, #34, #35 (DEC-P0-014, PR #36)

> **Status:** COMPLETE — all 4 agents returned. 3 HIGH findings caught in
> PR #33 fixed IN this PR (#36). 2 HIGH + 4 MEDIUM filed as follow-up PRs.
> **Authority:** operator-charter-forever.md §E + DEC-PROTOCOL-001.

This document records the retroactive Z+ hostile-review audit on the 3
PRs (DEC-P0-011, DEC-P0-012, DEC-P0-013) that were merged WITHOUT a
4-agent review pre-/post-implementation. Per Golden Rule #10 (honest
limits), this audit is forensic — it does NOT remove the discipline gap,
but it identifies which (if any) findings escaped into main.

---

## Methodology

4 parallel hostile-review agents spawned on the 3 merged-squash SHAs:

- PR #33: `16cafd1` — FNO test rewrite — IDX-only fixtures
- PR #34: `4206176` — always-latest dep bounds + Renovate config
- PR #35: `896fb0e` — pyright per-test downgrade fully removed

Each agent had a specific lens:

1. **Hot-path / Performance / O(1)** — production hot-path discipline
2. **Security / Secret / Supply-chain** — CVE risk, injection, etc.
3. **Correctness / Race / Audit** — false-OK, look-ahead, reproducibility
4. **Cite/Path Verification** — every cited symbol/path/decision ID exists

---

## Findings summary

| Agent | CRITICAL | HIGH | MEDIUM | LOW | Clean rows |
|---|---|---|---|---|---|
| 1: Hot-path | 0 | 0 | 0 | 3 | — |
| 2: Security | 0 | 2 | 3 | 1 | 3 |
| 3: Correctness | 0 | **3** | 4 | 3 | 2 |
| 4: Cite/Path | 0 | 0 | 0 | 0 | 15+ verified |

**Total across all 4 agents:** 0 CRITICAL, **5 HIGH**, 7 MEDIUM, 7 LOW.

**Agent 3's 3 HIGH findings are fixed IN this PR #36** (not deferred):
1. PR #33 `TestIdempotenceStress` false-OK — rewrote body to use 4 Track 1 indices
2. PR #33 MIGRATION.md hallucinated test method name — corrected to real method
3. PR #33 tautological `isinstance(counts, dict)` — replaced with `counts == {}`

Agent 2's 2 HIGH findings are addressable in a single follow-up PR #37.

---

## Agent 1 — Hot-path / Performance findings

| Severity | PR | File:line | Finding | Suggested fix |
|---|---|---|---|---|
| LOW | #33 | `tests/integration/test_instrument_store.py:69-80` | `_idx()` helper re-imports `zlib` AND re-constructs the 7-entry `_token_map` literal dict on EVERY call. Per-call: 1 module-attr lookup + 1 dict-literal allocation + 1 `dict.get`. Lookup itself is O(1). NOT in `brutex/*` hot path — test-only fixture. Test runtime impact: negligible (<1ms across the ~27 affected tests). | Hoist `_TOKEN_MAP` + `import zlib` to module scope. Cosmetic win, not a perf bug. |
| LOW | #35 | `brutex/math/__init__.pyi:31-43` | Stub uses `*args: Any, **kwargs: Any` signatures. This is a TYPE-ONLY artifact (.pyi never imported at runtime), so it cannot hide a real perf regression — runtime dispatch still goes through `brutex/math/__init__.py`. Risk: future caller code typed against `Any` could be looser than reality, but zero runtime cost. | None required. Optionally tighten signatures to match actual `fno_math_py` wheel. |
| LOW | #34 | `pyproject.toml:18,38-43` | Upper bounds widened to `<next_major_after_latest`. A future pyarrow 24.x → 25 major COULD silently install and bring a regression. `renovate.json` mitigates via `platformAutomerge=false` + label-gated major review. PR claims same baseline 1562 passed — but that pins to whatever was on PyPI at branch time, not future installs. | Add a CI job that runs `pip install --upgrade` weekly with a perf canary (Rule #12 throughput ≥50M/s) so dep drift gets caught before merge. |

**Verdict:** production hot-path modules UNTOUCHED across all 3 PRs. NONE
of `brutex/orchestrator/`, `brutex/engine/`, `brutex/data/`, `brutex/feed/`,
`brutex/strategies/`, `brutex/vocabulary/`, `brutex/indicators/` were
modified. No CRITICAL or HIGH findings.

---

## Agent 2 — Security / Secret / Supply-chain findings

| Severity | PR | File:line | Finding | Suggested fix |
|---|---|---|---|---|
| HIGH | #34 | `pyproject.toml:36` (`pyarrow>=16,<25`) | Multi-major jump (16→24) silently widens to versions never covered by `pip-audit`/`safety` in CI for this branch. pyarrow has CVE history (e.g. CVE-2024-52338 Arrow RCE in IPC parsing) and the 6-major leap means future `pip install -U` may pull a binary wheel CI never test-imported. | Pin `<22` (next major after currently-installed 21.0) and let Renovate raise individual major-bump PRs with explicit CI re-test. Add `pip-audit -r requirements-lock.txt` to CI before any major-bump merges. |
| HIGH | #34 | `renovate.json:38-43` (`vulnerabilityAlerts.automerge=false`) + L33 (`platformAutomerge=false`) | `enableVulnerabilityAlertsWithLabel(security)` is set, but `prCreation: "immediate"` only creates the PR — there is NO auto-merge path for security patches. A Dependabot-grade silent CVE bump waits indefinitely for human review. | Add a dedicated `packageRule` for `matchUpdateTypes=["patch"] + vulnerabilityFixes=true` that allows auto-merge on green CI. Bump `prHourlyLimit` carve-out for security label. |
| MEDIUM | #34 | `renovate.json:18` (`rangeStrategy: "bump"`) | `bump` mutates pyproject ranges directly on every minor/patch. Combined with the 6-major-wide `<25` pyarrow band (HIGH above), Renovate can land a `pyarrow=24.x` minor bump *without* re-pinning the upper band. | Switch to `rangeStrategy: "update-lockfile"` if a lockfile exists, otherwise `"pin"` for libs in `[project.dependencies]`. |
| MEDIUM | #35 | `brutex/math/__init__.pyi:30-41` | Stub re-declares 12 symbols with `*args: Any, **kwargs: Any` signatures. This **widens** the type surface for these public symbols vs the (currently unverified) Rust PyO3 signatures. If the Rust crate later adds strict signature enforcement, callers built against this stub will type-check but fail at runtime. Z+ "no false-OK" violation (charter §A rule 11). | Replace `*args: Any` with concrete signatures matching `rust/fno-math/src/lib.rs`. Or mark stubs `@overload` `NotImplemented`. |
| MEDIUM | #35 | `pyproject.toml:275-287` | Removing the `executionEnvironments` block is sound, BUT the surrounding global `[tool.pyright]` block still silences 12 categories including `reportPrivateUsage="none"` (L224). With test execution now under the same strict env, this allows tests to freely touch `_conn`, `_native`, `_make_placeholder` etc. across the whole repo. Over-broad silence masks accidental private-API consumers from third-party callers. | Narrow `reportPrivateUsage` from `"none"` to `"warning"` and add per-test-file `# pyright: ignore[reportPrivateUsage]` at the specific sites. |
| LOW | #33 | `tests/integration/test_instrument_store.py:_idx` `zlib.crc32(symbol.encode()) & 0xFFFFFF` | CRC32 is NOT crypto-safe and the masked 24-bit space (~16.7M) is small enough to collide for arbitrary symbol input. Test-only code path so no production injection vector. If a future PR reuses `_idx()` for a fuzzing harness, collisions silently break PK-uniqueness invariants. | Add a comment marking this as test-only OR widen to `& 0xFFFFFFFF`; consider `hashlib.blake2b(digest_size=4)`. |

**Verdict:** 2 HIGH + 3 MEDIUM warrant follow-up PRs. PR #37 (proposed)
should narrow pyarrow bound + wire vulnerability auto-merge. PR #38
should tighten the math stub signatures.

---

## Agent 3 — Correctness / Race / Audit findings

| Severity | PR | File:line | Finding | Disposition |
|---|---|---|---|---|
| HIGH | #33 | `tests/integration/test_instrument_store.py:817` (`TestIdempotenceStress.test_10x_upsert_same_batch_is_stable`) | False-OK: docstring + commit claim "rewrote with 4 Track 1 indices" but body STILL used FINNIFTY + `_make_chain()` FNO chains AND called `count(in_scope_only=False)`. Test passed silently. | **FIXED IN PR #36** — body rewritten to use 4 Track 1 indices (NIFTY/BANKNIFTY/SENSEX BSE/INDIAVIX), asserts both `in_scope_count == 4` AND `total_count == 4`. |
| HIGH | #33 | `docs/MIGRATION.md` DEC-P0-011 entry | **Golden Rule #1 hallucination**: cited `TestIdempotenceStress::test_partial_failure_rollback` as the test that got the `5 → 4` assertion change. NO SUCH METHOD EXISTS in any commit. The actual change was in `TestUniquenessAndDedup::test_verify_integrity_finds_no_duplicates_after_100_upserts`. | **FIXED IN PR #36** — MIGRATION.md entry corrected to cite the real method name with explicit "Corrected by DEC-P0-014 Agent 3 audit" note. |
| HIGH | #33 | `tests/integration/test_instrument_store.py:370` (`test_count_by_underlying_with_indices`) | Tautological assertion: only `assert isinstance(counts, dict)` — always true. Zero behavioral coverage. The only test of `count_by_underlying` in the suite. CG-01 coverage gate green-lit a broken method. | **FIXED IN PR #36** — replaced with `assert counts == {}` which pins the real schema invariant (IDX rows have NULL `underlying_symbol`, so SQL filter `WHERE underlying_symbol IS NOT NULL` returns empty). |
| MEDIUM | #34 | `pyproject.toml:46` + `.github/workflows/ci.yml:118` | Silent CI version drift: CI uses `pip install -e ".[dev]"` with `cache: pip`. Once mypy 2.x publishes a release matching `<3.0` AND the cache evicts, CI installs mypy 2.x on a non-deps PR. "Verified" claim used local version only. | Filed as follow-up PR #37 (consolidate with Agent 2 HIGH #1+#2). Add `constraints.txt`. |
| MEDIUM | #35 | `.github/workflows/ci.yml:120` (`pyright brutex`) | False-OK on "full project-wide strict mode" claim: CI runs `pyright brutex` only — NOT `pyright tests`. Strict mode for tests is enforced only by local-dev discipline. | **FIXED IN PR #36** — CI workflow updated to `pyright brutex tests` with rationale comment. |
| MEDIUM | #35 | `brutex/math/__init__.pyi:30-44` | Type erasure: all 12 symbols declared `(*args: Any, **kwargs: Any) -> Any`. Pyright accepts wrong args / zero args / anything. No production callers exist yet, so impact is roadmap-deferred. | Filed as follow-up PR #38 — tighten signatures once Rust crate surface stabilizes. |
| MEDIUM | #33 | `tests/integration/test_instrument_store.py:75-79` (`_idx` token fallback map) | Unverified securityIds for non-Track-1 indices: FINNIFTY=27, MIDCPNIFTY=28, NIFTYNXT50=29 not cited from Groww docs. Track 2 docs explicitly call these "UNVERIFIED". | Filed as follow-up PR #40 (consolidate with Agent 1 LOW #1 + Agent 2 LOW #1). |
| LOW | #33 | `tests/integration/test_instrument_store.py:611-621` (TestExpiriesTable + TestContractsTable) | Skip-marked indefinitely; no automated reactivation guard. | Filed as follow-up — replace `skip` with `xfail(strict=True)` when Track 2 Phase 1 starts. |
| LOW | #34 | PR body / commit message | Honest-envelope drift: claims "renovate.json (95 lines)" but actual is 231 lines. Trivial but a discipline lapse. | **DOCUMENTED** here; no PR-body edit to a merged PR (immutable history). Going-forward template captures the line-count discipline. |
| LOW | #35 | `tests/unit/test_strategies_registry.py:22-34` (`_make_strategy_class._TestStrat`) | Inconsistent pattern: this inline class STILL uses bare `META = StrategyMeta(...)` without `ClassVar` + `__init__`, while PR #35 added both to 8 other inline test strategies. Pyright doesn't check Protocol conformance here so no error fires, but the divergence is a maintenance hazard. | Filed as follow-up — apply same pattern for consistency. |

**Verdict on Agent 3:** 3 HIGH findings (including a Golden Rule #1
hallucination) ALL fixed IN this PR (#36) rather than deferred. Operator's
"100% testing coverage" + "100% bug fixing" + "no hallucination" demands
are now honored on the merged history via correction commits.

---

## Agent 4 — Cite/Path Verification findings

**0 failures / 15+ citations VERIFIED.**

| # | Citation | Source | Status |
|---|---|---|---|
| 1a-1g | 7 test classes in `test_instrument_store.py` | PR #33 | VERIFIED |
| 2 | `_idx()` per-symbol token map with verified Groww IDs | PR #33 | VERIFIED |
| 3 | 7 NEW pyproject bounds (structlog, pyarrow, pytest, pytest-cov, pytest-benchmark, mypy, mutmut) | PR #34 | VERIFIED |
| 4 | renovate.json with 13 packageRules | PR #34 | VERIFIED |
| 5 | `brutex/math/__init__.pyi` with 12 symbols | PR #35 | VERIFIED |
| 6 | 6 strategy classes with `META: ClassVar[StrategyMeta]` | PR #35 | VERIFIED |
| 7 | `# type: ignore[arg-type]` REMOVED from test_orchestrator_audit.py | PR #35 | VERIFIED |
| 8 | executionEnvironments block REMOVED from pyproject.toml | PR #35 | VERIFIED |
| 9 | PRs #27, #28, #30, #31, #32, #33, #34 merge commits | PR #35 closure table | VERIFIED |
| 10 | "1562 passed, 11 skipped" baseline | PRs #34 + #35 | VERIFIED (internally consistent) |
| 11 | "PyPI latest 2.1.0" mypy claim | PR #34 | PLAUSIBLE — confirmed via PyPI API |
| 12 | `:enablePreCommit` Renovate preset | PR #34 | VERIFIED |

**Verdict:** ZERO citation failures. Unlike the prior anti-pattern that
cited nonexistent test paths, every cited artifact in PRs #33/#34/#35
maps to a real symbol in current main.

---

## Follow-up PRs proposed

| PR # | Title | Severity | Findings |
|---|---|---|---|
| #37 | Z+ defensive — pyarrow narrow bound + vuln auto-merge | HIGH | Agent 2 #1 + #2 |
| #38 | Math facade stub — tighten signatures (.pyi rewrite) | MEDIUM | Agent 2 #4 |
| #39 | Pyright reportPrivateUsage narrowed to "warning" | MEDIUM | Agent 2 #5 |
| #40 | Hoist _idx() helper to module scope + crc32 widening | LOW (combined) | Agent 1 #1 + Agent 2 #6 |
| #41 | Perf canary CI job (weekly pip install -U) | LOW | Agent 1 #3 |

These are tracked in MIGRATION.md DEC-P0-014 and constitute the
discipline-enforcement closure.

---

## Honest envelope

- ✅ REAL: 3 of 4 agents returned findings; all in this doc.
- ✅ REAL: Agent 3 returned with **3 HIGH findings** including a Golden
  Rule #1 hallucination. All 3 are FIXED in this PR #36 (not deferred).
- ✅ REAL: 0 CRITICAL findings across all 4 agents. 5 HIGH total —
  3 fixed in-PR (Agent 3), 2 deferred to PR #37 (Agent 2: pyarrow + vuln auto-merge).
- ✅ REAL: Production hot-path modules UNTOUCHED across all 3 PRs.
- ✅ REAL: All 15+ citations verified against current main.
- 🟡 PARTIAL: This audit does NOT close the original discipline gap
  (the 4-agent review SHOULD have happened BEFORE/DURING those PRs, not
  after merge). Going-forward discipline is captured in
  `docs/zplus/ZPLUS_PR_DISCIPLINE.md` + enforced by
  `scripts/ci/zplus-pr-guard.sh`.

---

## Decision ID

DEC-P0-014 — `docs/MIGRATION.md` entry #152.
