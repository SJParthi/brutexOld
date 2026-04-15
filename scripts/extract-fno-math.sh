#!/usr/bin/env bash
#
# scripts/extract-fno-math.sh
#
# One-shot extraction tool: copies tickvault's pure-compute Rust files
# (crates/trading/src/greeks/**, crates/trading/src/indicator/**, and
# pure-math helpers) into BruteX's rust/fno-math/src/ tree.
#
# Per REQUIREMENTS.md §7A Shared Core Library Contract. After this
# script runs, the operator must:
#
#   1. Open a PR on SJParthi/tickvault that makes tickvault depend on
#      this fno-math crate instead of its in-tree files.
#   2. Build the PyO3 wrapper:  cd rust/fno-math-py && maturin develop
#   3. Replace brutex/math/__init__.py's NotImplementedError placeholder
#      with re-exports from fno_math_py.
#   4. Run the parity tests:  pytest tests/golden/test_tickvault_math_parity.py
#   5. Commit everything on a BruteX branch.
#
# Prerequisites:
#   - Both repositories checked out locally
#   - rsync installed
#   - Rust toolchain installed (for the subsequent `cargo check`)
#
# Usage:
#   ./scripts/extract-fno-math.sh /path/to/tickvault /path/to/brutex
#
# Defaults:
#   tickvault: $HOME/dev/tickvault
#   brutex:    $(git rev-parse --show-toplevel)

set -euo pipefail

TICKVAULT_ROOT="${1:-${HOME}/dev/tickvault}"
BRUTEX_ROOT="${2:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

FNO_MATH_ROOT="${BRUTEX_ROOT}/rust/fno-math"

# ── Sanity checks ─────────────────────────────────────────────────

if [[ ! -d "${TICKVAULT_ROOT}" ]]; then
    cat >&2 <<EOF
error: tickvault repo not found at: ${TICKVAULT_ROOT}

Pass the correct path as the first argument:
  ./scripts/extract-fno-math.sh /path/to/tickvault

Or set it as the default by editing this script.
EOF
    exit 1
fi

if [[ ! -d "${TICKVAULT_ROOT}/crates/trading/src/greeks" ]]; then
    cat >&2 <<EOF
error: ${TICKVAULT_ROOT}/crates/trading/src/greeks does not exist.

The extraction script expects tickvault's standard layout with
crates/trading/src/greeks/ and crates/trading/src/indicator/ present.

Did you clone the wrong repo, or did tickvault's layout change?
EOF
    exit 1
fi

if [[ ! -d "${FNO_MATH_ROOT}" ]]; then
    cat >&2 <<EOF
error: fno-math crate not found at: ${FNO_MATH_ROOT}

Run this script from inside a checkout of SJParthi/bruteX, or pass
the bruteX root as the second argument.
EOF
    exit 1
fi

# ── Capture tickvault commit SHA for the provenance record ───────

pushd "${TICKVAULT_ROOT}" >/dev/null
TICKVAULT_SHA="$(git rev-parse HEAD)"
TICKVAULT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
TICKVAULT_DIRTY="$(git status --porcelain | head -c1)"
popd >/dev/null

if [[ -n "${TICKVAULT_DIRTY}" ]]; then
    cat >&2 <<EOF
warning: tickvault working tree has uncommitted changes.
         Extraction will include them, but the provenance SHA
         (${TICKVAULT_SHA}) will not reflect them.

Press Enter to continue or Ctrl-C to abort.
EOF
    read -r
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  fno-math extraction"
echo "═══════════════════════════════════════════════════════════════"
echo "  tickvault:  ${TICKVAULT_ROOT}"
echo "  tickvault@: ${TICKVAULT_SHA} (${TICKVAULT_BRANCH})"
echo "  fno-math:   ${FNO_MATH_ROOT}"
echo "  provenance: ${FNO_MATH_ROOT}/EXTRACTION.md"
echo "═══════════════════════════════════════════════════════════════"

