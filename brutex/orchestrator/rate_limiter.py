"""Adaptive token-bucket rate limiter — the COMMON pacing core reused by the vendor adapter.

Vendor facts (CLAUDE.md §2 Rule 6 + §6, current): Groww backtesting
(``get_historical_candles``) runs under an operator-confirmed HARD
**500 req/min** governor and has **NO daily quota** (DEC-GROWW-RATE-PERMIN-001,
2026-06-13, not vendor-documented); the per-SECOND cap is **UNVERIFIED**
(``GROWW_RATE_LIMIT_RPS_UNVERIFIED`` — an open Phase-0 probe gate). Production
pacing is owned by :class:`~brutex.adapters.rate_common.RateController` (AIMD
per-second ladder, operator-directed 8 r/s ceiling
``GROWW_RATE_LIMIT_RPS_CEILING`` + the 500/min governor), which REUSES this
bucket as its single shared token stream (CLAUDE.md §11 COMMON — no
re-implementation of the token math); the production rate is injected via
``initial_rps`` / :meth:`AdaptiveTokenBucket.set_rate`. This module's OWN
defaults (3.0 / 1.5 r/s below, DEC-P0-030, PR #50c) date from the RETIRED
prior-vendor "5 req/s + 100,000/day" model and govern only the direct
env-var path, not the production pull.

## Contract

* **Start at ``DEFAULT_INITIAL_RPS`` req/s** (= 3.0; the
  ``BRUTEX_OHLC_RATE_LIMIT_RPS`` env-var default — a historic 60%-of-5
  tuning, DEC-P0-030; today it merely coincides with the AIMD soft floor.
  The Groww per-second cap is UNVERIFIED per CLAUDE.md §6; production
  injects its own rate via ``RateController``).
* **Auto-downshift to ``DEFAULT_DOWNSHIFT_RPS`` req/s** (= 1.5) after
  ``DEFAULT_DOWNSHIFT_TRIGGER`` (= 3) consecutive 429 / rate-limit
  exception hits inside ``DEFAULT_DOWNSHIFT_WINDOW`` (= 60) seconds.
* **Emit a ``rate_limit.downshift`` Telegram event** when downshifting.
* **Upshift back to ``DEFAULT_INITIAL_RPS``** after
  ``DEFAULT_UPSHIFT_RECOVERY`` (= 120) seconds of clean operation
  (no 429s).
* **Never drops a request silently.** When the bucket is empty,
  :meth:`acquire` blocks until a token is available. The caller
  always gets through eventually.
* **Optional daily quota gate.** When constructed with a
  :class:`~brutex.orchestrator.quota_tracker.QuotaGate`, :meth:`acquire`
  consults the gate AFTER
  bucket success — if the gate's daily limit is exhausted, raises
  :class:`~brutex.orchestrator.quota_tracker.DailyQuotaExceeded` which
  the adapter maps to ``Outcome.FATAL`` (halt the phase, no auto-retry).
  DORMANT in production: Groww backtesting has NO daily quota
  (operator-confirmed 2026-06-13, DEC-GROWW-RATE-PERMIN-001), so both
  live build sites construct the controller with ``quota_tracker=None``
  (``dashboard/pull_controller.py`` + ``cli.py``).

## Design

Classic token bucket:

* ``rate`` — tokens added per second (this is what "adaptive" changes)
* ``capacity`` — max tokens in the bucket (small burst allowance)
* ``tokens`` — current token count, recomputed on every call
* ``last_refill_monotonic`` — when tokens were last added

The bucket uses ``time.monotonic()`` so it's immune to NTP clock
jumps, daylight savings, etc.

## Testability

The limiter takes optional ``clock`` and ``sleep`` callables as
constructor arguments. Tests inject fake implementations that
advance a synthetic clock and skip real ``time.sleep`` calls. In
production, these default to ``time.monotonic`` and ``time.sleep``.
"""

from __future__ import annotations

import math
import os
import struct
import threading
import time as _time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import structlog

if TYPE_CHECKING:
    from brutex.orchestrator.quota_tracker import QuotaGate

from brutex.models import BruteXError

_LOG = structlog.get_logger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Constants (tuned per FR-ING-017)
# ────────────────────────────────────────────────────────────────────────

#: Default steady-state rate (req/s). A historic 60%-of-5 tuning from the
#: RETIRED prior-vendor model (the Groww per-second cap is UNVERIFIED per
#: CLAUDE.md §6). Was 15.0 (calibrated for the deleted broker's 25 r/s);
#: retuned to 3.0 in DEC-P0-030 (PR #50c).
DEFAULT_INITIAL_RPS: Final[float] = 3.0

