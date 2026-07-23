"""Flag-gated Rust options-cost tail seam — the ``DEC-OPTCOST-TAILS-RUST-001`` facade.

When — and ONLY when — ``BRUTEX_OPTCOST_TAILS_RUST`` is set truthy, the six
unclaimed options-cost tail modules (``brutex/costs/constants.py`` ·
``brutex/data/moneyness.py`` · ``brutex/options/{lot_size_history,
expiry_calendar, strike_rules, strike_grid}.py``) route their pure decision
math through the ``fno_math`` wheel's ``optcost_*`` kernels
(``rust/fno-math/src/optcost/``). This leaf owns the flag, the guard/latch/
burn-in apparatus, and every per-seam kernel-outcome composition; the call
sites gain only a two-line gate each.

## Home + leaf discipline (the recorded location decision)

A TOP-LEVEL leaf (the ``brutex/feed_rust.py`` precedent), NOT
``brutex/math/optcost_tails.py`` and NOT ``brutex/costs/optcost_rust.py``:

* ``brutex/math/__init__.py`` hard-imports BOTH Rust wheels at package load
  (``_reexport`` runs at import time), so any ``brutex.math.*`` home would
  import ``fno_math`` even with the flag OFF — the bomb-spy OFF-default law
  ("the Rust extension is never imported by the seams") would be violated
  structurally.
* ``brutex/costs/optcost_rust.py`` creates a HARD import cycle:
  ``options.lot_size_history`` → ``brutex.costs`` (package init) →
  ``calculator`` → ``from brutex.options.lot_size_history import …`` while
  that module is still mid-initialisation → ``ImportError`` at boot.

The top-level leaf imports ONLY stdlib + ``structlog`` at module scope
(``brutex/__init__.py`` is version-only); call-site types
(``UnverifiedCostRegimeError``) and the COMMON degrade/panic
belts (re-exported by ``brutex.data.lake_rust_render`` for sibling seams —
CLAUDE.md §11 COMMON, no copies) are imported LAZILY at call time, when the
caller's own module is fully initialised; the ``classify_graded`` bucket
values are INJECTED by the caller instead, so this facade imports
``brutex.data.moneyness`` NOWHERE — not even under ``TYPE_CHECKING`` (the
CODEQL FIX ROUND, code-scanning alerts 1308 + 1309: the interim
``TYPE_CHECKING``-only ``Moneyness`` import was runtime-free yet still a
module-level cyclic-import finding — the ``fno_symbol_rust`` precedent).
Call sites import this facade, never the reverse.

## The flag — ``BRUTEX_OPTCOST_TAILS_RUST`` (DEFAULT ON per
## DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23)

Resolved per call (the ``lake_rust_render`` posture): a
truthy literal (``1/true/yes/on``) → ON; a falsy literal
(``0/false/no/off``) → OFF — the explicit kill switch; unset /
whitespace-empty → ON (the default);
any OTHER value → OFF with one loud ``optcost_rust.fallback`` WARN (reason
``flag_unrecognised``) per process. The truthy/falsy vocabulary is a LOCAL
twin of :func:`brutex.engine.sweep_lookahead.env_flag_tristate` —
byte-for-byte, equality-pinned in the unit suite — NOT an import, because
``brutex/engine/__init__.py`` hard-imports the ``fno_math`` wheel and the
OFF path of a costs/moneyness read must never require it (the
``lake_rust_render`` local-twin rationale verbatim).

## Fail-closed layers (all process-local)

1. **Import guard + symbol handshake** — the wheel is resolved lazily on
   the first engaged call; ``ImportError`` latches ``kernel_unavailable``,
   an importable wheel MISSING any of the 11 ``optcost_*`` callables (a
   stale pre-optcost build) latches ``kernel_stale`` — each ONE loud
   ``optcost_rust.rust_path_disabled`` WARN; every call serves the oracle.
2. **Burn-in self-check + autorevert** — the first :data:`BURN_IN_SAMPLES`
   ENGAGED calls per SEAM also run the live Python oracle and compare
   outcomes (exact value equality — Decimal outcomes compare on
   ``as_tuple()`` so an exponent drift can never ship — and, on refusal
   arms, exception TYPE + ``str()`` byte equality). A mismatch logs
   ``optcost_rust.autoreverted`` (ERROR), SHIPS THE ORACLE OUTCOME, and
   latches the seam OFF for the process. During burn-in the ORACLE outcome
   is always the one shipped (equal on a clean compare, so no behaviour
   fork).
3. **Refuse-poison output validation** — every kernel return is
   structurally validated (codes in the known vocabulary, refusal row
   indices in range AND actually refusal rows, ints where ints are due);
   any violation raises :class:`KernelResultRefused` → one
   ``optcost_rust.fallback`` WARN (reason ``result_refused``) + the oracle.
   A poisoned wheel can only ever degrade, never serve.
4. **Degrade + panic belts** — kernel raises in the COMMON degrade set
   (:func:`brutex.data.lake_rust_render.degrade_errors`) → one
   ``kernel_error`` WARN + the oracle; anything the COMMON panic belt
   catches (incl. a ``pyo3_runtime.PanicException`` leaking a pyo3
   boundary) → latch ``kernel_panic`` + the oracle.
   ``KeyboardInterrupt`` / ``SystemExit`` / ``MemoryError`` re-raise RAW
   (the ``errors.py`` escape-set doctrine — OOM is never a fallback).

## Refusal law (VERIFIED-RATES-ONLY — hard rule 3)

The kernels return CODES only; every refusal exception —
``UnverifiedCostRegimeError`` (DEC-ETC-REGIME-HISTORY-001), the
empty-table / pre-history ``ValueError``\\s — is composed PYTHON-SIDE here
from the SAME inputs (the caller's own ``RegimeRow`` / history tuples)
using the same f-string shapes as the oracle lines, so the exception TYPE
and MESSAGE BYTES are identical on both arms (pinned by the parity suites).
A rate is NEVER fabricated: an unknown code, an out-of-range index, or a
non-refusal row behind a refusal code all refuse the kernel result.

## Money + exactness law (hard rule 4)

Decimal↔int conversions are EXACT pure-integer ``as_tuple()`` arithmetic
(never a context-rounded ``scaleb``/``quantize``): a Decimal not exactly
representable as i64 paisa — or a strike ladder without ONE uniform decimal
exponent (the exponent is part of the byte identity of the derived step) —
routes SILENTLY to the Python oracle. Nothing is ever approximated.

Kernel/oracle EQUIVALENCE for the moneyness seams is proven under the
DEFAULT decimal context (28 significant digits — the proof envelope in
``rust/fno-math/src/optcost/moneyness.rs``). A caller-MODIFIED ambient
context (a reduced ``prec`` / enabled traps) skews the ORACLE arm's own
Decimal arithmetic, not the kernel's exact-integer law — so post-burn-in
the two arms can diverge there, the ON value being the exact answer. Zero
production callers mutate the decimal context (grep-verified; the one
production ``localcontext`` in the repo, ``brutex/data/pcr.py``, pins a
default-equivalent context around its own division) and the behavior is
pinned as conscious in the unit suite (round-1 review).

## Honest envelope (Golden Rule #10)

Expected payoff ≈ 0 — these are cold/warm per-trade + read-side paths, not
the ~33B-trial mask path; the conversion is for Rust uniformity (ADR-0020
banner), no speedup is claimed. The engaged path ADDS per call — steady
state included: one env probe; the :func:`_resolve_kernel` guard re-run
(a ``sys.modules``-cached ``importlib.import_module`` lookup PLUS the full
11-symbol getattr/callable handshake, on EVERY engaged call — never
memoised); table→ordinal list builds (N ≤ 6 rows); one FFI crossing; the
refuse-poison validation of every kernel return; and — during burn-in —
the full oracle run (round-2 review: this list is the exhaustive
enumeration, doc-pinned). A persistently ``result_refused`` kernel NEVER
latches the seam OFF — it re-pays the crossing + validation on every call
and the oracle serves every time, a deliberate per-call degrade mirroring
``kernel_error`` (only the import-guard reasons, ``kernel_panic`` and
``burn_in_mismatch`` latch; this class never converges to a latch —
behavior-pinned). The latch / warn memo / burn-in
counters are the same *documented exception* to the no-global-mutable-state
rule as ``rust_metrics``'s (plain unlocked writes: a racing thread can at
worst run an extra burn-in compare or emit a duplicate warn line, never
ship a wrong value — any mismatch ships the oracle outcome).
:func:`reset_optcost_rust_for_tests` restores a fresh process view — test
isolation only, never called from production paths.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol, cast

import structlog

if TYPE_CHECKING:  # pragma: no cover - imports only for static typing
    from collections.abc import Callable, Sequence
    from types import ModuleType


class RegimeRowLike(Protocol):
    """The structural shape of :class:`brutex.costs.constants.RegimeRow`.

    A Protocol (not an import) keeps this leaf free of module-scope brutex
    imports (the leaf-discipline law in the module docstring).
    """

    @property
    def start(self) -> date:  # pragma: no cover — Protocol stub, never executed
        """The regime's inclusive start date (or an unverified-window marker)."""
        ...

    @property
    def rate_bps_x100(self) -> int | None:  # pragma: no cover — Protocol stub, never executed
        """The verified rate in bps x100, or ``None`` for a rate-less refusal row."""
        ...

    @property
    def source(self) -> str:  # pragma: no cover — Protocol stub, never executed
        """The row's citation (circular id) or its honest verification-gap note."""
        ...


