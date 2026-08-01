"""Default-ON Rust seam for the nightly chain's pure decision kernels.

DEC-NIGHTLY-RUST-CORE-001; default flipped ON by
DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23). The routed
surfaces are the PURE laws of
:mod:`brutex.nightly.chain` (``first_breaker`` · ``exit_code_for`` ·
``audit_reconcile_tripwire``), :mod:`brutex.nightly.report`
(``_verdict`` · ``summary_lines`` · ``_count_text``) and
:mod:`brutex.nightly.schedule` (``render_launchd_plist`` · ``parse_at`` ·
``pmset_wake_command`` · ``seconds_since_scheduled_fire``) — when
``BRUTEX_NIGHTLY_RUST`` is ON each delegates its decision to the
`rust/brutex-ops` crate through this facade; the explicit-falsy kill
switch runs the verbatim retained Python body (the ``*_python`` oracle),
byte-identical AND crate-untouched (bomb-crate spy-pinned). The flag is ON
by default (unset/blank → ON) per DEC-RUST-DEFAULT-ON-WAVE-001.

The :mod:`brutex.data.lake_rust_stats` pattern, with the SAME deliberate
structural difference: **no version guard.** That guard exists where the
crate links a third-party polars whose bytes drift across releases; this
crate is pure std-Rust mirroring stdlib string/int laws — there is no
wheel↔crate lockstep to attest, so a version guard would attest nothing.
The per-kernel burn-in bit-compare + the frozen Rust-side goldens are the
drift teeth instead.

Fail-closed layers, in order (every path returns the PYTHON value):

1. the caller-side flag gate (:func:`nightly_rust_enabled` — default ON
   per DEC-RUST-DEFAULT-ON-WAVE-001; explicit falsy is the kill switch;
   junk values loud-OFF once per process);
2. the import/API-surface guard (:func:`_resolve_crate` — absent wheel or
   a foreign install missing a callable latches ``crate_unavailable``);
3. the per-kernel burn-in self-check — the first :data:`BURN_IN_SAMPLES`
   engaged calls per kernel compute BOTH arms and compare exactly
   (``==`` over str/int/tuple values ≡ a byte compare for strings); ANY
   difference ships the ORACLE value and autoreverts the WHOLE facade for
   the process (ONE shared latch — a wrong kernel means an untrustworthy
   crate; the lake_rust_merge shared-latch posture, disclosed);
4. the degrade set (:data:`_DEGRADE_ERRORS` + the pyo3 panic belt) — a
   crate raise serves the oracle with one WARN, never a new crash class;
   this is ALSO the refuse-to-guess channel: the crate raises
   ``ValueError`` on inputs it cannot mirror faithfully (non-ASCII
   ``--at`` text, out-of-``datetime``-range components, a non-finite
   duration) and the oracle then produces the genuine Python behavior —
   the value OR the original Python exception. Those refusals are STAMPED
   crate-side with :data:`_INPUT_REFUSED_MARKER`, so under strict mode
   (DEC-RUST-STRICT-NO-FALLBACK-001) :func:`_degrade_reason` separates the
   legitimate wide INPUT (keeps degrading) from a crate FAULT (refuses) on
   evidence the crate itself supplied — never on message prose;
5. the refuse-poison gate — a wrong-shaped crate return (non-int exit
   code, non-str plist, a malformed summary tuple) is never consumed.

``MemoryError``/``KeyboardInterrupt``/``SystemExit`` re-raise RAW (the
errors.py escape doctrine — an OOM or an operator interrupt is never
laundered into a fallback).

Pre-formatting law (the lake_rust_stats "isoformat pinned both arms"
satisfaction): anything whose faithful mirror would require re-deriving a
CPython formatter is PRE-FORMATTED Python-side and crosses the FFI as a
string — the audit-reconcile problem strings' ``repr()`` renderings, and
the plist's two pathlib-joined log paths. Both arms consume the identical
strings, so no Rust re-derivation of ``repr``/``pathlib`` exists.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.config.rust_strict import refuse_if_strict
from brutex.data.lake_rust_render import env_flag_tristate

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The pair length shared by the summary tuple + the (hour, minute) verdict.
_PAIR_LEN: Final[int] = 2

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; explicit falsy
#: is the kill switch; junk fails loud-OFF).
NIGHTLY_RUST_ENV: Final[str] = "BRUTEX_NIGHTLY_RUST"

#: Engaged calls PER KERNEL bit-compared against the Python oracle before
#: crate values ship un-oracled (the #1531/#1532 burn-in law; per-kernel
#: budgets — the window_ledger_rust multi-law posture).
BURN_IN_SAMPLES: Final[int] = 16

#: The process-local autorevert latch — ``[None]`` = live; a reason string
#: latches the Rust path OFF for the remainder of the process (SHARED
#: across kernels — module docstring layer 3).
#:
#: THREAD-SAFETY (hostile-review NIT, by design): this latch + the burn-in
#: budgets + the warn-once memo below are process-local module state with NO
#: locking — the nightly chain and its status probe are single-threaded
#: orchestration. A hypothetical race could only mis-account a burn-in
#: decrement or duplicate a warn-once line, never return a wrong value
#: (every path serves either the crate value bit-compared against the
#: oracle, or the oracle itself).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-kernel remaining burn-in budgets (lazily seeded; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {}

#: The budget a kernel's first engaged call seeds (test-adjustable).
_BURN_IN_DEFAULT: Final[list[int]] = [BURN_IN_SAMPLES]

#: Warn-once registry (per-process, test-resettable).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (once per process, never a warn reason).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The crate callables this seam requires (the API-surface guard).
_CRATE_API: Final[tuple[str, ...]] = (
    "nightly_first_breaker_index",
    "nightly_exit_code",
    "nightly_audit_reconcile_detail",
    "nightly_count_text",
    "nightly_verdict",
    "nightly_summary_lines",
    "nightly_xml_escape",
    "nightly_render_launchd_plist",
    "nightly_parse_at",
    "nightly_pmset_wake_command",
    "nightly_seconds_since_fire",
)

#: The narrow degrade-to-oracle catch set. ``ValueError`` carries the
#: crate's refuse-to-guess refusals (module docstring layer 4);
#: ``TypeError``/``OverflowError`` are PyO3 argument-conversion failures
#: (junk field types / out-of-i64 ints); ``RuntimeError`` is a mapped
#: panic; ``AttributeError`` is the broken-crate-internals belt.
#: ``MemoryError`` is DELIBERATELY absent (module docstring).
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)


def nightly_rust_enabled() -> bool:
    """Resolve ``BRUTEX_NIGHTLY_RUST`` (default ON, junk loud-OFF).

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): truthy →
    ``True``; falsy → ``False`` (the silent explicit kill switch); unset /
    whitespace-empty → ``True`` (the default); any OTHER non-empty value →
    ``False`` with one ``nightly_rust.fallback`` WARN (reason
    ``flag_unrecognised``) per process — the junk arm is unchanged. Read
    per call so spawn workers resolve their own inherited environment
    (the lake_rust_stats posture).
    """
    raw = os.environ.get(NIGHTLY_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def nightly_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_nightly_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state so
    the returned value is the crate's with no oracle in the path (a parity
    test depends on that — an armed burn-in would cushion a divergence by
    shipping oracle values). Never called from production paths (the
    lake_rust_stats reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING.clear()
    _BURN_IN_DEFAULT[0] = remaining
    _OBSERVABILITY_MEMO.clear()


#: The crate's input-envelope refusal MARKER — the stable fragment
#: ``rust/brutex-ops/src/nightly.rs::INPUT_REFUSED_MARKER`` stamps on every
#: refusal that is an out-of-envelope INPUT rather than a crate FAULT
#: (DEC-RUST-STRICT-NO-FALLBACK-001; the ``brutex/strategies/combo_rust.py``
#: doctrine, same literal, one lane over). Its PRESENCE is what separates
#: CLASS C (keep degrading, even under strict mode) from CLASS A/B (refuse)
#: inside the shared :data:`_DEGRADE_ERRORS` classes — the classification
#: reads evidence the crate itself minted at its own raise site, never a
#: natural-language sniff of an arbitrary message. A crate FAULT (a stale
#: wheel's arity mismatch, a mapped panic, broken internals) is deliberately
#: left UNMARKED. Cross-language drift is pinned crate-side by
#: ``input_refused_marker_literal_is_the_facade_contract`` and Python-side by
#: the unit suite.
_INPUT_REFUSED_MARKER: Final[str] = "input outside the crate envelope"

#: DEC-RUST-STRICT-NO-FALLBACK-001 — the ruled CLASS-C exemption: reasons
#: strict mode must NOT refuse because the CRATE ITSELF declined a legitimate
#: INPUT that sits outside its faithful-mirror envelope and handed the call
#: back so the Python oracle — always the intended handler for these — could
#: decide. Refusing one would crash a legitimate call.
#:
#: Every member is minted ONLY when the crate's message carries
#: :data:`_INPUT_REFUSED_MARKER` (see :func:`_degrade_reason`), and each
#: names ONE law's out-of-envelope population (crate evidence in
#: ``rust/brutex-ops/src/nightly.rs``, whose four marked cores each carry an
#: ENVELOPE-ERR INVARIANT note stating that EVERY ``Err`` they return is an
#: input refusal — a future fault arm must not reuse it):
#:
#: * ``summary_lines_input_refused`` — a non-finite or
#:   ``>= MAX_EXACT_DURATION`` (2**53) elapsed-seconds duration, outside the
#:   f64 exactly-representable-integer domain the mirror needs.
#: * ``parse_at_input_refused`` — non-ASCII ``--at`` text (CPython's
#:   un-flagged ``\\d`` matches Unicode decimal digits and the hand-rolled
#:   ASCII matcher refuses to re-derive that), or a ``str`` the FFI cannot
#:   extract (a surrogateescape'd non-UTF-8 argv byte).
#: * ``pmset_wake_command_input_refused`` — ``checked_mul``/``add``/``sub``
#:   overflow, or an out-of-``i64`` ``int`` argument.
#: * ``seconds_since_scheduled_fire_input_refused`` — a datetime component
#:   outside its documented range, or an out-of-``i64`` ``int`` argument.
#:
#: Enumerated EXPLICITLY (not a ``_input_refused`` suffix rule) so the
#: exemption is fail-closed: a marked message surfacing under any OTHER
#: kernel name still refuses.
_STRICT_EXEMPT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "summary_lines_input_refused",
        "parse_at_input_refused",
        "pmset_wake_command_input_refused",
        "seconds_since_scheduled_fire_input_refused",
    }
)

#: EMPTY — nothing is held out of the taxonomy any more.
#:
#: ``{summary_lines,parse_at,pmset_wake_command,seconds_since_scheduled_fire}
#: _error`` used to live here because each bundled a genuine "Rust broke"
#: fault together with the crate's refuse-to-guess arm, indistinguishable at
#: the choke point (both arrive as the same ``ValueError`` from pyo3's
#: ``Err(String)`` mapping). That is resolved: the crate now MARKS every
#: input-envelope refusal it raises (:data:`_INPUT_REFUSED_MARKER`), those
#: cases route to the ``*_input_refused`` members of
#: :data:`_STRICT_EXEMPT_REASONS` above, and the four original tokens retain
#: exactly the UNMARKED remainder — a stale wheel's arity/type mismatch, a
#: mapped panic, broken internals — which is CLASS A/B and DOES refuse.
#:
#: The other four kernels never needed a split: ``first_breaker`` (impl
#: returns ``Option``, no ``Err``) and ``render_launchd_plist`` (impl returns
#: ``String``, no ``Err``) can only fault; ``exit_code_for`` and
#: ``audit_reconcile_tripwire`` ``Err`` only on a length mismatch their own
#: doc-comments call *"a facade bug — refuse loudly"*, so their FFI faces
#: deliberately keep the UNMARKED mapping.
#:
#: The set is kept (empty) rather than deleted so a future "held pending a
#: ruling" reason has a named, reviewable home instead of being quietly
#: folded into the ruled exemption.
_STRICT_UNRESOLVED_REASONS: Final[frozenset[str]] = frozenset()

#: The reasons strict mode does NOT refuse — the ruled CLASS-C exemption
#: plus the (currently empty) unresolved-pending-ruling set, unioned at ONE
#: point so the two provenances stay separately reviewable above.
_STRICT_NOT_REFUSED: Final[frozenset[str]] = _STRICT_EXEMPT_REASONS | _STRICT_UNRESOLVED_REASONS


def _degrade_reason(kernel: str, exc: BaseException) -> str:
    """Classify a crate-call degrade as CLASS C (input) or CLASS A/B (fault).

    Reads the marker the CRATE minted at its own raise site
    (:data:`_INPUT_REFUSED_MARKER`) — never a guess from the exception
    class, which is shared across both populations inside
    :data:`_DEGRADE_ERRORS`:

    * marked → ``{kernel}_input_refused``, the ruled CLASS-C exemption
      (:data:`_STRICT_EXEMPT_REASONS`) — keeps degrading to the oracle even
      under strict mode, because the oracle is the intended handler;
    * UNMARKED → ``{kernel}_error``, the genuine-fault remainder, which
      strict mode REFUSES.

    Only the four cores whose ``Err`` is provably input-only are marked
    crate-side, so an unmarked message from ANY kernel is a fault.
    """
    if _INPUT_REFUSED_MARKER in str(exc):
        return f"{kernel}_input_refused"
    return f"{kernel}_error"


def _warn_once(reason: str, **context: object) -> None:
    """Refuse under strict mode; else log ``nightly_rust.fallback`` warn-once.

    DEC-RUST-STRICT-NO-FALLBACK-001. CLASS A (``flag_unrecognised``, every
    ``{kernel}_error`` — now the UNMARKED fault remainder) and CLASS B
    (every ``{kernel}_result_refused`` — the crate ran and its shape gate
    rejected the output) refuse. Only :data:`_STRICT_NOT_REFUSED` — the
    ruled CLASS-C ``*_input_refused`` exemption, unioned with the now-EMPTY
    unresolved set — returns instead and keeps degrading to the oracle.
    """
    if reason not in _STRICT_NOT_REFUSED:
        refuse_if_strict("brutex.nightly.nightly_rust", reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("nightly_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Refuse under strict mode; else latch the Rust path OFF for the process.

    ``error=True`` logs ``nightly_rust.kernel_autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``nightly_rust.rust_path_disabled`` at WARN. Repeats are no-ops.

    DEC-RUST-STRICT-NO-FALLBACK-001: reasons here are CLASS A
    (``crate_unavailable``, ``crate_panic``) or CLASS B (``burn_in_mismatch``
    — the crate DISAGREED with the oracle), so the refusal is UNCONDITIONAL.
    It fires BEFORE the latch write, so under strict mode the latch can never
    be set and :func:`nightly_rust_disabled`'s early-return in
    :func:`route_kernel` is structurally unreachable — wiring these two
    functions therefore covers every announced fall-back this facade takes.
    """
    refuse_if_strict("brutex.nightly.nightly_rust", reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("nightly_rust.kernel_autoreverted", reason=reason, **context)
    else:
        _LOG.warning("nightly_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard: return ``brutex_ops``, or latch OFF + ``None``.

    An absent wheel latches ``crate_unavailable`` with one loud WARN naming
    the remedy; a module that imports but lacks a callable
    :data:`_CRATE_API` member (a pre-nightly-lane wheel, a name collision,
    a hand-broken install) latches the SAME reason — an ``AttributeError``
    must never leave this seam (the lake_rust_stats fail-closed contract).
    """
    try:
        crate = importlib.import_module("brutex_ops")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-ops (maturin build; see rust/brutex-ops/README.md)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_unavailable",
                error_detail=f"brutex_ops.{name} missing or not callable — stale/foreign install",
                remedy="pip install rust/brutex-ops (maturin build; see rust/brutex-ops/README.md)",
            )
            return None
    return crate


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — the ``lake_rust_stats`` twin.

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak; the repo convention (CodeQL
    ``py/catch-base-exception``) forbids a non-re-raising literal ``except
    BaseException``, so the class is resolved GUARDEDLY by name from its
    two plausible homes and folded beside ``Exception`` (a LOCAL twin
    because the sibling helpers are private to their modules; the homes +
    semantics are equality-pinned in the unit suite).
    """
    belt: list[type[BaseException]] = [Exception]
    for home in ("pyo3_runtime", "polars.exceptions"):
        try:
            module = importlib.import_module(home)
        except ImportError:
            continue
        panic = getattr(module, "PanicException", None)
        if isinstance(panic, type) and issubclass(panic, BaseException) and panic not in belt:
            belt.append(panic)
    return tuple(belt)


def route_kernel[T](
    kernel: str,
    *,
    crate_call: Callable[[ModuleType], object],
    python_call: Callable[[], T],
    validate: Callable[[object], bool],
) -> T:
    """The routed kernel dispatch — called by a seam ONLY when the flag is ON.

    Runs the fail-closed layers (module docstring) and returns the kernel's
    value. EVERY path returns a valid Python-law value: a latched / guarded
    / degraded / refused call returns ``python_call()`` (the live oracle —
    whose OWN exceptions propagate untouched, so a junk-input law like
    ``parse_at``'s ValueError keeps its genuine Python surface), a burn-in
    mismatch returns the ORACLE value, and only a guard-clean,
    validate-clean, burn-in-clean call returns the Rust value.

    Parameters
    ----------
    kernel
        The kernel name (burn-in budget key + observability token).
    crate_call
        One-arg callable receiving the resolved ``brutex_ops`` module and
        returning the crate's value (builds its flat FFI arguments lazily —
        a latched process never pays that construction).
    python_call
        Zero-arg callable running the verbatim retained Python body — the
        live parity oracle.
    validate
        Refuse-poison shape gate over the RAW crate return; ``False``
        serves the oracle with one ``<kernel>_result_refused`` WARN.
    """
    if nightly_rust_disabled():
        return python_call()
    crate = _resolve_crate()
    if crate is None:
        return python_call()
    try:
        value = crate_call(crate)
    except _DEGRADE_ERRORS as exc:
        # ONE classification point: the crate MARKS its out-of-envelope input
        # refusals, so a legitimate wide input keeps degrading to the oracle
        # (CLASS C) while an UNMARKED fault refuses under strict mode
        # (CLASS A/B) — DEC-RUST-STRICT-NO-FALLBACK-001, :func:`_degrade_reason`.
        _warn_once(_degrade_reason(kernel, exc), error_detail=repr(exc))
        return python_call()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never laundered into a
        # fallback. Sits ABOVE the panic belt so its `Exception` member can
        # never swallow an OOM.
        raise
    except _panic_belt_errors() as exc:
        # A panic escaping pyo3's own mapping (or any out-of-degrade-set
        # Exception) marks a broken FFI contract — latch OFF and serve the
        # oracle, never a new crash class into the nightly chain.
        _disable("crate_panic", error_detail=repr(exc))
        return python_call()
    if not validate(value):
        # Refuse-poison gate: a wrong-shaped return is a crate BUG — never
        # consumed. Structurally unreachable through the real crate (its
        # FFI constructs the typed values); guards a stale/foreign install.
        _warn_once(f"{kernel}_result_refused", result_type=type(value).__name__)
        return python_call()
    if _ENGAGED_KEY not in _OBSERVABILITY_MEMO:
        _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
        _LOG.info("nightly_rust.engaged")
    remaining = _BURN_IN_REMAINING.setdefault(kernel, _BURN_IN_DEFAULT[0])
    if remaining > 0:
        oracle_value = python_call()
        if value != oracle_value:
            _disable(
                "burn_in_mismatch",
                error=True,
                kernel=kernel,
                rust_value_type=type(value).__name__,
            )
            return oracle_value
        _BURN_IN_REMAINING[kernel] = remaining - 1
        if remaining == 1:
            _LOG.info("nightly_rust.burn_in_complete", kernel=kernel, samples=BURN_IN_SAMPLES)
    # ``validate`` (+ the burn-in bit-compare while it runs) is the runtime
    # proof the crate value has the oracle's type — the cast records that.
    return cast("T", value)


def is_int(value: object) -> bool:
    """Refuse-poison shape: a genuine ``int`` (``bool`` is a crate bug)."""
    return type(value) is int


def is_str(value: object) -> bool:
    """Refuse-poison shape: a genuine ``str``."""
    return type(value) is str


def is_float(value: object) -> bool:
    """Refuse-poison shape: a genuine ``float``."""
    return type(value) is float


def is_summary_tuple(value: object) -> bool:
    """Refuse-poison shape: ``tuple[list[str], str]`` exactly."""
    if not (isinstance(value, tuple) and len(value) == _PAIR_LEN):
        return False
    lines, verdict = value
    if not (isinstance(lines, list) and type(verdict) is str):
        return False
    return all(type(line) is str for line in lines)


def is_hm_tuple_or_none(value: object) -> bool:
    """Refuse-poison shape: ``tuple[int, int] | None`` (parse_at verdict)."""
    if value is None:
        return True
    return (
        isinstance(value, tuple)
        and len(value) == _PAIR_LEN
        and all(type(part) is int for part in value)
    )


__all__ = [
    "BURN_IN_SAMPLES",
    "NIGHTLY_RUST_ENV",
    "is_float",
    "is_hm_tuple_or_none",
    "is_int",
    "is_str",
    "is_summary_tuple",
    "nightly_rust_disabled",
    "nightly_rust_enabled",
    "reset_nightly_rust_for_tests",
    "route_kernel",
]
