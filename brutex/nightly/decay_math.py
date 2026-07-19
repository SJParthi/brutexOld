"""Decay / champion verdict math — the born-Rust kernel seam (LANE W5).

DEC-NIGHTLY-DECAY-R48-001 + DEC-CHAMPION-CHALLENGER-001. The nightly
continuous-adaptation legs (:mod:`brutex.nightly.decay`,
:mod:`brutex.nightly.champions`) compute three pieces of verdict math:

1. the **trailing fold** — population-std Sharpe (zero risk-free, no
   annualisation — the :func:`brutex.metrics.cpcv.sharpe_of_returns`
   FORMULA) + the largest peak-to-trough decline of the cumulative-return
   equity path (the lockbox ``_max_drawdown`` law) + the net, over one
   per-trade return series;
2. the **R48 decay verdict** under the LOCKBOX tolerance law
   (`DEC-LOCK-001` — imported constants, never re-typed): DECAYED iff
   ``trailing_sharpe < (1 - DECAY_SHARPE_TOLERANCE) × discovery_sharpe``
   (the 0.70× floor) OR ``trailing_max_dd > (1 + DECAY_MDD_TOLERANCE) ×
   discovery_max_dd`` (the 1.50× ceiling), with honest ABSTAIN outcomes at
   low N / undefined metrics that are NEVER a decay claim;
3. the **champion domination compare** — challenger strictly higher
   lockbox Sharpe AND max-DD no worse.

Per the BORN-RUST rule (rust-o1-totality-law, 2026-07-18) the math is
implemented in ``rust/fno-math/src/decay.rs`` and exposed through the
``fno_math`` wheel; this module is the seam: env-flag resolution, the
verbatim Python ORACLE (the live parity fallback), the per-process burn-in
bit-compare with the ship-oracle autorevert latch (the
DEC-SWEEP-EXIT-INDEX-FASTPATH-001 pattern), and warn-once fallback logging.

## The flag — ``BRUTEX_DECAY_RUST`` (default ON)

Resolved per call via the COMMON
:func:`brutex.engine.sweep_lookahead.env_flag_tristate` (CLAUDE.md §11 —
never a copied vocabulary): a truthy literal forces ON, a falsy literal is
the explicit operator kill switch, unset/empty falls to
:data:`DECAY_RUST_DEFAULT` (ON), and an UNRECOGNISED value fails SAFE to
OFF with one warn-once ``decay_math.flag_unrecognised`` — under a
default-ON kernel a typo'd kill switch must still KILL (the
``rust_metrics`` law). OFF serves the verbatim Python oracle.

## Burn-in + ship-oracle autorevert

For the first :data:`SELF_CHECK_SAMPLES` kernel-served calls PER FUNCTION
per process, the oracle also runs and the results are bit-compared
(floats by ``struct.pack`` bit pattern — signed zeros + NaN patterns
included; ``None``/int/bool exact). ANY mismatch ships the ORACLE value
(correctness first), logs one ``decay_math.kernel_autoreverted`` ERROR and
latches the kernel OFF for the process. A missing/stale wheel or an
unexpected kernel exception degrades to the oracle with one warn-once
``decay_math.kernel_fallback`` per reason — never a raise the pure-Python
arm would not have produced. The latch/counters are the documented
log-volume exception to the no-global-mutable-state rule (results never
depend on them); :func:`reset_decay_math_for_tests` restores a fresh view.

## Bit-identity claim (Golden Rule #4) — STRUCTURAL

The oracle is written as PURE sequential Python loops (deliberately NOT
numpy) and the Rust kernel transcribes it operation-for-operation, so
equal IEEE op order ⇒ equal bits (the ``stats::cpcv`` argument). HONEST
divergence note: :func:`brutex.metrics.cpcv.sharpe_of_returns` computes
the same formula with numpy PAIRWISE reductions, so this fold can differ
from it by ULPs at large n — immaterial to a 30%-tolerance verdict and
disclosed here rather than hidden. The lockbox tolerance constants
themselves are IMPORTED (:data:`DECAY_SHARPE_TOLERANCE` /
:data:`DECAY_MDD_TOLERANCE`), never re-minted.

## Honest envelope (Golden Rule #10)

The fold is ``O(n)`` in the trailing trade count (tens–hundreds — trivial
on the nightly path); the verdict/domination compares are ``O(1)``. The
FFI copies one float list per fold call. NO speedup is claimed — the
kernel exists because the BORN-RUST rule requires new verdict math to be
born in Rust, with the oracle retained as the live parity fallback.
"""

