#!/usr/bin/env bash
# renumber.sh — club X refold: resolve the MIGRATION.md #1795 double-claim.
#
# Context: merging integration/club-x-dhan with post-club-P main auto-merges
# docs/MIGRATION.md textually but leaves entry number 1795 claimed TWICE:
#   - club P's  DEC-AUDIT-LAKE-REPLAY-ORACLE-001  (stays at #1795 — untouched)
#   - Dhan's    DEC-FEED-INTERFACE-001            (moves to NEXT-FREE)
# validate_ledger_numbering.py exits 1 on the merged file until this runs.
#
# TARGET NUMBER IS DETECTED AT EXECUTION (coordinator ruling 2026-07-15):
# target = (max entry-header number in the MERGED docs/MIGRATION.md) + 1.
# As of the 2026-07-15 dry run that detects #1800 (max header = #1799;
# DEC-DHAN-GUARD-RETIRE-002/#1800 did NOT land — deferred post-merge).
# If a parallel branch lands more entries first, the detection self-adjusts.
#
# Deterministic + idempotent: re-running after a successful apply is a no-op
# (detected via the DEC-FEED-INTERFACE-001 header no longer sitting at #1795).
# Anchors are DEC-id-adjacent — NEVER a bare-number global replace, so club P's
# own #1795 citations and coincidental numeric matches are untouchable.
#
# Usage (from the repo root of the merged tree, CLAUDE.md conflict already
# resolved):   bash renumber.sh
# Optional:    REFOLD_DATE=YYYY-MM-DD bash renumber.sh   (default 2026-07-15)
set -euo pipefail

REFOLD_DATE="${REFOLD_DATE:-2026-07-15}"

python3 - "$REFOLD_DATE" <<'PYEOF'
import io
import re
import sys

DATE = sys.argv[1]
OLD = "1795"

def read(p):
    return io.open(p, encoding="utf-8").read()

def write(p, s):
    io.open(p, "w", encoding="utf-8", newline="").write(s)

mig_path = "docs/MIGRATION.md"
mig = read(mig_path)

# ---- idempotency gate + target detection -------------------------------
hdr_at_1795 = re.search(r"(?m)^1795\. \*\*DEC-FEED-INTERFACE-001", mig)
hdr_any = re.search(r"(?m)^(\d+)[a-z]?\. \*\*DEC-FEED-INTERFACE-001", mig)
if hdr_any is None:
    print("ERROR: no DEC-FEED-INTERFACE-001 entry header found in "
          "docs/MIGRATION.md — wrong tree?", file=sys.stderr)
    sys.exit(2)
if hdr_at_1795 is None:
    print(f"ALREADY APPLIED: DEC-FEED-INTERFACE-001 header sits at "
          f"#{hdr_any.group(1)} (not #1795) — no-op.")
    sys.exit(0)

all_nums = [int(m.group(1))
            for m in re.finditer(r"(?m)^(\d+)[a-z]?\. \*\*", mig)]
TARGET = str(max(all_nums) + 1)
print(f"DETECTED next-free ledger number: #{TARGET} "
      f"(max existing header = #{max(all_nums)})")

# ---- replacement table --------------------------------------------------
# (file, anchor, replacement, expected_count) — every anchor is
# DEC-FEED-INTERFACE-001-adjacent or full-sentence, never a bare number.
T = TARGET
chain_old = ("DEC-LEDGER-RENUMBER-001): #1792 → #1795 — "
             "main's 2026-07-14/15 wave")
chain_new = ("DEC-LEDGER-RENUMBER-001): #1792 → #1795 → #" + T +
             " (the #1795 → #" + T + " hop at the " + DATE +
             " post-club-P refold — club P / PR #1547 took #1795 for "
             "DEC-AUDIT-LAKE-REPLAY-ORACLE-001) — "
             "main's 2026-07-14/15 wave")
