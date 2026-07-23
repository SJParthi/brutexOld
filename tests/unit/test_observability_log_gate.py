"""Unit battery for the COMMON LRU log-emission gate (DEC-RESULTS-TOPK-ROLLUP-001).

Drives :mod:`brutex.observability.log_gate` — the stdlib-only leaf shared by
the engine rollup writers and the dashboard ``/results`` serve path (the
PR #1436 CodeQL bidirectional-cycle fix) — to 100% line+branch STANDALONE:
the once-per-key emission law, the LRU eviction + re-emission bound, the
degraded-latch report-once recovery, and the capacity validation. Moved out
of ``tests/unit/test_engine_results_rollup.py`` at the CI-red fix round so
the leaf owns an import-verified dedicated suite (the
``coverage_gate_discovery`` NEW-module law).
"""

from __future__ import annotations

import pytest
from brutex.observability.log_gate import LogGate


class TestLogGate:
    def test_once_per_key(self) -> None:
        gate = LogGate()
        assert gate.should_emit(("a", "b")) is True
        assert gate.should_emit(("a", "b")) is False
        assert gate.should_emit(("a", "c")) is True

    def test_lru_eviction_re_emits(self) -> None:
        gate = LogGate(capacity=2)
        assert gate.should_emit(("k1",)) is True
        assert gate.should_emit(("k2",)) is True
        assert gate.should_emit(("k1",)) is False  # refreshed (move_to_end)
        assert gate.should_emit(("k3",)) is True  # evicts k2 (the LRU)
        assert gate.should_emit(("k2",)) is True  # evicted → re-emits (evicts k1)
        assert gate.should_emit(("k3",)) is False  # still resident
        assert gate.should_emit(("k1",)) is True  # evicted by k2's re-entry

    def test_clear_degraded_reports_once(self) -> None:
        gate = LogGate()
        assert gate.clear_degraded() is False
        gate.should_emit(("x",))
        assert gate.clear_degraded() is True
        assert gate.clear_degraded() is False

    def test_capacity_validation(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            LogGate(capacity=0)
