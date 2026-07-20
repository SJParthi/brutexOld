"""Non-blocking, deadline-guarded memo for the CLI rate-bridge file read.

DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001. The ``GET /api/v1/pull/rate`` route
(polled every ~3 s by the site-wide widget, from EVERY dashboard page) used to
call :func:`brutex.data.rate_bridge.read_rate_snapshot` — a synchronous
``Path.read_bytes()`` of ``<lake_root>/.rate/pull_rate.json`` — directly inside
its ``async def`` body, i.e. ON the uvicorn event loop. The lake lives on an
external volume (CLAUDE.md §8); a slow or WEDGED disk (a stalled external SSD /
NFS mount whose read syscall blocks in uninterruptible sleep and never raises —
the exact hang mode DEC-FETCH-HUNG-WRITE-DEADLINE-001 documents on the write
side) therefore froze the WHOLE dashboard: every route, every poller.

This module is the fix — ONE small cache composing EXISTING repo primitives
(CLAUDE.md §11 COMMON, no copy) — and, since fix 6b of
DEC-MEMORY-FOOTPRINT-HARDENING-001, ALSO the home of
:class:`NonBlockingFileMemoBase`, the ONE TTL + deadline-guard +
non-blocking-single-flight core this cache and its sweep-progress twin
(:class:`~brutex.dashboard.sweep_progress_cache.SweepProgressCache`) both
delegate to byte-identically (the twins previously duplicated the whole
state machine — the #1522 spawn-failure MAJOR had to be fixed twice):

* **Content token** — :func:`brutex.dashboard.lake_revision.rate_bridge_revision`
  (the shared ``os.stat`` token core, inode-folded because the publisher swaps
  the inode on every atomic ``os.replace``). HONESTY NOTE: the token exists for
  the live-vs-cached PROVENANCE distinction and to skip a redundant decode when
  the file is unchanged — it is NOT wedge protection (``os.stat`` wedges exactly
  like ``read_bytes`` on a stalled mount), which is why the probe runs INSIDE
  the deadline guard below.
* **TTL memo** — a repeat poll within :data:`RATE_BRIDGE_CACHE_TTL_S` (2 s,
  deliberately ``< pull_rate_view.STALE_AFTER_S`` = 12 s — test-pinned) is a
  zero-syscall O(1) in-memory hit.
* **Deadline guard** — the post-TTL refresh (stat + conditional read) runs on
  :func:`brutex.adapters.groww.deadline.run_with_deadline`'s daemon (the repo's
  ONE freeze guard), so even the refresh OWNER is bounded by
  :data:`RATE_BRIDGE_READ_DEADLINE_S` (0.5 s).
* **Non-blocking single-flight** — while a refresh is in flight every other
  caller returns the last-known memo IMMEDIATELY (never a timeout-less wait);
  once the daemon RUNS, ``_inflight`` is cleared ONLY by that daemon itself,
  so a wedged disk leaves EXACTLY ONE abandoned daemon per outage (not one per
  TTL), and its eventual late return self-heals the cache (last-write-wins
  adoption). A guard that fails BEFORE its daemon could run (``Thread.start()``
  under thread/memory exhaustion) is caught by the OWNER, which releases the
  flag itself and serves ``unverified`` — a spawn failure can never wedge the
  single-flight nor raise out of :meth:`RateBridgeCache.get`.
* **The Rust decision kernel (DEC-RUST-PULL-STATE-001, flip phase)** — the
  two clockless LAWS (the three-arm serve decision + the token-revalidation
  compare) are served by the ``brutex_pull_state`` kernel
  (:func:`brutex.pull_state_rust.rust_memo_serve_arm` /
  :func:`brutex.pull_state_rust.rust_token_revalidates`); the former
  byte-identical Python condition bodies are DELETED. The THREAD CORE — the
  lock, the deadline-guard daemon, the ``_inflight`` clearing law, the
  degrade latch — is PERMANENT-PYTHON (the design §2.3 ruling; its Rust
  port is DEFERRED to ``DEC-RUST-PULL-GLUE-002``). A kernel fault on the
  SERVE path raises the typed fail-closed error out of
  :meth:`RateBridgeCache.get`; on the refresh DAEMON it folds into the
  base's ``refresh_failed`` WARN (memo kept, TTL-cadence retry — a daemon
  raise has nowhere louder to go). The single-flight fail-safe belt stays:
  a poisoned OWNER arm while a refresh is in flight is FORCED to the
  follower serve, never a second daemon.

No stale illusion, by construction (Golden Rule #1)
---------------------------------------------------
The cache stores the decoded :class:`~brutex.data.rate_bridge.RateBridgeSnapshot`
— NEVER a built view. ``bridge_age`` / ``seconds_since_update`` are recomputed
on every request from the snapshot's own embedded ``wall_time``, so a memoised
snapshot AGES honestly and the existing 12 s ``STALE_AFTER_S`` machinery flips
the verdict to stalled/idle exactly as a fresh re-read would. A "cached" serve
can lag a new publish by at most the 2 s TTL (< the 12 s verdict window, so a
verdict CATEGORY can never flip from caching alone); an "unverified" serve may
lag arbitrarily but is LABELLED so, with a growing ``probe_age_s``. Reported
staleness can only read HIGHER than truth (by ≤ TTL), never fresher-than-real.
:func:`stamp_bridge_freshness` is the ONE place the two payload keys are
written (the ``summary_cache.stamp_freshness`` doctrine).

Honest envelope (Golden Rule #10)
---------------------------------
* A wedged read syscall CANNOT be killed — the guarantee is a free event loop
  plus EVERY response bounded (owner ≤ ~deadline once per outage, followers
  O(1)-immediate) plus at most ONE abandoned daemon per outage, never zombie
  reaping (the DEC-FETCH-HUNG-WRITE-DEADLINE-001 stance).
* The route-level "every response bounded" holds GIVEN an available AnyIO
  worker token: the route reaches this cache via ``run_in_threadpool``, whose
  DEFAULT 40-token limiter is shared by EVERY offloaded dashboard read, and
  the sibling lake offloads (summary / movers / db_view) carry NO deadline
  guard — a pool-wide wedge (every token stuck on an undeadlined sibling read
  of the same stalled volume) would queue this route's offload behind them.
  The bounds above are guarantees OF THIS CACHE; pool-wide wedge protection
  is OUT OF SCOPE here — a named follow-up.
* O-complexity per path, honestly: TTL hit — O(1) pure in-memory, ZERO
  syscalls; post-TTL revalidate — O(1) + ONE ``os.stat`` on the guard's daemon;
  content-change miss — one stat + one ~300-byte read + JSON decode (constant
  in lake depth but a REAL disk read, not claimed free); healthy-path overhead
  — one short-lived daemon thread per post-TTL refresh (≤ 1 per TTL while
  polled). This is a latency/liveness fix — NO throughput claim.
* The symmetric CLI WRITE-side hang (``publish_rate_snapshot``'s undeadlined
  ``fsync`` inside the pull's per-window tick) is a SEPARATE named follow-up —
  NOT fixed here.
"""

