#!/usr/bin/env bash
#
# scripts/ci/check-conventional-commits.sh
#
# Validates that every commit in a range follows the Conventional
# Commits spec (https://www.conventionalcommits.org/en/v1.0.0/).
#
# Per CLAUDE.md §3 Branching and Commits.
#
# Allowed types:
#   feat     new feature
#   fix      bug fix
#   docs     documentation
#   style    formatting / whitespace
#   refactor code restructure, no behaviour change
#   perf     performance
#   test     test changes
#   chore    tooling / maintenance
#   build    build system
#   ci       CI configuration
#   revert   reverts a previous commit
#   club     merge-vehicle / clubbing commit (the repo's established
#            integration-vehicle convention; DEC-CONVENTIONAL-COMMIT-CLUB-TYPE-001)
#
# Format: <type>(<optional-scope>): <description>
#   example: feat(adapter): add Groww instrument snapshot pull
#   example: fix(engine): O(1) regression in bar lookup
#   example: docs(requirements): add FR-MATH section
#
# Scope: one or more [a-z0-9_/-]+ segments, comma-separated
# (e.g. fix(options,calendars): ...). Comma-delimited multi-scope is
# accepted for parity with the reference parser
# (conventional-commits-parser, used by commitlint AND Release Please —
# this repo's release automation), which parses it as a single scope
# token; the SPEC's grammar does not forbid it. This matters here
# because published commits are never amended or force-pushed
# (CLAUDE.md §3), so the checker must accept ecosystem-valid subjects
# that the automation already handles rather than force a history
# rewrite. Empty scopes `()`/empty segments `(a,)` and compound TYPES
# (`ci+chore:`) remain rejected — the type allowlist is untouched.
#
# Usage:
#   check-conventional-commits.sh <from-rev> <to-rev>
#   check-conventional-commits.sh --msg-file <path>
#
# In CI (GitHub Actions PR event):
#   check-conventional-commits.sh $PR_BASE_SHA $PR_HEAD_SHA
#
# In pre-commit (commit-msg stage):
#   check-conventional-commits.sh --msg-file "$COMMIT_MSG_FILE"
#
# DEC-WINDOW-OPTION-DECODE-001 hardening: the commit-msg hook previously ran
# `HEAD~1 HEAD`, which at commit-msg time validates the PREVIOUS commit — never
# the message being written. The moment a non-conforming title landed on main
# (PR #588's dual-type squash title `fix(coverage)+feat(engine): …`), EVERY
# subsequent local commit failed regardless of its own message. `--msg-file`
# validates exactly the new message; the range mode stays for CI.

set -euo pipefail

REGEX='^(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert|club)(\([a-z0-9_/-]+(,[a-z0-9_/-]+)*\))?!?: .+'

print_help() {
    cat >&2 <<'EOF'

Required format:
  <type>(<optional-scope>): <description>

Allowed types: feat, fix, docs, style, refactor, perf, test, chore,
               build, ci, revert, club

Examples:
  feat(adapter): add Groww instrument snapshot pull
  fix(engine): O(1) regression in bar lookup
  docs(requirements): add FR-MATH section
  chore(claude): update CLAUDE.md rules
EOF
}

if [[ "${1:-}" == "--msg-file" ]]; then
    MSG_FILE="${2:?--msg-file requires the commit-message path}"
    subject="$(head -n 1 "$MSG_FILE")"
    # Merge commits are exempt (the commitlint defaultIgnores rule; force-push
    # is denied here, so conflict resolution NEEDS merge commits).
    if [[ "$subject" == Merge* ]] || [[ "$subject" =~ $REGEX ]]; then
        echo "✅ commit message follows Conventional Commits."
        exit 0
    fi
    echo "❌ ${subject}" >&2
    echo "" >&2
    echo "This commit message does not follow Conventional Commits." >&2
    print_help
    exit 1
fi

FROM="${1:-HEAD~1}"
TO="${2:-HEAD}"

FAILED=0
COUNT=0

# `--no-merges`: merge commits are exempt from Conventional Commits (the
# commitlint default `defaultIgnores` does the same). This is also required
# by this repo's workflow: force-push is denied (settings deny-list), so the
# only way to resolve a base-branch conflict on a feature branch is a merge
# commit ("Merge branch 'origin/main' …"), which can never match <type>: <desc>.
# Every NON-merge commit is still strictly validated below.
while IFS= read -r line; do
    COUNT=$((COUNT + 1))
    # line is: "<sha> <subject>"
    sha="${line%% *}"
    subject="${line#* }"

    if [[ ! "$subject" =~ $REGEX ]]; then
        echo "❌ ${sha}: ${subject}" >&2
        FAILED=1
    fi
done < <(git log --no-merges --format='%h %s' "${FROM}..${TO}" 2>/dev/null || true)

if [[ $COUNT -eq 0 ]]; then
    echo "⏸  No commits in range ${FROM}..${TO}"
    exit 0
fi

if [[ $FAILED -ne 0 ]]; then
    echo "" >&2
    echo "One or more commits in this range do not follow Conventional Commits." >&2
    print_help
    echo "" >&2
    echo "To amend the last commit: git commit --amend" >&2
    echo "To rewrite multiple: git rebase -i <from-sha>" >&2
    exit 1
fi

echo "✅ All ${COUNT} commits follow Conventional Commits."
