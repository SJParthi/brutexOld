"""O(1) fetch-window coverage — skip already-pulled windows, never waste a vendor hit.

The "what's already pulled" ledger (DEC-FETCH-COVERAGE-001): the spot ingest
tiles ``[start, end)`` into fetch windows (one window = one vendor API call);
this tracker records each **completed** window so a re-pull can **skip the
network call** for a window it already has — only the missing / new-day / failed
windows hit the vendor again. That is what keeps a re-click from re-burning the
Dhan **5 req/s** Data budget (CLAUDE.md §6) on data already in the lake.

Membership is **O(1)** (a hash set keyed by an immutable :class:`WindowKey`),
**O(1)** to mark, and serialise/restore so the coverage survives a process
restart (``to_records`` / ``from_records`` — persist alongside the lake). The
idempotent sink stays the correctness backstop: even if a window is re-fetched,
the write is a no-op on stored bars (CLAUDE.md §2 Rule 7), so skipping is a pure
**quota/latency** optimisation that can never lose or duplicate data.

A window's key includes its exact ``(start, end)`` dates, so the **current**
(today-touching) window gets a fresh key each day — it is never wrongly skipped
on the next day's pull, while every **settled past** window is skipped.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True, slots=True)
class WindowKey:
    """One fetch window's coverage cell — the unit of a single vendor API call.

    Immutable + hashable, so it is an O(1) set member. ``market`` namespaces
    the data source (e.g. ``"dhan"``); ``security_id`` + ``interval`` +
    ``(start, end)`` pin the exact window the adapter would fetch.
    """

    market: str
    security_id: str
    interval: str
    start: date
    end: date


@dataclass(slots=True)
class FetchCoverage:
    """An O(1) set of completed fetch windows (the skip-if-already-pulled ledger).

    Thread-safety (DEC-DHAN-PARALLEL-INGEST-001). One ledger is threaded into
    every parallel ``(instrument, interval)`` worker (spot) / underlying worker
    (options) so the workers share one skip-set. A :class:`threading.Lock`
    guards every read/write (``is_complete`` / ``mark_complete`` /
    ``completed_count`` / ``to_records``) so a concurrent ``mark_complete`` can
    never tear the set during another worker's probe or serialisation. This is
    defensive — a duplicate fetch is already idempotent at the sink (Rule #7) —
    but cheap (O(1) hold time, no I/O under the lock) and it keeps the
    skip/serialise reads consistent under the fan-out.
    """

    _complete: set[WindowKey] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def is_complete(self, key: WindowKey) -> bool:
        """Return ``True`` iff *key*'s window was already pulled — **O(1)**, atomic."""
        with self._lock:
            return key in self._complete

    def mark_complete(self, key: WindowKey) -> None:
        """Record *key*'s window as pulled — **O(1)**, idempotent, atomic."""
        with self._lock:
            self._complete.add(key)

    def unmark(self, key: WindowKey) -> None:
        """Drop *key* from the completed set so it is RE-PULLED — **O(1)**, idempotent, atomic.

        The inverse of :meth:`mark_complete` (DEC-FAILED-WINDOW-FORCE-REPULL-001):
        a window wrongly marked complete by the coarse span-enclosure reconcile
        (an internal-gap ``FAILED`` window) is un-marked so the ingest re-fetches
        it instead of skipping it forever. ``set.discard`` — a no-op if absent.
        """
        with self._lock:
            self._complete.discard(key)

    @property
    def completed_count(self) -> int:
        """Number of windows recorded complete (thread-safe)."""
        with self._lock:
            return len(self._complete)

    def to_records(self) -> list[tuple[str, str, str, str, str]]:
        """Serialise to a deterministic, sorted list of string tuples (atomic snapshot)."""
        with self._lock:
            return sorted(
                (k.market, k.security_id, k.interval, k.start.isoformat(), k.end.isoformat())
                for k in self._complete
            )

    @classmethod
    def from_records(cls, records: Iterable[tuple[str, str, str, str, str]]) -> FetchCoverage:
        """Restore from :meth:`to_records` output (round-trips exactly)."""
        coverage = cls()
        for market, security_id, interval, start, end in records:
            coverage._complete.add(
                WindowKey(
                    market=market,
                    security_id=security_id,
                    interval=interval,
                    start=date.fromisoformat(start),
                    end=date.fromisoformat(end),
                )
            )
        return coverage


__all__ = ["FetchCoverage", "WindowKey"]
