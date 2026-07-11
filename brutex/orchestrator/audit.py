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

The free-form fields ``actor`` / ``action`` / ``subject`` are joined
into the preimage with a literal ``|`` delimiter byte, so they MUST NOT
themselves contain ``|`` — otherwise the preimage is ambiguous and two
distinct field triples could share one hash (e.g.
``action="A|B", subject="C"`` vs ``action="A", subject="B|C"`` both
flatten to ``…|A|B|C|…``). :meth:`AuditEntry.__post_init__` rejects a
``|`` in any of those fields with :exc:`AuditFieldError`, on BOTH the
write path (entry construction) and the read path
(:meth:`AuditEntry.from_json_line`), keeping the preimage injective and
the chain genuinely tamper-evident.

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from types import TracebackType
from typing import Any, BinaryIO, ClassVar, Final, Protocol, Self, cast, runtime_checkable

import structlog

from brutex.models import BruteXError

_LOG = structlog.get_logger(__name__)

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


class AuditFieldError(AuditError):
    """Raised when a free-form field contains the hash-preimage delimiter.

    The :meth:`AuditEntry.compute_hash` preimage joins the variable-length
    free-form fields ``actor`` / ``action`` / ``subject`` with a literal
    ``b"|"`` byte. If any of those fields itself contained a ``"|"`` the
    encoding would be non-injective: ``action="A|B", subject="C"`` and
    ``action="A", subject="B|C"`` would both flatten to the SAME preimage
    ``…|A|B|C|…`` → an identical hash. A tampered on-disk line could then be
    re-split into a different (action, subject) pair WITHOUT breaking the
    chain. :meth:`AuditEntry.__post_init__` rejects a ``"|"`` in any of those
    three fields with this error so the preimage stays unambiguous and the
    collision is impossible by construction. Because :meth:`from_json_line`
    rebuilds the entry through the SAME ``__post_init__``, a tampered line
    that injected a ``"|"`` is likewise rejected on the read path
    (surfaced through :func:`_read_entries` / :func:`verify_chain` as an
    :class:`AuditError`).
    """


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
# Rust audit-chain kernel seam (DEC-AUDIT-CHAIN-RUST-PORT-001, ADR-0019)
# ────────────────────────────────────────────────────────────────────────
#
# Default-OFF flag-gated dispatch of the SERIALIZATION + HASH layer
# (``compute_hash`` / ``to_json_line``) to the ``fno_math`` Rust kernel,
# parity-gated on the frozen ``tests/golden/fixtures/audit_chain_parity_*``
# fixtures. The #1531/#1532 rollout pattern: memoized flag + kernel
# resolution, a 16-sample per-process BURN-IN double-compute against the
# retained Python bodies (mismatch → ship the PYTHON bytes + latch the
# kernel off + ONE ``audit.kernel_autoreverted`` ERROR), and a LOUD
# warn-once fallback for every out-of-model payload
# (``audit.rust_envelope_fallback``) — the Python bodies are the oracle and
# are NEVER deleted, so wheel state can never change chain bytes or error
# behaviour. All file I/O / fsync / locking / torn-tail semantics are
# untouched — only the two pure byte producers dispatch.

#: The env flag (default OFF). Truthy = ``1/true/yes/on``; falsy =
#: ``0/false/no/off``; unset/empty = OFF; anything else = OFF with one
#: ``audit.rust_flag_unrecognised`` warning.
RUST_AUDIT_CHAIN_ENV: Final[str] = "BRUTEX_RUST_AUDIT_CHAIN"

#: Rust-served computations (hash + line share ONE budget) that are
#: double-computed against the Python oracle per process (the burn-in).
AUDIT_BURN_IN_SAMPLES: Final[int] = 16

#: The env-flag vocabulary — a DELIBERATE local copy of the sweep flags'
#: ``_TRUTHY`` / ``_FALSY`` literal sets: the COMMON classifier lives in
#: ``brutex.engine.sweep_lookahead``, but the engine package imports THIS
#: module (``engine.sweep_run`` → ``orchestrator.audit``), so importing the
#: engine from here would create an orchestrator ⇄ engine package cycle
#: (the CodeQL ``py/cyclic-import`` class). Layering wins over §11 COMMON
#: here — noted, not silent.
_RUST_FLAG_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_RUST_FLAG_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class _RustAuditKernel:
    """The resolved Rust kernel surface — hash fn + line fn + envelope type.

    Held in the process memo so the seam resolves ``fno_math`` exactly once;
    tests inject a fake instance through :func:`_set_rust_kernel_for_tests`.
    """

    hash_fn: Callable[..., str]
    line_fn: Callable[..., str]
    envelope_error: type[BaseException]


