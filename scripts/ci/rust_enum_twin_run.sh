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
  arm_root="$SCRATCH/$1"
  mkdir -p "$arm_root"
  echo "── arm $1 (BRUTEX_SWEEP_RUST_ENUM=$2) ──"
  env BRUTEX_SWEEP_RUST_ENUM="$2" BRUTEX_RESULTS_DEPTH=0 \
    "$PYTHON" -m brutex sweep-all \
    --symbols "$TWIN_SYMBOLS" \
    --start "$TWIN_START" --end "$TWIN_END" \
    --intervals "$TWIN_INTERVALS" \
    --directions "$TWIN_DIRECTIONS" \
    --k "$TWIN_K" \
    --top "$TWIN_TOP" \
    --lake-root "$LAKE_ROOT" \
    --results-root "$arm_root/results" \
    --catalog "$arm_root/catalog.duckdb"
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
if cmp -s "$OFF_MANIFEST" "$ON_MANIFEST"; then
  echo "TWIN-RUN PASS: BRUTEX_SWEEP_RUST_ENUM=0 and =1 produced BYTE-IDENTICAL artifacts"
  echo "  ($(wc -l < "$OFF_MANIFEST" | tr -d ' ') files compared; scratch kept at $SCRATCH)"
  exit 0
else
  echo "TWIN-RUN FAIL: the two arms DIVERGED — diff follows" >&2
  diff "$OFF_MANIFEST" "$ON_MANIFEST" >&2 || true
  exit 1
fi
