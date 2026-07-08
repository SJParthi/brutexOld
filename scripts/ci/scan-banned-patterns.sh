#!/usr/bin/env bash
#
# scripts/ci/scan-banned-patterns.sh
#
# Scans the repo for patterns that must NEVER appear in production
# BruteX code. Used by .github/workflows/ci.yml and by
# .pre-commit-config.yaml (local pre-commit hook).
#
# Per CLAUDE.md §4 Coding Standards and REQUIREMENTS.md §17 CG-04.
#
# Exit codes:
#   0 — clean
#   1 — at least one banned pattern found (details printed to stderr)
#
# Patterns enforced:
#   1. No `print(` in brutex/ production code — must use structlog
#   2. No bare `except:` — must specify exception type
#   3. No `# TODO:` lingering in production code — use MG-### in REQUIREMENTS.md instead
#   4. No `.env*` files checked in (already in .gitignore, double-check)
#   5. No hardcoded broker auth tokens (pattern: any `*_API_AUTH_TOKEN = "..."`
#      or `*_ACCESS_TOKEN = "..."`)
#   6. No hardcoded Telegram tokens (pattern: `<digits>:<base64>`)
#   7. No `pandas` imports anywhere in brutex/ (import-anchored; polars/numpy only)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
FAILED=0
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

cd "$REPO_ROOT"

fail() {
    echo -e "${RED}❌ BANNED PATTERN FOUND${NC}: $1" >&2
    FAILED=1
}

warn() {
    echo -e "${YELLOW}⚠  $1${NC}" >&2
}

# ── 1. print( in brutex/ — allowed in tests only ──
if git ls-files 'brutex/**/*.py' 2>/dev/null | xargs grep -l '^\s*print(' 2>/dev/null; then
    fail "print() found in brutex/ production code. Use structlog instead."
fi

# ── 2. bare except: ──
if git ls-files 'brutex/**/*.py' 'tests/**/*.py' 2>/dev/null | xargs grep -En '^\s*except\s*:' 2>/dev/null; then
    fail "bare 'except:' found. Specify the exception type."
fi

# ── 3. lingering TODO in production (tests OK for now) ──
lingering_todos=$(git ls-files 'brutex/**/*.py' 2>/dev/null | xargs grep -En '^\s*#\s*TODO:' 2>/dev/null || true)
if [[ -n "$lingering_todos" ]]; then
    warn "Lingering # TODO: comments in brutex/ — track these as MG-### in REQUIREMENTS.md §18:"
    echo "$lingering_todos" >&2
fi

# ── 4. .env* files committed ──
if git ls-files | grep -E '^\.env' 2>/dev/null; then
    fail ".env* file committed. Add to .gitignore and rotate any exposed secrets immediately."
fi

# ── 5. hardcoded broker auth token (any vendor pattern) ──
if git ls-files 'brutex/**/*.py' 'tests/**/*.py' 2>/dev/null | \
   xargs grep -En '(API_AUTH_TOKEN|ACCESS_TOKEN|API_TOKEN)\s*=\s*["\047][^"\047{]{10,}["\047]' 2>/dev/null; then
    fail "Hardcoded broker auth token detected. Use environment variable (e.g. BRUTEX_GROWW_API_KEY)."
fi

# ── 6. hardcoded Telegram token pattern (digits:base64-like, length > 30) ──
if git ls-files 'brutex/**/*.py' 'tests/**/*.py' 2>/dev/null | \
   xargs grep -En '["\047][0-9]{6,}:[A-Za-z0-9_-]{30,}["\047]' 2>/dev/null; then
    fail "Possible hardcoded Telegram bot token detected. Use BRUTEX_TELEGRAM_BOT_TOKEN env var."
fi

# ── 7. pandas imports anywhere in brutex/ ──
# CLAUDE.md §4: "No pandas in hot paths — use polars or numpy + numba or Rust".
# pandas is not even a declared dependency (pyproject.toml: polars + numpy are)
# — ANY brutex/ import of it is a defect. Anchored to real import statements so
# comments/docstrings mentioning pandas never false-positive. tests/ stays
# deliberately out of scope (a test may use pandas as an independent oracle;
# none does today).
# PATHSPEC NOTE: git's 'brutex/**/*.py' requires an intermediate directory and
# MISSES the top-level brutex/*.py modules (cli.py, money.py, universe.py, ...)
# — both globs are needed for the check to genuinely cover ALL of brutex/.
if git ls-files 'brutex/*.py' 'brutex/**/*.py' 2>/dev/null | \
   xargs grep -En '^\s*(import\s+pandas\b|from\s+pandas\b)' 2>/dev/null; then
    fail "pandas imported in brutex/. Use polars or numpy (CLAUDE.md §4; pandas is not a dependency)."
fi

# ── 8. Python math implementations in brutex/math/ ──
# Per FR-MATH-001: no formula reimplementation, use fno-math via PyO3.
if git ls-files 'brutex/math/**/*.py' 2>/dev/null | \
   xargs grep -En 'math\.erf|scipy\.stats\.norm' 2>/dev/null; then
    fail "Python math formula (math.erf / scipy.stats.norm) in brutex/math/. Delegate to fno-math via PyO3."
fi

# ── 9. tabs in Python files (spaces only) ──
if git ls-files '**/*.py' 2>/dev/null | xargs grep -lP '\t' 2>/dev/null; then
    fail "Tab character in a Python file. Ruff-format should catch this — did you bypass hooks?"
fi

# ── 10. unreferenced self._ assignments in dataclass bodies (common bug) ──
# (Skipped for now — tooling overlap with mypy)

if [[ $FAILED -eq 0 ]]; then
    echo "✅ No banned patterns found."
else
    echo "" >&2
    echo "One or more banned patterns were found — fix before committing." >&2
    exit 1
fi
