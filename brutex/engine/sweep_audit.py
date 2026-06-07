"""Per-combo hash-chained audit log for the brute-force sweep (DEC-SWEEP-REALTIME-GUARANTEE-001).

The tamper-evident record of every material action a sweep takes — one
append-only, **BLAKE3** hash-chained ``.jsonl`` line per combo evaluation
(plus the sweep lifecycle markers). This is the sweep-side companion to the
ingest-side :mod:`brutex.orchestrator.audit`: same proven chain design (genesis
prev-hash, ``prev_hash[n] == entry_hash[n-1]``, recompute-and-compare verify),
but BLAKE3 throughout (CLAUDE.md Golden Rule #4 — BLAKE3 is the BruteX hash;
Golden Rule #8 — every material action appends one hash-chained JSONL line and
breaking the chain fails CI).

It is a **NEW STANDALONE** module — it does not import, edit, or wrap
:mod:`brutex.orchestrator.audit` (that log is keyed to the ingest pipeline's
SHA256 chain and stays untouched). Wiring it into ``engine/sweep_driver.py`` is
a separate Wave-2 follow-up; this module only provides the primitive.

## Storage format

One newline-delimited JSON object per line, e.g.::

    {"seq": 0, "prev_hash": "00…00", "ts_utc": 1.7e9, "actor": "sweep", "action": "SWEEP_START", "subject": "sweep:abc", "payload": {"total_combos": 22100}, "entry_hash": "9f…"}
    {"seq": 1, "prev_hash": "9f…", "ts_utc": 1.7e9, "actor": "sweep", "action": "COMBO_DONE", "subject": "combo:5e…", "payload": {"mask": 7, "trades": 42}, "entry_hash": "a1…"}

## Integrity

Each entry's ``entry_hash`` is::

    blake3(
        seq | prev_hash | ts_utc | actor | action | subject | canonical_json(payload)
    ).hexdigest()

with ``|`` (a single ASCII pipe) between fields. The chain invariant is
``prev_hash(entry[n]) == entry_hash(entry[n-1])``; the genesis entry uses 64
zero hex chars. Any tamper — a flipped payload byte, a reordered line, a
deleted entry — breaks the chain at the offending seq and
:func:`verify_chain` raises :exc:`SweepAuditChainCorruption` with the seq.

## Why wheel-free BLAKE3

The combo-identity hash in :func:`brutex.strategies.combo.combo_hash` uses the
compiled ``fno_math.blake3_hash`` Rust kernel. This assurance-layer module is
deliberately **wheel-free** (it must run + be 100%-tested without the Rust
wheel), so it uses the pure-pip ``blake3`` package — the same BLAKE3 algorithm,
a different (no-local-build) provider. Both produce the identical 32-byte
digest, so a future wiring PR can cross-check the two if needed.

## Durability

Every :meth:`SweepAuditWriter.append` writes the line and ``flush()``es
immediately (no buffering), so a crash or ``SIGKILL`` between appends leaves
the log with the last write either fully present or fully absent — never half a
line. ``close()`` additionally best-effort ``fsync``s.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from types import TracebackType
from typing import Any, Final, Self

import blake3

from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: The 64-hex-char all-zero hash used as the genesis entry's ``prev_hash``.
GENESIS_PREV_HASH: Final[str] = "0" * 64

#: Default actor stamped on every entry.
DEFAULT_ACTOR: Final[str] = "sweep"

#: Length in hex chars of a BLAKE3-256 digest (32 bytes → 64 hex chars).
BLAKE3_HEX_LEN: Final[int] = 64


# ────────────────────────────────────────────────────────────────────────
# Exceptions
# ────────────────────────────────────────────────────────────────────────


class SweepAuditError(BruteXError):
    """Base class for sweep-audit-log errors."""


class SweepAuditChainCorruption(SweepAuditError):
    """Raised when :func:`verify_chain` finds the chain is broken.

    Attributes
    ----------
    seq
        The ``seq`` number of the first entry that failed verification.
    expected
        The expected value (a ``prev_hash`` or a recomputed ``entry_hash``).
    actual
        The value that was found instead.
    reason
        Human-readable explanation of what broke.
    """

    def __init__(self, *, seq: int, expected: str, actual: str, reason: str) -> None:
        self.seq = seq
        self.expected = expected
        self.actual = actual
        self.reason = reason
        super().__init__(
            f"Sweep audit chain corruption at seq={seq}: {reason}. "
            f"Expected {expected!r}, got {actual!r}."
        )


class SweepAuditWriteError(SweepAuditError):
    """Raised on any file-I/O failure in :class:`SweepAuditWriter`."""


# ────────────────────────────────────────────────────────────────────────
# Canonical JSON helper
# ────────────────────────────────────────────────────────────────────────


def _canonical_json(obj: Any) -> str:
    """Return a stable, hash-friendly JSON representation.

    Keys sorted, compact separators, ``ensure_ascii=False`` so non-ASCII text
    hashes identically across platforms — the same canonicalisation
    :mod:`brutex.orchestrator.audit` uses, so the two chains stay comparable.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ────────────────────────────────────────────────────────────────────────