# ── Do the copy ──────────────────────────────────────────────────

# Greeks
mkdir -p "${FNO_MATH_ROOT}/src/greeks"
echo "▸ Copying greeks/..."
rsync -a --delete \
    --exclude='target' \
    --exclude='*.lock' \
    "${TICKVAULT_ROOT}/crates/trading/src/greeks/" \
    "${FNO_MATH_ROOT}/src/greeks/"

# Indicators (tickvault has very few — mostly OBI)
mkdir -p "${FNO_MATH_ROOT}/src/indicators"
echo "▸ Copying indicators/..."
rsync -a --delete \
    --exclude='target' \
    --exclude='*.lock' \
    "${TICKVAULT_ROOT}/crates/trading/src/indicator/" \
    "${FNO_MATH_ROOT}/src/indicators/"

# ── Rewrite internal module paths ────────────────────────────────
#
# Tickvault's files reference crate::greeks::, crate::indicator::, etc.
# After extraction, those become fno_math::greeks::, fno_math::indicators::
# We rewrite the imports in place so the extracted files compile as
# members of the fno-math crate.

echo "▸ Rewriting module paths..."

# crate::trading::greeks    → crate::greeks   (fno-math's own self-ref)
# crate::trading::indicator → crate::indicators
# ...then for any external consumer rewrites, use find + sed in a
# commit-local edit. Exact patterns depend on tickvault's actual
# imports — the operator reviews the diff before committing.

find "${FNO_MATH_ROOT}/src/greeks" "${FNO_MATH_ROOT}/src/indicators" \
    -name '*.rs' -type f | while read -r f; do
    # POSIX sed compatible substitutions (works on macOS and Linux)
    sed -i.bak \
        -e 's|crate::trading::greeks|crate::greeks|g' \
        -e 's|crate::trading::indicator|crate::indicators|g' \
        -e 's|crate::greeks::|crate::greeks::|g' \
        -e 's|crate::indicator::|crate::indicators::|g' \
        "$f"
    rm -f "${f}.bak"
done

# ── Write provenance file ────────────────────────────────────────

cat > "${FNO_MATH_ROOT}/EXTRACTION.md" <<EOF
# fno-math — extraction provenance

This file records which commit of tickvault was extracted into this
crate. The drift-check script (\`scripts/check-fno-math-drift.sh\`)
uses this record to detect when tickvault has moved ahead and a
re-extraction is needed.

| Field | Value |
|-------|-------|
| Extracted from | \`https://github.com/SJParthi/tickvault\` |
| Commit SHA | \`${TICKVAULT_SHA}\` |
| Branch | \`${TICKVAULT_BRANCH}\` |
| Extracted at | $(date -u +%Y-%m-%dT%H:%M:%SZ) |
| Dirty tree | $([ -n "${TICKVAULT_DIRTY}" ] && echo "YES (uncommitted changes)" || echo "no") |
| Extractor | \`scripts/extract-fno-math.sh\` |

## Re-extraction

When tickvault updates its math code, run:

\`\`\`bash
scripts/extract-fno-math.sh /path/to/tickvault
\`\`\`

Review the diff, commit, bump \`rust/fno-math/Cargo.toml\` version,
then open a PR on both tickvault and brutex that updates to the new
version in lockstep (cross-repo CI enforces the pairing).
EOF

echo "✅ Extraction complete."
echo ""
echo "Next steps:"
echo "  1. cd ${BRUTEX_ROOT}/rust/fno-math"
echo "  2. cargo check    # verify the copied code compiles as-is"
echo "  3. Review the diff:  git diff rust/fno-math/"
echo "  4. cd ../fno-math-py && maturin develop"
echo "  5. cd ${BRUTEX_ROOT} && pytest tests/golden/test_tickvault_math_parity.py"
echo "  6. git add rust/ brutex/math/ tests/golden/"
echo "  7. git commit -m 'feat(math): extract fno-math from tickvault@${TICKVAULT_SHA:0:12}'"
