"""Flag-gated Rust sweep-metrics seam — the per-mask drawdown kernel binding.

DEC-SWEEP-RUST-METRICS-001 (MIGRATION.md #1186) — Phase B of the Rust-migration
blueprint. Phase A (DEC-SWEEP-RUST-TRADESIM-WIRE-001, #1182) moved the sweep's
trade walk into Rust; the per-mask trades→metrics TAIL
(:func:`brutex.engine.sweep_driver.metrics_from_trades` =
``build_equity_curve`` + ``compute_metrics``, dominated by the ``O(N_bars)``
``_drawdown_stats`` scan) stayed pure Python and is the measured next
bottleneck. This module is the Python side of the Phase-B port: the env-flag
resolution, the f64-exactness envelope guard, the kernel binding with its
result validation, and the LOUD-but-flood-safe fallback logging. The
orchestration that mirrors ``build_equity_curve``'s exit-mapping/span
semantics lives next to that function in :mod:`brutex.engine.sweep_driver`
(``_metrics_from_trades_via_kernel``) — one file to review for bit-identity.

## The flag — ``BRUTEX_SWEEP_RUST_METRICS`` over ``SWEEP_RUST_METRICS_DEFAULT``

:func:`sweep_rust_metrics_enabled` reads the env per call, exactly like the
sibling :func:`brutex.engine.sweep_lookahead.sweep_lookahead_guard_enabled`
(whose ``_TRUTHY`` / ``_FALSY`` literal sets are consumed through the
PUBLIC :func:`~brutex.engine.sweep_lookahead.env_flag_tristate`
classifier, not copied — CLAUDE.md §11 COMMON). Per-call resolution (one ``os.environ`` dict probe,
~0.1 µs against a multi-ms per-mask tail; pinned by the OFF-cost A/B bench)
was chosen over a once-per-process memo so that (a) there is no
module-global FLAG state, and (b) every ``sweep_parallel`` worker process
reads its OWN inherited environment at call time — workers can never arm
inconsistently from a parent-only resolution. Resolution: a ``_TRUTHY``
value forces ON, a ``_FALSY`` value forces OFF (the explicit operator kill
switch), unset/whitespace-empty falls to
:data:`SWEEP_RUST_METRICS_DEFAULT`, and any OTHER (unrecognised) value
fails SAFE to OFF with one warn-once ``flag_unrecognised`` event — under a
default-ON kernel a typo'd kill switch (``flase``, ``0ff``) must still
KILL, never be silently ignored. OFF keeps the caller on the verbatim
pre-#1186 Python path, byte-identical by construction. The default is the ONE constant
:data:`SWEEP_RUST_METRICS_DEFAULT` — **ON since ADR-0017
(DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001)**, the "separate later ADR" the
DEC-SWEEP-RUST-METRICS-001 flag section deferred to; the revert is that one
constant, and ``BRUTEX_SWEEP_RUST_METRICS=0`` is the per-process operator
kill switch.

## Burn-in self-check + autorevert latch (DEC-SWEEP-EXIT-INDEX-FASTPATH-001)

The kernel arm additionally carries a per-process BURN-IN self-check, the
safety net that makes a default-ON kernel honest: for the first
:data:`SELF_CHECK_SAMPLES` kernel-served masks per process, the sweep-driver
orchestration ALSO runs the Python oracle and bit-compares the two 4-tuples
(:func:`drawdown_compare_key` — ints exact, floats by ``struct.pack`` bit
pattern, signed zeros included). A mismatch — a kernel numerical bug the
domain validation cannot see — SHIPS THE ORACLE VALUE (correctness first,
never a wrong number persisted) and latches the kernel OFF for the rest of
the process (:func:`disable_kernel`, one ``sweep_rust_metrics.
kernel_autoreverted`` ERROR; every later call short-circuits to the Python
oracle via the :func:`kernel_disabled` gate with a warn-once fallback). The
latch and the remaining-samples counter are the same *documented exception*
to the no-global-mutable-state rule as the observability memo — a genuine
mismatch never changes RESULTS (the oracle value ships either way), only
which engine computes them; :func:`reset_self_check_for_tests` restores a
fresh process view. The counter/latch are plain writes (no lock): a racing
worker thread can at worst run a handful of extra self-checks — never a
wrong result.

## Bit-exactness envelope (Golden Rule #4) — why 2⁵²

The Python oracle folds equity in arbitrary-precision ints and computes
``dd_pct = drop / peak`` with CPython's correctly-rounded ``int / int``. The
kernel computes ``(drop as f64) / (peak as f64)`` — exact ONLY while both
operands convert to f64 exactly. :data:`MAX_EXACT_EQUITY_PAISA` = 2⁵² bounds
``start_equity_paisa + Σ|pnl|`` so every equity value (≤ 2⁵²) and every
peak-to-trough drop (≤ 2⁵³) is exactly representable, making the kernel's
division bit-identical to Python's (see ``rust/fno-math/src/metrics.rs`` for
the full derivation). :func:`exceeds_exact_equity_envelope` checks the bound
in EXACT Python int arithmetic; anything outside routes to the Python oracle
with one WARNING — never a raise the pure-Python arm would not have produced
(the Phase-A fallback law), and incidentally 2⁵² ≪ ``i64::MAX`` makes every
kernel-side i64 add provably overflow-free.

## Fallback + engagement observability (Golden Rule #10)

Every fallback reason logs ``sweep_rust_metrics.fallback`` (WARNING) ONCE per
process per reason — the sweep calls this seam once per MASK (up to millions
per run), so a sticky reason (an out-of-envelope tuple, a stale wheel) would
otherwise flood the logs; the memo mirrors Phase A's per-(instance, reason)
``_call_fallback_warned``. A kernel result that fails the domain validation
is REFUSED loudly (``sweep_rust_metrics.kernel_result_refused``, ERROR — a
refused result indicates a kernel BUG, never adopted silently) and the mask
is served by the Python oracle. The first successful kernel call logs
``sweep_rust_metrics.engaged`` (INFO) — the observable "the wired ON path
actually runs Rust" signal the golden suite asserts (the vacuous-gate trap).
These memos are LOG-VOLUME state only: results never depend on them, and
:func:`reset_observability_memo_for_tests` restores a fresh process view for
test isolation (the documented exception to the no-global-mutable-state rule,
same in kind as Phase A's instance-level warn-once memo — there is no
instance at this stateless seam to hang it on).

## Honest envelope (Golden Rule #10)

* Constant-factor win only: the per-tuple tail stays ``O(kept · N_bars)``.
* The exit-timestamp→index dict build is a per-mask ``O(N_bars)`` cost ONLY
  on the DEMOTED path now: the sweep spine hands the metrics tail each
  mask's certified exit indices (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 — the
  trade simulators return them alongside the trades), so the certified fast
  path is ``O(trades)`` per mask; an uncertified/inconsistent stream demotes
  to the historical dict build, never to the Python oracle.
* The burn-in self-check runs the ``O(N_bars)`` Python oracle for the first
  :data:`SELF_CHECK_SAMPLES` kernel-served masks per process — a bounded,
  amortised-nil cost, excluded from the steady-state speedup bench.
* A Rust-side allocation failure under the crate's ``panic=abort`` profile
  is a process abort the Python ``MemoryError`` contract cannot catch (the
  kernel's per-call scratch is ~8 bytes/bar — same envelope as Phase A's
  resident construction).
* An env flip MID-call-sequence takes effect on the next mask (per-call
  read); results are bit-identical either way.
"""

