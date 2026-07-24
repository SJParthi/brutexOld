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
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from types import TracebackType
from typing import Any, BinaryIO, Final, Self

import blake3

from brutex.engine.sweep_ops_rust import (
    routed_audit_entry_hash,
    routed_chain_verify_step,
    sweep_ops_rust_enabled,
)
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


def _entry_hash_python(
    seq_str: str,
    prev_hash: str,
    ts_str: str,
    actor: str,
    action: str,
    subject: str,
    payload_json: str,
) -> str:
    """The verbatim BLAKE3 entry-hash law over the PRE-RENDERED field strings.

    The LIVE parity oracle for the DEC-RUST-SWEEP-OPS-001 routed law
    (``routed_audit_entry_hash``): :meth:`SweepAuditEntry.compute_hash`'s
    hashing body, extracted with the three rendered strings (``str(seq)`` /
    ``str(ts_utc)`` / ``_canonical_json(payload)``) hoisted to the caller —
    byte-identical, since the hasher consumed exactly those encodings.
    """
    h = blake3.blake3()
    h.update(seq_str.encode("utf-8"))
    h.update(b"|")
    h.update(prev_hash.encode("utf-8"))
    h.update(b"|")
    h.update(ts_str.encode("utf-8"))
    h.update(b"|")
    h.update(actor.encode("utf-8"))
    h.update(b"|")
    h.update(action.encode("utf-8"))
    h.update(b"|")
    h.update(subject.encode("utf-8"))
    h.update(b"|")
    h.update(payload_json.encode("utf-8"))
    # str(...) — the twin_detect.py idiom: the isolated pre-commit mypy env
    # lacks the blake3 wheel, so hexdigest() reads Any there (no-any-return).
    return str(h.hexdigest())


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

        Python renders every field string (``str(seq)``, the float-repr
        ``str(ts_utc)``, the canonical payload JSON — the repr laws stay
        Python BY LAW) and the hashing law routes through the default-ON
        DEC-RUST-SWEEP-OPS-001 seam with :func:`_entry_hash_python` as the
        LIVE oracle (flag OFF = the verbatim Python hash, byte-identical).
        """
        seq_str = str(self.seq)
        ts_str = str(self.ts_utc)
        payload_json = _canonical_json(self.payload)
        if sweep_ops_rust_enabled():
            return routed_audit_entry_hash(
                seq_str,
                self.prev_hash,
                ts_str,
                self.actor,
                self.action,
                self.subject,
                payload_json,
                python_law=lambda: _entry_hash_python(
                    seq_str,
                    self.prev_hash,
                    ts_str,
                    self.actor,
                    self.action,
                    self.subject,
                    payload_json,
                ),
            )
        return _entry_hash_python(
            seq_str,
            self.prev_hash,
            ts_str,
            self.actor,
            self.action,
            self.subject,
            payload_json,
        )

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
            # Recover the tail with an O(1) reverse read instead of loading the
            # WHOLE log into memory (DEC-AUDIT-RESUME-TAILREAD-001): the resume
            # path only ever needed the LAST committed entry's seq + entry_hash,
            # so `_read_tail_entry` seeks to the end and parses just that final
            # committed line. A torn final line (an interrupted append from a
            # crash / SIGKILL mid-write) is tolerated + skipped; if present we
            # truncate the file to its last committed (newline-terminated) byte
            # BEFORE re-opening for append, so the resumed chain continues from
            # the last complete hash-valid entry on pristine bytes
            # (DEC-AUDIT-TORN-TAIL-001). Real corruption of the last committed
            # line still raises out of `_read_tail_entry`. Full-chain integrity
            # verification remains the SEPARATE `verify_chain` contract. This
            # mirrors the ingest-side fix in brutex.orchestrator.audit (the two
            # chains are deliberately STANDALONE — SHA256 vs BLAKE3 — so the
            # logic is duplicated here, not imported).
            last, torn_tail = _read_tail_entry(path)
            if torn_tail:
                _truncate_torn_tail(path)
            if last is not None:
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


def _iter_committed_entries(path: Path) -> Iterator[SweepAuditEntry]:
    """Generator body of :func:`iter_entries` — one parsed entry per committed line.

    Applies the DEC-AUDIT-TORN-TAIL-001 discriminator line by line (see
    :func:`iter_entries` for the full contract): an unterminated FINAL line is
    an uncommitted torn tail and is skipped, a blank committed line is benign
    filler, and a malformed committed (newline-terminated) line is REAL
    corruption and raises :class:`SweepAuditError` naming ``path:line_no``.
    Peak memory is O(1) — one raw line + one parsed entry at a time, never the
    whole log (DEC-SWEEP-SCALE-OPERABILITY-001).
    """
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            if not raw.endswith("\n"):
                # Unterminated final line == interrupted append == torn tail.
                # Skip it (committed or not, it is uncommitted); only the
                # FILE's final line can ever lack the "\n" — the writer
                # appends `line + "\n"` in one write call.
                continue
            line = raw.rstrip("\n")
            if not line:
                # A blank committed (newline-terminated) line is benign filler.
                continue
            try:
                yield SweepAuditEntry.from_json_line(line)
            except SweepAuditError as exc:
                raise SweepAuditError(f"{path}:{line_no}: {exc}") from exc


def iter_entries(path: Path) -> Iterator[SweepAuditEntry]:
    """Stream every committed entry from *path* WITHOUT verifying the chain.

    The O(1)-memory streaming primitive (DEC-SWEEP-SCALE-OPERABILITY-001) behind
    :func:`read_entries` and :func:`verify_chain`: entries are parsed one line
    at a time, so a multi-GB sweep log is scanned without ever materialising
    the whole chain into a list.

    Torn-tail discriminator (hash-chain-safe)
    -----------------------------------------
    The writer always appends ``line + "\\n"`` in one :py:meth:`write` call, so
    a committed record is **defined** as a line terminated by ``"\\n"``. A crash
    mid-append can therefore only ever leave its damage on the FINAL line and
    only when that line is missing its trailing newline. That newline-less
    final line — whether its JSON is partial or even complete-but-unterminated
    — is an UNCOMMITTED torn tail and is skipped (DEC-AUDIT-TORN-TAIL-001).
    Everything that IS newline-terminated is a committed record and is parsed
    strictly: a malformed line that carries a trailing newline is REAL
    corruption and still raises. Mid-file integrity is NOT weakened — only an
    unterminated tail is forgiven, and the hash chain over the committed
    prefix is still verified by :func:`verify_chain`.

    Parameters
    ----------
    path
        The sweep audit ``.jsonl`` file.

    Returns
    -------
    Iterator[SweepAuditEntry]
        Lazily-parsed committed entries in file order.

    Raises
    ------
    SweepAuditError
        Immediately (at call time) if the file is missing; during iteration if
        any committed (newline-terminated) line is malformed / missing
        required fields.
    """
    if not path.exists():
        raise SweepAuditError(f"Sweep audit log not found: {path}")
    return _iter_committed_entries(path)


def _has_torn_tail(path: Path) -> bool:
    """Return ``True`` iff *path* ends in an unterminated (torn) final line — O(1).

    The same predicate :func:`_read_tail_entry` computes inline (the file does
    not end in ``"\\n"``): an unterminated final line is an interrupted append
    (DEC-AUDIT-TORN-TAIL-001). An empty file has no lines, so it is never torn.
    One ``seek`` + a single-byte read — never the whole log.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return False
        f.seek(size - 1)
        return f.read(1) != b"\n"


