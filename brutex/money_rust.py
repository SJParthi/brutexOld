"""Default-OFF Rust seam for the DEC-MONEY-2DP-001 money laws (slice T35).

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). The FOUR routed surfaces
are :mod:`brutex.money`'s public functions — when ``BRUTEX_CORE_RUST_MONEY``
is ON they delegate to the ``brutex_core`` crate
(``rust/brutex-core/src/money.rs``, exact decimal-STRING arithmetic)
through this facade; the flag-OFF default is the verbatim CPython
``Decimal`` oracle, byte-identical AND crate-untouched (bomb-crate
spy-pinned).

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed. Every one of these laws is a
sub-microsecond ``Decimal`` op; flag-ON ADDS an env read, an eligibility
gate and an FFI hop per call — a small slowdown, shipped INERT (a default
flip needs its own DEC). ``round_money`` additionally snaps EVERY stored
OHLC value at the lake-write boundary (run-identity / stored-bytes
critical — the audit-session addendum), which is exactly why the seam is
burn-in-checked, refuse-poison-gated and default-OFF.

Flag-OFF cost disclosure (review r1, measured 2026-07-17): the DEFAULT
(flag-OFF) path pays a per-call env probe — ``os.environ.get`` on a miss
is ~700 ns/call on the build box (an exception-mediated ``_Environ``
lookup), putting the OFF-path ``round_money`` at ~1.1 µs/call vs the
~225 ns pre-seam body; the unset-fast-path in :func:`money_rust_enabled`
shaves the strip/lower/set-lookup tail (~65 ns) but the probe itself is
DELIBERATE — spawn workers must read their own inherited environment
(the PR-1 posture; caching the resolve would break that contract). The
lake-write HOT path is unaffected: the vectorized ``_snap_price_expr``
never calls scalar ``round_money`` — the scalar surfaces run O(bars) at
ingest / O(finalists) at run-end, aggregate ~seconds.

The :mod:`brutex.data.lake_rust_stats` facade pattern, with TWO deliberate
structural differences, both documented:

* **A LOCAL flag-vocabulary twin, not an import** (the
  :mod:`brutex.feed_rust` precedent): ``brutex/money.py`` is a ROOT leaf —
  ``brutex.data.parquet_writer`` (and ~19 other modules across data /
  engine / dashboard / costs) import it at module scope, so this facade
  must NOT import ``env_flag_tristate`` from
  ``brutex.data.lake_rust_render`` (executing ``brutex/data/__init__``
  from under ``brutex.money`` would be a circular import) nor from
  ``brutex.engine.sweep_lookahead`` (``brutex/engine/__init__``
  hard-imports the ``fno_math`` wheel — a money render must never require
  the engine wheel). The inline ``_TRUTHY``/``_FALSY`` sets are
  mirror-pinned against ``sweep_lookahead``'s by a vocabulary-equality
  test (``tests/unit/test_money_rust.py``), so the homes cannot drift
  silently.
* **No version guard** (the ``lake_rust_stats`` reasoning verbatim): the
  crate side is pure std-Rust digit arithmetic locked to the *stdlib*
  ``decimal`` byte law — there is no wheel↔crate lockstep to attest. The
  per-law burn-in bit-compare + the frozen literal goldens (Python:
  ``tests/golden/test_money_rounding_golden.py``; cargo: the snap-corpus
  cross-crate pin) are the drift teeth instead.

Fail-closed layers, in order (every path returns the PYTHON-law value):

1. the caller-side flag gate in :mod:`brutex.money`
   (:func:`money_rust_enabled` — default OFF; junk values loud-OFF once
   per process);
2. the ELIGIBILITY gate (:func:`_eligible_rupees` /
   :func:`_eligible_paisa` — the gate law below): an input whose oracle
   behavior could be raise-vs-value divergent is routed to the oracle
   WITHOUT touching the crate;
3. the import/API-surface guard (:func:`_resolve_crate` — absent wheel
   latches ``crate_unavailable``; a wheel missing a callable latches
   ``crate_stale``);
4. the refuse-poison gate — a non-``str`` crate return, or a returned
   string failing its law's cheap shape check, is refused with one WARN
   (a poisoned crate can only degrade, never serve);
5. the per-law burn-in self-check — the first :data:`BURN_IN_SAMPLES`
   engaged calls PER LAW compute BOTH arms and compare the canonical
   strings; ANY difference ships the ORACLE value and autoreverts the
   Rust path for the process (the #1531/#1532 pattern; per-law budgets
   per the :mod:`brutex.data.window_ledger_rust` precedent);
6. the degrade set (:data:`_DEGRADE_ERRORS` + the pyo3 panic belt) — a
   crate raise serves the oracle with one WARN, never a new crash class.

``MemoryError`` / ``KeyboardInterrupt`` / ``SystemExit`` re-raise RAW (the
``errors.py`` escape-set doctrine): money laws run inside cost/lake-write
paths whose upstream policy treats an OOM as a global stop — this seam
must not launder one into a fallback.

## The ELIGIBILITY GATE LAW (layer 2 — the strings-only FFI contract)

Only values whose oracle outcome is a PLAIN VALUE under the AMBIENT
decimal context may cross (anything else → oracle, crate untouched,
spy-pinned):

* rupee ``Decimal``s: exact type :class:`~decimal.Decimal` (a subclass's
  overridden methods must reach the oracle — the ``window_ledger_rust``
  exact-type precedent; a non-Decimal reaches the oracle and reproduces
  today's exact ``AttributeError``), FINITE (``NaN``/``sNaN``/``±Inf``
  quantize-raise in the oracle — preserved by routing them there),
  ``as_tuple().exponent >= -60`` and ``adjusted() <= 20`` (bounds the
  ``format(d, "f")`` crossing string to ~85 chars — every real money
  value is orders of magnitude inside), ``adjusted() + 5 <=
  getcontext().prec`` (the quantize/×100 headroom law: a 2dp quantize
  needs ``adjusted+3`` result digits and the ``× 100`` shift two more —
  past that the oracle raises ``InvalidOperation`` at default traps, so
  those inputs must take the oracle), and NO ``Inexact``/``Rounded``
  trap armed (a caller that traps them makes the oracle RAISE on any
  rounding — raise-vs-value divergence, so trapped contexts take the
  oracle wholesale);
* paisa ``int``s: exact type :class:`int` (``bool`` is an ``int``
  subclass whose ``str()`` is not a digit string — oracle), ``|paisa| <
  2**63`` (the milestone gate law; the crossing is a string either way —
  this bounds the crate's worst case), digit-count headroom
  ``len(str(abs(paisa))) + 2 <= getcontext().prec`` (the ``/ 100`` +
  quantize exactness law under a shrunken ambient precision), and the
  same trap conjunct.
"""

