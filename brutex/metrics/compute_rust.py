"""Flag-gated Rust metrics-FOLD seam — the ``_fold_trades`` kernel binding.

DEC-METRICS-RUST-COMPUTE-FOLD-001 — the Rust arm of the Slice-0 fold/combine
decomposition (DEC-SWEEP-COMPUTE-FOLD-DECOMP-001, MIGRATION.md #1685). The
metric body reads ``combine_metrics_from_folds(_fold_trades(...))``; this
module is the Python side of the FOLD half's Rust port: the env-flag
resolution, the kernel binding with its refuse-poison result validation, the
per-process burn-in self-check + autorevert latch, and the LOUD-but-
flood-safe fallback logging. The COMBINE tail deliberately stays in Python —
it carries libm ``pow`` (``growth ** (1.0/span_years)`` for CAGR;
``math.pow(m2_n, 1.5)`` for skewness), which has NO correct-rounding
guarantee across libms (the exact MUST-STAY-PYTHON class
``rust/fno-math/src/metrics.rs`` documents), so a full fold+combine port
could diverge in the last ulp. The fold uses only IEEE-exact operations, so
the split ships bit-identity BY CONSTRUCTION instead of by hope.

## The flag — ``BRUTEX_METRICS_RUST_FOLD`` (Pattern A, default OFF)

:func:`metrics_rust_fold_enabled` reads the env PER CALL (the
``rust_metrics`` per-call-resolution rationale: no module-global flag state;
parallel workers read their own inherited environment). Resolution: a
``_TRUTHY`` value is the ONLY way ON; a ``_FALSY`` value is OFF;
unset/whitespace-empty falls to :data:`METRICS_RUST_FOLD_DEFAULT` (``False``
— ships INERT; the one-constant default flip is a LATER DEC on the
DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 precedent); any OTHER value fails
SAFE to OFF with one warn-once ``flag_unrecognised`` event. OFF keeps the
caller on the verbatim Python ``_fold_trades`` path — byte-identical by
construction (the never-routes bomb-kernel spy pins it).

LEAF DISCIPLINE (verified import direction): ``brutex.metrics.compute``
imports this module at module scope, and ``brutex.engine`` (whose
``__init__`` pulls ``sweep_driver`` → ``brutex.metrics.compute``) must stay
importable — so this module must NOT import ``env_flag_tristate`` from
``brutex.engine.sweep_lookahead`` (a metrics→engine edge would both close an
import cycle and make a plain metrics import transitively hard-require the
``fno_math`` wheel via ``brutex/engine/__init__``). It carries its own
inline ``_TRUTHY`` / ``_FALSY`` vocabulary — the ``brutex.feed_rust`` leaf
precedent — mirror-pinned against ``sweep_lookahead``'s sets by a test. The
``brutex.metrics.compute`` internals it needs at runtime (``TradeFolds`` /
``Moments`` / ``_fold_trades``) are imported LAZILY inside the function
bodies (``compute`` is fully initialised by any call time; a module-scope
import would be the actual cycle).

## Bit-exactness (Golden Rule #4) — why the RETURNS column is computed HERE

The Python fold's one float-producing input expression is
``[t.pnl_paisa / start_equity_paisa for t in trades]`` — CPython's
correctly-rounded big-int ``int / int``, which a Rust
``(pnl as f64) / (start as f64)`` double-rounds whenever either operand
exceeds 2⁵³. This seam therefore computes the returns column with the
VERBATIM Python expression and ships it across the FFI as f64 — bit-identical
BY CONSTRUCTION, no exactness envelope needed. Everything the kernel derives
from it is plain sequential ``+ - * /`` in the Python body's exact operation
order (``rust/fno-math/src/metrics_fold.rs`` carries the term-for-term
transcription proof). Integer aggregates are folded in i128 Rust-side and
cross back as exact Python ints; a trade column value outside i64 raises
``OverflowError`` at PyO3 argument conversion, which degrades to the
unbounded-int Python oracle (tested).

## Burn-in self-check + autorevert latch (the #1531/#1532 pattern)

For the first :data:`SELF_CHECK_SAMPLES` kernel-served folds per process,
:func:`fold_trades_via_kernel` ALSO runs the Python ``_fold_trades`` oracle
and bit-compares the two :class:`~brutex.metrics.compute.TradeFolds`
(:func:`fold_compare_key` — ints exact, floats by ``struct.pack`` bit
pattern, signed zeros included; ``downside_sum_sq`` is compared through
``float(...)`` because the Python arm legitimately holds the int ``0`` on an
empty downside — ``0 / n`` and ``0.0 / n`` are the same ``+0.0``
combine-side, pinned). A mismatch SHIPS THE ORACLE VALUE (correctness first
— never a wrong fold consumed), latches the kernel OFF for the process
(:func:`disable_kernel`, one ``metrics_rust_fold.kernel_autoreverted``
ERROR), and every later call short-circuits to the oracle. The latch and
counter are the same documented no-global-mutable-state exception as the
``rust_metrics`` seam's (results never depend on them);
:func:`reset_self_check_for_tests` restores a fresh process view.

## Fault taxonomy (the greeks-batch autorevert-fence precedent)

``MemoryError`` re-raises RAW (OOM is a GLOBAL stop — never absorbed into a
fallback); ANY other kernel-call exception (PyO3 conversion
``TypeError``/``OverflowError``, the kernel's own boundary ``ValueError``,
a poisoned-wheel raise) degrades to the Python oracle with one warn-once
``metrics_rust_fold.fallback`` per reason per process. A structurally
invalid kernel result is REFUSED loudly (``kernel_result_refused``, ERROR —
a refused result indicates a kernel BUG, never adopted silently). A Rust
PANIC is structurally unreachable through the kernel's pure ``Result`` core;
under the crate's ``panic = "abort"`` release profile a genuine allocation
failure aborts the process (the same disclosed envelope as every sibling
kernel — no Python fence can catch it).

## Honest envelope (Golden Rule #10)

* Both arms are ``O(n·log n)`` per fold (the percentile sort dominates) —
  NO O(1) claim, and NO speedup claim is made: the seam ships INERT
  (default OFF) as the safe FFI stepping-stone; a default flip needs its
  own DEC with measured evidence.
* The burn-in runs the Python oracle for the first
  :data:`SELF_CHECK_SAMPLES` kernel-served folds per process — a bounded,
  amortised-nil startup cost.
* The column extraction + returns list are ``O(n)`` Python work per
  kernel-served call (the FFI marshalling cost — disclosed).
"""