def _read_entries_with_torn_tail(path: Path) -> tuple[list[SweepAuditEntry], bool]:
    """Read every entry from *path*, tolerating a single torn final line.

    Returns the parsed entries plus a flag that is ``True`` when the file's
    LAST line was a **torn tail** — an interrupted append left behind by a
    crash / ``SIGKILL`` mid-write (DEC-AUDIT-TORN-TAIL-001). Re-expressed over
    the streaming :func:`iter_entries` + the O(1) :func:`_has_torn_tail`
    predicate (DEC-SWEEP-SCALE-OPERABILITY-001) — the discriminator lives in ONE
    place (CLAUDE.md §11 COMMON); the list materialisation here is this
    variant's deliberate contract.

    This is the deliberate STANDALONE twin of
    :func:`brutex.orchestrator.audit._read_entries_with_torn_tail` (that chain
    is SHA256, this one BLAKE3 — they do not import one another).

    Raises
    ------
    SweepAuditError
        If the file is missing, or any committed (newline-terminated) line is
        malformed / missing required fields.
    """
    entries = list(iter_entries(path))
    return entries, _has_torn_tail(path)


#: Reverse-read chunk size (bytes) for :func:`_read_tail_entry` +
#: :func:`_truncate_torn_tail`. Read the file end backwards this many bytes at a
#: time until the last committed line (resume) / last newline (truncate) is
#: isolated — so BOTH the resume tail-read AND the torn-tail repair touch O(1)
#: tail bytes, never the whole log.
_TAIL_CHUNK_BYTES: Final[int] = 8192