from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.metrics.lockbox import DEFAULT_MDD_TOLERANCE, DEFAULT_SHARPE_TOLERANCE

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

logger = structlog.get_logger(__name__)

__all__ = [
    "DECAY_MDD_TOLERANCE",
    "DECAY_MIN_TRADES",
    "DECAY_MIN_TRADES_ENV",
    "DECAY_RUST_DEFAULT",
    "DECAY_RUST_ENV",
    "DECAY_SHARPE_TOLERANCE",
    "SELF_CHECK_SAMPLES",
    "VERDICT_ABSTAIN_LOW_N",
    "VERDICT_ABSTAIN_UNDEFINED",
    "VERDICT_DECAYED",
    "VERDICT_HEALTHY",
    "TrailingFold",
    "champion_dominates",
    "decay_rust_enabled",
    "decay_verdict_token",
    "reset_decay_math_for_tests",
    "resolve_decay_min_trades",
    "trailing_fold",
]

#: The kill-switch env flag (default ON — the BORN-RUST kernel is the
#: production arm; a falsy literal serves the Python oracle).
DECAY_RUST_ENV: Final[str] = "BRUTEX_DECAY_RUST"

#: The ONE default constant (the ADR-0017 one-constant-revert idiom).
DECAY_RUST_DEFAULT: Final[bool] = True

#: The decay Sharpe tolerance — the LOCKBOX constant, imported not re-typed
#: (DEC-LOCK-001; the 0.70× discovery floor).
DECAY_SHARPE_TOLERANCE: Final[float] = DEFAULT_SHARPE_TOLERANCE

#: The decay max-DD tolerance — the LOCKBOX constant (the 1.50× ceiling).
DECAY_MDD_TOLERANCE: Final[float] = DEFAULT_MDD_TOLERANCE

#: W5-DEFAULT low-N abstain floor (standing in for an un-ruled operator
#: value — the W1 defaults precedent, disclosed in the ledger): a trailing
#: window holding fewer trades ABSTAINS (never a DECAYED verdict). The
#: DEC-LOCK-001 500-trade lockbox floor would abstain EVERY ~63-session
#: trailing window, so a dedicated smaller floor is required; 10 trades is
#: the smallest count at which a 30%-tolerance Sharpe compare says anything.
DECAY_MIN_TRADES: Final[int] = 10

#: Fail-loud env override of :data:`DECAY_MIN_TRADES`.
DECAY_MIN_TRADES_ENV: Final[str] = "BRUTEX_DECAY_MIN_TRADES"

#: Per-process burn-in sample count per kernel function (the
#: DEC-SWEEP-EXIT-INDEX-FASTPATH-001 / W1 rank_select shape).
SELF_CHECK_SAMPLES: Final[int] = 16

#: Verdict tokens (the wire vocabulary of decay-diff.json + the report
#: block). Order matches the kernel's u8 codes 0..3.
VERDICT_HEALTHY: Final[str] = "healthy"
VERDICT_DECAYED: Final[str] = "decayed"
VERDICT_ABSTAIN_LOW_N: Final[str] = "abstain_low_n"
VERDICT_ABSTAIN_UNDEFINED: Final[str] = "abstain_undefined"

_VERDICT_BY_CODE: Final[tuple[str, str, str, str]] = (
    VERDICT_HEALTHY,
    VERDICT_DECAYED,
    VERDICT_ABSTAIN_LOW_N,
    VERDICT_ABSTAIN_UNDEFINED,
)

#: A Sharpe needs at least this many samples (the cpcv ``_MIN_SAMPLES`` law).
_MIN_SAMPLES: Final[int] = 2


@dataclass(frozen=True, slots=True)
class TrailingFold:
    """One trailing window's fold: Sharpe (or ``None``), max-DD, net, count.

    ``sharpe`` / ``max_drawdown`` / ``net`` are in RETURN units (per-trade
    ``pnl_paisa / start_equity_paisa``); ``n_trades`` is the series length.
    """

    sharpe: float | None
    max_drawdown: float
    net: float
    n_trades: int


# ── module state (log-volume + latch only — results never depend on it) ──