from __future__ import annotations

import functools
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final

import structlog

from brutex import pull_state_rust as _pull_state_rust
from brutex.adapters.groww.deadline import DeadlineExceeded, run_with_deadline
from brutex.dashboard.lake_revision import rate_bridge_revision
from brutex.data import rate_bridge

_LOG = structlog.get_logger("brutex.dashboard.rate_bridge_cache")

__all__ = [
    "BRIDGE_SOURCE_CACHED",
    "BRIDGE_SOURCE_LIVE",
    "BRIDGE_SOURCE_UNVERIFIED",
    "BRIDGE_SOURCE_UNWIRED",
    "MEMO_SOURCE_CACHED",
    "MEMO_SOURCE_LIVE",
    "MEMO_SOURCE_UNVERIFIED",
    "RATE_BRIDGE_CACHE_TTL_S",
    "RATE_BRIDGE_READ_DEADLINE_ENV",
    "RATE_BRIDGE_READ_DEADLINE_S",
    "RATE_BRIDGE_TTL_ENV",
    "BridgeReadResult",
    "NonBlockingFileMemoBase",
    "RateBridgeCache",
    "resolve_rate_bridge_read_deadline_s",
    "resolve_rate_bridge_ttl_s",
    "stamp_bridge_freshness",
]

#: Seconds a completed refresh stays fresh — the zero-syscall O(1) hot-path
#: window. MUST stay below ``pull_rate_view.STALE_AFTER_S`` (12 s, test-pinned)
#: so a cached serve can never flip a verdict category by itself; 2 s matches
#: ``summary_cache.DEFAULT_TTL_FLOOR_S`` and sits under the widget's 3 s poll.
RATE_BRIDGE_CACHE_TTL_S: Final[float] = 2.0

