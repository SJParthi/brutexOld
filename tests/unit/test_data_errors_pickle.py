"""Pickle round-trip suite for :mod:`brutex.data.errors` (DEC-GREEKS-PARALLEL-001).

A lake exception raised INSIDE a spawn ``ProcessPoolExecutor`` worker crosses
the process boundary via pickle. The default ``BaseException`` reduce re-feeds
``args`` — the already-FORMATTED message — to ``__init__``, which broke every
constructor that formats its arguments:

* ``PartitionLockTimeout(5.0)`` / ``WriteDeadlineExceeded(90.0)`` —
  ``pickle.loads`` RAISED ``ValueError: Unknown format code 'g' for object of
  type 'str'`` (the ``:g`` spec re-applied to the message string). An
  unpicklable exception escaping a pool worker poisons the WHOLE pool into a
  generic ``BrokenProcessPool`` (the real error surviving only as a traceback
  string), so this was a whole-pool-kill class of bug.
* ``LakeWriteHalt("disk_full")`` — round-tripped but with a DOUBLED message
  (``"lake write halted: lake write halted: disk_full"``).

Both empirically proven pre-fix; the ``__reduce__`` constructor-arg fixes are
pinned here per class (type / ``str`` / ``args`` / attribute fidelity). This
file is also the module's discovery-gate test: it exercises every class +
:func:`~brutex.data.errors.parquet_read_skip_errors` so
``brutex/data/errors.py`` reads 100% line+branch standalone.
"""

from __future__ import annotations

import pickle

import pytest
from brutex.data.errors import (
    CatalogError,
    DataLakeError,
    GreeksEnrichPoolBroken,
    LakeWriteHalt,
    MarketHoursHalt,
    PartitionError,
    PartitionLockTimeout,
    ResampleInputError,
    StoreWriteDeadlineExceeded,
    WriteDeadlineExceeded,
    WriterError,
    parquet_read_skip_errors,
)
from brutex.models import BruteXError


def _roundtrip(exc: BaseException) -> BaseException:
    return pickle.loads(pickle.dumps(exc))  # noqa: S301 — self-produced bytes


class TestConstructorArgReduce:
    """The four formatted-constructor classes round-trip EXACTLY."""

    def test_lake_write_halt_message_not_doubled(self) -> None:
        """Pre-fix regression: the default reduce re-fed the formatted message
        to ``__init__`` and the copy read ``lake write halted: lake write
        halted: disk_full``. Post-fix: exactly one prefix, ``reason`` intact."""
        exc = LakeWriteHalt("disk_full")
        copy = _roundtrip(exc)
        assert type(copy) is LakeWriteHalt
        assert str(copy) == "lake write halted: disk_full"
        assert str(copy) == str(exc)
        assert copy.args == exc.args
        assert isinstance(copy, LakeWriteHalt)
        assert copy.reason == "disk_full"

    def test_partition_lock_timeout_roundtrips(self) -> None:
        """Pre-fix regression: ``pickle.loads`` raised ``ValueError: Unknown
        format code 'g' for object of type 'str'`` (empirically proven) —
        the default reduce re-called the ctor with the formatted string."""
        exc = PartitionLockTimeout(5.0)
        copy = _roundtrip(exc)
        assert type(copy) is PartitionLockTimeout
        assert str(copy) == str(exc)
        assert copy.args == exc.args
        assert isinstance(copy, PartitionLockTimeout)
        assert copy.timeout_seconds == 5.0

    def test_write_deadline_exceeded_roundtrips(self) -> None:
        """Same latent ``:g``-re-format bug as ``PartitionLockTimeout`` —
        adapter-only today (not on the Greeks worker path), fixed as cheap
        hardening while in this file."""
        exc = WriteDeadlineExceeded(90.0)
        copy = _roundtrip(exc)
        assert type(copy) is WriteDeadlineExceeded
        assert str(copy) == str(exc)
        assert isinstance(copy, WriteDeadlineExceeded)
        assert copy.deadline_seconds == 90.0

    def test_greeks_enrich_pool_broken_roundtrips(self) -> None:
        """The NEW class carries the same discipline from birth — it can never
        re-introduce the doubled-message bug (it is parent-side by design and
        should never actually cross a pool boundary; consistency, honestly)."""
        exc = GreeksEnrichPoolBroken("a worker process died before completing its unit")
        copy = _roundtrip(exc)
        assert type(copy) is GreeksEnrichPoolBroken
        assert str(copy) == str(exc)
        assert str(copy).count("greeks enrichment worker pool broken:") == 1
        assert isinstance(copy, GreeksEnrichPoolBroken)
        assert copy.detail == "a worker process died before completing its unit"

    def test_lake_write_halt_worker_pool_broken(self) -> None:
        """The DEC-HARDWARE-ENVELOPE-001 RESERVED token round-trips like any
        other ``reason`` — pinned so a future process-pool stage adopting it
        inherits the clean round-trip."""
        copy = _roundtrip(LakeWriteHalt("worker_pool_broken"))
        assert isinstance(copy, LakeWriteHalt)
        assert copy.reason == "worker_pool_broken"

    def test_store_write_deadline_exceeded_roundtrips(self) -> None:
        """DEC-STORE-WRITE-DEADLINE-001: the registry/coverage store-write twin —
        the SAME ``:g``-re-format latent bug class, so all four positional ctor
        args (incl. the ``latched`` fail-fast flag) must round-trip exactly."""
        exc = StoreWriteDeadlineExceeded(90.0, "registry", "upsert_contracts")
        copy = _roundtrip(exc)
        assert type(copy) is StoreWriteDeadlineExceeded
        assert str(copy) == str(exc)
        assert copy.args == exc.args
        assert copy.deadline_seconds == 90.0
        assert copy.store == "registry"
        assert copy.op == "upsert_contracts"
        assert copy.latched is False

    def test_store_write_deadline_latched_roundtrips(self) -> None:
        exc = StoreWriteDeadlineExceeded(0.2, "coverage", "upsert_coverage", latched=True)
        copy = _roundtrip(exc)
        assert type(copy) is StoreWriteDeadlineExceeded
        assert copy.latched is True
        assert str(copy).endswith("[latched fail-fast]")
        assert str(copy) == str(exc)

    def test_store_write_deadline_is_transient_never_a_halt(self) -> None:
        """The catch-site contract: a DataLakeError, deliberately NOT LakeWriteHalt."""
        exc = StoreWriteDeadlineExceeded(90.0, "registry", "op")
        assert isinstance(exc, DataLakeError)
        assert not isinstance(exc, LakeWriteHalt)

    def test_double_roundtrip_is_stable(self) -> None:
        """A second hop (paranoia: nested pools / re-pickled results) is a fixpoint."""
        exc = LakeWriteHalt("out_of_memory")
        twice = _roundtrip(_roundtrip(exc))
        assert isinstance(twice, LakeWriteHalt)
        assert str(twice) == "lake write halted: out_of_memory"
        assert twice.reason == "out_of_memory"


