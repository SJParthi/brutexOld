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
``_TRUTHY`` value forces ON; a ``_FALSY`` value forces OFF (the kill
switch);
unset/whitespace-empty falls to :data:`METRICS_RUST_FOLD_DEFAULT` (``True``
— ON by default per DEC-RUST-DEFAULT-ON-WAVE-001, operator directive
2026-07-23; the revert is that one constant); any OTHER value fails
SAFE to OFF with one warn-once ``flag_unrecognised`` event. OFF keeps the
caller on the verbatim Python ``_fold_trades`` path — byte-identical by
construction (the never-routes bomb-kernel spy pins it).

LEAF DISCIPLINE (verified import direction — CodeQL py/cyclic-import
clean STRUCTURALLY, the #1533 lake_revision precedent): this module is a
PURE LEAF — it imports NOTHING from the metrics compute module in ANY form
(no module-level import, no TYPE_CHECKING import, no lazy in-function
import; the plain-data :data:`FoldWire` boundary + the INJECTED
``oracle_wire`` callable carry every cross-module need, so the only import
edge is the consumer's own lazy ``from brutex.metrics import compute_rust``
inside its ``_resolve_folds`` dispatch — one direction, no cycle for CodeQL
to flag). ``brutex.engine`` (whose ``__init__`` pulls ``sweep_driver`` →
the metrics compute module) must also stay importable — so this module must
NOT import ``env_flag_tristate`` from ``brutex.engine.sweep_lookahead`` (a
metrics→engine edge would both close an import cycle and make a plain
metrics import transitively hard-require the ``fno_math`` wheel via
``brutex/engine/__init__``). It carries its own inline ``_TRUTHY`` /
``_FALSY`` vocabulary — the ``brutex.feed_rust`` leaf precedent —
mirror-pinned against ``sweep_lookahead``'s sets by a test. The one type it
needs for annotations (``ClosedTrade``) comes from the already-leaf
``brutex.metrics.types``.

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
:func:`fold_wire_via_kernel` ALSO evaluates the INJECTED ``oracle_wire``
callable (the consumer passes its own Python-fold wire projection in — the
dependency-inversion half of the plain-data boundary) and bit-compares the
two :data:`FoldWire` tuples (:func:`wire_compare_key` — ints exact, floats
by ``struct.pack`` bit pattern, signed zeros included; ``downside_sum_sq``
rides a ``float(...)`` belt because the Python fold legitimately holds the
int ``0`` on an empty downside, float-normalised by the consumer's wire
projection — ``0 / n`` and ``0.0 / n`` are the same ``+0.0`` combine-side,
pinned). A mismatch SHIPS THE ORACLE VALUE (correctness first
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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

    from brutex.metrics.types import ClosedTrade

_LOG = structlog.get_logger("brutex.metrics.compute_rust")

#: The environment flag (truthy forces ON, falsy is the explicit kill
#: switch; unset falls to the default — ON per
#: DEC-RUST-DEFAULT-ON-WAVE-001).
METRICS_RUST_FOLD_ENV: Final[str] = "BRUTEX_METRICS_RUST_FOLD"

#: The ONE default-arm constant: what :func:`metrics_rust_fold_enabled`
#: returns when the flag is unset or whitespace-empty. ``True`` — ON by
#: default per DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive
#: 2026-07-23, the DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 precedent); the
#: revert is this one constant.
METRICS_RUST_FOLD_DEFAULT: Final[bool] = True

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
_RECENCY_WIRE_ARITY: Final[int] = 2
_BUCKET_PAIR_ARITY: Final[int] = 2
_COUNTS_ARITY: Final[int] = 7
_SUMS_ARITY: Final[int] = 6
_MOMENTS_ARITY: Final[int] = 5
_FLOATS_ARITY: Final[int] = 3

#: The PLAIN-DATA fold boundary (the structural CodeQL py/cyclic-import
#: fix): the seam speaks ONLY these tuples — `(n_trades, winners, losers,
#: max_holding_bars, largest_loss_paisa, max_consecutive_losses,
#: max_consecutive_wins)` · the six exact integer sums `(net_pnl,
#: gross_profit, gross_loss_abs, sum_mfe, sum_mae, sum_holding)` · the
#: `(n, mean, m2, m3, m4)` moments · the `(downside_sum_sq, p95, p5)`
#: floats — and the consumer (`_resolve_folds` in the metrics compute
#: module) owns the `TradeFolds` construction, so this leaf imports
#: NOTHING from that module in ANY form.
FoldCounts = tuple[int, int, int, int, int, int, int]
FoldSums = tuple[int, int, int, int, int, int]
FoldMomentsWire = tuple[int, float, float, float, float]
FoldFloats = tuple[float, float, float]
FoldWire = tuple[FoldCounts, FoldSums, FoldMomentsWire, FoldFloats]

#: The recency pass's plain-data wire (DEC-METRICS-TRAILING-WINDOW-001
#: Stage-C2 — the ``fold_recency`` twin's boundary): the trailing window's
#: :data:`FoldWire` (``None`` when zero trades exit inside the window) plus
#: the exact-integer ``(age_month_bucket, net_pnl_paisa)`` pairs sorted
#: ascending by key. Every slot is an int (or the trailing ``FoldWire``'s
#: mixed tuples), so the burn-in compare is exact ``==`` on the buckets and
#: :func:`wire_compare_key` on the trailing half.
RecencyBuckets = tuple[tuple[int, int], ...]
RecencyWire = tuple[FoldWire | None, RecencyBuckets]

#: The one epoch basis every µs instant this seam ships shares —
#: correctness reads only DIFFERENCES of the instants (the window compare
#: + the bucket key), but a single helper keeps anchor/cutoff/exit on one
#: basis by construction (the C2 brief's epoch-basis watch item).
_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_US: Final[timedelta] = timedelta(microseconds=1)

#: Self-check samples still to run this process (single-cell mutable — the
#: documented no-global-mutable-state exception, module docstring).
_SELF_CHECK_REMAINING: Final[list[int]] = [SELF_CHECK_SAMPLES]

#: The recency wire face's OWN burn-in budget (each kernel face burns in
#: its first :data:`SELF_CHECK_SAMPLES` served calls independently — a
#: fold-face pass must not exhaust the recency face's compare budget); the
#: autorevert LATCH is SHARED (:func:`disable_kernel` — a mismatch on
#: either face disables the whole kernel for the process, the #1605
#: pattern).
_RECENCY_SELF_CHECK_REMAINING: Final[list[int]] = [SELF_CHECK_SAMPLES]

#: The autorevert latch: the mismatch reason once the kernel is disabled
#: for this process, else ``None`` (same documented exception).
_KERNEL_DISABLED_REASON: Final[list[str | None]] = [None]

#: Once-per-process-per-reason fallback memo + the engaged-once latch.
#: LOG-VOLUME state ONLY: results never depend on it.
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The recency face's own engaged-once memo key (each face logs its first
#: served call independently — a fold-face engage must not mute the
#: recency face's evidence).
_RECENCY_ENGAGED_KEY: Final[str] = "__recency_engaged__"


def metrics_rust_fold_enabled() -> bool:
    """Resolve the Rust-fold flag over :data:`METRICS_RUST_FOLD_DEFAULT`.

    * a ``_TRUTHY`` value (``1`` / ``true`` / ``yes`` / ``on``,
      case-insensitive, surrounding whitespace ignored) → ``True``;
    * a ``_FALSY`` value (``0`` / ``false`` / ``no`` / ``off``) → ``False``;
    * unset or whitespace-empty → :data:`METRICS_RUST_FOLD_DEFAULT`
      (``True`` — ON by default per DEC-RUST-DEFAULT-ON-WAVE-001; the
      one-constant revert lever);
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
        warn_fallback_once("flag_unrecognised", value=repr(raw))
        return False
    return METRICS_RUST_FOLD_DEFAULT


def warn_fallback_once(reason: str, **context: object) -> None:
    """Log ``metrics_rust_fold.fallback`` ONCE per process per *reason*.

    A SYMBOL-carrying reason dedupes per ``(reason, symbol)`` — a wheel
    stale on BOTH kernel faces (``metrics_fold_trades`` +
    ``metrics_fold_recency``) names EACH missing face once instead of
    muting the second behind the first (review-round fix); the logged
    ``reason`` token itself is unchanged. The seam runs once per metric
    row (potentially millions per sweep), so a sticky fallback reason
    would flood the logs; the first occurrence is loud (WARNING), repeats
    fall back silently. The fallback itself is bit-exact regardless (the
    Python oracle serves the row).
    """
    key = f"{reason}:{context['symbol']}" if "symbol" in context else reason
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
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
    Every subsequent :func:`fold_wire_via_kernel` call short-circuits to
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


def recency_self_check_pending() -> bool:
    """Return ``True`` while the RECENCY face still owes burn-in samples."""
    return _RECENCY_SELF_CHECK_REMAINING[0] > 0


def consume_recency_self_check_sample() -> None:
    """Consume one recency burn-in sample after a PASSED bit-compare.

    The recency twin of :func:`consume_self_check_sample` — clamped at
    zero; a FAILED compare latches :func:`disable_kernel` instead.
    """
    if _RECENCY_SELF_CHECK_REMAINING[0] > 0:
        _RECENCY_SELF_CHECK_REMAINING[0] -= 1


def reset_self_check_for_tests(*, remaining: int = SELF_CHECK_SAMPLES) -> None:
    """Reset the burn-in budgets + autorevert latch (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state for
    BOTH kernel faces (fold + recency); the default restores a
    fresh-process view. Never called from production.
    """
    _SELF_CHECK_REMAINING[0] = remaining
    _RECENCY_SELF_CHECK_REMAINING[0] = remaining
    _KERNEL_DISABLED_REASON[0] = None


def reset_observability_memo_for_tests() -> None:
    """Clear the warn-once / engaged-once memo (test isolation ONLY)."""
    _OBSERVABILITY_MEMO.clear()


def wire_compare_key(wire: FoldWire) -> tuple[object, ...]:
    """Map a :data:`FoldWire` to its BIT-exact comparison key.

    Ints compare exactly under ``==``; every float is compared by its
    IEEE-754 bit pattern (``struct.pack('<d', …)``) so a signed-zero or
    last-ulp difference can never hide behind float equality — the #1531
    burn-in discipline. ``downside_sum_sq`` is normalised through
    ``float(...)`` as a total belt: the Python oracle's ``_fold_wire``
    projection already float-normalises the ``sum([])`` int-``0`` an
    empty downside legitimately produces (combine-equivalent —
    ``0 / n == 0.0 / n == +0.0``, pinned by the golden suite's
    ``scratch_only`` corpus case), and ``float(+0.0)`` is the identity,
    so the key equates exactly that pair and nothing else.
    """
    counts, sums, moments, floats = wire
    return (
        *counts,
        *sums,
        moments[0],
        struct.pack("<d", moments[1]),
        struct.pack("<d", moments[2]),
        struct.pack("<d", moments[3]),
        struct.pack("<d", moments[4]),
        struct.pack("<d", float(floats[0])),
        struct.pack("<d", floats[1]),
        struct.pack("<d", floats[2]),
    )


def _resolve_kernel(symbol: str = "metrics_fold_trades") -> Callable[..., object] | None:
    """Return the named ``fno_math`` kernel symbol, or ``None`` with a warn-once.

    The import is LAZY and in-function so this module — and the whole
    ``brutex.metrics`` package — imports cleanly with NO wheel present
    (the metrics surface must never hard-require the kernel). A missing
    wheel (ImportError, including the shim's own stale-wheel handshake
    refusal, or the AttributeError a partially initialised module can
    raise) degrades as ``wheel_missing``; an importable wheel missing the
    symbol degrades as ``wheel_stale`` (the ``feed_rust`` taxonomy). Both
    kernel faces (``metrics_fold_trades`` + the Stage-C2
    ``metrics_fold_recency``) resolve through this ONE helper.
    """
    try:
        import fno_math
    except (ImportError, AttributeError):
        warn_fallback_once("wheel_missing")
        return None
    kernel: Callable[..., object] | None = getattr(fno_math, symbol, None)
    if kernel is None:
        warn_fallback_once("wheel_stale", symbol=symbol)
        return None
    return kernel


def datetime_to_epoch_us(ts: datetime) -> int:
    """Convert a tz-aware datetime to exact epoch microseconds.

    Pure integer floor division of the aware-datetime delta against the
    ONE :data:`_EPOCH_UTC` basis — exact at CPython's full datetime
    resolution (1 µs), lossless for pre-1970 instants (negative µs), and
    tz-arithmetic-free beyond the aware subtraction itself. NEVER
    ``ts.timestamp() * 1e6`` (float-lossy). Callers guarantee ``ts`` is
    tz-aware (a naive value raises the aware-vs-naive ``TypeError`` from
    the subtraction — the consumer routes naive anchors to the Python arm
    BEFORE conversion so ``fold_recency``'s own ``ValueError`` law holds).
    """
    return (ts - _EPOCH_UTC) // _ONE_US


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


def fold_wire_via_kernel(
    trades: Sequence[ClosedTrade],
    start_equity_paisa: int,
    *,
    oracle_wire: Callable[[], FoldWire],
) -> FoldWire | None:
    """Run the Rust metrics FOLD; ``None`` ⇒ serve the row from Python.

    The ONE FFI call site. Returns a :data:`FoldWire` bit-identical to the
    Python fold's wire projection on success (module docstring carries the
    exactness argument; the burn-in self-check verifies the first
    :data:`SELF_CHECK_SAMPLES` per process against the INJECTED
    ``oracle_wire`` — the dependency-INVERSION half of the plain-data
    boundary: the consumer passes its own oracle in, so this leaf never
    imports the metrics compute module in any form, at any line). Returns
    ``None`` — after a warn-once (or, for a refused result, an ERROR) —
    when the kernel is unavailable, raises, or returns a structurally
    invalid wire tuple; the caller then serves the row through the
    bit-exact Python oracle, so wheel state can never change results.
    ``MemoryError`` re-raises RAW (the house OOM law); every OTHER
    kernel-call exception degrades (the greeks-batch autorevert-fence
    taxonomy — module docstring). Callers guarantee ``trades`` is
    non-empty, ``start_equity_paisa > 0``, and that ``oracle_wire``
    computes the Python fold's wire for EXACTLY these arguments (the
    burn-in's correctness anchor — a wrong oracle would autorevert a
    correct kernel, never serve a wrong number).
    """
    if kernel_disabled():
        warn_fallback_once("kernel_disabled")
        return None
    kernel = _resolve_kernel()
    if kernel is None:
        return None
    # The verbatim Python-fold returns expression — CPython's correctly
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
    # The structural gate above proves the shape; narrow for the type-checkers.
    wire = cast("FoldWire", result)
    if self_check_pending():
        oracle = oracle_wire()
        if wire_compare_key(wire) != wire_compare_key(oracle):
            # SHIP THE ORACLE VALUE (correctness first) + latch the kernel
            # off for the process — the #1531/#1532 autorevert law.
            disable_kernel("burn_in_mismatch", n_trades=len(trades))
            return oracle
        consume_self_check_sample()
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info("metrics_rust_fold.engaged", n_trades=len(trades))
    return wire


def recency_wire_compare_key(wire: RecencyWire) -> tuple[object, ...]:
    """Map a :data:`RecencyWire` to its BIT-exact comparison key.

    The trailing half rides :func:`wire_compare_key` (``None`` compares
    only to ``None``); the decay buckets are exact int pairs, so plain
    tuple equality IS bit equality — no float normalisation exists on that
    half, and the sorted-emit law makes the compare order-sensitive by
    construction.
    """
    trailing, buckets = wire
    return (None if trailing is None else wire_compare_key(trailing), buckets)


def _valid_recency_wire(result: object, n_windowed: int, net_pnl_paisa: int) -> bool:
    """Refuse-poison gate over a recency kernel result.

    Structural laws (proven for every well-formed input by the crate's own
    unit suite): a 2-tuple of (trailing, buckets); the trailing half is
    ``None`` IFF zero trades exit inside the window, else a
    :func:`_valid_fold_wire`-passing wire over exactly the windowed count;
    the buckets a non-empty sequence of genuine-int 2-tuples with
    NON-NEGATIVE STRICTLY-ASCENDING keys whose values sum EXACTLY to the
    stream's net P&L (the fold is a pure integer partition — the
    conservation law the property suite pins on the Python arm).
    """
    if not isinstance(result, tuple) or len(result) != _RECENCY_WIRE_ARITY:
        return False
    trailing, buckets = result
    if n_windowed == 0:
        if trailing is not None:
            return False
    elif not _valid_fold_wire(trailing, n_windowed):
        return False
    if not isinstance(buckets, (list, tuple)) or not buckets:
        return False
    prev_key = -1
    total = 0
    for pair in buckets:
        if not (isinstance(pair, tuple) and len(pair) == _BUCKET_PAIR_ARITY):
            return False
        key, bucket_sum = pair
        if not (_is_int(key) and _is_int(bucket_sum)):
            return False
        if key <= prev_key:  # also refuses a negative first key (prev -1)
            return False
        prev_key = key
        total += bucket_sum
    return total == net_pnl_paisa


def recency_wire_via_kernel(
    trades: Sequence[ClosedTrade],
    start_equity_paisa: int,
    *,
    anchor_us: int,
    cutoff_us: int,
    oracle_wire: Callable[[], RecencyWire],
) -> RecencyWire | None:
    """Run the Rust RECENCY fold; ``None`` ⇒ serve the row from Python.

    The Stage-C2 wire face (DEC-METRICS-TRAILING-WINDOW-001) — the
    ``fold_recency`` twin of :func:`fold_wire_via_kernel`, sharing its
    whole fault taxonomy: the SAME autorevert latch (a mismatch on either
    face disables the kernel process-wide), ``MemoryError`` re-raises RAW,
    every other kernel-call exception degrades warn-once, a structurally
    invalid result is refused LOUDLY, and the burn-in bit-compares the
    first :data:`SELF_CHECK_SAMPLES` recency-served calls against the
    INJECTED ``oracle_wire`` (its OWN budget — the shared-latch /
    per-face-budget split is documented on
    :data:`_RECENCY_SELF_CHECK_REMAINING`). ``anchor_us`` / ``cutoff_us``
    are the consumer-converted epoch-µs window bounds
    (:func:`datetime_to_epoch_us` — one basis by construction); the
    returns column keeps the VERBATIM Python division (the fold face's
    exactness law) and the buckets cross as exact ints. Callers guarantee
    ``trades`` is non-empty, ``start_equity_paisa > 0``, every
    ``exit_ts`` tz-aware, and ``oracle_wire`` the Python fold's wire for
    EXACTLY these arguments.
    """
    if kernel_disabled():
        warn_fallback_once("kernel_disabled")
        return None
    kernel = _resolve_kernel("metrics_fold_recency")
    if kernel is None:
        return None
    # The verbatim Python-fold returns expression (the fold face's law) +
    # the exact epoch-µs exit instants on the shared basis.
    returns = [t.pnl_paisa / start_equity_paisa for t in trades]
    exit_us = [datetime_to_epoch_us(t.exit_ts) for t in trades]
    try:
        result = kernel(
            [t.pnl_paisa for t in trades],
            [t.mfe_paisa for t in trades],
            [t.mae_paisa for t in trades],
            [t.holding_bars for t in trades],
            returns,
            exit_us,
            anchor_us,
            cutoff_us,
        )
    except MemoryError:
        raise  # house law: MemoryError re-raises raw (never absorbed)
    except Exception as exc:  # the shared autorevert-fence taxonomy
        warn_fallback_once("kernel_error", error=repr(exc))
        return None
    n_windowed = sum(1 for us in exit_us if us >= cutoff_us)
    net_pnl_paisa = sum(t.pnl_paisa for t in trades)
    if not _valid_recency_wire(result, n_windowed, net_pnl_paisa):
        # A structurally invalid result is a kernel BUG: refuse it LOUDLY
        # (its own memo key — a fold-face refusal must not mute this one).
        if "recency_kernel_result_refused" not in _OBSERVABILITY_MEMO:
            _OBSERVABILITY_MEMO.add("recency_kernel_result_refused")
            _LOG.error(
                "metrics_rust_fold.recency_kernel_result_refused",
                result=repr(result),
                n_trades=len(trades),
            )
        return None
    # The structural gate proves the shape; normalise the kernel's bucket
    # LIST into the wire's tuple form and narrow for the type-checkers.
    trailing_raw, buckets_raw = cast("tuple[FoldWire | None, list[tuple[int, int]]]", result)
    wire: RecencyWire = (trailing_raw, tuple(buckets_raw))
    if recency_self_check_pending():
        oracle = oracle_wire()
        if recency_wire_compare_key(wire) != recency_wire_compare_key(oracle):
            # SHIP THE ORACLE VALUE + latch the SHARED kernel latch off.
            disable_kernel("recency_burn_in_mismatch", n_trades=len(trades))
            return oracle
        consume_recency_self_check_sample()
    if _RECENCY_ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_RECENCY_ENGAGED_KEY)
        _LOG.info("metrics_rust_fold.recency_engaged", n_trades=len(trades))
    return wire


__all__ = [
    "METRICS_RUST_FOLD_DEFAULT",
    "METRICS_RUST_FOLD_ENV",
    "SELF_CHECK_SAMPLES",
    "FoldWire",
    "RecencyBuckets",
    "RecencyWire",
    "consume_recency_self_check_sample",
    "consume_self_check_sample",
    "datetime_to_epoch_us",
    "disable_kernel",
    "fold_wire_via_kernel",
    "kernel_disabled",
    "metrics_rust_fold_enabled",
    "recency_self_check_pending",
    "recency_wire_compare_key",
    "recency_wire_via_kernel",
    "reset_observability_memo_for_tests",
    "reset_self_check_for_tests",
    "self_check_pending",
    "warn_fallback_once",
    "wire_compare_key",
]
