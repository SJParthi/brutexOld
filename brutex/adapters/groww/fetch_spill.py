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
It is NOT a throughput increase (the achieved rate is still vendor-bound). The spill is bounded in
**TWO dimensions** (DEC-FETCH-SPILL-COUNT-CAP-001): on-disk BYTES (the byte cap above) AND live
spilled-window HANDLE COUNT — each spilled window keeps a small resident handle graph (a frozen
tail + closures) in the pipeline's overflow deque, so a flood of TINY/EMPTY windows (a 2-byte
``"[]"`` payload — the deep-OTM options chain is mostly empty) could otherwise park millions of
handles in memory before the byte cap ever tripped. When EITHER cap is hit ``try_spill`` returns
``None`` and the producer falls back to the SAME bounded, stop/drain-aware back-pressure — a
slowdown, never a drop and never an OOM. It bounds the OVERFLOW (bytes + handle count); it does
not bound RSS at the OS level (a true RSS ceiling needs ``setrlimit`` / cgroups — out of scope,
as in DEC-LAKE-WRITE-HALT-001). The spill files are best-effort cleaned at drain; any straggler
is a small bounded set under the spill root, removed on the next clean drain. O(1) per window —
one ``atomic_write_via`` / one ``json.loads`` / one ``unlink``, NOT per-bar.

## Per-pull isolation (DEC-FETCH-SPILL-ISOLATION-001)

The spill directory is NAMESPACED PER PULL: :meth:`FetchSpillArea.under_lake_root` builds a
UNIQUE ``<lake_root>/.fetch-spill/pull-<pid>-<uuid>/`` subdirectory, so two concurrent pulls
sharing one ``BRUTEX_LAKE_ROOT`` (a CLI ``brutex fetch`` + a dashboard pull) can NEVER delete
each other's LIVE spill files — previously both used the ONE flat ``.fetch-spill`` dir and
either pull's drain ``cleanup()`` swept EVERY file in it, including the sibling's, whose
consumer then failed the spill load. :meth:`FetchSpillArea.cleanup` removes ONLY its own
namespace dir, plus a conservative STALE sweep of sibling entries older than
:data:`_STALE_SPILL_SECONDS` (a hard-killed pull's straggler dir — bounded by the spill cap —
would otherwise accrete forever now that no other pull sweeps it). Honest envelope: the stale
threshold is generous (24 h ≫ every bounded pipeline deadline), and even a wrongly-swept live
file is non-fatal — the guarded spill load defers that one window to the next pull
(``fetch_pipeline.spill_load_failed``), never losing data.

stdlib-only at import (``os`` / ``json`` / ``tempfile`` / ``threading`` / ``time`` / ``uuid`` /
``dataclasses`` / ``datetime`` / ``decimal`` / ``pathlib``) plus the COMMON
:func:`~brutex.data.atomic_io.atomic_write_via` + :class:`~brutex.data.models.CandleBar`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
import uuid
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
    "FETCH_SPILL_MAX_WINDOWS_ENV",
    "FetchSpillArea",
    "SpilledWindow",
    "resolve_fetch_spill_max_bytes",
    "resolve_fetch_spill_max_windows",
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

#: Default hard cap on the number of LIVE spilled WINDOWS (DEC-FETCH-SPILL-COUNT-CAP-001).
#: Every spilled window keeps a small resident HANDLE graph (a frozen tail + closures) in the
#: pipeline's per-shard overflow deque, so the overflow memory is O(handle count) — the byte cap
#: alone cannot bound it: an EMPTY window's payload is 2 bytes (``"[]"``), so ~134M empty-window
#: handles fit under the 256 MiB byte cap (the deep-OTM options chain is mostly empty) → OOM.
#: 8192 is generous — at ~KB per handle graph it bounds the overflow to low tens of MiB — and for
#: NORMAL windows (a 30-day 1-minute chunk is ~0.6-0.8 MB) the byte cap trips FIRST at a few
#: hundred windows, so this count cap binds ONLY on the tiny/empty pathology. Operator-tunable via
#: :data:`FETCH_SPILL_MAX_WINDOWS_ENV`; NOT a vendor limit.
_DEFAULT_FETCH_SPILL_MAX_WINDOWS: int = 8192

#: Environment override for the spilled-window count cap (operator escape hatch, no code change).
FETCH_SPILL_MAX_WINDOWS_ENV: str = "BRUTEX_GROWW_FETCH_SPILL_MAX_WINDOWS"

