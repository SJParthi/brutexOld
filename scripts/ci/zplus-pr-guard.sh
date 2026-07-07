#!/usr/bin/env bash
# scripts/ci/zplus-pr-guard.sh — mechanical ratchet for Z+ defensive discipline.
#
# Per operator decree 2026-05-17 ("i need the gauarnteed z plus defensive
# layer motherfucker") + DEC-P0-014 (PR #36): every PR opened by Claude
# MUST carry the Z+ matrices + 4-agent review evidence in the body BEFORE
# the merge gate accepts it.
#
# This script reads a PR body (passed on stdin, or via $1 path) and refuses
# to exit 0 unless ALL of the following marker phrases are present:
#
#   1. "Z+ 15-row matrix"        (from operator-charter-forever.md §C)
#   2. "Z+ 7-row resilience matrix"
#   3. "Z+ 7-layer audit"        (DETECT/VERIFY/RECONCILE/PREVENT/AUDIT/RECOVER/COOLDOWN)
#   4. "4-agent hostile review"  (Plan + Explore + general-purpose × 2)
#   5. "Honest envelope"          (Golden Rule #10 envelope qualifier)
#   6. "Decision ID"              (DEC-P0-NNN reference)
#
# Failure mode is FAIL-CLOSED (exit code 1), per Z+ doctrine. Operator
# override via OPERATOR_OVERRIDE=true OPERATOR_OVERRIDE_REASON="<why>" —
# logs the bypass to scripts/ci/pre-merge-overrides.log (same audit file
# as pre-merge-check.sh).
#
# Per CLAUDE.md Golden Rule #10 (honest limits) + Rule #12 (100% standards):
# the matrices are EVIDENCE of the discipline, not the discipline itself.
# A green-stamped PR with missing matrices means the agent skipped review.

set -euo pipefail

PR_BODY_FILE="${1:-/dev/stdin}"
OVERRIDE_FLAG="${OPERATOR_OVERRIDE:-false}"
OVERRIDE_LOG="${OVERRIDE_LOG_PATH:-$(dirname "$0")/pre-merge-overrides.log}"
# DEC-P0-058 (PR-N+5): unconditional bot exemption for dependabot[bot]
# + renovate[bot]. These bots open only dep-bump PRs by design — they
# cannot carry the 6-marker Z+ body that human/Claude PRs carry. The
# pre-DEC-P0-058 version of this guard required a paired `security`
# label, which made routine cargo / pip / github-actions bumps un-
# mergeable without manual label application — defeating the operator
# decree "always automated + always latest".
#
# Threat model preserved: ALL non-bot actors still get full Z+ marker
# enforcement. The exemption is bot-scoped + author-pattern-pinned,
# not a blanket bypass. Substantive CI safety remains in the CI-OK
# aggregate gate (auto-merge.yml requires every check green).
#
# `PR_HAS_SECURITY_LABEL` env var still accepted for backwards-compat
# with any caller that sets it; the value is ignored.
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

# DEC-P0-058 (PR-N+5): author-pattern exemption only — see header.
if [[ -n "$PR_AUTHOR" ]] \
    && [[ "$PR_AUTHOR" =~ $PR_AUTHOR_EXEMPT_PATTERN ]]; then
    echo "Z+ PR guard: bot-exempt path — author=$PR_AUTHOR"
    # Audit log every exempt bypass so the operator can review.
    TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "$TIMESTAMP TOOL=zplus-pr-guard EXEMPT=bot AUTHOR=$PR_AUTHOR" \
        >> "$OVERRIDE_LOG"
    exit 0
fi

REQUIRED_MARKERS=(
    "Z+ 15-row matrix"
    "Z+ 7-row resilience matrix"
    "Z+ 7-layer audit"
    "4-agent hostile review"
    "Honest envelope"
    "Decision ID"
)

MISSING=()
for marker in "${REQUIRED_MARKERS[@]}"; do
    if ! grep -qiF "$marker" <<<"$BODY"; then
        MISSING+=("$marker")
    fi
done

if [[ ${#MISSING[@]} -eq 0 ]]; then
    echo "Z+ PR guard: all 6 required markers present"
    exit 0
fi

# At least one marker missing — fail closed or honor override.
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "====================================================================" >&2
echo "Z+ PR GUARD: BLOCKED" >&2
echo "====================================================================" >&2
echo "Missing required Z+ discipline markers in PR body:" >&2
for m in "${MISSING[@]}"; do
    echo "  - $m" >&2
done
echo "" >&2
echo "Per operator-charter-forever.md §C + DEC-P0-014: every PR MUST" >&2
echo "carry these matrices + review evidence. See" >&2
echo "docs/zplus/ZPLUS_PR_DISCIPLINE.md for the template." >&2
echo "" >&2
echo "Override: OPERATOR_OVERRIDE=true OPERATOR_OVERRIDE_REASON='<why>' $0 $PR_BODY_FILE" >&2

if [[ "$OVERRIDE_FLAG" == "true" ]]; then
    REASON="${OPERATOR_OVERRIDE_REASON}"
    MISSING_JOINED="$(IFS=','; echo "${MISSING[*]}")"
    echo "$TIMESTAMP TOOL=zplus-pr-guard REASON=$REASON MISSING=$MISSING_JOINED" \
        >> "$OVERRIDE_LOG"
    echo "OPERATOR_OVERRIDE=true: bypass logged to $OVERRIDE_LOG" >&2
    exit 0
fi

exit 1