#: Rate we downshift to after consecutive 429s. Was 10.0 (broker-tuned);
#: retuned to 1.5 (50% of new initial) in DEC-P0-030. The invariant
#: ``downshift_rps < initial_rps`` enforced in ``__post_init__`` forces
#: atomic swap of both constants.
DEFAULT_DOWNSHIFT_RPS: Final[float] = 1.5

#: How many consecutive 429s within :data:`DEFAULT_DOWNSHIFT_WINDOW`
#: seconds trigger the downshift.
DEFAULT_DOWNSHIFT_TRIGGER: Final[int] = 3

#: Rolling window (seconds) over which we count 429s for downshift.
DEFAULT_DOWNSHIFT_WINDOW: Final[float] = 60.0

#: How long (seconds) we must stay clean before upshifting back.
DEFAULT_UPSHIFT_RECOVERY: Final[float] = 120.0

#: Burst capacity — how many tokens the bucket holds when full.
#: A burst of 5 lets us absorb short cadence variations without
#: dropping below the steady-state rate.
DEFAULT_BURST: Final[int] = 5

#: Legacy hard cap (5 r/s) carried over from the pre-pivot adapter — a
#: RETIRED prior-vendor figure (the Groww per-second cap is UNVERIFIED per
#: CLAUDE.md §6). The ``initial_rps <= _LEGACY_HARD_CAP_RPS`` defense-in-depth
#: clamp proposed per DEC-P0-031 (PR #50d, Agent 2 §D) was DEFERRED and never
#: landed (see the ``__post_init__`` NOTE) — the production
#: :class:`RateController` in ``brutex/adapters/rate_common.py`` relies on the
#: clamp's ABSENCE to run its operator-directed 8 r/s ceiling; do not add it.
_LEGACY_HARD_CAP_RPS: Final[float] = 5.0

#: Safety cap on the refill-loop inside :meth:`AdaptiveTokenBucket.acquire`.
#: Under injected fake clocks, round-off errors can occasionally leave
#: the token count at ``0.9999…`` indefinitely — this bound guarantees
#: the loop terminates after at most 16 iterations even in that case.
_ACQUIRE_LOOP_CAP: Final[int] = 16


# ────────────────────────────────────────────────────────────────────────
# Rust rate-math kernel seam (DEC-RATE-MATH-RUST-001, ADR-0019 / #1727a)
# ────────────────────────────────────────────────────────────────────────
#
# The FOUR pure IEEE f64 token-math expressions of this bucket (refill /
# consume / retry-wait / set-rate post-state) dispatch to the fno_math
# kernel under the default-OFF ``BRUTEX_RUST_RATE_MATH`` flag, with the
# #1531-pattern burn-in (16 double-computes compared on exact f64 BYTES via
# ``struct.pack``) + autorevert latch (``rate_math.kernel_autoreverted``) +
# the retained Python expressions as the oracle. Everything stateful stays
# Python: locks, clock/sleep, the 429 deque window, the quota-gate seam,
# events, the ``_ACQUIRE_LOOP_CAP`` escape hatch. §2 Rule 6 scope guard:
# NO cap / floor / ladder / governor VALUE moves — the kernel receives the
# already-resolved floats and returns arithmetic, nothing else.

#: The default-OFF opt-in flag for the Rust token-math kernel.
RUST_RATE_MATH_ENV: Final[str] = "BRUTEX_RUST_RATE_MATH"

#: Burn-in double-compute budget (mirrors the audit/calendar seams' 16).
RATE_MATH_BURN_IN_SAMPLES: Final[int] = 16

#: The env-flag vocabulary — a DELIBERATE local copy of the sweep flags'
#: ``_TRUTHY`` / ``_FALSY`` literal sets: the COMMON classifier lives in
#: ``brutex.engine.sweep_lookahead``, but the engine package imports THIS
#: package (``engine.sweep_run`` → ``orchestrator.audit``), so importing the
#: engine from here would create an orchestrator ⇄ engine package cycle
#: (the CodeQL ``py/cyclic-import`` class). Layering wins over §11 COMMON
#: here — noted, not silent (the audit.py seam precedent).
_RUST_FLAG_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_RUST_FLAG_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class _RustRateKernel:
    """The resolved Rust kernel surface — the four token-math functions.

    Held in the process memo so the seam resolves ``fno_math`` exactly once;
    tests inject a fake instance through :func:`_set_rust_kernel_for_tests`.
    """

    refill_fn: Callable[[float, float, float, float], float]
    consume_fn: Callable[[float, float], tuple[float, float]]
    more_wait_fn: Callable[[float, float, float], float]
    set_rate_fn: Callable[[float], tuple[float, float]]


