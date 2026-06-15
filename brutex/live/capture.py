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

Each callback persists a **DELTA**, not the full snapshot: only the
instruments whose FULL observation (price + vendor ``tsInMillis`` + volume / OI)
CHANGED since the previous callback are written (via
:func:`~brutex.live.tick.prune_unchanged`), and a callback in which NOTHING
changed writes nothing at all. Keying on the whole leaf — not price alone —
keeps every DISTINCT vendor tick, including a liquid equity's same-price reprint
with a fresh timestamp ("save ALL the ticks"); only a byte-for-byte re-read of an
identical leaf is dropped. This is the O(1)-per-instrument fix for the operator's
live run, where writing the entire 779-instrument snapshot verbatim on every
~140/sec callback produced **4.33 GB in 4 minutes** of almost-pure redundancy.
The candle builder takes open/high/low/close as first/max/min/last (volume
hard-0), none of which an extra same-price observation can alter, so the 1-minute
candles built from the delta stream are byte-identical to those from the
full-snapshot stream (DEC-LIVE-DELTA-CAPTURE-001).

Read-only: this subscribes to market data and writes a log. It never places,
modifies, or cancels an order (Golden Rule #3 scope guard).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import structlog

from brutex.live.store import TickRecord, TickSink
from brutex.live.tick import Quote, prune_unchanged

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
        store: TickSink,
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
        #: Last WRITTEN :class:`~brutex.live.tick.Quote` per instrument key — the
        #: delta baseline. A FULL-leaf key (price + vendor ``tsInMillis`` +
        #: volume / OI), so a same-price reprint with a fresh timestamp is kept as
        #: a genuine new tick ("save ALL the ticks"). Seeded empty so the first
        #: callback keeps every instrument; advanced only when a record is
        #: actually written (a skipped callback never changes the baseline, so the
        #: next real change is still detected). The baseline is CLEARED at every
        #: minute boundary (see :meth:`_on_tick`) so the first callback of each new
        #: minute re-writes a full keyframe.
        self._last_obs: dict[str, Quote] = {}
        #: The UTC minute of the last WRITTEN record (``None`` before the first).
        #: When the incoming callback's minute differs, the baseline is reset so a
        #: per-minute keyframe is emitted — this is what makes the delta-stream
        #: candles byte-identical to the full-stream candles (DEC-LIVE-DELTA).
        self._last_minute: datetime | None = None

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
        """Feed callback: stamp ns, read the snapshot, persist the DELTA only.

        Groww's SDK invokes the callback **with a ``meta`` argument** on every
        update (``def on_data_received(meta): ...`` — ``docs/groww/api-reference/
        07-feed.md``; CLAUDE.md §6), so this MUST accept it. We take ``*_args``
        and IGNORE them: the snapshot comes from :attr:`_read_snapshot` (the full
        current feed state), not the per-callback meta — and ``*_args`` stays
        robust to both the 1-arg (Groww) and 0-arg feed-callback conventions.
        Defining it as 0-arg crashed every live tick with ``_on_tick() takes 1
        positional argument but 2 were given`` (operator open-bell run).

        **Delta + per-minute keyframe (DEC-LIVE-DELTA-CAPTURE-001).**
        :func:`~brutex.live.tick.prune_unchanged` drops every instrument whose
        value equals the last WRITTEN value, so within a minute only the changed
        instruments are persisted; an UNCHANGED callback prunes to empty and we
        write NOTHING (no record, the counter does NOT advance, the baseline is
        untouched — O(1), no redundant 779-instrument line). At each **minute
        boundary** the baseline is CLEARED first, so the first callback of every
        new minute re-writes a full keyframe for every instrument present. This is
        what keeps the candles byte-identical to the full-snapshot stream: the
        builder emits a bar for a minute only if a tick arrived in it, so an
        instrument that stays quiet across a minute boundary still needs ONE
        re-stamp that minute (the carry-forward bar) — the keyframe supplies it
        while the within-minute prune still strips the ~140/sec redundancy.

        The ns/ts stamps and ``seq`` semantics are otherwise identical to the
        full-snapshot capture; ``seq`` advances only on a written record.
        """
        recv_ns = self._ns_clock()
        recv_ts = self._clock()
        snapshot = self._read_snapshot()
        minute = recv_ts.replace(second=0, microsecond=0)
        baseline = self._last_obs if minute == self._last_minute else {}
        pruned, updated = prune_unchanged(snapshot, baseline)
        if not pruned:
            return
        record = self._store.append(pruned, recv_ts=recv_ts, recv_ns=recv_ns)
        self._last_obs = updated
        self._last_minute = minute
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