#: Kernel memo: ``[resolved?]`` + the resolved value (two cells so the
#: sentinel needs no ``object()`` type gymnastics). Single-cell mutable
#: module state — the same documented exception to the
#: no-global-mutable-state rule as ``brutex.engine.rust_metrics``.
_RUST_KERNEL_RESOLVED: Final[list[bool]] = [False]
_RUST_KERNEL_VALUE: Final[list[_RustAuditKernel | None]] = [None]

#: Burn-in budget + the autorevert latch (reason once latched, else None).
_RUST_BURN_IN_REMAINING: Final[list[int]] = [AUDIT_BURN_IN_SAMPLES]
_RUST_DISABLED_REASON: Final[list[str | None]] = [None]

#: Warn-once memo — LOG-VOLUME state only; results never depend on it.
_RUST_WARN_MEMO: Final[set[str]] = set()


def _warn_rust_once(event: str, **context: object) -> None:
    """Log *event* at WARNING once per process (per event + reason key)."""
    key = f"{event}:{context.get('reason', '')}"
    if key in _RUST_WARN_MEMO:
        return
    _RUST_WARN_MEMO.add(key)
    _LOG.warning(event, **context)


def _rust_flag_enabled() -> bool:
    """Resolve the default-OFF ``BRUTEX_RUST_AUDIT_CHAIN`` flag (fail-safe OFF).

    A junk (unrecognised, non-empty) value warns once and stays OFF — an
    operator's mistyped intent is never silently ignored.
    """
    raw = os.environ.get(RUST_AUDIT_CHAIN_ENV, "")
    value = raw.strip().lower()
    if value in _RUST_FLAG_TRUTHY:
        return True
    if value and value not in _RUST_FLAG_FALSY:
        _warn_rust_once("audit.rust_flag_unrecognised", value=raw)
    return False


def _import_fno_math() -> Any:
    """Import indirection for the kernel wheel (monkeypatched by tests)."""
    import fno_math

    return fno_math


def _resolve_rust_kernel() -> _RustAuditKernel | None:
    """Resolve the Rust kernel once per process — ``None`` = the Python arm.

    A wheel that is absent, or too old to carry the slice-9 symbols, warns
    ONCE (``audit.rust_kernel_unavailable``) and permanently falls back —
    never a crash (the #1531 wheel-state law).
    """
    if not _rust_flag_enabled():
        return None
    try:
        fno_math = _import_fno_math()
    except ImportError:
        _warn_rust_once("audit.rust_kernel_unavailable", reason="wheel_missing")
        return None
    hash_fn = getattr(fno_math, "audit_entry_hash", None)
    line_fn = getattr(fno_math, "audit_entry_line", None)
    envelope_error = getattr(fno_math, "AuditEnvelopeError", None)
    if (
        not callable(hash_fn)
        or not callable(line_fn)
        or not isinstance(envelope_error, type)
        or not issubclass(envelope_error, BaseException)
    ):
        _warn_rust_once("audit.rust_kernel_unavailable", reason="wheel_stale")
        return None
    return _RustAuditKernel(
        hash_fn=cast("Callable[..., str]", hash_fn),
        line_fn=cast("Callable[..., str]", line_fn),
        envelope_error=envelope_error,
    )


def _rust_audit_kernel() -> _RustAuditKernel | None:
    """The memoized kernel handle; ``None`` while OFF/unavailable/latched."""
    if _RUST_DISABLED_REASON[0] is not None:
        return None
    if not _RUST_KERNEL_RESOLVED[0]:
        _RUST_KERNEL_VALUE[0] = _resolve_rust_kernel()
        _RUST_KERNEL_RESOLVED[0] = True
    return _RUST_KERNEL_VALUE[0]


def _disable_rust_kernel(reason: str, **context: object) -> None:
    """Latch the kernel OFF for the process (the burn-in autorevert).

    The first call logs ``audit.kernel_autoreverted`` at ERROR — a genuine
    kernel-wrongness signal, never throttled; repeats are no-ops.
    """
    if _RUST_DISABLED_REASON[0] is None:
        _RUST_DISABLED_REASON[0] = reason
        _LOG.error("audit.kernel_autoreverted", reason=reason, **context)


def _set_rust_kernel_for_tests(kernel: _RustAuditKernel | None) -> None:
    """Inject a resolved kernel (test isolation ONLY — the fake-kernel seam)."""
    _RUST_KERNEL_VALUE[0] = kernel
    _RUST_KERNEL_RESOLVED[0] = True


def _reset_rust_audit_for_tests(*, burn_in_remaining: int = AUDIT_BURN_IN_SAMPLES) -> None:
    """Restore a fresh-process seam view (test isolation ONLY)."""
    _RUST_KERNEL_RESOLVED[0] = False
    _RUST_KERNEL_VALUE[0] = None
    _RUST_BURN_IN_REMAINING[0] = burn_in_remaining
    _RUST_DISABLED_REASON[0] = None
    _RUST_WARN_MEMO.clear()


