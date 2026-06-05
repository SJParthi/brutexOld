#!/usr/bin/env bash
# preflight.sh — verify the host tools that `make run` itself depends on.
#
# `make run` already auto-installs everything the PROJECT owns (the venv, every
# Python library, the fno_math Rust kernel). But three tools must exist on the
# machine FIRST, because the bootstrapper is written in terms of them and cannot
# install what it runs on:
#
#   * python3.12  — the interpreter the venv is built from
#   * make        — runs this Makefile
#   * cargo       — builds the fno_math Rust kernel (one-time)
#
# This script verifies all three, prints a clear ✅/❌ table, and on a miss
# exits non-zero with the EXACT one-line install command for the host OS —
# never a cryptic downstream crash. It NEVER auto-installs system tools
# (no `curl | sh`, no sudo): that is an explicit, operator-consented step
# (CLAUDE.md §settings deny-list + Golden Rule #1 honesty).
#
# Decision: DEC-RUN-PREFLIGHT-001.

set -uo pipefail

PY="${PY:-python3.12}"
OS="$(uname -s 2>/dev/null || echo unknown)"
missing=0

case "$OS" in
  Darwin) PKG="brew install" ;;
  Linux)  PKG="sudo apt-get install -y" ;;
  *)      PKG="<your package manager> install" ;;
esac

echo "──────────────────────────────────────────────────────────────"
echo "  BruteX preflight — verifying host tools (DEC-RUN-PREFLIGHT-001)"
echo "──────────────────────────────────────────────────────────────"

# ── python3.12 ────────────────────────────────────────────────────
if command -v "$PY" >/dev/null 2>&1; then
  echo "  ✅ python : $("$PY" --version 2>&1)  ($(command -v "$PY"))"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null; then
  echo "  ✅ python : $(python3 --version 2>&1) — re-run as: make run PY=python3"
else
  echo "  ❌ python3.12 NOT found."
  case "$OS" in
    Darwin) echo "       fix: $PKG python@3.12" ;;
    Linux)  echo "       fix: $PKG python3.12 python3.12-venv" ;;
    *)      echo "       fix: install Python 3.12 from https://www.python.org/downloads/" ;;
  esac
  missing=1
fi

# ── make ──────────────────────────────────────────────────────────
if command -v make >/dev/null 2>&1; then
  echo "  ✅ make   : $(make --version 2>/dev/null | head -1)"
else
  echo "  ❌ make NOT found."
  case "$OS" in
    Darwin) echo "       fix: xcode-select --install   (or: $PKG make)" ;;
    Linux)  echo "       fix: $PKG make" ;;
    *)      echo "       fix: install GNU make" ;;
  esac
  missing=1
fi

# ── cargo (Rust) ──────────────────────────────────────────────────
if command -v cargo >/dev/null 2>&1 || [ -x "${HOME}/.cargo/bin/cargo" ]; then
  CARGO_BIN="$(command -v cargo 2>/dev/null || echo "${HOME}/.cargo/bin/cargo")"
  echo "  ✅ cargo  : $("$CARGO_BIN" --version 2>&1)  (builds fno_math + trading_core kernels)"
else
  echo "  ❌ cargo (Rust toolchain) NOT found — needed once to build fno_math + trading_core."
  echo "       fix: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
  echo "            (then restart the shell, or: source \$HOME/.cargo/env)"
  missing=1
fi

echo "──────────────────────────────────────────────────────────────"
if [ "$missing" -ne 0 ]; then
  echo "  ⛔ Preflight failed — install the tool(s) marked ❌ above, then re-run."
  echo "     (These are the ONLY manual steps; everything else is automatic.)"
  exit 1
fi
echo "  ✅ All host tools present — handing off to the turnkey build."
echo "──────────────────────────────────────────────────────────────"
exit 0
