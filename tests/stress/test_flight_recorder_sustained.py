"""Sustained-load stress: flight-recorder chain integrity under a 100k-record soak.

DEC-SWEEP-UNKILLABLE-CORE-001 (R41) — the recorder must survive a
100k-tuple-shaped run: a realistic mixed-kind stream (decimated TUPLE_DONE
via ``tuple_record_every`` + periodic CHECKPOINTs + the three milestone
edges + RUN_START/RUN_END) with ZERO drops, ZERO degradation, an EXACTLY
predictable on-disk record count, and a chain that verifies end-to-end
through the D-2 matching verifier.

Budget: ~100k ``record`` calls ≈ 100 s measured on this container (one
fsync per durable append — the honest dominant cost; decimation is exactly
why a 100k-tuple grid stays cheap: only ~10k TUPLE_DONE lines land).
Override via ``STRESS_EVENTS`` for longer local soaks. Memory profiling is
deliberately out of scope here (the ``test_audit_chain_sustained`` precedent:
``ru_maxrss`` is platform-divergent and process-noisy) — chain integrity +
exact counter honesty are the stress signals enforced.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest
from brutex.observability.flight_recorder import (
    FlightRecorder,
    search_timeline,
    verify_recorder_chain,
)

pytestmark = pytest.mark.stress

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r41_flight_recorder_100k_record_soak": ("R41", "R16"),
}

_RUN_ID: Final[str] = "5e" * 32

#: TUPLE_DONE decimation stride under soak (the D-9 production shape).
_EVERY: Final[int] = 10

#: CHECKPOINT cadence (never decimated).
_CHECKPOINT_EVERY: Final[int] = 1_000


def test_r41_flight_recorder_100k_record_soak(tmp_path: Path) -> None:
    """Soak the recorder with a 100k-tuple-shaped stream and re-verify the chain.

    Defaults to 100_000 tuple events. Override via ``STRESS_EVENTS``.
    """
    n_events = int(os.environ.get("STRESS_EVENTS", "100000"))
    recorder = FlightRecorder.open(tmp_path, run_id=_RUN_ID, attempt=1, tuple_record_every=_EVERY)
    assert recorder.record("RUN_START", {"total": n_events}) is True
    recorded_tuples = 0
    checkpoints = 0
    milestones = 0
    for i in range(1, n_events + 1):
        durable = recorder.record(
            "TUPLE_DONE",
            {"index": i - 1, "done_cum": i, "skipped_cum": 0, "failed_cum": 0},
        )
        if durable:
            recorded_tuples += 1
        if i % _CHECKPOINT_EVERY == 0:
            assert recorder.record("CHECKPOINT", {"index": i}) is True
            checkpoints += 1
        pct = (i * 100) // n_events
        for edge in (25, 50, 75):
            if pct >= edge and recorder.milestone_pct(edge, {"tuples_done": i, "total": n_events}):
                milestones += 1
    assert recorder.record("RUN_END", {"exit_code": 0, "suspect": False}) is True
    recorder.close()

    # Counter honesty — EXACT, never approximate (mutation-hardening).
    expected_tuples = (n_events - 1) // _EVERY + 1  # calls 1, 11, 21, ... (D-9)
    assert recorded_tuples == expected_tuples
    assert checkpoints == n_events // _CHECKPOINT_EVERY
    assert milestones == 3
    assert recorder.dropped == 0
    assert recorder.degraded is False

    # Chain integrity end-to-end through the D-2 matching verifier.
    expected_total = 1 + expected_tuples + checkpoints + milestones + 1
    assert verify_recorder_chain(tmp_path, _RUN_ID) == expected_total

    # Bounded forensics still works over the soaked timeline.
    tail = search_timeline(tmp_path, _RUN_ID, kind="CHECKPOINT", limit=5)
    tail_payloads = [r["payload"] for r in tail]
    assert all(isinstance(p, dict) for p in tail_payloads)
    assert [p["index"] for p in tail_payloads if isinstance(p, dict)] == [
        _CHECKPOINT_EVERY * k for k in range(1, 6)
    ]
    ends = search_timeline(tmp_path, _RUN_ID, kind="RUN_END")
    assert len(ends) == 1
    assert ends[0]["payload"] == {"exit_code": 0, "suspect": False, "attempt": 1}
