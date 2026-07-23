"""Default-OFF Rust seam for the two cross-verify pure-law surfaces.

DEC-CROSSVERIFY-RUST-CORE (first cut). The routed surfaces are
:func:`brutex.crossverify.csv_render.render_day_csv` (the pinned CSV byte
assembly) and :func:`brutex.crossverify.canonical.canonical_identity` +
:func:`~brutex.crossverify.canonical.s3_key` (the deny-by-default mapper +
the ONE key law) — when ``BRUTEX_CROSSVERIFY_RUST`` is ON they delegate the
pure string laws to the ``brutex_ops`` crate (`rust/brutex-ops`) through this
facade; the flag-OFF default runs the verbatim Python bodies in place,
byte-identical AND crate-untouched (bomb-crate spy-pinned).

The :mod:`brutex.data.lake_rust_stats` pattern (itself the PR-1
:mod:`brutex.data.lake_rust_render` shape), with TWO deliberate structural
differences, each documented:

* **No version guard** — the crate side is pure std-Rust string assembly
  locked to stdlib/`csv.writer` byte laws; there is no third-party
  wheel↔crate lockstep to attest (the #1914 deviation, same rationale). The
  per-law burn-in bit-compare + the flag-ON golden corpus are the drift
  teeth instead.
* **A LOCAL flag-vocabulary twin** — this package must not import the
  engine (`brutex.engine.sweep_lookahead` hard-imports the ``fno_math``
  wheel) nor grow a ``brutex.data.lake_rust_render`` dependency for one
  helper; the :data:`_TRUTHY`/:data:`_FALSY` literals + tristate law are
  therefore a LOCAL twin, equality-pinned against the engine vocabulary in
  the unit suite (the #1891 precedent).

Fail-closed layers, in order (every path returns/raises the PYTHON
oracle's outcome):

1. the caller-side flag gate (:func:`crossverify_rust_enabled` — default
   OFF; junk values loud-OFF once per process);
2. the import/API-surface handshake (:func:`_resolve_crate` — an absent
   wheel latches ``crate_unavailable``; a wheel missing a
   :data:`_CRATE_API` callable latches ``crate_stale``);
3. the per-law burn-in self-check — the first :data:`BURN_IN_SAMPLES`
   engaged calls per law compute BOTH arms and bit-compare; ANY difference
   ships the ORACLE value and autoreverts the Rust path for the process
   (the #1531/#1532 pattern, one ERROR);
4. the degrade set (:data:`_DEGRADE_ERRORS` + the pyo3 panic belt) — a
   crate raise serves the oracle with one capped WARN, never a new crash
   class;
5. the refuse-poison gates — a malformed crate return (wrong type/shape)
   or a typed CSV refusal code serves the oracle with one WARN. A typed
   refusal maps onto the EXISTING :class:`ExportContractError` semantics
   BY re-running the oracle: for genuinely-refusable input the oracle
   raises the exact pinned message bytes (never a new exception class);
   for a spuriously-refusing crate the oracle's bytes serve (self-heal).

``MemoryError`` re-raises raw (an OOM stays a global stop — the errors.py
doctrine); ``KeyboardInterrupt``/``SystemExit`` propagate.

Concurrency envelope (round-1 hostile-review NIT-3 — the sibling-facade
law, matching :mod:`brutex.data.lake_rust_stats` /
:mod:`brutex.data.lake_rust_render`): the latch
(:data:`_DISABLED_REASON`), the per-law burn-in budgets
(:data:`_BURN_IN_REMAINING`) and the warn-once memo
(:data:`_OBSERVABILITY_MEMO`) are the same documented exception to the
no-global-mutable-state rule as the lake facades' — plain UNLOCKED
writes. A racing thread (a BackgroundPullJob worker beside a CLI run in
one process, flag ON) can at worst emit a duplicate WARN or run ±1 extra
burn-in compare (memory-only), NEVER a wrong byte — a burn-in mismatch
ships the oracle value regardless of which thread observes it, and the
latch check-then-set race only delays the latch by one call. Default-OFF
keeps the whole surface latent in production today.

Cost honesty (Golden Rule #10): a uniformity conversion — the Python side
still runs the full Decimal/datetime validation + cell pre-formatting in
BOTH arms, so the engaged path adds one FFI hop per file/mapping and the
burn-in window pays a double render. No speedup is claimed anywhere.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The opt-in env flag (default OFF; Pattern A — only explicit truthy enables).
CROSSVERIFY_RUST_ENV: Final[str] = "BRUTEX_CROSSVERIFY_RUST"

#: Engaged calls per law bit-compared against the Python oracle before crate
#: values ship un-oracled (the #1531/#1532 burn-in law).
BURN_IN_SAMPLES: Final[int] = 16

#: Truthy env literals — the LOCAL twin of the engine flag vocabulary
#: (``brutex.engine.sweep_lookahead._TRUTHY``); NOT an import because the
#: engine package hard-imports the ``fno_math`` wheel (module docstring).
#: Equality-pinned in ``tests/unit/test_crossverify_rust_seam.py``.
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: Falsy env literals — the explicit kill-switch vocabulary, the mirror of
#: :data:`_TRUTHY` (same local-twin rationale + pin).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The per-law burn-in law names (dict keys + log tokens).
_LAW_CSV: Final[str] = "csv_render"
_LAW_CANONICAL: Final[str] = "canonical_identity"
_LAW_S3_KEY: Final[str] = "s3_key"

#: The process-local autorevert latch — ``[None]`` = live; a reason string
#: latches the Rust path OFF for the remainder of the process (the
#: documented no-global-mutable-state exception, the sibling-facade shape).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Remaining burn-in budget PER LAW (mutable cells; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_CSV: BURN_IN_SAMPLES,
    _LAW_CANONICAL: BURN_IN_SAMPLES,
    _LAW_S3_KEY: BURN_IN_SAMPLES,
}

#: Warn-once registry (per-process, test-resettable — the seams run per
#: exported symbol, ~760 per day; a sticky reason must not flood the logs).
_OBSERVABILITY_MEMO: Final[set[str]] = set()

#: The engaged-INFO memo key (never a warn reason).
_ENGAGED_KEY: Final[str] = "__engaged__"

#: The crate callables this seam requires (the API-surface handshake).
_CRATE_API: Final[tuple[str, ...]] = (
    "crossverify_render_csv",
    "crossverify_canonical_identity",
    "crossverify_s3_key",
)

#: The narrow degrade-to-oracle catch set (the sibling facades' law):
#: ``ValueError`` covers ``UnicodeEncodeError`` (lone-surrogate extraction);
#: ``TypeError``/``OverflowError`` are PyO3 argument-conversion failures;
#: ``RuntimeError`` is a mapped crate panic; ``AttributeError`` is the
#: broken-crate-internals belt. ``MemoryError`` is DELIBERATELY absent
#: (module docstring).
_DEGRADE_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: The mapped identity's field count (the ``CanonicalIdentity`` shape).
_IDENTITY_FIELD_COUNT: Final[int] = 6


def env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the flag vocabulary (local twin — module docs).

    ``True`` for a :data:`_TRUTHY` literal, ``False`` for a :data:`_FALSY`
    literal, ``None`` for anything else (unset/empty AND unrecognised — the
    caller owns the default + warn policy). Byte-identical semantics to
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate`, equality-pinned
    in the unit suite; NOT imported from there (module docstring).
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def crossverify_rust_enabled() -> bool:
    """Resolve ``BRUTEX_CROSSVERIFY_RUST`` (default ON, junk loud-OFF).

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): truthy →
    ``True``; falsy → ``False`` (the silent explicit kill switch); unset /
    whitespace-empty → ``True`` (the default); any OTHER non-empty value →
    ``False`` with one ``crossverify_rust.fallback`` WARN (reason
    ``flag_unrecognised``) per process — the junk arm is unchanged. Read
    per call so spawned workers resolve their own inherited environment
    (the sibling-facade posture).
    """
    raw = os.environ.get(CROSSVERIFY_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
        return False
    return True


def rust_seam_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_crossverify_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset the latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state so a
    returned value is the crate's with no oracle in the path (the flag-ON
    golden + differential suites depend on that — an armed burn-in would
    cushion a divergence by shipping oracle bytes). Never called from
    production paths (the sibling reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[_LAW_CSV] = remaining
    _BURN_IN_REMAINING[_LAW_CANONICAL] = remaining
    _BURN_IN_REMAINING[_LAW_S3_KEY] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``crossverify_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("crossverify_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``crossverify_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``crossverify_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("crossverify_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("crossverify_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_ops``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A wheel
    that imports but lacks a callable :data:`_CRATE_API` member (a
    pre-crossverify-lane wheel, a name collision, a hand-broken install)
    latches ``crate_stale`` (the stale-wheel handshake — this crate carries
    no third-party lockstep, so the symbol surface + the burn-in are the
    freshness teeth; module docstring). Fail-closed: never a new crash class.
    """
    try:
        crate = importlib.import_module("brutex_ops")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-ops (the ops-rust-parity lane's install step)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_ops.{name} missing or not callable — "
                "the installed wheel predates the crossverify lane (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-ops",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``crossverify_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "crossverify_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def _panic_belt_errors() -> tuple[type[BaseException], ...]:
    """The narrow FFI panic-belt catch set (the sibling facades' twin).

    ``pyo3_runtime.PanicException`` is the one ``BaseException`` subclass a
    pyo3 boundary can leak; the repo convention (CodeQL
    ``py/catch-base-exception``) forbids a non-re-raising literal ``except
    BaseException``, so the class is resolved GUARDEDLY by name and folded
    beside ``Exception``. ONE home is probed (``pyo3_runtime``) — this
    crate links no polars, so the ``polars.exceptions`` re-export home of
    the lake facades is deliberately not consulted (a polars-hosted
    PanicException is the SAME class object re-exported when both wheels
    are pyo3-built; the ``pyo3_runtime`` home is the canonical one). The
    release crate profile keeps the cargo-default UNWIND (the unification
    panic-profile ruling — NO ``panic = "abort"``), so a genuine runtime
    Rust panic reaches this belt as a catchable PanicException and degrades
    to the oracle (the crate cores are written structurally panic-free
    regardless — the belt is defense-in-depth).
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


def routed_render_csv(
    header: str,
    cells_rows: Sequence[Sequence[str]],
    *,
    python_render: Callable[[], bytes],
) -> bytes:
    """The routed CSV byte assembly — called ONLY when the flag is ON.

    *cells_rows* are the PRE-VALIDATED, PRE-FORMATTED cell rows the caller
    (:func:`~brutex.crossverify.csv_render.render_day_csv`) built by running
    the verbatim Python refusal laws FIRST — so on the integrated path every
    exception surfaces from the Python laws with the exact pinned message
    bytes BEFORE any FFI. EVERY path returns valid contract bytes or raises
    what the oracle raises: a latched / guarded / degraded call returns
    ``python_render()``, a typed crate refusal or malformed return serves
    the oracle with one WARN (module docstring layer 5), a burn-in mismatch
    returns the ORACLE bytes + autoreverts, and only a guard-clean,
    burn-in-clean call returns the Rust bytes.
    """
    if rust_seam_disabled():
        return python_render()
    crate = _resolve_crate()
    if crate is None:
        return python_render()
    try:
        rust_bytes, refusal = crate.crossverify_render_csv(
            header, [list(row) for row in cells_rows]
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("csv_render_error", error_detail=repr(exc))
        return python_render()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_render()
    if refusal is not None or not isinstance(rust_bytes, bytes):
        # A typed string-law refusal on PRE-VALIDATED cells marks drift
        # between the belts (or a direct hostile seam call); a non-bytes
        # return is a crate BUG. Either way the ORACLE decides: for
        # genuinely-refusable input it raises the exact pinned
        # ExportContractError bytes, else its bytes serve (self-heal) —
        # never a new exception class, never a published wrong byte.
        _warn_once(
            "csv_refusal_or_poison",
            refusal=None if refusal is None else tuple(refusal),
            result_type=type(rust_bytes).__name__,
        )
        return python_render()
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_CSV] > 0:
        oracle_bytes = python_render()
        if rust_bytes != oracle_bytes:
            _disable(
                "burn_in_mismatch_csv",
                error=True,
                rust_len=len(rust_bytes),
                python_len=len(oracle_bytes),
            )
            return oracle_bytes
        _BURN_IN_REMAINING[_LAW_CSV] -= 1
        if _BURN_IN_REMAINING[_LAW_CSV] == 0:
            _LOG.info("crossverify_rust.burn_in_complete", law=_LAW_CSV, samples=BURN_IN_SAMPLES)
    return rust_bytes


def routed_canonical_identity(
    *,
    exchange: str,
    segment: str,
    lake_symbol: str,
    fno_underlying: str | None,
    fno_expiry_iso: str | None,
    fno_is_future: bool,
    fno_exchange: str | None,
    python_oracle: Callable[[], tuple[str, str, str, str, str, str] | None],
) -> tuple[str, str, str, str, str, str] | None:
    """The routed identity mapping — called ONLY when the flag is ON.

    The F&O grammar fields arrive PYTHON-PARSED (``parse_fno_symbol`` is
    never re-implemented; the caller passes ``None`` fields exactly when the
    parse returned ``None``). Returns the mapped identity's six fields (the
    ``CanonicalIdentity`` declaration order) or ``None`` — deny-by-default
    in BOTH arms. Guard/degrade/burn-in laws as in the module docstring.
    """
    if rust_seam_disabled():
        return python_oracle()
    crate = _resolve_crate()
    if crate is None:
        return python_oracle()
    try:
        rust_fields = crate.crossverify_canonical_identity(
            exchange,
            segment,
            lake_symbol,
            fno_underlying,
            fno_expiry_iso,
            fno_is_future,
            fno_exchange,
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("canonical_error", error_detail=repr(exc))
        return python_oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_oracle()
    fields: tuple[str, str, str, str, str, str] | None
    if rust_fields is None:
        fields = None
    elif (
        isinstance(rust_fields, tuple)
        and len(rust_fields) == _IDENTITY_FIELD_COUNT
        and all(isinstance(field, str) for field in rust_fields)
    ):
        # Re-assembled element-wise so the DECLARED shape is what ships —
        # the shape gate above proved each member a str (mypy cannot narrow
        # a compound predicate; this is the narrow, never a mutation).
        fields = (
            rust_fields[0],
            rust_fields[1],
            rust_fields[2],
            rust_fields[3],
            rust_fields[4],
            rust_fields[5],
        )
    else:
        # Refuse-poison gate: a malformed mapping is a crate BUG — never
        # constructed into a CanonicalIdentity. Structurally unreachable
        # through the real crate; guards a stale/foreign install.
        _warn_once("canonical_result_refused", result_type=type(rust_fields).__name__)
        return python_oracle()
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_CANONICAL] > 0:
        oracle_fields = python_oracle()
        if fields != oracle_fields:
            _disable(
                "burn_in_mismatch_canonical",
                error=True,
                # Vendor-derived symbol material rides repr() (the #907
                # house log-injection law; round-1 hostile-review NIT-4).
                rust_fields=None if fields is None else repr(list(fields)),
                python_fields=None if oracle_fields is None else repr(list(oracle_fields)),
            )
            return oracle_fields
        _BURN_IN_REMAINING[_LAW_CANONICAL] -= 1
        if _BURN_IN_REMAINING[_LAW_CANONICAL] == 0:
            _LOG.info(
                "crossverify_rust.burn_in_complete",
                law=_LAW_CANONICAL,
                samples=BURN_IN_SAMPLES,
            )
    return fields


def routed_s3_key(
    *,
    prefix: str,
    day_iso: str,
    segment_token: str,
    canonical_symbol: str,
    python_oracle: Callable[[], str],
) -> str:
    """The routed S3 key assembly — called ONLY when the flag is ON.

    *day_iso* is the caller's own ``day.isoformat()`` (Python pre-formats;
    the crate never re-derives a date render — so a ``datetime`` subclass
    day renders identically in both arms by construction). Guard/degrade/
    burn-in laws as in the module docstring.
    """
    if rust_seam_disabled():
        return python_oracle()
    crate = _resolve_crate()
    if crate is None:
        return python_oracle()
    try:
        rust_key = crate.crossverify_s3_key(prefix, day_iso, segment_token, canonical_symbol)
    except _DEGRADE_ERRORS as exc:
        _warn_once("s3_key_error", error_detail=repr(exc))
        return python_oracle()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_oracle()
    if not isinstance(rust_key, str):
        _warn_once("s3_key_result_refused", result_type=type(rust_key).__name__)
        return python_oracle()
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_S3_KEY] > 0:
        oracle_key = python_oracle()
        if rust_key != oracle_key:
            _disable(
                "burn_in_mismatch_s3_key",
                error=True,
                # Vendor-derived symbol material rides repr() (the #907
                # house log-injection law; round-1 hostile-review NIT-4).
                rust_key=repr(rust_key),
                python_key=repr(oracle_key),
            )
            return oracle_key
        _BURN_IN_REMAINING[_LAW_S3_KEY] -= 1
        if _BURN_IN_REMAINING[_LAW_S3_KEY] == 0:
            _LOG.info(
                "crossverify_rust.burn_in_complete",
                law=_LAW_S3_KEY,
                samples=BURN_IN_SAMPLES,
            )
    return rust_key


__all__ = [
    "BURN_IN_SAMPLES",
    "CROSSVERIFY_RUST_ENV",
    "crossverify_rust_enabled",
    "env_flag_tristate",
    "reset_crossverify_rust_for_tests",
    "routed_canonical_identity",
    "routed_render_csv",
    "routed_s3_key",
    "rust_seam_disabled",
]
