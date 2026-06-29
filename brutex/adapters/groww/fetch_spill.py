"""Bounded on-disk OVERFLOW for the fetch→write handoff (DEC-FETCH-NONBLOCKING-HANDOFF-001).

The async fetch pipeline (:mod:`brutex.adapters.groww.fetch_pipeline`) hands every fetched
window to a bounded ``queue.Queue(maxsize=4)`` write consumer. When the consumer falls behind a
**SUSTAINED-SLOW (not hung) disk** — each durable write completes, but slowly — that queue FILLS
and the producer's :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.submit` BLOCKS on
``queue.put`` back-pressure, coupling the fetch rate to the write rate. PR #962
(DEC-FETCH-HUNG-WRITE-DEADLINE-001) covers a HUNG write (a deadline raises + defers the window);
this module covers the SLOW case — the one a fixed in-memory bound can NEVER fully decouple,
because a slow-but-not-hung disk fills any fixed buffer.

## What this is

A small, bounded **spill area on disk** for a window's RAW fetched bars. When the in-memory queue
is full, the pipeline serialises the window's bars to ONE spill file (a bounded directory), DROPS
the in-memory bars reference (the bulk memory), and the producer returns IMMEDIATELY — the fetch
lane is decoupled from the write speed. The consumer reloads the bars from the spill file as it
catches up, processes the window's tail durably, then deletes the spill file. So:

* **the fetch never blocks on a slow write** — a full queue spills instead of blocking;
* **memory stays bounded** — the bulk bars live ON DISK while overflowed (only a tiny
  per-window header stays resident);
* **the spill itself is bounded** — a hard cap on the on-disk spill size; once the cap is
  reached the pipeline falls back to the EXISTING bounded back-pressure (a permanently-falling-
  behind disk is the only remaining case the fetch slows — preferring a slowdown to an OOM, and
  a *permanently* wedged volume is PR #962's deadline's job, not an unbounded backlog);
* **disk-full at the spill** maps through the COMMON :func:`~brutex.data.atomic_io.atomic_write_via`
  to the existing :class:`~brutex.data.errors.LakeWriteHalt` graceful-pull-halt path
  (DEC-LAKE-WRITE-HALT-001) — a full disk halts the pull, it never silently drops data.

## Serialisation

The spilled payload is the window's ``list[CandleBar]`` serialised as **JSON** — one object per
bar with the ``timestamp`` as an ISO-8601 string, the ``open`` / ``high`` / ``low`` / ``close``
prices as their exact ``str(Decimal)`` text (so the rupee value round-trips bit-for-bit with NO
float drift — the byte-for-byte reproducibility invariant ``NFR-DET-001``), and ``volume`` /
``open_interest`` as integers (``open_interest`` may be ``null``). JSON is deliberately chosen
over ``pickle`` (no arbitrary-code-execution surface on load, and a self-describing on-disk format)
even though the spill is only an EPHEMERAL same-process, same-run overflow buffer. Reload
reconstructs each :class:`~brutex.data.models.CandleBar`, whose ``__post_init__`` re-validates the
bar, so a corrupt spill can never yield a malformed bar. The atomic write
(tmp → ``fsync`` → ``os.replace``) guarantees a reader never sees a torn file.

## Honest envelope (Golden Rule #10)

This delivers **"the fetch lane never blocks on a slow write while memory stays bounded"** up to a
generous spill cap. It is NOT an infinite buffer: a disk that falls behind for the ENTIRE pull,
past the spill cap, re-applies the bounded back-pressure (the correct choice — an OOM is worse).
It is NOT a throughput increase (the achieved rate is still vendor-bound). It bounds the OVERFLOW;
it does not bound RSS at the OS level (a true RSS ceiling needs ``setrlimit`` / cgroups — out of
scope, as in DEC-LAKE-WRITE-HALT-001). The spill files are best-effort cleaned at drain; any
straggler is a small bounded set under the spill root, removed on the next clean drain. O(1) per
window — one ``atomic_write_via`` / one ``json.loads`` / one ``unlink``, NOT per-bar.

stdlib-only at import (``os`` / ``json`` / ``tempfile`` / ``threading`` / ``dataclasses`` /
``datetime`` / ``decimal`` / ``pathlib``) plus the COMMON
:func:`~brutex.data.atomic_io.atomic_write_via` + :class:`~brutex.data.models.CandleBar`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from brutex.data.atomic_io import atomic_write_via
from brutex.data.models import CandleBar

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

__all__ = [
    "FETCH_SPILL_MAX_BYTES_ENV",
    "FetchSpillArea",
    "SpilledWindow",
    "resolve_fetch_spill_max_bytes",
]

_LOG = structlog.get_logger("brutex.adapters.groww.fetch_spill")

#: Default hard cap on the TOTAL on-disk spill size (bytes), summed across all live spill files.
#: 256 MiB is generous — at a few KB of bars per window it holds thousands of overflowed windows,
#: far more than a slow-but-recovering disk ever backs up — yet finite so a disk that falls behind
#: for the whole pull re-applies the bounded back-pressure instead of filling the volume. Operator-
#: tunable via :data:`FETCH_SPILL_MAX_BYTES_ENV`; NOT a vendor limit.
_DEFAULT_FETCH_SPILL_MAX_BYTES: int = 256 * 1024 * 1024

#: Environment override for the spill cap (operator escape hatch, no code change).
FETCH_SPILL_MAX_BYTES_ENV: str = "BRUTEX_GROWW_FETCH_SPILL_MAX_BYTES"

#: The on-disk spill directory name under the lake root (kept OUTSIDE ``bars/`` + ``options/`` and
#: never a ``*.parquet``, so no lake scanner ever sees a spill file as data — mirrors the
#: ``.locks`` sidecar of DEC-LAKE-PARTITION-FLOCK-001).
_SPILL_DIRNAME: str = ".fetch-spill"

#: The suffix the atomic-write tmp carries; stripped to derive the final spill file path.
_TMP_SUFFIX: str = ".tmp"


def _encode_bars(bars: Sequence[CandleBar]) -> bytes:
    """Serialise *bars* to a compact JSON byte string with exact ``Decimal`` round-trip.

    Each :class:`~brutex.data.models.CandleBar` becomes one JSON object: ``timestamp`` as an
    ISO-8601 string, the four prices as their exact ``str(Decimal)`` text (NO float — the
    byte-for-byte reproducibility invariant), ``volume`` as an int, ``open_interest`` as an int or
    ``null``. UTF-8 encoded. O(bars), one pass (the spill is a per-window O(1) handoff). The inverse
    is :func:`_decode_bars`.
    """
    payload = [
        {
            "ts": bar.timestamp.isoformat(),
            "o": str(bar.open),
            "h": str(bar.high),
            "l": str(bar.low),
            "c": str(bar.close),
            "v": bar.volume,
            "oi": bar.open_interest,
        }
        for bar in bars
    ]
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _decode_bars(data: bytes) -> list[CandleBar]:
    """Reconstruct ``list[CandleBar]`` from the JSON bytes produced by :func:`_encode_bars`.

    Each object is rebuilt into a :class:`~brutex.data.models.CandleBar` — ``timestamp`` via
    :meth:`datetime.fromisoformat`, prices via ``Decimal(str)`` (exact, no float drift), volume/OI
    as ints (``oi`` may be ``None``). The dataclass ``__post_init__`` re-validates every bar, so a
    corrupt spill raises rather than yielding a malformed bar. O(bars), one pass.
    """
    rows = json.loads(data)
    return [
        CandleBar(
            timestamp=datetime.fromisoformat(row["ts"]),
            open=Decimal(row["o"]),
            high=Decimal(row["h"]),
            low=Decimal(row["l"]),
            close=Decimal(row["c"]),
            volume=int(row["v"]),
            open_interest=None if row["oi"] is None else int(row["oi"]),
        )
        for row in rows
    ]


def resolve_fetch_spill_max_bytes(raw: str | None = None) -> int:
    """Resolve the spill cap (bytes): env override, else the audited default.

    Reads :data:`FETCH_SPILL_MAX_BYTES_ENV`; falls back to
    :data:`_DEFAULT_FETCH_SPILL_MAX_BYTES` when unset, blank, non-integer, or non-positive (a bad
    value can NEVER disable the bound — fail-safe to the audited default, Golden Rule #1). A thin
    helper so the live build site + tests share ONE normalisation (CLAUDE.md §11 COMMON). O(1).

    Parameters
    ----------
    raw
        The cap value (``None`` reads :data:`FETCH_SPILL_MAX_BYTES_ENV`); a test may pass one.
    """
    value = os.environ.get(FETCH_SPILL_MAX_BYTES_ENV) if raw is None else raw
    if value is None or not value.strip():
        return _DEFAULT_FETCH_SPILL_MAX_BYTES
    try:
        parsed = int(value)
    except ValueError:
        return _DEFAULT_FETCH_SPILL_MAX_BYTES
    if parsed <= 0:
        return _DEFAULT_FETCH_SPILL_MAX_BYTES
    return parsed


@dataclass(frozen=True, slots=True)
class SpilledWindow:
    """A handle to ONE window whose RAW bars were written to the on-disk spill area.

    Immutable so it crosses the producer→consumer queue safely (in place of the resident bars).
    The consumer calls :meth:`FetchSpillArea.load` with this to reload the bars, then
    :meth:`FetchSpillArea.discard` to delete the file + release the byte budget.

    Attributes
    ----------
    path
        The atomic spill file holding this window's JSON-serialised ``list[CandleBar]``.
    size_bytes
        The file's byte size, reserved against the spill cap (released on
        :meth:`FetchSpillArea.discard`).
    """

    path: Path
    size_bytes: int


class FetchSpillArea:
    """A bounded on-disk overflow buffer for window RAW bars (one per pull, thread-safe).

    Built once per pull at the pipeline. :meth:`try_spill` atomically writes a window's bars to a
    spill file and reserves its size against the cap, returning a :class:`SpilledWindow` handle —
    or ``None`` when the cap would be exceeded (the caller then back-pressures instead, never an
    OOM). :meth:`load` reloads the bars; :meth:`discard` deletes the file + releases the budget.
    :meth:`cleanup` removes any straggler files at drain.

    The byte budget is guarded by ``_lock`` so concurrent consumers + the producer never
    double-count. The atomic write goes through the COMMON
    :func:`~brutex.data.atomic_io.atomic_write_via`, so a disk-full spill raises
    :class:`~brutex.data.errors.LakeWriteHalt` (the existing graceful-halt path).
    """

    def __init__(self, *, spill_dir: Path, max_bytes: int) -> None:
        """Create the spill area under *spill_dir* with a *max_bytes* total cap.

        Parameters
        ----------
        spill_dir
            The directory the spill files live in (created if absent). A dedicated
            ``.fetch-spill`` subdir under the lake root, kept outside the data partitions.
        max_bytes
            The hard cap on total live spill bytes (``>= 1``); overflow past it back-pressures.
        """
        if max_bytes < 1:
            raise ValueError(f"max_bytes must be >= 1, got {max_bytes}")
        self._dir = spill_dir
        self._max_bytes = max_bytes
        self._reserved = 0
        self._lock = threading.Lock()
        self._dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def under_lake_root(cls, lake_root: Path, *, max_bytes: int | None = None) -> FetchSpillArea:
        """Build the spill area at ``<lake_root>/.fetch-spill`` with the resolved cap.

        The COMMON build path the live site uses; *max_bytes* defaults to
        :func:`resolve_fetch_spill_max_bytes` (env-tunable). Keeping the spill UNDER the lake root
        (a dedicated sidecar dir) means a disk-full spill is the SAME volume the lake writes hit,
        so the :class:`~brutex.data.errors.LakeWriteHalt` envelope is consistent.
        """
        cap = resolve_fetch_spill_max_bytes() if max_bytes is None else max_bytes
        return cls(spill_dir=lake_root / _SPILL_DIRNAME, max_bytes=cap)

    @property
    def spill_dir(self) -> Path:
        """The directory the spill files live in (for the live-site log / tests)."""
        return self._dir

    @property
    def reserved_bytes(self) -> int:
        """The current total live spill bytes (thread-safe) — for the watchdog / tests."""
        with self._lock:
            return self._reserved

    def try_spill(self, bars: Sequence[CandleBar]) -> SpilledWindow | None:
        """Atomically spill *bars* to disk + reserve their size; ``None`` if the cap is hit.

        Serialises *bars* to JSON (:func:`_encode_bars`), measures its size, and reserves it against
        the cap UNDER the lock (a two-phase reserve so two concurrent spills can never jointly
        overshoot). When the reservation would exceed the cap the temp is deleted and ``None`` is
        returned — the caller falls back to bounded back-pressure (never an OOM). On success the temp
        is atomically promoted via :func:`~brutex.data.atomic_io.atomic_write_via`
        (tmp → ``fsync`` → ``os.replace``; disk-full → :class:`~brutex.data.errors.LakeWriteHalt`),
        and a :class:`SpilledWindow` handle is returned. O(1) per window (one JSON dump + one atomic
        rename), NOT per-bar.
        """
        payload = _encode_bars(list(bars))
        size = len(payload)
        with self._lock:
            if self._reserved + size > self._max_bytes:
                return None
            self._reserved += size
        # Reserved — now write durably. If the atomic write raises (e.g. a disk-full LakeWriteHalt)
        # release the reservation first so the budget never leaks on a failed spill.
        fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), prefix=".spill-", suffix=".pkl.tmp")
        os.close(fd)
        dest = Path(tmp_name[: -len(_TMP_SUFFIX)])  # the atomic write renames the tmp into ``dest``
        try:
            atomic_write_via(dest, lambda p: p.write_bytes(payload))
        except Exception:
            # Release the reservation on ANY write failure (e.g. a disk-full LakeWriteHalt) so the
            # budget never leaks on a failed spill, then re-raise UNCHANGED (a LakeWriteHalt halts
            # the pull through the existing graceful-halt path). Narrow to ``Exception`` (not
            # ``BaseException``) so a KeyboardInterrupt / SystemExit propagates as-is — the
            # in-memory reservation dies with the interrupted process anyway.
            with self._lock:
                self._reserved -= size
            raise
        return SpilledWindow(path=dest, size_bytes=size)

    def load(self, handle: SpilledWindow) -> list[CandleBar]:
        """Reload the spilled bars for *handle* (the consumer rehydration on dequeue).

        Reads + JSON-decodes the spill file (:func:`_decode_bars`, re-validating every bar). The
        file is NOT deleted here — :meth:`discard` deletes it AFTER the window's tail is durably
        processed, so a consumer crash mid-tail leaves the spill on disk for :meth:`cleanup` rather
        than losing the bars before they were written.
        """
        data = handle.path.read_bytes()
        return _decode_bars(data)

    def discard(self, handle: SpilledWindow) -> None:
        """Delete *handle*'s spill file + release its reserved bytes (after durable processing).

        Idempotent: a missing file (already cleaned) is suppressed. Releases the byte budget under
        the lock so a later spill can reuse the space.
        """
        with contextlib.suppress(FileNotFoundError):
            handle.path.unlink()
        with self._lock:
            self._reserved = max(0, self._reserved - handle.size_bytes)

    def cleanup(self) -> None:
        """Best-effort remove any straggler spill files at drain (never raises).

        A spill file survives only if its window was never discarded (a hard kill mid-tail). The
        directory is small + bounded, so a sweep at pull end keeps the spill area from accreting
        across pulls. The reservation counter is reset. Any unlink error is logged + swallowed —
        cleanup must never fail the pull's ``finally``.
        """
        try:
            entries = list(self._dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            try:
                entry.unlink()
            except OSError:
                _LOG.warning("fetch_spill.cleanup_unlink_failed", path=str(entry))
        with self._lock:
            self._reserved = 0
