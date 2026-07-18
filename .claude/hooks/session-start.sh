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

# Resolve repo root via (in order): CLAUDE_REPO_ROOT env var, git toplevel
# from current cwd, then current cwd as final fallback. Hardcoding a path
# would break the hook on any machine other than the original dev box and
# would silently turn the hook into a no-op (tests caught this in PR #125).
if [[ -n "${CLAUDE_REPO_ROOT:-}" ]]; then
  REPO_ROOT="${CLAUDE_REPO_ROOT}"
elif REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then
  :
else
  REPO_ROOT="$(pwd)"
fi
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
  # Truncate to 80 bytes, then drop any trailing INCOMPLETE multi-byte UTF-8
  # sequence `head -c` may have severed (a commit subject with a multi-byte
  # char — e.g. an em-dash or `→` arrow — straddling the byte boundary would
  # otherwise emit invalid UTF-8 and break any UTF-8 consumer of this stream;
  # `iconv -c` strips the orphaned byte(s)). Falls back to the raw 80-byte cut
  # if iconv is unavailable, so the HEAD line is always populated.
  LAST_COMMIT_RAW=$(git log -1 --format='%h %s' 2>/dev/null | head -c 80)
  if command -v iconv >/dev/null 2>&1; then
    LAST_COMMIT=$(printf '%s' "${LAST_COMMIT_RAW}" | iconv -c -f UTF-8 -t UTF-8 2>/dev/null)
  else
    LAST_COMMIT="${LAST_COMMIT_RAW}"
  fi

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
  if [[ -d "docs/claude" ]]; then
    SPLIT_NEWEST=$(ls -t docs/claude/*.md 2>/dev/null | head -1)
    if [[ -n "${SPLIT_NEWEST}" ]]; then
      SPLIT_MTIME=$(date -r "${SPLIT_NEWEST}" '+%Y-%m-%d %H:%M' 2>/dev/null || stat -c '%y' "${SPLIT_NEWEST}" 2>/dev/null | cut -d. -f1)
      echo "  docs/claude/:     newest ${SPLIT_NEWEST##*/} modified ${SPLIT_MTIME}"
    fi
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

  # ────────── automation & live state (DEC-SESSION-HOOK-LIVESTATE-001) ──────────
  # Surface live pipeline / audit / alerting state on every session so each
  # Claude session auto-inherits "what is the system doing right now" without
  # the operator running anything by hand. Every probe is best-effort: it is
  # time-boxed and `|| true`-guarded so the hook still exits 0 when the venv,
  # CLI, or logs are absent. The heavy full health check (`brutex doctor`) is
  # opt-in via BRUTEX_SESSION_HOOK_DOCTOR=1 to keep startup fast (Golden Rule
  # #10 honest envelope — cheap probes always-on, expensive one a flag away).
  echo "  Automation & live state (DEC-SESSION-HOOK-LIVESTATE-001):"

  # Pick a python that can run the CLI (prefer the project venv).
  HOOK_PY=""
  if [[ -x ".venv/bin/python" ]]; then
    HOOK_PY=".venv/bin/python"
  fi
  # Cross-platform time-box (macOS lacks `timeout`; coreutils ships `gtimeout`).
  HOOK_TO=""
  if command -v timeout >/dev/null 2>&1; then
    HOOK_TO="timeout 8"
  elif command -v gtimeout >/dev/null 2>&1; then
    HOOK_TO="gtimeout 8"
  fi

  # Pipeline status — reads the runtime file (cheap; always run).
  echo "    Pipeline status:"
  if [[ -n "${HOOK_PY}" ]]; then
    ${HOOK_TO} "${HOOK_PY}" -m brutex status 2>&1 | head -5 | sed 's/^/      /' || true
  else
    echo "      (skipped — .venv not present; run: make setup)"
  fi

  # Audit chain / logs surface — count log files (cheap; always run).
  AUDIT_DIR="${BRUTEX_AUDIT_DIR:-${HOME}/.brutex/audit}"
  echo "    Audit chain:"
  if [[ -d "${AUDIT_DIR}" ]]; then
    AUDIT_N=$(find "${AUDIT_DIR}" -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')
    echo "      ${AUDIT_N} log(s) in ${AUDIT_DIR} (full verify: brutex doctor)"
  else
    echo "      no audit logs yet (${AUDIT_DIR} absent)"
  fi

  # Alerting config visibility — presence only, never print the secret token.
  echo "    Alerting (Telegram):"
  if [[ "${BRUTEX_TELEGRAM_ENABLED:-}" == "true" && -n "${BRUTEX_TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "      ENABLED — alerts will fire (token present)"
  elif [[ -n "${BRUTEX_TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "      token present but BRUTEX_TELEGRAM_ENABLED != true (alerts dormant)"
  else
    echo "      dormant — set BRUTEX_TELEGRAM_ENABLED=true + BRUTEX_TELEGRAM_BOT_TOKEN to arm"
  fi

  # Opt-in full health check (heavier; off by default for fast startup).
  if [[ "${BRUTEX_SESSION_HOOK_DOCTOR:-}" == "1" && -n "${HOOK_PY}" ]]; then
    echo "    Doctor (full health check):"
    ${HOOK_TO} "${HOOK_PY}" -m brutex doctor 2>&1 | tail -8 | sed 's/^/      /' || true
  fi

  echo "    Automation commands: make run · brutex status · brutex doctor · brutex monitor · brutex dashboard"
  echo ""

  # ────────── reminder ──────────
  echo "  Pre-merge gates per docs/claude/12-session-etiquette.md (old §10):"
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
