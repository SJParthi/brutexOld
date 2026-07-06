"""Sustained bounded-memory stress for the sweep dedup digest (`sweep_cache`).

DEC-ENGINE-DEDUP-NAN-CANON-001 (7-test-types: stress). Drives
:func:`brutex.engine.sweep_cache.combo_digest` — the per-combo canonicalise +
BLAKE3 dedup key run ~33B times in a full sweep (CLAUDE.md §2 Rule 5) — over
≥100K sustained calls and proves the honest O(1)-memory envelope holds AT SCALE:

* the Python-heap footprint stays FLAT (``combo_digest`` retains nothing across
  calls — one transient JSON string + one 32-byte digest per call), so a genuine
  per-call leak would cross the ABSOLUTE, N-independent ``tracemalloc`` net
  ceiling (the leak class one-shot unit tests structurally cannot catch); and
* the digest is bit-for-bit DETERMINISTIC across the whole run — the same logical
  combo computed before, during, and after 100K churned calls always yields the
  SAME 32-byte digest (Golden Rule #7 idempotence; the dedup ground truth).

The ``tracemalloc`` measurement recipe (untracked warmup → tracked warmup →
baseline + ``reset_peak()`` → sustained phase → assert net ≤ ceiling) mirrors
``tests/stress/test_memory_growth.py``. The ceiling is ABSOLUTE and
N-independent: a larger soak strictly STRENGTHENS the proof (the per-call
detection floor shrinks as 1/N).
"""

from __future__ import annotations

import gc
import math
import os
import tracemalloc

import pytest
from brutex.engine.sweep_cache import SweepCacheError, combo_digest

#: All stress tests carry the ``stress`` marker (registered in pyproject.toml) so
#: the nightly ``pytest tests/stress -m stress`` soak selects them (DEC-STRESS-001).
pytestmark = pytest.mark.stress

#: An EXPLICIT knob wins (targeted soak); otherwise the ≥100K proof default. A
#: 1-byte/call leak ≈ 100 KB at the default ≫ the 64 KiB net ceiling.
_DEFAULT_EVENTS = 100_000


def _resolve_events() -> int:
    raw = os.environ.get("STRESS_SWEEP_CACHE_EVENTS", "").strip()
    if raw:
        return max(1, int(raw))
    return _DEFAULT_EVENTS


#: Absolute, N-independent net ceiling (combo_digest retains nothing per call).
_NET_CEILING_BYTES = 64 * 1024

#: A representative FINITE combo carrying a finite float param (exercises the
#: ``allow_nan=False`` path the change added).
_PARAMS = {"sl_paisa": 80, "tp_paisa": 50, "prior_n": 3, "proximity": 0.25}


def _digest(seed: int) -> bytes:
    return combo_digest(
        seed % (1 << 52),
        "NIFTY",
        "5m",
        "bull",
        _PARAMS,
        # The three STRUCTURAL reproducibility hashes
        # (DEC-COMBO-DIGEST-REPRO-HASHES-001) — fixed here; the perf/memory
        # envelope is what these suites measure, not the identity.
        data_snapshot_hash="snap-aaaa",
        indicator_version="ind-v1",
        code_commit_sha="c0ffee01",
    )


def test_combo_digest_bounded_memory_and_deterministic_under_sustained_load() -> None:
    """≥100K digests hold a flat heap + stay bit-for-bit deterministic."""
    events = _resolve_events()
    # A fixed probe combo whose digest is re-checked before/during/after the soak.
    probe_before = _digest(7)

    # UNTRACKED warmup absorbs one-time allocations (interned strings, the blake3
    # code path, the JSON encoder's internal caches) so they never count as drift.
    for i in range(2_000):
        _digest(i)
    tracemalloc.start()
    # TRACKED warmup turns the working set over once (tracked-for-tracked).
    for i in range(2_000):
        _digest(i)
    gc.collect()
    base_current, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()

    # The sustained phase: no O(N) bookkeeping inside the measured region.
    mid = events // 2
    probe_mid: bytes | None = None
    for i in range(events):
        _digest(i)
        if i == mid:
            probe_mid = _digest(7)

    gc.collect()
    end_current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    net = end_current - base_current
    assert net <= _NET_CEILING_BYTES, (
        f"combo_digest net heap grew {net} B over {events} sustained digests "
        f"(ceiling {_NET_CEILING_BYTES} B) — a per-call retained allocation would "
        f"scale with N and cross this N-independent floor."
    )

    # Determinism held throughout the churn (before == mid == after).
    probe_after = _digest(7)
    assert probe_mid == probe_before
    assert probe_after == probe_before
    assert len(probe_after) == 32


def test_non_finite_float_always_raises_under_sustained_load() -> None:
    """The one-NaN-policy guard holds under sustained mixed finite/non-finite load."""
    non_finite = (float("nan"), float("inf"), float("-inf"))
    events = max(1, _resolve_events() // 100)  # bounded — this is a correctness soak
    for i in range(events):
        # A finite call always succeeds (32-byte digest).
        assert len(_digest(i)) == 32
        # A non-finite param ALWAYS raises, never silently admitted.
        bad = non_finite[i % len(non_finite)]
        assert not math.isfinite(bad)
        with pytest.raises(SweepCacheError, match="non-finite float"):
            combo_digest(
                i,
                "NIFTY",
                "5m",
                "bull",
                {"x": bad},
                data_snapshot_hash="snap-aaaa",
                indicator_version="ind-v1",
                code_commit_sha="c0ffee01",
            )
