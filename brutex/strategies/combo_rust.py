"""combo_rust — the default-ON Rust routing facade for combo.py's two PURE byte laws.

DEC-STRATEGIES-RUST-COMBO-001 (T16 PR-1; default flipped ON by
DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23). Routes the
TWO pure combo-identity laws through the ``rust/brutex-strategies`` crate
(``brutex_strategies.canonical_json`` + ``brutex_strategies.first_unit_separator``)
behind the ``BRUTEX_STRATEGIES_RUST_COMBO`` flag, with the verbatim
Python law retained as the LIVE parity oracle (the ADR-0020 / PR-1
``lake_rust_render`` template; the per-law burn-in + injected-``python_law``
shape of ``brutex/data/window_ledger_rust.py``):

1. **The canonical-JSON render** — the single ``json.dumps(lowered,
   sort_keys=True, separators=(",", ":"), ensure_ascii=True)`` site inside
   ``canonicalise_params``. Canonicalisation itself
   (``combo._canonicalise_value``) stays PYTHON and runs BEFORE the seam, so
   the crossing tree is JSON-safe plain data — dict / list / str / int /
   bool / None, float-free and Decimal-free BY LAW (floats and Decimals exist
   only as already-rendered strings). The Rust kernel therefore NEVER
   reproduces ``float.__repr__`` / ``Decimal.__str__`` — the load-bearing
   byte-safety property of the whole conversion (the #1914 "Python
   pre-formats; ints+strings-only FFI" precedent applied to a tree).
2. **The ``\\x1f`` first-offender scan** — ``reject_unit_separator``'s
   value-scan law. The FFI carries NO message and NO exception class:
   ``first_unit_separator(values) -> int | None`` returns the offending
   VALUE index; this facade maps index → field NAME, and combo.py raises the
   injected error class with the verbatim f-string — Python owns the
   error-message bytes and the exception taxonomy by construction.

## Container symmetry (plain-dict law — doc-symmetry note)

The crate's tree walk uses a NON-exact container downcast for dicts (as it
does for lists, where "sequence lowering already happened"). That is safe for
the SAME structural reason: every dict that can legally cross this seam is a
fresh exact PLAIN ``dict`` built by ``_canonicalise_value``'s comprehension
(inner Mappings included), so a hostile ``dict`` SUBCLASS overriding
``items()`` — the one shape where CPython's encoder (``PyMapping_Items`` for
subclasses) and a storage-order iteration COULD diverge — is unreachable from
the sole production caller; it can arrive only via a hostile DIRECT call to
this internal, non-package-API seam, and even then the first-16 burn-in
byte-compare autoreverts any divergence. Dict KEYS, by contrast, get an
EXACT-type gate in the crate (``cast_exact`` — a ``str`` subclass key could
legally sort differently under CPython's ``sort_keys``), refused to the
oracle with the ``"non-exact str"`` marker → ``nonexact_key_refused``.

## The fail-closed layers (the PR-1 template, adapted)

* **Flag** — ``BRUTEX_STRATEGIES_RUST_COMBO`` default ON (per
  DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23); truthy →
  ON, falsy → OFF (the explicit kill switch), any other non-empty value →
  OFF with ONE loud ``combo_rust.fallback`` WARN (``flag_unrecognised``).
  OFF returns the oracle's result directly — the crate is never imported,
  so the explicit-OFF path is byte-identical to pre-facade behaviour
  (bomb-crate spy-pinned).
* **Import guard / SYMBOL handshake** — the laws are polars-free, so the
  PR-1 polars VERSION guard is meaningless here (a DELIBERATE, disclosed
  deviation — the #1914/#1915 precedent): an absent wheel latches
  ``crate_unavailable``; a wheel that imports but lacks either law callable
  is a foreign/broken (or pre-T16) install and latches ``crate_stale``.
  Both serve the oracle with one loud WARN. The wheel's OWN import-time
  ``_assert_compiled_surface`` handshake fires first for a symbol-stale
  install; its ImportError folds into ``crate_unavailable``.
* **Degrade set** — ``ValueError``/``RuntimeError`` (the crate's declared
  FFI error surface; a caught render panic maps to ``RuntimeError``),
  ``TypeError``/``OverflowError`` (PyO3 argument conversion — an
  out-of-``i128`` int and the exact-str key gate land here; a lone-surrogate
  ``str`` fails ``String`` extraction as ``UnicodeEncodeError ⊂
  ``ValueError``), ``AttributeError`` (broken-crate-internals belt).
  ``MemoryError`` is DELIBERATELY absent — OOM re-raises raw, identical to
  the pure-Python path. The narrow panic belt (:func:`_panic_belt_errors` —
  a LOCAL twin, §"local twins" below) latches ``crate_panic``.
* **Refuse-poison gates** — a non-``str`` canonical render / a
  non-``int``/negative/out-of-range scan index is REFUSED to the oracle
  (warn-once); a str-SUBCLASS dict key is refused BY THE CRATE (the
  ``"non-exact str"``-marked ``TypeError``) and surfaces as the distinct
  ``nonexact_key_refused`` warn-once reason.
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls PER LAW compare the Rust value against the live oracle; ANY
  difference ships the ORACLE value and autoreverts the Rust path for the
  process (``combo_rust.autoreverted`` ERROR — the #1531/#1532 pattern).

## Local twins (NOT engine / data imports — verified cycle + import-minimalism)

``env_flag_tristate`` is a LOCAL twin of
``brutex.engine.sweep_lookahead.env_flag_tristate`` because importing it
would close a VERIFIED cycle: ``brutex.engine.__init__`` eagerly imports
``results_store``/``stage1a``/…, which import ``brutex.strategies.combo`` at
module scope — and combo.py imports THIS module at module scope.
``_panic_belt_errors`` is a LOCAL twin of
``brutex.data.lake_rust_render._panic_belt_errors`` on import-minimalism:
combo.py sits on a very-early hot import path and must not grow a
strategies→data package edge for two tiny helpers (the ``lake_rust_stats``
local-twin precedent). BOTH twins are equality-PINNED by tests, which import
the originals freely. Import surface of this module: stdlib + structlog ONLY.

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. The engaged
path ADDS an FFI hop (plus a full tree extraction) per canonical render and
per scan — a small SLOWDOWN, not a win; both laws run per combo
MATERIALISATION (launch / persist / verify / re-derivation), never inside
the ~33B-trial mask/bar hot loop. The flag is read per call (one
``os.environ`` probe) so spawn workers resolve their own environment.
Disclosed ON-arm behavioral note: the routed scan MATERIALIZES the fields
iterable once (the Python law stops lazily at the first offender) — every
in-repo caller passes tuples, so this is behavior-identical in production;
flag-OFF and every crate-absent/latched path keep today's exact laziness.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any, Final, Protocol

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The env flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001; falsy is the
#: explicit kill switch; junk fails loud-OFF).
STRATEGIES_RUST_COMBO_ENV: Final[str] = "BRUTEX_STRATEGIES_RUST_COMBO"

#: Engaged calls PER LAW that are bit-compared against the live Python
#: oracle before Rust values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: The scan-law burn-in budget key.
_LAW_SCAN: Final[str] = "scan_sep"
#: The canonical-render-law burn-in budget key.
_LAW_CANON: Final[str] = "canonical_json"

#: Truthy env literals (case-insensitive, whitespace-stripped). A DELIBERATE
#: byte-for-byte mirror of the sweep flags' vocabulary
#: (``brutex.engine.sweep_lookahead._TRUTHY``) — a LOCAL twin, not an import,
#: because the engine package's eager init imports ``strategies.combo``
#: (module docstring — the verified cycle); equality-pinned in the unit suite.
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: Falsy env literals — the explicit kill-switch vocabulary, the mirror of
#: :data:`_TRUTHY` (same local-twin rationale).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Process-local OFF latch — the disable reason, or ``None`` when live.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_SCAN: BURN_IN_SAMPLES,
    _LAW_CANON: BURN_IN_SAMPLES,
}

#: The crate callables BOTH laws need — the SYMBOL handshake surface. A
#: wheel missing either is a pre-T16 (or foreign/broken) install
#: (``crate_stale``).
_CRATE_API: Final[tuple[str, ...]] = ("canonical_json", "first_unit_separator")

#: Once-per-process-per-reason log memo (the canonical seam runs per combo
#: materialisation — thousands per sweep launch; a sticky reason must not
#: flood the logs).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (not a fallback reason — namespaced apart).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The narrow degrade-to-oracle catch set (module docstring). MemoryError
#: deliberately absent — OOM re-raises raw, identical to the Python path.
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: The crate's str-SUBCLASS-key refusal marker (the STABLE fragment inside
#: the ``TypeError`` message ``canonical_json: dict key is a str subclass
#: (non-exact str) — refused to the oracle``) — distinguishes the
#: ``nonexact_key_refused`` reason from a generic ``canonical_json_error``
#: degrade inside the shared ``TypeError`` class.
_NONEXACT_KEY_MARKER: Final[str] = "non-exact str"


class CanonicalJsonLaw(Protocol):
    """The Python canonical-JSON oracle's call shape (the ``json.dumps`` law)."""

    def __call__(self, lowered: Mapping[str, Any]) -> str:
        """Render the LOWERED params tree to its canonical JSON string."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class FirstOffendingFieldLaw(Protocol):
    """The Python scan oracle's call shape (the lazy first-offender loop)."""

    def __call__(self, fields: Iterable[tuple[str, str]]) -> str | None:
        """Return the first ``\\x1f``-carrying field's NAME, else ``None``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the flag vocabulary (local twin — module docs).

    ``True`` for a :data:`_TRUTHY` literal, ``False`` for a :data:`_FALSY`
    literal, ``None`` for anything else (unset/empty AND unrecognised — the
    caller owns the default + warn policy). Byte-identical semantics to
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate`, equality-pinned
    in the unit suite; NOT imported from there because of the verified
    engine→strategies import cycle (module docstring).
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def combo_rust_enabled() -> bool:
    """Resolve ``BRUTEX_STRATEGIES_RUST_COMBO`` (default ON, junk loud-OFF).

    Truthy literal → ``True``; falsy literal → ``False`` (the explicit kill
    switch); unset/blank → ``True`` (the default — ON per
    DEC-RUST-DEFAULT-ON-WAVE-001, operator directive 2026-07-23); any OTHER
    non-empty value → ``False`` with one ``combo_rust.fallback`` WARN
    (``flag_unrecognised``) per process. Read per call so spawn workers
    resolve their own environment (the sibling facades' law, byte-identical
    vocabulary via the local :func:`env_flag_tristate` twin).
    """
    raw = os.environ.get(STRATEGIES_RUST_COMBO_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def rust_combo_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_combo_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    Rust value ships with no oracle in the path — the differential suites
    depend on that). Never called from production code (the sibling
    facades' reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[_LAW_SCAN] = remaining
    _BURN_IN_REMAINING[_LAW_CANON] = remaining
    _OBSERVABILITY_MEMO.clear()


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set — ``(Exception, *panic classes)``.

    A LOCAL twin of ``brutex.data.lake_rust_render._panic_belt_errors``
    (import-minimalism — module docstring), equality-pinned in the unit
    suite. ``pyo3_runtime.PanicException`` is the one ``BaseException``
    subclass a pyo3 boundary can leak (a Rust panic escaping
    ``catch_unwind``); it must latch ``crate_panic`` and degrade to the
    oracle, but the repo convention (CodeQL ``py/catch-base-exception``)
    forbids a non-re-raising literal ``except BaseException``, so the class
    is resolved GUARDEDLY by name from its two plausible homes and folded
    beside ``Exception``. Resolved lazily per raise — an ``except`` clause
    expression only evaluates while an exception is in flight.
    ``KeyboardInterrupt``/``SystemExit``/``MemoryError`` re-raise in the
    clause ABOVE the belt, so ``Exception`` here never swallows an OOM.
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


def _warn_once(reason: str, **context: object) -> None:
    """Log ``combo_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("combo_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``combo_rust.autoreverted`` at ERROR (the burn-in
    autorevert — a genuine wrongness signal); guard latches log
    ``combo_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("combo_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("combo_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_strategies``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A wheel
    that imports but lacks either :data:`_CRATE_API` callable is a pre-T16
    (or foreign/broken) install — latch ``crate_stale`` (the stale-wheel
    handshake; the polars version guard is meaningless for a polars-free
    law, module docstring). Fail-closed: never a new crash class.
    """
    try:
        crate = importlib.import_module("brutex_strategies")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-strategies",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_strategies.{name} missing or not callable — "
                "the installed wheel predates the combo laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-strategies",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``combo_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "combo_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def routed_canonical_json(
    lowered: Mapping[str, Any],
    *,
    python_law: CanonicalJsonLaw,
) -> str:
    """The routed canonical-JSON law — flag-gated Rust with the live Python oracle.

    EVERY path returns the canonical string: flag-OFF / latched / guarded /
    degraded calls return ``python_law(lowered)`` (the verbatim
    ``json.dumps`` expression), a burn-in mismatch returns the ORACLE value
    + autoreverts, and only a guard-clean, burn-in-clean call returns the
    Rust render. *lowered* is the ALREADY-canonicalised plain-data tree
    (``_canonicalise_value`` ran BEFORE this seam — module docstring), so
    every rejection ``canonicalise_params`` documents has already raised.
    """
    if not combo_rust_enabled() or rust_combo_disabled():
        return python_law(lowered)
    crate = _resolve_crate()
    if crate is None:
        return python_law(lowered)
    try:
        rust_value = crate.canonical_json(lowered)
    except _DEGRADE_ERRORS as exc:
        if isinstance(exc, TypeError) and _NONEXACT_KEY_MARKER in str(exc):
            # The crate's exact-str KEY gate (module docstring): a str
            # SUBCLASS key could sort differently under CPython's
            # sort_keys, so the crate REFUSES it with the stable marker —
            # surfaced under its own reason, distinct from a generic
            # degrade inside the shared TypeError class.
            _warn_once("nonexact_key_refused", error_detail=repr(exc))
        else:
            _warn_once("canonical_json_error", error_detail=repr(exc))
        return python_law(lowered)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(lowered)
    if not isinstance(rust_value, str):
        # Refuse-poison gate: a non-str return is a crate BUG — never
        # served. Structurally unreachable through the real crate (its FFI
        # returns PyResult<String>); guards a stale/foreign install.
        _warn_once("canonical_result_refused", result_type=type(rust_value).__name__)
        return python_law(lowered)
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_CANON] > 0:
        oracle_value = python_law(lowered)
        if rust_value != oracle_value:
            _disable(
                "burn_in_mismatch_canonical",
                error=True,
                rust_value=rust_value,
                python_value=oracle_value,
            )
            return oracle_value
        _BURN_IN_REMAINING[_LAW_CANON] -= 1
        if _BURN_IN_REMAINING[_LAW_CANON] == 0:
            _LOG.info(
                "combo_rust.burn_in_complete",
                law=_LAW_CANON,
                samples=BURN_IN_SAMPLES,
            )
    return rust_value


def routed_first_offending_field(
    fields: Iterable[tuple[str, str]],
    *,
    python_law: FirstOffendingFieldLaw,
) -> str | None:
    """The routed ``\\x1f`` scan law — flag-gated Rust index, Python name + raise.

    Returns the first offending FIELD NAME (or ``None``); the raise (message
    bytes + the injected error class) stays in combo.py. Flag-OFF / latched
    / crate-absent paths call ``python_law(fields)`` on the UNTOUCHED
    iterable — the verbatim lazy single-pass loop (a generator is consumed
    only to the first offender, exactly as today). The ON arm MATERIALIZES
    the iterable once (disclosed behavioral note, module docstring); its
    degrade/burn-in paths re-run the oracle over the materialized pair list
    (a legal ``Iterable[tuple[str, str]]`` — never a re-consumed generator),
    so unpack/type errors keep today's classes AND message bytes (the oracle
    re-raises its own).
    """
    if not combo_rust_enabled() or rust_combo_disabled():
        return python_law(fields)
    crate = _resolve_crate()
    if crate is None:
        return python_law(fields)
    # Materialize ONCE (the ON-arm consumption law). An iteration error here
    # propagates raw — the same class the OFF-arm loop would raise.
    pairs = list(fields)
    try:
        values = [field_value for _field_name, field_value in pairs]
        idx = crate.first_unit_separator(values)
    except _DEGRADE_ERRORS as exc:
        # Covers BOTH the local unpack/type failures (a malformed pair's
        # ValueError/TypeError) and the crate's extraction failures (a
        # non-str / lone-surrogate value) — the oracle re-runs over the
        # materialized pairs and raises ITS OWN error with today's bytes.
        _warn_once("scan_error", error_detail=repr(exc))
        return python_law(pairs)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(pairs)
    if idx is not None and (type(idx) is not int or idx < 0 or idx >= len(pairs)):
        # Refuse-poison gate: a non-int / negative / out-of-range index is
        # a crate BUG — never served (an exact-type check so a bool can
        # never masquerade as an index).
        _warn_once("scan_result_refused", result_repr=repr(idx))
        return python_law(pairs)
    rust_name = None if idx is None else pairs[idx][0]
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_SCAN] > 0:
        oracle_name = python_law(pairs)
        if rust_name != oracle_name:
            _disable(
                "burn_in_mismatch_scan",
                error=True,
                rust_name=rust_name,
                python_name=oracle_name,
            )
            return oracle_name
        _BURN_IN_REMAINING[_LAW_SCAN] -= 1
        if _BURN_IN_REMAINING[_LAW_SCAN] == 0:
            _LOG.info(
                "combo_rust.burn_in_complete",
                law=_LAW_SCAN,
                samples=BURN_IN_SAMPLES,
            )
    return rust_name


__all__ = [
    "BURN_IN_SAMPLES",
    "STRATEGIES_RUST_COMBO_ENV",
    "CanonicalJsonLaw",
    "FirstOffendingFieldLaw",
    "combo_rust_enabled",
    "env_flag_tristate",
    "reset_combo_rust_for_tests",
    "routed_canonical_json",
    "routed_first_offending_field",
    "rust_combo_disabled",
]