claude_hdr_old = (
    "The three colliding ledger rows were renumbered to the free\n"
    "consecutive #1795–#1799 block (DEC-IDs are the stable handles per\n"
    "DEC-LEDGER-RENUMBER-001).")
claude_hdr_new = (
    "The three colliding ledger rows were renumbered to the free\n"
    "consecutive #1795–#1799 block, then DEC-FEED-INTERFACE-001 moved\n"
    "#1795 → #" + T + " at the " + DATE + " post-club-P refold "
    "(club P / PR #1547 took\n#1795 for DEC-AUDIT-LAKE-REPLAY-ORACLE-001; "
    "DEC-IDs are the stable handles\nper DEC-LEDGER-RENUMBER-001).")

REPL = [
    # docs/MIGRATION.md — the header rename + in-entry chain extension +
    # the two umbrella/#1799 cross-citations.
    (mig_path, "\n1795. **DEC-FEED-INTERFACE-001",
               "\n" + T + ". **DEC-FEED-INTERFACE-001", 1),
    (mig_path, chain_old, chain_new, 1),
    (mig_path, "`DEC-FEED-INTERFACE-001` #1795",
               "`DEC-FEED-INTERFACE-001` #" + T, 1),
    (mig_path, "ADR-0021 (#1795/#1796)", "ADR-0021 (#" + T + "/#1796)", 1),
    # CLAUDE.md — header sentence rewrite + the 3 DEC-adjacent citations
    # (header line ~25, §1 Active-branch blurb ~2645, §3 blurb ~7277).
    ("CLAUDE.md", claude_hdr_old, claude_hdr_new, 1),
    ("CLAUDE.md", "DEC-FEED-INTERFACE-001 #1795",
                  "DEC-FEED-INTERFACE-001 #" + T, 3),
    # ci.yml — 4 per-module coverage-gate comments.
    (".github/workflows/ci.yml",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #1795)",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #" + T + ")", 4),
    # brutex module docstrings.
    ("brutex/data/feed_audit.py",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #1795)",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #" + T + ")", 1),
    ("brutex/feeds/__init__.py",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #1795)",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #" + T + ")", 1),
    ("brutex/feeds/binding.py",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #1795)",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #" + T + ")", 1),
    ("brutex/feeds/groww_binding.py",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #1795)",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #" + T + ")", 1),
    ("brutex/feeds/registry.py",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #1795)",
     "DEC-FEED-INTERFACE-001 (MIGRATION.md #" + T + ")", 1),
    # ADR-0021 Decision-IDs line.
    ("docs/adr/0021-historical-feed-and-feed-lake-root.md",
     "`DEC-FEED-INTERFACE-001` (MIGRATION.md entry #1795)",
     "`DEC-FEED-INTERFACE-001` (MIGRATION.md entry #" + T + ")", 1),
    # test docstrings.
    ("tests/integration/test_feed_lake_byte_parity.py",
     "DEC-FEED-INTERFACE-001 (#1795)",
     "DEC-FEED-INTERFACE-001 (#" + T + ")", 1),
    ("tests/unit/test_data_feed_audit.py",
     "DEC-FEED-INTERFACE-001, #1795",
     "DEC-FEED-INTERFACE-001, #" + T, 1),
    ("tests/unit/test_feeds_binding.py",
     "DEC-FEED-INTERFACE-001, #1795",
     "DEC-FEED-INTERFACE-001, #" + T, 1),
    ("tests/unit/test_feeds_groww_binding.py",
     "DEC-FEED-INTERFACE-001, #1795",
     "DEC-FEED-INTERFACE-001, #" + T, 1),
    ("tests/unit/test_feeds_registry.py",
     "DEC-FEED-INTERFACE-001, #1795",
     "DEC-FEED-INTERFACE-001, #" + T, 1),
]

# ---- apply with exact-count verification --------------------------------
manifest = []
errors = []
by_file = {}
for path, old, new, expected in REPL:
    s = by_file.get(path) or read(path)
    n = s.count(old)
    if n != expected:
        errors.append(f"{path}: anchor {old[:70]!r} found {n}x, "
                      f"expected {expected}x")
        continue
    by_file[path] = s.replace(old, new)
    manifest.append((path, expected, old, new))

if errors:
    print("ANCHOR MISMATCH — nothing written. The merged tree drifted from "
          "the probe; re-derive before applying:", file=sys.stderr)
    for e in errors:
        print("  - " + e, file=sys.stderr)
    sys.exit(2)

for path, s in by_file.items():
    write(path, s)

# ---- self-check ----------------------------------------------------------
mig2 = read(mig_path)
club_p_1795 = len(re.findall(r"(?m)^1795\. \*\*DEC-AUDIT-LAKE-REPLAY-ORACLE-001",
                             mig2))
old_hdrs = len(re.findall(r"(?m)^1795\. ", mig2))
new_hdr = len(re.findall(r"(?m)^" + T + r"\. \*\*DEC-FEED-INTERFACE-001", mig2))
assert club_p_1795 == 1, "club P's #1795 header damaged!"
assert old_hdrs == 1, f"expected exactly ONE remaining #1795 header, got {old_hdrs}"
assert new_hdr == 1, f"expected ONE #{T} DEC-FEED-INTERFACE-001 header, got {new_hdr}"
for path in sorted({p for p, *_ in REPL} - {mig_path}):
    s2 = read(path)
    assert "DEC-FEED-INTERFACE-001 #1795" not in s2 \
        and "DEC-FEED-INTERFACE-001 (MIGRATION.md #1795)" not in s2 \
        and "DEC-FEED-INTERFACE-001 (#1795)" not in s2 \
        and "DEC-FEED-INTERFACE-001, #1795" not in s2 \
        and "`DEC-FEED-INTERFACE-001` (MIGRATION.md entry #1795)" not in s2, \
        f"stale DEC-FEED-INTERFACE-001/#1795 adjacency left in {path}"

# ---- manifest ------------------------------------------------------------
print(f"\nRENUMBER MANIFEST — DEC-FEED-INTERFACE-001 #1795 -> #{T} "
      f"({DATE} post-club-P refold)")
for path, cnt, old, new in manifest:
    print(f"  {path}  ({cnt}x)")
    print(f"    - {old[:96]!r}")
    print(f"    + {new[:96]!r}")
print("""
DELIBERATELY UNTOUCHED (verified, do not 'fix'):
  - docs/MIGRATION.md: club P's `1795. **DEC-AUDIT-LAKE-REPLAY-ORACLE-001`
    header + its own '#1723 -> #1795' chain note (its number is legitimate).
  - docs/MIGRATION.md: the 5 dated push-time-weave notes '...the next-free
    consecutive #1795-#1799 (DEC-IDs stay the stable handles).' in entries
    #1796/#1797/#1799 + the FEED-INTERFACE + ROLLING-FLOOR entries — a
    historical record of the 2026-07-15 weave event; the FEED-INTERFACE
    entry's EXTENDED chain note records the new hop.
  - docs/MIGRATION.md: DEC-DHAN-ROLLING-FLOOR-001's '#1795 -> #1798' chain
    note — that #1795 is ITS OWN renumber history, not a citation.
  - Coincidental numeric matches (not ledger refs): docs/dashboard/mockups/
    v28.html + v29.html ('17956' cost cell), docs/research/
    ARCHITECTURE_TRILLION_SCALE_EVAL.md ('sweep_run.py:1795-1817' source
    line cite), docs/security/sbom-rust-fno-math.cdx.json + rust/fno-math/
    Cargo.lock(+fuzz) (checksum bytes), rust/trading-core/tests/fixtures/
    stage1a_batch_golden.json + tests/golden/fixtures/
    twin_state_golden_vectors.json (frozen golden numeric data — NEVER edit).
SELF-CHECK PASSED.""")
PYEOF