#: Wall-clock bound on ONE refresh (stat + conditional read) — the route-latency
#: cap on the slow-disk path. Small: the healthy read is a ~300-byte JSON file.
RATE_BRIDGE_READ_DEADLINE_S: Final[float] = 0.5

#: Env override for :data:`RATE_BRIDGE_CACHE_TTL_S` (float ≥ 0; ``0`` disables
#: the TTL fast path = every poll revalidates — the ntm_cache TTL≤0 precedent).
RATE_BRIDGE_TTL_ENV: Final[str] = "BRUTEX_RATE_BRIDGE_TTL_S"

#: Env override for :data:`RATE_BRIDGE_READ_DEADLINE_S` (float > 0).
RATE_BRIDGE_READ_DEADLINE_ENV: Final[str] = "BRUTEX_RATE_BRIDGE_READ_DEADLINE_S"

#: The COMMON memo provenance vocabulary (fix 6b,
#: DEC-MEMORY-FOOTPRINT-HARDENING-001): the ONE source-token set the shared
#: :class:`NonBlockingFileMemoBase` serves; the per-cache ``BRIDGE_SOURCE_*``
#: / ``PROGRESS_SOURCE_*`` names below are byte-identical aliases so every
#: existing payload key + test pin stays unchanged.
MEMO_SOURCE_LIVE: Final[str] = "live"
MEMO_SOURCE_CACHED: Final[str] = "cached"
MEMO_SOURCE_UNVERIFIED: Final[str] = "unverified"

#: This request's refresh COMPLETED within the deadline (a fresh read, a stat
#: revalidation, or an honest completed degrade such as a corrupt-file ``None``).
BRIDGE_SOURCE_LIVE: Final[str] = MEMO_SOURCE_LIVE
#: Served from the memo inside the TTL window — verification lag ≤ the TTL.
BRIDGE_SOURCE_CACHED: Final[str] = MEMO_SOURCE_CACHED
#: A refresh is in flight / wedged, or this owner's deadline expired — the
#: served value is last-known and may lag arbitrarily (LABELLED, never silent).
BRIDGE_SOURCE_UNVERIFIED: Final[str] = MEMO_SOURCE_UNVERIFIED
#: No instrument store / lake root is wired — there is no bridge to read.
BRIDGE_SOURCE_UNWIRED: Final[str] = "unwired"


def resolve_rate_bridge_ttl_s(env: Mapping[str, str] | None = None) -> float:
    """Resolve the cache TTL: :data:`RATE_BRIDGE_TTL_ENV` override, else the default.

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
    raw = source.get(RATE_BRIDGE_TTL_ENV, "").strip()
    if not raw:
        return RATE_BRIDGE_CACHE_TTL_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"{RATE_BRIDGE_TTL_ENV}={raw!r} is not a number (finite seconds >= 0 required)"
        ) from None
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{RATE_BRIDGE_TTL_ENV}={raw!r} must be a finite value >= 0 seconds")
    return value


def resolve_rate_bridge_read_deadline_s(env: Mapping[str, str] | None = None) -> float:
    """Resolve the refresh deadline: :data:`RATE_BRIDGE_READ_DEADLINE_ENV`, else the default.

    Fail-loud like :func:`resolve_rate_bridge_ttl_s`, but a deadline must be
    STRICTLY positive and finite — ``0`` / negative / ``inf`` / ``nan`` would
    defeat the bound (an ``inf`` deadline never expires; a ``0`` one always
    does), so they raise instead of silently disabling the freeze guard.

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
    raw = source.get(RATE_BRIDGE_READ_DEADLINE_ENV, "").strip()
    if not raw:
        return RATE_BRIDGE_READ_DEADLINE_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"{RATE_BRIDGE_READ_DEADLINE_ENV}={raw!r} is not a number (finite seconds > 0 required)"
        ) from None
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(
            f"{RATE_BRIDGE_READ_DEADLINE_ENV}={raw!r} must be a finite value > 0 seconds"
        )
    return value