from __future__ import annotations

import math
import os
import struct
from typing import TYPE_CHECKING, Final, TypeGuard

import structlog

# The truthy + falsy literal sets are defined ONCE on the sibling sweep-flag
# module (their vocabulary home) and consumed through its PUBLIC
# ``env_flag_tristate`` classifier — the sweep env flags accept the identical
# vocabulary with zero copied strip/lower/membership logic (CLAUDE.md §11
# COMMON; reviewer-directed "import it, don't copy").
from brutex.engine.sweep_lookahead import env_flag_tristate

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

    from brutex.metrics.types import ClosedTrade

_LOG = structlog.get_logger("brutex.engine.rust_metrics")

#: The env flag. A ``_TRUTHY`` value forces the Rust metrics tail ON, a
#: ``_FALSY`` value forces it OFF (the operator kill switch), unset/empty
#: falls to :data:`SWEEP_RUST_METRICS_DEFAULT`, and anything unrecognised
#: fails SAFE to OFF with one warn-once ``flag_unrecognised`` event (a
#: typo'd kill switch still kills under the default-ON kernel).
SWEEP_RUST_METRICS_ENV: Final[str] = "BRUTEX_SWEEP_RUST_METRICS"

#: The ONE default-arm constant (the DEC-SWEEP-RUST-METRICS-001 "flip to
#: ON-by-default is a SEPARATE later ADR" lever — THAT ADR is ADR-0017,
#: DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001): what
#: :func:`sweep_rust_metrics_enabled` returns when the env flag is unset
#: or whitespace-empty. ``True`` = the fused Rust drawdown
#: kernel serves by default — bit-identical to the Python oracle inside the
#: 2⁵² envelope, burn-in self-checked with the autorevert latch, and
#: killable per process with ``BRUTEX_SWEEP_RUST_METRICS=0`` (the ``_FALSY``
#: operator kill switch). Revert = this ONE constant.
SWEEP_RUST_METRICS_DEFAULT: Final[bool] = True

