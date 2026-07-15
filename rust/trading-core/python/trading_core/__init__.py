"""trading_core — shared indicator + 52-condition vocabulary kernel (PyO3 bridge).

The single source of truth for ``DEC-SHARED-CORE-001`` (``docs/MIGRATION.md`` #441).
The compiled cdylib ships as the private submodule ``trading_core._trading_core``;
this ``__init__.py`` re-exports its symbols so BruteX can write
``import trading_core; trading_core.evaluate_with_levels(...)``.

As of Phase 2b the BruteX vocabulary delegates to this wheel at runtime (the
wheel is built by maturin and installed in every CI job that imports
``brutex.vocabulary``). Exposed surface:

* ``evaluate_with_levels`` — BruteX's production per-bar evaluator (pre-built
  levels, no recompute).
* ``evaluate_from_raw`` — the parity path (recomputes pivots/fib from pdh/pdl/pdc).
* ``evaluate_spec`` — strategy-as-DATA (``DEC-STRATEGYSPEC-CORE-001``): a required-
  bits hex mask + direction over the same pre-built-levels ABI, returning
  ``(bar_bits, fires, direction)`` where ``fires = (bar_bits & mask) == mask`` —
  the backtest==live deploy contract (thin wrapper over ``evaluate_with_levels``,
  the frozen kernel untouched). ``SPEC_SCHEMA_VERSION`` / ``IMPLEMENTED_MASK`` are
  the companion constants.
* ``compute_pivot_levels`` / ``compute_fib_levels`` — the single-source indicator
  math BruteX's ``brutex.indicators`` wrappers pack into their dataclasses.
* ``compute_ema`` / ``ema_alpha`` / ``ema_seed`` / ``ema_step`` — the single-source
  EMA 20/200 recurrence BruteX's ``brutex.indicators.ema`` shim delegates to
  (``DEC-EMA-RUST-PORT-001``).
* ``greeks`` — the ONE shared Black-76 Greeks + verified Jäckel-IV seam
  (``DEC-GREEKS-ENGINE-PR2-001``): ``greeks(price, sigma, forward, strike,
  t_years, rate, is_call)`` → ``(iv, delta, gamma, theta, vega, rho, price,
  admissible)``; ``sigma=Some`` prices directly (live path), ``sigma=None``
  solves IV from the traded price first (historical path). BruteX's thin
  ``brutex.options.greeks`` wrapper delegates here; the ``GREEKS_*`` constants
  mirror the kernel's clamp / floor / admission constants.
* ``MarketStructureState`` — the stateful, look-ahead-clean BOS/CHoCH detector
  BruteX's ``stage1a`` engine drives per bar so the per-bar compute runs in Rust
  (the bit-identical backtest==live path; ``DEC-NIFTY-BOS-CHOCH-001``). The
  former ``VwapState`` twin was DELETED with the bits 52-53 tombstone (the
  VWAP/volume wipe — the spot-only engine has no traded volume); the 0.4.0
  shim REFUSES a stale wheel that still exposes it (see below).
* ``NARROW_CPR_WIDTH_BPS`` — the Central Pivot Range narrow-day threshold
  (bits 60-63; the ONE named constant, cross-pinnable from Python tests).
* ``stage1a_bar_bits_batch`` — the batch Stage-1A bar-bits kernel
  (``DEC-STAGE1A-RUST-BATCH-001``): one call per ``(instrument, tf)`` build over
  prepass-resolved columns/day-tables, returning one u128 mask per bar. The
  default-OFF ``BRUTEX_SWEEP_RUST_STAGE1A`` seam in
  ``brutex.engine.rust_stage1a`` is its ONE production consumer.

Wheel-source freshness handshake (R2-CC-G1)
-------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol this
shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export runs, so a
stale installed wheel that predates a source-tree symbol (the exact failure
three audit agents hit — a ``.so`` missing ``seconds_of_day``) raises a clear,
actionable ``ImportError`` naming the missing symbol(s) instead of a bare
symbol-not-found deep inside a consumer. ``__build_crate_version__`` (derived
from the compiled module's ``CARGO_PKG_VERSION``-baked ``__version__``, never
hardcoded) plus the in-repo ``EXPECTED_BUILD.json`` fingerprint give
``brutex doctor`` a wheel-freshness stage.

The import-time handshake is deliberately UNCONDITIONAL — there is NO env
escape. One could not help anyway: a ``.so`` genuinely missing a re-exported
symbol would fail the ``from ._trading_core import (...)`` below regardless,
so the only remedy for a symbol-stale install is the rebuild the ImportError
message names. Consequently any entry point that (transitively) imports
``trading_core`` — including every ``brutex`` CLI command via
``brutex.vocabulary`` — hard-fails on a symbol-stale wheel with THIS
actionable message; ``brutex doctor``'s softer ``wheel_freshness`` WARN stage
covers only the drift classes that still import (see ``brutex/doctor.py``).

HONEST residual gap (Golden Rule #10): the version + symbol-set handshake
catches a stale wheel that dropped/added a symbol or changed crate version. It
does NOT catch same-version, symbol-compatible BEHAVIORAL drift (Rust source
changed while version + symbol surface stayed identical). The formerly-named
"``build.rs`` source-content digest" follow-up is now CLOSED
(DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258): ``build.rs`` bakes the
crate-source BLAKE3 digest into the compiled module as ``__source_digest__``
and ``brutex doctor``'s ``wheel_source_digest`` stage compares it (WARN,
non-fatal) against the Python-twin recompute over the repo checkout —
LOCAL-machine scope only (CI always builds wheels from source, so it can
never see a stale wheel); this import-time handshake deliberately does NOT
gate on the digest (an older wheel must keep importing so the doctor can
warn).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` (read by ``brutex doctor``) mirrors this
#: list; a test pins the two equal so they cannot drift.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "FIB_LEVELS_LEN",
    "GREEKS_DELTA_ADMISSION_FLOOR",
    "GREEKS_IV_CLAMP_MAX",
    "GREEKS_IV_CLAMP_MIN",
    "GREEKS_T_FLOOR_YEARS",
    "GREEKS_VEGA_ADMISSION_FLOOR",
    "IMPLEMENTED_MASK",
    "MarketStructureState",
    "NARROW_CPR_WIDTH_BPS",
    "PIVOT_LEVELS_LEN",
    "SPEC_SCHEMA_VERSION",
    "TOTAL_CONDITIONS",
    "__version__",
    "compute_ema",
    "compute_fib_levels",
    "compute_pivot_levels",
    "derive_bar_metadata",
    "ema_alpha",
    "ema_seed",
    "ema_step",
    "evaluate_from_raw",
    "evaluate_spec",
    "evaluate_with_levels",
    "greeks",
    "minute_of_session",
    "resolve_near_month",
    "seconds_of_day",
    "spec_canonical_bytes",
    "stage1a_bar_bits_batch",
    "total_conditions",
    "version",
    "weekday_from_ordinal",
)


def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = _EXPECTED_COMPILED_SYMBOLS,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses any expected symbol.

    The teeth of the wheel-source freshness handshake: a symbol-incomplete
    (stale) compiled module fails HERE, at import, naming exactly what is
    missing and how to fix it — never a confusing ``ImportError: cannot import
    name ...`` or an ``AttributeError`` deep inside a consumer.
    """
    missing = sorted(name for name in expected if not hasattr(compiled, name))
    if missing:
        location = getattr(compiled, "__file__", "<unknown location>")
        raise ImportError(
            "trading_core: the installed compiled wheel "
            f"(trading_core._trading_core at {location}) is STALE — it is missing "
            f"symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/trading-core "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._trading_core", __name__)
_assert_compiled_surface(_compiled)

# Version-SENSITIVE staleness teeth (the 0.4.0 vocabulary reshape): the
# missing-symbol handshake above is one-directional — a STALE 0.3.x wheel
# carries every 0.4.0 symbol EXCEPT the new ones, but it ALSO still exposes
# the deleted ``VwapState`` and would otherwise die later as a bare
# ``TypeError`` on the narrowed evaluate ABIs. Refuse it loudly here.
if hasattr(_compiled, "VwapState"):
    raise ImportError(
        "trading_core: the installed compiled wheel "
        f"(trading_core._trading_core at {getattr(_compiled, '__file__', '<unknown location>')}) "
        "is STALE — it still exposes the DELETED VwapState (removed with the "
        "bits 52-53 VWAP tombstone in crate 0.4.0), so its evaluate ABIs "
        "predate the reshape. Rebuild + reinstall the wheel from the current "
        "source: run `maturin develop --release` inside rust/trading-core "
        "(with the project venv active)."
    )

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._trading_core import (  # noqa: E402  # type: ignore[import-not-found]
    FIB_LEVELS_LEN,
    GREEKS_DELTA_ADMISSION_FLOOR,
    GREEKS_IV_CLAMP_MAX,
    GREEKS_IV_CLAMP_MIN,
    GREEKS_T_FLOOR_YEARS,
    GREEKS_VEGA_ADMISSION_FLOOR,
    IMPLEMENTED_MASK,
    NARROW_CPR_WIDTH_BPS,
    PIVOT_LEVELS_LEN,
    SPEC_SCHEMA_VERSION,
    TOTAL_CONDITIONS,
    MarketStructureState,
    __version__,
    compute_ema,
    compute_fib_levels,
    compute_pivot_levels,
    derive_bar_metadata,
    ema_alpha,
    ema_seed,
    ema_step,
    evaluate_from_raw,
    evaluate_spec,
    evaluate_with_levels,
    greeks,
    minute_of_session,
    resolve_near_month,
    seconds_of_day,
    spec_canonical_bytes,
    stage1a_bar_bits_batch,
    total_conditions,
    version,
    weekday_from_ordinal,
)

#: Crate version of the INSTALLED compiled kernel — derived at import from the
#: compiled module's ``__version__`` (``env!("CARGO_PKG_VERSION")`` baked into
#: the ``.so`` at build time), NEVER hardcoded here. ``brutex doctor``'s
#: wheel-freshness stage compares this against the in-repo
#: ``EXPECTED_BUILD.json`` fingerprint.
#:
#: Deliberately NOT in ``__all__``: it is a build stamp, not kernel API. Both
#: consumers (``brutex.doctor.check_wheel_freshness`` and the golden-fixture
#: generator) read it via plain attribute access, which needs no ``__all__``
#: membership — and ``brutex.math`` rebuilds the merged two-kernel facade's
#: frozen public surface from each kernel's ``__all__``, so listing a
#: trading-core-specific stamp there would leak it into (and break) the frozen
#: facade golden (``tests/golden/test_math_facade_golden.py``).
__build_crate_version__: str = __version__

__all__ = [
    "FIB_LEVELS_LEN",
    "GREEKS_DELTA_ADMISSION_FLOOR",
    "GREEKS_IV_CLAMP_MAX",
    "GREEKS_IV_CLAMP_MIN",
    "GREEKS_T_FLOOR_YEARS",
    "GREEKS_VEGA_ADMISSION_FLOOR",
    "IMPLEMENTED_MASK",
    "NARROW_CPR_WIDTH_BPS",
    "PIVOT_LEVELS_LEN",
    "SPEC_SCHEMA_VERSION",
    "TOTAL_CONDITIONS",
    "MarketStructureState",
    "__version__",
    "compute_ema",
    "compute_fib_levels",
    "compute_pivot_levels",
    "derive_bar_metadata",
    "ema_alpha",
    "ema_seed",
    "ema_step",
    "evaluate_from_raw",
    "evaluate_spec",
    "evaluate_with_levels",
    "greeks",
    "minute_of_session",
    "resolve_near_month",
    "seconds_of_day",
    "spec_canonical_bytes",
    "stage1a_bar_bits_batch",
    "total_conditions",
    "version",
    "weekday_from_ordinal",
]
