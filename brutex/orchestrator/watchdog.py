"""Retry-storm give-up guard — the per-``(symbol, window)`` dead-man's-switch.

Closes **audit Gap #3** (DEC-RATE-STORM-WATCHDOG-001): a multi-hour
continuous-429 storm across a parallel window fan-out (e.g. 100 symbols ×
15 windows) used to lose bars *silently* — every window kept failing,
nothing gave up, and the operator only found out by hand-diffing the audit
chain after the run ended. This module turns that silent loss into a loud,
bounded, audited verdict.

## What it is (and is NOT)

This is the **policy oracle** for "stop retrying this tuple", NOT a thread,
not a scheduler, and not the retry loop itself. The caller (the ingest
window fan-out) pokes it:

* :meth:`RetryStormWatchdog.record_failure` on every failed attempt — the
  guard times the *continuous-failure streak* per tuple and returns the
  give-up verdict the instant the streak reaches ``max_retry_seconds``.
* :meth:`RetryStormWatchdog.record_success` on every success — resets that
  tuple's streak (a recovered window is not a storm victim).
* :meth:`RetryStormWatchdog.record_terminal_failure` when the bounded retry
  budget is already exhausted (the ingest progress stream reports a window
  ``failed=True`` after all rounds) — an *immediate* give-up, no timing.

Both give-up paths funnel through one internal verdict that:

1. marks the tuple terminally given-up (idempotent — a tuple gives up once),
2. stamps a hash-chained ``RETRY_GIVE_UP`` audit row (when an audit sink is
   wired), and
3. counts the give-up toward its symbol; when a symbol reaches **100%
   window-failure** it stamps one ``SYMBOL_WINDOW_FAILURE`` audit row and
   fires the operator alert **exactly once per symbol — never per window**.

## Design (mirrors :class:`brutex.observability.watchdog.Watchdog`)

* **Pure / no threads / no daemons** — the caller picks the cadence.
* **Caller-supplied clock** — a monotonic-seconds callable (default
  :func:`time.monotonic`); tests inject a fake clock and never sleep.
* **Caller-supplied sinks** — sink-agnostic ``on_give_up`` /
  ``on_symbol_failure`` callbacks and an optional structural
  :class:`AuditSink`. A buggy callback or a failing audit write is caught +
  logged and **never** crashes the run (Golden Rule #10).
* **O(1) per tuple** — every operation (register, record, give-up,
  symbol-failure check) is a dict/set probe: O(1) time, O(1) state *per
  tuple* (total state is O(#tuples), never O(time) — a 6-hour storm adds no
  state, only resets timestamps).

## Why monotonic, not wall-clock

The streak is ``now − first_failure``; a wall-clock backwards jump (NTP /
DST) would shrink an in-progress streak and defer a give-up forever.
:func:`time.monotonic` is guaranteed non-decreasing — same rationale as the
heartbeat watchdog.

## Decision: DEC-RATE-STORM-WATCHDOG-001.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol

from brutex.models import BruteXError

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: Audit action stamped when a single ``(symbol, window)`` tuple gives up.
AUDIT_ACTION_GIVE_UP: Final[str] = "RETRY_GIVE_UP"

#: Audit action stamped when a symbol reaches 100% window-failure.
AUDIT_ACTION_SYMBOL_FAILURE: Final[str] = "SYMBOL_WINDOW_FAILURE"

#: Separator between symbol + window in the ingest progress label
#: (``run_ingest`` builds ``f"{symbol_name} {interval}"``). The progress
#: hook splits on the LAST occurrence so a symbol that itself contains the
#: separator still resolves its trailing window token.
DEFAULT_LABEL_SEP: Final[str] = " "


# ────────────────────────────────────────────────────────────────────────
# Enums + events
# ────────────────────────────────────────────────────────────────────────


class GiveUpReason(StrEnum):
    """Why a tuple gave up — recorded on the event + audit row."""

    #: Continuous-failure streak reached ``max_retry_seconds`` (the headline
    #: time-based give-up; driven by :meth:`RetryStormWatchdog.record_failure`).
    TIMEOUT = "timeout"
    #: The bounded retry budget was already exhausted upstream — an immediate
    #: give-up (driven by :meth:`RetryStormWatchdog.record_terminal_failure`).
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class GiveUpEvent:
    """Emitted once when a ``(symbol, window)`` tuple gives up."""

    symbol: str
    window: str
    reason: GiveUpReason
    failure_seconds: float
    max_retry_seconds: float


@dataclass(frozen=True, slots=True)
class SymbolFailureEvent:
    """Emitted once when a symbol reaches 100% window-failure."""

    symbol: str
    failed_windows: int
    total_windows: int


#: Callback fired on every tuple give-up.
GiveUpCallback = Callable[[GiveUpEvent], None]

#: Callback fired once per symbol when it reaches 100% window-failure.
SymbolFailureCallback = Callable[[SymbolFailureEvent], None]

#: Monotonic-seconds source. Default :func:`time.monotonic`; tests fake it.
Clock = Callable[[], float]


class AuditSink(Protocol):
    """Structural type for the hash-chained audit writer the guard stamps to.

    Matched by :class:`brutex.orchestrator.audit.AuditWriter` (and by any
    test fake exposing the same one-method surface), so the guard never
    depends on the concrete writer class or its import chain.
    """

    def append(self, *, action: str, subject: str, payload: dict[str, Any]) -> Any:
        """Append one hash-chained audit row and return the written entry."""
        ...


def _noop_give_up(_event: GiveUpEvent) -> None:
    """Default ``on_give_up`` — does nothing."""


def _noop_symbol_failure(_event: SymbolFailureEvent) -> None:
    """Default ``on_symbol_failure`` — does nothing."""


# ────────────────────────────────────────────────────────────────────────
# RetryStormWatchdog
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RetryStormWatchdog:
    """Per-``(symbol, window)`` give-up guard for a window fan-out.

    Parameters
    ----------
    max_retry_seconds
        A tuple gives up once its *continuous-failure streak* (the span from
        its first un-recovered failure to the current one) reaches this many
        seconds. Must be ``> 0``.
    on_give_up
        Fired once per tuple give-up (default: no-op).
    on_symbol_failure
        Fired once per symbol when it reaches 100% window-failure
        (default: no-op).
    audit
        Optional :class:`AuditSink`; when wired, every give-up + every
        symbol-failure appends one hash-chained audit row.
    clock
        Monotonic-seconds source (default :func:`time.monotonic`).

    Notes
    -----
    Tuples MUST be :meth:`register`-ed before they are recorded — the symbol
    denominator (its full window count) has to be known before a 100%
    verdict can be trusted, so an unregistered tuple is a caller-contract
    violation (:exc:`KeyError`). The :meth:`progress_hook` wrapper swallows
    that so a stray label can never crash the ingest.
    """

    max_retry_seconds: float
    on_give_up: GiveUpCallback = _noop_give_up
    on_symbol_failure: SymbolFailureCallback = _noop_symbol_failure
    audit: AuditSink | None = None
    clock: Clock = time.monotonic
    _registered: set[tuple[str, str]] = field(default_factory=set, init=False)
    _window_total: dict[str, int] = field(default_factory=dict, init=False)
    _fail_start: dict[tuple[str, str], float] = field(default_factory=dict, init=False)
    _given_up: set[tuple[str, str]] = field(default_factory=set, init=False)
    _given_up_count: dict[str, int] = field(default_factory=dict, init=False)
    _alerted: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if self.max_retry_seconds <= 0:
            raise ValueError(
                f"RetryStormWatchdog.max_retry_seconds must be > 0; got {self.max_retry_seconds!r}"
            )

    # ── Registration ────────────────────────────────────────────────

    def register(self, symbol: str, window: str) -> None:
        """Declare one ``(symbol, window)`` fan-out unit. Idempotent. O(1)."""
        key = (symbol, window)
        if key in self._registered:
            return
        self._registered.add(key)
        self._window_total[symbol] = self._window_total.get(symbol, 0) + 1
        self._given_up_count.setdefault(symbol, 0)

    def register_fanout(self, symbols: Iterable[str], windows: Iterable[str]) -> None:
        """Register the full ``symbols × windows`` product (the fan-out grid)."""
        window_list = list(windows)
        for symbol in symbols:
            for window in window_list:
                self.register(symbol, window)

    def _require_registered(self, key: tuple[str, str]) -> None:
        """Raise ``KeyError`` if *key* was not registered before being recorded."""
        if key not in self._registered:
            raise KeyError(
                f"({key[0]!r}, {key[1]!r}) was not registered before being recorded; "
                f"call register()/register_fanout() for every fan-out unit first."
            )

    # ── Failure / success recording ─────────────────────────────────

    def record_failure(self, symbol: str, window: str) -> bool:
        """Record one failed attempt; return ``True`` iff it triggered a give-up.

        The first failure of a streak only *arms* the timer (returns
        ``False``). A subsequent failure whose streak has reached
        ``max_retry_seconds`` gives up. A tuple that already gave up is a
        no-op (returns ``False``).
        """
        key = (symbol, window)
        self._require_registered(key)
        if key in self._given_up:
            return False
        start = self._fail_start.get(key)
        if start is None:
            self._fail_start[key] = self.clock()
            return False
        elapsed = self.clock() - start
        if elapsed >= self.max_retry_seconds:
            return self._give_up(symbol, window, GiveUpReason.TIMEOUT, elapsed)
        return False

    def record_terminal_failure(self, symbol: str, window: str) -> bool:
        """Record an *already-exhausted* window as an immediate give-up.

        Used by the ingest progress stream: a window reported ``failed=True``
        after the bounded retry budget is a terminal failure, so it gives up
        at once (no streak timing). A tuple that already gave up is a no-op.
        """
        key = (symbol, window)
        self._require_registered(key)
        if key in self._given_up:
            return False
        start = self._fail_start.get(key)
        elapsed = (self.clock() - start) if start is not None else 0.0
        return self._give_up(symbol, window, GiveUpReason.TERMINAL, elapsed)

    def record_success(self, symbol: str, window: str) -> None:
        """Record a success — resets the tuple's continuous-failure streak.

        A tuple that already gave up stays terminally given-up (a late
        success does not un-fail it); this only clears an *in-progress*
        streak so a flaky-then-healthy window never counts toward a storm.
        """
        key = (symbol, window)
        self._require_registered(key)
        self._fail_start.pop(key, None)

    # ── Internal give-up verdict ─────────────────────────────────────

    def _give_up(self, symbol: str, window: str, reason: GiveUpReason, elapsed: float) -> bool:
        """Mark the tuple given-up, stamp audit, maybe fire the symbol alert."""
        key = (symbol, window)
        self._given_up.add(key)
        self._fail_start.pop(key, None)
        self._given_up_count[symbol] += 1

        event = GiveUpEvent(
            symbol=symbol,
            window=window,
            reason=reason,
            failure_seconds=elapsed,
            max_retry_seconds=self.max_retry_seconds,
        )
        self._stamp_audit(
            action=AUDIT_ACTION_GIVE_UP,
            subject=f"{symbol}|{window}",
            payload={
                "reason": reason.value,
                "failure_seconds": elapsed,
                "max_retry_seconds": self.max_retry_seconds,
            },
        )
        self._fire_give_up(event)

        # Each registered window gives up AT MOST once (the `_given_up` guard
        # above), so the per-symbol count climbs by 1 per give-up and equals the
        # window total EXACTLY once — at the final window. ``== total`` therefore
        # fires the 100%-failure alert once per symbol (never per window) without
        # a separate "already alerted" guard that would be an unreachable branch.
        total = self._window_total[symbol]
        if self._given_up_count[symbol] == total:
            self._alerted.add(symbol)
            failure_event = SymbolFailureEvent(
                symbol=symbol,
                failed_windows=self._given_up_count[symbol],
                total_windows=total,
            )
            self._stamp_audit(
                action=AUDIT_ACTION_SYMBOL_FAILURE,
                subject=symbol,
                payload={
                    "failed_windows": failure_event.failed_windows,
                    "total_windows": total,
                },
            )
            self._fire_symbol_failure(failure_event)
        return True

    def _stamp_audit(self, *, action: str, subject: str, payload: dict[str, Any]) -> None:
        """Append one hash-chained audit row, best-effort (never crashes the run)."""
        if self.audit is None:
            return
        try:
            self.audit.append(action=action, subject=subject, payload=payload)
        except (OSError, BruteXError):
            logger.exception(
                "RetryStormWatchdog: audit append failed for action=%s subject=%r; "
                "give-up state still recorded, run continues",
                action,
                subject,
            )

    def _fire_give_up(self, event: GiveUpEvent) -> None:
        """Invoke the ``on_give_up`` callback, best-effort (never crashes the run)."""
        try:
            self.on_give_up(event)
        except (OSError, ValueError, RuntimeError):
            logger.exception(
                "RetryStormWatchdog: on_give_up raised for %s|%s; watchdog continues",
                event.symbol,
                event.window,
            )

    def _fire_symbol_failure(self, event: SymbolFailureEvent) -> None:
        """Invoke the ``on_symbol_failure`` callback, best-effort (never crashes the run)."""
        try:
            self.on_symbol_failure(event)
        except (OSError, ValueError, RuntimeError):
            logger.exception(
                "RetryStormWatchdog: on_symbol_failure raised for %s; watchdog continues",
                event.symbol,
            )

    # ── Queries (O(1)) ───────────────────────────────────────────────

    def has_given_up(self, symbol: str, window: str) -> bool:
        """Return ``True`` iff this tuple has terminally given up. O(1)."""
        return (symbol, window) in self._given_up

    def given_up_count(self, symbol: str) -> int:
        """Return how many of *symbol*'s windows have given up. O(1)."""
        return self._given_up_count.get(symbol, 0)

    def window_total(self, symbol: str) -> int:
        """Return *symbol*'s registered window count (the 100% denominator). O(1)."""
        return self._window_total.get(symbol, 0)

    def symbol_failed(self, symbol: str) -> bool:
        """Return ``True`` iff *symbol* reached 100% window-failure. O(1)."""
        return symbol in self._alerted

    # ── Ingest progress-stream adapter ───────────────────────────────

    def progress_hook(
        self, *, label_sep: str = DEFAULT_LABEL_SEP
    ) -> Callable[[str, int, bool, bool], None]:
        """Return a ``run_ingest`` ``ProgressHook`` ``(label, bars, failed, gap)`` callable.

        The hook splits ``label`` (``"{symbol} {window}"``) on the LAST
        *label_sep*, routes ``failed=True`` to
        :meth:`record_terminal_failure` and ``failed=False`` to
        :meth:`record_success`. A ``gap=True`` tick (a benign vendor ``DH-905``
        no-data boundary — DEC-DHAN-NO-DATA-GAP-COUNT-001) is IGNORED: it is
        neither a failure nor a clean success, so a recurring known gap (e.g.
        NIFTY NEXT 50's pre-2021 daily range, empty on every pull) can never
        trip the retry-storm give-up. A label whose tuple was never registered
        (a stray / malformed label) is ignored — the guard never crashes the
        ingest fan-out (Golden Rule #10).
        """

        def _hook(label: str, _bars: int, failed: bool, gap: bool = False) -> None:
            if gap:
                return
            symbol, window = self._split_label(label, label_sep)
            if (symbol, window) not in self._registered:
                logger.warning(
                    "RetryStormWatchdog.progress_hook: unregistered label %r ignored", label
                )
                return
            if failed:
                self.record_terminal_failure(symbol, window)
            else:
                self.record_success(symbol, window)

        return _hook

    @staticmethod
    def _split_label(label: str, label_sep: str) -> tuple[str, str]:
        """Split ``"{symbol}{sep}{window}"`` on the LAST separator."""
        symbol, sep, window = label.rpartition(label_sep)
        if not sep:
            return label, ""
        return symbol, window


