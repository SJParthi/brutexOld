"""sweep_ops_rust — the default-OFF Rust routing facade for the sweep-ops PURE laws.

DEC-RUST-SWEEP-OPS-001 (slice 0 facade + slice 1 laws). Routes converted
sweep run-core/ops PURE laws through the ``rust/brutex-sweep-ops`` crate
behind the default-OFF ``BRUTEX_SWEEP_OPS_RUST`` flag, with the verbatim
Python law retained as the LIVE parity oracle (the
``brutex/data/window_ledger_rust.py`` template — itself the ADR-0020 / PR-1
``lake_rust_render`` shape).

Slice-1 routed laws (each wired at its owning module, which gates on
:func:`sweep_ops_rust_enabled` FIRST so the flag-OFF default path is
byte-identical including attribute-access order):

1. **Path-join law** (:func:`routed_run_path`) —
   ``brutex/engine/run_layout.py``'s ``run_dir`` + both ``mirror_run_dir``
   joins, via the ONE crate ``join_path_segments``. Only strings cross the
   FFI; the facade re-wraps the returned string in ``Path(...)`` (whose
   constructor normalisation makes the result object-equal to the
   chained-``/`` oracle for every accepted shape — burn-in-verified).
2. **Gate-config fragment law** (:func:`routed_gate_fragment`) —
   ``brutex/engine/sweep_identity.py``'s ``_gate_config_fragment`` emission
   law. Values cross as f64/i64 (bit-exact roundtrip — the greeks_batch
   precedent); EXACT-type gates (``float``/``int``, ``bool`` excluded)
   refuse anything lossy to the oracle. This law feeds RUN IDENTITY
   (``grid_identity`` / ``tuple_cache_key`` / ``_tuple_digest``) — the
   burn-in + the exact-type gates are the anti-drift teeth.
3. **Scaled stall-timeout law** (:func:`routed_stall_timeout`) —
   ``brutex/engine/sweep_stall_policy.py``'s raise-only scaled tail. The
   fail-loud env parses + ``StallPolicy`` construction stay Python.

## The fail-closed layers (the window_ledger_rust template, adapted)

* **Flag** — ``BRUTEX_SWEEP_OPS_RUST`` default OFF; truthy → ON, falsy →
  OFF, any other non-empty value → OFF with ONE loud
  ``sweep_ops_rust.fallback`` WARN (``flag_unrecognised``). OFF never
  imports the crate (spy-pinned) — the default path is byte-identical.
* **Import guard / SYMBOL handshake** — an absent wheel latches
  ``crate_unavailable``; a wheel that imports but lacks any
  :data:`_CRATE_API` callable is a pre-slice-1 (or foreign/broken) install
  and latches ``crate_stale``. Both serve the oracle with one loud WARN.
* **Degrade set** — ``ValueError``/``RuntimeError`` (the crate's declared
  FFI error surface), ``TypeError``/``OverflowError`` (PyO3 argument
  conversion — an out-of-``i64`` stage-3 knob lands here),
  ``AttributeError`` (broken-crate-internals belt). ``MemoryError`` is
  DELIBERATELY absent — OOM re-raises raw, identical to the pure-Python
  path; ``KeyboardInterrupt``/``SystemExit`` propagate raw.
* **Refuse-poison gates** — exact-type INPUT gates (a ``Path``/``bool``/
  subclass input never crosses) + structural OUTPUT gates (a non-``str``
  join, a malformed fragment tuple, a malformed timeout pair are REFUSED
  to the oracle — a stale/foreign wheel can only degrade, never serve).
* **Burn-in + autorevert** — the first :data:`BURN_IN_SAMPLES` engaged
  calls PER LAW compare the Rust value against the live oracle; ANY
  difference ships the ORACLE value and autoreverts the Rust path for the
  process (ERROR log — the #1531/#1532 pattern).

## Honesty (ADR-0020 banner; CLAUDE.md Golden Rule #10)

Expected payoff ≈ 0 — conversion is for uniformity, not speed. Every
slice-1 law is nanosecond-scale in Python and the FFI hop is a small
SLOWDOWN; acceptable because the seams run per launch / per run-dir
derivation (never per mask) and ship DEFAULT-OFF. The flag is read per call
(one ``os.environ`` probe) so spawn workers resolve their own inherited
environment. ``python_law`` parameters are ZERO-ARG closures (a documented
deviation from window_ledger_rust's arg-forwarding shape — three laws with
three shapes; closures keep the facade uniform).

Import discipline: stdlib + structlog + the TWO §11-COMMON helpers —
``env_flag_tristate`` from :mod:`brutex.engine.sweep_lookahead` (the
engine's flag-vocabulary home; the sweep_diffcheck/sweep_apriori precedent)
and ``_panic_belt_errors`` from :mod:`brutex.data.lake_rust_render` (the
ONE guardedly-resolved PanicException fold; the window_ledger_rust
cross-module-import precedent). Both are import-light, so the wired leaf
modules (run_layout et al.) stay light.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

# §11 COMMON — ONE flag vocabulary (the engine's home) + ONE panic belt (the
# brutex-lake facades' guardedly-resolved PanicException fold; underscore =
# module-privacy toward OUTSIDE consumers, the window_ledger_rust precedent).
from brutex.data.lake_rust_render import _panic_belt_errors
from brutex.engine.sweep_lookahead import env_flag_tristate

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_LOG = structlog.get_logger(__name__)

#: The opt-in env flag (default OFF; junk fails loud-OFF).
SWEEP_OPS_RUST_ENV: Final[str] = "BRUTEX_SWEEP_OPS_RUST"

#: Engaged calls PER LAW that are bit-compared against the live Python
#: oracle before Rust values ship uncompared (the #1531/#1532 pattern).
BURN_IN_SAMPLES: Final[int] = 16

#: The path-join-law burn-in budget key.
_LAW_RUN_PATH: Final[str] = "run_path"
#: The gate-fragment-law burn-in budget key.
_LAW_GATE_FRAGMENT: Final[str] = "gate_fragment"
#: The stall-timeout-law burn-in budget key.
_LAW_STALL_TIMEOUT: Final[str] = "stall_timeout"

#: Process-local OFF latch — the disable reason, or ``None`` when live.
_DISABLED_REASON: Final[list[str | None]] = [None]

#: Per-law burn-in budgets (mutated in place; reset seam below).
_BURN_IN_REMAINING: Final[dict[str, int]] = {
    _LAW_RUN_PATH: BURN_IN_SAMPLES,
    _LAW_GATE_FRAGMENT: BURN_IN_SAMPLES,
    _LAW_STALL_TIMEOUT: BURN_IN_SAMPLES,
}

#: The crate callables every slice-1 law needs — the SYMBOL handshake
#: surface. A wheel missing any is a pre-slice-1 install (``crate_stale``).
_CRATE_API: Final[tuple[str, ...]] = (
    "gate_config_fragment",
    "join_path_segments",
    "stall_scaled_timeout",
)

#: Structural arities of the crate's fragment legs + stall pair (the frozen
#: FFI shapes — refuse-poison gates below).
_OVERFIT_LEG_ARITY: Final[int] = 4
_STAGE3_LEG_ARITY: Final[int] = 3
_STALL_PAIR_ARITY: Final[int] = 2

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


def sweep_ops_rust_enabled() -> bool:
    """Resolve ``BRUTEX_SWEEP_OPS_RUST`` (default OFF, junk loud-OFF).

    Truthy literal → ``True``; falsy literal → ``False``; unset/blank →
    ``False`` (the default); any OTHER non-empty value → ``False`` with one
    ``sweep_ops_rust.fallback`` WARN (``flag_unrecognised``) per process.
    Read per call so spawn workers resolve their own environment (the
    sibling facades' law, byte-identical vocabulary via the imported
    :func:`env_flag_tristate`).
    """
    raw = os.environ.get(SWEEP_OPS_RUST_ENV, "")
    verdict = env_flag_tristate(raw)
    if verdict is not None:
        return verdict
    if raw.strip():
        _warn_once("flag_unrecognised", value=raw)
    return False


def sweep_ops_rust_disabled() -> bool:
    """``True`` iff a guard/autorevert latched the Rust path OFF this process."""
    return _DISABLED_REASON[0] is not None


def reset_sweep_ops_rust_for_tests(*, remaining: int = BURN_IN_SAMPLES) -> None:
    """Reset latch + per-law burn-in budgets + warn memo (test isolation ONLY).

    ``remaining=0`` puts the process in the post-burn-in steady state (the
    Rust value ships with no oracle in the path — the parity suites depend
    on that). Never called from production code (the sibling facades'
    reset-seam convention).
    """
    _DISABLED_REASON[0] = None
    for law in (_LAW_RUN_PATH, _LAW_GATE_FRAGMENT, _LAW_STALL_TIMEOUT):
        _BURN_IN_REMAINING[law] = remaining
    _OBSERVABILITY_MEMO.clear()


def _warn_once(reason: str, **context: object) -> None:
    """Log ``sweep_ops_rust.fallback`` WARN once per process per *reason*."""
    if reason in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(reason)
    _LOG.warning("sweep_ops_rust.fallback", reason=reason, **context)


def _disable(reason: str, *, error: bool = False, **context: object) -> None:
    """Latch the Rust path OFF for the remainder of the process.

    ``error=True`` logs ``sweep_ops_rust.autoreverted`` at ERROR (the
    burn-in autorevert — a genuine wrongness signal); guard latches log
    ``sweep_ops_rust.rust_path_disabled`` at WARN. Repeats are no-ops.
    """
    if _DISABLED_REASON[0] is not None:
        return
    _DISABLED_REASON[0] = reason
    if error:
        _LOG.error("sweep_ops_rust.autoreverted", reason=reason, **context)
    else:
        _LOG.warning("sweep_ops_rust.rust_path_disabled", reason=reason, **context)


def _resolve_crate() -> ModuleType | None:
    """Import guard + SYMBOL handshake: ``brutex_sweep_ops``, or latch OFF.

    An absent wheel (``ImportError`` — including the shim's own stale-wheel
    handshake raise) latches ``crate_unavailable``. A wheel that imports but
    lacks any :data:`_CRATE_API` callable is a pre-slice-1 (or
    foreign/broken) install — latch ``crate_stale``. Fail-closed: never a
    new crash class.
    """
    try:
        crate = importlib.import_module("brutex_sweep_ops")
    except ImportError as exc:
        _disable(
            "crate_unavailable",
            error_detail=repr(exc),
            remedy="maturin develop --release inside rust/brutex-sweep-ops "
            "(with the project venv active)",
        )
        return None
    for name in _CRATE_API:
        if not callable(getattr(crate, name, None)):
            _disable(
                "crate_stale",
                error_detail=f"brutex_sweep_ops.{name} missing or not callable — "
                "the installed wheel predates the slice-1 laws (or is foreign/broken)",
                remedy="rebuild + reinstall rust/brutex-sweep-ops (maturin develop --release)",
            )
            return None
    return crate


def _log_engaged_once(crate: ModuleType) -> None:
    """Log ``sweep_ops_rust.engaged`` INFO once per process."""
    if _ENGAGED_KEY in _OBSERVABILITY_MEMO:
        return
    _OBSERVABILITY_MEMO.add(_ENGAGED_KEY)
    _LOG.info(
        "sweep_ops_rust.engaged",
        crate_version=getattr(crate, "__version__", None),
    )


def _burn_in_ships_oracle(law: str, rust_value: object, oracle_value: object) -> bool:
    """Run the per-*law* burn-in compare; ``True`` ⇒ ship the ORACLE value.

    Decrements the law's budget on a clean compare (INFO once at zero); a
    mismatch autoreverts the process (ERROR) and returns ``True`` so the
    caller ships the oracle value. A zero budget is the steady state — the
    caller must not have computed the oracle (callers check the budget
    BEFORE building the oracle value; this helper is only reached with a
    positive budget).
    """
    if rust_value != oracle_value:
        _disable(
            f"burn_in_mismatch_{law}",
            error=True,
            rust_value=repr(rust_value),
            python_value=repr(oracle_value),
        )
        return True
    _BURN_IN_REMAINING[law] -= 1
    if _BURN_IN_REMAINING[law] == 0:
        _LOG.info("sweep_ops_rust.burn_in_complete", law=law, samples=BURN_IN_SAMPLES)
    return False


def routed_run_path(
    base: str,
    segments: tuple[str, ...],
    *,
    python_law: Callable[[], Path],
) -> Path:
    """The routed path-join law — flag-gated Rust with the live Python oracle.

    EVERY path returns a valid ``Path``: flag-OFF / latched / guarded /
    degraded calls return ``python_law()`` (the oracle), a burn-in mismatch
    returns the ORACLE value + autoreverts, and only a guard-clean,
    burn-in-clean call returns ``Path(<rust join>)``.

    Exact-type input gates: *base* and every segment must be exact ``str``
    (a ``Path``/subclass input never crosses the FFI) and every segment must
    be non-empty, relative and slash-free — the ONLY shapes the crate's join
    law mirrors ``pathlib`` for (anything else is refused to the oracle).
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if type(base) is not str or any(
        type(segment) is not str or not segment or "/" in segment for segment in segments
    ):
        _warn_once("run_path_input_refused", base_type=type(base).__name__)
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        joined = crate.join_path_segments(base, list(segments))
    except _DEGRADE_ERRORS as exc:
        _warn_once("run_path_error", error_detail=repr(exc))
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
    if not isinstance(joined, str):
        _warn_once("run_path_result_refused", result_type=type(joined).__name__)
        return python_law()
    rust_path = Path(joined)
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_RUN_PATH] > 0:
        oracle_path = python_law()
        if _burn_in_ships_oracle(_LAW_RUN_PATH, rust_path, oracle_path):
            return oracle_path
    return rust_path