_LOG = structlog.get_logger("brutex.optcost_rust")

#: The env flag. DEFAULT ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator
#: directive 2026-07-23); a falsy literal is the explicit kill switch back
#: to the live Python oracle.
OPTCOST_TAILS_RUST_ENV: Final[str] = "BRUTEX_OPTCOST_TAILS_RUST"

#: Engaged calls byte-compared against the live Python oracle per process
#: PER SEAM (the burn-in self-check; the #1531/#1532 ``SELF_CHECK_SAMPLES``
#: precedent, mirrored by lake_rust_render / greeks_enrich).
BURN_IN_SAMPLES: Final[int] = 16

#: Truthy/falsy env literals — a DELIBERATE byte-for-byte LOCAL twin of the
#: engine's COMMON ``sweep_lookahead`` vocabulary (module docstring; the
#: mirror is equality-pinned in ``tests/unit/test_optcost_rust.py``).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Kernel result codes — the frozen FFI vocabulary
#: (``rust/fno-math/src/optcost/mod.rs`` ``CODE_*`` consts).
_CODE_OK: Final[int] = 0
_CODE_REFUSAL: Final[int] = 1
_CODE_EMPTY_TABLE: Final[int] = 2
_CODE_PRE_HISTORY: Final[int] = 3
_CODE_NO_WEEKLY: Final[int] = 4
_CODE_NO_STEP: Final[int] = 5

#: ``date`` ordinal domain (``date.min``/``date.max``) — the kernel mirrors
#: these as ``MIN_ORDINAL``/``MAX_ORDINAL``; result ordinals outside the
#: window are refused (a ``date.fromordinal`` there would raise).
_MIN_ORDINAL: Final[int] = 1
_MAX_ORDINAL: Final[int] = 3_652_059

