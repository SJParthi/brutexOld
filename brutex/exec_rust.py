"""Flag-gated Rust execution-cores seam (DEC-EXEC-RUST-CORE-001, MIGRATION.md #1724).

Slice 7 / P8 of the Rust conversion program: the Python side of the
``rust/fno-math/src/exec/`` integer cores — flag resolution, the i64
envelope guard, the narrow kernel-call catch with LOUD-but-flood-safe
fallback logging, and typed thin wrappers over the ``exec_``-prefixed
kernel entries. The FOUR dispatch sites (``brutex.costs.calculator.
compute_trade_cost``, ``brutex.costs.futures_costs.
compute_futures_trade_cost``, ``brutex.options.executor.materialize_trade``,
``brutex.futures.executor.materialize_futures_trade``) call these wrappers
and fall back to their verbatim Python bodies whenever a wrapper returns
``None`` — the Python bodies stay the permanent oracles (deletion is a
distinct later decision, DEC-EXEC-PY-BODY-DELETE-001).

This module is deliberately PACKAGE-NEUTRAL (top-level ``brutex.exec_rust``):
it is consumed by both the costs package and the two executors, so a home
inside either would mint an options↔costs import cycle. It imports
``brutex.costs.calculator`` for the shared :class:`ChargeStack` shape only;
the dispatch sites import THIS module lazily inside the dispatching function
(the ``materialize_trade`` lazy-import precedent), so no import cycle exists
at module-load time.

## The flag — ``BRUTEX_EXEC_RUST`` over ``EXEC_RUST_DEFAULT`` (OFF)

:func:`exec_rust_enabled` mirrors the sibling
:func:`brutex.engine.rust_metrics.sweep_rust_metrics_enabled` exactly,
classified through the COMMON PUBLIC
:func:`brutex.engine.sweep_lookahead.env_flag_tristate` (never copied —
CLAUDE.md §11): a truthy value forces ON, a falsy value forces OFF,
unset/whitespace-empty falls to :data:`EXEC_RUST_DEFAULT` = **False**
(default-OFF is the standing law for a kernel where results exist; the flip
to ``True`` is NOT this change — it is its own later coordinator decision,
DEC-EXEC-RUST-FLIP-001), and any unrecognised value fails SAFE to OFF with
one warn-once ``exec_rust.flag_unrecognised`` event. Read per CALL so spawn
workers resolve their own inherited environment (no module-global flag
state).

## The fallback law (mirrors DEC-SWEEP-RUST-METRICS-001)

* wheel absent (``_rust is None`` — also the test seam) → ``None`` (Python
  arm) with one warn-once ``exec_rust.fallback`` per reason;
* an int argument outside the i64 envelope → ``None`` with one warn-once
  ``exec_rust.envelope_fallback`` (the kernel is i64-native; Python bigints
  keep the oracle arm correct);
* the kernel call itself is caught NARROWLY —
  ``(ValueError, TypeError, OverflowError)`` → ``None`` + warn-once;
  ``MemoryError`` re-raises BARE (a global stop, never swallowed);
* a wheel that imports but LACKS an ``exec_*`` entry (stale-wheel build
  skew) raises the named :class:`StaleExecWheelError` with the rebuild
  remediation — LOUD, never a silent Python-arm fallback;
* a MALFORMED kernel result (wrong wire arity / an unknown verdict kind) is
  REFUSED loudly at :class:`ChargeStack` construction or by the caller —
  never silently adopted. Served-VALUE integrity is guarded by the
  Rust-side ``ChargeStack::validated()`` construction check plus the
  dual-arm / golden / differential parity gates; on re-materialization the
  frozen dataclasses that DEFINE ``__post_init__`` invariants
  (``TradeInput`` / ``FuturesTradeInput`` / ``FuturesCostBreakdown``) still
  run them, while ``TradeCostBreakdown`` carries NO ``__post_init__`` — the
  options-breakdown boundary check is the Rust validation + the gates, not
  a Python-side assert.

## Honest envelope (Golden Rule #10)

COLD path: the executors run per-trade loops over FINALIST trades only.
The value of the Rust arm is one-language consistency + shared-crate reuse,
NOT speed — no speedup is claimed or measured here; the OFF arm's only cost
is one env read + one function call per dispatch. The warn-once memo is
LOG-VOLUME state only (results never depend on it);
:func:`reset_warn_memo_for_tests` restores a fresh view (the documented
exception to the no-global-mutable-state rule, the rust_metrics precedent).
"""

from __future__ import annotations

import os
from types import ModuleType
from typing import Any, Final

import structlog

from brutex.costs.calculator import ChargeStack
from brutex.engine.sweep_lookahead import env_flag_tristate

