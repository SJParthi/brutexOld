"""Default-OFF Rust seam for the ``normalise_tick_size`` law (slice T35 PR-2).

The ONE routed surface is :func:`brutex.models.normalise_tick_size` — the
module's single pure FUNCTION law. When ``BRUTEX_CORE_RUST_MODELS`` is ON it
delegates to the ``brutex_core`` crate (``rust/brutex-core/src/models.rs``,
exact decimal-STRING arithmetic) through this facade; the flag-OFF default
is the verbatim CPython oracle (``brutex.models._normalise_tick_size_python``),
behavior-identical AND crate-untouched (bomb-crate spy-pinned).

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed. ``normalise_tick_size`` has ZERO
production consumers outside ``brutex/models.py`` itself (exported public
API + unit-tested; grep census 2026-07-17), so flag-ON buys uniformity for
literally no runtime — shipped INERT (a default flip needs its own DEC).

THE TYPE-PORT NO-GO (the PR-2 design verdict, recorded here + in the
ledger): the REST of ``brutex/models.py`` — the four ``StrEnum`` taxonomies,
the frozen-slots ``Instrument`` dataclass and the ``BruteXError`` exception
root — is deliberately NOT hoisted onto pyo3 pyclasses. A pyo3 pyclass
cannot subclass CPython ``str`` (every ``StrEnum`` member IS a str —
``isinstance(m, str)``, ``json.dumps``, DuckDB binding, lake-path
interpolation all depend on it), cannot satisfy the ``dataclasses``
protocol (production calls ``dataclasses.replace`` on ``Instrument`` —
``brutex/dashboard/instrument_loader.py`` leg-economics fold), and a
type — unlike a function law — admits NO default-OFF seam: an absent
optional wheel cannot "degrade" a class identity, while a Python-twin
fallback would fork class identity per environment (pickle/isinstance
divergence across spawn workers). The trading-core VwapState hoist
precedent relied on a HARD-required wheel; ``brutex_core`` is optional by
the PR-1 design. The frozen type contract is mechanized instead in
``tests/unit/test_models_type_contract.py``.

The :mod:`brutex.money_rust` facade pattern, with the SAME two deliberate
structural differences (a LOCAL flag-vocabulary twin — ``brutex/models.py``
is a ROOT leaf imported by ~105 modules, so this facade must import
nothing from ``brutex.data``/``brutex.engine``; and NO version guard —
the crate side is pure std-Rust digit arithmetic locked to the stdlib
``decimal`` law, the burn-in + goldens are the drift teeth) plus ONE new
protocol:

## The REFUSE-vs-FAULT protocol (the crate contract)

``normalise_tick_size`` has TWO legitimate oracle outcomes — a member
``Decimal`` value, or a ``TickSizeFormatError`` raise. The crate signals a
law-level refusal as a ``ValueError`` whose message starts with the
declared :data:`REFUSE_PREFIX` marker; the facade then serves the ORACLE's
raise (exact message bytes stay 100% Python-owned — the ``refusal_error``
injection below verifies the oracle really raises that type). Any OTHER
crate error is a FAULT → degrade-to-oracle with one WARN. Burn-in
bit-compares the OUTCOME (kind + canonical value string) both arms.

Fail-closed layers, in order (every path serves the PYTHON-law outcome —
value or raise — never a new value class):

1. the caller-side flag gate in :mod:`brutex.models`
   (:func:`models_rust_enabled` — default OFF; junk values loud-OFF once
   per process);
2. the ELIGIBILITY gate (:func:`_encode_tick_input` — the gate law below):
   an input whose oracle behavior the crate grammar cannot reproduce
   byte-for-byte is routed to the oracle WITHOUT touching the crate;
3. the import/API-surface guard (:func:`_resolve_crate` — absent wheel
   latches ``crate_unavailable``; a wheel missing the callable latches
   ``crate_stale``);
4. the refuse-poison gate — a non-``str`` crate return, or a returned
   string outside the frozen 7-member :data:`_VALID_2DP_STRINGS` set, is
   refused with one WARN (a poisoned crate can only degrade, never serve);
5. the per-process burn-in self-check — the first :data:`BURN_IN_SAMPLES`
   engaged calls compute BOTH arms and compare outcomes; ANY difference
   ships the ORACLE outcome and autoreverts the Rust path for the process
   (the #1531/#1532 pattern);
6. the degrade set (:data:`_DEGRADE_ERRORS` + the pyo3 panic belt) — a
   crate fault serves the oracle with one WARN, never a new crash class.

``MemoryError`` / ``KeyboardInterrupt`` / ``SystemExit`` re-raise RAW (the
``errors.py`` escape-set doctrine).

## The ELIGIBILITY GATE LAW (layer 2 — the strings-only FFI contract)

Only inputs whose Python-type dispatch and value rendering the crate
grammar reproduces exactly may cross (anything else → oracle, crate
untouched, spy-pinned):

* exact-type :class:`~decimal.Decimal` (a subclass's overridden methods
  must reach the oracle), FINITE (``NaN`` quantizes to ``NaN`` → the
  oracle's membership raise; ``sNaN``/``±Inf`` raise raw
  ``InvalidOperation`` — today's law, preserved by routing them there),
  ``adjusted() <= 6`` and derived exponent ``>= -30`` (bounds the
  ``format(d, "f")`` crossing string; a bigger candidate raises raw
  ``InvalidOperation`` at quantize under default prec — oracle territory);
* exact-type :class:`str` matching the strict ASCII fixed-point grammar
  ``-?\\d+(\\.\\d+)?`` (≤ 64 chars): Python ``Decimal()`` additionally
  accepts whitespace / exponents / Unicode digits / ``NaN`` — those parse
  DIFFERENTLY from the crate grammar, so they take the oracle;
* exact-type :class:`int` with ``|raw| < 10**9`` (``bool`` is an ``int``
  subclass — the oracle owns the bool trap; the crate owns the
  ``[1, 1000)`` paise-range DECISION, the facade only bounds the string);
* exact-type :class:`float`, finite, whose ``str(raw)`` shortest-repr
  matches the same fixed-point grammar (``str(1e16)`` → ``"1e+16"`` etc.
  take the oracle);
* AND, for every kind, a non-hazardous ambient decimal context: no
  ``Inexact``/``Rounded`` trap armed (the oracle's quantize/division sets
  those flags — armed traps make it RAISE where the context-independent
  crate returns a value), the ambient ROUNDING mode exactly
  ``ROUND_HALF_EVEN`` (the crate's pinned mode; the oracle's un-pinned
  ``quantize(Decimal("0.01"))`` follows the ambient mode, so any other
  mode can round a near-boundary candidate to a DIFFERENT 2dp string than
  the crate — a member ⟷ non-member flip; HALF_EVEN is Python decimal's
  DEFAULT, so the default context stays eligible), and ``prec >= 12`` /
  ``Emax >= 12`` / ``Emin <= -12`` (the whole candidate envelope fits
  with headroom — a narrower window can raise
  ``InvalidOperation``/``Overflow`` in the oracle; the money-seam r2
  context-narrowing lesson applied from birth).
"""

