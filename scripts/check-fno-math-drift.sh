#!/usr/bin/env bash
#
# scripts/check-fno-math-drift.sh
#
# Drift detector per REQUIREMENTS.md §7A.4. Clones the tickvault repo
# into a scratch dir, diffs its greeks/ and indicator/ source trees
# against this BruteX checkout's rust/fno-math/src/{greeks,indicators}/,
# and fails (exit 1) if they differ AND tickvault's SHA is newer than
# the one recorded in rust/fno-math/EXTRACTION.md.
#
# Designed to run in BruteX CI on every push. Zero side effects:
# scratch dir is cleaned up even on failure.
#
# Usage: ./scripts/check-fno-math-drift.sh

set -euo pipefail

BRUTEX_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
FNO_MATH_ROOT="${BRUTEX_ROOT}/rust/fno-math"
EXTRACTION_FILE="${FNO_MATH_ROOT}/EXTRACTION.md"

if [[ ! -f "${EXTRACTION_FILE}" ]]; then
    echo "⏸  Skipping drift check — rust/fno-math has not been extracted yet."
    echo "   (EXTRACTION.md not found; this is expected before first extraction.)"
    exit 0
fi

# Extract the commit SHA recorded at extraction time
RECORDED_SHA="$(grep -Eo '`[0-9a-f]{7,40}`' "${EXTRACTION_FILE}" | head -1 | tr -d '`')"
if [[ -z "${RECORDED_SHA}" ]]; then
    echo "❌ Could not parse recorded commit SHA from ${EXTRACTION_FILE}"
    exit 2
fi

SCRATCH="$(mktemp -d)"
trap 'rm -rf "${SCRATCH}"' EXIT

echo "▸ Cloning tickvault into scratch (shallow, single branch)..."
git clone --depth 100 --quiet \
    https://github.com/SJParthi/tickvault.git \
    "${SCRATCH}/tickvault" >&2

pushd "${SCRATCH}/tickvault" >/dev/null
CURRENT_SHA="$(git rev-parse HEAD)"

# Is the current HEAD newer than (or equal to) the recorded SHA?
if git merge-base --is-ancestor "${RECORDED_SHA}" HEAD 2>/dev/null; then
    AHEAD="$(git rev-list --count "${RECORDED_SHA}..HEAD" 2>/dev/null || echo "?")"
else
    echo "⚠️  Recorded SHA ${RECORDED_SHA} is not an ancestor of tickvault HEAD."
    echo "   This means tickvault may have been rewritten (force-push) or the"
    echo "   recorded SHA is wrong. Manual review required."
    popd >/dev/null
    exit 3
fi
popd >/dev/null

echo "▸ Recorded SHA:    ${RECORDED_SHA}"
echo "▸ Current SHA:     ${CURRENT_SHA}"
echo "▸ Commits ahead:   ${AHEAD}"

# Before diffing, normalise tickvault's import paths to match ours.
# Extraction rewrote ``tickvault_common::*`` → ``crate::compat::*``
# so a naive diff shows every greek/indicator file as changed. We
# apply the same rewrite to a copy of tickvault's tree and diff
# the normalised copy against ours. -bB (ignore whitespace) further
# absorbs any cargo-fmt drift.
NORMALISED="${SCRATCH}/tickvault-normalised"
mkdir -p "${NORMALISED}"
cp -r "${SCRATCH}/tickvault/crates/trading/src/greeks" "${NORMALISED}/greeks"
cp -r "${SCRATCH}/tickvault/crates/trading/src/indicator" "${NORMALISED}/indicators"
find "${NORMALISED}" -type f -name "*.rs" -print0 | xargs -0 \
    sed -i \
        -e 's|tickvault_common::|crate::compat::|g' \
        -e 's|crate::indicator::|crate::indicators::|g'

DIFF_OUTPUT="$(
    {
        diff -ruN -bB \
            --exclude='tests.rs' \
            "${NORMALISED}/greeks/" \
            "${FNO_MATH_ROOT}/src/greeks/" 2>/dev/null || true
        diff -ruN -bB \
            --exclude='tests.rs' \
            "${NORMALISED}/indicators/" \
            "${FNO_MATH_ROOT}/src/indicators/" 2>/dev/null || true
    } | grep -v '^Only in .*:' || true
)"

if [[ -z "${DIFF_OUTPUT}" ]]; then
    echo "✅ No drift. rust/fno-math is in sync with tickvault@${CURRENT_SHA:0:12}."
    exit 0
fi

# Drift present — but is it meaningful? Only fail if tickvault moved.
if [[ "${CURRENT_SHA}" == "${RECORDED_SHA}" ]]; then
    echo "⚠️  Drift detected but recorded SHA is current — local edit to"
    echo "    rust/fno-math/src/ that hasn't been reflected in tickvault yet."
    echo ""
    echo "    This is allowed during local development but must be resolved"
    echo "    before merging: open a companion PR on tickvault with the same"
    echo "    change, then re-record the SHA via scripts/extract-fno-math.sh."
    exit 4
fi

cat >&2 <<EOF
❌ DRIFT DETECTED — tickvault has moved ahead and BruteX's fno-math
   needs to be re-extracted.

   recorded: ${RECORDED_SHA} (in EXTRACTION.md)
   current:  ${CURRENT_SHA} (tickvault@main)
   ahead:    ${AHEAD} commits

Diff summary:
$(echo "${DIFF_OUTPUT}" | head -30)
$([ "$(echo "${DIFF_OUTPUT}" | wc -l)" -gt 30 ] && echo "  (truncated — run diff locally for full)")

To fix:
  1. Pull the latest tickvault:  cd /path/to/tickvault && git pull
  2. Re-run extraction:  scripts/extract-fno-math.sh /path/to/tickvault
  3. Review and commit the resulting diff
  4. Bump rust/fno-math/Cargo.toml version
  5. Re-run this drift check: scripts/check-fno-math-drift.sh
EOF
exit 1