#: i64 bound for every value that crosses the FFI as a Rust ``i64``.
_I64_MAX: Final[int] = 2**63 - 1

#: Decimal digits certainly past i64 (``2**63 - 1`` has 19 digits) — the
#: early-out in :func:`_exact_scaled_int` before a huge ``10**shift``.
_I64_DIGITS: Final[int] = 19

#: A kernel ``(code, value)`` pair's arity (the frozen FFI shape).
_PAIR_ARITY: Final[int] = 2

#: The 11 ``optcost_*`` kernel callables the symbol handshake verifies —
#: all-or-nothing (a stale pre-optcost wheel latches ``kernel_stale``).
_KERNEL_API: Final[tuple[str, ...]] = (
    "optcost_atm_offset",
    "optcost_grid_around_atm",
    "optcost_history_lookup",
    "optcost_moneyness_classify",
    "optcost_next_monthly_expiry",
    "optcost_next_weekly_expiry",
    "optcost_offset_strike",
    "optcost_regime_rate",
    "optcost_round_to_atm",
    "optcost_strike_offset_from_atm",
    "optcost_strike_step",
)

#: The latch: the disable reason once the Rust path is OFF for this process
#: (the documented no-global-mutable-state exception — module docstring).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: The budget every lazily-seeded per-seam burn-in cell starts from (the
#: test reset seam re-points it — same documented exception).
_BURN_IN_DEFAULT: Final[list[int]] = [BURN_IN_SAMPLES]

#: Burn-in samples still to run this process, PER SEAM (dict ``setdefault``
#: is atomic under the GIL — same documented exception).
_BURN_IN_REMAINING: Final[dict[str, int]] = {}

#: Once-per-process warn/INFO memo (log-volume state ONLY — outcomes never
#: depend on it).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Memo key for the engaged-once INFO (namespaced apart from warn reasons).
_ENGAGED_KEY: Final[str] = "__engaged__"


class KernelResultRefused(Exception):
    """A kernel return violated the frozen output contract (refuse-poison).

    Raised ONLY inside this facade's kernel-outcome composition wrappers;
    :func:`_route` maps it to one ``optcost_rust.fallback`` WARN (reason
    ``result_refused``) + the Python oracle — a poisoned/stale wheel can
    only ever degrade, never serve. Deliberately NON-latching (unlike
    ``kernel_panic``): every occurrence re-pays the FFI crossing +
    validation on the next call and serves the oracle again (the module
    docstring's honest-envelope note).
    """


@dataclass(frozen=True, slots=True)
class Outcome:
    """One seam call's result: a value XOR a fully-composed exception.

    ``error is not None`` means the seam RAISES (:func:`_materialize`
    re-raises the instance); otherwise ``value`` is returned. The refusal
    exceptions are composed Python-side from the caller's own inputs, so
    their type + message bytes match the oracle arm exactly.
    """

    value: object = None
    error: Exception | None = None


def outcome_equal(a: Outcome, b: Outcome) -> bool:
    """Burn-in equality: exact value ``==``; refusal arms compare exception
    TYPE identity + ``str()`` byte equality (the parity law)."""
    if (a.error is None) != (b.error is None):
        return False
    if a.error is not None and b.error is not None:
        return type(a.error) is type(b.error) and str(a.error) == str(b.error)
    return bool(a.value == b.value)


def _decimal_outcome_equal(a: Outcome, b: Outcome) -> bool:
    """Burn-in equality for Decimal-valued seams — ``as_tuple()`` exact.

    Plain ``==`` on Decimals is exponent-INSENSITIVE (``Decimal("50") ==
    Decimal("50.00")``) while ``str()`` is not; the derived strike step's
    byte identity includes its exponent, so the compare must too.
    """
    if a.error is not None or b.error is not None:
        return outcome_equal(a, b)
    if a.value is None or b.value is None:
        return a.value is None and b.value is None
    return (
        isinstance(a.value, Decimal)
        and isinstance(b.value, Decimal)
        and a.value.as_tuple() == b.value.as_tuple()
    )