from __future__ import annotations

import importlib
import math
import os
import re
from decimal import ROUND_HALF_EVEN, Decimal, Inexact, Rounded, getcontext
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The opt-in env flag (default OFF; Pattern A — only explicit truthy
#: enables). Canonical ``BRUTEX_<CRATE>_RUST_<SEAM>`` form.
MODELS_RUST_ENV: Final[str] = "BRUTEX_CORE_RUST_MODELS"

#: Engaged calls outcome-compared against the Python oracle before crate
#: strings ship un-oracled (the #1531/#1532 burn-in law).
BURN_IN_SAMPLES: Final[int] = 16

#: The crate's declared law-refusal marker (``rust/brutex-core/src/models.rs``
#: ``REFUSE_PREFIX`` — mirror-pinned by the facade suite).
REFUSE_PREFIX: Final[str] = "tick_refuse:"

#: The process-local autorevert latch — ``[None]`` = live; a reason string
#: latches the Rust path OFF for the remainder of the process.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Burn-in budget (mutated in place; reset seam below). ONE law token —
#: this seam routes exactly one surface.
_BURN_IN_REMAINING: Final[list[int]] = [BURN_IN_SAMPLES]

#: Warn-once registry (per-process, test-resettable).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (never a warn reason).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The law token (log/latch reason prefixes).
_LAW: Final[str] = "normalise_tick_size"

#: The crate callable this seam requires (the API-surface handshake).
_CRATE_API: Final[tuple[str, ...]] = ("models_normalise_tick_size_str",)

#: The narrow degrade-to-oracle catch set for NON-refusal faults.
#: ``ValueError`` is handled SEPARATELY first (the refuse-vs-fault split);
#: ``RuntimeError`` is the crate's declared panic mapping;
#: ``TypeError``/``OverflowError`` are PyO3 argument-conversion failures;
#: ``AttributeError`` the broken-crate-internals belt. ``MemoryError`` is
#: DELIBERATELY absent (module docstring).
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: Inline flag vocabulary (LEAF discipline — module docstring; the
#: mirror-pin test asserts byte-equality with ``sweep_lookahead``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The frozen valid-member set in the canonical 2dp string form — the
#: refuse-poison gate law (mirror-pinned against
#: ``brutex.models.VALID_TICK_SIZES_RUPEES`` by the facade suite).
_VALID_2DP_STRINGS: Final[frozenset[str]] = frozenset(
    {"0.01", "0.05", "0.10", "0.25", "0.50", "1.00", "5.00"}
)

#: Eligibility-gate bounds (the gate law — module docstring).
_MAX_ADJUSTED: Final[int] = 6
_MIN_EXPONENT: Final[int] = -30
_MAX_STR_LEN: Final[int] = 64
_MAX_INT_MAGNITUDE: Final[int] = 10**9
_MIN_PREC: Final[int] = 12
_MIN_SAFE_EMAX: Final[int] = 12
_MAX_SAFE_EMIN: Final[int] = -12

#: The strict ASCII fixed-point grammar the crate parses (``re.ASCII`` so
#: Unicode digits — which Python ``Decimal()`` accepts — never match).
_FIXED_POINT_RE: Final[re.Pattern[str]] = re.compile(r"-?\d+(\.\d+)?", re.ASCII)


def models_rust_enabled() -> bool:
    """Resolve ``BRUTEX_CORE_RUST_MODELS`` (default OFF, junk loud-OFF).

    Truthy → ``True``; falsy → ``False`` (silent explicit OFF); unset →
    ``False`` off the raw probe alone (the money-seam absent fast path);
    any OTHER non-empty value → ``False`` with one ``models_rust.fallback``
    WARN (reason ``flag_unrecognised``) per process. Read per call so
    spawn workers resolve their own inherited environment (the PR-1
    posture).
    """
    raw = os.environ.get(MODELS_RUST_ENV)
    if raw is None:
        return False
    stripped = raw.strip().lower()
    if stripped in _TRUTHY:
        return True
    if stripped and stripped not in _FALSY:
        _warn_once("flag_unrecognised", value=stripped)
    return False


def models_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_models_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budget + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state so
    the adopted value is the crate's with no oracle in the path (the
    parity legs depend on that). Never called from production paths (the
    PR-1 reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[0] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``models_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("models_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``models_rust.autoreverted`` at ERROR (the burn-in
    autorevert); guard latches log ``models_rust.rust_path_disabled`` at
    WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("models_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("models_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_core``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A
    wheel that imports but lacks the callable :data:`_CRATE_API` member is
    a pre-models-seam (or foreign/broken) install — latch ``crate_stale``
    (no version guard — module docstring). Fail-closed: never an
    ``AttributeError`` out of this seam.
    """
    try:
        crate = importlib.import_module("brutex_core")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-core (maturin build; see rust/brutex-core/README.md)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_core.{name} missing or not callable — "
                "the installed wheel predates the models seam (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-core (see rust/brutex-core/README.md)",
            )
            return None
    return crate


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — the ``money_rust`` twin.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak; resolved GUARDEDLY by name and folded beside
    ``Exception`` (the CodeQL ``py/catch-base-exception`` convention). The
    crate's own ``catch_unwind`` maps panics to ``RuntimeError`` (the
    degrade set), so this belt is defense-in-depth over a broken boundary.
    """
    belt: list[type[BaseException]] = [Exception]
    try:
        module = importlib.import_module("pyo3_runtime")
    except ImportError:
        return tuple(belt)
    panic = getattr(module, "PanicException", None)
    if isinstance(panic, type) and issubclass(panic, BaseException) and panic not in belt:
        belt.append(panic)
    return tuple(belt)


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``models_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("models_rust.engaged", crate_version=getattr(crate, "__version__", None))


def _context_hazard() -> bool:
    """``True`` iff the ambient decimal context could make the oracle
    DIVERGE from the context-independent crate (gate law): an armed
    ``Inexact``/``Rounded`` trap (the oracle RAISES where the crate
    returns a value), an ambient rounding mode other than
    ``ROUND_HALF_EVEN`` (the crate's pinned mode — the oracle's un-pinned
    ``quantize`` follows the ambient mode, so any other mode can round a
    near-boundary candidate to a DIFFERENT 2dp string, flipping
    member ⟷ non-member), or a precision/exponent window narrower than
    the law's whole candidate envelope.
    """
    ctx = getcontext()
    if ctx.traps[Inexact] or ctx.traps[Rounded]:
        return True
    if ctx.rounding != ROUND_HALF_EVEN:
        return True
    if ctx.prec < _MIN_PREC:
        return True
    return ctx.Emax < _MIN_SAFE_EMAX or ctx.Emin > _MAX_SAFE_EMIN


def _encode_tick_input(raw: float | int | Decimal | str) -> tuple[str, str] | None:
    """The eligibility gate + kind/payload encoder (module docstring gate
    law). ``None`` = route to the oracle without touching the crate.

    Safety invariant: the gate need NOT exclude every oracle-raise input —
    the load-bearing law HOLDS GIVEN the gate's rounding guard: the gate
    requires the ambient rounding mode to be ``ROUND_HALF_EVEN`` (the
    crate's pinned mode), so crate-accepts-a-member ⟹ the oracle's
    ambient-mode quantize produces the same member without raising; every
    other input the crate can only REFUSE, and refuse→serve-oracle
    reproduces the oracle's behavior including its raw raises.
    """
    if _context_hazard():
        return None
    if type(raw) is Decimal:
        if not raw.is_finite():
            return None
        adjusted = raw.adjusted()
        # exponent = adjusted − len(digits) + 1 (the as_tuple identity,
        # computed int-only — the money-seam mypy-strict idiom).
        exponent = adjusted - len(raw.as_tuple().digits) + 1
        if adjusted > _MAX_ADJUSTED or exponent < _MIN_EXPONENT:
            return None
        return ("decimal", format(raw, "f"))
    if type(raw) is str:
        if len(raw) > _MAX_STR_LEN or _FIXED_POINT_RE.fullmatch(raw) is None:
            return None
        return ("str", raw)
    if type(raw) is int:
        # bool is excluded by the exact-type check — the oracle owns the
        # bool trap; the crate owns the [1, 1000) paise-range DECISION.
        if not -_MAX_INT_MAGNITUDE < raw < _MAX_INT_MAGNITUDE:
            return None
        return ("int", str(raw))
    if type(raw) is float:
        if not math.isfinite(raw):
            return None
        rendered = str(raw)
        if len(rendered) > _MAX_STR_LEN or _FIXED_POINT_RE.fullmatch(rendered) is None:
            return None
        return ("float", rendered)
    return None


def _oracle_outcome(
    python_law: Callable[[float | int | Decimal | str], Decimal],
    raw: float | int | Decimal | str,
    refusal_error: type[Exception],
) -> tuple[str, str]:
    """Run the oracle for a burn-in compare: ``("value", str(v))`` or
    ``("refuse", "")``. Any raise OTHER than *refusal_error* propagates —
    it is today's law (e.g. the raw ``InvalidOperation`` arm), and if the
    crate produced an outcome for such an input the eligibility gate is
    wrong: the caller's autorevert-on-mismatch never runs, but the raise
    itself IS the oracle's outcome, served verbatim.
    """
    try:
        return ("value", str(python_law(raw)))
    except refusal_error:
        return ("refuse", "")


def route_normalise_tick_size(
    raw: float | int | Decimal | str,
    python_law: Callable[[float | int | Decimal | str], Decimal],
    refusal_error: type[Exception],
) -> Decimal:
    """The routed ``normalise_tick_size`` — called by :mod:`brutex.models`
    ONLY when the flag is ON. EVERY path serves the Python-law OUTCOME:
    the member ``Decimal``, or the oracle's typed raise (*refusal_error* =
    ``TickSizeFormatError``, injected to avoid a module cycle — this
    facade must not import :mod:`brutex.models`).
    """
    if models_rust_disabled():
        return python_law(raw)
    encoded = _encode_tick_input(raw)
    if encoded is None:
        return python_law(raw)
    crate = _resolve_crate()
    if crate is None:
        return python_law(raw)
    kind, payload = encoded
    # ``adopted is None`` ⟺ the crate signalled the LAW refusal outcome.
    adopted: str | None = None
    try:
        rust_value: object = crate.models_normalise_tick_size_str(kind, payload)
    except ValueError as exc:
        if not str(exc).startswith(REFUSE_PREFIX):
            _warn_once(f"{_LAW}_error", error_detail=repr(exc))
            return python_law(raw)
        # else: a LAW refusal — the oracle raise is the outcome (adopted None).
    except _DEGRADE_ERRORS as exc:
        _warn_once(f"{_LAW}_error", error_detail=repr(exc))
        return python_law(raw)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine) — sits ABOVE the panic
        # belt so its `Exception` member can never swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(raw)
    else:
        if not isinstance(rust_value, str) or rust_value not in _VALID_2DP_STRINGS:
            # Refuse-poison gate: a non-str return or a non-member string
            # is a crate BUG — never served (structurally unreachable
            # through the real crate; guards a stale/foreign install).
            head = repr(rust_value[:24]) if isinstance(rust_value, str) else None
            _warn_once(
                f"{_LAW}_result_refused",
                result_type=type(rust_value).__name__,
                value_head=head,
            )
            return python_law(raw)
        adopted = rust_value
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[0] > 0:
        crate_outcome = ("refuse", "") if adopted is None else ("value", adopted)
        if _oracle_outcome(python_law, raw, refusal_error) != crate_outcome:
            _disable(
                f"burn_in_mismatch_{_LAW}",
                error=True,
                rust_outcome=crate_outcome,
            )
            return python_law(raw)
        _BURN_IN_REMAINING[0] -= 1
        if _BURN_IN_REMAINING[0] == 0:
            _LOG.info("models_rust.burn_in_complete", law=_LAW, samples=BURN_IN_SAMPLES)
    if adopted is None:
        return python_law(raw)  # raises refusal_error with today's exact bytes
    return Decimal(adopted)


__all__ = [
    "BURN_IN_SAMPLES",
    "MODELS_RUST_ENV",
    "REFUSE_PREFIX",
    "models_rust_disabled",
    "models_rust_enabled",
    "reset_models_rust_for_tests",
    "route_normalise_tick_size",
]