class TestMarketHoursHalt:
    """DEC-GROWW-MARKET-HOURS-GUARD-001 — the pull-control market-hours stop."""

    def test_default_reason_and_message(self) -> None:
        exc = MarketHoursHalt()
        assert exc.reason == "market_hours"
        assert str(exc) == "pull halted: market_hours"

    def test_explicit_reason(self) -> None:
        exc = MarketHoursHalt("market_hours")
        assert exc.reason == "market_hours"

    def test_roundtrip_not_doubled(self) -> None:
        """Same reduce discipline as LakeWriteHalt — exactly one prefix."""
        copy = _roundtrip(MarketHoursHalt())
        assert type(copy) is MarketHoursHalt
        assert str(copy) == "pull halted: market_hours"
        assert copy.reason == "market_hours"
        assert copy.args == MarketHoursHalt().args

    def test_double_roundtrip_stable(self) -> None:
        twice = _roundtrip(_roundtrip(MarketHoursHalt()))
        assert isinstance(twice, MarketHoursHalt)
        assert str(twice) == "pull halted: market_hours"

    def test_is_a_brutex_error_not_a_data_lake_error(self) -> None:
        """A pull-CONTROL stop — a BruteXError (for global-stop membership) but
        deliberately NOT a DataLakeError (it is not a lake-write failure)."""
        exc = MarketHoursHalt()
        assert isinstance(exc, BruteXError)
        assert not isinstance(exc, DataLakeError)

    def test_not_a_parquet_skip_error(self) -> None:
        """A market-hours halt must never be swallowed as an unreadable-file skip."""
        assert not isinstance(MarketHoursHalt(), parquet_read_skip_errors())


class TestMessageOnlyClasses:
    """The plain message-only classes keep the default (working) reduce."""

    @pytest.mark.parametrize(
        "cls",
        [DataLakeError, PartitionError, WriterError, ResampleInputError, CatalogError],
    )
    def test_roundtrip(self, cls: type[DataLakeError]) -> None:
        exc = cls("boom: details")
        copy = _roundtrip(exc)
        assert type(copy) is cls
        assert str(copy) == "boom: details"
        assert copy.args == exc.args

    def test_safe_message_survives(self) -> None:
        """``BruteXError.safe_message`` rides the pickled ``__dict__`` state."""
        exc = WriterError("raw with secret", safe_message="redacted")
        copy = _roundtrip(exc)
        assert isinstance(copy, WriterError)
        assert str(copy) == "redacted"


class TestParquetReadSkipErrors:
    """The one data-layer unreadable-file predicate — narrow on purpose."""

    def test_members(self) -> None:
        import polars as pl

        skip = parquet_read_skip_errors()
        assert skip == (pl.exceptions.PolarsError, OSError)

    def test_pool_broken_is_not_a_skip_error(self) -> None:
        """A dead worker pool is a HOST halt, never a quarantine-this-file
        signal — the movers/db resilient-skip paths must not swallow it."""
        skip = parquet_read_skip_errors()
        assert not isinstance(GreeksEnrichPoolBroken("x"), skip)

    def test_lake_write_halt_is_not_a_skip_error(self) -> None:
        skip = parquet_read_skip_errors()
        assert not isinstance(LakeWriteHalt("disk_full"), skip)

    def test_narrow_a_value_error_is_not_in_the_set(self) -> None:
        # NARROW on purpose (Golden Rule #1): a genuine code bug still propagates.
        skip = parquet_read_skip_errors()
        assert not issubclass(ValueError, skip)
