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

import math
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

#: Floor for the scan-STALLED detection bound (seconds): an in-flight scan is
#: honestly flagged ``scan_stalled`` once its runtime exceeds ``max(2 × the
#: last landed scan's duration, this floor)`` — generous by design (the
#: production scan legitimately runs ~26 min on a 2.14M-file lake, and the
#: dynamic 2× term scales the bound once a real duration is known).
#: DETECTION, not termination (the #999 stance): the daemon thread is never
#: killed, the last-known verdict keeps serving, and the WARN fires once per
#: episode. NOT an env knob — the non-blocking behaviour is DEFAULT-ON
#: with zero configuration; the constructor parameter is a test seam only.
#: FIRST-scan honesty: before any scan has landed a duration, the bound is
#: additionally seeded from the L3 sweep sentinel via the
#: ``first_scan_bound_hint`` seam (``max(hint, this floor)``) so a bigger
#: lake's legitimately-slow FIRST scan is never false-flagged; after the
#: first completion the 2× law takes over.
SCAN_STALE_FLOOR_S: Final[float] = 1800.0

#: Conservative per-file budget (seconds) for deriving the FIRST-scan stall
#: bound from the L3 sentinel's ``files_scanned`` when it recorded no
#: duration: 2 ms/file — the measured production scan ran ~26 min over the
#: 2.14M-file lake (~0.73 ms/file), so 2 ms/file over-estimates ~2.7× (a
#: stall bound must err generous, never trigger-happy).
FIRST_SCAN_PER_FILE_BUDGET_S: Final[float] = 0.002

