"""Skeleton for the audit-log → Parquet replay integration test (Phase 2).

Per ``docs/TRACK1_VERIFICATION_PYRAMID.md`` rows G2 (Reproducibility)
+ G7 (Audit chain), this is the cross-layer correctness anchor:

> ``replay(audit_log) → Parquet`` byte-equals the original Parquet
> (invariant P-20).

## Scope (Phase 2 deliverable)

Once the Parquet writer + ingest pipeline land:

1. Run a tiny ingest job that writes Parquet output + appends every
   action to the hash-chained audit log.
2. Replay the audit log through ``brutex.orchestrator.replay``.
3. Assert byte-equal Parquet output from replay vs. the original.

The L3 replay oracle in ``brutex.orchestrator.replay::assert_dedup_consistent``
(DEC-L3-REPLAY-ORACLE-001, MIGRATION entry #241) already validates the
**dedup-subject** projection — this skeleton extends that to the full
**Parquet-output** projection.

## Why two of these are still skeletons today

The Parquet writer is Phase 2 work. Until it lands, there's nothing
to roundtrip for the Parquet-output projection, so
``test_single_chunk_roundtrip`` / ``test_multi_chunk_roundtrip`` stay
pytest-skipped (CI reports them as TODO without failing).

``test_resume_from_kill_roundtrip`` is NO LONGER skipped — the
kill-recovery harness it waited for now exists (DEC-AUDIT-TORN-TAIL-001):
the audit writer's ``open(resume=True)`` recovers from a torn final JSONL
line left by a ``SIGKILL`` mid-append, so an unattended restart resumes a
clean hash-valid chain instead of dying with ``AuditError``. This test
exercises that resume-after-kill harness end to end on the audit log
itself (the §11 AUTOMATED auto-resume guarantee). The full torn-tail
behaviour matrix — for BOTH the ingest-side
:mod:`brutex.orchestrator.audit` and the sweep-side
:mod:`brutex.engine.sweep_audit` chains — lives in
``tests/unit/test_audit_torn_tail.py``.

Decision: DEC-PHASE1-INTEGRATION-SKELETONS-001 (MIGRATION entry #251);
DEC-AUDIT-TORN-TAIL-001.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.orchestrator.audit import AuditWriter, verify_chain


class TestAuditReplayRoundtrip:
    """``replay(audit_log) → Parquet`` byte-equals the original Parquet (P-20)."""

    def test_single_chunk_roundtrip(self) -> None:
        pytest.skip("Phase 2 implementation pending — needs Parquet writer")

    def test_multi_chunk_roundtrip(self) -> None:
        pytest.skip("Phase 2 implementation pending — needs Parquet writer")

    def test_resume_from_kill_roundtrip(self, tmp_path: Path) -> None:
        """A SIGKILL mid-append leaves a torn tail; resume recovers + continues.

        The kill-recovery harness (DEC-AUDIT-TORN-TAIL-001): write a clean
        chain, simulate a crash mid-append by appending raw partial bytes with
        NO trailing newline (exactly what an interrupted ``write()`` leaves),
        then ``open(resume=True)`` — which previously raised ``AuditError`` and
        aborted the unattended restart. After the fix it must skip the torn
        tail, recover the last complete hash-valid entry, continue the chain,
        and leave a fully verifiable log.
        """
        path = tmp_path / "audit" / "run-kill.jsonl"

        # 1. A clean committed prefix.
        with AuditWriter.open(path) as log:
            log.append(action="RUN_START", subject="run:kill", payload={"n": 0})
            log.append(action="INGEST_CHUNK_DONE", subject="NSE-NIFTY", payload={"bars": 375})

        # 2. Simulate the crash: a partial, newline-LESS final line, just as a
        #    SIGKILL between the write() and the durable flush of the next
        #    append would leave on disk.
        with path.open("a", encoding="utf-8") as raw:
            raw.write('{"seq": 2, "prev_hash": "deadbeef", "ts_utc": 1.0, "actor": "bru')

        # 3. Unattended restart. Pre-fix this raised AuditError; post-fix it
        #    recovers from the last complete entry.
        with AuditWriter.open(path, resume=True) as log:
            recovered = log.append(
                action="RUN_COMPLETE", subject="run:kill", payload={"total": 375}
            )

        # 4. The recovered entry continues the chain at seq 2 (the torn line's
        #    seq was uncommitted and is replaced).
        assert recovered.seq == 2

        # 5. The whole log verifies end to end — the torn tail is gone and the
        #    chain is intact across the kill boundary.
        assert verify_chain(path) == 3


class TestAuditReplayConsistency:
    """L3 oracle: replay matches the live LMDB dedup set (P-20 + DEC-L3-REPLAY-ORACLE-001)."""

    def test_live_set_matches_replayed_set(self) -> None:
        pytest.skip(
            "Phase 2 implementation pending — the dedup-subject path is already in "
            "brutex.orchestrator.replay.assert_dedup_consistent (DEC-L3-REPLAY-ORACLE-001, "
            "MIGRATION entry #241); this integration test wraps a real ingest run."
        )
