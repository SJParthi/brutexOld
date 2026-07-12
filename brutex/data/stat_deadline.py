"""Deadline + breach-armed fail-fast latch for read-side lake METADATA sweeps.

DEC-DASHBOARD-COLD-IO-DEADLINE-001 (guarantee-audit BUG-2). The dashboard's
read-side metadata sweeps — the stats-manifest consult
(:func:`~brutex.data.lake_stats_manifest.load_valid_dataset_stats_guarded`),
its post-query TOCTOU re-stat
(:func:`~brutex.data.lake_stats_manifest.bounded_token_sweep`), and the
cross-process freshness probe
(:func:`~brutex.dashboard.lake_revision.lake_revision`) — are pure
``os.stat``/``os.walk``/bounded-read metadata syscalls with NO wall-clock
bound: a WEDGED external volume (a hang that never raises — the mode
DEC-FETCH-HUNG-WRITE-DEADLINE-001 documents for the write path) blocked each
polled reader forever, and the 4-15 s pollers stacked wedged workers toward
the 32-token ``_LakeOffload`` limiter's exhaustion.

:class:`StatSweepGate` is the ONE guard for those sweeps. It reuses the
COMMON :func:`~brutex.config.deadline.run_with_deadline` primitive (NO copy,
CLAUDE.md §11) — ONE daemon per RUN (a run = one WHOLE sweep), never per
file — behind a **breach-armed** per-key fail-fast latch adapted from the
:class:`~brutex.data.store_write_deadline.StoreWriteDeadlineGuard` family for
LOCK-LESS concurrent readers: the latch is armed ONLY by an actual deadline
breach, never by in-flight-ness, so healthy concurrent sweeps on the same key
ALL proceed (two concurrent healthy ``/db`` clicks never degrade each other).
While a key is latched, :meth:`StatSweepGate.run` raises
:class:`StatSweepTimeout` (``latched=True``) instantly — no thread, no wait,
no guard-level log. The latch self-clears when the abandoned zombie's sweep
finally returns (the volume recovered) — token-identity-matched, ONE
:data:`STAT_GATE_RECOVERED_EVENT` INFO; no TTL.

**Honest envelope (Golden Rule #10):** the abandoned daemon sweep cannot be
killed — it dies with the process (the #999 stance,
:mod:`brutex.config.deadline`). The guarantee is "no polled reader is ever
blocked longer than one deadline once the latch is set; while the volume
stays wedged, later sweeps on that key fail fast with ZERO new threads; when
the volume recovers the zombie returns, the latch self-clears, and sweeps
resume" — NOT "the zombie thread is terminated". The abandoned-daemon count
per gate per key per outage is at most the sweeps already IN FLIGHT during
the one deadline window in which the breach lands (the latch arms only on
the FIRST breach, so each concurrent pre-breach flight leaves its own
zombie; sequentially exactly ONE), plus at most one extra per
release-then-rewedge oscillation — ZERO new threads spawn while latched,
and every zombie self-clears when its sweep returns.

Healthy-path cost: one short-lived daemon thread spawn + join per guarded
sweep (~50-150 µs) on top of the sweep's own ms-scale metadata syscalls —
disclosed, non-accumulating, zero extra ``os.stat`` calls (the guard wraps,
never re-stats).

Leaf discipline: imports are stdlib + ``structlog`` +
:mod:`brutex.config.deadline` (itself stdlib-only) — cycle-proof for both
:mod:`brutex.data` and :mod:`brutex.dashboard` consumers.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Final

import structlog

from brutex.config.deadline import (
    DeadlineExceeded,
    DeadlineSpawnError,
    resolve_env_seconds,
    run_with_deadline,
)

_LOG = structlog.get_logger(__name__)

__all__ = [
    "STAT_DEADLINE_ENV",
    "STAT_DEADLINE_S",
    "STAT_GATE_DEADLINE_EVENT",
    "STAT_GATE_RECOVERED_EVENT",
    "STAT_GATE_SPAWN_EVENT",
    "StatSweepGate",
    "StatSweepTimeout",
    "resolve_stat_deadline_seconds",
]

#: Environment override for the read-side stat-sweep deadline (operator escape
#: hatch, no code change) — ONE shared knob for the manifest consult, the
#: TOCTOU re-stat pass, and the revision probe (the same operation class:
#: bounded metadata syscalls over one dataset / a few sidecars).
STAT_DEADLINE_ENV: Final[str] = "BRUTEX_LAKE_STAT_DEADLINE_S"

#: Default wall-clock deadline for one guarded metadata sweep. A healthy
#: D≈80-file consult / 4-stat-plus-scandir probe is ms-scale, so 5 s is
#: ≥1000× headroom — pure HANG detection, never a healthy-path throttle
#: (the :data:`~brutex.data.store_write_deadline.STORE_WRITE_DEADLINE_S`
#: "GENEROUS — only a genuine hang can exhaust it" doctrine). Env-tunable via
#: :data:`STAT_DEADLINE_ENV`, fail-SAFE validated.
STAT_DEADLINE_S: Final[float] = 5.0

#: WARN — the gate's own deadline fired on an ACTUAL breach that ARMED the
#: key's latch (once per breach flip per key, never per latched fail-fast):
#: ``what`` / ``key`` / ``op`` / ``deadline_seconds``.
STAT_GATE_DEADLINE_EVENT: Final[str] = "stat_gate.deadline_exceeded"

#: INFO — the latch-owner zombie's sweep finally returned (the volume
#: recovered) and the latch self-cleared: ``what`` / ``key`` / ``op``.
#: Fires on the zombie's daemon thread.
STAT_GATE_RECOVERED_EVENT: Final[str] = "stat_gate.recovered"

#: WARN — the guard's worker daemon failed to SPAWN (thread exhaustion), so
#: the sweep never ran; flip-latched per key (one WARN per spawn outage, a
#: later successful spawn re-arms it): ``what`` / ``key`` / ``op``.
STAT_GATE_SPAWN_EVENT: Final[str] = "stat_gate.spawn_failed"


def resolve_stat_deadline_seconds(raw: str | None = None) -> float:
    """Resolve the stat-sweep deadline: env override, else the audited default.

    Reads :data:`STAT_DEADLINE_ENV` (``BRUTEX_LAKE_STAT_DEADLINE_S``); any
    unset / blank / non-numeric / non-positive / non-finite value fails SAFE
    to :data:`STAT_DEADLINE_S` via the ONE COMMON validation shape
    :func:`~brutex.config.deadline.resolve_env_seconds` — a bad value can
    NEVER disable the bound (Golden Rule #1; the
    ``resolve_store_write_deadline_seconds`` precedent — a deliberate,
    ledgered divergence from the rate-bridge fail-loud resolvers: a junk env
    must neither disable a wedge bound nor refuse a dashboard boot). O(1).

    Parameters
    ----------
    raw
        The value to validate (``None`` reads the env); a test may pass one.
    """
    return resolve_env_seconds(STAT_DEADLINE_ENV, STAT_DEADLINE_S, raw)


class StatSweepTimeout(RuntimeError):
    """A guarded metadata sweep blew (or is latched behind) its deadline.

    Deliberately a :class:`RuntimeError`, NOT a :class:`TimeoutError`:
    ``DeadlineExceeded ⊂ TimeoutError ⊂ OSError``, and the /db read path's
    resilient arms (``movers_read_skip_errors()`` ⊇ ``OSError``, plus every
    leaf-internal ``except OSError``) would silently misclassify a raw
    timeout as a corrupt-file skip. The two leaf consumers catch this type
    explicitly and convert it to their return-value degrades — it never
    crosses into :mod:`brutex.dashboard.db_view` or the app routes.

    Attributes
    ----------
    op
        The sweep's operation label (e.g. ``"stats_consult"``).
    key
        The gate key (the lake root) whose sweep timed out.
    latched
        ``True`` when this raise was the instant fail-fast behind a PRIOR
        breach's still-wedged zombie (no thread was spawned, no wait paid).
    spawn
        ``True`` when the guard's worker daemon failed to spawn (the sweep
        never ran) — never latches.
    """

    def __init__(self, *, what: str, op: str, key: str, latched: bool = False, spawn: bool = False):
        detail = "spawn failed" if spawn else ("latched" if latched else "deadline exceeded")
        super().__init__(f"{what} {op} for {key}: {detail}")
        self.op = op
        self.key = key
        self.latched = latched
        self.spawn = spawn


class _Flight:
    """One :meth:`StatSweepGate.run` invocation's shared caller/zombie record.

    ``done`` is set by the daemon-side ``finally`` UNDER the gate lock; the
    caller's breach arm latches ONLY IF ``not done`` — the done-flag race law:
    a sweep that completes inside the expiry→latch window must never leave a
    permanently stuck latch (its zombie is already gone, so nothing would
    ever self-clear it).
    """

    __slots__ = ("done", "op")

    def __init__(self, op: str) -> None:
        self.op = op
        self.done = False


class StatSweepGate:
    """Deadline + BREACH-armed per-key fail-fast latch for read-side metadata sweeps.

    ONE :func:`~brutex.config.deadline.run_with_deadline` daemon per RUN (a
    run = one whole sweep) — never per file. Healthy concurrent runs on the
    same key ALL proceed (the latch is armed only by an actual breach, never
    by in-flight-ness — two concurrent healthy consults/probes never degrade
    each other). While a key is latched, :meth:`run` raises
    :class:`StatSweepTimeout` (``latched=True``) instantly — no thread, no
    wait, no log. The latch owner is the FIRST breached flight
    (``setdefault``); its zombie's ``finally`` token-identity clears the
    latch + emits ONE :data:`STAT_GATE_RECOVERED_EVENT` INFO.

    Parameters
    ----------
    what
        Non-secret gate family label for the deadline message + log events
        (e.g. ``"lake stats sweep"``).
    guard
        The deadline primitive — injectable test seam (the
        ``rate_bridge_cache`` precedent); defaults to the COMMON
        :func:`~brutex.config.deadline.run_with_deadline`. Must honour its
        contract: run the callable, raise
        :class:`~brutex.config.deadline.DeadlineExceeded` on expiry /
        :class:`~brutex.config.deadline.DeadlineSpawnError` on spawn failure,
        re-raise the callable's own exceptions UNCHANGED.

    Notes
    -----
    The gate ``_lock`` guards only O(1) dict ops and never nests with any
    other lock. Per-key state means unrelated lakes (unique ``tmp_path``
    test lakes) never collide. The wedge is a process-wide VOLUME fact, so a
    module-level gate instance per consumer family is the sanctioned shape
    (the ``partition_lock`` process-registry precedent).
    """

    def __init__(self, *, what: str, guard: Callable[..., Any] = run_with_deadline) -> None:
        self._what = what
        self._guard = guard
        self._lock = threading.Lock()
        #: key → the FIRST breached flight whose zombie is still wedged (the
        #: fail-fast latch); popped by that zombie's ``finally`` alone.
        self._wedged: dict[str, _Flight] = {}
        #: Keys whose LAST spawn attempt failed (the flip-latch for the spawn
        #: WARN — one per outage, re-armed by a later successful spawn).
        self._spawn_warned: set[str] = set()

    def wedged(self, key: str) -> bool:
        """``True`` while *key* is latched behind an abandoned breached sweep."""
        with self._lock:
            return key in self._wedged

    def run[T](
        self, key: str, fn: Callable[[], T], *, op: str, deadline_s: float | None = None
    ) -> T:
        """Run the WHOLE sweep *fn* under the stat deadline + the *key* latch.

        The healthy path is byte-identical to calling *fn* directly (same
        syscalls, same order — the sweep merely executes on one short-lived
        daemon thread while the caller blocks on the deadline wait).

        Parameters
        ----------
        key
            The latch key — the lake root string (``str(lake_root)``): a
            wedge is a volume fact, so every dataset on the wedged lake
            shares one latch.
        fn
            The zero-arg sweep closure (metadata syscalls only — the whole
            consult / re-stat pass / probe, NEVER a per-file wrap).
        op
            Non-secret operation label for the breach WARN + the error.
        deadline_s
            The wall-clock bound; ``None`` resolves
            :func:`resolve_stat_deadline_seconds` per run (one
            ``os.environ.get`` per SWEEP — negligible; keeps
            ``monkeypatch.setenv`` tests trivial).

        Returns
        -------
        T
            *fn*'s result on a sweep that completes within the deadline.

        Raises
        ------
        StatSweepTimeout
            On a breach (``latched=False`` — the flip that ARMED the latch
            logs ONE :data:`STAT_GATE_DEADLINE_EVENT` WARN), instantly with
            ``latched=True`` while a prior breach's zombie is still wedged
            (no thread, no wait, no log), or with ``spawn=True`` when the
            guard daemon failed to spawn (*fn* never ran — no latch; WARN
            flip-latched per key). Any error *fn* raises ITSELF (a real
            :class:`OSError`, a :class:`ValueError`) propagates UNCHANGED —
            only the guard's own two types are re-typed.
        """
        with self._lock:
            latched = key in self._wedged
        if latched:
            # Fail-fast: a prior breach's zombie is still wedged on this
            # volume — O(1), no thread, no wait, no log (the breach WARN +
            # the consumers' per-consult fallback events carry the story).
            raise StatSweepTimeout(what=self._what, op=op, key=key, latched=True)
        seconds = resolve_stat_deadline_seconds() if deadline_s is None else deadline_s
        entry = _Flight(op)
        try:
            result: T = self._guard(
                lambda: self._tracked_call(fn, entry, key),
                seconds,
                what=f"{self._what} {op}",
                thread_name=f"stat-sweep-{op}",
            )
        except DeadlineExceeded as exc:
            armed = False
            with self._lock:
                # The done-flag race law: a sweep that completed inside the
                # expiry→latch window must NOT arm a latch its (already
                # finished) zombie can never clear. ``setdefault`` makes the
                # FIRST breached flight the latch owner — a concurrently
                # breached second flight's zombie no-ops on clear.
                if not entry.done:
                    armed = self._wedged.setdefault(key, entry) is entry
            if armed:
                _LOG.warning(
                    STAT_GATE_DEADLINE_EVENT,
                    what=self._what,
                    key=key,
                    op=op,
                    deadline_seconds=seconds,
                )
            raise StatSweepTimeout(what=self._what, op=op, key=key) from exc
        except DeadlineSpawnError as exc:
            # Thread exhaustion — fn NEVER ran, so there is no zombie to
            # self-clear a latch: never latch. WARN once per outage flip.
            with self._lock:
                warn = key not in self._spawn_warned
                self._spawn_warned.add(key)
            if warn:
                _LOG.warning(STAT_GATE_SPAWN_EVENT, what=self._what, key=key, op=op)
            raise StatSweepTimeout(what=self._what, op=op, key=key, spawn=True) from exc
        with self._lock:
            self._spawn_warned.discard(key)
        return result

    def _tracked_call[T](self, fn: Callable[[], T], entry: _Flight, key: str) -> T:
        """Run *fn*, then done-flag + token-identity self-clear (daemon side).

        A method with its OWN type parameter (the
        :meth:`~brutex.data.store_write_deadline.StoreWriteDeadlineGuard._guarded_call`
        precedent — no nested closure over an enclosing PEP 695 type
        variable). Only the flight that OWNS the current latch may clear it
        (a stale zombie from an earlier breach can never clobber a newer
        flight's latch); the recovered INFO fires on this daemon thread,
        AFTER the lock is released.
        """
        try:
            return fn()
        finally:
            recovered = False
            with self._lock:
                entry.done = True
                if self._wedged.get(key) is entry:
                    del self._wedged[key]
                    recovered = True
            if recovered:
                _LOG.info(STAT_GATE_RECOVERED_EVENT, what=self._what, key=key, op=entry.op)
