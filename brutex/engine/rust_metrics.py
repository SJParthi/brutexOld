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

## The flag — ``BRUTEX_SWEEP_RUST_METRICS``, default OFF

:func:`sweep_rust_metrics_enabled` reads the env per call, exactly like the
sibling :func:`brutex.engine.sweep_lookahead.sweep_lookahead_guard_enabled`
(whose ``_TRUTHY`` literal set is IMPORTED, not copied — CLAUDE.md §11
COMMON). Per-call resolution (one ``os.environ`` dict probe, ~0.1 µs against
a multi-ms per-mask tail; pinned by the OFF-cost A/B bench) was chosen over a
once-per-process memo so that (a) there is no module-global FLAG state, and
(b) every ``sweep_parallel`` worker process reads its OWN inherited
environment at call time — workers can never arm inconsistently from a
parent-only resolution. OFF (unset / empty / ``0`` / garbage) keeps the
caller on the verbatim pre-#1186 Python path, byte-identical by construction.
**The default stays OFF; flipping it ON by default is a SEPARATE later ADR,
explicitly NOT this change.**

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
* The exit-timestamp→index map stays a per-mask ``O(N_bars)`` Python dict
  build on the ON arm (``ClosedTrade`` carries timestamps, not indices, and
  the fenced ``sweep_run`` call site hands this seam bars+trades — no
  resident per-tuple state exists here).
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
from typing import TYPE_CHECKING, Final, TypeGuard

import structlog

# Intentional friend access (DEC-P0-017 per-site convention): the truthy
# literal set is defined ONCE on the sibling sweep-flag module — importing it
# keeps the two sweep env flags accepting the identical vocabulary
# (CLAUDE.md §11 COMMON; reviewer-directed "import it, don't copy").
from brutex.engine.sweep_lookahead import _TRUTHY  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

    from brutex.metrics.types import ClosedTrade

_LOG = structlog.get_logger("brutex.engine.rust_metrics")

#: The opt-in env flag. Unset / falsy ⇒ the Rust metrics tail is OFF (the
#: default) and the sweep metrics path is byte-identical and cost-unchanged.
SWEEP_RUST_METRICS_ENV: Final[str] = "BRUTEX_SWEEP_RUST_METRICS"

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


def sweep_rust_metrics_enabled() -> bool:
    """Return ``True`` iff ``BRUTEX_SWEEP_RUST_METRICS`` is set truthy.

    Truthy literals are ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive,
    surrounding whitespace ignored) — the SAME ``_TRUTHY`` vocabulary as the
    sweep look-ahead guard flag. Anything else — including unset, empty,
    ``0``, ``false`` or garbage — is OFF: an unrecognised value must never
    silently enable a different engine (fail-safe default). Read per call —
    see the module docstring for why (no global flag state; parallel workers
    read their own env).
    """
    return os.environ.get(SWEEP_RUST_METRICS_ENV, "").strip().lower() in _TRUTHY


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
    """
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
    "SWEEP_RUST_METRICS_ENV",
    "drawdown_stats_via_kernel",
    "exceeds_exact_equity_envelope",
    "reset_observability_memo_for_tests",
    "sweep_rust_metrics_enabled",
    "warn_fallback_once",
]