# ────────────────────────────────────────────────────────────────────────
# Factory
# ────────────────────────────────────────────────────────────────────────


def build_retry_storm_watchdog(
    *,
    max_retry_minutes: float,
    audit: AuditSink | None = None,
    on_give_up: GiveUpCallback | None = None,
    on_symbol_failure: SymbolFailureCallback | None = None,
    clock: Clock = time.monotonic,
) -> RetryStormWatchdog:
    """Build a :class:`RetryStormWatchdog` from the ``--max-retry-minutes`` flag.

    Converts the operator-facing *minutes* into the guard's internal seconds
    threshold. ``max_retry_minutes`` must be ``> 0`` (enforced by the
    watchdog's own validation).
    """
    return RetryStormWatchdog(
        max_retry_seconds=max_retry_minutes * 60.0,
        on_give_up=on_give_up if on_give_up is not None else _noop_give_up,
        on_symbol_failure=(
            on_symbol_failure if on_symbol_failure is not None else _noop_symbol_failure
        ),
        audit=audit,
        clock=clock,
    )


__all__ = [
    "AUDIT_ACTION_GIVE_UP",
    "AUDIT_ACTION_SYMBOL_FAILURE",
    "DEFAULT_LABEL_SEP",
    "AuditSink",
    "Clock",
    "GiveUpCallback",
    "GiveUpEvent",
    "GiveUpReason",
    "RetryStormWatchdog",
    "SymbolFailureCallback",
    "SymbolFailureEvent",
    "build_retry_storm_watchdog",
]
