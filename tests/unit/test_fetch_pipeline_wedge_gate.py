"""Unit tests for the ``FetchPipeline`` volume-wedge gate (DEC-VOLUME-KEEPALIVE-001).

Layers 2+3 of the volume-keepalive guard, at the producer's ``submit`` gate:

* a ``None`` keepalive is byte-identical to today (the default fast path);
* a WEDGED keepalive PAUSES ``submit`` — crucially BEFORE the producer can reach
  the confirmed-unbounded spill ``fsync`` (``try_spill`` spy proves zero spill
  calls while wedged — the verified-gap regression);
* the pause is stop-aware (a peer pool trip re-raises + releases it) and
  drain-aware (a pull-end drain never deadlocks behind it);
* a wedge outlasting ``halt_after_s`` raises ``LakeWriteHalt('volume_wedged')``
  FROM THE PRODUCER THREAD (the daemon never raises into the pull) and trips the
  pool — and that halt is pinned to ride the existing global-stop + CRITICAL
  pull-halt alert chains with ZERO new plumbing;
* ``drain`` stops the keepalive daemon on every exit path.
"""

# Whitebox tests of pipeline/keepalive privates (sanctioned, mirrors the sibling suite).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.fetch_pipeline import (
    FetchPipeline,
    FetchTail,
    RawFetchResult,
)
from brutex.adapters.groww.fetch_spill import FetchSpillArea
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar
from brutex.data.volume_keepalive import PROBE_FILENAME, VolumeKeepalive
from structlog.testing import capture_logs

#: Wall-clock budget for every bounded join in this file (the #999 20 s pattern).
_BUDGET_S = 20.0

#: One valid spillable bar for the spill-path regression.
_BAR = CandleBar(
    timestamp=datetime(2021, 1, 4, 4, 5, tzinfo=UTC),
    open=Decimal("100.00"),
    high=Decimal("110.00"),
    low=Decimal("90.00"),
    close=Decimal("105.00"),
    volume=3,
    open_interest=None,
)


def _result(
    shard_key: str,
    process: Callable[[FetchTail], None],
    *,
    spillable: bool = False,
) -> RawFetchResult:
    tail = FetchTail(
        shard_key=shard_key,
        process=process,
        raw_bars=(_BAR,) if spillable else (),
        rehydrate=(lambda bars: None) if spillable else None,
    )
    return RawFetchResult(tail=tail)


