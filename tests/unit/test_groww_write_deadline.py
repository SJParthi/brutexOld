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
  non-positive) of :func:`resolve_write_deadline_seconds`;
* the deadline-worker BEAT-ATTRIBUTION regression (DEC-PULL-BEAT-PERRUN-001 fix round):
  the run's own deadline-wrapped write's ``emit_progress_beat`` calls land on the run's
  KEYED channel with zero ``progress_beat.foreign_beat_dropped`` WARNs, and a later
  genuinely-foreign beat still WARNs (the epoch throttle is not consumed by the run's
  own writes).

Driven by deterministic FAKE sinks (a blocking ``threading.Event`` that never sets, a
slow-but-OK sleep, a raising sink) + small deadlines so the real timeout path runs fast.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator, Sequence
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
from brutex.observability.correlation import pull_id_scope
from brutex.observability.progress_beat import (
    emit_progress_beat,
    register_progress_beat,
    reset_progress_beats,
)


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


class _BeatingSink:
    """A sink whose ``append`` fires the REAL :func:`emit_progress_beat` — the flock shape.

    Stands in for the partition-flock lock-wait poll inside a contended
    ``ParquetWriter``-backed ``sink.append`` (``partition_lock._acquire_bounded`` fires
    one beat per ``BlockingIOError`` retry) without a real contended ``flock``; the beat
    call is the REAL seam function, so the FULL routing logic (ambient-id resolution on
    the deadline worker → keyed channel / foreign drop) is exercised end-to-end.
    """

    def __init__(self, beats: int) -> None:
        self._beats = beats

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
        for _ in range(self._beats):
            emit_progress_beat()
        return _result(len(bars))


class TestDeadlineWorkerBeatAttribution:
    """The run's OWN deadline-wrapped durable write beats its OWN keyed channel.

    DEC-PULL-BEAT-PERRUN-001 fix round (the deadline-thread attribution gap): both spine
    tails wrap ``sink.append`` in :func:`append_with_deadline`, whose
    ``run_with_deadline`` daemon worker previously started with an EMPTY contextvars
    context — the GAP-#15 antistall beats fired inside the flock poll resolved the
    ambient ``pull_id`` to ``""``, found no legacy registrant, and DROPPED as foreign
    (``progress_beat.foreign_beat_dropped``), so the run's own cross-process
    write-contention liveness signal never fed its watchdog AND the once-per-epoch
    foreign WARN was consumed by the run's own writes. Fails-before / passes-after:
    with the pre-fix empty-context worker,
    ``test_keyed_beats_land_from_a_deadline_wrapped_append`` observed ZERO keyed beats
    plus ONE foreign-drop WARN.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Iterator[None]:
        """The sanctioned seam reset, before AND after (never bleed a keyed channel)."""
        reset_progress_beats()
        yield
        reset_progress_beats()

    def test_keyed_beats_land_from_a_deadline_wrapped_append(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Beats fired inside the deadline worker reach the run's keyed channel, no WARN."""
        beats: list[int] = []
        register_progress_beat(lambda: beats.append(1), pull_id="abc123def456")
        caplog.set_level(logging.WARNING, logger="brutex.observability.progress_beat")
        with pull_id_scope("abc123def456"):
            result = _call(_BeatingSink(beats=3), deadline_seconds=5.0)
        assert result.bars_appended == 1
        assert len(beats) == 3, "the run's own write-contention beats must feed its channel"
        foreign = [r for r in caplog.records if "foreign_beat_dropped" in r.getMessage()]
        assert foreign == [], "the run's OWN writes must never be misdiagnosed as foreign"

    def test_genuine_foreign_beat_still_warns_after_the_runs_own_writes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The run's own writes no longer consume the once-per-epoch foreign-drop WARN.

        Before the fix the run's own deadline-wrapped beats fired the epoch's single
        ``progress_beat.foreign_beat_dropped`` WARN, silencing a LATER genuine zombie's
        WARN for the rest of the epoch. Now the zombie's WARN still fires — loud, naming
        the zombie's channel, not the run's.
        """
        register_progress_beat(lambda: None, pull_id="abc123def456")
        caplog.set_level(logging.WARNING, logger="brutex.observability.progress_beat")
        with pull_id_scope("abc123def456"):
            _call(_BeatingSink(beats=2), deadline_seconds=5.0)
        # A genuinely-foreign beat (a stale run's id, no registrant) AFTER the run's own
        # writes: the epoch WARN must still be armed and must name the ZOMBIE's channel.
        emit_progress_beat("feedfacef00d")
        foreign = [r for r in caplog.records if "foreign_beat_dropped" in r.getMessage()]
        assert len(foreign) == 1
        assert "feedfacef00d" in foreign[0].getMessage()


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

    def test_sink_own_timeout_error_propagates_not_reclassified(self) -> None:
        # DEC-FETCH-HUNG-WRITE-DEADLINE-001 hardening: a real ``TimeoutError`` raised by
        # ``sink.append`` ITSELF (a future network-backed writer; ``socket.timeout`` is an alias
        # of ``TimeoutError`` on 3.10+) must propagate UNCHANGED — it is NOT the deadline guard's
        # own ``DeadlineExceeded``, so it is NOT silently deferred as a hung durable write. The
        # except arm matches ``DeadlineExceeded`` (the subclass), not the broad ``TimeoutError``.
        own = TimeoutError("the writer's own socket timed out")
        sink = _RaisingSink(own)
        with pytest.raises(TimeoutError) as excinfo:
            _call(sink, deadline_seconds=5.0)
        # It is the writer's OWN error object, NOT a reclassified WriteDeadlineExceeded.
        assert excinfo.value is own
        assert not isinstance(excinfo.value, WriteDeadlineExceeded)
        assert "writer's own socket" in str(excinfo.value)
