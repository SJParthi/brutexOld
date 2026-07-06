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
(CLAUDE.md §11 COMMON, no copy):

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
  ``_inflight`` is cleared ONLY by the refresh daemon itself, so a wedged disk
  leaves EXACTLY ONE abandoned daemon per outage (not one per TTL), and its
  eventual late return self-heals the cache (last-write-wins adoption).

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
from typing import Any, Final

import structlog

from brutex.adapters.groww.deadline import DeadlineExceeded, run_with_deadline
from brutex.dashboard.lake_revision import rate_bridge_revision
from brutex.data import rate_bridge

_LOG = structlog.get_logger("brutex.dashboard.rate_bridge_cache")

__all__ = [
    "BRIDGE_SOURCE_CACHED",
    "BRIDGE_SOURCE_LIVE",
    "BRIDGE_SOURCE_UNVERIFIED",
    "BRIDGE_SOURCE_UNWIRED",
    "RATE_BRIDGE_CACHE_TTL_S",
    "RATE_BRIDGE_READ_DEADLINE_ENV",
    "RATE_BRIDGE_READ_DEADLINE_S",
    "RATE_BRIDGE_TTL_ENV",
    "BridgeReadResult",
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

#: This request's refresh COMPLETED within the deadline (a fresh read, a stat
#: revalidation, or an honest completed degrade such as a corrupt-file ``None``).
BRIDGE_SOURCE_LIVE: Final[str] = "live"
#: Served from the memo inside the TTL window — verification lag ≤ the TTL.
BRIDGE_SOURCE_CACHED: Final[str] = "cached"
#: A refresh is in flight / wedged, or this owner's deadline expired — the
#: served value is last-known and may lag arbitrarily (LABELLED, never silent).
BRIDGE_SOURCE_UNVERIFIED: Final[str] = "unverified"
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


class RateBridgeCache:
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
        self._ttl_s = resolve_rate_bridge_ttl_s() if ttl_s is None else ttl_s
        self._read_deadline_s = (
            resolve_rate_bridge_read_deadline_s() if read_deadline_s is None else read_deadline_s
        )
        self._clock = clock
        self._probe = probe
        self._reader = reader
        self._guard = guard
        self._lock = threading.Lock()
        self._snapshot: rate_bridge.RateBridgeSnapshot | None = None
        self._token: str | None = None
        # Monotonic instant of the last COMPLETED refresh attempt (None = never).
        self._probed_at: float | None = None
        # True while a refresh daemon is running; cleared ONLY by that daemon's
        # finally — so a deadline-exceeded owner leaves it SET and abandoned
        # daemons are bounded to EXACTLY ONE per outage.
        self._inflight = False
        # Once-per-outage log latch (WARN on the flip in, INFO on the flip out).
        self._degraded = False

    def get(self, lake_root: Path) -> BridgeReadResult:
        """Return the bridge snapshot + provenance for *lake_root*, bounded, never raising.

        Three arms: an O(1) zero-syscall TTL hit; an IMMEDIATE last-known
        answer while a refresh is in flight (the non-blocking single-flight);
        or an OWNER refresh bounded by the deadline guard (≤ ``read_deadline_s``
        even on a wedged disk, after which the memo is served ``unverified``).

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
        with self._lock:
            now = self._clock()
            age = None if self._probed_at is None else max(0.0, now - self._probed_at)
            if age is not None and self._ttl_s > 0 and age < self._ttl_s:
                # The zero-syscall O(1) hot path: every widget tick across all
                # open tabs lands here between refreshes.
                return BridgeReadResult(self._snapshot, BRIDGE_SOURCE_CACHED, age)
            if self._inflight:
                # Non-blocking single-flight: NEVER wait on the in-flight
                # refresh (the load-bearing difference from a timeout-less
                # Event wait) — serve last-known, honestly labelled.
                return BridgeReadResult(self._snapshot, BRIDGE_SOURCE_UNVERIFIED, age)
            self._inflight = True
        try:
            self._guard(functools.partial(self._refresh, lake_root), self._read_deadline_s)
        except DeadlineExceeded:
            # The refresh daemon is wedged on the lake volume. Leave _inflight
            # to the daemon's own finally (bounds abandoned daemons to ONE per
            # outage; its eventual late return self-heals the memo) and serve
            # the last-known value, labelled.
            with self._lock:
                first_flip = not self._degraded
                self._degraded = True
                snapshot = self._snapshot
                probed_at = self._probed_at
                now = self._clock()
            if first_flip:
                _LOG.warning(
                    "rate_bridge_cache.read_degraded",
                    lake_root=str(lake_root),
                    deadline_s=self._read_deadline_s,
                    detail=(
                        "bridge-file refresh blew its wall-clock deadline (a slow/"
                        "stalled lake volume) — serving the last-known snapshot as "
                        "'unverified' until the volume recovers"
                    ),
                )
            probe_age = None if probed_at is None else max(0.0, now - probed_at)
            return BridgeReadResult(snapshot, BRIDGE_SOURCE_UNVERIFIED, probe_age)
        with self._lock:
            recovered = self._degraded
            self._degraded = False
            snapshot = self._snapshot
        if recovered:
            _LOG.info("rate_bridge_cache.read_recovered", lake_root=str(lake_root))
        return BridgeReadResult(snapshot, BRIDGE_SOURCE_LIVE, 0.0)

    def _refresh(self, lake_root: Path) -> None:
        """Run ONE stat+decide+read refresh and commit it (runs on the guard's daemon).

        The guard discards the return value, so the commit is by side effect
        under the lock (last-write-wins — a LATE-returning wedged refresh still
        repairs the cache when the volume recovers). Token decision: an
        unchanged non-``None`` token revalidates without re-reading; a changed
        / first / ``None`` ("unknown — if uncertain, fetch") token re-reads. A
        ``None`` token is stored as ``None`` so an erroring stat is never
        trusted — the next post-TTL owner re-probes AND re-reads. A raising
        INJECTED probe/reader (the production ones never raise by contract) is
        caught + WARNed, the memo kept, and ``_probed_at`` STILL advanced so a
        persistent fast failure retries at TTL cadence, never per poll. The
        ``finally`` clears ``_inflight`` — ONLY here, never on the owner.
        """
        try:
            token = self._probe(lake_root)
            with self._lock:
                prior_token = self._token
            if token is not None and token == prior_token:
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
                snapshot = reader(lake_root)
                with self._lock:
                    self._snapshot = snapshot
                    self._token = token
                    self._probed_at = self._clock()
        except Exception as exc:  # pylint: disable=broad-except — an injected seam must never wedge the cache
            _LOG.warning(
                "rate_bridge_cache.refresh_failed",
                lake_root=str(lake_root),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            with self._lock:
                self._probed_at = self._clock()
        finally:
            with self._lock:
                self._inflight = False
