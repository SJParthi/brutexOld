"""Vendor-down error-storm breaker + dead-credential StillStale escalation.

DEC-VENDOR-DOWN-BREAKER-001. ONE COMMON, injectable, pure-state facility shared by
BOTH ingest spines (the spot spine :mod:`brutex.adapters.groww.ingest` and the F&O
spine :mod:`brutex.adapters.groww.option_ingest` — CLAUDE.md §11 COMMON, no
per-spine copy) that watches PER-WINDOW outcomes AFTER a window's bounded retry
ladder is exhausted, and escalates two sustained, success-free failure shapes the
existing per-window retry machinery deliberately never escalates:

1. **Vendor-down error storm.** A TRANSPORT-level failure (a retryable
   429 / 5xx / timeout / network / empty-body error that exhausted its ladder)
   observed across ``>=`` :data:`~brutex.adapters.groww.constants.\
GROWW_STORM_MIN_DISTINCT_SYMBOLS` DISTINCT symbols AND ``>=``
   :data:`~brutex.adapters.groww.constants.GROWW_STORM_MIN_FAILED_WINDOWS`
   DISTINCT all-failed windows with **ZERO successes** since the storm began is
   the whole-vendor-outage shape — every remaining window would fail identically,
   so burning the rest of the pull's fetches (and hours) against a down vendor is
   pure waste. The breaker raises :class:`VendorDownError` (a member of both
   spines' ``_GLOBAL_STOP_ERRORS``) → the pull HALTS gracefully, and a CRITICAL
   "VENDOR DOWN — WAIT" Telegram alert fires (best-effort). A clean
   vendor-confirmed EMPTY 200 is a SUCCESS here (the vendor answered — it is
   alive); ANY success fully resets the storm state, so a mixed
   success/failure pull can NEVER trip, and a single symbol's persistent
   failures can never trip (the distinct-symbol floor — the F&O spine feeds the
   contract's UNDERLYING symbol, so one underlying's whole multi-contract chain
   failing alone can never trip either). Each window is identified by its
   *window_key* and counts toward the storm **at most ONCE per pull**: the spot
   in-run retry passes, the until-clean rounds and a resume pull's re-observation
   of the same sticky failed window never re-count it, so a handful of
   persistently-failing windows can never accumulate a fake storm across
   failure-only retry streams (settled windows skip without observing success —
   the shape that previously false-tripped).
2. **Dead-credential StillStale escalation.** ``>=``
   :data:`~brutex.adapters.groww.constants.GROWW_CREDENTIAL_DEAD_STREAK`
   CONSECUTIVE ladder-exhausted
   :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError` outcomes
   spanning ``>=`` :data:`~brutex.adapters.groww.constants.\
GROWW_CREDENTIAL_DEAD_MIN_MINUTES` minutes with ZERO adopted-token successes is
   past the healthy "the Lambda mints a few minutes late" rotation shape
   (DEC-BROKER-READONLY-REAUTH-REREAD-001's honest envelope: a persistently-stale
   broker previously deferred FOREVER with no operator signal). The breaker
   raises :class:`~brutex.adapters.groww.errors.GrowwCredentialDeadError`
   (a TERMINAL :class:`~brutex.adapters.groww.errors.GrowwAuthError` subclass +
   a global stop) and fires a CRITICAL "OUR CREDENTIAL / LAMBDA BROKER DEAD —
   OPERATOR ACTION NEEDED" alert, textually DISTINCT from the vendor-down one.
   The time gate runs on an INJECTABLE monotonic clock (:attr:`ErrorStormBreaker.now`)
   so the T-minute logic is deterministic in tests — no wall-clock in the logic.

**What this module deliberately does NOT touch** (scope lock): the client's
bounded ``max_attempts`` fast-retry ladder, the AIMD rate ladder
(:mod:`brutex.adapters.rate_common`), the in-pull EMPTY-retry loops, and the
per-window retryability of StillStale/Throttled (they STAY retryable — this is a
SEPARATE streak escalator that merely OBSERVES their exhausted outcomes).

**Recovery model (both trips):** auto-recoverable by design — a trip halts only
THIS pull; every un-settled window stays FAILED/PLANNED in the durable window
ledger, so the next pull re-pulls exactly the missing windows (idempotent
keep-first sink). Nothing is lost, nothing is faked.

**Honest envelope (Golden Rule #10):** the thresholds are operator-tunable
HEURISTICS over response shapes, NOT vendor-documented limits (Groww documents no
outage or credential-death semantics — its published rate table covers only the
Live Data group, ``docs/groww/api-reference/01-introduction.md:130-134``). The
breaker cannot distinguish a vendor outage from a total local network outage (both
halt with the same VENDOR-DOWN verdict — either way, waiting is the right action);
a storm SMALLER than the floors (fewer symbols / distinct windows) is absorbed by
the existing per-window retry + across-pull re-pull, never halted. A resume pull
whose ONLY remaining un-settled work is ``>=`` the floors' worth of whole failing
symbols still trips (zero successes THIS pull — indistinguishable from an outage
without a success signal; the alert NAMES the symbols so the verdict is
inspectable). TIMING: the window floor is crossed within seconds under a
fast-fail outage shape (connection refused / instant 5xx) at the production
ceiling, but a read-timeout-shaped outage burns the full client timeout ladder
per window, so the trip can take tens of minutes there. A trip fires the
breaker's SPECIFIC CRITICAL alert once at the transition; the propagated halt
then ALSO fires the GENERIC pull-halt alert at the dashboard-job / CLI
chokepoint (``pull_jobs.is_pull_halt`` / the CLI ``_pull_halt_errors``) — a
DELIBERATE two-alert envelope (distinct dedupe keys: the specific verdict + the
generic "the pull STOPPED"), both truthful. Every observation is O(1) lock-held
counter/set work; the seen-window dedupe set grows monotonically, bounded by the
pull's window worklist and released with the per-pull breaker instance.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Final

import structlog

from brutex.adapters.empty_circuit_breaker import OFF_ENV_VALUES, resolve_int_env
from brutex.adapters.groww.constants import (
    GROWW_AUTHZ_STORM_MIN_SYMBOLS,
    GROWW_AUTHZ_STORM_MIN_SYMBOLS_ENV,
    GROWW_CREDENTIAL_DEAD_MIN_MINUTES,
    GROWW_CREDENTIAL_DEAD_MINUTES_ENV,
    GROWW_CREDENTIAL_DEAD_STREAK,
    GROWW_CREDENTIAL_DEAD_STREAK_ENV,
    GROWW_ERROR_STORM_ENABLED_ENV,
    GROWW_STORM_MIN_DISTINCT_SYMBOLS,
    GROWW_STORM_MIN_FAILED_WINDOWS,
    GROWW_STORM_MIN_FAILURES_ENV,
    GROWW_STORM_MIN_SYMBOLS_ENV,
)
from brutex.adapters.groww.errors import (
    GrowwAuthorisationStormError,
    GrowwCredentialDeadError,
    GrowwTokenStillStaleError,
)
from brutex.models import BruteXError

__all__ = [
    "ErrorStormBreaker",
    "VendorDownError",
    "observe_authorisation_failure",
    "observe_window_failure",
    "observe_window_success",
    "resolve_error_storm_breaker",
]

_LOG = structlog.get_logger("brutex.adapters.error_storm_breaker")

#: Trip-rule labels (the breaker latches exactly one).
_RULE_VENDOR_DOWN: Final[str] = "vendor_down"
_RULE_CREDENTIAL_DEAD: Final[str] = "credential_dead"
_RULE_AUTHZ_STORM: Final[str] = "authorisation_storm"

#: Cap on the symbols named in the halt message (the counts stay exact).
_MAX_NAMED_SYMBOLS: Final[int] = 10


class VendorDownError(BruteXError):
    """The pull-wide global stop raised when the vendor-down error-storm breaker trips.

    DEC-VENDOR-DOWN-BREAKER-001. Carries the storm's counts so the halt surface can
    NAME them. Added to both ingest runners' ``_GLOBAL_STOP_ERRORS`` (and excluded
    from :func:`~brutex.adapters.groww.fetch_pipeline._is_pool_isolatable`) so it
    propagates past the per-symbol quarantine and halts the whole pull — every
    remaining window would fail identically against a down vendor.

    Also constructible like any plain exception (``VendorDownError("msg")``) with
    the counts defaulting to 0: the pipeline's drift-pin test constructs every
    ``_GLOBAL_STOP_ERRORS`` member generically from a bare message (the same
    contract :class:`~brutex.adapters.empty_circuit_breaker.MassEmptySettleHalt`
    documents). The base ``__init__`` runs exactly ONCE on every path.
    """

    def __init__(
        self,
        *args: object,
        failed_windows: int = 0,
        distinct_symbols: int = 0,
        symbols: tuple[str, ...] = (),
    ) -> None:
        self.failed_windows = failed_windows
        self.distinct_symbols = distinct_symbols
        self.symbols = symbols
        if not args:
            named = ", ".join(symbols[:_MAX_NAMED_SYMBOLS])
            more = len(symbols) - _MAX_NAMED_SYMBOLS
            suffix = f" (+{more} more)" if more > 0 else ""
            args = (
                f"VENDOR DOWN — error storm tripped: {failed_windows} windows exhausted "
                f"their retry ladders across {distinct_symbols} distinct symbols "
                f"({named}{suffix}) with ZERO successes. Groww (or the network path to "
                "it) looks DOWN, so every remaining window would fail identically. The "
                "pull HALTED gracefully — WAIT for the vendor to recover, then re-run: "
                "the durable window ledger + idempotent sink re-pull exactly the "
                "missing windows (nothing lost, nothing faked).",
            )
        # The ONE base-class init call for every construction path.
        super().__init__(*args)


def _emit_vendor_down_alert(halt: VendorDownError) -> None:
    """Fire the best-effort CRITICAL VENDOR-DOWN Telegram alert (never raises).

    Mirrors :func:`~brutex.notifications.sweep_alerts.emit_pull_halt_alert`'s
    blanket-guard shape (DEC-TELEGRAM-PULL-HALT-001): a failure building or
    publishing the alert is swallowed + logged and can NEVER mask the halt the
    breaker raises. Telegram disabled (the default) → a silent no-op. The
    notifications import is lazy so this adapters module stays import-light.
    """
    try:
        from brutex.notifications.sweep_alerts import SweepAlerter

        SweepAlerter().vendor_down(
            failed_windows=halt.failed_windows,
            distinct_symbols=halt.distinct_symbols,
        )
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never masks the halt
        _LOG.exception("error_storm.vendor_down_alert_failed")


def _emit_credential_dead_alert(exc: GrowwCredentialDeadError) -> None:
    """Fire the best-effort CRITICAL CREDENTIAL-DEAD Telegram alert (never raises).

    The dead-credential twin of :func:`_emit_vendor_down_alert` — same
    blanket-guard, best-effort, silent-when-disabled contract; the reason token is
    the class name (short + non-secret, the ``pull_halted`` convention).
    """
    try:
        from brutex.notifications.sweep_alerts import SweepAlerter

        SweepAlerter().credential_dead(reason=type(exc).__name__)
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never masks the halt
        _LOG.exception("error_storm.credential_dead_alert_failed")


def _emit_authorisation_storm_alert(exc: GrowwAuthorisationStormError) -> None:
    """Fire the best-effort CRITICAL AUTHORISATION-STORM Telegram alert (never raises).

    The account-suspension twin of :func:`_emit_vendor_down_alert`
    (DEC-GROWW-AUTHZ-QUARANTINE-001) — same blanket-guard, best-effort,
    silent-when-disabled contract. Textually DISTINCT from BOTH the vendor-down
    alert (the vendor is UP — it is answering with authorisation rejections) and
    the credential-dead alert (the token IS authenticated — the ACCOUNT's
    entitlements are the problem), so the operator action is unambiguous: check
    the Groww account's status / entitlements, not the credential or the network.
    """
    try:
        from brutex.notifications.sweep_alerts import SweepAlerter

        SweepAlerter().authorisation_storm(distinct_symbols=exc.distinct_symbols)
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never masks the halt
        _LOG.exception("error_storm.authorisation_storm_alert_failed")


@dataclass(slots=True)
class ErrorStormBreaker:
    """Per-pull, thread-safe error-storm counter with the two escalation rules.

    ONE instance is threaded into every window worker of a pull (both spines share
    the implementation — CLAUDE.md §11 COMMON); all counter ops run under one lock,
    each O(1). Production constructs a fresh instance per pull via
    :func:`resolve_error_storm_breaker` at the REAL-vendor build sites only — a
    ``--dry-run`` / synthetic / unit-test pull passes ``None`` down the spines
    (never armed), mirroring the ``detect_completeness_gaps`` gating.

    Parameters
    ----------
    min_distinct_symbols
        Distinct-symbol floor for the vendor-down rule (``<= 0`` → any symbol
        count suffices; the floor is what keeps a single-symbol blip from ever
        tripping).
    min_failed_windows
        All-failed-window floor for the vendor-down rule (``<= 0`` disables the
        rule entirely).
    credential_dead_streak
        Consecutive-StillStale floor for the dead-credential rule (``<= 0``
        disables the rule entirely).
    credential_dead_min_seconds
        Minimum span (seconds, on the injected clock) between the streak's first
        and latest StillStale before the dead-credential rule may trip (``0``
        removes the time gate — pure streak).
    enabled
        ``False`` turns every method into a no-op (the operator kill switch).
    now
        Injected monotonic clock (``time.monotonic`` default) — the ONLY time
        source the T-minute logic reads, so tests drive it deterministically.
    alert_vendor_down / alert_credential_dead
        Best-effort alert hooks fired ONCE at the trip transition (outside the
        lock, exception-guarded). Defaults publish the CRITICAL Telegram alerts;
        tests inject recorders.
    """

    min_distinct_symbols: int = GROWW_STORM_MIN_DISTINCT_SYMBOLS
    min_failed_windows: int = GROWW_STORM_MIN_FAILED_WINDOWS
    credential_dead_streak: int = GROWW_CREDENTIAL_DEAD_STREAK
    credential_dead_min_seconds: float = GROWW_CREDENTIAL_DEAD_MIN_MINUTES * 60.0
    #: Distinct-symbol floor for the AUTHORISATION-storm rule
    #: (DEC-GROWW-AUTHZ-QUARANTINE-001): terminal authorisation rejections on this
    #: many DISTINCT symbols with zero transport successes since the storm began
    #: escalate to the fleet-wide :class:`~brutex.adapters.groww.errors.\
    #: GrowwAuthorisationStormError` halt. ``<= 0`` disables the arm (every
    #: authorisation error stays a per-symbol quarantine).
    authz_min_distinct_symbols: int = GROWW_AUTHZ_STORM_MIN_SYMBOLS
    enabled: bool = True
    now: Callable[[], float] = time.monotonic
    alert_vendor_down: Callable[[VendorDownError], None] = _emit_vendor_down_alert
    alert_credential_dead: Callable[[GrowwCredentialDeadError], None] = _emit_credential_dead_alert
    alert_authorisation_storm: Callable[[GrowwAuthorisationStormError], None] = (
        _emit_authorisation_storm_alert
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    #: The latched trip rule (``""`` = not tripped) — once set it never clears.
    _trip_rule: str = field(default="", repr=False)
    #: DISTINCT all-failed windows since the last success (vendor-down evidence).
    _failed_windows: int = field(default=0, repr=False)
    #: Distinct symbols contributing to the current storm (cleared by any success).
    _failed_symbols: set[str] = field(default_factory=set, repr=False)
    #: EVERY window key already counted THIS pull — the per-pull dedupe set. NEVER
    #: cleared (not even by a success): a retry pass / until-clean round / resume
    #: stream re-observing the SAME failed window must never re-count it, so a few
    #: sticky windows cannot accumulate a fake storm across failure-only retry
    #: streams. Grows monotonically, bounded by the pull's window worklist.
    _seen_window_keys: set[Hashable] = field(default_factory=set, repr=False)
    #: Distinct symbols with a TERMINAL authorisation rejection since the last
    #: transport success (DEC-GROWW-AUTHZ-QUARANTINE-001) — the authorisation-storm
    #: arm's evidence set. A set, so re-observing the SAME restricted symbol across
    #: windows / intervals / retry rounds never inflates the distinct count; ANY
    #: transport success clears it (an accepted fetch on another symbol disproves
    #: the account-wide-suspension shape this arm exists to catch).
    _authz_symbols: set[str] = field(default_factory=set, repr=False)
    #: Consecutive ladder-exhausted StillStale outcomes (zero successes between).
    _stale_streak: int = field(default=0, repr=False)
    #: Injected-clock instant of the streak's FIRST StillStale (``None`` = no streak).
    _stale_started: float | None = field(default=None, repr=False)
    #: Streak span (seconds) FROZEN at the instant the time gate passed — the halt
    #: message reads this, never the live clock, so the reported span is exactly
    #: the gated span (and post-trip re-raises never inflate it).
    _stale_elapsed_s: float = field(default=0.0, repr=False)

    @property
    def tripped(self) -> bool:
        """``True`` once either rule fired (latched for the pull) — O(1)."""
        with self._lock:
            return bool(self._trip_rule)

    @property
    def trip_rule(self) -> str:
        """The latched rule name (``"vendor_down"`` / ``"credential_dead"`` / ``""``) — O(1)."""
        with self._lock:
            return self._trip_rule

    def counts(self) -> tuple[int, int, int]:
        """``(failed_windows, distinct_symbols, stale_streak)`` snapshot — O(1), atomic."""
        with self._lock:
            return (self._failed_windows, len(self._failed_symbols), self._stale_streak)

    @property
    def authz_distinct_symbols(self) -> int:
        """Distinct authorisation-rejected symbols in the current storm — O(1), atomic."""
        with self._lock:
            return len(self._authz_symbols)

    def observe_success(self, symbol: str) -> None:
        """Record a SUCCESSFUL transport outcome for *symbol* — resets BOTH streaks, O(1).

        A success = the vendor answered cleanly (bars OR a vendor-confirmed empty
        200 — either way the vendor is ALIVE) after the window's ladder, so any
        in-progress storm was a blip: the vendor-down counters AND the StillStale
        streak (an adopted-token success proves the credential works) both reset.
        The per-pull ``_seen_window_keys`` dedupe set is deliberately KEPT — an
        already-counted window that keeps failing after the reset is old evidence,
        never a fresh storm contribution. A latched trip is NEVER un-latched (the
        halt already propagated; a stray late success from an in-flight sibling
        worker must not mask it). *symbol* is unused by the reset itself (both
        resets are pull-wide by design) and kept for seam-signature symmetry with
        :meth:`observe_transport_failure`.
        """
        if not self.enabled:
            return
        with self._lock:
            if self._trip_rule:
                return
            self._failed_windows = 0
            self._failed_symbols.clear()
            self._stale_streak = 0
            self._stale_started = None
            # A transport success ALSO resets the authorisation-storm evidence set
            # (DEC-GROWW-AUTHZ-QUARANTINE-001): an accepted fetch proves the account
            # is NOT fleet-suspended, so any accumulated per-symbol authorisation
            # rejections are individual entitlement restrictions — quarantines, never
            # a storm. A mixed success/authz pull can therefore NEVER trip this arm.
            self._authz_symbols.clear()

    def observe_transport_failure(
        self, symbol: str, *, error_type: str, window_key: Hashable, still_stale: bool = False
    ) -> None:
        """Record a LADDER-EXHAUSTED transport failure; RAISE on/after a trip — O(1).

        *window_key* is the window's stable identity (the spines pass the ledger
        :class:`~brutex.data.fetch_coverage.WindowKey`): a window counts toward
        the vendor-down storm at most ONCE per pull, however many retry passes /
        until-clean rounds re-observe its failure — so a few sticky windows can
        never accumulate a fake storm across failure-only retry streams, while a
        genuine outage still crosses the floor on FRESH windows within its first
        pass. *still_stale* routes the observation: a
        :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError` outcome
        extends the dead-credential streak (it is OUR credential, not vendor
        evidence — it neither counts toward nor resets the vendor-down storm, and
        it never marks the window seen), while any other transport failure counts
        toward the vendor-down storm AND breaks the strict StillStale
        consecutiveness. The moment either rule crosses, the matching global-stop
        error is built under the lock, the CRITICAL alert hook fires ONCE
        (outside the lock, exception-guarded), and the error is raised OUTSIDE
        the lock. An already-tripped breaker keeps raising its latched rule's
        error on EVERY further failure observation so every in-flight worker
        stops promptly (the raise is the mechanism — the spines cannot forget
        the halt).
        """
        if not self.enabled:
            return
        vendor_halt: VendorDownError | None = None
        cred_halt: GrowwCredentialDeadError | None = None
        authz_halt: GrowwAuthorisationStormError | None = None
        fresh_trip = False
        with self._lock:
            if not self._trip_rule:
                if still_stale:
                    if self._stale_streak == 0:
                        self._stale_started = self.now()
                    self._stale_streak += 1
                    if self._credential_dead_locked():
                        self._trip_rule = _RULE_CREDENTIAL_DEAD
                        fresh_trip = True
                else:
                    # A non-StillStale transport failure breaks the STRICT
                    # StillStale consecutiveness (rule 2 wants an uninterrupted
                    # auth-stall signature, never a mixed outage) — even when the
                    # window itself was already counted (the break is CURRENT
                    # evidence; the count dedupe is not).
                    self._stale_streak = 0
                    self._stale_started = None
                    if window_key not in self._seen_window_keys:
                        # Count each DISTINCT window once per pull: symbol + window
                        # move together, so the storm's symbol set stays exactly
                        # the symbols of its counted windows (message honesty).
                        self._seen_window_keys.add(window_key)
                        self._failed_windows += 1
                        self._failed_symbols.add(symbol)
                        if self._vendor_down_locked():
                            self._trip_rule = _RULE_VENDOR_DOWN
                            fresh_trip = True
            if self._trip_rule == _RULE_VENDOR_DOWN:
                vendor_halt = VendorDownError(
                    failed_windows=self._failed_windows,
                    distinct_symbols=len(self._failed_symbols),
                    symbols=tuple(sorted(self._failed_symbols)),
                )
            elif self._trip_rule == _RULE_CREDENTIAL_DEAD:
                cred_halt = self._credential_dead_error_locked()
            elif self._trip_rule == _RULE_AUTHZ_STORM:
                # An ALREADY-latched authorisation storm (tripped via
                # observe_authz_failure) keeps raising on every further transport
                # observation too — every in-flight worker must stop promptly.
                authz_halt = self._authz_storm_error_locked()
        if vendor_halt is not None:
            if fresh_trip:
                self._log_trip(_RULE_VENDOR_DOWN, error_type=error_type, detail=str(vendor_halt))
                self._fire_alert(_RULE_VENDOR_DOWN, self.alert_vendor_down, vendor_halt)
            raise vendor_halt
        if cred_halt is not None:
            if fresh_trip:
                self._log_trip(_RULE_CREDENTIAL_DEAD, error_type=error_type, detail=str(cred_halt))
                self._fire_alert(_RULE_CREDENTIAL_DEAD, self.alert_credential_dead, cred_halt)
            raise cred_halt
        if authz_halt is not None:
            raise authz_halt

    def observe_authz_failure(self, symbol: str, *, error_type: str) -> None:
        """Record a TERMINAL vendor AUTHORISATION rejection for *symbol*; RAISE on/after a trip.

        DEC-GROWW-AUTHZ-QUARANTINE-001 — the authorisation-storm arm. Each observed
        symbol joins the distinct-symbol evidence set (a set — re-observing the SAME
        restricted symbol across windows / intervals / retry rounds never inflates
        the count, so the arm counts SYMBOLS, not windows); when the set reaches
        :attr:`authz_min_distinct_symbols` with ZERO transport successes since the
        storm began (any success clears the set in :meth:`observe_success`), the
        fleet-wide :class:`~brutex.adapters.groww.errors.GrowwAuthorisationStormError`
        is built under the lock, the CRITICAL alert hook fires ONCE at the trip
        transition (outside the lock, exception-guarded), and the error is raised
        OUTSIDE the lock — restoring the old pull-wide HALT semantics for exactly
        the account-suspension shape. DELIBERATELY ORTHOGONAL to the other two arms:
        an authorisation rejection is neither vendor-transport evidence nor a
        StillStale token stall, so it never counts toward — nor resets — the
        vendor-down storm or the credential-dead streak (those arms are
        byte-unchanged). An already-tripped breaker (ANY rule) keeps raising its
        latched rule's error on every further observation, so in-flight workers
        stop promptly. O(1) lock-held set work.
        """
        if not self.enabled:
            return
        vendor_halt: VendorDownError | None = None
        cred_halt: GrowwCredentialDeadError | None = None
        authz_halt: GrowwAuthorisationStormError | None = None
        fresh_trip = False
        with self._lock:
            if not self._trip_rule:
                self._authz_symbols.add(symbol)
                if self._authz_storm_locked():
                    self._trip_rule = _RULE_AUTHZ_STORM
                    fresh_trip = True
            if self._trip_rule == _RULE_AUTHZ_STORM:
                authz_halt = self._authz_storm_error_locked()
            elif self._trip_rule == _RULE_VENDOR_DOWN:
                vendor_halt = VendorDownError(
                    failed_windows=self._failed_windows,
                    distinct_symbols=len(self._failed_symbols),
                    symbols=tuple(sorted(self._failed_symbols)),
                )
            elif self._trip_rule == _RULE_CREDENTIAL_DEAD:
                cred_halt = self._credential_dead_error_locked()
        if authz_halt is not None:
            if fresh_trip:
                self._log_trip(_RULE_AUTHZ_STORM, error_type=error_type, detail=str(authz_halt))
                self._fire_alert(_RULE_AUTHZ_STORM, self.alert_authorisation_storm, authz_halt)
            raise authz_halt
        if vendor_halt is not None:
            raise vendor_halt
        if cred_halt is not None:
            raise cred_halt

    def _authz_storm_locked(self) -> bool:
        """Evaluate the authorisation-storm rule under the lock — O(1).

        ``authz_min_distinct_symbols <= 0`` disables the arm entirely (every
        authorisation rejection stays a per-symbol quarantine).
        """
        return 0 < self.authz_min_distinct_symbols <= len(self._authz_symbols)

    def _authz_storm_error_locked(self) -> GrowwAuthorisationStormError:
        """Build the escalated authorisation-storm error from the locked set — O(symbols)."""
        return GrowwAuthorisationStormError(
            distinct_symbols=len(self._authz_symbols),
            symbols=tuple(sorted(self._authz_symbols)),
        )

    def _vendor_down_locked(self) -> bool:
        """Evaluate the vendor-down rule under the lock — O(1).

        ``min_failed_windows <= 0`` disables the rule; ``min_distinct_symbols <= 0``
        clamps to 1 (any symbol count).
        """
        return 0 < self.min_failed_windows <= self._failed_windows and len(
            self._failed_symbols
        ) >= max(1, self.min_distinct_symbols)

    def _credential_dead_locked(self) -> bool:
        """Evaluate the dead-credential rule under the lock — O(1).

        ``credential_dead_streak <= 0`` disables the rule; the time gate reads the
        INJECTED clock ONCE (deterministic in tests) and — when the gate passes —
        FREEZES the gated span into ``_stale_elapsed_s`` so the halt message
        reports exactly the span that satisfied the gate (never a later clock
        read); a negative configured span clamps to 0 (pure streak).
        """
        if not 0 < self.credential_dead_streak <= self._stale_streak:
            return False
        started = self._stale_started
        if started is None:  # pragma: no cover — a positive streak always stamps its start
            return False
        elapsed_s = self.now() - started
        if elapsed_s >= max(0.0, self.credential_dead_min_seconds):
            self._stale_elapsed_s = max(0.0, elapsed_s)
            return True
        return False

    def _credential_dead_error_locked(self) -> GrowwCredentialDeadError:
        """Build the escalated credential-dead error from the locked streak state — O(1).

        Reads the span FROZEN at the trip's gate check (``_stale_elapsed_s``) —
        never the live clock — so every post-trip re-raise reports the SAME span.
        """
        return GrowwCredentialDeadError(
            "OUR CREDENTIAL / token-broker Lambda looks DEAD — OPERATOR ACTION NEEDED: "
            f"{self._stale_streak} consecutive StillStale re-reads over "
            f"{self._stale_elapsed_s / 60.0:.1f} min with ZERO adopted-token successes. The re-read "
            "keeps returning the same dead token, so every remaining window would fail "
            "identically. The pull HALTED gracefully — re-provision the API key / TOTP "
            "secret or fix the token-broker Lambda, then re-run (the durable ledger + "
            "idempotent sink re-pull exactly the missing windows)."
        )

    @staticmethod
    def _log_trip(rule: str, *, error_type: str, detail: str) -> None:
        """Log the trip CRITICALLY at the transition — the loud, test-pinned event. O(1)."""
        _LOG.critical("error_storm.tripped", rule=rule, error_type=error_type, detail=detail)

    @staticmethod
    def _fire_alert[HaltT](rule: str, hook: Callable[[HaltT], None], halt: HaltT) -> None:
        """Fire the trip's alert hook ONCE — best-effort, exception-guarded, O(1).

        Runs OUTSIDE the lock at the trip TRANSITION only (re-raises on an
        already-tripped breaker never re-alert; the Telegram dedupe key coalesces
        any cross-process repeat). A broken notifier can never mask the halt.
        """
        try:
            hook(halt)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never masks the halt
            _LOG.exception("error_storm.trip_alert_failed", rule=rule)


def observe_window_success(breaker: ErrorStormBreaker | None, *, symbol: str) -> None:
    """The COMMON success-observation seam BOTH spines call — None-safe, best-effort, O(1).

    ``None`` (a dry-run / synthetic / unit-test pull — the arming gate) observes
    nothing. Any internal breaker bug is swallowed + logged so it can NEVER crash a
    healthy pull (a success observation has no trip to propagate).
    """
    if breaker is None:
        return
    try:
        breaker.observe_success(symbol)
    except Exception:  # pylint: disable=broad-exception-caught — a breaker bug never crashes a pull
        _LOG.exception("error_storm.observe_success_failed", symbol=symbol)


def observe_window_failure(
    breaker: ErrorStormBreaker | None, *, symbol: str, window_key: Hashable, exc: BaseException
) -> None:
    """The COMMON failure-observation seam BOTH spines call — None-safe, trip-propagating.

    Classifies *exc* (a ladder-exhausted retryable transport error) into the
    StillStale streak vs the vendor-down storm and feeds the breaker. *symbol* is
    the storm's distinct-symbol unit (the spot spine passes the instrument's
    ``symbol_name``; the F&O spine passes the contract's **underlying** symbol so
    one underlying's whole chain reads as ONE symbol); *window_key* is the
    window's stable identity (the ledger :class:`~brutex.data.fetch_coverage.\