#: Kernel memo + burn-in budget + autorevert latch + warn-once memo —
#: single-cell mutable module state, the same documented exception to the
#: no-global-mutable-state rule as the audit.py seam. The cells are written
#: under a BUCKET's lock (or test-only seams), so two buckets could race a
#: burn-in decrement — worst case one extra double-compute, never a wrong
#: result (mismatch always ships the PYTHON value and the latch write is
#: idempotent).
_RUST_KERNEL_RESOLVED: Final[list[bool]] = [False]
_RUST_KERNEL_VALUE: Final[list[_RustRateKernel | None]] = [None]
_RUST_BURN_IN_REMAINING: Final[list[int]] = [RATE_MATH_BURN_IN_SAMPLES]
_RUST_DISABLED_REASON: Final[list[str | None]] = [None]
_RUST_WARN_MEMO: Final[set[str]] = set()


def _warn_rust_once(event: str, **context: object) -> None:
    """Log *event* at WARNING once per process (per event + reason key)."""
    key = f"{event}:{context.get('reason', '')}"
    if key in _RUST_WARN_MEMO:
        return
    _RUST_WARN_MEMO.add(key)
    _LOG.warning(event, **context)


def _rust_flag_enabled() -> bool:
    """Resolve the default-OFF ``BRUTEX_RUST_RATE_MATH`` flag (fail-safe OFF).

    A junk (unrecognised, non-empty) value warns once and stays OFF — an
    operator's mistyped intent is never silently ignored.
    """
    raw = os.environ.get(RUST_RATE_MATH_ENV, "")
    value = raw.strip().lower()
    if value in _RUST_FLAG_TRUTHY:
        return True
    if value and value not in _RUST_FLAG_FALSY:
        # repr(): a newline-bearing env value must not forge log lines
        # (slice-9 review, edge NIT-3 — the repr() log-injection convention).
        _warn_rust_once("rate_math.rust_flag_unrecognised", value=repr(raw))
    return False


def _import_fno_math() -> Any:
    """Import indirection for the kernel wheel (monkeypatched by tests)."""
    import fno_math

    return fno_math


def _resolve_rust_kernel() -> _RustRateKernel | None:
    """Resolve the Rust kernel once per process — ``None`` = the Python arm.

    A wheel that is absent, or too old to carry the slice-9 rate-math
    symbols, warns ONCE (``rate_math.rust_kernel_unavailable``) and
    permanently falls back — never a crash (the #1531 wheel-state law).
    """
    if not _rust_flag_enabled():
        return None
    try:
        fno_math = _import_fno_math()
    except ImportError:
        _warn_rust_once("rate_math.rust_kernel_unavailable", reason="wheel_missing")
        return None
    refill_fn = getattr(fno_math, "bucket_refill", None)
    consume_fn = getattr(fno_math, "bucket_consume", None)
    more_wait_fn = getattr(fno_math, "bucket_more_wait", None)
    set_rate_fn = getattr(fno_math, "bucket_set_rate_state", None)
    if (
        not callable(refill_fn)
        or not callable(consume_fn)
        or not callable(more_wait_fn)
        or not callable(set_rate_fn)
    ):
        _warn_rust_once("rate_math.rust_kernel_unavailable", reason="wheel_stale")
        return None
    return _RustRateKernel(
        refill_fn=cast("Callable[[float, float, float, float], float]", refill_fn),
        consume_fn=cast("Callable[[float, float], tuple[float, float]]", consume_fn),
        more_wait_fn=cast("Callable[[float, float, float], float]", more_wait_fn),
        set_rate_fn=cast("Callable[[float], tuple[float, float]]", set_rate_fn),
    )


def _rust_rate_kernel() -> _RustRateKernel | None:
    """The memoized kernel handle; ``None`` while OFF/unavailable/latched."""
    if _RUST_DISABLED_REASON[0] is not None:
        return None
    if not _RUST_KERNEL_RESOLVED[0]:
        _RUST_KERNEL_VALUE[0] = _resolve_rust_kernel()
        _RUST_KERNEL_RESOLVED[0] = True
    return _RUST_KERNEL_VALUE[0]


def _disable_rust_kernel(reason: str, **context: object) -> None:
    """Latch the kernel OFF for the process (the burn-in autorevert).

    The first call logs ``rate_math.kernel_autoreverted`` at ERROR — a
    genuine kernel-wrongness signal, never throttled; repeats are no-ops.
    """
    if _RUST_DISABLED_REASON[0] is None:
        _RUST_DISABLED_REASON[0] = reason
        _LOG.error("rate_math.kernel_autoreverted", reason=reason, **context)


def _set_rust_kernel_for_tests(kernel: _RustRateKernel | None) -> None:
    """Inject a resolved kernel (test isolation ONLY — the fake-kernel seam)."""
    _RUST_KERNEL_VALUE[0] = kernel
    _RUST_KERNEL_RESOLVED[0] = True


