"""Bound the registry/coverage DuckDB writes by a wall-clock deadline.

DEC-STORE-WRITE-DEADLINE-001 — the metadata twin of DEC-FETCH-HUNG-WRITE-DEADLINE-001
(#999). The registry (:class:`~brutex.data.registry_store.RegistryStore`) + coverage
(:meth:`~brutex.data.instrument_store.InstrumentStore.upsert_coverage`) DuckDB writes
carry NO inherent wall-clock bound, and each runs while the CALLER holds the store's
Python lock — so a WEDGED write on a stalled external SSD (a hang that never raises)
would hold that lock forever and freeze EVERY metadata worker / dashboard route behind
it, with no guard firing. The 600 s pull watchdog only *observes* the stall; it never
frees the lane.

:class:`StoreWriteDeadlineGuard` is the ONE freeze guard for those writes. It reuses
the COMMON :func:`~brutex.config.deadline.run_with_deadline` primitive (NO copy,
CLAUDE.md §11) so a wedged write raises the TRANSIENT
:class:`~brutex.data.errors.StoreWriteDeadlineExceeded` after the deadline — the
metadata-pull catch sites defer the underlying's cell/job to the NEXT pull, exactly
like the ingest spines defer a window on
:class:`~brutex.data.errors.WriteDeadlineExceeded`. Never a pull-wide stop, never a
:class:`~brutex.data.errors.LakeWriteHalt`.

**The wedge latch (fail-fast).** Unlike the lake sink (whose per-partition writes are
independent), ALL of one store's writes go through ONE DuckDB connection whose internal
mutex the abandoned zombie write still holds. Waiting a fresh 90 s deadline per
subsequent batch would burn the whole pull one deadline at a time — so the guard
latches: while the abandoned write is still in flight (``wedged``), every later
:meth:`~StoreWriteDeadlineGuard.run` raises the SAME transient error instantly
(``latched=True``) with NO new thread, NO deadline wait, and NO guard-level log (the
caller-layer defer events carry the context — a per-batch guard log would storm). The
latch is token-identity-matched and SELF-CLEARS: when the zombie's wedged statement
finally returns (the volume recovered), its ``finally`` clears the token, logs one
:data:`STORE_WRITE_RECOVERED_EVENT`, and the store resumes.

**Honest envelope (Golden Rule #10):** the abandoned daemon write CANNOT be killed —
it dies with the process. The guarantee is "no pull driver/worker lane is ever blocked
longer than one deadline ONCE the wedge latch is set; every Python store lock is
caller-held and deterministically released on breach; the affected underlying is
deferred, loudly WARNed, and re-attempted on the next pull; while the volume stays
wedged, new guarded writes — and, store-wide, every non-exempt typed store method —
fail fast and loud; when the volume recovers the zombie completes its idempotent keyed
write, the wedge self-clears, and the store resumes" — NOT "the zombie write thread is
terminated". NAMED RESIDUAL: the latch is set only AT the first breach, so a caller
whose statement reached the wedged connection BEFORE that breach (the latch not yet
set) still blocks the lane unboundedly on the conn's internal mutex — it dies with the
process, exactly like the zombie itself; only callers arriving AFTER the breach are
protected. A late-completing zombie write is harmless by construction: every guarded
statement is an idempotent keyed ``INSERT OR REPLACE`` / ``UPDATE`` /
``DELETE``+``INSERT`` carrying exactly the values the healthy path would have written —
and the contract batch additionally runs in ONE explicit transaction (fix round 3), so
a zombie that dies with the process commits NOTHING (never a durable mid-batch PREFIX
of legs, which would wrongly settle the Phase-2 job and permanently skip an expired
expiry's remaining legs) while one that later completes commits the FULL batch.

O(1) per write BATCH: one daemon thread + one :class:`threading.Event` per
:meth:`~StoreWriteDeadlineGuard.run` (never per row); a wedged store costs one pointer
comparison and zero threads. Import-cheap and cycle-proof: this module imports only the
config-layer primitive, ``structlog``, and the exception type.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import structlog

from brutex.config.deadline import DeadlineExceeded, resolve_env_seconds, run_with_deadline
from brutex.data.errors import StoreWriteDeadlineExceeded

_LOG = structlog.get_logger(__name__)

__all__ = [
    "STORE_CLOSE_SKIPPED_EVENT",
    "STORE_WRITE_DEADLINE_ENV",
    "STORE_WRITE_DEADLINE_EVENT",
    "STORE_WRITE_DEADLINE_S",
    "STORE_WRITE_RECOVERED_EVENT",
    "StoreWriteDeadlineGuard",
    "resolve_store_write_deadline_seconds",
]

#: Environment override for the store-write deadline (operator escape hatch, no code
#: change) — the registry/coverage twin of ``BRUTEX_GROWW_WRITE_DEADLINE_S``.
STORE_WRITE_DEADLINE_ENV: Final[str] = "BRUTEX_REGISTRY_WRITE_DEADLINE_S"

#: Default wall-clock deadline for one registry/coverage DuckDB write batch. Mirrors
#: the audited :data:`~brutex.adapters.groww.constants.GROWW_WRITE_DEADLINE_S` (90 s):
#: these writes are ms-scale keyed upserts, so 90 s is GENEROUS (only a genuine hang
#: can exhaust it — the healthy path never waits), and it sits far under the 600 s
#: pull watchdog so the defer path always wins over a watchdog kill. HONEST (Golden
#: Rule #10): the figure is inherited from the #999 lake-write audit, NOT freshly
#: benchmarked for DuckDB; env-tunable via :data:`STORE_WRITE_DEADLINE_ENV`.
STORE_WRITE_DEADLINE_S: Final[float] = 90.0

#: WARN — the guard's own deadline fired on an ACTUAL breach (once per breach, never
#: per latched fail-fast): ``store`` / ``op`` / ``deadline_seconds``.
STORE_WRITE_DEADLINE_EVENT: Final[str] = "store.write_deadline_exceeded"

#: INFO — the abandoned zombie write finally completed (the volume recovered) and the
#: wedge latch self-cleared: ``store`` / ``op``.
STORE_WRITE_RECOVERED_EVENT: Final[str] = "store.write_recovered"

#: WARN — ``close()`` skipped the raw connection close because a zombie write is still
#: wedged inside it (closing under a mid-``execute`` zombie could turn a bounded block
#: into a crash): ``store`` / ``path``.
STORE_CLOSE_SKIPPED_EVENT: Final[str] = "store.close_skipped_wedged"


def resolve_store_write_deadline_seconds(raw: str | None = None) -> float:
    """Resolve the store-write deadline: env override, else the audited default.

    Reads :data:`STORE_WRITE_DEADLINE_ENV` (``BRUTEX_REGISTRY_WRITE_DEADLINE_S``);
    any unset / blank / non-numeric / non-positive / non-finite value fails safe to
    :data:`STORE_WRITE_DEADLINE_S` via the ONE COMMON validation shape
    :func:`~brutex.config.deadline.resolve_env_seconds` — a bad value can NEVER
    disable the bound (Golden Rule #1). O(1).

    Parameters
    ----------
    raw
        The value to validate (``None`` reads the env); a test may pass one.
    """
    return resolve_env_seconds(STORE_WRITE_DEADLINE_ENV, STORE_WRITE_DEADLINE_S, raw)


class StoreWriteDeadlineGuard:
    """Per-store wall-clock deadline + wedge fail-fast latch for DuckDB write batches.

    One instance per store (:class:`~brutex.data.registry_store.RegistryStore` /
    :class:`~brutex.data.instrument_store.InstrumentStore`) — per-instance state, no
    module globals, so there is no reset seam to leak between tests (CLAUDE.md §4 "no
    global mutable state").

    CONTRACT: :meth:`run` is called ONLY while the caller holds the store's own lock,
    and *fn* touches ONLY the raw DuckDB connection — never a Python lock. That is
    what keeps every Python lock caller-owned: :class:`threading.RLock` ownership is
    per-thread, so on a breach ``done.wait`` times out ON THE CALLER, the transient
    error propagates, and the caller's ``with self._lock`` block unwinds on the
    OWNING thread — the lock is deterministically released, never orphaned by the
    daemon.

    Concurrency note: ``_active`` writes/reads on the CALLER side are serialized by
    the store lock the contract requires; the daemon's ``finally`` runs unlocked —
    GIL-atomic attribute assignment + the token IDENTITY compare make the one
    caller/zombie race benign (worst case: one extra latched defer, or one missing /
    one late :data:`STORE_WRITE_RECOVERED_EVENT` INFO — never corruption, never a
    stuck latch: only the zombie that OWNS the current token can clear it).

    O(1) per write batch: one daemon thread + one :class:`threading.Event`; a wedged
    store spawns ZERO further threads (the latch is one pointer comparison).
    """

    def __init__(self, *, store_label: str) -> None:
        self._store_label = store_label
        #: The in-flight (possibly abandoned) write's identity token — non-``None``
        #: means a write is still running; after a breach that IS the wedge latch.
        self._active: object | None = None
        #: The op abandoned by the LAST breach; the zombie's ``finally`` logs the
        #: recovery INFO against it, then clears it.
        self._abandoned_op: str | None = None

    @property
    def wedged(self) -> bool:
        """``True`` while an abandoned (or in-flight) guarded write has not returned."""
        return self._active is not None

    def run[T](self, fn: Callable[[], T], *, op: str, deadline_seconds: float | None = None) -> T:
        """Run *fn* (raw DuckDB statements only) under the store-write deadline.

        The healthy path is byte-identical to calling *fn* directly (same statements,
        same order, same connection — the caller-held store lock still serializes
        every writer); the statements merely execute on one short-lived daemon thread
        per BATCH while the caller blocks on the deadline wait.

        Parameters
        ----------
        fn
            The write closure — raw ``conn.execute`` statements ONLY (never a Python
            lock; see the class contract).
        op
            Non-secret operation label for the breach WARN + the error.
        deadline_seconds
            The wall-clock bound; ``None`` resolves
            :func:`resolve_store_write_deadline_seconds` (env-tunable). Tests pass a
            small value to exercise the timeout fast.

        Returns
        -------
        T
            *fn*'s result on a write that completes within the deadline.

        Raises
        ------
        StoreWriteDeadlineExceeded
            On a breach (the guard's own deadline fired — one WARN
            :data:`STORE_WRITE_DEADLINE_EVENT`), or instantly with ``latched=True``
            while a prior breach's zombie is still wedged (no thread, no wait, no
            guard log — the caller-layer defer events carry the context). Any error
            *fn* raises ITSELF (a validation ``RegistryError``, a real duckdb error,
            even a genuine :class:`TimeoutError`) propagates UNCHANGED — only the
            guard's own :class:`~brutex.config.deadline.DeadlineExceeded` is re-typed.
        """
        seconds = (
            resolve_store_write_deadline_seconds() if deadline_seconds is None else deadline_seconds
        )
        if self._active is not None:
            # Wedge latch: a prior breach's zombie still holds the DuckDB connection's
            # internal mutex — fail fast in O(1) with NO new thread and NO deadline
            # wait, raising the SAME transient error the defer sites catch. No log
            # here (callers WARN with per-unit context; a guard log would storm).
            raise StoreWriteDeadlineExceeded(seconds, self._store_label, op, latched=True)
        token: object = object()
        self._active = token

        try:
            result = run_with_deadline(
                lambda: self._guarded_call(fn, token),
                seconds,
                what=f"{self._store_label} write",
                thread_name=f"{self._store_label}-write-deadline",
            )
        except DeadlineExceeded as exc:
            # The guard's OWN deadline fired (a wedged DuckDB write that never
            # raised). ``_abandoned_op`` is set BEFORE the WARN/raise (race-benign;
            # worst case a missing INFO if the zombie completes this instant). Only
            # the DeadlineExceeded subclass is re-typed — a real TimeoutError from
            # ``fn`` itself takes run_with_deadline's failure path and propagates
            # UNCHANGED (and ``_guarded_call``'s finally has already cleared the
            # token).
            self._abandoned_op = op
            _LOG.warning(
                STORE_WRITE_DEADLINE_EVENT,
                store=self._store_label,
                op=op,
                deadline_seconds=seconds,
            )
            raise StoreWriteDeadlineExceeded(seconds, self._store_label, op) from exc
        except BaseException:
            # NOT a breach: ``run_with_deadline`` failed OUTSIDE the deadline
            # machinery — most plausibly ``Thread.start()`` raising
            # ``RuntimeError("can't start new thread")`` under thread exhaustion
            # (realistic in EXACTLY the storm this guard targets: a persistently
            # wedged volume accumulates abandoned zombie daemons), or an
            # interrupted deadline wait. On a spawn failure ``_guarded_call``
            # NEVER ran, so NO zombie exists to self-clear the token — without this
            # clear the latch would stick for the PROCESS LIFETIME (every write
            # instantly ``latched=True``, every read the terminal wedged error,
            # ``close()`` skipping the raw close — a false "wedged volume"
            # diagnosis with no recovery but a restart). The token IDENTITY
            # check makes this a no-op on the fn-raised-error path (the daemon's
            # ``finally`` already cleared it) and can never clobber a newer
            # run's token; on a mid-wait interrupt with a genuine in-flight
            # zombie, clearing costs at most one full deadline wait on the next
            # write (bounded — the zombie's own finally is then a benign no-op),
            # never corruption, never a stuck latch.
            if self._active is token:
                self._active = None
            raise
        return result

    def _guarded_call[T](self, fn: Callable[[], T], token: object) -> T:
        """Run *fn*, then token-identity self-clear the wedge state (daemon side).

        A method with its OWN type parameter — not a closure over :meth:`run`'s —
        so no nested scope ever references an enclosing function's PEP 695 type
        variable (the shape CodeQL's uninitialized-local analysis cannot model).
        Behavior is byte-identical to the previous nested closure.

        Token-identity-matched self-recovery: only the run that OWNS the current
        token may clear it (a stale zombie from an earlier breach can never
        clobber a newer run's state). When this run had been abandoned (a breach
        set ``_abandoned_op``), the volume has now recovered — log ONE INFO so
        the operator sees the self-heal.
        """
        try:
            return fn()
        finally:
            if self._active is token:
                self._active = None
                if self._abandoned_op is not None:
                    _LOG.info(
                        STORE_WRITE_RECOVERED_EVENT,
                        store=self._store_label,
                        op=self._abandoned_op,
                    )
                    self._abandoned_op = None
