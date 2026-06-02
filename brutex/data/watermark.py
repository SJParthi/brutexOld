"""Per-chunk fetch-status watermark — the zero-data-loss + no-quota-burn tracker.

Closes the G-10 gap (``TRACK1_REQUIREMENTS_FINAL.md §3``): an empty
historical-candle window (pre-listing / trading-halt / holiday / sparse
F&O minute) would otherwise be re-requested forever on every resume,
silently burning the 100K/day Dhan quota.

A **chunk** is one historical-candle fetch window:
``(groww_symbol, interval, start, end)``. After every fetch attempt the
loop records the outcome here so that:

* **COMPLETED / NO_DATA** chunks are *settled* — never re-requested
  (the G-10 anti-loop fix; an empty window is recorded ``NO_DATA`` once
  and skipped thereafter).
* a crash + resume re-fetches **exactly** the unsettled chunks — no
  re-fetch of done work, no silent loss.
* **PARTIAL** chunks (interrupted mid-window) are re-attempted.
* **FAILED** chunks (terminal error) are *quarantined* — surfaced for
  operator attention, NOT auto-re-fetched (so a permanently-broken
  request can't hammer the API).

This module is **pure** (no I/O, no heavy deps). Persistence is the
caller's concern: :meth:`FetchWatermark.to_rows` / :meth:`from_rows`
serialize the state so it can be written to JSON / DuckDB / LMDB and
restored on the next run (the COMPREHENSIVE serialize/restore rule).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ChunkStatus(StrEnum):
    """Terminal outcome of one chunk fetch attempt."""

    COMPLETED = "completed"  # bars fetched + stored; settled, never re-fetch
    NO_DATA = "no_data"  # source returned empty (legit); settled (G-10 fix)
    PARTIAL = "partial"  # interrupted mid-window; re-attempt on resume
    FAILED = "failed"  # terminal error; quarantined, no auto-re-fetch


#: Statuses meaning "settled — do NOT re-request" (the G-10 anti-loop set).
_SETTLED: Final[frozenset[ChunkStatus]] = frozenset({ChunkStatus.COMPLETED, ChunkStatus.NO_DATA})


@dataclass(frozen=True, slots=True)
class ChunkKey:
    """Identity of one historical-candle fetch window (hashable)."""

    groww_symbol: str
    interval: str
    start: str  # ISO-8601 string (caller formats datetimes deterministically)
    end: str  # ISO-8601 string


class FetchWatermark:
    """In-memory per-chunk fetch-status tracker with serialize/restore."""

    def __init__(self) -> None:
        self._status: dict[ChunkKey, ChunkStatus] = {}

    def record(self, key: ChunkKey, status: ChunkStatus) -> None:
        """Record (or overwrite) the terminal outcome of *key*."""
        self._status[key] = status

    def get(self, key: ChunkKey) -> ChunkStatus | None:
        """Return the recorded status of *key*, or None if never attempted."""
        return self._status.get(key)

    def is_settled(self, key: ChunkKey) -> bool:
        """True iff *key* is COMPLETED or NO_DATA — never re-request it.

        This is the G-10 guard: a window the source confirmed empty is
        ``NO_DATA`` and stays settled, so it is not re-fetched forever.
        """
        return self._status.get(key) in _SETTLED

    def needs_fetch(self, key: ChunkKey) -> bool:
        """True iff *key* should be (re)fetched: never attempted, or PARTIAL.

        FAILED chunks are quarantined (see :meth:`quarantined`) and are
        NOT auto-fetched, so a terminally-broken request cannot hammer
        the API.
        """
        current = self._status.get(key)
        return current is None or current == ChunkStatus.PARTIAL

    def pending(self, all_keys: Iterable[ChunkKey]) -> list[ChunkKey]:
        """Return the subset of *all_keys* that still :meth:`needs_fetch`."""
        return [key for key in all_keys if self.needs_fetch(key)]

    def quarantined(self) -> list[ChunkKey]:
        """Return all FAILED chunks (operator attention; not auto-fetched)."""
        return [key for key, status in self._status.items() if status == ChunkStatus.FAILED]

    def summary(self) -> dict[str, int]:
        """Return a count of chunks per status (all statuses present, zero-filled)."""
        counts = {status.value: 0 for status in ChunkStatus}
        for status in self._status.values():
            counts[status.value] += 1
        return counts

    def to_rows(self) -> list[dict[str, str]]:
        """Serialize to a list of flat dict rows (JSON / DuckDB friendly)."""
        return [
            {
                "groww_symbol": key.groww_symbol,
                "interval": key.interval,
                "start": key.start,
                "end": key.end,
                "status": status.value,
            }
            for key, status in self._status.items()
        ]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, str]]) -> FetchWatermark:
        """Restore a watermark from rows produced by :meth:`to_rows`."""
        watermark = cls()
        for row in rows:
            watermark.record(
                ChunkKey(
                    groww_symbol=row["groww_symbol"],
                    interval=row["interval"],
                    start=row["start"],
                    end=row["end"],
                ),
                ChunkStatus(row["status"]),
            )
        return watermark


__all__ = [
    "ChunkKey",
    "ChunkStatus",
    "FetchWatermark",
]
