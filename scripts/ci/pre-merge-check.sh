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
REPO="${2:-SJParthi/bruteX}"
OVERRIDE_FLAG="${OPERATOR_OVERRIDE:-false}"
# Per Agent 5 of DEC-P0-004 hostile-review pass: a freshly-pushed PR may
# have empty statusCheckRollup (CI hasn't started registering jobs).
# If we don't gate on a minimum check count, the script falsely returns
# "all green" on empty rollup → false-merge approval (the exact PR #25
# failure mode this script exists to prevent). Set to the count of
# jobs in `ci-ok needs:` list (currently 20 hard gates).
EXPECTED_MIN_CHECKS="${EXPECTED_MIN_CHECKS:-20}"

# DEC-MERGE-LIVE-ROLLUP-001: a comma-separated allow-list of check-run NAMES
# to IGNORE in the rollup (count + failed + pending). Default EMPTY (no
# exclusion — every existing caller is byte-for-byte unchanged). The
# auto-merge workflow invokes this script as its final pre-merge gate, and
# its OWN check run ("Auto-merge on green CI") sits IN_PROGRESS on the head
# while the gate evaluates — that structurally-pending self-check would
# otherwise make the gate refuse forever. Excluding it (and only it) lets the
# gate see the COMPLETE CI rollup it actually cares about, while every real
# CI check still gates fully. The match is by EXACT name.
IGNORE_CHECK_NAMES="${IGNORE_CHECK_NAMES:-}"

# DEC-PREMERGE-M4PRO-NONBLOCK-001: check-run names that are NEVER allowed to
# block a merge. `m4pro-absolute-throughput` (the bench-m4pro.yml job id —
# the job has no `name:` field, so the rollup name IS the job id) runs on
# the self-hosted M4-Pro runner; when that runner is offline the check sits
# QUEUED/pending forever and this gate's fail-closed pending scan refused
# every touching PR indefinitely. It is genuinely NON-required: it is NOT
# in ci.yml's `ci-ok` aggregate-gate `needs:` list and the branch-protection
# required context is "CI OK (aggregate gate)" only (MIGRATION.md #382,
# DEC-M4PRO-PR-TRIGGER-001: "m4pro-* is NOT in the required CI OK (aggregate
# gate) set"; MIGRATION.md #1534, DEC-BENCH-ABS-BASELINE-001: "bench-m4pro
# stays a NON-required, non-PR check … a regression is caught post-merge …
# NOT as a merge blocker"). A bench regression is therefore caught
# POST-merge on the self-hosted runner (red run + auto-filed issue), never
# at this gate. This constant is deliberately HARDCODED — NOT env-overridable
# like IGNORE_CHECK_NAMES — so broadening the non-blocking set requires a
# code change + a new DEC, never a caller env tweak; and it is UNIONED with
# (never replaced by) the caller's IGNORE_CHECK_NAMES at all three scan
# sites, so auto-merge.yml's caller-SET IGNORE_CHECK_NAMES ("Auto-merge on
# green CI") cannot clobber it. The match is by EXACT name.
NONBLOCKING_CHECK_NAMES="m4pro-absolute-throughput"

# OPERATOR_OVERRIDE requires a non-empty reason (Agent 5 MED finding):
# default of "no reason provided" was useless for audit. Enforce.
if [[ "$OVERRIDE_FLAG" == "true" ]] && [[ -z "${OPERATOR_OVERRIDE_REASON:-}" ]]; then
    echo "ERROR: OPERATOR_OVERRIDE=true requires OPERATOR_OVERRIDE_REASON='<why>'." >&2
    echo "  e.g.: OPERATOR_OVERRIDE=true OPERATOR_OVERRIDE_REASON='one-time revert PR' $0 $PR_NUMBER" >&2
    exit 5
fi

