"""Non-blocking, deadline-guarded memo for the sweep-progress file read.

DEC-SWEEP-PROGRESS-NONBLOCK-CACHE-001. The ``GET /api/v1/sweep-progress``
route (polled every ~2 s by v29.html while a sweep runs) used to call
:meth:`~brutex.dashboard.app.DashboardState.sweep_progress_view` — a
synchronous ``path.exists()`` + :func:`brutex.engine.sweep_supervisor.
read_progress` of ``<lake>/sweeps/progress/sweep-progress.json`` — directly
inside its ``async def`` body, i.e. ON the uvicorn event loop. The progress
file lives on the lake volume (CLAUDE.md §8 — an external SSD); a slow or
WEDGED disk (a stalled mount whose stat/read syscall blocks in
uninterruptible sleep and never raises — the exact hang mode
DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001 #1522 documents for the rate-bridge
read) therefore froze the WHOLE dashboard: a measured 1.5 s stalled read
moved a concurrent ``/api/v1/state`` poll from 9.1 ms to 1305.4 ms.

This module is the sweep-progress twin of
:class:`~brutex.dashboard.rate_bridge_cache.RateBridgeCache` (the #1522
precedent — TTL memo + deadline-guarded refresh + NON-BLOCKING single-flight);
since fix 6b of DEC-MEMORY-FOOTPRINT-HARDENING-001 both twins DELEGATE the
whole state machine to the ONE shared
:class:`~brutex.dashboard.rate_bridge_cache.NonBlockingFileMemoBase`
(byte-identical behaviour — this suite passed unmodified), adapted here to
the progress file's own contract:

* **TTL memo** — a repeat poll within :data:`SWEEP_PROGRESS_CACHE_TTL_S`
  (1 s, deliberately below the page's 2 s poll cadence) is a zero-syscall
  O(1) in-memory hit. NO content/stat token (unlike the rate bridge): a
  RUNNING sweep rewrites the file about once a second, so a stat-token
  revalidation would almost never save the ~300-byte decode — the TTL alone
  is the read-rate bound, and an ``os.stat`` wedges exactly like the read
  anyway.
* **Deadline guard** — the post-TTL refresh (``path.exists()`` + the read,
  BOTH inside the guard — the exists() probe is a syscall on the same
  volume) runs on :func:`brutex.adapters.groww.deadline.run_with_deadline`'s
  daemon (the repo's ONE freeze guard), bounded by
  :data:`SWEEP_PROGRESS_READ_DEADLINE_S` (0.5 s).
* **Non-blocking single-flight** — while a refresh is in flight every other
  caller returns the last-known memo IMMEDIATELY; ``_inflight`` is cleared
  ONLY by the daemon that ran, so a wedged disk leaves EXACTLY ONE abandoned
  daemon per outage, and its eventual late return self-heals the memo. A
  guard that fails BEFORE its daemon could run (``Thread.start()`` under
  thread/memory exhaustion) is caught by the OWNER, which releases the flag
  itself — a spawn failure can never wedge the single-flight nor raise out
  of :meth:`SweepProgressCache.read` (the #1522 hostile-review MAJOR fold,
  mirrored).

No stale illusion, by construction (Golden Rule #1)
---------------------------------------------------
Every FILE-served payload is stamped with two ADDITIVE keys at serve time
(never into the memo): ``progress_source`` ∈ {``live``, ``cached``,
``unverified``} + ``progress_probe_age_s`` (seconds since the last COMPLETED
refresh attempt). A memoised serve can lag a new publish by at most the 1 s
TTL; an ``unverified`` serve may lag arbitrarily but is LABELLED so, with a
growing age. The in-process-push and idle arms of ``sweep_progress_view``
never ride this cache (they are pure in-memory — zero staleness) and stay
byte-identical, unstamped.

Honest envelope (Golden Rule #10)
---------------------------------
* A wedged read syscall CANNOT be killed — the guarantee is a bounded
  :meth:`read` (owner ≤ ~deadline once per outage, followers O(1)-immediate)
  plus at most ONE abandoned daemon per outage (the
  DEC-FETCH-HUNG-WRITE-DEADLINE-001 stance). The route ADDITIONALLY offloads
  the whole view via ``run_in_threadpool`` so even the bounded owner path
  never runs on the event loop.
* A cold-start wedge (no refresh ever completed) has NO memo to serve —
  :meth:`read` returns ``None`` and the view falls through to the in-process
  push / idle arms, exactly as a missing file does. Honest degrade, never a
  fabricated snapshot.
* O-complexity per path: TTL hit — O(1) pure in-memory, ZERO syscalls;
  post-TTL refresh — one ``exists()`` + one ~300-byte read + JSON decode on
  the guard's daemon (constant in lake depth, but a REAL disk read). This is
  a latency/liveness fix — NO throughput claim.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final

import structlog

from brutex.adapters.groww.deadline import run_with_deadline
from brutex.dashboard.rate_bridge_cache import (
    MEMO_SOURCE_CACHED,
    MEMO_SOURCE_LIVE,
    MEMO_SOURCE_UNVERIFIED,
    NonBlockingFileMemoBase,
)
from brutex.engine import sweep_supervisor

_LOG = structlog.get_logger("brutex.dashboard.sweep_progress_cache")

__all__ = [
    "PROGRESS_SOURCE_CACHED",
    "PROGRESS_SOURCE_LIVE",
    "PROGRESS_SOURCE_UNVERIFIED",
    "SWEEP_PROGRESS_CACHE_TTL_S",
    "SWEEP_PROGRESS_READ_DEADLINE_ENV",
    "SWEEP_PROGRESS_READ_DEADLINE_S",
    "SWEEP_PROGRESS_TTL_ENV",
    "ProgressReadResult",
    "SweepProgressCache",
    "resolve_sweep_progress_read_deadline_s",
    "resolve_sweep_progress_ttl_s",
]

#: Seconds a completed refresh stays fresh — the zero-syscall O(1) hot-path
#: window. Sits under the v29.html 2 s poll cadence so at most ~one real read
#: per poll tick, while a burst of concurrent pollers pays exactly one.
SWEEP_PROGRESS_CACHE_TTL_S: Final[float] = 1.0

#: Wall-clock bound on ONE refresh (exists + read) — the latency cap on the
#: slow-disk path. Small: the healthy read is a ~300-byte JSON file.
SWEEP_PROGRESS_READ_DEADLINE_S: Final[float] = 0.5

#: Env override for :data:`SWEEP_PROGRESS_CACHE_TTL_S` (float ≥ 0; ``0``
#: disables the TTL fast path = every poll revalidates — the ntm_cache TTL≤0
#: precedent, mirrored from the rate-bridge cache).
SWEEP_PROGRESS_TTL_ENV: Final[str] = "BRUTEX_SWEEP_PROGRESS_TTL_S"

#: Env override for :data:`SWEEP_PROGRESS_READ_DEADLINE_S` (float > 0).
SWEEP_PROGRESS_READ_DEADLINE_ENV: Final[str] = "BRUTEX_SWEEP_PROGRESS_READ_DEADLINE_S"

#: This request's refresh COMPLETED within the deadline. (Byte-identical
#: aliases of the COMMON ``MEMO_SOURCE_*`` vocabulary the shared
#: ``NonBlockingFileMemoBase`` serves — fix 6b.)
PROGRESS_SOURCE_LIVE: Final[str] = MEMO_SOURCE_LIVE
#: Served from the memo inside the TTL window — verification lag ≤ the TTL.
PROGRESS_SOURCE_CACHED: Final[str] = MEMO_SOURCE_CACHED
#: A refresh is in flight / wedged, or this owner's deadline expired — the
#: served value is last-known and may lag arbitrarily (LABELLED, never silent).
PROGRESS_SOURCE_UNVERIFIED: Final[str] = MEMO_SOURCE_UNVERIFIED


def resolve_sweep_progress_ttl_s(env: Mapping[str, str] | None = None) -> float:
    """Resolve the cache TTL: :data:`SWEEP_PROGRESS_TTL_ENV` override, else the default.

    Fail-loud (the DEC-RAM-BUDGETS-001 env doctrine): a NON-BLANK malformed /
    negative / non-finite value raises :class:`ValueError` — never a silent
    default that would mask an operator typo. ``0`` is VALID and disables the
    TTL fast path (every poll revalidates through the guard).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    float
        The TTL in seconds (≥ 0).

    Raises
    ------
    ValueError
        When the env var holds a non-blank value that is not a finite float ≥ 0.
    """
    source = os.environ if env is None else env
    raw = source.get(SWEEP_PROGRESS_TTL_ENV, "").strip()
    if not raw:
        return SWEEP_PROGRESS_CACHE_TTL_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"{SWEEP_PROGRESS_TTL_ENV}={raw!r} is not a number (finite seconds >= 0 required)"
        ) from None
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{SWEEP_PROGRESS_TTL_ENV}={raw!r} must be a finite value >= 0 seconds")
    return value


def resolve_sweep_progress_read_deadline_s(env: Mapping[str, str] | None = None) -> float:
    """Resolve the refresh deadline: :data:`SWEEP_PROGRESS_READ_DEADLINE_ENV`, else the default.

    Fail-loud like :func:`resolve_sweep_progress_ttl_s`, but a deadline must
    be STRICTLY positive and finite — ``0`` / negative / ``inf`` / ``nan``
    would defeat the bound, so they raise instead of silently disabling the
    freeze guard.

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    float
        The deadline in seconds (> 0).

    Raises
    ------
    ValueError
        When the env var holds a non-blank value that is not a finite float > 0.
    """
    source = os.environ if env is None else env
    raw = source.get(SWEEP_PROGRESS_READ_DEADLINE_ENV, "").strip()
    if not raw:
        return SWEEP_PROGRESS_READ_DEADLINE_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"{SWEEP_PROGRESS_READ_DEADLINE_ENV}={raw!r} is not a number "
            "(finite seconds > 0 required)"
        ) from None
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(
            f"{SWEEP_PROGRESS_READ_DEADLINE_ENV}={raw!r} must be a finite value > 0 seconds"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProgressReadResult:
    """One cache answer: the file payload (or ``None``) + its honest provenance.

    Attributes
    ----------
    payload
        The last-known decoded progress-file payload, or ``None`` — the file
        is MISSING (fall through to the in-process / idle arms, exactly as
        the direct ``path.exists()`` check did) or nothing has ever been read
        (cold). A PRESENT-but-corrupt file carries
        :func:`~brutex.engine.sweep_supervisor.read_progress`'s own idle
        fallback dict, preserving the pre-cache behaviour byte-for-byte.
    source
        Provenance: :data:`PROGRESS_SOURCE_LIVE` /
        :data:`PROGRESS_SOURCE_CACHED` / :data:`PROGRESS_SOURCE_UNVERIFIED`.
    probe_age_s
        Seconds since the last COMPLETED refresh attempt (monotonic delta,
        clamped ≥ 0): ``0.0`` on live, ≤ the TTL on cached, growing
        unboundedly on unverified, ``None`` when no refresh has ever
        completed (cold).
    """

    payload: dict[str, Any] | None
    source: str
    probe_age_s: float | None


def _stamp_progress_freshness(payload: dict[str, Any], result: ProgressReadResult) -> None:
    """Stamp ``progress_source`` + ``progress_probe_age_s`` onto *payload* (in place) — O(1).

    The ONE place the two honest freshness keys are written (the
    ``rate_bridge_cache.stamp_bridge_freshness`` doctrine). Additive-only:
    writes exactly these two keys, so every pre-existing sweep-progress field
    stays byte-identical. Applied to a per-request COPY — never the memo.
    """
    payload["progress_source"] = result.source
    payload["progress_probe_age_s"] = (
        None if result.probe_age_s is None else round(result.probe_age_s, 3)
    )


class SweepProgressCache(NonBlockingFileMemoBase[dict[str, Any]]):
    """TTL + deadline memo over the progress-file read, never blocking its caller.

    One instance per app (injected by ``build_app`` — no module-global mutable
    state, CLAUDE.md §4). Thread-safe: all state lives under ONE
    :class:`threading.Lock` held only for O(1) field ops, never across I/O.
    The TTL / follower / owner state machine lives ONCE in the shared
    :class:`~brutex.dashboard.rate_bridge_cache.NonBlockingFileMemoBase`
    (fix 6b, DEC-MEMORY-FOOTPRINT-HARDENING-001 — the hand-copied twin is
    gone; event names / payload keys / TTL + deadline envs byte-identical).
    See the module docstring for the full mechanism + honest envelope.

    Parameters
    ----------
    ttl_s
        Seconds a completed refresh stays fresh; ``None`` resolves
        :func:`resolve_sweep_progress_ttl_s` (env-overridable). ``0`` disables
        the TTL fast path (every poll revalidates through the guard).
    read_deadline_s
        Wall-clock bound on one refresh; ``None`` resolves
        :func:`resolve_sweep_progress_read_deadline_s` (env-overridable).
    clock
        Monotonic clock seam (tests inject a fake); default ``time.monotonic``.
    reader
        Progress reader; ``None`` LATE-BINDS to
        ``brutex.engine.sweep_supervisor.read_progress`` via module attribute
        at call time, preserving the ``monkeypatch.setattr(...)`` test seam
        (the rate-bridge cache's late-binding precedent).
    guard
        The freeze guard; default
        :func:`~brutex.adapters.groww.deadline.run_with_deadline`. Tests
        inject ``lambda fn, s: fn()`` for thread-free inline refreshes.
    """

    _EVENT_PREFIX: ClassVar[str] = "sweep_progress_cache"
    _TARGET_FIELD: ClassVar[str] = "path"
    _FILE_NOUN: ClassVar[str] = "progress-file"

    def __init__(
        self,
        *,
        ttl_s: float | None = None,
        read_deadline_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        reader: Callable[[Path], dict[str, Any]] | None = None,
        guard: Callable[[Callable[[], None], float], object] = run_with_deadline,
    ) -> None:
        super().__init__(
            ttl_s=resolve_sweep_progress_ttl_s() if ttl_s is None else ttl_s,
            read_deadline_s=(
                resolve_sweep_progress_read_deadline_s()
                if read_deadline_s is None
                else read_deadline_s
            ),
            clock=clock,
            guard=guard,
            log=_LOG,
        )
        self._reader = reader
        self._payload: dict[str, Any] | None = None

    def read(self, path: Path) -> dict[str, Any] | None:
        """Return the stamped file-bridge payload for *path*, bounded.

        The ``file_read`` seam :meth:`DashboardState.sweep_progress_view
        <brutex.dashboard.app.DashboardState.sweep_progress_view>` accepts:
        a ``dict`` means "serve THIS as the file arm" (a fresh per-request
        copy, freshness-stamped); ``None`` means "no file payload — fall
        through to the in-process / idle arms" (missing file, or a cold-start
        wedge with nothing memoised). Bounded on every path — TTL hit and
        follower serves are O(1) in-memory, an owner refresh is capped by the
        deadline guard. Never raises EXCEPT ``KeyboardInterrupt`` /
        ``SystemExit`` (with the single-flight flag released first) and the
        fail-closed pull-state kernel errors below: the three-arm serve
        DECISION now routes through :func:`brutex.pull_state_rust.
        rust_memo_serve_arm` (via the shared
        :meth:`~brutex.dashboard.rate_bridge_cache.NonBlockingFileMemoBase.
        _serve_bounded`), so a wheel-absent/stale/poisoned kernel propagates
        its typed error out of this read (DEC-RUST-PULL-STATE-001 flip phase).

        Parameters
        ----------
        path
            The progress JSON file (``state.sweep_progress_path``).

        Returns
        -------
        dict[str, Any] | None
            A stamped COPY of the file payload, or ``None``.

        Raises
        ------
        brutex.pull_state_rust.PullStateKernelError
            Wheel absent/stale, kernel raise, or a poisoned serve arm —
            fail-closed; raised BEFORE the single-flight ``_inflight`` is
            taken, so the single-flight state never leaks.
        brutex.pull_state_rust.PullStateFlagRemovedError
            The removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
        """
        result = self._read_result(path)
        if result.payload is None:
            return None
        out = dict(result.payload)
        _stamp_progress_freshness(out, result)
        return out

    def _read_result(self, path: Path) -> ProgressReadResult:
        """The three-arm cache core — delegates to the shared base (fix 6b)."""
        payload, source, probe_age = self._serve_bounded(path)
        return ProgressReadResult(payload, source, probe_age)

    def _memo_value(self) -> dict[str, Any] | None:
        """The memoised payload (called under the base's lock — plain read)."""
        return self._payload

    def _refresh_body(self, target: Path) -> None:
        """Run ONE exists+read refresh and commit it (runs on the guard's daemon).

        Preserves the direct view's file-arm contract byte-for-byte: a MISSING
        file commits ``None`` (the view falls through to in-process / idle); a
        PRESENT file commits :func:`~brutex.engine.sweep_supervisor.
        read_progress`'s answer, whose OWN corrupt-file fallback is the idle
        dict. The commit is by side effect under the lock (last-write-wins —
        a LATE-returning wedged refresh still repairs the cache when the
        volume recovers). The catch/finally discipline (raising injected
        reader WARNed + memo kept + ``_probed_at`` advanced; ``_inflight``
        cleared) lives ONCE in the shared base
        (:meth:`~brutex.dashboard.rate_bridge_cache.NonBlockingFileMemoBase._refresh`).
        """
        if not target.exists():
            payload: dict[str, Any] | None = None
        else:
            reader = self._reader
            if reader is None:
                # LATE-BOUND module attribute — preserves the existing
                # monkeypatch.setattr("brutex.engine.sweep_supervisor.
                # read_progress", ...) seam (the #1522 precedent).
                reader = sweep_supervisor.read_progress
            payload = reader(target)
        with self._lock:
            self._payload = payload
            self._probed_at = self._clock()
