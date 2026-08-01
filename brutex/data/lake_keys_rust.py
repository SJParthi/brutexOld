"""Default-ON Rust routing facade for the three pure ``lake_keys`` laws.

DEC-DATA-RUST-LAKE-KEYS-001 (T10 PR-2) — the sibling facade of
:mod:`brutex.data.fno_symbol_rust` (PR-1), routing the three pure builder
laws of :mod:`brutex.data.lake_keys` — ``stock_drift_fold`` (law A),
``spot_key_candidates`` (law B) and the ``spot_recovery_candidates`` CORE
(law C) — through the ``brutex_data`` crate behind the SAME
``BRUTEX_DATA_RUST`` flag (ONE flag for the data facades, §11 COMMON:
:func:`data_rust_enabled` is IMPORTED from ``fno_symbol_rust``, never
copied), with the verbatim Python bodies retained in ``lake_keys.py`` as
the LIVE parity oracles every degrade path ships.

Injection-only LEAF (the no-cycle law, DEC-DATA-RUST-PURELAW-001): this
module imports NOTHING from :mod:`brutex.data.lake_keys` — not even under
``TYPE_CHECKING`` (the exact shape that regressed in PR-1's CodeQL fix
round). The oracles arrive as injected callables typed by the LOCAL
structural Protocols below; the AST pin in the unit suite refuses any
reintroduced import. The import graph is one-directional:
``lake_keys → lake_keys_rust → {fno_symbol_rust, lake_rust_render}``.

FFI shape (the ONE conscious deviation from PR-1's integers-only law,
taken deliberately): candidate outputs are COMPUTED ASCII strings — the
``window_ledger_rust`` "computed-ASCII-out" allowance — because alias-pair
members and drift-bucket entries are PASS-THROUGHS of the pairs/bucket
arguments that a span into ``symbol`` cannot address, and a mixed
span+passthrough protocol would triple the FFI complexity for zero drift
protection beyond what burn-in already gives (whole-tuple exact compare —
any out-string drift autoreverts within the 16-sample window).

Guards (mirrors ``fno_symbol_rust`` section-for-section, MINUS the locale
guard and MINUS a wheel version guard — these laws are strptime-free and
polars-free; the :data:`_CRATE_API` symbol handshake is the only lockstep
this facade needs, the ``window_ledger_rust``/``lake_rust_stats``
precedent): flag OFF / latched → oracle; a non-ASCII ingress string (any
of the inputs, alias-pair members or bucket entries) DEGRADES to the
oracle log-free (a scope gate, never a raise — the oracle legally accepts
Unicode, the Rust contract does not); an absent wheel latches
``crate_unavailable``; a wheel missing ANY of the three law symbols
latches ``crate_stale`` (all-or-nothing — a PR-1-era wheel that only
carries ``parse_fno_symbol_coords`` must never half-engage); the degrade
set + the pyo3 panic belt + the raw ``MemoryError``/interrupt escape are
the PR-1 arms verbatim; every structurally-poisoned result is REFUSED
(warn-once ``result_refused_<law>``) and the oracle value ships.

Refuse-poison gates are STRUCTURAL only (the PR-1 MINOR-1 scoping):
semantic wrongness inside a structurally-legal result is the job of
burn-in + the frozen goldens + the lane differential. The RAW-identity
law (R1 CORR MINOR-1, binding): both identity-echo compares pin the RAW
parameter exactly as the oracle does — never ``.strip()``/``.upper()``
first — because a normalizing gate would refuse LEGAL oracle-emittable
entries (a space-wrapped ``underlying``'s recovery ladder legally emits
its stripped form). Empty entries in recovery are NOT refused: the
oracle's ``_add("")`` is reachable through an empty bucket entry, so the
gate must not refuse a shape the law can legally emit.

Burn-in accounting across three laws sharing one flag: the three counters
are INDEPENDENT — each law individually survives 16 engaged compares
before ITS results ship uncompared — while the latch is FACADE-WIDE (one
``burn_in_mismatch_fold`` disables candidates + recovery too: a crate
wrong on one law is untrusted on all three), and ``fno_symbol_rust``'s
latch/budgets are untouched (per-facade isolation, unit-pinned both
directions). Internal-call note (binding — an implementer must not "fix"
either): an engaged crate ``spot_recovery_candidates`` computes law B
INSIDE Rust and does NOT decrement the ``candidates`` budget (only direct
routed calls do); conversely the public wrapper's ``stock_drift_fold``
call for the bucket probe IS routed, so a recovery call with a non-None
map may decrement the ``fold`` budget.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from brutex.data.fno_symbol_rust import data_rust_enabled
from brutex.data.lake_rust_render import _panic_belt_errors

if TYPE_CHECKING:
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: Engaged-compare samples each law must survive before shipping uncompared.
BURN_IN_SAMPLES: Final[int] = 16

#: The per-law burn-in keys.
_LAW_FOLD: Final[str] = "fold"
_LAW_CANDIDATES: Final[str] = "candidates"
_LAW_RECOVERY: Final[str] = "recovery"

#: ONE facade-wide latch — any law's guard/autorevert latches all three laws
#: HERE, never ``fno_symbol_rust`` (per-facade isolation). Plain-unlocked
#: (the house thread posture): a racing write is benign — both arms ship
#: oracle-contract values and the latch converges.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Independent per-law burn-in budgets (three 16-sample windows).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_FOLD: BURN_IN_SAMPLES,
    _LAW_CANDIDATES: BURN_IN_SAMPLES,
    _LAW_RECOVERY: BURN_IN_SAMPLES,
}

#: The 3-symbol handshake — ALL must be callable, else ``crate_stale`` (the
#: all-or-nothing PR-2 law: a PR-1-era wheel must never half-engage).
_CRATE_API: Final[tuple[str, ...]] = (
    "stock_drift_fold",
    "spot_key_candidates",
    "spot_recovery_candidates",
)

#: Law B emits at most TWO rungs (stripped-upper + the ``EXCH-`` form).
_CANDIDATES_MAX_LEN: Final[int] = 2

#: Warn-once / engaged-once memo (reset via the test seam only).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: Sentinel memo key for the engaged INFO.
_ENGAGED_KEY: Final[str] = "__engaged__"

#: Crate exceptions that DEGRADE to the oracle (warn-once), never crash.
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)


class FoldLaw(Protocol):
    """Structural type of the law-A oracle (``_stock_drift_fold_python``)."""

    def __call__(self, symbol: str) -> str:
        """Fold *symbol* into its canonical drift-bucket key."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class CandidatesLaw(Protocol):
    """Structural type of the law-B oracle (``_spot_key_candidates_python``)."""

    def __call__(self, symbol: str, exchange: str) -> tuple[str, ...]:
        """Return the ordered spot-key candidate rungs for *symbol*."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class RecoveryLaw(Protocol):
    """Structural type of the law-C oracle (``_spot_recovery_candidates_python``)."""

    def __call__(
        self,
        underlying: str,
        exchange: str,
        *,
        alias_pairs: Sequence[tuple[str, str]],
        drift_bucket: Sequence[str],
    ) -> tuple[str, ...]:
        """Return the ordered recovery candidate rungs for *underlying*."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def lake_keys_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched THIS facade OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_lake_keys_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Restore the FULL virgin state (test isolation ONLY).

    Clears the facade latch + ALL THREE per-law burn-in budgets + the
    warn-once/engaged memos. ``remaining=0`` puts the process in the
    post-burn-in steady state (the Rust-routed value ships with no oracle
    in the path — the differential suites depend on that). Never called
    from production code (the sibling reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[_LAW_FOLD] = remaining
    _BURN_IN_REMAINING[_LAW_CANDIDATES] = remaining
    _BURN_IN_REMAINING[_LAW_RECOVERY] = remaining
    _OBSERVABILITY_MEMO.clear()


def _refuse_if_strict(reason: str, **context: object) -> None:
    """Strict-mode gate for the two announcement choke points.

    DEC-RUST-STRICT-NO-FALLBACK-001. Imported FUNCTION-LOCALLY on purpose:
    this facade's module-scope ``brutex.*`` import set is AST-pinned to the
    two sanctioned flag/panic-belt sources by
    ``tests/unit/test_lake_keys_rust.py::test_facade_is_a_leaf_import_wise``
    (the leaf-import law), and widening that pin to admit a new module-scope
    import would WEAKEN it. The function-local import keeps the pin intact
    and matches the ``observability_rust`` precedent.
    """
    from brutex.config.rust_strict import refuse_if_strict

    refuse_if_strict("brutex.data.lake_keys_rust", reason, **context)


def _warn_once(reason: str, **context: object) -> None:
    """Log ``lake_keys_rust.fallback`` WARN once per process per *reason*.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Every reason funnelled here is CLASS A (``fold_error`` /
    ``candidates_error`` / ``recovery_error`` — the crate raised) or CLASS B
    (``result_refused_fold`` / ``result_refused_candidates`` /
    ``result_refused_recovery`` — refuse-poison rejected the crate's return
    shape). There is NO class-C reason at this choke point, so no exemption
    set: the seams take plain ``str`` symbols/exchanges plus an optional
    drift mapping, and the ONE input the crate cannot serve — a non-ASCII
    symbol — is filtered by the sibling ``fno_symbol_rust`` ASCII pre-gate
    BEFORE this facade is reached, so it never announces here. Strict-OFF is
    byte-identical to the prior behaviour.
    """
    _refuse_if_strict(reason, **context)
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("lake_keys_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch THIS facade's Rust path OFF for the remainder of the process.

    ``error=True`` logs ``lake_keys_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``lake_keys_rust.rust_path_disabled`` at WARN. Repeats are no-ops.

    DEC-RUST-STRICT-NO-FALLBACK-001: refuses under ``BRUTEX_RUST_STRICT``.
    Reasons here are CLASS A (``crate_unavailable`` / ``crate_stale`` /
    ``crate_panic``) or CLASS B (``burn_in_mismatch_<law>`` — the crate
    disagreed with the oracle); none is class C, so none is exempt. The
    refusal fires BEFORE the latch is written, so under strict mode the
    latch is never set and every ``_DISABLED_REASON[0] is not None``
    early-return is structurally unreachable. Strict-OFF is byte-identical
    to the prior behaviour.
    """
    _refuse_if_strict(reason, error=error, **context)
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("lake_keys_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("lake_keys_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_data``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A wheel
    that imports but lacks ANY :data:`_CRATE_API` callable is a pre-PR-2
    (PR-1-era, foreign or broken) install — latch ``crate_stale``
    (all-or-nothing: the three laws engage together or not at all).
    Fail-closed: never a new crash class.
    """
    try:
        crate = importlib.import_module("brutex_data")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-data (built by data-rust-parity.yml)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_data.{name} missing or not callable — "
                "the installed wheel predates the lake_keys laws (PR-1-era) — "
                "rebuild rust/brutex-data",
                remedy="rebuild + reinstall rust/brutex-data",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``lake_keys_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "lake_keys_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def _fold_result_valid(result: object) -> bool:
    """Structural refuse-poison gate for law A (fold output).

    Refused: non-``str``; non-ASCII output (ASCII in → ASCII out, so any
    non-ASCII is a pure poison signature); any ``0x20`` space or ``0x2D``
    hyphen byte (the fold removes both); any ASCII lowercase byte (the
    fold uppercases). Wrong-but-well-formed folds are burn-in/golden work.
    """
    if not isinstance(result, str):
        return False
    if not result.isascii():
        return False
    if " " in result or "-" in result:
        return False
    return not any("a" <= ch <= "z" for ch in result)


def _entries_structurally_clean(entries: tuple[object, ...]) -> bool:
    """Shared entry checks: every entry a non-ASCII-free ``str``, no dups."""
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str) or not entry.isascii():
            return False
        if entry in seen:
            return False
        seen.add(entry)
    return True


def _candidates_result_valid(result: object, symbol: str) -> bool:
    """Structural refuse-poison gate for law B (candidates output).

    Refused: non-``list``/``tuple``; any non-``str``/non-ASCII entry;
    ``len > 2`` (the law has exactly two rungs); any EMPTY entry (law B
    drops empties by construction); any entry ``== symbol`` — the RAW
    param, NEVER stripped (identity echo; the STRIPPED form is rung 1's
    LEGAL value); duplicates.
    """
    if not isinstance(result, (list, tuple)):
        return False
    entries = tuple(result)
    if len(entries) > _CANDIDATES_MAX_LEN:
        return False
    if not _entries_structurally_clean(entries):
        return False
    return all(entry not in ("", symbol) for entry in entries)


def _recovery_result_valid(
    result: object,
    underlying: str,
    alias_pairs: Sequence[tuple[str, str]],
    drift_bucket: Sequence[str],
) -> bool:
    """Structural refuse-poison gate for law C (recovery output).

    Refused: non-``list``/``tuple``; any non-``str``/non-ASCII entry; any
    entry ``==`` the RAW *underlying* param — NEVER stripped/upper-first
    (the RAW-identity law: a space-wrapped underlying's ladder LEGALLY
    emits its stripped form, which a normalizing gate would wrongly
    refuse); duplicates; ``len > 2·len(alias_pairs) + 3 + len(drift_bucket)``
    (the structural output ceiling: ≤2 adds per alias pair + the C6 form +
    ≤2 law-B rungs + ≤ the bucket). EMPTY entries are NOT refused —
    ``_add("")`` is oracle-reachable through an empty bucket entry.
    """
    if not isinstance(result, (list, tuple)):
        return False
    entries = tuple(result)
    if len(entries) > 2 * len(alias_pairs) + 3 + len(drift_bucket):
        return False
    if not _entries_structurally_clean(entries):
        return False
    return all(entry != underlying for entry in entries)


def _burn_in_ok(law: str, rust_result: object, oracle_result: object, **context: object) -> bool:
    """Per-law burn-in compare: exact equality vs the oracle, or autorevert.

    While the *law*'s budget is positive, compare EXACT equality; a
    mismatch latches the WHOLE facade (``burn_in_mismatch_<law>``, ERROR)
    and returns ``False`` — the caller ships the ORACLE value. A match
    decrements the law's own budget; at zero, log ``burn_in_complete``.
    """
    if rust_result != oracle_result:
        _disable(
            f"burn_in_mismatch_{law}",
            error=True,
            rust_result=repr(rust_result),
            python_result=repr(oracle_result),
            **context,
        )
        return False
    _BURN_IN_REMAINING[law] -= 1
    if _BURN_IN_REMAINING[law] == 0:
        _LOG.info("lake_keys_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return True


def routed_stock_drift_fold(symbol: str, *, python_law: FoldLaw) -> str:
    """The routed law-A fold — flag-gated Rust, oracle on every degrade path."""
    if not data_rust_enabled() or _DISABLED_REASON[0] is not None:
        return python_law(symbol)
    if not symbol.isascii():
        # ASCII pre-gate — a scope gate, deliberately log-free (§ module doc).
        return python_law(symbol)
    crate = _resolve_crate()
    if crate is None:
        return python_law(symbol)
    try:
        raw = crate.stock_drift_fold(symbol)
    except _DEGRADE_ERRORS as exc:
        _warn_once("fold_error", error_detail=repr(exc))
        return python_law(symbol)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set — OOM/interrupt/shutdown never a silent fallback;
        # sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(symbol)
    if not _fold_result_valid(raw):
        _warn_once("result_refused_fold", result=repr(raw), symbol=repr(symbol))
        return python_law(symbol)
    rust_result = str(raw)
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_FOLD] > 0:
        oracle_result = python_law(symbol)
        if not _burn_in_ok(_LAW_FOLD, rust_result, oracle_result, symbol=repr(symbol)):
            return oracle_result
    return rust_result


def routed_spot_key_candidates(
    symbol: str, exchange: str, *, python_law: CandidatesLaw
) -> tuple[str, ...]:
    """The routed law-B candidates — flag-gated Rust, oracle on every degrade path."""
    if not data_rust_enabled() or _DISABLED_REASON[0] is not None:
        return python_law(symbol, exchange)
    if not (symbol.isascii() and exchange.isascii()):
        return python_law(symbol, exchange)
    crate = _resolve_crate()
    if crate is None:
        return python_law(symbol, exchange)
    try:
        raw = crate.spot_key_candidates(symbol, exchange)
    except _DEGRADE_ERRORS as exc:
        _warn_once("candidates_error", error_detail=repr(exc))
        return python_law(symbol, exchange)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law(symbol, exchange)
    if not _candidates_result_valid(raw, symbol):
        _warn_once("result_refused_candidates", result=repr(raw), symbol=repr(symbol))
        return python_law(symbol, exchange)
    rust_result = tuple(str(entry) for entry in raw)
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_CANDIDATES] > 0:
        oracle_result = python_law(symbol, exchange)
        if not _burn_in_ok(_LAW_CANDIDATES, rust_result, oracle_result, symbol=repr(symbol)):
            return oracle_result
    return rust_result


