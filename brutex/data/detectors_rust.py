"""detectors_rust — the default-OFF Rust routing facade for the T24 detector laws.

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (SLICE T24 PR-1; MIGRATION.md #1948).
Routes THREE cold data-integrity detector pure laws through the
``rust/brutex-lake`` crate behind three default-OFF flags, with the verbatim
Python law retained as the LIVE parity oracle (the ADR-0020
``lake_rust_render`` / G8 template, in the G6 ``render_shard`` integration
shape — the TARGET module gates its own flag and hands this facade zero-arg
factories, so no verdict dataclass ever has to import back into this module):

1. **Raw-integrity digest** (``BRUTEX_LAKE_RUST_RAW_INTEGRITY``) — the
   ``hashlib.blake2b(canonical, digest_size=32).hexdigest()`` leg of
   :func:`brutex.data.raw_integrity.hash_payload`. The CANONICALISATION
   (``json.dumps`` — CPython's ``float.__repr__`` byte law) stays Python:
   bytes cross IN, lowercase ASCII hex crosses OUT.
2. **Interior bar-less run walker** (``BRUTEX_LAKE_RUST_INTERIOR_GAP``) —
   the grouping decision of
   :func:`brutex.data.interior_gap_detect._interior_barless_runs`. The NSE
   calendar + observed overlay stay Python (the target's payload factory
   walks the interior once and passes TRADING-session ordinals); Rust groups
   the maximal bar-less runs; this facade materializes the dates.
3. **Partial-window truncation verdict** (``BRUTEX_LAKE_RUST_PARTIAL_WINDOW``)
   — the WHOLE decision tree of
   :func:`brutex.data.partial_window_detect.classify_partial_window` over
   plain ordinals + a Python-computed per-day SESSION GRID; the target
   materializes the ``PartialWindowVerdict`` via its ``materialize``
   callback. A session walk stepping OFF the grid ABSTAINS (the
   DEC-FEED-RUST-CORE-001 envelope contract) — one WARN per process, the
   oracle serves.

## The fail-closed layers (the PR-1 template, adapted)

* **Flags** — three per-module flags, each default OFF (truthy → ON, falsy →
  OFF, junk → loud-OFF once; the §11 COMMON :func:`env_flag_tristate`
  imported from the sibling facade). The flag gate lives in the TARGET
  modules (the G6 shape) so flag OFF is the literal verbatim oracle call —
  the crate is never imported (bomb-crate spy-pinned).
* **ONE shared latch** — the three laws share one process-local OFF latch
  (the PR-2 lake_rust_render precedent: either profile's trip disables
  both — DISCLOSED): any law's crate-absent / crate-stale / panic /
  burn-in-mismatch latch serves the oracle for ALL detector laws for the
  remainder of the process. Per-law burn-in budgets stay independent.
* **Import guard / SYMBOL handshake** — polars-free laws, so the PR-1
  polars VERSION guard is meaningless here (the G8 disclosed deviation):
  an absent wheel latches ``crate_unavailable``; a wheel missing ANY of the
  three law callables is a pre-T24 (or foreign/broken) install and latches
  ``crate_stale``.
* **Degrade set** — ``ValueError``/``RuntimeError`` (the crate's declared
  FFI error surface — NOTE: a Rust panic the crate's own ``catch_unwind``
  boundary CATCHES surfaces as ``PyRuntimeError`` and lands in THIS arm,
  a warn-once degrade with NO latch), ``TypeError``/``OverflowError``
  (PyO3 argument conversion — an out-of-``i64`` ordinal lands here),
  ``AttributeError`` (broken-crate-internals belt) → oracle with one
  capped WARN per reason. ``MemoryError`` is DELIBERATELY absent — OOM
  re-raises raw, identical to the pure-Python path;
  ``KeyboardInterrupt``/``SystemExit`` likewise. The narrow panic belt
  (:func:`_panic_belt_errors` — imported from the sibling facade, ONE
  copy per §11 COMMON) latches ``crate_panic`` ONLY for a panic that
  ESCAPES ``catch_unwind`` as a ``pyo3_runtime.PanicException`` (the
  executed truth — the round-1 review wording fold).
* **Refuse-poison gates** — a wrong-shaped crate return (non-str hex,
  junk run tuples / out-of-``date``-range ordinals, malformed verdict
  fields or an unknown reason token) is REFUSED to the oracle with one
  WARN — a poisoned wheel can only degrade, never corrupt.
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls PER LAW compare the (materialized) Rust value against the live
  oracle; ANY difference ships the ORACLE value and autoreverts the Rust
  path for the process (ERROR log — the #1531/#1532 pattern).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. All three
laws are cold pull-tail / doctor diagnostics; the FFI hop plus the payload
prep is a small SLOWDOWN, accepted because the surrounding paths are
vendor-I/O-dominated and the seams ship DEFAULT-OFF (a default flip needs
its own DEC). These detectors gate REAL ledger demotions
(DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001 #1031) —
decision parity, not speed, is the bar, hence the burn-in + the hostile
golden/differential corpus. Flags are read per call (one ``os.environ``
probe) so spawn workers resolve their own inherited environment.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

import structlog

# §11 COMMON — ONE flag vocabulary + ONE panic belt for the brutex-lake
# facades. `env_flag_tristate` is the sibling's public helper;
# `_panic_belt_errors` is a same-package private import (the underscore is
# module-privacy toward OUTSIDE consumers — duplicating the guardedly-
# resolved PanicException fold here would be the §11 violation; the G8
# window_ledger_rust precedent).
from brutex.data.lake_rust_render import _panic_belt_errors, env_flag_tristate

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The raw-integrity digest opt-in flag (default OFF; junk fails loud-OFF).
LAKE_RUST_RAW_INTEGRITY_ENV: Final[str] = "BRUTEX_LAKE_RUST_RAW_INTEGRITY"
#: The interior-gap run-walker opt-in flag.
LAKE_RUST_INTERIOR_GAP_ENV: Final[str] = "BRUTEX_LAKE_RUST_INTERIOR_GAP"
#: The partial-window verdict opt-in flag.
LAKE_RUST_PARTIAL_WINDOW_ENV: Final[str] = "BRUTEX_LAKE_RUST_PARTIAL_WINDOW"

#: Engaged calls PER LAW that are bit-compared against the live Python
#: oracle before Rust values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: The digest-law burn-in budget key.
_LAW_HASH: Final[str] = "hash"
#: The interior-runs-law burn-in budget key.
_LAW_INTERIOR_RUNS: Final[str] = "interior_runs"
#: The partial-window-law burn-in budget key.
_LAW_PARTIAL_WINDOW: Final[str] = "partial_window"

#: Process-local OFF latch — ONE latch shared by all three laws (module
#: docstring; the PR-2 shared-latch precedent, disclosed).
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_HASH: BURN_IN_SAMPLES,
    _LAW_INTERIOR_RUNS: BURN_IN_SAMPLES,
    _LAW_PARTIAL_WINDOW: BURN_IN_SAMPLES,
}

#: The crate callables ALL three laws need — the all-or-nothing SYMBOL
#: handshake surface. A wheel missing any is a pre-T24 install
#: (``crate_stale``).
_CRATE_API: Final[tuple[str, ...]] = (
    "raw_integrity_blake2b_hex",
    "interior_barless_runs",
    "classify_partial_window_verdict",
)

#: Once-per-process-per-reason log memo.
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

#: The production digest width (`raw_integrity._DIGEST_SIZE` twin — the
#: routed call passes it explicitly so the two can never drift silently:
#: the burn-in compares against the oracle's own digest).
_RAW_INTEGRITY_DIGEST_SIZE: Final[int] = 32

#: `date.toordinal()` bounds — the refuse-poison range for ordinals a
#: crate returns (a `date.fromordinal` outside it would raise).
_MIN_DATE_ORDINAL: Final[int] = 1
_MAX_DATE_ORDINAL: Final[int] = 3_652_059  # date.max.toordinal()

#: The crate run tuple arity — `(run_first, run_last, run_sessions)`.
_RUN_TUPLE_ARITY: Final[int] = 3

#: The crate verdict-fields arity — `(reason, last_bar, expected_end, missing)`.
_VERDICT_FIELDS_ARITY: Final[int] = 4


@dataclass(frozen=True, slots=True)
class InteriorRunsPayload:
    """The ints-only FFI payload for the interior run walker.

    Built by the target's payload factory: ``covered_ordinals`` are the
    covered-date ordinals (order-preserving — membership is a set lookup
    crate-side) and ``interior_session_ordinals`` the ordered TRADING-session
    ordinals STRICTLY between the first and last covered date, walked with
    the oracle's own two-gate law (the covered-breaks-static fold,
    DEC-SEED-OBSERVED-OVERLAY-001): a COVERED date enters iff the STATIC
    :func:`~brutex.calendars.nse.is_trading_day` says session, a NON-covered
    date iff
    :func:`~brutex.calendars.observed_sessions.is_trading_day_observed`
    does (the calendar stays Python — never re-implemented in Rust).
    """

    covered_ordinals: tuple[int, ...]
    interior_session_ordinals: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PartialWindowPayload:
    """The ints-only FFI payload for the partial-window verdict.

    ``trading_flags`` is the Python-computed SESSION GRID — one
    ``is_trading_day_observed`` flag per day over
    ``[span_start_ordinal, span_start_ordinal + len(trading_flags))``;
    ``coverage_ok`` folds the three ``is_holiday_data_available`` facts
    (calendar-availability stays Python). ``tolerance``/``live_edge`` are
    the caller-resolved tunables (ONE env read, shared by both arms).
    """

    last_bar_ordinal: int | None
    window_end_ordinal: int
    today_ordinal: int
    expiry_ordinal: int | None
    coverage_ok: bool
    tolerance: int
    live_edge: int
    span_start_ordinal: int
    trading_flags: tuple[bool, ...]


def raw_integrity_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_RAW_INTEGRITY`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_RAW_INTEGRITY_ENV)


def interior_gap_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_INTERIOR_GAP`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_INTERIOR_GAP_ENV)


def partial_window_rust_enabled() -> bool:
    """Resolve ``BRUTEX_LAKE_RUST_PARTIAL_WINDOW`` (default OFF, junk loud-OFF)."""
    return _flag_enabled(LAKE_RUST_PARTIAL_WINDOW_ENV)


def rust_detectors_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_detectors_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    Rust value ships with no oracle in the path — the differential suites
    depend on that). Never called from production code (the sibling
    facades' reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    _BURN_IN_REMAINING[_LAW_HASH] = remaining
    _BURN_IN_REMAINING[_LAW_INTERIOR_RUNS] = remaining
    _BURN_IN_REMAINING[_LAW_PARTIAL_WINDOW] = remaining
    _OBSERVABILITY_MEMO.clear()


def _flag_enabled(env_var: str) -> bool:
    """The ONE tristate resolution all three flags share (per-call env read).

    Truthy literal → ``True``; falsy literal → ``False``; unset/blank →
    ``False`` (the default); any OTHER non-empty value → ``False`` with one
    ``detectors_rust.fallback`` WARN (``flag_unrecognised``, naming the
    variable) per process per variable.
    """
    raw = os.environ.get(env_var, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once(f"flag_unrecognised:{env_var}", value=raw, env_var=env_var)
    return False


def _warn_once(reason: str, **context: object) -> None:
    """Log ``detectors_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("detectors_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``detectors_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``detectors_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("detectors_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("detectors_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_lake``, or latch OFF + ``None``.

    An absent wheel (``ImportError``) latches ``crate_unavailable``. A wheel
    that imports but lacks ANY :data:`_CRATE_API` callable is a pre-T24
    (or foreign/broken) install — latch ``crate_stale`` (the stale-wheel
    handshake; the polars version guard is meaningless for a polars-free
    law, module docstring). Fail-closed: never a new crash class.
    """
    try:
        crate = importlib.import_module("brutex_lake")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="pip install rust/brutex-lake (see docs/runbooks/lake-rust-render-lockstep.md)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_lake.{name} missing or not callable — "
                "the installed wheel predates the T24 detector laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-lake "
                "(see docs/runbooks/lake-rust-render-lockstep.md)",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``detectors_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info("detectors_rust.engaged", crate_version=getattr(crate, "__version__", None))


def _burn_in_check[VerdictT](
    law: str, rust_value: VerdictT, python_law: Callable[[], VerdictT]
) -> VerdictT:
    """The per-law burn-in leg: compare *rust_value* against the live oracle.

    Returns the value to SHIP — the Rust value on a clean compare (or in
    the post-burn-in steady state), the ORACLE value (+ autorevert latch,
    ERROR log) on any difference.
    """
    if _BURN_IN_REMAINING[law] <= 0:
        return rust_value
    oracle_value = python_law()
    if rust_value != oracle_value:
        _disable(
            f"burn_in_mismatch_{law}",
            error=True,
            rust_value=repr(rust_value),
            python_value=repr(oracle_value),
        )
        return oracle_value
    _BURN_IN_REMAINING[law] -= 1
    if _BURN_IN_REMAINING[law] == 0:
        _LOG.info("detectors_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return rust_value


def routed_hash_payload_hex(canonical: bytes, *, python_law: Callable[[], str]) -> str:
    """The routed raw-integrity digest — called ONLY when the flag is ON.

    *canonical* is the Python-computed canonical JSON byte string (the
    ``json.dumps`` law stays Python — module docstring); *python_law* is the
    zero-arg verbatim ``hashlib.blake2b`` oracle. EVERY path returns a valid
    hex digest: latched / guarded / degraded calls return ``python_law()``,
    a burn-in mismatch returns the ORACLE digest + autoreverts, and only a
    guard-clean, burn-in-clean call returns the Rust digest.
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        rust_hex = crate.raw_integrity_blake2b_hex(canonical, _RAW_INTEGRITY_DIGEST_SIZE)
    except _DEGRADE_ERRORS as exc:
        _warn_once("hash_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        # The escape set (the errors.py doctrine): operator interrupt /
        # interpreter shutdown / OOM stay global — never a silent fallback.
        # Sits ABOVE the panic belt so its `Exception` member can never
        # swallow a MemoryError.
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not isinstance(rust_hex, str):
        # Refuse-poison gate: a non-str return is a crate BUG — never
        # served (structurally unreachable through the real crate; guards a
        # stale/foreign install).
        _warn_once("hash_result_refused", result_type=type(rust_hex).__name__)
        return python_law()
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_HASH, rust_hex, python_law)


def routed_interior_barless_runs(
    *,
    payload_factory: Callable[[], InteriorRunsPayload],
    python_law: Callable[[], list[tuple[date, date, int]]],
) -> list[tuple[date, date, int]]:
    """The routed interior run walker — called ONLY when the flag is ON.

    *payload_factory* builds the ints-only :class:`InteriorRunsPayload`
    (the target's one calendar walk — a latched process never pays it);
    *python_law* is the zero-arg verbatim walker oracle. The crate's int
    3-tuples are materialized to ``(date, date, int)`` here
    (``date.fromordinal``) behind a refuse-poison shape gate.
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        payload = payload_factory()
        rust_runs = crate.interior_barless_runs(
            payload.covered_ordinals, payload.interior_session_ordinals
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("interior_runs_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    materialized = _materialize_runs(rust_runs)
    if materialized is None:
        _warn_once("interior_runs_result_refused", result_type=type(rust_runs).__name__)
        return python_law()
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_INTERIOR_RUNS, materialized, python_law)


def _materialize_runs(rust_runs: object) -> list[tuple[date, date, int]] | None:
    """Materialize the crate's run tuples to dates, or ``None`` to refuse.

    The refuse-poison gate for the interior-runs law: anything but a list
    of ``(int, int, int)`` 3-tuples with both ordinals inside the ``date``
    domain is refused to the oracle — a poisoned wheel can only degrade,
    never mint a junk date.
    """
    if not isinstance(rust_runs, list):
        return None
    out: list[tuple[date, date, int]] = []
    for item in rust_runs:
        if not (isinstance(item, tuple) and len(item) == _RUN_TUPLE_ARITY):
            return None
        first, last, sessions = item
        if not all(isinstance(value, int) for value in (first, last, sessions)):
            return None
        if not (
            _MIN_DATE_ORDINAL <= first <= _MAX_DATE_ORDINAL
            and _MIN_DATE_ORDINAL <= last <= _MAX_DATE_ORDINAL
        ):
            return None
        out.append((date.fromordinal(first), date.fromordinal(last), sessions))
    return out


def routed_classify_partial_window[VerdictT](
    *,
    payload_factory: Callable[[], PartialWindowPayload],
    python_law: Callable[[], VerdictT],
    materialize: Callable[[str, int | None, int | None, int], VerdictT],
) -> VerdictT:
    """The routed partial-window verdict — called ONLY when the flag is ON.

    *payload_factory* builds the ints-only :class:`PartialWindowPayload`
    (the target's IST conversions + coverage facts + session grid);
    *python_law* is the zero-arg verbatim classifier oracle; *materialize*
    turns the crate's ``(reason, last_bar_ordinal, expected_end_ordinal,
    missing_sessions)`` fields into the target's verdict object (defined
    target-side so this facade never imports the verdict dataclass — no
    import cycle). A crate ``None`` is the ABSTAIN contract (a session walk
    stepped off the grid) — one WARN per process, the oracle serves.
    """
    if rust_detectors_disabled():
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        payload = payload_factory()
        fields = crate.classify_partial_window_verdict(
            payload.last_bar_ordinal,
            payload.window_end_ordinal,
            payload.today_ordinal,
            payload.expiry_ordinal,
            payload.coverage_ok,
            payload.tolerance,
            payload.live_edge,
            payload.span_start_ordinal,
            payload.trading_flags,
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("partial_window_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if fields is None:
        # The envelope ABSTAIN (a walk stepped off the Python-built grid) —
        # an engagement miss, never a fault: warn once, serve the oracle.
        _warn_once("partial_window_abstain")
        return python_law()
    if not _partial_fields_well_formed(fields):
        _warn_once("partial_window_result_refused", result_type=type(fields).__name__)
        return python_law()
    reason, last_bar_ordinal, expected_end_ordinal, missing = fields
    verdict = materialize(reason, last_bar_ordinal, expected_end_ordinal, missing)
    _log_engaged_once(crate)
    return _burn_in_check(_LAW_PARTIAL_WINDOW, verdict, python_law)


#: The KNOWN partial-window reason-token vocabulary — the refuse-poison
#: MEMBERSHIP set for the crate-returned ``reason`` field (the round-1
#: review hardening). SOURCE: the inline ``reason="…"`` literals of
#: :func:`brutex.data.partial_window_detect._classify_partial_window_python`
#: (the oracle mints them as literals — no named constant exists there to
#: import, and importing that module HERE would be the verdict-dataclass
#: import cycle the G6 routing shape exists to avoid), so the set is
#: defined beside its gate and MIRROR-PINNED against the oracle's own
#: source by ``tests/unit/test_detectors_rust.py``.
_KNOWN_PARTIAL_REASONS: Final[frozenset[str]] = frozenset(
    {
        "empty",
        "out_of_calendar_coverage",
        "at_expiry",
        "live_edge",
        "complete",
        "truncated",
        "sparse_complete",
    }
)


def _partial_fields_well_formed(fields: object) -> bool:
    """The refuse-poison shape gate for the partial-window verdict fields.

    Beyond arity / field types / the ``date``-domain ordinal bounds, the
    ``reason`` must be a member of :data:`_KNOWN_PARTIAL_REASONS` — an
    unknown token is a stale/foreign crate speaking a different verdict
    vocabulary, refused to the oracle exactly like every other shape
    failure (one WARN; degrade, never a latch).
    """
    if not (isinstance(fields, tuple) and len(fields) == _VERDICT_FIELDS_ARITY):
        return False
    reason, last_bar_ordinal, expected_end_ordinal, missing = fields
    if not isinstance(reason, str) or reason not in _KNOWN_PARTIAL_REASONS:
        return False
    for ordinal in (last_bar_ordinal, expected_end_ordinal):
        if ordinal is None:
            continue
        if not isinstance(ordinal, int):
            return False
        if not _MIN_DATE_ORDINAL <= ordinal <= _MAX_DATE_ORDINAL:
            return False
    return isinstance(missing, int)


__all__ = [
    "BURN_IN_SAMPLES",
    "LAKE_RUST_INTERIOR_GAP_ENV",
    "LAKE_RUST_PARTIAL_WINDOW_ENV",
    "LAKE_RUST_RAW_INTEGRITY_ENV",
    "InteriorRunsPayload",
    "PartialWindowPayload",
    "interior_gap_rust_enabled",
    "partial_window_rust_enabled",
    "raw_integrity_rust_enabled",
    "reset_detectors_rust_for_tests",
    "routed_classify_partial_window",
    "routed_hash_payload_hex",
    "routed_interior_barless_runs",
    "rust_detectors_disabled",
]
