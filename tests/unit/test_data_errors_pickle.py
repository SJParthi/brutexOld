"""Pickle round-trip regression for the lake exceptions (DEC-HARDWARE-ENVELOPE-001, M2).

A worker in the (historic ProcessPool design of the) parallel prebuild pickled a
raised exception back to the parent; two custom-``__init__`` lake exceptions
FAILED to unpickle — ``PartitionLockTimeout`` / ``WriteDeadlineExceeded`` fed the
already-formatted message STRING back through ``f"{x:g}"`` and raised
``ValueError: Unknown format code 'g'``, and ``LakeWriteHalt`` double-prefixed its
``reason``. Even though the shipped prebuild uses a THREAD pool (no pickling),
these are real correctness bugs — any code that pickles a lake error (a process
pool, a multiprocessing queue, a cross-process audit) would hit them. This test
pins a clean round-trip for every ``DataLakeError`` a worker can raise.
"""

from __future__ import annotations

import pickle

import pytest
from brutex.data.errors import (
    CatalogError,
    DataLakeError,
    LakeWriteHalt,
    PartitionError,
    PartitionLockTimeout,
    ResampleInputError,
    WriteDeadlineExceeded,
    WriterError,
)


def _round_trip(exc: DataLakeError) -> DataLakeError:
    """Pickle then unpickle *exc* (trusted, in-test data — never untrusted)."""
    return pickle.loads(pickle.dumps(exc))  # noqa: S301 — trusted in-test round-trip


class TestCustomInitRoundTrip:
    def test_partition_lock_timeout(self) -> None:
        r = _round_trip(PartitionLockTimeout(1.5))
        assert isinstance(r, PartitionLockTimeout)
        assert r.timeout_seconds == 1.5
        assert "1.5s" in str(r)

    def test_write_deadline_exceeded(self) -> None:
        r = _round_trip(WriteDeadlineExceeded(90.0))
        assert isinstance(r, WriteDeadlineExceeded)
        assert r.deadline_seconds == 90.0
        assert "90s" in str(r)

    def test_lake_write_halt_reason_not_double_prefixed(self) -> None:
        r = _round_trip(LakeWriteHalt("disk_full"))
        assert isinstance(r, LakeWriteHalt)
        assert r.reason == "disk_full"  # NOT "lake write halted: disk_full"
        assert str(r) == "lake write halted: disk_full"

    def test_lake_write_halt_worker_pool_broken(self) -> None:
        r = _round_trip(LakeWriteHalt("worker_pool_broken"))
        assert isinstance(r, LakeWriteHalt)
        assert r.reason == "worker_pool_broken"


class TestDefaultInitRoundTrip:
    """The no-custom-``__init__`` lake errors round-trip via the default reduce."""

    @pytest.mark.parametrize(
        "exc",
        [
            DataLakeError("x"),
            PartitionError("x"),
            WriterError("x"),
            ResampleInputError("x"),
            CatalogError("x"),
        ],
    )
    def test_round_trips(self, exc: DataLakeError) -> None:
        r = _round_trip(exc)
        assert type(r) is type(exc)
        assert str(r) == str(exc)