__all__ = [
    "ENV_FLAG",
    "EXEC_RUST_DEFAULT",
    "StaleExecWheelError",
    "charge_stack_futures",
    "charge_stack_option",
    "exec_rust_enabled",
    "fill_futures",
    "fill_slippage",
    "materialize_futures_tail",
    "materialize_option_tail",
    "reset_warn_memo_for_tests",
]


class StaleExecWheelError(RuntimeError):
    """The installed ``fno_math`` wheel predates the ``src/exec/`` module.

    Raised (never a silent fallback) when ``BRUTEX_EXEC_RUST=1`` resolves a
    kernel module that imports fine but lacks a required ``exec_*`` entry —
    build skew between the Python tree and an old wheel. Remediation:
    rebuild the wheel (``pip install ./rust/fno-math``). The CI twin is the
    EXPECTED_BUILD.json wheel-freshness gate; this is the runtime guard.
    """


_LOG = structlog.get_logger(__name__)

#: The one-constant flip lever — OFF until the DEC-EXEC-RUST-FLIP-001
#: decision flips it (default-OFF where results exist; standing law 3).
EXEC_RUST_DEFAULT: Final[bool] = False

#: The env flag over the default (sibling of ``BRUTEX_SWEEP_RUST_METRICS``).
ENV_FLAG: Final[str] = "BRUTEX_EXEC_RUST"

#: i64 envelope bounds — the kernel is i64-native; anything outside routes
#: to the Python-bigint oracle arm.
_I64_MIN: Final[int] = -(2**63)
_I64_MAX: Final[int] = 2**63 - 1

#: Wire arity of one charge stack (the pinned :class:`ChargeStack` order).
_WIRE_LEN: Final[int] = 17


def _import_kernel() -> ModuleType | None:
    """Import the compiled kernel, or ``None`` on a wheel-free environment."""
    try:
        import fno_math
    except ImportError:  # pragma: no cover — exercised via the `_rust = None` test seam
        return None
    return fno_math


#: The kernel module — ``None`` on a wheel-free env (the type is inferred
#: from :func:`_import_kernel` — ``ModuleType | None``); monkeypatch to
#: ``None`` as the test seam (the ``rust_trade_sim`` precedent).
_rust = _import_kernel()

#: Warn-once memo — LOG-VOLUME state only, never results-bearing. Keyed on
#: the plain ``(event, reason)`` TUPLE (structural — a string-join key would
#: carry killable-looking-but-equivalent formatting mutants).
_warned: set[tuple[str, str]] = set()


def reset_warn_memo_for_tests() -> None:
    """Restore a fresh warn-once view (test isolation only)."""
    _warned.clear()


def _warn_once(event: str, *, reason: str, **kw: object) -> None:
    """Emit *event* at WARNING once per process per ``(event, reason)``."""
    key = (event, reason)
    if key in _warned:
        return
    _warned.add(key)
    _LOG.warning(event, reason=reason, **kw)


