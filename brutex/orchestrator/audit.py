"""Hash-chained append-only audit log — the tamper-evident record of
every material action BruteX takes during an ingest run.

Implements ``NFR-OBS-003`` (hash-chained audit) and the tamper-evident
replay contract from ``REQUIREMENTS.md §15.5 Audit Chain``.

## Storage format

Each log file is a standard newline-delimited JSON (``.jsonl``) file,
one entry per line. Example::

    {"seq": 0, "prev_hash": "0000...0000", "ts_utc": 1712987654.123, "actor": "brutex", "action": "RUN_START", "subject": "run:abc123", "payload": {...}, "entry_hash": "e3f1..."}
    {"seq": 1, "prev_hash": "e3f1...", "ts_utc": 1712987654.456, "actor": "brutex", "action": "PHASE_START", "subject": "phase:A_INSTRUMENTS", "payload": {...}, "entry_hash": "a9d4..."}

## Integrity

Each entry's ``entry_hash`` is::

    sha256(
        seq || prev_hash || ts_utc || actor || action || subject
             || canonical_json(payload)
    )

The chain is ``prev_hash(entry[n]) == entry_hash(entry[n-1])``. The
genesis entry uses 64 zero hex chars for ``prev_hash``. Any tamper
breaks the chain at the tampered entry's index.

## Verification

:func:`verify_chain` reads a log file and replays it, recomputing
every ``entry_hash`` and confirming:

1. ``seq`` starts at 0 and increments by 1
2. ``prev_hash`` of entry ``n`` equals ``entry_hash`` of entry ``n-1``
3. Genesis entry's ``prev_hash`` is all zeros
4. Every recomputed hash matches the stored hash

Any mismatch raises :exc:`AuditChainCorruption` with the offending
seq number.

## Usage

::

    from brutex.orchestrator.audit import AuditWriter

    with AuditWriter.open(Path("data/audit/run-abc123.jsonl")) as log:
        log.append(action="RUN_START", subject="run:abc123", payload={"universe": 223})
        log.append(action="PHASE_START", subject="phase:A_INSTRUMENTS", payload={"target": 223})
        log.append(action="INGEST_CHUNK_DONE", subject="NSE-NIFTY-22Dec25-25000-CE", payload={"bars": 375})
        log.append(action="RUN_COMPLETE", subject="run:abc123", payload={"total": 1_278_432})

    # Later, from brutex status or CI:
    AuditWriter.verify_chain(Path("data/audit/run-abc123.jsonl"))
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from types import TracebackType
from typing import Any, Final, Self

from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: The 64-hex-char all-zero hash used as the genesis entry's ``prev_hash``.
GENESIS_PREV_HASH: Final[str] = "0" * 64


# ────────────────────────────────────────────────────────────────────────
# Exceptions
# ────────────────────────────────────────────────────────────────────────


class AuditError(BruteXError):
    """Base class for audit-log errors."""


class AuditChainCorruption(AuditError):
    """Raised when :func:`verify_chain` finds the chain is broken.

    Attributes
    ----------
    seq
        The ``seq`` number of the first entry that failed verification.
    expected
        The expected value (depends on what broke — ``prev_hash`` or
        ``entry_hash``).
    actual
        The value that was found.
    reason
        Human-readable explanation.
    """

    def __init__(
        self,
        *,
        seq: int,
        expected: str,
        actual: str,
        reason: str,
    ) -> None:
        self.seq = seq
        self.expected = expected
        self.actual = actual
        self.reason = reason
        super().__init__(
            f"Audit chain corruption at seq={seq}: {reason}. Expected {expected!r}, got {actual!r}."
        )


class AuditWriteError(AuditError):
    """Raised on any file I/O failure in the writer."""


class AuditPayloadError(AuditError):
    """Raised when a payload cannot be serialised as RFC 8259 JSON.

    The most common cause is a ``float`` that is ``NaN`` / ``Infinity`` /
    ``-Infinity`` — Python's :func:`json.dumps` emits the bare tokens
    ``NaN`` / ``Infinity`` / ``-Infinity`` by default, which are **not**
    valid JSON (RFC 8259 §6 forbids them) and which a strict reader
    rejects. Rather than write a non-RFC, non-reproducible record into a
    tamper-evident chain, the writer refuses the append with this error so
    the caller can sanitise the value at the source. See
    :func:`_canonical_json`.
    """


# ────────────────────────────────────────────────────────────────────────
# Canonical JSON helper
# ────────────────────────────────────────────────────────────────────────


def _canonical_json(obj: Any) -> str:
    """Return a stable, hash-friendly, RFC 8259-valid JSON representation.

    * Keys sorted lexicographically
    * No whitespace between tokens (compact)
    * UTF-8 ensure_ascii=False so non-ASCII text hashes the same
      across platforms
    * ``allow_nan=False`` so a ``NaN`` / ``Infinity`` / ``-Infinity``
      value raises instead of emitting the bare non-RFC tokens
      ``NaN`` / ``Infinity`` / ``-Infinity`` (which Python's default
      ``allow_nan=True`` would write — invalid JSON a strict reader
      rejects, and which would also poison the entry hash with a value
      that cannot be round-tripped). The :class:`ValueError` is mapped
      to :class:`AuditPayloadError` so callers get a clear domain error.

    Raises
    ------
    AuditPayloadError
        If *obj* contains a non-finite float (``NaN`` / ``Inf``) and so
        cannot be represented as RFC-valid JSON.
    """
    try:
        return json.dumps(
            obj,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except ValueError as exc:
        raise AuditPayloadError(
            f"Audit payload is not RFC 8259-serialisable (NaN/Infinity not permitted): {exc}"
        ) from exc


# ────────────────────────────────────────────────────────────────────────
# AuditEntry dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One row in the audit log.

    Immutable by design. Created by :class:`AuditWriter` and
    reconstructed by :func:`verify_chain`. Direct instantiation is
    intended for tests only.
    """

    seq: int
    prev_hash: str
    ts_utc: float  # seconds since epoch (float for microsecond precision)
    actor: str
    action: str
    subject: str
    payload: dict[str, Any]
    entry_hash: str = field(default="")  # populated by compute_hash

    def compute_hash(self) -> str:
        """Compute what the ``entry_hash`` should be based on all other fields.

        Does NOT mutate ``self`` — returns a new hex string the caller
        can compare.
        """
        h = hashlib.sha256()
        h.update(str(self.seq).encode("utf-8"))
        h.update(b"|")
        h.update(self.prev_hash.encode("utf-8"))
        h.update(b"|")
        h.update(str(self.ts_utc).encode("utf-8"))
        h.update(b"|")
        h.update(self.actor.encode("utf-8"))
        h.update(b"|")
        h.update(self.action.encode("utf-8"))
        h.update(b"|")
        h.update(self.subject.encode("utf-8"))
        h.update(b"|")
        h.update(_canonical_json(self.payload).encode("utf-8"))
        return h.hexdigest()

    def to_json_line(self) -> str:
        """Serialise to a single JSONL line (no trailing newline)."""
        return _canonical_json(
            {
                "seq": self.seq,
                "prev_hash": self.prev_hash,
                "ts_utc": self.ts_utc,
                "actor": self.actor,
                "action": self.action,
                "subject": self.subject,
                "payload": self.payload,
                "entry_hash": self.entry_hash,
            }
        )

    @classmethod
    def from_json_line(cls, line: str) -> AuditEntry:
        """Parse an :class:`AuditEntry` from a single JSONL line.

        Raises
        ------
        AuditError
            If the line is malformed or missing required fields.
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError(f"Malformed audit line: {exc}") from exc

        required = {
            "seq",
            "prev_hash",
            "ts_utc",
            "actor",
            "action",
            "subject",
            "payload",
            "entry_hash",
        }
        missing = required - set(data.keys())
        if missing:
            raise AuditError(f"Audit line missing required fields: {sorted(missing)}")

        return cls(
            seq=int(data["seq"]),
            prev_hash=str(data["prev_hash"]),
            ts_utc=float(data["ts_utc"]),
            actor=str(data["actor"]),
            action=str(data["action"]),
            subject=str(data["subject"]),
            payload=dict(data["payload"]),
            entry_hash=str(data["entry_hash"]),
        )


# ────────────────────────────────────────────────────────────────────────
# AuditWriter — append-only, flush-per-entry
# ────────────────────────────────────────────────────────────────────────


class AuditWriter:
    """Append-only hash-chained audit log writer.

    Use as a context manager::

        with AuditWriter.open(path) as log:
            log.append(action="RUN_START", subject="run:...", payload={})

    Entries are flushed AND ``os.fsync``-ed to disk on every ``append``
    call (no buffering, DEC-AUDIT-FSYNC-DURABILITY-001) so a crash or
    SIGKILL between calls leaves the log in a consistent state — the last
    committed write is durably persisted (not merely in the OS page
    cache), and a torn / partial tail is the only thing a crash can leave,
    which the chain reader detects (an incomplete final line is skipped /
    surfaced as an :class:`AuditError`, never a corrupt committed prefix).

    **Not thread-safe.** The caller must serialize ``append`` calls
    externally (either via a single-writer design or a lock). The
    worker pool's design uses one writer per run on a single supervisor
    actor, which is inherently serialized.
    """

    def __init__(
        self,
        *,
        path: Path,
        actor: str = "brutex",
        initial_seq: int = 0,
        initial_prev_hash: str = GENESIS_PREV_HASH,
    ) -> None:
        self._path = path
        self._actor = actor
        self._seq = initial_seq
        self._prev_hash = initial_prev_hash
        self._fp: Any = None  # open file handle; typed as Any for os.fdopen compat

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        actor: str = "brutex",
        resume: bool = False,
    ) -> AuditWriter:
        """Open (or create) an audit log at *path*.

        Parameters
        ----------
        path
            Target file path. Parent directory is created if missing.
        actor
            Actor name stamped on every entry (defaults to ``"brutex"``).
        resume
            If True, read the existing file and continue from the last
            entry's seq+1 / entry_hash. If False, the file must NOT
            exist (otherwise :exc:`AuditWriteError` is raised — we do
            not overwrite an existing audit log).

        Returns
        -------
        AuditWriter
            Already opened for writing; caller should use it as a
            context manager.
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        if resume and path.exists():
            # Read-replay to catch up to the tail
            entries = list(_read_entries(path))
            if entries:
                last = entries[-1]
                writer = cls(
                    path=path,
                    actor=actor,
                    initial_seq=last.seq + 1,
                    initial_prev_hash=last.entry_hash,
                )
            else:
                writer = cls(path=path, actor=actor)
            # Open in append mode
            # DEC-P0-017 (PR #39): classmethod factory assigns the private
            # _fp on the just-constructed writer; writer owns the fd for
            # its lifetime (companion to noqa: SIM115 below).
            writer._fp = open(  # noqa: SIM115 — writer owns the fd  # pyright: ignore[reportPrivateUsage]
                path, "a", encoding="utf-8"
            )
            return writer

        if path.exists():
            raise AuditWriteError(
                f"Audit log {path} already exists and resume=False. "
                f"Use resume=True to continue an existing chain or delete "
                f"the file if you intend to start over."
            )

        writer = cls(path=path, actor=actor)
        # DEC-P0-017 (PR #39): same factory pattern as resume branch above.
        writer._fp = open(  # noqa: SIM115 — writer owns the fd  # pyright: ignore[reportPrivateUsage]
            path, "x", encoding="utf-8"
        )
        return writer

    def __enter__(self) -> Self:
        if self._fp is None:
            # Not opened via .open() — caller used the raw constructor.
            self._fp = open(self._path, "a", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying file handle."""
        if self._fp is not None:
            self._fp.flush()
            # Some filesystems / platforms (e.g., /tmp on macOS
            # sandbox) don't support fsync on append-only files.
            # Best-effort — the flush above still hits the kernel.
            with contextlib.suppress(OSError):
                os.fsync(self._fp.fileno())
            self._fp.close()
            self._fp = None

    def append(
        self,
        *,
        action: str,
        subject: str,
        payload: dict[str, Any] | None = None,
        ts_utc: float | None = None,
    ) -> AuditEntry:
        """Append one entry to the chain and flush to disk.

        Parameters
        ----------
        action
            Short uppercase identifier (e.g., ``RUN_START``,
            ``PHASE_START``, ``INGEST_CHUNK_DONE``,
            ``QUARANTINE_NEW``, ``RUN_COMPLETE``).
        subject
            Free-form subject the action acts on (e.g.,
            ``"run:abc123"``, ``"NSE-NIFTY-22Dec25-25000-CE"``).
        payload
            Action-specific data. Must be JSON-serializable.
            Defaults to empty dict if None.
        ts_utc
            Override timestamp (tests only). Defaults to
            ``time.time()``.

        Returns
        -------
        AuditEntry
            The entry that was written, including its computed
            ``entry_hash``.
        """
        if self._fp is None or getattr(self._fp, "closed", True):
            raise AuditWriteError(f"Cannot append to closed audit writer at {self._path}")

        stamped_payload = dict(payload) if payload else {}
        entry = AuditEntry(
            seq=self._seq,
            prev_hash=self._prev_hash,
            ts_utc=ts_utc if ts_utc is not None else time(),
            actor=self._actor,
            action=action,
            subject=subject,
            payload=stamped_payload,
        )
        computed_hash = entry.compute_hash()
        # Rebuild the entry with the real hash (dataclass is frozen)
        entry = AuditEntry(
            seq=entry.seq,
            prev_hash=entry.prev_hash,
            ts_utc=entry.ts_utc,
            actor=entry.actor,
            action=entry.action,
            subject=entry.subject,
            payload=entry.payload,
            entry_hash=computed_hash,
        )

        # to_json_line() may raise AuditPayloadError (NaN/Inf) BEFORE any
        # write — the entry is rejected without touching the file, so the
        # on-disk chain is never advanced past a non-RFC record.
        line = entry.to_json_line() + "\n"
        try:
            self._fp.write(line)
            self._fp.flush()
            # fsync the record to stable storage BEFORE the action is
            # considered committed (DEC-AUDIT-FSYNC-DURABILITY-001):
            # `flush()` only pushes the bytes into the OS page cache, so a
            # power loss / host crash after flush but before the kernel
            # writes back could lose the last record and break the chain.
            # ONE fsync per append (O(1) per record, NOT per byte) — same
            # best-effort guard as `close()`: a few filesystems / sandboxes
            # reject fsync on an append-only fd, where the flush above has
            # still reached the kernel.
            with contextlib.suppress(OSError):
                os.fsync(self._fp.fileno())
        except OSError as exc:
            raise AuditWriteError(
                f"Failed to write audit entry seq={entry.seq} to {self._path}: {exc}"
            ) from exc

        # Advance the chain
        self._seq += 1
        self._prev_hash = computed_hash
        return entry


# ────────────────────────────────────────────────────────────────────────
# verify_chain — standalone function called by brutex audit verify
# ────────────────────────────────────────────────────────────────────────


def _read_entries(path: Path) -> list[AuditEntry]:
    """Read every entry from *path* without verifying the chain."""
    if not path.exists():
        raise AuditError(f"Audit log not found: {path}")
    entries: list[AuditEntry] = []
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            if not line:
                continue
            try:
                entries.append(AuditEntry.from_json_line(line))
            except AuditError as exc:
                raise AuditError(f"{path}:{line_no}: {exc}") from exc
    return entries


def verify_chain(path: Path) -> int:
    """Read *path* and verify the hash chain.

    Returns the number of entries verified. Raises
    :exc:`AuditChainCorruption` on the first mismatch with details
    about the offending seq and what broke.

    Parameters
    ----------
    path
        Path to the audit log file.

    Raises
    ------
    AuditError
        If the file is missing, unreadable, or contains a malformed line.
    AuditChainCorruption
        If the chain's integrity is broken at any point.

    Returns
    -------
    int
        Number of entries successfully verified. A value of ``0`` means
        the file exists but is empty (trivially valid).
    """
    entries = _read_entries(path)

    prev_hash = GENESIS_PREV_HASH

    for expected_seq, entry in enumerate(entries):
        # 1. seq is a strictly increasing 0-based counter
        if entry.seq != expected_seq:
            raise AuditChainCorruption(
                seq=entry.seq,
                expected=str(expected_seq),
                actual=str(entry.seq),
                reason="seq number out of order",
            )
        # 2. prev_hash matches the previous entry's entry_hash
        if entry.prev_hash != prev_hash:
            raise AuditChainCorruption(
                seq=entry.seq,
                expected=prev_hash,
                actual=entry.prev_hash,
                reason="prev_hash does not match previous entry's entry_hash",
            )
        # 3. entry_hash is correct for the entry's contents
        recomputed = entry.compute_hash()
        if entry.entry_hash != recomputed:
            raise AuditChainCorruption(
                seq=entry.seq,
                expected=recomputed,
                actual=entry.entry_hash,
                reason="entry_hash does not match recomputed hash (content tampered)",
            )
        prev_hash = entry.entry_hash

    return len(entries)


def read_entries(path: Path) -> list[AuditEntry]:
    """Public wrapper around :func:`_read_entries`.

    Use when you want the entries without verifying the chain
    (e.g., the dashboard tail view). Use :func:`verify_chain` when
    you need the integrity guarantee.
    """
    return _read_entries(path)


__all__ = [
    "GENESIS_PREV_HASH",
    "AuditChainCorruption",
    "AuditEntry",
    "AuditError",
    "AuditPayloadError",
    "AuditWriteError",
    "AuditWriter",
    "read_entries",
    "verify_chain",
]