def _rust_serialize(kernel: _RustAuditKernel, surface: str, call: Callable[[], str]) -> str | None:
    """Run one kernel serialization; ``None`` ⇒ the caller uses the oracle.

    The fallback catch is NARROW: the kernel's own envelope refusal, plus the
    two PyO3 argument-conversion shapes an out-of-model ENTRY field produces
    (``OverflowError`` — a seq outside i64 on a tampered line;
    ``UnicodeEncodeError`` — a lone-surrogate free-form field, which the
    Python oracle then re-raises exactly as today's ``.encode`` does). Any
    other exception type propagates (a genuine code bug never hides).
    """
    try:
        return call()
    except kernel.envelope_error:
        _warn_rust_once("audit.rust_envelope_fallback", reason=surface)
        return None
    except (OverflowError, UnicodeEncodeError):
        _warn_rust_once("audit.rust_conversion_fallback", reason=surface)
        return None


def _settle_rust_result(surface: str, rust_value: str, python_thunk: Callable[[], str]) -> str:
    """Serve a kernel result through the burn-in double-compute window.

    Post burn-in: the kernel value serves directly. During burn-in the
    Python oracle also runs and the two strings are compared EXACTLY — a
    mismatch ships the PYTHON value (correctness first), latches the kernel
    off, and logs ONE ``audit.kernel_autoreverted`` ERROR. The thunk cannot
    raise here: a payload the kernel accepted is in-model, so the oracle's
    only raise arm (non-finite floats) is structurally unreachable.
    """
    if _RUST_BURN_IN_REMAINING[0] <= 0:
        return rust_value
    python_value = python_thunk()
    if rust_value != python_value:
        _disable_rust_kernel(
            "burn_in_mismatch", surface=surface, rust=rust_value, python=python_value
        )
        return python_value
    _RUST_BURN_IN_REMAINING[0] -= 1
    return rust_value


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

    #: The hash-preimage delimiter byte (see :meth:`compute_hash`). It must
    #: never appear inside a free-form field or the preimage becomes
    #: ambiguous — enforced by :meth:`__post_init__`.
    _DELIMITER: ClassVar[str] = "|"

    def __post_init__(self) -> None:
        """Reject the hash-preimage delimiter in the free-form fields.

        ``actor`` / ``action`` / ``subject`` are joined into the
        :meth:`compute_hash` preimage with a literal ``"|"`` byte. Allowing a
        ``"|"`` inside any of them would make the encoding non-injective (two
        distinct field triples could share one preimage → one hash), so a
        crafted or tampered ``"|"``-bearing field could be re-split into a
        different triple WITHOUT breaking the chain. Forbidding the single
        delimiter character keeps the preimage unambiguous and the chain
        tamper-evident, while changing NO hash bytes for any conforming entry.

        Raises
        ------
        AuditFieldError
            If ``actor``, ``action`` or ``subject`` contains ``"|"``.
        """
        for name in ("actor", "action", "subject"):
            value: str = getattr(self, name)
            if self._DELIMITER in value:
                raise AuditFieldError(
                    f"Audit field {name!r} must not contain the {self._DELIMITER!r} "
                    f"hash-preimage delimiter: {value!r}"
                )

    def compute_hash(self) -> str:
        """Compute what the ``entry_hash`` should be based on all other fields.

        Does NOT mutate ``self`` — returns a new hex string the caller
        can compare. Under ``BRUTEX_RUST_AUDIT_CHAIN`` (default OFF) the
        byte production dispatches to the ``fno_math`` kernel through the
        burn-in seam (DEC-AUDIT-CHAIN-RUST-PORT-001); every refusal path and
        the OFF arm serve from the verbatim Python body
        (:meth:`_compute_hash_python`) — bit-identical by construction.
        """
        kernel = _rust_audit_kernel()
        if kernel is not None:
            rust_value = _rust_serialize(
                kernel,
                "compute_hash",
                lambda: kernel.hash_fn(
                    self.seq,
                    self.prev_hash,
                    self.ts_utc,
                    self.actor,
                    self.action,
                    self.subject,
                    self.payload,
                ),
            )
            if rust_value is not None:
                return _settle_rust_result("compute_hash", rust_value, self._compute_hash_python)
        return self._compute_hash_python()

    def _compute_hash_python(self) -> str:
        """The retained Python oracle body of :meth:`compute_hash` — verbatim."""
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
        """Serialise to a single JSONL line (no trailing newline).

        Same dispatch discipline as :meth:`compute_hash`: the Rust arm is
        flag-gated + burn-in-checked, and a non-finite payload refuses at the
        KERNEL envelope → the Python oracle raises today's exact
        :class:`AuditPayloadError` BEFORE any caller write — the
        raise-before-write law is preserved on both arms.
        """
        kernel = _rust_audit_kernel()
        if kernel is not None:
            rust_value = _rust_serialize(
                kernel,
                "to_json_line",
                lambda: kernel.line_fn(
                    self.seq,
                    self.prev_hash,
                    self.ts_utc,
                    self.actor,
                    self.action,
                    self.subject,
                    self.payload,
                    self.entry_hash,
                ),
            )
            if rust_value is not None:
                return _settle_rust_result("to_json_line", rust_value, self._to_json_line_python)
        return self._to_json_line_python()

    def _to_json_line_python(self) -> str:
        """The retained Python oracle body of :meth:`to_json_line` — verbatim."""
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
            # verification remains the SEPARATE `verify_chain` contract, which
            # resume never performed.
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
# AuditSink — the write-target seam (DEC-MULTICORE-SWEEP-PHASE1-001)
# ────────────────────────────────────────────────────────────────────────