# SweepAuditEntry
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SweepAuditEntry:
    """One immutable row in the sweep audit log.

    Created by :class:`SweepAuditWriter` and reconstructed by
    :func:`verify_chain`. Direct instantiation is intended for tests.
    """

    seq: int
    prev_hash: str
    ts_utc: float
    actor: str
    action: str
    subject: str
    payload: dict[str, Any]
    entry_hash: str = field(default="")

    def compute_hash(self) -> str:
        """Return the BLAKE3 ``entry_hash`` for this entry's content.

        Does NOT mutate ``self`` — returns a fresh 64-char hex string the
        caller can compare against the stored :attr:`entry_hash`.
        """
        h = blake3.blake3()
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
    def from_json_line(cls, line: str) -> SweepAuditEntry:
        """Parse a :class:`SweepAuditEntry` from one JSONL line.

        Raises
        ------
        SweepAuditError
            If the line is malformed JSON or missing a required field.
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SweepAuditError(f"Malformed sweep-audit line: {exc}") from exc

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
            raise SweepAuditError(f"Sweep-audit line missing required fields: {sorted(missing)}")

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
# SweepAuditWriter — append-only, flush-per-entry
# ────────────────────────────────────────────────────────────────────────


class SweepAuditWriter:
    """Append-only BLAKE3 hash-chained sweep audit log writer.

    Use as a context manager::

        with SweepAuditWriter.open(path) as log:
            log.append(action="SWEEP_START", subject="sweep:abc", payload={"total": 22100})
            log.append(action="COMBO_DONE", subject="combo:5e…", payload={"mask": 7})

    **Not thread-safe** — like :class:`brutex.orchestrator.audit.AuditWriter`,
    the caller serialises appends (one writer on a single supervisor thread).
    """

    def __init__(
        self,
        *,
        path: Path,
        actor: str = DEFAULT_ACTOR,
        initial_seq: int = 0,
        initial_prev_hash: str = GENESIS_PREV_HASH,
    ) -> None:
        self._path = path
        self._actor = actor
        self._seq = initial_seq
        self._prev_hash = initial_prev_hash
        self._fp: Any = None

    @classmethod
    def open(
        cls, path: Path, *, actor: str = DEFAULT_ACTOR, resume: bool = False
    ) -> SweepAuditWriter:
        """Open (or create) a sweep audit log at *path*.

        Parameters
        ----------
        path
            Target file. The parent directory is created if missing.
        actor
            Actor name stamped on every entry (defaults to ``"sweep"``).
        resume
            If ``True`` and *path* exists, continue the existing chain from
            the last entry's ``seq + 1`` / ``entry_hash``. If ``False``, the
            file must NOT already exist (otherwise
            :exc:`SweepAuditWriteError` — an existing chain is never silently
            overwritten).
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        if resume and path.exists():
            entries = _read_entries(path)
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
            writer._fp = open(path, "a", encoding="utf-8")  # noqa: SIM115 — writer owns the fd
            return writer

        if path.exists():
            raise SweepAuditWriteError(
                f"Sweep audit log {path} already exists and resume=False. "
                f"Use resume=True to continue the chain or delete the file to start over."
            )

        writer = cls(path=path, actor=actor)
        writer._fp = open(path, "x", encoding="utf-8")  # noqa: SIM115 — writer owns the fd
        return writer

    def __enter__(self) -> Self:
        if self._fp is None:
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
        """Flush + best-effort fsync + close the underlying file handle."""
        if self._fp is not None:
            self._fp.flush()
            with contextlib.suppress(OSError):
                os.fsync(self._fp.fileno())
            self._fp.close()
            self._fp = None

    @property
    def seq(self) -> int:
        """The seq number the next :meth:`append` will write."""
        return self._seq

    @property
    def prev_hash(self) -> str:
        """The ``entry_hash`` of the last written entry (genesis if none)."""
        return self._prev_hash

    def append(
        self,
        *,
        action: str,
        subject: str,
        payload: dict[str, Any] | None = None,
        ts_utc: float | None = None,
    ) -> SweepAuditEntry:
        """Append one entry to the chain and flush to disk.

        Parameters
        ----------
        action
            Short uppercase identifier (e.g. ``SWEEP_START``, ``COMBO_DONE``,
            ``GATE_SURVIVOR``, ``COMBO_SKIPPED_CACHE``, ``SWEEP_COMPLETE``).
        subject
            The thing the action acts on (e.g. ``"sweep:abc"`` or
            ``"combo:<digest>"``).
        payload
            JSON-serialisable action data (defaults to an empty dict).
        ts_utc
            Override timestamp (tests only); defaults to ``time.time()``.

        Returns
        -------
        SweepAuditEntry
            The written entry, including its computed ``entry_hash``.

        Raises
        ------
        SweepAuditWriteError
            If the writer is closed or the disk write fails.
        """
        if self._fp is None or getattr(self._fp, "closed", True):
            raise SweepAuditWriteError(
                f"Cannot append to a closed sweep audit writer at {self._path}"
            )

        entry = SweepAuditEntry(
            seq=self._seq,
            prev_hash=self._prev_hash,
            ts_utc=ts_utc if ts_utc is not None else time(),
            actor=self._actor,
            action=action,
            subject=subject,
            payload=dict(payload) if payload else {},
        )
        computed_hash = entry.compute_hash()
        entry = SweepAuditEntry(
            seq=entry.seq,
            prev_hash=entry.prev_hash,
            ts_utc=entry.ts_utc,
            actor=entry.actor,
            action=entry.action,
            subject=entry.subject,
            payload=entry.payload,
            entry_hash=computed_hash,
        )

        line = entry.to_json_line() + "\n"
        try:
            self._fp.write(line)
            self._fp.flush()
        except OSError as exc:
            raise SweepAuditWriteError(
                f"Failed to write sweep-audit entry seq={entry.seq} to {self._path}: {exc}"
            ) from exc

        self._seq += 1
        self._prev_hash = computed_hash
        return entry