_kernel_disabled: bool = False
_fallback_warned: set[str] = set()
_self_check_remaining: dict[str, int] = {}


def reset_decay_math_for_tests() -> None:
    """Restore a fresh per-process view (latch + memos) for test isolation."""
    global _kernel_disabled  # noqa: PLW0603 — the documented reset hook
    _kernel_disabled = False
    _fallback_warned.clear()
    _self_check_remaining.clear()


def decay_rust_enabled() -> bool:
    """Resolve ``BRUTEX_DECAY_RUST`` per call (truthy ON / falsy OFF / unset
    default-ON / unrecognised fail-safe OFF with one warn)."""
    raw = os.environ.get(DECAY_RUST_ENV)
    if raw is None or not raw.strip():
        return DECAY_RUST_DEFAULT
    verdict = env_flag_tristate(raw)
    if verdict is None:
        _warn_once(
            "flag_unrecognised",
            detail=f"{DECAY_RUST_ENV}={raw!r} is not a recognised literal — kernel OFF",
        )
        return False
    return verdict


def resolve_decay_min_trades() -> int:
    """Resolve the low-N abstain floor (env override, fail-loud on junk).

    Raises
    ------
    ValueError
        If ``BRUTEX_DECAY_MIN_TRADES`` is set to a non-integer or a value
        ``< 1`` — a junk floor must never silently judge (the fail-loud
        tristate convention; the nightly leg catches this into its honest
        ``env_error`` block, never a chain break).
    """
    raw = os.environ.get(DECAY_MIN_TRADES_ENV)
    if raw is None or not raw.strip():
        return DECAY_MIN_TRADES
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"{DECAY_MIN_TRADES_ENV}={raw!r} is not an integer — set a whole number >= 1"
        ) from exc
    if value < 1:
        raise ValueError(f"{DECAY_MIN_TRADES_ENV}={raw!r} must be >= 1")
    return value


# ── the Python oracle (the live parity fallback — sequential by design) ──


def _oracle_trailing_fold(returns: Sequence[float]) -> tuple[float | None, float, float]:
    """Sequential ``(sharpe, max_drawdown, net)`` — the kernel's op-order twin."""
    n = len(returns)
    total = 0.0
    for r in returns:
        total += r
    sharpe: float | None
    if n < _MIN_SAMPLES:
        sharpe = None
    else:
        mean = total / n
        sq_sum = 0.0
        for r in returns:
            delta = r - mean
            sq_sum += delta * delta
        variance = sq_sum / n
        sharpe = (
            None if not math.isfinite(variance) or variance <= 0.0 else mean / math.sqrt(variance)
        )
    equity = 0.0
    peak = float("-inf")
    max_dd = 0.0
    for r in returns:
        equity += r
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
    return sharpe, max_dd, total


def _validate_verdict_inputs(
    discovery_sharpe: float,
    discovery_max_dd: float,
    sharpe_tolerance: float,
    mdd_tolerance: float,
) -> None:
    """The shared boundary validation (mirrors the kernel's error law)."""
    if not math.isfinite(sharpe_tolerance) or sharpe_tolerance < 0.0:
        raise ValueError(
            f"decay_verdict: sharpe_tolerance must be finite and >= 0, got {sharpe_tolerance}"
        )
    if not math.isfinite(mdd_tolerance) or mdd_tolerance < 0.0:
        raise ValueError(
            f"decay_verdict: mdd_tolerance must be finite and >= 0, got {mdd_tolerance}"
        )
    if not math.isfinite(discovery_sharpe):
        raise ValueError(f"decay_verdict: discovery_sharpe must be finite, got {discovery_sharpe}")
    if not math.isfinite(discovery_max_dd) or discovery_max_dd < 0.0:
        raise ValueError(
            f"decay_verdict: discovery_max_dd must be finite and >= 0, got {discovery_max_dd}"
        )


def _oracle_decay_verdict(
    trailing_sharpe: float | None,
    discovery_sharpe: float,
    trailing_max_dd: float,
    discovery_max_dd: float,
    n_trades: int,
    min_trades: int,
    sharpe_tolerance: float,
    mdd_tolerance: float,
) -> int:
    """The verdict-code oracle (inputs pre-validated by the public face)."""
    if n_trades < min_trades:
        return 2
    if trailing_sharpe is None or not math.isfinite(trailing_sharpe):
        return 3
    if not math.isfinite(trailing_max_dd):
        return 3
    sharpe_floor = discovery_sharpe * (1.0 - sharpe_tolerance)
    mdd_ceiling = discovery_max_dd * (1.0 + mdd_tolerance)
    if trailing_sharpe < sharpe_floor or trailing_max_dd > mdd_ceiling:
        return 1
    return 0


