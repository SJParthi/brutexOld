"""Combined-failure correlation for the live pull (DEC-FAILURE-CORRELATION-001).

The Groww pull can fail in several distinct ways at once. The existing rails each
see ONE failure in isolation:

* :mod:`brutex.adapters.groww.errors` classifies a single SDK exception into a
  taxonomy — rate-limit / timeout / server-5xx / empty-body — but has no view of
  what ELSE is failing at the same moment.
* :class:`brutex.data.errors.LakeWriteHalt` raises on a disk-full (``ENOSPC``) /
  OOM at the lake-write boundary, and the quota tracker stops on a daily-quota
  breach — again, each in isolation.
* :class:`brutex.observability.pull_health.PullHealthMonitor` watches the rate +
  throughput gauges, and :class:`brutex.notifications.sweep_alerts.SweepAlerter`
  routes ``rate_breach`` / ``pull_degraded`` / ``pull_halted`` to Telegram.

When several of these fire in a short window — a disk-fill ``ENOSPC`` DURING a
vendor-429 rate-storm, or an OOM DURING a token reauth — the operator gets N
uncorrelated alerts/log lines and has to reconstruct the causal chain by hand.
The most common trap is mis-attributing the ROOT: a full disk makes the lake
write block, the fetch workers back up, the in-flight calls pile, and the vendor
starts 429-ing the retries — so the operator sees a "rate storm" and chases the
vendor when the REAL root is the full disk.

This module is the correlation layer that closes that failure-matrix gap. Given
the recent failure signals (the SAME taxonomy the monitor / alerter already
produce), it detects co-occurrence within a tunable window and emits ONE
:class:`CorrelatedVerdict` that names the likely ROOT with a priority order and
labels every other co-occurring failure a downstream SYMPTOM.

## Priority order (why disk/OOM/quota beats vendor-429 beats timeout)

A host-resource failure (disk full / OOM) or a quota stop is a HARD, pull-wide
condition that CAUSES downstream symptoms: a blocked lake write backs up the
fetch pipeline, which makes calls pile up and the vendor 429-throttle the
retries. So when both co-occur, the disk/OOM/quota is the root and the 429s are
the symptom — never the other way round. A vendor-429 storm, in turn, naturally
produces timeouts (a throttled connection eventually times out), so a 429
out-ranks a bare timeout. The default order is therefore::

    disk_full / out_of_memory / quota  (host/account — root)
        >  rate_limit                  (vendor throttle)
        >  server_error                (vendor 5xx)
        >  timeout                     (network)
        >  empty_response              (transient body blip)
        >  auth                        (token — usually self-heals via reauth)

The order is INJECTABLE (a ``priority`` mapping) so an operator can re-tune it
without touching this module (CLAUDE.md Golden Rule #11 DYNAMIC).

## Honest complexity (Golden Rule #10 — NOT a false O(1) claim)

:meth:`FailureCorrelator.observe` is **O(k)** in the number of signals currently
inside the recent window, NOT O(1): it appends the new signal (amortised O(1) on
a :class:`collections.deque`), evicts everything older than the window from the
left (amortised O(1) total), then scans the surviving in-window signals once to
pick the highest-priority root and bucket the rest as symptoms — that scan is
O(k). ``k`` is BOUNDED: by the eviction it is at most the number of failures the
pull produces within ``window_seconds`` (a small constant in practice — a handful
of distinct failure kinds), and an explicit ``max_window_signals`` cap bounds it
hard even under a pathological burst. So it is O(k) per event over a bounded
recent-window — a small, constant-bounded cost on the failure path (which is rare
relative to the success path), never on the hot per-bar path.

## Never raises into the pull (Golden Rule #10)

:meth:`FailureCorrelator.observe` wraps its whole body so a malformed signal or a
buggy ``on_correlated`` callback can NEVER block, slow, or crash the pull. On any
internal error it logs and returns ``None`` (no verdict) — the failure that
triggered the observe still propagates through the normal path unchanged; the
correlation layer is additive PROOF, never a gate.

## Single-failure pass-through

A lone failure with nothing co-occurring is NOT a correlation: :meth:`observe`
still returns a verdict (so the caller has a uniform return), but its
``symptoms`` tuple is empty and :attr:`CorrelatedVerdict.is_correlated` is
``False``. The wired alert path fires the COMBINED alert only when
``is_correlated`` is ``True``, so a single failure rides its existing per-failure
alert unchanged — no double-alerting, no behaviour change for the common case.

## Decision: DEC-FAILURE-CORRELATION-001 (MIGRATION.md entry #937).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Failure taxonomy
# ────────────────────────────────────────────────────────────────────────


class FailureKind(StrEnum):
    """The distinct pull-failure kinds the correlator reasons about.

    Mirrors the taxonomy the existing rails already produce
    (:mod:`brutex.adapters.groww.errors` + :class:`brutex.data.errors.LakeWriteHalt`
    + the quota stop), kept as a small neutral enum so this module never imports
    the adapter / data-lake classes (Golden Rule #11 COMMON — one neutral vocab).
    """

    DISK_FULL = "disk_full"
    OUT_OF_MEMORY = "out_of_memory"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    EMPTY_RESPONSE = "empty_response"
    AUTH = "auth"


#: The default root-attribution priority (HIGHER number = more likely the ROOT).
#: Host-resource + account stops (disk/OOM/quota) out-rank the vendor throttle,
#: which out-ranks the vendor 5xx, which out-ranks a network timeout, which
#: out-ranks a transient empty body; an auth blip ranks lowest (it self-heals via
#: the mid-pull reauth, DEC-GROWW-TOKEN-REAUTH-001). See the module docstring for
#: the causal justification. Injectable via :class:`FailureCorrelator`.
DEFAULT_PRIORITY: Final[Mapping[FailureKind, int]] = {
    FailureKind.DISK_FULL: 100,
    FailureKind.OUT_OF_MEMORY: 100,
    FailureKind.QUOTA: 90,
    FailureKind.RATE_LIMIT: 70,
    FailureKind.SERVER_ERROR: 50,
    FailureKind.TIMEOUT: 40,
    FailureKind.EMPTY_RESPONSE: 30,
    FailureKind.AUTH: 20,
}

#: Default co-occurrence window (seconds). Two failures within this span of each
#: other are treated as co-occurring and correlated into one verdict.
DEFAULT_WINDOW_SECONDS: Final[float] = 30.0

#: Hard cap on the recent-window size, bounding ``k`` even under a pathological
#: burst that produces more failures than time-eviction alone would trim.
DEFAULT_MAX_WINDOW_SIGNALS: Final[int] = 256


# ────────────────────────────────────────────────────────────────────────
# Signals + verdict
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FailureSignal:
    """One observed pull failure, timestamped on a monotonic clock.

    Parameters
    ----------
    kind
        The :class:`FailureKind` of this failure.
    at
        The monotonic timestamp (seconds) when the failure was observed — taken
        from the correlator's injected clock so eviction + co-occurrence use the
        SAME time source (never wall-clock, which can jump).
    detail
        A short, non-secret human description (surfaced in the verdict message).
    """

    kind: FailureKind
    at: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CorrelatedVerdict:
    """The single correlated outcome of an :meth:`FailureCorrelator.observe` call.

    Parameters
    ----------
    root
        The highest-priority :class:`FailureKind` in the current window — the
        likely ROOT cause.
    root_detail
        The ``detail`` of the most-recent signal of the ``root`` kind.
    symptoms
        Every OTHER distinct failure kind co-occurring in the window, ordered
        highest-priority first — the downstream SYMPTOMS. Empty for a lone
        failure (see :attr:`is_correlated`).
    window_seconds
        The co-occurrence window used (echoed for the message / audit).
    """

    root: FailureKind
    root_detail: str
    symptoms: tuple[FailureKind, ...]
    window_seconds: float

    @property
    def is_correlated(self) -> bool:
        """``True`` iff at least one distinct symptom co-occurred with the root.

        A lone failure (no other kind in the window) is NOT a correlation — the
        caller should let it ride its existing per-failure alert unchanged.
        """
        return len(self.symptoms) > 0

    def summary(self) -> str:
        """Build the human-readable root-and-symptoms message.

        For a correlated verdict this names the root and lists the downstream
        symptoms (e.g. ``"disk_full is the root; downstream symptoms: rate_limit,
        timeout"``). For a lone failure it states the single failure plainly.
        """
        if not self.is_correlated:
            base = f"single failure: {self.root.value}"
            return f"{base} ({self.root_detail})" if self.root_detail else base
        symptom_list = ", ".join(kind.value for kind in self.symptoms)
        base = (
            f"{self.root.value} is the root; downstream symptoms: {symptom_list} "
            f"(within {self.window_seconds:.0f}s)"
        )
        return f"{base} — {self.root_detail}" if self.root_detail else base


#: Best-effort callback fired once per CORRELATED verdict (never for a lone
#: failure). Production wires a :class:`brutex.notifications.sweep_alerts.\
#: SweepAlerter`-backed emitter; tests pass a recorder.
CorrelatedCallback = Callable[[CorrelatedVerdict], None]


# ────────────────────────────────────────────────────────────────────────
# Correlator
# ────────────────────────────────────────────────────────────────────────


class FailureCorrelator:
    """Correlate co-occurring pull failures into one root-attributed verdict.

    Built ONCE per pull and fed each failure via :meth:`observe`. Holds a bounded
    monotonic-time recent-window of :class:`FailureSignal`; on each event it
    evicts stale signals, picks the highest-priority kind as the root, buckets the
    rest as symptoms, and (when more than one distinct kind co-occurs) fires the
    best-effort ``on_correlated`` callback with the single combined verdict.

    Parameters
    ----------
    on_correlated
        Best-effort callback fired ONCE per correlated verdict (deduped on the
        root+symptoms shape so a sustained co-occurrence does not spam). Never
        fired for a lone failure.
    window_seconds
        Co-occurrence window (seconds); injectable (DYNAMIC). Must be ``> 0``.
    priority
        :class:`FailureKind` → integer priority (higher = more likely root);
        injectable (DYNAMIC), defaults to :data:`DEFAULT_PRIORITY`. Must cover
        every :class:`FailureKind`.
    max_window_signals
        Hard cap on the retained recent-window size (bounds ``k``). Must be
        ``>= 1``.
    clock
        Monotonic seconds source; defaults to :func:`time.monotonic`. Tests inject
        a fake to avoid sleeping.
    """

    __slots__ = (
        "_clock",
        "_last_shape",
        "_max_signals",
        "_on_correlated",
        "_priority",
        "_signals",
        "_window",
    )

    def __init__(
        self,
        *,
        on_correlated: CorrelatedCallback,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        priority: Mapping[FailureKind, int] | None = None,
        max_window_signals: int = DEFAULT_MAX_WINDOW_SIGNALS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_seconds <= 0.0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds}")
        if max_window_signals < 1:
            raise ValueError(f"max_window_signals must be >= 1, got {max_window_signals}")
        resolved = dict(DEFAULT_PRIORITY if priority is None else priority)
        missing = [kind for kind in FailureKind if kind not in resolved]
        if missing:
            raise ValueError(f"priority is missing kinds: {[k.value for k in missing]}")
        self._on_correlated = on_correlated
        self._window = window_seconds
        self._priority = resolved
        self._max_signals = max_window_signals
        self._clock = clock
        self._signals: deque[FailureSignal] = deque()
        self._last_shape: tuple[FailureKind, tuple[FailureKind, ...]] | None = None

    # ── The observe hook ─────────────────────────────────────────────

    def observe(self, kind: FailureKind, *, detail: str = "") -> CorrelatedVerdict | None:
        """Ingest one failure; return the current correlated verdict (or ``None``).

        Best-effort — the whole body is guarded so a malformed input or a buggy
        ``on_correlated`` callback can NEVER block, slow, or crash the pull
        (Golden Rule #10). On any internal error it logs and returns ``None``; the
        failure that triggered the observe still propagates through the normal
        path unchanged.

        Returns the :class:`CorrelatedVerdict` for the current window (its
        :attr:`~CorrelatedVerdict.is_correlated` tells the caller whether to use
        the combined alert), or ``None`` only when the body raised internally.

        Complexity: O(k) in the in-window signal count (see the module docstring);
        ``k`` is bounded by time-eviction + ``max_window_signals``.
        """
        try:
            now = self._clock()
            self._signals.append(FailureSignal(kind=kind, at=now, detail=detail))
            self._evict(now)
            verdict = self._build_verdict()
            if verdict.is_correlated:
                self._maybe_fire(verdict)
            else:
                # A lone failure clears any prior correlated shape so the NEXT
                # genuine co-occurrence is not deduped away.
                self._last_shape = None
            return verdict
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
            logger.exception("failure_correlation.observe failed; the pull is unaffected")
            return None

    # ── Internals ────────────────────────────────────────────────────

    def _evict(self, now: float) -> None:
        """Drop signals older than the window, then enforce the hard size cap.

        Time-eviction from the left is amortised O(1) total; the size cap trims
        the oldest extras so ``k`` stays bounded even under a pathological burst.
        """
        cutoff = now - self._window
        while self._signals and self._signals[0].at < cutoff:
            self._signals.popleft()
        while len(self._signals) > self._max_signals:
            self._signals.popleft()

    def _build_verdict(self) -> CorrelatedVerdict:
        """Pick the highest-priority root + bucket the rest as ordered symptoms.

        One O(k) scan over the in-window signals. The root is the kind with the
        greatest priority (ties broken by the most-recent signal so the freshest
        equal-priority cause wins); symptoms are every OTHER distinct kind present,
        sorted highest-priority first for a stable, readable message.
        """
        # latest signal of each kind (preserves the most-recent detail per kind)
        latest: dict[FailureKind, FailureSignal] = {}
        for signal in self._signals:
            latest[signal.kind] = signal
        # root = highest priority; tie → most recent.
        root_signal = max(
            latest.values(),
            key=lambda s: (self._priority[s.kind], s.at),
        )
        root = root_signal.kind
        symptoms = tuple(
            sorted(
                (kind for kind in latest if kind != root),
                key=lambda kind: self._priority[kind],
                reverse=True,
            )
        )
        return CorrelatedVerdict(
            root=root,
            root_detail=root_signal.detail,
            symptoms=symptoms,
            window_seconds=self._window,
        )

    def _maybe_fire(self, verdict: CorrelatedVerdict) -> None:
        """Fire the callback once per distinct correlated shape (dedupe).

        The "shape" is ``(root, symptoms)`` — a sustained co-occurrence keeps the
        same shape and is fired only once; a NEW kind joining the window changes
        the shape and re-fires. A callback that raises is swallowed + logged so it
        can never crash the pull.
        """
        shape = (verdict.root, verdict.symptoms)
        if shape == self._last_shape:
            return
        self._last_shape = shape
        logger.warning(
            "failure_correlation.correlated root=%s symptoms=%s detail=%s",
            verdict.root.value,
            [kind.value for kind in verdict.symptoms],
            verdict.root_detail,
        )
        try:
            self._on_correlated(verdict)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort alert
            logger.exception(
                "failure_correlation: on_correlated callback raised for root %s; correlator continues",
                verdict.root.value,
            )


def make_correlated_failure_emitter() -> CorrelatedCallback:
    """Build a best-effort ``on_correlated`` callback routing to a Telegram alert.

    The COMMON wiring helper (CLAUDE.md §11) that maps a :class:`CorrelatedVerdict`
    to one :meth:`brutex.notifications.sweep_alerts.SweepAlerter.correlated_failure`
    push, so a production caller need not know the alerter surface. The
    :class:`~brutex.notifications.sweep_alerts.SweepAlerter` import is done LAZILY
    inside the returned closure (not at module top) so this module stays import-light
    and never pulls the notification stack until a verdict actually fires — and so a
    correlator built in a test with its OWN recorder callback never touches the
    notifier.

    The returned closure is itself fire-and-forget: a :class:`SweepAlerter` is a
    silent no-op when Telegram is disabled (the default) and swallows every publish
    error, and the :class:`FailureCorrelator` already wraps the callback in its own
    guard — so this can NEVER block, slow, or crash the pull (Golden Rule #10).
    """

    def _emit(verdict: CorrelatedVerdict) -> None:
        """Route one correlated verdict to a CRITICAL Telegram alert (best-effort)."""
        from brutex.notifications.sweep_alerts import SweepAlerter

        SweepAlerter().correlated_failure(
            root=verdict.root.value,
            symptoms=tuple(kind.value for kind in verdict.symptoms),
            summary=verdict.summary(),
        )

    return _emit


__all__ = [
    "DEFAULT_MAX_WINDOW_SIGNALS",
    "DEFAULT_PRIORITY",
    "DEFAULT_WINDOW_SECONDS",
    "CorrelatedCallback",
    "CorrelatedVerdict",
    "FailureCorrelator",
    "FailureKind",
    "FailureSignal",
    "make_correlated_failure_emitter",
]
