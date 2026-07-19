"""The COMMON two-defense gated file reader (DEC-LAKE-READ-INODE-GUARD-001).

Every plain ``Path.read_text()`` / ``Path.read_bytes()`` / ``path.open("rb")``
/ ``pl.read_parquet(path)`` at a lake/route read boundary shares TWO latent
liveness hazards this module closes at ONE chokepoint (CLAUDE.md §11 COMMON —
no per-site copy):

(a) **Planted-inode shape** — a FIFO planted at the path makes the *open*
    itself block for a writer FOREVER (no exception, so no degrade arm ever
    fires), and a ``/dev/zero`` char-device symlink streams infinite zeros
    (its ``st_size`` reads 0, so a pre-read size cap is NO defence). Closed by
    the fd-level regular-file gate the JSON sidecar loader has carried since
    DEC-LAKE-STATS-MANIFEST-001 #1614 / DEC-PULL-SCOPED-ALL-ONECLICK-001
    round 7: ``os.open(path, O_RDONLY | O_NONBLOCK)`` → ``S_ISREG`` on
    ``os.fstat(fd)`` of the SAME descriptor (never a pre-open ``stat()`` —
    TOCTOU) → read the VALIDATED inode. Home:
    :func:`brutex.data.sidecar_read.open_regular_binary` /
    :func:`~brutex.data.sidecar_read.read_regular_bytes` (re-exported here);
    a non-regular inode raises :class:`NotRegularFileError` — an ``OSError``
    subclass, so it folds into every site's EXISTING unreadable-file degrade
    arm (``parquet_read_skip_errors()`` carries ``OSError``; the route
    readers' ``except OSError`` corrupt arms catch it) as a loud, counted
    skip / typed refusal — never a hang, never a silent pass.

(b) **Wedged-volume stall** — a NORMAL regular file on a hung external
    SSD/NFS volume (the §8 8 TB-external-SSD scenario): ``S_ISREG`` passes,
    then the read blocks indefinitely. Closed by the deadline-wrapped
    variants (:func:`read_regular_bytes_deadline` /
    :func:`read_regular_text_deadline`) riding the ONE COMMON freeze guard
    :func:`brutex.config.deadline.run_with_deadline` (the rate_bridge_cache /
    write_deadline pattern). A blown deadline raises :class:`ReadStallError`
    — ALSO an ``OSError`` subclass, so the SAME existing degrade arms absorb
    it with zero per-site except-clause churn. The abandoned worker is a
    daemon (the #999 stance: the wedged C-level read cannot be killed — the
    guarantee is the CALLER is released + the site's degrade fires, not that
    the zombie thread dies). Deadline sites are the TIER-1 set: reads
    reachable from a dashboard route or performed while HOLDING a lock (the
    sweep checkpoint read under the lake-root pull single-flight — a stall
    there blocks EVERY later pull until the job is killed).

Regular-file behavior is BYTE-IDENTICAL everywhere: ``O_NONBLOCK`` is inert
for reads on a regular file (POSIX), the returned bytes/frames are exactly
the plain read's, and the parquet helpers hand the validated OPEN handle to
polars (a file object is read literally — glob interpretation is structurally
impossible, subsuming the #1594 round-15 ``glob=False`` law at these sites).

Import posture: module-level imports are stdlib + the stdlib-only
:mod:`brutex.config.deadline` + the stdlib-only
:mod:`brutex.data.sidecar_read` — WHEEL-FREE at import (polars is imported
function-locally inside the parquet helpers ONLY), so light consumers
(config / crossverify / observability / nightly / backup) never pay a polars
import and the CLI ``version`` polars-free fast path stays intact.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from brutex.config.deadline import (
    DeadlineExceeded,
    DeadlineSpawnError,
    run_with_deadline,
)
from brutex.data.sidecar_read import (
    NotRegularFileError,
    open_regular_binary,
    read_regular_bytes,
)

if TYPE_CHECKING:  # pragma: no cover — typing-only import (polars stays lazy)
    from collections.abc import Callable, Sequence

    import polars as pl

__all__ = [
    "READ_DEADLINE_S",
    "NotRegularFileError",
    "ReadStallError",
    "open_regular_binary",
    "read_capped_bytes_deadline",
    "read_parquet_glob_regular",
    "read_parquet_regular",
    "read_parquet_schema_regular",
    "read_regular_bytes",
    "read_regular_bytes_deadline",
    "read_regular_text",
    "read_regular_text_deadline",
    "scan_parquet_regular",
]

#: The deadline-core result type. A module-level ``TypeVar`` (NOT the PEP 695
#: ``def _deadline_read[T](...)`` inline form) so the name is unconditionally
#: bound at runtime — CodeQL modelled the inline type parameter referenced by
#: the nested ``_worker() -> T`` annotation as a possibly-uninitialized local
#: (code-scanning alert #1160; the zero-alert hygiene fix, semantics
#: identical).
_T = TypeVar("_T")

#: The default wall-clock bound for the deadline-wrapped variants. Small JSON
#: sidecars/artifacts read in well under a second on a healthy volume; 5 s is
#: the audited conservative bound the pull-lease I/O guard already uses
#: (``BRUTEX_PULL_LEASE_IO_DEADLINE_S`` default — DEC-ACCOUNT-LEASE-CROSSHOST-001
#: #1588), generous enough that a merely-slow disk never false-trips while a
#: genuinely wedged volume releases the caller in bounded time.
READ_DEADLINE_S = 5.0


class ReadStallError(OSError):
    """A guarded read blew its wall-clock deadline — a wedged-volume stall.

    DEC-LAKE-READ-INODE-GUARD-001 defense (b): raised by the
    ``*_deadline`` readers when :func:`run_with_deadline` expires (or its
    worker daemon fails to SPAWN under thread exhaustion — either way the
    bytes never arrived). Deliberately an :class:`OSError` subclass so it
    folds into every existing unreadable-file degrade arm exactly like
    :class:`NotRegularFileError` — the site's loud counted skip / typed
    refusal / None+WARN arm fires instead of the caller hanging forever.
    The underlying :class:`DeadlineExceeded` / :class:`DeadlineSpawnError`
    rides ``__cause__``.
    """


def read_parquet_regular(path: Path | str, *, columns: Sequence[str] | None = None) -> pl.DataFrame:
    """``pl.read_parquet`` through the fd-validated regular-file gate.

    Defense (a) for the LAKE parquet read boundaries: opens *path* via
    :func:`open_regular_binary` (O_NONBLOCK + ``S_ISREG`` on the fd) and
    hands the validated OPEN handle to polars — the read targets exactly the
    inode that passed the gate (no TOCTOU re-open), and a file object is
    read LITERALLY (no glob interpretation — the #1594 round-15 law holds by
    construction). A non-regular inode raises :class:`NotRegularFileError`
    (an ``OSError`` — a ``parquet_read_skip_errors()`` member, so every
    scan-skip / quarantine / movers-skip arm absorbs it unchanged); polars
    faults raise their ordinary ``PolarsError``. Regular files return
    byte-identical frames to ``pl.read_parquet(path, columns=...,
    glob=False)``.
    """
    import polars as pl

    with open_regular_binary(path) as handle:
        if columns is None:
            return pl.read_parquet(handle)
        return pl.read_parquet(handle, columns=list(columns))


def read_parquet_glob_regular(
    pattern: str, *, columns: Sequence[str] | None = None
) -> pl.DataFrame:
    """``pl.read_parquet(<glob>)`` with per-file fd validation — defense (a).

    The multi-file twin of :func:`read_parquet_regular` for the lake month
    globs (``…/**/*.parquet``): the matching files are enumerated with
    Python's recursive glob in SORTED order (measured identical to polars
    1.42's own glob expansion order on the lake's ``<yyyy>/<mm>.parquet``
    layout), each is opened through :func:`open_regular_binary` (a planted
    FIFO/device among the partition files raises
    :class:`NotRegularFileError` — a ``parquet_read_skip_errors()`` member —
    instead of hanging the whole read), and the validated OPEN handles are
    handed to polars as a list (read literally — frames byte-identical to the
    glob read on a healthy lake, concatenated in the same sorted order). A
    ZERO-match pattern delegates to the plain glob read — zero candidate
    files means zero inode risk, and polars raises its genuine
    empty-expansion ``ComputeError`` (error contract byte-identical).
    """
    import glob as _glob

    import polars as pl

    files = sorted(_glob.glob(pattern, recursive=True))
    if not files:
        if columns is None:
            return pl.read_parquet(pattern)
        return pl.read_parquet(pattern, columns=list(columns))
    with ExitStack() as stack:
        handles = [stack.enter_context(open_regular_binary(name)) for name in files]
        if columns is None:
            return pl.read_parquet(handles)
        return pl.read_parquet(handles, columns=list(columns))


def scan_parquet_regular(
    source: str | Path | Sequence[str | Path],
    *,
    missing_columns: Literal["insert", "raise"] = "raise",
    extra_columns: Literal["ignore", "raise"] = "raise",
) -> pl.LazyFrame:
    """``pl.scan_parquet`` with an EAGER per-file fd probe — defense (a) for LAZY scans.

    The lazy twin of :func:`read_parquet_glob_regular`
    (DEC-LAKE-READ-INODE-GUARD-002 — the verifier-identified residual):
    ``pl.scan_parquet`` defers every file OPEN to ``.collect()``, so a planted
    FIFO among the partition files hangs the COLLECT deep in C-level
    ``fifo_open`` where no Python guard can fire. This helper fd-probes every
    candidate file EAGERLY at scan-build time (``os.open(O_RDONLY|O_NONBLOCK)``
    → ``S_ISREG`` on the fd → close — the probe itself can never hang: a
    writer-less FIFO open is immediate under ``O_NONBLOCK``), THEN returns
    polars' OWN lazy scan — lazy semantics, projection/predicate pushdown and
    polars' own glob expansion/order are all preserved BYTE-IDENTICALLY for
    regular files (a path/glob source is handed to polars UNTOUCHED; an
    explicit sequence is handed over as the normalized ``list[str]`` in the
    caller's exact order — an identity for the production ``list[str]`` call
    sites, order + membership preserved).

    *source* shapes: an explicit file list (each entry probed directly — the
    ``ist_span_partition_files`` / ``month_partition_files`` sites) or a
    single path/glob string (candidates enumerated with Python's recursive
    glob — measured identical to polars 1.42's own expansion on the lake's
    ``<yyyy>/<mm>.parquet`` layout; a ZERO-match pattern probes nothing and
    delegates to polars' genuine empty-expansion behavior, error contract
    byte-identical). A non-regular inode raises :class:`NotRegularFileError`
    — an ``OSError``, a ``parquet_read_skip_errors()`` member — EAGERLY at
    scan-build (matching the guarded eager readers' error timing; a vanished
    list entry likewise raises its ``OSError`` eagerly). HONEST TOCTOU
    envelope: the collect re-opens by PATH, so an inode swapped in the
    probe→collect window is out of this gate's reach — the same disclosed
    residual class as every path-level guard in this module; the probe closes
    the PLANTED-inode class (the shape that exists before the read starts).
    """
    import glob as _glob

    import polars as pl

    if isinstance(source, str | Path):
        # Path/glob-string arm: probe the expanded candidates, then hand polars
        # the ORIGINAL source so its own glob expansion/order laws apply.
        for name in sorted(_glob.glob(str(source), recursive=True)):
            with open_regular_binary(name):
                pass
        return pl.scan_parquet(source, missing_columns=missing_columns, extra_columns=extra_columns)
    # Explicit-list arm: probe each entry, then hand polars the normalized
    # `list[str]` in the CALLER's order (str(entry) is an identity for the
    # production `list[str]` call sites — order + membership preserved).
    # glob=False (lap-8 fold re-derive — DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001,
    # the #1594 round-15 metachar law): an explicit file LIST is LITERAL — a
    # `[`-carrying list entry must never be glob-interpreted at collect into
    # its siblings' files (which the fd probe above read literally — the
    # probe-set and the read-set must be the SAME files, or a passing probe
    # blesses a diverging read). The path/glob-STRING arm above deliberately
    # keeps polars' glob semantics — glob expansion is that arm's purpose.
    candidates: list[str] = [str(entry) for entry in source]
    for name in candidates:
        with open_regular_binary(name):
            pass
    return pl.scan_parquet(
        candidates, missing_columns=missing_columns, extra_columns=extra_columns, glob=False
    )


def read_parquet_schema_regular(path: Path | str) -> dict[str, Any]:
    """``pl.read_parquet_schema`` through the fd-validated regular-file gate.

    The footer-probe twin of :func:`read_parquet_regular` (the
    ``_missing_columns_reason`` alien-schema classifier's read). Same error
    contract: a non-regular inode raises :class:`NotRegularFileError`;
    regular files return the identical schema mapping.
    """
    import polars as pl

    with open_regular_binary(path) as handle:
        return dict(pl.read_parquet_schema(handle))


def read_regular_text(path: Path | str, *, encoding: str = "utf-8") -> str:
    """Whole-file text through the fd-validated regular-file gate — defense (a) only.

    The tier-2 ``Path.read_text`` drop-in (operator/sweep-batch sites that
    hold no lock and serve no route — a stall there is killable, so only the
    planted-inode shape needs closing): :func:`read_regular_bytes` then
    ``decode(encoding)``. Error contract mirrors ``Path.read_text``
    (``OSError`` for I/O — incl. :class:`NotRegularFileError` — and
    ``UnicodeDecodeError`` for junk bytes); regular healthy files return
    byte-identical text.
    """
    return read_regular_bytes(path).decode(encoding)


#: Per-path stall gate (the DEC-LAKE-READ-INODE-GUARD-001 hostile-review
#: MINOR-2 fold — the rate_bridge_cache non-blocking single-flight posture):
#: when a deadline read of a path BLOWS its deadline, the path is registered
#: here with the abandoned worker's completion event.  While that event is
#: UNSET (the wedged read is still in flight on its abandoned daemon), every
#: later ``*_deadline`` read of the SAME path refuses INSTANTLY with
#: :class:`ReadStallError` instead of spawning ANOTHER abandoned daemon — a
#: polled route on a persistently wedged volume previously leaked one daemon
#: per poll, self-limited only by the OS thread ceiling.  A SET event
#: self-heals lazily at the next gate consult (the late return arrived —
#: normal reads resume).  Healthy reads never register, so regular-file
#: behavior stays byte-identical (one dict probe under the lock per read).
_STALL_GATE_LOCK = threading.Lock()
_STALLED_READS: dict[str, threading.Event] = {}


def _deadline_read(  # noqa: UP047 — module-level TypeVar by design: CodeQL alert #1160
    path: Path | str,
    read_fn: Callable[[], _T],
    *,
    deadline_s: float,
    what: str,
) -> _T:
    """Run *read_fn* under the per-path stall gate + the COMMON freeze guard.

    The ONE tier-1 deadline core every ``*_deadline`` reader delegates to:
    (1) consult the stall gate — a path whose PRIOR deadline read is still
    wedged on its abandoned daemon refuses instantly (no new thread); a
    completed late return self-heals the entry; (2) run *read_fn* under
    :func:`run_with_deadline`; (3) on expiry, register the path (skipped in
    the completed-just-after-expiry race — the worker's ``finally`` set the
    event first, so nothing is left to wait for; a
    :class:`DeadlineSpawnError` spawned NO worker, so nothing registers) and
    raise :class:`ReadStallError` with the cause chained.
    """
    key = str(path)
    with _STALL_GATE_LOCK:
        prior = _STALLED_READS.get(key)
        if prior is not None:
            if prior.is_set():
                # The wedged daemon finally returned — self-heal the entry.
                del _STALLED_READS[key]
            else:
                raise ReadStallError(
                    f"{what} refused — a prior guarded read of this path is "
                    f"still stalled (no new reader thread spawned): {path}"
                )
    done = threading.Event()

    def _worker() -> _T:
        try:
            return read_fn()
        finally:
            done.set()

    try:
        return run_with_deadline(
            _worker,
            deadline_s,
            what=what,
            thread_name="guarded-read",
        )
    except (DeadlineExceeded, DeadlineSpawnError) as exc:
        if isinstance(exc, DeadlineExceeded) and not done.is_set():
            with _STALL_GATE_LOCK:
                _STALLED_READS[key] = done
        raise ReadStallError(f"{what} stalled past {deadline_s:g}s: {path}") from exc


def read_regular_bytes_deadline(
    path: Path | str,
    *,
    deadline_s: float = READ_DEADLINE_S,
    what: str = "guarded file read",
) -> bytes:
    """Whole-file bytes with BOTH defenses: the shape gate + the stall bound.

    The tier-1 reader (route-reachable / lock-holding sites): runs
    :func:`read_regular_bytes` under the COMMON freeze guard. A non-regular
    inode raises :class:`NotRegularFileError` (the gate fires INSIDE the
    worker and re-raises unchanged on the caller thread); a wedged REGULAR
    file raises :class:`ReadStallError` after *deadline_s*; every other
    fault raises its ordinary ``OSError``. Regular healthy files return
    byte-identical content to ``Path.read_bytes()``. Per-path single-flight:
    while a prior stalled read of the SAME path is still abandoned-in-flight
    the call refuses instantly with :class:`ReadStallError` — never a second
    daemon per poll (see :data:`_STALLED_READS`).
    """
    return _deadline_read(path, lambda: read_regular_bytes(path), deadline_s=deadline_s, what=what)


def read_capped_bytes_deadline(
    path: Path | str,
    *,
    cap: int,
    deadline_s: float = READ_DEADLINE_S,
    what: str = "guarded file read",
) -> tuple[bytes, bool]:
    """Capped bytes + the ONE-byte over-probe with BOTH defenses (tier-1).

    The bounded-read twin of :func:`read_regular_bytes_deadline` for the
    polled route sites that must never read an out-of-band multi-GB file
    whole (the #1614 ``read(cap)`` + one-byte over-cap probe idiom — the
    health_hub ``report.json`` read): opens *path* through
    :func:`open_regular_binary` INSIDE the deadline worker, reads at most
    ``cap + 1`` bytes, and returns ``(raw, over)`` where ``over`` is True
    iff the file holds MORE than *cap* bytes (the caller's oversized
    degrade). Error contract identical to the whole-file twin
    (:class:`NotRegularFileError` / :class:`ReadStallError` / ordinary
    ``OSError`` — all ``OSError``), same per-path single-flight law.
    """

    def _read() -> tuple[bytes, bool]:
        with open_regular_binary(path) as handle:
            raw = handle.read(cap)
            over = handle.read(1)
        return raw, bool(over)

    return _deadline_read(path, _read, deadline_s=deadline_s, what=what)


def read_regular_text_deadline(
    path: Path | str,
    *,
    deadline_s: float = READ_DEADLINE_S,
    what: str = "guarded file read",
    encoding: str = "utf-8",
) -> str:
    """Whole-file text with BOTH defenses — the ``read_text`` drop-in.

    :func:`read_regular_bytes_deadline` then ``decode(encoding)``: the exact
    error split of ``Path.read_text(encoding=...)`` (``OSError`` for I/O,
    ``UnicodeDecodeError`` for junk bytes — the decode runs on the CALLER
    thread, never inside the guard) plus the two guarded ``OSError``
    subclasses. Regular healthy files return byte-identical text.
    """
    return read_regular_bytes_deadline(path, deadline_s=deadline_s, what=what).decode(encoding)
