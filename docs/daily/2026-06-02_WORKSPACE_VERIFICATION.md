# Workspace Verification — 2026-06-02 (5-agent deep pass)

> Operator ask: *"deep thorough research by activating all agents and all
> sub-agents … confirming the entire workspace works as expected."* This is the
> durable record of that pass, run against the merged tip `6bf814d`
> (`claude/peaceful-brahmagupta-OL4QO` == `origin/main`) in a faithful CI env
> (Python 3.12 venv + `[dev,data,ui]` + the Rust `fno_math` kernel).
> Honesty-first (CLAUDE.md Golden Rule #10): green is stated as green, gaps as gaps.

---

## 0. Headline

**The merge that was requested is already done.** `origin/main` was force-updated
to `6bf814d` (the branch tip); the branch is **0 ahead / 0 behind** `main` and
there are **zero open PRs**. The earlier "unrelated histories / no merge base"
appearance was a **shallow-clone artifact** (depth-50), resolved by unshallowing.

**The workspace is green on every CI gate it enforces**, with a small set of
**honest, pre-existing gaps** (below). It is domain-correct on every locked rule.

---

## 1. Gate results (independently re-run)

| Area | Result | Evidence |
|---|---|---|
| Test suite | ✅ **3,126 passed, 45 skipped, 1 xfail, 0 failures** (~82s) | `pytest -q` |
| Coverage | ✅ **97.84% line+branch ≥ 90% gate** + per-module strict-100 gates | `pytest --cov=brutex --cov-branch` |
| Lint | ✅ clean | `ruff check brutex tests` |
| Format | ✅ clean | `ruff format --check` (322 files) |
| Types (mypy) | ✅ **no issues in 150 files** | `mypy --strict brutex` |
| Types (pyright) | ✅ in CI (see §3 caveat) | `pyright brutex tests` |
| Security | ✅ no issues | `bandit -r brutex -ll` |
| Docstrings | ✅ **100%** on the gated surface | `interrogate … --fail-under=100` |
| O(1) benchmarks | ✅ pass | `pytest tests/bench` (see §3 tolerance note) |
| Dedup (O(1) uniqueness) | ✅ Bloom → LMDB → blake3, no false drops | `tests/property/test_bloom_invariants.py` |
| Domain invariants | ✅ 8/8 hold | scope-lock, VIX reference-only, no-look-ahead, audit chain, money 2dp |

---

## 2. CI / merge-readiness map (what actually blocks a PR)

The required `CI OK (aggregate gate)` aggregates **24 jobs**: lint, type, test,
property, bench, golden, security, docs, licenses, rust, banned-patterns,
secrets/gitleaks, codeql, semgrep, trivy, sbom, vulture, jscpd duplication,
mutation-py, proof-bundle, **zplus-pr-guard, dec-id-validator,
bugfix-regression-scan, no-hallucination-scan**. Smoke + red-team now run
in the per-PR `test` job (landed #280). This is a genuinely broad, real gate.

---

## 3. Honest gaps (pre-existing — tracked, not introduced here)

1. **O(1) bench tolerance is 5–10×, not the decreed 1.1×.** Deliberate
   shared-GHA flake mitigation (DEC-P0-020); the 1.1× target needs the
   self-hosted M4 Pro runner. Documented in `STANDARDS_100PCT.md`.
2. **The brute-force sweep engine (Stage 1A→1B→2→3→4) is not built yet.** The
   per-bar / per-mask / dedup / rate-limit **primitives** exist and are
   O(1)-gated; the cascade is Phase 3. The 33B-trial / 2–4-day SLA is an
   `xfail` target until then.
3. **pyright masked gate:** the CI `type` job installed only `.[dev]` (no `[ui]`),
   so `brutex/dashboard/` + the data paths were not faithfully type-checked in CI.
   Same shape as the #376 test-masking finding. **✅ CLOSED by DEC-PYRIGHT-UNMASK-001
   (#379):** the type job now installs `[dev,data,ui]`; the 10 surfaced errors
   (1 pydantic `reportUntypedBaseClass` + 9 Hypothesis `@composite` false-positives)
   are silenced per-site/per-file with rationale, and a regression guard prevents
   re-masking. `pyright brutex tests` = 0 errors.
4. **"int64 paisa storage" is overstated:** OHLC + PnL are stored as `Decimal`
   rupees (Parquet `Float64`); int64 paisa lives in the costs calculator +
   money display. Numerically safe; the doc claim in CLAUDE.md §6 / `money.py`
   should be reconciled. **Flagged for operator decision.**
5. **Coverage tail:** `dashboard/app.py` 74.7%, `dashboard/__init__.py` 45%
   (un-omitted in #280, below the 90% floor but offset by the omit list);
   `cli.py`, `__main__.py`, `data/catalog.py`, `data/instrument_store.py`,
   `execution/driver.py` remain omitted (dedicated-test follow-ups).
6. **No server-side branch protection:** auto-merge uses a script-side
   check-run lookup; the `CI OK` required-check lock is honored by convention.
   **Tracked follow-up.**
7. **Operator docs were stale (Groww):** `make setup` was broken + auth runbooks
   pointed at the wrong vendor — **fixed in this PR** (DEC-DHAN-DOC-RECONCILE-001).

---

## 4. Verdict

The engine's data/ingest/dedup/rate-limit foundation is **real, O(1)-gated (at
realistic CI tolerances), domain-correct, and merge-ready by its own charter**.
The honest deltas are the aspirational-1.1× tolerance, the not-yet-built sweep
engine (the next phase), and a handful of tracked masked/omit gaps — none of
which are regressions and all of which are disclosed in `MIGRATION.md` and
`STANDARDS_100PCT.md`.