def _oracle_champion_dominates(
    challenger_sharpe: float,
    challenger_max_dd: float,
    champion_sharpe: float,
    champion_max_dd: float,
) -> bool:
    """The domination oracle (inputs pre-validated by the public face)."""
    return challenger_sharpe > champion_sharpe and challenger_max_dd <= champion_max_dd


def _validate_domination_inputs(
    challenger_sharpe: float,
    challenger_max_dd: float,
    champion_sharpe: float,
    champion_max_dd: float,
) -> None:
    """Refuse a non-finite metric (a promotion never rides an undefined one)."""
    for name, value in (
        ("challenger_sharpe", challenger_sharpe),
        ("challenger_max_dd", challenger_max_dd),
        ("champion_sharpe", champion_sharpe),
        ("champion_max_dd", champion_max_dd),
    ):
        if not math.isfinite(value):
            raise ValueError(f"champion_dominates: {name} must be finite, got {value}")


# ── kernel binding ──────────────────────────────────────────────────────


def _import_kernel() -> ModuleType:
    """Import the compiled kernel (test seam — monkeypatched by the suite)."""
    import fno_math

    return fno_math


def _kernel_or_none(fn: str) -> ModuleType | None:
    """The kernel module iff the flag is ON, not latched, and importable."""
    if _kernel_disabled or not decay_rust_enabled():
        return None
    try:
        return _import_kernel()
    except ImportError as exc:
        _warn_once("wheel_missing", detail=f"{fn}: fno_math wheel unavailable: {exc}")
        return None


def _warn_once(reason: str, *, detail: str) -> None:
    """One ``decay_math.kernel_fallback``-family WARNING per reason per process."""
    if reason in _fallback_warned:
        return
    _fallback_warned.add(reason)
    event = (
        "decay_math.flag_unrecognised"
        if reason == "flag_unrecognised"
        else "decay_math.kernel_fallback"
    )
    logger.warning(event, reason=reason, detail=detail)


def _disable_kernel(fn: str, *, kernel: object, oracle: object) -> None:
    """Latch the kernel OFF for the process (ship-oracle autorevert)."""
    global _kernel_disabled  # noqa: PLW0603 — the documented autorevert latch
    _kernel_disabled = True
    logger.error(
        "decay_math.kernel_autoreverted",
        fn=fn,
        kernel_result=repr(kernel),
        oracle_result=repr(oracle),
        detail="kernel != oracle bit pattern — oracle shipped, kernel latched OFF",
    )


def _bits(value: float | None) -> bytes | None:
    """Bit pattern of a float (``None`` passes through) — signed-zero exact."""
    return None if value is None else struct.pack("<d", value)


def _fold_bits(
    result: tuple[float | None, float, float],
) -> tuple[bytes | None, bytes | None, bytes | None]:
    """The fold triple's bit-compare key."""
    sharpe, max_dd, net = result
    return _bits(sharpe), _bits(max_dd), _bits(net)


def _self_check_due(fn: str) -> bool:
    """Consume one burn-in sample for *fn*; True while samples remain."""
    remaining = _self_check_remaining.get(fn, SELF_CHECK_SAMPLES)
    if remaining <= 0:
        return False
    _self_check_remaining[fn] = remaining - 1
    return True


# ── public faces ────────────────────────────────────────────────────────