from __future__ import annotations

import os
import struct
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

    from brutex.metrics.compute import TradeFolds
    from brutex.metrics.types import ClosedTrade

_LOG = structlog.get_logger("brutex.metrics.compute_rust")

#: The opt-in environment flag (Pattern A — only an explicit ``_TRUTHY``
#: value enables the kernel; everything else is OFF).
METRICS_RUST_FOLD_ENV: Final[str] = "BRUTEX_METRICS_RUST_FOLD"

#: The ONE default-arm constant: what :func:`metrics_rust_fold_enabled`
#: returns when the flag is unset or whitespace-empty. ``False`` — the seam
#: ships INERT; flipping this default is a SEPARATE later DEC (the
#: DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 precedent).
METRICS_RUST_FOLD_DEFAULT: Final[bool] = False

#: Inline flag vocabulary (LEAF discipline — module docstring; the
#: mirror-pin test asserts byte-equality with ``sweep_lookahead``'s sets,
#: the ``brutex.feed_rust`` precedent).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Kernel-served folds bit-compared against the Python oracle per process
#: (the burn-in self-check — module docstring). The #1531 constant.
SELF_CHECK_SAMPLES: Final[int] = 16

#: The kernel result's four wire-tuple arities (counts · sums · moments ·
#: floats) — the frozen FFI shape ``metrics_fold_trades`` returns.
_WIRE_ARITY: Final[int] = 4
_COUNTS_ARITY: Final[int] = 7
_SUMS_ARITY: Final[int] = 6
_MOMENTS_ARITY: Final[int] = 5
_FLOATS_ARITY: Final[int] = 3

#: Self-check samples still to run this process (single-cell mutable — the
#: documented no-global-mutable-state exception, module docstring).
_SELF_CHECK_REMAINING: Final[list[int]] = [SELF_CHECK_SAMPLES]

#: The autorevert latch: the mismatch reason once the kernel is disabled
#: for this process, else ``None`` (same documented exception).
_KERNEL_DISABLED_REASON: Final[list[str | None]] = [None]

#: Once-per-process-per-reason fallback memo + the engaged-once latch.
#: LOG-VOLUME state ONLY: results never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"


