"""Tests for ``brutex.adapters.groww.write_deadline`` — the durable-write FREEZE GUARD.

DEC-FETCH-HUNG-WRITE-DEADLINE-001. 100% line+branch of :func:`append_with_deadline`
+ :func:`resolve_write_deadline_seconds`:

* a normal / merely-SLOW write that completes within the deadline returns the sink's
  result byte-identically (we did NOT break the normal path);
* a write that BLOCKS past the deadline raises the TRANSIENT
  :class:`~brutex.data.errors.WriteDeadlineExceeded` (the hung-write guard fires) WITHOUT
  the caller having to wait for the wedged write to finish (the fetch-lane-is-freed proof);
* a write that raises its OWN error (PartitionLockTimeout / LakeWriteHalt / a generic
  WriterError) propagates UNCHANGED — only a TIMEOUT is reclassified;
* every env-resolution branch (unset / blank / good / non-numeric / non-finite /
  non-positive) of :func:`resolve_write_deadline_seconds`.

Driven by deterministic FAKE sinks (a blocking ``threading.Event`` that never sets, a
slow-but-OK sleep, a raising sink) + small deadlines so the real timeout path runs fast.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.groww.constants import GROWW_WRITE_DEADLINE_S
from brutex.adapters.groww.write_deadline import (
    WRITE_DEADLINE_ENV,
    append_with_deadline,
    resolve_write_deadline_seconds,
)
from brutex.data.errors import (
    LakeWriteHalt,
    PartitionLockTimeout,
    WriteDeadlineExceeded,
    WriterError,
)
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult


def _result(n: int) -> SinkAppendResult:
    return SinkAppendResult(bars_appended=n, partitions_written=1, write_results=())


class _OkSink:
    """A sink whose ``append`` returns immediately (the normal path)."""

    def __init__(self) -> None:
        self.calls = 0
        self.kwargs: dict[str, object] = {}

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        self.calls += 1
        self.kwargs = {
            "exchange": exchange,
            "segment": segment,
            "symbol": symbol,
            "interval": interval,
            "bars": list(bars),
            "merge_mode": merge_mode,
        }
        return _result(len(bars))


class _SlowOkSink:
    """A sink that SLEEPS a little (< deadline) then returns — the slow-but-OK path."""

    def __init__(self, sleep_s: float) -> None:
        self._sleep_s = sleep_s
        self.calls = 0

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, merge_mode
        time.sleep(self._sleep_s)
        self.calls += 1
        return _result(len(bars))


class _HungSink:
    """A sink whose ``append`` BLOCKS forever on an Event that never sets (a wedged fsync).

    The test releases the gate AT TEARDOWN so the abandoned daemon worker can exit cleanly
    (no orphan thread leak); the guard fires LONG before that.
    """

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, bars, merge_mode
        self.entered.set()
        # Block until the test releases the gate (or a generous safety timeout so a test
        # crash can never leave a truly-forever daemon).
        self.gate.wait(timeout=30.0)
        return _result(0)


class _RaisingSink:
    """A sink whose ``append`` raises a caller-supplied error (NOT a timeout)."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, bars, merge_mode
        raise self._error


def _call(sink: object, *, deadline_seconds: float | None) -> SinkAppendResult:
    bar = CandleBar(
        timestamp=datetime(2021, 1, 4, 4, 5, tzinfo=UTC),
        open=Decimal("1.00"),
        high=Decimal("2.00"),
        low=Decimal("0.00"),
        close=Decimal("1.00"),
        volume=1,
        open_interest=None,
    )
    return append_with_deadline(
        sink,  # type: ignore[arg-type]
        exchange="NSE",
        segment="CASH",
        symbol="NIFTY",
        interval="1minute",
        bars=[bar],
        deadline_seconds=deadline_seconds,
    )


class TestResolveWriteDeadlineSeconds:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(WRITE_DEADLINE_ENV, raising=False)
        assert resolve_write_deadline_seconds() == GROWW_WRITE_DEADLINE_S

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WRITE_DEADLINE_ENV, "12.5")
        assert resolve_write_deadline_seconds() == 12.5

    def test_explicit_raw_overrides_env(self) -> None:
        assert resolve_write_deadline_seconds("3.0") == 3.0

    def test_blank_falls_back(self) -> None:
        assert resolve_write_deadline_seconds("   ") == GROWW_WRITE_DEADLINE_S

    def test_non_numeric_falls_back(self) -> None:
        assert resolve_write_deadline_seconds("not-a-number") == GROWW_WRITE_DEADLINE_S

    def test_non_finite_inf_falls_back(self) -> None:
        # An inf deadline would NEVER expire — must NOT disable the bound.
        assert resolve_write_deadline_seconds("inf") == GROWW_WRITE_DEADLINE_S

    def test_non_finite_nan_falls_back(self) -> None:
        assert resolve_write_deadline_seconds("nan") == GROWW_WRITE_DEADLINE_S

    def test_zero_falls_back(self) -> None:
        assert resolve_write_deadline_seconds("0") == GROWW_WRITE_DEADLINE_S

    def test_negative_falls_back(self) -> None:
        assert resolve_write_deadline_seconds("-5") == GROWW_WRITE_DEADLINE_S


