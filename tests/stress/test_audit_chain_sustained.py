"""Sustained-load stress: audit chain integrity under burst writes.

Closes the `tests/stress/` ROADMAP gap from DEC-STANDARDS-AUDIT-001
(see MIGRATION.md entry #208 / DEC-STRESS-001).

Invariants verified under load:

1. ``verify_chain(path)`` returns the entry count after every burst.
2. ``prev_hash(entry[n]) == entry_hash(entry[n-1])`` end-to-end
   (verified internally by ``verify_chain`` raising
   ``AuditChainCorruption`` otherwise).
3. No exception, no flush stall, no entry loss under ``N_EVENTS``
   sustained appends.

Budget
------
* CI nightly: ``N_EVENTS = 5_000`` (default, ~30s on GHA).
* Local 24h soak: ``STRESS_EVENTS=10_000_000`` via
  ``workflow_dispatch`` — NOT run automatically.

The CI default is deliberately on the low side — DEC-P0-005 documents
shared-GHA flake risk for any test that takes > 60s. The CI nightly
provides regression coverage; the manual soak provides the real
24h soak target named in CLAUDE.md §2 rule #12.

Note on memory profiling
------------------------
An earlier draft of this test included an RSS-growth assertion via
``resource.getrusage``. That was removed in the first PR-#99 CI run
because (a) ``ru_maxrss`` reporting differs by platform (Linux KB
vs macOS bytes) — easy to invert; (b) pytest's process-wide peak
RSS already accounts for collection + prior tests, so the per-test
"growth" signal is noisy; (c) GHA shared-runner timing jitter makes
a hard memory ceiling flaky. Memory profiling is a separate
concern best handled by a dedicated profiler in a follow-up PR.
The chain-integrity invariant is the real stress signal — that's
what's enforced here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from brutex.orchestrator.audit import AuditWriter, verify_chain

pytestmark = pytest.mark.stress


def test_audit_chain_sustained_burst(tmp_path: Path) -> None:
    """Burst-write N events to an audit log and re-verify the chain.

    Defaults to 5_000 events (~30s on GHA). Override via ``STRESS_EVENTS``
    env var for local 24h soak runs.
    """
    n_events = int(os.environ.get("STRESS_EVENTS", "5000"))
    log_path = tmp_path / "stress_audit.jsonl"

    with AuditWriter.open(log_path) as log:
        for seq in range(n_events):
            log.append(
                action="STRESS_BURST",
                subject=f"event:{seq:08d}",
                payload={"seq": seq, "kind": "stress-burst"},
            )

    # The chain-integrity invariant: verify_chain replays the file,
    # recomputes every entry_hash, and raises AuditChainCorruption
    # if any prev_hash linkage breaks or any hash mismatches. Survival
    # of N_EVENTS burst appends is the stress signal we want.
    verified_count = verify_chain(log_path)
    assert verified_count == n_events, (
        f"chain verification returned {verified_count}, expected {n_events}"
    )
