"""Typed exceptions for the BruteX data lake."""

from __future__ import annotations

from brutex.models import BruteXError


class DataLakeError(BruteXError):
    """Base class for every error raised by :mod:`brutex.data`."""


class PartitionError(DataLakeError):
    """Raised when a partition path cannot be computed or parsed.

    Examples:
    * An interval name not in the canonical intervals table
    * A symbol with an illegal character for the filesystem
    * A malformed partition path passed to :func:`parse_partition_path`
    """


class WriterError(DataLakeError):
    """Raised when a Parquet write fails or would violate idempotence.

    Examples:
    * Attempting to write an empty bar batch
    * Destination file exists with a different content hash
    * Underlying ``polars`` import fails at runtime
    """


class ResampleInputError(DataLakeError):
    """The 1-minute input to the canonical TF resampler violates its ordering contract.

    :func:`brutex.data.tf_resampler.resample_1m_to_tf` requires a null-free,
    STRICTLY-INCREASING ``ts_utc`` column (DEC-RESAMPLER-INPUT-GUARD-001). A
    duplicate timestamp silently DOUBLE-COUNTS the bucket's volume and can
    pollute high/low; an out-of-order row inverts the bucket's open/close
    (``maintain_order=True`` + ``first()``/``last()`` trust input order). This
    typed error makes the violation LOUD at the resampler boundary — the same
    raise-on-non-monotonic convention the feed resampler
    (``brutex.feed.errors.ResampleError``) and the execution driver already
    enforce — so corrupted input can never masquerade as a clean aggregate
    (Golden Rule #1).
    """


