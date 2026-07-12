"""Rust-arm differential suites (DEC-SWEEP-CORE-RUST-001, MIGRATION.md #1739).

The INDEPENDENT-ORACLE MANDATE home: the frozen hand-rolled fold reference
(:mod:`tests.rust_differential._reference_fold`) + the ≥10⁴-per-PR /
≥10⁶-on-branch ``fold_block`` differential lane. Nothing here imports
``brutex.metrics`` or ``fno_math`` into the ORACLE side — the AST pin in
``test_reference_fold_frozen.py`` enforces it.
"""
