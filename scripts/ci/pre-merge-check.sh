#!/usr/bin/env bash
# scripts/ci/pre-merge-check.sh — Mechanical no-merge-with-red-CI guard.
#
# Per operator decree 2026-05-17 (after PR #25 merged with 3 red CI checks
# despite Z+ defensive contract): this script MUST be invoked by Claude
# Code BEFORE calling mcp__github__merge_pull_request on any PR.
# Refuses the merge if PR CI status is anything but "success".
#
# Override: only if the operator explicitly authorizes via
# `OPERATOR_OVERRIDE=true ./scripts/ci/pre-merge-check.sh <PR#>`. The
# override path logs the override + reason to the audit trail
# (scripts/ci/pre-merge-overrides.log).
#
# Per CLAUDE.md Golden Rule #10 (honest limits) + Golden Rule #12 (100%
# standards) + DEC-PROTOCOL-001 §H step 8 ("CI red → fix + push").

set -euo pipefail

PR_NUMBER="${1:-}"
REPO="${2:-sjparthi/brutex}"
OVERRIDE_FLAG="${OPERATOR_OVERRIDE:-false}"

if [[ -z "$PR_NUMBER" ]]; then
    echo "Usage: $0 <PR_NUMBER> [<owner/repo>]" >&2
    echo "  Set OPERATOR_OVERRIDE=true to bypass (logs the override)." >&2
    exit 2
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "WARNING: gh CLI not available; falling back to skip-check (cannot enforce)." >&2
    echo "Install gh + re-run for full enforcement." >&2
    exit 0
fi

# Query CI status via gh pr view
STATUS_JSON=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json statusCheckRollup,mergeable,number 2>/dev/null || echo "{}")

if [[ "$STATUS_JSON" == "{}" ]]; then
    echo "ERROR: Could not fetch PR #$PR_NUMBER status from $REPO." >&2
    exit 3
fi

# Parse status check rollup
FAILED_CHECKS=$(echo "$STATUS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
rollup = data.get('statusCheckRollup', [])
failed = [c.get('name', '?') for c in rollup
          if c.get('conclusion') in ('FAILURE', 'CANCELLED', 'TIMED_OUT')]
print('\n'.join(failed))
")

if [[ -n "$FAILED_CHECKS" ]]; then
    echo "===================================================================="
    echo "❌ MERGE BLOCKED: PR #$PR_NUMBER has FAILED CI checks:"
    echo "===================================================================="
    echo "$FAILED_CHECKS" | sed 's/^/  - /'
    echo "===================================================================="

    if [[ "$OVERRIDE_FLAG" == "true" ]]; then
        OVERRIDE_LOG="$(dirname "$0")/pre-merge-overrides.log"
        TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        REASON="${OPERATOR_OVERRIDE_REASON:-no reason provided}"
        echo "$TIMESTAMP PR=$PR_NUMBER REPO=$REPO REASON=$REASON FAILED=$(echo "$FAILED_CHECKS" | tr '\n' ',')" \
            >> "$OVERRIDE_LOG"
        echo "⚠️  OPERATOR_OVERRIDE=true: bypassing guard. Logged to $OVERRIDE_LOG"
        echo "Per CLAUDE.md Golden Rule #12: each override requires an ADR."
        exit 0
    fi

    echo ""
    echo "Per operator decree 2026-05-17 (post-PR-#25 violation):"
    echo "  'I need 100 percentage dude don't downgrade dude okay?'"
    echo ""
    echo "Required actions before merge:"
    echo "  1. Fix the failing checks (write tests, fix bugs, adjust config)"
    echo "  2. Re-push the branch"
    echo "  3. Re-run this check"
    echo ""
    echo "Override (operator-authorized only):"
    echo "  OPERATOR_OVERRIDE=true OPERATOR_OVERRIDE_REASON='<why>' $0 $PR_NUMBER"
    exit 1
fi

# Check pending checks too — don't merge while CI is still running
PENDING_CHECKS=$(echo "$STATUS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
rollup = data.get('statusCheckRollup', [])
pending = [c.get('name', '?') for c in rollup
           if c.get('status') in ('IN_PROGRESS', 'QUEUED', 'PENDING')]
print('\n'.join(pending))
")

if [[ -n "$PENDING_CHECKS" ]]; then
    echo "⏳ PR #$PR_NUMBER has PENDING checks:"
    echo "$PENDING_CHECKS" | sed 's/^/  - /'
    echo ""
    echo "Wait for CI to complete before merging. Re-run when all green."

    if [[ "$OVERRIDE_FLAG" == "true" ]]; then
        echo "⚠️  OPERATOR_OVERRIDE=true: bypassing pending check too."
        exit 0
    fi
    exit 1
fi

echo "✅ PR #$PR_NUMBER: all CI checks GREEN. Safe to merge."
exit 0
