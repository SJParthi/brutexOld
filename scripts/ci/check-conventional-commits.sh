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
#
# Format: <type>(<optional-scope>): <description>
#   example: feat(adapter): add Groww instrument snapshot pull
#   example: fix(engine): O(1) regression in bar lookup
#   example: docs(requirements): add FR-MATH section
#
# Usage:
#   check-conventional-commits.sh <from-rev> <to-rev>
#
# In CI (GitHub Actions PR event):
#   check-conventional-commits.sh $PR_BASE_SHA $PR_HEAD_SHA
#
# In pre-commit (commit-msg stage):
#   check-conventional-commits.sh HEAD~1 HEAD

set -euo pipefail

FROM="${1:-HEAD~1}"
TO="${2:-HEAD}"

REGEX='^(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert)(\([a-z0-9_/-]+\))?!?: .+'

FAILED=0
COUNT=0

while IFS= read -r line; do
    COUNT=$((COUNT + 1))
    # line is: "<sha> <subject>"
    sha="${line%% *}"
    subject="${line#* }"

    if [[ ! "$subject" =~ $REGEX ]]; then
        echo "❌ ${sha}: ${subject}" >&2
        FAILED=1
    fi
done < <(git log --format='%h %s' "${FROM}..${TO}" 2>/dev/null || true)

if [[ $COUNT -eq 0 ]]; then
    echo "⏸  No commits in range ${FROM}..${TO}"
    exit 0
fi

if [[ $FAILED -ne 0 ]]; then
    echo "" >&2
    cat >&2 <<'EOF'
One or more commits in this range do not follow Conventional Commits.

Required format:
  <type>(<optional-scope>): <description>

Allowed types: feat, fix, docs, style, refactor, perf, test, chore,
               build, ci, revert

Examples:
  feat(adapter): add Groww instrument snapshot pull
  fix(engine): O(1) regression in bar lookup
  docs(requirements): add FR-MATH section
  chore(claude): update CLAUDE.md rules

To amend the last commit: git commit --amend
To rewrite multiple: git rebase -i <from-sha>
EOF
    exit 1
fi

echo "✅ All ${COUNT} commits follow Conventional Commits."