def open_ingest_audit(
    lake_root: Path,
    *,
    name: str = "spot-ingest",
    enabled: bool = True,
) -> contextlib.AbstractContextManager[AuditWriter | None]:
    """Open the lake's hash-chained ingest audit trail (Golden Rule #8).

    The ONE COMMON factory (CLAUDE.md §11 COMMON — DEC-INGEST-AUDIT-WIRING-001)
    every production ingest entrypoint binds — the CLI ``brutex fetch`` spot
    pull, the dashboard ``pull_all_spot`` / ``pull_candles`` spot pulls, and the
    dashboard F&O ``pull_contract_candles`` historical pull — so the audit path
    convention (``<lake>/audit/<name>.jsonl``) and the resume-the-existing-chain
    semantics can never drift between entrypoints. Each ``append`` through the
    returned writer is O(1) (one canonical-JSON line + flush + fsync) and, per
    the ingest spines' contract, is invoked ONLY from the single controller
    drain thread — never from a fetch worker — so wiring it can never block the
    fetch lane.

    Parameters
    ----------
    lake_root
        The data-lake root; the trail lives at ``<lake_root>/audit/<name>.jsonl``
        (:meth:`AuditWriter.open` creates the parent directory).
    name
        The trail's basename — ``"spot-ingest"`` (the spot spine, the
        DEC-INGEST-AUDIT-CHAIN-001 path, unchanged byte-for-byte) or
        ``"fno-ingest"`` (the F&O spine).
    enabled
        ``False`` (a ``--dry-run`` / ``--inspect`` / synthetic pull that writes
        no real data) returns ``contextlib.nullcontext(None)`` — no file, no
        directory, a ``None`` writer the spines' ``maybe_audit`` guard no-ops on.

    Returns
    -------
    contextlib.AbstractContextManager[AuditWriter | None]
        An open-for-append :class:`AuditWriter` continuing the existing chain
        (``resume=True`` when the file exists), or the null context when
        *enabled* is ``False``.
    """
    if not enabled:
        return contextlib.nullcontext(None)
    path = lake_root / "audit" / f"{name}.jsonl"
    return AuditWriter.open(path, resume=path.exists())


@dataclass(frozen=True, slots=True)
class BufferedAuditRecord:
    """One buffered ``append`` call — the *data* of an audit line, un-hashed.

    Collected by :class:`BufferedAuditCollector` inside a parallel-sweep worker
    process, returned to the parent (this is a plain, picklable dataclass), and
    replayed into the REAL hash-chained :class:`AuditWriter` on the parent's
    single writer-actor thread in canonical enumeration order — so the chain is
    produced on ONE thread, byte-identically to a sequential run
    (DEC-MULTICORE-SWEEP-PHASE1-001). It carries NO ``seq`` / ``prev_hash`` /
    ``entry_hash``: the ordering and the hash are the parent's authority, never a
    worker's (a worker has no view of the global chain position).

    Attributes
    ----------
    action
        The audit action label (e.g. ``SWEEP_START``).
    subject
        The audit subject.
    payload
        The JSON-serialisable action payload (already a plain ``dict``).
    """

    action: str
    subject: str
    payload: dict[str, Any]


@runtime_checkable
class AuditSink(Protocol):
    """The append-only write target :func:`run_persisted_sweep` writes through.

    A structural seam (DEC-MULTICORE-SWEEP-PHASE1-001) so the golden-locked
    per-tuple runner can write EITHER to the real hash-chained
    :class:`AuditWriter` (the sequential path, byte-unchanged) OR to a
    :class:`BufferedAuditCollector` (a parallel-sweep worker, which collects the
    line as plain data instead of hashing it). :class:`AuditWriter` and
    :class:`~brutex.engine.sweep_audit.SweepAuditWriter` both satisfy this Protocol
    structurally — their ``append`` signatures are identical — so widening the
    runner's annotation from ``AuditWriter`` to ``AuditSink`` is a pure widening
    that changes NO existing call.

    The return value is deliberately ``object`` (not ``AuditEntry``): the runner
    ignores it, and the collector has no entry to return. The Protocol therefore
    imposes the minimum contract both impls share.
    """

    def append(
        self,
        *,
        action: str,
        subject: str,
        payload: dict[str, Any] | None = None,
        ts_utc: float | None = None,
    ) -> object:
        """Append one audit line (or buffer it); return value is ignored by callers."""