class LakeWriteHalt(DataLakeError):
    """A lake write hit an UNRECOVERABLE host condition — disk full / out of memory.

    Unlike a per-symbol :class:`WriterError`, this is a PULL-WIDE stop: a full disk
    or an exhausted heap fails EVERY remaining window identically, so it must
    propagate past the per-symbol / per-contract quarantine (it is a member of both
    ingest spines' ``_GLOBAL_STOP_ERRORS``) and halt the pull GRACEFULLY — never a
    silent crash, never a half-written partition, never fabricated data (Golden Rule
    #1). The half-written temp file is always cleaned up before this is raised.

    ``reason`` is a non-secret machine token: ``"disk_full"`` (an ``OSError`` with
    errno ``ENOSPC``), ``"out_of_memory"`` (a ``MemoryError``), or
    ``"volume_wedged"`` — the lake volume itself is judged wedged, from EITHER of two
    detectors: the keepalive write-probe failing for longer than the
    ``BRUTEX_VOLUME_WEDGE_HALT_S`` bound (a spun-down / wedged external volume; raised
    FROM THE PRODUCER THREAD at the
    :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.submit` wedge gate,
    DEC-VOLUME-KEEPALIVE-001), or the per-partition write-health breaker
    (:class:`~brutex.data.write_breaker.WriteHealthBreaker`,
    DEC-WRITE-HEALTH-BREAKER-001 — 2 consecutive write-defers open a partition, 3
    DISTINCT open partitions trip the halt; raised from the window TAIL that observed
    the tripping defer);
    or ``"worker_pool_broken"`` (RESERVED —
    DEC-HARDWARE-ENVELOPE-001: NO shipped code raises it today; the parallel
    prebuild pivoted to a ``ThreadPoolExecutor``, where a worker cannot die
    separately from the process. The token is kept so a FUTURE process-pool
    stage can map a ``BrokenProcessPool`` to an honest pull-wide halt — reaching
    the CRITICAL Telegram alert — instead of an anonymous crash).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"lake write halted: {reason}")
        self.reason = reason

    def __reduce__(self) -> tuple[type[LakeWriteHalt], tuple[str]]:
        """Pickle as constructor args — REQUIRED for a process-pool crossing.

        The default ``BaseException`` reduce re-feeds ``args`` (the FORMATTED
        message) to ``__init__``, so an unpickled copy read
        ``"lake write halted: lake write halted: <reason>"`` — a doubled
        message (empirically proven; the DEC-GREEKS-PARALLEL-001 pickle
        round-trip suite pins the fix). ``type(self)`` keeps a same-signature
        subclass safe; a subclass CHANGING the constructor signature must
        override ``__reduce__``.
        """
        return (type(self), (self.reason,))


class PartitionLockTimeout(DataLakeError):
    """A cross-process partition write-lock could not be acquired within the deadline.

    The lake partition :func:`~brutex.data.partition_lock.partition_write_lock` acquires
    a bounded (never unconditionally blocking) advisory ``flock``: it retries a
    non-blocking ``LOCK_EX | LOCK_NB`` under a short deadline so a WEDGED peer holding the
    lock can NEVER pin a worker until the 600 s pull watchdog force-fails it. When the
    deadline expires this is raised — a TRANSIENT / RETRYABLE signal (it subclasses
    :class:`DataLakeError`, NOT :class:`LakeWriteHalt`, so it is NOT a pull-wide stop):
    the ingest spines catch it at the window boundary, record the window ``FAILED``, and
    DEFER it to the next pull, exactly like a 429 / timeout transient — never a crash,
    never fabricated data (Golden Rule #1). The peer that is genuinely making progress
    finishes its read-modify-write and the deferred window succeeds on the re-pull.

    ``timeout_seconds`` is the deadline that was exhausted (non-secret) for the log.
    """

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(
            f"partition write-lock not acquired within {timeout_seconds:g}s "
            "(peer holding the lock); window deferred to the next pull"
        )
        self.timeout_seconds = timeout_seconds

    def __reduce__(self) -> tuple[type[PartitionLockTimeout], tuple[float]]:
        """Pickle as constructor args — REQUIRED for a process-pool crossing.

        The default ``BaseException`` reduce re-calls
        ``PartitionLockTimeout(<formatted message string>)``, and the ``:g``
        format spec then blows up on the string — ``pickle.loads`` raised
        ``ValueError: Unknown format code 'g' for object of type 'str'``
        (empirically proven pre-fix). An unpicklable exception escaping a
        pool worker poisons the WHOLE pool into a generic
        ``BrokenProcessPool`` (DEC-GREEKS-PARALLEL-001), so the round-trip
        must be exact. Same subclass note as :class:`LakeWriteHalt`.
        """
        return (type(self), (self.timeout_seconds,))


class WriteDeadlineExceeded(DataLakeError):
    """A single durable lake write (``sink.append``) blew its wall-clock deadline.

    The lake write does a READ-modify-WRITE of a month partition ending in an
    ``os.fsync`` + atomic ``os.replace`` (:func:`~brutex.data.atomic_io.atomic_write_via`).
    Those syscalls carry NO inherent wall-clock bound, so a WEDGED durable write — a hung
    ``fsync`` / ``os.replace`` on a stalled external SSD / NFS mount that NEVER raises — would
    block its consumer thread forever, fill its bounded queue, and eventually wedge the
    PRODUCER (the fetch lane) on back-pressure at 0 r/s while the rate controller still
    "allows" the ceiling (DEC-FETCH-HUNG-WRITE-DEADLINE-001). This is DISTINCT from
    :class:`PartitionLockTimeout`, which bounds only the cross-process ``flock`` ACQUIRE — a
    hung write happens AFTER the lock is held, inside the merge / render / fsync / replace, and
    was previously unbounded.

    :func:`~brutex.adapters.groww.write_deadline.append_with_deadline` runs ``sink.append`` under
    a generous wall-clock deadline (:data:`~brutex.adapters.groww.constants.GROWW_WRITE_DEADLINE_S`)
    and raises this on expiry. Like :class:`PartitionLockTimeout` it is a TRANSIENT / RETRYABLE
    signal — it subclasses :class:`DataLakeError`, NOT :class:`LakeWriteHalt`, so it is NOT a
    pull-wide stop: the ingest spines catch it at the window boundary, record the window
    ``FAILED``, and DEFER it to the next pull, exactly like a 429 / lock-timeout transient. The
    fetch lane is FREED and the window is never silently lost (Golden Rule #1).

    **Honest envelope (Golden Rule #10):** the abandoned write runs in a daemon worker thread,
    so a C-level wedged ``fsync`` thread CANNOT be killed — it dies with the process. The
    guarantee is "the fetch lane is never blocked" (the producer is released + the window is
    deferred), NOT "the zombie write thread is terminated". A persistently-wedged volume no
    longer burns the whole pull deferring window after window: the write-health breaker
    (:class:`~brutex.data.write_breaker.WriteHealthBreaker`, DEC-WRITE-HEALTH-BREAKER-001)
    accumulates these defers per health key — the spot series+month partition; the F&O
    UNDERLYING (a per-contract key could never reach the shipped consecutive limit, so the
    F&O key groups the chain's sibling contracts) — and escalates a multi-key wedge to
    ``LakeWriteHalt("volume_wedged")`` — a graceful pull-wide halt with the CRITICAL
    Telegram alert (the operator's signal to act, now pushed not just logged). Honest
    floor: a pull whose defers span fewer than 3 distinct health keys still defers
    per-window without a halt.

    ``deadline_seconds`` is the deadline that was exhausted (non-secret) for the log.
    """

    def __init__(self, deadline_seconds: float) -> None:
        super().__init__(
            f"durable lake write not completed within {deadline_seconds:g}s "
            "(wedged fsync/replace); window deferred to the next pull"
        )
        self.deadline_seconds = deadline_seconds

    def __reduce__(self) -> tuple[type[WriteDeadlineExceeded], tuple[float]]:
        """Pickle as constructor args — same latent ``:g``-re-format bug as
        :class:`PartitionLockTimeout` (``pickle.loads`` raised ``ValueError``
        pre-fix). Not reachable from the Greeks pool workers today (the
        deadline wrapper is Groww-adapter-only), fixed as cheap latent-bug
        hardening while in this file (DEC-GREEKS-PARALLEL-001)."""
        return (type(self), (self.deadline_seconds,))


class GreeksEnrichPoolBroken(DataLakeError):
    """The Greeks-enrichment worker POOL died before completing its units.

    Raised by the parallel :func:`~brutex.data.greeks_enrich.enrich_greeks_for_targets`
    driver when :class:`concurrent.futures.process.BrokenProcessPool` surfaces — a
    worker process terminated ABRUPTLY (killed, crashed) instead of returning a
    result. HONEST envelope (Golden Rule #10): an OOM SIGKILL, an operator ``kill
    -9`` and a hard interpreter crash are NOT distinguishable from
    ``BrokenProcessPool`` (worker exit codes are not observable through the public
    executor API), so ``detail`` states the ambiguity instead of guessing a cause.

    Like :class:`LakeWriteHalt` this is a HOST-RESOURCE condition, not a
    per-contract data fault: it is recognised by the dashboard job layer's
    :func:`~brutex.dashboard.pull_jobs.is_pull_halt` global-stop predicate, so it
    re-raises through the fenced ``_enrich_greeks_from_minute`` wrapper and fires
    the CRITICAL Telegram pull-halt alert (DEC-GREEKS-PARALLEL-001). The
    just-pulled 1-minute candles are already durable; the overlay is re-derivable
    on the next pull (idempotent, stamp-driven — Golden Rule #7).

    ``detail`` is the non-secret honest description carried into the message.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"greeks enrichment worker pool broken: {detail}")
        self.detail = detail

    def __reduce__(self) -> tuple[type[GreeksEnrichPoolBroken], tuple[str]]:
        """Pickle as constructor args — the same discipline as the fixes above,
        so this class can never re-introduce the doubled-message bug."""
        return (type(self), (self.detail,))


class CatalogError(DataLakeError):
    """Raised when a DuckDB catalog operation fails or is malformed.

    Examples:
    * Duplicate ``(symbol, interval, yyyy, mm)`` row with divergent hash
    * ``duckdb`` import fails at runtime
    * Integrity check across catalog + filesystem reports a drift
    """


def parquet_read_skip_errors() -> tuple[type[BaseException], ...]:
    """Exception types that mean a single Parquet file is unreadable → skip / quarantine it.

    The ONE data-layer source of truth (CLAUDE.md §11 COMMON) for "this one Parquet
    file is corrupt / version-incompatible / fault — do not crash the whole pass over
    it". ``polars`` raises a :class:`polars.exceptions.PolarsError` (e.g. a
    ``ComputeError`` *"could not read parquet"* on a truncated / garbage file), and
    ``OSError`` covers file-level faults (a vanished / unreadable file). NARROW on
    purpose: a genuine code bug (``ValueError`` / ``KeyError`` / …) still propagates,
    so a logic error is never silently swallowed (Golden Rule #1).

    The dashboard ``/movers`` + ``/db`` read paths *widen* this set with
    :class:`duckdb.Error` (they read via DuckDB too) in
    :func:`brutex.dashboard.movers_resilient.movers_read_skip_errors`, which DELEGATES
    here — so the polars + OSError half lives in exactly one place. ``polars`` is
    imported inside the call (the data layer already lazy-imports it in the writers),
    so this stays importable without paying the polars import at module load.
    """
    import polars as pl

    return (pl.exceptions.PolarsError, OSError)