if [[ -z "$PR_NUMBER" ]]; then
    echo "Usage: $0 <PR_NUMBER> [<owner/repo>]" >&2
    echo "  Set OPERATOR_OVERRIDE=true to bypass (logs the override)." >&2
    exit 2
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "===================================================================" >&2
    echo "❌ MERGE BLOCKED: gh CLI not available; cannot verify PR CI status." >&2
    echo "===================================================================" >&2
    echo "Per Z+ defensive doctrine (DEC-P0-004): missing-instrument means" >&2
    echo "FAIL CLOSED, never FAIL OPEN. A guard that silently allows merge" >&2
    echo "when it cannot measure is a Golden-Rule-#10 violation." >&2
    echo "" >&2
    echo "Fix: install gh CLI (https://cli.github.com) and re-run." >&2
    echo "Operator override: OPERATOR_OVERRIDE=true OPERATOR_OVERRIDE_REASON='<why>' $0 $PR_NUMBER" >&2

    if [[ "$OVERRIDE_FLAG" == "true" ]]; then
        OVERRIDE_LOG="${OVERRIDE_LOG_PATH:-$(dirname "$0")/pre-merge-overrides.log}"
        TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        REASON="${OPERATOR_OVERRIDE_REASON:-no reason provided}"
        echo "$TIMESTAMP PR=$PR_NUMBER REPO=$REPO REASON=$REASON FAILED=gh-cli-missing" \
            >> "$OVERRIDE_LOG"
        echo "⚠️  OPERATOR_OVERRIDE=true: bypassing gh-missing check. Logged to $OVERRIDE_LOG"
        exit 0
    fi
    exit 4
fi

# Query CI status via gh pr view
STATUS_JSON=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json statusCheckRollup,mergeable,number 2>/dev/null || echo "{}")

if [[ "$STATUS_JSON" == "{}" ]]; then
    echo "ERROR: Could not fetch PR #$PR_NUMBER status from $REPO." >&2
    exit 3
fi

