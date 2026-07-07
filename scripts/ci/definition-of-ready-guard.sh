#!/usr/bin/env bash
# scripts/ci/definition-of-ready-guard.sh — the PLAN-FIRST gate.
#
# Per operator decree 2026-06-05 ("until or unless thoroughly designing /
# deriving an extensive plan by covering everything we should never ever go
# ahead with implementation") + DEC-DEFINITION-OF-READY-001: every PR MUST
# prove — in its body, BEFORE code is accepted — that the change was planned
# against every scenario and failure mode, with a real-run proof and a
# rollback. This is the mechanical complement to the Definition of DONE
# (the ci-ok merge gates): a Definition of READY.
#
# This script reads a PR body (passed on stdin, or via $1 path) and refuses
# to exit 0 unless ALL of the following section markers are present:
#
#   1. "Scenarios covered"  (happy + edge + failure + empty + concurrent)
#   2. "Failure modes"      (what the system does on each)
#   3. "Real-run proof"     (the exact command that demonstrates it working)
#   4. "Rollback plan"      (how to undo it)
#   5. "Out of scope"       (explicit — so silent scope-creep is impossible)
#
# The gate enforces the EXISTENCE + COMPLETENESS of the plan, not its wisdom.
# Per CLAUDE.md Golden Rule #10 (honest limits): a green stamp means the five
# sections are present, NOT that the plan is correct — that remains human
# judgement. A blank section means the author skipped planning, and the gate
# fails closed.
#
# Failure mode is FAIL-CLOSED (exit 1), mirroring scripts/ci/zplus-pr-guard.sh.
# Operator override via OPERATOR_OVERRIDE=true OPERATOR_OVERRIDE_REASON="<why>"
# — logs the bypass to the same audit file as the other PR-body guards.
# renovate[bot] / dependabot[bot] are author-exempt (they open only dep-bump
# PRs and structurally cannot carry a design plan), mirroring DEC-P0-058.

set -euo pipefail

PR_BODY_FILE="${1:-/dev/stdin}"
OVERRIDE_FLAG="${OPERATOR_OVERRIDE:-false}"
OVERRIDE_LOG="${OVERRIDE_LOG_PATH:-$(dirname "$0")/pre-merge-overrides.log}"
PR_AUTHOR="${PR_AUTHOR:-}"
PR_AUTHOR_EXEMPT_PATTERN="${PR_AUTHOR_EXEMPT_PATTERN:-^(renovate\[bot\]|dependabot\[bot\]|github-actions\[bot\])$}"

if [[ "$OVERRIDE_FLAG" == "true" ]] && [[ -z "${OPERATOR_OVERRIDE_REASON:-}" ]]; then
    echo "ERROR: OPERATOR_OVERRIDE=true requires OPERATOR_OVERRIDE_REASON='<why>'" >&2
    exit 5
fi

if [[ ! -r "$PR_BODY_FILE" ]] && [[ "$PR_BODY_FILE" != "/dev/stdin" ]]; then
    echo "ERROR: cannot read PR body from '$PR_BODY_FILE'" >&2
    exit 2
fi

BODY="$(cat "$PR_BODY_FILE")"

# DEC-P0-058 pattern: author-pattern exemption only — bots cannot carry a plan.
if [[ -n "$PR_AUTHOR" ]] \
    && [[ "$PR_AUTHOR" =~ $PR_AUTHOR_EXEMPT_PATTERN ]]; then
    echo "Definition-of-Ready guard: bot-exempt path — author=$PR_AUTHOR"
    TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "$TIMESTAMP TOOL=definition-of-ready-guard EXEMPT=bot AUTHOR=$PR_AUTHOR" \
        >> "$OVERRIDE_LOG"
    exit 0
fi

REQUIRED_MARKERS=(
    "Scenarios covered"
    "Failure modes"
    "Real-run proof"
    "Rollback plan"
    "Out of scope"
)

MISSING=()
for marker in "${REQUIRED_MARKERS[@]}"; do
    if ! grep -qiF "$marker" <<<"$BODY"; then
        MISSING+=("$marker")
    fi
done

if [[ ${#MISSING[@]} -eq 0 ]]; then
    echo "Definition-of-Ready guard: all 5 plan-first sections present"
    exit 0
fi

# At least one section missing — fail closed or honor override.
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "====================================================================" >&2
echo "DEFINITION-OF-READY GUARD: BLOCKED" >&2
echo "====================================================================" >&2
echo "The PR body is missing required plan-first sections:" >&2
for m in "${MISSING[@]}"; do
    echo "  - $m" >&2
done
echo "" >&2
echo "Per DEC-DEFINITION-OF-READY-001: no implementation is accepted until" >&2
echo "the plan covers every scenario + failure mode, with a real-run proof" >&2
echo "and a rollback. See docs/DEFINITION_OF_READY.md for the template." >&2
echo "" >&2
echo "Override: OPERATOR_OVERRIDE=true OPERATOR_OVERRIDE_REASON='<why>' $0 $PR_BODY_FILE" >&2

if [[ "$OVERRIDE_FLAG" == "true" ]]; then
    REASON="${OPERATOR_OVERRIDE_REASON}"
    MISSING_JOINED="$(IFS=','; echo "${MISSING[*]}")"
    echo "$TIMESTAMP TOOL=definition-of-ready-guard REASON=$REASON MISSING=$MISSING_JOINED" \
        >> "$OVERRIDE_LOG"
    echo "OPERATOR_OVERRIDE=true: bypass logged to $OVERRIDE_LOG" >&2
    exit 0
fi

exit 1