def routed_spot_recovery_candidates(
    underlying: str,
    exchange: str,
    *,
    alias_pairs: Sequence[tuple[str, str]],
    drift_bucket: Sequence[str],
    python_law: RecoveryLaw,
) -> tuple[str, ...]:
    """The routed law-C recovery core — flag-gated Rust, oracle on every degrade path.

    The alias pairs (env read) + the drift bucket (dict probe) are computed
    ONCE in the public ``lake_keys`` wrapper and feed BOTH arms, so burn-in
    compares LAWS, not I/O.
    """

    def _oracle() -> tuple[str, ...]:
        return python_law(underlying, exchange, alias_pairs=alias_pairs, drift_bucket=drift_bucket)

    if not data_rust_enabled() or _DISABLED_REASON[0] is not None:
        return _oracle()
    if not (underlying.isascii() and exchange.isascii()):
        return _oracle()
    if not all(k.isascii() and v.isascii() for k, v in alias_pairs):
        return _oracle()
    if not all(entry.isascii() for entry in drift_bucket):
        return _oracle()
    crate = _resolve_crate()
    if crate is None:
        return _oracle()
    try:
        raw = crate.spot_recovery_candidates(
            underlying, exchange, list(alias_pairs), list(drift_bucket)
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("recovery_error", error_detail=repr(exc))
        return _oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return _oracle()
    if not _recovery_result_valid(raw, underlying, alias_pairs, drift_bucket):
        _warn_once("result_refused_recovery", result=repr(raw), underlying=repr(underlying))
        return _oracle()
    rust_result = tuple(str(entry) for entry in raw)
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_RECOVERY] > 0:
        oracle_result = _oracle()
        if not _burn_in_ok(_LAW_RECOVERY, rust_result, oracle_result, underlying=repr(underlying)):
            return oracle_result
    return rust_result


__all__ = [
    "BURN_IN_SAMPLES",
    "CandidatesLaw",
    "FoldLaw",
    "RecoveryLaw",
    "lake_keys_rust_disabled",
    "reset_lake_keys_rust_for_tests",
    "routed_spot_key_candidates",
    "routed_spot_recovery_candidates",
    "routed_stock_drift_fold",
]