def metrics_rust_fold_enabled() -> bool:
    """Resolve the Rust-fold flag over :data:`METRICS_RUST_FOLD_DEFAULT`.

    * a ``_TRUTHY`` value (``1`` / ``true`` / ``yes`` / ``on``,
      case-insensitive, surrounding whitespace ignored) → ``True`` — the
      ONLY way the kernel arms (Pattern A);
    * a ``_FALSY`` value (``0`` / ``false`` / ``no`` / ``off``) → ``False``;
    * unset or whitespace-empty → :data:`METRICS_RUST_FOLD_DEFAULT`
      (``False`` — the one-constant flip lever);
    * any OTHER value → ``False`` with one warn-once ``flag_unrecognised``
      event — a typo'd opt-in (``ture``, ``0n``) is never silently armed
      NOR silently dropped without a trace.

    Read per call — no global flag state; every ``sweep_parallel`` worker
    process reads its OWN inherited environment at call time (the
    ``rust_metrics`` rationale).
    """
    raw = os.environ.get(METRICS_RUST_FOLD_ENV, "")
    stripped = raw.strip().lower()
    if stripped in _TRUTHY:
        return True
    if stripped in _FALSY:
        return False
    if stripped:
        warn_fallback_once("flag_unrecognised", value=raw)
        return False
    return METRICS_RUST_FOLD_DEFAULT


def warn_fallback_once(reason: str, **context: object) -> None:
    """Log ``metrics_rust_fold.fallback`` ONCE per process per *reason*.

    The seam runs once per metric row (potentially millions per sweep), so
    a sticky fallback reason would flood the logs; the first occurrence is
    loud (WARNING), repeats fall back silently. The fallback itself is
    bit-exact regardless (the Python oracle serves the row).
    """
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("metrics_rust_fold.fallback", reason=reason, **context)


def kernel_disabled() -> bool:
    """Return ``True`` iff the autorevert latch disabled the kernel this process."""
    return _KERNEL_DISABLED_REASON[0] is not None


def disable_kernel(reason: str, **context: object) -> None:
    """Latch the kernel OFF for the remainder of the process (the autorevert).

    Called on a burn-in bit-compare MISMATCH — a kernel numerical bug the
    structural validation cannot see. The first call logs
    ``metrics_rust_fold.kernel_autoreverted`` (ERROR — a genuine
    kernel-wrongness signal, never throttled); repeat calls are no-ops.
    Every subsequent :func:`fold_trades_via_kernel` call short-circuits to
    the Python oracle, so a wrong kernel can poison at most the ONE
    mismatching row — and even that row ships the ORACLE folds.
    """
    if _KERNEL_DISABLED_REASON[0] is None:
        _KERNEL_DISABLED_REASON[0] = reason
        _LOG.error("metrics_rust_fold.kernel_autoreverted", reason=reason, **context)


def self_check_pending() -> bool:
    """Return ``True`` while this process still owes burn-in self-check samples."""
    return _SELF_CHECK_REMAINING[0] > 0


def consume_self_check_sample() -> None:
    """Consume one burn-in sample after a PASSED kernel-vs-oracle bit-compare.

    Clamped at zero so a racing extra call can never go negative. A FAILED
    compare never consumes — it latches :func:`disable_kernel` instead.
    """
    if _SELF_CHECK_REMAINING[0] > 0:
        _SELF_CHECK_REMAINING[0] -= 1


