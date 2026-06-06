#!/usr/bin/env bash
# scripts/hooks/pre-push.sh — fast local PRE-PUSH gate.
#
# DEC-PRE-PUSH-HOOK-001. The complement to the SessionStart hook
# (DEC-SESSION-START-HOOK-001) and the commit-stage pre-commit hooks
# (.pre-commit-config.yaml): this runs the FASTEST MEANINGFUL CI SUBSET
# right before `git push`, so the noisy-flake class of failures (lint,
# format, broken import, smoke invariants) is caught in seconds locally
# instead of ~10+ minutes later in CI.
#
# Wiring: referenced as a `stages: [pre-push]` hook in
# .pre-commit-config.yaml; `pre-commit install --hook-type pre-push`
# (run by scripts/setup/bootstrap.sh) installs it into .git/hooks/pre-push.
#
# Philosophy (mirrors .pre-commit-config.yaml): FAST only (< ~60 s). The
# full pytest + coverage + mutation + bench gates stay CI-only — this hook
# is a tripwire, not the full gate (Golden Rule #10: it does NOT prove the
# push is mergeable, only that the cheap checks pass).
#
# Fail-closed (exit 1) so a red check blocks the push. Bypass for a genuine
# emergency is the standard `git push --no-verify` (logged by the operator's
# own judgement) — this hook does not invent its own override.

set -uo pipefail

# Resolve repo root (env override → git toplevel → cwd), mirroring
# session-start.sh so the hook works on any clone, not just the original box.
if [[ -n "${CLAUDE_REPO_ROOT:-}" ]]; then
  REPO_ROOT="${CLAUDE_REPO_ROOT}"
elif REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then
  :
else
  REPO_ROOT="$(pwd)"
fi
cd "${REPO_ROOT}" 2>/dev/null || exit 0

# Prefer the project venv's tools; fall back to whatever is on PATH so the
# hook still runs in a bare shell (it just relies on a global install then).
if [[ -x ".venv/bin/ruff" ]]; then
  RUFF=".venv/bin/ruff"
else
  RUFF="ruff"
fi
if [[ -x ".venv/bin/pytest" ]]; then
  PYTEST=".venv/bin/pytest"
else
  PYTEST="pytest"
fi

fail() { printf "\033[1;31m✗ pre-push BLOCKED:\033[0m %s\n" "$1" >&2; exit 1; }

printf "\033[1;34m==>\033[0m pre-push gate (DEC-PRE-PUSH-HOOK-001) — fast local subset\n" >&2

# 1. Lint + format (cheap, deterministic).
command -v "${RUFF%% *}" >/dev/null 2>&1 || "${RUFF}" --version >/dev/null 2>&1 || fail "ruff not found — run: make setup"
"${RUFF}" check brutex tests || fail "ruff check failed — fix lint before pushing"
"${RUFF}" format --check brutex tests || fail "ruff format --check failed — run: ruff format brutex tests"

# 2. Smoke suite — import + layout + conventional-commit invariants (~seconds).
#    Skipped gracefully (with a loud warning) only if pytest is genuinely
#    absent, so a bare clone without the venv can still push docs; CI remains
#    the hard gate in that case.
if "${PYTEST}" --version >/dev/null 2>&1; then
  "${PYTEST}" tests/smoke -q -p no:cacheprovider || fail "smoke tests failed — see output above"
else
  printf "\033[1;33m! pytest not found — skipping smoke suite (CI still enforces it)\033[0m\n" >&2
fi

printf "\033[1;32m✓ pre-push gate passed\033[0m\n" >&2
exit 0