@dataclass(frozen=True, slots=True)
class BridgeReadResult:
    """One cache answer: the snapshot to project + its honest provenance.

    Attributes
    ----------
    snapshot
        The last-known decoded bridge snapshot, or ``None`` (absent / corrupt /
        unwired / nothing read yet) — exactly the value the route unpacked from
        a direct :func:`~brutex.data.rate_bridge.read_rate_snapshot` call, so
        every downstream projection is byte-identical.
    source
        Provenance: :data:`BRIDGE_SOURCE_LIVE` / :data:`BRIDGE_SOURCE_CACHED` /
        :data:`BRIDGE_SOURCE_UNVERIFIED` / :data:`BRIDGE_SOURCE_UNWIRED`.
    probe_age_s
        Seconds since the last COMPLETED refresh attempt (monotonic delta,
        clamped ≥ 0 — the ntm_cache clock-skew precedent): ``0.0`` on live,
        ≤ the TTL on cached, growing unboundedly on unverified, ``None`` when
        no refresh has ever completed (cold) or the bridge is unwired.
    """

    snapshot: rate_bridge.RateBridgeSnapshot | None
    source: str
    probe_age_s: float | None


def stamp_bridge_freshness(payload: dict[str, Any], result: BridgeReadResult) -> None:
    """Stamp ``bridge_source`` + ``bridge_probe_age_s`` onto *payload* (in place) — O(1).

    The ONE place the two honest bridge-freshness payload keys are written
    (CLAUDE.md §11 COMMON — the ``summary_cache.stamp_freshness`` doctrine).
    Additive-only: writes exactly these two keys and mutates nothing else, so
    every pre-existing ``/api/v1/pull/rate`` field stays byte-identical.
    ``probe_age_s`` is rounded to 3 dp (the ``stamp_freshness`` precedent);
    ``None`` passes through as JSON ``null``.

    Parameters
    ----------
    payload
        The route's ``view.to_dict()`` mapping, mutated in place.
    result
        The :class:`BridgeReadResult` this request served.
    """
    payload["bridge_source"] = result.source
    payload["bridge_probe_age_s"] = (
        None if result.probe_age_s is None else round(result.probe_age_s, 3)
    )