#: The on-disk spill PARENT directory name under the lake root (kept OUTSIDE ``bars/`` +
#: ``options/`` and never a ``*.parquet``, so no lake scanner ever sees a spill file as data —
#: mirrors the ``.locks`` sidecar of DEC-LAKE-PARTITION-FLOCK-001). Each pull spills into its
#: OWN unique subdirectory of this parent (DEC-FETCH-SPILL-ISOLATION-001), so concurrent pulls
#: sharing one lake root never touch each other's live spill files.
_SPILL_DIRNAME: str = ".fetch-spill"

#: Age (seconds) past which a SIBLING spill entry (another pull's namespace dir, or a legacy
#: flat spill file) is considered a STRAGGLER from a hard-killed pull and is swept by
#: :meth:`FetchSpillArea.cleanup`. 24 hours is far beyond every bounded pipeline deadline
#: (fetch deadline 45 s · write deadline 90 s · drain join 60 s — a spill file is normally
#: consumed within seconds and the owning pull's own drain removes its dir), so no LIVE pull's
#: spill can plausibly reach this age; without the sweep a hard-killed pull's namespace dir
#: (bounded by the spill cap) would accrete forever now that no other pull cleans it.
_STALE_SPILL_SECONDS: float = 24.0 * 3600.0

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


def _remove_stale_entry(entry: Path) -> None:
    """Best-effort remove ONE stale spill straggler — a namespace dir or a legacy flat file.

    A directory (a hard-killed pull's ``pull-…`` namespace) has its files unlinked then the dir
    removed; anything else (a legacy pre-namespacing flat spill file) is unlinked. Any OS error
    is logged + swallowed (:meth:`FetchSpillArea.cleanup`'s never-raises contract). O(files).
    """
    try:
        if entry.is_dir():
            for child in entry.iterdir():
                child.unlink()
            entry.rmdir()
        else:
            entry.unlink()
    except OSError:
        _LOG.warning("fetch_spill.stale_sweep_failed", path=str(entry))
    else:
        _LOG.info("fetch_spill.stale_swept", path=str(entry))


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


