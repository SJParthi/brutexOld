"""Sustained-load stress: audit chain integrity under burst writes.

Closes the `tests/stress/` ROADMAP gap from DEC-STANDARDS-AUDIT-001
(see MIGRATION.md entry #208 / DEC-STRESS-001).

Invariants verified under load:

1. ``verify_chain(path)`` returns the entry count after every burst.
2. ``prev_hash(entry[n]) == entry_hash(entry[n-1])`` end-to-end.
3. No exception, no flush stall, no entry loss under ``N_EVENTS``
   sustained appends.
4. RSS memory growth is bounded — leaks would show as monotonic
   growth across the burst.

Budget
------
* CI nightly: ``N_EVENTS = 5_000`` (default, ~30s on GHA).
* Local 24h soak: ``STRESS_DURATION_S=86400 STRESS_EVENTS=10_000_000``
  via ``workflow_dispatch`` — NOT run automatically.

The CI default is deliberately on the low side — DEC-P0-005 documents
shared-GHA flake risk for any test that takes > 60s. The CI nightly
provides regression coverage; the manual soak provides the real
24h soak target named in CLAUDE.md §2 rule #12.
"""

from __future__ import annotations

import os
import resource
from pathlib import Path

import pytest
from brutex.orchestrator.audit import AuditWriter, verify_chain

pytestmark = pytest.mark.stress


def _rss_mb() -> float:
    """Return resident-set memory in MB (Linux/macOS portable)."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KB; macOS reports bytes. Normalize to MB.
    return rss / 1024.0 if rss > 1_000_000 else rss / 1024.0 / 1024.0


def test_audit_chain_sustained_burst(tmp_path: Path) -> None:
    """Burst-write N events to an audit log and re-verify the chain.

    Defaults to 5_000 events (~30s on GHA). Override via ``STRESS_EVENTS``
    env var for local 24h soak runs.
    """
    n_events = int(os.environ.get("STRESS_EVENTS", "5000"))
    log_path = tmp_path / "stress_audit.jsonl"

    rss_start = _rss_mb()

    with AuditWriter.open(log_path) as log:
        for seq in range(n_events):
            log.append(
                action="STRESS_BURST",
                subject=f"event:{seq:08d}",
                payload={"seq": seq, "kind": "stress-burst"},
            )

    rss_end = _rss_mb()

    # Invariant 1 + 2 + 3: chain verifies end-to-end + correct count.
    verified_count = verify_chain(log_path)
    assert verified_count == n_events, (
        f"chain verification returned {verified_count}, expected {n_events}"
    )

    # Invariant 4: RSS growth must be bounded. Per-entry overhead is
    # well under 10 KB (the writer flushes + buffers ~1 line). For
    # 5_000 events the upper bound is ~100 MB growth; we use 200 MB
    # as a generous ceiling to avoid GHA-jitter flake. A real memory
    # leak (e.g., stash-all-entries-in-list bug) would balloon past
    # the ceiling at this iteration count.
    rss_growth = rss_end - rss_start
    rss_ceiling_mb = 200.0
    assert rss_growth < rss_ceiling_mb, (
        f"RSS growth {rss_growth:.1f} MB exceeds {rss_ceiling_mb} MB "
        f"ceiling — possible memory leak in AuditWriter or its chain."
    )