def _iter_reverse_chunks(f: BinaryIO, size: int) -> Iterator[tuple[int, bytes]]:
    """Yield ``(pos, block)`` reading *f* backward from *size* in bounded blocks.

    The SINGLE reverse-seek mechanism shared by :func:`_read_tail_entry` (isolate
    the last committed line) and :func:`_find_last_newline_offset` (locate the
    last ``"\\n"`` for :func:`_truncate_torn_tail`) — so neither materialises the
    whole log (COMMON; one source of truth for the bounded reverse read). The
    STANDALONE twin of :func:`brutex.orchestrator.audit._iter_reverse_chunks`.

    Each yielded ``block`` is at most :data:`_TAIL_CHUNK_BYTES` bytes and ``pos``
    is its absolute start offset in *f*; blocks are yielded from the END of the
    file toward the start until offset ``0`` is reached. Peak resident bytes are
    O(:data:`_TAIL_CHUNK_BYTES`), never O(*size*). *f* must be a binary file
    already seekable to any offset in ``[0, size]``.
    """
    pos = size
    while pos > 0:
        read_size = min(_TAIL_CHUNK_BYTES, pos)
        pos -= read_size
        f.seek(pos)
        yield pos, f.read(read_size)


def _read_tail_entry(path: Path) -> tuple[SweepAuditEntry | None, bool]:
    """Recover only the LAST committed entry + the torn-tail flag from *path*.

    This is the O(1)-tail-read fast path for :meth:`SweepAuditWriter.open` on
    ``resume=True`` (DEC-AUDIT-RESUME-TAILREAD-001). It reads the file from the
    END backwards in :data:`_TAIL_CHUNK_BYTES` chunks to isolate the final
    committed (newline-terminated, non-blank) line, parses ONLY that one line,
    and returns it together with the SAME ``torn_tail`` flag
    :func:`_read_entries_with_torn_tail` computes — WITHOUT materialising every
    prior entry into memory.

    It is byte-for-byte equivalent to
    ``(entries[-1] if entries else None, torn_tail)`` for a clean or
    torn-tailed log: the resume path only ever used ``entries[-1]`` (its
    ``seq`` + ``entry_hash``), so recovering exactly that tail entry preserves
    the resumed chain identity, the torn-tail truncation (the flag is
    unchanged), and hash-chain integrity (the recovered ``entry_hash`` seeds
    ``prev_hash`` for the next append). Full-chain verification is a SEPARATE
    contract (:func:`verify_chain`) that was never part of ``resume``.

    This is the deliberate STANDALONE twin of
    :func:`brutex.orchestrator.audit._read_tail_entry` (that chain is SHA256,
    this one BLAKE3 — they do not import one another).

    Raises
    ------
    SweepAuditError
        If the file is missing, or the last committed line is malformed /
        missing required fields (real corruption of a committed record).

    Returns
    -------
    tuple[SweepAuditEntry | None, bool]
        ``(last_committed_entry_or_None, torn_tail)``. The entry is ``None``
        when the file has no committed non-blank line.
    """
    if not path.exists():
        raise SweepAuditError(f"Sweep audit log not found: {path}")

    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return None, False

        # torn_tail == the file does not end in "\n" (an unterminated final
        # line == an interrupted append).
        f.seek(size - 1)
        torn_tail = f.read(1) != b"\n"

        buf = b""
        pos = size
        last_committed_line: bytes | None = None
        for chunk_start, block in _iter_reverse_chunks(f, size):
            pos = chunk_start  # how far back the reverse walk has reached
            buf = block + buf
            newline_idx = buf.find(b"\n")
            if newline_idx == -1:
                continue
            candidate = _last_nonblank_committed_line(buf[newline_idx + 1 :])
            if candidate is not None:
                last_committed_line = candidate
                break
        else:
            last_committed_line = _last_nonblank_committed_line(buf)

        if last_committed_line is None and pos == 0:
            last_committed_line = _last_nonblank_committed_line(buf)

        if last_committed_line is None:
            return None, torn_tail

    line = last_committed_line.decode("utf-8").rstrip("\n")
    if not line:  # pragma: no cover - _last_nonblank_committed_line excludes blanks
        return None, torn_tail
    return SweepAuditEntry.from_json_line(line), torn_tail