class NonBlockingFileMemoBase[MemoValueT]:
    """The ONE TTL + deadline-guard + non-blocking-single-flight memo core.

    Fix 6b (DEC-MEMORY-FOOTPRINT-HARDENING-001): :class:`RateBridgeCache` and
    :class:`~brutex.dashboard.sweep_progress_cache.SweepProgressCache` were
    copy-twin state machines — the same TTL hit / non-blocking follower /
    deadline-guarded owner arms, the same abandoned-daemon bound, the same
    degraded-latch log discipline — duplicated line-for-line (the #1522
    ``refresh_spawn_failed`` hostile-review MAJOR had to be fixed in BOTH
    separately, the exact §11 COMMON failure mode). This base is the ONE
    home; both public classes delegate BYTE-IDENTICALLY (signatures, event
    names, payload keys, TTL/deadline envs all unchanged — both pre-existing
    suites pass UNMODIFIED, which is the parity proof).

    Subclass contract:

    * class attributes :attr:`_EVENT_PREFIX` (log event namespace),
      :attr:`_TARGET_FIELD` (the log field naming the read target) and
      :attr:`_FILE_NOUN` (the degrade-detail noun);
    * :meth:`_memo_value` — return the current memoised value (plain
      attribute read; called WHILE the base holds ``self._lock``, so it must
      never take the lock itself);
    * :meth:`_refresh_body` — ONE refresh attempt, committing its answer
      (incl. advancing ``self._probed_at``) under ``self._lock``; runs on the
      guard's daemon; an ``Exception`` it raises is caught by the base
      (``<prefix>.refresh_failed`` WARN, memo kept, ``_probed_at`` advanced
      so a persistent fast failure retries at TTL cadence, never per poll).

    State/threading law (verbatim from the twins): all state under ONE
    :class:`threading.Lock` held only for O(1) field ops, never across I/O;
    ``_inflight`` is cleared ONLY by the daemon that ran — or by the OWNER
    when the guard raised before the daemon could run — so a wedged disk
    leaves EXACTLY ONE abandoned daemon per outage and a spawn failure can
    never wedge the single-flight (the #1522 MAJOR fold, now fixed ONCE).
    """

    _EVENT_PREFIX: ClassVar[str]
    _TARGET_FIELD: ClassVar[str]
    _FILE_NOUN: ClassVar[str]

    def __init__(
        self,
        *,
        ttl_s: float,
        read_deadline_s: float,
        clock: Callable[[], float],
        guard: Callable[[Callable[[], None], float], object],
        log: Any,
    ) -> None:
        """Bind the resolved knobs (subclasses resolve env defaults FIRST).

        Parameters
        ----------
        ttl_s
            Seconds a completed refresh stays fresh (``0`` disables the TTL
            fast path — every poll revalidates through the guard).
        read_deadline_s
            Wall-clock bound on one refresh.
        clock
            Monotonic clock seam.
        guard
            The freeze guard (production: ``run_with_deadline``).
        log
            The OWNING module's structlog logger — event logger names stay
            byte-identical per cache.
        """
        self._ttl_s = ttl_s
        self._read_deadline_s = read_deadline_s
        self._clock = clock
        self._guard = guard
        self._memo_log = log
        self._lock = threading.Lock()
        # Monotonic instant of the last COMPLETED refresh attempt (None = never).
        self._probed_at: float | None = None
        # True while a refresh is running; see the class docstring's clearing law.
        self._inflight = False
        # Once-per-outage log latch (WARN on the flip in, INFO on the flip out).
        self._degraded = False

    def _memo_value(self) -> MemoValueT | None:
        """Return the memoised value (subclass attribute read — see class docstring)."""
        raise NotImplementedError  # pragma: no cover — subclass seam, never executed

    def _refresh_body(self, target: Path) -> None:
        """Run ONE refresh attempt for *target* (subclass seam — see class docstring)."""
        raise NotImplementedError  # pragma: no cover — subclass seam, never executed

    def _serve_bounded(self, target: Path) -> tuple[MemoValueT | None, str, float | None]:
        """The three-arm serve core: TTL hit / non-blocking follower / owner refresh.

        Returns
        -------
        tuple
            ``(value, source, probe_age_s)`` — source ∈
            {:data:`MEMO_SOURCE_LIVE`, :data:`MEMO_SOURCE_CACHED`,
            :data:`MEMO_SOURCE_UNVERIFIED`}; never raises except
            ``KeyboardInterrupt`` / ``SystemExit`` (flag released first) and
            the fail-closed pull-state kernel errors below.

        Raises
        ------
        brutex.pull_state_rust.PullStateKernelError
            Wheel absent/stale, kernel raise, or a poisoned serve arm —
            fail-closed (DEC-RUST-PULL-STATE-001); raised BEFORE
            ``_inflight`` is taken, so the single-flight state never leaks.
        brutex.pull_state_rust.PullStateFlagRemovedError
            The removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
        """
        with self._lock:
            now = self._clock()
            age = None if self._probed_at is None else max(0.0, now - self._probed_at)
            # --- rate_bridge_cache (Wave 3H, flip phase —
            # DEC-RUST-PULL-STATE-001): the three-arm serve DECISION IS the
            # Rust kernel, consulted over the PRE-COMPUTED age (the clock
            # read + the lock stay Python — the design §2.3
            # "decision-kernel-only, thread core PERMANENT-PYTHON" ruling);
            # the former byte-identical Python conditions are DELETED.  A
            # kernel fault raises the typed error out of this serve (the
            # lock unwinds cleanly; ``_inflight`` is untouched) — the
            # single-flight law stays BELT-guarded: the follower return
            # keeps its own ``or self._inflight`` arm, so a poisoned kernel
            # answering OWNER while a refresh is in flight is FORCED to the
            # follower serve and can never spawn a second refresh daemon.
            arm = _pull_state_rust.rust_memo_serve_arm(age, self._ttl_s, self._inflight)
            if arm == _pull_state_rust.MEMO_ARM_CACHED_HIT:
                # The zero-syscall O(1) hot path between refreshes.
                return (self._memo_value(), MEMO_SOURCE_CACHED, age)
            if arm == _pull_state_rust.MEMO_ARM_FOLLOWER or self._inflight:
                # Non-blocking single-flight: NEVER wait on the in-flight
                # refresh (the load-bearing difference from a timeout-less
                # Event wait) — serve last-known, honestly labelled.  The
                # trailing ``or self._inflight`` is the Wave-3H fail-safe
                # belt (see the seam comment above).
                return (self._memo_value(), MEMO_SOURCE_UNVERIFIED, age)
            self._inflight = True
        try:
            self._guard(functools.partial(self._refresh, target), self._read_deadline_s)
        except DeadlineExceeded:
            # The refresh daemon is wedged on the lake volume. Leave _inflight
            # to the daemon's own finally (bounds abandoned daemons to ONE per
            # outage; its eventual late return self-heals the memo) and serve
            # the last-known value, labelled.
            with self._lock:
                first_flip = not self._degraded
                self._degraded = True
                value = self._memo_value()
                probed_at = self._probed_at
                now = self._clock()
            if first_flip:
                self._memo_log.warning(
                    f"{self._EVENT_PREFIX}.read_degraded",
                    deadline_s=self._read_deadline_s,
                    detail=(
                        f"{self._FILE_NOUN} refresh blew its wall-clock deadline (a slow/"
                        "stalled lake volume) — serving the last-known snapshot as "
                        "'unverified' until the volume recovers"
                    ),
                    **{self._TARGET_FIELD: str(target)},
                )
            probe_age = None if probed_at is None else max(0.0, now - probed_at)
            return (value, MEMO_SOURCE_UNVERIFIED, probe_age)
        except Exception as exc:  # pylint: disable=broad-except — the poll route must never 500
            # The guard failed BEFORE its refresh daemon could run — with the
            # production ``run_with_deadline`` the only non-DeadlineExceeded
            # raise is ``threading.Thread(...).start()`` failing under
            # thread/memory exhaustion (``_refresh`` itself swallows every
            # Exception), so NO daemon exists to clear ``_inflight``: release
            # it HERE, or the cache would latch ``unverified`` forever with
            # zero retries and the raise would 500 the poll route (the #1522
            # hostile-review MAJOR — fixed ONCE here for both twins). With an
            # INJECTED guard whose daemon DID run, this re-clear is at worst
            # an early release (one duplicate refresh, last-write-wins commit)
            # — never a wedge, never a stale illusion. Serve last-known,
            # labelled; the next post-TTL poll retries (self-healing at poll
            # cadence).
            with self._lock:
                self._inflight = False
                first_flip = not self._degraded
                self._degraded = True
                value = self._memo_value()
                probed_at = self._probed_at
                now = self._clock()
            if first_flip:
                self._memo_log.warning(
                    f"{self._EVENT_PREFIX}.refresh_spawn_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    detail=(
                        "the deadline guard failed before the refresh daemon "
                        "could run (thread/memory exhaustion) — serving the "
                        "last-known snapshot as 'unverified'; the next poll "
                        "retries the refresh"
                    ),
                    **{self._TARGET_FIELD: str(target)},
                )
            probe_age = None if probed_at is None else max(0.0, now - probed_at)
            return (value, MEMO_SOURCE_UNVERIFIED, probe_age)
        except BaseException:
            # KeyboardInterrupt / SystemExit: NEVER swallowed — but the
            # single-flight flag must not leak with them either (no daemon ran
            # to clear it; see the Exception arm above).
            with self._lock:
                self._inflight = False
            raise
        with self._lock:
            recovered = self._degraded
            self._degraded = False
            value = self._memo_value()
        if recovered:
            self._memo_log.info(
                f"{self._EVENT_PREFIX}.read_recovered", **{self._TARGET_FIELD: str(target)}
            )
        return (value, MEMO_SOURCE_LIVE, 0.0)

    def _refresh(self, target: Path) -> None:
        """Run :meth:`_refresh_body` on the guard's daemon with the twins' catch law.

        A raising INJECTED seam (the production probes/readers never raise by
        contract) is caught + WARNed, the memo kept, and ``_probed_at`` STILL
        advanced so a persistent fast failure retries at TTL cadence, never
        per poll. The ``finally`` clears ``_inflight`` on EVERY path where
        this daemon ran; the owner releases it only when the guard raised
        before this daemon could run (see :meth:`_serve_bounded`).
        """
        try:
            self._refresh_body(target)
        except Exception as exc:  # pylint: disable=broad-except — an injected seam must never wedge the cache
            self._memo_log.warning(
                f"{self._EVENT_PREFIX}.refresh_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                **{self._TARGET_FIELD: str(target)},
            )
            with self._lock:
                self._probed_at = self._clock()
        finally:
            with self._lock:
                self._inflight = False