#: Kernel-served masks bit-compared against the Python oracle per process
#: (the burn-in self-check — module docstring). Bounded so the double
#: computation is an amortised-nil startup cost, large enough that a
#: deterministic kernel numerical regression on real data is overwhelmingly
#: likely to be exercised within the burn-in.
SELF_CHECK_SAMPLES: Final[int] = 16

#: Python mirror of the kernel's f64-exactness ceiling
#: (``rust/fno-math/src/metrics.rs`` ``MAX_EXACT_EQUITY_PAISA = 1 << 52``).
#: ``start_equity_paisa + Σ|pnl| <= 2**52`` guarantees every equity value,
#: running peak AND peak-to-trough drop converts to f64 EXACTLY, so the
#: kernel's ``drop/peak`` division is bit-identical to CPython's
#: correctly-rounded big-int division (module docstring). At the production
#: ₹100,000 notional base (10⁷ paisa) the envelope admits ~4.5·10⁸× the base
#: in summed |P&L| — no realistic sweep leaves it.
MAX_EXACT_EQUITY_PAISA: Final[int] = 1 << 52

#: The analytically-exact drawdown stats of a FLAT equity curve — the
#: zero-trade shortcut (66% of the profiled fixture tail needed NO Rust).
#: Proof of bit-identity to ``_drawdown_stats`` on ``equity ≡ start > 0``:
#: every bar has ``drop = 0`` (peak stays ``start``) ⇒ ``max_dd_paisa = 0``,
#: ``max_dd_bars = 0``; ``dd_pct = 0/start = +0.0`` ⇒ ``max_dd_pct`` stays
#: ``+0.0`` and ``sum_sq_pct`` stays ``+0.0``; ``ulcer = sqrt(+0.0/n) = +0.0``;
#: both ``× 100.0`` stay ``+0.0`` — including the SIGN of zero (pinned by a
#: bit-level test against the real scan).
FLAT_DRAWDOWN_STATS: Final[tuple[int, float, int, float]] = (0, 0.0, 0, 0.0)

#: Once-per-process-per-reason fallback memo + the engaged-once latch.
#: LOG-VOLUME state ONLY (module docstring): results never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: Arity of the ``_drawdown_stats`` result tuple the kernel mirrors.
_DRAWDOWN_ARITY: Final[int] = 4

#: Self-check samples still to run this process (single-cell mutable — the
#: documented no-global-mutable-state exception, module docstring).
_SELF_CHECK_REMAINING: Final[list[int]] = [SELF_CHECK_SAMPLES]

#: The autorevert latch: the mismatch reason once the kernel is disabled for
#: this process, else ``None`` (same documented exception as above).
_KERNEL_DISABLED_REASON: Final[list[str | None]] = [None]


