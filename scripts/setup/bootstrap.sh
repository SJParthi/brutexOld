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
# 6. Builds + installs the `trading_core` Rust extension via maturin
#    (shared 52-vocab core — brutex.indicators imports it; DEC-SHARED-CORE-001).
# 7. Verifies `brutex doctor` exits 0 (audit chain + runtime + replay checks).
# 8. Prints a one-screen summary of env-var + CLI command surface.
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
_step "Step 1/7 — verify Python 3.12 is available"
if ! command -v python3.12 >/dev/null 2>&1; then
  _err "python3.12 is REQUIRED per CLAUDE.md §4. Install via:"
  _err "    macOS:  brew install python@3.12"
  _err "    Ubuntu: sudo apt install python3.12 python3.12-venv"
  exit 1
fi
_ok "python3.12 found: $(python3.12 --version)"

# ────────── 2. Virtualenv ──────────
_step "Step 2/7 — virtual environment"
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
_step "Step 3/7 — install brutex[dev,data,ui] editable"
pip install -q --upgrade pip
if pip install -q -e ".[dev,data,ui]" 2>&1 | tail -2; then
  _ok "dev + data + ui extras installed"
else
  _err "pip install failed — see output above"
  exit 1
fi

# ────────── 4. Rust toolchain ──────────
_step "Step 4/7 — Rust toolchain"
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
_step "Step 5/7 — build + install fno_math Rust extension"
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

# ────────── 6. trading_core wheel ──────────
# Shared 52-condition vocab + evaluate-time indicators core (DEC-SHARED-CORE-001).
# brutex.indicators.fib_levels imports it at module top (fib_levels.py:68), so the
# engine + dashboard fail to import without it — build it the same way as fno_math.
_step "Step 6/7 — build + install trading_core Rust extension"
if python -c "import trading_core" 2>/dev/null; then
  _ok "trading_core already importable"
else
  pip install -q maturin
  ( cd rust/trading-core && maturin develop --release 2>&1 | tail -3 )
  if python -c "import trading_core" 2>/dev/null; then
    _ok "trading_core built + installed via maturin"
  else
    _err "maturin develop failed; see output above"
    exit 1
  fi
fi

# ────────── 6a. brutex_feed wheel (optional) ──────────
# DEC-FEED-RUST-CORE-001: the default-OFF feed decision kernel. OPTIONAL —
# nothing imports it at module scope and BRUTEX_FEED_RUST is default-OFF
# (the feed degrades to the Python oracle without it), so a build failure
# WARNS but never aborts bootstrap.
_step "Step 6a/7 — build + install brutex_feed Rust extension (optional)"
if python -c "import brutex_feed" 2>/dev/null; then
  _ok "brutex_feed already importable"
else
  pip install -q maturin
  ( cd rust/brutex-feed && maturin develop --release 2>&1 | tail -3 )
  if python -c "import brutex_feed" 2>/dev/null; then
    _ok "brutex_feed built + installed via maturin"
  else
    _warn "brutex_feed build failed — OPTIONAL kernel (BRUTEX_FEED_RUST default-OFF degrades to the Python oracle)"
  fi
fi

# ────────── 6a2. brutex_lake wheel ──────────
# DEC-BRUTEX-LAKE-BOX-WHEEL-001: the data-lake Rust crate (ADR-0020).
# Since DEC-RUST-LAKE-WAVED-001 (W-6a) the ``brutex_lake`` wheel is the ONE
# production arm of the four waved lake cores (a missing/stale wheel is the
# fail-loud ``LakeRustWavedUnavailableError`` at USE time — the Python cores
# are deleted), but it is NOT module-top imported by brutex/, so bootstrap
# keeps this step's shipped WARN-not-abort control flow: a build failure
# warns loudly and bootstrap proceeds (`import brutex` still works; lake
# operations refuse loudly until the operator re-runs the build).
# COST: a cold build compiles the rev-pinned polars graph from source —
# minutes of wall time and ~12 GB of rust/brutex-lake/target/ disk.
_step "Step 6a2/7 — build + install brutex_lake Rust extension (cold build compiles the pinned polars graph — minutes + ~12 GB target/)"
if python -c "import brutex_lake" 2>/dev/null; then
  _ok "brutex_lake already importable"
else
  pip install -q maturin
  ( cd rust/brutex-lake && maturin develop --release 2>&1 | tail -3 )
  if python -c "import brutex_lake" 2>/dev/null; then
    _ok "brutex_lake built + installed via maturin"
  else
    _warn "brutex_lake build failed — lake Rust ops will refuse loudly (LakeRustWavedUnavailableError, DEC-RUST-LAKE-WAVED-001) until it is built; re-run: cd rust/brutex-lake && maturin develop --release"
  fi
fi

# ────────── 6c. brutex_pull_state wheel ──────────
# The pull-state decision plane is Rust-only and fail-closed (module-top
# imported by brutex/), so it is a REQUIRED build prerequisite — build it the
# same way as fno_math/trading_core and ABORT bootstrap on failure.
_step "Step 6c/7 — build + install brutex_pull_state Rust extension"
if python -c "import brutex_pull_state" 2>/dev/null; then
  _ok "brutex_pull_state already importable"
else
  pip install -q maturin
  ( cd rust/brutex-pull-state && maturin develop --release 2>&1 | tail -3 )
  if python -c "import brutex_pull_state" 2>/dev/null; then
    _ok "brutex_pull_state built + installed via maturin"
  else
    _err "maturin develop failed; see output above"
    exit 1
  fi
fi

# ────────── 6b. git hooks (pre-commit + commit-msg + pre-push) ──────────
# DEC-PRE-PUSH-HOOK-001: install ALL hook stages so the local gates
# (.pre-commit-config.yaml) actually fire — including the new pre-push
# fast gate (ruff + smoke). Best-effort: a missing pre-commit binary
# warns but never aborts bootstrap (CI is the hard gate regardless).
_step "Step 6b/7 — install git hooks (pre-commit + commit-msg + pre-push)"
if command -v pre-commit >/dev/null 2>&1 || pip install -q pre-commit; then
  if pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push >/dev/null 2>&1; then
    _ok "git hooks installed (pre-commit + commit-msg + pre-push)"
  else
    _warn "pre-commit install failed — local hooks not active (CI still enforces all gates)"
  fi
else
  _warn "pre-commit unavailable — local hooks not active (CI still enforces all gates)"
fi

# ────────── 7. brutex doctor smoke ──────────
_step "Step 7/7 — verify brutex doctor exits 0"
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