def trailing_fold(returns: Sequence[float]) -> TrailingFold:
    """The trailing window fold (kernel-first, oracle parity fallback)."""
    values = [float(r) for r in returns]
    kernel = _kernel_or_none("trailing_fold")
    if kernel is None:
        sharpe, max_dd, net = _oracle_trailing_fold(values)
        return TrailingFold(sharpe=sharpe, max_drawdown=max_dd, net=net, n_trades=len(values))
    try:
        result = kernel.decay_trailing_fold(values)
    except Exception as exc:
        _warn_once("kernel_error_trailing_fold", detail=f"decay_trailing_fold raised: {exc!r}")
        sharpe, max_dd, net = _oracle_trailing_fold(values)
        return TrailingFold(sharpe=sharpe, max_drawdown=max_dd, net=net, n_trades=len(values))
    if _self_check_due("trailing_fold"):
        oracle = _oracle_trailing_fold(values)
        if _fold_bits(tuple(result)) != _fold_bits(oracle):
            _disable_kernel("trailing_fold", kernel=result, oracle=oracle)
            sharpe, max_dd, net = oracle
            return TrailingFold(sharpe=sharpe, max_drawdown=max_dd, net=net, n_trades=len(values))
    sharpe, max_dd, net = result
    return TrailingFold(sharpe=sharpe, max_drawdown=max_dd, net=net, n_trades=len(values))


def decay_verdict_token(
    *,
    trailing_sharpe: float | None,
    discovery_sharpe: float,
    trailing_max_dd: float,
    discovery_max_dd: float,
    n_trades: int,
    min_trades: int,
    sharpe_tolerance: float = DECAY_SHARPE_TOLERANCE,
    mdd_tolerance: float = DECAY_MDD_TOLERANCE,
) -> str:
    """The R48 decay verdict token (kernel-first, oracle parity fallback).

    Raises
    ------
    ValueError
        On a non-finite/negative tolerance, non-finite ``discovery_sharpe``,
        non-finite/negative ``discovery_max_dd``, or ``min_trades < 1`` —
        validated HERE (shared boundary), so kernel and oracle only ever see
        pre-validated inputs and cannot diverge on the error law.
    """
    if min_trades < 1:
        raise ValueError(f"decay_verdict: min_trades must be >= 1, got {min_trades}")
    _validate_verdict_inputs(discovery_sharpe, discovery_max_dd, sharpe_tolerance, mdd_tolerance)
    args = (
        trailing_sharpe,
        discovery_sharpe,
        trailing_max_dd,
        discovery_max_dd,
        n_trades,
        min_trades,
        sharpe_tolerance,
        mdd_tolerance,
    )
    kernel = _kernel_or_none("decay_verdict")
    if kernel is None:
        return _VERDICT_BY_CODE[_oracle_decay_verdict(*args)]
    try:
        code = int(kernel.decay_verdict(*args))
    except Exception as exc:
        _warn_once("kernel_error_decay_verdict", detail=f"decay_verdict raised: {exc!r}")
        return _VERDICT_BY_CODE[_oracle_decay_verdict(*args)]
    if _self_check_due("decay_verdict"):
        oracle_code = _oracle_decay_verdict(*args)
        if code != oracle_code:
            _disable_kernel("decay_verdict", kernel=code, oracle=oracle_code)
            return _VERDICT_BY_CODE[oracle_code]
    if not 0 <= code < len(_VERDICT_BY_CODE):
        # Refuse-poison: an out-of-vocabulary kernel code is a kernel BUG —
        # latch + serve the oracle, never adopt silently.
        oracle_code = _oracle_decay_verdict(*args)
        _disable_kernel("decay_verdict", kernel=code, oracle=oracle_code)
        return _VERDICT_BY_CODE[oracle_code]
    return _VERDICT_BY_CODE[code]


def champion_dominates(
    *,
    challenger_sharpe: float,
    challenger_max_dd: float,
    champion_sharpe: float,
    champion_max_dd: float,
) -> bool:
    """The domination compare (kernel-first, oracle parity fallback).

    Raises
    ------
    ValueError
        On any non-finite input (shared boundary validation).
    """
    _validate_domination_inputs(
        challenger_sharpe, challenger_max_dd, champion_sharpe, champion_max_dd
    )
    args = (challenger_sharpe, challenger_max_dd, champion_sharpe, champion_max_dd)
    kernel = _kernel_or_none("champion_dominates")
    if kernel is None:
        return _oracle_champion_dominates(*args)
    try:
        result = bool(kernel.champion_dominates(*args))
    except Exception as exc:
        _warn_once("kernel_error_champion_dominates", detail=f"champion_dominates raised: {exc!r}")
        return _oracle_champion_dominates(*args)
    if _self_check_due("champion_dominates"):
        oracle = _oracle_champion_dominates(*args)
        if result is not oracle:
            _disable_kernel("champion_dominates", kernel=result, oracle=oracle)
            return oracle
    return result