# Check mergeable state (Agent 5 MED finding: was fetched but unused).
# CONFLICTING means merge conflicts; BLOCKED means branch protection rules
# refuse the merge. Both are hard failures.
MERGEABLE=$(echo "$STATUS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(data.get('mergeable', 'UNKNOWN'))
")

if [[ "$MERGEABLE" == "CONFLICTING" ]]; then
    echo "❌ MERGE BLOCKED: PR #$PR_NUMBER has merge conflicts with the target branch." >&2
    echo "Fix: rebase or merge the target branch into your feature branch + resolve conflicts." >&2
    if [[ "$OVERRIDE_FLAG" != "true" ]]; then
        exit 6
    fi
fi

# Parse status check rollup. DEC-MERGE-LIVE-ROLLUP-001: drop any check whose
# name is in IGNORE_CHECK_NAMES (default none) before counting / scanning, so
# the auto-merge path can exclude its own structurally-pending self-check
# without weakening the gate for any real CI check.
# DEC-PREMERGE-M4PRO-NONBLOCK-001: the hardcoded NONBLOCKING_CHECK_NAMES set
# is UNIONED in at every scan site, so it holds regardless of the caller's
# IGNORE_CHECK_NAMES value.
ROLLUP_COUNT=$(echo "$STATUS_JSON" | IGNORE_CHECK_NAMES="$IGNORE_CHECK_NAMES" NONBLOCKING_CHECK_NAMES="$NONBLOCKING_CHECK_NAMES" python3 -c "
import json, os, sys
data = json.load(sys.stdin)
ignore = {n.strip() for n in os.environ.get('IGNORE_CHECK_NAMES', '').split(',') if n.strip()}
ignore |= {n.strip() for n in os.environ.get('NONBLOCKING_CHECK_NAMES', '').split(',') if n.strip()}
rollup = [c for c in data.get('statusCheckRollup', []) if c.get('name', '?') not in ignore]
print(len(rollup))
")

# Agent 5 CRIT fix: empty rollup means CI hasn't started yet. Treat as
# PENDING, not GREEN. Refuse merge until at least EXPECTED_MIN_CHECKS
# jobs have registered (default 20 per ci-ok needs: list).
if [[ "$ROLLUP_COUNT" -lt "$EXPECTED_MIN_CHECKS" ]]; then
    echo "===================================================================" >&2
    echo "❌ MERGE BLOCKED: PR #$PR_NUMBER has only $ROLLUP_COUNT CI checks" >&2
    echo "registered (expected ≥ $EXPECTED_MIN_CHECKS). CI may not have" >&2
    echo "started, or some jobs failed to register." >&2
    echo "===================================================================" >&2
    echo "Per Z+ defensive doctrine (DEC-P0-004, Agent 5 CRIT fix):" >&2
    echo "empty/incomplete rollup means we CANNOT measure CI status — fail" >&2
    echo "CLOSED. Wait for all $EXPECTED_MIN_CHECKS jobs to register + complete." >&2
    if [[ "$OVERRIDE_FLAG" != "true" ]]; then
        exit 7
    fi
fi

FAILED_CHECKS=$(echo "$STATUS_JSON" | IGNORE_CHECK_NAMES="$IGNORE_CHECK_NAMES" NONBLOCKING_CHECK_NAMES="$NONBLOCKING_CHECK_NAMES" python3 -c "
import json, os, sys
data = json.load(sys.stdin)
ignore = {n.strip() for n in os.environ.get('IGNORE_CHECK_NAMES', '').split(',') if n.strip()}
ignore |= {n.strip() for n in os.environ.get('NONBLOCKING_CHECK_NAMES', '').split(',') if n.strip()}
rollup = [c for c in data.get('statusCheckRollup', []) if c.get('name', '?') not in ignore]
failed = [c.get('name', '?') for c in rollup
          if c.get('conclusion') in ('FAILURE', 'CANCELLED', 'TIMED_OUT')]
print('\n'.join(failed))
")

# DEC-PREMERGE-M4PRO-NONBLOCK-001: WARN-ONLY visibility for a NONBLOCKING
# check that sits in a failed state. It NEVER affects the exit code — a
# non-required bench regression is caught post-merge on the self-hosted
# runner (DEC-BENCH-ABS-BASELINE-001), but the operator should still SEE it
# at merge time. A pending/absent non-blocking check is silently skipped
# (that is what ignoring means).
NONBLOCKING_FAILED=$(echo "$STATUS_JSON" | NONBLOCKING_CHECK_NAMES="$NONBLOCKING_CHECK_NAMES" python3 -c "
import json, os, sys
data = json.load(sys.stdin)
nonblocking = {n.strip() for n in os.environ.get('NONBLOCKING_CHECK_NAMES', '').split(',') if n.strip()}
failed = [c.get('name', '?') for c in data.get('statusCheckRollup', [])
          if c.get('name', '?') in nonblocking
          and c.get('conclusion') in ('FAILURE', 'CANCELLED', 'TIMED_OUT')]
print('\n'.join(failed))
" || true)

if [[ -n "$NONBLOCKING_FAILED" ]]; then
    echo "⚠️  Non-blocking check(s) in a failed state (never block merge — DEC-PREMERGE-M4PRO-NONBLOCK-001):"
    echo "$NONBLOCKING_FAILED" | sed 's/^/  - /'
fi

if [[ -n "$FAILED_CHECKS" ]]; then
    echo "===================================================================="
    echo "❌ MERGE BLOCKED: PR #$PR_NUMBER has FAILED CI checks:"
    echo "===================================================================="
    echo "$FAILED_CHECKS" | sed 's/^/  - /'
    echo "===================================================================="

    if [[ "$OVERRIDE_FLAG" == "true" ]]; then
        OVERRIDE_LOG="${OVERRIDE_LOG_PATH:-$(dirname "$0")/pre-merge-overrides.log}"
        TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        REASON="${OPERATOR_OVERRIDE_REASON:-no reason provided}"

        # DEC-P0-008 (PR #30, Z+ L7 COOLDOWN): rate-limit overrides to prevent
        # the escape hatch from becoming the default merge path. Thresholds
        # (override-able via env for tests):
        #   - Warn at 2 overrides in last 24h (encourages root-cause fix)
        #   - Hard-fail at 5 overrides in last 24h OR 10 in last 7 days
        #   - Always counts — never silently allows >threshold without explicit
        #     2nd-level operator escalation token
        OVERRIDE_24H_WARN="${OVERRIDE_24H_WARN:-2}"
        OVERRIDE_24H_HARD="${OVERRIDE_24H_HARD:-5}"
        OVERRIDE_7D_HARD="${OVERRIDE_7D_HARD:-10}"
        OVERRIDE_ESCALATION_TOKEN="${OPERATOR_OVERRIDE_ESCALATION:-}"

        if [[ -f "$OVERRIDE_LOG" ]]; then
            # Count overrides in last 24h + 7d. Each log line starts with
            # an ISO8601 UTC timestamp; we filter via Python for date math
            # rather than fragile bash arithmetic.
            count_24h=$(python3 -c "
import datetime, sys
from pathlib import Path
log = Path('$OVERRIDE_LOG')
if not log.exists():
    print(0); sys.exit(0)
cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24)
n = 0
for line in log.read_text().splitlines():
    if not line.strip():
        continue
    ts_str = line.split()[0]
    try:
        ts = datetime.datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        if ts >= cutoff:
            n += 1
    except ValueError:
        continue
print(n)
")
            count_7d=$(python3 -c "
import datetime, sys
from pathlib import Path
log = Path('$OVERRIDE_LOG')
if not log.exists():
    print(0); sys.exit(0)
cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=7)
n = 0
for line in log.read_text().splitlines():
    if not line.strip():
        continue
    ts_str = line.split()[0]
    try:
        ts = datetime.datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        if ts >= cutoff:
            n += 1
    except ValueError:
        continue
print(n)
")
            if [[ "$count_24h" -ge "$OVERRIDE_24H_HARD" ]] || [[ "$count_7d" -ge "$OVERRIDE_7D_HARD" ]]; then
                echo "===================================================================" >&2
                echo "❌ MERGE BLOCKED: OPERATOR_OVERRIDE rate limit exceeded." >&2
                echo "===================================================================" >&2
                echo "  Overrides in last 24h: $count_24h (hard limit: $OVERRIDE_24H_HARD)" >&2
                echo "  Overrides in last 7d:  $count_7d (hard limit: $OVERRIDE_7D_HARD)" >&2
                echo "" >&2
                echo "Per Z+ L7 COOLDOWN doctrine (DEC-P0-008): the override mechanism" >&2
                echo "exists for RARE one-off operator-authorized bypasses, not as a" >&2
                echo "default merge path. Repeated overrides indicate a gate that needs" >&2
                echo "FIXING, not bypassing." >&2
                echo "" >&2
                echo "Required actions:" >&2
                echo "  1. Identify which gate is causing repeated overrides" >&2
                echo "  2. Address the root cause (write tests, fix bugs, redesign gate)" >&2
                echo "  3. OR: provide a 2nd-level operator escalation token:" >&2
                echo "     OPERATOR_OVERRIDE_ESCALATION='I-acknowledge-this-bypasses-the-cooldown'" >&2
                echo "     (the literal string above; copying-pasting is the friction-by-design)" >&2
                if [[ "$OVERRIDE_ESCALATION_TOKEN" != "I-acknowledge-this-bypasses-the-cooldown" ]]; then
                    exit 8  # NEW exit code: rate-limit-exceeded without escalation
                fi
                echo "" >&2
                echo "⚠️  ESCALATION TOKEN provided — proceeding with bypass. Logged with marker." >&2
                REASON="$REASON [ESCALATED — rate-limit-bypassed]"
            elif [[ "$count_24h" -ge "$OVERRIDE_24H_WARN" ]]; then
                echo "⚠️  WARNING: $count_24h overrides in last 24h (warn threshold: $OVERRIDE_24H_WARN)." >&2
                echo "    Consider root-cause-fixing the gate instead of bypassing again." >&2
            fi
        fi

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
PENDING_CHECKS=$(echo "$STATUS_JSON" | IGNORE_CHECK_NAMES="$IGNORE_CHECK_NAMES" NONBLOCKING_CHECK_NAMES="$NONBLOCKING_CHECK_NAMES" python3 -c "
import json, os, sys
data = json.load(sys.stdin)
ignore = {n.strip() for n in os.environ.get('IGNORE_CHECK_NAMES', '').split(',') if n.strip()}
ignore |= {n.strip() for n in os.environ.get('NONBLOCKING_CHECK_NAMES', '').split(',') if n.strip()}
rollup = [c for c in data.get('statusCheckRollup', []) if c.get('name', '?') not in ignore]
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
