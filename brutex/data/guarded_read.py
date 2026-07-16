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

from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    from collections.abc import Sequence

    import polars as pl

__all__ = [
    "READ_DEADLINE_S",
    "NotRegularFileError",
    "ReadStallError",
    "open_regular_binary",
    "read_parquet_glob_regular",
    "read_parquet_regular",
    "read_parquet_schema_regular",
    "read_regular_bytes",
    "read_regular_bytes_deadline",
    "read_regular_text",
    "read_regular_text_deadline",
]

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
    byte-identical content to ``Path.read_bytes()``.
    """
    try:
        return run_with_deadline(
            lambda: read_regular_bytes(path),
            deadline_s,
            what=what,
            thread_name="guarded-read",
        )
    except (DeadlineExceeded, DeadlineSpawnError) as exc:
        raise ReadStallError(f"{what} stalled past {deadline_s:g}s: {path}") from exc


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