class TestAppendWithinDeadline:
    def test_normal_write_returns_result_and_forwards_kwargs(self) -> None:
        sink = _OkSink()
        result = _call(sink, deadline_seconds=5.0)
        assert result.bars_appended == 1
        assert sink.calls == 1
        # The append got the exact partition coordinates + bars.
        assert sink.kwargs["exchange"] == "NSE"
        assert sink.kwargs["segment"] == "CASH"
        assert sink.kwargs["symbol"] == "NIFTY"
        assert sink.kwargs["interval"] == "1minute"
        assert len(sink.kwargs["bars"]) == 1  # type: ignore[arg-type]

    def test_default_deadline_path_used_when_none(self) -> None:
        # deadline_seconds=None exercises the resolve_write_deadline_seconds() branch; a fast
        # OK sink completes well within the 90 s audited default.
        sink = _OkSink()
        result = _call(sink, deadline_seconds=None)
        assert result.bars_appended == 1
        assert sink.calls == 1

    def test_slow_but_ok_write_completes(self) -> None:
        # A write that takes < deadline still completes + records SUCCESS (the normal/slow path
        # is NOT broken).
        sink = _SlowOkSink(sleep_s=0.1)
        result = _call(sink, deadline_seconds=2.0)
        assert result.bars_appended == 1
        assert sink.calls == 1


class TestHungWriteTrips:
    def test_hung_write_raises_write_deadline_exceeded_fast(self) -> None:
        """A write that BLOCKS past the deadline raises WriteDeadlineExceeded WITHOUT waiting.

        The hung sink blocks on an Event that the test never sets until teardown. The guard
        must raise within ~the deadline (NOT after the 30 s safety) — proving the CALLER (the
        fetch-pipeline consumer) is freed while the wedged write is abandoned.
        """
        sink = _HungSink()
        deadline = 0.2
        started = time.monotonic()
        try:
            with pytest.raises(WriteDeadlineExceeded) as excinfo:
                _call(sink, deadline_seconds=deadline)
            elapsed = time.monotonic() - started
            # The guard fired in bounded time — far below the 30 s hung-write safety, proving the
            # caller did NOT wait for the wedged write to finish.
            assert elapsed < 5.0, f"guard took {elapsed:.2f}s — should be ~{deadline}s"
            # The transient carries the deadline that was exhausted, for the log.
            assert excinfo.value.deadline_seconds == deadline
            assert "deferred to the next pull" in str(excinfo.value)
            # The wedged write DID start on the worker (the hang is real, not a no-op).
            assert sink.entered.is_set()
        finally:
            # Release the abandoned daemon worker so it exits cleanly (no orphan thread).
            sink.gate.set()


class TestUnderlyingErrorsPropagate:
    def test_partition_lock_timeout_propagates_unchanged(self) -> None:
        # A real sink-raised PartitionLockTimeout is NOT reclassified — only a TIMEOUT becomes
        # WriteDeadlineExceeded. The spine catches both in the SAME defer block.
        sink = _RaisingSink(PartitionLockTimeout(0.5))
        with pytest.raises(PartitionLockTimeout):
            _call(sink, deadline_seconds=5.0)

    def test_lake_write_halt_propagates_unchanged(self) -> None:
        # A disk-full LakeWriteHalt must reach the spine's _GLOBAL_STOP_ERRORS path — it is a
        # pull-wide stop, NEVER demoted to a write-deadline defer.
        sink = _RaisingSink(LakeWriteHalt("disk_full"))
        with pytest.raises(LakeWriteHalt):
            _call(sink, deadline_seconds=5.0)

    def test_generic_writer_error_propagates_unchanged(self) -> None:
        sink = _RaisingSink(WriterError("boom"))
        with pytest.raises(WriterError):
            _call(sink, deadline_seconds=5.0)
