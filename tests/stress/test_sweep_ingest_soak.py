"""Sustained-load soak: the brute-force sweep engine + the ingest lake.

Closes the nightly-soak gap noted in ``docs/STANDARDS_100PCT.md`` §1
row 11 / §5: the nightly stress workflow exercised the audit chain +
the scheduler, but NOT the two hottest production paths — the Stage-2
``count_supermasks`` sweep kernel and the audit→Parquet ingest lake.
DEC-SWEEP-INGEST-SOAK-001 (``docs/MIGRATION.md`` entry #581) wires ONE
sub-24h nightly soak that runs BOTH automatically each night (not only
via ``workflow_dispatch``).

Invariants verified under load
------------------------------
**Sweep engine** (``brutex.engine.combo_mask.count_supermasks``):

1. The Rust kernel agrees bit-for-bit with the numpy reference oracle
   on every iteration (``count_supermasks == count_supermasks_reference``).
2. The result is byte-stable across ALL iterations — no drift, no
   per-iteration nondeterminism (CLAUDE.md Golden Rule #7 idempotence).

**Ingest lake** (``brutex.data.audit_replay``):

3. Every audited Parquet write extends the hash chain; ``verify_chain``
   survives the full burst.
4. Replaying the audit log alone rebuilds the lake byte-for-byte
   (invariant P-20) — the reproducibility guarantee, sustained.

Budget
------
* CI nightly (scheduled cron): bounded by ``STRESS_SWEEP_ITERS`` /
  ``STRESS_INGEST_BARS`` (defaults below) — sized to finish in a few
  minutes, well under 24h, so it can run automatically every night.
* Local / dispatch 24h soak: scale both env vars up via
  ``workflow_dispatch`` — NOT run automatically (it would burn GHA
  hours), matching the honest-envelope pattern in
  ``test_audit_chain_sustained.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.audit_replay import assert_replay_byte_equals_original, write_and_audit
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import partition_for_timestamp
from brutex.engine.combo_mask import count_supermasks, count_supermasks_reference
from brutex.orchestrator.audit import AuditWriter, verify_chain

pytestmark = pytest.mark.stress

_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}

#: Minute timestamps start here; bars step 1 minute so a soak fans out
#: across month partitions deterministically.
_BASE_TS = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def _synthetic_bar_bits(n_bars: int) -> list[int]:
    """Deterministic per-bar packed u64 condition bitsets."""
    # A simple LCG-free deterministic spread across the low 24 bits so
    # masks have a realistic mix of hits/misses.
    return [((i * 2654435761) & 0xFFFFFF) for i in range(n_bars)]


def _synthetic_masks(n_masks: int) -> list[int]:
    """Deterministic strategy masks (a few set bits each)."""
    return [(1 << (j % 24)) | (1 << ((j * 7 + 3) % 24)) for j in range(n_masks)]


def test_sweep_kernel_soak_is_parity_exact_and_stable() -> None:
    """Soak ``count_supermasks`` and assert kernel⇄reference parity + stability."""
    iters = int(os.environ.get("STRESS_SWEEP_ITERS", "2000"))
    n_bars = int(os.environ.get("STRESS_SWEEP_BARS", "512"))
    n_masks = int(os.environ.get("STRESS_SWEEP_MASKS", "64"))

    bar_bits = _synthetic_bar_bits(n_bars)
    masks = _synthetic_masks(n_masks)

    # Reference oracle computed ONCE — the kernel must reproduce it every
    # iteration with zero drift.
    expected = count_supermasks_reference(bar_bits, masks)

    for _ in range(iters):
        got = count_supermasks(bar_bits, masks)
        assert got == expected  # parity + per-iteration determinism


def _make_bars(n_bars: int) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for i in range(n_bars):
        px = Decimal(22000 + (i % 500))
        bars.append(
            CandleBar(
                timestamp=_BASE_TS + timedelta(minutes=i),
                open=px,
                high=px + Decimal(10),
                low=px - Decimal(5),
                close=px + Decimal(3),
                volume=1000 + (i % 97),
                open_interest=None,
            )
        )
    return bars


def test_ingest_lake_soak_replays_byte_equal(tmp_path: Path) -> None:
    """Soak the audit→Parquet lake: write many bars, then prove P-20 holds."""
    n_bars = int(os.environ.get("STRESS_INGEST_BARS", "5000"))

    audit_path = tmp_path / "audit" / "soak.jsonl"
    original = tmp_path / "lake_original"
    replay = tmp_path / "lake_replay"

    # Group the bar stream by (year, month) — ParquetWriter writes one
    # partition per call — and audit each partition write.
    bars = _make_bars(n_bars)
    groups: dict[tuple[int, int], list[CandleBar]] = {}
    for bar in bars:
        groups.setdefault(partition_for_timestamp(bar.timestamp), []).append(bar)

    writer = ParquetWriter(lake_root=original)
    with AuditWriter.open(audit_path) as audit:
        for _key, group in sorted(groups.items()):
            write_and_audit(writer=writer, audit=audit, bars=group, **_COORDS)

    # 1. The chain survived the burst.
    assert verify_chain(audit_path) == len(groups)
    # 2. Replay from the audit log alone rebuilds the lake byte-for-byte.
    replayed = assert_replay_byte_equals_original(
        audit_path=audit_path,
        original_lake_root=original,
        replay_lake_root=replay,
    )
    assert len(replayed) == len(groups)
    assert all(rw.hash_matches for rw in replayed)