WindowKey`) — each window counts at most ONCE per pull. A TRIP
    (:class:`VendorDownError` / :class:`~brutex.adapters.groww.errors.\
GrowwCredentialDeadError`) PROPAGATES — it is the global stop, the whole point —
    while any other internal breaker bug is swallowed + logged so a defect here can
    never crash a healthy pull. ``None`` breaker (dry-run / synthetic) is a no-op.
    """
    if breaker is None:
        return
    try:
        breaker.observe_transport_failure(
            symbol,
            error_type=type(exc).__name__,
            window_key=window_key,
            still_stale=isinstance(exc, GrowwTokenStillStaleError),
        )
    except (VendorDownError, GrowwCredentialDeadError, GrowwAuthorisationStormError):
        raise  # the TRIP is the mechanism — it must reach _GLOBAL_STOP_ERRORS
    except Exception:  # pylint: disable=broad-exception-caught — a breaker bug never crashes a pull
        _LOG.exception("error_storm.observe_failure_failed", symbol=symbol)


def observe_authorisation_failure(
    breaker: ErrorStormBreaker | None, *, symbol: str, window_key: Hashable, exc: BaseException
) -> None:
    """The COMMON authorisation-observation seam BOTH spines call — None-safe, trip-propagating.

    DEC-GROWW-AUTHZ-QUARANTINE-001. Called on the TERMINAL authorisation path of
    each spine (the branch that previously never observed anything — only the
    retryable branch fed the breaker) just before the per-symbol quarantine raise:
    feeds *symbol* to the breaker's authorisation-storm arm. *symbol* is the
    distinct-symbol unit (the spot spine passes the instrument's ``symbol_name``;
    the F&O spine passes the contract's **underlying** symbol, mirroring
    :func:`observe_window_failure`); *window_key* is carried for log evidence only
    (the arm counts distinct SYMBOLS — a set already dedupes re-observations). A
    TRIP (:class:`~brutex.adapters.groww.errors.GrowwAuthorisationStormError`, or
    another rule's latched halt) PROPAGATES — it is the global stop, the whole
    point — while any other internal breaker bug is swallowed + logged so a defect
    here can never crash a pull (the quarantine raise still follows at the call
    site). ``None`` breaker (dry-run / synthetic) is a no-op.
    """
    if breaker is None:
        return
    try:
        breaker.observe_authz_failure(symbol, error_type=type(exc).__name__)
    except (VendorDownError, GrowwCredentialDeadError, GrowwAuthorisationStormError):
        raise  # the TRIP is the mechanism — it must reach _GLOBAL_STOP_ERRORS
    except Exception:  # pylint: disable=broad-exception-caught — a breaker bug never crashes a pull
        _LOG.exception("error_storm.observe_authz_failed", symbol=symbol, window_key=window_key)


def _resolve_minutes(env_name: str, default: float) -> float:
    """Read a non-negative float-minutes env override; invalid/negative falls back — O(1)."""
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0.0:
        return default
    return value


def resolve_error_storm_breaker(*, now: Callable[[], float] | None = None) -> ErrorStormBreaker:
    """Build a FRESH per-pull breaker from env config (or the documented defaults).

    The ONE construction seam every REAL-vendor production pull site calls
    (CLI ``cmd_fetch``, dashboard ``pull_all_spot`` / ``pull_candles`` /
    ``historical_pull._run_phase``) — a new instance per pull, so storm state can
    never leak across pulls (Golden Rule #7). A dry-run / synthetic pull does NOT
    call this (it threads ``None`` — the breaker is never armed there, mirroring
    ``detect_completeness_gaps``). Every knob is env-overridable (DYNAMIC); a bad
    value fails safe to its default — only the explicit
    :data:`~brutex.adapters.groww.constants.GROWW_ERROR_STORM_ENABLED_ENV` off
    switch can disable the protection. *now* injects the monotonic clock (tests);
    ``None`` → :func:`time.monotonic`. O(1).
    """
    raw_enabled = os.environ.get(GROWW_ERROR_STORM_ENABLED_ENV)
    enabled = raw_enabled is None or raw_enabled.strip().lower() not in OFF_ENV_VALUES
    minutes = _resolve_minutes(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, GROWW_CREDENTIAL_DEAD_MIN_MINUTES)
    return ErrorStormBreaker(
        min_distinct_symbols=resolve_int_env(
            GROWW_STORM_MIN_SYMBOLS_ENV, GROWW_STORM_MIN_DISTINCT_SYMBOLS
        ),
        min_failed_windows=resolve_int_env(
            GROWW_STORM_MIN_FAILURES_ENV, GROWW_STORM_MIN_FAILED_WINDOWS
        ),
        credential_dead_streak=resolve_int_env(
            GROWW_CREDENTIAL_DEAD_STREAK_ENV, GROWW_CREDENTIAL_DEAD_STREAK
        ),
        credential_dead_min_seconds=minutes * 60.0,
        authz_min_distinct_symbols=resolve_int_env(
            GROWW_AUTHZ_STORM_MIN_SYMBOLS_ENV, GROWW_AUTHZ_STORM_MIN_SYMBOLS
        ),
        enabled=enabled,
        now=now if now is not None else time.monotonic,
    )
