# Red-team / Planted-Bug Test Harness

**Purpose:** Empirically prove that each CI gate actually blocks
violations. Per operator request 2026-05-16 night ("I don't have
the confidence dude") and 6-agent assurance round.

## What this directory proves

For every gate claimed in `docs/STANDARDS_100PCT.md` §1, there is
one or more redteam test that:

1. **Plants** a known violation in a sandbox fixture
2. **Runs** the actual gate tool (subprocess call to ruff / mypy / bandit / etc.)
3. **Asserts** the tool returned non-zero exit / flagged the violation

If the assertion FAILS, the gate is broken — silently passing
violations that should be caught. This is the only mechanism that
verifies the gate's claim is real.

## Why this approach

Per peer-reviewed literature (Rotten Green Tests, ICSE 2019):
- 294 of 19,905 Pharo tests had assertions that never executed
- Some "sleeping" 5+ years before detection
- > 25% of tests in some orgs had zero assertions

Per Trail of Bits 2025: "Use mutation testing to find the bugs your
tests don't catch."

**Configured ≠ Enforcing ≠ Effective.** The only way to know is to
plant a bug and verify it's caught.

## Coverage of CI gates

| Gate | Redteam test | Status |
|---|---|---|
| ruff lint | `test_redteam_ruff.py::test_ruff_catches_unused_import` | ✅ |
| ruff format | `test_redteam_ruff.py::test_ruff_format_catches_bad_indent` | ✅ |
| mypy strict | `test_redteam_mypy.py::test_mypy_catches_type_mismatch` | ✅ |
| bandit | `test_redteam_bandit.py::test_bandit_catches_eval_user_input` | ✅ |
| banned patterns (print) | `test_redteam_banned.py::test_scanner_catches_print` | ✅ |
| conventional commits | `test_redteam_conv.py::test_checker_rejects_bad_prefix` | ✅ |
| spec ↔ test crosswalk | `test_redteam_crosswalk.py::test_crosswalk_catches_missing_test` | ✅ |
| audit hash-chain integrity | `test_redteam_audit.py::test_chain_break_detected` | 📋 (needs `brutex doctor`) |
| combo dedup | `test_redteam_dedup.py::test_duplicate_write_rejected` | 📋 (needs LMDB store impl complete) |
| coverage gate | `test_redteam_coverage.py::test_uncovered_branch_fails_gate` | 📋 (needs CI integration) |
| O(1) perf regression | `test_redteam_perf.py::test_o_n_regression_fails_bench` | 📋 (needs benchmark baseline) |
| throughput canary | `test_redteam_throughput.py::test_sleep_breaks_canary` | 📋 (needs the canary test) |

Legend:
- ✅ implemented and asserting the gate works
- 📋 stub exists; needs corresponding infrastructure to be built first

## How to run

```bash
# Run all redteam tests locally
pytest tests/redteam/ -v

# Run only the actively-verifying tests
pytest tests/redteam/ -v -m "not roadmap"

# Run via CI nightly job
# See .github/workflows/redteam-nightly.yml
```

## Adding a new redteam test

1. Plant the violation in `tests/redteam/fixtures/<bug_name>.py`
2. Write the verifier in `tests/redteam/test_redteam_<gate>.py`
3. Use subprocess to call the actual gate tool
4. Assert non-zero exit code OR specific error message in output
5. Update this README with the new gate → test mapping

## What this is NOT

- NOT a substitute for the gate itself (ruff still runs on the package)
- NOT a substitute for property/mutation testing of business logic
- NOT a substitute for code review

It IS the empirical proof that "the gate exists" implies "the gate works."