class RateBridgeCache(NonBlockingFileMemoBase[rate_bridge.RateBridgeSnapshot]):
    """TTL + content-token memo over the bridge-file read, never blocking its caller.

    One instance per app (injected by ``build_app`` — no module-global mutable
    state, CLAUDE.md §4). Thread-safe: all state lives under ONE
    :class:`threading.Lock` held only for O(1) field ops, never across I/O.
    See the module docstring for the full mechanism + honest envelope.

    Parameters
    ----------
    ttl_s
        Seconds a completed refresh stays fresh; ``None`` resolves
        :func:`resolve_rate_bridge_ttl_s` (env-overridable). ``0`` disables the
        TTL fast path (every poll revalidates through the guard).
    read_deadline_s
        Wall-clock bound on one refresh; ``None`` resolves
        :func:`resolve_rate_bridge_read_deadline_s` (env-overridable).
    clock
        Monotonic clock seam (tests inject a fake); default ``time.monotonic``.
    probe
        Content-token probe (one ``os.stat``); default
        :func:`~brutex.dashboard.lake_revision.rate_bridge_revision`. Runs
        INSIDE the deadline guard — a wedged mount hangs ``os.stat`` exactly
        like ``read_bytes``.
    reader
        Snapshot reader; ``None`` LATE-BINDS to
        ``brutex.data.rate_bridge.read_rate_snapshot`` via module attribute at
        call time, so the existing ``monkeypatch.setattr("brutex.data.
        rate_bridge.read_rate_snapshot", ...)`` test seam keeps working.
    guard
        The freeze guard; default
        :func:`~brutex.adapters.groww.deadline.run_with_deadline`. Tests inject
        ``lambda fn, s: fn()`` for thread-free inline refreshes.
    """

    _EVENT_PREFIX: ClassVar[str] = "rate_bridge_cache"
    _TARGET_FIELD: ClassVar[str] = "lake_root"
    _FILE_NOUN: ClassVar[str] = "bridge-file"

    def __init__(
        self,
        *,
        ttl_s: float | None = None,
        read_deadline_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        probe: Callable[[Path], str | None] = rate_bridge_revision,
        reader: Callable[[Path], rate_bridge.RateBridgeSnapshot | None] | None = None,
        guard: Callable[[Callable[[], None], float], object] = run_with_deadline,
    ) -> None:
        super().__init__(
            ttl_s=resolve_rate_bridge_ttl_s() if ttl_s is None else ttl_s,
            read_deadline_s=(
                resolve_rate_bridge_read_deadline_s()
                if read_deadline_s is None
                else read_deadline_s
            ),
            clock=clock,
            guard=guard,
            log=_LOG,
        )
        self._probe = probe
        self._reader = reader
        self._snapshot: rate_bridge.RateBridgeSnapshot | None = None
        self._token: str | None = None

    def get(self, lake_root: Path) -> BridgeReadResult:
        """Return the bridge snapshot + provenance for *lake_root*, bounded.

        Three arms: an O(1) zero-syscall TTL hit; an IMMEDIATE last-known
        answer while a refresh is in flight (the non-blocking single-flight);
        or an OWNER refresh bounded by the deadline guard (≤ ``read_deadline_s``
        even on a wedged disk, after which the memo is served ``unverified``).
        A guard that FAILS to spawn its refresh daemon (``Thread.start()``
        under thread/memory exhaustion) degrades to the SAME labelled
        ``unverified`` serve with the single-flight flag released — never a
        wedge; ``KeyboardInterrupt`` / ``SystemExit`` propagate (with the
        flag released first), and a pull-state kernel fault raises the typed
        fail-closed error (see :meth:`NonBlockingFileMemoBase._serve_bounded`
        — the serve DECISION is the Rust kernel, DEC-RUST-PULL-STATE-001).

        Parameters
        ----------
        lake_root
            The lake root (locates ``.rate/pull_rate.json``). One cache serves
            one lake — the app wires exactly one store/lake pair.

        Returns
        -------
        BridgeReadResult
            The snapshot to project + its honest ``source`` / ``probe_age_s``.
        """
        snapshot, source, probe_age = self._serve_bounded(lake_root)
        return BridgeReadResult(snapshot, source, probe_age)

    def _memo_value(self) -> rate_bridge.RateBridgeSnapshot | None:
        """The memoised snapshot (called under the base's lock — plain read)."""
        return self._snapshot

    def _refresh_body(self, target: Path) -> None:
        """Run ONE stat+decide+read refresh and commit it (runs on the guard's daemon).

        The guard discards the return value, so the commit is by side effect
        under the lock (last-write-wins — a LATE-returning wedged refresh still
        repairs the cache when the volume recovers). Token decision — the Rust
        kernel's :func:`~brutex.pull_state_rust.rust_token_revalidates` law
        (DEC-RUST-PULL-STATE-001, flip phase): an unchanged non-``None`` token
        revalidates without re-reading; a changed / first / ``None`` ("unknown
        — if uncertain, fetch") token re-reads. A ``None`` token is stored as
        ``None`` so an erroring stat is never trusted — the next post-TTL
        owner re-probes AND re-reads. The catch/finally discipline (raising
        injected seam — a kernel fault included — WARNed + memo kept +
        ``_probed_at`` advanced; ``_inflight`` cleared) lives ONCE in the base
        (:meth:`NonBlockingFileMemoBase._refresh`).
        """
        token = self._probe(target)
        with self._lock:
            prior_token = self._token
        # --- rate_bridge_cache (Wave 3H, flip phase —
        # DEC-RUST-PULL-STATE-001): the token-revalidation law IS the Rust
        # kernel; the former byte-identical Python compare is DELETED.  A
        # kernel fault (incl. a lone-surrogate token failing the pyo3
        # ``&str`` edge) raises the typed error, which the PERMANENT-PYTHON
        # base catch law (:meth:`NonBlockingFileMemoBase._refresh`) folds
        # into its ``refresh_failed`` WARN — the memo is kept and
        # ``_probed_at`` advances, so the fault retries at TTL cadence (a
        # daemon-thread raise has nowhere louder to go; the SERVE path's own
        # kernel consult raises out of ``get()`` loudly).  The read itself +
        # the LATE-BOUND reader seam stay Python.
        revalidates = _pull_state_rust.rust_token_revalidates(token, prior_token)
        if revalidates:
            # Revalidated: the file is byte-unchanged (atomic-replace
            # publisher ⇒ same mtime_ns:size:ino = same content) — keep the
            # memo, skip the read.
            with self._lock:
                self._probed_at = self._clock()
        else:
            reader = self._reader
            if reader is None:
                # LATE-BOUND module attribute — preserves the existing
                # monkeypatch.setattr("brutex.data.rate_bridge.
                # read_rate_snapshot", ...) seam.
                reader = rate_bridge.read_rate_snapshot
            snapshot = reader(target)
            with self._lock:
                self._snapshot = snapshot
                self._token = token
                self._probed_at = self._clock()
