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

## Why this is a skeleton today

The Parquet writer is Phase 2 work. Until it lands, there's nothing
to roundtrip. The skeleton tests are pytest-skipped so CI reports them
as TODO without failing.

Decision: DEC-PHASE1-INTEGRATION-SKELETONS-001 (MIGRATION entry #251).
"""

from __future__ import annotations

import pytest


class TestAuditReplayRoundtrip:
    """``replay(audit_log) → Parquet`` byte-equals the original Parquet (P-20)."""

    def test_single_chunk_roundtrip(self) -> None:
        pytest.skip("Phase 2 implementation pending — needs Parquet writer")

    def test_multi_chunk_roundtrip(self) -> None:
        pytest.skip("Phase 2 implementation pending — needs Parquet writer")

    def test_resume_from_kill_roundtrip(self) -> None:
        pytest.skip("Phase 2 implementation pending — needs kill-recovery harness")


class TestAuditReplayConsistency:
    """L3 oracle: replay matches the live LMDB dedup set (P-20 + DEC-L3-REPLAY-ORACLE-001)."""

    def test_live_set_matches_replayed_set(self) -> None:
        pytest.skip(
            "Phase 2 implementation pending — the dedup-subject path is already in "
            "brutex.orchestrator.replay.assert_dedup_consistent (DEC-L3-REPLAY-ORACLE-001, "
            "MIGRATION entry #241); this integration test wraps a real ingest run."
        )
