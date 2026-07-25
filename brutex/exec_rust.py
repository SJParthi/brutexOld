"""Flag-gated Rust execution-cores seam (DEC-EXEC-RUST-CORE-001, MIGRATION.md #2212).

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
inside either would mint an options↔costs import cycle. Its ONLY costs-side
import is the shared :class:`ChargeStack` shape from the stdlib-only LEAF
``brutex.costs.types`` — never ``calculator`` (whose dispatch site imports
this module back), so the calculator ↔ exec_rust import pair is structurally
acyclic (CodeQL ``py/cyclic-import``; AST-pinned by the seam suite's
leaf-discipline test). The dispatch sites still import THIS module lazily
inside the dispatching function (the ``materialize_trade`` lazy-import
precedent) — deferral of the engine-package + one-time ``fno_math`` import
cost, no longer cycle-avoidance.

## The flag — ``BRUTEX_EXEC_RUST`` over ``EXEC_RUST_DEFAULT`` (ON since #2215)

:func:`exec_rust_enabled` mirrors the sibling
:func:`brutex.engine.rust_metrics.sweep_rust_metrics_enabled` exactly,
classified through the COMMON PUBLIC
:func:`brutex.engine.sweep_lookahead.env_flag_tristate` (never copied —
CLAUDE.md §11): a truthy value forces ON, a falsy value (``0``/``false``/
``no``/``off`` — the explicit kill switch) forces OFF,
unset/whitespace-empty falls to :data:`EXEC_RUST_DEFAULT` = **True** since
the operator RUST-O(1)-EVERYWHERE directive (DEC-RUST-DEFAULT-ON-WAVE-001,
#2215) — the kernel is ENGAGED by default with the degrade-to-oracle safety
layer untouched (this seam runs NO Python-side runtime burn-in/autorevert —
its value parity is guaranteed structurally + at CI, see "The parity
discipline" below) — and any unrecognised value fails SAFE to OFF with one
warn-once ``exec_rust.flag_unrecognised`` event. Read per CALL so spawn
workers
resolve their own inherited environment (no module-global flag state).

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
* a MALFORMED kernel result — wrong wire length, a non-int or
  out-of-i64-range wire element, an unknown verdict kind, or a broken
  kind↔wire pairing — is VALIDATED inside
  the seam BEFORE any indexing/adoption (:func:`_wire_fault` /
  :func:`_verdict_fault`, review round 2: R2-MINOR/R3-F2 killed the 17/18-
  slot futures-wire ``IndexError`` escape + the over-long/wrong-type silent
  adoption) and routes to the SAME warn-once ``exec_rust.kernel_fallback``
  (``error_type="KernelWireFault"``) → ``None`` → the Python arm — the warn
  fires INSIDE the seam, structurally, because the executors' broad prep
  catches would otherwise swallow an escaping ``TypeError`` with zero logs
  (R3-F3). Served-VALUE integrity is guarded by the
  Rust-side ``ChargeStack::validated()`` construction check plus the
  dual-arm / golden / differential parity gates; on re-materialization the
  frozen dataclasses that DEFINE ``__post_init__`` invariants
  (``TradeInput`` / ``FuturesTradeInput`` / ``FuturesCostBreakdown``) still
  run them, while ``TradeCostBreakdown`` carries NO ``__post_init__`` — the
  options-breakdown boundary check is the Rust validation + the gates, not
  a Python-side assert.

## The parity discipline — NO runtime burn-in on this seam (Golden Rule #1)

Unlike the RANKING seam (``brutex.engine.rust_parity_core`` — a 16-call
burn-in bit-compare + always-on differential + ship-the-oracle autorevert)
and the SWEEP-ENUM seam (``brutex.engine.rust_enum`` — a width-keyed shadow
burn-in + per-width autorevert), this EXEC seam runs NO Python-side runtime
burn-in and NO ship-the-oracle autorevert. That is a DELIBERATE design, not
an omission: exec is a cold per-FINALIST-trade path, and its Rust↔Python
value parity is guaranteed by (a) the Rust-side ``ChargeStack::validated()``
construction check, (b) the frozen-dataclass ``__post_init__`` invariants
re-run on re-materialization (``TradeInput`` / ``FuturesTradeInput`` /
``FuturesCostBreakdown``), (c) the seam's :func:`_wire_fault` /
:func:`_verdict_fault` structural gates + the i64 envelope guard, and
(d) the CI-time dual-arm / golden / differential parity suites over the
exact deployed wheel (:class:`StaleExecWheelError` + the EXPECTED_BUILD.json
freshness gate close the deployed-wheel-differs-from-CI-wheel gap). Any
deviation degrades to the verbatim Python oracle with one warn-once event;
there is no runtime bit-compare to autorevert on. Do NOT describe this seam
as carrying a burn-in/autorevert — it does not.

## Honest envelope (Golden Rule #10)

COLD path: the executors run per-trade loops over FINALIST trades only.
The value of the Rust arm is one-language consistency + shared-crate reuse,
NOT speed — no speedup is claimed or measured here; the OFF arm's steady
cost is one env read + one function call per dispatch, plus a ONE-TIME
``fno_math`` module import on the first dispatch of the process (the PyO3
module init runs; ``ImportError`` → ``None`` on wheel-free envs) — no
``exec_*`` kernel code ever RUNS while OFF. The warn-once memo is
LOG-VOLUME state only (results never depend on it);
:func:`reset_warn_memo_for_tests` restores a fresh view (the documented
exception to the no-global-mutable-state rule, the rust_metrics precedent).
"""