def _reset_rust_rate_math_for_tests(*, burn_in_remaining: int = RATE_MATH_BURN_IN_SAMPLES) -> None:
    """Restore a fresh-process seam view (test isolation ONLY)."""
    _RUST_KERNEL_RESOLVED[0] = False
    _RUST_KERNEL_VALUE[0] = None
    _RUST_BURN_IN_REMAINING[0] = burn_in_remaining
    _RUST_DISABLED_REASON[0] = None
    _RUST_WARN_MEMO.clear()


def _f64_bytes(values: tuple[float, ...]) -> bytes:
    """Exact IEEE-754 bit pattern of each value — the burn-in comparator.

    ``==`` would call ``-0.0`` equal to ``0.0`` and ``nan`` unequal to
    itself; the byte comparison is the honest "same f64" predicate.
    """
    return struct.pack(f"<{len(values)}d", *values)


def _settle_rust_result(
    surface: str,
    rust_values: tuple[float, ...],
    python_thunk: Callable[[], tuple[float, ...]],
) -> tuple[float, ...]:
    """Serve a kernel result through the burn-in double-compute window.

    Post burn-in: the kernel values serve directly. During burn-in the
    Python oracle also runs and the tuples are compared on exact f64
    BYTES — a mismatch ships the PYTHON values (correctness first),
    latches the kernel off, and logs ONE ``rate_math.kernel_autoreverted``
    ERROR. One shared budget across all four surfaces.
    """
    if _RUST_BURN_IN_REMAINING[0] <= 0:
        return rust_values
    python_values = python_thunk()
    if _f64_bytes(rust_values) != _f64_bytes(python_values):
        _disable_rust_kernel(
            "burn_in_mismatch", surface=surface, rust=rust_values, python=python_values
        )
        return python_values
    _RUST_BURN_IN_REMAINING[0] -= 1
    return rust_values


def _py_bucket_refill(tokens: float, burst: float, rps: float, elapsed: float) -> float:
    """The retained Python oracle — ``_refill_locked``'s exact expression.

    Callers pre-guard ``elapsed > 0`` (the ``_refill_locked`` early return,
    which also owns the ``_last_refill`` stamp decision), so this is the
    pure clamp arm only.
    """
    return min(burst, tokens + elapsed * rps)


def _py_bucket_consume(tokens: float, rps: float) -> tuple[float, float]:
    """The retained Python oracle — ``acquire``'s first consume step."""
    if tokens >= 1.0:
        return (tokens - 1.0, 0.0)
    # Not enough tokens — compute wait time for one full token.
    return (tokens, (1.0 - tokens) / rps)


def _py_bucket_more_wait(tokens: float, rps: float, eps: float) -> float:
    """The retained Python oracle — the acquire retry-loop wait step."""
    return max((1.0 - tokens) / rps, eps)


def _py_bucket_set_rate_state(new_rps: float) -> tuple[float, float]:
    """The retained Python oracle — ``set_rate``'s drained post-state pair."""
    return (0.0, new_rps / 2.0)


def _bucket_refill(tokens: float, burst: float, rps: float, elapsed: float) -> float:
    """Flag-dispatched refill arithmetic (state writes stay at the call site)."""
    kernel = _rust_rate_kernel()
    if kernel is None:
        return _py_bucket_refill(tokens, burst, rps, elapsed)
    rust = (kernel.refill_fn(tokens, burst, rps, elapsed),)
    return _settle_rust_result(
        "bucket_refill", rust, lambda: (_py_bucket_refill(tokens, burst, rps, elapsed),)
    )[0]


def _bucket_consume(tokens: float, rps: float) -> tuple[float, float]:
    """Flag-dispatched consume step → ``(new_tokens, wait_seconds)``."""
    kernel = _rust_rate_kernel()
    if kernel is None:
        return _py_bucket_consume(tokens, rps)
    settled = _settle_rust_result(
        "bucket_consume", kernel.consume_fn(tokens, rps), lambda: _py_bucket_consume(tokens, rps)
    )
    return (settled[0], settled[1])


def _bucket_more_wait(tokens: float, rps: float, eps: float) -> float:
    """Flag-dispatched retry-loop wait step."""
    kernel = _rust_rate_kernel()
    if kernel is None:
        return _py_bucket_more_wait(tokens, rps, eps)
    rust = (kernel.more_wait_fn(tokens, rps, eps),)
    return _settle_rust_result(
        "bucket_more_wait", rust, lambda: (_py_bucket_more_wait(tokens, rps, eps),)
    )[0]