def _keepalive(
    tmp_path: Path,
    *,
    interval_s: float | None = None,
    halt_after_s: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> VolumeKeepalive:
    """An UNSTARTED real keepalive whose wedge state the tests drive directly."""
    return VolumeKeepalive(
        tmp_path / PROBE_FILENAME,
        interval_s=interval_s,
        halt_after_s=halt_after_s,
        monotonic=monotonic,
        probe=lambda: None,
    )


class TestSubmitWedgeGate:
    def test_none_keepalive_is_byte_identical_fast_path(self, tmp_path: Path) -> None:
        """The default (no keepalive) submit path processes normally — no gate."""
        done = threading.Event()
        pipeline = FetchPipeline(workers=1)
        try:
            pipeline.submit(_result("NSE|NIFTY|1minute", lambda _t: done.set()))
            assert done.wait(_BUDGET_S)
        finally:
            pipeline.drain()

    def test_healthy_keepalive_is_fast_path(self, tmp_path: Path) -> None:
        """A wired-but-healthy keepalive never pauses submit."""
        done = threading.Event()
        keepalive = _keepalive(tmp_path)
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)
        try:
            pipeline.submit(_result("NSE|NIFTY|1minute", lambda _t: done.set()))
            assert done.wait(_BUDGET_S)
        finally:
            pipeline.drain()
        # drain owns the keepalive teardown even when it was never started.
        assert keepalive._stop_event.is_set()

    def test_wedged_pauses_then_proceeds_on_recovery(self, tmp_path: Path) -> None:
        """Submit blocks at the gate while wedged and self-recovers once cleared."""
        done = threading.Event()
        keepalive = _keepalive(tmp_path)
        keepalive._mark_wedged(OSError("spun down"))
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)
        try:
            with capture_logs() as logs:
                producer = threading.Thread(
                    target=pipeline.submit,
                    args=(_result("NSE|NIFTY|1minute", lambda _t: done.set()),),
                    daemon=True,
                )
                producer.start()
                time.sleep(0.2)
                assert producer.is_alive()  # paused at the gate
                assert not done.is_set()
                keepalive._mark_healthy()
                producer.join(timeout=_BUDGET_S)
                assert not producer.is_alive()
            assert done.wait(_BUDGET_S)
            assert sum(1 for e in logs if e["event"] == "fetch_pipeline.paused_wedged") == 1
            # The recovery re-armed the pause edge latch for the NEXT episode.
            assert pipeline._pause_fired is False
        finally:
            pipeline.drain()

    def test_pause_warning_is_edge_latched_across_producers(self, tmp_path: Path) -> None:
        """TWO producers paused by ONE wedge episode log exactly ONE warning."""
        keepalive = _keepalive(tmp_path)
        keepalive._mark_wedged(OSError("spun down"))
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)
        try:
            with capture_logs() as logs:
                producers = [
                    threading.Thread(
                        target=pipeline.submit,
                        args=(_result("NSE|NIFTY|1minute", lambda _t: None),),
                        daemon=True,
                    )
                    for _ in range(2)
                ]
                for p in producers:
                    p.start()
                time.sleep(0.3)
                assert all(p.is_alive() for p in producers)  # both paused
                keepalive._mark_healthy()
                for p in producers:
                    p.join(timeout=_BUDGET_S)
                    assert not p.is_alive()
            assert sum(1 for e in logs if e["event"] == "fetch_pipeline.paused_wedged") == 1
        finally:
            pipeline.drain()

    def test_wedged_past_halt_raises_from_producer_and_trips_pool(self, tmp_path: Path) -> None:
        """Layer 3: > halt_after_s wedged → LakeWriteHalt('volume_wedged') on the producer."""
        clock = [0.0]
        keepalive = _keepalive(tmp_path, halt_after_s=900.0, monotonic=lambda: clock[0])
        keepalive._mark_wedged(OSError("spun down"))
        clock[0] = 1000.0  # wedged for 1000 s > the 900 s bound
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)
        try:
            with capture_logs() as logs, pytest.raises(LakeWriteHalt) as excinfo:
                pipeline.submit(_result("NSE|NIFTY|1minute", lambda _t: None))
            assert excinfo.value.reason == "volume_wedged"
            assert any(
                e["event"] == "fetch_pipeline.halt" and e["reason"] == "volume_wedged" for e in logs
            )
            # The pool is TRIPPED: the next submit fails fast with the SAME halt.
            with pytest.raises(LakeWriteHalt):
                pipeline.submit(_result("NSE|NIFTY|1minute", lambda _t: None))
        finally:
            with pytest.raises(LakeWriteHalt):
                pipeline.drain()  # the captured halt re-raises at drain (existing contract)

    def test_peer_trip_releases_pause(self, tmp_path: Path) -> None:
        """A peer pool halt wakes + re-raises out of the pause (never an unbounded wedge)."""
        keepalive = _keepalive(tmp_path)
        keepalive._mark_wedged(OSError("spun down"))
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)
        raised: list[LakeWriteHalt] = []

        def _submit() -> None:
            try:
                pipeline.submit(_result("NSE|NIFTY|1minute", lambda _t: None))
            except LakeWriteHalt as exc:
                raised.append(exc)

        try:
            producer = threading.Thread(target=_submit, daemon=True)
            producer.start()
            time.sleep(0.2)
            assert producer.is_alive()  # paused
            pipeline._trip(LakeWriteHalt("disk_full"))
            producer.join(timeout=_BUDGET_S)
            assert not producer.is_alive()
            assert len(raised) == 1
            assert isinstance(raised[0], LakeWriteHalt)
            assert raised[0].reason == "disk_full"
        finally:
            with pytest.raises(LakeWriteHalt):
                pipeline.drain()

    def test_drain_releases_pause_no_deadlock(self, tmp_path: Path) -> None:
        """A pull-end drain never deadlocks behind a paused producer."""
        keepalive = _keepalive(tmp_path)
        keepalive._mark_wedged(OSError("spun down"))
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)
        producer = threading.Thread(
            target=pipeline.submit,
            args=(_result("NSE|NIFTY|1minute", lambda _t: None),),
            daemon=True,
        )
        producer.start()
        time.sleep(0.2)
        assert producer.is_alive()  # paused at the gate
        pipeline.drain()  # sets _draining → the pause returns; drain completes clean
        producer.join(timeout=_BUDGET_S)
        assert not producer.is_alive()

    def test_wedged_producer_never_reaches_spill_fsync(self, tmp_path: Path) -> None:
        """THE VERIFIED-GAP REGRESSION: the pause intercepts BEFORE _spill_full_window.

        Pre-fix, a full shard queue routed the producer straight into
        ``try_spill``'s producer-thread ``fsync`` — the confirmed unbounded hang on
        a wedged volume. With the gate, a wedged submit makes ZERO spill calls; the
        spill runs only after the volume recovers.
        """
        area = FetchSpillArea.under_lake_root(tmp_path)
        spill_calls: list[int] = []
        real_try_spill = area.try_spill

        def _spy(bars: object) -> object:
            spill_calls.append(1)
            return real_try_spill(bars)  # type: ignore[arg-type]

        area.try_spill = _spy  # type: ignore[method-assign, assignment]
        keepalive = _keepalive(tmp_path)
        release_consumer = threading.Event()
        processed: list[str] = []

        def _blocking_process(_t: FetchTail) -> None:
            release_consumer.wait(_BUDGET_S)
            processed.append("blocked")

        pipeline = FetchPipeline(workers=1, queue_maxsize=1, spill_area=area, keepalive=keepalive)
        try:
            # Fill the lane: A is picked up by the consumer (blocks); B fills the queue.
            pipeline.submit(_result("NSE|NIFTY|1minute", _blocking_process))
            pipeline.submit(_result("NSE|NIFTY|1minute", lambda _t: processed.append("B")))
            # Now WEDGE the volume and submit C (spillable — would hit try_spill today).
            keepalive._mark_wedged(OSError("spun down"))
            producer = threading.Thread(
                target=pipeline.submit,
                args=(
                    _result(
                        "NSE|NIFTY|1minute",
                        lambda _t: processed.append("C"),
                        spillable=True,
                    ),
                ),
                daemon=True,
            )
            producer.start()
            time.sleep(0.3)
            assert producer.is_alive()  # paused at the gate, NOT wedged inside the spill
            assert spill_calls == []  # ZERO spill fsyncs while the volume is wedged
            # Recovery: the producer proceeds; the (still-full) queue routes C to the spill.
            keepalive._mark_healthy()
            producer.join(timeout=_BUDGET_S)
            assert not producer.is_alive()
            assert len(spill_calls) == 1
            release_consumer.set()
        finally:
            release_consumer.set()
            pipeline.drain()
        assert sorted(processed) == ["B", "C", "blocked"]


