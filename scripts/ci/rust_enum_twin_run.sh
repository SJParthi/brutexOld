#!/usr/bin/env bash
# Real-data twin-run gate for the Rust sweep-enum kernel (DEC-SWEEP-ENUM-RUST-001).
#
# The design-of-record (DESIGN_FINAL_RUST_SWEEP_CORE.md §6 Slice 1, §7.2 venue
# law) mandates twin-run byte-identity on a SMALL REAL-DATA grid as an
# ON-BRANCH merge gate, executed on the operator's box (the real lake never
# reaches shared CI): this script runs the SAME pinned representative sub-grid
# TWICE through the REAL sweep CLI — first with BRUTEX_SWEEP_RUST_ENUM=0 (the
# Python spine), then =1 (the ranged fused kernel) — into two SEPARATE scratch
# results roots, byte-compares every persisted artifact, and prints an
# unambiguous PASS/FAIL line plus both hash manifests formatted for pasting
# into the PR body.
#
# ENGAGEMENT GATE (review round 2, DEC-SWEEP-ENUM-RUST-001): byte-identity
# alone is VACUOUS when the ON arm silently served the Python spine — a
# stale/absent wheel degrades with one warn-once fallback, and a shadow
# burn-in mismatch SHIPS THE PYTHON RESULT then latches the width off
# (kernel_autoreverted), so both arms match bytes and `cmp` alone would print
# a PASS that proves nothing about the kernel. Each arm's output is therefore
# teed to a log and, before any PASS, the ON arm's log MUST contain
# `sweep_rust_enum.engaged` and MUST NOT contain any masking/divergence
# marker (`sweep_rust_enum.fallback` — covers wheel_missing / wheel_stale /
# enum_total_mismatch / kernel_error / every other Python-served reason —
# `sweep_rust_enum.kernel_autoreverted`, `sweep_rust_enum.kernel_result_refused`).
# BRUTEX_LOG_LEVEL is pinned to INFO on both arms so the INFO-level engaged
# event is always visible to the gate regardless of the operator's shell env.
#
# SAFETY (by construction):
#   * READ-ONLY on the lake: results + the catalog DuckDB both go under a
#     fresh scratch dir this script creates (--results-root + --catalog);
#     the production <lake>/sweeps root is never touched. (The lake's own
#     read-side manifest caches may self-heal on read — lake-internal cache
#     files, never bar/ledger data.)
#   * Refuses LOUDLY if BRUTEX_SWEEP_RUST_ENUM is already set in the
#     environment — the script owns both arms of the flag.
#   * BRUTEX_RESULTS_DEPTH=0 on both arms: the run-end results-depth pass
#     writes wall-clock-stamped run artifacts that would false-diff; the twin
#     compare covers the store parquet + trial-stats sidecar + completion
#     sentinel byte artifacts (the design's store/sidecar compare).
#
# Usage (macOS bash 3.2-compatible; plain POSIX tools):
#   scripts/ci/rust_enum_twin_run.sh [LAKE_ROOT]
#   (LAKE_ROOT defaults to $BRUTEX_LAKE_ROOT)
# Optional env overrides: TWIN_SYMBOLS TWIN_START TWIN_END TWIN_INTERVALS
#   TWIN_DIRECTIONS TWIN_K TWIN_TOP TWIN_PYTHON
set -eu
# pipefail (bash >= 3.0, macOS 3.2 OK): run_arm pipes the CLI through `tee`
# for the engagement-gate logs — a failing sweep must still abort the script.
set -o pipefail

LAKE_ROOT="${1:-${BRUTEX_LAKE_ROOT:-}}"
if [ -z "$LAKE_ROOT" ]; then
  echo "FATAL: no lake root — pass it as \$1 or export BRUTEX_LAKE_ROOT" >&2
  echo "  (on the M4 Pro: scripts/ci/rust_enum_twin_run.sh ~/brutex-lake)" >&2
  exit 2
fi
if [ ! -d "$LAKE_ROOT" ]; then
  echo "FATAL: lake root '$LAKE_ROOT' is not a directory" >&2
  exit 2
fi
if [ -n "${BRUTEX_SWEEP_RUST_ENUM:-}" ]; then
  echo "FATAL: BRUTEX_SWEEP_RUST_ENUM is already set ('"$BRUTEX_SWEEP_RUST_ENUM"')" >&2
  echo "  — this script owns BOTH arms of the flag; unset it and re-run." >&2
  exit 2
fi

# The pinned representative sub-grid (design §9: >=2 TFs incl. one 1-minute
# tuple, k in {2,5}, at the operator's production --top=1000). The window is
# deliberately BOUNDED (one quarter) so the twin completes in minutes.
TWIN_SYMBOLS="${TWIN_SYMBOLS:-NIFTY}"
TWIN_START="${TWIN_START:-2024-01-01}"
TWIN_END="${TWIN_END:-2024-03-31}"
TWIN_INTERVALS="${TWIN_INTERVALS:-1minute,15minute}"
TWIN_DIRECTIONS="${TWIN_DIRECTIONS:-BUY}"
TWIN_K="${TWIN_K:-2,5}"
TWIN_TOP="${TWIN_TOP:-1000}"
PYTHON="${TWIN_PYTHON:-python3}"
if [ -x ".venv/bin/python" ]; then PYTHON=".venv/bin/python"; fi

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/rust-enum-twin.XXXXXX")"
echo "twin-run scratch: $SCRATCH (lake: $LAKE_ROOT — read-only)"
echo "sub-grid: symbols=$TWIN_SYMBOLS window=$TWIN_START..$TWIN_END intervals=$TWIN_INTERVALS directions=$TWIN_DIRECTIONS k=$TWIN_K top=$TWIN_TOP"