def sweep_rust_metrics_enabled() -> bool:
    """Resolve the Rust-metrics flag over :data:`SWEEP_RUST_METRICS_DEFAULT`.

    * a ``_TRUTHY`` value (``1`` / ``true`` / ``yes`` / ``on``,
      case-insensitive, surrounding whitespace ignored) → ``True``;
    * a ``_FALSY`` value (``0`` / ``false`` / ``no`` / ``off``) → ``False``
      — the explicit operator kill switch, honoured regardless of the
      default;
    * unset or whitespace-empty → :data:`SWEEP_RUST_METRICS_DEFAULT` (the
      one-constant flip lever);
    * any OTHER value → ``False`` with one warn-once ``flag_unrecognised``
      fallback event: the fail-safe is OFF — under a default-ON kernel a
      typo'd kill switch (``flase``, ``0ff``) must still KILL, never be
      silently ignored. The ``_FALSY`` vocabulary is the RECOGNISED
      (silent) OFF; a garbage value's OFF is WARNED.

    The vocabulary is the SAME ``_TRUTHY`` / ``_FALSY`` pair as the sweep
    look-ahead guard flag, classified through the COMMON
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate` (this function
    owns only the default + warn policy for the ``None`` verdict). Read per
    call — see the module docstring for why (no global flag state; parallel
    workers read their own env).
    """
    raw = os.environ.get(SWEEP_RUST_METRICS_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        # An unrecognised NON-EMPTY value fails SAFE to OFF, loudly: under a
        # default-ON kernel a typo'd kill switch ("flase", "0ff") must still
        # KILL — never a silently-ignored operator intent (warn-once).
        warn_fallback_once("flag_unrecognised", value=raw)
        return False
    return SWEEP_RUST_METRICS_DEFAULT


def self_check_pending() -> bool:
    """Return ``True`` while this process still owes burn-in self-check samples.

    Consulted by the sweep-driver orchestration AFTER a successful kernel
    call; ``True`` means "also run the Python oracle and bit-compare". The
    budget never blocks results — it only decides whether the cross-check
    runs (module docstring).
    """
    return _SELF_CHECK_REMAINING[0] > 0


def consume_self_check_sample() -> None:
    """Consume one burn-in sample after a PASSED kernel-vs-oracle bit-compare.

    Clamped at zero so a racing extra call can never go negative. A FAILED
    compare never consumes — it latches :func:`disable_kernel` instead.
    """
    if _SELF_CHECK_REMAINING[0] > 0:
        _SELF_CHECK_REMAINING[0] -= 1


def kernel_disabled() -> bool:
    """Return ``True`` iff the autorevert latch has disabled the kernel this process."""
    return _KERNEL_DISABLED_REASON[0] is not None


def disable_kernel(reason: str, **context: object) -> None:
    """Latch the kernel OFF for the remainder of the process (the autorevert).

    Called by the sweep-driver self-check on a bit-compare MISMATCH — a
    kernel numerical bug the domain validation cannot see. The first call
    logs ``sweep_rust_metrics.kernel_autoreverted`` (ERROR — a genuine
    kernel-wrongness signal, never throttled below the operator's view);
    repeat calls are no-ops (the latch is already set). Every subsequent
    :func:`drawdown_stats_via_kernel` call short-circuits to the Python
    oracle, so a wrong kernel can poison at most the ONE mismatching mask —
    and even that mask ships the ORACLE value (the caller's contract).
    """
    if _KERNEL_DISABLED_REASON[0] is None:
        _KERNEL_DISABLED_REASON[0] = reason
        _LOG.error("sweep_rust_metrics.kernel_autoreverted", reason=reason, **context)


def drawdown_compare_key(stats: tuple[int, float, int, float]) -> tuple[int, bytes, int, bytes]:
    """Map a drawdown 4-tuple to its BIT-exact comparison key.

    Ints compare exactly under ``==``; the two floats are compared by their
    IEEE-754 bit pattern (``struct.pack('<d', …)``) so a signed-zero or
    payload difference can never hide behind float equality — the same
    bit-compare discipline as the golden/property suites. Total for every
    domain-valid tuple (ints are unbounded in Python — packing them could
    raise on a poisoned huge value, so they are deliberately NOT packed).
    """
    max_dd_paisa, max_dd_pct, max_dd_bars, ulcer = stats
    return (max_dd_paisa, struct.pack("<d", max_dd_pct), max_dd_bars, struct.pack("<d", ulcer))


def reset_self_check_for_tests(*, remaining: int = SELF_CHECK_SAMPLES) -> None:
    """Reset the burn-in budget + autorevert latch (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (used
    by the speedup bench and the poison-flows-through gates); the default
    restores a fresh-process view. Never called from production code paths.
    """
    _SELF_CHECK_REMAINING[0] = remaining
    _KERNEL_DISABLED_REASON[0] = None


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY).

    The memo throttles LOG VOLUME across a whole process; tests capturing
    structlog output reset it so each test observes first-occurrence events.
    Never called from production code paths.
    """
    _OBSERVABILITY_MEMO.clear()


def warn_fallback_once(reason: str, **context: object) -> None:
    """Log ``sweep_rust_metrics.fallback`` ONCE per process per *reason*.

    The seam runs once per MASK, so a sticky fallback reason would flood the
    logs at production sweep scale (the Phase-A per-(instance, reason) memo
    rationale — module docstring). The first occurrence is loud (WARNING);
    repeats of the same reason fall back silently. The fallback itself is
    bit-exact regardless (the Python oracle serves the mask).
    """
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("sweep_rust_metrics.fallback", reason=reason, **context)


def exceeds_exact_equity_envelope(start_equity_paisa: int, trades: Sequence[ClosedTrade]) -> bool:
    """Return ``True`` iff ``start + Σ|pnl|`` exceeds the f64-exactness bound.

    EXACT Python int arithmetic (never floats), ``O(trades)`` per mask —
    negligible next to the ``O(N_bars)`` scan it guards. ``True`` means the
    kernel could double-round the ``drop/peak`` division (or, far beyond,
    overflow i64), so the caller must route this mask to the Python oracle.
    """
    magnitude = start_equity_paisa
    for trade in trades:
        magnitude += abs(trade.pnl_paisa)
    return magnitude > MAX_EXACT_EQUITY_PAISA


def _resolve_kernel() -> Callable[[int, int, list[tuple[int, int]]], object] | None:
    """Return ``fno_math.sweep_drawdown_stats``, or ``None`` with a warn-once.

    The production sweep hard-imports ``fno_math`` at module scope
    (``sweep_driver.py``), so a truly wheel-ABSENT environment fails long
    before this seam — the real production trigger is a STALE wheel that
    imports but lacks the Phase-B symbol (the ``AttributeError`` arm), which
    must degrade the METRICS tail to Python (loudly) while the Phase-A trade
    sim stays on Rust. Mirrors ``ResidentTradeSim``'s lazy single-import-site
    convention.
    """
    try:
        import fno_math
    except ImportError:
        warn_fallback_once("wheel_missing")
        return None
    kernel: Callable[[int, int, list[tuple[int, int]]], object] | None = getattr(
        fno_math, "sweep_drawdown_stats", None
    )
    if kernel is None:
        warn_fallback_once("wheel_stale")
        return None
    return kernel


def _valid_drawdown(result: object, n_bars: int) -> TypeGuard[tuple[int, float, int, float]]:
    """Cheap domain validation of a kernel result — the refuse-poison gate.

    ``O(1)``; catches a malformed kernel return (wrong arity/types, a
    non-finite or negative statistic, a duration exceeding the window)
    WITHOUT recomputing the scan. A result inside this domain that is
    nonetheless WRONG is the golden/property suites' layer to catch — no
    runtime check short of running the Python oracle could, and running both
    engines per mask would erase the win.
    """
    if not isinstance(result, tuple) or len(result) != _DRAWDOWN_ARITY:
        return False
    max_dd_paisa, max_dd_pct, max_dd_bars, ulcer = result
    if not isinstance(max_dd_paisa, int) or isinstance(max_dd_paisa, bool):
        return False
    if not isinstance(max_dd_bars, int) or isinstance(max_dd_bars, bool):
        return False
    if not isinstance(max_dd_pct, float) or not isinstance(ulcer, float):
        return False
    if max_dd_paisa < 0 or not 0 <= max_dd_bars <= n_bars:
        return False
    if not math.isfinite(max_dd_pct) or max_dd_pct < 0.0:
        return False
    return math.isfinite(ulcer) and ulcer >= 0.0


def drawdown_stats_via_kernel(
    n_bars: int, start_equity_paisa: int, exits: list[tuple[int, int]]
) -> tuple[int, float, int, float] | None:
    """Run the fused Rust equity-fold + drawdown scan; ``None`` ⇒ use Python.

    The ONE FFI call site. Returns the ``_drawdown_stats``-shaped 4-tuple
    ``(max_dd_paisa, max_dd_pct, max_dd_duration_bars, ulcer_index)`` on
    success. Returns ``None`` — after a warn-once (or, for a refused result,
    an ERROR) — when the kernel is unavailable, raises a boundary/conversion
    error, or returns a domain-invalid tuple; the caller then serves the mask
    through the bit-exact Python oracle, so wheel state can never change
    results or error behaviour (the Phase-A law). The fallback catch is
    NARROW (``ValueError`` — the kernel's own boundary arms; ``TypeError`` /
    ``OverflowError`` — PyO3 argument-conversion failures): a truly
    unexpected exception type PROPAGATES, the repo's genuine-code-bug-still-
    raises stance (CLAUDE.md §4 — no broad excepts), and ``MemoryError``
    re-raises BARE (OOM is a GLOBAL stop, DEC-LAKE-WRITE-HALT-001 — never
    swallowed into a fallback).

    The caller is expected to have pre-guarded the exactness envelope
    (:func:`exceeds_exact_equity_envelope`) and the exit indices (mapped
    from the same bar window), so the kernel's own boundary ``ValueError``
    arms are structurally unreachable belts on this path.

    An autorevert-latched process (:func:`disable_kernel` fired on a burn-in
    self-check mismatch) short-circuits HERE — one warn-once
    ``kernel_disabled`` fallback, no FFI — so a kernel proven wrong once can
    never serve another mask this process.
    """
    if kernel_disabled():
        warn_fallback_once("kernel_disabled")
        return None
    kernel = _resolve_kernel()
    if kernel is None:
        return None
    try:
        result = kernel(n_bars, start_equity_paisa, exits)
    except (ValueError, TypeError, OverflowError) as exc:
        # The kernel's boundary ValueError arms + PyO3 argument-conversion
        # TypeError/OverflowError degrade to the bit-exact oracle; any OTHER
        # exception type (incl. MemoryError — a GLOBAL stop) propagates.
        warn_fallback_once("kernel_error", error=repr(exc))
        return None
    if not _valid_drawdown(result, n_bars):
        # A domain-invalid result is a kernel BUG: refuse it LOUDLY (never
        # silently adopted — the never-adopt stance of the Phase-A
        # re-attribution path) and serve the mask from the Python oracle.
        if "kernel_result_refused" not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add("kernel_result_refused")
            _LOG.error(
                "sweep_rust_metrics.kernel_result_refused",
                result=repr(result),
                n_bars=n_bars,
            )
        return None
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info("sweep_rust_metrics.engaged", n_bars=n_bars)
    return result


__all__ = [
    "FLAT_DRAWDOWN_STATS",
    "MAX_EXACT_EQUITY_PAISA",
    "SELF_CHECK_SAMPLES",
    "SWEEP_RUST_METRICS_DEFAULT",
    "SWEEP_RUST_METRICS_ENV",
    "consume_self_check_sample",
    "disable_kernel",
    "drawdown_compare_key",
    "drawdown_stats_via_kernel",
    "exceeds_exact_equity_envelope",
    "kernel_disabled",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "self_check_pending",
    "sweep_rust_metrics_enabled",
    "warn_fallback_once",
]
