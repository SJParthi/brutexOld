"""Drive the Groww live feed and persist every callback to the tick store.

The Groww feed delivers asynchronously: once you ``subscribe_index_value(...)``,
the SDK's daemon thread invokes your **0-argument** ``on_data_received`` callback
on every update; the callback then pulls the current snapshot via
``get_index_value()`` (verified against ``growwapi`` v1.5.x). :class:`TickCapture`
wraps that contract behind two injected callables — ``subscribe`` (registers our
callback) and ``read_snapshot`` (pulls the snapshot) — so the whole loop is
unit-tested with fakes, no ``growwapi`` and no network. The real feed is built at
the CLI boundary (:mod:`brutex.live.__main__`), the only ``# pragma: no cover``.

Read-only: this subscribes to market data and writes a log. It never places,
modifies, or cancels an order (Golden Rule #3 scope guard).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import structlog

from brutex.live.store import TickRecord, TickStore

_LOG = structlog.get_logger("brutex.live.capture")

#: Subscribe a 0-arg callback with the feed (closes over the instrument list).
SubscribeFn = Callable[[Callable[[], None]], None]
#: Pull the current feed snapshot mapping.
SnapshotFn = Callable[[], Mapping[str, Any]]


def _utc_now() -> datetime:
    """Current UTC instant (injection seam for deterministic tests)."""
    return datetime.now(UTC)


class TickCapture:
    """Subscribe to a live feed and append every snapshot to a :class:`TickStore`."""

    def __init__(
        self,
        *,
        store: TickStore,
        subscribe: SubscribeFn,
        read_snapshot: SnapshotFn,
        clock: Callable[[], datetime] = _utc_now,
        ns_clock: Callable[[], int] = time.time_ns,
        on_record: Callable[[TickRecord], None] | None = None,
    ) -> None:
        """Wire the capture.

        Parameters
        ----------
        store
            Durable append-only sink (one line per callback).
        subscribe
            Registers our 0-arg callback with the feed (e.g.
            ``lambda cb: feed.subscribe_index_value(instruments, on_data_received=cb)``).
        read_snapshot
            Pulls the current snapshot (e.g. ``feed.get_index_value``).
        clock
            Returns the UTC receive timestamp (injectable for tests).
        ns_clock
            Returns OUR receive time in nanoseconds (``time.time_ns`` — the
            high-resolution cadence oracle; injectable for tests).
        on_record
            Optional per-record hook (e.g. a live counter / progress log).
        """
        self._store = store
        self._subscribe = subscribe
        self._read_snapshot = read_snapshot
        self._clock = clock
        self._ns_clock = ns_clock
        self._on_record = on_record
        self._count = 0

    @property
    def count(self) -> int:
        """Number of snapshots captured so far this session."""
        return self._count

    def start(self) -> None:
        """Register the on-tick callback with the feed (begins delivery)."""
        _LOG.info("live_capture_start")
        self._subscribe(self._on_tick)

    def _on_tick(self) -> None:
        """Feed callback: stamp ns + snapshot now, persist, advance the counter."""
        recv_ns = self._ns_clock()
        recv_ts = self._clock()
        snapshot = self._read_snapshot()
        record = self._store.append(snapshot, recv_ts=recv_ts, recv_ns=recv_ns)
        self._count += 1
        if self._on_record is not None:
            self._on_record(record)

    def run_for(
        self,
        seconds: float,
        *,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
        poll_interval: float = 0.5,
    ) -> int:
        """Start delivery and keep the thread alive for *seconds*, then return.

        The SDK delivers ticks on its own daemon thread; this loop only keeps the
        process alive (injected *sleep* / *monotonic* make it deterministic in
        tests). Returns the total number of snapshots captured.
        """
        self.start()
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            sleep(poll_interval)
        _LOG.info("live_capture_done", captured=self._count)
        return self._count


__all__ = ["SnapshotFn", "SubscribeFn", "TickCapture", "_utc_now"]
