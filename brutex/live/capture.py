"""Drive the Groww live feed and persist every callback to the tick store.

The Groww feed delivers asynchronously: once you ``subscribe_index_value(...)``,
the SDK's daemon thread invokes your ``on_data_received`` callback **with a meta
argument** on every update (``def on_data_received(meta): ...`` — verified live
against ``growwapi`` at the open; ``docs/groww/api-reference/07-feed.md``); the
callback ignores the meta and pulls the current snapshot via
``get_index_value()``. :class:`TickCapture`
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

#: Subscribe our callback with the feed (closes over the instrument list). Groww
#: calls it WITH a meta arg (``_on_tick(*_args)`` accepts + ignores it); the
#: callable type stays 0-arg-compatible since ``*_args`` is assignable to it.
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
            Registers our callback with the feed (e.g.
            ``lambda cb: feed.subscribe_index_value(instruments, on_data_received=cb)``);
            Groww invokes it with a meta arg, which ``_on_tick`` accepts + ignores.
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
        self._heartbeats = 0

    @property
    def count(self) -> int:
        """Number of snapshots captured so far this session."""
        return self._count

    @property
    def heartbeats(self) -> int:
        """Number of progress/tracking heartbeats emitted so far this session."""
        return self._heartbeats

    def start(self) -> None:
        """Register the on-tick callback with the feed (begins delivery)."""
        _LOG.info("live_capture_start")
        self._subscribe(self._on_tick)

    def _on_tick(self, *_args: Any) -> None:
        """Feed callback: stamp ns + snapshot now, persist, advance the counter.

        Groww's SDK invokes the callback **with a ``meta`` argument** on every
        update (``def on_data_received(meta): ...`` — ``docs/groww/api-reference/
        07-feed.md``; CLAUDE.md §6), so this MUST accept it. We take ``*_args``
        and IGNORE them: the snapshot comes from :attr:`_read_snapshot` (the full
        current feed state), not the per-callback meta — and ``*_args`` stays
        robust to both the 1-arg (Groww) and 0-arg feed-callback conventions.
        Defining it as 0-arg crashed every live tick with ``_on_tick() takes 1
        positional argument but 2 were given`` (operator open-bell run).
        """
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
        heartbeat_interval: float = 30.0,
    ) -> int:
        """Start delivery and keep the thread alive for *seconds*, then return.

        The SDK delivers ticks on its own daemon thread; this loop only keeps the
        process alive (injected *sleep* / *monotonic* make it deterministic in
        tests). Every *heartbeat_interval* seconds it emits a
        ``live_capture_progress`` tracking line (captured-so-far, ticks since the
        last beat, elapsed, rate) so an unattended multi-hour run is VISIBLY
        alive — a beat with ``since_last=0`` is itself the signal that the feed
        has gone quiet. Returns the total number of snapshots captured.
        """
        self.start()
        start = monotonic()
        deadline = start + seconds
        next_beat = start + heartbeat_interval
        last_count = 0
        while True:
            now = monotonic()
            if now >= deadline:
                break
            if now >= next_beat:
                last_count = self._heartbeat(now - start, last_count)
                next_beat += heartbeat_interval
            sleep(poll_interval)
        _LOG.info("live_capture_done", captured=self._count, heartbeats=self._heartbeats)
        return self._count

    def _heartbeat(self, elapsed: float, last_count: int) -> int:
        """Emit one progress/tracking line; return the new running tick count.

        *elapsed* is strictly positive whenever a beat fires (it is only called
        once ``now >= start + heartbeat_interval``), so the rate is safe.
        """
        captured = self._count
        self._heartbeats += 1
        _LOG.info(
            "live_capture_progress",
            captured=captured,
            since_last=captured - last_count,
            elapsed_s=round(elapsed, 1),
            ticks_per_s=round(captured / elapsed, 2),
        )
        return captured


__all__ = ["SnapshotFn", "SubscribeFn", "TickCapture", "_utc_now"]
