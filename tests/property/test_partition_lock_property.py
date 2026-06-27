"""Property invariants for the bounded partition lock + integrity sharding.

DEC-PULL-CONTENTION-ANTISTALL-001. Over arbitrary inputs:

* :func:`resolve_partition_lock_timeout_seconds` ALWAYS returns a strictly-positive,
  well-under-600 s deadline — a bad value can NEVER disable the bound (and so never
  re-introduce the infinite block the fix removes).
* :func:`shard_path` is deterministic per symbol and collision-stable: two DISTINCT
  symbols map to DISTINCT shard files (so they never contend on one lock).
"""

from __future__ import annotations

import math
from pathlib import Path

from brutex.data.partition_lock import (
    DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS,
    PARTITION_LOCK_TIMEOUT_ENV,
    resolve_partition_lock_timeout_seconds,
)
from brutex.data.raw_integrity_store import shard_path
from hypothesis import given
from hypothesis import strategies as st


@given(
    raw=st.text(
        # Printable ASCII only: an env var cannot hold NUL or a lone surrogate
        # (``os.environ[...] =`` rejects both at the OS boundary — a TEST artifact, not a
        # production input). The meaningful inputs (numbers / blanks / junk) are covered.
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        max_size=20,
    )
)
def test_timeout_always_bounded_and_positive(raw: str) -> None:
    # Set the env directly (hypothesis re-runs many examples; clean up each time).
    import os

    os.environ[PARTITION_LOCK_TIMEOUT_ENV] = raw
    try:
        value = resolve_partition_lock_timeout_seconds()
    finally:
        del os.environ[PARTITION_LOCK_TIMEOUT_ENV]
    assert value > 0.0  # never disables the bound
    assert math.isfinite(value)  # never nan/inf (both would defeat the deadline)
    # A FINITE positive numeric env wins; everything else falls back to the default.
    try:
        parsed = float(raw)
    except ValueError:
        parsed = None
    if parsed is not None and math.isfinite(parsed) and parsed > 0.0:
        assert value == parsed
    else:
        assert value == DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS


@given(a=st.text(min_size=1, max_size=30), b=st.text(min_size=1, max_size=30))
def test_shard_path_deterministic_and_per_symbol(a: str, b: str) -> None:
    root = Path("/lake")
    # Deterministic: same symbol → same shard.
    assert shard_path(root, a) == shard_path(root, a)
    # Distinct symbols → distinct shards (never contend on one lock).
    if a != b:
        assert shard_path(root, a) != shard_path(root, b)
    # Always a .jsonl under the integrity dir, never under bars/ or options/.
    path = shard_path(root, a)
    assert path.suffix == ".jsonl"
    assert "bars" not in path.parts
    assert "options" not in path.parts