def _last_nonblank_committed_line(chunk: bytes) -> bytes | None:
    """Return the last newline-terminated non-blank line in *chunk*, or ``None``.

    A committed line is one terminated by ``"\\n"``; a blank line (only the
    newline) is benign filler and skipped. Trailing bytes NOT terminated by
    ``"\\n"`` are the (already-flagged) torn tail and ignored here. Mirrors the
    per-line handling of :func:`_read_entries_with_torn_tail`. The STANDALONE
    twin of :func:`brutex.orchestrator.audit._last_nonblank_committed_line`.
    """
    end = chunk.rfind(b"\n")
    while end != -1:
        start = chunk.rfind(b"\n", 0, end)
        candidate = chunk[start + 1 : end]  # start == -1 → from 0
        if candidate:
            return candidate + b"\n"
        end = start
    return None


def _find_last_newline_offset(f: BinaryIO, size: int) -> int:
    """Return the absolute offset of the LAST ``"\\n"`` in *f*, or ``-1`` if none.

    Walks *f* backward via :func:`_iter_reverse_chunks`, so it reads O(1) tail
    bytes (peak O(:data:`_TAIL_CHUNK_BYTES`)) rather than the whole file — even
    when the torn final line spans several reverse-seek blocks. The first block
    (scanning from the end) that contains any ``"\\n"`` yields the answer: the
    last newline in the file is that block's LAST newline (no earlier block can
    hold a later one). *size* must equal the file's byte length. The STANDALONE
    twin of :func:`brutex.orchestrator.audit._find_last_newline_offset`.
    """
    for pos, block in _iter_reverse_chunks(f, size):
        idx = block.rfind(b"\n")
        if idx != -1:
            return pos + idx
    return -1