#: PHYSICAL CEILING (seconds) on the sentinel-seeded FIRST-scan stall bound —
#: 24 h. Round-2 refuter: ``math.isfinite`` alone is not enough — a corrupt
#: sentinel with a FINITE-but-astronomical value (``duration_ms=1e308`` →
#: hint 1e305 s) passed the finite+positive gates and yielded an effectively
#: infinite bound, silently disabling staleness detection for the
#: corrupt-sentinel + wedged-first-scan combo. A hint ABOVE this ceiling is
#: treated as ABSENT (the bare :data:`SCAN_STALE_FLOOR_S` governs). 24 h is
#: generous by orders of magnitude against physics — the measured production
#: census runs ~26 min over a 2.14M-file lake and is unthrottled (unlike the
#: L3 sweep's opt-in pacing); no plausible lake legitimately needs a >24 h
#: FIRST-scan bound, so anything above it is a corrupt sentinel, never a hint.
FIRST_SCAN_BOUND_CEILING_S: Final[float] = 24 * 3600.0

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
    scan_stale_after_s
        Explicit scan-STALLED detection bound (seconds); ``None`` (production)
        derives ``max(2 × the last landed scan's duration,
        :data:`SCAN_STALE_FLOOR_S`)`` per snapshot. A test seam, never an env
        knob — the non-blocking route is DEFAULT-ON with zero configuration.
    first_scan_bound_hint
        Optional zero-arg FIRST-scan stall-bound seed (seconds; production:
        the L3 sentinel's recorded duration when stored, else its
        ``files_scanned`` × :data:`FIRST_SCAN_PER_FILE_BUDGET_S`). Read ON
        the daemon thread before the first scan (it is lake I/O); a raising
        / ``None`` / non-finite / above-:data:`FIRST_SCAN_BOUND_CEILING_S`
        hint degrades to the bare floor (a corrupt sentinel is never a
        hint). Inert once a scan has landed a real duration (the 2× law
        governs from then on).
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
        scan_stale_after_s: float | None = None,
        first_scan_bound_hint: Callable[[], float | None] | None = None,
    ) -> None:
        self._scan = scan
        self._generation = generation
        self._files_total_estimate = files_total_estimate
        self._clock = clock if clock is not None else time.monotonic
        self._now_utc = now_utc
        self._spawn = spawn if spawn is not None else _default_spawn
        self._ttl_floor_s = ttl_floor_s
        self._max_floor_s = max_floor_s
        self._scan_stale_after_s = scan_stale_after_s
        self._first_scan_bound_hint = first_scan_bound_hint
        #: The resolved first-scan bound seed (read once on the daemon thread
        #: before the first scan; ``None`` until read / when unavailable).
        self._first_bound_hint: float | None = None
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
        #: Monotonic clock stamp at the in-flight scan's start (the STALLED
        #: detection baseline; ``None`` = no scan ever started).
        self._scan_started_clock: float | None = None
        #: Once-per-episode throttle for the ``lake_census.scan_stalled`` WARN
        #: (reset at every new scan start).
        self._stall_warned = False
        #: Completion wall-clock of the last LANDED scan (the verdict's age).
        self._as_of_utc: str | None = None
        #: Last scan error — a STABLE sanitised token + exception CLASS name
        #: only (the full detail lives in the server log — never raw
        #: exception text into the route/DOM); cleared by the next clean scan.
        self._error: str | None = None
        #: Landed-scan counter (observability / test oracle).
        self._scans_completed = 0

    # ── the O(1) read surface ────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """The O(1) route payload: last-known verdict + honest scan state.

        A landed verdict's keys are byte-compatible with the pre-fix payload
        (``census_to_dict`` shape) with the additive keys stamped on top:
        ``scan_in_progress`` / ``scan_error`` / ``as_of_utc`` /
        ``scan_progress`` (``files_scanned`` + ``files_total_estimate`` +
        ``started_utc``) / ``scan_stalled`` / ``refresh_probe_wedged`` (+
        ``refresh_probe_wedged_s``). Before the first scan lands the payload
        is the NEW honest warming arm — ``{"present": true}`` plus the same
        additive keys and NO verdict fields (a verdict is never fabricated,
        Golden Rule #1).

        LOUD STALENESS (never a silent forever-warming): an in-flight episode
        older than ``max(2 × the last landed scan's duration,
        SCAN_STALE_FLOOR_S)`` — seeded on the FIRST scan from the L3
        sentinel hint — is flagged + WARNed ONCE per episode
        (``lake_census.scan_stalled``) — DETECTION not termination (the #999
        stance: the daemon thread cannot be killed; the last-known verdict
        keeps serving). The flag is SPLIT so the pair can never contradict
        (the round-1 refuter's "✅ CLEAN yet census scan stalled" repro):

        * ``scan_stalled`` — ONLY where ``scan_in_progress`` is also true: a
          wedged SCAN body (``phase="scan"``, judged from the scan's own
          start stamp) or a wedged FIRST-BOOT check prefix with no payload
          to serve (``phase="probe"`` — the generation probe is lake I/O and
          can hang BEFORE the scan body sets ``_scanning``; without this arm
          a first boot would read ``scan_in_progress`` with
          ``started_utc=None`` FOREVER, a silent forever-warming);
        * ``refresh_probe_wedged`` — a wedged REFRESH probe while a landed
          payload keeps serving (``scan_in_progress`` false): judged from
          the episode's OWN arming stamp (``_last_check_at``), NEVER the
          previous scan's ``started_utc`` (whose age is meaningless here),
          with ``refresh_probe_wedged_s`` = seconds since arming.

        The once-per-episode WARN carries ``started_utc``/``files_scanned``
        ONLY for ``phase="scan"`` — a probe episode never reuses the
        PREVIOUS scan's stamps. A backwards clock (skew: now < baseline)
        can never false-flag — negative elapsed is below every bound.
        Completion / error clears both flags.
        """
        stall_warn: dict[str, Any] | None = None
        with self._lock:
            base: dict[str, Any] = (
                dict(self._payload) if self._payload is not None else {"present": True}
            )
            # A first-ever in-flight check with no payload ALWAYS proceeds to
            # the scan (nothing to serve → needs_scan unconditionally), so it
            # honestly reads in-progress even before the scan body is entered.
            in_progress = self._scanning or (self._in_flight and self._payload is None)
            base["scan_in_progress"] = in_progress
            stalled = False
            probe_wedged = False
            probe_wedged_s: float | None = None
            phase: str | None = None
            baseline: float | None = None
            if self._scanning:
                # ``_scanning`` is only ever set together with the start stamp
                # (same lock block in ``_run_scan``) — the ternary is a
                # type-narrowing belt, not a live branch.
                started_clock = self._scan_started_clock
                baseline = started_clock if started_clock is not None else self._clock()
                phase = "scan"
            elif self._in_flight:
                # The check PREFIX (generation probe / bound-hint read — lake
                # I/O) wedged before the scan body: judge from the episode's
                # OWN arming stamp. ``_last_check_at`` is always set when
                # arming (same lock block in ``maybe_refresh``) — narrowing
                # belt again.
                check_at = self._last_check_at
                baseline = check_at if check_at is not None else self._clock()
                phase = "probe"
            if baseline is not None:
                elapsed = self._clock() - baseline
                bound = self._stale_bound_locked()
                if elapsed > bound:
                    if in_progress:
                        # A wedged scan / first-boot probe — the warming or
                        # rescanning state itself is stalled.
                        stalled = True
                    else:
                        # A wedged refresh probe BEHIND a served verdict —
                        # the distinct honest signal (never "stalled scan").
                        probe_wedged = True
                        probe_wedged_s = elapsed
                    if not self._stall_warned:
                        self._stall_warned = True
                        stall_warn = {
                            "phase": phase,
                            "elapsed_s": elapsed,
                            "bound_s": bound,
                            # Scan-phase stamps ONLY — a probe episode never
                            # reuses the previous scan's started_utc / count.
                            "started_utc": (self._started_utc if phase == "scan" else None),
                            "files_scanned": (self._files_scanned if phase == "scan" else None),
                        }
            base["scan_stalled"] = stalled
            base["refresh_probe_wedged"] = probe_wedged
            base["refresh_probe_wedged_s"] = probe_wedged_s
            base["scan_error"] = self._error
            base["as_of_utc"] = self._as_of_utc
            base["scan_progress"] = {
                "files_scanned": self._files_scanned,
                "files_total_estimate": self._estimate,
                "started_utc": self._started_utc,
            }
        if stall_warn is not None:
            # Logged OUTSIDE the lock (the module's no-I/O-under-lock rule).
            _LOG.warning("lake_census.scan_stalled", **stall_warn)
        return base

    def _stale_bound_locked(self) -> float:
        """The scan-STALLED bound (seconds) — caller holds the lock, O(1).

        The explicit constructor bound when given (test seam); before the
        FIRST landed scan, ``max(sentinel hint, SCAN_STALE_FLOOR_S)`` (the
        hint = the L3 sweep's own recorded duration / per-file derivation —
        a bigger lake's legitimately-slow first scan is not false-flagged);
        after it, ``max(2 × last landed duration, SCAN_STALE_FLOOR_S)`` —
        generous, and self-scaling once a real scan duration is known.

        POISONED-HINT GUARDS (round-1 + round-2 refuters): a sentinel hint
        that is non-finite (``inf``/``-inf``/``nan`` — ``max(inf, floor)``
        yields an INFINITE bound, ``max(nan, floor)`` is order-dependent) OR
        finite-but-ABOVE the physical :data:`FIRST_SCAN_BOUND_CEILING_S`
        (24 h — a corrupt ``duration_ms=1e308`` sentinel passed the
        finite+positive gates and seeded a 1e305 s bound, staleness detection
        effectively disabled) is treated as ABSENT — the bound degrades to
        the bare :data:`SCAN_STALE_FLOOR_S`, and is therefore NEVER wider
        than the ceiling on the first-scan arm. A hint exactly AT the
        ceiling is accepted (boundary law pinned).
        """
        if self._scan_stale_after_s is not None:
            return self._scan_stale_after_s
        if self._scans_completed == 0:
            hint = self._first_bound_hint
            if hint is None or not math.isfinite(hint) or hint > FIRST_SCAN_BOUND_CEILING_S:
                hint = 0.0
            return max(hint, SCAN_STALE_FLOOR_S)
        return max(2.0 * self._compute_duration, SCAN_STALE_FLOOR_S)

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
            # A new in-flight episode re-arms the once-per-episode stall WARN
            # (the probe-phase verdict judges from this arming stamp).
            self._stall_warned = False
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
            with self._lock:
                need_hint = (
                    self._scans_completed == 0
                    and self._first_bound_hint is None
                    and self._first_scan_bound_hint is not None
                )
            if need_hint and self._first_scan_bound_hint is not None:
                # Seed the FIRST-scan stall bound from the L3 sentinel (lake
                # I/O — daemon thread only); a raising / None hint degrades
                # to the bare floor. Read ONCE; inert after the first scan
                # lands a real duration.
                hint: float | None = None
                try:
                    hint = self._first_scan_bound_hint()
                except Exception as exc:  # pylint: disable=broad-except — the hint is best-effort
                    _LOG.warning("lake_census.first_scan_bound_hint_failed", error=str(exc))
                    hint = None
                with self._lock:
                    self._first_bound_hint = hint
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
        """Run ONE census scan and land its outcome — success OR honest error.

        ONE try/finally spans the setup AND the scan (round-1 refuter): a
        raise ANYWHERE after ``_scanning`` flips true (the ``now_utc`` seam,
        an allocation failure inside the lock block, the scan itself, the
        success-landing stamp) is guaranteed to leave ``_scanning`` False on
        exit — a stranded true flag would read ``scan_in_progress`` FOREVER
        while the belt baseline made ``scan_stalled`` unreachable (elapsed
        ≈ 0 against a fresh fallback clock). Every raise lands the SAME
        sanitised error arm (token + class name; base-floor re-arm), and the
        wall stamps are read OUTSIDE the lock blocks so those blocks stay
        pure assignments.
        """
        estimate: int | None = None
        if self._files_total_estimate is not None:
            try:
                estimate = self._files_total_estimate()
            except Exception as exc:  # pylint: disable=broad-except — estimate is best-effort
                _LOG.warning("lake_census.estimate_failed", error=str(exc))
                estimate = None
        started = self._clock()
        try:
            started_utc = self._now_utc()
            with self._lock:
                self._scanning = True
                self._files_scanned = 0
                self._estimate = estimate
                self._started_utc = started_utc
                self._scan_started_clock = started
                self._stall_warned = False  # a new scan opens a new stall episode
            payload = self._scan(self._count_file)
            finished = self._clock()
            as_of_utc = self._now_utc()
            with self._lock:
                self._scanning = False
                self._payload = payload
                self._generation_at = gen
                self._computed_at = finished
                self._compute_duration = max(finished - started, 0.0)
                self._as_of_utc = as_of_utc
                self._error = None
                self._scans_completed += 1
        except Exception as exc:  # pylint: disable=broad-except — an erroring scan must not kill polls
            # The FULL detail (exception text — which may carry absolute lake
            # paths) goes to the server log ONLY; the served ``scan_error`` is
            # a STABLE generic token + the exception CLASS name — never raw
            # exception text into the route/DOM (the `store_error_payload`
            # sanitisation stance; CodeQL py/stack-trace-exposure).
            _LOG.warning("lake_census.scan_failed", error=str(exc), error_type=type(exc).__name__)
            with self._lock:
                # HONEST error state: the last-known verdict (if any) keeps
                # serving with ``scan_error`` stamped; a verdict is NEVER
                # fabricated. The base floor (duration reset to 0) lets the
                # next poll past ~2 s re-arm instead of waiting out a stale
                # 120 s adaptive floor from the previous slow success.
                # (``_scanning`` is reset by the finally belt below.)
                self._error = (
                    f"census scan failed ({type(exc).__name__}) — "
                    "see server log (lake_census.scan_failed)"
                )
                self._computed_at = self._clock()
                self._compute_duration = 0.0
        finally:
            with self._lock:
                if self._scanning:
                    # Reached ONLY on a raise between the setup flip and the
                    # success landing (the success arm already reset it) —
                    # the guarantee that no exit strands ``scan_in_progress``.
                    self._scanning = False

    def _count_file(self) -> None:
        """The per-file progress tick (the scanner's ``per_file_pause`` seam)."""
        with self._lock:
            self._files_scanned += 1


__all__ = [
    "CENSUS_THREAD_NAME",
    "FIRST_SCAN_BOUND_CEILING_S",
    "FIRST_SCAN_PER_FILE_BUDGET_S",
    "SCAN_STALE_FLOOR_S",
    "CensusScan",
    "LakeCensusHolder",
]