def routed_gate_fragment(
    overfit_values: tuple[float, float, float, float] | None,
    stage3_enable: bool,
    stage3_knobs: tuple[int, int, int],
    *,
    python_law: Callable[[], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """The routed gate-config fragment law (feeds RUN IDENTITY — see docstring).

    Exact-type input gates: every overfit value must be exact ``float`` and
    every stage-3 knob exact ``int`` (``bool`` IS an ``int`` subclass whose
    canonical-JSON render differs — excluded), so the f64/i64 FFI roundtrip
    is bit-exact by construction; anything else is refused to the oracle.
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if overfit_values is not None and any(type(value) is not float for value in overfit_values):
        _warn_once("gate_fragment_input_refused", leg="overfit")
        return python_law()
    if any(type(knob) is not int for knob in stage3_knobs):
        _warn_once("gate_fragment_input_refused", leg="stage3")
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        overfit_leg, stage3_leg = crate.gate_config_fragment(
            overfit_values, bool(stage3_enable), *stage3_knobs
        )
    except _DEGRADE_ERRORS as exc:
        _warn_once("gate_fragment_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not _valid_fragment_legs(overfit_leg, stage3_leg):
        _warn_once("gate_fragment_result_refused")
        return python_law()
    fragment: dict[str, Any] = {}
    if overfit_leg is not None:
        fragment["overfit"] = list(overfit_leg)
    if stage3_leg is not None:
        fragment["stage3"] = list(stage3_leg)
    rust_fragment = fragment or None
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_GATE_FRAGMENT] > 0:
        oracle_fragment = python_law()
        if _burn_in_ships_oracle(_LAW_GATE_FRAGMENT, rust_fragment, oracle_fragment):
            return oracle_fragment
    return rust_fragment


def _valid_fragment_legs(overfit_leg: object, stage3_leg: object) -> bool:
    """Structural OUTPUT gate for the crate's fragment legs (refuse-poison)."""
    if overfit_leg is not None and not (
        isinstance(overfit_leg, tuple)
        and len(overfit_leg) == _OVERFIT_LEG_ARITY
        and all(type(value) is float for value in overfit_leg)
    ):
        return False
    return stage3_leg is None or (
        isinstance(stage3_leg, tuple)
        and len(stage3_leg) == _STAGE3_LEG_ARITY
        and all(type(knob) is int for knob in stage3_leg)
    )


def routed_stall_timeout(
    factor: float,
    estimate: float,
    base: float,
    *,
    python_law: Callable[[], tuple[float, bool]],
) -> tuple[float, bool]:
    """The routed scaled stall-timeout tail — ``(timeout_seconds, scaled?)``.

    Exact-type input gates: all three operands must be exact ``float`` (the
    callers' fail-loud env parses always produce floats; anything else is
    refused to the oracle so the f64 roundtrip is bit-exact).
    """
    if not sweep_ops_rust_enabled() or sweep_ops_rust_disabled():
        return python_law()
    if any(type(value) is not float for value in (factor, estimate, base)):
        _warn_once("stall_timeout_input_refused")
        return python_law()
    crate = _resolve_crate()
    if crate is None:
        return python_law()
    try:
        result = crate.stall_scaled_timeout(factor, estimate, base)
    except _DEGRADE_ERRORS as exc:
        _warn_once("stall_timeout_error", error_detail=repr(exc))
        return python_law()
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except _panic_belt_errors() as exc:
        _disable("crate_panic", error_detail=repr(exc))
        return python_law()
    if not (
        isinstance(result, tuple)
        and len(result) == _STALL_PAIR_ARITY
        and type(result[0]) is float
        and type(result[1]) is bool
    ):
        _warn_once("stall_timeout_result_refused", result_type=type(result).__name__)
        return python_law()
    rust_pair: tuple[float, bool] = (result[0], result[1])
    _log_engaged_once(crate)
    if _BURN_IN_REMAINING[_LAW_STALL_TIMEOUT] > 0:
        oracle_pair = python_law()
        if _burn_in_ships_oracle(_LAW_STALL_TIMEOUT, rust_pair, oracle_pair):
            return oracle_pair
    return rust_pair


__all__ = [
    "BURN_IN_SAMPLES",
    "SWEEP_OPS_RUST_ENV",
    "reset_sweep_ops_rust_for_tests",
    "routed_gate_fragment",
    "routed_run_path",
    "routed_stall_timeout",
    "sweep_ops_rust_disabled",
    "sweep_ops_rust_enabled",
]
