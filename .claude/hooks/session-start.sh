#!/usr/bin/env bash
# session-start.sh — print BruteX project state at every Claude session start.
#
# Per docs/STANDARDS_100PCT_HONEST_AUDIT.md §1 row "Per new session", the
# pre-2026-05-25 honest envelope flagged that NO SessionStart hook existed —
# new Claude sessions relied on discipline to read CLAUDE.md + the spec +
# recent MIGRATION entries before editing.
#
# This hook closes that gap: it prints a concise project-state summary to
# stderr on every session start (current + new). Output is bounded (< 50
# lines) so it doesn't blow context, but rich enough that Claude knows:
#
#   1. Current branch + git status (clean / dirty / how many commits ahead)
#   2. CLAUDE.md freshness (last-modified date)
#   3. Last 5 MIGRATION.md decision IDs (so we know what's been ratcheted)
#   4. Latest STANDARDS row matrix freshness (last-modified of audit doc)
#   5. Whether key environment artifacts are present (venv, fno_math wheel)
#   6. The reminder of mandatory pre-commit / pre-merge commands
#
# All output goes to stderr (per Claude Code hook contract) so it doesn't
# corrupt stdout-based tool output. Exit code 0 always — this is a
# notification, not a gate.
#
# Decision: DEC-SESSION-START-HOOK-001 (2026-05-25, MIGRATION entry #237).

set -uo pipefail  # no -e: don't abort on missing optional artifacts

REPO_ROOT="${CLAUDE_REPO_ROOT:-/home/user/bruteX}"
cd "${REPO_ROOT}" 2>/dev/null || exit 0  # silent exit if not a repo

# ────────── header ──────────
{
  echo ""
  echo "═══════════════════════════════════════════════════════════════════════"
  echo "  BruteX — SessionStart hook (DEC-SESSION-START-HOOK-001)"
  echo "═══════════════════════════════════════════════════════════════════════"

  # ────────── git status ──────────
  BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
  UNCOMMITTED=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  AHEAD=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")
  LAST_COMMIT=$(git log -1 --format='%h %s' 2>/dev/null | head -c 80)

  echo "  Branch:           ${BRANCH}"
  echo "  Commits ahead of main: ${AHEAD}"
  echo "  Uncommitted files: ${UNCOMMITTED}"
  echo "  HEAD:             ${LAST_COMMIT}"
  echo ""

  # ────────── docs freshness ──────────
  if [[ -f "CLAUDE.md" ]]; then
    CLAUDE_MTIME=$(date -r CLAUDE.md '+%Y-%m-%d %H:%M' 2>/dev/null || stat -c '%y' CLAUDE.md 2>/dev/null | cut -d. -f1)
    echo "  CLAUDE.md:        last modified ${CLAUDE_MTIME}"
  fi
  if [[ -f "docs/STANDARDS_100PCT_HONEST_AUDIT.md" ]]; then
    AUDIT_MTIME=$(date -r docs/STANDARDS_100PCT_HONEST_AUDIT.md '+%Y-%m-%d %H:%M' 2>/dev/null || stat -c '%y' docs/STANDARDS_100PCT_HONEST_AUDIT.md 2>/dev/null | cut -d. -f1)
    echo "  Honest audit:     last refreshed ${AUDIT_MTIME}"
  fi
  echo ""

  # ────────── recent decisions ──────────
  if [[ -f "docs/MIGRATION.md" ]]; then
    echo "  Last 5 MIGRATION decisions (by entry number):"
    grep -oE "^[0-9]+\. \*\*DEC-[A-Z0-9-]+-[0-9]+" docs/MIGRATION.md 2>/dev/null \
      | sort -t. -k1 -n -r \
      | head -5 \
      | sed 's/^/    /'
  fi
  echo ""

  # ────────── env artifacts ──────────
  echo "  Environment:"
  if [[ -d ".venv" ]]; then
    PY_VER=$(.venv/bin/python --version 2>&1 | head -c 30)
    echo "    .venv/         present (${PY_VER})"
  else
    echo "    .venv/         MISSING — run: python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
  fi
  if .venv/bin/python -c "import fno_math" 2>/dev/null; then
    echo "    fno_math       importable"
  else
    echo "    fno_math       MISSING — run: cd rust/fno-math && PATH=/root/.cargo/bin:\$PATH ../../.venv/bin/maturin develop --release"
  fi
  echo ""

  # ────────── reminder ──────────
  echo "  Pre-merge gates per CLAUDE.md §10:"
  echo "    .venv/bin/ruff check brutex tests && .venv/bin/mypy brutex && .venv/bin/pytest -q"
  echo ""
  echo "  Z+ 6-marker PR-body discipline per scripts/ci/zplus-pr-guard.sh:"
  echo "    1. Z+ 15-row matrix"
  echo "    2. Z+ 7-row resilience matrix"
  echo "    3. Z+ 7-layer audit"
  echo "    4. 4-agent hostile review"
  echo "    5. Honest envelope"
  echo "    6. Decision ID"
  echo ""
  echo "═══════════════════════════════════════════════════════════════════════"
  echo ""
} >&2

exit 0