def env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the flag vocabulary (local twin — module docs).

    ``True`` for a :data:`_TRUTHY` literal, ``False`` for a :data:`_FALSY`
    literal, ``None`` for anything else (unset/empty AND unrecognised — the
    caller owns the default + warn policy). Byte-identical semantics to
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate`, equality-pinned
    in the unit suite; NOT imported from there because the engine package
    hard-imports the ``fno_math`` wheel (module docstring).
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def optcost_rust_enabled() -> bool:
    """Resolve ``BRUTEX_OPTCOST_TAILS_RUST`` (default ON, junk fails loud-OFF).

    Unset/blank → ``True`` (ON by default per DEC-RUST-DEFAULT-ON-WAVE-001,
    operator directive 2026-07-23); a falsy literal stays the explicit kill
    switch. Read per call (one ``os.environ`` probe) so workers resolve
    their own inherited environment — no global flag state. A non-empty
    unrecognised value logs ONE ``optcost_rust.fallback`` WARN
    (``flag_unrecognised``) per process and fails SAFE to OFF — a typo'd
    value is never silently ignored.
    """
    raw = os.environ.get(OPTCOST_TAILS_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def optcost_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_optcost_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state for
    every seam (kernel outcomes ship directly — the differential suites
    depend on that; an armed burn-in would cushion a first divergence by
    shipping the oracle outcome). The default restores a fresh-process
    view. Never called from production code paths.
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_DEFAULT[0] = remaining
    _BURN_IN_REMAINING.clear()
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``optcost_rust.fallback`` WARN once per process per memo key.

    Degrade reasons that can differ per seam fold the seam name into the
    memo key (``context["seam"]``) so one seam's first fault does not
    silence a different seam's — the oracle serves regardless.
    """
    key = f"{reason}|{context.get('seam', '')}"
    if key in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(key)
    _LOG.warning("optcost_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``optcost_rust.autoreverted`` at ERROR (the burn-in
    mismatch — a genuine wrongness signal); guard latches log
    ``optcost_rust.rust_path_disabled`` at WARN. Repeat calls are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("optcost_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("optcost_rust.rust_path_disabled", reason=reason, **context)


def _resolve_kernel() -> ModuleType | None:
    """Import guard + symbol handshake: ``fno_math``, or latch + ``None``.

    ``ImportError`` (wheel absent / the shim's own compiled-surface
    handshake refusing) latches ``kernel_unavailable``; an importable wheel
    missing any :data:`_KERNEL_API` callable (a stale pre-optcost build
    whose shim predates the symbols) latches ``kernel_stale``. Either way
    ONE loud WARN names the remedy and every call serves the oracle.
    """
    try:
        kernel = importlib.import_module("fno_math")
    except ImportError as exc:
        _disable(
            "kernel_unavailable",
            error_detail=repr(exc),
            remedy="pip install ./rust/fno-math (or maturin develop --release)",
        )
        return None
    for name in _KERNEL_API:
        if not callable(getattr(kernel, name, None)):
            _disable(
                "kernel_stale",
                error_detail=f"fno_math.{name} missing or not callable — stale/foreign wheel",
                remedy="rebuild the fno_math wheel from this repo state",
            )
            return None
    return kernel


def _degrade_errors() -> tuple[type[Exception], ...]:
    """The COMMON degrade-to-oracle catch set (imported, never copied).

    ``brutex.data.lake_rust_render`` re-exports its stateless belts for
    sibling seams (CLAUDE.md §11 COMMON); imported LAZILY — only while an
    exception is in flight — so this leaf keeps zero brutex imports at
    module scope (the leaf-discipline law in the module docstring).
    """
    from brutex.data.lake_rust_render import degrade_errors

    return degrade_errors()


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The COMMON ``(Exception, *PanicException)`` belt (imported, never copied)."""
    from brutex.data.lake_rust_render import panic_belt_errors

    return panic_belt_errors()


def _route(
    seam: str,
    *,
    kernel_call: Callable[[ModuleType], Outcome],
    oracle: Callable[[], Outcome],
    equal: Callable[[Outcome, Outcome], bool] = outcome_equal,
) -> Outcome:
    """The routed seam call — reached ONLY when the flag resolved truthy.

    Runs the fail-closed layers (module docstring) and returns the outcome
    to materialise. EVERY path yields a valid outcome: latched / guarded /
    degraded calls yield ``oracle()``; a burn-in call yields the ORACLE
    outcome (autoreverting on mismatch); only a steady-state, guard-clean
    call yields the kernel-derived outcome.
    """
    if optcost_rust_disabled():
        return oracle()
    kernel = _resolve_kernel()
    if kernel is None:
        return oracle()
    try:
        rust_outcome = kernel_call(kernel)
    except KernelResultRefused as exc:
        _warn_once("result_refused", seam=seam, error_detail=repr(exc))
        return oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global stops — never a fallback.
        # This clause sits ABOVE the panic belt so its `Exception` member
        # can never swallow a MemoryError.
        raise
    except _degrade_errors() as exc:
        _warn_once("kernel_error", seam=seam, error_detail=repr(exc))
        return oracle()
    except _panic_belt_errors() as exc:
        # The COMMON belt over pyo3's own boundary: a PanicException is a
        # BaseException no `except Exception` sees — latch + oracle, never
        # a new crash class into a costs/read path (fail-closed).
        _disable("kernel_panic", seam=seam, error_detail=repr(exc))
        return oracle()
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info("optcost_rust.engaged", seam=seam)
    remaining = _BURN_IN_REMAINING.setdefault(seam, _BURN_IN_DEFAULT[0])
    if remaining > 0:
        oracle_outcome = oracle()
        if not equal(rust_outcome, oracle_outcome):
            _disable(
                "burn_in_mismatch",
                error=True,
                seam=seam,
                rust_outcome=repr(rust_outcome),
                oracle_outcome=repr(oracle_outcome),
            )
        else:
            _BURN_IN_REMAINING[seam] = remaining - 1
            if remaining == 1:
                _LOG.info("optcost_rust.burn_in_complete", seam=seam, samples=BURN_IN_SAMPLES)
        return oracle_outcome
    return rust_outcome


def _materialize(outcome: Outcome) -> object:
    """Return the outcome's value, or re-raise its composed exception."""
    if outcome.error is not None:
        raise outcome.error
    return outcome.value


def _refuse_unless_int(seam: str, value: object) -> int:
    """Refuse-poison: the kernel promised an int here (bool is not an int)."""
    if type(value) is not int:
        msg = f"{seam}: kernel returned {type(value).__name__}, expected int"
        raise KernelResultRefused(msg)
    return value


def _code_value(seam: str, result: object) -> tuple[int, int]:
    """Refuse-poison: unpack a kernel ``(code, value)`` pair of real ints."""
    if not (isinstance(result, tuple) and len(result) == _PAIR_ARITY):
        msg = f"{seam}: kernel returned {type(result).__name__}, expected a (code, value) pair"
        raise KernelResultRefused(msg)
    return _refuse_unless_int(seam, result[0]), _refuse_unless_int(seam, result[1])


def _exact_scaled_int(value: Decimal, scale: int) -> int | None:
    """``value × 10**scale`` as an EXACT int within i64, else ``None``.

    Pure ``as_tuple()`` integer arithmetic — never a context-rounded
    ``scaleb``/``quantize`` (a >28-digit coefficient would silently round
    there; hard rule 4 forbids approximation, so inexact → ``None`` → the
    Python oracle serves). Handles negative values (the sign bit) although
    every current caller pre-filters to positives.
    """
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # NaN/Inf markers — callers pre-filter
        return None
    coeff = 0
    for digit in digits:
        coeff = coeff * 10 + digit
    shift = exponent + scale
    if shift >= 0:
        if len(digits) + shift > _I64_DIGITS:  # certainly past i64
            return None
        power: int = 10**shift  # declared int: `int ** int` types as Any
        scaled = coeff * power
    else:
        if -shift > len(digits):  # more fractional digits than exist ⇒ inexact
            return None
        divisor: int = 10**-shift  # declared int (same Any note)
        quotient, remainder = divmod(coeff, divisor)
        if remainder:
            return None
        scaled = quotient
    if scaled > _I64_MAX:
        return None
    return -scaled if sign else scaled


def _uniform_exponent_units(values: Sequence[Decimal]) -> tuple[int, list[int]] | None:
    """The strike ladder as ``(exponent, coefficients)`` — or ``None``.

    The derived strike step's byte identity includes its decimal EXPONENT
    (``Decimal("50")`` vs ``Decimal("50.00")`` are ``==`` but not the same
    bytes), and Python's set-dedup keeps an arbitrary representative among
    equal-but-distinctly-represented strikes — so the kernel engages ONLY
    when every qualifying strike shares ONE exponent ``e``: then values are
    exactly ``coeff × 10**e``, dedup/sort/min-gap over the coefficients is
    provably identical to the Decimal law, and the step materialises as
    ``Decimal(gap).scaleb(e)`` with the exact oracle exponent (Decimal
    subtraction's ideal exponent for exact results is ``min(e, e) = e``).
    Mixed exponents / an out-of-i64 coefficient / a non-finite marker →
    ``None`` → the Python oracle serves (hard rule 4 — never approximate).
    """
    exponents: set[int] = set()
    coefficients: list[int] = []
    for value in values:
        sign, digits, exponent = value.as_tuple()
        if not isinstance(exponent, int) or sign:  # non-finite / negative — pre-filtered
            return None
        exponents.add(exponent)
        if len(exponents) > 1:
            return None
        coeff = 0
        for digit in digits:
            coeff = coeff * 10 + digit
        if coeff > _I64_MAX:
            return None
        coefficients.append(coeff)
    if not coefficients:
        return None
    return exponents.pop(), coefficients


def _history_columns(history: Sequence[tuple[date, int]]) -> tuple[list[int], list[int]]:
    """Split a dated history table into parallel (ordinals, values) lists."""
    starts = [row[0].toordinal() for row in history]
    values = [row[1] for row in history]
    return starts, values


def _result_date(seam: str, ordinal: int) -> date:
    """Materialise a kernel result ordinal as a date (refuse-poison bounded)."""
    if not _MIN_ORDINAL <= ordinal <= _MAX_ORDINAL:
        msg = f"{seam}: kernel result ordinal {ordinal} outside the date domain"
        raise KernelResultRefused(msg)
    return date.fromordinal(ordinal)


def _prehistory_error(history: Sequence[tuple[date, int]], on_date: date) -> ValueError:
    """The expiry-calendar pre-history refusal — ``_lookup_in_history``'s bytes."""
    return ValueError(f"on_date {on_date} is before recorded history start ({history[0][0]})")


# ─────────────────────────────────────────────────────────────────
# Per-seam routed calls (the call sites' two-line gates land here)
# ─────────────────────────────────────────────────────────────────


def regime_rate(
    regimes: Sequence[RegimeRowLike],
    on_date: date,
    *,
    charge: str,
    exchange: str,
    python: Callable[[], int],
) -> int:
    """Routed :func:`brutex.costs.constants.rate_for_date` (seam ``regime_rate``).

    The kernel returns codes only; the refusal exceptions are composed HERE
    from the caller's own ``RegimeRow`` objects with the oracle lines'
    exact f-string shapes (VERIFIED-RATES-ONLY — module docstring). *python*
    is the verbatim oracle body; its raises (``ValueError`` ∪ the
    ``UnverifiedCostRegimeError`` subclass) are captured for burn-in parity.

    The OK-arm refuse-poison (``value ∈ table``) is STRUCTURAL, not
    semantic — it cannot tell a correct rate from a table rate wrongly
    returned on a refusal DATE (round-1 review). The semantic refusal
    guarantee for the production surface rests on (a) callers only ever
    passing the four shipped ``Final`` tables and (b) the exhaustive
    full-date-plane ON-vs-oracle differential over exactly those tables
    (± the boundary corpus, both ETC refusal rows) — defense-in-depth
    posture, per the module docstring's fail-closed layers.
    """

    def _kernel(kernel: ModuleType) -> Outcome:
        starts = [row.start.toordinal() for row in regimes]
        rates = [-1 if row.rate_bps_x100 is None else row.rate_bps_x100 for row in regimes]
        code, value = _code_value(
            "regime_rate", kernel.optcost_regime_rate(starts, rates, on_date.toordinal())
        )
        if code == _CODE_OK:
            if value < 0 or value not in rates:
                msg = f"regime_rate: kernel rate {value} not a table rate"
                raise KernelResultRefused(msg)
            return Outcome(value=value)
        if code == _CODE_REFUSAL:
            if not 0 <= value < len(regimes):
                msg = f"regime_rate: refusal row index {value} out of range"
                raise KernelResultRefused(msg)
            row = regimes[value]
            if row.rate_bps_x100 is not None:
                msg = f"regime_rate: row {value} is not a refusal row"
                raise KernelResultRefused(msg)
            from brutex.costs.types import UnverifiedCostRegimeError

            next_start = regimes[value + 1].start if value + 1 < len(regimes) else None
            return Outcome(
                error=UnverifiedCostRegimeError(
                    charge=charge,
                    exchange=exchange,
                    on_date=on_date,
                    window_start=row.start,
                    verified_from=next_start,
                    source=row.source,
                )
            )
        if code == _CODE_EMPTY_TABLE:
            return Outcome(error=ValueError("rate_for_date: empty regime table"))
        if code == _CODE_PRE_HISTORY:
            if not regimes:
                msg = "regime_rate: pre-history code on an empty table"
                raise KernelResultRefused(msg)
            return Outcome(
                error=ValueError(
                    f"rate_for_date: on_date {on_date} precedes the first recorded "
                    f"regime ({regimes[0].start}) — refusing to fabricate a rate"
                )
            )
        msg = f"regime_rate: unknown kernel code {code}"
        raise KernelResultRefused(msg)

    def _oracle() -> Outcome:
        try:
            return Outcome(value=python())
        except ValueError as exc:  # UnverifiedCostRegimeError ⊂ ValueError
            return Outcome(error=exc)

    return cast("int", _materialize(_route("regime_rate", kernel_call=_kernel, oracle=_oracle)))


def history_value(
    seam: str,
    history: Sequence[tuple[date, int]],
    on_date: date,
    *,
    prehistory_message: Callable[[], str],
    python: Callable[[], int],
) -> int:
    """Routed dated-history bisect — ``get_lot_size`` (seam ``lot_size``) and
    ``_lookup_in_history`` (seam ``expiry_dow``) share this one law.

    *prehistory_message* composes the caller's exact pre-history
    ``ValueError`` bytes lazily (only the refusal arm pays the format).
    """

    def _kernel(kernel: ModuleType) -> Outcome:
        starts, values = _history_columns(history)
        code, value = _code_value(
            seam, kernel.optcost_history_lookup(starts, values, on_date.toordinal())
        )
        if code == _CODE_OK:
            if value not in values:
                msg = f"{seam}: kernel value {value} not a table value"
                raise KernelResultRefused(msg)
            return Outcome(value=value)
        if code == _CODE_PRE_HISTORY:
            return Outcome(error=ValueError(prehistory_message()))
        msg = f"{seam}: unknown kernel code {code}"
        raise KernelResultRefused(msg)

    def _oracle() -> Outcome:
        try:
            return Outcome(value=python())
        except ValueError as exc:
            return Outcome(error=exc)

    return cast("int", _materialize(_route(seam, kernel_call=_kernel, oracle=_oracle)))


def next_weekly(
    history: Sequence[tuple[date, int]],
    on_or_after: date,
    *,
    python: Callable[[], date | None],
) -> date | None:
    """Routed :func:`brutex.options.expiry_calendar.next_weekly_expiry`
    (seam ``next_weekly``): ``NO_WEEKLY`` materialises as ``None``;
    pre-history composes ``_lookup_in_history``'s exact ``ValueError``.

    NESTED-ORACLE DISCLOSURE (round-1 review): flag-ON, *python* (the
    verbatim ``_next_weekly_expiry_python``) resolves its weekly DOW via
    ``get_weekly_dow`` → the gated ``expiry_dow`` seam, so once THAT seam's
    burn-in exhausts, this seam's burn-in compares kernel vs a
    partially-kernel-routed oracle. SAFE-biased, never masking: this
    kernel's internal lookup is a DISTINCT Rust path, so a shared-kernel
    defect surfaces as a burn-in MISMATCH (autorevert), never a masked
    pass — and the shared lookup kernel is independently differential-
    tested against a pure-Python oracle (the ``lot_size`` full-date-plane
    suite). Flag-OFF is pure Python end to end.
    """

    def _kernel(kernel: ModuleType) -> Outcome:
        starts, dows = _history_columns(history)
        code, value = _code_value(
            "next_weekly", kernel.optcost_next_weekly_expiry(starts, dows, on_or_after.toordinal())
        )
        if code == _CODE_OK:
            return Outcome(value=_result_date("next_weekly", value))
        if code == _CODE_NO_WEEKLY:
            return Outcome(value=None)
        if code == _CODE_PRE_HISTORY:
            return Outcome(error=_prehistory_error(history, on_or_after))
        msg = f"next_weekly: unknown kernel code {code}"
        raise KernelResultRefused(msg)

    def _oracle() -> Outcome:
        try:
            return Outcome(value=python())
        except ValueError as exc:
            return Outcome(error=exc)

    return cast(
        "date | None", _materialize(_route("next_weekly", kernel_call=_kernel, oracle=_oracle))
    )


def next_monthly(
    history: Sequence[tuple[date, int]],
    on_or_after: date,
    *,
    python: Callable[[], date],
) -> date:
    """Routed :func:`brutex.options.expiry_calendar.next_monthly_expiry`
    (seam ``next_monthly``). The kernel Errs on the December-9999 rollover
    Python's ``date`` refuses — that degrades to the oracle, which raises
    exactly as today. The :func:`next_weekly` NESTED-ORACLE DISCLOSURE
    applies verbatim (the oracle's ``get_monthly_dow`` rides the gated
    ``expiry_dow`` seam — safe-biased, independently pure-oracle-tested)."""

    def _kernel(kernel: ModuleType) -> Outcome:
        starts, dows = _history_columns(history)
        code, value = _code_value(
            "next_monthly",
            kernel.optcost_next_monthly_expiry(
                starts, dows, on_or_after.year, on_or_after.month, on_or_after.toordinal()
            ),
        )
        if code == _CODE_OK:
            return Outcome(value=_result_date("next_monthly", value))
        if code == _CODE_PRE_HISTORY:
            return Outcome(error=_prehistory_error(history, on_or_after))
        msg = f"next_monthly: unknown kernel code {code}"
        raise KernelResultRefused(msg)

    def _oracle() -> Outcome:
        try:
            return Outcome(value=python())
        except ValueError as exc:
            return Outcome(error=exc)

    return cast("date", _materialize(_route("next_monthly", kernel_call=_kernel, oracle=_oracle)))


def classify_graded[BucketT](
    *,
    is_put: bool,
    strike: Decimal,
    spot: Decimal,
    band: Decimal,
    itm: BucketT,
    atm: BucketT,
    otm: BucketT,
    python: Callable[[], BucketT],
) -> BucketT:
    """Routed graded arm of :func:`brutex.data.moneyness.classify`
    (seam ``classify``) — NA/UNKNOWN are decided at the call site pre-FFI.

    The graded bucket VALUES (``itm``/``atm``/``otm`` —
    :class:`brutex.data.moneyness.Moneyness` members at the one production
    call site) are INJECTED by the caller, so this facade never imports
    ``brutex.data.moneyness`` at any scope — not even under
    ``TYPE_CHECKING`` (the CODEQL FIX ROUND: code-scanning alerts 1308 +
    1309 flagged the ``moneyness`` ⇄ ``optcost_rust`` module-level import
    cycle; the injection mirrors the ``fno_symbol_rust`` precedent — the
    import graph is acyclic in BOTH directions, statically and at runtime,
    pinned by ``TestImportGraphAcyclic``).

    The paisa-exactness gate (hard rule 4): any input not exactly
    representable as i64 paisa routes SILENTLY to the oracle — never a
    rounded paisa, never a changed bucket. The module's never-raises
    contract is preserved on every path (kernel faults degrade to the
    oracle inside :func:`_route`). Kernel/oracle equivalence is proven for
    the DEFAULT decimal context ONLY (module docstring — the oracle's own
    ``spot − strike`` subtraction is ambient-context arithmetic).
    """

    def _oracle() -> Outcome:
        return Outcome(value=python())

    strike_paisa = _exact_scaled_int(strike, 2)
    spot_paisa = _exact_scaled_int(spot, 2)
    band_paisa = _exact_scaled_int(band, 2)
    if strike_paisa is None or spot_paisa is None or band_paisa is None:
        return python()

    graded: dict[int, BucketT] = {
        2: itm,
        3: atm,
        4: otm,
    }

    def _kernel(kernel: ModuleType) -> Outcome:
        code = _refuse_unless_int(
            "classify",
            kernel.optcost_moneyness_classify(is_put, strike_paisa, spot_paisa, band_paisa),
        )
        bucket = graded.get(code)
        if bucket is None:
            msg = f"classify: kernel code {code} outside the graded vocabulary"
            raise KernelResultRefused(msg)
        return Outcome(value=bucket)

    return cast("BucketT", _materialize(_route("classify", kernel_call=_kernel, oracle=_oracle)))


def atm_offset_paisa(
    strike: Decimal,
    spot: Decimal,
    step: Decimal,
    *,
    python: Callable[[], int],
) -> int:
    """Routed graded arm of :func:`brutex.data.moneyness.atm_offset`
    (seam ``atm_offset``) — the exact-integer ROUND_HALF_DOWN law, gated on
    i64-paisa exactness (inexact → the oracle, hard rule 4). Equivalence to
    the Decimal oracle is proven for the DEFAULT 28-digit decimal context
    ONLY (module docstring — a caller-modified ambient context skews the
    oracle's division, never the kernel; pinned as conscious)."""
    strike_paisa = _exact_scaled_int(strike, 2)
    spot_paisa = _exact_scaled_int(spot, 2)
    step_paisa = _exact_scaled_int(step, 2)
    if strike_paisa is None or spot_paisa is None or step_paisa is None:
        return python()

    def _kernel(kernel: ModuleType) -> Outcome:
        return Outcome(
            value=_refuse_unless_int(
                "atm_offset", kernel.optcost_atm_offset(strike_paisa, spot_paisa, step_paisa)
            )
        )

    def _oracle() -> Outcome:
        return Outcome(value=python())

    return cast("int", _materialize(_route("atm_offset", kernel_call=_kernel, oracle=_oracle)))


def strike_step(
    qualifying: Sequence[Decimal],
    *,
    python: Callable[[], Decimal | None],
) -> Decimal | None:
    """Routed pure-observed arm of
    :func:`brutex.data.moneyness.derive_strike_step` (seam ``strike_step``).

    Engages ONLY on a uniform-exponent ladder (see
    :func:`_uniform_exponent_units` — the derived step's byte identity
    includes its exponent); otherwise the oracle serves. Burn-in compares
    on ``as_tuple()`` so an exponent drift can never ship. Kernel/oracle
    equivalence is proven for the DEFAULT decimal context ONLY (module
    docstring): the oracle's gap subtractions AND this seam's own
    ``Decimal(gap).scaleb(exponent)`` materialisation are ambient-context
    arithmetic — exact under the default context (i64 coefficients ≤ 19
    digits < 28), both context-rounded under a caller-reduced ``prec``
    (verified: prec=6 rounds a 10000001 gap to ``1.00000E+7`` on BOTH arms).
    """
    units = _uniform_exponent_units(qualifying)
    if units is None:
        return python()
    exponent, coefficients = units

    def _kernel(kernel: ModuleType) -> Outcome:
        code, value = _code_value("strike_step", kernel.optcost_strike_step(coefficients))
        if code == _CODE_OK:
            if value <= 0:
                msg = f"strike_step: kernel step {value} not positive"
                raise KernelResultRefused(msg)
            return Outcome(value=Decimal(value).scaleb(exponent))
        if code == _CODE_NO_STEP:
            return Outcome(value=None)
        msg = f"strike_step: unknown kernel code {code}"
        raise KernelResultRefused(msg)

    def _oracle() -> Outcome:
        return Outcome(value=python())

    return cast(
        "Decimal | None",
        _materialize(
            _route("strike_step", kernel_call=_kernel, oracle=_oracle, equal=_decimal_outcome_equal)
        ),
    )


def offset_strike(
    atm_strike: int,
    step_count: int,
    strike_step_value: int,
    *,
    is_bull: bool,
    python: Callable[[], int],
) -> int:
    """Routed :func:`brutex.options.strike_rules.resolve_offset_strike`
    arithmetic (seam ``offset_strike``) — the non-OFFSET-rule ``ValueError``
    is decided at the call site pre-FFI. Kernel i64 overflow degrades to
    the oracle (Python ints are unbounded — same answer, no refusal)."""

    def _kernel(kernel: ModuleType) -> Outcome:
        return Outcome(
            value=_refuse_unless_int(
                "offset_strike",
                kernel.optcost_offset_strike(atm_strike, step_count, strike_step_value, is_bull),
            )
        )

    def _oracle() -> Outcome:
        return Outcome(value=python())

    return cast("int", _materialize(_route("offset_strike", kernel_call=_kernel, oracle=_oracle)))


def strike_offset(
    atm_strike: int,
    offset_steps: int,
    step: int,
    *,
    python: Callable[[], int],
) -> int:
    """Routed :func:`brutex.options.strike_grid.strike_offset_from_atm`
    (seam ``grid_offset``)."""

    def _kernel(kernel: ModuleType) -> Outcome:
        return Outcome(
            value=_refuse_unless_int(
                "grid_offset",
                kernel.optcost_strike_offset_from_atm(atm_strike, offset_steps, step),
            )
        )

    def _oracle() -> Outcome:
        return Outcome(value=python())

    return cast("int", _materialize(_route("grid_offset", kernel_call=_kernel, oracle=_oracle)))


def grid_around(
    atm_strike: int,
    step: int,
    half_width: int,
    *,
    python: Callable[[], list[int]],
) -> list[int]:
    """Routed :func:`brutex.options.strike_grid.grid_around_atm`
    (seam ``grid_around``) — the kernel list is length- and
    element-validated (refuse-poison) before it can serve."""

    def _kernel(kernel: ModuleType) -> Outcome:
        chain = kernel.optcost_grid_around_atm(atm_strike, step, half_width)
        if not isinstance(chain, list):
            msg = f"grid_around: kernel returned {type(chain).__name__}, expected list"
            raise KernelResultRefused(msg)
        expected_len = max(0, 2 * half_width + 1)
        if len(chain) != expected_len:
            msg = f"grid_around: kernel chain length {len(chain)} != {expected_len}"
            raise KernelResultRefused(msg)
        return Outcome(value=[_refuse_unless_int("grid_around", value) for value in chain])

    def _oracle() -> Outcome:
        return Outcome(value=python())

    return cast(
        "list[int]", _materialize(_route("grid_around", kernel_call=_kernel, oracle=_oracle))
    )


def round_atm(
    spot_price: float,
    step: int,
    *,
    python: Callable[[], int],
) -> int:
    """Routed :func:`brutex.options.strike_grid.round_to_atm`
    (seam ``round_to_atm``) — the one sanctioned f64 FFI crossing (the
    Python function's own float-rupee input domain). Outside the kernel's
    provable envelope (non-finite spot, out-of-i64 quotient) it raises →
    the oracle serves, raising exactly as today: a NaN OR infinite spot
    reaches ``int()`` as NaN (``inf // step`` is NaN in CPython float floor
    division — VERIFIED, not assumed) → the NaN ``ValueError``;
    ``OverflowError`` stays in the capture set as a defensive belt for the
    ``int()`` family's other documented raise — both captured for burn-in
    TYPE + message parity."""

    def _kernel(kernel: ModuleType) -> Outcome:
        return Outcome(
            value=_refuse_unless_int("round_to_atm", kernel.optcost_round_to_atm(spot_price, step))
        )

    def _oracle() -> Outcome:
        try:
            return Outcome(value=python())
        except (ValueError, OverflowError) as exc:
            return Outcome(error=exc)

    return cast("int", _materialize(_route("round_to_atm", kernel_call=_kernel, oracle=_oracle)))


__all__ = [
    "BURN_IN_SAMPLES",
    "OPTCOST_TAILS_RUST_ENV",
    "KernelResultRefused",
    "Outcome",
    "atm_offset_paisa",
    "classify_graded",
    "env_flag_tristate",
    "grid_around",
    "history_value",
    "next_monthly",
    "next_weekly",
    "offset_strike",
    "optcost_rust_disabled",
    "optcost_rust_enabled",
    "outcome_equal",
    "regime_rate",
    "reset_optcost_rust_for_tests",
    "round_atm",
    "strike_offset",
    "strike_step",
]