def _bucket_set_rate_state(new_rps: float) -> tuple[float, float]:
    """Flag-dispatched ``set_rate`` post-state → ``(tokens, downshift_rps)``."""
    kernel = _rust_rate_kernel()
    if kernel is None:
        return _py_bucket_set_rate_state(new_rps)
    settled = _settle_rust_result(
        "bucket_set_rate_state",
        kernel.set_rate_fn(new_rps),
        lambda: _py_bucket_set_rate_state(new_rps),
    )
    return (settled[0], settled[1])


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class RateLimiterError(BruteXError):
    """Base class for rate limiter errors."""


# ────────────────────────────────────────────────────────────────────────
# Event type emitted on state transitions
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RateLimiterEvent:
    """One anomaly/state-change event from the limiter.

    Published to subscribers (dashboard WebSocket, Telegram notifier,
    audit log). Each event has a ``kind`` label and an ``at``
    timestamp (monotonic seconds, NOT wall clock).
    """

    kind: str  # one of: "downshift" | "upshift" | "rate_limit_hit"
    at: float  # monotonic seconds
    # ``# pragma: no mutate`` ×2 (DEC-MUTATION-RUNTIME-001): mutmut's
    # ``float | None`` → ``float & None`` annotation flips are provably
    # equivalent (PEP 563 ``from __future__ import annotations`` — field
    # annotations are never evaluated). The pragmas are line-scoped, so the
    # ``None`` defaults they also skip are pinned byte-exact by
    # ``test_optional_fields_default_byte_exact``.
    from_rps: float | None = None  # pragma: no mutate
    to_rps: float | None = None  # pragma: no mutate
    recent_429_count: int = 0