def exec_rust_enabled() -> bool:
    """Resolve ``BRUTEX_EXEC_RUST`` over :data:`EXEC_RUST_DEFAULT` per call.

    Truthy → ON; falsy → OFF (the explicit operator switch); unset or
    whitespace-empty → the default (**False** — the flip is
    DEC-EXEC-RUST-FLIP-001, not this change); any other value → fail-safe
    OFF with one warn-once ``exec_rust.flag_unrecognised``. Vocabulary via
    the COMMON :func:`~brutex.engine.sweep_lookahead.env_flag_tristate`.
    """
    raw = os.environ.get(ENV_FLAG, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once(
            "exec_rust.flag_unrecognised",
            reason="flag_unrecognised",
            flag=ENV_FLAG,
            value=raw,
            resolved="off",
        )
        return False
    return EXEC_RUST_DEFAULT


def _kernel_or_none() -> ModuleType | None:
    """The kernel iff the flag is ON and the wheel is importable, else None."""
    if not exec_rust_enabled():
        return None
    if _rust is None:
        _warn_once("exec_rust.fallback", reason="wheel_missing")
        return None
    return _rust


def _kernel_fn(rust: ModuleType, name: str) -> Any:
    """Resolve kernel entry *name*, or raise LOUDLY on a stale wheel.

    A wheel that imports but lacks the ``exec_*`` symbols predates
    ``src/exec/`` (stale-wheel build skew) — that is a deployment fault,
    not a fallback condition: raise the named :class:`StaleExecWheelError`
    with the rebuild remediation instead of the bare ``AttributeError`` a
    direct attribute access would surface (and NEVER a silent Python-arm
    fallback — ``RuntimeError`` is outside every narrow catch on the
    dispatch path, so it propagates).
    """
    fn = getattr(rust, name, None)
    if fn is None:
        raise StaleExecWheelError(
            f"fno_math wheel predates the exec module ({name} missing) "
            "— rebuild the wheel: pip install ./rust/fno-math"
        )
    return fn


def _fits_i64(*values: int) -> bool:
    """True iff every value fits the kernel's i64 envelope."""
    return all(_I64_MIN <= v <= _I64_MAX for v in values)


def _envelope_fallback(site: str) -> None:
    _warn_once("exec_rust.envelope_fallback", reason=f"envelope:{site}", site=site)


def _kernel_fallback(site: str, exc: BaseException) -> None:
    _warn_once(
        "exec_rust.kernel_fallback",
        reason=f"kernel:{site}",
        site=site,
        error=str(exc),
        error_type=type(exc).__name__,
    )


def _stack_from_wire(wire: list[int]) -> ChargeStack:
    """Re-wrap the pinned 17-slot kernel wire as the shared NamedTuple.

    A wrong-arity OPTIONS wire is a kernel CONTRACT bug — the ``TypeError``
    from the NamedTuple constructor propagates loudly (refusal, never
    silent adoption). Served-value integrity is the Rust-side
    ``ChargeStack::validated()`` check + the dual-arm/golden/differential
    gates (``TradeCostBreakdown`` has no ``__post_init__``; see the module
    docstring's fallback law).
    """
    return ChargeStack(*wire)


def fill_slippage(
    entry_open_paisa: int, exit_open_paisa: int, *, is_long: bool
) -> tuple[int, int, int] | None:
    """Kernel-served ``_apply_slippage`` twin, or ``None`` → Python arm."""
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(entry_open_paisa, exit_open_paisa):
        _envelope_fallback("fill_slippage")
        return None
    try:
        out: tuple[int, int, int] = _kernel_fn(rust, "exec_apply_slippage")(
            entry_open_paisa, exit_open_paisa, is_long
        )
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        _kernel_fallback("fill_slippage", exc)
        return None
    return out


def fill_futures(
    entry_open_paisa: int, exit_open_paisa: int, *, is_long: bool
) -> tuple[int, int, int] | None:
    """Kernel-served ``apply_futures_fill`` twin, or ``None`` → Python arm."""
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(entry_open_paisa, exit_open_paisa):
        _envelope_fallback("fill_futures")
        return None
    try:
        out: tuple[int, int, int] = _kernel_fn(rust, "exec_apply_futures_fill")(
            entry_open_paisa, exit_open_paisa, is_long
        )
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        _kernel_fallback("fill_futures", exc)
        return None
    return out


def charge_stack_option(
    entry_open_paisa: int,
    exit_open_paisa: int,
    *,
    is_long: bool,
    lot_size: int,
    n_lots: int,
    brokerage_round_trip_paisa: int,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> ChargeStack | None:
    """Kernel-served options charge stack, or ``None`` → the Python arm.

    Argument shape = ``brutex.costs.calculator._charge_stack_py`` exactly
    (rates caller-resolved; the refusal raises never reach here).
    """
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(
        entry_open_paisa,
        exit_open_paisa,
        lot_size,
        n_lots,
        brokerage_round_trip_paisa,
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    ):
        _envelope_fallback("charge_stack_option")
        return None
    try:
        wire: list[int] = _kernel_fn(rust, "exec_option_charge_stack")(
            entry_open_paisa,
            exit_open_paisa,
            is_long,
            lot_size,
            n_lots,
            brokerage_round_trip_paisa,
            stt_rate,
            etc_rate,
            sebi_rate,
            ipft_rate,
            stamp_rate,
            gst_rate,
        )
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        _kernel_fallback("charge_stack_option", exc)
        return None
    return _stack_from_wire(wire)


def charge_stack_futures(
    entry_open_paisa: int,
    exit_open_paisa: int,
    *,
    is_long: bool,
    lot_size: int,
    n_lots: int,
    cost_free: bool,
    brokerage_flat_paisa: int,
    brokerage_turnover_bps_x100: int | None,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> ChargeStack | None:
    """Kernel-served futures charge stack, or ``None`` → the Python arm.

    Argument shape = ``brutex.costs.futures_costs._futures_charge_stack_py``
    exactly (``cost_free`` and every rate/refusal decision caller-resolved).
    """
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(
        entry_open_paisa,
        exit_open_paisa,
        lot_size,
        n_lots,
        brokerage_flat_paisa,
        # None carries no envelope check (the kernel takes Option<i64>); a
        # present turnover is checked like every other int (structural
        # empty-or-one splice — never a substituted sentinel value).
        *(() if brokerage_turnover_bps_x100 is None else (brokerage_turnover_bps_x100,)),
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    ):
        _envelope_fallback("charge_stack_futures")
        return None
    try:
        wire: list[int] = _kernel_fn(rust, "exec_futures_charge_stack")(
            entry_open_paisa,
            exit_open_paisa,
            is_long,
            lot_size,
            n_lots,
            cost_free,
            brokerage_flat_paisa,
            brokerage_turnover_bps_x100,
            stt_rate,
            etc_rate,
            sebi_rate,
            ipft_rate,
            stamp_rate,
            gst_rate,
        )
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        _kernel_fallback("charge_stack_futures", exc)
        return None
    return _stack_from_wire(wire)


def materialize_option_tail(
    entry_open_paisa: int,
    exit_open_paisa: int,
    *,
    entry_volume: int,
    exit_volume: int,
    is_long: bool,
    reject_stale: bool,
    lot_size: int,
    n_lots: int,
    brokerage_round_trip_paisa: int,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> tuple[str, ChargeStack | None] | None:
    """Kernel-served options post-I/O tail, or ``None`` → the Python tail.

    Returns ``("stale_bar", None)`` / ``("tick_floor", None)`` /
    ``("trade", stack)`` — the executor maps the discriminants onto
    :class:`~brutex.options.executor.RejectReason` and emits the SAME
    structlog WARNs (identical event names / keys / cardinality).
    """
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(
        entry_open_paisa,
        exit_open_paisa,
        entry_volume,
        exit_volume,
        lot_size,
        n_lots,
        brokerage_round_trip_paisa,
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    ):
        _envelope_fallback("materialize_option_tail")
        return None
    try:
        kind, wire = _kernel_fn(rust, "exec_options_materialize")(
            entry_open_paisa,
            exit_open_paisa,
            entry_volume,
            exit_volume,
            is_long,
            reject_stale,
            lot_size,
            n_lots,
            brokerage_round_trip_paisa,
            stt_rate,
            etc_rate,
            sebi_rate,
            ipft_rate,
            stamp_rate,
            gst_rate,
        )
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        _kernel_fallback("materialize_option_tail", exc)
        return None
    if wire is None:
        return (kind, None)
    return (kind, _stack_from_wire(wire))


def materialize_futures_tail(
    entry_open_paisa: int,
    exit_open_paisa: int,
    *,
    entry_volume: int,
    exit_volume: int,
    path_highs_paisa: list[int],
    path_lows_paisa: list[int],
    is_long: bool,
    reject_stale: bool,
    lot_size: int,
    n_lots: int,
    cost_free: bool,
    brokerage_flat_paisa: int,
    brokerage_turnover_bps_x100: int | None,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> tuple[str, ChargeStack | None, int, int] | None:
    """Kernel-served futures post-I/O tail, or ``None`` → the Python tail.

    Returns ``("stale_bar", None, 0, 0)`` or
    ``("trade", stack, mae_paisa, mfe_paisa)``.
    """
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(
        entry_open_paisa,
        exit_open_paisa,
        entry_volume,
        exit_volume,
        lot_size,
        n_lots,
        brokerage_flat_paisa,
        # None carries no envelope check (Option<i64> at the kernel edge).
        *(() if brokerage_turnover_bps_x100 is None else (brokerage_turnover_bps_x100,)),
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
        *path_highs_paisa,
        *path_lows_paisa,
    ):
        _envelope_fallback("materialize_futures_tail")
        return None
    try:
        kind, wire = _kernel_fn(rust, "exec_futures_materialize")(
            entry_open_paisa,
            exit_open_paisa,
            entry_volume,
            exit_volume,
            path_highs_paisa,
            path_lows_paisa,
            is_long,
            reject_stale,
            lot_size,
            n_lots,
            cost_free,
            brokerage_flat_paisa,
            brokerage_turnover_bps_x100,
            stt_rate,
            etc_rate,
            sebi_rate,
            ipft_rate,
            stamp_rate,
            gst_rate,
        )
        if wire is None:
            return (kind, None, 0, 0)
        # Inside the narrow try DELIBERATELY (review round 1, H3-NIT-3): a
        # malformed SHORT futures wire makes ChargeStack(*wire[:17]) raise
        # TypeError → caught → warn-once fallback → the Python arm serves,
        # like every other malformed kernel shape on this site.
        return (
            kind,
            _stack_from_wire(wire[:_WIRE_LEN]),
            wire[_WIRE_LEN],
            wire[_WIRE_LEN + 1],
        )
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        _kernel_fallback("materialize_futures_tail", exc)
        return None