class TestDrainStopsKeepalive:
    def test_drain_stops_started_keepalive(self, tmp_path: Path) -> None:
        keepalive = _keepalive(tmp_path, interval_s=60.0)
        keepalive.start()
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)
        pipeline.drain()
        thread = keepalive._thread
        assert thread is not None
        assert not thread.is_alive()
        # A second drain (the early idle-return path) re-stops idempotently.
        pipeline.drain()

    def test_drain_stops_keepalive_before_captured_halt_reraise(self, tmp_path: Path) -> None:
        keepalive = _keepalive(tmp_path, interval_s=60.0)
        keepalive.start()
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)
        pipeline._trip(LakeWriteHalt("disk_full"))
        with pytest.raises(LakeWriteHalt):
            pipeline.drain()
        thread = keepalive._thread
        assert thread is not None
        assert not thread.is_alive()

    def test_keepalive_stop_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        keepalive = _keepalive(tmp_path)
        pipeline = FetchPipeline(workers=1, keepalive=keepalive)

        def _boom() -> None:
            raise RuntimeError("stop boom")

        monkeypatch.setattr(keepalive, "stop", _boom)
        with capture_logs() as logs:
            pipeline.drain()  # completes despite the teardown failure
        assert any(e["event"] == "fetch_pipeline.keepalive_stop_failed" for e in logs)


class TestHaltRidesExistingAlertChain:
    """No new alert plumbing: 'volume_wedged' rides the pre-wired global-stop paths."""

    def test_volume_wedged_is_a_pull_halt(self) -> None:
        from brutex.dashboard.pull_jobs import is_pull_halt

        assert is_pull_halt(LakeWriteHalt("volume_wedged")) is True

    def test_volume_wedged_in_both_spines_global_stop_errors(self) -> None:
        from brutex.adapters.groww import option_ingest, run_ingest

        halt = LakeWriteHalt("volume_wedged")
        assert isinstance(halt, run_ingest._GLOBAL_STOP_ERRORS)
        assert isinstance(halt, option_ingest._GLOBAL_STOP_ERRORS)

    def test_emit_pull_halt_alert_surfaces_reason_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.notifications import sweep_alerts

        calls: list[str] = []

        def _spy(self: object, *, reason: str) -> None:
            calls.append(reason)

        monkeypatch.setattr(sweep_alerts.SweepAlerter, "pull_halted", _spy)
        sweep_alerts.emit_pull_halt_alert(LakeWriteHalt("volume_wedged"))
        assert calls == ["volume_wedged"]
