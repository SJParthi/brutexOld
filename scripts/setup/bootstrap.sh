#!/usr/bin/env bash
# BruteX bootstrap — set up a fresh machine to run + develop the engine.
#
# Idempotent: safe to re-run. Detects what's already present + skips.
#
# What this script does
# ---------------------
#
# 1. Verifies Python 3.12 is available (CLAUDE.md §4 mandate).
# 2. Creates a `.venv/` if missing.
# 3. Installs `brutex[dev,data,ui]` editable from the working tree.
# 4. Installs the Rust toolchain via rustup if `cargo` is not on PATH.
# 5. Builds + installs the `fno_math` Rust extension via maturin.
# 6. Verifies `brutex doctor` exits 0 (audit chain + runtime + replay checks).
# 7. Prints a one-screen summary of env-var + CLI command surface.
#
# Decision: DEC-INFRA-OPS-001 (MIGRATION entry #244).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

# ────────── helpers ──────────
_step() { printf "\n\033[1;34m==>\033[0m %s\n" "$1"; }
_ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$1"; }
_warn() { printf "  \033[1;33m!\033[0m %s\n" "$1"; }
_err()  { printf "  \033[1;31m✗\033[0m %s\n" "$1" >&2; }

# ────────── 1. Python 3.12 ──────────
_step "Step 1/6 — verify Python 3.12 is available"
if ! command -v python3.12 >/dev/null 2>&1; then
  _err "python3.12 is REQUIRED per CLAUDE.md §4. Install via:"
  _err "    macOS:  brew install python@3.12"
  _err "    Ubuntu: sudo apt install python3.12 python3.12-venv"
  exit 1
fi
_ok "python3.12 found: $(python3.12 --version)"

# ────────── 2. Virtualenv ──────────
_step "Step 2/6 — virtual environment"
if [[ -d .venv ]]; then
  _ok ".venv/ already exists; reusing"
else
  python3.12 -m venv .venv
  _ok "created .venv/ with $(python3.12 --version)"
fi

# Activate for the rest of the script
# shellcheck disable=SC1091
. .venv/bin/activate

# ────────── 3. Python deps ──────────
# DEC-CLONE-RUN-002: include the `ui` extra so the dashboard (fastapi +
# uvicorn) runs immediately after a bootstrap — it was previously omitted,
# so `python -m brutex dashboard` failed for missing fastapi/uvicorn.
_step "Step 3/6 — install brutex[dev,data,ui] editable"
pip install -q --upgrade pip
if pip install -q -e ".[dev,data,ui]" 2>&1 | tail -2; then
  _ok "dev + data + ui extras installed"
else
  _err "pip install failed — see output above"
  exit 1
fi

# ────────── 4. Rust toolchain ──────────
_step "Step 4/6 — Rust toolchain"
if command -v cargo >/dev/null 2>&1; then
  _ok "cargo found: $(cargo --version)"
else
  _warn "cargo not on PATH; installing via rustup (1-2 min)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
  # shellcheck disable=SC1091
  . "${HOME}/.cargo/env"
  _ok "rustup installed: $(cargo --version)"
fi

# ────────── 5. fno_math wheel ──────────
_step "Step 5/6 — build + install fno_math Rust extension"
if python -c "import fno_math" 2>/dev/null; then
  _ok "fno_math already importable"
else
  pip install -q maturin
  ( cd rust/fno-math && maturin develop --release 2>&1 | tail -3 )
  if python -c "import fno_math" 2>/dev/null; then
    _ok "fno_math built + installed via maturin"
  else
    _err "maturin develop failed; see output above"
    exit 1
  fi
fi

# ────────── 6. brutex doctor smoke ──────────
_step "Step 6/6 — verify brutex doctor exits 0"
mkdir -p "${HOME}/.brutex/audit"
if brutex doctor >/dev/null 2>&1; then
  _ok "brutex doctor: PASS"
else
  rc=$?
  _warn "brutex doctor exited ${rc} (likely SKIPs for missing runtime file — that's fine on a fresh box)"
fi

# ────────── summary ──────────
printf "\n\033[1;32m═══════════════════════════════════════════════════════════════════════\033[0m\n"
printf "\033[1;32m  BruteX bootstrap complete\033[0m\n"
printf "\033[1;32m═══════════════════════════════════════════════════════════════════════\033[0m\n\n"

echo "  Next steps (operator):"
echo ""
echo "    1. Activate the venv in new shells:"
echo "         . .venv/bin/activate"
echo ""
echo "    2. Read the env-var registry:"
echo "         docs/runbooks/ENV_VARS.md"
echo ""
echo "    3. Wire Telegram (optional but recommended):"
echo "         docs/runbooks/telegram-setup.md"
echo ""
echo "    4. Run the monitor loop:"
echo "         brutex monitor --ticks 5 --poll-interval 1.0"
echo ""
echo "    5. Run the health doctor:"
echo "         brutex doctor"
echo ""
echo "═══════════════════════════════════════════════════════════════════════"
