"""Chaos tests for the hash-chained audit log (DEC-CHAOS-RACE-001).

Distinct from ``tests/stress/test_audit_chain_sustained.py`` (ONE long ordered
run) and ``tests/integration/test_dhan_options_audit_chain.py`` (ingest wiring):
these inject **randomized restart boundaries** and **concurrent verify reads** to
prove the chain's two hard invariants survive chaotic schedules —

1. crash/restart resume (``AuditWriter.open(resume=True)``) NEVER breaks chain
   continuity, no matter where the "crash" falls between appends; and
2. a reader verifying a partially-written log NEVER sees a CORRUPT chain
   (:class:`AuditChainCorruption`) — at worst it sees an incomplete tail
   (:class:`AuditError`), never a torn committed prefix.

``CHAOS_ROUNDS`` / ``CHAOS_WRITERS`` scale the run (small default for the PR gate;
a soak sets them large). Marked ``chaos`` so the default unit suite stays fast.
"""

from __future__ import annotations

import os
import random
import threading
from pathlib import Path

import pytest
from brutex.orchestrator.audit import (
    AuditChainCorruption,
    AuditError,
    AuditWriter,
    verify_chain,
)

pytestmark = pytest.mark.chaos

#: Rounds of crash/restart (overridable for a soak via the env).
_ROUNDS = int(os.environ.get("CHAOS_ROUNDS", "40"))
#: Concurrent reader threads in the verify-during-write test.
_READERS = int(os.environ.get("CHAOS_WRITERS", "8"))


def _append_batch(writer: AuditWriter, rng: random.Random, count: int) -> int:
    """Append *count* randomized entries; return how many were written."""
    actions = ("RUN_START", "PHASE_START", "INGEST_CHUNK_DONE", "QUARANTINE_NEW", "RUN_COMPLETE")
    for _ in range(count):
        writer.append(
            action=rng.choice(actions),
            subject=f"obj:{rng.randrange(10_000)}",
            payload={"n": rng.randrange(1_000), "tag": rng.choice(["a", "b", "c"])},
        )
    return count


@pytest.mark.parametrize("seed", [1, 7, 42, 1234])
def test_chain_survives_randomized_crash_restart_cycles(tmp_path: Path, seed: int) -> None:
    """Across many randomized open(resume)→append→"crash" cycles the chain stays
    intact: ``verify_chain`` passes after EVERY restart and the total is exact.

    The chaos is the *boundary*: each round appends a random 0..K entries then drops
    the writer (simulating a crash), so the resume path is exercised at every
    possible offset — including the zero-append round (resume an unchanged tail).
    """
    rng = random.Random(seed)
    path = tmp_path / "chaos-audit.jsonl"
    total = 0
    for round_no in range(_ROUNDS):
        writer = AuditWriter.open(path, resume=round_no > 0)
        try:
            total += _append_batch(writer, rng, rng.randrange(0, 6))
        finally:
            writer.close()
        # After every "crash" the on-disk chain must verify, and its length must
        # equal everything written so far — no lost/duplicated/renumbered entry.
        assert verify_chain(path) == total

    # Final whole-file replay (the audit-log oracle) + contiguous seq 0..total-1.
    assert verify_chain(path) == total
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == total


def test_concurrent_verify_during_writes_never_sees_corruption(tmp_path: Path) -> None:
    """One writer appends while N readers spin on ``verify_chain``.

    The invariant under the race: a reader may catch an INCOMPLETE tail (a partial
    final line → :class:`AuditError`), but it must NEVER observe a CORRUPT chain
    (:class:`AuditChainCorruption`) — the committed prefix is always consistent
    because each ``append`` writes one whole ``line + "\\n"`` then flushes. We also
    assert at least one verify SUCCEEDED (the readers really did observe the file).
    """
    path = tmp_path / "concurrent-audit.jsonl"
    n_writes = 300
    writer = AuditWriter.open(path)
    done = threading.Event()
    corruption: list[AuditChainCorruption] = []
    successes = [0]
    lock = threading.Lock()

    def reader() -> None:
        local_ok = 0
        while not done.is_set():
            try:
                verify_chain(path)
                local_ok += 1
            except AuditChainCorruption as exc:  # the invariant violation
                corruption.append(exc)
                return
            except AuditError:
                # Incomplete tail mid-write (or not-yet-created) — tolerated.
                continue
        with lock:
            successes[0] += local_ok

    threads = [threading.Thread(target=reader) for _ in range(_READERS)]
    for t in threads:
        t.start()
    try:
        rng = random.Random(99)
        for _ in range(n_writes):
            writer.append(
                action="INGEST_CHUNK_DONE", subject=f"k:{rng.randrange(9999)}", payload={}
            )
    finally:
        done.set()
        for t in threads:
            t.join()
        writer.close()

    assert not corruption, f"verify_chain saw a CORRUPT chain mid-write: {corruption[:1]}"
    assert successes[0] > 0, "no reader ever completed a verify — the race was not exercised"
    # The fully-written chain still verifies to the exact count.
    assert verify_chain(path) == n_writes