def _truncate_torn_tail(path: Path) -> None:
    """Drop a torn final line so the next append continues a clean chain.

    Truncates *path* IN PLACE to keep ONLY its committed (newline-terminated)
    bytes — the longest prefix ending in ``"\\n"`` — so the uncommitted torn
    remnant a crash left behind is removed and :meth:`SweepAuditWriter.append`
    resumes a pristine chain (DEC-AUDIT-TORN-TAIL-001).

    The cut point (the last ``"\\n"``) is located with a bounded reverse-seek
    (:func:`_find_last_newline_offset`) instead of reading the whole log into
    memory, so the repair is O(:data:`_TAIL_CHUNK_BYTES`) resident bytes even on
    a multi-GB sweep log (DEC-AUDIT-TORN-TAIL-O1-001, superseding the prior
    ``f.read()``-the-whole-file rewrite). The resulting file is byte-identical to
    that earlier rewrite for every input: the kept prefix is exactly
    ``data[: data.rfind(b"\\n") + 1]`` (empty when there is no committed
    newline).

    The truncation is an in-place :func:`os.truncate`, which only ever DROPS the
    trailing (uncommitted) bytes — the committed prefix bytes are never
    rewritten or moved, so an interruption DURING the repair cannot corrupt them
    (and a re-run simply re-truncates the same boundary; idempotent). A
    best-effort ``fsync`` persists the shortened length, mirroring the writer's
    durability guard.

    The STANDALONE twin of
    :func:`brutex.orchestrator.audit._truncate_torn_tail`.

    Raises
    ------
    SweepAuditWriteError
        If the repair fails for any I/O reason.
    """
    try:
        with open(path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            cut = _find_last_newline_offset(f, size)
            # cut == -1 → no committed newline at all → keep nothing.
            keep = cut + 1 if cut != -1 else 0
            os.truncate(f.fileno(), keep)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
    except OSError as exc:
        raise SweepAuditWriteError(
            f"Failed to truncate torn tail of sweep audit log {path}: {exc}"
        ) from exc


def _read_entries(path: Path) -> list[SweepAuditEntry]:
    """Materialise every entry from *path* without verifying the chain.

    A thin ``list(...)`` over the streaming :func:`iter_entries`
    (DEC-SWEEP-SCALE-OPERABILITY-001) — deliberately O(N) memory for the list
    contract. A torn final line is tolerated and skipped
    (DEC-AUDIT-TORN-TAIL-001); real corruption of a committed
    (newline-terminated) line still raises :class:`SweepAuditError`.
    """
    return list(iter_entries(path))


def read_entries(path: Path) -> list[SweepAuditEntry]:
    """Return all entries in *path* WITHOUT verifying the chain.

    A list materialisation kept for the quick tail / dashboard view's list
    contract — deliberately O(N) memory (the O(1)-memory streaming primitive
    is :func:`iter_entries`). Use :func:`verify_chain` when you need the
    integrity guarantee.
    """
    return _read_entries(path)


#: The verify-step failure tokens → the frozen corruption reason bytes.
_VERIFY_STEP_REASONS: Final[dict[str, str]] = {
    "seq_out_of_order": "seq number out of order",
    "prev_hash_mismatch": "prev_hash does not match previous entry's entry_hash",
    "entry_hash_mismatch": "entry_hash does not match recomputed hash (content tampered)",
}


def _verify_step_python(
    expected_seq: int,
    prev_expected: str,
    seq: int,
    prev_hash: str,
    ts_str: str,
    actor: str,
    action: str,
    subject: str,
    payload_json: str,
    entry_hash: str,
) -> tuple[str, str, str]:
    """The verbatim per-entry verify decision — ``(token, expected, actual)``.

    The LIVE parity oracle for the DEC-RUST-SWEEP-OPS-001 routed step law
    (``routed_chain_verify_step``): :func:`verify_chain`'s three checks in
    their frozen order (seq → prev-hash linkage → recomputed content hash),
    first failure wins; ``"ok"`` carries the recomputed hash. Message BYTES
    stay with :func:`verify_chain` (the :data:`_VERIFY_STEP_REASONS` map).
    """
    if seq != expected_seq:
        return ("seq_out_of_order", str(expected_seq), str(seq))
    if prev_hash != prev_expected:
        return ("prev_hash_mismatch", prev_expected, prev_hash)
    recomputed = _entry_hash_python(
        str(seq), prev_hash, ts_str, actor, action, subject, payload_json
    )
    if entry_hash != recomputed:
        return ("entry_hash_mismatch", recomputed, entry_hash)
    return ("ok", recomputed, "")


def verify_chain(path: Path) -> int:
    """Stream *path* and verify the BLAKE3 hash chain end-to-end — O(1) memory.

    Iterates :func:`iter_entries` one entry at a time
    (DEC-SWEEP-SCALE-OPERABILITY-001), carrying only the previous ``entry_hash`` +
    the running count — a multi-GB sweep log verifies without materialising
    the whole chain into memory (the prior implementation built the full entry
    list first). The three per-entry checks are unchanged: seq ordering,
    ``prev_hash`` linkage, recomputed ``entry_hash`` — expressed as the ONE
    per-entry step decision (:func:`_verify_step_python`, the LIVE oracle the
    default-ON DEC-RUST-SWEEP-OPS-001 seam routes; Python renders the field
    strings + owns every raise and message byte).

    Returns the number of entries verified. ``0`` means the file exists but is
    empty (trivially valid).

    Raises
    ------
    SweepAuditError
        If the file is missing, unreadable, or has a malformed line.
    SweepAuditChainCorruption
        On the first integrity break, carrying the offending ``seq``.
    """
    prev_hash = GENESIS_PREV_HASH
    verified = 0

    for expected_seq, entry in enumerate(iter_entries(path)):
        ts_str = str(entry.ts_utc)
        payload_json = _canonical_json(entry.payload)
        step_args = (
            expected_seq,
            prev_hash,
            entry.seq,
            entry.prev_hash,
            ts_str,
            entry.actor,
            entry.action,
            entry.subject,
            payload_json,
            entry.entry_hash,
        )
        if sweep_ops_rust_enabled():

            def _oracle(
                args: tuple[int, str, int, str, str, str, str, str, str, str] = step_args,
            ) -> tuple[str, str, str]:
                return _verify_step_python(*args)

            token, expected, actual = routed_chain_verify_step(*step_args, python_law=_oracle)
        else:
            token, expected, actual = _verify_step_python(*step_args)
        if token != "ok":  # noqa: S105 # nosec B105 — verdict token, not a secret
            raise SweepAuditChainCorruption(
                seq=entry.seq,
                expected=expected,
                actual=actual,
                reason=_VERIFY_STEP_REASONS[token],
            )
        prev_hash = entry.entry_hash
        verified += 1

    return verified


__all__ = [
    "BLAKE3_HEX_LEN",
    "DEFAULT_ACTOR",
    "GENESIS_PREV_HASH",
    "SweepAuditChainCorruption",
    "SweepAuditEntry",
    "SweepAuditError",
    "SweepAuditWriteError",
    "SweepAuditWriter",
    "iter_entries",
    "read_entries",
    "verify_chain",
]
