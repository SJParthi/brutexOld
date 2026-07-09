"""Non-blocking last-known-verdict holder for the lake duplicate census.

DEC-LAKE-CENSUS-NONBLOCKING-001. The ``/api/v1/universe/lake-uniqueness``
census is an O(files) whole-lake scan — 26 minutes measured on the operator's
2.14M-file lake. The pre-fix route offloaded the scan to ``run_in_threadpool``
(the loop stayed free — the #1316/#1302 offloads were real), but
``SummaryCache``'s COLD single-flight parked every concurrent poller in an
UNBOUNDED ``decision.wait()`` ON a threadpool worker, and ``universe.html``
polls the route every 15 s with no in-flight guard — so ~40 stacked pollers
exhausted the shared default AnyIO limiter and EVERY offloaded route (the
whole data surface incl. ``/db``) froze until the scan completed (reproduced
on a real uvicorn: ``/api/v1/state`` = 5 ms while ``/api/v1/universe/summary``
+ the ``/db`` datasets discovery were frozen >10 s).

:class:`LakeCensusHolder` fixes the wedge structurally:

* the scan runs on a DEDICATED named daemon thread (the
  :func:`brutex.data.lake_heal._default_spawn` pattern) — ZERO AnyIO
  threadpool tokens, ever;
* it is SINGLE-FLIGHT — a concurrent poll neither spawns a second scan nor
  waits for the running one (this SUPERSEDES DEC-SCAN-SINGLE-FLIGHT-001's
  unbounded-wait clause for the LAKE_UNIQUENESS kind ONLY; the SPOT/OPTIONS
  ``SummaryCache`` kinds keep their wait — same hazard class, a named
  follow-up in MIGRATION.md);
* :meth:`snapshot` is O(1): the last-known verdict (or the honest
  no-verdict-yet warming state — never a fabricated verdict) plus
  ``scan_in_progress`` + a live ``scan_progress`` counter fed by the
  scanner's EXISTING ``per_file_pause`` seam (zero scanner changes);
* freshness mirrors the pre-fix law: a scan re-arms only when the write
  generation moved AND the adaptive TTL floor (the ONE
  :func:`~brutex.dashboard.summary_cache.effective_adaptive_floor` #920
  formula) has elapsed — a pull's ``mark_dirty`` storm can never amplify the
  15 s poll into back-to-back whole-lake scans.

Honest envelope (Golden Rule #10): the scan itself stays O(files) — this
module changes WHERE it runs and what a poll pays (O(1)), never the scan's
own cost; the first-ever boot has NO verdict until one scan completes (served
as ``scan_in_progress`` with progress, never fabricated); a scan ERROR is
stored + surfaced (``scan_error``) while the last-known verdict keeps serving,
and the next poll past the base floor re-arms.

No module-global state (CLAUDE.md §4): constructed in
:func:`brutex.dashboard.app.build_app` with injectable ``scan`` / ``clock`` /
``spawn`` / ``now_utc`` seams for deterministic tests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

import structlog

from brutex.dashboard.summary_cache import (
    DEFAULT_TTL_FLOOR_S,
    MAX_ADAPTIVE_FLOOR_S,
    effective_adaptive_floor,
)

_LOG = structlog.get_logger("brutex.dashboard.lake_census")

#: The dedicated scan thread's name — pinned by the offload regression tests
#: (an AnyIO worker thread would carry a different name; the census must never
#: consume a shared threadpool token).
CENSUS_THREAD_NAME: Final[str] = "brutex-lake-census"

#: The census scan callable: receives the holder's per-file progress callback
#: (threaded into ``scan_lake_invariants(per_file_pause=...)`` — the existing
#: L3 throttle seam, which fires per file INCLUDING skips) and returns the
#: ``census_to_dict``-shaped payload.
CensusScan = Callable[[Callable[[], None]], dict[str, Any]]


def _default_spawn(body: Callable[[], None]) -> None:
    """Run *body* on the named census daemon thread (the lake_heal pattern)."""
    threading.Thread(target=body, name=CENSUS_THREAD_NAME, daemon=True).start()


def _utc_now_iso() -> str:
    """Default wall-clock stamp — UTC ISO-8601 (injected for testability)."""
    return datetime.now(UTC).isoformat()


class LakeCensusHolder:
    """Single-flight daemon-thread census runner + O(1) verdict snapshot.

    Parameters
    ----------
    scan
        The census builder (production: ``app._build_lake_census_payload``).
        Called ON the daemon thread with the holder's progress callback.
    generation
        Zero-arg write-generation reader (production:
        ``lambda: _summary_cache.generation``). Called ON the daemon thread —
        NEVER on the event loop: the probe stats the window ledger + digests
        the sweeps tree (lake I/O, the #1316 offload class), which is WHY
        :meth:`maybe_refresh` takes no generation argument and defers the
        read to the check thread.
    files_total_estimate
        Optional zero-arg estimator for the honest progress denominator
        (production: the L3 sweep sentinel's ``files_scanned`` — ``None`` on a
        first-ever lake with no sentinel; a raising estimator degrades to
        ``None``, never a crash). Read ON the daemon thread (it is lake I/O).
    clock
        Monotonic time source (injected for deterministic TTL tests);
        ``None`` = :func:`time.monotonic`.
    now_utc
        Wall-clock ISO stamp source for ``started_utc`` / ``as_of_utc``.
    spawn
        Thread-spawn seam; ``None`` = the named daemon thread.
    ttl_floor_s / max_floor_s
        The #920 adaptive-floor parameters (base + cap), byte-identical
        semantics to the superseded ``SummaryCache`` wiring.
    """

    def __init__(
        self,
        *,
        scan: CensusScan,
        generation: Callable[[], int],
        files_total_estimate: Callable[[], int | None] | None = None,
        clock: Callable[[], float] | None = None,
        now_utc: Callable[[], str] = _utc_now_iso,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        ttl_floor_s: float = DEFAULT_TTL_FLOOR_S,
        max_floor_s: float = MAX_ADAPTIVE_FLOOR_S,
    ) -> None:
        self._scan = scan
        self._generation = generation
        self._files_total_estimate = files_total_estimate
        self._clock = clock if clock is not None else time.monotonic
        self._now_utc = now_utc
        self._spawn = spawn if spawn is not None else _default_spawn
        self._ttl_floor_s = ttl_floor_s
        self._max_floor_s = max_floor_s
        self._lock = threading.Lock()
        #: Single-flight over the whole check-then-maybe-scan thread body.
        self._in_flight = False
        #: True ONLY while the actual O(files) scan is running (the check
        #: prefix — one generation read — is not "scanning").
        self._scanning = False
        #: Last-known census payload (``census_to_dict`` shape), or ``None``
        #: until the first scan lands. NEVER fabricated.
        self._payload: dict[str, Any] | None = None
        #: The write generation observed at the LAST landed scan's start.
        self._generation_at: int | None = None
        #: Clock stamps driving the adaptive TTL floor (#920 semantics).
        self._computed_at = 0.0
        self._compute_duration = 0.0
        #: Clock stamp of the last spawned check — bounds check-thread spawns
        #: to at most one per base floor window even when nothing changed.
        self._last_check_at: float | None = None
        #: Live progress (fed per file by the scanner's per_file_pause seam).
        self._files_scanned = 0
        self._estimate: int | None = None
        self._started_utc: str | None = None
        #: Completion wall-clock of the last LANDED scan (the verdict's age).
        self._as_of_utc: str | None = None
        #: Last scan error (``"Type: msg"``), cleared by the next clean scan.
        self._error: str | None = None
        #: Landed-scan counter (observability / test oracle).
        self._scans_completed = 0

    # ── the O(1) read surface ────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """The O(1) route payload: last-known verdict + honest scan state.

        A landed verdict's keys are byte-compatible with the pre-fix payload
        (``census_to_dict`` shape) with FOUR additive keys stamped on top:
        ``scan_in_progress`` / ``scan_error`` / ``as_of_utc`` /
        ``scan_progress`` (``files_scanned`` + ``files_total_estimate`` +
        ``started_utc``). Before the first scan lands the payload is the NEW
        honest warming arm — ``{"present": true}`` plus the same four keys and
        NO verdict fields (a verdict is never fabricated, Golden Rule #1).
        """
        with self._lock:
            base: dict[str, Any] = (
                dict(self._payload) if self._payload is not None else {"present": True}
            )
            # A first-ever in-flight check with no payload ALWAYS proceeds to
            # the scan (nothing to serve → needs_scan unconditionally), so it
            # honestly reads in-progress even before the scan body is entered.
            base["scan_in_progress"] = self._scanning or (self._in_flight and self._payload is None)
            base["scan_error"] = self._error
            base["as_of_utc"] = self._as_of_utc
            base["scan_progress"] = {
                "files_scanned": self._files_scanned,
                "files_total_estimate": self._estimate,
                "started_utc": self._started_utc,
            }
            return base

    @property
    def scans_completed(self) -> int:
        """How many scans have LANDED a payload — O(1) (test/observability)."""
        with self._lock:
            return self._scans_completed

    # ── the non-blocking refresh surface ─────────────────────────────────────

    def maybe_refresh(self) -> bool:
        """Arm ONE background freshness check if eligible — NEVER blocks.

        O(1) pure clock/flag math on the caller's thread (the event loop): no
        generation read, no lake I/O — the generation probe is lake I/O
        (#1316) and runs on the spawned daemon thread instead. Eligibility:
        not already in flight, the last landed scan is past its adaptive
        floor (``max(base, its own duration)`` capped — the #920 law), and at
        least one base floor since the last spawned check (so stacked pollers
        cost at most one tiny check thread per window). Returns ``True`` iff
        a check thread was spawned. A spawn failure (thread exhaustion) is
        WARNed and released — the next poll retries; never a route 500.
        """
        with self._lock:
            if self._in_flight:
                return False
            now = self._clock()
            if self._payload is not None or self._error is not None:
                floor = effective_adaptive_floor(
                    self._compute_duration, base_s=self._ttl_floor_s, cap_s=self._max_floor_s
                )
                if now - self._computed_at < floor:
                    return False
            if self._last_check_at is not None and now - self._last_check_at < self._ttl_floor_s:
                return False
            self._in_flight = True
            self._last_check_at = now
        try:
            self._spawn(self._run_check)
        except Exception as exc:  # pylint: disable=broad-except — spawn must never 500 a poll
            with self._lock:
                self._in_flight = False
            _LOG.warning("lake_census.spawn_failed", error=str(exc))
            return False
        return True

    # ── the daemon-thread body ───────────────────────────────────────────────

    def _run_check(self) -> None:
        """Check freshness, scan if stale — the whole body runs OFF the loop."""
        try:
            gen: int | None
            try:
                gen = self._generation()
            except Exception as exc:  # pylint: disable=broad-except — probe error ≠ dead census
                _LOG.warning("lake_census.generation_probe_failed", error=str(exc))
                gen = None
            with self._lock:
                payload_missing = self._payload is None
                generation_at = self._generation_at
            if not payload_missing and (gen is None or gen == generation_at):
                # Fresh enough (or unknowable this round): the stored verdict
                # stands; an unknown generation with a payload serves stale and
                # retries next window — never a speculative whole-lake scan.
                return
            self._run_scan(gen)
        finally:
            with self._lock:
                self._in_flight = False

    def _run_scan(self, gen: int | None) -> None:
        """Run ONE census scan and land its outcome — success OR honest error."""
        estimate: int | None = None
        if self._files_total_estimate is not None:
            try:
                estimate = self._files_total_estimate()
            except Exception as exc:  # pylint: disable=broad-except — estimate is best-effort
                _LOG.warning("lake_census.estimate_failed", error=str(exc))
                estimate = None
        with self._lock:
            self._scanning = True
            self._files_scanned = 0
            self._estimate = estimate
            self._started_utc = self._now_utc()
        started = self._clock()
        try:
            payload = self._scan(self._count_file)
        except Exception as exc:  # pylint: disable=broad-except — an erroring scan must not kill polls
            _LOG.warning("lake_census.scan_failed", error=str(exc), error_type=type(exc).__name__)
            with self._lock:
                self._scanning = False
                # HONEST error state: the last-known verdict (if any) keeps
                # serving with ``scan_error`` stamped; a verdict is NEVER
                # fabricated. The base floor (duration reset to 0) lets the
                # next poll past ~2 s re-arm instead of waiting out a stale
                # 120 s adaptive floor from the previous slow success.
                self._error = f"{type(exc).__name__}: {exc}"
                self._computed_at = self._clock()
                self._compute_duration = 0.0
            return
        finished = self._clock()
        with self._lock:
            self._scanning = False
            self._payload = payload
            self._generation_at = gen
            self._computed_at = finished
            self._compute_duration = max(finished - started, 0.0)
            self._as_of_utc = self._now_utc()
            self._error = None
            self._scans_completed += 1

    def _count_file(self) -> None:
        """The per-file progress tick (the scanner's ``per_file_pause`` seam)."""
        with self._lock:
            self._files_scanned += 1


__all__ = [
    "CENSUS_THREAD_NAME",
    "CensusScan",
    "LakeCensusHolder",
]
