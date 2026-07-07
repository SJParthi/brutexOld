# Pull Request — BruteX

> **Merge-Readiness Charter (DEC-MERGE-GUARANTEE-001):** this PR obeys
> [`docs/MERGE_READINESS_GUARANTEE.md`](../docs/MERGE_READINESS_GUARANTEE.md)
> — the §3 gate matrix + §4 Definition of Done. **Never merge over a red or
> pending gate** (`mergeable_state` within the charter §4.5 envelope —
> strict `clean` once G2 branch protection lands; `dirty`/`behind`/
> `blocked`/`unknown` always refused, DEC-AUTOMERGE-CLEAN-STRICT-001).
> Per Golden Rule #10,
> do not claim "100% / fully / guaranteed" for any partial/roadmap/out-of-scope
> row; disclose it in the Honest envelope.

## Definition of Ready (plan-first gate — DEC-DEFINITION-OF-READY-001)

<!-- All five sections are REQUIRED. A blank section fails the
`definition-of-ready` CI gate. See docs/DEFINITION_OF_READY.md. -->

### Scenarios covered
<!-- happy + edge + empty/zero + failure + concurrent — list them -->

### Failure modes
<!-- each way it can fail, and what the system does on each -->

### Real-run proof
<!-- the EXACT command that shows it working against reality (not "tests pass") -->

### Rollback plan
<!-- how to undo this safely -->

### Out of scope
<!-- what this PR explicitly does NOT do (Golden Rule #3) -->

---

## Summary

<!-- 1-3 bullets: what changed + why -->

## Spec / decision links

<!-- Every locked decision must reference its DEC-* ID and MIGRATION.md row -->

- Decision ID(s): DEC-...
- MIGRATION.md row(s): #...
- Spec section(s) touched:

## Type of change

- [ ] feat — new feature
- [ ] fix — bug fix (includes regression test that FAILED before this PR)
- [ ] docs — documentation only
- [ ] refactor — no behavior change
- [ ] test — test-only
- [ ] chore — tooling / CI / build
- [ ] perf — performance improvement

## Golden Rule #12 — 100% standards self-check

Mark each box before requesting review. CI gates enforce these
automatically — this checklist is a pre-flight, not the gate itself.

### Coverage + tests
- [ ] Coverage line ≥ 100% on changed `brutex/` files (new modules) OR ≥ 90% on legacy modules
- [ ] Coverage branch ≥ 100% on changed files
- [ ] Property tests added if logic has > 2 branches
- [ ] Golden fixtures added if output is byte-significant
- [ ] Benchmark added if hot path (O(1) gate)

### Bug fixes
- [ ] Regression test added that FAILS without the fix
      (CI-enforced on `fix:`-titled PRs: `bugfix-reversion-check` reverts your
      `brutex/` changes to merge-base and requires the shipped tests to fail —
      DEC-TEST-APPARATUS-DEBTS-001(c))
- [ ] Issue / DEC-ID referenced above

### Type / lint / security
- [ ] `mypy --strict` passes locally
- [ ] `pyright` passes locally
- [ ] `ruff check` passes locally
- [ ] `bandit` shows 0 high/critical
- [ ] No new copy-paste vs existing code (vulture + manual scan)

### Performance
- [ ] No new O(n) where O(1) was achievable
- [ ] No new pandas in hot path (polars / numpy / Rust only)
- [ ] No new global mutable state

### Observability
- [ ] Material state changes emit an audit JSONL row
- [ ] Errors use structlog, never `print`
- [ ] If a new metric: added to Prometheus exporter
- [ ] If a new CRITICAL condition: alert wired to Telegram

### Documentation
- [ ] Public symbols have NumPy-style docstrings
- [ ] Specs updated if behavior contract changed
- [ ] Amendment log in spec updated with date + reason
- [ ] No silent scope expansion (Golden Rule #3)

### No hallucination (Golden Rule #1)
- [ ] Every factual claim in code comments / docs has a primary source OR is flagged UNVERIFIED
- [ ] No invented rate, threshold, or constant — all sourced from `COSTS_VERIFIED.md` / `MIGRATION.md` / primary docs

## Test plan

<!-- How to verify this PR locally + what was tested -->

- [ ] `pytest tests/unit tests/property` passes
- [ ] `pytest tests/integration` passes (if integration code touched)
- [ ] `pytest tests/bench -m bench` passes (if hot-path code touched)
- [ ] Manual smoke test if applicable

## Operator action required (post-merge)

<!-- List any operator action — e.g. mount SSD, paste token, run migration -->

- None / or list

---

<!-- CI gates that will run on push (do not delete) -->

CI will enforce:
- 100% pass rate on unit + integration + property + golden + bench
- Coverage gate (per pyproject.toml fail_under)
- mypy --strict + pyright + ruff + bandit + vulture + gitleaks
- Mutation score on critical modules (weekly + nightly)
- Conventional commit message format
- No banned patterns (print, bare except, pandas-in-hot-path)
- O(1) NFR gates on declared hot paths

Any red gate BLOCKS this PR. Per Golden Rule #12, operator override requires ADR.