from __future__ import annotations

import importlib
import os
from decimal import Decimal, Inexact, Rounded, getcontext
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The opt-in env flag (default OFF; Pattern A — only explicit truthy
#: enables). Canonical ``BRUTEX_<CRATE>_RUST_<SEAM>`` form.
MONEY_RUST_ENV: Final[str] = "BRUTEX_CORE_RUST_MONEY"

#: Engaged calls bit-compared against the Decimal oracle PER LAW before
#: crate strings ship un-oracled (the #1531/#1532 burn-in law).
BURN_IN_SAMPLES: Final[int] = 16

#: The four law tokens (per-law burn-in budgets + reason prefixes).
_LAW_ROUND: Final[str] = "round_money"
_LAW_TO_PAISA: Final[str] = "rupees_to_paisa"
_LAW_TO_RUPEES: Final[str] = "paisa_to_rupees"
_LAW_FORMAT: Final[str] = "format_inr"

#: The process-local autorevert latch — ``[None]`` = live; a reason string
#: latches the Rust path OFF for the remainder of the process.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_ROUND: BURN_IN_SAMPLES,
    _LAW_TO_PAISA: BURN_IN_SAMPLES,
    _LAW_TO_RUPEES: BURN_IN_SAMPLES,
    _LAW_FORMAT: BURN_IN_SAMPLES,
}