class BufferedAuditCollector:
    """An :class:`AuditSink` that BUFFERS append calls as plain data — no hashing.

    Used inside a parallel-sweep worker process (DEC-MULTICORE-SWEEP-PHASE1-001):
    the worker runs the UNCHANGED per-tuple body
    (:func:`brutex.engine.sweep_run.run_persisted_sweep`) but hands it this
    collector as its ``audit`` sink, so every ``SWEEP_START`` / ``SWEEP_FLUSH`` /
    ``SWEEP_COMPLETE`` / ``SWEEP_RESUMED`` line is appended to an in-memory list
    of :class:`BufferedAuditRecord` instead of the durable hash chain. The parent
    then replays :attr:`records` into the real :class:`AuditWriter` in canonical
    enumeration order.

    **Why no hashing here.** A worker has no view of the global chain position
    (``seq`` / ``prev_hash``), so it CANNOT compute a chain-correct
    ``entry_hash``. Buffering the raw line data + replaying it on the parent's
    single writer-actor is the ONE design that keeps the chain byte-identical to
    a sequential run (the parent owns the ``seq`` / ``prev_hash`` / ``ts`` and
    appends in enumeration order).

    **Thread-safety.** Not thread-safe — one collector is used by exactly one
    worker running exactly one tuple's body (a strictly sequential caller inside
    that worker), mirroring the single-writer contract of :class:`AuditWriter`.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[BufferedAuditRecord] = []

    def append(
        self,
        *,
        action: str,
        subject: str,
        payload: dict[str, Any] | None = None,
        ts_utc: float | None = None,
    ) -> BufferedAuditRecord:
        """Buffer one audit line as plain data (the ``ts_utc`` override is ignored).

        The ``ts_utc`` parameter exists only for :class:`AuditSink` signature
        parity; a worker cannot stamp a chain-correct timestamp, so the parent's
        real :class:`AuditWriter` supplies it at replay time. Returns the buffered
        record (the runner ignores it, matching :meth:`AuditWriter.append`).
        """
        record = BufferedAuditRecord(
            action=action,
            subject=subject,
            payload=dict(payload) if payload else {},
        )
        self._records.append(record)
        return record

    @property
    def records(self) -> tuple[BufferedAuditRecord, ...]:
        """The buffered records in append order (immutable snapshot)."""
        return tuple(self._records)


# ────────────────────────────────────────────────────────────────────────
# verify_chain — standalone function called by brutex audit verify
# ────────────────────────────────────────────────────────────────────────


def _iter_committed_entries(path: Path) -> Iterator[AuditEntry]:
    """Generator body of :func:`iter_entries` — one parsed entry per committed line.

    Applies the DEC-AUDIT-TORN-TAIL-001 discriminator line by line (see
    :func:`iter_entries` for the full contract): an unterminated FINAL line is
    an uncommitted torn tail and is skipped, a blank committed line is benign
    filler, and a malformed committed (newline-terminated) line is REAL
    corruption and raises :class:`AuditError` naming ``path:line_no``. Peak
    memory is O(1) — one raw line + one parsed entry at a time, never the
    whole log (DEC-AUDIT-VERIFY-STREAM-001; the port of
    :func:`brutex.engine.sweep_audit._iter_committed_entries`'s pattern).
    """
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            if not raw.endswith("\n"):
                # Unterminated final line == interrupted append == torn tail.
                # A purely-blank unterminated tail (e.g. the crash struck right
                # at the start of the record) carries no entry but is still the
                # torn remnant the resume path truncates; a content-bearing one
                # is the partial/complete-but-unterminated record. Either way it
                # is uncommitted: skip it (only the FILE's final line can ever
                # lack the "\n" — the writer appends `line + "\n"` in one write).
                # Equivalent-mutant pragma: text-mode iteration can only yield a
                # newline-less line LAST, so this always runs on the final
                # iteration and `continue` ≡ `break`.
                continue  # pragma: no mutate
            line = raw.rstrip("\n")
            if not line:
                # A blank committed line (newline-terminated) is benign filler
                # and is skipped without affecting the chain.
                continue
            try:
                yield AuditEntry.from_json_line(line)
            except AuditError as exc:
                raise AuditError(f"{path}:{line_no}: {exc}") from exc


def iter_entries(path: Path) -> Iterator[AuditEntry]:
    """Stream every committed entry from *path* WITHOUT verifying the chain.

    The O(1)-memory streaming primitive (DEC-AUDIT-VERIFY-STREAM-001) behind
    :func:`read_entries` and :func:`verify_chain` — the SHA-256 orchestrator
    twin of :func:`brutex.engine.sweep_audit.iter_entries` (that chain is
    BLAKE3; they deliberately do not import one another): entries are parsed
    one line at a time, so a multi-GB audit log is scanned without ever
    materialising the whole chain into a list.

    Torn-tail discriminator (hash-chain-safe)
    -----------------------------------------
    The writer always appends ``line + "\\n"`` in one :py:meth:`write` call, so
    a committed record is **defined** as a line terminated by ``"\\n"``. A crash
    mid-append can therefore only ever leave its damage on the FINAL line and
    only when that line is missing its trailing newline (the write was cut off
    before, or partway through, the ``"\\n"``). That newline-less final line —
    whether its JSON is partial or even complete-but-unterminated — is an
    UNCOMMITTED torn tail and is skipped so an unattended resume can recover
    from the last complete, hash-valid entry instead of dying with
    :class:`AuditError`. Everything that IS newline-terminated is a committed
    record and is parsed strictly: a malformed line that carries a trailing
    newline is REAL corruption and still raises. Mid-file integrity is NOT
    weakened — only an unterminated tail is forgiven, and the hash chain over
    the committed prefix is still verified by :func:`verify_chain`.

    Parameters
    ----------
    path
        The audit ``.jsonl`` file.

    Returns
    -------
    Iterator[AuditEntry]
        Lazily-parsed committed entries in file order.

    Raises
    ------
    AuditError
        Immediately (at call time) if the file is missing; during iteration if
        any committed (newline-terminated) line is malformed / missing
        required fields.
    """
    if not path.exists():
        raise AuditError(f"Audit log not found: {path}")
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
        # Argument-free read(): after seek(size - 1) exactly 1 byte remains, so
        # read() returns that identical byte — no size literal means NO
        # equivalent read-width mutant on this line, so the `!=` / b"\n"
        # mutants stay IN the mutation gate (suite-killed by
        # TestHasTornTailPins; the -003 fixer round 2 measured the prior
        # line-scoped pragma hiding exactly those killable mutants).
        return f.read() != b"\n"


def _read_entries_with_torn_tail(path: Path) -> tuple[list[AuditEntry], bool]:
    """Read every entry from *path*, tolerating a single torn final line.

    Returns the parsed entries plus a flag that is ``True`` when the file's
    LAST line was a **torn tail** — an interrupted append left behind by a
    crash / ``SIGKILL`` mid-write (DEC-AUDIT-TORN-TAIL-001). Re-expressed over
    the streaming :func:`iter_entries` + the O(1) :func:`_has_torn_tail`
    predicate (DEC-AUDIT-VERIFY-STREAM-001) — the discriminator lives in ONE
    place (CLAUDE.md §11 COMMON); the list materialisation here is this
    variant's deliberate contract. See :func:`iter_entries` for the full
    torn-tail discriminator.

    Raises
    ------
    AuditError
        If the file is missing, or any committed (newline-terminated) line is
        malformed / missing required fields.
    """
    entries = list(iter_entries(path))
    return entries, _has_torn_tail(path)


def _read_entries(path: Path) -> list[AuditEntry]:
    """Read every entry from *path* without verifying the chain.

    A torn final line (an interrupted append left by a crash mid-write) is
    tolerated and skipped (DEC-AUDIT-TORN-TAIL-001); real corruption of a
    committed (newline-terminated) line still raises :class:`AuditError`. See
    :func:`_read_entries_with_torn_tail` for the full discriminator.
    """
    entries, _torn_tail = _read_entries_with_torn_tail(path)
    return entries


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
    whole log (COMMON; one source of truth for the bounded reverse read).

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


def _read_tail_entry(path: Path) -> tuple[AuditEntry | None, bool]:
    """Recover only the LAST committed entry + the torn-tail flag from *path*.

    This is the O(1)-tail-read fast path for :meth:`AuditWriter.open` on
    ``resume=True`` (DEC-AUDIT-RESUME-TAILREAD-001). It reads the file from the
    END backwards in :data:`_TAIL_CHUNK_BYTES` chunks to isolate the final
    committed (newline-terminated, non-blank) line, parses ONLY that one line,
    and returns it together with the SAME ``torn_tail`` flag
    :func:`_read_entries_with_torn_tail` computes — WITHOUT materialising every
    prior entry into memory.

    It is byte-for-byte equivalent to
    ``(_read_entries_with_torn_tail(path)[0][-1] if entries else None,
    torn_tail)`` for a clean or torn-tailed log: the resume path only ever used
    ``entries[-1]`` (its ``seq`` + ``entry_hash``), so recovering exactly that
    tail entry preserves the resumed chain identity, the torn-tail truncation
    (the flag is unchanged), and hash-chain integrity (the recovered
    ``entry_hash`` seeds ``prev_hash`` for the next append). Full-chain
    verification is a SEPARATE contract (:func:`verify_chain`) that was never
    part of ``resume`` and is unaffected.

    Torn-tail discriminator (identical to :func:`_read_entries_with_torn_tail`)
    ---------------------------------------------------------------------------
    The writer always appends ``line + "\\n"`` in one :py:meth:`write`, so a
    committed record is a line terminated by ``"\\n"``. The file's final line is
    a torn tail iff the file is non-empty AND does not end in ``"\\n"`` — an
    interrupted append. Everything up to and including the last ``"\\n"`` is the
    committed prefix; the last non-blank committed line is the tail entry.

    Raises
    ------
    AuditError
        If the file is missing, or the last committed line is malformed /
        missing required fields (real corruption of a committed record).

    Returns
    -------
    tuple[AuditEntry | None, bool]
        ``(last_committed_entry_or_None, torn_tail)``. The entry is ``None``
        when the file has no committed non-blank line (empty, blank-only, or a
        torn-tail-only file).
    """
    if not path.exists():
        raise AuditError(f"Audit log not found: {path}")

    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return None, False

        # torn_tail == the file does not end in "\n" (an unterminated final
        # line == an interrupted append). Read the final byte to decide.
        f.seek(size - 1)
        # Argument-free read(): after seek(size - 1) exactly 1 byte remains, so
        # read() returns that identical byte — no size literal means NO
        # equivalent read-width mutant on this line, so the `!=` / b"\n"
        # mutants stay IN the mutation gate (suite-killed by
        # TestReadTailEntryPins; the -003 fixer round 2 measured the prior
        # line-scoped pragma hiding exactly those killable mutants).
        torn_tail = f.read() != b"\n"

        # Walk backwards in chunks, accumulating the tail bytes, until we have
        # isolated the last committed (newline-terminated) NON-BLANK line. The
        # committed prefix ends at the last "\n"; the tail line we want is the
        # last non-blank line ending in (or terminated by) that "\n".
        last_committed_line = _isolate_last_committed_line(f, size)

        if last_committed_line is None:
            return None, torn_tail

    line = last_committed_line.decode("utf-8").rstrip("\n")
    if not line:  # pragma: no cover - _last_nonblank_committed_line excludes blanks
        return None, torn_tail
    return AuditEntry.from_json_line(line), torn_tail


def _isolate_last_committed_line(f: BinaryIO, size: int) -> bytes | None:
    """Reverse-walk *f* and return its last committed NON-BLANK line, or ``None``.

    The accumulate-and-scan half of :func:`_read_tail_entry`: prepend
    reverse-seek blocks (:func:`_iter_reverse_chunks`) until the last
    ``"\\n"``-terminated non-blank line is isolated, reading O(1) tail bytes in
    the common case (the chunk-consumption pin
    ``test_tail_read_consumes_only_the_final_chunk``). *size* must be the
    file's byte length and MUST be ``> 0`` — the walk then always yields at
    least one block, so the accumulator is seeded from the FIRST block instead
    of a ``b""`` literal. PRAGMA-FREE by construction (DEC-MUTATION-RUNTIME-003
    fixer round 3): the prior in-line loop's seed pragma hid a suite-killable
    ``buf = None`` crash mutant behind its equivalent ``b"XXXX"`` twin (the
    seed was NOT a dead store — the first iteration read it via
    ``block + buf``); with the first-block seed there is no seed literal, no
    ``pos = size`` / ``= None`` dead store, no for-else, and no redundant
    post-loop rescue left to justify.
    """
    walk = _iter_reverse_chunks(f, size)
    pos, buf = next(walk)
    while True:
        # Split into physical lines. Because we always prepend, only the FIRST
        # fragment can be incomplete (more bytes may precede it), so only lines
        # AFTER the first newline are fully-known. partition() carries no -1
        # sentinel, so the find()-shape's equivalent `-1 → -2` numeric-mutant
        # class disappears STRUCTURALLY and NO pragma is needed: every
        # remaining mutant on these lines either degrades the early exit into
        # extra reverse-chunk reads (pinned by the chunk-consumption test; the
        # -003 fixer round 2 measured the prior line-scoped pragma hiding
        # exactly that suite-killable comparison flip) or over-runs the walk
        # into a loud StopIteration past the file start.
        _incomplete_head, sep, complete = buf.partition(b"\n")
        if sep:
            # Everything after the first newline is complete; scan its lines
            # from the end for the last non-blank committed line.
            candidate = _last_nonblank_committed_line(complete)
            if candidate is not None:
                return candidate
        if pos == 0:
            # The walk reached the file start with no committed non-blank line
            # found in the known-complete region: buf now holds the WHOLE
            # file, so the first fragment is a complete line too.
            return _last_nonblank_committed_line(buf)
        # No answer yet — accumulate the next block back and re-scan.
        chunk_start, block = next(walk)
        pos = chunk_start
        buf = block + buf


def _last_nonblank_committed_line(chunk: bytes) -> bytes | None:
    """Return the last newline-terminated non-blank line in *chunk*, or ``None``.

    A committed line is one terminated by ``"\\n"``; a blank line (only the
    newline) is benign filler and skipped. Any trailing bytes NOT terminated by
    ``"\\n"`` are the (already-flagged) torn tail and ignored here. Mirrors the
    per-line handling of :func:`_read_entries_with_torn_tail`.
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
    hold a later one). *size* must equal the file's byte length.
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
    remnant a crash left behind is removed and :meth:`AuditWriter.append`
    resumes a pristine chain (DEC-AUDIT-TORN-TAIL-001).

    The cut point (the last ``"\\n"``) is located with a bounded reverse-seek
    (:func:`_find_last_newline_offset`) instead of reading the whole log into
    memory, so the repair is O(:data:`_TAIL_CHUNK_BYTES`) resident bytes even on
    a multi-GB log (DEC-AUDIT-TORN-TAIL-O1-001, superseding the prior
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

    Raises
    ------
    AuditWriteError
        If the repair fails for any I/O reason.
    """
    try:
        with open(path, "r+b") as f:
            # Equivalent-mutant pragma: size only tiles the reverse walk — an
            # off-by-one over-read past EOF is harmless (read() truncates) and
            # the located last-newline offset is tiling-invariant.
            f.seek(0, os.SEEK_END)  # pragma: no mutate
            size = f.tell()
            cut = _find_last_newline_offset(f, size)
            # cut == -1 (no committed newline at all) folds in arithmetically:
            # cut + 1 == 0 → keep nothing. The redundant `if cut != -1 else 0`
            # arm is DELETED outright (the #624 dead-clamp idiom) instead of
            # pragma'd: the -003 fixer round 2 measured that line-scoped
            # pragma ALSO hiding the suite-killable `!=`→`==` flip — an
            # always-truncate-to-0 audit-log wipe. The `+ 1` keep boundary
            # stays IN the mutation gate, byte-pinned by
            # TestTruncateTornTailPins.
            keep = cut + 1
            os.truncate(f.fileno(), keep)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
    except OSError as exc:
        raise AuditWriteError(f"Failed to truncate torn tail of audit log {path}: {exc}") from exc


def verify_chain(path: Path) -> int:
    """Stream *path* and verify the SHA-256 hash chain end-to-end — O(1) memory.

    Iterates :func:`iter_entries` one entry at a time
    (DEC-AUDIT-VERIFY-STREAM-001 — the port of the
    :func:`brutex.engine.sweep_audit.verify_chain` generator pattern), carrying
    only the previous ``entry_hash`` + the running count — a multi-GB audit log
    verifies without materialising the whole chain into memory (the prior
    implementation built the full entry list first). The per-entry checks are
    unchanged: seq ordering, ``prev_hash`` linkage, recomputed ``entry_hash``
    (including the tampered non-finite-payload mapping to
    :class:`AuditChainCorruption`).

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
    prev_hash = GENESIS_PREV_HASH
    verified = 0

    for expected_seq, entry in enumerate(iter_entries(path)):
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
        # 3. entry_hash is correct for the entry's contents.
        #    A tampered on-disk line can carry a non-finite float
        #    (``json.loads`` accepts the bare ``NaN`` / ``Infinity`` tokens by
        #    default), which ``compute_hash`` → ``_canonical_json`` rejects with
        #    an :class:`AuditPayloadError`. That is a chain-integrity failure of
        #    THIS entry, so it is re-raised as :class:`AuditChainCorruption`
        #    naming the offending seq — the documented contract (the writer can
        #    never emit such a line; only a tamper produces it).
        try:
            recomputed = entry.compute_hash()
        except AuditPayloadError as exc:
            raise AuditChainCorruption(
                seq=entry.seq,
                expected="a finite, RFC 8259-serialisable payload",
                actual="non-finite payload (NaN / Infinity / -Infinity)",
                reason="payload contains a non-finite float (tampered line)",
            ) from exc
        if entry.entry_hash != recomputed:
            raise AuditChainCorruption(
                seq=entry.seq,
                expected=recomputed,
                actual=entry.entry_hash,
                reason="entry_hash does not match recomputed hash (content tampered)",
            )
        prev_hash = entry.entry_hash
        verified += 1

    return verified


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
    "AuditFieldError",
    "AuditPayloadError",
    "AuditSink",
    "AuditWriteError",
    "AuditWriter",
    "BufferedAuditCollector",
    "BufferedAuditRecord",
    "iter_entries",
    "open_ingest_audit",
    "read_entries",
    "verify_chain",
]