def reset_self_check_for_tests(*, remaining: int = SELF_CHECK_SAMPLES) -> None:
    """Reset the burn-in budget + autorevert latch (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state; the
    default restores a fresh-process view. Never called from production.
    """
    _SELF_CHECK_REMAINING[0] = remaining
    _KERNEL_DISABLED_REASON[0] = None


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY)."""
    _OBSERVABILITY_MEMO.clear()


def fold_compare_key(folds: TradeFolds) -> tuple[object, ...]:
    """Map a :class:`TradeFolds` to its BIT-exact comparison key.

    Ints compare exactly under ``==``; every float is compared by its
    IEEE-754 bit pattern (``struct.pack('<d', …)``) so a signed-zero or
    last-ulp difference can never hide behind float equality — the #1531
    burn-in discipline. ``downside_sum_sq`` is normalised through
    ``float(...)`` first: the Python arm legitimately holds the int ``0``
    on an empty downside (``sum([])``) where the kernel returns ``+0.0``;
    the two are combine-equivalent (``0 / n == 0.0 / n == +0.0``, pinned by
    the golden suite's ``scratch_only`` corpus case) and ``float(0)`` is
    ``+0.0``, so the key equates exactly that pair and nothing else.
    """
    moments = folds.moments
    return (
        folds.n_trades,
        folds.net_pnl_paisa,
        folds.gross_profit_paisa,
        folds.gross_loss_abs_paisa,
        folds.winners,
        folds.losers,
        folds.sum_mfe_paisa,
        folds.sum_mae_paisa,
        folds.sum_holding_bars,
        folds.max_holding_bars,
        folds.largest_loss_paisa,
        folds.max_consecutive_losses,
        folds.max_consecutive_wins,
        moments.n,
        struct.pack("<d", moments.mean),
        struct.pack("<d", moments.m2),
        struct.pack("<d", moments.m3),
        struct.pack("<d", moments.m4),
        struct.pack("<d", float(folds.downside_sum_sq)),
        struct.pack("<d", folds.p95),
        struct.pack("<d", folds.p5),
    )


def _resolve_kernel() -> Callable[..., object] | None:
    """Return ``fno_math.metrics_fold_trades``, or ``None`` with a warn-once.

    The import is LAZY and in-function so this module — and the whole
    ``brutex.metrics`` package — imports cleanly with NO wheel present
    (the metrics surface must never hard-require the kernel). A missing
    wheel (ImportError, including the shim's own stale-wheel handshake
    refusal, or the AttributeError a partially initialised module can
    raise) degrades as ``wheel_missing``; an importable wheel missing the
    symbol degrades as ``wheel_stale`` (the ``feed_rust`` taxonomy).
    """
    try:
        import fno_math
    except (ImportError, AttributeError):
        warn_fallback_once("wheel_missing")
        return None
    kernel: Callable[..., object] | None = getattr(fno_math, "metrics_fold_trades", None)
    if kernel is None:
        warn_fallback_once("wheel_stale")
        return None
    return kernel


def _is_int(value: object) -> bool:
    """Return ``True`` iff *value* is a genuine int (bools refused)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_fold_wire(result: object, n_trades: int) -> bool:
    """Refuse-poison gate over a kernel result (the ``rust_metrics`` precedent).

    Structural laws (proven for every well-formed input by the crate's own
    unit suite — a violation here can only mean a stale/corrupt wheel):
    the four nested wire tuples at their frozen arities; every integer slot
    a genuine int (bools refused); every float slot a float; the counts
    consistent with ``n_trades`` (``winners + losers <= n``, streaks in
    ``[0, n]``, ``moments.n == n``). A result inside this domain that is
    nonetheless WRONG is the burn-in + golden suites' layer to catch.
    """
    if not isinstance(result, tuple) or len(result) != _WIRE_ARITY:
        return False
    counts, sums, moments, floats = result
    if not (
        isinstance(counts, tuple)
        and len(counts) == _COUNTS_ARITY
        and isinstance(sums, tuple)
        and len(sums) == _SUMS_ARITY
        and isinstance(moments, tuple)
        and len(moments) == _MOMENTS_ARITY
        and isinstance(floats, tuple)
        and len(floats) == _FLOATS_ARITY
    ):
        return False
    if not all(_is_int(v) for v in counts) or not all(_is_int(v) for v in sums):
        return False
    kernel_n, winners, losers, max_holding, largest_loss, max_l, max_w = counts
    if kernel_n != n_trades:
        return False
    if not (0 <= winners <= n_trades and 0 <= losers <= n_trades):
        return False
    if winners + losers > n_trades:
        return False
    if not (0 <= max_l <= n_trades and 0 <= max_w <= n_trades):
        return False
    if max_holding < 1 or largest_loss > 0:
        return False
    if not _is_int(moments[0]) or moments[0] != n_trades:
        return False
    if not all(isinstance(v, float) for v in moments[1:]):
        return False
    return all(isinstance(v, float) for v in floats)


def fold_trades_via_kernel(
    trades: Sequence[ClosedTrade], start_equity_paisa: int
) -> TradeFolds | None:
    """Run the Rust metrics FOLD; ``None`` ⇒ serve the row from Python.

    The ONE FFI call site. Returns a :class:`TradeFolds` bit-identical to
    ``_fold_trades(trades, start_equity_paisa)`` on success (module
    docstring carries the exactness argument; the burn-in self-check
    verifies the first :data:`SELF_CHECK_SAMPLES` per process). Returns
    ``None`` — after a warn-once (or, for a refused result, an ERROR) —
    when the kernel is unavailable, raises, or returns a structurally
    invalid wire tuple; the caller then serves the row through the
    bit-exact Python oracle, so wheel state can never change results.
    ``MemoryError`` re-raises RAW (the house OOM law); every OTHER
    kernel-call exception degrades (the greeks-batch autorevert-fence
    taxonomy — module docstring). Callers guarantee ``trades`` is
    non-empty and ``start_equity_paisa > 0`` (the ``_fold_trades``
    contract — the zero-trade row is ``_empty_metrics``, the caller's arm).
    """
    if kernel_disabled():
        warn_fallback_once("kernel_disabled")
        return None
    kernel = _resolve_kernel()
    if kernel is None:
        return None
    # Lazy import — `compute` imports THIS module at module scope, so the
    # reverse edge must be function-local (fully initialised by call time).
    from brutex.metrics.compute import Moments, TradeFolds, _fold_trades

    # The verbatim `_fold_trades` returns expression — CPython's correctly
    # rounded int/int division stays Python-side (module docstring).
    returns = [t.pnl_paisa / start_equity_paisa for t in trades]
    try:
        result = kernel(
            [t.pnl_paisa for t in trades],
            [t.mfe_paisa for t in trades],
            [t.mae_paisa for t in trades],
            [t.holding_bars for t in trades],
            returns,
        )
    except MemoryError:
        raise  # house law: MemoryError re-raises raw (never absorbed)
    except Exception as exc:  # the autorevert-fence taxonomy: ANY other
        # kernel-call fault (PyO3 conversion TypeError/OverflowError, the
        # kernel's boundary ValueError, a poisoned-wheel raise) degrades to
        # the bit-exact oracle — never a raise the Python arm would not
        # have produced (the greeks-batch precedent).
        warn_fallback_once("kernel_error", error=repr(exc))
        return None
    if not _valid_fold_wire(result, len(trades)):
        # A structurally invalid result is a kernel BUG: refuse it LOUDLY
        # (never silently adopted) and serve the row from the oracle.
        if "kernel_result_refused" not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add("kernel_result_refused")
            _LOG.error(
                "metrics_rust_fold.kernel_result_refused",
                result=repr(result),
                n_trades=len(trades),
            )
        return None
    wire: tuple[Any, ...] = result
    counts, sums, moments_wire, floats = wire
    folds = TradeFolds(
        n_trades=counts[0],
        net_pnl_paisa=sums[0],
        gross_profit_paisa=sums[1],
        gross_loss_abs_paisa=sums[2],
        winners=counts[1],
        losers=counts[2],
        sum_mfe_paisa=sums[3],
        sum_mae_paisa=sums[4],
        sum_holding_bars=sums[5],
        max_holding_bars=counts[3],
        largest_loss_paisa=counts[4],
        max_consecutive_losses=counts[5],
        max_consecutive_wins=counts[6],
        moments=Moments(
            n=moments_wire[0],
            mean=moments_wire[1],
            m2=moments_wire[2],
            m3=moments_wire[3],
            m4=moments_wire[4],
        ),
        downside_sum_sq=floats[0],
        p95=floats[1],
        p5=floats[2],
    )
    if self_check_pending():
        oracle = _fold_trades(trades, start_equity_paisa)
        if fold_compare_key(folds) != fold_compare_key(oracle):
            # SHIP THE ORACLE VALUE (correctness first) + latch the kernel
            # off for the process — the #1531/#1532 autorevert law.
            disable_kernel("burn_in_mismatch", n_trades=len(trades))
            return oracle
        consume_self_check_sample()
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info("metrics_rust_fold.engaged", n_trades=len(trades))
    return folds


__all__ = [
    "METRICS_RUST_FOLD_DEFAULT",
    "METRICS_RUST_FOLD_ENV",
    "SELF_CHECK_SAMPLES",
    "consume_self_check_sample",
    "disable_kernel",
    "fold_compare_key",
    "fold_trades_via_kernel",
    "kernel_disabled",
    "metrics_rust_fold_enabled",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "self_check_pending",
    "warn_fallback_once",
]