#: Warn-once registry (per-process, test-resettable — money laws run at
#: per-value frequency; a sticky reason would flood the logs).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (never a warn reason).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The crate callables this seam requires (the API-surface handshake).
_CRATE_API: Final[tuple[str, ...]] = (
    "round_money_str",
    "rupees_to_paisa_str",
    "paisa_to_rupees_str",
    "format_inr_str",
)

#: The narrow degrade-to-oracle catch set. ``ValueError`` is the crate's
#: declared parse/shape refusal; ``RuntimeError`` its declared panic
#: mapping; ``TypeError``/``OverflowError`` are PyO3 argument-conversion
#: failures; ``AttributeError`` the broken-crate-internals belt.
#: ``MemoryError`` is DELIBERATELY absent (module docstring).
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: Inline flag vocabulary (LEAF discipline — module docstring; the
#: mirror-pin test asserts byte-equality with ``sweep_lookahead``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Eligibility-gate bounds (the gate law — module docstring).
_MAX_ADJUSTED: Final[int] = 20
_MIN_EXPONENT: Final[int] = -60
_PREC_HEADROOM: Final[int] = 5
_MAX_PAISA: Final[int] = 2**63

#: ASCII-strict digit set for the refuse-poison shape checks (``int()`` /
#: ``Decimal()`` would happily parse Unicode digits and ``int()`` even
#: ``"1_0"`` — the validators, not the constructors, are the gate).
_ASCII_DIGITS: Final[frozenset[str]] = frozenset("0123456789")

#: Shape-law lengths: a 2dp body is at least ``D.dd`` (4 chars) and a
#: display grouping chunk is exactly 3 digits (Python ``:,`` grouping).
_MIN_FIXED2_LEN: Final[int] = 4
_GROUP_LEN: Final[int] = 3


def money_rust_enabled() -> bool:
    """Resolve ``BRUTEX_CORE_RUST_MONEY`` (default OFF, junk loud-OFF).

    Truthy → ``True``; falsy → ``False`` (silent explicit OFF); unset /
    whitespace-empty → ``False`` (the default); any OTHER non-empty value →
    ``False`` with one ``money_rust.fallback`` WARN (reason
    ``flag_unrecognised``) per process. Read per call so spawn workers
    resolve their own inherited environment (the PR-1 posture).

    ABSENT fast path (review r1, MINOR): an UNSET flag — the production
    default — returns ``False`` off the raw ``environ`` probe alone,
    skipping the strip/lower/set-lookup normalization. Semantically
    identical (unset → OFF silently IS the tristate law); any SET value
    takes the full tristate handling byte-identically.
    """
    raw = os.environ.get(MONEY_RUST_ENV)
    if raw is None:
        return False
    stripped = raw.strip().lower()
    if stripped in _TRUTHY:
        return True
    if stripped and stripped not in _FALSY:
        _warn_once("flag_unrecognised", value=stripped)
    return False


def money_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_money_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state so
    the adopted value is the crate's with no oracle in the path (the
    parity legs depend on that — an armed burn-in would cushion a
    divergence by shipping oracle values). Never called from production
    paths (the PR-1 reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    for law in _BURN_IN_REMAINING:
        _BURN_IN_REMAINING[law] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``money_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("money_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``money_rust.autoreverted`` at ERROR (the burn-in
    autorevert — a genuine wrongness signal on a stored-bytes-critical
    law); guard latches log ``money_rust.rust_path_disabled`` at WARN.
    Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("money_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("money_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_core``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A
    wheel that imports but lacks any callable :data:`_CRATE_API` member is
    a pre-money-seam (or foreign/broken) install — latch ``crate_stale``
    (the ``window_ledger_rust`` stale-wheel handshake; no version guard —
    module docstring). Fail-closed: never an ``AttributeError`` out of
    this seam.
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
                "the installed wheel predates the money seam (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-core (see rust/brutex-core/README.md)",
            )
            return None
    return crate


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — the ``lake_rust_stats`` twin.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak; the repo convention (CodeQL
    ``py/catch-base-exception``) forbids a non-re-raising literal ``except
    BaseException``, so the class is resolved GUARDEDLY by name and folded
    beside ``Exception``. ONE home is probed — ``pyo3_runtime`` (the
    ``polars.exceptions`` home of the lake twins is irrelevant to a
    polars-free crate; a documented narrowing). The crate's own
    ``catch_unwind`` maps panics to ``RuntimeError`` (the degrade set), so
    this belt is defense-in-depth over a broken boundary.
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
    """Log ``money_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("money_rust.engaged", crate_version=getattr(crate, "__version__", None))


def _context_hazard() -> bool:
    """``True`` iff the ambient context makes oracle rounding RAISE (gate law)."""
    traps = getcontext().traps
    return bool(traps[Inexact] or traps[Rounded])


def _eligible_rupees(rupees: object) -> bool:
    """The rupee-input eligibility gate (module docstring gate law)."""
    if type(rupees) is not Decimal or not rupees.is_finite():
        return False
    adjusted = rupees.adjusted()
    # exponent = adjusted − len(digits) + 1 (the ``as_tuple`` identity,
    # computed int-only — a FINITE Decimal's ``as_tuple().exponent`` is
    # always an int, but its TYPE carries the non-finite str markers, so
    # deriving it keeps mypy strict clean with no dead defensive arm).
    exponent = adjusted - len(rupees.as_tuple().digits) + 1
    if exponent < _MIN_EXPONENT:
        return False
    if adjusted > _MAX_ADJUSTED or adjusted + _PREC_HEADROOM > getcontext().prec:
        return False
    return not _context_hazard()


def _eligible_paisa(paisa: object) -> bool:
    """The paisa-input eligibility gate (module docstring gate law)."""
    if type(paisa) is not int:
        return False
    if not -_MAX_PAISA < paisa < _MAX_PAISA:
        return False
    if len(str(abs(paisa))) + 2 > getcontext().prec:
        return False
    return not _context_hazard()


def _is_digits(chunk: str) -> bool:
    """``True`` iff *chunk* is non-empty and ASCII-0-9 only (shape checks)."""
    return bool(chunk) and all(ch in _ASCII_DIGITS for ch in chunk)


def _valid_fixed2_str(value: str) -> bool:
    """Shape law for a 2dp fixed-point crate return (``-?\\d+\\.\\d\\d``)."""
    body = value.removeprefix("-")
    if len(body) < _MIN_FIXED2_LEN or body[-3] != ".":
        return False
    return _is_digits(body[:-3]) and _is_digits(body[-2:])


def _valid_int_str(value: str) -> bool:
    """Shape law for an integer paisa crate return (``-?\\d+``, ASCII-strict)."""
    return _is_digits(value.removeprefix("-"))


def _valid_inr_str(value: str) -> bool:
    """Shape law for a ``format_inr`` crate return (``₹-?\\d{1,3}(,\\d{3})*\\.\\d\\d``)."""
    if not value.startswith("₹"):
        return False
    body = value[1:].removeprefix("-")
    if len(body) < _MIN_FIXED2_LEN or body[-3] != ".":
        return False
    if not _is_digits(body[-2:]):
        return False
    groups = body[:-3].split(",")
    if not 1 <= len(groups[0]) <= _GROUP_LEN or not _is_digits(groups[0]):
        return False
    return all(len(group) == _GROUP_LEN and _is_digits(group) for group in groups[1:])


def _adopt_crate_string(
    law: str,
    crate_name: str,
    argument: str,
    validate: Callable[[str], bool],
    oracle_canonical: Callable[[], str],
) -> str | None:
    """Run layers 3-6 for one engaged call; return the crate string to
    ADOPT, or ``None`` (= serve the Python oracle).

    ``oracle_canonical`` renders the oracle result in the law's canonical
    string form (``str()`` of the Decimal/int, the ``format_inr`` string
    itself) — only invoked while the law's burn-in budget is live. A
    burn-in mismatch autoreverts and returns ``None``: the caller
    recomputes the (pure, deterministic) oracle — one redundant compute on
    the one-shot autorevert path, never on the steady state.
    """
    crate = _resolve_crate()
    if crate is None:
        return None
    try:
        rust_value = getattr(crate, crate_name)(argument)
    except _DEGRADE_ERRORS as exc:
        _warn_once(f"{law}_error", error_detail=repr(exc))
        return None
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent
        # fallback. Sits ABOVE the panic belt so its `Exception` member
        # can never swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        # A panic escaping the crate's catch_unwind boundary (or any
        # out-of-degrade-set Exception) marks a broken FFI contract —
        # latch OFF and serve the oracle, never a new crash class into a
        # cost/lake-write path.
        _disable("crate_panic", error_detail=repr(exc))
        return None
    if not isinstance(rust_value, str) or not validate(rust_value):
        # Refuse-poison gate: a non-str return or a shape-law violation is
        # a crate BUG — never served, never even CONSTRUCTED from (the
        # validators are ASCII-strict, so `Decimal(...)`/`int(...)` on an
        # adopted string cannot raise — the shape check IS the
        # construction guard). Structurally unreachable through the real
        # crate; guards a stale/foreign install.
        head = repr(rust_value[:24]) if isinstance(rust_value, str) else None
        _warn_once(
            f"{law}_result_refused",
            result_type=type(rust_value).__name__,
            value_head=head,
        )
        return None
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[law] > 0:
        oracle_value = oracle_canonical()
        if rust_value != oracle_value:
            _disable(
                f"burn_in_mismatch_{law}",
                error=True,
                rust_value=rust_value,
                python_value=oracle_value,
            )
            return None
        _BURN_IN_REMAINING[law] -= 1
        if _BURN_IN_REMAINING[law] == 0:
            _LOG.info("money_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return rust_value


def route_round_money(rupees: Decimal, python_law: Callable[[Decimal], Decimal]) -> Decimal:
    """The routed ``round_money`` — called by :mod:`brutex.money` ONLY when
    the flag is ON. EVERY path returns the correct quantized ``Decimal``.
    """
    if money_rust_disabled() or not _eligible_rupees(rupees):
        return python_law(rupees)
    adopted = _adopt_crate_string(
        _LAW_ROUND,
        "round_money_str",
        format(rupees, "f"),
        _valid_fixed2_str,
        lambda: str(python_law(rupees)),
    )
    if adopted is None:
        return python_law(rupees)
    return Decimal(adopted)


def route_rupees_to_paisa(rupees: Decimal, python_law: Callable[[Decimal], int]) -> int:
    """The routed ``rupees_to_paisa`` — every path returns the exact paisa int."""
    if money_rust_disabled() or not _eligible_rupees(rupees):
        return python_law(rupees)
    adopted = _adopt_crate_string(
        _LAW_TO_PAISA,
        "rupees_to_paisa_str",
        format(rupees, "f"),
        _valid_int_str,
        lambda: str(python_law(rupees)),
    )
    if adopted is None:
        return python_law(rupees)
    return int(adopted)


def route_paisa_to_rupees(paisa: int, python_law: Callable[[int], Decimal]) -> Decimal:
    """The routed ``paisa_to_rupees`` — every path returns the exact 2dp Decimal."""
    if money_rust_disabled() or not _eligible_paisa(paisa):
        return python_law(paisa)
    adopted = _adopt_crate_string(
        _LAW_TO_RUPEES,
        "paisa_to_rupees_str",
        str(paisa),
        _valid_fixed2_str,
        lambda: str(python_law(paisa)),
    )
    if adopted is None:
        return python_law(paisa)
    return Decimal(adopted)


def route_format_inr(paisa: int, python_law: Callable[[int], str]) -> str:
    """The routed ``format_inr`` — every path returns the display string."""
    if money_rust_disabled() or not _eligible_paisa(paisa):
        return python_law(paisa)
    adopted = _adopt_crate_string(
        _LAW_FORMAT,
        "format_inr_str",
        str(paisa),
        _valid_inr_str,
        lambda: python_law(paisa),
    )
    if adopted is None:
        return python_law(paisa)
    return adopted


__all__ = [
    "BURN_IN_SAMPLES",
    "MONEY_RUST_ENV",
    "money_rust_disabled",
    "money_rust_enabled",
    "reset_money_rust_for_tests",
    "route_format_inr",
    "route_paisa_to_rupees",
    "route_round_money",
    "route_rupees_to_paisa",
]
