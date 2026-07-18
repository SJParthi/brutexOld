#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# no-dhan-guard.sh — zero-`dhan` enforcement for the LIVE BruteX surface.
#
# WHY
# ---
# The live vendor is Groww (DEC-GROWW-DHAN-REVERT-001's successor migration;
# ADR-0009 §5 — the Dhan adapter was fully removed in the J4 PR series). This
# guard prevents the retired "dhan" vendor word from creeping back into any
# code, comment, docstring, operator doc, or script. If it finds `dhan`
# (case-insensitive) anywhere on the scanned surface, it prints every
# offending `file:line` and FAILS (exit 1). A clean surface exits 0.
#
# AMENDED by DEC-DHAN-ADAPTER-001 (2026-07-18, ADR-0024): the operator directed
# a SECOND vendor adapter — Dhan Data API v2, LAKE/reference-only, INERT until
# the ₹499/mo Data-API subscription + credentials are provisioned. The ONE
# sanctioned home for the vendor word is the new `brutex/adapters/dhan/`
# package (exemption 5 below). Everywhere ELSE the ban STANDS — which makes
# this guard the STRUCTURAL enforcement of the adapter's inertness: any
# attempt to wire Dhan into the engine / dashboard / CLI / pull spines would
# necessarily name it outside the sanctioned package and FAIL this guard,
# forcing the conscious live-wiring DEC the ADR requires.
#
# POSITIVE SCAN SCOPE (where `dhan` is forbidden)
# -----------------------------------------------
#   - brutex/            (the Python package — all live code)
#   - scripts/           (operator + CI scripts) EXCEPT this guard itself
#   - CLAUDE.md          (operator project rules, repo root)
#   - README.md          (operator entry doc, repo root)
#   - docs/runbooks/     (operator-facing runbooks)
#
# EXEMPTIONS (where `dhan` is allowed / not scanned) — the LEDGER rationale
# ------------------------------------------------------------------------
#   1. The immutable decision-ID tokens that contain "DHAN" ANYWHERE — both the
#      `DEC-DHAN-<...>` family (e.g. DEC-DHAN-1M-ONLY-001) AND the migration IDs
#      that carry the word mid-token (e.g. DEC-GROWW-DHAN-REVERT-001,
#      DEC-DHAN-GROWW-REMIGRATE-001). These name locked decisions in the ledger
#      and can never be rewritten (renaming a decision ID would orphan every
#      cross-reference). They are STRIPPED from each line before the scan, so a
#      line whose ONLY `dhan` is inside a `DEC-...DHAN...` token passes.
#   2. The historical/design-history docs under `docs/` — the decision ledger
#      (`docs/MIGRATION.md`), the ADRs (`docs/adr/`), and the Track-1 spec +
#      sibling design docs (`docs/TRACK1_*.md`, etc.). These record the Dhan
#      era as immutable history and are NOT scanned. The ONLY operator docs in
#      `docs/` that ARE scanned are the runbooks (`docs/runbooks/`), which were
#      scrubbed to Groww. (CLAUDE.md / README.md live at the repo root and are
#      scanned directly.)
#   3. This guard script itself (`scripts/ci/no-dhan-guard.sh`) — it documents
#      the exemptions and necessarily contains the word it bans.
#   4. Build / VCS / vendor artifacts: `.git/`, `.venv/`, `target/`,
#      `__pycache__/`, `*.egg-info/`, `node_modules/`.
#   5. `brutex/adapters/dhan/` — the SANCTIONED second-vendor adapter package
#      (DEC-DHAN-ADAPTER-001 / ADR-0024; lake/reference-only, subscription-
#      gated, INERT). The vendor word is legitimate ONLY here; the ban on the
#      rest of `brutex/` is what structurally keeps the adapter unwired.
#
# USAGE
# -----
#   bash scripts/ci/no-dhan-guard.sh
#
# EXIT CODES
# ----------
#   0 — no forbidden `dhan` found on the scanned surface.
#   1 — one or more forbidden `dhan` occurrences (each printed as file:line).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Resolve the repo root (this script lives in <root>/scripts/ci/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# This guard script's own path (relative to repo root), excluded from the scan.
SELF_REL="scripts/ci/no-dhan-guard.sh"

# Build the list of files to scan. We enumerate the positive-scope roots and
# filter out the artifact directories + this script itself. `git ls-files`
# would miss untracked-but-staged work, so we walk the filesystem directly and
# exclude artifacts explicitly (the guard must hold even outside a git tree).
scan_targets=(
  "brutex"
  "scripts"
  "CLAUDE.md"
  "README.md"
  "docs/runbooks"
)

# Collect candidate files (NUL-delimited for path safety).
mapfile -d '' -t files < <(
  find "${scan_targets[@]}" \
    -type d \( \
      -name '.git' -o \
      -name '.venv' -o \
      -name 'target' -o \
      -name '__pycache__' -o \
      -name 'node_modules' -o \
      -name '*.egg-info' \
    \) -prune -o \
    -path 'brutex/adapters/dhan' -prune -o \
    -type f -print0 2>/dev/null
)

hits=0
for f in "${files[@]}"; do
  # Exempt the guard script itself.
  if [[ "${f#./}" == "${SELF_REL}" ]]; then
    continue
  fi
  # Strip every immutable decision-ID token (`DEC-<UPPER-WORDS>-NNN`) THEN scan
  # for `dhan` (case-insensitive). This exempts both the DEC-DHAN-* family AND
  # the migration IDs that carry the word mid-token (e.g.
  # DEC-GROWW-DHAN-REVERT-001, DEC-DHAN-GROWW-REMIGRATE-001) — a DEC-ID is a
  # ledger citation, never the live vendor word itself, so removing all of them
  # only ever hides `dhan` that lived inside an immutable ID. `grep -n` reports
  # the original 1-based line numbers.
  while IFS= read -r match; do
    echo "${f#./}:${match}"
    hits=$((hits + 1))
  done < <(sed -E 's/DEC-[A-Za-z0-9-]+//g' "$f" | grep -niE 'dhan' || true)
done

if [[ "${hits}" -gt 0 ]]; then
  echo ""
  echo "❌ no-dhan-guard: found ${hits} forbidden 'dhan' occurrence(s) on the live surface."
  echo "   The live vendor is Groww. The ONLY sanctioned 'dhan' home is the inert"
  echo "   second-vendor package brutex/adapters/dhan/ (DEC-DHAN-ADAPTER-001 / ADR-0024)"
  echo "   — wiring Dhan anywhere else requires its own live-wiring DEC first."
  echo "   (Immutable DEC-DHAN-* decision IDs are exempt and are stripped before scanning;"
  echo "    historical docs under docs/ — MIGRATION.md, adr/, TRACK1_*.md — are not scanned.)"
  exit 1
fi

echo "✅ no-dhan-guard: zero forbidden 'dhan' outside the sanctioned adapter package (brutex/ minus adapters/dhan/, scripts/, CLAUDE.md, README.md, docs/runbooks/)."
exit 0
