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
#   1b. The per-feed scope-map KEY `dhan` in its sanctioned CONFIG-TOKEN
#      spellings ONLY — DEC-UNIVERSE-INDEX-ONLY-001 (MIGRATION.md #1934, the
#      r6 confirmation-review fold): the coordinator-relayed directive-(B)
#      per-feed scope map is `{groww: index4, dhan: index4}`
#      (`brutex/config/feed_scope.py` `DEFAULT_FEED_SCOPES`), so the lowercase
#      key `dhan` names a scope-KNOWN adapterless feed SLOT by ledgered design,
#      never stale live-vendor wording. Stripped shapes (lowercase only, before
#      the scan): the quoted key `"dhan"`, the backticked key reference
#      (`dhan` / RST double-backtick), and the scope-binding forms
#      `dhan=index4|full` / `dhan: index4|full` (the env-CSV + map notations).
#      DELIBERATELY NARROW so the guard's stale-vendor-wording teeth survive:
#      bare prose ("pull from dhan"), identifiers (`dhan_client` — mid-token
#      never matches the backtick form), uppercase `DHAN`, and any
#      non-scope-token binding (`dhan: the old vendor`) ALL still FAIL —
#      pinned by tests/unit/test_no_dhan_guard.py.
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
    -type f -print0 2>/dev/null
)

hits=0
for f in "${files[@]}"; do
  # Exempt the guard script itself.
  if [[ "${f#./}" == "${SELF_REL}" ]]; then
    continue
  fi
  # Strip every immutable decision-ID token (`DEC-<UPPER-WORDS>-NNN`) AND the
  # exemption-1b sanctioned scope-map-KEY spellings, THEN scan for `dhan`
  # (case-insensitive). The DEC strip exempts both the DEC-DHAN-* family AND
  # the migration IDs that carry the word mid-token (e.g.
  # DEC-GROWW-DHAN-REVERT-001, DEC-DHAN-GROWW-REMIGRATE-001) — a DEC-ID is a
  # ledger citation, never the live vendor word itself. The 1b strip removes
  # ONLY the lowercase config-token shapes of the #1934 scope-map key
  # (`"dhan"` · backticked `dhan` · `dhan=index4|full` / `dhan: index4|full`),
  # so bare vendor prose, identifiers, and uppercase still fail. `grep -n`
  # reports the original 1-based line numbers.
  while IFS= read -r match; do
    echo "${f#./}:${match}"
    hits=$((hits + 1))
  done < <(sed -E 's/DEC-[A-Za-z0-9-]+//g; s/"dhan"|`+dhan`+|dhan ?[=:] ?(index4|full)//g' "$f" | grep -niE 'dhan' || true)
done

if [[ "${hits}" -gt 0 ]]; then
  echo ""
  echo "❌ no-dhan-guard: found ${hits} forbidden 'dhan' occurrence(s) on the live surface."
  echo "   The live vendor is Groww — reword to Groww or vendor-neutral language."
  echo "   (Immutable DEC-DHAN-* decision IDs and the #1934 scope-map key's sanctioned"
  echo "    config-token spellings — \"dhan\" / backticked / dhan=index4|full — are exempt"
  echo "    and are stripped before scanning; historical docs under docs/ — MIGRATION.md,"
  echo "    adr/, TRACK1_*.md — are not scanned.)"
  exit 1
fi

echo "✅ no-dhan-guard: zero forbidden 'dhan' on the live surface (brutex/, scripts/, CLAUDE.md, README.md, docs/runbooks/)."
exit 0