# ────────────────────────────────────────────────────────────────────────
# Adaptive token bucket
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class AdaptiveTokenBucket:
    """Thread-safe adaptive token bucket.

    Create one instance per vendor adapter. Workers call
    :meth:`acquire` before every API call and :meth:`record_rate_limit_hit`
    if the call returned a vendor 429 / rate-limit response
    (:class:`~brutex.adapters.groww.errors.GrowwRateLimitError` per the Groww
    error hierarchy in ``brutex/adapters/groww/errors.py``).

    Parameters
    ----------
    initial_rps
        Steady-state rate in tokens per second.
    downshift_rps
        Rate we switch to after consecutive 429s.
    downshift_trigger
        How many 429s within :attr:`downshift_window` trigger the
        downshift.
    downshift_window
        Seconds — the rolling window used for the 429 count.
    upshift_recovery
        Seconds — how long we must be clean before upshifting back.
    burst
        Max tokens held in the bucket (allows short bursts).
    clock
        Callable returning monotonic seconds. Defaults to
        :func:`time.monotonic`. Injectable for tests.
    sleep
        Callable taking a seconds argument. Defaults to
        :func:`time.sleep`. Injectable for tests.
    """

    initial_rps: float = DEFAULT_INITIAL_RPS
    downshift_rps: float = DEFAULT_DOWNSHIFT_RPS
    downshift_trigger: int = DEFAULT_DOWNSHIFT_TRIGGER
    downshift_window: float = DEFAULT_DOWNSHIFT_WINDOW
    upshift_recovery: float = DEFAULT_UPSHIFT_RECOVERY
    burst: int = DEFAULT_BURST
    clock: Callable[[], float] = _time.monotonic
    sleep: Callable[[float], None] = _time.sleep
    #: Optional daily-quota gate. When set, :meth:`acquire` calls
    #: ``quota_tracker.acquire_or_raise()`` AFTER the bucket token is
    #: consumed (Agent 1 §A — outside the bucket lock to avoid leaking
    #: gate latency into observability reads). When ``None``, the
    #: bucket runs in "bucket-only" mode — the PRODUCTION posture (Groww
    #: has no daily quota, DEC-QUOTA-VERDICT-MOOT-001); a future capped
    #: vendor wires a :class:`~brutex.orchestrator.quota_tracker.QuotaGate`
    #: implementation here (DEC-ORCH-DEAD-SCAFFOLD-DELETE-001).
    #: ``# pragma: no mutate`` (DEC-MUTATION-RUNTIME-001): the ``| None`` →
    #: ``& None`` annotation flip is provably equivalent (PEP 563 — never
    #: evaluated; ``QuotaGate`` is a TYPE_CHECKING-only name). The
    #: ``None`` default the line-scoped pragma also skips stays pinned by
    #: ``test_quota_tracker_none_is_legacy_path``.
    quota_tracker: QuotaGate | None = None  # pragma: no mutate
    #: Ease-in: start the bucket EMPTY (0 tokens) instead of full (``burst``).
    #: With the default ``False`` the bucket grants its ``burst`` allowance
    #: immediately (legacy behaviour). The :class:`RateController` passes ``True`` so
    #: that the opening call cannot release a free burst token — it waits one full
    #: ``1/rate`` interval. A mid-pull rate change goes through :meth:`set_rate`
    #: (NOT a rebuild), which likewise drains the bucket to empty, so a stepdown
    #: can't release a stale higher-rate token either — keeping the PHYSICAL issue
    #: rate strictly ≤ the current rate at all times (DEC-DHAN-RATE-HARD-CAP-001 +
    #: DEC-DHAN-RATE-SINGLE-BUCKET-001 / Golden Rule #6).
    start_empty: bool = False

    # ── Runtime state (not dataclass fields — computed in __post_init__) ──
    _current_rps: float = field(init=False)
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _recent_429s: deque[float] = field(init=False)
    _last_429_at: float = field(init=False)
    _last_success_at: float = field(init=False)
    _lock: threading.Lock = field(init=False)
    _pending_events: list[RateLimiterEvent] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if self.initial_rps <= 0:
            raise RateLimiterError(f"initial_rps must be > 0, got {self.initial_rps}")
        # NOTE: Defense-in-depth clamp `initial_rps <= _LEGACY_HARD_CAP_RPS`
        # (Agent 2 §D from PR #50d pre-impl review) was intentionally
        # DEFERRED and never landed. The production caller is
        # `rate_common.RateController` (the shared live-pull pacer), which
        # RELIES on the clamp's ABSENCE to run the operator-directed 8 r/s
        # ceiling (GROWW_RATE_LIMIT_RPS_CEILING, DEC-GROWW-RATE-CEILING-8-002).
        # Do NOT add the deferred clamp — it would raise RateLimiterError
        # on the live pull.
        if self.downshift_rps <= 0:
            raise RateLimiterError(f"downshift_rps must be > 0, got {self.downshift_rps}")
        if self.downshift_rps >= self.initial_rps:
            raise RateLimiterError(
                f"downshift_rps ({self.downshift_rps}) must be < initial_rps ({self.initial_rps})"
            )
        if self.downshift_trigger < 1:
            raise RateLimiterError(f"downshift_trigger must be ≥ 1, got {self.downshift_trigger}")
        if self.burst < 1:
            raise RateLimiterError(f"burst must be ≥ 1, got {self.burst}")

        self._current_rps = self.initial_rps
        self._tokens = 0.0 if self.start_empty else float(self.burst)
        self._last_refill = self.clock()
        self._recent_429s = deque()
        # Sentinel "no 429 yet" — only ever read by ``_maybe_upshift_locked``,
        # which is itself unreachable until a downshift has stamped a real
        # timestamp here (the early-return at ``_current_rps == initial_rps``
        # guards the never-downshifted path). ``-inf`` keeps the recovery gate
        # ``now - _last_429_at >= upshift_recovery`` trivially open if it were
        # ever reached without a downshift.
        self._last_429_at = float("-inf")
        self._last_success_at = self.clock()
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────

    def acquire(self) -> float:
        """Block until one token is available, then consume it.

        Returns the wall-clock seconds waited. Always non-negative.
        Never drops the call — callers always get through.

        Thread-safe. If multiple threads call ``acquire`` at once they
        serialize through the internal lock, which is what we want —
        token bucket semantics.
        """
        with self._lock:
            self._maybe_upshift_locked()
            self._refill_locked()
            # The consume step's arithmetic is the flag-dispatched
            # ``_bucket_consume`` (DEC-RATE-MATH-RUST-001); on the
            # not-enough-tokens arm the returned tokens equal the input, so
            # the state write is byte-identical to the historical
            # branch-local ``-= 1.0`` / untouched split.
            self._tokens, wait_seconds = _bucket_consume(self._tokens, self._current_rps)

        if wait_seconds > 0.0:
            # Release the lock while sleeping so other threads can still
            # check upshift status. After waking, re-enter and consume.
            self.sleep(wait_seconds)
            with self._lock:
                self._refill_locked()
                # Guaranteed at least 1.0 after the sleep unless downshift
                # happened concurrently, in which case loop once more.
                # Use a small epsilon to absorb float arithmetic drift so
                # `tokens == 0.9999…` doesn't trigger an infinite micro-wait
                # loop.
                eps = 1e-9
                iterations = 0
                while self._tokens < 1.0 - eps:
                    iterations += 1
                    if iterations > _ACQUIRE_LOOP_CAP:
                        # Safety net — in the fake-clock world, round-off
                        # should converge in 1-2 iterations; 16 is a hard
                        # cap so tests can't deadlock on us.
                        self._tokens = 1.0
                        # ``# pragma: no mutate`` (DEC-MUTATION-RUNTIME-001):
                        # ``_tokens`` was just set to 1.0, so the loop guard
                        # ``_tokens < 1.0 - eps`` is statically False — mutmut's
                        # ``break`` → ``continue`` re-tests the guard and exits
                        # identically. Provably equivalent.
                        break  # pragma: no mutate
                    more_wait = _bucket_more_wait(self._tokens, self._current_rps, eps)
                    self.sleep(more_wait)
                    self._refill_locked()
                    wait_seconds += more_wait
                self._tokens -= 1.0

        # Daily quota gate (DEC-P0-030, PR #50c). Called OUTSIDE the
        # bucket lock per Agent 1 §A to avoid leaking gate latency
        # into dashboard reads. The quota gate carries its own locking; the
        # two locks compose sequentially (bucket released → quota acquired)
        # so no deadlock possible. If the daily quota is exhausted, the
        # bucket token is "wasted" — acceptable per FATAL semantics
        # (phase halts, no auto-retry).
        if self.quota_tracker is not None:
            # May raise DailyQuotaExceeded; bubbles up to the caller.
            self.quota_tracker.acquire_or_raise()

        return wait_seconds

    def record_rate_limit_hit(self) -> RateLimiterEvent | None:
        """Called when a call just returned a 429 / rate-limit exception.

        Adds the hit to the rolling window. May trigger a downshift
        if the trigger threshold is exceeded.

        Returns
        -------
        RateLimiterEvent | None
            A ``downshift`` event if we just downshifted, otherwise
            a ``rate_limit_hit`` event for dashboard visibility.
            Callers should publish returned events to Telegram / the
            dashboard / the audit log.
        """
        with self._lock:
            now = self.clock()
            self._recent_429s.append(now)
            # Stamp the most-recent 429 so the upshift recovery gate
            # (``_maybe_upshift_locked``) can require a full
            # :attr:`upshift_recovery` window of quiet AFTER it, independently
            # of the shorter ``downshift_window`` the rolling 429 count uses.
            self._last_429_at = now
            # Evict 429s older than the window
            cutoff = now - self.downshift_window
            while self._recent_429s and self._recent_429s[0] < cutoff:
                self._recent_429s.popleft()

            count = len(self._recent_429s)

            # Should we downshift?
            if (
                math.isclose(self._current_rps, self.initial_rps, rel_tol=1e-6)
                and count >= self.downshift_trigger
            ):
                from_rps = self._current_rps
                self._current_rps = self.downshift_rps
                # Drain bucket so callers feel the new rate immediately
                self._tokens = min(self._tokens, float(self.burst))
                return RateLimiterEvent(
                    kind="downshift",
                    at=now,
                    from_rps=from_rps,
                    to_rps=self.downshift_rps,
                    recent_429_count=count,
                )

            return RateLimiterEvent(
                kind="rate_limit_hit",
                at=now,
                from_rps=self._current_rps,
                to_rps=self._current_rps,
                recent_429_count=count,
            )

    def record_success(self) -> None:
        """Called after any successful (non-429) call.

        Updates the "last clean operation" timestamp used by the
        upshift logic.
        """
        with self._lock:
            self._last_success_at = self.clock()

    def current_rps(self) -> float:
        """Dashboard stat — current effective rate."""
        with self._lock:
            return self._current_rps

    def set_rate(self, new_rps: float) -> None:
        """Atomically change the steady-state rate on this LIVE bucket — O(1).

        The :class:`RateController`'s step ladder (5→4→3 down / 3→4→5 up) calls this
        so a rate change mutates the ONE shared bucket IN PLACE instead of swapping in
        a freshly-built one. Swapping created a transient where in-flight
        :meth:`acquire` callers paced on the OLD bucket while new callers paced on
        the NEW one — two independent token streams whose COMBINED physical issue
        rate briefly exceeded the cap (the operator's ``rate.breach rps_1s=11``
        opening 5→4→3 cascade). With a single persistent object every caller
        shares one ≤ ``new_rps`` token stream at all times
        (DEC-DHAN-RATE-SINGLE-BUCKET-001 / CLAUDE.md §2 Rule 6).

        Drains the bucket to EMPTY on the change — exactly mirroring the prior
        ``start_empty=True`` rebuild — so the first call after a DOWNSHIFT waits a
        full ``1/new_rps`` interval and cannot release a stale higher-rate token.
        ``initial_rps`` (and the unused ``downshift_rps``) move WITH the rate so
        the bucket's own dormant 429/upshift logic stays self-consistent: a
        controller never feeds the bucket 429s — it owns its own ladder — so with
        ``_current_rps == initial_rps`` the internal :meth:`_maybe_upshift_locked`
        stays a no-op and never fights the controller's chosen rate.

        Raises
        ------
        RateLimiterError
            If *new_rps* is not strictly positive.
        """
        if new_rps <= 0:
            raise RateLimiterError(f"new_rps must be > 0, got {new_rps}")
        with self._lock:
            drained_tokens, downshift_rps = _bucket_set_rate_state(new_rps)
            self.initial_rps = new_rps
            self.downshift_rps = downshift_rps
            self._current_rps = new_rps
            self._tokens = drained_tokens
            self._last_refill = self.clock()

    def available_tokens(self) -> float:
        """Dashboard stat — how many tokens are currently in the bucket."""
        with self._lock:
            self._refill_locked()
            return self._tokens

    def recent_429_count(self) -> int:
        """Dashboard stat — how many 429s in the rolling window."""
        with self._lock:
            now = self.clock()
            cutoff = now - self.downshift_window
            while self._recent_429s and self._recent_429s[0] < cutoff:
                self._recent_429s.popleft()
            return len(self._recent_429s)

    def pending_events(self) -> list[RateLimiterEvent]:
        """Drain and return any pending events.

        Currently unused — events are returned directly from
        :meth:`record_rate_limit_hit`. Kept for a future batched
        mode if we add upshift auto-events.
        """
        with self._lock:
            events = self._pending_events[:]
            self._pending_events.clear()
            return events

    # ── Internal helpers (lock held by caller) ────────────────────

    def _refill_locked(self) -> None:
        """Refill the token bucket based on elapsed wall-clock time (caller holds the lock)."""
        now = self.clock()
        elapsed = now - self._last_refill
        # ``# pragma: no mutate`` (DEC-MUTATION-RUNTIME-001): at ``elapsed == 0``
        # the body below is a no-op (``tokens + 0 * rps == tokens``; the ``min``
        # clamp is inert under the ``tokens <= burst`` invariant every writer
        # preserves; ``_last_refill = now`` rewrites the identical value), so
        # mutmut's ``<=`` → ``<`` flip is provably equivalent. The ``0 → 1`` flip
        # the line-scoped pragma also skips is a REAL mutant — pinned byte-exact
        # by ``test_subsecond_elapsed_refill_pinned``.
        if elapsed <= 0:  # pragma: no mutate
            return
        self._tokens = _bucket_refill(self._tokens, float(self.burst), self._current_rps, elapsed)
        self._last_refill = now

    def _maybe_upshift_locked(self) -> RateLimiterEvent | None:
        """Check whether enough clean time has passed to upshift.

        Called on every :meth:`acquire` (cheap — just a timestamp
        comparison). If we currently run at downshift_rps and a full
        :attr:`upshift_recovery` window has elapsed since the most recent
        429 (``self._last_429_at``) AND we've had at least one successful
        call, upshift back to initial_rps.

        The recovery gate is keyed on :attr:`upshift_recovery` (the
        documented "stay clean before upshifting back" window — typically
        twice the rolling :attr:`downshift_window` used to COUNT 429s for the
        downshift trigger), so the bucket never recovers faster than the spec
        promises (Golden Rule #6: never recover faster than specified).
        """
        if math.isclose(self._current_rps, self.initial_rps, rel_tol=1e-6):
            return None
        now = self.clock()
        # Recovery gate: the most-recent 429 must be at least
        # ``upshift_recovery`` seconds in the past. ``_last_429_at`` is always
        # a real timestamp here because reaching this point requires a prior
        # downshift, and every downshift stamps it (the construction sentinel
        # ``-inf`` is never observed).
        if now - self._last_429_at < self.upshift_recovery:
            return None
        # Defensive clock-anomaly guard: a backwards-stepping injected clock
        # could put ``now`` before the last success — never upshift early.
        if now - self._last_success_at < 0:
            return None
        # A monotonic clock CAN legitimately start at 0.0; require at least one
        # recorded success before trusting the recovery window.
        if self._last_success_at <= 0:
            return None
        # Upshift!
        from_rps = self._current_rps
        self._current_rps = self.initial_rps
        event = RateLimiterEvent(
            kind="upshift",
            at=now,
            from_rps=from_rps,
            to_rps=self.initial_rps,
            recent_429_count=0,
        )
        self._pending_events.append(event)
        return event


__all__ = [
    "DEFAULT_BURST",
    "DEFAULT_DOWNSHIFT_RPS",
    "DEFAULT_DOWNSHIFT_TRIGGER",
    "DEFAULT_DOWNSHIFT_WINDOW",
    "DEFAULT_INITIAL_RPS",
    "DEFAULT_UPSHIFT_RECOVERY",
    "AdaptiveTokenBucket",
    "RateLimiterError",
    "RateLimiterEvent",
]