from __future__ import annotations

import os
from types import ModuleType
from typing import Any, Final

import structlog

from brutex.costs.types import ChargeStack
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

#: The one-constant flip lever — ``True`` since the operator
#: RUST-O(1)-EVERYWHERE directive (DEC-RUST-DEFAULT-ON-WAVE-001, #2215):
#: the exec-cost kernel is ENGAGED by default, with the i64-envelope route +
#: the degrade-to-oracle safety layer untouched (no Python-side runtime
#: burn-in/autorevert on this seam — see the module docstring's "parity
#: discipline"). The explicit ``0`` env value is the kill switch.
EXEC_RUST_DEFAULT: Final[bool] = True

#: The env flag over the default (sibling of ``BRUTEX_SWEEP_RUST_METRICS``).
ENV_FLAG: Final[str] = "BRUTEX_EXEC_RUST"

#: i64 envelope bounds — the kernel is i64-native; anything outside routes
#: to the Python-bigint oracle arm.
_I64_MIN: Final[int] = -(2**63)
_I64_MAX: Final[int] = 2**63 - 1

#: Wire arity of one charge stack (the pinned :class:`ChargeStack` order).
_WIRE_LEN: Final[int] = 17

#: Wire arity of a futures TRADE verdict — the 17 stack slots + mae + mfe
#: (``exec_futures.rs`` pushes the two excursions onto the stack wire).
_FUTURES_WIRE_LEN: Final[int] = _WIRE_LEN + 2


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

    Truthy → ON; falsy → OFF (the explicit ``0`` kill switch); unset or
    whitespace-empty → the default (**True** since DEC-RUST-DEFAULT-ON-WAVE-001,
    #2215); any other value → fail-safe OFF with one warn-once
    ``exec_rust.flag_unrecognised``. Vocabulary via the COMMON
    :func:`~brutex.engine.sweep_lookahead.env_flag_tristate`.
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
    """Warn-once + Python-arm fallback for an out-of-i64-envelope input.

    Fired by a seam whose :func:`_fits_i64` pre-gate refused an input the
    kernel's ``i64`` wire cannot carry — the seam then serves the exact
    Python arm instead (never a truncated kernel call). One
    ``exec_rust.envelope_fallback`` WARNING per *site* per process (the
    :func:`_warn_once` ``(event, reason)`` dedupe; ``reason`` =
    ``"envelope:<site>"``).
    """
    _warn_once("exec_rust.envelope_fallback", reason=f"envelope:{site}", site=site)


def _kernel_fallback(site: str, exc: BaseException) -> None:
    """Warn-once + Python-arm fallback for a kernel-raised *exc* at *site*.

    Fired when the dispatched kernel call raised inside a seam's narrow
    catch — the seam then serves the exact Python arm (the loudness law:
    never a silent fallback). One ``exec_rust.kernel_fallback`` WARNING per
    *site* per process, carrying ``error`` (``str(exc)``) + ``error_type``
    for triage (``reason`` = ``"kernel:<site>"``).
    """
    _warn_once(
        "exec_rust.kernel_fallback",
        reason=f"kernel:{site}",
        site=site,
        error=str(exc),
        error_type=type(exc).__name__,
    )


def _wire_fallback(site: str, fault: str) -> None:
    """Warn-once + Python-arm fallback for a malformed kernel RESULT shape.

    The structural loudness law (review round 2, R3-F3): a kernel-contract
    fault must warn INSIDE the seam BEFORE the ``None`` fallback sentinel
    returns — the pre-fix design let ``ChargeStack(*wire)`` raise
    ``TypeError`` OUTSIDE the seam's try, and the executors' broad prep
    catches (``options/executor.py`` / ``futures/executor.py``) swallowed
    it with ZERO logs (a silent Python-arm fallback, against this module's
    own law). Same event/reason family as :func:`_kernel_fallback` so the
    flood-safety memo is shared per site.
    """
    _warn_once(
        "exec_rust.kernel_fallback",
        reason=f"kernel:{site}",
        site=site,
        error=fault,
        error_type="KernelWireFault",
    )


def _wire_fault(wire: object, *, expected_len: int) -> str | None:
    """Describe a malformed kernel wire, or ``None`` when well-formed.

    Well-formed = a ``list``/``tuple`` of EXACTLY *expected_len* genuine
    ``int`` elements, each inside the i64 range (``bool`` and an
    out-of-range int are refused — the kernel wire is ``Vec<i64>``, so
    anything else is build/contract skew). Review round 2
    (R2-MINOR/R3-F2): every kernel-returned wire is validated through this
    predicate BEFORE any indexing — a 17/18-slot futures wire previously
    raised an UNCAUGHT ``IndexError`` past every catch on the dispatch
    path, a ≥20-slot wire was silently truncated-adopted, and a
    right-length wire of junk elements was silently adopted whole (the
    range check closes rA round-2 NIT-1's last int-shaped adoption
    channel).
    """
    if not isinstance(wire, list | tuple):
        return f"wire is {type(wire).__name__}, expected a list or tuple"
    if len(wire) != expected_len:
        return f"wire length {len(wire)} != the pinned {expected_len}"
    for i, item in enumerate(wire):
        if not isinstance(item, int) or isinstance(item, bool):
            return f"wire[{i}] is {type(item).__name__}, expected int"
        if not _fits_i64(item):
            # The real kernel wire is Vec<i64>, so an out-of-range int is
            # the same build/contract skew as a wrong element type.
            return f"wire[{i}] is outside the i64 range"
    return None


def _verdict_fault(
    kind: object, wire: object, *, reject_kinds: tuple[str, ...], expected_len: int
) -> str | None:
    """Describe a malformed kernel materialize VERDICT, or ``None``.

    Contract per site: ``("trade", <expected_len>-slot int wire)`` or
    ``(<reject kind>, None)`` with *reject_kinds* pinned by the caller
    (options ``("stale_bar", "tick_floor")``; futures ``("stale_bar",)``).
    Membership is tested on a TUPLE (never a set) so an unhashable junk
    *kind* classifies as a fault instead of raising out of the guard.
    """
    if kind == "trade":
        if wire is None:
            return "verdict ('trade', None) is missing its stack wire"
        return _wire_fault(wire, expected_len=expected_len)
    if kind not in reject_kinds:
        return f"unknown verdict kind {kind!r}"
    if wire is not None:
        return f"verdict kind {kind!r} carries an unexpected wire"
    return None


def _stack_from_wire(wire: Any) -> ChargeStack:
    """Re-wrap a VALIDATED pinned 17-slot kernel wire as the NamedTuple.

    Every caller runs :func:`_wire_fault` (directly or via
    :func:`_verdict_fault`) FIRST, so this constructor never sees a
    malformed wire — a shape/type violation is a kernel CONTRACT fault and
    routes to the warn-once :func:`_wire_fallback` → the Python arm
    (review round 2; supersedes the round-1 "the ``TypeError`` propagates
    loudly" design, whose raise the options executor's broad prep catch
    swallowed with zero logs — R3-F3). Served-value integrity is the
    Rust-side ``ChargeStack::validated()`` check + the
    dual-arm/golden/differential gates (``TradeCostBreakdown`` has no
    ``__post_init__``; see the module docstring's fallback law).
    """
    return ChargeStack(*wire)


def fill_slippage(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    *,
    is_long: bool,
) -> tuple[int, int, int] | None:
    """Kernel-served ``_apply_slippage`` twin (worst-case bar-extreme
    anchors, DEC-FILL-WORST-CASE-ONLY-001), or ``None`` → Python arm."""
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(entry_high_paisa, entry_low_paisa, exit_high_paisa, exit_low_paisa):
        _envelope_fallback("fill_slippage")
        return None
    try:
        out = _kernel_fn(rust, "exec_apply_slippage")(
            entry_high_paisa, entry_low_paisa, exit_high_paisa, exit_low_paisa, is_long
        )
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        _kernel_fallback("fill_slippage", exc)
        return None
    fault = _wire_fault(out, expected_len=3)
    if fault is not None:
        _wire_fallback("fill_slippage", fault)
        return None
    return (out[0], out[1], out[2])


def fill_futures(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    *,
    is_long: bool,
) -> tuple[int, int, int] | None:
    """Kernel-served ``apply_futures_fill`` twin (worst-case bar-extreme
    anchors, DEC-FILL-WORST-CASE-ONLY-001), or ``None`` → Python arm."""
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(entry_high_paisa, entry_low_paisa, exit_high_paisa, exit_low_paisa):
        _envelope_fallback("fill_futures")
        return None
    try:
        out = _kernel_fn(rust, "exec_apply_futures_fill")(
            entry_high_paisa, entry_low_paisa, exit_high_paisa, exit_low_paisa, is_long
        )
    except MemoryError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        _kernel_fallback("fill_futures", exc)
        return None
    fault = _wire_fault(out, expected_len=3)
    if fault is not None:
        _wire_fallback("fill_futures", fault)
        return None
    return (out[0], out[1], out[2])


def charge_stack_option(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
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
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
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
        wire = _kernel_fn(rust, "exec_option_charge_stack")(
            entry_high_paisa,
            entry_low_paisa,
            exit_high_paisa,
            exit_low_paisa,
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
    fault = _wire_fault(wire, expected_len=_WIRE_LEN)
    if fault is not None:
        _wire_fallback("charge_stack_option", fault)
        return None
    return _stack_from_wire(wire)


def charge_stack_futures(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
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
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
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
        wire = _kernel_fn(rust, "exec_futures_charge_stack")(
            entry_high_paisa,
            entry_low_paisa,
            exit_high_paisa,
            exit_low_paisa,
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
    fault = _wire_fault(wire, expected_len=_WIRE_LEN)
    if fault is not None:
        _wire_fallback("charge_stack_futures", fault)
        return None
    return _stack_from_wire(wire)


def materialize_option_tail(
    entry_open_paisa: int,
    exit_open_paisa: int,
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    *,
    entry_volume: int,
    exit_volume: int,
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
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
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
            entry_high_paisa,
            entry_low_paisa,
            exit_high_paisa,
            exit_low_paisa,
            entry_volume,
            exit_volume,
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
        _kernel_fallback("materialize_option_tail", exc)
        return None
    # Whole-verdict validation BEFORE adoption (review round 2, R3-F3): the
    # pre-fix `_stack_from_wire(wire)` sat outside the try, so a wrong-arity
    # wire raised TypeError past the seam (no warn) into the options
    # executor's broad prep catch — a ZERO-LOG silent fallback.
    fault = _verdict_fault(
        kind, wire, reject_kinds=("stale_bar", "tick_floor"), expected_len=_WIRE_LEN
    )
    if fault is not None:
        _wire_fallback("materialize_option_tail", fault)
        return None
    if wire is None:
        return (kind, None)
    return (kind, _stack_from_wire(wire))


def materialize_futures_tail(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    *,
    entry_volume: int,
    exit_volume: int,
    path_highs_paisa: list[int],
    path_lows_paisa: list[int],
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
) -> tuple[str, ChargeStack | None, int, int] | None:
    """Kernel-served futures post-I/O tail, or ``None`` → the Python tail.

    Returns ``("stale_bar", None, 0, 0)`` or
    ``("trade", stack, mae_paisa, mfe_paisa)``.
    """
    rust = _kernel_or_none()
    if rust is None:
        return None
    if not _fits_i64(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
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
            entry_high_paisa,
            entry_low_paisa,
            exit_high_paisa,
            exit_low_paisa,
            entry_volume,
            exit_volume,
            path_highs_paisa,
            path_lows_paisa,
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
        _kernel_fallback("materialize_futures_tail", exc)
        return None
    # Whole-verdict validation BEFORE any indexing (review round 2,
    # R2-MINOR/R3-F2 — supersedes the round-1 inside-the-try slicing, whose
    # "caught like every other malformed shape" claim was FALSE for wire
    # lengths 17/18: `wire[_WIRE_LEN]` raised IndexError past the narrow
    # catch AND the executor's broad catch — a raw crash — while a ≥20-slot
    # wire was silently truncated-adopted).
    fault = _verdict_fault(kind, wire, reject_kinds=("stale_bar",), expected_len=_FUTURES_WIRE_LEN)
    if fault is not None:
        _wire_fallback("materialize_futures_tail", fault)
        return None
    if wire is None:
        return (kind, None, 0, 0)
    return (
        kind,
        _stack_from_wire(wire[:_WIRE_LEN]),
        wire[_WIRE_LEN],
        wire[_WIRE_LEN + 1],
    )