def resolve_fetch_spill_max_windows(raw: str | None = None) -> int:
    """Resolve the spilled-window COUNT cap: env override, else the audited default.

    Reads :data:`FETCH_SPILL_MAX_WINDOWS_ENV`; falls back to
    :data:`_DEFAULT_FETCH_SPILL_MAX_WINDOWS` when unset, blank, non-integer, or non-positive (a
    bad value can NEVER disable the bound — fail-safe to the audited default, Golden Rule #1).
    The structural twin of :func:`resolve_fetch_spill_max_bytes` so the live build site + tests
    share ONE normalisation (CLAUDE.md §11 COMMON). O(1).

    Parameters
    ----------
    raw
        The cap value (``None`` reads :data:`FETCH_SPILL_MAX_WINDOWS_ENV`); a test may pass one.
    """
    value = os.environ.get(FETCH_SPILL_MAX_WINDOWS_ENV) if raw is None else raw
    if value is None or not value.strip():
        return _DEFAULT_FETCH_SPILL_MAX_WINDOWS
    try:
        parsed = int(value)
    except ValueError:
        return _DEFAULT_FETCH_SPILL_MAX_WINDOWS
    if parsed <= 0:
        return _DEFAULT_FETCH_SPILL_MAX_WINDOWS
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
    spill file and reserves its size against the BYTE cap AND one slot against the WINDOW-count
    cap (DEC-FETCH-SPILL-COUNT-CAP-001 — the resident per-window handle graph is what the count
    cap bounds), returning a :class:`SpilledWindow` handle — or ``None`` when EITHER cap would be
    exceeded (the caller then back-pressures instead, never an OOM). :meth:`load` reloads the
    bars; :meth:`discard` deletes the file + releases both budgets. :meth:`cleanup` removes any
    straggler files at drain.

    Both budgets are guarded by the ONE ``_lock`` so concurrent consumers + the producer never
    double-count. The atomic write goes through the COMMON
    :func:`~brutex.data.atomic_io.atomic_write_via`, so a disk-full spill raises
    :class:`~brutex.data.errors.LakeWriteHalt` (the existing graceful-halt path).
    """

    def __init__(
        self,
        *,
        spill_dir: Path,
        max_bytes: int,
        max_windows: int = _DEFAULT_FETCH_SPILL_MAX_WINDOWS,
    ) -> None:
        """Create the spill area under *spill_dir* with the two hard caps.

        Parameters
        ----------
        spill_dir
            The directory the spill files live in (created if absent). A per-pull
            ``.fetch-spill/pull-<pid>-<uuid>`` subdir under the lake root
            (:meth:`under_lake_root`), kept outside the data partitions.
        max_bytes
            The hard cap on total live spill bytes (``>= 1``); overflow past it back-pressures.
        max_windows
            The hard cap on LIVE spilled-window COUNT (``>= 1``) — bounds the resident overflow
            handle graphs the byte cap cannot see (DEC-FETCH-SPILL-COUNT-CAP-001); overflow past
            it back-pressures. Defaults to :data:`_DEFAULT_FETCH_SPILL_MAX_WINDOWS` so every
            existing construction site keeps its behaviour on normal windows (the byte cap trips
            first) while the tiny/empty-window flood is newly bounded.
        """
        if max_bytes < 1:
            raise ValueError(f"max_bytes must be >= 1, got {max_bytes}")
        if max_windows < 1:
            raise ValueError(f"max_windows must be >= 1, got {max_windows}")
        self._dir = spill_dir
        self._max_bytes = max_bytes
        self._max_windows = max_windows
        self._reserved = 0
        self._reserved_windows = 0
        self._lock = threading.Lock()
        self._dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def under_lake_root(
        cls, lake_root: Path, *, max_bytes: int | None = None, max_windows: int | None = None
    ) -> FetchSpillArea:
        """Build the spill area at a UNIQUE ``<lake_root>/.fetch-spill/pull-<pid>-<uuid>/``.

        The COMMON build path the live site uses; *max_bytes* defaults to
        :func:`resolve_fetch_spill_max_bytes` and *max_windows* to
        :func:`resolve_fetch_spill_max_windows` (both env-tunable). Keeping the spill UNDER the
        lake root (a dedicated sidecar dir) means a disk-full spill is the SAME volume the lake
        writes hit, so the :class:`~brutex.data.errors.LakeWriteHalt` envelope is consistent.

        The subdirectory is NAMESPACED PER PULL (DEC-FETCH-SPILL-ISOLATION-001): the old FLAT
        ``<lake_root>/.fetch-spill`` was SHARED by every concurrent pull on the lake (CLI +
        dashboard), so one pull's drain :meth:`cleanup` deleted the OTHER pull's live spill
        files and its consumer failed the load. A ``pull-<pid>-<uuid>`` dir per
        :class:`FetchSpillArea` makes cross-pull deletion impossible; :meth:`cleanup` removes
        only this pull's own dir (plus stale hard-kill stragglers).
        """
        cap = resolve_fetch_spill_max_bytes() if max_bytes is None else max_bytes
        window_cap = resolve_fetch_spill_max_windows() if max_windows is None else max_windows
        namespace = f"pull-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        return cls(
            spill_dir=lake_root / _SPILL_DIRNAME / namespace,
            max_bytes=cap,
            max_windows=window_cap,
        )

    @property
    def spill_dir(self) -> Path:
        """The directory the spill files live in (for the live-site log / tests)."""
        return self._dir

    @property
    def reserved_bytes(self) -> int:
        """The current total live spill bytes (thread-safe) — for the watchdog / tests."""
        with self._lock:
            return self._reserved

    @property
    def reserved_windows(self) -> int:
        """The current LIVE spilled-window count (thread-safe) — for the watchdog / tests."""
        with self._lock:
            return self._reserved_windows

    def try_spill(self, bars: Sequence[CandleBar]) -> SpilledWindow | None:
        """Atomically spill *bars* to disk + reserve both budgets; ``None`` if EITHER cap is hit.

        Serialises *bars* to JSON (:func:`_encode_bars`), measures its size, and reserves it
        against the BYTE cap plus ONE slot against the WINDOW-count cap
        (DEC-FETCH-SPILL-COUNT-CAP-001) UNDER the lock — one atomic two-phase reserve, so two
        concurrent spills can never jointly overshoot either cap. When either reservation would
        exceed its cap, ``None`` is returned — the caller falls back to bounded back-pressure
        (never an OOM, never a dropped window). On success the temp is atomically promoted via
        :func:`~brutex.data.atomic_io.atomic_write_via`
        (tmp → ``fsync`` → ``os.replace``; disk-full → :class:`~brutex.data.errors.LakeWriteHalt`),
        and a :class:`SpilledWindow` handle is returned. O(1) per window (one JSON dump + one atomic
        rename), NOT per-bar.
        """
        payload = _encode_bars(list(bars))
        size = len(payload)
        with self._lock:
            if (
                self._reserved + size > self._max_bytes
                or self._reserved_windows + 1 > self._max_windows
            ):
                return None
            self._reserved += size
            self._reserved_windows += 1
        # Reserved — now write durably. If the atomic write raises (e.g. a disk-full LakeWriteHalt)
        # release the reservation first so the budget never leaks on a failed spill.
        fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), prefix=".spill-", suffix=".pkl.tmp")
        os.close(fd)
        dest = Path(tmp_name[: -len(_TMP_SUFFIX)])  # the atomic write renames the tmp into ``dest``
        try:
            atomic_write_via(dest, lambda p: p.write_bytes(payload))
        except Exception:
            # Release BOTH reservations on ANY write failure (e.g. a disk-full LakeWriteHalt) so
            # neither budget leaks on a failed spill, then re-raise UNCHANGED (a LakeWriteHalt
            # halts the pull through the existing graceful-halt path). Narrow to ``Exception``
            # (not ``BaseException``) so a KeyboardInterrupt / SystemExit propagates as-is — the
            # in-memory reservations die with the interrupted process anyway.
            with self._lock:
                self._reserved -= size
                self._reserved_windows -= 1
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
        """Delete *handle*'s spill file + release both budgets (after durable processing).

        Idempotent: a missing file (already cleaned) is suppressed. Releases the byte budget AND
        one window-count slot under the lock (both floored at 0 so a double-discard can never
        drive a budget negative) so a later spill can reuse the space.
        """
        with contextlib.suppress(FileNotFoundError):
            handle.path.unlink()
        with self._lock:
            self._reserved = max(0, self._reserved - handle.size_bytes)
            self._reserved_windows = max(0, self._reserved_windows - 1)

    def cleanup(self) -> None:
        """Best-effort remove THIS pull's spill dir + stale stragglers at drain (never raises).

        A spill file survives only if its window was never discarded (a hard kill mid-tail).
        Scoped to THIS pull's OWN namespace dir (DEC-FETCH-SPILL-ISOLATION-001) — a concurrent
        pull's LIVE spill files are NEVER touched (the old flat shared dir let one pull's drain
        delete the sibling pull's files, silently killing its consumer's spill load). The own
        dir is removed after its files; both reservation counters are reset; then sibling entries
        older than :data:`_STALE_SPILL_SECONDS` (hard-killed pulls' stragglers) are swept. Any
        unlink error is logged + swallowed — cleanup must never fail the pull's ``finally``.
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
        with contextlib.suppress(OSError):
            self._dir.rmdir()
        with self._lock:
            self._reserved = 0
            self._reserved_windows = 0
        self._sweep_stale_siblings()

    def _sweep_stale_siblings(self) -> None:
        """Best-effort sweep SIBLING spill entries older than the stale threshold (never raises).

        Runs ONLY when this area sits inside a ``.fetch-spill`` parent (the
        :meth:`under_lake_root` layout) — a directly-constructed area (tests / custom dirs)
        never sweeps its arbitrary parent. A sibling entry (another pull's ``pull-…`` namespace
        dir, or a legacy flat spill file from the pre-namespacing layout) is removed IFF its
        mtime is at least :data:`_STALE_SPILL_SECONDS` old — a hard-killed pull's straggler, far
        past every bounded pipeline deadline. A LIVE concurrent pull's dir is younger (its own
        spill activity keeps the mtime fresh) and is left alone; even a wrongly-swept file is
        non-fatal (the guarded spill load defers that one window — never lost data). O(siblings).
        """
        parent = self._dir.parent
        if parent.name != _SPILL_DIRNAME:
            return
        now = time.time()
        try:
            siblings = list(parent.iterdir())
        except OSError:
            return
        for entry in siblings:
            if entry == self._dir:
                continue  # own dir — already handled above (defensive; it was just removed)
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue  # vanished / unreadable between iterdir and stat — leave it
            if now - mtime < _STALE_SPILL_SECONDS:
                continue  # plausibly a LIVE concurrent pull — never touch it
            _remove_stale_entry(entry)