run_arm() {
  # $1 = arm name (off|on), $2 = flag value (0|1)
  # Output (stdout+stderr, where structlog writes) is teed to $SCRATCH/$1.log
  # so the engagement gate below can inspect the ON arm's kernel events.
  arm_root="$SCRATCH/$1"
  arm_log="$SCRATCH/$1.log"
  mkdir -p "$arm_root"
  echo "── arm $1 (BRUTEX_SWEEP_RUST_ENUM=$2) ──"
  env BRUTEX_SWEEP_RUST_ENUM="$2" BRUTEX_RESULTS_DEPTH=0 BRUTEX_LOG_LEVEL=INFO \
    "$PYTHON" -m brutex sweep-all \
    --symbols "$TWIN_SYMBOLS" \
    --start "$TWIN_START" --end "$TWIN_END" \
    --intervals "$TWIN_INTERVALS" \
    --directions "$TWIN_DIRECTIONS" \
    --k "$TWIN_K" \
    --top "$TWIN_TOP" \
    --lake-root "$LAKE_ROOT" \
    --results-root "$arm_root/results" \
    --catalog "$arm_root/catalog.duckdb" 2>&1 | tee "$arm_log"
}

hash_manifest() {
  # Relative-path sha256 manifest of every persisted twin artifact (store
  # parquet + trial-stats sidecar + completion sentinel). Audit .jsonl files
  # carry wall-clock timestamps and are deliberately excluded (the audit
  # CHAIN integrity has its own gate).
  arm_root="$SCRATCH/$1/results"
  (
    cd "$arm_root"
    find . -type f \( -name '*.parquet' -o -name '*.trials.json' \
      -o -name '*.complete.json' \) -print | sort | while read -r f; do
      if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$f"
      else
        sha256sum "$f"
      fi
    done
  )
}

run_arm off 0
run_arm on 1

OFF_MANIFEST="$SCRATCH/off.manifest"
ON_MANIFEST="$SCRATCH/on.manifest"
hash_manifest off > "$OFF_MANIFEST"
hash_manifest on > "$ON_MANIFEST"

echo ""
echo "── OFF-arm artifact hashes (paste into the PR body) ──"
cat "$OFF_MANIFEST"
echo ""
echo "── ON-arm artifact hashes (paste into the PR body) ──"
cat "$ON_MANIFEST"
echo ""

if [ ! -s "$OFF_MANIFEST" ]; then
  echo "TWIN-RUN FAIL: the OFF arm produced ZERO artifacts — the sub-grid did not run" >&2
  exit 1
fi

# ── ENGAGEMENT GATE (must clear BEFORE any PASS can print) ──
# A byte-identical twin proves kernel-vs-Python equivalence ONLY if the
# kernel genuinely served the ON arm. Two reachable masked-PASS paths
# otherwise: (1) a stale/absent wheel → warn-once fallback → every tuple
# Python-served; (2) a shadow-burn-in mismatch → the wrapper ships the
# Python result and autoreverts the width → all later tuples Python-served.
ON_LOG="$SCRATCH/on.log"
if ! grep -q 'sweep_rust_enum\.engaged' "$ON_LOG"; then
  echo "TWIN-RUN FAIL: the ON arm never logged sweep_rust_enum.engaged —" >&2
  echo "  the kernel did not serve (stale/absent wheel or a sticky fallback);" >&2
  echo "  byte-identity under full Python fallback proves nothing. Rebuild the" >&2
  echo "  wheel (maturin develop --release in rust/fno-math) and re-run." >&2
  echo "  ON-arm kernel events (log kept at $ON_LOG):" >&2
  grep -n 'sweep_rust_enum\.' "$ON_LOG" >&2 || echo "  (none logged)" >&2
  exit 1
fi
BAD_MARKERS='sweep_rust_enum\.(fallback|kernel_autoreverted|kernel_result_refused)'
if grep -E -q "$BAD_MARKERS" "$ON_LOG"; then
  echo "TWIN-RUN FAIL: the ON arm logged a kernel fallback/divergence marker —" >&2
  echo "  those tuples were Python-served, so a byte-identical compare would be" >&2
  echo "  MASKED (an autoreverted shadow mismatch is a REAL kernel bug even" >&2
  echo "  though the shipped bytes match). Offending lines (log at $ON_LOG):" >&2
  grep -E -n "$BAD_MARKERS" "$ON_LOG" >&2
  exit 1
fi
echo "engagement gate: ON arm logged sweep_rust_enum.engaged with zero fallback/autorevert/refusal markers"

if cmp -s "$OFF_MANIFEST" "$ON_MANIFEST"; then
  echo "TWIN-RUN PASS: BRUTEX_SWEEP_RUST_ENUM=0 and =1 produced BYTE-IDENTICAL artifacts"
  echo "  ($(wc -l < "$OFF_MANIFEST" | tr -d ' ') files compared; scratch kept at $SCRATCH)"
  exit 0
else
  echo "TWIN-RUN FAIL: the two arms DIVERGED — diff follows" >&2
  diff "$OFF_MANIFEST" "$ON_MANIFEST" >&2 || true
  exit 1
fi
