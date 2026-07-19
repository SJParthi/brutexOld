"""Crash-safe resumable sweep checkpoint (DEC-SWEEP-REALTIME-GUARANTEE-001).

The durable "where did the sweep get to?" marker so a killed sweep resumes
where it left off instead of recomputing from combo 0 (CLAUDE.md §2 Rule 7 —
idempotence; same inputs → same outputs, and a re-run never repeats finished
work). The checkpoint records the index of the **last fully-completed combo**;
:attr:`SweepCheckpoint.next_index` is where the resumed sweep starts.

## Crash safety — atomic tmp + ``os.replace``

:meth:`SweepCheckpointStore.save` writes to a sibling ``*.tmp`` file, ``flush``
+ ``fsync``s it, then ``os.replace``s it over the real path. ``os.replace`` is
atomic on POSIX + Windows, so a crash mid-write leaves EITHER the previous
checkpoint OR the new one fully on disk — never a half-written / truncated JSON
file that would fail to parse on resume (mirrors
:func:`brutex.engine.results_store._atomic_write` +
``brutex.data.parquet_writer``).

## Idempotence + monotonicity

:meth:`SweepCheckpointStore.record` only advances the stored index — a stale or
out-of-order ``record`` of an earlier index is ignored (returns ``False``), so
a re-delivered progress callback can never rewind the checkpoint. Re-recording
the same index is a no-op (also ``False``). This keeps the checkpoint a
monotonic high-water mark.

## Honest envelope (Golden Rule #10)

Every operation is O(1) — a single small-JSON read or atomic write, no scan of
the work already done. The checkpoint stores only an integer index + a little
metadata; it does **not** store the per-combo results (those are the
:class:`~brutex.engine.results_store.ResultsStore`'s job). Resume correctness
therefore assumes the sweep enumerates combos in the **same deterministic
order** every run (which the K-of-N mask enumeration in
:mod:`brutex.engine.combo_mask` guarantees) — the checkpoint is an index into
that fixed order, not a content address.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.models import BruteXError

_LOG = structlog.get_logger("brutex.engine.sweep_checkpoint")

#: Schema version stamped on every checkpoint so a future format change is
#: detectable on load rather than silently mis-parsed.
CHECKPOINT_VERSION: Final[int] = 1

#: Sanity cap on the checkpoint file size :meth:`SweepCheckpointStore.load`
#: will read. A legitimate checkpoint is a ~150-byte JSON object; anything
#: beyond this bound is corruption/tampering and is REFUSED with
#: :class:`SweepCheckpointError` BEFORE the read — never an unbounded
#: ``read_text`` (a multi-GiB planted file → ``MemoryError``) and never a
#: parse of attacker-shaped bytes (a deeply-nested JSON array →
#: ``RecursionError`` escaping every consumer's narrow domain-error catch,
#: refuter-confirmed). 1 MiB leaves ~4 orders of magnitude of headroom.
MAX_CHECKPOINT_BYTES: Final[int] = 1 << 20


class SweepCheckpointError(BruteXError):
    """Raised on a malformed checkpoint payload or a checkpoint I/O failure."""


@dataclass(frozen=True, slots=True)
class SweepCheckpoint:
    """An immutable snapshot of a sweep's resume position.

    Parameters
    ----------
    sweep_id
        Stable identifier of the sweep this checkpoint belongs to (e.g. a
        run-config digest). Non-empty.
    total_combos
        Total combos the sweep will enumerate (``>= 0``). The completion
        denominator.
    last_completed_index
        Zero-based index of the **last fully-completed** combo, or ``-1`` when
        no combo has completed yet (a fresh sweep). Must satisfy
        ``-1 <= last_completed_index < total_combos``.
    updated_at
        Wall-clock seconds (epoch) of the last write.

    Raises
    ------
    SweepCheckpointError
        On an empty ``sweep_id``, a negative ``total_combos``, or a
        ``last_completed_index`` outside ``[-1, total_combos)``.
    """

    sweep_id: str
    total_combos: int
    last_completed_index: int
    updated_at: float

    def __post_init__(self) -> None:
        if not self.sweep_id:
            raise SweepCheckpointError("SweepCheckpoint.sweep_id must be non-empty")
        if self.total_combos < 0:
            raise SweepCheckpointError(
                f"SweepCheckpoint.total_combos must be >= 0, got {self.total_combos}"
            )
        if self.last_completed_index < -1:
            raise SweepCheckpointError(
                f"SweepCheckpoint.last_completed_index must be >= -1, "
                f"got {self.last_completed_index}"
            )
        if self.last_completed_index >= self.total_combos:
            raise SweepCheckpointError(
                f"SweepCheckpoint.last_completed_index ({self.last_completed_index}) must be "
                f"< total_combos ({self.total_combos})"
            )

    @property
    def next_index(self) -> int:
        """The index the resumed sweep should start from (``last + 1``)."""
        return self.last_completed_index + 1

    @property
    def is_complete(self) -> bool:
        """``True`` iff every enumerated combo has completed."""
        return self.next_index >= self.total_combos

    @property
    def completed_count(self) -> int:
        """How many combos have completed so far (``last_completed_index + 1``)."""
        return self.last_completed_index + 1

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable on-disk representation."""
        return {
            "version": CHECKPOINT_VERSION,
            "sweep_id": self.sweep_id,
            "total_combos": self.total_combos,
            "last_completed_index": self.last_completed_index,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> SweepCheckpoint:
        """Rebuild a :class:`SweepCheckpoint` from its on-disk dict.

        Raises
        ------
        SweepCheckpointError
            If the payload is not a JSON object (a valid-JSON non-dict like
            ``[]`` / ``null`` / ``"hello"`` / ``42`` — every corruption class
            must land in the caller's ``SweepCheckpointError`` handling, never
            an ``AttributeError`` that wedges the run), a required field is
            missing, has the wrong type, overflows the int coercion (stdlib
            ``json.loads`` accepts ``1e999`` and the bare ``Infinity`` /
            ``-Infinity`` literals as ``float('inf')``, and ``int(inf)``
            raises ``OverflowError`` — fix round 6, refuter-confirmed: it is
            neither a ``ValueError`` nor a ``TypeError``, so it previously
            escaped raw and wedged every wet run behind one corrupt file), or
            the schema version is unknown.
        """
        if not isinstance(data, dict):
            raise SweepCheckpointError(
                f"Checkpoint payload must be a JSON object, got {type(data).__name__}"
            )
        required = {"version", "sweep_id", "total_combos", "last_completed_index", "updated_at"}
        missing = required - set(data.keys())
        if missing:
            raise SweepCheckpointError(
                f"Checkpoint dict missing required fields: {sorted(missing)}"
            )
        version = data["version"]
        if version != CHECKPOINT_VERSION:
            raise SweepCheckpointError(
                f"Unknown checkpoint schema version {version!r} "
                f"(this build understands version {CHECKPOINT_VERSION})"
            )
        try:
            return cls(
                sweep_id=str(data["sweep_id"]),
                total_combos=int(data["total_combos"]),
                last_completed_index=int(data["last_completed_index"]),
                updated_at=float(data["updated_at"]),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            # OverflowError covers int(float("inf")) — a JSON 1e999/Infinity
            # in an int field (subclasses ArithmeticError, NOT ValueError).
            raise SweepCheckpointError(f"Malformed checkpoint field: {exc}") from exc


class SweepCheckpointStore:
    """Atomic, monotonic, resumable checkpoint backed by a single JSON file.

    Parameters
    ----------
    path
        Destination JSON file (parents created on first :meth:`save`).
    sweep_id
        Identifier stamped on every checkpoint this store writes.
    total_combos
        Total combos the sweep enumerates (``>= 0``).
    clock
        Zero-arg epoch-seconds source for the ``updated_at`` stamp (defaults
        to :func:`time.time`); injected for deterministic tests.

    Raises
    ------
    SweepCheckpointError
        If ``sweep_id`` is empty or ``total_combos`` is negative.
    """

    def __init__(
        self,
        *,
        path: Path,
        sweep_id: str,
        total_combos: int,
        clock: Any = time,
    ) -> None:
        if not sweep_id:
            raise SweepCheckpointError("SweepCheckpointStore.sweep_id must be non-empty")
        if total_combos < 0:
            raise SweepCheckpointError(
                f"SweepCheckpointStore.total_combos must be >= 0, got {total_combos}"
            )
        self._path = path
        self._sweep_id = sweep_id
        self._total_combos = total_combos
        self._clock = clock
        # In-memory high-water mark; -1 == nothing completed yet.
        self._last_index = -1

    @property
    def path(self) -> Path:
        """The configured checkpoint file path."""
        return self._path

    @property
    def last_completed_index(self) -> int:
        """The current in-memory high-water index (``-1`` if none)."""
        return self._last_index

    @property
    def next_index(self) -> int:
        """The index a resume should start from (``last_completed_index + 1``)."""
        return self._last_index + 1

    def load(self) -> SweepCheckpoint | None:
        """Read + adopt the on-disk checkpoint, returning it (or ``None``).

        Returns ``None`` when no checkpoint file exists (a first run). When one
        exists for a **different** ``sweep_id`` it is ignored (returns ``None``)
        — a stale checkpoint from another sweep must never resume this one. On a
        matching checkpoint, the in-memory high-water mark is adopted so
        subsequent :meth:`record` calls continue monotonically.

        Every corruption class maps to :class:`SweepCheckpointError` — never a
        raw parser/OS error that escapes a consumer's narrow domain-error
        catch: an oversized file (> :data:`MAX_CHECKPOINT_BYTES`) is refused
        BEFORE the read, a deeply-nested valid-JSON payload's
        ``RecursionError`` is mapped alongside ``json.JSONDecodeError``, and a
        SAME-``sweep_id`` checkpoint whose ``total_combos`` disagrees with
        this store's is refused (a tampered/inflated total would otherwise
        adopt a ``next_index`` past this store's real total — a silent
        zero-work "complete"; the id derives from the same inputs as the
        total, so a mismatch is corruption by construction).

        Raises
        ------
        SweepCheckpointError
            If the file exists but is unreadable, undecodable (invalid UTF-8 —
            on-disk bitrot / an external truncation, NOT a torn write from this
            atomic writer), oversized, malformed, or carries this ``sweep_id``
            with a mismatched ``total_combos``.
        """
        if not self._path.exists():
            return None
        try:
            size = self._path.stat().st_size
        except OSError as exc:
            raise SweepCheckpointError(f"Failed to stat checkpoint {self._path}: {exc}") from exc
        if size > MAX_CHECKPOINT_BYTES:
            raise SweepCheckpointError(
                f"Checkpoint {self._path} is {size} bytes — beyond the "
                f"{MAX_CHECKPOINT_BYTES}-byte sanity cap for a small-JSON checkpoint; "
                "refusing to read it (corruption/tampering)"
            )
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SweepCheckpointError(f"Failed to read checkpoint {self._path}: {exc}") from exc
        except (UnicodeDecodeError, ValueError) as exc:
            # Invalid UTF-8 bytes on disk. NOT a torn write from THIS writer —
            # ``save`` is tmp→fsync→``os.replace`` (``_atomic_write``), so a
            # kill -9 mid-``save`` leaves the OLD intact file, never a half-file.
            # The real triggers are on-disk bitrot or an EXTERNAL truncation /
            # a non-brutex writer clobbering the path. ``read_text`` then raises
            # ``UnicodeDecodeError`` (a ``ValueError``), which — like the
            # malformed-JSON branch below — must map to the typed error, never
            # escape RAW past a consumer's narrow ``SweepCheckpointError`` catch
            # and fire a false halt alert (Golden Rule #1 / #10).
            raise SweepCheckpointError(
                f"Undecodable checkpoint bytes at {self._path}: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError) as exc:
            # RecursionError: a pathologically nested valid-JSON payload (e.g.
            # 200k nested arrays fits well under the size cap) — parser-level
            # corruption, same contract as malformed JSON (refuter-confirmed).
            raise SweepCheckpointError(f"Malformed checkpoint JSON at {self._path}: {exc}") from exc
        checkpoint = SweepCheckpoint.from_dict(data)
        if checkpoint.sweep_id != self._sweep_id:
            # A checkpoint from a different sweep — never resume from it.
            return None
        if checkpoint.total_combos != self._total_combos:
            # Same sweep_id but a DIFFERENT total: the id is derived from the
            # same inputs as the total (grid identity / backfill plan), so this
            # is corruption or tampering — refuse LOUDLY rather than adopting
            # an index that could no-op the whole run as instantly "complete".
            raise SweepCheckpointError(
                f"Checkpoint {self._path} carries sweep_id {self._sweep_id!r} with "
                f"total_combos={checkpoint.total_combos} but this store enumerates "
                f"{self._total_combos} — corrupt/tampered; refusing to adopt it"
            )
        self._last_index = checkpoint.last_completed_index
        return checkpoint

    def load_or_quarantine(
        self,
        *,
        now: Callable[[], float] = time,
        log: Any = _LOG,
    ) -> SweepCheckpoint | None:
        """Load the checkpoint, or QUARANTINE a corrupt one and start FRESH.

        The resilient resume loader (DEC-SWEEP-ACCEPTANCE-LIFECYCLE-001 / G12):
        a corrupt on-disk checkpoint must never ABORT a multi-day sweep with a
        CRITICAL alert. On a clean/absent checkpoint this is byte-identical to
        :meth:`load` (returns the adopted checkpoint or ``None`` — every
        ``record`` then continues from the adopted high-water mark exactly as
        today). On any :class:`SweepCheckpointError` it:

        1. renames the offending checkpoint FILE to a sibling
           ``<name>.corrupt-<ns>`` (forensic preservation — never unlinked; a
           name collision from a same-nanosecond re-run gets a ``-<counter>``
           suffix so it never clobbers a prior quarantine),
        2. logs a LOUD ``sweep.checkpoint_ignored`` WARNING (the ignore-loudly
           pattern of :func:`brutex.data.greeks_backfill`), and
        3. returns ``None`` so the driver starts fresh from index 0 — exactly
           the no-file path (``load`` raises BEFORE it ever adopts
           ``last_completed_index``, so ``next_index`` reads ``0`` after a
           refused/corrupt load, never a half-adopted resume point).

        Only the checkpoint FILE is quarantined — NEVER the quarantine sidecar
        (``.sweep_quarantine.json``) or the dedup cache. A rename that itself
        fails (``OSError``) is logged ``sweep.checkpoint_quarantine_failed`` and
        STILL returns ``None`` — a rename failure must never re-abort the sweep.
        ``now`` is injectable for deterministic tests.

        Parameters
        ----------
        now
            Zero-arg epoch-seconds source for the ``.corrupt-<ns>`` stamp
            (defaults to :func:`time.time`); injected for deterministic tests.
        log
            The structlog logger for the WARNING (defaults to this module's).

        Returns
        -------
        SweepCheckpoint | None
            The adopted checkpoint (clean load), or ``None`` (no file, a
            different-sweep file, or a corrupt file that was quarantined).
        """
        try:
            return self.load()
        except SweepCheckpointError as exc:
            quarantine_path = self._quarantine_corrupt_checkpoint(now=now, log=log)
            log.warning(
                "sweep.checkpoint_ignored",
                path=str(self._path),
                quarantine_path=(str(quarantine_path) if quarantine_path is not None else None),
                error=str(exc),
                action="fresh start (re-run the deterministic combo order from 0)",
            )
            return None

    def _quarantine_corrupt_checkpoint(
        self,
        *,
        now: Callable[[], float],
        log: Any,
    ) -> Path | None:
        """Rename the corrupt checkpoint FILE aside, returning the sibling path.

        Collision-safe (``-<counter>`` suffix if a same-nanosecond quarantine
        already exists) and rename-``OSError``-tolerant (logs
        ``sweep.checkpoint_quarantine_failed`` and returns ``None``, so the
        caller still starts fresh). Never touches any file other than this
        store's checkpoint path.
        """
        stamp = int(now() * 1e9)
        target = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        counter = 0
        while target.exists():
            counter += 1
            target = self._path.with_name(f"{self._path.name}.corrupt-{stamp}-{counter}")
        try:
            self._path.rename(target)
        except OSError as exc:
            log.warning(
                "sweep.checkpoint_quarantine_failed",
                path=str(self._path),
                target=str(target),
                error=str(exc),
            )
            return None
        return target

    def discard_adoption(self) -> None:
        """Reset the in-memory high-water mark to "nothing completed" (``-1``).

        Un-adopts a checkpoint a prior :meth:`load` adopted, WITHOUT touching
        the on-disk file: a consumer holding OUT-OF-BAND evidence that the
        on-disk resume point is stale (DEC-GREEKS-BULK-REENRICH-001 fix round
        10 — an intervening Greeks-overlay writer signal newer than the
        checkpoint's ``updated_at``) restarts its enumeration from index 0
        while keeping this store's ``sweep_id``/``total_combos`` identity.
        The caller decides what happens to the stale FILE (the backfill
        deletes it); subsequent :meth:`record` calls advance monotonically
        from ``-1`` again, and the first advancing record atomically
        overwrites whatever is on disk. O(1), in-memory only — never raises.
        """
        self._last_index = -1

    def record(self, completed_index: int) -> bool:
        """Advance + persist the high-water mark to *completed_index*.

        Returns ``True`` if the checkpoint advanced (the index was strictly
        greater than the stored high-water mark and was written to disk), or
        ``False`` if the call was a no-op — a stale / re-delivered index ``<=``
        the current mark is ignored so the checkpoint can never rewind
        (idempotent monotonic high-water mark, CLAUDE.md §2 Rule 7).

        Raises
        ------
        SweepCheckpointError
            If ``completed_index`` is outside ``[0, total_combos)``, or the
            atomic write fails.
        """
        if completed_index < 0:
            raise SweepCheckpointError(
                f"record: completed_index must be >= 0, got {completed_index}"
            )
        if completed_index >= self._total_combos:
            raise SweepCheckpointError(
                f"record: completed_index ({completed_index}) must be "
                f"< total_combos ({self._total_combos})"
            )
        if completed_index <= self._last_index:
            return False
        self._last_index = completed_index
        self.save(self._build_checkpoint())
        return True

    def save(self, checkpoint: SweepCheckpoint) -> None:
        """Atomically write *checkpoint* to :attr:`path`.

        Tempfile in the destination directory → ``flush`` → ``fsync`` →
        ``os.replace``, so a crash mid-write never corrupts the resume point.
        The parent-directory ``mkdir`` runs INSIDE the ``OSError`` →
        :class:`SweepCheckpointError` mapping (refuter-confirmed: a stray
        non-directory file at the checkpoint dir raised a raw
        ``FileExistsError`` past every consumer's documented advisory
        ``except SweepCheckpointError`` and aborted the whole run — every
        checkpoint I/O failure must honour the domain-error contract).

        Raises
        ------
        SweepCheckpointError
            If creating the parent directory or writing the file fails.
        """
        content = json.dumps(checkpoint.to_dict(), sort_keys=True).encode("utf-8")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self._path, content)
        except OSError as exc:
            raise SweepCheckpointError(f"Failed to write checkpoint {self._path}: {exc}") from exc

    def _build_checkpoint(self) -> SweepCheckpoint:
        """Materialise a :class:`SweepCheckpoint` from the current store state."""
        return SweepCheckpoint(
            sweep_id=self._sweep_id,
            total_combos=self._total_combos,
            last_completed_index=self._last_index,
            updated_at=float(self._clock()),
        )


def _atomic_write(dest: Path, content: bytes) -> None:
    """Atomically write *content* to *dest* via tempfile + fsync + ``os.replace``.

    Delegates to the COMMON guard-free :func:`brutex.data.atomic_io.atomic_write_simple`
    (the ONE tmp→``fsync``→``os.replace``→cleanup, CLAUDE.md §11,
    DEC-ATOMIC-WRITE-CONSOLIDATE-001): a kill mid-write leaves either the old file or
    the new one — never a partial. Guard-FREE on purpose — a raw ``OSError`` propagates
    unchanged so the caller's ``SweepCheckpointError`` contract is preserved.
    """
    atomic_write_simple(dest, lambda tmp: tmp.write_bytes(content))


__all__ = [
    "CHECKPOINT_VERSION",
    "MAX_CHECKPOINT_BYTES",
    "SweepCheckpoint",
    "SweepCheckpointError",
    "SweepCheckpointStore",
]