# ────────────────────────────────────────────────────────────────────────
# Read + verify
# ────────────────────────────────────────────────────────────────────────


def _read_entries(path: Path) -> list[SweepAuditEntry]:
    """Read every entry from *path* without verifying the chain."""
    if not path.exists():
        raise SweepAuditError(f"Sweep audit log not found: {path}")
    entries: list[SweepAuditEntry] = []
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            if not line:
                continue
            try:
                entries.append(SweepAuditEntry.from_json_line(line))
            except SweepAuditError as exc:
                raise SweepAuditError(f"{path}:{line_no}: {exc}") from exc
    return entries


def read_entries(path: Path) -> list[SweepAuditEntry]:
    """Return all entries in *path* WITHOUT verifying the chain.

    Use for a quick tail / dashboard view; use :func:`verify_chain` when you
    need the integrity guarantee.
    """
    return _read_entries(path)


def verify_chain(path: Path) -> int:
    """Read *path* and verify the BLAKE3 hash chain end-to-end.

    Returns the number of entries verified. ``0`` means the file exists but is
    empty (trivially valid).

    Raises
    ------
    SweepAuditError
        If the file is missing, unreadable, or has a malformed line.
    SweepAuditChainCorruption
        On the first integrity break, carrying the offending ``seq``.
    """
    entries = _read_entries(path)
    prev_hash = GENESIS_PREV_HASH

    for expected_seq, entry in enumerate(entries):
        if entry.seq != expected_seq:
            raise SweepAuditChainCorruption(
                seq=entry.seq,
                expected=str(expected_seq),
                actual=str(entry.seq),
                reason="seq number out of order",
            )
        if entry.prev_hash != prev_hash:
            raise SweepAuditChainCorruption(
                seq=entry.seq,
                expected=prev_hash,
                actual=entry.prev_hash,
                reason="prev_hash does not match previous entry's entry_hash",
            )
        recomputed = entry.compute_hash()
        if entry.entry_hash != recomputed:
            raise SweepAuditChainCorruption(
                seq=entry.seq,
                expected=recomputed,
                actual=entry.entry_hash,
                reason="entry_hash does not match recomputed hash (content tampered)",
            )
        prev_hash = entry.entry_hash

    return len(entries)


__all__ = [
    "BLAKE3_HEX_LEN",
    "DEFAULT_ACTOR",
    "GENESIS_PREV_HASH",
    "SweepAuditChainCorruption",
    "SweepAuditEntry",
    "SweepAuditError",
    "SweepAuditWriteError",
    "SweepAuditWriter",
    "read_entries",
    "verify_chain",
]
