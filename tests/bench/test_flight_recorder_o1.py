"""O(1) bench gate for :meth:`FlightRecorder.record` (R41, CLAUDE.md §2 r5).

The recorder append must be latency-independent of how many records the
run's chain already holds — the underlying :class:`AuditWriter` keeps the
running ``seq``/``prev_hash`` in memory and never re-reads/re-hashes the
on-disk chain (the exact regression class the ratio catches: an O(n)
re-read/re-hash at the 100k base would blow the ceiling by orders of
magnitude). Mirrors ``tests/bench/test_orchestrator_audit_o1.py``, including
its honest prefill delta: bases are prefilled via the writer-equivalent
:func:`tests.fakes.audit_chain.write_clean_chain` (no per-line fsync) and the
recorder resumes them (O(1) tail read), so ONLY the timed region runs the
real production ``record`` (fsync included, nothing patched out). The ratio
routes through the single-source-of-truth
:func:`tests.bench._o1_tiers.ratio_ceiling` (5× shared CI / strict M4-Pro
tier). HONEST envelope: the per-append fsync is an additive constant shared
by both bases — it compresses the ratio, so the gate reliably catches the
heavyweight re-read/re-hash class, not an arbitrarily cheap O(n) term.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from brutex.observability.flight_recorder import (
    FlightRecorder,
    recorder_path,
    verify_recorder_chain,
)

from tests.bench._o1_tiers import ratio_ceiling
from tests.fakes.audit_chain import write_clean_chain

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r41_prefill_is_recorder_equivalent": ("R41",),
    "test_r41_record_ratio_is_o1_vs_chain_length": ("R41",),
}

#: Prior-record bases — LARGE ≫ SMALL so an O(n) re-hash would be ~100× slower.
_SMALL: Final[int] = 1_000
_LARGE: Final[int] = 100_000

_RUN_ID: Final[str] = "0f" * 32

#: Fixed payload — constant canonical-JSON cost; we time the chain append.
_PAYLOAD: Final[dict[str, int]] = {"index": 7, "done_cum": 8, "skipped_cum": 0, "failed_cum": 0}


def _resumed(results_root: Path, n: int) -> FlightRecorder:
    """Prefill *n* writer-equivalent lines (no fsync), resume the real recorder."""
    path = recorder_path(results_root, _RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_clean_chain(path, n)
    return FlightRecorder.open(results_root, run_id=_RUN_ID, attempt=1)


class TestRecordIsO1:
    def test_r41_prefill_is_recorder_equivalent(self, tmp_path: Path) -> None:
        """The hand-built prefill is genuinely chain-valid (defensive pin):
        real records resumed onto a 25-line prefill continue the chain and the
        WHOLE file passes the D-2 matching verifier."""
        recorder = _resumed(tmp_path, 25)
        for _ in range(5):
            assert recorder.record("CHECKPOINT", _PAYLOAD) is True
        recorder.close()
        assert verify_recorder_chain(tmp_path, _RUN_ID) == 30
        assert recorder.dropped == 0
        assert recorder.degraded is False

    def test_r41_record_ratio_is_o1_vs_chain_length(self, tmp_path: Path) -> None:
        """Recording one event costs the same at a 100k-record base as at 1k.

        An O(1) record keeps the running ``prev_hash``/``seq`` in memory and
        hashes only the new record. An O(n) regression (re-reading or
        re-hashing the whole on-disk chain per record) scales with the
        prior-record count and blows the ratio.
        """

        def _time(name: str, n: int, *, iterations: int = 400) -> float:
            root = tmp_path / name
            recorder = _resumed(root, n)
            try:
                for _ in range(25):  # warm
                    recorder.record("CHECKPOINT", _PAYLOAD)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    recorder.record("TUPLE_DONE", _PAYLOAD)
                t1 = time.perf_counter_ns()
            finally:
                recorder.close()
            assert recorder.dropped == 0
            assert recorder.degraded is False
            return (t1 - t0) / iterations

        t_small = _time("small", _SMALL)
        t_large = _time("large", _LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"FlightRecorder.record ratio {ratio:.2f}x (100k vs 1k prior records) exceeds "
            f"the ceiling — the append may have regressed from O(1) to an O(n) "
            f"re-hash/re-read of the chain. t_1k={t_small:.0f}ns, t_100k={t_large:.0f}ns."
        )
