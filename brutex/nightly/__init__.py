"""BruteX nightly zero-click automation (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The R9 nightly chain — COLLISION → DISK → BACKUP → PULL → PREBUILD → SWEEP —
plus the R50 backup/restore and launchd scheduling. Modules are imported
explicitly (``brutex.nightly.chain`` etc.); NO re-exports here, so importing
one leaf never drags the whole package (and no import cycles can form).
"""
