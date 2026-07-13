"""Bench pins for the stage-stamp cost model (R42 — HONEST-linear validate).

Two pins, both routed through the single-source-of-truth tier helpers
(``tests/bench/_o1_tiers``) with best-of-N-MIN timing (DEC-CANARY-MINTIME-001):

1. **validate is HONESTLY O(artifact bytes):** the docstring claims linear —
   the pin asserts the PER-BYTE cost is FLAT as the artifact grows 32×
   (a super-linear regression, e.g. an accidental double read or a whole-file
   ``read_bytes`` + copy per chunk, inflates the per-byte ratio). The
   big/small workloads cross a CPU cache tier, so the strict tier uses the
   measured :func:`cache_tier_ceiling` allowance.
2. **stamp PARSE is flat vs artifact size:** :func:`read_stage_stamp` touches
   only the ~200-byte sidecar, so its latency must be independent of how big
   the stamped artifact is.

Never claimed: an O(1)-total validate — re-hashing the content is the whole
point of the R42 stamp law (Golden Rule #10).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from brutex.observability.stage_stamp import (
    StampVerdict,
    read_stage_stamp,
    validate_stage_stamp,
    write_stage_stamp,
)

from tests.bench._o1_tiers import cache_tier_ceiling, ratio_ceiling

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r42_validate_per_byte_cost_flat_honest_linear": ("R42",),
    "test_r42_stamp_parse_flat_vs_artifact_size": ("R42",),
}

_RUN_ID: Final[str] = "1a" * 32

#: Artifact sizes: 256 KiB vs 8 MiB (32×) — big enough that an O(bytes²) or
#: double-read regression is unmissable, small enough for shared CI.
_SMALL_BYTES: Final[int] = 256 * 1024
_LARGE_BYTES: Final[int] = 8 * 1024 * 1024


def _best_of_min(op: Callable[[], object], *, rounds: int = 7, warmup: int = 2) -> float:
    """Best-of-N-MIN wall time per op in ns (DEC-CANARY-MINTIME-001 idiom)."""
    for _ in range(warmup):
        op()
    best = float("inf")
    for _ in range(rounds):
        t0 = time.perf_counter_ns()
        op()
        t1 = time.perf_counter_ns()
        best = min(best, float(t1 - t0))
    return best


def _stamped_artifact(tmp_path: Path, name: str, size: int) -> Path:
    artifact = tmp_path / name
    artifact.write_bytes(b"\xa5" * size)
    write_stage_stamp(artifact, stage="bench", run_id=_RUN_ID)
    return artifact


class TestStampCostModel:
    def test_r42_validate_per_byte_cost_flat_honest_linear(self, tmp_path: Path) -> None:
        """validate's PER-BYTE cost is flat as the artifact grows 32× — the
        honest-linear pin (total cost grows with bytes BY DESIGN; a
        super-linear or double-read regression inflates per-byte cost)."""
        small = _stamped_artifact(tmp_path, "small.bin", _SMALL_BYTES)
        large = _stamped_artifact(tmp_path, "large.bin", _LARGE_BYTES)

        def _validate(artifact: Path) -> None:
            verdict, _ = validate_stage_stamp(artifact, expected_stage="bench")
            assert verdict is StampVerdict.OK

        t_small = _best_of_min(lambda: _validate(small))
        t_large = _best_of_min(lambda: _validate(large))
        per_byte_small = t_small / _SMALL_BYTES
        per_byte_large = t_large / _LARGE_BYTES
        ratio = per_byte_large / max(per_byte_small, 1e-9)
        assert ratio < cache_tier_ceiling(5.0), (
            f"validate_stage_stamp per-byte cost ratio {ratio:.2f}x (8MiB vs 256KiB) exceeds "
            f"the ceiling — the streamed hash may have regressed super-linearly. "
            f"per-byte small={per_byte_small:.3f}ns, large={per_byte_large:.3f}ns."
        )

    def test_r42_stamp_parse_flat_vs_artifact_size(self, tmp_path: Path) -> None:
        """read_stage_stamp (sidecar parse only) is independent of artifact size."""
        small = _stamped_artifact(tmp_path, "parse-small.bin", _SMALL_BYTES)
        large = _stamped_artifact(tmp_path, "parse-large.bin", _LARGE_BYTES)

        def _time(artifact: Path, *, iterations: int = 200) -> float:
            def _op() -> None:
                for _ in range(iterations):
                    stamp = read_stage_stamp(artifact)
                    assert stamp.stage == "bench"

            return _best_of_min(_op) / iterations

        t_small = _time(small)
        t_large = _time(large)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"read_stage_stamp ratio {ratio:.2f}x (8MiB vs 256KiB artifact) exceeds the "
            f"ceiling — the parse may have regressed to touching the ARTIFACT bytes. "
            f"t_small={t_small:.0f}ns, t_large={t_large:.0f}ns."
        )
